"""Keyless assembled replay tests: real app/provider/tools, recorded model boundary."""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from crew.app import build_app
from crew.core.envelope import Envelope
from crew.state.config import Config, ModelProfile
from crew.state.credentials import store_key
from tests.e2e.replay_server import ReplayServer, recorded_responses

FIXTURES = Path(__file__).parent / "e2e" / "fixtures" / "replay"
HTTP_MCP_FIXTURE = Path(__file__).parent / "e2e" / "fixtures" / "http_echo_mcp.py"


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


async def _run_assembled(
    fixture: Path,
    files: dict[str, str],
    prompt: str,
    tmp_path: Path,
    monkeypatch,
    *,
    cron: bool = False,
    mcp_url: str = "",
):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / "crew-home"))
    monkeypatch.setenv("ACE_REPLAY_API_KEY", "replay-key")
    store_key("local", "replay", "replay-key")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for relative, content in files.items():
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    server = ReplayServer.from_jsonl(fixture).start()
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
        cron_db_path=str(tmp_path / "cron.db"),
        model_profiles={"replay": profile},
        active_model_id="replay",
        title_auto=False,
        cron_enabled=cron,
        mcp_servers={"echo": {"url": mcp_url, "transport": "http"}} if mcp_url else {},
        plugins_enabled=[],
    )
    config.activate_model("replay")
    app = build_app(config=config, enable_team=False)
    app.active_owner.claim("local")
    try:
        await app.startup(start_cron=cron)
        try:
            if mcp_url:
                for _ in range(100):
                    rows = app.mcp_manager.status() if app.mcp_manager is not None else []
                    if rows and all(row.get("connected") for row in rows):
                        break
                    await asyncio.sleep(0.05)
            workspace_id = app.workspace_store.create(
                "Replay", root_path=str(workspace), owner_account_id="local",
            )["id"]
            session_id = "replay-case"
            app.session_store.ensure_session(
                session_id, workspace_id=workspace_id, owner_account_id="local",
            )
            envelope = Envelope.of(
                prompt,
                session_id=session_id,
                channel="cli",
                mode="agent",
                workspace_id=workspace_id,
            )
            if mcp_url:
                envelope.params["_allow_mcp_install"] = True
            chunks = [chunk async for chunk in app.handle(envelope)]
            jobs = app.cron_store.list(owner_account_id="local") if cron else []
        finally:
            await app.shutdown()
    finally:
        server.stop()
    return chunks, workspace, server, jobs


def _tools(chunks) -> set[str]:
    return {
        str(chunk.body.get("name") or "")
        for chunk in chunks
        if chunk.kind == "tool" and chunk.body.get("phase") == "start"
    }


def _final(chunks) -> str:
    return "".join(
        str(chunk.body.get("text") or "") for chunk in chunks if chunk.kind == "final"
    )


@pytest.mark.asyncio
async def test_assembled_app_replays_recorded_partial_failure(tmp_path, monkeypatch):
    chunks, _, server, _ = await _run_assembled(
        FIXTURES / "partial_failure.jsonl",
        {"marker.txt": "PRESENT-REPLAY"},
        "Read missing.txt and marker.txt, then report both outcomes.",
        tmp_path,
        monkeypatch,
    )
    final = _final(chunks)
    assert [chunk for chunk in chunks if chunk.kind == "error"] == []
    assert "file_read" in _tools(chunks)
    assert "PRESENT-REPLAY" in final
    assert "missing" in final
    assert server.remaining == 0


@pytest.mark.asyncio
async def test_assembled_app_replays_a_recorded_write(tmp_path, monkeypatch):
    chunks, workspace, server, _ = await _run_assembled(
        FIXTURES / "write_file.jsonl",
        {"input.txt": "hello-replay"},
        "Read input.txt, write output.txt, and reply done.",
        tmp_path,
        monkeypatch,
    )
    assert [chunk for chunk in chunks if chunk.kind == "error"] == []
    assert {"file_read", "file_write"} <= _tools(chunks)
    assert (workspace / "output.txt").read_text(encoding="utf-8") == "hello-replay-done"
    assert "done" in _final(chunks)
    assert server.remaining == 0


@pytest.mark.asyncio
async def test_assembled_app_replays_a_recorded_cron_create(tmp_path, monkeypatch):
    chunks, _, server, jobs = await _run_assembled(
        FIXTURES / "cron_create.jsonl",
        {},
        "Create the cron job now.",
        tmp_path,
        monkeypatch,
        cron=True,
    )
    assert [chunk for chunk in chunks if chunk.kind == "error"] == []
    assert {"tool_search", "cron_create"} <= _tools(chunks)
    assert _final(chunks).strip() == "created"
    assert len(jobs) == 1
    assert jobs[0]["name"] == "replay-cron"
    assert jobs[0]["query"] == "reply with cron-ok"
    assert server.remaining == 0


@pytest.mark.asyncio
async def test_assembled_app_replays_mcp_status(tmp_path, monkeypatch):
    process, url = _start_http_mcp()
    try:
        chunks, _, server, _ = await _run_assembled(
            FIXTURES / "mcp_status.jsonl",
            {},
            "Check the echo MCP server and reply with its state.",
            tmp_path,
            monkeypatch,
            mcp_url=url,
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    assert [chunk for chunk in chunks if chunk.kind == "error"] == []
    assert "mcp_status" in _tools(chunks)
    assert _final(chunks).strip() == "connected"
    assert any(
        "echo" in json.dumps(chunk.body, ensure_ascii=False)
        for chunk in chunks
        if chunk.kind == "tool"
    )
    assert server.remaining == 0


def test_recorded_responses_extracts_replayable_events(tmp_path):
    trace = tmp_path / "llm.jsonl"
    trace.write_text(
        "\n".join([
            '{"dir": "request", "session_id": "s1", "messages": []}',
            '{"dir": "response", "session_id": "s1", "text": "done", "tool_calls": [], "finish_reason": "stop"}',
            '{"dir": "response", "session_id": "s2", "text": "other", "tool_calls": [], "finish_reason": "stop"}',
            '{"dir": "response", "session_id": "s1", "text": "", "tool_calls": [{"id": "c", "name": "file_read", "arguments": {"path": "a"}}], "finish_reason": "tool_calls"}',
            '{"dir": "aux_response", "session_id": "s1", "text": "title"}',
            '{"dir": "response", "session_id": "s1", "text": "", "tool_calls": []}',
        ]) + "\n",
        encoding="utf-8",
    )

    responses = recorded_responses(trace, session_id="s1")
    assert [item["text"] for item in responses] == ["done", ""]
    assert responses[1]["tool_calls"][0]["name"] == "file_read"
    assert recorded_responses(trace) == responses[:1] + [
        {"text": "other", "tool_calls": [], "finish_reason": "stop", "reasoning": ""},
    ] + responses[1:]


def test_record_replay_fixture_cli_writes_a_fixture(tmp_path):
    trace = tmp_path / "llm.jsonl"
    trace.write_text(
        '{"dir": "response", "session_id": "s1", "text": "done", "tool_calls": [], "finish_reason": "stop"}\n',
        encoding="utf-8",
    )
    output = tmp_path / "fixture.jsonl"
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "scripts" / "record_replay_fixture.py"),
            "--trace", str(trace),
            "--output", str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8").strip() == (
        '{"text": "done", "tool_calls": [], "finish_reason": "stop", "reasoning": ""}'
    )
    repeated = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "scripts" / "record_replay_fixture.py"),
            "--trace", str(trace),
            "--output", str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert repeated.returncode != 0
    assert "output exists" in repeated.stderr


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
