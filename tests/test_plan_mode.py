"""对话级 Plan 模式 + 会话 Todo 工具测试。

覆盖：
- TodoStore：replace / merge / format_for_injection（实现了 的行为）
- PlanModeManager：enter → request_approval → approve / reject 状态机
- 计划文件读写（plan_file）
- enter_plan_mode 兼容拒绝 / exit_plan_mode / todo 工具行为
- ToolRunner 的 plan 只读门控：file_write 仅放行计划文件
- SingleAgent._effective_tool_filter：plan 激活时收窄到只读白名单
"""

import json

import pytest

from crew.core.mocks import FakeProvider, InMemorySessionStore, NullMemory
from crew.core.runctx import current_agent_workdir, current_owner_account_id, current_session_id
from crew.core.types import ChatResponse, Message, ToolCall
from crew.plugins.manager import PluginManager
from crew.tools.registry import Registry, register_builtin_tools

from crew.agent.plan import (
    PLAN_MODE_TOOLS,
    PlanModeManager,
    count_user_turns_since_last_plan_attachment,
    create_plan_attachment_message,
    get_plan_mode_attachment_messages,
    plan_path,
    read_plan,
    write_plan,
)
from crew.agent.plan.todo import TodoStore
from crew.agent.plan.tools import register_plan_tools
from crew.state.session_store import SQLiteSessionStore


@pytest.fixture(autouse=True)
def _isolate_plan_state_files():
    """每个测试前清理 plans 目录下的 state.json，避免跨测试/跨运行的 plan 状态文件残留
    被 PlanModeManager._restore 读到而污染 is_active 等查询。计划文件 plan_*.md 不受影响
    （每 session 独占子目录，互不串读）。"""
    from crew.agent.plan.manager import plans_dir

    directory = plans_dir()
    if directory.is_dir():
        for f in directory.rglob("state.json"):
            try:
                f.unlink()
            except OSError:
                pass
    yield


# --------------------------------------------------------------------------- #
# TodoStore（实现）
# --------------------------------------------------------------------------- #
def test_todo_store_replace_and_merge():
    store = TodoStore()
    store.write([
        {"id": "T1", "content": "探索代码", "status": "pending"},
        {"id": "T2", "content": "写计划", "status": "pending"},
    ])
    assert len(store.read()) == 2

    # replace：整表覆盖
    store.write([{"id": "T3", "content": "执行", "status": "in_progress"}])
    items = store.read()
    assert [i["id"] for i in items] == ["T3"]

    # merge：按 id 更新 + 追加
    store.write([
        {"id": "T3", "content": "执行", "status": "completed"},
        {"id": "T4", "content": "验证", "status": "pending"},
    ], merge=True)
    items = {i["id"]: i for i in store.read()}
    assert items["T3"]["status"] == "completed"
    assert items["T4"]["content"] == "验证"


def test_todo_format_for_injection_only_active():
    store = TodoStore()
    assert store.format_for_injection() is None
    store.write([
        {"id": "T1", "content": "已完成项", "status": "completed"},
        {"id": "T2", "content": "进行中项", "status": "in_progress"},
        {"id": "T3", "content": "待办项", "status": "pending"},
        {"id": "T4", "content": "取消项", "status": "cancelled"},
    ])
    out = store.format_for_injection()
    # 只重注入 pending / in_progress
    assert "进行中项" in out and "待办项" in out
    assert "已完成项" not in out and "取消项" not in out


def test_todo_invalid_status_normalized():
    store = TodoStore()
    store.write([{"id": "X", "content": "c", "status": "bogus"}])
    assert store.read()[0]["status"] == "pending"


# --------------------------------------------------------------------------- #
# PlanModeManager 状态机
# --------------------------------------------------------------------------- #
def test_plan_manager_state_machine():
    mgr = PlanModeManager()
    sid = "s1"
    assert not mgr.is_active(sid)
    assert mgr.phase(sid) == "inactive"

    mgr.enter(sid)
    assert mgr.is_active(sid)
    assert not mgr.is_awaiting_approval(sid)
    assert mgr.phase(sid) == "active"

    mgr.request_approval(sid)
    assert mgr.is_awaiting_approval(sid)
    assert mgr.is_active(sid)  # 等审批期间仍只读
    assert mgr.phase(sid) == "review"

    mgr.approve(sid)
    assert not mgr.is_active(sid)
    assert not mgr.is_awaiting_approval(sid)
    assert mgr.phase(sid) == "approved"
    # just_approved 一次性
    assert mgr.take_just_approved(sid) is True
    assert mgr.take_just_approved(sid) is False


def test_plan_manager_enter_after_approve_starts_new_plan():
    """已批准后再 enter：开启新计划文件，不把旧计划重新拉回可编辑/待审批。"""
    mgr = PlanModeManager()
    sid = "s_reenter_after_approve"
    mgr.enter(sid)
    first = plan_path(sid)
    write_plan(sid, "# 旧计划\n")
    mgr.request_approval(sid)
    mgr.approve(sid)
    assert mgr.phase(sid) == "approved"

    mgr.enter(sid)
    second = plan_path(sid)
    assert mgr.phase(sid) == "active"
    assert second != first
    assert (read_plan(sid) or "").strip() == ""


def test_plan_manager_enter_is_idempotent():
    mgr = PlanModeManager()
    sid = "s1_repeat"
    mgr.enter(sid)
    first = plan_path(sid)
    mgr.request_approval(sid)
    mgr.enter(sid)
    assert mgr.is_active(sid)
    assert not mgr.is_awaiting_approval(sid)
    assert plan_path(sid) == first


def test_plan_manager_reject_keeps_active():
    mgr = PlanModeManager()
    sid = "s2"
    mgr.enter(sid)
    mgr.request_approval(sid)
    mgr.reject(sid)
    assert mgr.is_active(sid)
    assert not mgr.is_awaiting_approval(sid)
    assert mgr.phase(sid) == "revising"


def test_plan_manager_reject_and_cancel_terminal_phases():
    mgr = PlanModeManager()
    sid = "s2_terminal"
    mgr.enter(sid)
    mgr.request_approval(sid)
    mgr.reject_and_exit(sid)
    assert not mgr.is_active(sid)
    assert mgr.phase(sid) == "rejected"

    mgr.enter(sid)
    mgr.exit(sid)
    assert not mgr.is_active(sid)
    assert mgr.phase(sid) == "cancelled"


def test_plan_manager_reset():
    mgr = PlanModeManager()
    sid = "s3"
    mgr.enter(sid)
    mgr.todo_store(sid).write([{"id": "A", "content": "x", "status": "pending"}])
    mgr.reset(sid)
    assert not mgr.is_active(sid)
    assert not mgr.todo_store(sid).has_items()


def test_plan_manager_reset_removes_plan_directory(tmp_path, monkeypatch):
    """删会话 / reset 必须清掉该会话整个 plans/<owner>/<sid>/，避免 plan_*.md 长期占盘。"""
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    from crew.agent.plan.manager import _plan_dir

    mgr = PlanModeManager()
    sid = "s-reset-dir"
    owner = "A:uid-a"
    path = write_plan(sid, "# plan body\n\nstep 1", owner_account_id=owner)
    plan_dir = _plan_dir(sid, owner_account_id=owner)
    assert path.is_file()
    assert plan_dir.is_dir()
    # 同会话多次计划会留下历史 md；reset 应整目录删除，不只删 state.json
    (plan_dir / "plan_old_history.md").write_text("# old", encoding="utf-8")

    mgr.enter(sid, owner_account_id=owner)
    mgr.reset(sid, owner_account_id=owner)

    assert not mgr.is_active(sid, owner_account_id=owner)
    assert not plan_dir.exists()
    assert not path.exists()


def test_plan_manager_restores_legacy_active_awaiting_state(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    from crew.agent.plan.manager import _plan_dir

    sid = "legacy-state"
    owner = "A:uid-a"
    write_plan(sid, "# old", owner_account_id=owner)
    state_path = _plan_dir(sid, owner_account_id=owner) / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"active": True, "awaiting": True}), encoding="utf-8")

    restored = PlanModeManager()
    assert restored.phase(sid, owner_account_id=owner) == "review"
    assert restored.is_active(sid, owner_account_id=owner)
    assert restored.is_awaiting_approval(sid, owner_account_id=owner)


def test_plan_approval_sets_internal_todo_reminder():
    mgr = PlanModeManager()
    sid = "s-reminder"
    mgr.enter(sid)
    mgr.request_approval(sid)
    mgr.approve(sid)

    reminder = mgr.take_todo_reminder(sid)
    assert reminder is not None
    assert "todo_reminder" in reminder
    assert mgr.take_todo_reminder(sid) is None


def test_todo_use_clears_plan_approval_reminder():
    mgr = PlanModeManager()
    sid = "s-reminder-clear"
    mgr.enter(sid)
    mgr.request_approval(sid)
    mgr.approve(sid)

    mgr.mark_todo_used(sid)
    assert mgr.take_todo_reminder(sid) is None


def test_plan_manager_todo_store_is_owner_scoped():
    mgr = PlanModeManager()
    sid = "same"

    mgr.todo_store(sid, owner_account_id="A:uid-a").write([
        {"id": "A", "content": "owner A", "status": "pending"}
    ])
    mgr.todo_store(sid, owner_account_id="B:uid-b").write([
        {"id": "B", "content": "owner B", "status": "pending"}
    ])

    assert mgr.todo_store(sid, owner_account_id="A:uid-a").read()[0]["content"] == "owner A"
    assert mgr.todo_store(sid, owner_account_id="B:uid-b").read()[0]["content"] == "owner B"


def test_plan_manager_hydrates_history_by_owner():
    store = InMemorySessionStore()
    sid = "same"
    store.save(sid, [
        Message(role="tool", content=json.dumps({"todos": [{"id": "A", "content": "owner A", "status": "pending"}]}), name="todo")
    ], owner_account_id="A:uid-a")
    store.save(sid, [
        Message(role="tool", content=json.dumps({"todos": [{"id": "B", "content": "owner B", "status": "pending"}]}), name="todo")
    ], owner_account_id="B:uid-b")

    mgr = PlanModeManager(session_store=store)

    assert mgr.todo_store(sid, owner_account_id="A:uid-a").read()[0]["content"] == "owner A"
    assert mgr.todo_store(sid, owner_account_id="B:uid-b").read()[0]["content"] == "owner B"


# --------------------------------------------------------------------------- #
# 计划文件读写
# --------------------------------------------------------------------------- #
def test_plan_file_read_write(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    monkeypatch.delenv("CREW_TASK_WORKSPACE_ROOT", raising=False)
    sid = "sess-abc"
    assert read_plan(sid) is None
    p = write_plan(sid, "# 计划\n步骤1")
    assert p == plan_path(sid)
    assert "plans" in p.parts  # 落在 .crew/plans/<owner>/<sid>/ 子目录下
    assert p.name.startswith("plan_")
    assert p.suffix == ".md"
    assert read_plan(sid) == "# 计划\n步骤1"


def test_plan_file_is_owner_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    monkeypatch.delenv("CREW_TASK_WORKSPACE_ROOT", raising=False)
    sid = "same"

    path_a = write_plan(sid, "# A", owner_account_id="A:uid-a")
    path_b = write_plan(sid, "# B", owner_account_id="B:uid-b")

    assert path_a != path_b
    # owner 体现在子目录段，不再进文件名
    assert "A_uid-a" in str(path_a)
    assert "B_uid-b" in str(path_b)
    assert "A_uid-a" not in str(path_b)
    assert "B_uid-b" not in str(path_a)
    assert read_plan(sid, owner_account_id="A:uid-a") == "# A"
    assert read_plan(sid, owner_account_id="B:uid-b") == "# B"
    assert read_plan(sid) is None


def test_reconcile_file_changes_drops_added_then_deleted(tmp_path):
    """本轮新建后又已删除的路径：对账后从累计与本轮摘要中剔除，不落盘。"""
    from crew.agent.plan import PlanModeManager

    mgr = PlanModeManager()
    sid = "sess-fc-drop"
    ghost = tmp_path / "_smoke.js"
    ghost.write_text("console.log(1)", encoding="utf-8")
    change = {
        "path": str(ghost),
        "name": ghost.name,
        "added": 1,
        "removed": 0,
        "status": "added",
        "diff": [{"line": 0, "kind": "add", "text": "console.log(1)"}],
    }
    store = mgr.file_change_store(sid)
    store.append(change)
    mgr.record_turn_file_change(sid, change)
    ghost.unlink()

    reconciled = mgr.reconcile_file_changes(sid)
    assert reconciled == []
    assert mgr.file_change_store(sid) == []
    assert mgr.drain_turn_file_changes(sid) == []


def test_reconcile_file_changes_drops_session_created_even_if_modified(tmp_path):
    """本会话新建后再次写入变成 modified，删除后仍应整条剔除（临时脚本）。"""
    from crew.agent.plan import PlanModeManager

    mgr = PlanModeManager()
    sid = "sess-fc-created-mod"
    ghost = tmp_path / "_smoke2.js"
    ghost.write_text("v1", encoding="utf-8")
    first = {
        "path": str(ghost),
        "name": ghost.name,
        "added": 1,
        "removed": 0,
        "status": "added",
        "created_in_session": True,
        "diff": [],
    }
    store = mgr.file_change_store(sid)
    store.append(first)
    mgr.record_turn_file_change(sid, first)
    # 二次写入：status 升为 modified，但保留 created_in_session
    second = {
        "path": str(ghost),
        "name": ghost.name,
        "added": 2,
        "removed": 1,
        "status": "modified",
        "created_in_session": True,
        "diff": [],
    }
    store[:] = [second]
    mgr.record_turn_file_change(sid, second)
    ghost.unlink()

    reconciled = mgr.reconcile_file_changes(sid)
    assert reconciled == []
    assert mgr.drain_turn_file_changes(sid) == []


def test_reconcile_file_changes_marks_modified_as_deleted(tmp_path):
    """已有文件被修改后再删除：对账后保留并标为 deleted。"""
    from crew.agent.plan import PlanModeManager

    mgr = PlanModeManager()
    sid = "sess-fc-del"
    target = tmp_path / "keep_then_gone.py"
    target.write_text("x = 1\n", encoding="utf-8")
    change = {
        "path": str(target),
        "name": target.name,
        "added": 1,
        "removed": 1,
        "status": "modified",
        "diff": [],
    }
    store = mgr.file_change_store(sid)
    store.append(change)
    mgr.record_turn_file_change(sid, change)
    target.unlink()

    reconciled = mgr.reconcile_file_changes(sid)
    assert len(reconciled) == 1
    assert reconciled[0]["status"] == "deleted"
    assert reconciled[0]["path"] == str(target)
    drained = mgr.drain_turn_file_changes(sid)
    assert len(drained) == 1
    assert drained[0]["status"] == "deleted"


def test_reconcile_keeps_existing_added_file(tmp_path):
    """新建且仍在磁盘：对账后保持 added。"""
    from crew.agent.plan import PlanModeManager

    mgr = PlanModeManager()
    sid = "sess-fc-keep"
    target = tmp_path / "new.html"
    target.write_text("<html></html>", encoding="utf-8")
    change = {
        "path": str(target),
        "name": target.name,
        "added": 1,
        "removed": 0,
        "status": "added",
        "diff": [],
    }
    mgr.file_change_store(sid).append(change)
    mgr.record_turn_file_change(sid, change)

    reconciled = mgr.reconcile_file_changes(sid)
    assert len(reconciled) == 1
    assert reconciled[0]["status"] == "added"


def test_reconcile_resolves_relative_path_via_workdir(tmp_path, monkeypatch):
    """相对路径按 agent workdir 绝对化后再 is_file，不依赖进程 cwd。"""
    from crew.agent.plan import PlanModeManager

    work = tmp_path / "ws"
    work.mkdir()
    rel_name = "rel_only.py"
    (work / rel_name).write_text("ok\n", encoding="utf-8")
    token = current_agent_workdir.set(str(work))
    try:
        mgr = PlanModeManager()
        sid = "sess-rel"
        change = {
            "path": rel_name,
            "name": rel_name,
            "added": 1,
            "removed": 0,
            "status": "added",
            "diff": [],
        }
        mgr.file_change_store(sid).append(change)
        monkeypatch.chdir(tmp_path)
        reconciled = mgr.reconcile_file_changes(sid)
        assert len(reconciled) == 1
        assert reconciled[0]["path"] == rel_name
    finally:
        current_agent_workdir.reset(token)


def test_format_approved_plan_content_truncates():
    from crew.agent.plan import PLAN_APPROVED_CONTENT_MAX_CHARS, format_approved_plan_content

    short = "hello plan"
    assert format_approved_plan_content(short) == short
    long = "x" * (PLAN_APPROVED_CONTENT_MAX_CHARS + 500)
    out = format_approved_plan_content(long)
    assert "truncated" in out
    assert out.startswith("x" * 20)
    # 正文部分被截到上限；整段含省略提示，应短于原文
    assert len(out) < len(long)
    assert out[:PLAN_APPROVED_CONTENT_MAX_CHARS] == "x" * PLAN_APPROVED_CONTENT_MAX_CHARS


# --------------------------------------------------------------------------- #
# 工具行为
# --------------------------------------------------------------------------- #
@pytest.fixture
def plan_registry():
    r = Registry()
    mgr = PlanModeManager()
    register_plan_tools(r, mgr)
    return r, mgr


async def test_enter_plan_mode_tool_rejected_for_model_entry(plan_registry):
    r, mgr = plan_registry
    sid = "sess-enter"
    token = current_session_id.set(sid)
    owner_token = current_owner_account_id.set("A:uid-a")
    try:
        res = await r.execute(ToolCall("c1", "enter_plan_mode", {}))
        assert '"error"' in res.content
        assert "不能由模型主动调用" in res.content
        assert not mgr.is_active(sid, owner_account_id="A:uid-a")
    finally:
        current_session_id.reset(token)
        current_owner_account_id.reset(owner_token)


async def test_exit_plan_mode_requires_plan_file(plan_registry, tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    monkeypatch.delenv("CREW_TASK_WORKSPACE_ROOT", raising=False)
    r, mgr = plan_registry
    sid = "sess-exit"
    token = current_session_id.set(sid)
    owner_token = current_owner_account_id.set("A:uid-a")
    try:
        mgr.enter(sid, owner_account_id="A:uid-a")
        # 计划文件为空 → 返回带 "error" 字段的 JSON 硬错误，强制模型先写计划。
        # 同时仍登记一帧「计划为空」review（empty=True）供 ws 推送提示卡，但不进入 awaiting。
        res = await r.execute(ToolCall("c1", "exit_plan_mode", {}))
        assert '"error"' in res.content
        assert "计划文件为空" in res.content
        assert not mgr.is_awaiting_approval(sid, owner_account_id="A:uid-a")
        review = mgr.take_pending_review(sid, owner_account_id="A:uid-a")
        assert review is not None and review.get("empty") is True

        # 写入计划后 → exit 成功，登记非空 review，进入待审批
        write_plan(sid, "# 计划\n做事", owner_account_id="A:uid-a")
        res2 = await r.execute(ToolCall("c2", "exit_plan_mode", {}))
        assert '"error"' not in res2.content
        assert mgr.is_awaiting_approval(sid, owner_account_id="A:uid-a")
        review2 = mgr.take_pending_review(sid, owner_account_id="A:uid-a")
        assert review2 is not None and review2.get("empty") is False
        assert review2.get("status") == "pending"
        assert review2.get("phase") == "review"
        assert "# 计划" in review2.get("plan", "")
    finally:
        current_session_id.reset(token)
        current_owner_account_id.reset(owner_token)


async def test_exit_plan_mode_repeat_while_awaiting_is_idempotent(plan_registry, tmp_path, monkeypatch):
    """已 awaiting 时模型重复调 exit_plan_mode：不应重新 submit_review，pending_review 不被覆盖。"""
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    monkeypatch.delenv("CREW_TASK_WORKSPACE_ROOT", raising=False)
    r, mgr = plan_registry
    sid = "sess-repeat"
    token = current_session_id.set(sid)
    owner_token = current_owner_account_id.set("A:uid-a")
    try:
        mgr.enter(sid, owner_account_id="A:uid-a")
        write_plan(sid, "# 计划\n做事", owner_account_id="A:uid-a")
        # 第一次提交 → awaiting + pending_review
        res1 = await r.execute(ToolCall("c1", "exit_plan_mode", {}))
        assert not res1.is_error
        assert mgr.is_awaiting_approval(sid, owner_account_id="A:uid-a")
        # 第二次重复调用 → 返回「请勿重复」，pending_review 不被重新登记（take 仍能拿到第一次的）
        res2 = await r.execute(ToolCall("c2", "exit_plan_mode", {}))
        assert not res2.is_error
        assert "请勿重复" in res2.content
        review = mgr.take_pending_review(sid, owner_account_id="A:uid-a")
        assert review is not None and review["empty"] is False
    finally:
        current_session_id.reset(token)
        current_owner_account_id.reset(owner_token)


async def test_exit_plan_mode_ignored_when_not_active(plan_registry, tmp_path, monkeypatch):
    """审批通过后 plan 已退出：再调 exit_plan_mode 不应重新置位 awaiting。"""
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    monkeypatch.delenv("CREW_TASK_WORKSPACE_ROOT", raising=False)
    r, mgr = plan_registry
    sid = "sess-exit-after-approve"
    token = current_session_id.set(sid)
    try:
        # 走一遍完整流程：进入 → 写计划 → 提交审批 → 批准
        mgr.enter(sid)
        write_plan(sid, "# 计划\n做事")
        mgr.request_approval(sid)
        mgr.approve(sid)
        assert not mgr.is_active(sid)
        assert not mgr.is_awaiting_approval(sid)

        # 任务执行完毕后模型又调 exit_plan_mode → 必须被拦下，不重新进入待审批
        res = await r.execute(ToolCall("c1", "exit_plan_mode", {}))
        assert '"error"' in res.content
        assert not mgr.is_awaiting_approval(sid)
    finally:
        current_session_id.reset(token)


async def test_todo_tool_via_registry(plan_registry):
    r, mgr = plan_registry
    sid = "sess-todo"
    token = current_session_id.set(sid)
    owner_token = current_owner_account_id.set("A:uid-a")
    try:
        res = await r.execute(ToolCall("c1", "todo", {
            "todos": [{"id": "T1", "content": "干活", "status": "pending"}]
        }))
        data = json.loads(res.content)
        assert data["summary"]["total"] == 1
        # 与 manager 共享同一 store
        assert mgr.todo_store(sid, owner_account_id="A:uid-a").has_items()
    finally:
        current_session_id.reset(token)
        current_owner_account_id.reset(owner_token)


async def test_todo_tool_works_without_plan_active(plan_registry):
    r, mgr = plan_registry
    sid = "sess-normal-todo"
    token = current_session_id.set(sid)
    try:
        assert not mgr.is_active(sid)
        res = await r.execute(ToolCall("c1", "todo", {
            "todos": [{"id": "N1", "content": "普通任务步骤", "status": "in_progress"}]
        }))
        data = json.loads(res.content)
        assert data["todos"][0]["content"] == "普通任务步骤"
        assert mgr.todo_store(sid).read()[0]["status"] == "in_progress"
    finally:
        current_session_id.reset(token)


# --------------------------------------------------------------------------- #
# ToolRunner 只读门控
# --------------------------------------------------------------------------- #
def _make_runner(mgr, sid):
    from crew.agent.loop.tool_guardrails import ToolCallGuardrailController
    from crew.agent.loop.tool_runner import ToolRunner

    return ToolRunner(
        Registry(),
        plugins=None,
        guardrails=ToolCallGuardrailController(),
        session_id=sid,
        plan_manager=mgr,
    )


def test_plan_mode_block_file_write(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    monkeypatch.delenv("CREW_TASK_WORKSPACE_ROOT", raising=False)
    mgr = PlanModeManager()
    sid = "sess-gate"
    runner = _make_runner(mgr, sid)
    owner_token = current_owner_account_id.set("A:uid-a")

    try:
        other = str(tmp_path / "other.py")

        # plan 未激活 → 不拦截
        tc_other = ToolCall("c1", "file_write", {"path": other, "content": "x"})
        assert runner._plan_mode_block(tc_other) is None

        # plan 激活 → 写非计划文件被拦截
        mgr.enter(sid, owner_account_id="A:uid-a")
        blocked = runner._plan_mode_block(tc_other)
        assert blocked is not None and "只读" in blocked

        # 写计划文件放行
        plan_file = str(plan_path(sid, owner_account_id="A:uid-a"))
        tc_plan = ToolCall("c2", "file_write", {"path": plan_file, "content": "# 计划"})
        assert runner._plan_mode_block(tc_plan) is None

        # 只读工具不受门控影响
        tc_read = ToolCall("c3", "file_read", {"path": other})
        assert runner._plan_mode_block(tc_read) is None
    finally:
        current_owner_account_id.reset(owner_token)


def test_plan_mode_allows_plan_file_in_crew_home(tmp_path, monkeypatch):
    """计划文件在 Layer 1（.crew/plans/），允许写；其他路径拒绝。"""
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    monkeypatch.delenv("CREW_TASK_WORKSPACE_ROOT", raising=False)
    mgr = PlanModeManager()
    sid = "sess-plan-layer1"
    runner = _make_runner(mgr, sid)
    owner_token = current_owner_account_id.set("A:uid-a")

    try:
        mgr.enter(sid, owner_account_id="A:uid-a")
        plan = plan_path(sid, owner_account_id="A:uid-a")
        # 计划文件应在 .crew/plans/<owner>/<sid>/ 下（Layer 1）
        assert "plans" in plan.parts
        assert (tmp_path / ".crew") in plan.parents

        # 允许写计划文件
        tc_plan = ToolCall("c1", "file_write", {"path": str(plan), "content": "# 计划"})
        assert runner._plan_mode_block(tc_plan) is None

        # 其他任意路径被拦截
        tc_other = ToolCall("c2", "file_write", {"path": str(tmp_path / "other.md"), "content": "x"})
        blocked = runner._plan_mode_block(tc_other)
        assert blocked is not None
        assert "plans" in blocked and "plan_" in blocked
    finally:
        current_owner_account_id.reset(owner_token)


def test_todo_snapshot_event_not_plan_dependent():
    mgr = PlanModeManager()
    sid = "sess-todo-snapshot"
    mgr.todo_store(sid).write([{"id": "T1", "content": "普通任务", "status": "pending"}])
    runner = _make_runner(mgr, sid)
    seq = 0

    def next_seq():
        nonlocal seq
        seq += 1
        return seq

    event = runner._todo_snapshot_event("rid", next_seq)
    assert event.kind == "todo_updated"
    assert event.body["todos"][0]["content"] == "普通任务"


def test_executor_appends_internal_todo_reminder_after_approval():
    from crew.agent.executor.builtin import BuiltinExecutor
    from crew.core.types import Message

    mgr = PlanModeManager()
    sid = "sess-exec-reminder"
    mgr.enter(sid)
    mgr.request_approval(sid)
    mgr.approve(sid)

    class _Stub:
        plan_manager = mgr

    base = [Message.system("s")]
    messages = BuiltinExecutor._maybe_append_todo_reminder(_Stub(), base, sid)
    assert len(messages) == 2
    assert "todo_reminder" in messages[-1].content
    assert messages[-1].is_meta is True
    assert BuiltinExecutor._maybe_append_todo_reminder(_Stub(), base, sid) == base


def test_plan_approved_reminder_injects_plan_file_content(tmp_path, monkeypatch):
    """批准后执行轮 reminder 须内嵌落盘正文（含手改），不能只给路径。"""
    from crew.agent.plan import PLAN_APPROVED_REMINDER, write_plan
    from crew.agent.runtime import SingleAgent

    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    monkeypatch.delenv("CREW_TASK_WORKSPACE_ROOT", raising=False)

    mgr = PlanModeManager()
    sid = "sess-approved-inject"
    mgr.enter(sid)
    edited = "# 手改计划\n\n- 步骤 B（用户补充）"
    write_plan(sid, edited)
    mgr.request_approval(sid)
    mgr.approve(sid)

    agent = SingleAgent.__new__(SingleAgent)
    agent.plan_manager = mgr
    blocks = agent._plan_reminder_blocks(sid, owner_account_id="")
    assert blocks, "just_approved 应注入至少一块 reminder"
    body = blocks[0]
    assert "BEGIN APPROVED PLAN" in body
    assert "步骤 B（用户补充）" in body
    assert edited.splitlines()[0] in body
    # 格式化模板占位符均已替换
    assert "{plan_content}" not in body
    assert "{plan_file}" not in body
    # 一次性：再次取不应再注入批准块
    assert agent._plan_reminder_blocks(sid, owner_account_id="") == []
    # 模板本身仍要求 plan_content 占位
    assert "{plan_content}" in PLAN_APPROVED_REMINDER


# --------------------------------------------------------------------------- #
# 有效工具集收窄
# --------------------------------------------------------------------------- #
def test_effective_tool_filter_narrows_in_plan():
    from crew.agent.runtime import SingleAgent

    mgr = PlanModeManager()
    sid = "sess-filter"
    reg = Registry()
    register_builtin_tools(reg)
    register_plan_tools(reg, mgr)

    class _Stub:
        tool_filter = None
        plan_manager = mgr
        registry = reg

    stub = _Stub()
    # 未激活 → 即使 tool_filter=None，也枚举 registry 并隐藏 Plan 控制工具。
    eff_normal = SingleAgent._effective_tool_filter(stub, sid)
    assert eff_normal is not None
    assert "enter_plan_mode" not in eff_normal
    assert "exit_plan_mode" not in eff_normal
    assert "file_read" in eff_normal
    # 激活 → 收窄到只读白名单
    mgr.enter(sid)
    assert SingleAgent._effective_tool_filter(stub, sid) == list(PLAN_MODE_TOOLS)

    # 与既有 filter 取交集
    stub.tool_filter = ["file_read", "file_write", "memory"]  # memory 不在白名单
    eff = SingleAgent._effective_tool_filter(stub, sid)
    assert set(eff) == {"file_read", "file_write"}


def test_effective_tool_filter_hides_plan_control_tools_when_not_active():
    """plan 未激活时从工具列表剔除 enter_plan_mode / exit_plan_mode。"""
    from crew.agent.runtime import SingleAgent

    mgr = PlanModeManager()
    sid = "sess-filter-exit"

    class _Stub:
        tool_filter = ["file_read", "file_write", "enter_plan_mode", "exit_plan_mode"]
        plan_manager = mgr

    stub = _Stub()
    eff = SingleAgent._effective_tool_filter(stub, sid)
    assert "exit_plan_mode" not in eff
    assert "enter_plan_mode" not in eff
    # plan 激活后 exit_plan_mode 可见，enter_plan_mode 仍不可见（白名单内无主动进入）。
    mgr.enter(sid)
    eff_active = SingleAgent._effective_tool_filter(stub, sid)
    assert "exit_plan_mode" in eff_active
    assert "enter_plan_mode" not in eff_active


def test_effective_tool_filter_plan_active_owner_isolated():
    """同名 session 的 Plan active 按 owner 隔离，A 激活不影响 B。"""
    from crew.agent.runtime import SingleAgent

    mgr = PlanModeManager()
    sid = "sess-owner-filter"

    class _Stub:
        tool_filter = ["file_read", "enter_plan_mode", "exit_plan_mode"]
        plan_manager = mgr

    stub = _Stub()
    mgr.enter(sid, owner_account_id="A:uid-a")

    eff_a = SingleAgent._effective_tool_filter(stub, sid, owner_account_id="A:uid-a")
    eff_b = SingleAgent._effective_tool_filter(stub, sid, owner_account_id="B:uid-b")

    assert "exit_plan_mode" in eff_a
    assert "exit_plan_mode" not in eff_b
    assert "enter_plan_mode" not in eff_b


def test_ask_followup_question_in_plan_whitelist():
    """ask_followup_question 进 plan 白名单（复用 Crew AskUserQuestion 的澄清能力）。"""
    assert "ask_followup_question" in PLAN_MODE_TOOLS


def test_submit_review_empty_vs_nonempty(tmp_path, monkeypatch):
    """submit_review：plan 空登记 empty=True 不 awaiting；plan 非空登记 empty=False 且 awaiting。"""
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    monkeypatch.delenv("CREW_TASK_WORKSPACE_ROOT", raising=False)
    mgr = PlanModeManager()
    sid = "sess-submit"
    mgr.enter(sid, owner_account_id="A:uid-a")

    # 空 → empty review，不 awaiting
    mgr.submit_review(sid, owner_account_id="A:uid-a")
    assert not mgr.is_awaiting_approval(sid, owner_account_id="A:uid-a")
    review = mgr.take_pending_review(sid, owner_account_id="A:uid-a")
    assert review == {"plan": None, "empty": True, "phase": "active", "status": "empty"}

    # take 后再取为 None（一次性消费，幂等防重复推）
    assert mgr.take_pending_review(sid, owner_account_id="A:uid-a") is None

    # 非空 → empty=False，awaiting=True，plan 文本回灌
    write_plan(sid, "# 计划\n做事", owner_account_id="A:uid-a")
    mgr.submit_review(sid, owner_account_id="A:uid-a")
    assert mgr.is_awaiting_approval(sid, owner_account_id="A:uid-a")
    review2 = mgr.take_pending_review(sid, owner_account_id="A:uid-a")
    assert review2["empty"] is False
    assert review2["phase"] == "review"
    assert review2["status"] == "pending"
    assert "# 计划" in review2["plan"]


def test_enter_clears_pending_review_residual(tmp_path, monkeypatch):
    """enter() 清上轮滞留的 pending_review（防 dispatch 异常未走到 ws 推送段时残留）。"""
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    monkeypatch.delenv("CREW_TASK_WORKSPACE_ROOT", raising=False)
    mgr = PlanModeManager()
    sid = "sess-residual"
    mgr.enter(sid, owner_account_id="A:uid-a")
    # 模拟一次未消费的空 review 滞留
    mgr.submit_review(sid, owner_account_id="A:uid-a")
    assert mgr.take_pending_review(sid, owner_account_id="A:uid-a") is not None
    # 再 submit 一次（模拟模型又调 exit_plan_mode 但 ws 没消费），然后 enter 应清掉
    mgr.submit_review(sid, owner_account_id="A:uid-a")
    mgr.enter(sid, owner_account_id="A:uid-a")
    assert mgr.take_pending_review(sid, owner_account_id="A:uid-a") is None


# --------------------------------------------------------------------------- #
# Plan 模式纯文本直接 final（不做后端 nudge 重跑）
# --------------------------------------------------------------------------- #
def _plan_agent(provider, mgr):
    """挂了 plan_manager 的 SingleAgent + BuiltinExecutor，对齐 app.py 装配方式。"""
    from crew.agent.executor.builtin import BuiltinExecutor
    from crew.agent.runtime import SingleAgent

    reg = Registry()
    register_builtin_tools(reg)
    register_plan_tools(reg, mgr)
    plugins = PluginManager()
    executor = BuiltinExecutor(
        provider, reg, plugins, max_iterations=5, plan_manager=mgr,
    )
    return SingleAgent(
        provider=provider,
        registry=reg,
        session_store=InMemorySessionStore(),
        memory=NullMemory(),
        plugins=plugins,
        executor=executor,
        plan_manager=mgr,
        max_iterations=5,
    )


async def test_plan_mode_chitchat_reply_not_nudged(tmp_path, monkeypatch):
    """plan 模式下对「你好」直接 final，不生成 plan，不进审批。"""
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    monkeypatch.delenv("CREW_TASK_WORKSPACE_ROOT", raising=False)
    from crew.core.envelope import Envelope

    mgr = PlanModeManager()
    sid = "sess-chat"
    token = current_session_id.set(sid)
    owner_token = current_owner_account_id.set("A:uid-a")
    try:
        mgr.enter(sid, owner_account_id="A:uid-a")
        provider = FakeProvider(script=[ChatResponse(text="你好！有什么可以帮你的吗？")])
        agent = _plan_agent(provider, mgr)
        final = None
        async for ch in agent.run(Envelope.of("你好", session_id=sid, user_id="A:uid-a")):
            if ch.kind == "final":
                final = ch.body["text"]
        assert len(provider.calls) == 1
        assert final == "你好！有什么可以帮你的吗？"
        # 没有落盘计划、没有进入待审批
        assert read_plan(sid, owner_account_id="A:uid-a") is None
        assert not mgr.is_awaiting_approval(sid, owner_account_id="A:uid-a")
        assert mgr.take_pending_review(sid, owner_account_id="A:uid-a") is None
    finally:
        current_session_id.reset(token)
        current_owner_account_id.reset(owner_token)


async def test_plan_mode_plan_like_plain_text_not_nudged(tmp_path, monkeypatch):
    """plan 模式下即使模型输出计划式纯文本，也不再由后端 nudge 重跑。"""
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    monkeypatch.delenv("CREW_TASK_WORKSPACE_ROOT", raising=False)
    from crew.core.envelope import Envelope

    mgr = PlanModeManager()
    sid = "sess-plan-text"
    token = current_session_id.set(sid)
    owner_token = current_owner_account_id.set("A:uid-a")
    try:
        mgr.enter(sid, owner_account_id="A:uid-a")
        plan_text = "我的计划如下：第一步修改文件 A，第二步重构模块 B。"
        provider = FakeProvider(script=[ChatResponse(text=plan_text)])
        agent = _plan_agent(provider, mgr)
        final = None
        async for _ in agent.run(Envelope.of("帮我重构 B 模块", session_id=sid, user_id="A:uid-a")):
            if _.kind == "final":
                final = _.body["text"]
        assert len(provider.calls) == 1
        assert final == plan_text
        assert not mgr.is_awaiting_approval(sid, owner_account_id="A:uid-a")
        assert mgr.take_pending_review(sid, owner_account_id="A:uid-a") is None
    finally:
        current_session_id.reset(token)
        current_owner_account_id.reset(owner_token)


async def test_plan_mode_stops_after_exit_plan_mode_approval_request(tmp_path, monkeypatch):
    """exit_plan_mode 成功提交审批后，本轮立即停止，不再进入下一轮执行。"""
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    monkeypatch.delenv("CREW_TASK_WORKSPACE_ROOT", raising=False)
    from crew.core.envelope import Envelope

    mgr = PlanModeManager()
    sid = "sess-stop-after-exit"
    owner = "A:uid-a"
    token = current_session_id.set(sid)
    owner_token = current_owner_account_id.set(owner)
    try:
        mgr.enter(sid, owner_account_id=owner)
        plan_file = str(plan_path(sid, owner_account_id=owner))
        provider = FakeProvider(script=[
            ChatResponse(tool_calls=[
                ToolCall("c1", "file_write", {"path": plan_file, "content": "# Context\n\n测试计划"}),
                ToolCall("c2", "exit_plan_mode", {}),
            ]),
            ChatResponse(tool_calls=[
                ToolCall("c3", "file_write", {"path": str(tmp_path / "should-not-write.txt"), "content": "bad"}),
            ]),
        ])
        agent = _plan_agent(provider, mgr)
        finals = []
        async for ch in agent.run(Envelope.of("帮我做一个需要计划的任务", session_id=sid, user_id=owner)):
            if ch.kind == "final":
                finals.append(ch.body["text"])

        assert len(provider.calls) == 1
        assert finals == [""]
        assert mgr.is_awaiting_approval(sid, owner_account_id=owner)
        assert read_plan(sid, owner_account_id=owner) == "# Context\n\n测试计划"
        assert not (tmp_path / "should-not-write.txt").exists()
    finally:
        current_session_id.reset(token)
        current_owner_account_id.reset(owner_token)


# --------------------------------------------------------------------------- #
# Plan attachment message + throttling
# --------------------------------------------------------------------------- #
def test_plan_attachment_first_active_turn_is_full(tmp_path, monkeypatch):
    """Plan active 后首轮把 full plan_mode attachment 写入 history。"""
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    mgr = PlanModeManager()
    sid = "attachment-first"
    owner = "A:uid-a"
    mgr.enter(sid, owner_account_id=owner)
    history = [Message.user("implement this")]

    attachments = get_plan_mode_attachment_messages(history, sid, mgr, owner_account_id=owner)

    assert len(attachments) == 1
    msg = attachments[0]
    assert msg.is_meta is True
    assert msg.attachment_type == "plan_mode"
    assert msg.attachment_data["reminderType"] == "full"
    assert "Plan mode is active" in msg.content


def test_plan_attachment_throttles_by_real_user_turns(tmp_path, monkeypatch):
    """已有 plan attachment 后，未满 5 个真实 user turn 不重复注入。"""
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    mgr = PlanModeManager()
    sid = "attachment-throttle"
    owner = "A:uid-a"
    mgr.enter(sid, owner_account_id=owner)
    history = [Message.user("turn 1")]
    history.extend(get_plan_mode_attachment_messages(history, sid, mgr, owner_account_id=owner))

    for idx in range(2, 6):
        history.append(Message.user(f"turn {idx}"))
        assert get_plan_mode_attachment_messages(history, sid, mgr, owner_account_id=owner) == []

    history.append(Message.user("turn 6"))
    attachments = get_plan_mode_attachment_messages(history, sid, mgr, owner_account_id=owner)
    assert len(attachments) == 1
    assert attachments[0].attachment_type == "plan_mode"
    assert attachments[0].attachment_data["reminderType"] == "sparse"
    assert "Plan mode still active" in attachments[0].content


def test_plan_attachment_full_sparse_cycle_matches_expected_schedule(tmp_path, monkeypatch):
    """plan_mode attachment 第 1、6 次为 full，其余为 sparse。"""
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    mgr = PlanModeManager()
    sid = "attachment-cycle"
    owner = "A:uid-a"
    mgr.enter(sid, owner_account_id=owner)
    history: list[Message] = []
    reminder_types: list[str] = []

    for attachment_index in range(6):
        for turn_index in range(5 if attachment_index else 1):
            history.append(Message.user(f"user {attachment_index}-{turn_index}"))
        attachments = get_plan_mode_attachment_messages(history, sid, mgr, owner_account_id=owner)
        assert len(attachments) == 1
        history.extend(attachments)
        reminder_types.append(attachments[0].attachment_data["reminderType"])

    assert reminder_types == ["full", "sparse", "sparse", "sparse", "sparse", "full"]


def test_plan_exit_attachment_injected_once_after_approval(tmp_path, monkeypatch):
    """approve 后下一轮注入一次 plan_mode_exit attachment，之后不重复。"""
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    mgr = PlanModeManager()
    sid = "attachment-exit"
    owner = "A:uid-a"
    mgr.enter(sid, owner_account_id=owner)
    write_plan(sid, "# plan", owner_account_id=owner)
    mgr.request_approval(sid, owner_account_id=owner)
    mgr.approve(sid, owner_account_id=owner)
    history = [Message.user("continue")]

    attachments = get_plan_mode_attachment_messages(history, sid, mgr, owner_account_id=owner)
    assert len(attachments) == 1
    assert attachments[0].attachment_type == "plan_mode_exit"
    assert "Plan mode is no longer active" in attachments[0].content

    assert get_plan_mode_attachment_messages(history, sid, mgr, owner_account_id=owner) == []


def test_plan_attachment_metadata_survives_session_store_roundtrip(tmp_path):
    """attachment 持久化后重新 load，下一轮仍可被节流扫描识别。"""
    store = SQLiteSessionStore(str(tmp_path / "crew.db"))
    sid = "attachment-persist"
    owner = "A:uid-a"
    messages = [
        Message.user("turn 1"),
        create_plan_attachment_message(
            "plan_mode",
            "Plan mode is active.",
            data={"reminderType": "full", "plan_file": "/tmp/plan.md"},
        ),
        Message.user("turn 2"),
    ]

    store.save(sid, messages, owner_account_id=owner)
    loaded = store.load(sid, owner_account_id=owner)

    assert loaded[1].attachment_type == "plan_mode"
    assert loaded[1].attachment_data["reminderType"] == "full"
    turns, found = count_user_turns_since_last_plan_attachment(loaded)
    assert found is True
    assert turns == 1


# --------------------------------------------------------------------------- #
# Terminal 保留在 plan 白名单
# --------------------------------------------------------------------------- #
def test_terminal_kept_in_plan_mode_tools():
    """plan 模式下 terminal 仍保留在白名单内（受 terminal_guard 约束）。"""
    from crew.agent.plan.tools import PLAN_MODE_TOOLS
    assert "terminal" in PLAN_MODE_TOOLS
    assert "file_read" in PLAN_MODE_TOOLS
    assert "file_write" in PLAN_MODE_TOOLS
    assert "exit_plan_mode" in PLAN_MODE_TOOLS
