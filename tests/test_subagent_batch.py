"""阶段 B 批量调度器与同构模板测试（爬坡 / 限流退避 / 定序聚合 / 模板展开）。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from crew.agent.subagent.runtime import (
    BatchSchedulePolicy,
    SubagentRuntime,
    looks_rate_limited,
)
from crew.agent.subagent.service import (
    StartRequest,
    SubagentResult,
    SubagentRun,
)
from crew.agent.subagent.tools import _expand_template_items
from crew.app import build_app
from crew.core.errors import ToolError
from crew.core.types import ToolCall
from crew.state.config import Config


class FakeClock:
    """假时钟：sleep 即推进，不等待真实时间。"""

    def __init__(self) -> None:
        self.now = 0.0

    def now_fn(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.now += max(0.0, delay)
        await asyncio.sleep(0)


class ScriptedProvider:
    """按 label 脚本化结局的 provider；记录每次 start 的假时钟时刻。"""

    inherits_parent_context = False

    def __init__(
        self,
        *,
        rate_limit_labels: set[str] | None = None,
        rate_limit_times: int = 1,
        delay_labels: set[str] | None = None,
        delay_seconds: float = 0.0,
    ) -> None:
        from crew.agent.subagent.service import SubagentCapabilities

        self.name = "scripted"
        self.capabilities = SubagentCapabilities()
        self._rate_limit_labels = rate_limit_labels or set()
        self._rate_limit_times = rate_limit_times
        self._delay_labels = delay_labels or set()
        self._delay = delay_seconds
        self.launch_times: list[float] = []
        self.launch_counts: dict[str, int] = {}
        self._clock: FakeClock | None = None

    def bind_clock(self, clock: FakeClock) -> "ScriptedProvider":
        self._clock = clock
        return self

    async def start(self, request: Any) -> SubagentRun:
        label = request.label
        count = self.launch_counts.get(label, 0)
        self.launch_counts[label] = count + 1
        if self._clock is not None:
            self.launch_times.append(self._clock.now)
        if label in self._delay_labels:
            await asyncio.sleep(self._delay)

        async def outcome() -> SubagentResult:
            if (
                label in self._rate_limit_labels
                and self.launch_counts[label] <= self._rate_limit_times
            ):
                return SubagentResult(
                    stop_reason="error",
                    diagnostic="provider error 429: too many requests",
                )
            return SubagentResult(output=f"done:{label}")

        return SubagentRun(f"run::{label}::{count}", outcome())


def _items(labels: list[str]):
    return [
        (
            StartRequest(label=label, goal_text=f"work {label}", provider="scripted"),
            None,
        )
        for label in labels
    ]


@pytest.mark.asyncio
async def test_batch_launch_ramp_first_burst_then_one_per_interval():
    clock = FakeClock()
    provider = ScriptedProvider().bind_clock(clock)
    runtime = SubagentRuntime(provider)
    policy = BatchSchedulePolicy(
        max_concurrent=3, initial_burst=2, launch_interval=1.0
    )

    results = await runtime.execute_batch(
        _items(["a", "b", "c", "d"]),
        policy=policy,
        now_fn=clock.now_fn,
        sleep=clock.sleep,
    )

    assert [r.output for r in results] == ["done:a", "done:b", "done:c", "done:d"]
    # 首发 2 个在 t=0，此后每 1.0s 放 1 个
    assert provider.launch_times == [0.0, 0.0, 1.0, 2.0]


@pytest.mark.asyncio
async def test_batch_results_are_index_ordered_regardless_of_completion():
    provider = ScriptedProvider(delay_labels={"a"}, delay_seconds=0.05)
    runtime = SubagentRuntime(provider)
    policy = BatchSchedulePolicy(max_concurrent=4, initial_burst=8)

    results = await runtime.execute_batch(
        _items(["a", "b", "c"]),
        policy=policy,
    )
    assert [r.stop_reason for r in results] == ["completed"] * 3
    assert [r.output for r in results] == ["done:a", "done:b", "done:c"]


@pytest.mark.asyncio
async def test_batch_rate_limit_requeues_with_backoff_and_settles():
    clock = FakeClock()
    provider = ScriptedProvider(
        rate_limit_labels={"b"}, rate_limit_times=1
    ).bind_clock(clock)
    runtime = SubagentRuntime(provider)
    policy = BatchSchedulePolicy(
        max_concurrent=3,
        initial_burst=8,
        backoff_base=2.0,
        backoff_factor=2.0,
        max_retries=5,
    )

    results = await runtime.execute_batch(
        _items(["a", "b", "c"]),
        policy=policy,
        now_fn=clock.now_fn,
        sleep=clock.sleep,
    )

    # b 第一次被限流后按指数退避重排，第二次成功；其余不受影响
    assert provider.launch_counts["b"] == 2
    assert [r.output for r in results] == ["done:a", "done:b", "done:c"]
    first_b = provider.launch_times[1]
    retried_b = provider.launch_times[-1]
    assert retried_b - first_b >= 2.0


@pytest.mark.asyncio
async def test_batch_rate_limit_only_unfinished_slot_fails_fast():
    provider = ScriptedProvider(rate_limit_labels={"solo"}, rate_limit_times=99)
    runtime = SubagentRuntime(provider)
    policy = BatchSchedulePolicy(
        max_concurrent=2, initial_burst=8, max_retries=50, backoff_base=0.01
    )

    results = await runtime.execute_batch(
        _items(["solo"]),
        policy=policy,
    )
    # 唯一未完成槽不再无限重排：立即按失败落槽
    assert provider.launch_counts["solo"] == 1
    assert results[0].stop_reason == "error"
    assert looks_rate_limited(results[0].diagnostic)


@pytest.mark.asyncio
async def test_batch_rate_limit_retry_cap_is_respected():
    provider = ScriptedProvider(
        rate_limit_labels={"a", "b"}, rate_limit_times=99
    )
    runtime = SubagentRuntime(provider)
    policy = BatchSchedulePolicy(
        max_concurrent=2,
        initial_burst=8,
        backoff_base=0.001,
        max_retries=1,
    )

    results = await runtime.execute_batch(
        _items(["a", "b"]),
        policy=policy,
    )
    # max_retries=1 → 首次尝试 + 1 次重试后按失败落槽
    assert provider.launch_counts["a"] == 2
    assert provider.launch_counts["b"] == 2
    assert all(r.stop_reason == "error" for r in results)


@pytest.mark.asyncio
async def test_batch_cancel_propagates_and_cancels_in_flight():
    provider = ScriptedProvider(delay_labels={"a"}, delay_seconds=5.0)
    runtime = SubagentRuntime(provider)
    policy = BatchSchedulePolicy(max_concurrent=1, initial_burst=8)

    task = asyncio.ensure_future(
        runtime.execute_batch(_items(["a", "b"]), policy=policy)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_looks_rate_limited_classification():
    assert looks_rate_limited("HTTP 429 too many requests")
    assert looks_rate_limited("Rate limit exceeded for model")
    assert looks_rate_limited("provider rate_limit")
    assert not looks_rate_limited("connection reset by peer")
    # 配额耗尽是终态，不当作瞬时限流
    assert not looks_rate_limited("API quota exceeded for billing")
    assert not looks_rate_limited("")


def test_template_expansion_validation():
    expanded = _expand_template_items(
        "审查文件 {item} 的类型问题", ["a.py", "b.py"], max_batch_items=8
    )
    assert expanded == ["审查文件 a.py 的类型问题", "审查文件 b.py 的类型问题"]

    with pytest.raises(ToolError, match=r"\{item\}"):
        _expand_template_items("没有占位符", ["a"], max_batch_items=8)
    with pytest.raises(ToolError, match="非空字符串数组"):
        _expand_template_items("go {item}", [], max_batch_items=8)
    with pytest.raises(ToolError, match="上限"):
        _expand_template_items("go {item}", ["x"] * 9, max_batch_items=8)
    with pytest.raises(ToolError, match="重复"):
        _expand_template_items("相同任务 {item}", ["x", "x"], max_batch_items=8)


async def test_delegate_task_template_mode_end_to_end():
    app = build_app(config=Config(max_iterations=5))
    result = await app.registry.execute(
        ToolCall(
            "t1",
            "delegate_task",
            {
                "prompt_template": "用一句话说明 {item} 的用途",
                "items": ["pytest", "ruff"],
            },
        )
    )
    assert not result.is_error
    payload = json.loads(result.content)
    outputs = payload["results"]
    assert len(outputs) == 2
    assert all(entry["status"] == "completed" for entry in outputs)


async def test_delegate_task_template_rejects_conflicting_modes():
    app = build_app(config=Config(max_iterations=5))
    result = await app.registry.execute(
        ToolCall(
            "t1",
            "delegate_task",
            {
                "prompt_template": "处理 {item}",
                "items": ["a"],
                "goal": "互斥",
            },
        )
    )
    assert result.is_error
    assert "互斥" in result.content
