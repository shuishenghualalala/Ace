"""Authenticated RPC broker between the gateway and the Electron browser host.

The Electron main process connects *to* the gateway and registers itself as the
browser host for one authenticated account.  Browser tools never receive the
host socket and the renderer never receives a CDP endpoint.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect


RECORDING_V11_PHASE_A_ENV = "CREW_BROWSER_RECORDING_V11_PHASE_A"
_V11_PAGE_GUID_RE = re.compile(r"^p(?:0|[1-9][0-9]*)$")
_V11_RECORDING_ID_RE = re.compile(r"^[0-9a-fA-F]{8,32}$")
_V11_JS_SAFE_INTEGER_MAX = 9_007_199_254_740_991
_HOST_WS_MAX_FRAME_BYTES = 4 * 1024 * 1024


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate object key")
        result[key] = value
    return result
_V11_MODIFIERS = ("Alt", "Control", "Meta", "Shift")
_V11_TARGET_FIELDS = frozenset(
    {
        "tag",
        "text",
        "ariaLabel",
        "href",
        "ordinal",
        "id",
        "name",
        "role",
        "inputType",
        "contentEditable",
        "testId",
        "testIdAttribute",
        "cssPath",
        "framePath",
    }
)
_V11_EVIDENCE_FIELDS = frozenset(
    {
        "url",
        "hint",
        "tier",
        "target",
        "dragTarget",
        "snapshot",
        "snapshotDropped",
        "backendNodeId",
    }
)
_V11_BASE_FIELDS = frozenset(
    {
        "schemaVersion",
        "type",
        "targetId",
        "recordingId",
        "step",
        "eventIndex",
        "transactionId",
        "transactionKind",
        "recordKind",
        "pageGuid",
        "timestamp",
        "provenance",
    }
)
_V11_PROVENANCE_FIELDS = frozenset(
    {
        "schemaVersion",
        "source",
        "capturePhase",
        "browserTrusted",
        "targetEvidence",
        "nativeInput",
    }
)
_V11_PERSISTED_PROVENANCE_FIELDS = (
    _V11_PROVENANCE_FIELDS | {"transport"}
)


def recording_v11_phase_a_enabled() -> bool:
    """Return whether v11 contracts are enabled (explicit ``0`` is rollback)."""

    return os.environ.get(RECORDING_V11_PHASE_A_ENV) != "0"


def _v11_safe_int(value: Any, *, minimum: int = 0) -> int | None:
    if (
        type(value) is not int
        or value < minimum
        or value > _V11_JS_SAFE_INTEGER_MAX
    ):
        return None
    return value


def _v11_point(value: Any) -> dict[str, float] | None | bool:
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"x", "y"}
        or any(
            type(value.get(axis)) not in {int, float}
            or not math.isfinite(float(value[axis]))
            or float(value[axis]) < 0
            for axis in ("x", "y")
        )
    ):
        return False
    return {"x": float(value["x"]), "y": float(value["y"])}


def _v11_finite_point(value: Any) -> dict[str, float] | bool:
    if (
        not isinstance(value, dict)
        or set(value) != {"x", "y"}
        or any(
            type(value.get(axis)) not in {int, float}
            or not math.isfinite(float(value[axis]))
            for axis in ("x", "y")
        )
    ):
        return False
    return {"x": float(value["x"]), "y": float(value["y"])}


_V11_POINTER_TELEMETRY_RANGES = {
    "pressure": (0.0, 1.0),
    "tangentialPressure": (-1.0, 1.0),
    "tiltX": (-90.0, 90.0),
    "tiltY": (-90.0, 90.0),
    "twist": (0.0, 359.0),
    "width": (0.0, math.inf),
    "height": (0.0, math.inf),
}


def _v11_pointer_sample(
    value: Any,
    *,
    elapsed: bool,
) -> dict[str, float] | bool:
    required = {"x", "y"} | ({"elapsedMs"} if elapsed else set())
    allowed = required | set(_V11_POINTER_TELEMETRY_RANGES)
    if (
        not isinstance(value, dict)
        or not required.issubset(value)
        or not set(value).issubset(allowed)
        or any(
            type(value.get(axis)) not in {int, float}
            or not math.isfinite(float(value[axis]))
            for axis in required
        )
    ):
        return False
    clean = {
        "x": float(value["x"]),
        "y": float(value["y"]),
    }
    if elapsed:
        clean["elapsedMs"] = float(value["elapsedMs"])
    for name, (minimum, maximum) in _V11_POINTER_TELEMETRY_RANGES.items():
        if name not in value:
            continue
        raw = value[name]
        if (
            type(raw) not in {int, float}
            or not math.isfinite(float(raw))
            or not minimum <= float(raw) <= maximum
        ):
            return False
        clean[name] = float(raw)
    return clean


def _v11_viewport(value: Any) -> dict[str, float] | bool:
    if (
        not isinstance(value, dict)
        or set(value) != {"width", "height"}
        or any(
            type(value.get(dimension)) not in {int, float}
            or not math.isfinite(float(value[dimension]))
            for dimension in ("width", "height")
        )
    ):
        return False
    return {
        "width": float(value["width"]),
        "height": float(value["height"]),
    }


def _normalize_v11_target(value: Any) -> dict[str, Any] | None | bool:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _V11_TARGET_FIELDS:
        return False
    text_fields = (
        "tag",
        "text",
        "ariaLabel",
        "href",
        "id",
        "name",
        "role",
        "inputType",
        "testId",
        "testIdAttribute",
        "cssPath",
    )
    if (
        any(not isinstance(value.get(name), str) for name in text_fields)
        or type(value.get("contentEditable")) is not bool
        or _v11_safe_int(value.get("ordinal")) is None
        or not isinstance(value.get("framePath"), list)
        or any(
            not isinstance(fragment, str) or not fragment
            for fragment in value["framePath"]
        )
    ):
        return False
    return {
        **{name: value[name] for name in text_fields},
        "ordinal": int(value["ordinal"]),
        "contentEditable": bool(value["contentEditable"]),
        "framePath": list(value["framePath"]),
    }


def _normalize_v11_evidence(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != _V11_EVIDENCE_FIELDS:
        return None
    if (
        any(
            not isinstance(value.get(name), str)
            for name in ("url", "hint", "snapshot")
        )
        or value.get("tier")
        not in {"plain", "identifier", "secret", "handoff"}
        or type(value.get("snapshotDropped")) is not bool
        or _v11_safe_int(value.get("backendNodeId")) is None
    ):
        return None
    target = _normalize_v11_target(value.get("target"))
    drag_target = _normalize_v11_target(value.get("dragTarget"))
    if target is False or drag_target is False:
        return None
    return {
        "url": value["url"],
        "hint": value["hint"],
        "tier": value["tier"],
        "target": target,
        "dragTarget": drag_target,
        "snapshot": value["snapshot"],
        "snapshotDropped": value["snapshotDropped"],
        "backendNodeId": int(value["backendNodeId"]),
    }


def _normalize_v11_modifiers(value: Any) -> list[str] | None:
    if (
        not isinstance(value, list)
        or any(
            not isinstance(modifier, str)
            or modifier not in _V11_MODIFIERS
            for modifier in value
        )
        or len(set(value)) != len(value)
    ):
        return None
    return [modifier for modifier in _V11_MODIFIERS if modifier in value]


def _normalize_v11_action(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    name = value.get("name")
    if name == "click":
        if set(value) != {
            "name",
            "selector",
            "button",
            "modifiers",
            "clickCount",
            "position",
        }:
            return None
        modifiers = _normalize_v11_modifiers(value.get("modifiers"))
        position = _v11_point(value.get("position"))
        if (
            not isinstance(value.get("selector"), str)
            or not value["selector"]
            or value.get("button") not in {"left", "middle", "right"}
            or _v11_safe_int(value.get("clickCount"), minimum=1) is None
            or modifiers is None
            or position is False
        ):
            return None
        return {
            "name": "click",
            "selector": value["selector"],
            "button": value["button"],
            "modifiers": modifiers,
            "clickCount": int(value["clickCount"]),
            "position": position,
        }
    if name == "hover":
        if set(value) != {"name", "selector", "position"}:
            return None
        position = _v11_point(value.get("position"))
        if (
            not isinstance(value.get("selector"), str)
            or not value["selector"]
            or position is False
        ):
            return None
        return {
            "name": "hover",
            "selector": value["selector"],
            "position": position,
        }
    if name == "fill":
        if (
            set(value) != {"name", "selector", "text"}
            or not isinstance(value.get("selector"), str)
            or not value["selector"]
            or not isinstance(value.get("text"), str)
        ):
            return None
        return {
            "name": "fill",
            "selector": value["selector"],
            "text": value["text"],
        }
    if name in {"check", "uncheck"}:
        if (
            set(value) != {"name", "selector"}
            or not isinstance(value.get("selector"), str)
            or not value["selector"]
        ):
            return None
        return {"name": name, "selector": value["selector"]}
    if name == "select":
        if (
            set(value) != {"name", "selector", "options"}
            or not isinstance(value.get("selector"), str)
            or not value["selector"]
            or not isinstance(value.get("options"), list)
            or any(not isinstance(option, str) for option in value["options"])
        ):
            return None
        return {
            "name": "select",
            "selector": value["selector"],
            "options": list(value["options"]),
        }
    if name == "press":
        if set(value) != {"name", "selector", "key", "modifiers"}:
            return None
        modifiers = _normalize_v11_modifiers(value.get("modifiers"))
        if (
            not isinstance(value.get("selector"), str)
            or not isinstance(value.get("key"), str)
            or not value["key"]
            or modifiers is None
        ):
            return None
        return {
            "name": "press",
            "selector": value["selector"],
            "key": value["key"],
            "modifiers": modifiers,
        }
    if name == "setInputFiles":
        if (
            set(value) != {"name", "selector", "files"}
            or not isinstance(value.get("selector"), str)
            or not value["selector"]
            or not isinstance(value.get("files"), list)
            or any(not isinstance(path, str) or not path for path in value["files"])
        ):
            return None
        return {
            "name": "setInputFiles",
            "selector": value["selector"],
            "files": list(value["files"]),
        }
    if name == "x-crew-drop":
        data = value.get("data")
        if (
            set(value) != {"name", "selector", "files", "data"}
            or not isinstance(value.get("selector"), str)
            or not value["selector"]
            or not isinstance(value.get("files"), list)
            or any(
                not isinstance(path, str) or not path
                for path in value["files"]
            )
            or not isinstance(data, dict)
            or any(
                not isinstance(mime, str) or not isinstance(payload, str)
                for mime, payload in data.items()
            )
        ):
            return None
        return {
            "name": name,
            "selector": value["selector"],
            "files": list(value["files"]),
            "data": dict(data),
        }
    if name == "navigate":
        if (
            set(value) != {"name", "url"}
            or not isinstance(value.get("url"), str)
            or not value["url"]
        ):
            return None
        return {"name": name, "url": value["url"]}
    if name == "openPage":
        if (
            set(value) not in (
                {"name", "url"},
                {"name", "url", "viewport"},
            )
            or not isinstance(value.get("url"), str)
            or not value["url"]
        ):
            return None
        viewport = (
            _v11_viewport(value.get("viewport"))
            if "viewport" in value
            else None
        )
        if viewport is False:
            return None
        return {
            "name": name,
            "url": value["url"],
            **({"viewport": viewport} if viewport is not None else {}),
        }
    if name in {"closePage", "x-crew-activatePage"}:
        return {"name": name} if set(value) == {"name"} else None
    if name == "x-crew-navigate":
        if (
            set(value) != {"name", "operation", "url"}
            or value.get("operation")
            not in {"goto", "back", "forward", "reload"}
            or not isinstance(value.get("url"), str)
            or value["operation"] == "goto"
            and not value["url"]
            or value["operation"] != "goto"
            and value["url"]
        ):
            return None
        return {
            "name": name,
            "operation": value["operation"],
            "url": value["url"],
        }
    if name == "x-crew-resize":
        if (
            set(value) != {"name", "width", "height"}
            or any(
                type(value.get(dimension)) not in {int, float}
                or not math.isfinite(float(value[dimension]))
                for dimension in ("width", "height")
            )
        ):
            return None
        return {
            "name": name,
            "width": float(value["width"]),
            "height": float(value["height"]),
        }
    if name == "x-crew-drag":
        if (
            set(value)
            != {
                "name",
                "sourceSelector",
                "targetSelector",
                "sourcePosition",
                "targetPosition",
            }
            or not isinstance(value.get("sourceSelector"), str)
            or not value["sourceSelector"]
            or not isinstance(value.get("targetSelector"), str)
            or not value["targetSelector"]
        ):
            return None
        source_position = _v11_point(value.get("sourcePosition"))
        target_position = _v11_point(value.get("targetPosition"))
        if source_position is False or target_position is False:
            return None
        return {
            "name": name,
            "sourceSelector": value["sourceSelector"],
            "targetSelector": value["targetSelector"],
            "sourcePosition": source_position,
            "targetPosition": target_position,
        }
    if name == "x-crew-pointerGesture":
        has_pointer_type = "pointerType" in value
        if (
            set(value)
            != {
                "name",
                "selector",
                "button",
                "modifiers",
                "start",
                "points",
            } | ({"pointerType"} if has_pointer_type else set())
            or not isinstance(value.get("selector"), str)
            or not value["selector"]
            or value.get("button") not in {"left", "middle", "right"}
            or has_pointer_type
            and value.get("pointerType") not in {"mouse", "pen", "touch"}
            or value.get("pointerType") == "touch"
            and value.get("button") != "left"
        ):
            return None
        modifiers = _normalize_v11_modifiers(value.get("modifiers"))
        start = _v11_pointer_sample(value.get("start"), elapsed=False)
        raw_points = value.get("points")
        if (
            modifiers is None
            or start is False
            or not isinstance(raw_points, list)
            or not raw_points
        ):
            return None
        points: list[dict[str, float]] = []
        previous_elapsed_ms = 0.0
        for raw_point in raw_points:
            point = _v11_pointer_sample(raw_point, elapsed=True)
            if point is False or point["elapsedMs"] < previous_elapsed_ms:
                return None
            previous_elapsed_ms = point["elapsedMs"]
            points.append(point)
        return {
            "name": name,
            "selector": value["selector"],
            "button": value["button"],
            "modifiers": modifiers,
            "start": start,
            "points": points,
            **(
                {"pointerType": value["pointerType"]}
                if has_pointer_type
                else {}
            ),
        }
    if name == "x-crew-scroll":
        if (
            set(value) != {"name", "selector", "deltaX", "deltaY"}
            or not isinstance(value.get("selector"), str)
            or type(value.get("deltaX")) is not int
            or type(value.get("deltaY")) is not int
            or _v11_safe_int(abs(value["deltaX"])) is None
            or _v11_safe_int(abs(value["deltaY"])) is None
            or value["deltaX"] == value["deltaY"] == 0
        ):
            return None
        return {
            "name": name,
            "selector": value["selector"],
            "deltaX": value["deltaX"],
            "deltaY": value["deltaY"],
        }
    return None


def _normalize_v11_signal(
    value: Any,
    details: Any,
    *,
    page_guid: str,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    if not isinstance(value, dict) or not isinstance(details, dict):
        return None
    name = value.get("name")
    if name == "navigation":
        if (
            set(value) != {"name", "url"}
            or not isinstance(value.get("url"), str)
            or not value["url"]
            or details
        ):
            return None
        return ({"name": name, "url": value["url"]}, {})
    if name == "popup":
        if (
            set(value) != {"name", "popupPageGuid"}
            or not isinstance(value.get("popupPageGuid"), str)
            or _V11_PAGE_GUID_RE.fullmatch(value["popupPageGuid"]) is None
            or set(details)
            != {"openerPageGuid", "popupIndex", "disposition", "activate"}
            or details.get("openerPageGuid") != page_guid
            or _v11_safe_int(details.get("popupIndex"), minimum=1) is None
            or not isinstance(details.get("disposition"), str)
            or not details["disposition"]
            or type(details.get("activate")) is not bool
        ):
            return None
        return (
            {"name": name, "popupPageGuid": value["popupPageGuid"]},
            {
                "openerPageGuid": page_guid,
                "popupIndex": int(details["popupIndex"]),
                "disposition": details["disposition"],
                "activate": details["activate"],
            },
        )
    if name == "download":
        if (
            set(value) != {"name", "downloadAlias"}
            or not isinstance(value.get("downloadAlias"), str)
            or not value["downloadAlias"]
            or set(details) != {"ordinal", "suggestedFilename"}
            or _v11_safe_int(details.get("ordinal"), minimum=1) is None
            or not isinstance(details.get("suggestedFilename"), str)
        ):
            return None
        return (
            {"name": name, "downloadAlias": value["downloadAlias"]},
            {
                "ordinal": int(details["ordinal"]),
                "suggestedFilename": details["suggestedFilename"],
            },
        )
    if name == "dialog":
        if (
            set(value) != {"name", "dialogAlias"}
            or not isinstance(value.get("dialogAlias"), str)
            or not value["dialogAlias"]
            or set(details) != {"type", "action", "promptText"}
            or details.get("type")
            not in {"alert", "confirm", "prompt", "beforeunload"}
            or details.get("action") not in {"accept", "dismiss"}
            or not isinstance(details.get("promptText"), str)
            or details["type"] != "prompt"
            and details["promptText"]
            or details["action"] == "dismiss"
            and details["promptText"]
        ):
            return None
        return (
            {"name": name, "dialogAlias": value["dialogAlias"]},
            {
                "type": details["type"],
                "action": details["action"],
                "promptText": details["promptText"],
            },
        )
    if name == "x-crew-pageClosed":
        if (
            set(value) != {"name", "closedPageGuid", "reason"}
            or value.get("closedPageGuid") != page_guid
            or not isinstance(value.get("reason"), str)
            or details
        ):
            return None
        return (
            {
                "name": name,
                "closedPageGuid": page_guid,
                "reason": value["reason"],
            },
            {},
        )
    return None


def _normalize_v11_provenance(
    value: Any,
    *,
    persisted: bool,
) -> dict[str, Any] | None:
    expected_fields = (
        _V11_PERSISTED_PROVENANCE_FIELDS
        if persisted
        else _V11_PROVENANCE_FIELDS
    )
    if (
        not isinstance(value, dict)
        or set(value) != expected_fields
        or value.get("schemaVersion") != 1
        or value.get("source")
        not in {
            "document-world",
            "isolated-world",
            "legacy-isolated-world",
            "host-navigation",
            "browser-host",
        }
        or value.get("capturePhase") not in {"event-callback", "host"}
        or type(value.get("browserTrusted")) is not bool
        or value.get("targetEvidence") not in {"synchronous", "redacted", "none"}
        or value.get("nativeInput")
        not in {"unverified", "correlated", "host", "legacy-host-checked"}
        or persisted
        and value.get("transport") != "authenticated-electron-host"
    ):
        return None
    return {
        "schemaVersion": 1,
        "source": value["source"],
        "capturePhase": value["capturePhase"],
        "browserTrusted": value["browserTrusted"],
        "targetEvidence": value["targetEvidence"],
        "nativeInput": value["nativeInput"],
        "transport": "authenticated-electron-host",
    }


def normalize_recording_event_v11(
    value: Any,
    *,
    persisted: bool = False,
) -> dict[str, Any] | None:
    """Validate and canonicalize one feature-gated v11 append-only trace row.

    Phase A intentionally exposes no Host writer or replay executor.  Keeping
    the gate here means an accidental v11 Host rollout cannot silently feed a
    manager that still executes only replay.v2.
    """

    if not recording_v11_phase_a_enabled() or not isinstance(value, dict):
        return None
    record_kind = value.get("recordKind")
    expected_fields = (
        _V11_BASE_FIELDS | {"action", "evidence"}
        if record_kind == "action"
        else _V11_BASE_FIELDS | {"signal", "details"}
        if record_kind == "signal"
        else frozenset()
    )
    target_id = value.get("targetId")
    recording_id = value.get("recordingId")
    page_guid = value.get("pageGuid")
    step = _v11_safe_int(value.get("step"), minimum=1)
    event_index = _v11_safe_int(value.get("eventIndex"), minimum=1)
    transaction_id = _v11_safe_int(value.get("transactionId"), minimum=1)
    timestamp = _v11_safe_int(value.get("timestamp"))
    provenance = _normalize_v11_provenance(
        value.get("provenance"),
        persisted=persisted,
    )
    if (
        not expected_fields
        or set(value) != expected_fields
        or value.get("schemaVersion") != 11
        or value.get("type") != "recording"
        or not isinstance(target_id, str)
        or not target_id
        or not isinstance(recording_id, str)
        or _V11_RECORDING_ID_RE.fullmatch(recording_id) is None
        or not isinstance(page_guid, str)
        or _V11_PAGE_GUID_RE.fullmatch(page_guid) is None
        or step is None
        or event_index is None
        or transaction_id is None
        or timestamp is None
        or value.get("transactionKind") not in {"action", "observation"}
        or record_kind == "action"
        and value.get("transactionKind") != "action"
        or provenance is None
    ):
        return None
    event: dict[str, Any] = {
        "schemaVersion": 11,
        "type": "recording",
        "targetId": target_id,
        "recordingId": recording_id.lower(),
        "step": step,
        "eventIndex": event_index,
        "transactionId": transaction_id,
        "transactionKind": value["transactionKind"],
        "recordKind": record_kind,
        "pageGuid": page_guid,
        "timestamp": timestamp,
        "provenance": provenance,
    }
    if record_kind == "action":
        action = _normalize_v11_action(value.get("action"))
        evidence = _normalize_v11_evidence(value.get("evidence"))
        if action is None or evidence is None:
            return None
        event["action"] = action
        event["evidence"] = evidence
    else:
        normalized_signal = _normalize_v11_signal(
            value.get("signal"),
            value.get("details"),
            page_guid=page_guid,
        )
        if normalized_signal is None:
            return None
        event["signal"], event["details"] = normalized_signal
    return event


class ElectronBridgeError(RuntimeError):
    """A browser-host transport or remote execution failure."""

    def __init__(
        self,
        message: str,
        *,
        uncertain: bool = False,
        browser_stopped: bool = False,
        stop_unconfirmed: bool = False,
        retryable: bool = False,
        request_sent: bool = False,
        connection_generation: int = 0,
        remote_terminal: bool = False,
        code: str = "",
        phase: str = "",
        partial: bool = False,
        completed_count: int = 0,
    ) -> None:
        super().__init__(message)
        # Host 的稳定错误码（如 stale_ref_security），供上层区分可恢复失败。
        self.code = code
        self.phase = phase
        self.partial = partial
        self.completed_count = max(0, int(completed_count))
        self.uncertain = uncertain
        self.browser_stopped = browser_stopped
        self.stop_unconfirmed = stop_unconfirmed
        # True 表示这些标志来自 Host 的确定答复（我们知道那次动作的终局），
        # False 表示我们从未拿到答复（超时/失联，只能自己判定）。
        self.remote_terminal = remote_terminal
        # Transport-only metadata. BrowserManager intentionally sees only the
        # lifecycle flags above; the bridge uses these fields for one bounded,
        # read-only retry after an actual Host epoch change.
        self.retryable = retryable
        self.request_sent = request_sent
        self.connection_generation = connection_generation


class ElectronBridgeCancelled(asyncio.CancelledError):
    """Cancellation that waited for a sent mutation's remote terminal result."""

    def __init__(self, error: ElectronBridgeError) -> None:
        super().__init__(str(error))
        self.uncertain = error.uncertain
        self.browser_stopped = error.browser_stopped
        # Host 给出了确定的终局结果 → 采信它（我们知道那次动作到底怎么了）。
        # 没拿到答复（超时/失联）→ 这次已发出的 mutation 是否执行过永远无法确认，
        # fail-stop。不能继承超时错误的 stop_unconfirmed=False——那是为「纯超时不该
        # 锁 Profile」设的，而这里叠加了「调用方放弃了一个已发出的 mutation」。
        self.stop_unconfirmed = (
            error.stop_unconfirmed if error.remote_terminal else True
        )


@dataclass
class _PendingRequest:
    future: asyncio.Future[dict[str, Any]]
    mutating: bool
    sent: bool = False


@dataclass
class _AttemptState:
    sent: bool = False


@dataclass
class _HostConnection:
    socket: WebSocket
    generation: int
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: dict[str, _PendingRequest] = field(default_factory=dict)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    initialization_error: ElectronBridgeError | None = None
    connected_at: float = field(default_factory=time.monotonic)


RegistrationCallback = Callable[[], Awaitable[None] | None]
EventCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


class ElectronBrowserBridge:
    """Route one account's browser RPC calls to its connected Electron host."""

    def __init__(self) -> None:
        self._connections: dict[str, _HostConnection] = {}
        self._generations: dict[str, int] = {}

    @staticmethod
    def _runtime_key(value: str) -> str:
        key = str(value or "").strip()
        if re.fullmatch(r"crew_[0-9a-f]{12}", key) is None:
            raise ElectronBridgeError("无效的桌面浏览器账号标识")
        return key

    def connected(self, runtime_key: str | None = None) -> bool:
        if runtime_key is None:
            return any(
                connection.ready.is_set() and connection.initialization_error is None
                for connection in self._connections.values()
            )
        try:
            key = self._runtime_key(runtime_key)
        except ElectronBridgeError:
            return False
        connection = self._connections.get(key)
        return bool(
            connection is not None
            and connection.ready.is_set()
            and connection.initialization_error is None
        )

    @property
    def connected_count(self) -> int:
        return sum(
            connection.ready.is_set() and connection.initialization_error is None
            for connection in self._connections.values()
        )

    async def serve(
        self,
        socket: WebSocket,
        runtime_key: str,
        *,
        on_registered: RegistrationCallback | None = None,
        on_event: EventCallback | None = None,
    ) -> None:
        """Attach one authenticated main-process WebSocket until disconnect.

        A re-registered Host is not made available to ordinary tool calls until
        ``on_registered`` has completed the account epoch reset.  The callback
        may issue requests on this socket with ``_allow_unready=True``.
        """

        key = self._runtime_key(runtime_key)
        await socket.accept()
        generation = self._generations.get(key, 0) + 1
        self._generations[key] = generation
        connection = _HostConnection(socket, generation)
        previous = self._connections.get(key)
        self._connections[key] = connection
        if previous is not None:
            self._fail_pending(previous, "桌面浏览器宿主已重新连接")
            try:
                await previous.socket.close(code=1012, reason="browser-host-replaced")
            except Exception:
                pass

        registration_task: asyncio.Task[None] | None = None

        async def initialize() -> None:
            try:
                if on_registered is not None:
                    result = on_registered()
                    if inspect.isawaitable(result):
                        await result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                connection.initialization_error = ElectronBridgeError(
                    f"桌面浏览器宿主初始化失败：{exc}",
                    retryable=True,
                    connection_generation=generation,
                )
            finally:
                connection.ready.set()
            if connection.initialization_error is not None:
                try:
                    await socket.close(code=1011, reason="browser-host-initialization-failed")
                except Exception:
                    pass

        if on_registered is None:
            connection.ready.set()
        else:
            # The receive loop must already be running while initialization
            # sends the idempotent close_owner RPC on this same socket.
            registration_task = asyncio.create_task(initialize())

        try:
            while True:
                message = await self._receive_host_frame(socket)
                if not isinstance(message, dict):
                    continue
                if message.get("type") == "event":
                    event = self._bounded_host_event(message.get("event"))
                    if event is not None and on_event is not None:
                        try:
                            result = on_event(event)
                            if inspect.isawaitable(result):
                                await result
                        except Exception:
                            # Host events are observational. A malformed event
                            # consumer must never tear down the control channel.
                            pass
                    continue
                if message.get("type") != "response":
                    continue
                request_id = str(message.get("id") or "")
                pending = connection.pending.pop(request_id, None)
                if pending is None or pending.future.done():
                    continue
                pending.future.set_result(message)
        except WebSocketDisconnect:
            pass
        finally:
            if self._connections.get(key) is connection:
                self._connections.pop(key, None)
            self._fail_pending(connection, "桌面浏览器宿主已断开")
            if registration_task is not None and not registration_task.done():
                registration_task.cancel()
                await asyncio.gather(registration_task, return_exceptions=True)

    @staticmethod
    async def _receive_host_frame(socket: WebSocket) -> dict[str, Any]:
        """Bound and validate the wire frame before JSON parsing."""
        receive = getattr(socket, "receive", None)
        if not callable(receive):
            message = await socket.receive_json()
            if not isinstance(message, dict):
                raise WebSocketDisconnect(code=1003, reason="invalid-browser-host-frame")
            return message
        event = await receive()
        if event.get("type") == "websocket.disconnect":
            raise WebSocketDisconnect(
                code=int(event.get("code") or 1000),
                reason="",
            )
        if event.get("type") != "websocket.receive":
            raise WebSocketDisconnect(code=1003, reason="invalid-browser-host-frame")
        if event.get("bytes") is not None:
            with suppress(Exception):
                await socket.close(code=1003, reason="binary-browser-host-frame")
            raise WebSocketDisconnect(code=1003, reason="binary-browser-host-frame")
        text = event.get("text")
        if not isinstance(text, str):
            with suppress(Exception):
                await socket.close(code=1003, reason="invalid-browser-host-frame")
            raise WebSocketDisconnect(code=1003, reason="invalid-browser-host-frame")
        try:
            if len(text.encode("utf-8")) > _HOST_WS_MAX_FRAME_BYTES:
                raise ValueError("frame-too-large")
            message = json.loads(
                text,
                object_pairs_hook=lambda pairs: _strict_json_object(pairs),
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("constant")),
            )
        except (UnicodeEncodeError, json.JSONDecodeError, ValueError, RecursionError):
            with suppress(Exception):
                await socket.close(code=1009, reason="invalid-browser-host-frame")
            raise WebSocketDisconnect(code=1009, reason="invalid-browser-host-frame")
        if not isinstance(message, dict):
            with suppress(Exception):
                await socket.close(code=1003, reason="invalid-browser-host-frame")
            raise WebSocketDisconnect(code=1003, reason="invalid-browser-host-frame")
        return message

    @classmethod
    def _bounded_host_event(cls, value: Any) -> dict[str, Any] | None:
        """按信封判别位分发宿主事件。未知类型一律丢弃，不做修补。"""
        if not isinstance(value, dict):
            return None
        if value.get("type") == "recording":
            return cls._bounded_recording_event(value)
        if value.get("type") == "download":
            return cls._bounded_download_event(value)
        return cls._bounded_debug_event(value)

    # 录制事件使用显式版本化白名单。缺省版本 1 只用于滚动升级兼容；一旦宿主声明
    # schemaVersion，就必须是我们完整理解的版本，未知版本整条拒绝，不能“尽量保留”。
    _RECORDING_SCHEMA_VERSION = 10
    _RECORDING_PROVENANCE_VERSION = 1
    _JS_SAFE_INTEGER_MAX = 9_007_199_254_740_991
    _RECORDING_ALLOWED_FIELDS = {
        1: frozenset(
            {
                "type", "targetId", "label", "action", "url", "hint", "tier", "value",
                "key", "page", "selector", "pageTruncated", "page_dropped", "target",
                "step", "scrollX", "scrollY", "backendNodeId", "timestamp",
            }
        ),
        3: frozenset(
            {
                "schemaVersion", "type", "targetId", "recordingId", "label", "action",
                "url", "hint", "tier", "value", "valueTruncated", "key", "page", "selector",
                "targetSelector", "pageTruncated", "page_dropped", "target", "dragTarget",
                "step", "scrollX", "scrollY",
                "backendNodeId", "timestamp", "provenance",
            }
        ),
        4: frozenset(
            {
                "schemaVersion", "type", "targetId", "recordingId", "label", "action",
                "url", "hint", "tier", "value", "valueTruncated", "key", "page", "selector",
                "targetSelector", "pageTruncated", "page_dropped", "target", "dragTarget",
                "step", "scrollX", "scrollY", "clickButton", "clickCount", "modifiers",
                "backendNodeId", "timestamp", "provenance",
            }
        ),
        5: frozenset(
            {
                "schemaVersion", "type", "targetId", "recordingId", "label", "action",
                "url", "hint", "tier", "value", "valueTruncated", "key", "page", "selector",
                "targetSelector", "pageTruncated", "page_dropped", "target", "dragTarget",
                "step", "scrollX", "scrollY", "clickButton", "clickCount", "modifiers",
                "uploadMode", "paths", "fileCount", "multiple", "accept",
                "backendNodeId", "timestamp", "provenance",
            }
        ),
        6: frozenset(
            {
                "schemaVersion", "type", "targetId", "recordingId", "label", "action",
                "url", "hint", "tier", "value", "values", "valueTruncated", "key",
                "page", "selector", "targetSelector", "pageTruncated", "page_dropped",
                "target", "dragTarget", "step", "scrollX", "scrollY", "clickButton",
                "clickCount", "modifiers", "uploadMode", "paths", "fileCount",
                "multiple", "accept", "backendNodeId", "timestamp", "provenance",
            }
        ),
        7: frozenset(
            {
                "schemaVersion", "type", "targetId", "recordingId", "label", "action",
                "url", "hint", "tier", "value", "values", "valueTruncated", "key",
                "page", "selector", "targetSelector", "pageTruncated", "page_dropped",
                "target", "dragTarget", "step", "scrollX", "scrollY", "clickButton",
                "clickCount", "modifiers", "uploadMode", "paths", "fileCount",
                "multiple", "accept", "backendNodeId", "timestamp", "provenance",
            }
        ),
        8: frozenset(
            {
                "schemaVersion", "type", "targetId", "recordingId", "label", "action",
                "url", "hint", "tier", "value", "values", "valueTruncated", "key",
                "page", "selector", "targetSelector", "pageTruncated", "page_dropped",
                "target", "dragTarget", "step", "scrollX", "scrollY", "clickButton",
                "clickCount", "position", "modifiers", "uploadMode", "paths", "fileCount",
                "multiple", "accept", "backendNodeId", "timestamp", "provenance",
            }
        ),
        9: frozenset(
            {
                "schemaVersion", "type", "targetId", "recordingId", "label", "action",
                "url", "hint", "tier", "value", "values", "valueTruncated", "key", "causalId",
                "page", "selector", "targetSelector", "pageTruncated", "page_dropped",
                "target", "dragTarget", "step", "scrollX", "scrollY", "clickButton",
                "clickCount", "position", "modifiers", "uploadMode", "paths", "fileCount",
                "multiple", "accept", "dialogAction", "dialogType", "dialogText",
                "backendNodeId", "timestamp", "provenance",
            }
        ),
        10: frozenset(
            {
                "schemaVersion", "type", "targetId", "recordingId", "label", "action",
                "url", "hint", "tier", "value", "values", "valueTruncated", "key", "causalId",
                "page", "selector", "targetSelector", "pageTruncated", "page_dropped",
                "target", "dragTarget", "step", "scrollX", "scrollY", "clickButton",
                "clickCount", "position", "modifiers", "uploadMode", "paths", "fileCount",
                "multiple", "accept", "dialogAction", "dialogType", "dialogText",
                "backendNodeId", "timestamp", "provenance",
                "openerPage", "popupOrdinal", "createdByCausalId",
            }
        ),
    }
    # Historical v1-v9 limits. v10 iterates the same field set but passes
    # ``limit=None`` so the bridge cannot truncate replay evidence.
    _LEGACY_RECORDING_TEXT_LIMITS = {
        "label": 256,
        "action": 32,
        "url": 16_384,
        "hint": 200,
        "tier": 16,
        "value": 4_096,
        "key": 40,
        "dialogAction": 8,
        "dialogType": 20,
        "dialogText": 10_000,
        # 页面态快照。只在页面内容变化的那一步携带，其余步骤为空串。
        # 上限与宿主的 MAX_TEXT 一致。
        "page": 30_000,
        # Playwright codegen 生成的稳定 selector。它来自宿主而非页面直传，但仍设
        # 独立上限，防止版本 bug 把整段页面内容塞进 selector。
        "selector": 4096,
        "targetSelector": 4096,
    }
    # 宿主因帧超限而摘掉页面快照时置位。编译期看到它就知道这一步的页面态缺失，
    # 而不是误以为「这一步页面没变化」。
    _RECORDING_BOOL_FIELDS = ("page_dropped", "valueTruncated")
    _RECORDING_TIERS = frozenset({"plain", "identifier", "secret", "handoff"})
    _RECORDING_ACTIONS = frozenset(
        {
            "click", "dblclick", "drag", "upload", "input", "submit", "key",
            "scroll", "navigate", "dialog", "limit",
        }
    )
    _TEST_ID_ATTRIBUTES = frozenset({"data-testid", "data-test", "data-qa"})
    _PROVENANCE_SOURCES = frozenset(
        {
            "document-world",
            "isolated-world",
            "legacy-isolated-world",
            "host-navigation",
            "browser-host",
        }
    )

    @staticmethod
    def _recording_text(value: Any, limit: int | None) -> str:
        if not isinstance(value, str):
            return ""
        return value if limit is None else value[:limit]

    @staticmethod
    def _recording_int(
        value: Any,
        *,
        minimum: int,
        maximum: int | None,
    ) -> int:
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or isinstance(value, float)
            and not math.isfinite(value)
        ):
            return 0
        integer = int(value)
        if maximum is None:
            return max(minimum, integer)
        return max(minimum, min(maximum, integer))

    @staticmethod
    def _selector_fragment(value: Any, *, exact: bool = False) -> str:
        if exact:
            return value if isinstance(value, str) else ""
        if not isinstance(value, str) or not value or len(value) > 2048:
            return ""
        if ">>" in value or re.search(
            r"internal:|css=|xpath=|text=|aria-ref", value, re.IGNORECASE
        ):
            return ""
        return (
            value
            if re.fullmatch(r"""[A-Za-z0-9_\-#.>:()\\[\]"'= ]+""", value)
            else ""
        )

    @staticmethod
    def _evidence_token(
        value: Any,
        limit: int = 200,
        *,
        exact: bool = False,
    ) -> str:
        if exact:
            return value if isinstance(value, str) else ""
        if not isinstance(value, str) or not value or len(value) > limit:
            return ""
        return value if re.fullmatch(r"[A-Za-z0-9_.:\[\]-]+", value) else ""

    @staticmethod
    def _recording_bool(value: Any) -> bool:
        if value is True or value is False:
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized == "true":
                return True
            if normalized in {"", "false"}:
                return False
        return False

    @classmethod
    def _bounded_recording_event(cls, value: dict[str, Any]) -> dict[str, Any] | None:
        if value.get("schemaVersion") == 11:
            return normalize_recording_event_v11(value)

        target_id = value.get("targetId")
        if not isinstance(target_id, str) or not target_id or len(target_id) > 256:
            return None

        raw_schema = value.get("schemaVersion")
        if raw_schema is None:
            schema_version = 1
        elif (
            isinstance(raw_schema, int)
            and not isinstance(raw_schema, bool)
            and raw_schema in cls._RECORDING_ALLOWED_FIELDS
            and raw_schema != 1
        ):
            schema_version = raw_schema
        else:
            return None
        allowed_fields = cls._RECORDING_ALLOWED_FIELDS[schema_version]
        exact_v10 = schema_version >= 10

        event: dict[str, Any] = {
            "schemaVersion": schema_version,
            "type": "recording",
            "targetId": target_id,
        }
        for name, limit in cls._LEGACY_RECORDING_TEXT_LIMITS.items():
            raw = value.get(name) if name in allowed_fields else None
            event[name] = cls._recording_text(raw, None if exact_v10 else limit)
        if event["action"] not in cls._RECORDING_ACTIONS:
            return None
        # v10 is a functional trace: signed URLs, selectors, page evidence and
        # every field value remain byte-for-byte intact.

        event["step"] = cls._recording_int(
            value.get("step"),
            minimum=0,
            maximum=cls._JS_SAFE_INTEGER_MAX if exact_v10 else 1_000_000,
        )
        if schema_version >= 9:
            raw_causal_id = value.get("causalId")
            # causalId=0 has the explicit meaning "standalone modal".  Never
            # coerce a missing/string/float/corrupt identity to zero, otherwise
            # a damaged atomic action silently degrades into an unrelated
            # wait-dialog step.
            if (
                type(raw_causal_id) is not int
                or not 0 <= raw_causal_id <= cls._JS_SAFE_INTEGER_MAX
            ):
                return None
            event["causalId"] = raw_causal_id
        if schema_version >= 10:
            opener_page = value.get("openerPage")
            popup_ordinal = value.get("popupOrdinal")
            created_by_causal_id = value.get("createdByCausalId")
            if (
                re.fullmatch(r"p[1-9][0-9]*", event["label"]) is None
                or
                not isinstance(opener_page, str)
                or opener_page
                and re.fullmatch(r"p[1-9][0-9]*", opener_page) is None
                or type(popup_ordinal) is not int
                or popup_ordinal < 0
                or type(created_by_causal_id) is not int
                or not 0 <= created_by_causal_id <= cls._JS_SAFE_INTEGER_MAX
                or bool(opener_page) != (popup_ordinal > 0)
                or not opener_page
                and created_by_causal_id != 0
            ):
                return None
            event["openerPage"] = opener_page
            event["popupOrdinal"] = popup_ordinal
            event["createdByCausalId"] = created_by_causal_id
        event["scrollX"] = cls._recording_int(
            value.get("scrollX"),
            minimum=-cls._JS_SAFE_INTEGER_MAX if exact_v10 else -1_000_000,
            maximum=cls._JS_SAFE_INTEGER_MAX if exact_v10 else 1_000_000,
        )
        event["scrollY"] = cls._recording_int(
            value.get("scrollY"),
            minimum=-cls._JS_SAFE_INTEGER_MAX if exact_v10 else -1_000_000,
            maximum=cls._JS_SAFE_INTEGER_MAX if exact_v10 else 1_000_000,
        )
        event["backendNodeId"] = cls._recording_int(
            value.get("backendNodeId"), minimum=0, maximum=2_147_483_647
        )
        event["timestamp"] = cls._recording_int(
            value.get("timestamp"), minimum=0, maximum=cls._JS_SAFE_INTEGER_MAX
        )
        if schema_version >= 4:
            if event["action"] in {"click", "dblclick"}:
                click_button = value.get("clickButton")
                click_count = value.get("clickCount")
                modifiers = value.get("modifiers")
                if (
                    click_button not in {"left", "middle", "right"}
                    or type(click_count) is not int
                    or not 1 <= click_count <= (
                        cls._JS_SAFE_INTEGER_MAX if exact_v10 else 3
                    )
                    or event["action"] == "dblclick"
                    and click_count != 2
                    or not isinstance(modifiers, list)
                    or len(modifiers) > 4
                    or any(
                        not isinstance(modifier, str)
                        or modifier not in {"Alt", "Control", "Meta", "Shift"}
                        for modifier in modifiers
                    )
                    or len(set(modifiers)) != len(modifiers)
                ):
                    return None
                event["clickButton"] = click_button
                event["clickCount"] = click_count
                event["modifiers"] = [
                    modifier
                    for modifier in ("Alt", "Control", "Meta", "Shift")
                    if modifier in modifiers
                ]
            else:
                event["clickButton"] = ""
                event["clickCount"] = 0
                event["modifiers"] = []
        if schema_version >= 5:
            upload_mode = value.get("uploadMode")
            raw_paths = value.get("paths")
            raw_file_count = value.get("fileCount")
            multiple = value.get("multiple")
            accept = value.get("accept")
            if event["action"] == "upload":
                if (
                    upload_mode not in {"paths", "handoff", "clear"}
                    or not isinstance(raw_paths, list)
                    or not exact_v10
                    and len(raw_paths) > 256
                    or any(
                        not isinstance(item, str)
                        or not item
                        or not exact_v10
                        and len(item) > 32_768
                        or "\x00" in item
                        for item in raw_paths
                    )
                    or not exact_v10
                    and sum(len(item) for item in raw_paths) > 1024 * 1024
                    or type(raw_file_count) is not int
                    or not 0 <= raw_file_count <= (
                        cls._JS_SAFE_INTEGER_MAX if exact_v10 else 1_000_000
                    )
                    or type(multiple) is not bool
                    or not isinstance(accept, str)
                    or not exact_v10
                    and len(accept) > 4_096
                    or "\x00" in accept
                    or upload_mode == "paths"
                    and (
                        not raw_paths
                        or len(raw_paths) != raw_file_count
                    )
                    or upload_mode == "handoff"
                    and (raw_paths or raw_file_count < 1)
                    or upload_mode == "clear"
                    and (raw_paths or raw_file_count != 0)
                ):
                    return None
                event["uploadMode"] = upload_mode
                event["paths"] = list(raw_paths)
                event["fileCount"] = raw_file_count
                event["multiple"] = multiple
                event["accept"] = accept
            elif (
                upload_mode != ""
                or raw_paths != []
                or raw_file_count != 0
                or multiple is not False
                or accept != ""
            ):
                return None
            else:
                event["uploadMode"] = ""
                event["paths"] = []
                event["fileCount"] = 0
                event["multiple"] = False
                event["accept"] = ""
        if schema_version >= 6:
            raw_values = value.get("values")
            if (
                not isinstance(raw_values, list)
                or not exact_v10
                and len(raw_values) > 32
                or any(
                    not isinstance(item, str)
                    or not exact_v10
                    and len(item) > 4_096
                    for item in raw_values
                )
            ):
                return None
            event["values"] = list(raw_values)
        if schema_version >= 8:
            raw_position = value.get("position")
            if raw_position is None:
                event["position"] = None
            elif (
                event["action"] not in {"click", "dblclick"}
                or not isinstance(raw_position, dict)
                or set(raw_position) != {"x", "y"}
                or any(
                    type(raw_position[axis]) not in {int, float}
                    or not math.isfinite(float(raw_position[axis]))
                    or float(raw_position[axis]) < 0
                    or not exact_v10
                    and float(raw_position[axis]) > 1_000_000
                    for axis in ("x", "y")
                )
            ):
                return None
            else:
                event["position"] = {
                    "x": float(raw_position["x"]),
                    "y": float(raw_position["y"]),
                }
        if schema_version >= 9:
            dialog_action = value.get("dialogAction")
            dialog_type = value.get("dialogType")
            dialog_text = value.get("dialogText")
            if event["action"] == "dialog":
                if (
                    dialog_action not in {"accept", "dismiss"}
                    or dialog_type
                    not in {"alert", "confirm", "prompt", "beforeunload"}
                    or not isinstance(dialog_text, str)
                    or not exact_v10
                    and len(dialog_text) > 10_000
                    or "\x00" in dialog_text
                    or dialog_type != "prompt"
                    and dialog_text != ""
                    or dialog_action == "dismiss"
                    and dialog_text != ""
                ):
                    return None
                event["dialogAction"] = dialog_action
                event["dialogType"] = dialog_type
                event["dialogText"] = dialog_text
            elif (
                dialog_action != ""
                or dialog_type != ""
                or dialog_text != ""
            ):
                return None
            else:
                event["dialogAction"] = ""
                event["dialogType"] = ""
                event["dialogText"] = ""
        elif event["action"] == "dialog":
            return None
        for name in cls._RECORDING_BOOL_FIELDS:
            event[name] = value.get(name) is True if name in allowed_fields else False
        event["pageTruncated"] = (
            cls._recording_bool(value.get("pageTruncated"))
            if "pageTruncated" in allowed_fields
            else False
        )
        event["recordingId"] = ""
        if schema_version >= 2:
            recording_id = value.get("recordingId")
            if isinstance(recording_id, str) and re.fullmatch(
                r"[0-9a-fA-F]{8,32}", recording_id
            ):
                event["recordingId"] = recording_id.lower()

        # v10 的 tier 只是描述性元数据，未知值不得触发证据丢失。旧 schema
        # 继续维持历史 fail-closed 行为，保证已持久化轨迹的解释不变。
        if event["tier"] not in cls._RECORDING_TIERS:
            event["tier"] = (
                "plain"
                if exact_v10 or event.get("action") != "input"
                else "secret"
            )
        raw_input_value = value.get("value")
        event["valueTruncated"] = (
            False
            if exact_v10
            else bool(
                event["action"] == "input"
                and event["tier"] not in {"secret", "handoff"}
                and (
                    value.get("valueTruncated") is True
                    or isinstance(raw_input_value, str)
                    and len(raw_input_value)
                    > cls._LEGACY_RECORDING_TEXT_LIMITS["value"]
                )
            )
        )
        # 旧 schema 保持历史红action；v10 的 secret/handoff 与 plain 一样完整。
        sensitive = (
            not exact_v10 and event["tier"] in {"secret", "handoff"}
        )

        def bounded_target(raw_target: Any) -> dict[str, Any] | None:
            if not isinstance(raw_target, dict) or sensitive:
                return None
            if (
                schema_version >= 3
                and type(raw_target.get("contentEditable")) is not bool
            ):
                raise ValueError("invalid contentEditable")
            raw_frame_path = raw_target.get("framePath")
            if exact_v10 and (
                not isinstance(raw_frame_path, list)
                or any(
                    not isinstance(item, str) or not item
                    for item in raw_frame_path
                )
            ):
                raise ValueError("invalid framePath")
            test_id = cls._evidence_token(
                raw_target.get("testId"), exact=exact_v10
            )
            test_id_attribute = raw_target.get("testIdAttribute")
            tag = raw_target.get("tag")
            role = raw_target.get("role")
            input_type = raw_target.get("inputType")
            result = {
                "tag": cls._evidence_token(
                    tag.lower() if isinstance(tag, str) else "",
                    40,
                    exact=exact_v10,
                ),
                "text": cls._recording_text(
                    raw_target.get("text"), None if exact_v10 else 200
                ),
                "ariaLabel": cls._recording_text(
                    raw_target.get("ariaLabel"), None if exact_v10 else 200
                ),
                "href": (
                    cls._recording_text(raw_target.get("href"), None)
                    if exact_v10
                    else str(raw_target.get("href") or "")[:16_384]
                ),
                "ordinal": cls._recording_int(
                    raw_target.get("ordinal"),
                    minimum=0,
                    maximum=(
                        cls._JS_SAFE_INTEGER_MAX if exact_v10 else 1_000_000
                    ),
                ),
                "id": cls._evidence_token(
                    raw_target.get("id"), exact=exact_v10
                ),
                "name": cls._evidence_token(
                    raw_target.get("name"), exact=exact_v10
                ),
                "role": cls._evidence_token(
                    role.lower() if isinstance(role, str) else "",
                    80,
                    exact=exact_v10,
                ),
                "inputType": cls._evidence_token(
                    input_type.lower() if isinstance(input_type, str) else "",
                    40,
                    exact=exact_v10,
                ),
                "testId": test_id,
                "testIdAttribute": (
                    test_id_attribute
                    if (
                        test_id_attribute in cls._TEST_ID_ATTRIBUTES
                        and (exact_v10 or test_id)
                    )
                    else ""
                ),
                "cssPath": cls._selector_fragment(
                    raw_target.get("cssPath"), exact=exact_v10
                ),
                "framePath": [
                    fragment
                    for fragment in (
                        cls._selector_fragment(item, exact=exact_v10)
                        for item in (
                            raw_frame_path
                            if exact_v10 and isinstance(raw_frame_path, list)
                            else raw_frame_path[:8]
                            if isinstance(raw_frame_path, list)
                            else []
                        )
                    )
                    if fragment
                ],
            }
            if schema_version >= 3:
                result["contentEditable"] = raw_target["contentEditable"]
            return result

        try:
            event["target"] = bounded_target(value.get("target"))
            event["dragTarget"] = (
                bounded_target(value.get("dragTarget"))
                if event["action"] == "drag"
                else None
            )
        except ValueError:
            return None
        if event["action"] == "drag" and event["dragTarget"] is None:
            return None
        if schema_version >= 6:
            target = event["target"]
            multi_select_input = (
                not sensitive
                and event["action"] == "input"
                and isinstance(target, dict)
                and target.get("tag") == "select"
                and target.get("inputType") == "select-multiple"
            )
            if not multi_select_input and event["values"]:
                return None

        target_evidence = (
            "redacted" if sensitive else ("synchronous" if event["target"] else "none")
        )
        if schema_version >= 2:
            provenance = value.get("provenance")
            if not isinstance(provenance, dict):
                return None
            source = provenance.get("source")
            capture_phase = provenance.get("capturePhase")
            native_input = provenance.get("nativeInput")
            host_generated = event["action"] in {"navigate", "dialog", "limit"}
            if (
                provenance.get("schemaVersion") != cls._RECORDING_PROVENANCE_VERSION
                or source not in cls._PROVENANCE_SOURCES
                or capture_phase not in {"event-callback", "host"}
                or not isinstance(provenance.get("browserTrusted"), bool)
                or provenance.get("targetEvidence") != target_evidence
                or native_input not in {"unverified", "correlated", "host"}
                or host_generated
                and (
                    source not in {"host-navigation", "browser-host"}
                    or capture_phase != "host"
                    or provenance.get("browserTrusted") is not False
                    or native_input != "host"
                )
                or not host_generated
                and (
                    source
                    not in {
                        "document-world",
                        "isolated-world",
                        "legacy-isolated-world",
                    }
                    or capture_phase != "event-callback"
                    or provenance.get("browserTrusted") is not True
                    or native_input not in {"unverified", "correlated"}
                )
            ):
                return None
            event["provenance"] = {
                "schemaVersion": cls._RECORDING_PROVENANCE_VERSION,
                "source": source,
                "capturePhase": capture_phase,
                "browserTrusted": provenance["browserTrusted"],
                "targetEvidence": target_evidence,
                "nativeInput": native_input,
                "transport": "authenticated-electron-host",
            }
        else:
            host_generated = event["action"] in {"navigate", "dialog", "limit"}
            event["provenance"] = {
                "schemaVersion": cls._RECORDING_PROVENANCE_VERSION,
                "source": "browser-host" if host_generated else "legacy-isolated-world",
                "capturePhase": "host" if host_generated else "event-callback",
                "browserTrusted": not host_generated,
                "targetEvidence": target_evidence,
                "nativeInput": "host" if host_generated else "legacy-host-checked",
                "transport": "authenticated-electron-host",
            }

        # 旧 schema 的历史语义最后执行；v10 不进入该分支，所有定位与动作证据
        # 原样跨过 Python bridge。
        if sensitive:
            event["url"] = ""
            event["hint"] = f"<{event['tier']} field>"
            event["value"] = ""
            if schema_version >= 6:
                event["values"] = []
            event["valueTruncated"] = False
            event["key"] = ""
            if schema_version >= 8:
                event["position"] = None
            event["selector"] = ""
            event["targetSelector"] = ""
            event["target"] = None
            event["dragTarget"] = None
            if schema_version >= 5:
                event["uploadMode"] = ""
                event["paths"] = []
                event["fileCount"] = 0
                event["multiple"] = False
                event["accept"] = ""
            event["page"] = ""
            event["pageTruncated"] = False
        return event

    @classmethod
    def _bounded_debug_event(cls, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict) or value.get("type") != "debug":
            return None
        channel = value.get("channel")
        target_id = value.get("targetId")
        record = value.get("record")
        if (
            channel not in {"console", "network"}
            or not isinstance(target_id, str)
            or not target_id
            or len(target_id) > 256
            or not isinstance(record, dict)
        ):
            return None
        try:
            json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return None
        return {
            "type": "debug",
            "channel": channel,
            "targetId": target_id,
            "record": record,
        }

    @classmethod
    def _bounded_download_event(cls, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict) or set(value) != {
            "type",
            "runtimeKey",
            "downloadId",
            "targetId",
            "sessionHash",
            "path",
            "name",
            "suggestedFilename",
            "url",
            "state",
            "receivedBytes",
            "totalBytes",
            "createdAt",
            "completedAt",
            "error",
        }:
            return None
        text_fields = (
            "downloadId",
            "targetId",
            "sessionHash",
            "path",
            "name",
            "suggestedFilename",
            "url",
            "state",
            "error",
        )
        if (
            value.get("type") != "download"
            or not isinstance(value.get("runtimeKey"), str)
            or any(not isinstance(value.get(field), str) for field in text_fields)
            or not value["downloadId"]
            or not value["targetId"]
            or re.fullmatch(r"[0-9a-f]{32}", value["sessionHash"]) is None
            or value["state"]
            not in {"progressing", "completed", "cancelled", "interrupted"}
            or any(
                _v11_safe_int(value.get(field)) is None
                for field in (
                    "receivedBytes",
                    "totalBytes",
                    "createdAt",
                    "completedAt",
                )
            )
        ):
            return None
        return {
            "type": "download",
            **{field: value[field] for field in text_fields},
            "receivedBytes": int(value["receivedBytes"]),
            "totalBytes": int(value["totalBytes"]),
            "createdAt": int(value["createdAt"]),
            "completedAt": int(value["completedAt"]),
        }

    @staticmethod
    def _fail_pending(connection: _HostConnection, message: str) -> None:
        for pending in connection.pending.values():
            if pending.future.done():
                continue
            if pending.mutating and pending.sent:
                error = ElectronBridgeError(
                    message,
                    uncertain=True,
                    stop_unconfirmed=True,
                    request_sent=True,
                    connection_generation=connection.generation,
                )
            else:
                error = ElectronBridgeError(
                    message,
                    retryable=True,
                    request_sent=pending.sent,
                    connection_generation=connection.generation,
                )
            pending.future.set_exception(error)
        connection.pending.clear()

    async def _wait_until_ready(
        self,
        key: str,
        connection: _HostConnection,
        *,
        timeout: float,
        allow_unready: bool,
    ) -> None:
        if allow_unready:
            return
        try:
            await asyncio.wait_for(connection.ready.wait(), timeout=max(0.01, timeout))
        except asyncio.TimeoutError as exc:
            raise ElectronBridgeError(
                "桌面浏览器宿主仍在初始化，请稍后重试",
                retryable=True,
                connection_generation=connection.generation,
            ) from exc
        if self._connections.get(key) is not connection:
            raise ElectronBridgeError(
                "桌面浏览器宿主连接已切换",
                retryable=True,
                connection_generation=connection.generation,
            )
        if connection.initialization_error is not None:
            raise connection.initialization_error

    async def _attempt(
        self,
        key: str,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float,
        mutating: bool,
        allow_unready: bool,
        state: _AttemptState,
    ) -> Any:
        connection = self._connections.get(key)
        if connection is None:
            raise ElectronBridgeError(
                "桌面内置浏览器尚未连接；请打开 Crew 桌面应用并保持登录",
                retryable=True,
                connection_generation=self._generations.get(key, 0),
            )
        deadline = time.monotonic() + max(0.01, timeout)

        def remaining() -> float:
            value = deadline - time.monotonic()
            if value <= 0:
                raise asyncio.TimeoutError
            return value

        await self._wait_until_ready(
            key,
            connection,
            timeout=remaining(),
            allow_unready=allow_unready,
        )
        request_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        pending = _PendingRequest(future=future, mutating=mutating)
        connection.pending[request_id] = pending
        try:
            async def send() -> None:
                async with connection.send_lock:
                    if self._connections.get(key) is not connection:
                        raise ElectronBridgeError(
                            "桌面浏览器宿主连接已切换",
                            retryable=True,
                            connection_generation=connection.generation,
                        )
                    # Mark before the await. If send_json raises or times out,
                    # the transport cannot prove that a mutation was not received.
                    pending.sent = True
                    state.sent = True
                    await connection.socket.send_json(
                        {
                            "type": "request",
                            "id": request_id,
                            "runtime_key": key,
                            "method": str(method or "")[:80],
                            "params": params,
                        }
                    )

            # One absolute deadline covers queueing for the socket, the actual
            # WebSocket write and the response. Otherwise a blocked send_json
            # would make a cancelled sent mutation wait forever in request().
            await asyncio.wait_for(send(), timeout=remaining())
            response = await asyncio.wait_for(future, timeout=remaining())
        except asyncio.TimeoutError as exc:
            raise ElectronBridgeError(
                "桌面浏览器操作超时；若动作可能已发送，请重新观察页面",
                uncertain=bool(mutating and pending.sent),
                # 超时 ≠ 关停未确认。stop_unconfirmed 的语义是「无法确认浏览器已关闭」，
                # 它会锁住 Profile 并要求重启应用；而这里 socket 仍然活着，只是响应慢
                # （大页面、慢网络都会）。把超时也标成 stop_unconfirmed，会让一次点击
                # 超时就 fence 整个账号、清空所有会话的标签页。动作结果的不确定性由
                # uncertain 承载即可，manager 会据此只作废本会话的观察。
                # 连接断开与用户取消仍然置位 stop_unconfirmed——那两种是真的失联。
                request_sent=pending.sent,
                connection_generation=connection.generation,
            ) from exc
        except ElectronBridgeError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if mutating and pending.sent:
                raise ElectronBridgeError(
                    "桌面浏览器通信失败；变更操作结果未知",
                    uncertain=True,
                    stop_unconfirmed=True,
                    request_sent=True,
                    connection_generation=connection.generation,
                ) from exc
            raise ElectronBridgeError(
                "桌面浏览器通信失败",
                retryable=True,
                request_sent=pending.sent,
                connection_generation=connection.generation,
            ) from exc
        finally:
            connection.pending.pop(request_id, None)

        if response.get("ok") is not True:
            error = str(response.get("error") or "桌面浏览器操作失败")
            raise ElectronBridgeError(
                error,
                uncertain=bool(response.get("uncertain")),
                browser_stopped=bool(response.get("browser_stopped")),
                stop_unconfirmed=bool(response.get("stop_unconfirmed")),
                request_sent=True,
                connection_generation=connection.generation,
                remote_terminal=True,
                code=str(response.get("code") or ""),
                phase=str(response.get("phase") or ""),
                partial=bool(response.get("partial")),
                completed_count=(
                    int(response.get("completed_count"))
                    if type(response.get("completed_count")) is int
                    and 0 <= int(response["completed_count"]) <= self._JS_SAFE_INTEGER_MAX
                    else 0
                ),
            )
        return response.get("result")

    async def request(
        self,
        runtime_key: str,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float,
        mutating: bool = False,
        retry_readonly: bool = False,
        _allow_unready: bool = False,
    ) -> Any:
        """Send one Host RPC with lifecycle-aware cancellation and retry rules."""

        key = self._runtime_key(runtime_key)
        total_timeout = max(0.1, float(timeout))
        state = _AttemptState()

        async def run() -> Any:
            # Every WebSocket registration is a new Host epoch. Manager state
            # is reset before that connection becomes ready, so replaying an
            # old read across epochs would both deadlock on owner.lock and read
            # a page that no longer exists. ``retry_readonly`` only documents
            # that the caller may issue a *new* explicit observation afterward.
            return await self._attempt(
                key,
                method,
                params,
                timeout=total_timeout,
                mutating=mutating,
                allow_unready=_allow_unready,
                state=state,
            )

        task = asyncio.create_task(run())
        try:
            return await asyncio.shield(task) if mutating else await task
        except asyncio.CancelledError:
            if not mutating or not state.sent:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
            # A sent mutation owns BrowserManager's account lock until the Host
            # confirms it or the bounded timeout produces fail-stop metadata.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    # Repeated caller cancellation still cannot abandon a
                    # possibly executed mutation.
                    continue
                except ElectronBridgeError:
                    break
            try:
                task.result()
            except ElectronBridgeError as exc:
                # Keep the remote lifecycle outcome without consuming the
                # user's cancellation and starting another LLM iteration.
                raise ElectronBridgeCancelled(exc) from None
            raise


electron_browser_bridge = ElectronBrowserBridge()
