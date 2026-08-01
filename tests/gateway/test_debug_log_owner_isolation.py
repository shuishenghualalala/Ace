"""Owner-scoped debug log tests."""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from crew.app import build_app
from crew.core.types import Message
from crew.gateway.server import create_app
from crew.state.config import Config


@pytest.mark.asyncio
async def test_debug_log_filters_same_session_id_by_owner(tmp_path, monkeypatch, auth_headers):
    home = tmp_path / ".crew"
    logs = home / "logs"
    logs.mkdir(parents=True)
    monkeypatch.setenv("CREW_HOME", str(home))
    trace = logs / "llm.jsonl"
    rows = [
        {"ts": 1, "session_id": "same", "owner_account_id": "A:uid-a", "msg": "a-only"},
        {"ts": 2, "session_id": "same", "owner_account_id": "B:uid-b", "msg": "b-only"},
    ]
    trace.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    crew = build_app(
        config=Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False, gateway_dev_mode=False),
        enable_team=False,
    )
    crew.session_store.save("same", [Message.user("a")], owner_account_id="A:uid-a")
    crew.session_store.save("same", [Message.user("b")], owner_account_id="B:uid-b")
    app = create_app(crew)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp_a = await client.get("/api/session/same/debug-log")
        logged_out = await client.post("/api/auth/logout")
        assert logged_out.status_code == 200
        monkeypatch.setattr("crew.gateway.auth.LOCAL_OWNER_ACCOUNT_ID", "B:uid-b")
        resp_b = await client.get("/api/session/same/debug-log")

    assert resp_a.status_code == 200
    assert resp_b.status_code == 200
    assert [event["msg"] for event in resp_a.json()["events"]] == ["a-only"]
    assert [event["msg"] for event in resp_b.json()["events"]] == ["b-only"]
