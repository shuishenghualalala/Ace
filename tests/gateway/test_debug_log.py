"""session_debug_log 安全测试（G7）：精确会话匹配 + 有界读取。"""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from crew.app import build_app
from crew.core.types import Message
from crew.gateway.server import create_app


def test_default_config_disables_full_llm_trace():
    """完整 LLM trace 默认关闭，必须显式开启后才能落盘/查询。"""
    from crew.state.config import Config

    assert Config().llm_trace is False


@pytest.fixture
def crew_env(tmp_path, monkeypatch):
    """隔离 crew_home，并写入带两个会话事件的 llm.jsonl。"""
    home = tmp_path / ".crew"
    (home / "logs").mkdir(parents=True)
    monkeypatch.setenv("CREW_HOME", str(home))

    # 构造 trace：session "a" 与 session "alpha"（id 以 a 开头）。旧的前缀匹配会把
    # "alpha" 也算进 /api/session/a，必须修正为精确匹配。
    rows = [
        {"ts": 1, "session_id": "a", "owner_account_id": "A:uid-a", "msg": "belongs-to-a"},
        {"ts": 2, "session_id": "alpha", "owner_account_id": "A:uid-a", "msg": "must-NOT-leak"},
        {"ts": 3, "session_id": "a::sub", "owner_account_id": "A:uid-a", "msg": "old-prefix-suffix-must-NOT-leak"},
        {"ts": 4, "session_id": "a", "owner_account_id": "A:uid-a", "msg": "second-for-a"},
        {"ts": 5, "session_id": "a", "msg": "legacy-ownerless-must-NOT-leak"},
    ]
    trace = home / "logs" / "llm.jsonl"
    trace.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    return home


@pytest.mark.asyncio
async def test_debug_log_exact_session_match(crew_env, auth_headers, tmp_path):
    """查询 /api/session/a 只返回 session_id == 'a' 的事件，不泄露 'alpha'/'a::sub'。"""
    from crew.state.config import Config

    # 关闭 dev_mode，否则 loopback 身份被劫持成 dev:dev，会话归属校验 404
    crew = build_app(
        config=Config(
            db_path=str(tmp_path / "crew.db"),
            cron_enabled=False,
            gateway_dev_mode=False,
            llm_trace=True,
        ),
        enable_team=False,
    )
    crew.session_store.save("a", [Message.user("debug")], owner_account_id="A:uid-a")
    app = create_app(crew)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.get("/api/session/a/debug-log")
    assert resp.status_code == 200
    data = resp.json()
    msgs = [ev["msg"] for ev in data["events"]]
    assert "belongs-to-a" in msgs
    assert "second-for-a" in msgs
    # 关键：前缀相近的其他会话事件不应泄露
    assert "must-NOT-leak" not in msgs
    assert "old-prefix-suffix-must-NOT-leak" not in msgs
    assert "legacy-ownerless-must-NOT-leak" not in msgs
    # 全部返回事件都属于精确会话 a
    assert all(ev["session_id"] == "a" for ev in data["events"])


@pytest.mark.asyncio
async def test_debug_log_limit_bounds_result(crew_env, auth_headers, tmp_path):
    """limit 截断后只保留最后 N 条匹配（deque maxlen）。"""
    home = crew_env
    # 追加大量 a 会话事件
    trace = home / "logs" / "llm.jsonl"
    with trace.open("a", encoding="utf-8") as fh:
        for i in range(500, 600):
            fh.write(json.dumps({
                "ts": i,
                "session_id": "a",
                "owner_account_id": "A:uid-a",
                "msg": f"bulk-{i}",
            }) + "\n")
    from crew.state.config import Config

    crew = build_app(
        config=Config(
            db_path=str(tmp_path / "crew.db"),
            cron_enabled=False,
            gateway_dev_mode=False,
            llm_trace=True,
        ),
        enable_team=False,
    )
    crew.session_store.save("a", [Message.user("debug")], owner_account_id="A:uid-a")
    app = create_app(crew)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.get("/api/session/a/debug-log", params={"limit": 5})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["events"]) == 5
    # 是最后 5 条（按 ts 升序）
    assert [ev["msg"] for ev in data["events"]] == [f"bulk-{i}" for i in range(595, 600)]
