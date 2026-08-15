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


class _RawSocket(_Socket):
    async def receive(self) -> Any:
        value = await self.incoming.get()
        if isinstance(value, BaseException):
            raise value
        return value


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


@pytest.mark.asyncio
async def test_bridge_bounds_recording_events_and_strips_secrets() -> None:
    """录制事件走字段白名单，且密码/验证码的值在这一层再清一次。

    这是纵深防御的第三道：注入脚本已经不带值（`secret`/`handoff` 档的值根本不出
    页面进程），宿主侧 `parseRecorderEvent` 又清了一次。这里还要清，是因为轨迹
    文件会被写进磁盘、之后交给 LLM 编译成技能——它是最后一道能拦住的地方。
    """
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
                "type": "recording",
                "targetId": "target-1",
                "label": "s0-1",
                "action": "input",
                "step": 3,
                "url": "https://example.com/login",
                "hint": "input 密码",
                "tier": "secret",
                "value": "hunter2",
                "backendNodeId": 7,
                "target": {"tag": "input", "text": "", "href": "", "ordinal": 1},
                # 宿主未来可能加字段；白名单之外的一律不放行
                "unexpected": {"nested": "payload"},
            },
        }
    )
    # targetId 缺失一律丢弃
    await socket.incoming.put(
        {"type": "event", "event": {"type": "recording", "action": "click"}}
    )
    await asyncio.sleep(0)

    assert len(events) == 1
    recorded = events[0]
    assert recorded["value"] == ""
    assert recorded["tier"] == "secret"
    assert "unexpected" not in recorded
    assert recorded["action"] == "input"
    assert recorded["step"] == 3
    # 敏感步骤的定位面整体归零；只清文本而保留 selector/cssPath 仍可泄漏值。
    assert recorded["target"] is None
    # 页面态字段：这一步没带就是空串，且 page_dropped 明确为 False
    assert recorded["page"] == ""
    assert recorded["page_dropped"] is False
    assert recorded["pageTruncated"] is False
    assert recorded["selector"] == ""
    assert recorded["schemaVersion"] == 1

    # 宿主因帧超限摘掉页面快照时要能区分「没变化」与「丢了」
    await socket.incoming.put(
        {
            "type": "event",
            "event": {
                "type": "recording", "targetId": "target-1", "action": "click",
                "step": 4, "page": "", "page_dropped": True,
            },
        }
    )
    await asyncio.sleep(0)
    assert events[-1]["page_dropped"] is True

    await socket.incoming.put(WebSocketDisconnect())
    await server


def test_bridge_v3_recording_schema_preserves_bounded_replay_evidence() -> None:
    """v3 只保留已声明字段，且普通 URL/href 必须可无损回放。"""
    event = ElectronBrowserBridge._bounded_recording_event(
        {
            "schemaVersion": 3,
            "type": "recording",
            "targetId": "target-1",
            "recordingId": "A1B2C3D4E5F60708",
            "label": "s0123-1",
            "action": "click",
            "step": 9,
            "url": "https://example.com/items?id=7&token=secret-token",
            "hint": "a 详情",
            "tier": "plain",
            "selector": "internal:role=link[name=\"详情\"i]" + "x" * 5000,
            "page": "P" * 40_000,
            # 当前 Host 旧实现发字符串，新实现发 bool；bridge 归一成一个 bool 契约。
            "pageTruncated": "true",
            "target": {
                "tag": "A",
                "text": "详情",
                "ariaLabel": "详情",
                "href": "/items?id=7&sig=deadbeef",
                "ordinal": 2,
                "id": "detailsLink",
                "name": "details",
                "role": "LINK",
                "inputType": "",
                "testId": "item-details",
                "testIdAttribute": "data-testid",
                "cssPath": "#list > li:nth-of-type(2) > a:nth-of-type(1)",
                "framePath": ["#outer-frame", "#inner-frame"],
                "contentEditable": False,
                "unknownTargetField": "drop-me",
            },
            "provenance": {
                "schemaVersion": 1,
                "source": "document-world",
                "capturePhase": "event-callback",
                "browserTrusted": True,
                "targetEvidence": "synchronous",
                "nativeInput": "correlated",
                "unknown": "drop-me",
            },
            "unknownTopLevel": "drop-me",
        }
    )
    assert event is not None
    assert event["schemaVersion"] == 3
    assert event["recordingId"] == "a1b2c3d4e5f60708"
    assert len(event["selector"]) == 4096
    assert len(event["page"]) == 30_000
    assert event["pageTruncated"] is True
    assert event["url"] == "https://example.com/items?id=7&token=secret-token"
    assert event["target"] == {
        "tag": "a",
        "text": "详情",
        "ariaLabel": "详情",
        "href": "/items?id=7&sig=deadbeef",
        "ordinal": 2,
        "id": "detailsLink",
        "name": "details",
        "role": "link",
        "inputType": "",
        "testId": "item-details",
        "testIdAttribute": "data-testid",
        "cssPath": "#list > li:nth-of-type(2) > a:nth-of-type(1)",
        "framePath": ["#outer-frame", "#inner-frame"],
        "contentEditable": False,
    }
    assert event["provenance"] == {
        "schemaVersion": 1,
        "source": "document-world",
        "capturePhase": "event-callback",
        "browserTrusted": True,
        "targetEvidence": "synchronous",
        "nativeInput": "correlated",
        "transport": "authenticated-electron-host",
    }
    assert "unknownTopLevel" not in event
    assert "drop-me" not in json.dumps(event, ensure_ascii=False)

    # 未知 schema 仍整条拒绝；原生输入关联现在是非阻断审计信号，避免丢 IME/粘贴。
    assert ElectronBrowserBridge._bounded_recording_event(
        {
            "schemaVersion": 2,
            "type": "recording",
            "targetId": "target-1",
            "action": "click",
        }
    ) is None
    unverified = {
        "schemaVersion": 3,
        "type": "recording",
        "targetId": "target-1",
        "action": "click",
        "tier": "plain",
        "target": None,
        "provenance": {
            "schemaVersion": 1,
            "source": "document-world",
            "capturePhase": "event-callback",
            "browserTrusted": True,
            "targetEvidence": "none",
            "nativeInput": "unverified",
        },
    }
    bounded_unverified = ElectronBrowserBridge._bounded_recording_event(unverified)
    assert bounded_unverified is not None
    assert bounded_unverified["provenance"]["nativeInput"] == "unverified"

    long_input = {
        **unverified,
        "action": "input",
        "value": "值" * 4_097,
        "valueTruncated": False,
    }
    bounded_long_input = ElectronBrowserBridge._bounded_recording_event(long_input)
    assert bounded_long_input is not None
    assert len(bounded_long_input["value"]) == 4_096
    assert bounded_long_input["valueTruncated"] is True


def test_bridge_v4_preserves_exact_pointer_semantics() -> None:
    raw = {
        "schemaVersion": 4,
        "type": "recording",
        "targetId": "target-1",
        "recordingId": "abcdef0123456789",
        "action": "click",
        "tier": "plain",
        "selector": "internal:testid=[data-testid=\"open-menu\"s]",
        "clickButton": "middle",
        "clickCount": 1,
        "modifiers": ["Meta", "Shift"],
        "target": None,
        "provenance": {
            "schemaVersion": 1,
            "source": "document-world",
            "capturePhase": "event-callback",
            "browserTrusted": True,
            "targetEvidence": "none",
            "nativeInput": "unverified",
        },
    }
    event = ElectronBrowserBridge._bounded_recording_event(raw)
    assert event is not None
    assert event["schemaVersion"] == 4
    assert event["clickButton"] == "middle"
    assert event["clickCount"] == 1
    assert event["modifiers"] == ["Meta", "Shift"]

    for patch in (
        {"clickButton": "primary"},
        {"clickCount": 0},
        {"modifiers": ["Meta", "Meta"]},
        {"modifiers": [{"key": "Meta"}]},
    ):
        assert ElectronBrowserBridge._bounded_recording_event(
            {**raw, **patch}
        ) is None


def test_bridge_v8_preserves_canvas_click_position_and_v7_compatibility() -> None:
    common = {
        "type": "recording",
        "targetId": "target-1",
        "recordingId": "abcdef0123456789",
        "action": "click",
        "tier": "plain",
        "selector": "#chart",
        "clickButton": "left",
        "clickCount": 1,
        "modifiers": [],
        "values": [],
        "target": None,
        "uploadMode": "",
        "paths": [],
        "fileCount": 0,
        "multiple": False,
        "accept": "",
        "provenance": {
            "schemaVersion": 1,
            "source": "document-world",
            "capturePhase": "event-callback",
            "browserTrusted": True,
            "targetEvidence": "none",
            "nativeInput": "unverified",
        },
    }
    event = ElectronBrowserBridge._bounded_recording_event(
        {
            **common,
            "schemaVersion": 8,
            "position": {"x": 127.5, "y": 42.25},
        }
    )
    assert event is not None
    assert event["schemaVersion"] == 8
    assert event["position"] == {"x": 127.5, "y": 42.25}

    legacy_v7 = ElectronBrowserBridge._bounded_recording_event(
        {**common, "schemaVersion": 7}
    )
    assert legacy_v7 is not None
    assert "position" not in legacy_v7

    for position in (
        {"x": -1, "y": 2},
        {"x": 1},
        {"x": float("nan"), "y": 2},
        [1, 2],
    ):
        assert (
            ElectronBrowserBridge._bounded_recording_event(
                {**common, "schemaVersion": 8, "position": position}
            )
            is None
        )


def test_bridge_v9_preserves_strict_causal_identity_and_empty_prompt() -> None:
    common = {
        "schemaVersion": 9,
        "type": "recording",
        "targetId": "target-1",
        "recordingId": "abcdef0123456789",
        "label": "page",
        "action": "click",
        "tier": "plain",
        "selector": "#open",
        "clickButton": "left",
        "clickCount": 1,
        "position": None,
        "modifiers": [],
        "values": [],
        "target": None,
        "uploadMode": "",
        "paths": [],
        "fileCount": 0,
        "multiple": False,
        "accept": "",
        "dialogAction": "",
        "dialogType": "",
        "dialogText": "",
        "causalId": 42,
        "provenance": {
            "schemaVersion": 1,
            "source": "document-world",
            "capturePhase": "event-callback",
            "browserTrusted": True,
            "targetEvidence": "none",
            "nativeInput": "unverified",
        },
    }
    event = ElectronBrowserBridge._bounded_recording_event(common)
    assert event is not None
    assert event["causalId"] == 42

    prompt = {
        **common,
        "action": "dialog",
        "selector": "",
        "clickButton": "",
        "clickCount": 0,
        "causalId": 42,
        "dialogAction": "accept",
        "dialogType": "prompt",
        "dialogText": "",
        "provenance": {
            "schemaVersion": 1,
            "source": "browser-host",
            "capturePhase": "host",
            "browserTrusted": False,
            "targetEvidence": "none",
            "nativeInput": "host",
        },
    }
    prompt_event = ElectronBrowserBridge._bounded_recording_event(prompt)
    assert prompt_event is not None
    assert prompt_event["dialogText"] == ""
    assert prompt_event["causalId"] == 42

    for invalid in (None, "42", 1.5, -1, 9_007_199_254_740_992, True):
        assert ElectronBrowserBridge._bounded_recording_event(
            {**common, "causalId": invalid}
        ) is None


def test_bridge_v10_preserves_recording_local_popup_topology() -> None:
    base = {
        "schemaVersion": 10,
        "type": "recording",
        "targetId": "target-popup",
        "recordingId": "abcdef0123456789",
        "label": "p2",
        "openerPage": "p1",
        "popupOrdinal": 1,
        "createdByCausalId": 42,
        "action": "navigate",
        "tier": "plain",
        "selector": "",
        "clickButton": "",
        "clickCount": 0,
        "position": None,
        "modifiers": [],
        "values": [],
        "target": None,
        "uploadMode": "",
        "paths": [],
        "fileCount": 0,
        "multiple": False,
        "accept": "",
        "dialogAction": "",
        "dialogType": "",
        "dialogText": "",
        "causalId": 42,
        "provenance": {
            "schemaVersion": 1,
            "source": "host-navigation",
            "capturePhase": "host",
            "browserTrusted": False,
            "targetEvidence": "none",
            "nativeInput": "host",
        },
    }
    event = ElectronBrowserBridge._bounded_recording_event(base)
    assert event is not None
    assert event["label"] == "p2"
    assert event["openerPage"] == "p1"
    assert event["popupOrdinal"] == 1
    assert event["createdByCausalId"] == 42

    main = ElectronBrowserBridge._bounded_recording_event(
        {
            **base,
            "targetId": "target-main",
            "label": "p1",
            "openerPage": "",
            "popupOrdinal": 0,
            "createdByCausalId": 0,
            "causalId": 0,
        }
    )
    assert main is not None
    assert main["openerPage"] == ""

    for patch in (
        {"label": "native-target-id"},
        {"openerPage": "p1", "popupOrdinal": 0},
        {"openerPage": "", "popupOrdinal": 1},
        {"openerPage": "", "createdByCausalId": 42},
        {"popupOrdinal": 1.5},
        {"createdByCausalId": "42"},
    ):
        assert ElectronBrowserBridge._bounded_recording_event({**base, **patch}) is None


def test_bridge_v10_preserves_unbounded_secret_and_handoff_replay_evidence() -> None:
    long_value = "密码与 OTP 精确值🚀\n" * 10_000
    long_text = "页面与目标证据" * 20_000
    url = (
        "https://example.com/login?token=exact-token&ticket=ST-"
        + "x" * 100_000
        + "#/callback?code=exact-code"
    )
    selector = (
        'internal:role=textbox[name="' + long_text + '"i] >> nth=0'
    )
    target_selector = (
        'internal:testid=[data-testid="field with spaces 🚀"s]'
    )
    frame_path = [
        f'internal:testid=[data-testid="frame {index} 🚀"s]'
        for index in range(100)
    ]
    target = {
        "tag": "INPUT",
        "text": long_text,
        "ariaLabel": long_text,
        "href": url,
        "ordinal": 2_000_000,
        "id": "字段 id 含空格、引号\"与🚀",
        "name": "login field name",
        "role": "TEXTBOX",
        "inputType": "PASSWORD",
        "testId": "field with spaces 🚀",
        "testIdAttribute": "data-testid",
        "cssPath": "#app >> internal:role=textbox",
        "framePath": frame_path,
        "contentEditable": False,
    }
    common = {
        "schemaVersion": 10,
        "type": "recording",
        "targetId": "target-main",
        "recordingId": "abcdef0123456789",
        "label": "p1",
        "openerPage": "",
        "popupOrdinal": 0,
        "createdByCausalId": 0,
        "action": "input",
        "step": 2_000_000,
        "url": url,
        "hint": long_text,
        "tier": "secret",
        "value": long_value,
        "values": [],
        "valueTruncated": False,
        "key": "",
        "page": long_text,
        "selector": selector,
        "targetSelector": target_selector,
        "pageTruncated": False,
        "page_dropped": False,
        "target": target,
        "dragTarget": None,
        "scrollX": 2_000_000,
        "scrollY": -2_000_000,
        "clickButton": "",
        "clickCount": 0,
        "position": None,
        "modifiers": [],
        "uploadMode": "",
        "paths": [],
        "fileCount": 0,
        "multiple": False,
        "accept": "",
        "dialogAction": "",
        "dialogType": "",
        "dialogText": "",
        "causalId": 77,
        "backendNodeId": 7,
        "timestamp": 9_000_000_000_000_000,
        "provenance": {
            "schemaVersion": 1,
            "source": "document-world",
            "capturePhase": "event-callback",
            "browserTrusted": True,
            "targetEvidence": "synchronous",
            "nativeInput": "correlated",
        },
        "unknown": "drop-me",
    }

    for tier in ("secret", "handoff"):
        event = ElectronBrowserBridge._bounded_recording_event(
            {**common, "tier": tier}
        )
        assert event is not None
        assert event["tier"] == tier
        assert event["value"] == long_value
        assert event["valueTruncated"] is False
        assert event["url"] == url
        assert event["hint"] == long_text
        assert event["page"] == long_text
        assert event["selector"] == selector
        assert event["targetSelector"] == target_selector
        assert event["target"] == {
            **target,
            "tag": "input",
            "role": "textbox",
            "inputType": "password",
        }
        assert event["step"] == 2_000_000
        assert event["scrollX"] == 2_000_000
        assert event["scrollY"] == -2_000_000
        assert event["provenance"]["targetEvidence"] == "synchronous"
        assert "unknown" not in event


def test_bridge_v10_preserves_many_select_values_and_upload_paths() -> None:
    values = [f"选项-{index}" for index in range(5_000)]
    values.append("超长选项🚀" * 20_000)
    target = {
        "tag": "select",
        "text": "",
        "ariaLabel": "成员",
        "href": "",
        "ordinal": 1,
        "id": "members",
        "name": "members",
        "role": "",
        "inputType": "select-multiple",
        "testId": "",
        "testIdAttribute": "",
        "cssPath": "#members",
        "framePath": [],
        "contentEditable": False,
    }
    common = {
        "schemaVersion": 10,
        "type": "recording",
        "targetId": "target-main",
        "recordingId": "abcdef0123456789",
        "label": "p1",
        "openerPage": "",
        "popupOrdinal": 0,
        "createdByCausalId": 0,
        "action": "input",
        "tier": "plain",
        "value": values[0],
        "values": values,
        "valueTruncated": False,
        "target": target,
        "clickButton": "",
        "clickCount": 0,
        "position": None,
        "modifiers": [],
        "uploadMode": "",
        "paths": [],
        "fileCount": 0,
        "multiple": False,
        "accept": "",
        "dialogAction": "",
        "dialogType": "",
        "dialogText": "",
        "causalId": 1,
        "provenance": {
            "schemaVersion": 1,
            "source": "document-world",
            "capturePhase": "event-callback",
            "browserTrusted": True,
            "targetEvidence": "synchronous",
            "nativeInput": "correlated",
        },
    }
    selected = ElectronBrowserBridge._bounded_recording_event(common)
    assert selected is not None
    assert selected["values"] == values
    assert selected["value"] == values[0]

    paths = [f"/tmp/批量目录/文件-{index}.bin" for index in range(1_000)]
    paths[0] = "/tmp/" + "超长目录🚀" * 10_000 + "/文件.bin"
    accept = ".custom-" + "x" * 100_000
    upload = ElectronBrowserBridge._bounded_recording_event(
        {
            **common,
            "action": "upload",
            "value": "",
            "values": [],
            "target": {
                **target,
                "tag": "input",
                "inputType": "file",
                "id": "directory",
                "name": "directory",
                "cssPath": "#directory",
            },
            "uploadMode": "paths",
            "paths": paths,
            "fileCount": len(paths),
            "multiple": True,
            "accept": accept,
        }
    )
    assert upload is not None
    assert upload["paths"] == paths
    assert upload["fileCount"] == len(paths)
    assert upload["accept"] == accept


def test_bridge_v5_preserves_complete_upload_or_exact_empty_surface() -> None:
    raw = {
        "schemaVersion": 5,
        "type": "recording",
        "targetId": "target-1",
        "recordingId": "abcdef0123456789",
        "action": "upload",
        "tier": "plain",
        "selector": "internal:testid=[data-testid=\"attachment\"s]",
        "clickButton": "",
        "clickCount": 0,
        "modifiers": [],
        "uploadMode": "paths",
        "paths": ["/tmp/report.pdf"],
        "fileCount": 1,
        "multiple": False,
        "accept": ".pdf,application/pdf",
        "target": {
            "tag": "input",
            "text": "",
            "ariaLabel": "Attachment",
            "href": "",
            "ordinal": 1,
            "id": "attachment",
            "name": "attachment",
            "role": "",
            "inputType": "file",
            "testId": "attachment",
            "testIdAttribute": "data-testid",
            "cssPath": "#attachment",
            "framePath": [],
            "contentEditable": False,
        },
        "provenance": {
            "schemaVersion": 1,
            "source": "document-world",
            "capturePhase": "event-callback",
            "browserTrusted": True,
            "targetEvidence": "synchronous",
            "nativeInput": "correlated",
        },
    }
    event = ElectronBrowserBridge._bounded_recording_event(raw)
    assert event is not None
    assert event["schemaVersion"] == 5
    assert event["uploadMode"] == "paths"
    assert event["paths"] == ["/tmp/report.pdf"]
    assert event["fileCount"] == 1
    assert event["multiple"] is False
    assert event["accept"] == ".pdf,application/pdf"

    click = ElectronBrowserBridge._bounded_recording_event(
        {
            **raw,
            "action": "click",
            "clickButton": "left",
            "clickCount": 1,
            "uploadMode": "",
            "paths": [],
            "fileCount": 0,
            "multiple": False,
            "accept": "",
        }
    )
    assert click is not None
    assert click["uploadMode"] == ""
    assert click["paths"] == []

    for patch in (
        {"paths": [], "fileCount": 1},
        {"uploadMode": "handoff", "paths": ["/tmp/report.pdf"]},
        {"uploadMode": "clear", "fileCount": 1},
        {"multiple": "false"},
    ):
        assert ElectronBrowserBridge._bounded_recording_event(
            {**raw, **patch}
        ) is None


def test_bridge_v3_contenteditable_proof_is_exact_and_typed() -> None:
    raw = {
        "schemaVersion": 3,
        "type": "recording",
        "targetId": "target-1",
        "recordingId": "abcdef0123456789",
        "action": "input",
        "tier": "plain",
        "target": {
            "tag": "div",
            "text": "",
            "ariaLabel": "",
            "href": "",
            "ordinal": 1,
            "id": "editor",
            "name": "comment",
            "role": "",
            "inputType": "",
            "testId": "",
            "testIdAttribute": "",
            "cssPath": "#editor",
            "framePath": [],
            "contentEditable": True,
        },
        "provenance": {
            "schemaVersion": 1,
            "source": "isolated-world",
            "capturePhase": "event-callback",
            "browserTrusted": True,
            "targetEvidence": "synchronous",
            "nativeInput": "correlated",
        },
    }
    event = ElectronBrowserBridge._bounded_recording_event(raw)
    assert event is not None
    assert event["schemaVersion"] == 3
    assert event["target"]["contentEditable"] is True

    for invalid in (None, "true", 1):
        candidate = json.loads(json.dumps(raw))
        if invalid is None:
            candidate["target"].pop("contentEditable")
        else:
            candidate["target"]["contentEditable"] = invalid
        assert ElectronBrowserBridge._bounded_recording_event(candidate) is None


def test_bridge_adversarial_sensitive_sentinel_has_zero_selector_target_page_leakage() -> None:
    """secret/handoff 的页面可控面全部归零，不能靠换字段把 sentinel 带进 JSONL。"""
    sentinel = "S3NTINEL-private-90817"
    for tier in ("secret", "handoff"):
        event = ElectronBrowserBridge._bounded_recording_event(
            {
                "schemaVersion": 3,
                "type": "recording",
                "targetId": "target-1",
                "recordingId": "abcdef0123456789",
                "action": "input",
                "tier": tier,
                "url": f"https://example.com/{sentinel}?token={sentinel}",
                "hint": sentinel,
                "value": sentinel,
                "key": sentinel,
                "selector": f"internal:text={sentinel}",
                "page": f"page::{sentinel}",
                "pageTruncated": True,
                "target": {
                    "tag": sentinel,
                    "text": sentinel,
                    "ariaLabel": sentinel,
                    "href": f"/{sentinel}?token={sentinel}",
                    "ordinal": 1,
                    "id": sentinel,
                    "name": sentinel,
                    "role": sentinel,
                    "inputType": sentinel,
                    "testId": sentinel,
                    "testIdAttribute": "data-testid",
                    "cssPath": f"#{sentinel}",
                    "framePath": [f"#{sentinel}"],
                },
                "provenance": {
                    "schemaVersion": 1,
                    "source": "isolated-world",
                    "capturePhase": "event-callback",
                    "browserTrusted": True,
                    "targetEvidence": "redacted",
                    "nativeInput": "correlated",
                    "unknownSentinel": sentinel,
                },
                "unknownSentinel": sentinel,
            }
        )
        assert event is not None
        serialized = json.dumps(event, ensure_ascii=False)
        assert sentinel not in serialized
        assert event["selector"] == ""
        assert event["target"] is None
        assert event["page"] == ""
        assert event["pageTruncated"] is False
        assert event["url"] == ""
        assert event["value"] == ""
        assert event["key"] == ""
        assert event["hint"] == f"<{tier} field>"


@pytest.mark.asyncio
async def test_bridge_rejects_oversized_host_frame_before_json_dispatch() -> None:
    bridge = ElectronBrowserBridge()
    socket = _RawSocket()
    server = asyncio.create_task(bridge.serve(socket, "crew_0123456789ab"))  # type: ignore[arg-type]
    await socket.accepted.wait()
    await socket.incoming.put({
        "type": "websocket.receive",
        "text": "{" + "x" * (4 * 1024 * 1024) + "}",
    })
    await server
    assert socket.closed == [(1009, "invalid-browser-host-frame")]


def test_bridge_rejects_non_account_runtime_keys() -> None:
    bridge = ElectronBrowserBridge()
    assert bridge.connected("../../other-account") is False
