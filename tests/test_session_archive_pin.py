"""会话归档（archived）与置顶（pinned）契约测试。

覆盖：
- SQLiteSessionStore.set_archived / set_pinned 持久化与联动（归档清置顶）
- list_sessions 默认排除归档、include_archived=True 包含归档
- list_sessions 排序：pinned DESC, updated_at DESC
- InMemorySessionStore 同语义
- Gateway /api/session/{id}/archive 与 /api/session/{id}/pin 端点 + 账号隔离 + 主列表过滤
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from crew.app import build_app
from crew.core.mocks import InMemorySessionStore
from crew.core.types import Message
from crew.gateway.server import create_app
from crew.state.config import Config
from crew.state.session_store import SQLiteSessionStore

OWNER_A = "A:uid-a"
OWNER_B = "B:uid-b"


def _save_with_updated_at(store, sid: str, owner: str, ts: float) -> None:
    """保存一条会话并把 updated_at 校准到指定时间，便于排序断言。"""
    store.save(sid, [Message.user(sid)], owner_account_id=owner)
    # 直接改库内 updated_at（save 用 time.time()，无法注入时钟）
    store._writer.execute(lambda conn: conn.execute(
        "UPDATE sessions SET updated_at = ? WHERE session_id = ? AND owner_account_id = ?",
        (ts, sid, owner),
    ))


def test_sqlite_archive_hides_from_default_list_and_includes_when_asked(tmp_path):
    store = SQLiteSessionStore(str(tmp_path / "crew.db"))
    store.save("s1", [Message.user("hi")], owner_account_id=OWNER_A)

    assert [r["session_id"] for r in store.list_sessions(owner_account_id=OWNER_A)] == ["s1"]

    store.set_archived("s1", True, owner_account_id=OWNER_A)
    # 默认排除归档
    assert store.list_sessions(owner_account_id=OWNER_A) == []
    # include_archived=True 返回归档会话，且带 archived=True 标记
    rows = store.list_sessions(owner_account_id=OWNER_A, include_archived=True)
    assert [r["session_id"] for r in rows] == ["s1"]
    assert rows[0]["archived"] is True
    assert rows[0]["pinned"] is False


def test_sqlite_archive_clears_pinned(tmp_path):
    store = SQLiteSessionStore(str(tmp_path / "crew.db"))
    store.save("s1", [Message.user("hi")], owner_account_id=OWNER_A)
    store.set_pinned("s1", True, owner_account_id=OWNER_A)
    assert store.list_sessions(owner_account_id=OWNER_A, include_archived=True)[0]["pinned"] is True

    store.set_archived("s1", True, owner_account_id=OWNER_A)
    row = store.list_sessions(owner_account_id=OWNER_A, include_archived=True)[0]
    assert row["archived"] is True
    assert row["pinned"] is False  # 归档顺带清置顶


def test_sqlite_pinned_sorts_before_others_regardless_of_updated_at(tmp_path):
    store = SQLiteSessionStore(str(tmp_path / "crew.db"))
    _save_with_updated_at(store, "old_pinned", OWNER_A, 1000.0)
    _save_with_updated_at(store, "recent_normal", OWNER_A, 9000.0)

    store.set_pinned("old_pinned", True, owner_account_id=OWNER_A)

    order = [r["session_id"] for r in store.list_sessions(owner_account_id=OWNER_A)]
    # 置顶的旧会话排在最新的未置顶会话之前
    assert order == ["old_pinned", "recent_normal"]

    # 取消置顶后恢复纯时间倒序
    store.set_pinned("old_pinned", False, owner_account_id=OWNER_A)
    assert [r["session_id"] for r in store.list_sessions(owner_account_id=OWNER_A)] == ["recent_normal", "old_pinned"]


def test_sqlite_archive_is_owner_scoped(tmp_path):
    store = SQLiteSessionStore(str(tmp_path / "crew.db"))
    store.save("shared", [Message.user("a")], owner_account_id=OWNER_A)
    store.save("shared", [Message.user("b")], owner_account_id=OWNER_B)

    store.set_archived("shared", True, owner_account_id=OWNER_A)
    # A 归档了自己的 shared，B 的不受影响
    assert store.list_sessions(owner_account_id=OWNER_A) == []
    assert [r["session_id"] for r in store.list_sessions(owner_account_id=OWNER_B)] == ["shared"]


def test_inmemory_archive_and_pin_match_sqlite_semantics():
    store = InMemorySessionStore()
    store.save("s1", [Message.user("hi")], owner_account_id=OWNER_A)
    store.save("s2", [Message.user("hi2")], owner_account_id=OWNER_A)

    # InMemory 没有 updated_at 时钟注入，置顶排序仅靠 pinned 标志
    store.set_pinned("s2", True, owner_account_id=OWNER_A)
    order = [r["session_id"] for r in store.list_sessions(owner_account_id=OWNER_A)]
    assert order[0] == "s2"
    assert store.list_sessions(owner_account_id=OWNER_A)[0]["pinned"] is True

    store.set_archived("s2", True, owner_account_id=OWNER_A)
    # 归档后默认列表不返回 s2
    assert [r["session_id"] for r in store.list_sessions(owner_account_id=OWNER_A)] == ["s1"]
    archived_rows = store.list_sessions(owner_account_id=OWNER_A, include_archived=True)
    s2 = next(r for r in archived_rows if r["session_id"] == "s2")
    assert s2["archived"] is True
    assert s2["pinned"] is False  # 归档清置顶


@pytest.mark.asyncio
async def test_gateway_archive_and_pin_endpoints(tmp_path, auth_headers, monkeypatch):
    crew = build_app(config=Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False), enable_team=False)
    crew.session_store.save("s-a", [Message.user("hello A")], owner_account_id=OWNER_A)
    app = create_app(crew)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        # 归档 s-a
        resp = await client.put("/api/session/s-a/archive", json={"archived": True})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "archived": True}
        # 主列表不再返回 s-a
        listed = await client.get("/api/sessions")
        assert listed.json() == []
        # A 仍是 Active Owner，B 在 A 显式 Logout 前不能建立任何操作连接。
        monkeypatch.setattr("crew.gateway.auth.LOCAL_OWNER_ACCOUNT_ID", "B:uid-b")
        cross = await client.put("/api/session/s-a/archive", json={"archived": False})
        assert cross.status_code == 423

        # 取消归档
        monkeypatch.setattr("crew.gateway.auth.LOCAL_OWNER_ACCOUNT_ID", OWNER_A)
        resp = await client.put("/api/session/s-a/archive", json={"archived": False})
        assert resp.json() == {"ok": True, "archived": False}
        listed = await client.get("/api/sessions")
        assert [r["session_id"] for r in listed.json()] == ["s-a"]

        # 置顶 s-a
        resp = await client.put("/api/session/s-a/pin", json={"pinned": True})
        assert resp.json() == {"ok": True, "pinned": True}
        listed = await client.get("/api/sessions")
        assert listed.json()[0]["pinned"] is True

        # 同理，B 的置顶操作被 Active Owner 租约先行拒绝。
        monkeypatch.setattr("crew.gateway.auth.LOCAL_OWNER_ACCOUNT_ID", "B:uid-b")
        cross_pin = await client.put("/api/session/s-a/pin", json={"pinned": False})
        assert cross_pin.status_code == 423


@pytest.mark.asyncio
async def test_gateway_ensure_session_persists_a_welcome_page_draft(tmp_path, auth_headers):
    crew = build_app(
        config=Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False),
        enable_team=False,
    )
    app = create_app(crew)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        response = await client.post(
            "/api/session/browser-draft/ensure",
            json={"workspace_id": "default", "title": "浏览器"},
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True, "session_id": "browser-draft"}
        assert crew.session_store.session_belongs_to("browser-draft", OWNER_A)

        listed = await client.get("/api/sessions")
        assert [row["session_id"] for row in listed.json()] == ["browser-draft"]
