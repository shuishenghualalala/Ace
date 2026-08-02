"""Contracts for the default-on append-only recorder v11 and replay.v3 IR."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crew.agent.skills import validate_generated_skill
from crew.browser.electron_bridge import (
    ElectronBrowserBridge,
    normalize_recording_event_v11,
)
from crew.browser.manager import BrowserManager
from crew.browser.types import BrowserConfig
from crew.core.runctx import (
    current_owner_account_id,
    current_session_id,
    current_tool_call_id,
)
from crew.gateway.ws import _apply_browser_skill_policy
from plugins.browser.compile_tool import (
    RecordWorkflowTools,
    WorkflowRejected,
    _compile_plan,
    _draft_payload,
    _read_trace,
)
from plugins.browser.replay_tool import _validate_executable_capabilities
from plugins.browser.workflow_store import (
    WORKFLOW_STORE_SCHEMA,
    WORKFLOW_STORE_SCHEMA_V3,
    WorkflowStoreError,
    _artifact_from_raw,
    build_workflow_artifact,
    read_workflow,
)


RECORDING_ID = "aaaa1111"
OWNER = "owner-v11"


@pytest.fixture
def v11_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CREW_BROWSER_RECORDING_V11_PHASE_A", raising=False)


def _provenance(*, persisted: bool = False) -> dict:
    value = {
        "schemaVersion": 1,
        "source": "browser-host",
        "capturePhase": "host",
        "browserTrusted": False,
        "targetEvidence": "none",
        "nativeInput": "host",
    }
    if persisted:
        value["transport"] = "authenticated-electron-host"
    return value


def _evidence(
    *,
    hint: str = "",
    tier: str = "plain",
    url: str = "https://example.test/",
) -> dict:
    return {
        "url": url,
        "hint": hint,
        "tier": tier,
        "target": None,
        "dragTarget": None,
        "snapshot": "",
        "snapshotDropped": False,
        "backendNodeId": 0,
    }


def _base(
    *,
    step: int,
    event_index: int,
    transaction_id: int,
    transaction_kind: str,
    record_kind: str,
    page: str,
) -> dict:
    return {
        "schemaVersion": 11,
        "type": "recording",
        "targetId": f"target-{page}",
        "recordingId": RECORDING_ID,
        "step": step,
        "eventIndex": event_index,
        "transactionId": transaction_id,
        "transactionKind": transaction_kind,
        "recordKind": record_kind,
        "pageGuid": page,
        "timestamp": event_index,
        "provenance": _provenance(),
    }


def _action(
    action: dict,
    *,
    step: int,
    event_index: int,
    transaction_id: int,
    page: str,
    evidence: dict | None = None,
) -> dict:
    return {
        **_base(
            step=step,
            event_index=event_index,
            transaction_id=transaction_id,
            transaction_kind="action",
            record_kind="action",
            page=page,
        ),
        "action": action,
        "evidence": evidence or _evidence(),
    }


def _signal(
    signal: dict,
    details: dict,
    *,
    step: int,
    event_index: int,
    transaction_id: int,
    transaction_kind: str,
    page: str,
) -> dict:
    return {
        **_base(
            step=step,
            event_index=event_index,
            transaction_id=transaction_id,
            transaction_kind=transaction_kind,
            record_kind="signal",
            page=page,
        ),
        "signal": signal,
        "details": details,
    }


def _bridge_rows(rows: list[dict]) -> list[dict]:
    normalized = [ElectronBrowserBridge._bounded_recording_event(row) for row in rows]
    assert all(row is not None for row in normalized)
    return [row for row in normalized if row is not None]


def _write_trace(path: Path, rows: list[dict]) -> Path:
    path.write_text(
        "".join(
            # Matches JSON.stringify for lone UTF-16 code units too.
            json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def test_v11_bridge_is_default_on_explicitly_rollbackable_and_strict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selector = "iframe[name='应用'] >> internal:role=button[name=\"提交\"]" * 100
    raw = _action(
        {
            "name": "fill",
            "selector": selector,
            "text": "",
        },
        step=1,
        event_index=1,
        transaction_id=101,
        page="p1",
        evidence=_evidence(hint="密码", tier="secret"),
    )

    monkeypatch.delenv("CREW_BROWSER_RECORDING_V11_PHASE_A", raising=False)
    assert ElectronBrowserBridge._bounded_recording_event(raw) is not None

    monkeypatch.setenv("CREW_BROWSER_RECORDING_V11_PHASE_A", "0")
    assert ElectronBrowserBridge._bounded_recording_event(raw) is None

    monkeypatch.setenv("CREW_BROWSER_RECORDING_V11_PHASE_A", "1")
    event = ElectronBrowserBridge._bounded_recording_event(raw)
    assert event is not None
    assert event["action"]["selector"] == selector
    assert event["action"]["text"] == ""
    assert event["evidence"]["tier"] == "secret"
    assert event["provenance"]["transport"] == "authenticated-electron-host"
    assert normalize_recording_event_v11(event, persisted=True) == event

    assert ElectronBrowserBridge._bounded_recording_event({**raw, "unknown": "not allowed"}) is None
    corrupt = {**raw, "transactionId": 0}
    assert ElectronBrowserBridge._bounded_recording_event(corrupt) is None


@pytest.mark.parametrize(
    "action",
    [
        {
            "name": "click",
            "selector": 'internal:role=button[name="Go"]',
            "button": "middle",
            "modifiers": ["Meta", "Shift"],
            "clickCount": 1,
            "position": {"x": 1, "y": 2.5},
        },
        {"name": "hover", "selector": "css=.menu", "position": None},
        {"name": "fill", "selector": "css=input", "text": ""},
        {"name": "check", "selector": "css=#agree"},
        {"name": "uncheck", "selector": "css=#agree"},
        {"name": "select", "selector": "css=select", "options": []},
        {
            "name": "press",
            "selector": "",
            "key": "Enter",
            "modifiers": ["Control"],
        },
        {
            "name": "setInputFiles",
            "selector": "css=input[type=file]",
            "files": ["/tmp/报告.pdf"],
        },
        {"name": "navigate", "url": "https://example.test/a?x=1#y"},
        {"name": "openPage", "url": "about:blank"},
        {"name": "closePage"},
        {
            "name": "x-crew-navigate",
            "operation": "back",
            "url": "",
        },
        {
            "name": "x-crew-drag",
            "sourceSelector": "css=#from",
            "targetSelector": "css=#to",
            "sourcePosition": None,
            "targetPosition": {"x": 5, "y": 6},
        },
        {
            "name": "x-crew-drop",
            "selector": "css=#drop-zone",
            "files": ["/tmp/外部.txt"],
            "data": {
                "text/plain": "exact",
                "text/uri-list": "https://example.test/a?token=exact#fragment",
                "application/x-custom": "\x00exact\x01payload",
            },
        },
        {
            "name": "x-crew-pointerGesture",
            "selector": "css=#signature",
            "button": "left",
            "modifiers": ["Shift", "Control"],
            "start": {"x": -1.25, "y": 2.5},
            "points": [
                {"x": 3.75, "y": 4.125, "elapsedMs": 5.5},
                {"x": -2, "y": 8, "elapsedMs": 12},
            ],
        },
        {
            "name": "x-crew-scroll",
            "selector": "",
            "deltaX": -10,
            "deltaY": 20,
        },
        {"name": "x-crew-activatePage"},
    ],
)
def test_v11_bridge_accepts_complete_action_union(
    v11_gate: None,
    action: dict,
) -> None:
    event = ElectronBrowserBridge._bounded_recording_event(
        _action(
            action,
            step=1,
            event_index=1,
            transaction_id=1,
            page="p1",
        )
    )
    assert event is not None
    assert event["action"]["name"] == action["name"]


@pytest.mark.parametrize(
    ("signal", "details", "page"),
    [
        (
            {"name": "navigation", "url": "https://example.test/next"},
            {},
            "p1",
        ),
        (
            {"name": "popup", "popupPageGuid": "p2"},
            {
                "openerPageGuid": "p1",
                "popupIndex": 1,
                "disposition": "background-tab",
                "activate": False,
            },
            "p1",
        ),
        (
            {"name": "download", "downloadAlias": "d1"},
            {"ordinal": 1, "suggestedFilename": "报告.pdf"},
            "p1",
        ),
        (
            {"name": "dialog", "dialogAlias": "dlg1"},
            {"type": "prompt", "action": "accept", "promptText": ""},
            "p1",
        ),
        (
            {
                "name": "x-crew-pageClosed",
                "closedPageGuid": "p1",
                "reason": "window.close",
            },
            {},
            "p1",
        ),
    ],
)
def test_v11_bridge_accepts_complete_signal_union(
    v11_gate: None,
    signal: dict,
    details: dict,
    page: str,
) -> None:
    event = ElectronBrowserBridge._bounded_recording_event(
        _signal(
            signal,
            details,
            step=1,
            event_index=1,
            transaction_id=1,
            transaction_kind="observation",
            page=page,
        )
    )
    assert event is not None
    assert event["signal"] == signal


def test_v11_compiler_groups_late_signals_and_closes_pages(
    v11_gate: None,
    tmp_path: Path,
) -> None:
    rows = _bridge_rows(
        [
            _action(
                {"name": "openPage", "url": "https://example.test/start"},
                step=1,
                event_index=1,
                transaction_id=101,
                page="p1",
            ),
            _action(
                {
                    "name": "hover",
                    "selector": 'internal:role=menuitem[name="Products"]',
                    "position": None,
                },
                step=2,
                event_index=2,
                transaction_id=102,
                page="p1",
            ),
            # A later logical transaction may be appended before all signals
            # for transaction 102 arrive. Grouping is by transactionId, never
            # by JSONL adjacency.
            _action(
                {"name": "closePage"},
                step=3,
                event_index=3,
                transaction_id=103,
                page="p2",
            ),
            _signal(
                {"name": "popup", "popupPageGuid": "p2"},
                {
                    "openerPageGuid": "p1",
                    "popupIndex": 1,
                    "disposition": "foreground-tab",
                    "activate": True,
                },
                step=2,
                event_index=4,
                transaction_id=102,
                transaction_kind="action",
                page="p1",
            ),
            # This child navigation arrives after the action and still belongs
            # to transaction 102 rather than a timestamp guess.
            _signal(
                {"name": "navigation", "url": "https://example.test/popup"},
                {},
                step=2,
                event_index=5,
                transaction_id=102,
                transaction_kind="action",
                page="p2",
            ),
            _signal(
                {
                    "name": "x-crew-pageClosed",
                    "closedPageGuid": "p2",
                    "reason": "explicit",
                },
                {},
                step=3,
                event_index=6,
                transaction_id=103,
                transaction_kind="action",
                page="p2",
            ),
        ]
    )
    trace = _read_trace(
        _write_trace(tmp_path / "trace.jsonl", rows),
        RECORDING_ID,
    )
    assert trace.schema_version == 11
    assert [group.step for group in trace.transactions] == [1, 2, 3]
    assert [len(group.effects) for group in trace.transactions] == [0, 2, 1]

    # Selecting p1->p2 activity automatically closes over p1's openPage
    # definition; signal rows never need separate source_step selections.
    plan, inputs = _compile_plan(
        {
            "schema_version": "crew.browser.workflow.v1",
            "steps": [{"source_step": 2}, {"source_step": 3}],
        },
        trace,
    )
    assert inputs == {}
    assert [step["kind"] for step in plan] == [
        "open_page",
        "hover",
        "close_page",
        "snapshot_full",
    ]
    assert plan[0]["mode"] == "reuse_current"
    assert plan[1]["effects"] == [
        {
            "kind": "popup",
            "page": "p2",
            "opener_page": "p1",
            "popup_index": 1,
            "activate": True,
            "disposition": "foreground-tab",
        },
        {
            "kind": "navigation",
            "page": "p2",
            "url": "https://example.test/popup",
        },
    ]
    assert plan[2]["effects"] == [{"kind": "page_closed", "page": "p2", "reason": "explicit"}]
    assert plan[-1]["page"] == "p1"

    draft = _draft_payload(
        owner=OWNER,
        session_id="session-v11",
        recording_id=RECORDING_ID,
        slug="phase-a-v11",
        workflow={
            "schema_version": "crew.browser.workflow.v1",
            "steps": [{"source_step": 2}, {"source_step": 3}],
        },
        trace=trace,
    )
    assert draft["plan"] == plan
    assert draft["capabilities"] == [
        "open_page",
        "close_page",
        "hover",
        "snapshot_full",
        "popup",
        "navigation_effect",
        "page_closed",
    ]

    artifact = build_workflow_artifact(
        owner=OWNER,
        hosts=trace.hosts,
        inputs=inputs,
        plan=plan,
        schema_version=WORKFLOW_STORE_SCHEMA_V3,
    )
    assert artifact.payload["schema_version"] == WORKFLOW_STORE_SCHEMA_V3
    assert artifact.payload["capabilities"] == [
        "open_page",
        "close_page",
        "hover",
        "snapshot_full",
        "popup",
        "navigation_effect",
        "page_closed",
    ]
    assert _artifact_from_raw(OWNER, artifact.workflow_id, artifact.raw) == artifact
    # With the v11/v3 gate enabled, the immutable artifact is executable. The
    # Manager still performs a separate Host capability handshake immediately
    # before beginning replay.
    _validate_executable_capabilities(artifact)


def test_v11_values_and_arrays_survive_bridge_compile_and_store(
    v11_gate: None,
    tmp_path: Path,
) -> None:
    exact_text = "\ud800" + "甲" * 20_000
    exact_path = "/tmp/\udfff-报告.pdf"
    rows = _bridge_rows(
        [
            _action(
                {"name": "openPage", "url": "https://example.test/form"},
                step=1,
                event_index=1,
                transaction_id=301,
                page="p1",
            ),
            _action(
                {
                    "name": "fill",
                    "selector": "css=textarea[name='正文']",
                    "text": exact_text,
                },
                step=2,
                event_index=2,
                transaction_id=302,
                page="p1",
                evidence=_evidence(hint="正文", tier="secret"),
            ),
            _action(
                {
                    "name": "select",
                    "selector": "css=select[multiple]",
                    "options": [],
                },
                step=3,
                event_index=3,
                transaction_id=303,
                page="p1",
                evidence=_evidence(hint="分类"),
            ),
            _action(
                {
                    "name": "setInputFiles",
                    "selector": "css=input[type=file]",
                    "files": [exact_path],
                },
                step=4,
                event_index=4,
                transaction_id=304,
                page="p1",
                evidence=_evidence(hint="附件"),
            ),
        ]
    )
    trace = _read_trace(
        _write_trace(tmp_path / "trace.jsonl", rows),
        RECORDING_ID,
    )
    plan, inputs = _compile_plan(
        {
            "schema_version": "crew.browser.workflow.v1",
            "steps": [
                {"source_step": 2},
                {"source_step": 3},
                {"source_step": 4},
            ],
        },
        trace,
    )
    assert inputs["field_1"]["default"] == exact_text
    assert inputs["field_2"]["default"] == []
    assert inputs["field_3"]["default"] == [exact_path]
    artifact = build_workflow_artifact(
        owner=OWNER,
        hosts=trace.hosts,
        inputs=inputs,
        plan=plan,
        schema_version=WORKFLOW_STORE_SCHEMA_V3,
    )
    assert b"\\ud800" in artifact.raw
    assert b"\\udfff" in artifact.raw
    loaded = _artifact_from_raw(OWNER, artifact.workflow_id, artifact.raw)
    assert loaded.payload["inputs"]["field_1"]["default"] == exact_text
    assert loaded.payload["inputs"]["field_3"]["default"] == [exact_path]


def test_v11_drag_positions_and_external_drop_survive_compile_and_store(
    v11_gate: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exact_paths = [
        f"/tmp/外部-{index}-\udfff.bin"
        for index in range(1_001)
    ]
    exact_data = {
        "text/plain": "\ud800" + "甲" * 20_000,
        "text/uri-list": "https://example.test/a?token=exact#fragment",
        "application/x-custom": "\x00exact\x01payload",
        "": "",
    }
    rows = _bridge_rows(
        [
            _action(
                {"name": "openPage", "url": "https://example.test/drop"},
                step=1,
                event_index=1,
                transaction_id=401,
                page="p1",
            ),
            _action(
                {
                    "name": "x-crew-drag",
                    "sourceSelector": "css=#source",
                    "targetSelector": "css=#target",
                    "sourcePosition": {"x": 10.25, "y": 15.5},
                    "targetPosition": {"x": 30.5, "y": 36.5},
                },
                step=2,
                event_index=2,
                transaction_id=402,
                page="p1",
            ),
            _action(
                {
                    "name": "x-crew-drop",
                    "selector": "css=#drop-zone",
                    "files": exact_paths,
                    "data": exact_data,
                },
                step=3,
                event_index=3,
                transaction_id=403,
                page="p1",
                evidence=_evidence(hint="Drop files here"),
            ),
        ]
    )
    trace = _read_trace(
        _write_trace(tmp_path / "trace.jsonl", rows),
        RECORDING_ID,
    )
    plan, inputs = _compile_plan(
        {
            "schema_version": "crew.browser.workflow.v1",
            "steps": [{"source_step": 2}, {"source_step": 3}],
        },
        trace,
    )
    assert plan[1] == {
        "kind": "drag",
        "page": "p1",
        "source_selector": "css=#source",
        "target_selector": "css=#target",
        "source_position": {"x": 10.25, "y": 15.5},
        "target_position": {"x": 30.5, "y": 36.5},
        "effects": [],
    }
    assert plan[2] == {
        "kind": "drop",
        "page": "p1",
        "selector": "css=#drop-zone",
        "input_key": "field_1",
        "data": exact_data,
        "effects": [],
    }
    assert inputs["field_1"]["kind"] == "files"
    assert inputs["field_1"]["default"] == exact_paths

    artifact = build_workflow_artifact(
        owner=OWNER,
        hosts=trace.hosts,
        inputs=inputs,
        plan=plan,
        schema_version=WORKFLOW_STORE_SCHEMA_V3,
    )
    loaded = _artifact_from_raw(OWNER, artifact.workflow_id, artifact.raw)
    assert loaded.payload["plan"][1]["source_position"] == {
        "x": 10.25,
        "y": 15.5,
    }
    assert loaded.payload["plan"][2]["data"] == exact_data
    assert loaded.payload["inputs"]["field_1"]["default"] == exact_paths

    draft = _draft_payload(
        owner=OWNER,
        session_id="session-v11-drop",
        recording_id=RECORDING_ID,
        slug="external-drop-flow",
        workflow={
            "schema_version": "crew.browser.workflow.v1",
            "steps": [{"source_step": 2}, {"source_step": 3}],
        },
        trace=trace,
    )
    skill_dir = tmp_path / "external-drop-flow"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        str(draft["preview"]),
        encoding="utf-8",
    )
    assert validate_generated_skill(skill_dir, "external-drop-flow") == []

    class PolicyManager:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def clear_readonly(self, owner: str, session_id: str) -> None:
            self.calls.append(("clear", owner, session_id))

        def set_readonly(self, *args) -> None:
            self.calls.append(("set", *args))

    policy_manager = PolicyManager()
    monkeypatch.setattr(
        "crew.gateway.ws.get_skills",
        lambda: {
            "external-drop-flow": {
                "skill_dir": str(skill_dir),
            }
        },
    )
    _apply_browser_skill_policy(
        type("Crew", (), {"browser_manager": policy_manager})(),
        "external-drop-flow",
        OWNER,
        "session-v11-drop",
    )
    # 技能激活不再改会话上的任何运行期档位——授权来自不可变 plan。
    assert policy_manager.calls == []


def test_v11_pointer_gesture_survives_bridge_compile_and_v3_store_without_cap(
    v11_gate: None,
    tmp_path: Path,
) -> None:
    points = [
        {
            "x": float(index) - 250.5,
            "y": float(index % 17) + 0.125,
            "elapsedMs": float(index) * 1.25,
        }
        for index in range(501)
    ]
    gesture = {
        "name": "x-crew-pointerGesture",
        "selector": "css=#signature-pad",
        "button": "right",
        "modifiers": ["Shift", "Alt"],
        "start": {"x": -10.25, "y": 20.5},
        "points": points,
    }
    rows = _bridge_rows(
        [
            _action(
                {"name": "openPage", "url": "https://example.test/sign"},
                step=1,
                event_index=1,
                transaction_id=401,
                page="p1",
            ),
            _action(
                gesture,
                step=2,
                event_index=2,
                transaction_id=402,
                page="p1",
                evidence=_evidence(hint="signature canvas"),
            ),
        ]
    )
    assert rows[1]["action"]["modifiers"] == ["Alt", "Shift"]
    assert len(rows[1]["action"]["points"]) == 501

    trace = _read_trace(
        _write_trace(tmp_path / "pointer-trace.jsonl", rows),
        RECORDING_ID,
    )
    plan, inputs = _compile_plan(
        {
            "schema_version": "crew.browser.workflow.v1",
            "steps": [{"source_step": 2}],
        },
        trace,
    )
    assert inputs == {}
    pointer = next(step for step in plan if step["kind"] == "pointer_gesture")
    assert pointer == {
        "kind": "pointer_gesture",
        "page": "p1",
        "selector": "css=#signature-pad",
        "button": "right",
        "modifiers": ["Alt", "Shift"],
        "start": {"x": -10.25, "y": 20.5},
        "points": [
            {
                "x": point["x"],
                "y": point["y"],
                "elapsed_ms": point["elapsedMs"],
            }
            for point in points
        ],
        "effects": [],
    }
    artifact = build_workflow_artifact(
        owner=OWNER,
        hosts=trace.hosts,
        inputs=inputs,
        plan=plan,
        schema_version=WORKFLOW_STORE_SCHEMA_V3,
    )
    assert "pointer_gesture" in artifact.payload["capabilities"]
    assert (
        len(
            next(
                step
                for step in artifact.payload["plan"]
                if step["kind"] == "pointer_gesture"
            )["points"]
        )
        == 501
    )

    non_monotonic = {
        **gesture,
        "points": [
            {"x": 1, "y": 1, "elapsedMs": 10},
            {"x": 2, "y": 2, "elapsedMs": 9},
        ],
    }
    assert (
        ElectronBrowserBridge._bounded_recording_event(
            _action(
                non_monotonic,
                step=1,
                event_index=1,
                transaction_id=1,
                page="p1",
            )
        )
        is None
    )


def test_v11_pen_samples_survive_bridge_compile_and_v3_store(
    v11_gate: None,
    tmp_path: Path,
) -> None:
    start = {
        "x": 10.25,
        "y": 20.5,
        "pressure": 0.2,
        "tangentialPressure": -0.3,
        "tiltX": 11,
        "tiltY": -12,
        "twist": 19,
        "width": 7,
        "height": 5,
    }
    points = [
        {
            "x": 30.75,
            "y": 40.125,
            "elapsedMs": 5.5,
            "pressure": 0.8,
            "tangentialPressure": 0.25,
            "tiltX": 21,
            "tiltY": -22,
            "twist": 29,
            "width": 8,
            "height": 6,
        },
        {
            "x": -2.5,
            "y": 8.25,
            "elapsedMs": 12,
            "pressure": 0,
            "tiltX": 23,
            "tiltY": -24,
            "twist": 31,
            "width": 9,
            "height": 7,
        },
    ]
    gesture = {
        "name": "x-crew-pointerGesture",
        "selector": "css=#pen-pad",
        "pointerType": "pen",
        "button": "left",
        "modifiers": ["Shift"],
        "start": start,
        "points": points,
    }
    rows = _bridge_rows(
        [
            _action(
                {"name": "openPage", "url": "https://example.test/draw"},
                step=1,
                event_index=1,
                transaction_id=501,
                page="p1",
            ),
            _action(
                gesture,
                step=2,
                event_index=2,
                transaction_id=502,
                page="p1",
                evidence=_evidence(hint="pen canvas"),
            ),
        ]
    )
    assert rows[1]["action"] == gesture

    trace = _read_trace(
        _write_trace(tmp_path / "pen-trace.jsonl", rows),
        RECORDING_ID,
    )
    plan, inputs = _compile_plan(
        {
            "schema_version": "crew.browser.workflow.v1",
            "steps": [{"source_step": 2}],
        },
        trace,
    )
    assert inputs == {}
    pointer = next(step for step in plan if step["kind"] == "pointer_gesture")
    assert pointer == {
        "kind": "pointer_gesture",
        "page": "p1",
        "selector": "css=#pen-pad",
        "pointer_type": "pen",
        "button": "left",
        "modifiers": ["Shift"],
        "start": {
            "x": 10.25,
            "y": 20.5,
            "pressure": 0.2,
            "tangential_pressure": -0.3,
            "tilt_x": 11,
            "tilt_y": -12,
            "twist": 19,
            "width": 7,
            "height": 5,
        },
        "points": [
            {
                "x": 30.75,
                "y": 40.125,
                "elapsed_ms": 5.5,
                "pressure": 0.8,
                "tangential_pressure": 0.25,
                "tilt_x": 21,
                "tilt_y": -22,
                "twist": 29,
                "width": 8,
                "height": 6,
            },
            {
                "x": -2.5,
                "y": 8.25,
                "elapsed_ms": 12,
                "pressure": 0,
                "tilt_x": 23,
                "tilt_y": -24,
                "twist": 31,
                "width": 9,
                "height": 7,
            },
        ],
        "effects": [],
    }
    artifact = build_workflow_artifact(
        owner=OWNER,
        hosts=trace.hosts,
        inputs=inputs,
        plan=plan,
        schema_version=WORKFLOW_STORE_SCHEMA_V3,
    )
    stored = next(
        step
        for step in artifact.payload["plan"]
        if step["kind"] == "pointer_gesture"
    )
    assert stored == pointer

    impossible = {
        **gesture,
        "start": {**start, "pressure": 1.01},
    }
    assert (
        ElectronBrowserBridge._bounded_recording_event(
            _action(
                impossible,
                step=1,
                event_index=1,
                transaction_id=1,
                page="p1",
            )
        )
        is None
    )


def test_v11_resize_survives_bridge_compile_and_v3_store_without_cap(
    v11_gate: None,
    tmp_path: Path,
) -> None:
    resize = {
        "name": "x-crew-resize",
        "width": 1e300,
        "height": 707.25,
    }
    rows = _bridge_rows(
        [
            _action(
                {
                    "name": "openPage",
                    "url": "https://example.test/responsive",
                    "viewport": {"width": 900, "height": 620},
                },
                step=1,
                event_index=1,
                transaction_id=451,
                page="p1",
            ),
            _action(
                resize,
                step=2,
                event_index=2,
                transaction_id=452,
                page="p1",
            ),
        ]
    )
    assert rows[1]["action"] == resize

    trace = _read_trace(
        _write_trace(tmp_path / "resize-trace.jsonl", rows),
        RECORDING_ID,
    )
    plan, inputs = _compile_plan(
        {
            "schema_version": "crew.browser.workflow.v1",
            "steps": [{"source_step": 2}],
        },
        trace,
    )
    assert inputs == {}
    assert plan[:2] == [
        {
            "kind": "open_page",
            "page": "p1",
            "url": "https://example.test/responsive",
            "mode": "reuse_current",
            "activate": True,
            "viewport": {"width": 900.0, "height": 620.0},
            "effects": [],
        },
        {
            "kind": "resize",
            "page": "p1",
            "width": 1e300,
            "height": 707.25,
            "effects": [],
        },
    ]
    assert plan[2] == {
        "kind": "snapshot_full",
        "page": "p1",
        "effects": [],
    }
    artifact = build_workflow_artifact(
        owner=OWNER,
        hosts=trace.hosts,
        inputs=inputs,
        plan=plan,
        schema_version=WORKFLOW_STORE_SCHEMA_V3,
    )
    assert artifact.payload["capabilities"] == [
        "open_page",
        "resize",
        "snapshot_full",
    ]
    assert artifact.payload["plan"][1] == plan[1]

    for invalid_width in (True, float("inf"), float("nan")):
        invalid = _action(
            {
                "name": "x-crew-resize",
                "width": invalid_width,
                "height": 720,
            },
            step=1,
            event_index=1,
            transaction_id=1,
            page="p1",
        )
        assert ElectronBrowserBridge._bounded_recording_event(invalid) is None
        invalid_plan = [dict(step) for step in plan]
        invalid_plan[1] = {**invalid_plan[1], "width": invalid_width}
        with pytest.raises(WorkflowStoreError, match="workflow_resize_invalid"):
            build_workflow_artifact(
                owner=OWNER,
                hosts=trace.hosts,
                inputs=inputs,
                plan=invalid_plan,
                schema_version=WORKFLOW_STORE_SCHEMA_V3,
            )

        invalid_open = _action(
            {
                "name": "openPage",
                "url": "https://example.test/responsive",
                "viewport": {"width": invalid_width, "height": 620},
            },
            step=1,
            event_index=1,
            transaction_id=1,
            page="p1",
        )
        assert ElectronBrowserBridge._bounded_recording_event(invalid_open) is None
        invalid_open_plan = [dict(step) for step in plan]
        invalid_open_plan[0] = {
            **invalid_open_plan[0],
            "viewport": {"width": invalid_width, "height": 620},
        }
        with pytest.raises(WorkflowStoreError, match="workflow_resize_invalid"):
            build_workflow_artifact(
                owner=OWNER,
                hosts=trace.hosts,
                inputs=inputs,
                plan=invalid_open_plan,
                schema_version=WORKFLOW_STORE_SCHEMA_V3,
            )


def test_v11_signal_only_transactions_compile_to_waits(
    v11_gate: None,
    tmp_path: Path,
) -> None:
    rows = _bridge_rows(
        [
            _action(
                {"name": "openPage", "url": "https://example.test/"},
                step=1,
                event_index=1,
                transaction_id=201,
                page="p1",
            ),
            _signal(
                {"name": "popup", "popupPageGuid": "p2"},
                {
                    "openerPageGuid": "p1",
                    "popupIndex": 1,
                    "disposition": "foreground-tab",
                    "activate": True,
                },
                step=2,
                event_index=2,
                transaction_id=202,
                transaction_kind="observation",
                page="p1",
            ),
            _signal(
                {"name": "navigation", "url": "https://example.test/timer"},
                {},
                step=2,
                event_index=3,
                transaction_id=202,
                transaction_kind="observation",
                page="p2",
            ),
            _signal(
                {
                    "name": "x-crew-pageClosed",
                    "closedPageGuid": "p2",
                    "reason": "window.close",
                },
                {},
                step=3,
                event_index=4,
                transaction_id=203,
                transaction_kind="observation",
                page="p2",
            ),
        ]
    )
    trace = _read_trace(
        _write_trace(tmp_path / "trace.jsonl", rows),
        RECORDING_ID,
    )
    plan, _inputs = _compile_plan(
        {
            "schema_version": "crew.browser.workflow.v1",
            "steps": [{"source_step": 2}, {"source_step": 3}],
        },
        trace,
    )
    assert [step["kind"] for step in plan] == [
        "open_page",
        "wait_page",
        "wait_page_closed",
        "snapshot_full",
    ]
    assert plan[1]["effects"] == [
        {
            "kind": "navigation",
            "page": "p2",
            "url": "https://example.test/timer",
        }
    ]


def test_v11_initial_navigation_observation_remains_an_explicit_wait(
    v11_gate: None,
    tmp_path: Path,
) -> None:
    """The Host must journal the event across the openPage/wait RPC boundary."""
    rows = _bridge_rows(
        [
            _action(
                {"name": "openPage", "url": "https://example.test/start"},
                step=1,
                event_index=1,
                transaction_id=501,
                page="p0",
            ),
            _signal(
                {
                    "name": "navigation",
                    "url": "https://example.test/ready",
                },
                {},
                step=2,
                event_index=2,
                transaction_id=502,
                transaction_kind="observation",
                page="p0",
            ),
        ]
    )
    trace = _read_trace(
        _write_trace(tmp_path / "initial-navigation.jsonl", rows),
        RECORDING_ID,
    )
    plan, inputs = _compile_plan(
        {
            "schema_version": "crew.browser.workflow.v1",
            "steps": [{"source_step": 2}],
        },
        trace,
    )
    assert inputs == {}
    assert plan == [
        {
            "kind": "open_page",
            "page": "p0",
            "url": "https://example.test/start",
            "mode": "reuse_current",
            "activate": True,
            "effects": [],
        },
        {
            "kind": "wait_navigation",
            "page": "p0",
            "url": "https://example.test/ready",
            "effects": [],
        },
        {
            "kind": "snapshot_full",
            "page": "p0",
            "effects": [],
        },
    ]


def test_v11_group_ir_rejects_missing_action_and_event_gaps(
    v11_gate: None,
    tmp_path: Path,
) -> None:
    missing_action = _bridge_rows(
        [
            _signal(
                {"name": "navigation", "url": "https://example.test/"},
                {},
                step=1,
                event_index=1,
                transaction_id=401,
                transaction_kind="action",
                page="p1",
            )
        ]
    )
    with pytest.raises(
        WorkflowRejected,
        match="trace_transaction_shape_invalid",
    ):
        _read_trace(
            _write_trace(tmp_path / "missing-action.jsonl", missing_action),
            RECORDING_ID,
        )

    gap = _bridge_rows(
        [
            _action(
                {"name": "openPage", "url": "https://example.test/"},
                step=1,
                event_index=2,
                transaction_id=402,
                page="p1",
            )
        ]
    )
    with pytest.raises(
        WorkflowRejected,
        match="trace_event_indices_not_consecutive",
    ):
        _read_trace(
            _write_trace(tmp_path / "event-gap.jsonl", gap),
            RECORDING_ID,
        )


def test_v3_store_rejects_incomplete_lifecycle_and_is_off_by_default(
    v11_gate: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incomplete = [
        {
            "kind": "open_page",
            "page": "p1",
            "url": "https://example.test/",
            "mode": "reuse_current",
            "activate": True,
            "effects": [],
        },
        {"kind": "close_page", "page": "p1", "effects": []},
    ]
    with pytest.raises(WorkflowStoreError, match="workflow_close_page_invalid"):
        build_workflow_artifact(
            owner=OWNER,
            hosts=["example.test"],
            inputs={},
            plan=incomplete,
            schema_version=WORKFLOW_STORE_SCHEMA_V3,
        )

    use_after_close = [
        {
            "kind": "open_page",
            "page": "p1",
            "url": "https://example.test/",
            "mode": "reuse_current",
            "activate": True,
            "effects": [],
        },
        {
            "kind": "close_page",
            "page": "p1",
            "effects": [
                {
                    "kind": "page_closed",
                    "page": "p1",
                    "reason": "explicit",
                }
            ],
        },
        {
            "kind": "hover",
            "page": "p1",
            "selector": "css=.menu",
            "position": None,
            "effects": [],
        },
    ]
    with pytest.raises(WorkflowStoreError, match="workflow_page_closed"):
        build_workflow_artifact(
            owner=OWNER,
            hosts=["example.test"],
            inputs={},
            plan=use_after_close,
            schema_version=WORKFLOW_STORE_SCHEMA_V3,
        )

    v2 = build_workflow_artifact(
        owner=OWNER,
        hosts=["example.test"],
        inputs={},
        plan=[
            {"kind": "navigate", "url": "https://example.test/"},
            {"kind": "snapshot_full"},
        ],
    )
    assert v2.payload["schema_version"] == WORKFLOW_STORE_SCHEMA

    v3 = build_workflow_artifact(
        owner=OWNER,
        hosts=["example.test"],
        inputs={},
        plan=[
            {
                "kind": "open_page",
                "page": "p1",
                "url": "https://example.test/",
                "mode": "reuse_current",
                "activate": True,
                "effects": [],
            },
            {
                "kind": "snapshot_full",
                "page": "p1",
                "effects": [],
            },
        ],
        schema_version=WORKFLOW_STORE_SCHEMA_V3,
    )
    monkeypatch.setenv("CREW_BROWSER_RECORDING_V11_PHASE_A", "0")
    with pytest.raises(WorkflowStoreError, match="workflow_binding_invalid"):
        # The v3 bytes stay immutable; only acceptance is gated.
        _artifact_from_raw(OWNER, v3.workflow_id, v3.raw)


async def test_v11_draft_can_be_installed_as_v3_when_gate_is_enabled(
    v11_gate: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CREW_HOME", str(tmp_path))
    manager = BrowserManager(BrowserConfig())
    tools = RecordWorkflowTools(manager)
    session_id = "v11-install-session"
    directory = manager.recording_dir(OWNER, session_id, RECORDING_ID)
    directory.mkdir(parents=True, exist_ok=True)
    _write_trace(
        directory / "trace.jsonl",
        _bridge_rows(
            [
                _action(
                    {
                        "name": "openPage",
                        "url": "https://example.test/",
                    },
                    step=1,
                    event_index=1,
                    transaction_id=1,
                    page="p0",
                ),
                _action(
                    {
                        "name": "hover",
                        "selector": "css=#menu",
                        "position": None,
                    },
                    step=2,
                    event_index=2,
                    transaction_id=2,
                    page="p0",
                ),
            ]
        ),
    )
    tokens = [
        (current_owner_account_id, current_owner_account_id.set(OWNER)),
        (current_session_id, current_session_id.set(session_id)),
        (current_tool_call_id, current_tool_call_id.set("v11-install-call")),
    ]
    try:
        compiled = await tools.compile_handler(
            {
                "recording_id": RECORDING_ID,
                "slug": "v11-installed-flow",
                "workflow": {
                    "schema_version": "crew.browser.workflow.v1",
                    "steps": [{"source_step": 2}],
                },
            }
        )
        assert compiled.startswith("DRAFT_OK: ")
        public = json.loads(compiled.removeprefix("DRAFT_OK: "))
        install_args = {
            "recording_id": RECORDING_ID,
            "draft_id": public["draft_id"],
            "draft_digest": public["draft_digest"],
        }
        # 安装不弹确认：用户已经按了录制按钮、又点了生成技能，
        # 第三次询问只是打断同一个意图。范围摘要随结果回给模型用于汇报。
        assert tools.install_permission_resolver(install_args) is None
        installed = await tools.install_handler(install_args)
        assert installed.startswith("INSTALL_OK: ")
        installed_payload = json.loads(
            installed.removeprefix("INSTALL_OK: ")
        )
        artifact = read_workflow(
            OWNER,
            installed_payload["workflow_id"],
        )
        assert artifact.payload["schema_version"] == WORKFLOW_STORE_SCHEMA_V3
        assert [step["kind"] for step in artifact.payload["plan"]] == [
            "open_page",
            "hover",
            "snapshot_full",
        ]
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)
        await manager.aclose()
