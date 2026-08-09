"""会话存储 + 记忆。"""

import sqlite3
import threading
import time
from pathlib import Path

from crew.core.runctx import current_owner_account_id
from crew.core.types import Message, ToolCall
from crew.memory.simple import SQLiteMemory
from crew.state.session_store import SQLiteSessionStore
from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite


def test_session_store_roundtrip(tmp_path):
    store = SQLiteSessionStore(str(tmp_path / "s.db"))
    user = Message.user("hi")
    user.timestamp = 10.0
    assistant = Message.assistant(
        "",
        [
            ToolCall(
                "c1",
                "terminal",
                {"command": "ls"},
                started_at=11.0,
                duration=1.5,
                result="out",
                status="done",
            )
        ],
    )
    assistant.timestamp = 12.0
    assistant.turn_started_at = 10.0
    assistant.turn_duration = 2.0
    assistant.thinking = "先列出目录再执行命令"
    assistant.turn_file_changes = [
        {
            "path": "/tmp/snake_game.html",
            "name": "snake_game.html",
            "added": 419,
            "removed": 117,
            "status": "modified",
        }
    ]
    msgs = [
        user,
        assistant,
        Message.tool("c1", "out"),
    ]
    store.save("s1", msgs)
    loaded = store.load("s1")
    assert len(loaded) == 3
    assert loaded[0].timestamp == 10.0
    assert loaded[1].tool_calls[0].name == "terminal"
    assert loaded[1].turn_started_at == 10.0
    assert loaded[1].turn_duration == 2.0
    assert loaded[1].thinking == "先列出目录再执行命令"
    assert loaded[1].turn_file_changes == [
        {
            "path": "/tmp/snake_game.html",
            "name": "snake_game.html",
            "added": 419,
            "removed": 117,
            "status": "modified",
        }
    ]
    assert loaded[1].tool_calls[0].started_at == 11.0
    assert loaded[1].tool_calls[0].duration == 1.5
    assert loaded[1].tool_calls[0].result == "out"
    assert loaded[1].tool_calls[0].status == "done"
    assert loaded[2].tool_call_id == "c1"


def test_child_sessions_are_scoped_to_owner(tmp_path):
    store = SQLiteSessionStore(str(tmp_path / "s.db"))
    store.save("parent::turn::1::leader", [Message.user("A child")], owner_account_id="A:1")
    store.save("parent::turn::1::leader", [Message.user("B child")], owner_account_id="B:2")

    children_a = store.load_child_sessions("parent", owner_account_id="A:1")
    children_b = store.load_child_sessions("parent", owner_account_id="B:2")

    assert [(sid, messages[0].content) for sid, messages in children_a] == [
        ("parent::turn::1::leader", "A child")
    ]
    assert [(sid, messages[0].content) for sid, messages in children_b] == [
        ("parent::turn::1::leader", "B child")
    ]


def test_session_store_ignores_legacy_tool_source():
    raw = (
        '[{"role":"assistant","content":"done","tool_calls":['
        '{"id":"c1","name":"terminal","arguments":{},"source":"acp",'
        '"result":"ok","status":"done"}]}]'
    )
    loaded = SQLiteSessionStore._load(raw)
    assert loaded[0].tool_calls[0].name == "terminal"
    assert loaded[0].tool_calls[0].result == "ok"


def test_list_sessions(tmp_path):
    store = SQLiteSessionStore(str(tmp_path / "s.db"))
    store.save("s1", [Message.user("第一个会话的问题"), Message.assistant("回答")])
    store.save("s2", [Message.user("第二个会话")])
    sessions = store.list_sessions()
    assert len(sessions) == 2
    ids = {s["session_id"] for s in sessions}
    assert ids == {"s1", "s2"}
    s1 = next(s for s in sessions if s["session_id"] == "s1")
    assert s1["title"] == "第一个会话的问题"
    assert s1["message_count"] == 2


def test_list_sessions_metadata(tmp_path):
    """list_sessions 返回 created_at / token_count，并按列计数。"""
    store = SQLiteSessionStore(str(tmp_path / "s.db"))
    store.save("s1", [Message.user("问题很长" * 20), Message.assistant("回答")])
    s1 = next(s for s in store.list_sessions() if s["session_id"] == "s1")
    assert s1["created_at"] > 0
    assert s1["message_count"] == 2


def test_created_at_preserved_on_resave(tmp_path):
    """二次 save 不应改写 created_at，但 updated_at 与消息数应更新。"""
    store = SQLiteSessionStore(str(tmp_path / "s.db"))
    store.save("s1", [Message.user("a")])
    first = next(s for s in store.list_sessions() if s["session_id"] == "s1")
    created0, updated0 = first["created_at"], first["updated_at"]

    import time as _t

    _t.sleep(0.01)
    store.save("s1", [Message.user("a"), Message.assistant("b")])
    second = next(s for s in store.list_sessions() if s["session_id"] == "s1")
    assert second["created_at"] == created0  # 保留
    assert second["updated_at"] >= updated0  # 刷新
    assert second["message_count"] == 2


def test_explicit_title_not_overwritten_by_resave(tmp_path):
    """set_title 后再 save，标题不应被首条 user 消息 fallback 覆盖。"""
    store = SQLiteSessionStore(str(tmp_path / "s.db"))
    store.save("s1", [Message.user("原始问题")])
    store.set_title("s1", "自定义标题")
    store.save("s1", [Message.user("原始问题"), Message.assistant("回答")])
    s1 = next(s for s in store.list_sessions() if s["session_id"] == "s1")
    assert s1["title"] == "自定义标题"


def test_migration_from_legacy_schema(tmp_path):
    """旧库（仅 session_id/messages/updated_at）应能被无损迁移并正常工作。"""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, messages TEXT NOT NULL, updated_at REAL NOT NULL)"
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?, ?, ?)",
        ("old", '[{"role": "user", "content": "旧消息"}]', 1.0),
    )
    conn.commit()
    conn.close()

    store = SQLiteSessionStore(str(db))  # 触发迁移
    loaded = store.load("old")
    assert len(loaded) == 1 and loaded[0].content == "旧消息"
    # 迁移后新会话功能正常
    store.save("new", [Message.user("新问题")])
    assert {s["session_id"] for s in store.list_sessions()} == {"old", "new"}


def test_set_status_and_not_overwritten_by_save(tmp_path):
    """set_status 写入 last_status/last_error；后续 save() 不应覆盖它们。"""
    store = SQLiteSessionStore(str(tmp_path / "s.db"))
    store.save("s1", [Message.user("hi")])
    store.set_status("s1", "failed", "boom")
    assert store.get_status("s1") == ("failed", "boom")

    # 再保存一轮消息，状态列应保持不变
    store.save("s1", [Message.user("hi"), Message.assistant("再答")])
    assert store.get_status("s1") == ("failed", "boom")
    # list_sessions 也带出 last_status
    s1 = next(s for s in store.list_sessions() if s["session_id"] == "s1")
    assert s1["last_status"] == "failed"


def test_get_status_unknown_session(tmp_path):
    store = SQLiteSessionStore(str(tmp_path / "s.db"))
    assert store.get_status("nope") == ("", "")


def test_session_append_and_clear(tmp_path):
    store = SQLiteSessionStore(str(tmp_path / "s.db"))
    store.append("s1", [Message.user("a")])
    store.append("s1", [Message.user("b")])
    assert len(store.load("s1")) == 2
    store.clear("s1")
    assert store.load("s1") == []


async def test_memory_recall_same_session(tmp_path):
    mem = SQLiteMemory(str(tmp_path / "m.db"))
    await mem.write("s1", [Message.user("我喜欢用 Python 写后端")])
    hit = await mem.prefetch("s1", "Python 项目")
    assert "Python" in hit
    miss = await mem.prefetch("s1", "完全无关xyz")
    assert miss == ""


async def test_memory_does_not_leak_across_sessions(tmp_path):
    mem = SQLiteMemory(str(tmp_path / "m.db"))
    await mem.write("s1", [Message.user("我喜欢用 Python 写后端")])
    # s2 查 s1 的内容 —— 必须召回不到（会话隔离）
    hit = await mem.prefetch("s2", "Python 项目")
    assert hit == ""


async def test_memory_same_session_id_is_owner_scoped(tmp_path):
    mem = SQLiteMemory(str(tmp_path / "m.db"))
    token_a = current_owner_account_id.set("A:uid-a")
    try:
        await mem.write("same", [Message.user("我是 A 的偏好")])
    finally:
        current_owner_account_id.reset(token_a)

    token_b = current_owner_account_id.set("B:uid-b")
    try:
        await mem.write("same", [Message.user("我是 B 的偏好")])
        hit_b = await mem.prefetch("same", "偏好")
    finally:
        current_owner_account_id.reset(token_b)

    token_a = current_owner_account_id.set("A:uid-a")
    try:
        hit_a = await mem.prefetch("same", "偏好")
    finally:
        current_owner_account_id.reset(token_a)

    assert "A 的偏好" in hit_a
    assert "B 的偏好" not in hit_a
    assert "B 的偏好" in hit_b
    assert "A 的偏好" not in hit_b


def test_sqlite_write_helper_retries_locked_write(tmp_path):
    db = tmp_path / "locked.db"
    owner = connect_sqlite(db)
    owner.execute("CREATE TABLE items (id INTEGER PRIMARY KEY)")
    owner.execute("BEGIN IMMEDIATE")
    owner.execute("INSERT INTO items (id) VALUES (1)")

    contender = sqlite3.connect(
        str(db),
        check_same_thread=False,
        timeout=0.01,
        isolation_level=None,
    )
    helper = SQLiteWriteHelper(contender, threading.Lock())
    errors = []

    def write_after_retry():
        try:
            helper.execute(lambda conn: conn.execute("INSERT INTO items (id) VALUES (2)"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=write_after_retry)
    thread.start()
    time.sleep(0.08)
    owner.commit()
    thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert errors == []
    rows = owner.execute("SELECT id FROM items ORDER BY id").fetchall()
    assert [r[0] for r in rows] == [1, 2]
    owner.close()
    contender.close()


def test_load_config_resolves_memory_db_path_under_crew_home(tmp_path, monkeypatch):
    """Bug A: memory_db_path 应像 db_path 一样解析到 crew_home 下，且为绝对路径。"""
    from crew.state.config import load_config

    home = tmp_path / "home"
    monkeypatch.setenv("CREW_HOME", str(home))
    cfg = load_config()
    assert cfg.memory_db_path == str(home / "crew_data" / "memory.db")
    assert Path(cfg.memory_db_path).is_absolute()


async def test_memory_chinese_roundtrip_no_mojibake(tmp_path):
    """Bug C: 验证中文写入/读回不乱码（排除 GBK 显示问题 vs 真乱码）。"""
    mem = SQLiteMemory(str(tmp_path / "m.db"))
    original = "我喜欢用 Python 写后端"
    await mem.write("s1", [Message.user(original)])
    hit = await mem.prefetch("s1", "Python")
    # 召回的文本必须包含原始中文，且不是 ?? 之类乱码
    assert original in hit, f"中文 round-trip 失败，召回内容: {hit!r}"
