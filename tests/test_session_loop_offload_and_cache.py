"""P1-2 / P1-5：事件循环解阻塞与投影缓存治理的行为测试。

P1-2：sessions 路由的纯同步 handler 走线程池、session_history 的 store 调用
      经 asyncio.to_thread 执行——store 的 SQLite 调用不得占用事件循环线程。
P1-5：投影缓存 LRU 上界、Team 子会话前缀范围查询（主键区间扫描 + 边界正确性）、
      断点扫描按 last_status 过滤候选、agent 配置批量读取、fork 来源索引存在。
"""

from __future__ import annotations

import sqlite3
import threading
from unittest.mock import patch

import pytest

from crew.core.types import Message
from crew.state.session_store import SessionEventType, SQLiteSessionStore

OWNER = "A:uid-a"


# ---- P1-5：投影缓存 LRU ----


def test_projection_cache_evicts_lru_beyond_limit(tmp_path):
    store = SQLiteSessionStore(str(tmp_path / "crew.db"))
    try:
        limit = store.PROJECTION_CACHE_LIMIT
        total = limit + 5
        for i in range(total):
            store.save(f"s{i}", [Message.user(f"q{i}")], owner_account_id=OWNER)
            store.load(f"s{i}", owner_account_id=OWNER)
        assert len(store._projections) == limit
        # 最先加载的会话已被淘汰，最近加载的仍在
        assert ("", f"s0") not in store._projections and (OWNER, "s0") not in store._projections
        assert (OWNER, f"s{total - 1}") in store._projections
    finally:
        store.close()


def test_projection_cache_lru_keeps_hot_session(tmp_path):
    store = SQLiteSessionStore(str(tmp_path / "crew.db"))
    try:
        limit = store.PROJECTION_CACHE_LIMIT
        store.save("hot", [Message.user("q")], owner_account_id=OWNER)
        store.load("hot", owner_account_id=OWNER)
        for i in range(limit + 3):
            store.save(f"s{i}", [Message.user(f"q{i}")], owner_account_id=OWNER)
            store.load(f"s{i}", owner_account_id=OWNER)
            store.load("hot", owner_account_id=OWNER)  # 反复触碰保持热度
        assert (OWNER, "hot") in store._projections
    finally:
        store.close()


def test_projection_cache_eviction_only_drops_cache_not_data(tmp_path):
    store = SQLiteSessionStore(str(tmp_path / "crew.db"))
    try:
        store.save("s0", [Message.user("q0"), Message.assistant("a0")], owner_account_id=OWNER)
        store.load("s0", owner_account_id=OWNER)
        for i in range(1, store.PROJECTION_CACHE_LIMIT + 2):
            store.save(f"s{i}", [Message.user(f"q{i}")], owner_account_id=OWNER)
            store.load(f"s{i}", owner_account_id=OWNER)
        assert (OWNER, "s0") not in store._projections
        # 淘汰后重新加载：从事件增量重建，数据无损
        msgs = store.load("s0", owner_account_id=OWNER)
        assert [m.content for m in msgs] == ["q0", "a0"]
    finally:
        store.close()


# ---- P1-5：Team 子会话前缀范围查询 ----


def test_load_child_sessions_range_matches_exact_prefix_only(tmp_path):
    store = SQLiteSessionStore(str(tmp_path / "crew.db"))
    try:
        # 真子会话、前缀相近但非子会话、无关会话三类边界
        for sid in ("t1::turn::1::leader", "t1::member-b", "t10::turn::1::leader", "t1x::other", "plain"):
            store.save(sid, [Message.user(f"q-{sid}")], owner_account_id=OWNER)
        children = store.load_child_sessions("t1", owner_account_id=OWNER)
        assert sorted(sid for sid, _ in children) == ["t1::member-b", "t1::turn::1::leader"]
    finally:
        store.close()


def test_load_child_sessions_range_uses_pk_index_scan(tmp_path):
    db = str(tmp_path / "crew.db")
    store = SQLiteSessionStore(db)
    try:
        store.save("t1::turn::1::leader", [Message.user("q")], owner_account_id=OWNER)
        conn = sqlite3.connect(db)
        try:
            plan = conn.execute(
                "EXPLAIN QUERY PLAN SELECT session_id FROM sessions "
                "WHERE session_id >= ? AND session_id < ? AND owner_account_id = ?",
                ("t1::", "t1:;", OWNER),
            ).fetchall()
            detail = " ".join(str(row[-1]) for row in plan).upper()
            # 主键区间扫描（SQLITE_AUTOINDEX_SESSIONS_1 = 复合主键的 covering index），
            # 不允许退化为整表 SCAN。
            assert "SEARCH SESSIONS USING" in detail, detail
            assert "SCAN SESSIONS" not in detail, detail
        finally:
            conn.close()
    finally:
        store.close()


def test_fork_source_index_exists(tmp_path):
    db = str(tmp_path / "crew.db")
    store = SQLiteSessionStore(db)
    try:
        conn = sqlite3.connect(db)
        try:
            names = {row[1] for row in conn.execute("PRAGMA index_list(sessions)").fetchall()}
            assert "idx_sessions_source" in names
        finally:
            conn.close()
    finally:
        store.close()


# ---- P1-5：断点扫描候选过滤 ----


def test_scan_breakpoints_skips_terminal_status_candidates(tmp_path):
    store = SQLiteSessionStore(str(tmp_path / "crew.db"))
    try:
        # A：开放回合且 last_status=running（崩溃现场）→ 报告
        store.save("sa", [Message.user("q1")], owner_account_id=OWNER)
        store.record_turn_event("sa", kind=SessionEventType.TURN_START, owner_account_id=OWNER)
        store.set_status("sa", "running", owner_account_id=OWNER)
        # B：事件层同样开放回合，但 last_status=completed（终态已写，回合早已收敛过）→ 跳过
        store.save("sb", [Message.user("x")], owner_account_id=OWNER)
        store.record_turn_event("sb", kind=SessionEventType.TURN_START, owner_account_id=OWNER)
        store.set_status("sb", "completed", owner_account_id=OWNER)

        reports = store.scan_breakpoints(OWNER)
        assert [r["session_id"] for r in reports] == ["sa"]
    finally:
        store.close()


# ---- P1-5：agent 配置批量读取 ----


def test_get_agent_configs_batch_reads_only_requested(tmp_path):
    store = SQLiteSessionStore(str(tmp_path / "crew.db"))
    try:
        store.set_agent_config("s1", {"executor": "team"}, owner_account_id=OWNER)
        store.set_agent_config("s2", {"executor": "builtin"}, owner_account_id=OWNER)
        store.set_agent_config("s3", {"executor": "external"}, owner_account_id=OWNER)

        out = store.get_agent_configs(["s1", "s3", "missing", "s1"], OWNER)
        assert set(out.keys()) == {"s1", "s3"}
        assert out["s1"]["executor"] == "team"
        assert out["s3"]["executor"] == "external"
        assert store.get_agent_configs([], OWNER) == {}
    finally:
        store.close()


# ---- P1-2：store 调用离开事件循环线程 ----


@pytest.mark.asyncio
async def test_session_history_load_runs_off_loop_thread(tmp_path, auth_headers):
    from httpx import ASGITransport, AsyncClient

    from crew.app import build_app
    from crew.gateway.server import create_app
    from crew.state.config import Config

    crew = build_app(
        config=Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False),
        enable_team=False,
    )
    crew.session_store.save(
        "s1", [Message.user("q1"), Message.assistant("a1")], owner_account_id=OWNER
    )
    app = create_app(crew)

    seen_threads: list[threading.Thread] = []
    original_load = SQLiteSessionStore.load

    def spy_load(self, session_id, owner_account_id=None, **kwargs):
        seen_threads.append(threading.current_thread())
        return original_load(self, session_id, owner_account_id=owner_account_id, **kwargs)

    with patch.object(SQLiteSessionStore, "load", spy_load):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
            resp = await client.get("/api/session/s1")
            assert resp.status_code == 200
            assert resp.json() and resp.json()[0]["content"] == "q1"

    assert seen_threads, "store.load 未被调用"
    assert all(t is not threading.main_thread() for t in seen_threads), (
        "store.load 仍在主线程执行：事件循环会被 SQLite 阻塞"
    )


@pytest.mark.asyncio
async def test_sessions_list_sync_handler_runs_in_threadpool(tmp_path, auth_headers):
    from httpx import ASGITransport, AsyncClient

    from crew.app import build_app
    from crew.gateway.server import create_app
    from crew.state.config import Config

    crew = build_app(
        config=Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False),
        enable_team=False,
    )
    crew.session_store.save("s1", [Message.user("q1")], owner_account_id=OWNER)
    app = create_app(crew)

    seen_threads: list[threading.Thread] = []
    original_list = SQLiteSessionStore.list_sessions

    def spy_list(self, *args, **kwargs):
        seen_threads.append(threading.current_thread())
        return original_list(self, *args, **kwargs)

    with patch.object(SQLiteSessionStore, "list_sessions", spy_list):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
            resp = await client.get("/api/sessions")
            assert resp.status_code == 200
            assert [item["session_id"] for item in resp.json()] == ["s1"]

    assert seen_threads, "list_sessions 未被调用"
    assert all(t is not threading.main_thread() for t in seen_threads), (
        "纯同步 handler 未走线程池：事件循环会被 SQLite 阻塞"
    )
