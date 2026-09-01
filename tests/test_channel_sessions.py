"""渠道会话绑定与列表过滤单元测试。"""

from __future__ import annotations

import sqlite3
import time

import pytest
from httpx import ASGITransport, AsyncClient

from crew.app import build_app
from crew.core.envelope import Envelope
from crew.core.types import Message
from crew.gateway.channel_sessions import (
    channel_platform_from_session_id,
    is_channel_session_id,
    list_channel_session_groups,
    prepare_inbound_channel_envelope,
)
from crew.gateway.server import create_app
from crew.state.channel_bindings import ChannelBindingsStore
from crew.state.config import Config
from crew.state.session_store import SQLiteSessionStore

OWNER = "A:uid-a"


@pytest.fixture
def store(tmp_path):
    return SQLiteSessionStore(str(tmp_path / "crew.db"))


@pytest.fixture
def bindings(tmp_path):
    return ChannelBindingsStore(str(tmp_path / "crew.db"))


def test_is_channel_session_id():
    assert is_channel_session_id("agent:main:feishu:dm:u1")
    assert is_channel_session_id("agent:main:testchat:dm:u1")
    assert not is_channel_session_id("testchat:acct:u1")
    assert not is_channel_session_id("sess-1")


def test_channel_platform_from_session_id():
    assert channel_platform_from_session_id("agent:main:feishu:dm:abc") == "feishu"
    assert channel_platform_from_session_id("agent:main:testchat:dm:u1") == "testchat"
    assert channel_platform_from_session_id("testchat:acct:u1") is None
    assert channel_platform_from_session_id("sess-1") is None


def test_bind_on_connect_preserves_bound_at_on_reconnect(bindings):
    first = bindings.bind_on_connect("feishu", OWNER)
    bound_at = first["bound_at"]
    time.sleep(0.01)
    second = bindings.bind_on_connect("feishu", OWNER)
    assert second["bound_at"] == bound_at


def test_bind_on_connect_resets_bound_at_when_owner_changes(bindings):
    first = bindings.bind_on_connect("feishu", OWNER)
    time.sleep(0.01)
    second = bindings.bind_on_connect("feishu", "B:uid-b")
    assert second["bound_at"] > first["bound_at"]


def test_legacy_single_platform_binding_migrates_to_composite_owner_key(tmp_path):
    db_path = tmp_path / "crew.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE channel_bindings (
            platform TEXT PRIMARY KEY,
            owner_account_id TEXT NOT NULL,
            bound_at REAL NOT NULL
        );
        INSERT INTO channel_bindings VALUES ('feishu', 'A:uid-a', 10);
        """
    )
    conn.commit()
    conn.close()

    store = ChannelBindingsStore(str(db_path))
    store.bind_on_connect("feishu", "B:uid-b")

    assert {row["owner_account_id"] for row in store.list_for_platform("feishu")} == {
        "A:uid-a",
        "B:uid-b",
    }
    assert store.get_binding("feishu", "A:uid-a") == "A:uid-a"
    assert store.get_binding("feishu", "B:uid-b") == "B:uid-b"
    store.close()


def test_list_sessions_excludes_channel_by_default(store):
    store.ensure_session("agent:main:feishu:dm:u1", owner_account_id=OWNER)
    store.ensure_session("agent:main:testchat:dm:u1", owner_account_id=OWNER)
    store.ensure_session("desktop-1", owner_account_id=OWNER)
    rows = store.list_sessions(owner_account_id=OWNER)
    assert [r["session_id"] for r in rows] == ["desktop-1"]
    all_rows = store.list_sessions(owner_account_id=OWNER, exclude_channel_sessions=False)
    assert {r["session_id"] for r in all_rows} == {
        "agent:main:feishu:dm:u1",
        "agent:main:testchat:dm:u1",
        "desktop-1",
    }


def test_list_channel_session_groups_filters_by_bound_at_and_message_count(store, bindings):
    bindings.bind_on_connect("feishu", OWNER)
    bound_at = bindings.list_for_owner(OWNER)[0]["bound_at"]
    old_sid = "agent:main:feishu:dm:old"
    new_sid = "agent:main:feishu:dm:new"
    store.ensure_session(old_sid, owner_account_id=OWNER)
    store.ensure_session(new_sid, owner_account_id=OWNER)
    # 模拟绑定前的旧会话：updated_at 早于 bound_at
    with store._lock:
        store._conn.execute(
            """
            UPDATE sessions
            SET created_at = ?, updated_at = ?, message_count = 2
            WHERE session_id = ? AND owner_account_id = ?
            """,
            (bound_at - 100, bound_at - 100, old_sid, OWNER),
        )
        store._conn.commit()
    store.save(new_sid, [Message.user("hi")], owner_account_id=OWNER)
    groups = list_channel_session_groups(
        store,
        bindings,
        OWNER,
        {"feishu": "飞书"},
    )
    assert len(groups) == 1
    assert groups[0]["platform"] == "feishu"
    assert [s["session_id"] for s in groups[0]["sessions"]] == [new_sid]


def test_prepare_inbound_remaps_owner(store, bindings):
    bindings.bind_on_connect("feishu", OWNER)
    store.ensure_session("agent:main:feishu:dm:u1", owner_account_id="platform-user")

    class _Env:
        channel = "feishu"
        session_id = "agent:main:feishu:dm:u1"
        user_id = "platform-user"
        params: dict = {"platform_uid": "platform-user"}

    class _Crew:
        channel_bindings = bindings
        session_store = store

    env = _Env()
    prepare_inbound_channel_envelope(_Crew(), env)
    assert env.user_id == OWNER
    cfg = store.get_agent_config(env.session_id, owner_account_id=OWNER)
    assert cfg.get("channel_source")


def test_bound_channel_message_is_listed_for_desktop_owner(store, bindings):
    bindings.bind_on_connect("feishu", OWNER)
    sid = "agent:main:feishu:dm:u1"

    class _Env:
        channel = "feishu"
        session_id = sid
        user_id = "platform-user"
        params: dict = {"platform_uid": "platform-user"}

    class _Crew:
        channel_bindings = bindings
        session_store = store

    env = _Env()
    prepare_inbound_channel_envelope(_Crew(), env)
    store.save(sid, [Message.user("hi from channel")], owner_account_id=env.user_id)

    groups = list_channel_session_groups(store, bindings, OWNER, {"feishu": "飞书"})
    assert [s["session_id"] for s in groups[0]["sessions"]] == [sid]
    assert store.load(sid, owner_account_id="platform-user") == []


def test_rebound_existing_channel_session_uses_updated_at(store, bindings):
    sid = "agent:main:feishu:dm:u1"
    store.save(sid, [Message.user("old")], owner_account_id=OWNER)
    first_bound_at = bindings.bind_on_connect("feishu", OWNER)["bound_at"]
    bindings.bind_on_connect("feishu", "B:uid-b")
    time.sleep(0.01)
    rebound_at = bindings.bind_on_connect("feishu", OWNER)["bound_at"]

    with store._lock:
        store._conn.execute(
            "UPDATE sessions SET created_at = ?, updated_at = ? WHERE session_id = ? AND owner_account_id = ?",
            (first_bound_at - 100, rebound_at - 10, sid, OWNER),
        )
        store._conn.commit()
    assert list_channel_session_groups(store, bindings, OWNER, {"feishu": "飞书"}) == []

    store.save(sid, [Message.user("old"), Message.user("new after rebind")], owner_account_id=OWNER)

    groups = list_channel_session_groups(store, bindings, OWNER, {"feishu": "飞书"})
    assert [s["session_id"] for s in groups[0]["sessions"]] == [sid]


@pytest.mark.asyncio
async def test_api_channel_sessions(tmp_path, auth_headers):
    cfg = Config(db_path=str(tmp_path / "crew.db"))
    crew = build_app(config=cfg, enable_team=False)
    crew.channel_bindings.bind_on_connect("feishu", OWNER)
    sid = "agent:main:feishu:dm:u1"
    crew.session_store.ensure_session(sid, owner_account_id=OWNER)
    crew.session_store.save(sid, [Message.user("hello")], owner_account_id=OWNER)
    app = create_app(crew)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.get("/api/channel-sessions")
    assert resp.status_code == 200
    data = resp.json()
    platforms = data.get("platforms") or []
    assert len(platforms) == 1
    assert platforms[0]["platform"] == "feishu"
    assert platforms[0]["sessions"][0]["session_id"] == sid


@pytest.mark.asyncio
async def test_api_channel_sessions_lists_inbound_message_after_gateway_restart(tmp_path, auth_headers):
    cfg = Config(db_path=str(tmp_path / "crew.db"))
    first = build_app(config=cfg, enable_team=False)
    first.channel_bindings.bind_on_connect("feishu", OWNER)
    first.channel_bindings.close()

    crew = build_app(config=cfg, enable_team=False)
    sid = "agent:main:feishu:dm:u1"
    env = Envelope(
        session_id=sid,
        channel="feishu",
        user_id="platform-user",
        params={"query": "hi from channel", "platform_uid": "platform-user"},
    )
    prepare_inbound_channel_envelope(crew, env)
    crew.session_store.save(sid, [Message.user(env.query)], owner_account_id=env.user_id)
    app = create_app(crew)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.get("/api/channel-sessions")

    assert resp.status_code == 200
    platforms = resp.json().get("platforms") or []
    assert [group["platform"] for group in platforms] == ["feishu"]
    assert platforms[0]["sessions"][0]["session_id"] == sid
