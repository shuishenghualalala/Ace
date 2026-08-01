"""会话过期 + AgentManager 淘汰测试。"""

import time

import pytest

from crew.app import AgentManager
from crew.state.session_store import SQLiteSessionStore


class _FakeAgent:
    """测试用 Agent 替身。"""

    def __init__(self) -> None:
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1


def _factory():
    return _FakeAgent()


def _session_keys(mgr: AgentManager) -> set[str]:
    return {key[1] for key in mgr._cache}  # key = (owner, session_id, fingerprint)
    # 缓存键为 (owner_account_id, session_id, fingerprint)
    return {key[1] for key in mgr._cache}


def _first_key(mgr: AgentManager, session_id: str):
    return next(key for key in mgr._cache if key[1] == session_id)


# ---------------------------------------------------------------------------
# AgentManager LRU + TTL 测试
# ---------------------------------------------------------------------------


def test_lru_eviction_on_cap():
    """超过 max_size 时淘汰最久未用的条目。"""
    mgr = AgentManager(_factory, max_size=3)
    mgr.get("s1")
    mgr.get("s2")
    mgr.get("s3")
    assert len(mgr._cache) == 3
    # 加入第 4 个，s1 应被淘汰
    mgr.get("s4")
    assert len(mgr._cache) == 3
    assert "s1" not in _session_keys(mgr)


def test_lru_move_to_end_on_access():
    """访问已有条目时移到末尾，不被淘汰。"""
    mgr = AgentManager(_factory, max_size=3)
    mgr.get("s1")
    mgr.get("s2")
    mgr.get("s3")
    # 访问 s1，使其成为最近使用
    mgr.get("s1")
    # 加入新条目，s2 应被淘汰（最久未用）
    mgr.get("s4")
    assert "s2" not in _session_keys(mgr)
    assert "s1" in _session_keys(mgr)  # s1 刚被访问过，不应被淘汰


def test_evict_idle():
    """超过 idle_ttl 的条目被淘汰。"""
    mgr = AgentManager(_factory, max_size=10, idle_ttl=1.0)
    mgr.get("s1")
    # 手动模拟时间流逝：设置 s1 的访问时间为很久以前
    mgr._access_ts[_first_key(mgr, "s1")] = time.monotonic() - 100.0
    # s2 最近访问
    mgr.get("s2")

    evicted = mgr.evict_idle()
    assert evicted == 1
    assert "s1" not in _session_keys(mgr)
    assert "s2" in _session_keys(mgr)


def test_evict_idle_none_expired():
    """所有条目都在 TTL 内，不淘汰。"""
    mgr = AgentManager(_factory, max_size=10, idle_ttl=3600.0)
    mgr.get("s1")
    mgr.get("s2")
    evicted = mgr.evict_idle()
    assert evicted == 0
    assert len(mgr._cache) == 2


def test_drop_and_clear():
    """drop 和 clear 正常工作。"""
    mgr = AgentManager(_factory)
    mgr.get("s1")
    mgr.get("s2")
    mgr.drop("s1")
    assert "s1" not in _session_keys(mgr)
    mgr.clear()
    assert len(mgr._cache) == 0


@pytest.mark.asyncio
async def test_active_agent_eviction_waits_for_last_lease_then_closes_once():
    created: list[_FakeAgent] = []

    def factory():
        agent = _FakeAgent()
        created.append(agent)
        return agent

    mgr = AgentManager(factory)
    async with mgr.lease("s1") as old_agent:
        mgr.drop("s1")
        assert old_agent.close_calls == 0
        replacement = mgr.get("s1")
        assert replacement is not old_agent

    await mgr.wait_closed()
    assert old_agent.close_calls == 1
    assert replacement.close_calls == 0


@pytest.mark.asyncio
async def test_lru_and_idle_eviction_close_inactive_agents():
    mgr = AgentManager(_factory, max_size=1, idle_ttl=1.0)
    first = mgr.get("s1")
    second = mgr.get("s2")
    await mgr.wait_closed()
    assert first.close_calls == 1
    assert second.close_calls == 0

    mgr._access_ts[_first_key(mgr, "s2")] = time.monotonic() - 100.0
    assert mgr.evict_idle() == 1
    await mgr.wait_closed()
    assert second.close_calls == 1


@pytest.mark.asyncio
async def test_agent_manager_repeated_aclose_is_safe_and_rejects_new_leases():
    mgr = AgentManager(_factory)
    agent = mgr.get("s1")

    await mgr.aclose()
    await mgr.aclose()

    assert agent.close_calls == 1
    with pytest.raises(RuntimeError, match="已关闭"):
        async with mgr.lease("s2"):
            pass


# ---------------------------------------------------------------------------
# SessionStore 过期清理测试
# ---------------------------------------------------------------------------


def test_expire_idle_sessions(tmp_path):
    """空闲超时的会话被清理。"""
    db_path = str(tmp_path / "test.db")
    store = SQLiteSessionStore(db_path)

    from crew.core.types import Message

    # 保存两个会话
    store.save("s1", [Message.user("hello")])
    store.save("s2", [Message.user("world")])
    # 标记 s1 为 completed
    store.set_status("s1", "completed")

    # 手动把 s1 的 updated_at 改为很久以前
    old_time = time.time() - 7200  # 2 小时前
    store._conn.execute("UPDATE sessions SET updated_at = ? WHERE session_id = ?", (old_time, "s1"))
    store._conn.commit()

    # 清理 1 小时（3600秒）前的空闲会话
    expired = store.expire_idle_sessions(3600.0)
    assert expired == 1
    # s1 被清理，s2 保留
    assert store.load("s1") == []
    assert len(store.load("s2")) == 1


def test_expire_does_not_remove_running_sessions(tmp_path):
    """正在运行的会话不被清理。"""
    db_path = str(tmp_path / "test.db")
    store = SQLiteSessionStore(db_path)

    from crew.core.types import Message

    store.save("s1", [Message.user("hello")])
    # 模拟正在运行（last_status 为空或 running）
    store.set_status("s1", "running")

    # 把 updated_at 改为很久以前
    old_time = time.time() - 7200
    store._conn.execute("UPDATE sessions SET updated_at = ? WHERE session_id = ?", (old_time, "s1"))
    store._conn.commit()

    # 清理不应删除正在运行的会话
    expired = store.expire_idle_sessions(3600.0)
    assert expired == 0
    assert len(store.load("s1")) == 1


def test_expire_zero_timeout_no_op(tmp_path):
    """idle_seconds=0 时不清理。"""
    db_path = str(tmp_path / "test.db")
    store = SQLiteSessionStore(db_path)

    from crew.core.types import Message

    store.save("s1", [Message.user("hello")])
    old_time = time.time() - 7200
    store._conn.execute("UPDATE sessions SET updated_at = ? WHERE session_id = ?", (old_time, "s1"))
    store._conn.commit()

    expired = store.expire_idle_sessions(0)
    assert expired == 0


def test_expire_excludes_running_via_exclude_set(tmp_path):
    """显式传入 running/queued session_id 时，即使 last_status 是 completed 也不清理。"""
    db_path = str(tmp_path / "test.db")
    store = SQLiteSessionStore(db_path)

    from crew.core.types import Message

    store.save("s1", [Message.user("hello")])
    store.set_status("s1", "completed")
    old_time = time.time() - 7200
    store._conn.execute("UPDATE sessions SET updated_at = ? WHERE session_id = ?", (old_time, "s1"))
    store._conn.commit()

    expired = store.expire_idle_sessions(3600.0, exclude_session_ids={"s1"})
    assert expired == 0
    assert len(store.load("s1")) == 1


def test_set_running_status_refreshes_updated_at(tmp_path):
    """set_status('running') 会刷新 updated_at，防止长任务被过期清理。"""
    db_path = str(tmp_path / "test.db")
    store = SQLiteSessionStore(db_path)

    from crew.core.types import Message

    store.save("s1", [Message.user("hello")])
    old_time = time.time() - 7200
    store._conn.execute("UPDATE sessions SET updated_at = ? WHERE session_id = ?", (old_time, "s1"))
    store._conn.commit()

    store.set_status("s1", "running")
    row = store._conn.execute("SELECT updated_at, last_status FROM sessions WHERE session_id = ?", ("s1",)).fetchone()
    assert row[1] == "running"
    assert row[0] > old_time + 1
