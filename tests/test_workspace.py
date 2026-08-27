"""工作空间：存储 CRUD、会话归属过滤、空间指令注入。"""

import pytest
from pathlib import Path

from crew.agent.runtime import SingleAgent
from crew.app import CrewApp
from crew.core.envelope import Envelope
from crew.core.mocks import (
    FakeProvider,
    InMemorySessionStore,
    InMemoryWorkspaceStore,
    NullMemory,
)
from crew.core.types import Message
from crew.core.types import ChatResponse, ToolCall
from crew.plugins.manager import PluginManager
from crew.state.config import Config
from crew.state.session_store import SQLiteSessionStore
from crew.state.home import task_workspace_path
from crew.state.workspace_store import SQLiteWorkspaceStore
from crew.tools.registry import Registry


def test_workspace_store_crud(tmp_path):
    store = SQLiteWorkspaceStore(str(tmp_path / "w.db"))
    # 默认空间存在
    assert store.get("default")["name"] == "默认工作空间"
    ws = store.create("电商后台", instructions="用 TypeScript")
    assert ws["instructions"] == "用 TypeScript"
    assert any(w["id"] == ws["id"] for w in store.list())
    updated = store.update(ws["id"], name="改名了")
    assert updated["name"] == "改名了"
    store.delete(ws["id"])
    assert all(w["id"] != ws["id"] for w in store.list())


def test_workspace_store_root_path(tmp_path):
    store = SQLiteWorkspaceStore(str(tmp_path / "w.db"))
    project = tmp_path / "my-project"
    project.mkdir()
    ws = store.create("我的项目", root_path=str(project))
    assert ws["root_path"] == str(project.resolve())
    got = store.get(ws["id"])
    assert got["root_path"] == str(project.resolve())


def test_default_workspace_protected(tmp_path):
    store = SQLiteWorkspaceStore(str(tmp_path / "w.db"))
    with pytest.raises(ValueError):
        store.delete("default")


def test_builtin_wiki_workspace_auto_created(tmp_path):
    """wiki 内置工作空间：Wiki Agent 会话的 workspace_id="wiki" 查不到时幂等自建。"""
    store = SQLiteWorkspaceStore(str(tmp_path / "w.db"))
    ws = store.get("wiki", owner_account_id="owner-a")
    assert ws["name"] == "Wiki 知识库"
    assert ws["hidden"] is True
    # 幂等，且按账号隔离
    assert store.get("wiki", owner_account_id="owner-a")["id"] == "wiki"
    assert all(w["id"] != "wiki" for w in store.list(owner_account_id="owner-b"))


def test_builtin_wiki_workspace_protected(tmp_path):
    store = SQLiteWorkspaceStore(str(tmp_path / "w.db"))
    store.get("wiki")  # 先确保存在
    with pytest.raises(ValueError):
        store.delete("wiki")


def test_builtin_companion_workspace_auto_created_and_protected(tmp_path):
    store = SQLiteWorkspaceStore(str(tmp_path / "w.db"))
    ws = store.get("companion", owner_account_id="owner-a")
    assert ws["name"] == "同伴空间"
    assert ws["hidden"] is False
    with pytest.raises(ValueError):
        store.delete("companion", owner_account_id="owner-a")


def test_unknown_workspace_still_missing(tmp_path):
    """内置之外的 id 不放开校验：不存在仍抛 KeyError。"""
    store = SQLiteWorkspaceStore(str(tmp_path / "w.db"))
    with pytest.raises(KeyError):
        store.get("ghost")


def test_inmemory_store_mirrors_builtin_workspaces():
    store = InMemoryWorkspaceStore()
    assert store.get("wiki")["hidden"] is True
    assert store.get("companion")["name"] == "同伴空间"
    store.delete("wiki")  # mock 对齐既有 default 行为：内置空间删除被忽略
    assert store.get("wiki")["id"] == "wiki"
    with pytest.raises(KeyError):
        store.get("ghost")


def test_session_list_filtered_by_workspace(tmp_path):
    from crew.state.session_store import SQLiteSessionStore

    store = SQLiteSessionStore(str(tmp_path / "s.db"))
    store.save("s1", [Message.user("A")], workspace_id="ws_a")
    store.save("s2", [Message.user("B")], workspace_id="ws_b")
    a = store.list_sessions("ws_a")
    assert len(a) == 1 and a[0]["session_id"] == "s1"
    assert len(store.list_sessions()) == 2


async def test_agent_injects_workspace_instructions():
    provider = FakeProvider()
    reg = Registry()
    agent = SingleAgent(
        provider=provider,
        registry=reg,
        session_store=InMemorySessionStore(),
        memory=NullMemory(),
        plugins=PluginManager(),
    )
    env = Envelope.of("你好", session_id="s1", params={"workspace_instructions": "只说英文"})
    async for _ in agent.run(env):
        pass
    # workspace_instructions 现在在 <system-reminder> 注入的 user 消息中，而非 system prompt
    # FakeProvider.calls 记录发送给 LLM 的消息列表
    all_text = " ".join(m.content for m in provider.calls[0] if m.content)
    assert "只说英文" in all_text
    assert "项目提示词" in all_text


def test_workspace_hidden_flag(tmp_path):
    store = SQLiteWorkspaceStore(str(tmp_path / "w.db"))
    ws = store.create("隐藏测试")
    assert ws.get("hidden") is False
    updated = store.update(ws["id"], hidden=True)
    assert updated["hidden"] is True
    got = store.get(ws["id"])
    assert got["hidden"] is True
    store.update(ws["id"], hidden=False)
    assert store.get(ws["id"])["hidden"] is False


def test_delete_sessions_for_workspace(tmp_path):
    from crew.state.session_store import SQLiteSessionStore

    ws_store = SQLiteWorkspaceStore(str(tmp_path / "ws.db"))
    sess_store = SQLiteSessionStore(str(tmp_path / "s.db"))
    ws = ws_store.create("待删空间")
    from crew.core.types import Message

    sess_store.save("s1", [Message.user("A")], workspace_id=ws["id"])
    sess_store.save("s2", [Message.user("B")], workspace_id=ws["id"])
    sess_store.save("s3", [Message.user("C")], workspace_id="default")
    deleted = sess_store.delete_sessions_for_workspace(ws["id"])
    assert set(deleted) == {"s1", "s2"}
    assert len(sess_store.list_sessions(ws["id"])) == 0
    assert len(sess_store.list_sessions("default")) == 1


def test_workspace_delete_can_be_wrapped_with_session_delete_in_one_transaction(tmp_path):
    db = str(tmp_path / "shared.db")
    ws_store = SQLiteWorkspaceStore(db)
    sess_store = SQLiteSessionStore(db)
    ws = ws_store.create("事务空间")
    sess_store.save("s1", [Message.user("A")], workspace_id=ws["id"])

    def _delete_and_fail(conn):
        sess_store.delete_sessions_for_workspace(ws["id"], writer=conn)
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        sess_store.transaction(_delete_and_fail)

    assert ws_store.get(ws["id"])["id"] == ws["id"]
    assert [row["session_id"] for row in sess_store.list_sessions(ws["id"])] == ["s1"]


def test_save_title_fallback_defaults_to_first_user_message(tmp_path):
    """save 的标题 fallback：占位标题「新会话」或未传 title_fallback（None 保持旧行为，
    兼容未传该参数的调用方）时，都以首条 user 消息截断作为标题。"""
    store = SQLiteSessionStore(str(tmp_path / "s.db"))
    # 占位标题被首条 user 消息覆盖
    store.ensure_session("s1", title="新会话")
    store.save("s1", [Message.user("帮我写一段 Python 脚本")])
    s1 = next(s for s in store.list_sessions() if s["session_id"] == "s1")
    assert s1["title"] == "帮我写一段 Python 脚本"
    # title_fallback=None（旧行为）同样取首条 user 消息
    store.save("s-legacy", [Message.user("帮我看看天气")], title_fallback=None)
    row = next(s for s in store.list_sessions() if s["session_id"] == "s-legacy")
    assert row["title"] == "帮我看看天气"


def test_save_does_not_overwrite_workspace_id_on_update(tmp_path):
    """workspace_id 仅在首次创建时写入；后续 save 不应回写覆盖已确定的归属。

    复现「test 工作空间会话刷新后漂到 default」：第二轮回写带了不同的 workspace_id，
    旧行为会把它覆盖掉，导致会话在 default/test 之间反复漂移。
    """
    store = SQLiteSessionStore(str(tmp_path / "s.db"))
    store.save("s-drift", [Message.user("在 test 里问问题")], workspace_id="test")
    # 第二轮回写带了 default（例如 dispatcher 中间态/旧 envelope）
    store.save("s-drift", [Message.user("在 test 里问问题"), Message.assistant("回答")], workspace_id="default")
    row = next(s for s in store.list_sessions() if s["session_id"] == "s-drift")
    assert row["workspace_id"] == "test"


def test_save_title_fallback_empty_keeps_placeholder_for_summary(tmp_path):
    """enable_title=True 时传 title_fallback='' 留空占位，等 set_title 写入摘要标题。

    避免「先 save 写入截断用户原话 → 摘要生成失败/未完成 → 标题永久停在原话」。
    """
    store = SQLiteSessionStore(str(tmp_path / "s.db"))
    store.ensure_session("s-title", title="新会话")
    store.save("s-title", [Message.user("你好，你有哪些文件")], title_fallback="")
    row = next(s for s in store.list_sessions() if s["session_id"] == "s-title")
    # 占位标题保留，未被截断的用户原话抢占
    assert row["title"] in ("", "新会话")
    # 摘要生成后 set_title 写入
    store.set_title("s-title", "文件清单问答")
    row = next(s for s in store.list_sessions() if s["session_id"] == "s-title")
    assert row["title"] == "文件清单问答"


def test_save_title_fallback_none_keeps_legacy_behavior(tmp_path):
    """title_fallback=None 保持旧行为（首条 user 消息截断作 fallback），兼容未传该参数的调用方。"""
    store = SQLiteSessionStore(str(tmp_path / "s.db"))
    store.save("s-legacy", [Message.user("帮我看看天气")], title_fallback=None)
    row = next(s for s in store.list_sessions() if s["session_id"] == "s-legacy")
    assert row["title"] == "帮我看看天气"


def test_app_enrich_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    monkeypatch.setenv("CREW_TASK_WORKSPACE_ROOT", str(tmp_path / "task-output"))
    ws_store = InMemoryWorkspaceStore()
    ws = ws_store.create("空间X", instructions="遵守X规范", owner_account_id="local")
    app = CrewApp(
        Config(),
        FakeProvider(),
        Registry(),
        InMemorySessionStore(),
        ws_store,
        NullMemory(),
        PluginManager(),
    )
    env = Envelope.of("hi", session_id="s1", workspace_id=ws["id"], user_id="local")
    app._enrich_workspace(env)
    assert env.params["workspace_instructions"] == "遵守X规范"
    assert env.params["workspace_root_path"] == str(
        task_workspace_path(ws["id"], owner_account_id="local")
    )


def test_app_enrich_workspace_fills_root_when_instructions_are_preassembled(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    monkeypatch.setenv("CREW_TASK_WORKSPACE_ROOT", str(tmp_path / "task-output"))
    ws_store = InMemoryWorkspaceStore()
    ws = ws_store.create("空间X", instructions="遵守X规范", owner_account_id="local")
    app = CrewApp(
        Config(),
        FakeProvider(),
        Registry(),
        InMemorySessionStore(),
        ws_store,
        NullMemory(),
        PluginManager(),
    )
    env = Envelope.of(
        "hi",
        session_id="s1",
        workspace_id=ws["id"],
        user_id="local",
        params={"workspace_instructions": "已由上游装配"},
    )

    app._enrich_workspace(env)

    assert env.params["workspace_instructions"] == "已由上游装配"
    assert env.params["workspace_root_path"] == str(
        task_workspace_path(ws["id"], owner_account_id="local")
    )


async def test_single_agent_writes_relative_artifacts_to_task_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    monkeypatch.setenv("CREW_TASK_WORKSPACE_ROOT", str(tmp_path / "task-output"))
    marker = "agent-output.txt"
    provider = FakeProvider(script=[
        ChatResponse(tool_calls=[ToolCall("c1", "file_write", {"path": marker, "content": "ok"})]),
        ChatResponse(text="done"),
    ])
    reg = Registry()
    from crew.tools.registry import register_builtin_tools

    register_builtin_tools(reg)
    agent = SingleAgent(
        provider=provider,
        registry=reg,
        session_store=InMemorySessionStore(),
        memory=NullMemory(),
        plugins=PluginManager(),
    )
    async for _ in agent.run(Envelope.of("写文件", session_id="s-artifact", workspace_id="ws-main", user_id="")):
        pass

    # Layer 3：work_dir = {task_workspace_root}/{workspace_id}/（只到 workspace 级）
    expected = tmp_path / "task-output" / "ws-main" / marker
    assert expected.read_text(encoding="utf-8") == "ok"
    assert not (Path.cwd() / marker).exists()
