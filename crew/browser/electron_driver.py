"""BrowserDriver backed by Electron's bundled Chromium and WebContentsView."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from crew.browser.driver import BrowserDriver, BrowserDriverError, BrowserOperationCancelled
from crew.browser.electron_bridge import (
    ElectronBridgeCancelled,
    ElectronBridgeError,
    electron_browser_bridge,
)
from crew.browser.types import BrowserConfig


class ElectronBrowserDriver(BrowserDriver):
    """Translate the existing deterministic driver contract into desktop RPC.

    The compatibility-shaped ``execute`` method lets BrowserManager retain its
    approval, ref-generation and session-isolation logic while the implementation
    uses Electron's sandboxed WebContentsView and bundled Chromium directly.
    """

    def __init__(self, config: BrowserConfig) -> None:
        self.config = config

    @staticmethod
    def _raise(exc: ElectronBridgeError) -> None:
        raise BrowserDriverError(
            str(exc),
            uncertain=exc.uncertain,
            browser_stopped=exc.browser_stopped,
            stop_unconfirmed=exc.stop_unconfirmed,
            code=getattr(exc, "code", ""),
        ) from None

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
                str(exc),
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
        if command in {"snapshot", "get"}:
            return True
        if command == "tab":
            return values == ["list"]
        if command == "dialog":
            return values == ["status"]
        if command == "console":
            return "--clear" not in values
        if command == "network":
            return bool(values and values[0] == "requests" and "--clear" not in values)
        return False

    async def prepare(self) -> bool:
        # The Chromium runtime is already part of Electron.  A desktop host may
        # connect after the gateway starts, especially under UOS/systemd.
        return bool(self.config.enabled)

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
        # Caller annotations are advisory only. Any command outside the strict
        # read-only whitelist is a mutation for cancellation, uncertainty and
        # Host bookkeeping purposes.
        effective_mutating = bool(mutating or not self._provably_readonly(command, args))
        result = await self._request(
            owner_session,
            "execute",
            {
                "profile_dir": str(profile_dir.resolve()),
                "command": str(command),
                "args": [str(item) for item in args],
                "proxy_url": proxy_url,
                "download_dir": str(download_dir.resolve()) if download_dir else "",
                "mutating": effective_mutating,
            },
            timeout=timeout,
            mutating=effective_mutating,
            retry_readonly=not effective_mutating,
        )
        if not isinstance(result, dict):
            raise BrowserDriverError("桌面浏览器返回了无效结果")
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
        include_security: bool = True,
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
                "proxy_url": proxy_url,
                "download_dir": str(download_dir.resolve()) if download_dir else "",
            },
            timeout=timeout,
            retry_readonly=True,
        )
        if not isinstance(result, list):
            raise BrowserDriverError("桌面浏览器返回了无效图片列表")
        return [item for item in result if isinstance(item, dict)][:100]

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
        result = await self._request(
            owner_session,
            "coordinate_click",
            {
                "profile_dir": str(profile_dir.resolve()),
                "target_id": target_id,
                "x": int(x),
                "y": int(y),
                "proxy_url": proxy_url,
                "expected_epoch": expected_epoch,
            },
            timeout=timeout,
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
        result = await self._request(
            owner_session,
            "download",
            {
                "profile_dir": str(profile_dir.resolve()),
                "target_id": target_id,
                "ref": native_ref,
                "target": str(target.resolve()),
                "max_bytes": int(max_bytes),
                # The Host expires its grant before this bridge-side deadline,
                # leaving time for the error response to cross the WebSocket.
                "timeout_ms": max(1000, int(timeout * 1000) - 1000),
                "proxy_url": proxy_url,
                "download_dir": str(download_dir.resolve()) if download_dir else "",
            },
            timeout=timeout,
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
