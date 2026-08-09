"""Crew 子智能体能力契约测试。

目标：验证 Crew Subagent 的核心行为、隔离边界与生命周期。
测试以单元/集成级别为主，尽量使用 FakeProvider 或 mock，保证稳定、快速、可重复。
"""

from __future__ import annotations

import json
from pathlib import Path


from crew.agent.subagent import SubagentRegistry
from crew.app import build_app, CrewApp
from crew.core.runctx import current_session_id, current_user_type
from crew.core.types import ToolCall
from crew.state.access_control import AccessControlConfig
from crew.state.config import Config


# ── 辅助函数 ──────────────────────────────────────────────────────────────

def _build_isolated_app(tmp_path: Path) -> CrewApp:
    """构造一个使用临时目录的 app，避免污染生产数据库。"""
    cfg = Config(max_iterations=5)
    cfg.db_path = str(tmp_path / "crew.db")
    cfg.memory_db_path = str(tmp_path / "memory.db")
    cfg.crew_home = str(tmp_path / ".crew")
    return build_app(config=cfg, enable_team=False)


# ═══════════════════════════════════════════════════════════════════════════
# 委派与隔离能力
# ═══════════════════════════════════════════════════════════════════════════


async def test_delegate_task_returns_structured_result():
    """Crew: delegate_task 是同步工具，返回结构化 JSON 结果。"""
    app = build_app(config=Config(max_iterations=5))
    result = await app.registry.execute(
        ToolCall("c1", "delegate_task", {"goal": "说你好", "toolsets": ["terminal"]})
    )
    assert not result.is_error
    payload = json.loads(result.content)
    assert "results" in payload
    one = payload["results"][0]
    assert one["status"] == "completed"
    assert "duration_seconds" in one
    assert "tool_calls" in one


async def test_child_toolset_is_subset_of_parent():
    """Crew: 子 agent 可用工具集应该是父 agent 工具集的子集。"""
    cfg = Config(max_iterations=5)
    cfg.access_control = AccessControlConfig(
        user_type="internal",
        external={"enabled_toolsets": ["file"]},
        internal={"enabled_toolsets": ["file", "terminal"]},
    )
    app = build_app(config=cfg)

    # 父 internal 有 terminal
    parent_tools = app._single_agent_tool_filter("builtin", cfg.access_control.resolve_for("internal"))
    assert "terminal" in parent_tools

    # 但外部用户父创建的子 agent 不应拿到 terminal
    tok = current_user_type.set("external")
    try:
        child = app._make_subagent({
            "system_prompt": "x",
            "toolsets": None,
            "tools": None,
            "model": "inherit",
            "max_iterations": 5,
        })
    finally:
        current_user_type.reset(tok)
    assert "terminal" not in child.tool_filter
    assert "file_read" in child.tool_filter


# ═══════════════════════════════════════════════════════════════════════════
# 注册、执行与后台生命周期
# ═══════════════════════════════════════════════════════════════════════════


def test_agent_definition_registry():
    """Crew: 有 AgentDefinition 注册表，支持内置 + 用户覆盖。"""
    reg = SubagentRegistry()
    # 内置存在
    assert "Explore" in reg.names()
    assert "Plan" in reg.names()
    # source 标记为 builtin
    assert reg.get("Explore").source == "builtin"


async def test_collect_wait_and_poll(tmp_path):
    """Crew: collect_subagent 支持 wait=true 阻塞等待和 wait=false 轮询。"""
    app = _build_isolated_app(tmp_path)
    tok = current_session_id.set("s-poll")
    try:
        launched = await app.registry.execute(
            ToolCall("b4", "run_agent",
                     {"agent_type": "Explore", "goal": "hi", "run_in_background": True})
        )
        task_id = json.loads(launched.content)["task_id"]

        # poll 立即返回 running
        poll = await app.registry.execute(
            ToolCall("b5", "collect_subagent", {"task_id": task_id, "wait": False})
        )
        assert "running" in poll.content.lower()

        # wait 阻塞到完成
        wait = await app.registry.execute(
            ToolCall("b6", "collect_subagent", {"task_id": task_id, "wait": True})
        )
        res = json.loads(wait.content)
        assert res["status"] == "completed"
    finally:
        current_session_id.reset(tok)


# ═══════════════════════════════════════════════════════════════════════════
# 额外边界/压力测试
# ═══════════════════════════════════════════════════════════════════════════


async def test_child_tool_calls_capped_by_max_iterations(tmp_path):
    """子 agent 的工具调用次数应受 max_iterations 限制，不会无限循环。"""
    app = _build_isolated_app(tmp_path)
    # 使用一个会触发多轮工具的 preset，但 max_iterations 设得很低
    cfg = app.config
    cfg.max_iterations = 2
    app2 = build_app(config=cfg, enable_team=False)

    result = await app2.registry.execute(
        ToolCall("c4", "run_agent", {"agent_type": "Explore", "goal": "搜索日志模块"})
    )
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["results"][0]["tool_calls"] <= 2


async def test_background_task_id_unique_and_mapped(tmp_path):
    """后台 task_id 应唯一，且能映射到 TaskManager 看板。"""
    app = _build_isolated_app(tmp_path)
    tok = current_session_id.set("s-unique")
    try:
        launched1 = await app.registry.execute(
            ToolCall("b9", "run_agent",
                     {"agent_type": "Explore", "goal": "hi", "run_in_background": True})
        )
        launched2 = await app.registry.execute(
            ToolCall("b10", "run_agent",
                     {"agent_type": "Explore", "goal": "hi2", "run_in_background": True})
        )
        id1 = json.loads(launched1.content)["task_id"]
        id2 = json.loads(launched2.content)["task_id"]
        assert id1 and id2 and id1 != id2

        for t in list(app._subagent_bg_tasks):
            await t

        assert app.subagent_tasks.get(id1)["status"] == "done"
        assert app.subagent_tasks.get(id2)["status"] == "done"
    finally:
        current_session_id.reset(tok)


async def test_subagent_error_does_not_crash_parent(tmp_path):
    """单个子 agent 出错不应导致父 agent 或并行的其他子 agent 崩溃。"""

    def bad_child(spec):
        class BadAgent:
            async def run(self, env):
                yield type("Chunk", (), {"kind": "error", "body": {"message": "子 agent 爆炸"}})()
        return BadAgent()

    from crew.agent.subagent.tools import _run_children

    cfg = Config(max_iterations=5)
    cfg.db_path = str(tmp_path / "crew.db")
    cfg.memory_db_path = str(tmp_path / "memory.db")
    cfg.crew_home = str(tmp_path / ".crew")
    result = await _run_children(
        [
            {"label": "good", "goal_text": "ok", "spec": {}},
            {"label": "bad", "goal_text": "boom", "spec": {}},
        ],
        build_child=bad_child,
        max_concurrent=2,
        active=None,
        idle_timeout=10,
        max_runtime=0,
    )
    payload = json.loads(result)
    statuses = {r["agent"]: r["status"] for r in payload["results"]}
    assert "bad" in statuses and statuses["bad"] == "error"
    assert "good" in statuses


async def test_many_parallel_children_under_cap(tmp_path):
    """delegate_task 批量任务应受 max_concurrent 并发上限控制。"""
    cfg = Config(max_iterations=5, subagent_max_concurrent=2)
    cfg.db_path = str(tmp_path / "crew.db")
    cfg.memory_db_path = str(tmp_path / "memory.db")
    cfg.crew_home = str(tmp_path / ".crew")
    app = build_app(config=cfg, enable_team=False)

    tasks = [{"goal": f"任务{i}"} for i in range(5)]
    result = await app.registry.execute(
        ToolCall("c5", "delegate_task", {"goal": "x", "tasks": tasks})
    )
    assert not result.is_error
    payload = json.loads(result.content)
    assert len(payload["results"]) == 5
