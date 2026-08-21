"""BrowserDriver backed by Electron's bundled Chromium and WebContentsView."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Sequence

from crew.browser.driver import (
    BrowserDriver,
    BrowserDriverError,
    BrowserOperationCancelled,
    _safe_browser_error,
)
from crew.browser.electron_bridge import (
    ElectronBridgeCancelled,
    ElectronBridgeError,
    electron_browser_bridge,
)
from crew.browser.types import BrowserConfig

_HOST_RESPONSE_GRACE_SECONDS = 2.0


class ElectronBrowserDriver(BrowserDriver):
    """Translate the existing deterministic driver contract into desktop RPC.

    The compatibility-shaped ``execute`` method lets BrowserManager retain its
    approval, ref-generation and session-isolation logic while the implementation
    uses Electron's sandboxed WebContentsView and bundled Chromium directly.
    """

    def __init__(self, config: BrowserConfig) -> None:
        self.config = config

    def requires_policy_proxy(self) -> bool:
        return True

    @staticmethod
    def _raise(exc: ElectronBridgeError) -> None:
        code = str(getattr(exc, "code", "") or "")
        error = BrowserDriverError(
            _safe_browser_error(exc),
            uncertain=exc.uncertain,
            browser_stopped=exc.browser_stopped,
            stop_unconfirmed=exc.stop_unconfirmed,
            code=code,
            phase=getattr(exc, "phase", ""),
            partial=getattr(exc, "partial", False),
            completed_count=getattr(exc, "completed_count", 0),
        )
        if code in {"dialog_pending", "file_chooser_pending"}:
            error.next_state = {  # type: ignore[attr-defined]
                "status": "blocked",
                "recoverable": True,
                "reason": code,
                "retry_original_action": False,
            }
        raise error from None

    async def _request(
        self,
        owner_session: str,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None,
        mutating: bool = False,
        retry_readonly: bool = False,
        allow_unready: bool = False,
    ) -> Any:
        try:
            return await electron_browser_bridge.request(
                owner_session,
                method,
                params,
                timeout=timeout or self.config.command_timeout_seconds,
                mutating=mutating,
                retry_readonly=retry_readonly,
                _allow_unready=allow_unready,
            )
        except ElectronBridgeCancelled as exc:
            raise BrowserOperationCancelled(
                _safe_browser_error(exc, fallback="浏览器操作已被用户取消"),
                uncertain=exc.uncertain,
                browser_stopped=exc.browser_stopped,
                stop_unconfirmed=exc.stop_unconfirmed,
            ) from None
        except ElectronBridgeError as exc:
            self._raise(exc)
        raise AssertionError("unreachable")

    @staticmethod
    def _provably_readonly(command: str, args: Sequence[str]) -> bool:
        command = str(command or "")
        values = [str(item) for item in args]
        if command in {"snapshot", "find", "get"}:
            return True
        if command == "tab":
            return values == ["list"]
        if command == "dialog":
            return values == ["status"]
        if command == "console":
            return "--clear" not in values
        if command == "network":
            return bool(values and values[0] == "requests" and "--clear" not in values)
        if command in {"network_requests", "network_request"}:
            return True
        return False

    async def prepare(self) -> bool:
        # The Chromium runtime is already part of Electron.  A desktop host may
        # connect after the gateway starts, especially under UOS/systemd.
        return bool(self.config.enabled)

    async def configure_proxy(
        self,
        owner_session: str,
        profile_dir: Path,
        endpoint_url: str,
        credentials: tuple[str, str],
    ) -> None:
        """Install proxy credentials through structured RPC, never URL userinfo."""

        username, password = credentials
        await self._request(
            owner_session,
            "configure_proxy",
            {
                "profile_dir": str(profile_dir.resolve()),
                "proxy_url": str(endpoint_url),
                "proxy_username": str(username),
                "proxy_password": str(password),
            },
            timeout=self.config.command_timeout_seconds + _HOST_RESPONSE_GRACE_SECONDS,
            mutating=True,
        )

    async def execute(
        self,
        owner_session: str,
        profile_dir: Path,
        command: str,
        args: Sequence[str] = (),
        *,
        timeout: float | None = None,
        proxy_url: str = "",
        download_dir: Path | None = None,
        mutating: bool = False,
    ) -> dict[str, Any]:
        return await self._execute_rpc(
            owner_session,
            profile_dir,
            command,
            args,
            target_id="",
            timeout=timeout,
            proxy_url=proxy_url,
            download_dir=download_dir,
            mutating=mutating,
        )

    async def execute_targeted(
        self,
        owner_session: str,
        profile_dir: Path,
        command: str,
        args: Sequence[str] = (),
        *,
        target_id: str,
        timeout: float | None = None,
        proxy_url: str = "",
        download_dir: Path | None = None,
        mutating: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(target_id, str) or not target_id:
            raise BrowserDriverError("目标标签页无效", code="invalid_target")
        return await self._execute_rpc(
            owner_session,
            profile_dir,
            command,
            args,
            target_id=target_id,
            timeout=timeout,
            proxy_url=proxy_url,
            download_dir=download_dir,
            mutating=mutating,
        )

    async def capabilities(
        self,
        owner_session: str,
        profile_dir: Path,
        *,
        timeout: float | None = None,
        proxy_url: str = "",
        download_dir: Path | None = None,
    ) -> dict[str, Any]:
        """Read the Electron Host's versioned browser protocol contract."""
        del profile_dir, proxy_url, download_dir
        operation_timeout = (
            float(timeout)
            if timeout is not None
            else float(self.config.command_timeout_seconds)
        )
        result = await self._request(
            owner_session,
            "capabilities",
            {},
            timeout=operation_timeout + _HOST_RESPONSE_GRACE_SECONDS,
            mutating=False,
            retry_readonly=True,
        )
        if not isinstance(result, dict):
            raise BrowserDriverError(
                "桌面浏览器返回了无效能力声明",
                code="replay_v3_capabilities_invalid",
            )
        return result

    async def execute_transaction(
        self,
        owner_session: str,
        profile_dir: Path,
        transaction: dict[str, Any],
        *,
        timeout: float | None = None,
        proxy_url: str = "",
        download_dir: Path | None = None,
    ) -> dict[str, Any]:
        """Send one replay.v3 action/effect transaction to Electron."""
        if (
            not isinstance(transaction, dict)
            or any(
                key in {
                    "profile_dir",
                    "proxy_url",
                    "download_dir",
                    "max_transfer_bytes",
                }
                for key in transaction
            )
        ):
            raise BrowserDriverError(
                "原子回放事务字段冲突",
                code="replay_transaction_invalid",
            )
        operation_timeout = (
            float(timeout)
            if timeout is not None
            else float(self.config.command_timeout_seconds)
        )
        result = await self._request(
            owner_session,
            "execute_transaction",
            {
                "profile_dir": str(profile_dir.resolve()),
                **transaction,
                "proxy_url": proxy_url,
                "download_dir": (
                    str(download_dir.resolve()) if download_dir else ""
                ),
                "max_transfer_bytes": int(self.config.max_transfer_bytes),
            },
            timeout=operation_timeout + _HOST_RESPONSE_GRACE_SECONDS,
            mutating=True,
            retry_readonly=False,
        )
        if not isinstance(result, dict):
            raise BrowserDriverError(
                "桌面浏览器返回了无效原子回放结果",
                uncertain=True,
                partial=True,
                code="replay_transaction_response_invalid",
            )
        return result

    async def _execute_rpc(
        self,
        owner_session: str,
        profile_dir: Path,
        command: str,
        args: Sequence[str],
        *,
        target_id: str,
        timeout: float | None,
        proxy_url: str,
        download_dir: Path | None,
        mutating: bool,
    ) -> dict[str, Any]:
        # Caller annotations are advisory only. Any command outside the strict
        # read-only whitelist is a mutation for cancellation, uncertainty and
        # Host bookkeeping purposes.
        effective_mutating = bool(mutating or not self._provably_readonly(command, args))
        operation_timeout = (
            float(timeout)
            if timeout is not None
            else float(self.config.command_timeout_seconds)
        )
        deadline_ms = int(time.time() * 1000 + operation_timeout * 1000)
        result = await self._request(
            owner_session,
            "execute",
            {
                "profile_dir": str(profile_dir.resolve()),
                "command": str(command),
                "args": [str(item) for item in args],
                **({"target_id": target_id} if target_id else {}),
                "command_timeout_ms": max(1, int(operation_timeout * 1000)),
                "command_deadline_ms": deadline_ms,
                "proxy_url": proxy_url,
                "download_dir": str(download_dir.resolve()) if download_dir else "",
                "max_transfer_bytes": int(self.config.max_transfer_bytes),
                "mutating": effective_mutating,
            },
            timeout=operation_timeout + _HOST_RESPONSE_GRACE_SECONDS,
            mutating=effective_mutating,
            retry_readonly=not effective_mutating,
        )
        if not isinstance(result, dict):
            raise BrowserDriverError("桌面浏览器返回了无效结果")
        return result

    async def execute_with_dialogs(
        self,
        owner_session: str,
        profile_dir: Path,
        command: str,
        args: Sequence[str] = (),
        *,
        target_id: str,
        expected_dialogs: list[dict[str, Any]],
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
        proxy_url: str = "",
        download_dir: Path | None = None,
    ) -> dict[str, Any]:
        if not isinstance(target_id, str) or not target_id:
            raise BrowserDriverError("目标标签页无效", code="invalid_target")
        structured = dict(payload or {})
        if any(
            key
            in {
                "profile_dir",
                "command",
                "args",
                "target_id",
                "expected_dialogs",
                "command_timeout_ms",
                "command_deadline_ms",
                "proxy_url",
                "download_dir",
                "max_transfer_bytes",
                "mutating",
            }
            for key in structured
        ):
            raise BrowserDriverError(
                "原子对话框回放 payload 字段冲突",
                code="invalid_expected_dialogs",
            )
        operation_timeout = (
            float(timeout)
            if timeout is not None
            else float(self.config.command_timeout_seconds)
        )
        result = await self._request(
            owner_session,
            "execute",
            {
                "profile_dir": str(profile_dir.resolve()),
                "command": str(command),
                "args": [str(item) for item in args],
                "target_id": target_id,
                "expected_dialogs": [dict(item) for item in expected_dialogs],
                "command_timeout_ms": max(
                    1,
                    int(1000 * operation_timeout),
                ),
                "command_deadline_ms": int(
                    time.time() * 1000 + operation_timeout * 1000
                ),
                **structured,
                "proxy_url": proxy_url,
                "download_dir": str(download_dir.resolve()) if download_dir else "",
                "max_transfer_bytes": int(self.config.max_transfer_bytes),
                "mutating": True,
            },
            timeout=operation_timeout + _HOST_RESPONSE_GRACE_SECONDS,
            mutating=True,
            retry_readonly=False,
        )
        if not isinstance(result, dict):
            raise BrowserDriverError("桌面浏览器返回了无效原子对话框结果")
        return result

    async def fill_form(
        self,
        owner_session: str,
        profile_dir: Path,
        fields: list[dict[str, Any]],
        *,
        target_id: str,
        timeout: float,
        proxy_url: str = "",
        download_dir: Path | None = None,
    ) -> dict[str, Any]:
        # 与本文件其它动作方法一致：拒绝空/非法 targetId。宿主按不可伪造的
        # targetId 路由动作，空串会静默落到"当前活动标签页"，可能是另一个页面
        # ——批量填表填到错误的窗口是最难排查的一类故障。
        if not isinstance(target_id, str) or not target_id:
            raise BrowserDriverError("目标标签页无效", code="invalid_target")
        operation_timeout = float(timeout)
        result = await self._request(
            owner_session,
            "execute",
            {
                "profile_dir": str(profile_dir.resolve()),
                "command": "fill_form",
                "args": [],
                # Keep private values in a typed RPC field. They are never
                # rendered into argv, permission prompts or diagnostic text.
                "fields": fields,
                "target_id": str(target_id or ""),
                "command_timeout_ms": max(1, int(operation_timeout * 1000)),
                "command_deadline_ms": int(
                    time.time() * 1000 + operation_timeout * 1000
                ),
                "proxy_url": proxy_url,
                "download_dir": str(download_dir.resolve()) if download_dir else "",
                "max_transfer_bytes": int(self.config.max_transfer_bytes),
                "mutating": True,
            },
            timeout=operation_timeout + _HOST_RESPONSE_GRACE_SECONDS,
            mutating=True,
            retry_readonly=False,
        )
        if not isinstance(result, dict):
            raise BrowserDriverError("桌面浏览器返回了无效批量表单结果")
        return result

    async def upload_with_trigger(
        self,
        owner_session: str,
        profile_dir: Path,
        *,
        target_id: str,
        trigger_selector: str,
        input_selector: str,
        files: list[str],
        timeout: float,
        proxy_url: str = "",
        download_dir: Path | None = None,
    ) -> dict[str, Any]:
        if not isinstance(target_id, str) or not target_id:
            raise BrowserDriverError("目标标签页无效", code="invalid_target")
        operation_timeout = float(timeout)
        result = await self._request(
            owner_session,
            "execute",
            {
                "profile_dir": str(profile_dir.resolve()),
                "command": "upload_with_trigger",
                "args": [],
                "trigger_selector": trigger_selector,
                "input_selector": input_selector,
                "files": list(files),
                "target_id": target_id,
                "command_timeout_ms": max(1, int(operation_timeout * 1000)),
                "command_deadline_ms": int(
                    time.time() * 1000 + operation_timeout * 1000
                ),
                "proxy_url": proxy_url,
                "download_dir": str(download_dir.resolve()) if download_dir else "",
                "max_transfer_bytes": int(self.config.max_transfer_bytes),
                "mutating": True,
            },
            timeout=operation_timeout + _HOST_RESPONSE_GRACE_SECONDS,
            mutating=True,
            retry_readonly=False,
        )
        if not isinstance(result, dict):
            raise BrowserDriverError("桌面浏览器返回了无效原子上传结果")
        return result

    async def close(self, owner_session: str, profile_dir: Path) -> bool | None:
        result = await self._request(
            owner_session,
            "close_owner",
            {"profile_dir": str(profile_dir.resolve())},
            timeout=self.config.command_timeout_seconds,
            mutating=True,
        )
        return bool(result is True or (isinstance(result, dict) and result.get("closed") is True))

    async def interrupt(self, owner_session: str, profile_dir: Path) -> None:
        await self._request(
            owner_session,
            "close_owner",
            {"profile_dir": str(profile_dir.resolve()), "interrupt": True},
            timeout=self.config.command_timeout_seconds,
            mutating=True,
        )

    async def deny_downloads(
        self,
        owner_session: str,
        profile_dir: Path,
        *,
        proxy_url: str = "",
        download_dir: Path | None = None,
    ) -> None:
        await self._request(
            owner_session,
            "deny_downloads",
            {
                "profile_dir": str(profile_dir.resolve()),
                "proxy_url": proxy_url,
                "download_dir": str(download_dir.resolve()) if download_dir else "",
            },
            timeout=self.config.command_timeout_seconds,
            mutating=True,
        )

    async def clear_owner_data(
        self,
        owner_session: str,
        profile_dir: Path,
        *,
        timeout: float,
        proxy_url: str = "",
        download_dir: Path | None = None,
    ) -> bool | None:
        common = {
            "profile_dir": str(profile_dir.resolve()),
            "proxy_url": proxy_url,
            "download_dir": str(download_dir.resolve()) if download_dir else "",
        }
        # clear_owner_data intentionally requires an existing Electron owner.
        # A read-only tab list creates the owner without creating a page, which
        # keeps clearing an inactive account idempotent.
        await self._request(
            owner_session,
            "execute",
            {
                **common,
                "command": "tab",
                "args": ["list"],
                "mutating": False,
            },
            timeout=timeout,
            retry_readonly=True,
        )
        result = await self._request(
            owner_session,
            "clear_owner_data",
            {"profile_dir": str(profile_dir.resolve())},
            timeout=timeout,
            mutating=True,
        )
        return bool(
            result is True or (isinstance(result, dict) and result.get("cleared") is True)
        )

    async def page_guard(
        self,
        owner_session: str,
        profile_dir: Path,
        *,
        target_id: str,
        state_key: str,
        state_token: str,
        reset: bool,
        timeout: float,
        include_security: bool = False,
        proxy_url: str = "",
        download_dir: Path | None = None,
    ) -> str | None:
        result = await self._request(
            owner_session,
            "page_guard",
            {
                "profile_dir": str(profile_dir.resolve()),
                "target_id": target_id,
                "state_key": state_key,
                "state_token": state_token,
                "reset": bool(reset),
                "include_security": bool(include_security),
                "command_timeout_ms": max(1, int(float(timeout) * 1000)),
                "proxy_url": proxy_url,
                "download_dir": str(download_dir.resolve()) if download_dir else "",
            },
            timeout=timeout,
            mutating=bool(reset),
            retry_readonly=not reset,
        )
        return str(result) if result is not None else None

    async def page_images(
        self,
        owner_session: str,
        profile_dir: Path,
        *,
        target_id: str,
        timeout: float,
        proxy_url: str = "",
        download_dir: Path | None = None,
    ) -> list[dict[str, str]]:
        result = await self._request(
            owner_session,
            "page_images",
            {
                "profile_dir": str(profile_dir.resolve()),
                "target_id": target_id,
                "command_timeout_ms": max(1, int(float(timeout) * 1000)),
                "proxy_url": proxy_url,
                "download_dir": str(download_dir.resolve()) if download_dir else "",
            },
            timeout=timeout,
            retry_readonly=True,
        )
        if not isinstance(result, list):
            raise BrowserDriverError("桌面浏览器返回了无效图片列表")
        return [item for item in result if isinstance(item, dict)]

    async def close_target(
        self,
        owner_session: str,
        profile_dir: Path,
        *,
        target_id: str,
        timeout: float,
        proxy_url: str = "",
        download_dir: Path | None = None,
    ) -> None:
        await self._request(
            owner_session,
            "close_target",
            {
                "profile_dir": str(profile_dir.resolve()),
                "target_id": target_id,
            },
            timeout=timeout,
            mutating=True,
        )

    async def coordinate_click_atomic(
        self,
        owner_session: str,
        profile_dir: Path,
        *,
        target_id: str,
        x: int,
        y: int,
        timeout: float,
        proxy_url: str = "",
        download_dir: Path | None = None,
        expected_epoch: str = "",
    ) -> dict[str, Any] | None:
        operation_timeout = float(timeout)
        deadline_ms = int(time.time() * 1000 + operation_timeout * 1000)
        result = await self._request(
            owner_session,
            "coordinate_click",
            {
                "profile_dir": str(profile_dir.resolve()),
                "target_id": target_id,
                "x": int(x),
                "y": int(y),
                "proxy_url": proxy_url,
                "download_dir": str(download_dir.resolve()) if download_dir else "",
                "expected_epoch": expected_epoch,
                "command_timeout_ms": max(1, int(operation_timeout * 1000)),
                "command_deadline_ms": deadline_ms,
            },
            timeout=operation_timeout + _HOST_RESPONSE_GRACE_SECONDS,
            mutating=True,
        )
        if not isinstance(result, dict):
            raise BrowserDriverError("桌面浏览器返回了无效坐标点击结果")
        return result

    async def download_bounded(
        self,
        owner_session: str,
        profile_dir: Path,
        native_ref: str,
        target: Path,
        *,
        target_id: str = "",
        max_bytes: int,
        timeout: float,
        proxy_url: str = "",
        download_dir: Path | None = None,
    ) -> dict[str, Any]:
        operation_timeout = float(timeout)
        deadline_ms = int(time.time() * 1000 + operation_timeout * 1000)
        result = await self._request(
            owner_session,
            "download",
            {
                "profile_dir": str(profile_dir.resolve()),
                "target_id": target_id,
                "ref": native_ref,
                "target": str(target.resolve()),
                "max_bytes": int(max_bytes),
                "timeout_ms": max(1, int(operation_timeout * 1000)),
                "command_timeout_ms": max(1, int(operation_timeout * 1000)),
                "command_deadline_ms": deadline_ms,
                "proxy_url": proxy_url,
                "download_dir": str(download_dir.resolve()) if download_dir else "",
            },
            timeout=operation_timeout + _HOST_RESPONSE_GRACE_SECONDS,
            mutating=True,
        )
        if not isinstance(result, dict):
            raise BrowserDriverError("桌面浏览器返回了无效下载结果")
        return result

    async def set_mode(
        self,
        owner_session: str,
        profile_dir: Path,
        *,
        target_id: str,
        mode: str,
    ) -> None:
        await self._request(
            owner_session,
            "set_mode",
            {
                "profile_dir": str(profile_dir.resolve()),
                "target_id": target_id,
                "mode": mode,
            },
            timeout=self.config.command_timeout_seconds,
            mutating=True,
        )

    async def set_recording(
        self,
        owner_session: str,
        profile_dir: Path,
        *,
        target_id: str,
        action: str,
        recording_id: str = "",
    ) -> dict:
        """录制开关。action ∈ start | pause | resume | stop。"""
        operation_timeout = float(self.config.command_timeout_seconds)
        deadline_ms = int(time.time() * 1000 + operation_timeout * 1000)
        return await self._request(
            owner_session,
            "set_recording",
            {
                "profile_dir": str(profile_dir.resolve()),
                "target_id": target_id,
                "action": action,
                "recording_id": recording_id,
                "command_timeout_ms": max(
                    1,
                    int(operation_timeout * 1000),
                ),
                "command_deadline_ms": deadline_ms,
            },
            timeout=operation_timeout + _HOST_RESPONSE_GRACE_SECONDS,
            mutating=True,
        )

    def available(self) -> bool:
        # Availability is dynamic.  Keep the tool registered so starting the
        # desktop later can satisfy it; execute() still fails closed without a
        # host for the current account.
        return bool(self.config.enabled)

    def availability_error(self) -> str:
        if electron_browser_bridge.connected():
            return ""
        return "桌面内置浏览器尚未连接"


def runtime_doctor(config: BrowserConfig, runtime_key: str = "") -> dict[str, Any]:
    connected = electron_browser_bridge.connected(runtime_key or None)
    return {
        "ok": bool(config.enabled and connected),
        "enabled": bool(config.enabled),
        "runtime": "electron",
        "engine": "WebContentsView",
        "host_connections": electron_browser_bridge.connected_count,
        "message": (
            "Electron 浏览器宿主已连接"
            if connected
            else "请打开 Crew 桌面应用并保持登录"
        ),
    }
