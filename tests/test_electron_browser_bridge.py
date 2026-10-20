"""Focused tests for the authenticated Electron browser-host broker."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi import WebSocketDisconnect

from crew.browser.electron_bridge import (
    ElectronBridgeCancelled,
    ElectronBridgeError,
    ElectronBrowserBridge,
)


class _Socket:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[Any] = asyncio.Queue()
        self.outgoing: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.accepted = asyncio.Event()
        self.closed: list[tuple[int, str]] = []

    async def accept(self) -> None:
        self.accepted.set()

    async def close(self, code: int, reason: str = "") -> None:
        self.closed.append((code, reason))

    async def send_json(self, value: dict[str, Any]) -> None:
        await self.outgoing.put(value)

    async def receive_json(self) -> Any:
        value = await self.incoming.get()
        if isinstance(value, BaseException):
            raise value
        return value


class _BlockingSendSocket(_Socket):
    def __init__(self) -> None:
        super().__init__()
        self.send_started = asyncio.Event()

    async def send_json(self, value: dict[str, Any]) -> None:
        del value
        self.send_started.set()
        await asyncio.Event().wait()


async def _connect(bridge: ElectronBrowserBridge, key: str) -> tuple[_Socket, asyncio.Task]:
    socket = _Socket()
    task = asyncio.create_task(bridge.serve(socket, key))  # type: ignore[arg-type]
    await socket.accepted.wait()
    return socket, task


@pytest.mark.asyncio
async def test_bridge_routes_response_only_to_bound_account() -> None:
    bridge = ElectronBrowserBridge()
    key = "crew_0123456789ab"
    socket, server = await _connect(bridge, key)

    request = asyncio.create_task(
        bridge.request(key, "execute", {"command": "snapshot"}, timeout=1)
    )
    outbound = await socket.outgoing.get()
    assert outbound["runtime_key"] == key
    assert outbound["method"] == "execute"
    assert outbound["params"] == {"command": "snapshot"}
    await socket.incoming.put(
        {"type": "response", "id": outbound["id"], "ok": True, "result": {"data": "ok"}}
    )
    assert await request == {"data": "ok"}

    with pytest.raises(ElectronBridgeError, match="尚未连接"):
        await bridge.request("crew_ffffffffffff", "execute", {}, timeout=0.1)

    await socket.incoming.put(WebSocketDisconnect())
    await server


@pytest.mark.asyncio
async def test_bridge_preserves_remote_uncertain_failure_flags() -> None:
    bridge = ElectronBrowserBridge()
    key = "crew_0123456789ab"
    socket, server = await _connect(bridge, key)

    request = asyncio.create_task(bridge.request(key, "execute", {}, timeout=1))
    outbound = await socket.outgoing.get()
    await socket.incoming.put(
        {
            "type": "response",
            "id": outbound["id"],
            "ok": False,
            "error": "动作结果未知",
            "uncertain": True,
            "partial": True,
            "completed_count": 2,
            "browser_stopped": False,
            "stop_unconfirmed": True,
        }
    )
    with pytest.raises(ElectronBridgeError, match="结果未知") as captured:
        await request
    assert captured.value.uncertain is True
    assert captured.value.partial is True
    assert captured.value.completed_count == 2
    assert captured.value.stop_unconfirmed is True

    await socket.incoming.put(WebSocketDisconnect())
    await server


@pytest.mark.asyncio
async def test_reconnect_fails_old_pending_calls_and_replaces_socket() -> None:
    bridge = ElectronBrowserBridge()
    key = "crew_0123456789ab"
    first, first_server = await _connect(bridge, key)
    pending = asyncio.create_task(bridge.request(key, "execute", {}, timeout=1))
    await first.outgoing.get()

    second, second_server = await _connect(bridge, key)
    with pytest.raises(ElectronBridgeError, match="重新连接"):
        await pending
    assert first.closed == [(1012, "browser-host-replaced")]

    await first.incoming.put(WebSocketDisconnect())
    await first_server
    await second.incoming.put(WebSocketDisconnect())
    await second_server


@pytest.mark.asyncio
async def test_no_host_is_retryable_without_stopping_browser_lifecycle() -> None:
    bridge = ElectronBrowserBridge()

    with pytest.raises(ElectronBridgeError, match="尚未连接") as captured:
        await bridge.request(
            "crew_0123456789ab",
            "execute",
            {"command": "tab"},
            timeout=0.1,
            mutating=True,
        )

    assert captured.value.retryable is True
    assert captured.value.request_sent is False
    assert captured.value.uncertain is False
    assert captured.value.browser_stopped is False
    assert captured.value.stop_unconfirmed is False


@pytest.mark.asyncio
async def test_sent_mutation_disconnect_is_uncertain_and_fail_stopped() -> None:
    bridge = ElectronBrowserBridge()
    key = "crew_0123456789ab"
    socket, server = await _connect(bridge, key)
    pending = asyncio.create_task(
        bridge.request(key, "execute", {}, timeout=1, mutating=True)
    )
    await socket.outgoing.get()

    await socket.incoming.put(WebSocketDisconnect())
    await server
    with pytest.raises(ElectronBridgeError, match="断开") as captured:
        await pending
    assert captured.value.request_sent is True
    assert captured.value.uncertain is True
    assert captured.value.stop_unconfirmed is True
    assert captured.value.retryable is False


@pytest.mark.asyncio
async def test_readonly_request_fails_retryable_without_cross_epoch_replay() -> None:
    bridge = ElectronBrowserBridge()
    key = "crew_0123456789ab"
    first, first_server = await _connect(bridge, key)
    request = asyncio.create_task(
        bridge.request(
            key,
            "execute",
            {"command": "snapshot"},
            timeout=1,
            retry_readonly=True,
        )
    )
    await first.outgoing.get()

    second, second_server = await _connect(bridge, key)
    with pytest.raises(ElectronBridgeError, match="重新连接") as captured:
        await request
    assert captured.value.retryable is True
    assert captured.value.uncertain is False
    assert second.outgoing.empty()

    await first.incoming.put(WebSocketDisconnect())
    await first_server
    await second.incoming.put(WebSocketDisconnect())
    await second_server


@pytest.mark.asyncio
async def test_sent_mutation_defers_cancellation_until_remote_completion() -> None:
    bridge = ElectronBrowserBridge()
    key = "crew_0123456789ab"
    socket, server = await _connect(bridge, key)
    request = asyncio.create_task(
        bridge.request(key, "execute", {}, timeout=1, mutating=True)
    )
    outbound = await socket.outgoing.get()

    request.cancel()
    await asyncio.sleep(0)
    assert not request.done()
    request.cancel()
    await asyncio.sleep(0)
    assert not request.done()
    await socket.incoming.put(
        {"type": "response", "id": outbound["id"], "ok": True, "result": {"ok": True}}
    )
    with pytest.raises(asyncio.CancelledError):
        await request

    await socket.incoming.put(WebSocketDisconnect())
    await server


@pytest.mark.asyncio
async def test_sent_mutation_remote_error_does_not_consume_user_cancellation() -> None:
    bridge = ElectronBrowserBridge()
    key = "crew_0123456789ab"
    socket, server = await _connect(bridge, key)
    request = asyncio.create_task(
        bridge.request(key, "execute", {}, timeout=1, mutating=True)
    )
    outbound = await socket.outgoing.get()

    request.cancel()
    await asyncio.sleep(0)
    assert not request.done()
    await socket.incoming.put(
        {
            "type": "response",
            "id": outbound["id"],
            "ok": False,
            "error": "账号浏览器已停止",
            "browser_stopped": True,
            "stop_unconfirmed": False,
        }
    )
    with pytest.raises(ElectronBridgeCancelled, match="已停止") as captured:
        await request
    assert captured.value.browser_stopped is True
    assert captured.value.stop_unconfirmed is False

    await socket.incoming.put(WebSocketDisconnect())
    await server


@pytest.mark.asyncio
async def test_sent_mutation_blocked_send_is_bounded_by_rpc_deadline() -> None:
    bridge = ElectronBrowserBridge()
    key = "crew_0123456789ab"
    socket = _BlockingSendSocket()
    server = asyncio.create_task(bridge.serve(socket, key))  # type: ignore[arg-type]
    await socket.accepted.wait()

    request = asyncio.create_task(
        bridge.request(key, "execute", {}, timeout=0.1, mutating=True)
    )
    await socket.send_started.wait()
    request.cancel()

    with pytest.raises(ElectronBridgeCancelled) as captured:
        await asyncio.wait_for(request, timeout=0.5)
    assert captured.value.uncertain is True
    assert captured.value.stop_unconfirmed is True

    await socket.incoming.put(WebSocketDisconnect())
    await server


@pytest.mark.asyncio
async def test_registration_callback_can_reset_host_before_regular_requests() -> None:
    bridge = ElectronBrowserBridge()
    key = "crew_0123456789ab"
    socket = _Socket()

    async def registered() -> None:
        await bridge.request(
            key,
            "close_owner",
            {},
            timeout=1,
            mutating=True,
            _allow_unready=True,
        )

    server = asyncio.create_task(
        bridge.serve(socket, key, on_registered=registered)  # type: ignore[arg-type]
    )
    await socket.accepted.wait()
    regular = asyncio.create_task(
        bridge.request(key, "execute", {"command": "snapshot"}, timeout=1)
    )

    reset = await socket.outgoing.get()
    assert reset["method"] == "close_owner"
    assert socket.outgoing.empty()
    await socket.incoming.put(
        {"type": "response", "id": reset["id"], "ok": True, "result": {"closed": True}}
    )
    outbound = await asyncio.wait_for(socket.outgoing.get(), timeout=0.5)
    assert outbound["method"] == "execute"
    await socket.incoming.put(
        {"type": "response", "id": outbound["id"], "ok": True, "result": {"ok": True}}
    )
    assert await regular == {"ok": True}

    await socket.incoming.put(WebSocketDisconnect())
    await server


@pytest.mark.asyncio
async def test_bridge_forwards_complete_supported_debug_events() -> None:
    bridge = ElectronBrowserBridge()
    key = "crew_0123456789ab"
    events: list[dict[str, Any]] = []
    socket = _Socket()
    server = asyncio.create_task(
        bridge.serve(  # type: ignore[arg-type]
            socket,
            key,
            on_event=lambda event: events.append(event),
        )
    )
    await socket.accepted.wait()
    await socket.incoming.put(
        {
            "type": "event",
            "event": {
                "type": "debug",
                "channel": "console",
                "targetId": "target-1",
                "record": {"text": "ok"},
            },
        }
    )
    await socket.incoming.put(
        {
            "type": "event",
            "event": {
                "type": "debug",
                "channel": "cookies",
                "targetId": "target-1",
                "record": {"text": "ignored"},
            },
        }
    )
    await socket.incoming.put(
        {
            "type": "event",
            "event": {
                "type": "debug",
                "channel": "network",
                "targetId": "target-1",
                "record": {"text": "x" * 9000},
            },
        }
    )
    await asyncio.sleep(0)

    assert events == [
        {
            "type": "debug",
            "channel": "console",
            "targetId": "target-1",
            "record": {"text": "ok"},
        },
        {
            "type": "debug",
            "channel": "network",
            "targetId": "target-1",
            "record": {"text": "x" * 9000},
        },
    ]

    await socket.incoming.put(WebSocketDisconnect())
    await server


@pytest.mark.asyncio
async def test_bridge_forwards_complete_native_download_lifecycle_events() -> None:
    bridge = ElectronBrowserBridge()
    key = "crew_0123456789ab"
    events: list[dict[str, Any]] = []
    socket = _Socket()
    server = asyncio.create_task(
        bridge.serve(  # type: ignore[arg-type]
            socket,
            key,
            on_event=lambda event: events.append(event),
        )
    )
    await socket.accepted.wait()
    base = {
        "type": "download",
        "runtimeKey": key,
        "downloadId": "download-1",
        "targetId": "target-popup",
        "sessionHash": "a" * 32,
        "path": "/tmp/downloads/browser/report (1).csv",
        "name": "report (1).csv",
        "suggestedFilename": "report.csv",
        "url": "https://example.com/report.csv",
        "receivedBytes": 0,
        "totalBytes": 42,
        "createdAt": 1_700_000_000_000,
        "completedAt": 0,
        "error": "",
    }
    await socket.incoming.put(
        {
            "type": "event",
            "event": {**base, "state": "progressing"},
        }
    )
    await socket.incoming.put(
        {
            "type": "event",
            "event": {
                **base,
                "state": "completed",
                "receivedBytes": 42,
                "completedAt": 1_700_000_000_100,
            },
        }
    )
    # Unknown fields and invalid counters must reject the complete frame; the
    # bridge cannot manufacture a partial download record.
    await socket.incoming.put(
        {
            "type": "event",
            "event": {**base, "state": "completed", "unexpected": True},
        }
    )
    await socket.incoming.put(
        {
            "type": "event",
            "event": {**base, "state": "completed", "receivedBytes": -1},
        }
    )
    await asyncio.sleep(0)

    assert events == [
        {
            key_: value
            for key_, value in {**base, "state": "progressing"}.items()
            if key_ != "runtimeKey"
        },
        {
            key_: value
            for key_, value in {
                **base,
                "state": "completed",
                "receivedBytes": 42,
                "completedAt": 1_700_000_000_100,
            }.items()
            if key_ != "runtimeKey"
        },
    ]

    await socket.incoming.put(WebSocketDisconnect())
    await server


    bridge = ElectronBrowserBridge()
    assert bridge.connected("../../other-account") is False
