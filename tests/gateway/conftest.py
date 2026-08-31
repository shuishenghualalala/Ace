"""tests/gateway 共享测试 helper。"""

from __future__ import annotations

import json
from pathlib import Path

from crew.browser.manager import BrowserManager, _Owner, _Session, _Tab  # noqa: SLF001 - 白盒播种内部图
from crew.browser.types import BrowserConfig


class FakeTargetedDriver:
    """只实现 execute_targeted 的假 driver：按 payload 回 Host 形状的 eval 结果。"""

    def __init__(self, payload: dict | None = None, error: Exception | None = None) -> None:
        self._payload = payload
        self._error = error
        self.calls: list[dict] = []

    async def execute_targeted(
        self,
        runtime_key,
        profile_dir,
        command,
        args,
        *,
        target_id,
        timeout=None,
        proxy_url="",
        download_dir=None,
        mutating=False,
    ):
        self.calls.append({
            "runtime_key": runtime_key,
            "command": command,
            "args": list(args),
            "target_id": target_id,
            "mutating": mutating,
        })
        if self._error is not None:
            raise self._error
        return {
            "success": True,
            "data": {
                "value": self._payload,
                "serialized": json.dumps(self._payload, ensure_ascii=False),
            },
        }


def make_browser_manager(
    payload: dict | None = None,
    *,
    mode: str = "ai",
    tabs: tuple[str, ...] = ("s0123-1",),
    driver_error: Exception | None = None,
) -> BrowserManager:
    """真实 BrowserManager + FakeTargetedDriver，白盒预置 owner/session/tab 图。

    相比 SimpleNamespace 镜像 manager 私有结构，用真实内部类构造：manager 内部
    结构变化时测试在 setup 阶段就响亮失败，而不是用假形状悄悄跑偏。
    """
    manager = BrowserManager(BrowserConfig(), FakeTargetedDriver(payload, driver_error))
    session = _Session(session_id="session", owner="dev:dev", mode=mode)
    for tab_id in tabs:
        session.tabs[tab_id] = _Tab(
            id=tab_id,
            label=tab_id,
            target_id=f"target-{tab_id}",
            url="https://example.com/p",
            title="标题",
        )
    owner = _Owner(
        owner="dev:dev",
        runtime_key="crew_0123456789ab",
        profile_dir=Path("/tmp/profile"),
    )
    owner.sessions["session"] = session
    manager._owners["dev:dev"] = owner  # noqa: SLF001 - 白盒播种
    return manager
