"""Stable browser-driver contract used by BrowserManager.

Concrete drivers own the transport and browser host. Crew's production
implementation lives in :mod:`crew.browser.electron_driver` and delegates to
the authenticated Electron main-process browser host.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Sequence

from crew.core.errors import ToolError


class BrowserDriverError(ToolError):
    """Browser operation failure with lifecycle semantics for BrowserManager."""

    def __init__(
        self,
        message: str,
        *,
        uncertain: bool = False,
        browser_stopped: bool = False,
        stop_unconfirmed: bool = False,
        code: str = "",
        phase: str = "",
        partial: bool = False,
        completed_count: int = 0,
    ) -> None:
        super().__init__(message)
        self.uncertain = uncertain
        self.browser_stopped = browser_stopped
        self.stop_unconfirmed = stop_unconfirmed
        # 驱动/宿主给出的稳定错误码（如 stale_ref_security），供上层区分「ref 失效」
        # 这类可恢复失败与真正的故障；靠匹配错误文本区分太脆。
        self.code = code
        self.phase = phase
        self.partial = partial
        # Confirmed steps in a non-atomic compound action. The current
        # uncertain dispatch is deliberately excluded.
        self.completed_count = max(0, int(completed_count))


class BrowserOperationCancelled(asyncio.CancelledError):
    """Cancellation that retains the terminal state of a sent mutation.

    A sent mutation cannot be abandoned until the Host reports whether it ran.
    These lifecycle flags let BrowserManager fail-stop its local owner state
    while cancellation still wins at the agent boundary, so pressing Stop can
    never become a normal tool error followed by another LLM iteration.
    """

    def __init__(
        self,
        message: str = "浏览器操作已被用户取消",
        *,
        uncertain: bool = False,
        browser_stopped: bool = False,
        stop_unconfirmed: bool = False,
    ) -> None:
        super().__init__(message)
        self.uncertain = uncertain
        self.browser_stopped = browser_stopped
        self.stop_unconfirmed = stop_unconfirmed


class BrowserDriver(ABC):
    """Transport-neutral interface for Crew's deterministic browser actions."""

    async def prepare(self) -> bool:
        """Perform optional readiness work before the first browser action."""
        return self.available()

    @abstractmethod
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
    ) -> dict[str, Any]: ...

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
        """Execute against one exact target when the transport supports it.

        Alternative/test drivers retain source compatibility through this
        default delegation. Production Electron overrides it and binds the RPC
        to ``target_id`` so two sessions sharing one Host owner cannot race via
        whichever tab happened to be active.
        """
        return await self.execute(
            owner_session,
            profile_dir,
            command,
            args,
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
        """Return Browser Host protocol capabilities.

        Replay v3 deliberately has no compatibility emulation: an older
        driver cannot turn a sequence of ordinary commands into the Host's
        atomic action/effect transaction.  Concrete transports must therefore
        opt in explicitly.
        """
        del owner_session, profile_dir, timeout, proxy_url, download_dir
        raise BrowserDriverError(
            "浏览器宿主不支持原子 replay.v3",
            code="replay_v3_unsupported",
        )

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
        """Execute one replay action with its effects armed atomically.

        This method must never be implemented by issuing multiple ``execute``
        calls.  Doing so loses popup/dialog/download/page-close events between
        the action and the later observer and makes same-URL sibling pages
        ambiguous.
        """
        del (
            owner_session,
            profile_dir,
            transaction,
            timeout,
            proxy_url,
            download_dir,
        )
        raise BrowserDriverError(
            "浏览器宿主不支持原子 replay.v3",
            code="replay_v3_unsupported",
        )

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
        """Run one mutation with an atomically pre-armed dialog sequence.

        A compatibility driver cannot emulate this by issuing a later
        ``dialog`` command: the triggering Playwright action is blocked until
        the modal is handled. Concrete drivers must carry ``expected_dialogs``
        in the same transport request as the mutation.
        """
        raise BrowserDriverError(
            "当前浏览器驱动不支持原子 JavaScript 对话框回放",
            code="expected_dialogs_unsupported",
        )

    @abstractmethod
    async def close(self, owner_session: str, profile_dir: Path) -> bool | None: ...

    async def interrupt(self, owner_session: str, profile_dir: Path) -> None:
        """Best-effort emergency stop for drivers without a stronger primitive."""
        await self.close(owner_session, profile_dir)

    async def deny_downloads(
        self,
        owner_session: str,
        profile_dir: Path,
        *,
        proxy_url: str = "",
        download_dir: Path | None = None,
    ) -> None:
        """Deny incidental page downloads when supported by the driver."""
        return None

    async def clear_owner_data(
        self,
        owner_session: str,
        profile_dir: Path,
        *,
        timeout: float,
        proxy_url: str = "",
        download_dir: Path | None = None,
    ) -> bool | None:
        """Clear browser-engine storage before closing one owner.

        Alternative drivers may leave this as a no-op. The Electron driver
        overrides it so its in-memory Session clears persistent data and
        releases connections before BrowserManager closes the owner.
        """
        return None

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
        """Read or reset a private page marker when the driver supports one."""
        return None

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
        """Read image metadata without exposing arbitrary page JavaScript."""
        raise BrowserDriverError("当前浏览器驱动不支持隔离的图片元数据读取")

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
        """Close one exact browser target when supported by the driver."""
        raise BrowserDriverError("当前浏览器驱动不支持按 targetId 关闭标签页")

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
        """Atomically hit-test and click one CSS point when supported.

        ``None`` means unsupported and permits BrowserManager's compatibility
        path. Implementations must return only after mouse-up or a classified
        failure; a failure after mouse-down must be marked uncertain. Production
        hosts must bind ``expected_epoch`` to the exact screenshot frame and
        consume it once before dispatching input.
        """
        return None

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
        """Execute one typed, non-atomic form batch.

        The structured field payload must never be flattened into command-line
        arguments, logs or error text. The native implementation resolves each
        exact ref immediately before its field action, runs official Playwright
        actions in caller order and reports confirmed completion.
        """
        raise BrowserDriverError(
            "当前浏览器驱动不支持原生批量表单",
            code="fill_form_unsupported",
        )

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
        """Atomically trigger and complete one recorded file upload.

        Selectors and up to 256 paths stay in typed RPC fields so the transport
        does not silently inherit the generic argv limit. Implementations must
        clear stale chooser state before the trigger and must not split the
        trigger click from chooser completion across calls.
        """
        raise BrowserDriverError(
            "当前浏览器驱动不支持原子文件上传",
            code="upload_with_trigger_unsupported",
        )

    async def set_mode(
        self,
        owner_session: str,
        profile_dir: Path,
        *,
        target_id: str,
        mode: str,
    ) -> None:
        """Switch between AI, human-takeover and paused input modes."""
        return None

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
        """Download one approved target using the common execute contract."""
        return await self.execute(
            owner_session,
            profile_dir,
            "download",
            [native_ref, str(target)],
            timeout=timeout,
            proxy_url=proxy_url,
            download_dir=download_dir,
            mutating=True,
        )

    @abstractmethod
    def available(self) -> bool: ...

    def availability_error(self) -> str:
        """Return a user-facing reason when the driver is unavailable."""
        return "" if self.available() else "内置浏览器不可用"
