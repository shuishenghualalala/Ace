"""阶段 B 语义测试：结局折叠、深度账本、四态投递、摘要兜底、结算落库。"""

from __future__ import annotations

from typing import Any

import pytest

from crew.agent.subagent.context import (
    ParentDeliveryState,
    SubagentNotificationQueue,
    SubagentNotificationRouter,
    format_subagent_notifications,
)
from crew.agent.subagent.policy import delegation_blocked_toolsets
from crew.agent.subagent.runtime import (
    RunLogRecorder,
    SubagentRuntime,
    fold_run_outcome,
)
from crew.agent.subagent.service import (
    StartRequest,
    SubagentDepthError,
    SubagentResult,
    SubagentRun,
)
from crew.agent.subagent.store import SubagentStore


# ═══════════════════════════════════════════════════════════════════════════
# B4 结局折叠：唯一推导入口
# ═══════════════════════════════════════════════════════════════════════════


def _events(*reasons: str) -> list[dict]:
    events = [{"event": "run/start", "data": {}}]
    for reason in reasons:
        events.append({"event": "turn/start", "data": {}})
        events.append({"event": "turn/end", "data": {"reason": reason}})
    return events


@pytest.mark.parametrize(
    "reasons,expected",
    [
        (("completed",), "completed"),
        (("blocked",), "refusal"),
        (("aborted",), "aborted"),
        (("interrupted",), "aborted"),
        (("error",), "error"),
        (("max_tokens",), "max_tokens"),
        (("max-tokens",), "max_tokens"),
        # 已记录的失败优先于其后的取消
        (("error", "aborted"), "error"),
        (("aborted", "error"), "error"),
        # 不可名状按失败复核
        (("weird-vocabulary",), "error"),
        # 超时是运行时按 timeout_kind 生成的结论，不由 turn 结局推导
        (("timed_out",), "error"),
    ],
)
def test_fold_run_outcome_mapping_table(reasons, expected):
    assert fold_run_outcome(_events(*reasons)) == expected


def test_fold_run_outcome_empty_log_falls_back_to_provider_report():
    assert fold_run_outcome([], fallback="aborted") == "aborted"
    assert fold_run_outcome([{"event": "output/final", "data": {}}]) == "error"


# ═══════════════════════════════════════════════════════════════════════════
# B3 深度账本与可见性真值表
# ═══════════════════════════════════════════════════════════════════════════


class _OkProvider:
    inherits_parent_context = False
    name = "ok"

    from crew.agent.subagent.service import SubagentCapabilities

    capabilities = SubagentCapabilities()

    async def start(self, request: Any) -> SubagentRun:
        run = SubagentRun(
            request.descriptor.subagent_id,
            SubagentResult(output="done"),
        )
        # provider 侧记 turn 边界，供折叠入口复算
        recorder = request.runtime_options.get("run_log")
        if isinstance(recorder, RunLogRecorder):
            recorder.turn_start()
            recorder.turn_end("completed")
            recorder.output_final("done")
        return run


@pytest.fixture()
def store(tmp_path):
    instance = SubagentStore(tmp_path / "subagent.db")
    yield instance
    instance.close()


@pytest.mark.asyncio
async def test_depth_ledger_uses_persisted_parent_as_lower_bound(store):
    # 父会话自身是一个已落库的 depth=2 子身份
    store.record_run_start(
        subagent_id="sub::parent::1",
        parent_session_id="root",
        owner_account_id="local",
        workspace_id=None,
        depth=2,
        mode="one_shot",
        provider="ok",
        label="parent",
    )
    runtime = SubagentRuntime(_OkProvider(), store=store, max_depth=3)
    run = await runtime.start(
        StartRequest(
            label="child",
            goal_text="work",
            parent_session_id="sub::parent::1",
            provider="ok",
        )
    )
    assert run.descriptor.delegation_depth == 3
    await run.dispose()


@pytest.mark.asyncio
async def test_depth_ledger_is_monotonic_against_lower_runtime_values(store):
    store.record_run_start(
        subagent_id="sub::parent::1",
        parent_session_id="root",
        owner_account_id="local",
        workspace_id=None,
        depth=2,
        mode="one_shot",
        provider="ok",
        label="parent",
    )
    runtime = SubagentRuntime(_OkProvider(), store=store, max_depth=3)
    # 重放方声称 parent_depth=0（低于持久值）：仍按持久下界计算
    run = await runtime.start(
        StartRequest(
            label="child",
            goal_text="work",
            parent_session_id="sub::parent::1",
            parent_depth=0,
            provider="ok",
        )
    )
    assert run.descriptor.delegation_depth == 3
    await run.dispose()


@pytest.mark.asyncio
async def test_depth_limit_rejects_beyond_max(store):
    runtime = SubagentRuntime(_OkProvider(), store=store, max_depth=2)
    with pytest.raises(SubagentDepthError):
        await runtime.start(
            StartRequest(
                label="too-deep",
                goal_text="work",
                parent_session_id="sub::parent::1",
                parent_depth=2,
                provider="ok",
            )
        )


def test_delegation_visibility_truth_table():
    base = delegation_blocked_toolsets(1, allow_nested=False, max_depth=3)
    assert "subagent" in base and "subagent.preset" in base
    assert "external_agent" in base  # 外援通道任何深度都不放行

    opened = delegation_blocked_toolsets(1, allow_nested=True, max_depth=3)
    assert "subagent" not in opened and "subagent.preset" not in opened
    assert "external_agent" in opened  # 嵌套委派不等于嵌套外援

    # 深度达上限：可见性收回（启动前仍会被运行时深度校验拒绝）
    capped = delegation_blocked_toolsets(3, allow_nested=True, max_depth=3)
    assert "subagent" in capped


# ═══════════════════════════════════════════════════════════════════════════
# B2/B4 结算落库：run 行 / descriptor / 边 / run log
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_execute_settles_store_rows_and_closes_edge(store):
    runtime = SubagentRuntime(_OkProvider(), store=store)
    result = await runtime.execute(
        StartRequest(
            label="child",
            goal_text="work",
            parent_session_id="parent-1",
            owner_account_id="local",
            provider="ok",
        )
    )
    assert result.stop_reason == "completed"

    run = store.get_run(result and _last_run_id(store))
    assert run is not None
    assert run["status"] == "settled"
    assert run["stop_reason"] == "completed"
    assert [e["event"] for e in run["run_log"]] == [
        "run/start",
        "turn/start",
        "turn/end",
        "output/final",
        "run/settle",
    ]
    edge = store.get_edge(run["subagent_id"])
    assert edge is not None and edge["status"] == "closed"
    descriptor = store.read_descriptor(run["subagent_id"])
    assert descriptor is not None and descriptor["provider"] == "ok"


def _last_run_id(store: SubagentStore) -> str:
    return store.list_runs(limit=1)[0]["subagent_id"]


@pytest.mark.asyncio
async def test_fold_overrides_provider_report_when_log_disagrees(store):
    class _LyingProvider(_OkProvider):
        async def start(self, request: Any) -> SubagentRun:
            recorder = request.runtime_options.get("run_log")
            if isinstance(recorder, RunLogRecorder):
                recorder.turn_start()
                recorder.turn_end("error")  # 日志记了失败
            return SubagentRun(
                request.descriptor.subagent_id,
                SubagentResult(output="done", stop_reason="completed"),
            )

    runtime = SubagentRuntime(_LyingProvider(), store=store)
    result = await runtime.execute(
        StartRequest(
            label="child",
            goal_text="work",
            parent_session_id="parent-1",
            provider="ok",
        )
    )
    # 日志是真相：provider 自报 completed，日志记 error → 以日志为准
    assert result.stop_reason == "error"
    assert store.get_run(_last_run_id(store))["stop_reason"] == "error"


# ═══════════════════════════════════════════════════════════════════════════
# B5 四态投递路由
# ═══════════════════════════════════════════════════════════════════════════


def _result(task_id: str = "task-1") -> dict:
    return {
        "agent": "worker",
        "status": "completed",
        "summary": "done",
        "task_id": task_id,
        "owner_account_id": "local",
    }


def test_router_drops_when_parent_gone():
    queue = SubagentNotificationQueue()
    router = SubagentNotificationRouter(queue)
    action = router.deliver("s1", _result(), state=ParentDeliveryState.GONE)
    assert action == "dropped"
    assert queue.drain("s1", "local") == []


def test_router_enqueues_for_idle_and_draining():
    queue = SubagentNotificationQueue()
    router = SubagentNotificationRouter(queue)
    assert (
        router.deliver("s1", _result(), state=ParentDeliveryState.IDLE)
        == "enqueued"
    )
    assert (
        router.deliver("s1", _result("task-2"), state=ParentDeliveryState.DRAINING)
        == "enqueued"
    )
    assert len(queue.drain("s1", "local")) == 2


def test_router_busy_steer_success_removes_queued_copy():
    queue = SubagentNotificationQueue()
    router = SubagentNotificationRouter(queue)
    steered: list[str] = []

    action = router.deliver(
        "s1",
        _result(),
        state=ParentDeliveryState.BUSY,
        steer=lambda text: (steered.append(text) or True),
    )
    assert action == "steered"
    # steer 即投递：队列副本被摘除，内容只进一次
    assert queue.drain("s1", "local") == []
    assert len(steered) == 1
    # steer 的文案与 drain 文案逐字一致
    assert steered[0] == format_subagent_notifications([_result()])


def test_router_busy_steer_failure_falls_back_to_queue():
    queue = SubagentNotificationQueue()
    router = SubagentNotificationRouter(queue)
    action = router.deliver(
        "s1", _result(), state=ParentDeliveryState.BUSY, steer=lambda _t: False
    )
    assert action == "enqueued"
    assert len(queue.drain("s1", "local")) == 1


# ═══════════════════════════════════════════════════════════════════════════
# B7 摘要兜底
# ═══════════════════════════════════════════════════════════════════════════


class _TerseChild:
    """首轮交卷过短，收到扩写指令后补全。"""

    def __init__(self) -> None:
        self.turns: list[str] = []

    async def run(self, envelope: Any):
        text = str(getattr(envelope, "query", "") or "")
        self.turns.append(text)
        if "过于简短" in text:
            yield {"kind": "final", "body": {"text": "这是一个足够长的完整汇报" * 10}}
        else:
            yield {"kind": "final", "body": {"text": "短"}}

    def aclose(self) -> None:
        return None


class _ChunkLike:
    """dict chunk 的属性访问包装（provider 读 chunk.kind/body）。"""

    def __init__(self, data: dict) -> None:
        self.kind = data["kind"]
        self.body = data.get("body", {})


class _AttrChild(_TerseChild):
    async def run(self, envelope: Any):
        async for chunk in super().run(envelope):
            yield _ChunkLike(chunk)


@pytest.mark.asyncio
async def test_summary_rewrite_appends_one_continuation_turn():
    from crew.agent.subagent.providers import SUMMARY_CONTINUATION_PROMPT

    child = _AttrChild()
    runtime = SubagentRuntime(build_child=lambda spec: child)
    result = await runtime.execute(
        StartRequest(label="t", goal_text="work", parent_session_id="p"),
        runtime_options={"summary_policy": {"min_chars": 50, "retries": 1}},
    )
    assert result.stop_reason == "completed"
    assert len(result.output) >= 50
    assert len(child.turns) == 2
    assert SUMMARY_CONTINUATION_PROMPT in child.turns[1]


@pytest.mark.asyncio
async def test_summary_rewrite_not_triggered_without_policy_or_when_long():
    child = _AttrChild()
    runtime = SubagentRuntime(build_child=lambda spec: child)
    result = await runtime.execute(
        StartRequest(label="t", goal_text="work", parent_session_id="p")
    )
    assert result.output == "短"  # 无策略 → 不扩写
    assert len(child.turns) == 1

    long_child = _LongChild()
    runtime2 = SubagentRuntime(build_child=lambda spec: long_child)
    # 有策略但首轮已足够长：不扩写
    result2 = await runtime2.execute(
        StartRequest(label="t", goal_text="work", parent_session_id="p"),
        runtime_options={"summary_policy": {"min_chars": 20, "retries": 1}},
    )
    assert len(result2.output) >= 20
    assert len(long_child.turns) == 1


class _LongChild(_TerseChild):
    async def run(self, envelope: Any):
        text = str(getattr(envelope, "query", "") or "")
        self.turns.append(text)
        yield _ChunkLike(
            {"kind": "final", "body": {"text": "已经是足够长的完整汇报" * 5}}
        )


@pytest.mark.asyncio
async def test_max_tokens_delivery_is_not_rewritten():
    class _MaxTokensChild(_TerseChild):
        async def run(self, envelope: Any):
            text = str(getattr(envelope, "query", "") or "")
            self.turns.append(text)
            yield _ChunkLike(
                {"kind": "final", "body": {"text": "短", "reason": "max_tokens"}}
            )

    child = _MaxTokensChild()
    runtime = SubagentRuntime(build_child=lambda spec: child)
    result = await runtime.execute(
        StartRequest(label="t", goal_text="work", parent_session_id="p"),
        runtime_options={"summary_policy": {"min_chars": 50, "retries": 1}},
    )
    # 因 token 上限截断不视为交卷：不触发扩写
    assert result.stop_reason == "max_tokens"
    assert len(child.turns) == 1
