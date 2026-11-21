"""Keyless Gateway approval for MCP update over a real HTTP MCP server."""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from crew.gateway.server import create_app
from crew.state.config import Config, ModelProfile
from crew.state.credentials import store_key
from tests.e2e.replay_server import ReplayServer

ROOT = Path(__file__).parent
HTTP_MCP_FIXTURE = ROOT / "e2e" / "fixtures" / "http_echo_mcp.py"


def _start_http_mcp() -> tuple[subprocess.Popen, str]:
    pytest.importorskip("mcp")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    process = subprocess.Popen(
        [sys.executable, str(HTTP_MCP_FIXTURE), str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return process, f"http://127.0.0.1:{port}/mcp"
        except OSError:
            time.sleep(0.05)
    process.terminate()
    raise AssertionError("HTTP MCP fixture did not start")


@asynccontextmanager
async def _open_ws(app):
    inbound: asyncio.Queue = asyncio.Queue()
    outbound: asyncio.Queue = asyncio.Queue()
    scope = {
        "type": "websocket",
        "scheme": "ws",
        "path": "/ws",
        "raw_path": b"/ws",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "subprotocols": [],
        "http_version": "1.1",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "server": ("127.0.0.1", 8765),
        "client": ("127.0.0.1", 16000),
        "state": {},
    }
    task = asyncio.create_task(app(scope, inbound.get, outbound.put))
    await inbound.put({"type": "websocket.connect"})
    first = await asyncio.wait_for(outbound.get(), 5)
    assert first["type"] == "websocket.accept", first

    async def send(message):
        await inbound.put({"type": "websocket.receive", "text": json.dumps(message)})

    async def receive():
        event = await asyncio.wait_for(outbound.get(), 10)
        assert event["type"] == "websocket.send", event
        return json.loads(event.get("text") or event["bytes"])

    try:
        yield send, receive
    finally:
        await inbound.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(task, 10)


@pytest.mark.asyncio
async def test_gateway_replays_mcp_update_approval(tmp_path, monkeypatch):
    from crew.app import build_app

    process, mcp_url = _start_http_mcp()
    fixture = tmp_path / "mcp_update.jsonl"
    fixture.write_text(
        json.dumps({
            "text": "",
            "tool_calls": [{
                "id": "call_replay_update",
                "name": "mcp_update",
                "arguments": {
                    "name": "echo",
                    "transport": "http",
                    "url": mcp_url,
                    "headers": {"X-Replay": "1"},
                },
            }],
            "finish_reason": "tool_calls",
        }) + "\n" + json.dumps({
            "text": "updated",
            "tool_calls": [],
            "finish_reason": "stop",
        }) + "\n",
        encoding="utf-8",
    )
    replay = ReplayServer.from_jsonl(fixture).start()
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "mcp_servers:\n"
        "  echo:\n"
        f"    url: {mcp_url}\n"
        "    transport: http\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ACE_REPLAY_API_KEY", "replay-key")
    store_key("local", "replay", "replay-key")
    profile = ModelProfile(
        id="replay",
        name="Replay",
        api_key="replay-key",
        api_key_env="ACE_REPLAY_API_KEY",
        base_url=replay.base_url,
        model="deepseek-flash",
        max_tokens=1024,
        context_window=131072,
    )
    config = Config(
        db_path=str(tmp_path / "crew.db"),
        memory_db_path=str(tmp_path / "memory.db"),
        cron_db_path=str(tmp_path / "cron.db"),
        config_path=str(config_file),
        model_profiles={"replay": profile},
        active_model_id="replay",
        title_auto=False,
        plugins_enabled=[],
        gateway_dev_mode=False,
        gateway_admin_accounts=["local"],
        auth_mode="local",
        mcp_servers={"echo": {"url": mcp_url, "transport": "http"}},
    )
    config.activate_model("replay")
    crew = build_app(config=config, enable_team=False)
    crew.active_owner.claim("local")
    app = create_app(crew)
    workspace_id = "default"
    session_id = "replay-mcp-update"
    crew.session_store.ensure_session(
        session_id, workspace_id=workspace_id, owner_account_id="local",
    )
    try:
        await crew.startup(start_cron=False)
        for _ in range(100):
            rows = crew.mcp_manager.status() if crew.mcp_manager is not None else []
            if rows and all(row.get("connected") for row in rows):
                break
            await asyncio.sleep(0.05)
        request_id = "replay-mcp-update-turn"
        frames = []
        async with _open_ws(app) as (send, receive):
            await send({
                "session_id": session_id,
                "request_id": request_id,
                "query": "Update the echo MCP server headers.",
                "mode": "agent",
                "workspace_id": workspace_id,
            })
            for _ in range(100):
                event = await receive()
                frames.append(event)
                if event.get("kind") == "followup_question":
                    body = event.get("body") if isinstance(event.get("body"), dict) else {}
                    if any(item.get("id") == "perm" for item in body.get("questions") or []):
                        await send({
                            "action": "followup_answer",
                            "session_id": session_id,
                            "request_id": request_id,
                            "question_id": body.get("question_id"),
                            "submission_id": "approve-update",
                            "answers": [{"id": "perm", "answers": ["allow_once"]}],
                        })
                if event.get("is_final") is True:
                    break
        assert any(
            event.get("kind") == "followup_question" for event in frames
        ), frames
        assert "X-Replay" in config_file.read_text(encoding="utf-8")
        assert any(
            event.get("is_final") is True
            and (event.get("body") or {}).get("text") == "updated"
            for event in frames
        ), frames
    finally:
        await crew.shutdown()
        replay.stop()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
