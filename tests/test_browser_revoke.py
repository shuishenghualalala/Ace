"""BrowserManager 能力撤销（revoke_owner / capability generation）测试。"""

from __future__ import annotations

import asyncio

import pytest

from crew.browser.driver import BrowserDriverError
from crew.browser.manager import BrowserManager
from tests.test_browser_use import FakeBrowserDriver


@pytest.fixture
async def browser(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "crew.browser.manager.get_owner_runtime_home",
        lambda owner: tmp_path / "accounts" / str(owner),
    )
    driver = FakeBrowserDriver()
    from crew.browser.types import BrowserConfig

    manager = BrowserManager(BrowserConfig(), driver)
    await manager.startup()
    try:
        yield manager, driver
    finally:
        await manager.aclose()


async def test_revoke_without_owner_instance_bumps_generation(browser):
    manager, _driver = browser
    assert manager.capability_generation("owner-a") == 0
    await manager.revoke_owner("owner-a")
    assert manager.capability_generation("owner-a") == 1
    await manager.revoke_owner("owner-a")
    assert manager.capability_generation("owner-a") == 2


async def test_renew_capability_clears_page_observations_without_approval_state(browser):
    manager, _driver = browser
    await manager.navigate("owner-a", "session-a", "https://example.com")
    owner = manager._owners["owner-a"]
    session = owner.sessions["session-a"]
    assert session.refs
    tab_ids = set(session.tabs)

    decision = manager.permission_for(
        "browser_download",
        {"ref": "p1:e5"},
        "owner-a",
        "session-a",
    )
    assert decision is not None and decision.behavior == "ask"

    generation = manager.renew_capability("owner-a")

    assert generation == 1
    assert not session.refs
    assert session.screenshot_id == ""
    assert session.page_marker == ""
    assert set(session.tabs) == tab_ids


async def test_capability_runtime_state_reports_fail_closed_tombstones(browser):
    manager, _driver = browser
    assert manager.capability_runtime_state("owner-a") == {
        "ready": True,
        "closing": False,
        "actions_blocked": False,
        "stop_unconfirmed": False,
    }
    await manager.navigate("owner-a", "session-a", "https://example.com")
    owner = manager._owners["owner-a"]
    owner.closing = True
    owner.actions_blocked = True
    owner.stop_unconfirmed = True

    assert manager.capability_runtime_state("owner-a") == {
        "ready": False,
        "closing": True,
        "actions_blocked": True,
        "stop_unconfirmed": True,
    }


async def test_revoke_keeps_functional_build_free_of_approval_tokens(browser):
    manager, _driver = browser
    await manager.navigate("owner-a", "session-a", "https://example.com")
    await manager.navigate("owner-b", "session-a", "https://example.com/b")

    owner_a_decision = manager.permission_for(
        "browser_download", {"ref": "p1:e5"}, "owner-a", "session-a"
    )
    owner_b_decision = manager.permission_for(
        "browser_download", {"ref": "p1:e5"}, "owner-b", "session-a"
    )
    assert owner_a_decision is not None and owner_a_decision.behavior == "ask"
    assert owner_b_decision is not None and owner_b_decision.behavior == "ask"

    await manager.revoke_owner("owner-a")

    assert not hasattr(manager, "_pending_approvals")
    assert not hasattr(manager, "_granted_approvals")


async def test_revoke_blocks_actions_and_removes_owner(browser):
    manager, driver = browser
    await manager.navigate("owner-a", "session-a", "https://example.com")
    owner_before = manager._owners.get("owner-a")
    assert owner_before is not None

    await manager.revoke_owner("owner-a")

    assert owner_before.actions_blocked is True
    assert owner_before.closing is True
    assert "owner-a" not in manager._owners
    assert owner_before.closed_event.is_set()
    # 关闭已确认：driver.close 被调用、标签页被清空
    assert any(call[0] in {"close", "interrupt"} for call in driver.calls)
    assert not any(
        data.get("owner") == owner_before.runtime_key for data in driver.tabs.values()
    )


async def test_revoke_wakes_browser_subscriber_before_owner_teardown(browser):
    manager, _driver = browser
    await manager.navigate("owner-a", "session-a", "https://example.com")

    subscription = manager.subscribe("owner-a", "session-a")
    initial = await anext(subscription)
    assert initial["type"] == "state"

    await manager.revoke_owner("owner-a")
    terminal = await asyncio.wait_for(anext(subscription), 2)

    assert terminal == {
        "type": "owner_revoked",
        "code": 4401,
        "reason": "登录状态已失效",
    }
    await subscription.aclose()


async def test_revoked_owner_inflight_action_is_interrupted(browser):
    manager, _driver = browser
    await manager.navigate("owner-a", "session-a", "https://example.com")
    owner = manager._owners["owner-a"]
    # 模拟 revoke 先置 blocked（revoke_owner 的第一步语义）
    owner.actions_blocked = True
    owner.closing = True
    with pytest.raises(BrowserDriverError, match="已停止"):
        await manager._run(owner, owner.sessions["session-a"], "get", ["text"])


async def test_revoke_is_fail_stop_when_close_fails(browser, monkeypatch):
    manager, _driver = browser
    await manager.navigate("owner-a", "session-a", "https://example.com")
    owner = manager._owners["owner-a"]

    async def failing_close(target):
        raise RuntimeError("close boom")

    original_close = manager._close_owner
    monkeypatch.setattr(manager, "_close_owner", failing_close)
    # 不应向外炸出
    await manager.revoke_owner("owner-a")
    # fail-stop：保留同一 Profile 的墓碑，拒绝创建替代实例。
    assert owner.closing is True
    assert owner.actions_blocked is True
    assert manager._owners["owner-a"] is owner
    assert owner.stop_unconfirmed is True
    # 墓碑立起后必须唤醒 closed_event：在关闭窗口内已 await 该事件的等待者只有被
    # 唤醒才能重新拿锁、看到 stop_unconfirmed 并抛错，否则永久挂死（见下条回归）。
    assert owner.closed_event.is_set()
    assert manager.capability_generation("owner-a") == 1
    with pytest.raises(BrowserDriverError, match="Profile 已保持锁定"):
        await manager.snapshot("owner-a", "fresh-session")
    # fixture teardown 需要恢复真实 close，把保留的墓碑安全收掉。
    monkeypatch.setattr(manager, "_close_owner", original_close)


async def test_owner_waiter_wakes_when_revoke_close_fails(browser, monkeypatch):
    """关闭窗口内已 await closed_event 的 _owner() 等待者，必须在关闭失败后被唤醒
    并抛「关闭状态无法确认」，绝不能永久挂死（回归：revoke 异常路径漏 set 事件）。"""
    manager, _driver = browser
    await manager.navigate("owner-a", "session-a", "https://example.com")
    owner = manager._owners["owner-a"]

    slow_close_entered = asyncio.Event()
    release_close = asyncio.Event()

    async def failing_close(target):
        slow_close_entered.set()
        await release_close.wait()
        raise RuntimeError("close boom")

    original_close = manager._close_owner
    monkeypatch.setattr(manager, "_close_owner", failing_close)

    revoke_task = asyncio.create_task(manager.revoke_owner("owner-a"))
    await asyncio.wait_for(slow_close_entered.wait(), 2)

    # owner 正在关闭、但关闭失败尚未记录：并发动作抓走 closed_event 并停在其上。
    waiter = asyncio.create_task(manager.snapshot("owner-a", "session-b"))
    await asyncio.sleep(0.05)
    assert not waiter.done(), "等待者应当停在 closed_event 上"

    release_close.set()
    await revoke_task
    assert owner.stop_unconfirmed is True
    assert owner.closed_event.is_set()

    # 等待者被唤醒 → 重新拿锁 → 看到墓碑 → 抛干净错误，而不是无限挂起。
    with pytest.raises(BrowserDriverError, match="Profile 已保持锁定"):
        await asyncio.wait_for(waiter, 2)

    monkeypatch.setattr(manager, "_close_owner", original_close)


async def test_revoke_closes_cold_start_race_with_capability_lease(browser):
    manager, _driver = browser
    generation = manager.capability_generation("owner-a")
    started = asyncio.Event()
    resume = asyncio.Event()

    async def stale_action():
        with manager.capability_lease("owner-a", generation):
            manager.ensure_capability_current("owner-a", generation)
            started.set()
            await resume.wait()
            return await manager.navigate("owner-a", "session-a", "https://example.com")

    task = asyncio.create_task(stale_action())
    await started.wait()
    await manager.revoke_owner("owner-a")
    resume.set()

    with pytest.raises(BrowserDriverError, match="BROWSER_CAPABILITY_DISABLED"):
        await task
    assert "owner-a" not in manager._owners


async def test_cancelled_revoke_finishes_cleanup_before_releasing_cancellation(
    browser, monkeypatch
):
    manager, _driver = browser
    await manager.navigate("owner-a", "session-a", "https://example.com")
    owner = manager._owners["owner-a"]
    original_close = manager._close_owner
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    async def blocking_close(target):
        close_started.set()
        await release_close.wait()
        await original_close(target)

    monkeypatch.setattr(manager, "_close_owner", blocking_close)
    task = asyncio.create_task(manager.revoke_owner("owner-a"))
    await close_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    release_close.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "owner-a" not in manager._owners
    assert owner.closed_event.is_set()
    assert owner.stop_unconfirmed is False


async def test_cancelled_revoke_waiting_for_owner_lock_still_fences_and_cleans(browser):
    manager, _driver = browser
    await manager.navigate("owner-a", "session-a", "https://example.com")
    owner = manager._owners["owner-a"]
    await manager._owners_lock.acquire()
    try:
        task = asyncio.create_task(manager.revoke_owner("owner-a"))
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert manager.capability_generation("owner-a") == 0
    finally:
        manager._owners_lock.release()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert manager.capability_generation("owner-a") == 1
    assert "owner-a" not in manager._owners
    assert owner.closed_event.is_set()


async def test_revoke_does_not_affect_other_owners(browser):
    manager, _driver = browser
    await manager.navigate("owner-a", "session-a", "https://example.com/a")
    await manager.navigate("owner-b", "session-a", "https://example.com/b")

    await manager.revoke_owner("owner-a")

    assert manager.capability_generation("owner-b") == 0
    assert "owner-b" in manager._owners
    # owner-b 仍可执行动作
    result = await manager.snapshot("owner-b", "session-a")
    assert result


async def test_ensure_capability_current(browser):
    manager, _driver = browser
    manager.ensure_capability_current("owner-a", 0)
    await manager.revoke_owner("owner-a")
    with pytest.raises(BrowserDriverError, match="BROWSER_CAPABILITY_DISABLED"):
        manager.ensure_capability_current("owner-a", 0)
    # 新代次可匹配
    manager.ensure_capability_current("owner-a", 1)
