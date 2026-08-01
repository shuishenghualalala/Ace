"""追问选择框（ask_followup_question）取消链路测试。

desktop 端「取消」按钮 → WS followup_cancel → 后端 cancel_followup → 工具 handler 识别取消标记。
本测试覆盖 desktop 依赖的后端契约：cancel 回灌取消标记、wait 正常返回、重复取消安全。
"""

import asyncio

import pytest

from crew.core.followup import CANCELLED_MARKER, FollowupWaiter


@pytest.mark.asyncio
async def test_cancel_resolves_with_marker():
    """cancel 后 wait 返回取消标记答案，而非抛 CancelledError。"""
    waiter = FollowupWaiter()
    session_id = "desktop-session"
    question_id = waiter.create(session_id)

    async def cancel_soon():
        await asyncio.sleep(0.01)
        assert waiter.cancel(session_id, question_id) is True

    canceller = asyncio.create_task(cancel_soon())
    answers = await waiter.wait(session_id, question_id, timeout=1.0)
    await canceller

    assert answers == [{"id": CANCELLED_MARKER, "answers": []}]


@pytest.mark.asyncio
async def test_cancel_is_idempotent_and_safe():
    """对已取消/不存在的追问再次取消，返回 False 不抛异常。"""
    waiter = FollowupWaiter()
    session_id = "desktop-session"
    question_id = waiter.create(session_id)

    assert waiter.cancel(session_id, question_id) is True
    # 已取消（Future 已被 pop）：再次取消返回 False
    assert waiter.cancel(session_id, question_id) is False
    # 从不存在的追问取消：返回 False
    assert waiter.cancel("no-such-session", "no-such-question") is False


@pytest.mark.asyncio
async def test_cancel_does_not_propagate_cancelled_error():
    """cancel 不会让 wait 抛 CancelledError 冒泡到调用方（保护 agent 主任务）。"""
    waiter = FollowupWaiter()
    session_id = "desktop-session"
    question_id = waiter.create(session_id)

    async def run():
        await asyncio.sleep(0.01)
        waiter.cancel(session_id, question_id)

    asyncio.create_task(run())
    # 若 cancel 用 future.cancel()，这里会抛 CancelledError；改造后应正常返回。
    answers = await waiter.wait(session_id, question_id, timeout=1.0)
    assert isinstance(answers, list)


@pytest.mark.asyncio
async def test_is_waiting_reflects_future_state():
    """is_waiting 应准确反映追问是否仍在等待（未回答/未取消/未超时）。"""
    waiter = FollowupWaiter()
    session_id = "desktop-session"
    question_id = waiter.create(session_id)

    assert waiter.is_waiting(session_id, question_id) is True

    # 取消后 future 被 pop，不再等待
    assert waiter.cancel(session_id, question_id) is True
    assert waiter.is_waiting(session_id, question_id) is False

    # 不存在的追问返回 False
    assert waiter.is_waiting("no-such-session", "no-such-question") is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
