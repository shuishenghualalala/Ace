"""Atomic replay.v3 Manager and transport contracts."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Sequence

import pytest

from crew.browser.driver import BrowserDriver, BrowserDriverError
from crew.browser.manager import BrowserManager
from crew.browser.types import BrowserConfig
from crew.core.runctx import (
    current_agent_workdir,
    current_owner_account_id,
    current_session_id,
    current_tool_call_id,
)
from plugins.browser.replay_tool import (
    RecordReplayTool,
    _resolve_inputs,
    _resolved_step,
)
from plugins.browser.workflow_store import (
    WORKFLOW_STORE_SCHEMA_V3,
    build_workflow_artifact,
    publish_workflow,
)

pytestmark = pytest.mark.asyncio

OWNER = "owner-replay-v3"
SESSION = "session-replay-v3"
WORKFLOW_ID = "a" * 64
WORKFLOW_DIGEST = "b" * 64
NONCE = "replay_v3_" + "c" * 32


class StrictAtomicDriver(BrowserDriver):
    """A Host fake that makes every legacy poll/action an immediate failure."""

    def __init__(self) -> None:
        self.capability_calls = 0
        self.transactions: list[dict[str, Any]] = []
        self.responses: list[dict[str, Any] | BrowserDriverError] = []
        self.capability_payload: dict[str, Any] = {
            "recordingEventSchemas": [10, 11],
            "replayArtifactSchemas": [
                "crew.browser.replay.v2",
                "crew.browser.replay.v3",
            ],
            "atomicReplayEffects": True,
        }

    def available(self) -> bool:
        return True

    async def execute(
        self,
        owner_session: str,
        profile_dir: Path,
        command: str,
        args: Sequence[str] = (),
        **_kwargs: Any,
    ) -> dict[str, Any]:
        del owner_session, profile_dir, command, args
        raise AssertionError(
            "replay.v3 must not issue execute/tab-list/url polling RPCs"
        )

    async def capabilities(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        self.capability_calls += 1
        return deepcopy(self.capability_payload)

    async def execute_transaction(
        self,
        _owner_session: str,
        _profile_dir: Path,
        transaction: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self.transactions.append(deepcopy(transaction))
        if not self.responses:
            raise AssertionError("unexpected replay transaction")
        response = self.responses.pop(0)
        if isinstance(response, BrowserDriverError):
            raise response
        return deepcopy(response)

    async def close(self, *_args: Any, **_kwargs: Any) -> bool:
        return True


def _response(
    effects: list[dict[str, Any]],
    *,
    bindings: list[tuple[str, str]] = (),
    active: str = "",
    closed: list[str] = (),
    downloads: list[dict[str, Any]] = (),
    snapshot: str | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "matchedEffects": deepcopy(effects),
        "pageBindings": [
            {"pageGuid": page, "targetId": target}
            for page, target in bindings
        ],
        "downloads": [
            {
                "path": f"/tmp/{item.get('alias', 'download')}",
                "state": "completed",
                "receivedBytes": 1,
                **deepcopy(item),
            }
            for item in downloads
        ],
        "activePageGuid": active,
        "closedPageGuids": list(closed),
    }
    if snapshot is not None:
        value["snapshot"] = snapshot
    return value


async def _begin(
    manager: BrowserManager,
    *,
    tool_call_id: str = "replay-v3-call",
) -> object:
    token = current_tool_call_id.set(tool_call_id)
    await manager.begin_replay(
        OWNER,
        SESSION,
        workflow_id=WORKFLOW_ID,
        workflow_digest=WORKFLOW_DIGEST,
        capability_generation=0,
        replay_nonce=NONCE,
        allowed_hosts=(),
        schema_version=WORKFLOW_STORE_SCHEMA_V3,
    )
    return token


async def _step(
    manager: BrowserManager,
    index: int,
    step: dict[str, Any],
) -> str:
    return await manager.replay_step(
        OWNER,
        SESSION,
        workflow_id=WORKFLOW_ID,
        workflow_digest=WORKFLOW_DIGEST,
        replay_nonce=NONCE,
        step_index=index,
        step=step,
    )


async def test_v3_topology_is_page_guid_bound_without_tab_or_url_polling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CREW_HOME", str(tmp_path))
    monkeypatch.delenv("CREW_BROWSER_RECORDING_V11_PHASE_A", raising=False)
    driver = StrictAtomicDriver()
    manager = BrowserManager(BrowserConfig(command_timeout_seconds=1), driver)
    popup_siblings = [
        {
            "kind": "popup",
            "page": "p1",
            "opener_page": "p0",
            "popup_index": 1,
            "activate": True,
            "disposition": "foreground-tab",
        },
        {
            "kind": "navigation",
            "page": "p1",
            "url": "https://same.example/item",
        },
        {
            "kind": "popup",
            "page": "p2",
            "opener_page": "p0",
            "popup_index": 2,
            "activate": False,
            "disposition": "background-tab",
        },
        {
            "kind": "navigation",
            "page": "p2",
            "url": "https://same.example/item",
        },
    ]
    nested = [
        {
            "kind": "popup",
            "page": "p3",
            "opener_page": "p1",
            "popup_index": 1,
            "activate": True,
            "disposition": "foreground-tab",
        }
    ]
    close_opener = [
        {"kind": "page_closed", "page": "p0", "reason": "explicit"}
    ]
    ephemeral = [
        {
            "kind": "popup",
            "page": "p4",
            "opener_page": "p3",
            "popup_index": 1,
            "activate": True,
            "disposition": "foreground-tab",
        },
        {
            "kind": "navigation",
            "page": "p4",
            "url": "https://ephemeral.example/",
        },
        {
            "kind": "page_closed",
            "page": "p4",
            "reason": "window.close",
        },
    ]
    timer_popup_effects = [
        {
            "kind": "navigation",
            "page": "p6",
            "url": "https://same.example/item",
        }
    ]
    driver.responses = [
        _response([], bindings=[("p0", "target-root")], active="p0"),
        _response(
            popup_siblings,
            bindings=[
                ("p1", "target-sibling-a"),
                ("p2", "target-sibling-b"),
            ],
            active="p1",
        ),
        _response(
            nested,
            bindings=[("p3", "target-nested")],
            active="p3",
        ),
        _response(close_opener, active="p3", closed=["p0"]),
        _response(
            ephemeral,
            bindings=[("p4", "target-ephemeral")],
            active="p3",
            closed=["p4"],
        ),
        _response(
            timer_popup_effects,
            bindings=[("p6", "target-timer-popup")],
            active="p6",
        ),
        _response([], bindings=[("p5", "target-manual-root")], active="p5"),
        _response([], active="p5", snapshot="final-v3-snapshot"),
    ]
    token = await _begin(manager)
    try:
        steps = [
            {
                "kind": "open_page",
                "page": "p0",
                "url": "https://root.example/",
                "mode": "reuse_current",
                "activate": True,
                "effects": [],
            },
            {
                "kind": "click",
                "page": "p0",
                "selector": "css=#siblings",
                "button": "left",
                "click_count": 1,
                "modifiers": [],
                "position": None,
                "effects": popup_siblings,
            },
            {
                "kind": "click",
                "page": "p1",
                "selector": "css=#nested",
                "button": "left",
                "click_count": 1,
                "modifiers": [],
                "position": None,
                "effects": nested,
            },
            {
                "kind": "close_page",
                "page": "p0",
                "effects": close_opener,
            },
            {
                "kind": "click",
                "page": "p3",
                "selector": "css=#ephemeral",
                "button": "left",
                "click_count": 1,
                "modifiers": [],
                "position": None,
                "effects": ephemeral,
            },
            {
                "kind": "wait_page",
                "page": "p6",
                "opener_page": "p3",
                "popup_index": 2,
                "activate": True,
                "disposition": "foreground-tab",
                "effects": timer_popup_effects,
            },
            {
                "kind": "open_page",
                "page": "p5",
                "url": "https://manual-root.example/",
                "mode": "new",
                "activate": True,
                "effects": [],
            },
            {
                "kind": "snapshot_full",
                "page": "p5",
                "effects": [],
            },
        ]
        observations = [
            await _step(manager, index, step)
            for index, step in enumerate(steps)
        ]
        assert observations[-1] == "final-v3-snapshot"
        assert driver.capability_calls == 1
        assert len(driver.transactions) == len(steps)
        assert [
            transaction["transactionId"]
            for transaction in driver.transactions
        ] == list(range(1, len(steps) + 1))
        assert (
            driver.transactions[2]["source"]
            == {"pageGuid": "p1", "targetId": "target-sibling-a"}
        )
        sibling_bindings = {
            page["pageGuid"]: page["targetId"]
            for page in driver.transactions[2]["knownPages"]
        }
        assert sibling_bindings["p1"] == "target-sibling-a"
        assert sibling_bindings["p2"] == "target-sibling-b"
        assert (
            driver.transactions[4]["source"]["targetId"]
            == "target-nested"
        )
        assert driver.transactions[5]["source"] == {
            "pageGuid": "p3",
            "targetId": "target-nested",
        }
        assert driver.transactions[5]["action"] == {
            "name": "x-crew-waitPopup",
            "popupPageGuid": "p6",
            "popupIndex": 2,
            "activate": True,
            "disposition": "foreground-tab",
        }
        # Closed opener and ephemeral popup remain tombstones and never return
        # to knownPages even though their immutable bindings are retained.
        final_known = {
            page["pageGuid"]
            for page in driver.transactions[-1]["knownPages"]
        }
        assert "p0" not in final_known
        assert "p4" not in final_known
        lease = manager._owners[OWNER].sessions[SESSION].active_replay
        assert lease is not None
        assert lease.page_targets == {
            "p0": "target-root",
            "p1": "target-sibling-a",
            "p2": "target-sibling-b",
            "p3": "target-nested",
            "p4": "target-ephemeral",
            "p5": "target-manual-root",
            "p6": "target-timer-popup",
        }
        assert lease.closed_pages == {"p0", "p4"}
    finally:
        await manager.end_replay(
            OWNER,
            SESSION,
            workflow_id=WORKFLOW_ID,
            workflow_digest=WORKFLOW_DIGEST,
            capability_generation=0,
            replay_nonce=NONCE,
            reason="completed",
        )
        current_tool_call_id.reset(token)
        await manager.aclose()


async def test_v3_waits_and_full_action_union_use_one_transaction_each(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CREW_HOME", str(tmp_path))
    monkeypatch.setenv("CREW_BROWSER_RECORDING_V11_PHASE_A", "1")
    driver = StrictAtomicDriver()
    manager = BrowserManager(BrowserConfig(command_timeout_seconds=1), driver)
    dialog_and_downloads = [
        {
            "kind": "dialog",
            "page": "p0",
            "alias": "dialog-1",
            "type": "confirm",
            "accept": True,
            "text": "",
        },
        {
            "kind": "dialog",
            "page": "p0",
            "alias": "dialog-2",
            "type": "confirm",
            "accept": True,
            "text": "",
        },
        {
            "kind": "download",
            "page": "p0",
            "alias": "download-1",
            "ordinal": 1,
            "suggested_filename": "same.pdf",
        },
        {
            "kind": "download",
            "page": "p0",
            "alias": "download-2",
            "ordinal": 2,
            "suggested_filename": "same.pdf",
        },
    ]
    steps = [
        {
            "kind": "open_page",
            "page": "p0",
            "url": "https://example.test/",
            "mode": "reuse_current",
            "activate": True,
            "viewport": {"width": 1024.5, "height": 720.25},
            "effects": [],
        },
        {
            "kind": "navigate",
            "page": "p0",
            "operation": "reload",
            "url": "",
            "effects": [],
        },
        {"kind": "activate_page", "page": "p0", "effects": []},
        {
            "kind": "resize",
            "page": "p0",
            "width": 963.5,
            "height": 707.25,
            "effects": [],
        },
        {
            "kind": "hover",
            "page": "p0",
            "selector": "css=#hover",
            "position": None,
            "effects": [],
        },
        {
            "kind": "click",
            "page": "p0",
            "selector": "css=#click",
            "button": "middle",
            "click_count": 1,
            "modifiers": ["Meta"],
            "position": {"x": 1.0, "y": 2.0},
            "effects": dialog_and_downloads,
        },
        {
            "kind": "dblclick",
            "page": "p0",
            "selector": "css=#double",
            "button": "left",
            "click_count": 2,
            "modifiers": [],
            "position": None,
            "effects": [],
        },
        {
            "kind": "fill",
            "page": "p0",
            "selector": "css=input",
            "text": "\ud800exact\x00value",
            "effects": [],
        },
        {
            "kind": "check",
            "page": "p0",
            "selector": "css=#toggle",
            "checked": False,
            "effects": [],
        },
        {
            "kind": "select",
            "page": "p0",
            "selector": "css=select",
            "options": [],
            "effects": [],
        },
        {
            "kind": "press",
            "page": "p0",
            "selector": "",
            "key": "Control+Enter",
            "effects": [],
        },
        {
            "kind": "upload",
            "page": "p0",
            "selector": "css=input[type=file]",
            "files": ["/tmp/\udfff-report.pdf"],
            "effects": [],
        },
        {
            "kind": "drag",
            "page": "p0",
            "source_selector": "css=#a",
            "target_selector": "css=#b",
            "source_position": None,
            "target_position": {"x": 5.0, "y": 6.0},
            "effects": [],
        },
        {
            "kind": "drop",
            "page": "p0",
            "selector": "css=#drop-zone",
            "files": ["/tmp/外部-a.txt", "/tmp/外部-b.txt"],
            "data": {
                "text/plain": "\ud800exact\x00value",
                "text/uri-list": "https://example.test/a?token=exact#fragment",
                "application/x-custom": "\x00exact\x01payload",
                "": "",
            },
            "effects": [],
        },
        {
            "kind": "pointer_gesture",
            "page": "p0",
            "selector": "css=#signature",
            "button": "right",
            "modifiers": ["Shift", "Alt"],
            "start": {"x": -1.25, "y": 2.5},
            "points": [
                {"x": 3.75, "y": 4.125, "elapsed_ms": 5.5},
                {"x": -2.0, "y": 8.0, "elapsed_ms": 12.0},
            ],
            "effects": [],
        },
        {
            "kind": "scroll",
            "page": "p0",
            "selector": "",
            "delta_x": -5,
            "delta_y": 10,
            "effects": [],
        },
        {
            "kind": "wait_navigation",
            "page": "p0",
            "url": "https://example.test/ready",
            "effects": [],
        },
        {
            "kind": "wait_download",
            "page": "p0",
            "alias": "timer-download",
            "ordinal": 3,
            "suggested_filename": "same.pdf",
            "effects": [],
        },
        {
            "kind": "wait_dialog",
            "page": "p0",
            "alias": "timer-dialog",
            "type": "prompt",
            "accept": True,
            "text": "answer",
            "effects": [],
        },
        {
            "kind": "snapshot_full",
            "page": "p0",
            "effects": [],
        },
        {
            "kind": "wait_page_closed",
            "page": "p0",
            "reason": "window.close",
            "effects": [],
        },
    ]
    driver.responses = [
        _response([], bindings=[("p0", "target-main")], active="p0"),
        *[
            _response(
                step["effects"],
                downloads=(
                    [
                        {
                            "alias": "download-1",
                            "pageGuid": "p0",
                            "ordinal": 1,
                            "suggestedFilename": "same.pdf",
                        },
                        {
                            "alias": "download-2",
                            "pageGuid": "p0",
                            "ordinal": 2,
                            "suggestedFilename": "same.pdf",
                        },
                    ]
                    if step["effects"] == dialog_and_downloads
                    else [
                        {
                            "alias": "timer-download",
                            "pageGuid": "p0",
                            "ordinal": 3,
                            "suggestedFilename": "same.pdf",
                        }
                    ]
                    if step["kind"] == "wait_download"
                    else []
                ),
                snapshot=(
                    "all-actions-final"
                    if step["kind"] == "snapshot_full"
                    else None
                ),
                closed=(
                    ["p0"]
                    if step["kind"] == "wait_page_closed"
                    else []
                ),
                active=(
                    ""
                    if step["kind"] == "wait_page_closed"
                    else "p0"
                ),
            )
            for step in steps[1:]
        ],
    ]
    token = await _begin(manager, tool_call_id="replay-v3-union")
    try:
        for index, step in enumerate(steps):
            await _step(manager, index, step)
        assert len(driver.transactions) == len(steps)
        names = [
            transaction["action"]["name"]
            for transaction in driver.transactions
        ]
        assert names == [
            "openPage",
            "x-crew-navigate",
            "x-crew-activatePage",
            "x-crew-resize",
            "hover",
            "click",
            "click",
            "fill",
            "uncheck",
            "select",
            "press",
            "setInputFiles",
            "x-crew-drag",
            "x-crew-drop",
            "x-crew-pointerGesture",
            "x-crew-scroll",
            "x-crew-waitNavigation",
            "x-crew-waitDownload",
            "x-crew-waitDialog",
            "x-crew-snapshot",
            "x-crew-waitPageClosed",
        ]
        assert driver.transactions[0]["action"] == {
            "name": "openPage",
            "url": "https://example.test/",
            "viewport": {"width": 1024.5, "height": 720.25},
        }
        compound = driver.transactions[5]
        assert compound["expectedEffects"] == dialog_and_downloads
        assert compound["action"]["clickCount"] == 1
        assert driver.transactions[7]["action"]["text"] == "\ud800exact\x00value"
        assert driver.transactions[9]["action"]["options"] == []
        assert driver.transactions[11]["action"]["files"] == [
            "/tmp/\udfff-report.pdf"
        ]
        assert driver.transactions[13]["action"] == {
            "name": "x-crew-drop",
            "selector": "css=#drop-zone",
            "files": ["/tmp/外部-a.txt", "/tmp/外部-b.txt"],
            "data": {
                "text/plain": "\ud800exact\x00value",
                "text/uri-list": "https://example.test/a?token=exact#fragment",
                "application/x-custom": "\x00exact\x01payload",
                "": "",
            },
        }
        assert driver.transactions[14]["action"] == {
            "name": "x-crew-pointerGesture",
            "selector": "css=#signature",
            "button": "right",
            "modifiers": ["Alt", "Shift"],
            "start": {"x": -1.25, "y": 2.5},
            "points": [
                {"x": 3.75, "y": 4.125, "elapsedMs": 5.5},
                {"x": -2.0, "y": 8.0, "elapsedMs": 12.0},
            ],
        }
    finally:
        await manager.end_replay(
            OWNER,
            SESSION,
            workflow_id=WORKFLOW_ID,
            workflow_digest=WORKFLOW_DIGEST,
            capability_generation=0,
            replay_nonce=NONCE,
            reason="completed",
        )
        current_tool_call_id.reset(token)
        await manager.aclose()


async def test_v3_pointer_gesture_manager_rejects_non_monotonic_points() -> None:
    with pytest.raises(BrowserDriverError) as raised:
        BrowserManager._replay_v3_action(
            {
                "kind": "pointer_gesture",
                "page": "p0",
                "selector": "css=#signature",
                "button": "left",
                "modifiers": [],
                "start": {"x": 1, "y": 2},
                "points": [
                    {"x": 3, "y": 4, "elapsed_ms": 10},
                    {"x": 5, "y": 6, "elapsed_ms": 9},
                ],
                "effects": [],
            }
        )
    assert raised.value.code == "replay_step_invalid"


async def test_v3_pen_samples_translate_to_strict_host_transaction() -> None:
    page, action, effects = BrowserManager._replay_v3_action(
        {
            "kind": "pointer_gesture",
            "page": "p0",
            "selector": "css=#pen-pad",
            "pointer_type": "pen",
            "button": "left",
            "modifiers": ["Shift"],
            "start": {
                "x": 1,
                "y": 2,
                "pressure": 0.25,
                "tangential_pressure": -0.4,
                "tilt_x": 11,
                "tilt_y": -12,
                "twist": 33,
                "width": 8,
                "height": 6,
            },
            "points": [
                {
                    "x": 3,
                    "y": 4,
                    "elapsed_ms": 5.5,
                    "pressure": 0.75,
                    "tangential_pressure": 0.2,
                    "tilt_x": 21,
                    "tilt_y": -22,
                    "twist": 44,
                    "width": 9,
                    "height": 7,
                },
                {
                    "x": 5,
                    "y": 6,
                    "elapsed_ms": 9,
                    "pressure": 0,
                },
            ],
            "effects": [],
        }
    )
    assert page == "p0"
    assert effects == []
    assert action == {
        "name": "x-crew-pointerGesture",
        "selector": "css=#pen-pad",
        "pointerType": "pen",
        "button": "left",
        "modifiers": ["Shift"],
        "start": {
            "x": 1.0,
            "y": 2.0,
            "pressure": 0.25,
            "tangentialPressure": -0.4,
            "tiltX": 11.0,
            "tiltY": -12.0,
            "twist": 33.0,
            "width": 8.0,
            "height": 6.0,
        },
        "points": [
            {
                "x": 3.0,
                "y": 4.0,
                "elapsedMs": 5.5,
                "pressure": 0.75,
                "tangentialPressure": 0.2,
                "tiltX": 21.0,
                "tiltY": -22.0,
                "twist": 44.0,
                "width": 9.0,
                "height": 7.0,
            },
            {
                "x": 5.0,
                "y": 6.0,
                "elapsedMs": 9.0,
                "pressure": 0.0,
            },
        ],
    }

    for invalid in (
        {
            "pointer_type": "touch",
            "button": "right",
        },
        {
            "pointer_type": "pen",
            "button": "left",
            "start": {"x": 1, "y": 2, "pressure": 1.01},
        },
    ):
        step = {
            "kind": "pointer_gesture",
            "page": "p0",
            "selector": "css=#pen-pad",
            "pointer_type": invalid["pointer_type"],
            "button": invalid["button"],
            "modifiers": [],
            "start": invalid.get("start", {"x": 1, "y": 2}),
            "points": [{"x": 3, "y": 4, "elapsed_ms": 1}],
            "effects": [],
        }
        with pytest.raises(BrowserDriverError) as raised:
            BrowserManager._replay_v3_action(step)
        assert raised.value.code == "replay_step_invalid"


@pytest.mark.parametrize("width", [True, float("inf"), float("nan")])
async def test_v3_resize_manager_rejects_non_finite_dimensions(width: object) -> None:
    with pytest.raises(BrowserDriverError) as raised:
        BrowserManager._replay_v3_action(
            {
                "kind": "resize",
                "page": "p0",
                "width": width,
                "height": 720,
                "effects": [],
            }
        )
    assert raised.value.code == "replay_step_invalid"

    with pytest.raises(BrowserDriverError) as open_raised:
        BrowserManager._replay_v3_action(
            {
                "kind": "open_page",
                "page": "p0",
                "url": "https://example.test/",
                "mode": "reuse_current",
                "activate": True,
                "viewport": {"width": width, "height": 720},
                "effects": [],
            }
        )
    assert open_raised.value.code == "replay_step_invalid"


async def test_v3_capability_handshake_refuses_non_atomic_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CREW_HOME", str(tmp_path))
    monkeypatch.setenv("CREW_BROWSER_RECORDING_V11_PHASE_A", "1")
    driver = StrictAtomicDriver()
    driver.capability_payload["atomicReplayEffects"] = False
    manager = BrowserManager(BrowserConfig(command_timeout_seconds=1), driver)
    token = current_tool_call_id.set("replay-v3-capability")
    try:
        with pytest.raises(BrowserDriverError) as raised:
            await manager.begin_replay(
                OWNER,
                SESSION,
                workflow_id=WORKFLOW_ID,
                workflow_digest=WORKFLOW_DIGEST,
                capability_generation=0,
                replay_nonce=NONCE,
                allowed_hosts=(),
                schema_version=WORKFLOW_STORE_SCHEMA_V3,
            )
        assert raised.value.code == "replay_v3_unsupported"
        assert driver.capability_calls == 1
        assert driver.transactions == []
    finally:
        current_tool_call_id.reset(token)
        await manager.aclose()


async def test_v3_closed_page_is_a_permanent_tombstone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CREW_HOME", str(tmp_path))
    monkeypatch.setenv("CREW_BROWSER_RECORDING_V11_PHASE_A", "1")
    driver = StrictAtomicDriver()
    manager = BrowserManager(BrowserConfig(command_timeout_seconds=1), driver)
    closed_effect = [
        {"kind": "page_closed", "page": "p0", "reason": "explicit"}
    ]
    driver.responses = [
        _response([], bindings=[("p0", "target-main")], active="p0"),
        _response(closed_effect, closed=["p0"]),
    ]
    token = await _begin(manager, tool_call_id="replay-v3-tombstone")
    try:
        await _step(
            manager,
            0,
            {
                "kind": "open_page",
                "page": "p0",
                "url": "https://example.test/",
                "mode": "reuse_current",
                "activate": True,
                "effects": [],
            },
        )
        await _step(
            manager,
            1,
            {
                "kind": "close_page",
                "page": "p0",
                "effects": closed_effect,
            },
        )
        with pytest.raises(BrowserDriverError) as raised:
            await _step(
                manager,
                2,
                {
                    "kind": "hover",
                    "page": "p0",
                    "selector": "css=body",
                    "position": None,
                    "effects": [],
                },
            )
        assert raised.value.code == "replay_page_closed"
        assert len(driver.transactions) == 2
    finally:
        # The failed step aborts the lease by design.
        current_tool_call_id.reset(token)
        await manager.aclose()


async def test_v3_runtime_input_overrides_preserve_exact_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CREW_BROWSER_RECORDING_V11_PHASE_A", "1")
    artifact = build_workflow_artifact(
        owner=OWNER,
        hosts=(),
        inputs={
            "text": {
                "kind": "text",
                "required": True,
                "display_name": "Text",
                "recorded_hint": "",
                "default": "recorded",
            },
            "options": {
                "kind": "select",
                "required": True,
                "display_name": "Options",
                "recorded_hint": "",
                "default": ["recorded"],
            },
            "files": {
                "kind": "files",
                "required": True,
                "display_name": "Files",
                "recorded_hint": "",
                "default": ["/tmp/recorded.txt"],
            },
        },
        plan=[
            {
                "kind": "open_page",
                "page": "p0",
                "url": "about:blank",
                "mode": "reuse_current",
                "activate": True,
                "effects": [],
            },
            {
                "kind": "fill",
                "page": "p0",
                "selector": "css=input",
                "input_key": "text",
                "effects": [],
            },
            {
                "kind": "select",
                "page": "p0",
                "selector": "css=select",
                "input_key": "options",
                "effects": [],
            },
            {
                "kind": "drop",
                "page": "p0",
                "selector": "css=#drop-zone",
                "input_key": "files",
                "data": {
                    "text/plain": "\ud800recorded\x00",
                    "": "",
                },
                "effects": [],
            },
            {
                "kind": "snapshot_full",
                "page": "p0",
                "effects": [],
            },
        ],
        schema_version=WORKFLOW_STORE_SCHEMA_V3,
    )
    resolved = _resolve_inputs(
        artifact,
        {
            "text": "\ud800override\x00",
            "options": ["", "\udfff", "same", "same"],
            "files": ["/tmp/\udfff-override.txt"],
        },
    )
    fill = _resolved_step(
        artifact.payload["plan"][1],
        resolved,
        schema_version=WORKFLOW_STORE_SCHEMA_V3,
    )
    select = _resolved_step(
        artifact.payload["plan"][2],
        resolved,
        schema_version=WORKFLOW_STORE_SCHEMA_V3,
    )
    drop = _resolved_step(
        artifact.payload["plan"][3],
        resolved,
        schema_version=WORKFLOW_STORE_SCHEMA_V3,
    )
    assert fill["text"] == "\ud800override\x00"
    assert fill["effects"] == []
    assert select["options"] == ["", "\udfff", "same", "same"]
    assert drop == {
        "kind": "drop",
        "page": "p0",
        "selector": "css=#drop-zone",
        "files": ["/tmp/\udfff-override.txt"],
        "data": {
            "text/plain": "\ud800recorded\x00",
            "": "",
        },
        "effects": [],
    }
    assert "input_key" not in fill
    assert "input_key" not in select
    assert "input_key" not in drop


async def test_record_replay_tool_executes_v3_end_to_end_with_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CREW_HOME", str(tmp_path))
    monkeypatch.setenv("CREW_BROWSER_RECORDING_V11_PHASE_A", "1")
    artifact = build_workflow_artifact(
        owner=OWNER,
        hosts=(),
        inputs={
            "text": {
                "kind": "text",
                "required": True,
                "display_name": "Text",
                "recorded_hint": "",
                "default": "recorded",
            },
            "options": {
                "kind": "select",
                "required": True,
                "display_name": "Options",
                "recorded_hint": "",
                "default": ["recorded"],
            },
            "files": {
                "kind": "files",
                "required": True,
                "display_name": "Files",
                "recorded_hint": "",
                "default": ["/tmp/recorded.pdf"],
            },
        },
        plan=[
            {
                "kind": "open_page",
                "page": "p0",
                "url": "about:blank",
                "mode": "reuse_current",
                "activate": True,
                "effects": [],
            },
            {
                "kind": "fill",
                "page": "p0",
                "selector": "css=input",
                "input_key": "text",
                "effects": [],
            },
            {
                "kind": "select",
                "page": "p0",
                "selector": "css=select",
                "input_key": "options",
                "effects": [],
            },
            {
                "kind": "upload",
                "page": "p0",
                "selector": "css=input[type=file]",
                "input_key": "files",
                "effects": [],
            },
            {
                "kind": "snapshot_full",
                "page": "p0",
                "effects": [],
            },
        ],
        schema_version=WORKFLOW_STORE_SCHEMA_V3,
    )
    publish_workflow(OWNER, artifact)
    driver = StrictAtomicDriver()
    driver.responses = [
        _response([], bindings=[("p0", "target-main")], active="p0"),
        _response([], active="p0"),
        _response([], active="p0"),
        _response([], active="p0"),
        _response([], active="p0", snapshot="tool-final-snapshot"),
    ]
    manager = BrowserManager(BrowserConfig(command_timeout_seconds=1), driver)
    tool = RecordReplayTool(manager)
    tokens = [
        (current_owner_account_id, current_owner_account_id.set(OWNER)),
        (current_session_id, current_session_id.set(SESSION)),
        (current_tool_call_id, current_tool_call_id.set("v3-tool-call")),
        (
            current_agent_workdir,
            current_agent_workdir.set(str(tmp_path)),
        ),
    ]
    try:
        result = await tool.handler(
            {
                "workflow_id": artifact.workflow_id,
                "inputs": {
                    "text": "\ud800override\x00",
                    "options": ["", "same", "same"],
                    # Official setInputFiles([]) clears the input; replay.v3
                    # must not force the recorded non-empty default.
                    "files": [],
                },
            }
        )
        assert result.startswith("REPLAY_OK: ")
        payload = json.loads(result.removeprefix("REPLAY_OK: "))
        assert payload == {
            "workflow_id": artifact.workflow_id,
            "executed_steps": 5,
            "takeover": False,
            "snapshot": "tool-final-snapshot",
        }
        assert driver.capability_calls == 1
        assert len(driver.transactions) == 5
        assert driver.transactions[1]["action"] == {
            "name": "fill",
            "selector": "css=input",
            "text": "\ud800override\x00",
        }
        assert driver.transactions[2]["action"]["options"] == [
            "",
            "same",
            "same",
        ]
        assert driver.transactions[3]["action"]["files"] == []
        session = manager._owners[OWNER].sessions[SESSION]
        assert session.active_replay is None
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)
        await manager.aclose()


async def test_record_replay_tool_preserves_partial_uncertain_v3_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CREW_HOME", str(tmp_path))
    monkeypatch.setenv("CREW_BROWSER_RECORDING_V11_PHASE_A", "1")
    artifact = build_workflow_artifact(
        owner=OWNER,
        hosts=(),
        inputs={},
        plan=[
            {
                "kind": "open_page",
                "page": "p0",
                "url": "about:blank",
                "mode": "reuse_current",
                "activate": True,
                "effects": [],
            },
            {
                "kind": "click",
                "page": "p0",
                "selector": "css=#submit",
                "button": "left",
                "click_count": 1,
                "modifiers": [],
                "position": None,
                "effects": [],
            },
            {
                "kind": "snapshot_full",
                "page": "p0",
                "effects": [],
            },
        ],
        schema_version=WORKFLOW_STORE_SCHEMA_V3,
    )
    publish_workflow(OWNER, artifact)
    driver = StrictAtomicDriver()
    driver.responses = [
        _response([], bindings=[("p0", "target-main")], active="p0"),
        BrowserDriverError(
            "transaction timeout",
            code="command_timeout",
            uncertain=True,
            partial=True,
        ),
    ]
    manager = BrowserManager(BrowserConfig(command_timeout_seconds=1), driver)
    tool = RecordReplayTool(manager)
    tokens = [
        (current_owner_account_id, current_owner_account_id.set(OWNER)),
        (current_session_id, current_session_id.set(SESSION)),
        (current_tool_call_id, current_tool_call_id.set("v3-failure-call")),
    ]
    try:
        result = await tool.handler(
            {"workflow_id": artifact.workflow_id, "inputs": {}}
        )
        assert result.startswith("REPLAY_PARTIAL: ")
        payload = json.loads(result.removeprefix("REPLAY_PARTIAL: "))
        assert payload["workflow_id"] == artifact.workflow_id
        assert payload["executed_steps"] == 1
        assert payload["outcome_uncertain"] is True
        assert payload["code"] == "command_timeout"
        assert len(driver.transactions) == 2
        assert manager._owners[OWNER].sessions[SESSION].active_replay is None
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)
        await manager.aclose()


async def test_v3_gate_off_rejects_before_capability_or_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CREW_HOME", str(tmp_path))
    monkeypatch.setenv("CREW_BROWSER_RECORDING_V11_PHASE_A", "1")
    artifact = build_workflow_artifact(
        owner=OWNER,
        hosts=(),
        inputs={},
        plan=[
            {
                "kind": "open_page",
                "page": "p0",
                "url": "about:blank",
                "mode": "reuse_current",
                "activate": True,
                "effects": [],
            },
            {
                "kind": "snapshot_full",
                "page": "p0",
                "effects": [],
            },
        ],
        schema_version=WORKFLOW_STORE_SCHEMA_V3,
    )
    publish_workflow(OWNER, artifact)
    monkeypatch.setenv("CREW_BROWSER_RECORDING_V11_PHASE_A", "0")
    driver = StrictAtomicDriver()
    manager = BrowserManager(BrowserConfig(command_timeout_seconds=1), driver)
    tool = RecordReplayTool(manager)
    tokens = [
        (current_owner_account_id, current_owner_account_id.set(OWNER)),
        (current_session_id, current_session_id.set(SESSION)),
        (current_tool_call_id, current_tool_call_id.set("v3-gate-off")),
    ]
    try:
        assert await tool.handler(
            {"workflow_id": artifact.workflow_id, "inputs": {}}
        ) == "REPLAY_REJECTED: workflow_unavailable"
        assert driver.capability_calls == 0
        assert driver.transactions == []
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)
        await manager.aclose()


async def test_v3_download_ordinal_restarts_for_each_replay_on_same_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recorded ordinals are replay-relative, never owner-target lifetime."""
    monkeypatch.setenv("CREW_HOME", str(tmp_path))
    monkeypatch.setenv("CREW_BROWSER_RECORDING_V11_PHASE_A", "1")
    driver = StrictAtomicDriver()
    manager = BrowserManager(BrowserConfig(command_timeout_seconds=1), driver)
    driver.responses = [
        _response([], bindings=[("p0", "same-target")], active="p0"),
        _response(
            [],
            active="p0",
            downloads=[
                {
                    "alias": "first-run-download",
                    "pageGuid": "p0",
                    "ordinal": 1,
                    "suggestedFilename": "same.pdf",
                }
            ],
        ),
        _response([], bindings=[("p0", "same-target")], active="p0"),
        _response(
            [],
            active="p0",
            downloads=[
                {
                    "alias": "second-run-download",
                    "pageGuid": "p0",
                    "ordinal": 1,
                    "suggestedFilename": "same.pdf",
                }
            ],
        ),
    ]
    token = current_tool_call_id.set("v3-repeat-download")
    try:
        for run_index, alias in enumerate(
            ("first-run-download", "second-run-download")
        ):
            await manager.begin_replay(
                OWNER,
                SESSION,
                workflow_id=WORKFLOW_ID,
                workflow_digest=WORKFLOW_DIGEST,
                capability_generation=0,
                replay_nonce=f"repeat_{run_index}_" + "x" * 24,
                allowed_hosts=(),
                schema_version=WORKFLOW_STORE_SCHEMA_V3,
            )
            await manager.replay_step(
                OWNER,
                SESSION,
                workflow_id=WORKFLOW_ID,
                workflow_digest=WORKFLOW_DIGEST,
                replay_nonce=f"repeat_{run_index}_" + "x" * 24,
                step_index=0,
                step={
                    "kind": "open_page",
                    "page": "p0",
                    "url": "about:blank",
                    "mode": "reuse_current",
                    "activate": True,
                    "effects": [],
                },
            )
            await manager.replay_step(
                OWNER,
                SESSION,
                workflow_id=WORKFLOW_ID,
                workflow_digest=WORKFLOW_DIGEST,
                replay_nonce=f"repeat_{run_index}_" + "x" * 24,
                step_index=1,
                step={
                    "kind": "wait_download",
                    "page": "p0",
                    "alias": alias,
                    "ordinal": 1,
                    "suggested_filename": "same.pdf",
                    "effects": [],
                },
            )
            assert await manager.end_replay(
                OWNER,
                SESSION,
                workflow_id=WORKFLOW_ID,
                workflow_digest=WORKFLOW_DIGEST,
                capability_generation=0,
                replay_nonce=f"repeat_{run_index}_" + "x" * 24,
                reason="completed",
            )

        assert [
            transaction["transactionId"]
            for transaction in driver.transactions
        ] == [1, 2, 1, 2]
        download_actions = [
            transaction["action"]
            for transaction in driver.transactions
            if transaction["action"]["name"] == "x-crew-waitDownload"
        ]
        assert [action["ordinal"] for action in download_actions] == [1, 1]
        assert [action["alias"] for action in download_actions] == [
            "first-run-download",
            "second-run-download",
        ]
        assert [
            transaction["source"]["targetId"]
            for transaction in driver.transactions
            if transaction["action"]["name"] == "x-crew-waitDownload"
        ] == ["same-target", "same-target"]
    finally:
        current_tool_call_id.reset(token)
        await manager.aclose()
