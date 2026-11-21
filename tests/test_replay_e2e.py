"""Keyless assembled replay test: real app/provider/tools, recorded model boundary."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from crew.app import build_app
from crew.core.envelope import Envelope
from crew.state.config import Config, ModelProfile
from tests.e2e.replay_server import ReplayServer

FIXTURE = Path(__file__).parent / "e2e" / "fixtures" / "replay" / "partial_failure.jsonl"


def test_replay_server_fails_loud_when_no_response_remains():
    server = ReplayServer([]).start()
    try:
        with httpx.Client(trust_env=False, timeout=5) as client:
            response = client.post(
                f"{server.base_url}/chat/completions",
                json={"stream": False},
            )
    finally:
        server.stop()
    assert response.status_code == 500
    assert "no recorded model response" in response.text


@pytest.mark.asyncio
async def test_assembled_app_replays_recorded_partial_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / "crew-home"))
    monkeypatch.setenv("ACE_REPLAY_API_KEY", "replay-key")
    from crew.state.credentials import store_key

    store_key("local", "replay", "replay-key")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = "PRESENT-REPLAY"
    (workspace / "marker.txt").write_text(marker, encoding="utf-8")

    with ReplayServer.from_jsonl(FIXTURE) as server:
        profile = ModelProfile(
            id="replay",
            name="Replay",
            api_key="replay-key",
            api_key_env="ACE_REPLAY_API_KEY",
            base_url=server.base_url,
            model="deepseek-flash",
            max_tokens=1024,
            context_window=131072,
        )
        config = Config(
            db_path=str(tmp_path / "crew.db"),
            memory_db_path=str(tmp_path / "memory.db"),
            model_profiles={"replay": profile},
            active_model_id="replay",
            title_auto=False,
            cron_enabled=False,
            plugins_enabled=[],
        )
        config.activate_model("replay")
        app = build_app(config=config, enable_team=False)
        app.active_owner.claim("local")
        await app.startup(start_cron=False)
        try:
            workspace_id = app.workspace_store.create(
                "Replay", root_path=str(workspace), owner_account_id="local",
            )["id"]
            session_id = "replay-partial"
            app.session_store.ensure_session(
                session_id, workspace_id=workspace_id, owner_account_id="local",
            )
            envelope = Envelope.of(
                "Read missing.txt and marker.txt, then report both outcomes.",
                session_id=session_id,
                channel="cli",
                mode="agent",
                workspace_id=workspace_id,
            )
            chunks = [chunk async for chunk in app.handle(envelope)]
        finally:
            await app.shutdown()

    final = "".join(
        str(chunk.body.get("text") or "") for chunk in chunks if chunk.kind == "final"
    )
    tools = {
        str(chunk.body.get("name") or "")
        for chunk in chunks
        if chunk.kind == "tool" and chunk.body.get("phase") == "start"
    }
    errors = [chunk for chunk in chunks if chunk.kind == "error"]

    assert errors == []
    assert "file_read" in tools
    assert marker in final
    assert "missing" in final
    assert server.remaining == 0
