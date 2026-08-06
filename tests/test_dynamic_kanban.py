"""Dynamic Kanban 后端测试。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from crew.core.envelope import Envelope, ResponseChunk
from crew.core.mocks import FakeProvider, InMemorySessionStore, NullMemory
from crew.core.types import ChatResponse, Message, ToolCall
from crew.dynamickanban.manager import DynamicKanbanManager
from crew.dynamickanban.models import PlanDelta
from crew.dynamickanban.orchestrator import WorkflowOrchestrator
from crew.dynamickanban.runtime import WorkflowRuntime
from crew.dynamickanban.runtime_models import (
    AgentCall,
    AgentCallResult,
    Phase,
    PhaseResult,
    RuntimeState,
    WorkflowDefinition,
)
from crew.dynamickanban.runtime_models import WorkflowDefinitionMigrationError
from crew.dynamickanban.store import SQLiteKanbanStore
from crew.gateway.dispatcher import SessionDispatcher
from crew.plugins.manager import PluginManager
from crew.state.config import Config, load_config
from crew.tools.registry import Registry, register_builtin_tools


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "kanban_test.db")


@pytest.fixture
def store(db_path: str) -> SQLiteKanbanStore:
    return SQLiteKanbanStore(db_path).for_owner("local")


@pytest.fixture
def base_registry() -> Registry:
    registry = Registry()
    register_builtin_tools(registry)
    return registry


@pytest.fixture
def session_store() -> InMemorySessionStore:
    return InMemorySessionStore()


@pytest.fixture
def plugins() -> PluginManager:
    return PluginManager([], registry=Registry())


def test_create_workflow_and_task(store: SQLiteKanbanStore) -> None:
    wf = store.create_workflow("s1", "测试需求")
    assert wf.session_id == "s1"
    assert wf.title == "测试需求"

    task = store.add_task(wf.id, "子任务 A", assignee="coder")
    assert task.workflow_id == wf.id
    assert task.status == "ready"  # 无依赖，自动 promote


def test_runtime_staffing_reassignment_is_atomic_with_workflow_revision(
    store: SQLiteKanbanStore,
) -> None:
    workflow, tasks = store.create_workflow_graph(
        "staffing-session",
        "实现接口",
        context={
            "source": "team",
            "workflow_plan": {
                "version": 1,
                "revision": 1,
                "nodes": [{"id": "build", "assignee_id": "worker-a"}],
            },
        },
        nodes=[{
            "id": "build",
            "title": "实现接口",
            "detail": "完成后端实现",
            "assignee": "worker-a",
            "status": "failed",
        }],
        edges=[],
        event_type="team_plan_created",
        event_payload={"node_task_ids": {}},
    )
    revised = {
        "version": 1,
        "revision": 2,
        "nodes": [{"id": "build", "assignee_id": "runtime-worker"}],
        "runtime_members": [{"member_id": "runtime-worker"}],
    }
    task = tasks["build"]

    updated_workflow, updated_task = store.apply_task_reassignment_revision(
        workflow.id,
        task.id,
        revised,
        assignee="runtime-worker",
        reason="runtime_staffing",
        delta={"reassigned_node": {"node_id": "build"}},
    )

    assert updated_workflow.context["workflow_plan"] == revised
    assert updated_workflow.context["current_revision"] == 2
    assert updated_task.assignee == "runtime-worker"
    assert updated_task.status == "pending"
    assert updated_task.retry_count == 0
    event = next(
        item
        for item in store.get_board_state(workflow.id)["events"]
        if item["event_type"] == "workflow_plan_revised"
    )
    assert event["task_id"] == task.id
    assert event["payload"]["reason"] == "runtime_staffing"


def test_same_session_id_isolated_by_owner(db_path: str) -> None:
    root = SQLiteKanbanStore(db_path)
    owner_a = root.for_owner("A:uid-a")
    owner_b = root.for_owner("B:uid-b")

    workflow_a = owner_a.create_workflow("same", "A workflow")
    workflow_b = owner_b.create_workflow("same", "B workflow")

    assert owner_a.get_latest_workflow_by_session("same").id == workflow_a.id
    assert owner_b.get_latest_workflow_by_session("same").id == workflow_b.id
    assert owner_a.get_workflow(workflow_b.id) is None
    assert owner_b.get_workflow(workflow_a.id) is None


def test_unscoped_store_rejects_workflow_access(db_path: str) -> None:
    root = SQLiteKanbanStore(db_path)

    with pytest.raises(ValueError, match="Owner"):
        root.create_workflow("s1", "must be scoped")


def test_scoped_store_cannot_rebind_owner(db_path: str) -> None:
    owner_a = SQLiteKanbanStore(db_path).for_owner("A:uid-a")

    assert owner_a.for_owner("A:uid-a") is owner_a
    with pytest.raises(ValueError, match="不可切换"):
        owner_a.for_owner("B:uid-b")


def test_legacy_workflow_migration_only_backfills_provable_owner(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-kanban.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            owner_account_id TEXT NOT NULL,
            session_id TEXT NOT NULL
        );
        CREATE TABLE kanban_workflows (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            context TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        """
    )
    conn.executemany(
        "INSERT INTO sessions (owner_account_id, session_id) VALUES (?, ?)",
        [
            ("A:uid-a", "unique"),
            ("A:uid-a", "ambiguous"),
            ("B:uid-b", "ambiguous"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO kanban_workflows (
            id, session_id, title, status, context, created_at, updated_at
        ) VALUES (?, ?, ?, 'active', '{}', 1, 1)
        """,
        [
            ("wf_unique", "unique", "unique"),
            ("wf_ambiguous", "ambiguous", "ambiguous"),
            ("wf_orphan", "orphan", "orphan"),
        ],
    )
    conn.commit()
    conn.close()

    root = SQLiteKanbanStore(db_path)

    owner_a = root.for_owner("A:uid-a")
    owner_b = root.for_owner("B:uid-b")
    assert owner_a.get_workflow("wf_unique") is not None
    assert owner_b.get_workflow("wf_unique") is None
    assert owner_a.get_workflow("wf_ambiguous") is None
    assert owner_b.get_workflow("wf_ambiguous") is None
    assert owner_a.get_workflow("wf_orphan") is None
    root.close()

    # Reopening reruns migrations; quarantined rows must remain quarantined.
    reopened = SQLiteKanbanStore(db_path)
    assert reopened.for_owner("A:uid-a").get_workflow("wf_unique") is not None
    reopened.close()
    check = sqlite3.connect(db_path)
    rows = dict(
        check.execute(
            "SELECT id, isolation_state FROM kanban_workflows ORDER BY id"
        ).fetchall()
    )
    check.close()
    assert rows == {
        "wf_ambiguous": "legacy_ambiguous",
        "wf_orphan": "legacy_orphaned",
        "wf_unique": "owned",
    }


def test_pause_resume_preserves_workflow_context_atomically(store: SQLiteKanbanStore) -> None:
    workflow = store.create_workflow(
        "session-context",
        "context",
        context={"definition": {"phases": ["one"]}, "attempt": 2},
    )

    paused = store.pause_workflow(workflow.id, "first reason")
    assert paused.context == {
        "definition": {"phases": ["one"]},
        "attempt": 2,
        "pause_reason": "first reason",
    }

    paused_again = store.pause_workflow(workflow.id, "updated reason")
    assert paused_again.context == {
        "definition": {"phases": ["one"]},
        "attempt": 2,
        "pause_reason": "updated reason",
    }

    resumed = store.resume_workflow(workflow.id)
    assert resumed.status == "active"
    assert resumed.context == {"definition": {"phases": ["one"]}, "attempt": 2}


def test_pause_rejects_terminal_workflow(store: SQLiteKanbanStore) -> None:
    workflow = store.create_workflow("session-terminal", "terminal", context={"kept": True})
    store.update_workflow_status(workflow.id, "done")

    with pytest.raises(ValueError, match="终态"):
        store.pause_workflow(workflow.id, "too late")

    assert store.get_workflow(workflow.id).context == {"kept": True}


def test_latest_workflow_can_exclude_team_source(store: SQLiteKanbanStore) -> None:
    kanban_wf = store.create_workflow("same-session", "Dynamic Kanban")
    team_wf = store.create_workflow("same-session", "Team 看板", context={"source": "team"})

    assert store.get_latest_workflow_by_session("same-session").id == team_wf.id
    assert store.get_latest_workflow_by_session("same-session", exclude_source="team").id == kanban_wf.id


def test_dependency_promote(store: SQLiteKanbanStore) -> None:
    wf = store.create_workflow("s1", "依赖测试")
    t1 = store.add_task(wf.id, "T1", assignee="coder")
    t2 = store.add_task(wf.id, "T2", assignee="coder", parent_task_ids=[t1.id])

    assert t1.status == "ready"
    assert t2.status == "pending"

    run_id = "run_1"
    claimed = store.claim_ready_task(t1.id, run_id)
    assert claimed is not None
    assert claimed.status == "running"

    store.update_task_status(t1.id, "done", result_summary="T1 done")
    t2_after = store.get_task(t2.id)
    assert t2_after.status == "ready"


def test_cas_claim(store: SQLiteKanbanStore) -> None:
    wf = store.create_workflow("s1", "CAS 测试")
    t = store.add_task(wf.id, "任务", assignee="coder")

    c1 = store.claim_ready_task(t.id, "run_1")
    assert c1 is not None
    c2 = store.claim_ready_task(t.id, "run_2")
    assert c2 is None


def test_reset_failed_to_ready(store: SQLiteKanbanStore) -> None:
    wf = store.create_workflow("s1", "重试测试")
    t = store.add_task(wf.id, "任务", max_retries=1)
    store.update_task_status(t.id, "failed")
    store.update_task_status(t.id, "failed")  # retry_count=2 >= max_retries

    store.reset_failed_to_ready(wf.id)
    task = store.get_task(t.id)
    assert task.status == "blocked"


def test_apply_plan_extension(store: SQLiteKanbanStore) -> None:
    wf = store.create_workflow("s1", "delta 测试")
    t1 = store.add_task(wf.id, "T1")
    store.update_task_status(t1.id, "done")

    delta = PlanDelta(
        add_tasks=[
            {
                "title": "T2",
                "detail": "detail",
                "assignee": "coder",
                "parent_task_ids": [t1.id],
            }
        ],
        add_dependencies=[],
    )
    added = store.apply_plan_extension(wf.id, delta)
    assert len(added) == 1
    assert added[0].status == "ready"


# --------------------------------------------------------------------------- #
# RuntimeState replan_count
# --------------------------------------------------------------------------- #
def test_runtime_state_replan_count_round_trip() -> None:
    """replan_count 是失败自动 replan 的状态契约，持久化后应能恢复。"""
    state = RuntimeState.from_dict({"workflow_id": "wf", "replan_count": 2})

    assert state.replan_count == 2
    assert state.to_dict()["replan_count"] == 2
    assert RuntimeState(workflow_id="wf").replan_count == 0


def test_legacy_replan_config_is_safely_ignored(tmp_path: Path) -> None:
    """旧部署残留的无效上限字段不应恢复成当前配置能力。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("runtime:\n  dk_max_replan: 7\n", encoding="utf-8")

    config = load_config(config_path=config_path)

    assert not hasattr(config, "dk_max_replan")


# --------------------------------------------------------------------------- #
# Engine tests
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Manager tests
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_manager_interact(
    db_path: str,
    base_registry: Registry,
    session_store: InMemorySessionStore,
    plugins: PluginManager,
) -> None:
    provider = FakeProvider()
    cfg = Config(db_path=db_path)
    store = SQLiteKanbanStore(db_path)
    manager = DynamicKanbanManager(
        store=store,
        provider=provider,
        base_registry=base_registry,
        session_store=session_store,
        memory=NullMemory(),
        plugins=plugins,
        config=cfg,
    )
    env = Envelope.of("测试请求", session_id="s1", mode="dynamic_kanban")
    chunks = [c async for c in manager.interact(env)]
    assert chunks[-1].kind == "final"


@pytest.mark.asyncio
async def test_manager_session_exclusive_lock(
    db_path: str,
    base_registry: Registry,
    session_store: InMemorySessionStore,
    plugins: PluginManager,
) -> None:
    """同一 session 的并发请求不应产生两个同时运行的 engine / workflow。"""
    barrier = asyncio.Event()
    provider = FakeProvider()
    cfg = Config(db_path=db_path)
    store = SQLiteKanbanStore(db_path)

    def slow_agent_factory(**kwargs):
        class _SlowAgent:
            async def run(self, env: Envelope):
                yield ResponseChunk.status_event(env.request_id, "working", 1)
                await barrier.wait()
                yield ResponseChunk.final(env.request_id, "done", 2)
        return _SlowAgent()

    manager = DynamicKanbanManager(
        store=store,
        provider=provider,
        base_registry=base_registry,
        session_store=session_store,
        memory=NullMemory(),
        plugins=plugins,
        config=cfg,
        agent_factory=slow_agent_factory,
    )
    owner_store = store.for_owner("local")
    session_id = "s_concurrent"
    env1 = Envelope.of("请求1", session_id=session_id, mode="dynamic_kanban", request_id="req_1")
    env2 = Envelope.of("请求2", session_id=session_id, mode="dynamic_kanban", request_id="req_2")

    async def _consume(gen: AsyncIterator[ResponseChunk]) -> list[ResponseChunk]:
        return [c async for c in gen]

    t1 = asyncio.create_task(_consume(manager.interact(env1)))
    t2 = asyncio.create_task(_consume(manager.interact(env2)))

    # 等待第一个请求拿到锁并创建 workflow，第二个请求排队
    await asyncio.sleep(0.3)
    workflows = owner_store.list_workflows_by_session(session_id)
    assert len(workflows) == 1, f"并发时应只有一个 workflow，实际 {len(workflows)}"

    barrier.set()
    chunks1, chunks2 = await asyncio.gather(t1, t2)
    assert chunks1[-1].kind == "final"
    assert chunks2[-1].kind == "final"

    # 两个请求串行执行，第一个结束后第二个会新建 workflow，因此最终是两个 workflow
    workflows = owner_store.list_workflows_by_session(session_id)
    assert len(workflows) == 2


@pytest.mark.asyncio
async def test_manager_interrupt(
    db_path: str,
    base_registry: Registry,
    session_store: InMemorySessionStore,
    plugins: PluginManager,
) -> None:
    """interrupt() 应能通知运行中的 engine 停止并把任务标记为失败。"""
    barrier = asyncio.Event()
    provider = FakeProvider()
    cfg = Config(db_path=db_path)
    store = SQLiteKanbanStore(db_path)

    def slow_agent_factory(**kwargs):
        class _SlowAgent:
            async def run(self, env: Envelope):
                yield ResponseChunk.status_event(env.request_id, "working", 1)
                await barrier.wait()
                yield ResponseChunk.final(env.request_id, "done", 2)
        return _SlowAgent()

    manager = DynamicKanbanManager(
        store=store,
        provider=provider,
        base_registry=base_registry,
        session_store=session_store,
        memory=NullMemory(),
        plugins=plugins,
        config=cfg,
        agent_factory=slow_agent_factory,
    )
    owner_store = store.for_owner("local")
    session_id = "s_interrupt"
    env = Envelope.of("长任务", session_id=session_id, mode="dynamic_kanban", request_id="req_i")

    async def _consume(gen: AsyncIterator[ResponseChunk]) -> list[ResponseChunk]:
        return [c async for c in gen]

    task = asyncio.create_task(_consume(manager.interact(env)))

    # 等待 engine 启动并把任务置为 running
    for _ in range(100):
        if manager._engines:
            break
        await asyncio.sleep(0.01)
    assert manager._engines, "engine 应已注册"

    assert manager.interrupt(session_id, "测试中断", owner_account_id="local") is True
    barrier.set()
    chunks = await task
    assert chunks[-1].kind == "final"

    wf = owner_store.get_latest_workflow_by_session(session_id)
    assert wf is not None
    assert wf.status == "failed"
    assert any(
        t.status == "failed" and t.result_summary == "被用户中断"
        for t in owner_store.list_tasks(wf.id)
    )


def test_manager_interrupt_requires_owner(
    db_path: str,
    base_registry: Registry,
    session_store: InMemorySessionStore,
    plugins: PluginManager,
) -> None:
    """中断是每次「停止」都会走的 best-effort 级联，绝大多数会话没有 DK workflow。

    缺 owner_account_id 时必须安静返回 False——store 的 _require_owner 会抛
    ValueError，早先 app.interrupt 没把 owner 传下来，于是每点一次停止就在网关刷一条
    「Dynamic Kanban Store 操作缺少 Owner scope」ERROR 堆栈。
    """
    provider = FakeProvider()
    cfg = Config(db_path=db_path)
    store = SQLiteKanbanStore(db_path)
    manager = DynamicKanbanManager(
        store=store,
        provider=provider,
        base_registry=base_registry,
        session_store=session_store,
        memory=NullMemory(),
        plugins=plugins,
        config=cfg,
    )
    assert manager.interrupt("s_missing_owner", "测试中断") is False
    assert manager.interrupt("s_missing_owner", "测试中断", owner_account_id="   ") is False
    # 有 owner 但该 session 没有活跃 workflow：同样是 False，且不抛异常。
    assert manager.interrupt("s_missing_owner", "测试中断", owner_account_id="local") is False


def test_manager_steer_requires_owner(
    db_path: str,
    base_registry: Registry,
    session_store: InMemorySessionStore,
    plugins: PluginManager,
) -> None:
    """steer() 在缺少 owner_account_id 时应优雅返回 False，而不是抛 ValueError。"""
    provider = FakeProvider()
    cfg = Config(db_path=db_path)
    store = SQLiteKanbanStore(db_path)
    manager = DynamicKanbanManager(
        store=store,
        provider=provider,
        base_registry=base_registry,
        session_store=session_store,
        memory=NullMemory(),
        plugins=plugins,
        config=cfg,
    )
    assert manager.steer("s_missing_owner", "补充指令") is False


@pytest.mark.asyncio
async def test_manager_interrupt_is_owner_scoped(
    db_path: str,
    base_registry: Registry,
    session_store: InMemorySessionStore,
    plugins: PluginManager,
) -> None:
    """interrupt() 应只命中同 owner 的 workflow，不能跨 owner 中断。"""
    provider = FakeProvider()
    cfg = Config(db_path=db_path)
    store = SQLiteKanbanStore(db_path)

    manager = DynamicKanbanManager(
        store=store,
        provider=provider,
        base_registry=base_registry,
        session_store=session_store,
        memory=NullMemory(),
        plugins=plugins,
        config=cfg,
    )

    # 为 owner_a 创建一个 active workflow
    owner_a_store = store.for_owner("owner_a")
    owner_a_store.create_workflow("s_scoped", "owner_a 需求")

    # owner_b 查不到该 workflow，因此中断失败
    assert manager.interrupt("s_scoped", "测试中断", owner_account_id="owner_b") is False

    # owner_a 可以查到并中断：没有运行中 engine 时走 DB 兜底，
    # 把残留任务和 workflow 标记为失败（paused/active 僵尸均可中止）
    assert manager.interrupt("s_scoped", "测试中断", owner_account_id="owner_a") is True
    wf = owner_a_store.get_latest_workflow_by_session("s_scoped")
    assert wf is not None and wf.status == "failed"


def test_app_interrupt_passes_owner_scope_to_dynamic_kanban() -> None:
    """app.interrupt 必须把 owner_account_id 透传给 DK，并把 DK 的结果并入返回值。

    早先这里断言 inspect.getsource 的字面子串——那是测排版不是测行为：把调用换成
    self.team 之类的真 bug 会照常绿，而 black 折行、局部变量改名却会假红。
    """
    from types import SimpleNamespace

    from crew.app import CrewApp

    seen: list[tuple[str, str | None, str]] = []

    class _RecordingKanban:
        def interrupt(self, session_id, message=None, owner_account_id=""):
            seen.append((session_id, message, owner_account_id))
            return True

    app = CrewApp.__new__(CrewApp)
    app.agents = SimpleNamespace(peek=lambda *a, **k: None)
    app.team = SimpleNamespace()
    app.dynamic_kanban = _RecordingKanban()
    app.subagent_active = None

    # 返回 True 证明 DK 的结果确实被并进了 interrupted，而不是被丢弃。
    assert app.interrupt("s1", "停止", owner_account_id="acct-1") is True
    assert seen == [("s1", "停止", "acct-1")]


# --------------------------------------------------------------------------- #
# App routing tests
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_app_handle_dynamic_kanban(tmp_path: Path) -> None:
    """验证 build_app 装配后，mode=dynamic_kanban 能正常路由。"""
    from crew.app import build_app

    db_path = tmp_path / "app_test.db"
    config = Config(
        db_path=str(db_path),
        memory_db_path=str(tmp_path / "memory_test.db"),
        api_key="",
    )
    app = build_app(config, enable_team=False)
    assert app.dynamic_kanban is not None

    env = Envelope.of("测试请求", session_id="session_1", mode="dynamic_kanban")
    chunks = [c async for c in app.handle(env)]
    assert chunks[-1].kind == "final"




def test_runtime_resolves_workflow_workdir_under_project_workspace(
    tmp_path: Path,
    db_path: str,
    base_registry: Registry,
    plugins: PluginManager,
) -> None:
    """当 envelope 携带 workspace_root_path 时，workflow 产物目录应落到项目工作空间下。"""
    provider = FakeProvider()
    store = SQLiteKanbanStore(db_path).for_owner("local")

    def dummy_agent_factory(**kwargs):
        class DummyAgent:
            async def run(self, env: Envelope):
                yield ResponseChunk.final(env.request_id, "done")
        return DummyAgent()

    runtime = WorkflowRuntime(
        store=store,
        agent_factory=dummy_agent_factory,
        base_registry=base_registry,
        provider=provider,
    )

    project_root = tmp_path / "project"
    project_root.mkdir()
    wf = store.create_workflow(
        "s_project",
        "项目空间测试",
    )
    env = Envelope.of("测试", session_id="s_project", mode="dynamic_kanban", user_id="local")
    env.params["workspace_root_path"] = str(project_root)

    workdir = runtime._resolve_workflow_workdir(wf, env)

    expected = project_root / "workflows" / wf.id
    assert workdir.resolve() == expected.resolve()


def test_runtime_resolves_workflow_workdir_fallback_without_project_workspace(
    tmp_path: Path,
    db_path: str,
    base_registry: Registry,
    plugins: PluginManager,
    monkeypatch,
) -> None:
    """未提供 workspace_root_path 时，workflow 产物目录应回退到 task_workspace_path。"""
    store = SQLiteKanbanStore(db_path).for_owner("local")

    def dummy_agent_factory(**kwargs):
        class DummyAgent:
            async def run(self, env: Envelope):
                yield ResponseChunk.final(env.request_id, "done")
        return DummyAgent()

    runtime = WorkflowRuntime(
        store=store,
        agent_factory=dummy_agent_factory,
        base_registry=base_registry,
        provider=FakeProvider(),
    )

    workspace_base = tmp_path / "workspaces"
    workspace_base.mkdir()
    monkeypatch.setattr(
        "crew.dynamickanban.runtime.task_workspace_path",
        lambda workspace_id, *, owner_account_id="": workspace_base / str(workspace_id),
    )

    wf = store.create_workflow("s_fallback", "回退测试")
    env = Envelope.of("测试", session_id="s_fallback", mode="dynamic_kanban", user_id="local")
    env.workspace_id = "ws_fallback"

    workdir = runtime._resolve_workflow_workdir(wf, env)
    assert workdir.resolve() == (workspace_base / "ws_fallback" / "workflows" / wf.id).resolve()


def test_manager_clear_session_workspaces_removes_project_workspace_dirs(
    tmp_path: Path,
    db_path: str,
    base_registry: Registry,
    session_store: InMemorySessionStore,
    plugins: PluginManager,
) -> None:
    """clear_session_workspaces 应能清理项目工作空间下的 workflow 目录。"""
    cfg = Config(db_path=db_path)
    store = SQLiteKanbanStore(db_path)
    manager = DynamicKanbanManager(
        store=store,
        provider=FakeProvider(),
        base_registry=base_registry,
        session_store=session_store,
        memory=NullMemory(),
        plugins=plugins,
        config=cfg,
    )

    project_root = tmp_path / "project"
    project_root.mkdir()
    owner_store = store.for_owner("local")
    wf = owner_store.create_workflow(
        "s_clear_project",
        "清理项目空间测试",
        context={
            "workspace_root_path": str(project_root),
        },
    )
    workdir = project_root / "workflows" / wf.id
    workdir.mkdir(parents=True)
    marker = workdir / "marker.txt"
    marker.write_text("keep", encoding="utf-8")

    # 更新 workflow context 中的 workflow_workdir，使其指向项目空间目录
    wf.context["workflow_workdir"] = str(workdir)
    owner_store.update_workflow_status(wf.id, wf.status, context=wf.context)

    removed = manager.clear_session_workspaces("s_clear_project", owner_account_id="local")

    assert any(Path(item).resolve() == workdir.resolve() for item in removed)
    assert not workdir.exists()
    assert not marker.exists()












class _CapturingProviderForHistory(FakeProvider):
    """捕获最近一次 chat 调用传入的 messages。"""

    def __init__(self, script=None):
        super().__init__(script=script)
        self.last_messages: list[Message] = []

    async def chat(self, messages, tools=None):
        self.last_messages = list(messages)
        return await super().chat(messages, tools=tools)






def test_kanban_plan_next_tool(store: SQLiteKanbanStore) -> None:
    """kanban_plan_next 工具应能直接创建下游任务并建立依赖。"""
    from crew.dynamickanban.tools import create_kanban_registry

    wf = store.create_workflow("s_plan_tool", "测试规划工具")
    lead_task = store.add_task(wf.id, "制定大纲", assignee="lead")
    store.update_task_status(lead_task.id, "done")

    tools = create_kanban_registry(
        store, wf.id, actor="worker", valid_roles=["lead", "analyst", "writer"]
    )
    plan_tool = tools["kanban_plan_next"]

    plan_json = json.dumps(
        {
            "add_tasks": [
                {
                    "title": "调研 A",
                    "detail": "维度 A 调研",
                    "assignee": "analyst",
                    "parent_task_ids": ["CURRENT_TASK_ID"],
                },
                {
                    "title": "调研 B",
                    "detail": "维度 B 调研",
                    "assignee": "analyst",
                    "parent_task_ids": [lead_task.id],
                },
            ],
            "add_dependencies": [],
        },
        ensure_ascii=False,
    )

    import asyncio

    result = asyncio.run(plan_tool.run({"task_id": lead_task.id, "plan_json": plan_json}))
    data = json.loads(result)
    assert data["ok"] is True
    assert data["added_count"] == 2

    tasks = {t.title: t for t in store.list_tasks(wf.id)}
    assert "调研 A" in tasks and "调研 B" in tasks
    for title in ("调研 A", "调研 B"):
        parents = store.get_parent_task_ids(tasks[title].id)
        assert lead_task.id in parents

    events = store.list_events(wf.id)
    assert any(e.event_type == "plan_expanded" for e in events)


def test_kanban_plan_next_rejects_cycle_without_mutating_board(
    store: SQLiteKanbanStore,
) -> None:
    """Plan Extension 引入环时应在任何任务或依赖写入前失败。"""
    from crew.dynamickanban.tools import create_kanban_registry

    wf = store.create_workflow("s_plan_cycle", "测试规划环路")
    parent = store.add_task(wf.id, "父任务", assignee="lead")
    child = store.add_task(
        wf.id,
        "子任务",
        assignee="writer",
        parent_task_ids=[parent.id],
    )
    plan_tool = create_kanban_registry(
        store,
        wf.id,
        actor="worker",
        valid_roles=["lead", "writer"],
    )["kanban_plan_next"]
    plan_json = json.dumps(
        {
            "add_tasks": [{"title": "不应创建", "assignee": "writer"}],
            "add_dependencies": [[child.id, parent.id]],
        },
        ensure_ascii=False,
    )

    import asyncio

    result = asyncio.run(plan_tool.run({"task_id": child.id, "plan_json": plan_json}))
    data = json.loads(result)

    assert data["ok"] is False
    assert "循环依赖" in data["error"]
    assert [task.title for task in store.list_tasks(wf.id)] == ["父任务", "子任务"]
    assert store.get_parent_task_ids(parent.id) == []
    assert store.get_parent_task_ids(child.id) == [parent.id]


def test_kanban_add_task_normalizes_assignee_to_team_roles(store: SQLiteKanbanStore) -> None:
    """kanban_add_task 工具应把非团队成员角色归一化为默认角色。"""
    from crew.dynamickanban.tools import create_kanban_registry

    wf = store.create_workflow("s_add", "测试新增任务")
    tools = create_kanban_registry(store, wf.id, actor="worker", valid_roles=["product_manager", "coder"])
    add_tool = tools["kanban_add_task"]

    import asyncio

    result = asyncio.run(add_tool.run({"title": "新任务", "detail": "", "assignee": "researcher"}))
    data = json.loads(result)
    assert data["assignee"] == "product_manager"

    tasks = store.list_tasks(wf.id)
    assert len(tasks) == 1
    assert tasks[0].assignee == "product_manager"






class _DummyTaskRuntime:
    """满足 SessionDispatcher 侧链流程的最小 task_runtime 桩。"""

    def __init__(self) -> None:
        self._tasks: dict[str, dict[str, Any]] = {}

    def create_runtime(self, **kwargs: Any) -> dict[str, Any]:
        task_id = f"task_{kwargs.get('request_id', 'r')}"
        self._tasks[task_id] = {
            "backgrounded": False,
            "output_ref": f"/tmp/{task_id}.json",
            "status": "running",
        }
        return {"task_id": task_id}

    def update(self, task_id: str, **kwargs: Any) -> None:
        self._tasks.setdefault(task_id, {}).update(kwargs)

    def get(self, task_id: str, **kwargs: Any) -> dict[str, Any]:
        return self._tasks.get(task_id, {"backgrounded": False, "output_ref": "", "status": "running"})

    def mark_running(self, task_id: str) -> None:
        pass

    def touch_activity(self, task_id: str, progress: Any = None, **kwargs: Any) -> None:
        pass

    def attach_worker(self, task_id: str, task: Any, *, cancel: Any) -> None:
        pass

    def finish(self, task_id: str, **kwargs: Any) -> None:
        self._tasks.setdefault(task_id, {})["status"] = kwargs.get("status", "completed")


@pytest.mark.asyncio
async def test_dispatcher_does_not_overwrite_dynamic_kanban_history() -> None:
    """SessionDispatcher 对 dynamic_kanban 模式不能执行 sidechain 收敛覆盖，否则历史会丢失。"""
    store = InMemorySessionStore()
    owner = "user-dk"

    async def inner(envelope: Envelope) -> Any:
        """模拟 DynamicKanbanManager：直接把本轮消息写到原始 session。"""
        sid = envelope.session_id
        store.append(sid, [Message(role="user", content="用户请求", timestamp=1.0)], owner_account_id=owner)
        store.append(
            sid,
            [Message(role="assistant", content="Dynamic Kanban 最终回复", timestamp=2.0)],
            owner_account_id=owner,
        )
        yield ResponseChunk.final(envelope.request_id, "Dynamic Kanban 最终回复", sequence=1)

    dispatcher = SessionDispatcher(
        inner,
        store,
        task_runtime=_DummyTaskRuntime(),
    )

    env = Envelope.of("用户请求", session_id="s_dk_dispatch", user_id=owner, mode="dynamic_kanban")
    chunks = [c async for c in dispatcher.run(env)]
    assert chunks[-1].kind == "final"

    history = store.load("s_dk_dispatch", owner_account_id=owner)
    assert len(history) == 2, f"dynamic_kanban 历史不应被覆盖，实际 {history!r}"
    assert history[0].role == "user"
    assert history[0].content == "用户请求"
    assert history[1].role == "assistant"
    assert history[1].content == "Dynamic Kanban 最终回复"


@pytest.mark.asyncio
async def test_dispatcher_overwrites_agent_history_via_sidechain() -> None:
    """普通 agent 模式仍走 sidechain 收敛：inner 写到 sidechain 的消息应被合并回原始 session。"""
    store = InMemorySessionStore()
    owner = "user-agent"

    async def inner(envelope: Envelope) -> Any:
        """模拟普通 SingleAgent：把用户消息和回复写到 sidechain session。"""
        sid = envelope.session_id
        store.append(sid, [Message(role="user", content="用户请求", timestamp=1.0)], owner_account_id=owner)
        store.append(sid, [Message(role="assistant", content="Agent 回复", timestamp=2.0)], owner_account_id=owner)
        yield ResponseChunk.final(envelope.request_id, "Agent 回复", sequence=1)

    dispatcher = SessionDispatcher(
        inner,
        store,
        task_runtime=_DummyTaskRuntime(),
    )

    env = Envelope.of("用户请求", session_id="s_agent_dispatch", user_id=owner, mode="agent")
    chunks = [c async for c in dispatcher.run(env)]
    assert chunks[-1].kind == "final"

    history = store.load("s_agent_dispatch", owner_account_id=owner)
    roles = [m.role for m in history]
    assert "user" in roles
    assert "assistant" in roles


# --------------------------------------------------------------------------- #
# WorkflowOrchestrator tests
# --------------------------------------------------------------------------- #
def _definition_phase(phase_id: str, next_phase_ids: list[str] | None = None) -> dict[str, Any]:
    phase: dict[str, Any] = {
        "id": phase_id,
        "name": phase_id,
        "agent_calls": [],
    }
    if next_phase_ids is not None:
        phase["next_phase_ids"] = next_phase_ids
    return phase


def test_definition_migrates_consistent_legacy_topology_to_v2_edges() -> None:
    legacy = {
        "summary": "legacy",
        "phases": [
            _definition_phase("a", ["b"]),
            _definition_phase("b", ["c"]),
            _definition_phase("c", []),
        ],
        "edges": [["a", "b"], ["b", "c"]],
    }

    definition = WorkflowDefinition.from_dict(legacy)

    assert definition.edges == [("a", "b"), ("b", "c")]
    persisted = definition.to_dict()
    assert persisted["schema_version"] == 2
    assert all("next_phase_ids" not in phase for phase in persisted["phases"])


def test_definition_migrates_next_only_and_double_empty_legacy_topology() -> None:
    next_only = WorkflowDefinition.from_dict(
        {
            "phases": [
                _definition_phase("a", ["b"]),
                _definition_phase("b", []),
            ]
        }
    )
    double_empty = WorkflowDefinition.from_dict(
        {
            "phases": [
                _definition_phase("a"),
                _definition_phase("b"),
                _definition_phase("c"),
            ],
            "edges": [],
        }
    )

    assert next_only.edges == [("a", "b")]
    assert double_empty.edges == [("a", "b"), ("b", "c")]


def test_definition_rejects_conflicting_legacy_topology_with_diagnostic() -> None:
    legacy = {
        "phases": [
            _definition_phase("a", ["b"]),
            _definition_phase("b", ["c"]),
            _definition_phase("c", []),
        ],
        "edges": [["a", "c"]],
    }

    with pytest.raises(WorkflowDefinitionMigrationError) as captured:
        WorkflowDefinition.from_dict(legacy)

    assert captured.value.diagnostic()["persisted_edges"] == [["a", "c"]]
    assert captured.value.diagnostic()["legacy_runtime_edges"] == [
        ["a", "b"],
        ["b", "c"],
    ]


@pytest.mark.parametrize(
    ("phases", "edges", "message"),
    [
        (["a"], [["a", "a"]], "自环"),
        (["a", "b"], [["a", "b"], ["b", "a"]], "入口|循环"),
        (["a", "b"], [["a", "missing"]], "未知 phase"),
        (["a", "a"], [], "重复 phase"),
        ([], [], "至少需要一个 phase"),
    ],
)
def test_v2_definition_rejects_illegal_dag(
    phases: list[str],
    edges: list[list[str]],
    message: str,
) -> None:
    data = {
        "schema_version": 2,
        "phases": [_definition_phase(phase_id) for phase_id in phases],
        "edges": edges,
    }

    with pytest.raises(ValueError, match=message):
        WorkflowDefinition.from_dict(data)


def test_phase_runtime_model_has_no_second_topology_surface() -> None:
    phase = Phase(id="only", name="Only")

    assert not hasattr(phase, "next_phase_ids")


def test_runtime_ready_order_comes_from_edges_not_phase_neighbors() -> None:
    definition = WorkflowDefinition(
        summary="branch and join",
        phases=[
            Phase(id="join", name="Join"),
            Phase(id="left", name="Left"),
            Phase(id="root", name="Root"),
            Phase(id="right", name="Right"),
        ],
        edges=[
            ("root", "left"),
            ("root", "right"),
            ("left", "join"),
            ("right", "join"),
        ],
    )
    runtime = object.__new__(WorkflowRuntime)
    state = RuntimeState(workflow_id="wf", current_phase_id="")

    assert runtime._current_phase(definition, state).id == "root"
    state.phase_results["root"] = PhaseResult(phase_id="root", status="done")
    assert runtime._advance_phase(definition, state) is True
    assert state.current_phase_id == "left"

    state.phase_results["left"] = PhaseResult(phase_id="left", status="done")
    assert runtime._advance_phase(definition, state) is True
    assert state.current_phase_id == "right"

    state.phase_results["right"] = PhaseResult(phase_id="right", status="done")
    assert runtime._advance_phase(definition, state) is True
    assert state.current_phase_id == "join"

    state.phase_results["join"] = PhaseResult(phase_id="join", status="done")
    assert runtime._advance_phase(definition, state) is False
    assert state.current_phase_id == ""


def test_runtime_board_dependencies_are_derived_from_definition_edges(
    store: SQLiteKanbanStore,
) -> None:
    workflow = store.create_workflow("dag-board", "DAG board")
    definition = WorkflowDefinition(
        summary="branch and join",
        phases=[
            Phase(id="join", name="Join", agent_calls=[AgentCall("join-call", "qa", "join")]),
            Phase(id="left", name="Left", agent_calls=[AgentCall("left-call", "dev", "left")]),
            Phase(id="root", name="Root", agent_calls=[AgentCall("root-call", "lead", "root")]),
            Phase(id="right", name="Right", agent_calls=[AgentCall("right-call", "dev", "right")]),
        ],
        edges=[
            ("root", "left"),
            ("root", "right"),
            ("left", "join"),
            ("right", "join"),
        ],
    )
    runtime = object.__new__(WorkflowRuntime)
    runtime.store = store

    runtime._sync_definition_to_board(workflow.id, definition)
    board = store.get_board_state(workflow.id)
    titles = {task["id"]: task["title"] for task in board["tasks"]}
    dependencies = {
        (titles[item["parent_task_id"]], titles[item["child_task_id"]])
        for item in board["dependencies"]
    }

    assert dependencies == {
        ("[root:root-call] root", "[left:left-call] left"),
        ("[root:root-call] root", "[right:right-call] right"),
        ("[left:left-call] left", "[join:join-call] join"),
        ("[right:right-call] right", "[join:join-call] join"),
    }


def test_runtime_board_resync_preserves_plan_extension_dependencies(
    store: SQLiteKanbanStore,
) -> None:
    """恢复时只替换 Runtime 投影边，不得删除显式 Plan Extension 的下游边。"""
    workflow = store.create_workflow("dag-board-extension", "DAG board extension")
    definition = WorkflowDefinition(
        summary="linear",
        phases=[
            Phase(id="root", name="Root", agent_calls=[AgentCall("root-call", "lead", "root")]),
            Phase(id="child", name="Child", agent_calls=[AgentCall("child-call", "dev", "child")]),
        ],
        edges=[("root", "child")],
    )
    runtime = object.__new__(WorkflowRuntime)
    runtime.store = store
    runtime._sync_definition_to_board(workflow.id, definition)
    projected_child = next(
        task for task in store.list_tasks(workflow.id) if task.title.startswith("[child:")
    )
    extension = store.add_task(
        workflow.id,
        "显式扩展任务",
        parent_task_ids=[projected_child.id],
    )

    runtime._sync_definition_to_board(workflow.id, definition)

    assert store.get_parent_task_ids(extension.id) == [projected_child.id]


@pytest.mark.asyncio
async def test_manager_quarantines_conflicting_stored_definition(
    db_path: str,
    base_registry: Registry,
    session_store: InMemorySessionStore,
    plugins: PluginManager,
) -> None:
    store = SQLiteKanbanStore(db_path)
    owner_store = store.for_owner("local")
    conflicting = {
        "phases": [
            _definition_phase("a", ["b"]),
            _definition_phase("b", []),
        ],
        "edges": [["b", "a"]],
    }
    workflow = owner_store.create_workflow(
        "legacy-conflict",
        "legacy conflict",
        context={"workflow_definition": conflicting},
    )
    manager = DynamicKanbanManager(
        store=store,
        provider=FakeProvider(),
        base_registry=base_registry,
        session_store=session_store,
        memory=NullMemory(),
        plugins=plugins,
        config=Config(db_path=db_path),
    )

    with pytest.raises(WorkflowDefinitionMigrationError):
        await manager._load_or_build_definition(workflow, workflow.title)

    quarantined = owner_store.get_workflow(workflow.id)
    assert quarantined is not None
    assert quarantined.status == "failed"
    diagnostic = quarantined.context["workflow_definition_quarantine"]
    assert diagnostic["persisted_edges"] == [["b", "a"]]
    assert diagnostic["legacy_runtime_edges"] == [["a", "b"]]


@pytest.mark.asyncio
async def test_manager_quarantines_invalid_v2_definition(
    db_path: str,
    base_registry: Registry,
    session_store: InMemorySessionStore,
    plugins: PluginManager,
) -> None:
    store = SQLiteKanbanStore(db_path)
    owner_store = store.for_owner("local")
    workflow = owner_store.create_workflow(
        "invalid-v2",
        "invalid v2",
        context={
            "workflow_definition": {
                "schema_version": 2,
                "phases": [_definition_phase("a"), _definition_phase("b")],
                "edges": [["a", "b"], ["b", "a"]],
            }
        },
    )
    manager = DynamicKanbanManager(
        store=store,
        provider=FakeProvider(),
        base_registry=base_registry,
        session_store=session_store,
        memory=NullMemory(),
        plugins=plugins,
        config=Config(db_path=db_path),
    )

    with pytest.raises(ValueError, match="入口|循环"):
        await manager._load_or_build_definition(workflow, workflow.title)

    quarantined = owner_store.get_workflow(workflow.id)
    assert quarantined is not None
    assert quarantined.status == "failed"
    assert "workflow_definition_quarantine" in quarantined.context




def _make_runtime_agent_factory(provider: FakeProvider, session_store: InMemorySessionStore, plugins: PluginManager):
    """构造用于 runtime 测试的 agent_factory。"""
    from crew.agent.runtime import SingleAgent
    from crew.agent.executor import BuiltinExecutor
    from crew.agent.loop import ToolCallGuardrailConfig
    from crew.agent.compact import ContextCompactor

    def factory(*, registry, system_prompt, agent_id="dk_worker", lightweight=True, user_type="internal", **kwargs):
        compactor = ContextCompactor(provider, enabled=False, store=None)
        executor = BuiltinExecutor(
            provider,
            registry,
            plugins,
            max_iterations=5,
            max_retries=1,
            backoff_seconds=0.1,
            guardrail_config=ToolCallGuardrailConfig(warnings_enabled=False, hard_stop_enabled=False),
            parallel_tools=False,
            empty_retry_max=1,
            continuation_max=1,
            max_parallel_tool_calls=1,
            max_delegate_tool_calls=0,
            compactor=compactor,
        )
        return SingleAgent(
            provider=provider,
            registry=registry,
            session_store=session_store,
            memory=NullMemory(),
            plugins=plugins,
            system_prompt=system_prompt,
            max_iterations=5,
            executor=executor,
            compactor=compactor,
            lightweight=lightweight,
            user_type=user_type,
            agent_id=agent_id,
        )
    return factory


@pytest.mark.asyncio
async def test_runtime_runs_single_phase(
    store: SQLiteKanbanStore,
    base_registry: Registry,
    session_store: InMemorySessionStore,
    plugins: PluginManager,
) -> None:
    """Runtime 执行单 phase 单 call，应产出 final chunk。"""
    provider = FakeProvider()
    runtime = WorkflowRuntime(
        store=store,
        agent_factory=_make_runtime_agent_factory(provider, session_store, plugins),
        base_registry=base_registry,
        provider=provider,
        max_concurrent=1,
    )
    wf = store.create_workflow("s_runtime", "hello")
    definition = WorkflowDefinition(
        summary="单阶段测试",
        phases=[
            Phase(
                id="phase_1",
                name="阶段1",
                agent_calls=[
                    AgentCall(
                        id="phase_1_call",
                        role="coder",
                        prompt="请回复 hello",
                        outputs=["text"],
                    )
                ],
            )
        ],
        max_concurrent=1,
    )
    env = Envelope.of("hello", session_id="s_runtime")
    chunks = [c async for c in runtime.run(wf, definition, "req_rt", env)]
    assert chunks[-1].kind == "final"

    tasks = store.list_tasks(wf.id)
    assert len(tasks) == 1
    assert tasks[0].status == "done"


@pytest.mark.asyncio
async def test_runtime_emits_workflow_progress_chunks(
    store: SQLiteKanbanStore,
    base_registry: Registry,
    session_store: InMemorySessionStore,
    plugins: PluginManager,
) -> None:
    """Runtime 执行 workflow 时应产出结构化 workflow_progress 帧。"""
    provider = FakeProvider()
    runtime = WorkflowRuntime(
        store=store,
        agent_factory=_make_runtime_agent_factory(provider, session_store, plugins),
        base_registry=base_registry,
        provider=provider,
        max_concurrent=1,
    )
    wf = store.create_workflow("s_progress", "progress test")
    definition = WorkflowDefinition(
        summary="进度帧测试",
        phases=[
            Phase(
                id="phase_1",
                name="阶段1",
                agent_calls=[
                    AgentCall(
                        id="phase_1_call",
                        role="coder",
                        prompt="请回复 hello",
                        outputs=["text"],
                    )
                ],
            ),
            Phase(
                id="phase_2",
                name="阶段2",
                agent_calls=[
                    AgentCall(
                        id="phase_2_call",
                        role="writer",
                        prompt="请综合",
                        outputs=["text"],
                    )
                ],
            ),
        ],
        max_concurrent=1,
    )
    env = Envelope.of("progress test", session_id="s_progress")
    chunks = [c async for c in runtime.run(wf, definition, "req_progress", env)]
    progress_chunks = [c for c in chunks if c.kind == "workflow_progress"]
    assert len(progress_chunks) >= 3, f"应至少产生启动、进入阶段、完成等 progress 帧，实际 {len(progress_chunks)}"

    first = progress_chunks[0]
    assert first.body["workflow_id"] == wf.id
    assert first.body["status"] == "running"
    assert first.body["current_phase"]["id"] == "phase_1"

    # 最终帧应标记为 done
    last_progress = progress_chunks[-1]
    assert last_progress.body["status"] == "done"
    assert last_progress.body.get("current_phase") is None or last_progress.body["current_phase"]["status"] == "done"


@pytest.mark.asyncio
async def test_runtime_pause_and_resume(
    db_path: str,
    base_registry: Registry,
    session_store: InMemorySessionStore,
    plugins: PluginManager,
) -> None:
    """Runtime 支持 pause/resume：暂停后可从同一 phase 继续执行。"""
    barrier = asyncio.Event()
    provider = FakeProvider()
    store = SQLiteKanbanStore(db_path).for_owner("local")

    def pausable_factory(*, registry, system_prompt, agent_id="dk_worker", **kwargs):
        from crew.agent.runtime import SingleAgent
        from crew.agent.executor import BuiltinExecutor
        from crew.agent.loop import ToolCallGuardrailConfig
        from crew.agent.compact import ContextCompactor

        compactor = ContextCompactor(provider, enabled=False, store=None)
        executor = BuiltinExecutor(
            provider,
            registry,
            plugins,
            max_iterations=5,
            guardrail_config=ToolCallGuardrailConfig(warnings_enabled=False, hard_stop_enabled=False),
            compactor=compactor,
        )
        agent = SingleAgent(
            provider=provider,
            registry=registry,
            session_store=session_store,
            memory=NullMemory(),
            plugins=plugins,
            system_prompt=system_prompt,
            max_iterations=5,
            executor=executor,
            compactor=compactor,
            lightweight=True,
            agent_id=agent_id,
        )
        orig_run = agent.run

        async def _run(env: Envelope):
            yield ResponseChunk.status_event(env.request_id, "working", 1)
            await barrier.wait()
            async for chunk in orig_run(env):
                yield chunk

        agent.run = _run
        return agent

    runtime = WorkflowRuntime(
        store=store,
        agent_factory=pausable_factory,
        base_registry=base_registry,
        provider=provider,
        max_concurrent=1,
    )
    wf = store.create_workflow("s_pause", "pause test")
    definition = WorkflowDefinition(
        summary="暂停恢复测试",
        phases=[
            Phase(
                id="phase_1",
                name="阶段1",
                agent_calls=[
                    AgentCall(
                        id="phase_1_call",
                        role="coder",
                        prompt="请回复 done",
                        outputs=["text"],
                    )
                ],
            )
        ],
        max_concurrent=1,
    )
    env = Envelope.of("pause test", session_id="s_pause")

    async def _consume(gen):
        return [c async for c in gen]

    task = asyncio.create_task(_consume(runtime.run(wf, definition, "req_p", env)))

    # 等待 runtime 开始执行 call
    for _ in range(100):
        tasks = store.list_tasks(wf.id)
        if tasks and tasks[0].status == "running":
            break
        await asyncio.sleep(0.01)

    runtime.request_pause()
    barrier.set()
    await task

    # 暂停后应出现 paused 或 final 之前的状态，且 workflow 为 paused
    wf = store.get_workflow(wf.id)
    assert wf.status == "paused"
    state = store.load_runtime_state(wf.id)
    assert state is not None
    assert state.status == "paused"

    # 保存 definition 到 context 以模拟 manager 的持久化
    store.update_workflow_status(wf.id, wf.status, context={**wf.context, "workflow_definition": definition.to_dict()})

    # resume
    store.resume_workflow(wf.id)
    runtime2 = WorkflowRuntime(
        store=store,
        agent_factory=_make_runtime_agent_factory(provider, session_store, plugins),
        base_registry=base_registry,
        provider=provider,
        max_concurrent=1,
    )
    resume_chunks = [c async for c in runtime2.run(wf, definition, "req_p2", env)]
    assert resume_chunks[-1].kind == "final"

    wf = store.get_workflow(wf.id)
    assert wf.status == "done"


@pytest.mark.asyncio
async def test_manager_pause_and_resume_stream(
    db_path: str,
    base_registry: Registry,
    session_store: InMemorySessionStore,
    plugins: PluginManager,
) -> None:
    """manager.pause() / resume_stream() 能让 workflow 暂停后继续。"""
    barrier = asyncio.Event()
    provider = FakeProvider()
    store = SQLiteKanbanStore(db_path)
    cfg = Config(db_path=db_path)

    def pausable_factory(*, registry, system_prompt, agent_id="dk_worker", **kwargs):
        from crew.agent.runtime import SingleAgent
        from crew.agent.executor import BuiltinExecutor
        from crew.agent.loop import ToolCallGuardrailConfig
        from crew.agent.compact import ContextCompactor

        compactor = ContextCompactor(provider, enabled=False, store=None)
        executor = BuiltinExecutor(
            provider,
            registry,
            plugins,
            max_iterations=5,
            guardrail_config=ToolCallGuardrailConfig(warnings_enabled=False, hard_stop_enabled=False),
            compactor=compactor,
        )
        agent = SingleAgent(
            provider=provider,
            registry=registry,
            session_store=session_store,
            memory=NullMemory(),
            plugins=plugins,
            system_prompt=system_prompt,
            max_iterations=5,
            executor=executor,
            compactor=compactor,
            lightweight=True,
            agent_id=agent_id,
        )
        orig_run = agent.run

        async def _run(env: Envelope):
            yield ResponseChunk.status_event(env.request_id, "working", 1)
            await barrier.wait()
            async for chunk in orig_run(env):
                yield chunk

        agent.run = _run
        return agent

    manager = DynamicKanbanManager(
        store=store,
        provider=provider,
        base_registry=base_registry,
        session_store=session_store,
        memory=NullMemory(),
        plugins=plugins,
        config=cfg,
        agent_factory=pausable_factory,
    )
    session_id = "s_mgr_pause"
    env = Envelope.of("长任务", session_id=session_id, mode="dynamic_kanban", request_id="req_mp")

    async def _consume(gen):
        return [c async for c in gen]

    task = asyncio.create_task(_consume(manager.interact(env)))

    # 等待 runtime 开始执行
    for _ in range(100):
        if manager._engines:
            break
        await asyncio.sleep(0.01)
    assert manager._engines, "runtime 应已注册"

    assert manager.pause(session_id, "测试暂停", owner_account_id="local") is True
    barrier.set()
    chunks = await task
    assert chunks[-1].kind == "final"

    owner_store = store.for_owner("local")
    wf = owner_store.get_latest_active_workflow_by_session(
        session_id,
        active_statuses={"paused"},
    )
    assert wf is not None
    assert wf.status == "paused"

    # resume
    history_before = session_store.load(session_id, owner_account_id="local")
    env2 = Envelope.of("继续", session_id=session_id, mode="dynamic_kanban", request_id="req_mp2")
    resume_chunks = [c async for c in manager.resume_stream(session_id, "req_mp2", env2)]
    assert resume_chunks[-1].kind == "final"

    wf = owner_store.get_latest_active_workflow_by_session(
        session_id,
        active_statuses={"done"},
    )
    assert wf is not None
    assert wf.status == "done"

    # resume 的 status/final 产出应落进会话历史，否则客户端重载后丢失
    history_after = session_store.load(session_id, owner_account_id="local")
    assert len(history_after) > len(history_before)
    final_text = resume_chunks[-1].body.get("text") or resume_chunks[-1].body.get("message") or ""
    assert any(
        m.role == "assistant" and m.content == final_text for m in history_after
    ), "resume 的 final 文本应持久化到会话历史"


def test_manager_build_tool_filter_respects_disabled_and_enabled_tools(
    db_path: str,
    base_registry: Registry,
    session_store: InMemorySessionStore,
    plugins: PluginManager,
) -> None:
    """enabled_tools / disabled_tools 应在 Dynamic Kanban worker 工具过滤中生效。"""
    cfg = Config(db_path=db_path, cron_enabled=False)
    store = SQLiteKanbanStore(db_path)
    manager = DynamicKanbanManager(
        store=store,
        provider=FakeProvider(),
        base_registry=base_registry,
        session_store=session_store,
        memory=NullMemory(),
        plugins=plugins,
        config=cfg,
    )
    # 1) disabled_tools 生效
    tool_filter = manager._build_tool_filter(
        {"disabled_tools": ["delegate_task"]},
        base_registry,
    )
    assert "delegate_task" not in tool_filter, "disabled_tools 中的工具应被过滤"
    assert tool_filter, "tool_filter 不应为空"

    # 2) enabled_tools 生效：只保留显式允许的工具
    tool_filter = manager._build_tool_filter(
        {"enabled_tools": ["file_read"]},
        base_registry,
    )
    assert tool_filter == ["file_read"], "enabled_tools 应严格限定可用工具"


def test_build_structured_summary_extracts_phase_results() -> None:
    """_build_structured_summary 应从 RuntimeState 提取结构化摘要，不调用 LLM。"""
    from crew.dynamickanban.models import Workflow

    workflow = Workflow(session_id="s_summary", title="测试需求", owner_account_id="local")
    definition = WorkflowDefinition(
        summary="摘要测试",
        phases=[
            Phase(
                id="phase_plan",
                name="规划",
                agent_calls=[
                    AgentCall(
                        id="plan_call",
                        role="planner",
                        prompt="做规划",
                        outputs=["plan"],
                    )
                ],
            ),
            Phase(
                id="phase_code",
                name="编码",
                agent_calls=[
                    AgentCall(
                        id="code_call",
                        role="coder",
                        prompt="写代码",
                        outputs=["code"],
                    )
                ],
            ),
        ],
    )
    state = RuntimeState(
        workflow_id=workflow.id,
        status="done",
        phase_results={
            "phase_plan": PhaseResult(
                phase_id="phase_plan",
                status="done",
                call_results={
                    "plan_call": AgentCallResult(
                        call_id="plan_call",
                        status="done",
                        text="确定使用 JWT + Redis 方案",
                        outputs={"plan": "JWT + Redis"},
                        artifacts=["/tmp/plan.md"],
                    )
                },
            ),
            "phase_code": PhaseResult(
                phase_id="phase_code",
                status="done",
                call_results={
                    "code_call": AgentCallResult(
                        call_id="code_call",
                        status="done",
                        text="def login(): pass",
                        outputs={"code": "def login(): pass"},
                    )
                },
            ),
        },
        variables={"final_answer": "登录功能已完成"},
    )

    payload = WorkflowRuntime._build_structured_summary(workflow, definition, state)
    assert payload["workflow_id"] == workflow.id
    assert payload["status"] == "done"
    assert payload["title"] == "测试需求"
    assert payload["output_variables"] == {"final_answer": "登录功能已完成"}
    assert len(payload["phases"]) == 2
    assert payload["phases"][0]["phase_name"] == "规划"
    assert payload["phases"][0]["calls"][0]["role"] == "planner"
    assert payload["phases"][0]["calls"][0]["text"] == "确定使用 JWT + Redis 方案"
    assert payload["phases"][0]["calls"][0]["outputs"] == {"plan": "JWT + Redis"}
    assert payload["phases"][0]["calls"][0]["artifacts"] == ["/tmp/plan.md"]
    assert payload["phases"][1]["calls"][0]["text"] == "def login(): pass"
    assert payload["failed_calls"] == []


def test_build_structured_summary_paused_guides_resume_button() -> None:
    """paused 状态的 structured_summary 应引导用户点 ▶️ 继续，而不是误导性的“发送消息继续”。"""
    from crew.dynamickanban.models import Workflow

    workflow = Workflow(session_id="s_summary_paused", title="暂停需求", owner_account_id="local")
    definition = WorkflowDefinition(
        summary="暂停摘要测试",
        phases=[Phase(id="phase_plan", name="规划", agent_calls=[])],
    )
    state = RuntimeState(workflow_id=workflow.id, status="paused", current_phase_id="phase_plan")

    payload = WorkflowRuntime._build_structured_summary(workflow, definition, state)
    assert payload["status"] == "paused"
    assert "▶️ 继续" in payload["message"]
    assert "发送消息" not in payload["message"]


def test_build_structured_summary_reports_failed_calls() -> None:
    """_build_structured_summary 应列出失败的 agent_call。"""
    from crew.dynamickanban.models import Workflow

    workflow = Workflow(session_id="s_fail", title="失败测试", owner_account_id="local")
    definition = WorkflowDefinition(
        summary="失败摘要测试",
        phases=[
            Phase(
                id="phase_code",
                name="编码",
                agent_calls=[
                    AgentCall(
                        id="code_call",
                        role="coder",
                        prompt="写代码",
                        outputs=["code"],
                    )
                ],
            ),
        ],
    )
    state = RuntimeState(
        workflow_id=workflow.id,
        status="failed",
        phase_results={
            "phase_code": PhaseResult(
                phase_id="phase_code",
                status="failed",
                call_results={
                    "code_call": AgentCallResult(
                        call_id="code_call",
                        status="failed",
                        error="模型调用超时",
                    )
                },
            ),
        },
    )

    payload = WorkflowRuntime._build_structured_summary(workflow, definition, state)
    assert payload["status"] == "failed"
    assert len(payload["failed_calls"]) == 1
    assert payload["failed_calls"][0]["call_id"] == "code_call"
    assert payload["failed_calls"][0]["error"] == "模型调用超时"



# --------------------------------------------------------------------------- #
# planning 模式（template / dynamic / hybrid）
# --------------------------------------------------------------------------- #












def _make_bare_runtime(store: SQLiteKanbanStore, **kwargs) -> WorkflowRuntime:
    def _dummy_factory(**factory_kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError("agent_factory 不应在这些测试中被调用")

    return WorkflowRuntime(
        store=store,
        agent_factory=_dummy_factory,
        base_registry=Registry(),
        provider=FakeProvider(),
        **kwargs,
    )


def _two_phase_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        summary="两阶段",
        phases=[
            Phase(
                id="p1",
                name="阶段1",
                agent_calls=[AgentCall(id="p1_call", role="lead", prompt="规划", outputs=["text"])],
            ),
            Phase(
                id="p2",
                name="阶段2",
                agent_calls=[AgentCall(id="p2_call", role="coder", prompt="执行", outputs=["text"])],
            ),
        ],
        edges=[("p1", "p2")],
    )


def test_plan_extension_writes_back_to_definition(store: SQLiteKanbanStore) -> None:
    """kanban_plan_next 的扩展应写回持久化 definition：新增 phase + edge，可被调度与恢复。"""
    runtime = _make_bare_runtime(store)
    wf = store.create_workflow("s_ext", "扩展测试")
    definition = _two_phase_definition()
    runtime._active_definition = definition
    runtime._active_workflow_id = wf.id
    runtime._sync_definition_to_board(wf.id, definition)

    # 找到 p1 对应的看板任务作为父任务
    p1_task = next(t for t in store.list_tasks(wf.id) if t.title.startswith("[p1:"))
    delta = PlanDelta(
        add_tasks=[
            {
                "title": "补充调研",
                "detail": "补充调研某主题并写入 workdir",
                "assignee": "lead",
                "parent_task_ids": [p1_task.id],
            }
        ],
        add_dependencies=[],
    )
    added = store.apply_plan_extension(wf.id, delta)
    runtime._apply_plan_extension_to_definition(wf.id, delta, added)

    # definition 增加了新 phase，且有 p1 -> 新 phase 的边
    new_phase = next(p for p in definition.phases if p.id not in ("p1", "p2"))
    assert ("p1", new_phase.id) in definition.edges
    assert new_phase.agent_calls[0].role == "lead"

    # 持久化到 workflow.context，resume 时 _load_or_build_definition 能读到
    wf_after = store.get_workflow(wf.id)
    stored = WorkflowDefinition.from_dict(wf_after.context["workflow_definition"])
    assert any(p.id == new_phase.id for p in stored.phases)
    assert ("p1", new_phase.id) in stored.edges

    # runtime_call_titles 记录了新 call -> 看板标题映射，状态可回写
    titles = wf_after.context["runtime_call_titles"]
    assert titles[new_phase.agent_calls[0].id] == "补充调研"

    # 新 phase 在 p1 完成后 ready
    state = RuntimeState(workflow_id=wf.id)
    state.phase_results["p1"] = PhaseResult(phase_id="p1", status="done")
    state.completed_phase_ids.append("p1")
    ready = definition.ready_phase_ids(
        completed_phase_ids={"p1"},
        terminal_phase_ids={"p1"},
    )
    assert new_phase.id in ready


def test_sync_definition_to_board_reuses_extension_task_titles(store: SQLiteKanbanStore) -> None:
    """resume 时再次同步 definition 到看板，扩展任务不应重复建卡。"""
    runtime = _make_bare_runtime(store)
    wf = store.create_workflow("s_ext2", "扩展幂等测试")
    definition = _two_phase_definition()
    runtime._active_definition = definition
    runtime._active_workflow_id = wf.id
    runtime._sync_definition_to_board(wf.id, definition)

    p1_task = next(t for t in store.list_tasks(wf.id) if t.title.startswith("[p1:"))
    delta = PlanDelta(
        add_tasks=[
            {
                "title": "补充任务X",
                "detail": "补充细节",
                "assignee": "coder",
                "parent_task_ids": [p1_task.id],
            }
        ],
    )
    added = store.apply_plan_extension(wf.id, delta)
    runtime._apply_plan_extension_to_definition(wf.id, delta, added)

    before = len(store.list_tasks(wf.id))
    # 模拟 resume：重新同步 definition 到看板
    runtime._sync_definition_to_board(wf.id, definition)
    after = len(store.list_tasks(wf.id))
    assert after == before, "扩展任务在 resume 同步时被重复建卡"


def test_plan_next_tool_invokes_definition_callback(store: SQLiteKanbanStore) -> None:
    """PlanNextTool 应用扩展后应调用 on_plan_extension 回调。"""
    from crew.dynamickanban.tools import create_kanban_registry

    wf = store.create_workflow("s_cb", "回调测试")
    lead_task = store.add_task(wf.id, "规划", assignee="lead")
    calls: list[tuple[PlanDelta, list]] = []
    tools = create_kanban_registry(
        store,
        wf.id,
        actor="worker",
        valid_roles=["lead", "coder"],
        on_plan_extension=lambda delta, added: calls.append((delta, added)),
    )
    plan_json = json.dumps(
        {"add_tasks": [{"title": "下游任务", "assignee": "coder", "parent_task_ids": ["CURRENT_TASK_ID"]}]},
        ensure_ascii=False,
    )
    result = asyncio.run(tools["kanban_plan_next"].run({"task_id": lead_task.id, "plan_json": plan_json}))
    data = json.loads(result)
    assert data["ok"] is True
    assert data["definition_synced"] is True
    assert len(calls) == 1
    assert len(calls[0][1]) == 1


def test_worker_registry_contains_workflow_bound_kanban_tools(store: SQLiteKanbanStore) -> None:
    """Runtime 为 worker 构建的 registry 应包含绑定当前 workflow 的看板工具。"""
    runtime = _make_bare_runtime(store)
    call = AgentCall(id="c1", role="coder", prompt="x", outputs=["text"])
    reg_a = runtime._build_worker_registry("wf_a", call)
    reg_b = runtime._build_worker_registry("wf_b", call)

    tool_a = reg_a.get("kanban_plan_next")
    tool_b = reg_b.get("kanban_plan_next")
    assert tool_a.workflow_id == "wf_a"
    assert tool_b.workflow_id == "wf_b"
    assert tool_a is not tool_b
    # base registry 的工具仍然可用
    assert "kanban_add_task" in reg_a.names()


# --------------------------------------------------------------------------- #
# 失败自动 replan / steer 重规划
# --------------------------------------------------------------------------- #
class _FakeRepairOrchestrator:
    """模拟 orchestrator.build_repair_phases。"""

    def __init__(self, phases: list[Phase] | None = None, fail: bool = False) -> None:
        self._phases = phases or [
            Phase(
                id="repair_1",
                name="修复阶段",
                agent_calls=[
                    AgentCall(id="repair_1_call", role="coder", prompt="修复问题", outputs=["text"])
                ],
            )
        ]
        self._fail = fail
        self.contexts: list[dict] = []

    async def build_repair_phases(self, request, context):
        self.contexts.append(context)
        if self._fail:
            raise RuntimeError("LLM 不可用")
        # 每次返回新实例，避免 id 冲突副作用污染
        return [
            Phase(
                id=p.id,
                name=p.name,
                agent_calls=[
                    AgentCall(id=c.id, role=c.role, prompt=c.prompt, outputs=list(c.outputs))
                    for c in p.agent_calls
                ],
            )
            for p in self._phases
        ]


def _failed_state(workflow_id: str) -> RuntimeState:
    state = RuntimeState(workflow_id=workflow_id)
    state.phase_results["p1"] = PhaseResult(phase_id="p1", status="done")
    state.completed_phase_ids.append("p1")
    state.phase_results["p2"] = PhaseResult(
        phase_id="p2",
        status="failed",
        call_results={
            "p2_call": AgentCallResult(call_id="p2_call", status="failed", error="超时"),
        },
        error="阶段 p2 验证失败超过最大重试次数 1",
    )
    return state


@pytest.mark.asyncio
async def test_try_replan_appends_repair_and_resets_failed_phase(store: SQLiteKanbanStore) -> None:
    """失败 replan：新增修复 phase 链接到失败 phase 前，失败 phase 重置待重跑。"""
    orchestrator = _FakeRepairOrchestrator()
    runtime = _make_bare_runtime(store, orchestrator=orchestrator, max_replans=2)
    wf = store.create_workflow("s_replan", "重规划测试")
    definition = _two_phase_definition()
    runtime._active_definition = definition
    runtime._active_workflow_id = wf.id
    runtime._sync_definition_to_board(wf.id, definition)
    state = _failed_state(wf.id)
    store.save_runtime_state(state)

    seq = iter(range(1, 100))
    chunks = await runtime._try_replan(
        workflow=wf,
        definition=definition,
        phase=definition.phases[1],
        state=state,
        request_id="req_replan",
        seq_fn=lambda: next(seq),
    )

    assert chunks is not None
    # 修复 phase 接入：repair_1 -> p2
    assert any(p.id == "repair_1" for p in definition.phases)
    assert ("repair_1", "p2") in definition.edges
    # 失败 phase 被重置，重试次数清零，replan 计数 +1
    assert state.phase_results["p2"].status == "pending"
    assert state.phase_results["p2"].call_results == {}
    assert "p2" not in state.phase_retry_counts
    assert state.replan_count == 1
    assert state.current_phase_id == "repair_1"
    # 修复上下文包含失败信息
    assert orchestrator.contexts[0]["failed_phase_id"] == "p2"
    assert orchestrator.contexts[0]["failed_calls"][0]["error"] == "超时"
    # definition 已持久化
    stored = WorkflowDefinition.from_dict(store.get_workflow(wf.id).context["workflow_definition"])
    assert any(p.id == "repair_1" for p in stored.phases)


@pytest.mark.asyncio
async def test_try_replan_respects_budget(store: SQLiteKanbanStore) -> None:
    """replan 次数达到上限后不再重规划，返回 None 走原失败路径。"""
    orchestrator = _FakeRepairOrchestrator()
    runtime = _make_bare_runtime(store, orchestrator=orchestrator, max_replans=1)
    wf = store.create_workflow("s_budget", "预算测试")
    definition = _two_phase_definition()
    runtime._active_definition = definition
    runtime._active_workflow_id = wf.id
    state = _failed_state(wf.id)
    state.replan_count = 1

    chunks = await runtime._try_replan(
        workflow=wf,
        definition=definition,
        phase=definition.phases[1],
        state=state,
        request_id="req_budget",
        seq_fn=lambda: 1,
    )
    assert chunks is None
    assert orchestrator.contexts == []  # 未调用 LLM


@pytest.mark.asyncio
async def test_try_replan_returns_none_when_llm_fails(store: SQLiteKanbanStore) -> None:
    """LLM 生成修复阶段失败时不改变 definition/state，走原失败路径。"""
    orchestrator = _FakeRepairOrchestrator(fail=True)
    runtime = _make_bare_runtime(store, orchestrator=orchestrator, max_replans=2)
    wf = store.create_workflow("s_replan_fail", "重规划失败测试")
    definition = _two_phase_definition()
    runtime._active_definition = definition
    runtime._active_workflow_id = wf.id
    state = _failed_state(wf.id)

    chunks = await runtime._try_replan(
        workflow=wf,
        definition=definition,
        phase=definition.phases[1],
        state=state,
        request_id="req_rf",
        seq_fn=lambda: 1,
    )
    assert chunks is None
    assert [p.id for p in definition.phases] == ["p1", "p2"]
    assert state.phase_results["p2"].status == "failed"


@pytest.mark.asyncio
async def test_build_repair_phases_parses_llm_output() -> None:
    """orchestrator.build_repair_phases 应解析 LLM 输出的修复阶段 JSON。"""
    provider = FakeProvider(
        script=[
            ChatResponse(
                text='{"phases": [{"id": "repair_x", "name": "修复",'
                ' "agent_calls": [{"id": "rc", "role": "coder", "prompt": "修复", "outputs": ["text"]}]}]}'
            )
        ]
    )
    orchestrator = WorkflowOrchestrator(provider)
    phases = await orchestrator.build_repair_phases(
        "做个功能",
        {"failed_phase_id": "p2", "error": "超时"},
    )
    assert len(phases) == 1
    assert phases[0].id == "repair_x"
    assert phases[0].agent_calls[0].role == "coder"


# --------------------------------------------------------------------------- #
# steer：运行中注入指令
# --------------------------------------------------------------------------- #
def test_manager_steer_writes_notes(
    db_path: str,
    base_registry: Registry,
    session_store: InMemorySessionStore,
    plugins: PluginManager,
) -> None:
    """manager.steer 应把指令写入活跃 workflow 的 context；无活跃 workflow 返回 False。"""
    provider = FakeProvider()
    cfg = Config(db_path=db_path)
    store = SQLiteKanbanStore(db_path).for_owner("local")
    manager = DynamicKanbanManager(
        store=store,
        provider=provider,
        base_registry=base_registry,
        session_store=session_store,
        memory=NullMemory(),
        plugins=plugins,
        config=cfg,
    )
    wf = store.create_workflow("s_steer", "steer 测试")

    assert manager.steer("s_steer", "请增加安全审查环节", owner_account_id="local") is True
    ctx = store.get_workflow(wf.id).context
    assert len(ctx["steer_notes"]) == 1
    assert ctx["steer_notes"][0]["text"] == "请增加安全审查环节"

    assert manager.steer("s_steer", "再加一个性能测试", owner_account_id="local") is True
    ctx = store.get_workflow(wf.id).context
    assert len(ctx["steer_notes"]) == 2

    assert manager.steer("s_unknown", "x", owner_account_id="local") is False


@pytest.mark.asyncio
async def test_try_apply_steer_consumes_notes_and_replans(store: SQLiteKanbanStore) -> None:
    """Runtime 应消费 steer_notes：标记已应用并生成调整阶段插入下一 phase 前。"""
    orchestrator = _FakeRepairOrchestrator()
    runtime = _make_bare_runtime(store, orchestrator=orchestrator, max_replans=2)
    wf = store.create_workflow("s_steer_rt", "steer 运行时测试")
    definition = _two_phase_definition()
    runtime._active_definition = definition
    runtime._active_workflow_id = wf.id
    runtime._sync_definition_to_board(wf.id, definition)

    # p1 已完成，p2 是下一个待执行 phase；注入 steer 指令
    state = RuntimeState(workflow_id=wf.id)
    state.phase_results["p1"] = PhaseResult(phase_id="p1", status="done")
    state.completed_phase_ids.append("p1")
    state.current_phase_id = "p2"
    ctx = dict(wf.context or {})
    ctx["steer_notes"] = [{"text": "请增加安全审查环节", "ts": 1.0}]
    store.update_workflow_status(wf.id, wf.status, context=ctx)

    chunks = await runtime._try_apply_steer(
        workflow=wf,
        definition=definition,
        state=state,
        request_id="req_steer",
        seq_fn=lambda: 1,
    )

    assert chunks is not None
    # 调整阶段插入到 p2 之前
    assert any(p.id == "repair_1" for p in definition.phases)
    assert ("repair_1", "p2") in definition.edges
    assert state.current_phase_id == "repair_1"
    assert orchestrator.contexts[0]["steer_instruction"] == "请增加安全审查环节"
    # steer_notes 已标记应用，再次调用返回 None
    assert store.get_workflow(wf.id).context["steer_applied"] == 1
    again = await runtime._try_apply_steer(
        workflow=wf,
        definition=definition,
        state=state,
        request_id="req_steer2",
        seq_fn=lambda: 1,
    )
    assert again is None


@pytest.mark.asyncio
async def test_runtime_executes_plan_next_extension_end_to_end(
    store: SQLiteKanbanStore,
    base_registry: Registry,
    session_store: InMemorySessionStore,
    plugins: PluginManager,
) -> None:
    """端到端扩图闭环：规划型 worker 调 kanban_plan_next，新增阶段被 Runtime 调度执行。"""

    class _PlanningWorkerProvider(FakeProvider):
        """第 1 次 chat：lead 调 kanban_plan_next；后续：文本回复。"""

        def __init__(self, store: SQLiteKanbanStore, workflow_id: str) -> None:
            super().__init__()
            self._store = store
            self._workflow_id = workflow_id
            self._step = 0

        async def chat(self, messages, tools=None):
            self._step += 1
            if self._step == 1:
                # 此时 runtime 已同步 definition 到看板，唯一的卡就是规划任务
                board_tasks = self._store.list_tasks(self._workflow_id)
                assert len(board_tasks) == 1
                plan_json = json.dumps(
                    {
                        "add_tasks": [
                            {
                                "title": "执行开发",
                                "detail": "完成代码实现",
                                "assignee": "coder",
                                "parent_task_ids": ["CURRENT_TASK_ID"],
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
                return ChatResponse(
                    tool_calls=[
                        ToolCall(
                            id="tc_plan_next",
                            name="kanban_plan_next",
                            arguments={"task_id": board_tasks[0].id, "plan_json": plan_json},
                        )
                    ]
                )
            if self._step == 2:
                return ChatResponse(text="计划已提交")
            if self._step == 3:
                return ChatResponse(text="开发完成")
            return ChatResponse(text="综合结果")

    wf = store.create_workflow("s_e2e", "扩图闭环端到端")
    provider = _PlanningWorkerProvider(store, wf.id)
    runtime = WorkflowRuntime(
        store=store,
        agent_factory=_make_runtime_agent_factory(provider, session_store, plugins),
        base_registry=base_registry,
        provider=provider,
        max_concurrent=1,
    )
    definition = WorkflowDefinition(
        summary="先规划后执行",
        phases=[
            Phase(
                id="plan",
                name="规划",
                agent_calls=[
                    AgentCall(id="plan_call", role="lead", prompt="制定执行计划", outputs=["text"])
                ],
            )
        ],
        max_concurrent=1,
    )
    env = Envelope.of("做个功能", session_id="s_e2e")
    chunks = [c async for c in runtime.run(wf, definition, "req_e2e", env)]

    assert chunks[-1].kind == "final"
    # 扩图写回 definition 并被调度执行
    assert len(definition.phases) == 2
    new_phase = definition.phases[1]
    assert new_phase.agent_calls[0].role == "coder"
    assert ("plan", new_phase.id) in definition.edges

    board = {t.title: t for t in store.list_tasks(wf.id)}
    assert "执行开发" in board
    assert board["执行开发"].status == "done"

    # 持久化的 definition 包含新阶段（resume 不丢）
    stored = WorkflowDefinition.from_dict(store.get_workflow(wf.id).context["workflow_definition"])
    assert len(stored.phases) == 2

    state = store.load_runtime_state(wf.id)
    assert state.status == "done"
    assert state.phase_results[new_phase.id].status == "done"



# --------------------------------------------------------------------------- #
# P1 缺陷回归测试：resume 竞态 / stop 重试 / 硬取消清理 / 断连可恢复
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_manager_resume_stream_waits_for_previous_engine(
    db_path: str,
    base_registry: Registry,
    session_store: InMemorySessionStore,
    plugins: PluginManager,
) -> None:
    """pause 落盘后旧 runtime 尚未退出时，resume_stream 应等待而不是并发新建 runtime。"""
    barrier = asyncio.Event()
    provider = FakeProvider()
    cfg = Config(db_path=db_path)
    store = SQLiteKanbanStore(db_path)

    def slow_agent_factory(**kwargs):
        class _SlowAgent:
            async def run(self, env: Envelope):
                yield ResponseChunk.status_event(env.request_id, "working", 1)
                await barrier.wait()
                yield ResponseChunk.final(env.request_id, "done", 2)

        return _SlowAgent()

    manager = DynamicKanbanManager(
        store=store,
        provider=provider,
        base_registry=base_registry,
        session_store=session_store,
        memory=NullMemory(),
        plugins=plugins,
        config=cfg,
        agent_factory=slow_agent_factory,
    )
    session_id = "s_resume_race"
    env = Envelope.of("长任务", session_id=session_id, mode="dynamic_kanban", request_id="req_rr")

    async def _consume(gen: AsyncIterator[ResponseChunk]) -> list[ResponseChunk]:
        return [c async for c in gen]

    interact_task = asyncio.create_task(_consume(manager.interact(env)))
    for _ in range(100):
        if manager._engines:
            break
        await asyncio.sleep(0.01)
    assert manager._engines, "runtime 应已注册"
    old_runtime = next(iter(manager._engines.values()))

    # pause 立即落盘 paused，但旧 runtime 仍卡在 phase 内（barrier 未放行）
    assert manager.pause(session_id, "测试暂停", owner_account_id="local") is True

    env2 = Envelope.of("继续", session_id=session_id, mode="dynamic_kanban", request_id="req_rr2")
    resume_task = asyncio.create_task(_consume(manager.resume_stream(session_id, "req_rr2", env2)))

    # resume 应等待旧 runtime 退出，而不是立刻覆盖 engine 注册
    await asyncio.sleep(0.3)
    engine_key = next(iter(manager._engines))
    assert manager._engines[engine_key] is old_runtime, "旧 runtime 未退出前不应注册新 runtime"

    # 放行旧 runtime：它协作式 pause 退出后，resume 才接管注册并跑完
    barrier.set()
    await interact_task
    resume_chunks = await resume_task
    assert resume_chunks[-1].kind == "final"

    owner_store = store.for_owner("local")
    wf = owner_store.get_latest_workflow_by_session(session_id)
    assert wf is not None
    assert wf.status == "done"
    # 旧 runtime 的安全 pop 不应挤掉新 runtime 的注册；两边退出后注册表应为空
    assert not manager._engines


@pytest.mark.asyncio
async def test_runtime_stop_does_not_retry_failed_calls(
    store: SQLiteKanbanStore,
    base_registry: Registry,
) -> None:
    """phase 内收到 stop 后，失败重试循环不应把所有 call 重跑一遍。"""
    call_count = 0
    started = asyncio.Event()
    release = asyncio.Event()
    provider = FakeProvider()

    def counting_factory(**kwargs):
        class _CountingAgent:
            async def run(self, env: Envelope):
                nonlocal call_count
                call_count += 1
                started.set()
                await release.wait()
                yield ResponseChunk.final(env.request_id, "done", 1)

        return _CountingAgent()

    runtime = WorkflowRuntime(
        store=store,
        agent_factory=counting_factory,
        base_registry=base_registry,
        provider=provider,
        max_concurrent=1,
    )
    wf = store.create_workflow("s_stop_retry", "stop retry test")
    definition = WorkflowDefinition(
        summary="停止重试测试",
        phases=[
            Phase(
                id="phase_1",
                name="阶段1",
                agent_calls=[
                    AgentCall(
                        id="phase_1_call",
                        role="coder",
                        prompt="请回复 done",
                        outputs=["text"],
                    )
                ],
            )
        ],
        max_concurrent=1,
    )
    env = Envelope.of("stop retry test", session_id="s_stop_retry")

    async def _consume(gen: AsyncIterator[ResponseChunk]) -> list[ResponseChunk]:
        return [c async for c in gen]

    task = asyncio.create_task(_consume(runtime.run(wf, definition, "req_sr", env)))
    await asyncio.wait_for(started.wait(), timeout=5)

    # call 执行期间收到 stop：本次 call 按中断失败收尾，但不应触发重试重跑
    runtime.request_stop()
    release.set()
    chunks = await task
    assert chunks[-1].kind == "final"
    assert call_count == 1, f"stop 后不应重跑失败 call，实际执行 {call_count} 次"

    assert store.get_workflow(wf.id).status == "failed"


@pytest.mark.asyncio
async def test_runtime_hard_cancel_cleans_board_tasks(
    store: SQLiteKanbanStore,
    base_registry: Registry,
) -> None:
    """dispatcher.stop 的硬取消路径（request_stop + task.cancel）不应残留 running/pending 任务。"""
    started = asyncio.Event()
    provider = FakeProvider()

    def stuck_factory(**kwargs):
        class _StuckAgent:
            async def run(self, env: Envelope):
                started.set()
                await asyncio.Event().wait()  # 永不返回，等待被取消
                yield ResponseChunk.final(env.request_id, "done", 1)  # pragma: no cover

        return _StuckAgent()

    runtime = WorkflowRuntime(
        store=store,
        agent_factory=stuck_factory,
        base_registry=base_registry,
        provider=provider,
        max_concurrent=1,
    )
    wf = store.create_workflow("s_hard_cancel", "hard cancel test")
    definition = WorkflowDefinition(
        summary="硬取消测试",
        phases=[
            Phase(
                id="phase_1",
                name="阶段1",
                agent_calls=[
                    AgentCall(
                        id="phase_1_call",
                        role="coder",
                        prompt="请回复 done",
                        outputs=["text"],
                    )
                ],
            )
        ],
        max_concurrent=1,
    )
    env = Envelope.of("hard cancel test", session_id="s_hard_cancel")

    async def _consume(gen: AsyncIterator[ResponseChunk]) -> list[ResponseChunk]:
        return [c async for c in gen]

    task = asyncio.create_task(_consume(runtime.run(wf, definition, "req_hc", env)))
    await asyncio.wait_for(started.wait(), timeout=5)

    # 模拟 dispatcher.stop：先 interrupt 置标志，紧接着 task.cancel() 硬取消
    runtime.request_stop()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    tasks = store.list_tasks(wf.id)
    assert tasks, "看板应已同步任务"
    assert all(
        t.status not in ("pending", "ready", "running") for t in tasks
    ), f"硬取消后不应残留未收尾任务：{[(t.id, t.status) for t in tasks]}"
    assert store.get_workflow(wf.id).status == "failed"


@pytest.mark.asyncio
async def test_manager_resume_stream_disconnect_keeps_workflow_resumable(
    db_path: str,
    base_registry: Registry,
    session_store: InMemorySessionStore,
    plugins: PluginManager,
) -> None:
    """resume SSE 客户端断连（取消 resume 生成器）不应把 workflow 打进 failed 终态。"""
    gate = asyncio.Event()
    agent_started = asyncio.Event()
    provider = FakeProvider()
    cfg = Config(db_path=db_path)
    store = SQLiteKanbanStore(db_path)

    def slow_agent_factory(**kwargs):
        class _SlowAgent:
            async def run(self, env: Envelope):
                agent_started.set()
                yield ResponseChunk.status_event(env.request_id, "working", 1)
                await gate.wait()
                yield ResponseChunk.final(env.request_id, "done", 2)

        return _SlowAgent()

    manager = DynamicKanbanManager(
        store=store,
        provider=provider,
        base_registry=base_registry,
        session_store=session_store,
        memory=NullMemory(),
        plugins=plugins,
        config=cfg,
        agent_factory=slow_agent_factory,
    )
    owner_store = store.for_owner("local")
    session_id = "s_resume_disconnect"
    env = Envelope.of("长任务", session_id=session_id, mode="dynamic_kanban", request_id="req_rd")

    async def _consume(gen: AsyncIterator[ResponseChunk]) -> list[ResponseChunk]:
        return [c async for c in gen]

    # 先跑出 paused 状态的 workflow
    interact_task = asyncio.create_task(_consume(manager.interact(env)))
    for _ in range(100):
        if manager._engines:
            break
        await asyncio.sleep(0.01)
    assert manager._engines, "runtime 应已注册"
    assert manager.pause(session_id, "测试暂停", owner_account_id="local") is True
    gate.set()
    await interact_task
    wf = owner_store.get_latest_active_workflow_by_session(session_id, active_statuses={"paused"})
    assert wf is not None

    # 模板 definition 只有一个 phase，暂停时已跑完；追加一个未执行的 phase，
    # 让 resume 后有真实工作可做（断连时 call 必须仍在执行中）
    definition = WorkflowDefinition.from_dict(wf.context["workflow_definition"])
    definition.phases.append(
        Phase(
            id="phase_2",
            name="阶段2",
            agent_calls=[
                AgentCall(id="phase_2_call", role="writer", prompt="请综合", outputs=["text"])
            ],
        )
    )
    owner_store.update_workflow_status(
        wf.id,
        wf.status,
        context={**wf.context, "workflow_definition": definition.to_dict()},
    )

    # resume 后模拟客户端断开：直接取消消费 resume 流的任务
    gate.clear()
    agent_started.clear()
    env2 = Envelope.of("继续", session_id=session_id, mode="dynamic_kanban", request_id="req_rd2")
    resume_task = asyncio.create_task(_consume(manager.resume_stream(session_id, "req_rd2", env2)))
    await asyncio.wait_for(agent_started.wait(), timeout=5)
    resume_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await resume_task

    # 断连后 workflow 应落 paused（可恢复），绝不落 failed 终态，也不残留 running 任务
    wf = owner_store.get_workflow(wf.id)
    assert wf.status == "paused", f"断连后 workflow 应可恢复，实际 {wf.status}"
    state = owner_store.load_runtime_state(wf.id)
    assert state is not None
    assert state.status == "paused"
    assert all(
        t.status not in ("pending", "ready", "running") for t in owner_store.list_tasks(wf.id)
    )

    # 再次 resume 可以跑完
    gate.set()
    env3 = Envelope.of("继续", session_id=session_id, mode="dynamic_kanban", request_id="req_rd3")
    resume_chunks = [c async for c in manager.resume_stream(session_id, "req_rd3", env3)]
    assert resume_chunks[-1].kind == "final"
    assert owner_store.get_workflow(wf.id).status == "done"



# --------------------------------------------------------------------------- #
# 回归测试：paused 可中止 / pause 不耗 loop_count / pause 不启动新 call
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_manager_interrupt_paused_workflow_marks_failed(
    db_path: str,
    base_registry: Registry,
    session_store: InMemorySessionStore,
    plugins: PluginManager,
) -> None:
    """paused 状态的 workflow（runtime 已退出）点中止应落 failed，而不是静默无效。"""
    barrier = asyncio.Event()
    provider = FakeProvider()
    cfg = Config(db_path=db_path)
    store = SQLiteKanbanStore(db_path)

    def slow_agent_factory(**kwargs):
        class _SlowAgent:
            async def run(self, env: Envelope):
                yield ResponseChunk.status_event(env.request_id, "working", 1)
                await barrier.wait()
                yield ResponseChunk.final(env.request_id, "done", 2)

        return _SlowAgent()

    manager = DynamicKanbanManager(
        store=store,
        provider=provider,
        base_registry=base_registry,
        session_store=session_store,
        memory=NullMemory(),
        plugins=plugins,
        config=cfg,
        agent_factory=slow_agent_factory,
    )
    owner_store = store.for_owner("local")
    session_id = "s_interrupt_paused"
    env = Envelope.of("长任务", session_id=session_id, mode="dynamic_kanban", request_id="req_ip")

    async def _consume(gen: AsyncIterator[ResponseChunk]) -> list[ResponseChunk]:
        return [c async for c in gen]

    interact_task = asyncio.create_task(_consume(manager.interact(env)))
    for _ in range(100):
        if manager._engines:
            break
        await asyncio.sleep(0.01)
    assert manager._engines, "runtime 应已注册"

    assert manager.pause(session_id, "测试暂停", owner_account_id="local") is True
    barrier.set()
    await interact_task
    wf = owner_store.get_latest_active_workflow_by_session(session_id, active_statuses={"paused"})
    assert wf is not None, "workflow 应处于 paused"
    assert not manager._engines, "runtime 应已退出"

    # paused 状态中止：无 runtime 可置标志，应走 DB 兜底落 failed
    assert manager.interrupt(session_id, "中止", owner_account_id="local") is True
    wf = owner_store.get_workflow(wf.id)
    assert wf.status == "failed"
    state = owner_store.load_runtime_state(wf.id)
    assert state is not None and state.status == "failed"
    assert all(
        t.status not in ("pending", "ready", "running") for t in owner_store.list_tasks(wf.id)
    )


@pytest.mark.asyncio
async def test_runtime_pause_does_not_consume_loop_count(
    store: SQLiteKanbanStore,
    base_registry: Registry,
) -> None:
    """暂停是控制操作，不应消耗 loop_count 执行配额。"""
    provider = FakeProvider()

    def factory(**kwargs):
        class _Agent:
            async def run(self, env: Envelope):
                yield ResponseChunk.final(env.request_id, "done", 1)

        return _Agent()

    runtime = WorkflowRuntime(
        store=store,
        agent_factory=factory,
        base_registry=base_registry,
        provider=provider,
        max_concurrent=1,
    )
    wf = store.create_workflow("s_pause_loop_count", "pause loop count test")
    definition = WorkflowDefinition(
        summary="暂停计数测试",
        phases=[
            Phase(
                id="phase_1",
                name="阶段1",
                agent_calls=[
                    AgentCall(id="phase_1_call", role="coder", prompt="请回复 done", outputs=["text"])
                ],
            )
        ],
        max_concurrent=1,
    )
    env = Envelope.of("pause loop count test", session_id="s_pause_loop_count")

    runtime.request_pause()
    chunks = [c async for c in runtime.run(wf, definition, "req_plc", env)]
    assert chunks[-1].kind == "final"

    state = store.load_runtime_state(wf.id)
    assert state is not None
    assert state.status == "paused"
    assert state.loop_count == 0, f"暂停不应消耗 loop_count，实际 {state.loop_count}"


@pytest.mark.asyncio
async def test_runtime_pause_skips_unstarted_calls(
    store: SQLiteKanbanStore,
    base_registry: Registry,
) -> None:
    """phase 内收到 pause：在跑的 call 收尾，未启动的 call 不再启动，resume 后续跑。"""
    call_count = 0
    first_started = asyncio.Event()
    release = asyncio.Event()
    provider = FakeProvider()

    def factory(**kwargs):
        class _Agent:
            async def run(self, env: Envelope):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    first_started.set()
                    await release.wait()
                yield ResponseChunk.final(env.request_id, f"done{call_count}", 1)

        return _Agent()

    runtime = WorkflowRuntime(
        store=store,
        agent_factory=factory,
        base_registry=base_registry,
        provider=provider,
        max_concurrent=1,
    )
    wf = store.create_workflow("s_pause_grain", "pause grain test")
    definition = WorkflowDefinition(
        summary="暂停粒度测试",
        phases=[
            Phase(
                id="phase_1",
                name="阶段1",
                agent_calls=[
                    AgentCall(id="call_a", role="coder", prompt="任务A", outputs=["text"]),
                    AgentCall(id="call_b", role="coder", prompt="任务B", outputs=["text"]),
                ],
            )
        ],
        max_concurrent=1,
    )
    env = Envelope.of("pause grain test", session_id="s_pause_grain")

    async def _consume(gen: AsyncIterator[ResponseChunk]) -> list[ResponseChunk]:
        return [c async for c in gen]

    task = asyncio.create_task(_consume(runtime.run(wf, definition, "req_pg", env)))
    await asyncio.wait_for(first_started.wait(), timeout=5)

    # 第一个 call 执行期间暂停：它跑完收尾，第二个 call 不应启动
    runtime.request_pause()
    release.set()
    chunks = await task
    assert chunks[-1].kind == "final"
    assert call_count == 1, f"暂停后未启动的 call 不应执行，实际执行 {call_count} 次"

    state = store.load_runtime_state(wf.id)
    assert state is not None and state.status == "paused"
    pr = state.phase_results["phase_1"]
    assert pr.status == "running", "有未执行 call 的 phase 不能误判终态"
    assert pr.call_results["call_a"].status == "done"
    assert "call_b" not in pr.call_results
    assert store.get_workflow(wf.id).status == "paused"

    # resume：新 runtime 从 call_b 续跑，不重跑 call_a
    wf = store.resume_workflow(wf.id)
    runtime2 = WorkflowRuntime(
        store=store,
        agent_factory=factory,
        base_registry=base_registry,
        provider=provider,
        max_concurrent=1,
    )
    resume_chunks = [c async for c in runtime2.run(wf, definition, "req_pg2", env)]
    assert resume_chunks[-1].kind == "final"
    assert call_count == 2, f"resume 应只补跑未执行的 call，实际总执行 {call_count} 次"
    assert store.get_workflow(wf.id).status == "done"
