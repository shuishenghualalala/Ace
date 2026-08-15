"""Gateway WebSocket protocol boundary security tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request, WebSocket
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import crew.gateway.connections as connections_module
from crew.core.envelope import ResponseChunk
from crew.gateway.app import (
    _GatewayJSONStructureLimitMiddleware,
    _GatewayRequestBodyLimitMiddleware,
)
from crew.gateway.connections import ConnectionManager
from crew.gateway.json_budget import JSONStructureBudget, JSONStructureInvalid
from crew.gateway.streaming import StreamLimits, bounded_streaming_response
from crew.gateway.ws import WebSocketProtocolError, create_ws_router, validate_ws_message


class _ActiveOwner:
    def claim(self, _owner_account_id: str):
        return SimpleNamespace(claimed_at=1.0)


class _SessionStore:
    def __init__(self) -> None:
        self.sessions = {"s1"}

    def session_belongs_to(self, session_id: str, owner_account_id: str) -> bool:
        return owner_account_id == "local" and session_id in self.sessions

    def ensure_session(self, session_id: str, **_kwargs) -> None:
        self.sessions.add(session_id)

    def get_agent_config(self, _session_id: str, **_kwargs):
        return {}


class _Plugins:
    async def run_plugin_command(self, *_args, **_kwargs):
        return None


class _SecurityLifecycle:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str]] = []

    def resume_session(self, owner: str, session_id: str) -> None:
        self.events.append(("resume", owner, session_id))

    def freeze_session(self, owner: str, session_id: str) -> None:
        self.events.append(("freeze", owner, session_id))


class _Crew:
    def __init__(self) -> None:
        self.config = SimpleNamespace(auth_session_ttl_seconds=600)
        self.active_owner = _ActiveOwner()
        self.session_store = _SessionStore()
        self.plugins = _Plugins()
        self.plan_manager = None
        self.wiki_manager = None
        self.security_service = _SecurityLifecycle()

    async def dispatch(self, envelope):
        yield ResponseChunk.final(envelope.request_id, "ok")


def _client(*, connections: ConnectionManager | None = None, crew: _Crew | None = None):
    app = FastAPI()
    crew = crew or _Crew()
    connections = connections or ConnectionManager(min_interval=0)
    dispatcher = SimpleNamespace()
    channel_manager = SimpleNamespace(status=list)
    app.include_router(create_ws_router(crew, dispatcher, connections, channel_manager))
    return TestClient(app), crew, connections


def _json_budget_client(**limits):
    app = FastAPI()
    app.add_middleware(_GatewayJSONStructureLimitMiddleware, **limits)

    @app.post("/json")
    async def json_body(payload: dict):
        return payload

    @app.post("/raw")
    async def raw_body(request: Request):
        return {"size": len(await request.body())}

    @app.websocket("/socket")
    async def socket(ws: WebSocket):
        await ws.accept()
        await ws.send_text("ok")
        await ws.close()

    return TestClient(app)


@pytest.mark.parametrize(
    "headers,expected",
    [
        (
            [(b"content-length", b"0"), (b"content-length", b"0")],
            "重复 Content-Length",
        ),
        (
            [(b"transfer-encoding", b"chunked"), (b"transfer-encoding", b"chunked")],
            "重复 Transfer-Encoding",
        ),
        (
            [(b"content-length", b"0"), (b"transfer-encoding", b"chunked")],
            "Content-Length 与 Transfer-Encoding 同时出现",
        ),
        (
            [(b"transfer-encoding", b"gzip")],
            "不支持的 Transfer-Encoding",
        ),
    ],
)
def test_http_request_framing_rejects_duplicate_or_ambiguous_headers(
    headers,
    expected,
) -> None:
    async def noop_app(_scope, _receive, _send) -> None:
        raise AssertionError("ambiguous framing reached the application")

    async def run_case() -> None:
        middleware = _GatewayRequestBodyLimitMiddleware(noop_app)
        events: list[dict] = []

        async def send(message: dict) -> None:
            events.append(message)

        await middleware(
            {
                "type": "http",
                "method": "POST",
                "path": "/",
                "headers": headers,
            },
            lambda: {"type": "http.request", "body": b""},
            send,
        )

        assert events[0]["status"] == 400
        assert expected in events[1]["body"].decode("utf-8")

    asyncio.run(run_case())


def test_http_json_structure_budget_rejects_before_route_and_supports_json_media_types():
    client = _json_budget_client(max_depth=2)

    response = client.post(
        "/json",
        content='{"a":{"b":{"c":1}}}',
        headers={"content-type": "application/vnd.ace+json"},
    )

    assert response.status_code == 413
    assert response.json() == {"ok": False, "error": "JSON 请求结构超过安全上限"}


@pytest.mark.parametrize(
    ("payload", "limit"),
    [
        ('{"a":1,"b":2}', {"max_object_keys": 1}),
        ('{"text":"12345"}', {"max_string_bytes": 4}),
        ('{"items":[1,2,3]}', {"max_array_items": 2}),
        ('{"number":12345}', {"max_number_digits": 4}),
        ('{"a":1,"b":2}', {"max_nodes": 2}),
    ],
)
def test_http_json_structure_budget_rejects_each_resource_dimension(payload, limit):
    response = _json_budget_client(**limit).post(
        "/json", content=payload, headers={"content-type": "application/json"}
    )

    assert response.status_code == 413


def test_http_json_structure_budget_does_not_touch_form_multipart_or_websocket():
    client = _json_budget_client(max_depth=0, max_object_keys=0)

    assert client.post("/raw", data={"field": "value"}).status_code == 200
    assert client.post(
        "/raw", files={"file": ("note.txt", b"value", "text/plain")}
    ).status_code == 200

    with client.websocket_connect("/socket") as socket:
        assert socket.receive_text() == "ok"


@pytest.mark.parametrize(
    ("limits", "body"),
    [
        ({"max_object_keys": 1}, b'{"a":1,"b":2}'),
        ({"max_array_items": 1}, b'{"a":[1,2]}'),
        ({"max_string_bytes": 3}, b'{"a":"long"}'),
        ({"max_number_chars": 3}, b'{"a":1234}'),
        ({"max_nodes": 2}, b'{"a":[1]}'),
    ],
)
def test_http_json_structure_budget_caps_each_structural_dimension(limits, body):
    client = _json_budget_client(**limits)

    response = client.post(
        "/json",
        content=body,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json() == {"ok": False, "error": "JSON 请求结构超过安全上限"}


def test_json_structure_scanner_preserves_chunk_state_and_rejects_trailing_comma():
    scanner = JSONStructureBudget()
    body = b'{"a":1,"b":[true,null]}'
    for index in range(0, len(body), 2):
        scanner.feed(body[index : index + 2])
    scanner.finish()

    invalid = JSONStructureBudget()
    with pytest.raises(JSONStructureInvalid):
        invalid.feed(b'{"a":1,}')


def test_ws_rejects_unknown_fields_before_dispatch():
    client, _crew, connections = _client()

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"kind": "pong", "owner_account_id": "forged"})
        error = ws.receive_json()

    assert error["kind"] == "error"
    assert error["body"]["code"] == "PROTOCOL_INVALID"
    assert "owner_account_id" not in str(error)
    assert connections._conns == {}


def test_ws_requires_protocol_identity_on_every_frame() -> None:
    with pytest.raises(WebSocketProtocolError, match="PROTOCOL_INVALID"):
        validate_ws_message({"kind": "pong"})


def test_ws_rejects_duplicate_json_object_keys():
    client, _crew, _connections = _client()

    with client.websocket_connect("/ws") as ws:
        ws.send_text('{"kind":"pong","kind":"pong"}')
        error = ws.receive_json()

    assert error["kind"] == "error"
    assert error["body"]["code"] == "PROTOCOL_INVALID"


def test_ws_rejects_binary_frames():
    client, _crew, _connections = _client()

    with client.websocket_connect("/ws") as ws:
        ws.send_bytes(b'{"kind":"pong"}')
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()

    assert closed.value.code == 1003


def test_ws_rejects_oversized_frames_without_parsing_them():
    client, _crew, _connections = _client()

    with client.websocket_connect("/ws") as ws:
        ws.send_text('{"kind":"pong","padding":"' + ("x" * (1024 * 1024)) + '"}')
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()

    assert closed.value.code == 1009


def test_ws_rejects_oversized_session_lists_atomically():
    client, _crew, connections = _client()

    with client.websocket_connect("/ws") as ws:
        ws.send_json(
            {
                "action": "subscribe",
                "session_id": "s1",
                "sessions": [f"s-{index}" for index in range(101)],
            }
        )
        error = ws.receive_json()

    assert error["body"]["code"] == "PROTOCOL_INVALID"
    assert connections._conns == {}


def test_ws_rejects_internal_sidechain_subscription():
    client, _crew, connections = _client()

    with client.websocket_connect("/ws") as ws:
        ws.send_json(
            {
                "action": "subscribe",
                "session_id": "s1",
                "sessions": ["s1::turn::request-0001"],
            }
        )
        error = ws.receive_json()

    assert error["body"]["code"] == "PROTOCOL_INVALID"
    assert connections._conns == {}


def test_invalid_messages_consume_authenticated_socket_rate_budget(monkeypatch):
    monkeypatch.setattr(connections_module, "_MAX_INBOUND_PER_SOCKET", 2)
    client, _crew, _connections = _client()

    with client.websocket_connect("/ws") as ws:
        for _ in range(2):
            ws.send_json({"kind": "pong", "unknown": True})
            assert ws.receive_json()["body"]["code"] == "PROTOCOL_INVALID"
        ws.send_json({"kind": "pong", "unknown": True})
        assert ws.receive_json()["body"]["code"] == "RATE_LIMITED"
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()

    assert closed.value.code == 1008


def test_ws_rejects_replayed_request_id_across_reconnects():
    client, _crew, _connections = _client()
    payload = {
        "query": "hello",
        "session_id": "s1",
        "request_id": "request-replay-0001",
        "mode": "agent",
        "workspace_id": "default",
    }

    with client.websocket_connect("/ws") as ws:
        ws.send_json(
            {
                **payload,
                "protocol_version": 1,
                "client_sequence": 1,
                "nonce": "nonce-000000000101",
            }
        )
        assert ws.receive_json()["kind"] == "final"

    with client.websocket_connect("/ws") as ws:
        ws.send_json(
            {
                **payload,
                "protocol_version": 1,
                "client_sequence": 1,
                "nonce": "nonce-000000000102",
            }
        )
        error = ws.receive_json()

    assert error["kind"] == "error"
    assert error["body"]["code"] == "REPLAY_DETECTED"


def test_disconnect_freezes_authority_and_authenticated_reconnect_resumes_it():
    client, crew, _connections = _client()

    with client.websocket_connect("/ws") as ws:
        ws.send_json(
            {
                "query": "first",
                "session_id": "s1",
                "request_id": "request-lifecycle-0001",
                "mode": "agent",
                "workspace_id": "default",
                "protocol_version": 1,
                "client_sequence": 1,
                "nonce": "nonce-lifecycle-0001",
            }
        )
        assert ws.receive_json()["kind"] == "final"

    assert crew.security_service.events == [
        ("resume", "local", "s1"),
        ("freeze", "local", "s1"),
    ]

    with client.websocket_connect("/ws") as ws:
        ws.send_json(
            {
                "query": "second",
                "session_id": "s1",
                "request_id": "request-lifecycle-0002",
                "mode": "agent",
                "workspace_id": "default",
                "protocol_version": 1,
                "client_sequence": 1,
                "nonce": "nonce-lifecycle-0002",
            }
        )
        assert ws.receive_json()["kind"] == "final"

    assert crew.security_service.events[-2:] == [
        ("resume", "local", "s1"),
        ("freeze", "local", "s1"),
    ]


def test_ws_rejects_duplicate_or_out_of_order_client_sequence():
    client, _crew, _connections = _client()

    with client.websocket_connect("/ws") as ws:
        ws.send_json(
            {
                "kind": "pong",
                "protocol_version": 1,
                "client_sequence": 1,
                "nonce": "nonce-000000000001",
            }
        )
        ws.send_json(
            {
                "kind": "pong",
                "protocol_version": 1,
                "client_sequence": 1,
                "nonce": "nonce-000000000002",
            }
        )
        error = ws.receive_json()

    assert error["kind"] == "error"
    assert error["body"]["code"] == "SEQUENCE_INVALID"


def test_ws_rejects_client_sequence_gaps():
    client, _crew, _connections = _client()

    with client.websocket_connect("/ws") as ws:
        ws.send_json(
            {
                "kind": "pong",
                "protocol_version": 1,
                "client_sequence": 1,
                "nonce": "nonce-000000000011",
            }
        )
        ws.send_json(
            {
                "kind": "pong",
                "protocol_version": 1,
                "client_sequence": 3,
                "nonce": "nonce-000000000013",
            }
        )
        error = ws.receive_json()

    assert error["kind"] == "error"
    assert error["body"]["code"] == "SEQUENCE_INVALID"


def test_replay_table_capacity_fails_closed_without_evicting_live_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(connections_module, "_REPLAY_TABLE_LIMIT", 1)
    manager = ConnectionManager(min_interval=0)
    first_socket = object()
    second_socket = object()
    replay_socket = object()

    assert (
        manager.claim_inbound_identity(
            "local",
            first_socket,
            session_id="s1",
            request_id="request-0001",
            client_sequence=1,
            nonce="nonce-000000000001",
            now=1.0,
        )
        is None
    )
    assert (
        manager.claim_inbound_identity(
            "local",
            second_socket,
            session_id="s1",
            request_id="request-0002",
            client_sequence=1,
            nonce="nonce-000000000002",
            now=2.0,
        )
        == "REPLAY_STATE_EXHAUSTED"
    )
    assert (
        manager.claim_inbound_identity(
            "local",
            replay_socket,
            session_id="s1",
            request_id="request-0003",
            client_sequence=1,
            nonce="nonce-000000000001",
            now=3.0,
        )
        == "REPLAY_DETECTED"
    )


class _StreamRequest:
    def __init__(self, *, disconnect_after: int | None = None) -> None:
        self.calls = 0
        self.disconnect_after = disconnect_after

    async def is_disconnected(self) -> bool:
        self.calls += 1
        return self.disconnect_after is not None and self.calls >= self.disconnect_after


def _safe_stream_error(_reason: str) -> bytes:
    return b'{"type":"error","error":"stream_failed"}\n'


@pytest.mark.asyncio
async def test_bounded_stream_does_not_leak_upstream_fault_body() -> None:
    async def source():
        yield b"ok\n"
        raise RuntimeError("provider secret body")

    response = bounded_streaming_response(
        _StreamRequest(),
        source(),
        media_type="application/x-ndjson",
        limits=StreamLimits(max_chunk_bytes=128),
        error_event=_safe_stream_error,
    )

    body = b"".join([chunk async for chunk in response.body_iterator])

    assert body == b'ok\n{"type":"error","error":"stream_failed"}\n'
    assert b"provider secret body" not in body


@pytest.mark.asyncio
async def test_bounded_stream_fails_closed_on_idle_timeout() -> None:
    async def source():
        await asyncio.sleep(0.05)
        yield b"late\n"

    response = bounded_streaming_response(
        _StreamRequest(),
        source(),
        media_type="text/event-stream",
        limits=StreamLimits(idle_timeout_s=0.01),
        error_event=_safe_stream_error,
    )

    body = b"".join([chunk async for chunk in response.body_iterator])

    assert body == _safe_stream_error("timeout")


@pytest.mark.asyncio
async def test_bounded_stream_absolute_deadline_not_extended_by_keepalive() -> None:
    closed = False

    async def source():
        nonlocal closed
        try:
            while True:
                yield b"keep\n"
                await asyncio.sleep(0.005)
        finally:
            closed = True

    response = bounded_streaming_response(
        _StreamRequest(),
        source(),
        media_type="application/x-ndjson",
        limits=StreamLimits(
            idle_timeout_s=1.0,
            absolute_timeout_s=0.02,
            max_output_bytes=64 * 1024,
        ),
        error_event=_safe_stream_error,
    )

    body = b"".join([chunk async for chunk in response.body_iterator])

    assert body.endswith(_safe_stream_error("timeout"))
    assert closed is True


@pytest.mark.asyncio
async def test_bounded_stream_caps_each_chunk_and_total_output() -> None:
    async def source():
        yield b"1234"
        yield b"5678"
        yield b"90ab"

    response = bounded_streaming_response(
        _StreamRequest(),
        source(),
        media_type="application/x-ndjson",
        limits=StreamLimits(max_chunk_bytes=8, max_output_bytes=8),
    )

    body = b"".join([chunk async for chunk in response.body_iterator])

    assert body == b"12345678"


@pytest.mark.asyncio
async def test_bounded_stream_closes_source_after_client_disconnect() -> None:
    closed = False

    async def source():
        nonlocal closed
        try:
            yield b"first\n"
            yield b"second\n"
        finally:
            closed = True

    response = bounded_streaming_response(
        _StreamRequest(disconnect_after=2),
        source(),
        media_type="application/x-ndjson",
    )

    body = b"".join([chunk async for chunk in response.body_iterator])

    assert body == b"first\n"
    assert closed is True


@pytest.mark.asyncio
async def test_bounded_stream_times_out_slow_consumer_and_closes_source() -> None:
    closed = False

    async def source():
        nonlocal closed
        try:
            yield b"first\n"
        finally:
            closed = True

    response = bounded_streaming_response(
        _StreamRequest(),
        source(),
        media_type="application/x-ndjson",
        limits=StreamLimits(send_timeout_s=0.01),
    )

    async def slow_send(message):
        if message["type"] == "http.response.body" and message["more_body"]:
            await asyncio.sleep(0.05)

    with pytest.raises(asyncio.TimeoutError):
        await response.stream_response(slow_send)

    assert closed is True
