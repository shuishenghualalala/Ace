"""Two-process Companion round-trip through the pluggable Nearby transport."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import queue
import re
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from crew.app import build_app
from crew.gateway.server import create_app
from crew.state.config import Config

ROOT = Path(__file__).resolve().parents[1]


class _NearbyNode:
    def __init__(self, binary: Path, endpoint: str, peer_id: str, name: str, state_dir: Path):
        self.process = subprocess.Popen(
            [
                str(binary),
                "--transport", "mock",
                "--mock-endpoint", endpoint,
                "--peer-id", peer_id,
                "--display-name", name,
                "--state-dir", str(state_dir),
                "--ipc",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._pending: list[dict[str, Any]] = []
        threading.Thread(target=self._read_stdout, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            try:
                self._events.put(json.loads(line))
            except json.JSONDecodeError:
                continue

    def send(self, command: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(command, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def wait_for(
        self,
        event_type: str,
        predicate: Callable[[dict[str, Any]], bool] | None = None,
        timeout: float = 8,
    ) -> dict[str, Any]:
        predicate = predicate or (lambda _event: True)
        deadline = time.monotonic() + timeout
        while True:
            for index, event in enumerate(self._pending):
                if event.get("type") == event_type and predicate(event):
                    return self._pending.pop(index)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stderr = ""
                if self.process.poll() is not None and self.process.stderr is not None:
                    stderr = self.process.stderr.read()
                raise AssertionError(
                    f"Nearby event {event_type!r} timed out; pending={self._pending!r}; stderr={stderr}"
                )
            self._pending.append(self._events.get(timeout=remaining))

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        try:
            self.send({"type": "shutdown"})
            self.process.wait(timeout=3)
        except (BrokenPipeError, subprocess.TimeoutExpired):
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)


def _free_endpoint() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return f"127.0.0.1:{listener.getsockname()[1]}"


def _wait_for_bus(endpoint: str, process: subprocess.Popen[str]) -> None:
    host, raw_port = endpoint.rsplit(":", 1)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr is not None else ""
            raise AssertionError(f"Mock Bus exited early: {stderr}")
        try:
            with socket.create_connection((host, int(raw_port)), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError("Mock Bus did not start")


def _gateway(tmp_path: Path, name: str):
    home = tmp_path / name
    home.mkdir()
    crew = build_app(
        config=Config(
            db_path=str(home / "crew.db"),
            memory_db_path=str(home / "memory.db"),
            crew_home=str(home),
            cron_enabled=False,
        ),
        enable_team=False,
    )
    return crew, create_app(crew)


@pytest.mark.asyncio
async def test_two_instances_exchange_and_restore_companion_history(tmp_path, auth_headers):
    executable = "crew-nearby.exe" if os.name == "nt" else "crew-nearby"
    await asyncio.to_thread(
        subprocess.run,
        ["cargo", "build", "--quiet", "--manifest-path", str(ROOT / "nearby" / "Cargo.toml")],
        cwd=ROOT,
        check=True,
        timeout=120,
    )
    binary = ROOT / "nearby" / "target" / "debug" / executable
    endpoint = _free_endpoint()
    bus = await asyncio.to_thread(
        subprocess.Popen,
        [str(binary), "--transport", "mock", "--mock-endpoint", endpoint, "--mock-bus"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    node_a: _NearbyNode | None = None
    node_b: _NearbyNode | None = None
    try:
        _wait_for_bus(endpoint, bus)
        node_a = _NearbyNode(binary, endpoint, "ace_alice", "Alice", tmp_path / "nearby-a")
        node_a.wait_for("ready")
        node_b = _NearbyNode(binary, endpoint, "ace_bob", "Bob", tmp_path / "nearby-b")
        node_b.wait_for("ready")
        node_a.wait_for("peer_discovered", lambda event: event["peer"]["peer_id"] == "ace_bob")
        node_b.wait_for("peer_discovered", lambda event: event["peer"]["peer_id"] == "ace_alice")
        node_a.send({"type": "connect_peer", "peer_id": "ace_bob"})
        node_a.wait_for("peer_connected", lambda event: event["peer"]["peer_id"] == "ace_bob")
        node_b.wait_for("peer_connected", lambda event: event["peer"]["peer_id"] == "ace_alice")

        crew_a, app_a = _gateway(tmp_path, "gateway-a")
        crew_b, app_b = _gateway(tmp_path, "gateway-b")
        async with (
            AsyncClient(
                transport=ASGITransport(app=app_a), base_url="http://alice", headers=auth_headers
            ) as alice,
            AsyncClient(
                transport=ASGITransport(app=app_b), base_url="http://bob", headers=auth_headers
            ) as bob,
        ):
            for client, peer_id, display_name in (
                (alice, "ace_bob", "Bob"),
                (bob, "ace_alice", "Alice"),
            ):
                response = await client.post("/api/companion/link-state", json={
                    "type": "peer",
                    "peer_id": peer_id,
                    "profile": {"peer_id": peer_id, "display_name": display_name},
                    "connection_state": "connected",
                })
                assert response.status_code == 200

            opened_a = await alice.post("/api/companion/conversations/open", json={
                "kind": "nearby_dm", "target_id": "ace_bob", "title": "Bob"
            })
            session_a = opened_a.json()["session_id"]
            sent_a = await alice.post(
                f"/api/companion/conversations/{session_a}/messages",
                json={"text": "Alice 发出的消息"},
            )
            event_a = sent_a.json()["event_id"]
            node_a.send({
                "type": "send_peer_message",
                "peer_id": "ace_bob",
                "text": "Alice 发出的消息",
                "client_message_id": event_a,
            })
            incoming_b = await asyncio.to_thread(node_b.wait_for, "peer_message_received")
            projected_b = await bob.post("/api/companion/link-state", json={
                "type": "message",
                "kind": "nearby_dm",
                "target_id": "ace_alice",
                "conversation_title": "Alice",
                "message_id": incoming_b["message_id"],
                "sender_id": "ace_alice",
                "sender_name": "Alice",
                "sender_kind": "human",
                "text": incoming_b["text"],
            })
            assert projected_b.json()["appended"] is True
            duplicate_b = await bob.post("/api/companion/link-state", json={
                "type": "message",
                "kind": "nearby_dm",
                "target_id": "ace_alice",
                "conversation_title": "Alice",
                "message_id": incoming_b["message_id"],
                "sender_id": "ace_alice",
                "sender_name": "Alice",
                "sender_kind": "human",
                "text": incoming_b["text"],
            })
            assert duplicate_b.json()["appended"] is False
            delivered_a = await asyncio.to_thread(
                node_a.wait_for,
                "message_delivered",
                lambda event: event["message_id"] == event_a,
            )
            settled_a = await alice.post(
                f"/api/companion/outbox/{delivered_a['message_id']}/settle",
                json={"status": "delivered"},
            )
            assert settled_a.json()["status"] == "delivered"

            session_b = projected_b.json()["binding"]["session_id"]
            sent_b = await bob.post(
                f"/api/companion/conversations/{session_b}/messages",
                json={"text": "Bob 的回复"},
            )
            event_b = sent_b.json()["event_id"]
            node_b.send({
                "type": "send_peer_message",
                "peer_id": "ace_alice",
                "text": "Bob 的回复",
                "client_message_id": event_b,
            })
            incoming_a = await asyncio.to_thread(node_a.wait_for, "peer_message_received")
            projected_a = await alice.post("/api/companion/link-state", json={
                "type": "message",
                "kind": "nearby_dm",
                "target_id": "ace_bob",
                "conversation_title": "Bob",
                "message_id": incoming_a["message_id"],
                "sender_id": "ace_bob",
                "sender_name": "Bob",
                "sender_kind": "human",
                "text": incoming_a["text"],
            })
            assert projected_a.json()["appended"] is True
            delivered_b = await asyncio.to_thread(
                node_b.wait_for,
                "message_delivered",
                lambda event: event["message_id"] == event_b,
            )
            await bob.post(
                f"/api/companion/outbox/{delivered_b['message_id']}/settle",
                json={"status": "delivered"},
            )

            filename = "截屏 2026-08-24 (最终版).png"
            file_bytes = b"companion-image-bytes"
            uploaded_response = await alice.post("/api/upload", json={
                "filename": filename,
                "content": base64.b64encode(file_bytes).decode("ascii"),
            })
            assert uploaded_response.status_code == 200
            uploaded = uploaded_response.json()
            assert re.fullmatch(r"att_[a-f0-9]{32}", uploaded["id"])

            prepared_response = await alice.post(
                "/api/companion/files/prepare", json=uploaded
            )
            assert prepared_response.status_code == 200
            prepared = prepared_response.json()["file"]
            assert prepared["file_id"] == uploaded["id"]
            assert prepared["name"] == filename

            queued_file = await alice.post(
                f"/api/companion/conversations/{session_a}/messages",
                json={"text": "", "attachments": [uploaded]},
            )
            assert queued_file.status_code == 200
            file_event_id = queued_file.json()["event_id"]
            node_a.send({
                "type": "send_peer_file",
                "peer_id": "ace_bob",
                "file_id": prepared["file_id"],
                "name": prepared["name"],
                "mime_type": prepared["mime_type"],
                "size": prepared["size"],
                "sha256": prepared["sha256"],
                "file_path": prepared["path"],
                "client_message_id": file_event_id,
            })
            incoming_file = await asyncio.to_thread(
                node_b.wait_for,
                "message",
                lambda event: event.get("message", {}).get("type") == "peer.file",
            )
            incoming_payload = incoming_file["message"]["payload"]["file"]
            projected_file = await bob.post("/api/companion/link-state", json={
                "type": "file",
                "kind": "nearby_dm",
                "target_id": "ace_alice",
                "conversation_title": "Alice",
                "message_id": f"file:{incoming_payload['file_id']}",
                "sender_id": "ace_alice",
                "sender_name": "Alice",
                "sender_kind": "human",
                "file": incoming_payload,
            })
            assert projected_file.status_code == 200
            received_attachment = projected_file.json()["attachment"]
            assert received_attachment["name"] == filename
            assert Path(received_attachment["path"]).read_bytes() == file_bytes

            delivered_file = await asyncio.to_thread(
                node_a.wait_for,
                "message_delivered",
                lambda event: event["message_id"] == file_event_id,
            )
            settled_file = await alice.post(
                f"/api/companion/outbox/{delivered_file['message_id']}/settle",
                json={"status": "delivered"},
            )
            assert settled_file.json()["status"] == "delivered"

            sessions_a = (await alice.get("/api/sessions", params={"workspace_id": "companion"})).json()
            sessions_b = (await bob.get("/api/sessions", params={"workspace_id": "companion"})).json()
            history_a = (await alice.get(f"/api/session/{session_a}")).json()
            history_b = (await bob.get(f"/api/session/{session_b}")).json()

        assert any(item["session_id"] == session_a for item in sessions_a)
        assert any(item["session_id"] == session_b for item in sessions_b)
        assert [item["content"] for item in history_a[:2]] == ["Alice 发出的消息", "Bob 的回复"]
        assert [item["content"] for item in history_b[:2]] == ["Alice 发出的消息", "Bob 的回复"]
        assert filename in history_a[2]["content"]
        assert filename in history_b[2]["content"]
        assert history_a[1]["origin"]["sender_name"] == "Bob"
        assert history_b[0]["origin"]["sender_name"] == "Alice"
        assert len(crew_a.session_store.load(session_a, owner_account_id="A:uid-a")) == 3
        assert len(crew_b.session_store.load(session_b, owner_account_id="A:uid-a")) == 3
    finally:
        if node_a is not None:
            node_a.close()
        if node_b is not None:
            node_b.close()
        if bus.poll() is None:
            bus.terminate()
            try:
                bus.wait(timeout=3)
            except subprocess.TimeoutExpired:
                bus.kill()
                bus.wait(timeout=3)
