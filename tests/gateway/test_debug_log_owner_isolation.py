"""Owner-scoped debug log tests."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from crew.app import build_app
from crew.core.types import Message
from crew.gateway.server import create_app
from crew.state.config import Config
from crew.state import logging as trace_log


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_log", [False, True])
async def test_debug_log_filters_same_session_id_by_owner(tmp_path, monkeypatch, auth_headers, explicit_log):
    home = tmp_path / "测试用户" / ".Crew"
    log_file = str(tmp_path / "custom" / "crew.log") if explicit_log else ""
    install_root = tmp_path / "install"
    install_root.mkdir()
    (install_root / ".crew").write_bytes(b"cannot be a log directory")
    monkeypatch.chdir(install_root)
    monkeypatch.setenv("CREW_HOME", str(home))
    monkeypatch.setenv("CREW_PACKAGED", "1")

    crew = build_app(
        config=Config(db_path=str(tmp_path / "crew.db"), log_file=log_file, cron_enabled=False, gateway_dev_mode=False),
        enable_team=False,
    )
    crew.session_store.save("same", [Message.user("a")], owner_account_id="A:uid-a")
    crew.session_store.save("same", [Message.user("b")], owner_account_id="B:uid-b")
    app = create_app(crew)
    transport = ASGITransport(app=app)
    trace_log._setup_llm_trace(log_file)
    try:
        for owner, message in (("A:uid-a", "a-only"), ("B:uid-b", "b-only")):
            trace_log.llm_trace("response", {"session_id": "same", "owner_account_id": owner, "msg": message})
        assert trace_log.flush_llm_trace()
    finally:
        trace_log.shutdown_llm_trace()
    expected_logs = tmp_path / "custom" if explicit_log else home / "logs"
    assert (expected_logs / "llm.jsonl").is_file()
    assert list(install_root.iterdir()) == [install_root / ".crew"]

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
