"""Functional contracts for recording -> Workflow IR compilation."""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from crew.browser.manager import BrowserManager
from crew.browser.types import BrowserConfig
from crew.core.runctx import (
    current_owner_account_id,
    current_session_id,
    current_tool_call_id,
)
from crew.gateway.ws import _apply_browser_skill_policy
from plugins.browser.compile_tool import (
    COMPILE_SCHEMA,
    INSTALL_SCHEMA,
    WORKFLOW_SCHEMA_VERSION,
    register_record_compile_tool,
)
from plugins.browser.workflow_store import WorkflowStoreError, read_workflow

OWNER = "owner-a"
SESSION = "session-1"
RECORDING = "aaaa1111"


class _Ctx:
    def __init__(self) -> None:
        self.tools: dict[str, dict] = {}

    def register_tool(self, *, name: str, handler, **kwargs) -> None:
        self.tools[name] = {"handler": handler, **kwargs}


@pytest.fixture
def compile_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path))
    manager = BrowserManager(BrowserConfig())
    ctx = _Ctx()
    capability = {"denied": ""}
    register_record_compile_tool(
        ctx,
        manager,
        capability_check=lambda: capability["denied"] or None,
    )
    tokens = [
        (current_owner_account_id, current_owner_account_id.set(OWNER)),
        (current_session_id, current_session_id.set(SESSION)),
        (current_tool_call_id, current_tool_call_id.set("install-call-1")),
    ]

    def write_trace(
        recording_id: str,
        records: list[dict],
        *,
        owner: str = OWNER,
        session_id: str = SESSION,
    ) -> Path:
        directory = manager.recording_dir(owner, session_id, recording_id)
        directory.mkdir(parents=True, exist_ok=True)
        trace = directory / "trace.jsonl"
        trace.write_text(
            "".join(
                json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                + "\n"
                for record in records
            ),
            encoding="utf-8",
        )
        trace.chmod(0o600)
        return trace

    try:
        yield {
            "ctx": ctx,
            "manager": manager,
            "write_trace": write_trace,
            "compile": ctx.tools["record_compile"]["handler"],
            "install": ctx.tools["record_install"]["handler"],
            "resolver": ctx.tools["record_install"]["permission_resolver"],
            "approver": ctx.tools["record_install"]["permission_approver"],
            "capability": capability,
        }
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


def _record(
    step: int,
    action: str = "navigate",
    *,
    url: str = "https://oa.example/list",
    selector: str = "",
    tier: str = "plain",
    value: str = "",
    value_truncated: bool = False,
    key: str = "",
    href: str = "",
    scroll_x: int = 0,
    scroll_y: int = 0,
    tag: str = "",
    input_type: str = "",
    content_editable: bool = False,
) -> dict:
    sensitive = tier in {"secret", "handoff"}
    target_needed = bool(
        tag
        or href
        or action in {"click", "key", "input", "scroll", "submit"}
        and selector
    )
    target = (
        {
            "tag": tag or ("button" if action == "click" else "input"),
            "text": "Recorded target",
            "ariaLabel": "",
            "href": href,
            "ordinal": 1,
            "id": "",
            "name": "",
            "role": "",
            "inputType": input_type,
            "testId": "",
            "testIdAttribute": "",
            "cssPath": tag or "button",
            "framePath": [],
            "contentEditable": content_editable,
        }
        if target_needed and not sensitive
        else None
    )
    host_generated = action in {"navigate", "dialog", "limit"}
    return {
        "schemaVersion": 3,
        "recordingId": RECORDING,
        "label": "page",
        "step": step,
        "action": action,
        "url": "" if sensitive else url,
        "hint": f"<{tier} field>" if sensitive else "",
        "target": target,
        "tier": tier,
        "value": "" if sensitive else value,
        "valueTruncated": False if sensitive else value_truncated,
        "key": "" if sensitive else key,
        "scrollX": scroll_x,
        "scrollY": scroll_y,
        "backendNodeId": 0,
        "timestamp": step,
        "selector": "" if sensitive else selector,
        "page": "",
        "pageTruncated": False,
        "page_dropped": False,
        "provenance": {
            "schemaVersion": 1,
            "source": "host-navigation" if host_generated else "document-world",
            "capturePhase": "host" if host_generated else "event-callback",
            "browserTrusted": not host_generated,
            "targetEvidence": (
                "redacted"
                if sensitive
                else ("synchronous" if target is not None else "none")
            ),
            "nativeInput": "host" if host_generated else "unverified",
            "transport": "authenticated-electron-host",
        },
    }


def _upload_record(
    step: int,
    *,
    mode: str,
    selector: str = "#attachment",
    paths: list[str] | None = None,
    file_count: int | None = None,
    multiple: bool = False,
    accept: str = ".pdf",
    url: str = "https://oa.example/form",
) -> dict:
    selected_paths = list(paths or [])
    record = _record(
        step,
        "upload",
        url=url,
        selector=selector,
        tag="input",
        input_type="file",
    )
    record.update(
        {
            "schemaVersion": 5,
            "dragTarget": None,
            "targetSelector": "",
            "clickButton": "",
            "clickCount": 0,
            "modifiers": [],
            "uploadMode": mode,
            "paths": selected_paths,
            "fileCount": (
                len(selected_paths)
                if file_count is None
                else file_count
            ),
            "multiple": multiple,
            "accept": accept,
        }
    )
    return record


def _as_v5(record: dict) -> dict:
    converted = dict(record)
    action = converted["action"]
    converted.update(
        {
            "schemaVersion": 5,
            "dragTarget": converted.get("dragTarget"),
            "targetSelector": converted.get("targetSelector", ""),
            "clickButton": "left" if action in {"click", "dblclick"} else "",
            "clickCount": (
                2 if action == "dblclick" else 1 if action == "click" else 0
            ),
            "modifiers": [],
            "uploadMode": "",
            "paths": [],
            "fileCount": 0,
            "multiple": False,
            "accept": "",
        }
    )
    return converted


def _as_v6(record: dict, *, values: list[str] | None = None) -> dict:
    converted = _as_v5(record)
    converted["schemaVersion"] = 6
    converted["values"] = list(values or [])
    return converted


def _as_v8(
    record: dict,
    *,
    values: list[str] | None = None,
    position: dict[str, float] | None = None,
) -> dict:
    converted = _as_v6(record, values=values)
    converted["schemaVersion"] = 8
    converted["position"] = position
    return converted


def _as_v9(
    record: dict,
    *,
    values: list[str] | None = None,
    position: dict[str, float] | None = None,
    causal_id: int = 0,
) -> dict:
    converted = _as_v8(record, values=values, position=position)
    converted["schemaVersion"] = 9
    converted["causalId"] = causal_id
    converted["dialogAction"] = ""
    converted["dialogType"] = ""
    converted["dialogText"] = ""
    return converted


def _as_v10(
    record: dict,
    *,
    values: list[str] | None = None,
    position: dict[str, float] | None = None,
    causal_id: int = 0,
    opener_page: str | None = None,
    popup_ordinal: int | None = None,
    created_by_causal_id: int | None = None,
) -> dict:
    converted = _as_v9(
        record,
        values=values,
        position=position,
        causal_id=causal_id,
    )
    converted["schemaVersion"] = 10
    if converted.get("label") == "page":
        converted["label"] = "p0"
    if opener_page is not None:
        converted["openerPage"] = opener_page
    if popup_ordinal is not None:
        converted["popupOrdinal"] = popup_ordinal
    if created_by_causal_id is not None:
        converted["createdByCausalId"] = created_by_causal_id
    return converted


def _compile_args(*steps: dict, slug: str = "recorded-workflow") -> dict:
    return {
        "recording_id": RECORDING,
        "slug": slug,
        "workflow": {
            "schema_version": WORKFLOW_SCHEMA_VERSION,
            "steps": list(steps or ({"source_step": 1},)),
        },
    }


def _payload(result: str, prefix: str) -> dict:
    assert result.startswith(prefix), result
    return json.loads(result[len(prefix) :])


async def _draft(
    env,
    records: list[dict],
    *steps: dict,
    slug: str = "recorded-workflow",
) -> tuple[dict, Path, dict]:
    env["write_trace"](RECORDING, records)
    public = _payload(
        await env["compile"](
            _compile_args(
                *(steps or ({"source_step": 1},)),
                slug=slug,
            )
        ),
        "DRAFT_OK: ",
    )
    directory = env["manager"].recording_dir(OWNER, SESSION, RECORDING)
    path = directory / ".workflow-drafts" / f"{public['draft_id']}.json"
    return public, path, json.loads(path.read_text("utf-8"))


def _install_args(public: dict) -> dict:
    return {
        "recording_id": RECORDING,
        "draft_id": public["draft_id"],
        "draft_digest": public["draft_digest"],
    }


def test_public_schemas_are_strict_and_versioned():
    compile_params = COMPILE_SCHEMA["parameters"]
    install_params = INSTALL_SCHEMA["parameters"]
    assert compile_params["additionalProperties"] is False
    assert install_params["additionalProperties"] is False
    assert (
        compile_params["properties"]["workflow"]["properties"]["schema_version"][
            "const"
        ]
        == WORKFLOW_SCHEMA_VERSION
    )
    Draft202012Validator.check_schema(compile_params)
    Draft202012Validator.check_schema(install_params)


async def test_button_and_spa_dynamic_clicks_replay_as_real_clicks(compile_env):
    records = [
        _record(1),
        _record(2, "click", selector="#load-results", tag="button"),
        _record(3, "click", selector="#dynamic-row-action", tag="button"),
    ]
    _public, _path, draft = await _draft(
        compile_env,
        records,
        *({"source_step": index} for index in range(1, 4)),
    )
    assert draft["plan"] == [
        {"kind": "navigate", "url": "https://oa.example/list"},
        {"kind": "click", "selector": "#load-results"},
        {"kind": "click", "selector": "#dynamic-row-action"},
        {"kind": "snapshot_full"},
    ]


async def test_double_click_and_drag_keep_both_stable_selectors(compile_env):
    drag = _record(3, "drag", selector="#card-a", tag="div")
    drag["targetSelector"] = "#column-done"
    drag["dragTarget"] = {
        **drag["target"],
        "text": "Done",
        "cssPath": "div:nth-of-type(2)",
    }
    _public, _path, draft = await _draft(
        compile_env,
        [
            _record(1),
            _record(2, "dblclick", selector="#open-row", tag="button"),
            drag,
        ],
        {"source_step": 1},
        {"source_step": 2},
        {"source_step": 3},
    )
    assert draft["plan"][1:3] == [
        {"kind": "dblclick", "selector": "#open-row"},
        {
            "kind": "drag",
            "source_selector": "#card-a",
            "target_selector": "#column-done",
        },
    ]


async def test_submitter_click_and_enter_are_not_takeovers(compile_env):
    records = [
        _record(1),
        _record(
            2,
            "input",
            selector="#query",
            value="recorded-but-parameterized",
            tag="input",
            input_type="search",
        ),
        _record(3, "key", selector="#query", key="Enter", tag="input"),
        # Transitional traces may contain an old submit row. It is ignored
        # because Enter/click is the actual trigger.
        _record(4, "submit", selector="#search-form", tag="form"),
        _record(5, "click", selector="#submit", tag="button"),
    ]
    _public, _path, draft = await _draft(
        compile_env,
        records,
        *({"source_step": index} for index in range(1, 6)),
    )
    assert [step["kind"] for step in draft["plan"]] == [
        "navigate",
        "fill_form",
        "press",
        "click",
        "snapshot_full",
    ]
    assert draft["plan"][2] == {
        "kind": "press",
        "selector": "#query",
        "key": "Enter",
    }
    assert all(step["kind"] != "takeover" for step in draft["plan"])


async def test_click_caused_host_navigation_is_observation_not_second_navigate(
    compile_env,
):
    records = [
        _record(1, url="https://app.example/start"),
        _record(
            2,
            "click",
            url="https://app.example/start",
            selector="#open-details",
            tag="button",
        ),
        _record(3, url="https://app.example/details?id=42#/summary"),
    ]
    _public, _path, draft = await _draft(
        compile_env,
        records,
        {"source_step": 1},
        {"source_step": 2},
        {"source_step": 3},
    )
    assert draft["plan"] == [
        {"kind": "navigate", "url": "https://app.example/start"},
        {
            "kind": "click",
            "selector": "#open-details",
            "postconditions": [
                {
                    "kind": "url",
                    "target": "same_tab",
                    "url": "https://app.example/details?id=42#/summary",
                }
            ],
        },
        {"kind": "snapshot_full"},
    ]


async def test_enter_caused_host_navigation_is_observation_not_second_navigate(
    compile_env,
):
    query = _record(
        2,
        "input",
        url="https://app.example/search",
        selector="#query",
        value="recorded-query",
        tag="input",
        input_type="search",
    )
    query["target"]["ariaLabel"] = "Query"
    records = [
        _record(1, url="https://app.example/search"),
        query,
        _record(
            3,
            "key",
            url="https://app.example/search",
            selector="#query",
            key="Enter",
            tag="input",
        ),
        _record(4, url="https://app.example/search?q=crew#/results"),
    ]
    _public, _path, draft = await _draft(
        compile_env,
        records,
        *({"source_step": index} for index in range(1, 5)),
    )
    assert [step["kind"] for step in draft["plan"]] == [
        "navigate",
        "fill_form",
        "press",
        "snapshot_full",
    ]
    assert draft["plan"][2] == {
        "kind": "press",
        "selector": "#query",
        "key": "Enter",
        "postconditions": [
            {
                "kind": "url",
                "target": "same_tab",
                "url": "https://app.example/search?q=crew#/results",
            }
        ],
    }


async def test_delayed_navigation_is_preserved_as_action_postcondition(
    compile_env,
):
    click = _record(
        2,
        "click",
        url="https://app.example/start",
        selector="#async-route",
        tag="button",
    )
    click["timestamp"] = 10_000
    legacy_submit_observation = _record(
        3,
        "submit",
        url="https://app.example/start",
    )
    legacy_submit_observation["timestamp"] = 12_000
    delayed_navigation = _record(
        4,
        url="https://app.example/ready?from=post#/done",
    )
    # Playwright RecorderSignalProcessor uses a five-second causal window.
    # Keep this beyond the old 2s/2.5s implementations and place a legacy
    # observation row in between to prove persistence latency is not adjacency.
    delayed_navigation["timestamp"] = 14_500
    _public, _path, draft = await _draft(
        compile_env,
        [
            _record(1, url="https://app.example/start"),
            click,
            legacy_submit_observation,
            delayed_navigation,
        ],
        {"source_step": 1},
        {"source_step": 2},
        {"source_step": 3},
        {"source_step": 4},
    )
    assert draft["plan"][1] == {
        "kind": "click",
        "selector": "#async-route",
        "postconditions": [
            {
                "kind": "url",
                "target": "same_tab",
                "url": "https://app.example/ready?from=post#/done",
            }
        ],
    }
    assert sum(step["kind"] == "navigate" for step in draft["plan"]) == 1


async def test_v10_interleaved_pages_bind_navigation_by_global_causal_id(
    compile_env,
):
    opener_action = _as_v10(
        _record(
            1,
            "click",
            url="https://app.example/opener",
            selector="#same-selector",
            tag="button",
        ),
        causal_id=701,
    )
    opener_action["label"] = "p0"
    popup_action = _as_v10(
        _record(
            2,
            "click",
            url="https://app.example/popup",
            selector="#same-selector",
            tag="button",
        ),
        causal_id=702,
        opener_page="p0",
        popup_ordinal=1,
        created_by_causal_id=700,
    )
    popup_action["label"] = "p1"
    delayed_opener_navigation = _as_v10(
        _record(3, url="https://app.example/opener/complete"),
        causal_id=701,
    )
    delayed_opener_navigation["label"] = "p0"
    delayed_opener_navigation["timestamp"] = 100_000
    popup_navigation = _as_v10(
        _record(4, url="https://app.example/popup/complete"),
        causal_id=702,
        opener_page="p0",
        popup_ordinal=1,
        created_by_causal_id=700,
    )
    popup_navigation["label"] = "p1"
    popup_navigation["timestamp"] = 200_000

    _public, _path, draft = await _draft(
        compile_env,
        [
            opener_action,
            popup_action,
            delayed_opener_navigation,
            popup_navigation,
        ],
        *({"source_step": index} for index in range(1, 5)),
        slug="interleaved-navigation-causality",
    )
    opener_step = next(
        step
        for step in draft["plan"]
        if step.get("page") == "p0" and step.get("kind") == "click"
    )
    popup_step = next(
        step
        for step in draft["plan"]
        if step.get("page") == "p1" and step.get("kind") == "click"
    )
    assert opener_step["postconditions"] == [
        {
            "kind": "url",
            "target": "same_tab",
            "url": "https://app.example/opener/complete",
        }
    ]
    assert popup_step["postconditions"] == [
        {
            "kind": "url",
            "target": "same_tab",
            "url": "https://app.example/popup/complete",
        }
    ]


async def test_v10_navigation_without_causal_id_is_never_time_guessed(
    compile_env,
):
    action = _as_v10(
        _record(
            1,
            "click",
            url="https://app.example/start",
            selector="#route-later",
            tag="button",
        ),
        causal_id=801,
    )
    explicit_navigation = _as_v10(
        _record(2, url="https://app.example/address-bar")
    )
    explicit_navigation["timestamp"] = action["timestamp"] + 1
    _public, _path, draft = await _draft(
        compile_env,
        [action, explicit_navigation],
        {"source_step": 1},
        {"source_step": 2},
        slug="v10-no-time-guess",
    )
    assert draft["plan"][:2] == [
        {"kind": "click", "selector": "#route-later"},
        {"kind": "navigate", "url": "https://app.example/address-bar"},
    ]


async def test_v10_rejects_duplicate_action_causal_id_across_pages(
    compile_env,
):
    first = _as_v10(
        _record(1, "click", selector="#first", tag="button"),
        causal_id=901,
    )
    first["label"] = "p0"
    second = _as_v10(
        _record(2, "click", selector="#second", tag="button"),
        causal_id=901,
    )
    second["label"] = "p1"
    navigation = _as_v10(
        _record(3, url="https://app.example/complete"),
        causal_id=901,
    )
    navigation["label"] = "p0"
    compile_env["write_trace"](RECORDING, [first, second, navigation])
    assert await compile_env["compile"](
        _compile_args(
            {"source_step": 1},
            {"source_step": 2},
            {"source_step": 3},
        )
    ) == "DRAFT_REJECTED: trace_causal_action_ambiguous"


async def test_legacy_navigation_window_fallback_is_page_scoped(
    compile_env,
):
    opener_action = _record(
        1,
        "click",
        selector="#same-selector",
        tag="button",
    )
    opener_action["label"] = "legacy-opener"
    other_page_action = _record(
        2,
        "click",
        selector="#same-selector",
        tag="button",
    )
    other_page_action["label"] = "legacy-other"
    opener_navigation = _record(
        3,
        url="https://app.example/opener-complete",
    )
    opener_navigation["label"] = "legacy-opener"
    _public, _path, draft = await _draft(
        compile_env,
        [opener_action, other_page_action, opener_navigation],
        {"source_step": 1},
        {"source_step": 2},
        {"source_step": 3},
        slug="legacy-page-scoped-navigation",
    )
    opener_step = next(
        step
        for step in draft["plan"]
        if step.get("page") == "p0" and step.get("kind") == "click"
    )
    other_step = next(
        step
        for step in draft["plan"]
        if step.get("page") == "p1" and step.get("kind") == "click"
    )
    assert opener_step["postconditions"] == [
        {
            "kind": "url",
            "target": "same_tab",
            "url": "https://app.example/opener-complete",
        }
    ]
    assert "postconditions" not in other_step


async def test_popup_navigation_postcondition_activates_recorded_popup(
    compile_env,
):
    opener = _as_v10(
        _record(
            2,
            "click",
            url="https://app.example/start",
            selector="#open-popup",
            tag="button",
        ),
        causal_id=101,
    )
    opener["label"] = "p0"
    popup_navigation = _as_v10(
        _record(
            3,
            url="https://app.example/popup?flow=oauth#/ready",
        ),
        opener_page="p0",
        popup_ordinal=1,
        created_by_causal_id=101,
    )
    popup_navigation["label"] = "p1"
    popup_action = _as_v10(
        _record(
            4,
            "click",
            url="https://app.example/popup?flow=oauth#/ready",
            selector="#continue-in-popup",
            tag="button",
        ),
        opener_page="p0",
        popup_ordinal=1,
        created_by_causal_id=101,
    )
    popup_action["label"] = "p1"
    _public, _path, draft = await _draft(
        compile_env,
        [
            _as_v10(_record(1, url="https://app.example/start")),
            opener,
            popup_navigation,
            popup_action,
        ],
        *({"source_step": index} for index in range(1, 5)),
    )
    assert draft["plan"][1] == {
        "kind": "click",
        "page": "p0",
        "selector": "#open-popup",
        "postconditions": [
            {
                "kind": "url",
                "target": "popup",
                "url": "https://app.example/popup?flow=oauth#/ready",
                "activate": True,
                "page": "p1",
                "opener_page": "p0",
                "popup_ordinal": 1,
            }
        ],
    }
    assert draft["plan"][2] == {
        "kind": "click",
        "page": "p1",
        "selector": "#continue-in-popup",
    }


async def test_middle_click_popup_postcondition_stays_in_background(
    compile_env,
):
    middle_click = _as_v10(
        _record(
            2,
            "click",
            url="https://app.example/start",
            selector="#background-report",
            tag="a",
        ),
        causal_id=202,
    )
    middle_click["label"] = "p0"
    middle_click.update(
        {
            "clickButton": "middle",
            "clickCount": 1,
            "modifiers": [],
        }
    )
    popup_navigation = _as_v10(
        _record(
            3,
            url="https://app.example/report#/ready",
        ),
        opener_page="p0",
        popup_ordinal=1,
        created_by_causal_id=202,
    )
    popup_navigation["label"] = "p1"
    _public, _path, draft = await _draft(
        compile_env,
        [
            _as_v10(_record(1, url="https://app.example/start")),
            middle_click,
            popup_navigation,
        ],
        *({"source_step": index} for index in range(1, 4)),
    )
    assert draft["plan"][1] == {
        "kind": "click",
        "page": "p0",
        "selector": "#background-report",
        "button": "middle",
        "click_count": 1,
        "modifiers": [],
        "postconditions": [
            {
                "kind": "url",
                "target": "popup",
                "url": "https://app.example/report#/ready",
                "activate": False,
                "page": "p1",
                "opener_page": "p0",
                "popup_ordinal": 1,
            }
        ],
    }


async def test_change_handler_navigation_ends_form_batch_with_postcondition(
    compile_env,
):
    department = _record(
        2,
        "input",
        url="https://app.example/form",
        selector="#department",
        value="engineering",
        tag="select",
        input_type="select-one",
    )
    department["target"]["ariaLabel"] = "Department"
    _public, _path, draft = await _draft(
        compile_env,
        [
            _record(1, url="https://app.example/form"),
            department,
            _record(3, url="https://app.example/team/engineering#/members"),
            _record(
                4,
                "click",
                url="https://app.example/team/engineering#/members",
                selector="#first-member",
                tag="button",
            ),
        ],
        *({"source_step": index} for index in range(1, 5)),
    )
    assert draft["plan"][1]["kind"] == "fill_form"
    assert draft["plan"][1]["postconditions"] == [
        {
            "kind": "url",
            "target": "same_tab",
            "origin": "https://app.example",
            "url_pattern": [
                {"literal": "https://app.example/team/"},
                {"input_key": "department", "encoding": "path"},
                {"literal": "#/members"},
            ],
        }
    ]
    assert draft["plan"][2] == {
        "kind": "click",
        "selector": "#first-member",
    }


async def test_parameterized_navigation_uses_runtime_value_and_wildcards_nonce(
    compile_env,
):
    query = _record(
        2,
        "input",
        url="https://app.example/search",
        selector="#query",
        value="old query",
        tag="input",
        input_type="search",
    )
    query["target"]["ariaLabel"] = "Query"
    enter = _record(
        3,
        "key",
        url="https://app.example/search",
        selector="#query",
        key="Enter",
        tag="input",
        input_type="search",
    )
    navigation = _record(
        4,
        url=(
            "https://app.example/search?q=old+query"
            "&state=A1B2C3D4E5F60708#/results"
        ),
    )
    navigation["timestamp"] = enter["timestamp"] + 500
    _public, path, draft = await _draft(
        compile_env,
        [_record(1, url="https://app.example/search"), query, enter, navigation],
        *({"source_step": index} for index in range(1, 5)),
        slug="parameterized-search-navigation",
    )
    assert draft["plan"][2] == {
        "kind": "press",
        "selector": "#query",
        "key": "Enter",
        "postconditions": [
            {
                "kind": "url",
                "target": "same_tab",
                "origin": "https://app.example",
                "url_pattern": [
                    {"literal": "https://app.example/search?q="},
                    {"input_key": "query", "encoding": "plus"},
                    {"literal": "&state="},
                    {"wildcard": "query_value"},
                    {"literal": "#/results"},
                ],
            }
        ],
    }
    serialized = path.read_text("utf-8")
    assert draft["inputs"]["query"]["default"] == "old query"
    assert "old+query" not in serialized
    assert "A1B2C3D4E5F60708" not in serialized


async def test_causal_navigation_is_coalesce_boundary_when_selector_repeats(
    compile_env,
):
    source_query = _record(
        2,
        "input",
        url="https://app.example/search",
        selector="#query",
        value="source",
        tag="input",
    )
    destination_query = _record(
        4,
        "input",
        url="https://app.example/results",
        selector="#query",
        value="destination",
        tag="input",
    )
    _public, _path, draft = await _draft(
        compile_env,
        [
            _record(1, url="https://app.example/search"),
            source_query,
            _record(3, url="https://app.example/results"),
            destination_query,
        ],
        *({"source_step": index} for index in range(1, 5)),
    )
    form_steps = [step for step in draft["plan"] if step["kind"] == "fill_form"]
    assert len(form_steps) == 2
    assert form_steps[0]["fields"][0]["selector"] == "#query"
    assert form_steps[0]["postconditions"] == [
        {
            "kind": "url",
            "target": "same_tab",
            "url": "https://app.example/results",
        }
    ]
    assert form_steps[1]["fields"][0]["selector"] == "#query"


async def test_pure_address_navigation_remains_an_explicit_action(compile_env):
    records = [
        _record(1, url="https://app.example/start"),
        _record(2, url="https://app.example/address-bar-destination?tab=2#main"),
    ]
    _public, _path, draft = await _draft(
        compile_env,
        records,
        {"source_step": 1},
        {"source_step": 2},
    )
    assert draft["plan"][:2] == [
        {"kind": "navigate", "url": "https://app.example/start"},
        {
            "kind": "navigate",
            "url": "https://app.example/address-bar-destination?tab=2#main",
        },
    ]


async def test_consecutive_form_controls_compile_to_one_typed_batch(compile_env):
    recorded_values = {
        "plain text",
        "rich text",
        "engineering",
        "recorded-slider-private",
    }
    records = [
        _record(1),
        _record(
            2,
            "input",
            selector="#name",
            value="plain text",
            tag="input",
            input_type="text",
        ),
        _record(
            3,
            "input",
            selector="#notes",
            value="rich text",
            tag="div",
            content_editable=True,
        ),
        _record(
            4,
            "input",
            selector="#department",
            value="engineering",
            tag="select",
            input_type="select-one",
        ),
        _record(
            5,
            "input",
            selector="#enabled",
            value="checked",
            tag="input",
            input_type="checkbox",
        ),
        _record(
            6,
            "input",
            selector="#priority",
            value="recorded-slider-private",
            tag="input",
            input_type="range",
        ),
    ]
    _public, _path, draft = await _draft(
        compile_env,
        records,
        *({"source_step": index} for index in range(1, 7)),
    )
    batch = draft["plan"][1]
    assert batch["kind"] == "fill_form"
    assert [field["type"] for field in batch["fields"]] == [
        "textbox",
        "textbox",
        "combobox",
        "checkbox",
        "slider",
    ]
    assert len(batch["fields"]) == 5
    assert draft["plan"][-1] == {"kind": "snapshot_full"}
    assert all(step["kind"] != "takeover" for step in draft["plan"])
    defaults = set()
    for spec in draft["inputs"].values():
        default = spec.get("default")
        if isinstance(default, str):
            defaults.add(default)
        elif isinstance(default, list):
            defaults.update(default)
    assert defaults == recorded_values


async def test_v6_multi_select_compiles_to_native_playwright_select(compile_env):
    multiple = _as_v6(
        _record(
            2,
            "input",
            selector="#members",
            value="alpha",
            tag="select",
            input_type="select-multiple",
        ),
        values=["alpha", "gamma"],
    )
    multiple["target"]["ariaLabel"] = "Members"
    multiple["target"]["name"] = "members"
    _public, _path, draft = await _draft(
        compile_env,
        [_record(1), multiple],
        {"source_step": 1},
        {"source_step": 2},
    )

    assert draft["plan"][1] == {
        "kind": "select",
        "selector": "#members",
        "input_key": "members",
    }
    assert draft["inputs"]["members"] == {
        "kind": "select",
        "required": True,
        "display_name": "Members",
        "recorded_hint": "select · select-multiple · name=members",
        "default": ["alpha", "gamma"],
    }
    serialized = json.dumps(draft, ensure_ascii=False)
    assert "alpha" in serialized
    assert "gamma" in serialized


async def test_real_mouse_form_sequence_preserves_tab_and_coalesces_safe_stages(
    compile_env,
):
    username_click = _record(
        2,
        "click",
        selector="#username",
        tag="input",
        input_type="text",
    )
    username = _record(
        3,
        "input",
        selector="#username",
        value="recorded-user",
        tag="input",
        input_type="text",
    )
    country_click = _record(
        4,
        "click",
        selector="#country",
        tag="select",
        input_type="select-one",
    )
    country = _record(
        5,
        "input",
        selector="#country",
        value="cn",
        tag="select",
        input_type="select-one",
    )
    remark = _record(
        7,
        "input",
        selector="#remark",
        value="recorded-remark",
        tag="textarea",
    )
    toggle_click = _record(
        8,
        "click",
        selector="#active",
        tag="input",
        input_type="checkbox",
    )
    toggle = _record(
        9,
        "input",
        selector="#active",
        value="checked",
        tag="input",
        input_type="checkbox",
    )
    for record, label, name in (
        (username_click, "Username", "username"),
        (username, "Username", "username"),
        (country_click, "Country", "country"),
        (country, "Country", "country"),
        (remark, "Remark", "remark"),
        (toggle_click, "Active", "active"),
        (toggle, "Active", "active"),
    ):
        record["target"]["ariaLabel"] = label
        record["target"]["name"] = name

    records = [
        _record(1),
        username_click,
        username,
        country_click,
        country,
        _record(6, "key", selector="#country", key="Tab", tag="select"),
        remark,
        toggle_click,
        toggle,
        _record(10, "click", selector="#save", tag="button"),
    ]
    _public, _path, draft = await _draft(
        compile_env,
        records,
        *({"source_step": index} for index in range(1, 11)),
    )

    assert [step["kind"] for step in draft["plan"]] == [
        "navigate",
        "fill_form",
        "press",
        "fill_form",
        "click",
        "snapshot_full",
    ]
    batch = draft["plan"][1]
    assert [field["type"] for field in batch["fields"]] == [
        "textbox",
        "combobox",
    ]
    assert draft["plan"][2] == {
        "kind": "press",
        "selector": "#country",
        "key": "Tab",
    }
    second_batch = draft["plan"][3]
    assert [field["type"] for field in second_batch["fields"]] == [
        "textbox",
        "checkbox",
    ]
    assert [field.get("input_key") for field in batch["fields"]] == [
        "username",
        "country",
    ]
    assert [field.get("input_key") for field in second_batch["fields"]] == [
        "remark",
        None,
    ]
    assert draft["inputs"] == {
        "country": {
            "kind": "select",
            "required": True,
            "display_name": "Country",
            "recorded_hint": "select · select-one · name=country",
            "default": ["cn"],
        },
        "remark": {
            "kind": "text",
            "required": True,
            "display_name": "Remark",
            "recorded_hint": "textarea · name=remark",
            "default": "recorded-remark",
        },
        "username": {
            "kind": "text",
            "required": True,
            "display_name": "Username",
            "recorded_hint": "input · text · name=username",
            "default": "recorded-user",
        },
    }
    assert draft["plan"][4] == {"kind": "click", "selector": "#save"}


async def test_text_editing_keys_and_repeated_corrections_are_last_write_wins(
    compile_env,
):
    first_draft = _record(
        2,
        "input",
        selector="#username",
        value="first-draft",
        tag="input",
        input_type="text",
    )
    second_draft = _record(
        4,
        "input",
        selector="#username",
        value="second-draft",
        tag="input",
        input_type="text",
    )
    country = _record(
        5,
        "input",
        selector="#country",
        value="cn",
        tag="select",
        input_type="select-one",
    )
    final_username = _record(
        6,
        "input",
        selector="#username",
        value="final-value",
        tag="input",
        input_type="text",
    )
    for record, label in (
        (first_draft, "Username"),
        (second_draft, "Username"),
        (country, "Country"),
        (final_username, "Username"),
    ):
        record["target"]["ariaLabel"] = label
    records = [
        _record(1),
        first_draft,
        _record(
            3,
            "key",
            selector="#username",
            key="Delete",
            tag="input",
            input_type="text",
        ),
        second_draft,
        country,
        final_username,
    ]
    _public, _path, draft = await _draft(
        compile_env,
        records,
        *({"source_step": index} for index in range(1, 7)),
    )
    assert [step["kind"] for step in draft["plan"]] == [
        "navigate",
        "fill_form",
        "snapshot_full",
    ]
    batch = draft["plan"][1]
    assert [field["selector"] for field in batch["fields"]] == [
        "#username",
        "#country",
    ]
    assert [field["input_key"] for field in batch["fields"]] == [
        "username",
        "country",
    ]
    assert set(draft["inputs"]) == {"country", "username"}
    serialized = json.dumps(draft, ensure_ascii=False)
    assert draft["inputs"]["username"]["default"] == "final-value"
    assert draft["inputs"]["country"]["default"] == ["cn"]
    assert "first-draft" not in serialized
    assert "second-draft" not in serialized


async def test_immediate_trusted_input_updates_compile_to_one_final_value(
    compile_env,
):
    records = []
    for step, value in enumerate(("a", "ab", "abc"), start=1):
        update = _as_v10(
            _record(
                step,
                "input",
                selector="#navigate-on-input",
                value=value,
                tag="input",
                input_type="text",
            ),
            causal_id=step,
        )
        update["label"] = "p0"
        records.append(update)

    _public, _path, draft = await _draft(
        compile_env,
        records,
        *({"source_step": step} for step in range(1, 4)),
        slug="immediate-input-updates",
    )
    assert draft["plan"] == [
        {
            "kind": "fill_form",
            "fields": [
                {
                    "type": "textbox",
                    "selector": "#navigate-on-input",
                    "input_key": "recorded_target",
                }
            ],
        },
        {"kind": "snapshot_full"},
    ]
    assert draft["inputs"]["recorded_target"]["default"] == "abc"


async def test_empty_textbox_click_stays_without_input_but_adjacent_edit_replaces_it(
    compile_env,
):
    click = _as_v10(
        _record(
            1,
            "click",
            selector="#empty-textbox",
            tag="input",
            input_type="text",
        ),
        causal_id=41,
    )
    click["label"] = "p0"
    _public, _path, click_only = await _draft(
        compile_env,
        [click],
        {"source_step": 1},
        slug="empty-textbox-click",
    )
    assert click_only["plan"][0] == {
        "kind": "click",
        "selector": "#empty-textbox",
    }

    edited = _as_v10(
        _record(
            2,
            "input",
            selector="#empty-textbox",
            value="typed",
            tag="input",
            input_type="text",
        ),
        causal_id=42,
    )
    edited["label"] = "p0"
    _public, _path, click_then_input = await _draft(
        compile_env,
        [click, edited],
        {"source_step": 1},
        {"source_step": 2},
        slug="empty-textbox-edited",
    )
    assert all(
        step["kind"] != "click" for step in click_then_input["plan"]
    )
    assert click_then_input["plan"][0]["kind"] == "fill_form"
    assert click_then_input["inputs"]["recorded_target"]["default"] == "typed"


async def test_page_and_selector_jointly_define_every_input_coalesce(
    compile_env,
):
    opener_click = _as_v10(
        _record(1, "click", selector="#shared", tag="input"),
        causal_id=11,
    )
    opener_click["label"] = "p0"
    popup_input = _as_v10(
        _record(
            2,
            "input",
            selector="#shared",
            value="popup-before-key",
            tag="input",
            input_type="text",
        )
    )
    popup_input["label"] = "p1"
    opener_key = _as_v10(
        _record(
            3,
            "key",
            selector="#shared",
            key="Delete",
            tag="input",
            input_type="text",
        )
    )
    opener_key["label"] = "p0"
    popup_after_key = _as_v10(
        _record(
            4,
            "input",
            selector="#shared",
            value="popup-after-key",
            tag="input",
            input_type="text",
        )
    )
    popup_after_key["label"] = "p1"
    opener_final = _as_v10(
        _record(
            5,
            "input",
            selector="#shared",
            value="opener-final",
            tag="input",
            input_type="text",
        )
    )
    opener_final["label"] = "p0"
    popup_final = _as_v10(
        _record(
            6,
            "input",
            selector="#shared",
            value="popup-final",
            tag="input",
            input_type="text",
        )
    )
    popup_final["label"] = "p1"

    _public, _path, draft = await _draft(
        compile_env,
        [
            opener_click,
            popup_input,
            opener_key,
            popup_after_key,
            opener_final,
            popup_final,
        ],
        *({"source_step": index} for index in range(1, 7)),
        slug="cross-page-shared-selector",
    )

    assert draft["plan"][0] == {
        "kind": "click",
        "selector": "#shared",
        "page": "p0",
    }
    assert {
        "kind": "press",
        "selector": "#shared",
        "key": "Delete",
        "page": "p0",
    } in draft["plan"]
    form_steps = [step for step in draft["plan"] if step["kind"] == "fill_form"]
    # The adjacent p0/p1 final edits use the same selector but remain separate
    # page-scoped actions rather than one selector-keyed last write.
    assert any(
        step.get("page") == "p0"
        and step["fields"][0]["selector"] == "#shared"
        for step in form_steps
    )
    assert any(
        step.get("page") == "p1"
        and step["fields"][0]["selector"] == "#shared"
        for step in form_steps
    )
    defaults = {
        spec["default"] for spec in draft["inputs"].values()
    }
    assert {"opener-final", "popup-final"} <= defaults


async def test_long_input_keys_keep_readable_prefix_stable_hash_and_64_limit(
    compile_env,
):
    common = "Customer account delegated approval contact " + "x" * 120
    records: list[dict] = []
    for step, (selector, label) in enumerate(
        (
            ("#primary", common + " alpha"),
            ("#secondary", common + " beta"),
            ("#duplicate", common + " alpha"),
        ),
        start=1,
    ):
        record = _as_v10(
            _record(
                step,
                "input",
                selector=selector,
                value=f"value-{step}",
                tag="input",
                input_type="text",
            )
        )
        record["target"]["ariaLabel"] = label
        records.append(record)

    _public, _path, first = await _draft(
        compile_env,
        records,
        *({"source_step": index} for index in range(1, 4)),
        slug="long-input-keys",
    )
    _public, _path, second = await _draft(
        compile_env,
        records,
        *({"source_step": index} for index in range(1, 4)),
        slug="long-input-keys",
    )
    keys = [
        field["input_key"]
        for field in first["plan"][0]["fields"]
    ]
    assert keys == [
        field["input_key"]
        for field in second["plan"][0]["fields"]
    ]
    assert len(set(keys)) == 3
    assert all(
        len(key) <= 64
        and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key)
        for key in keys
    )
    assert keys[0].startswith("customer_account_delegated_approval_contact_")
    assert keys[0][-12:].isalnum()
    assert keys[1] != keys[0]
    # Exact duplicate semantics retain the same stable digest and add a
    # bounded collision suffix without overflowing the replay schema.
    assert keys[2].endswith(f"{keys[0][-12:]}_2")


async def test_long_form_remains_one_complete_playwright_batch(compile_env):
    records = [_record(1)]
    for index in range(33):
        records.append(
            _record(
                index + 2,
                "input",
                selector=f"#field-{index}",
                value=f"value-{index}",
                tag="input",
                input_type="text",
            )
        )
    _public, _path, draft = await _draft(
        compile_env,
        records,
        *({"source_step": index} for index in range(1, 35)),
    )
    batches = [step for step in draft["plan"] if step["kind"] == "fill_form"]
    assert [len(step["fields"]) for step in batches] == [33]
    assert [field["selector"] for field in batches[0]["fields"]] == [
        f"#field-{index}" for index in range(33)
    ]
    assert len(draft["inputs"]) == 33


async def test_navigation_preserves_query_hash_and_multiple_hosts(compile_env):
    records = [
        _record(
            1,
            url=(
                "https://id.example/login?client=crew&access_token=private"
                "&signature=a%2Fb+%2B"
                "#/authorize?locale=zh-CN"
            ),
        ),
        _record(
            2,
            url=(
                "https://app.example/callback?code=business-code&view=grid"
                "#/home?tab=requests"
            ),
        ),
    ]
    _public, _path, draft = await _draft(
        compile_env,
        records,
        {"source_step": 1},
        {"source_step": 2},
    )
    assert draft["hosts"] == ["app.example", "id.example"]
    first, second = draft["plan"][:2]
    assert first["url"] == (
        "https://id.example/login?client=crew&access_token=private"
        "&signature=a%2Fb+%2B#/authorize?locale=zh-CN"
    )
    assert (
        second["url"]
        == "https://app.example/callback?code=business-code&view=grid"
        "#/home?tab=requests"
    )


async def test_recorded_hosts_are_unbounded_diagnostics_not_compile_capabilities(
    compile_env,
):
    records = [
        _record(
            index,
            url=f"https://tenant-{index:02d}.example/page",
        )
        for index in range(1, 34)
    ]
    _public, _path, draft = await _draft(
        compile_env,
        records,
        *({"source_step": index} for index in range(1, 34)),
    )
    assert len(draft["hosts"]) == 33
    assert draft["hosts"][0] == "tenant-01.example"
    assert draft["hosts"][-1] == "tenant-33.example"
    assert sum(step["kind"] == "navigate" for step in draft["plan"]) == 33


async def test_sensitive_only_recording_can_have_no_diagnostic_host(compile_env):
    _public, _path, draft = await _draft(
        compile_env,
        [_record(1, "input", tier="secret")],
        {"source_step": 1},
    )
    assert draft["hosts"] == []
    # 挂起型 takeover 之后会自动追加一次 snapshot_full：用户填完之后续跑，
    # 第一件该做的事就是重新看一眼落在了哪里。
    assert draft["plan"] == [
        {"kind": "takeover", "reason": "secret"},
        {"kind": "snapshot_full"},
    ]


async def test_scroll_keeps_container_and_exact_two_axis_delta(compile_env):
    _public, _path, draft = await _draft(
        compile_env,
        [
            _record(1),
            _record(
                2,
                "scroll",
                selector="#virtual-list",
                scroll_x=-240,
                scroll_y=1_375,
                tag="div",
            ),
        ],
        {"source_step": 1},
        {"source_step": 2},
    )
    assert draft["plan"][1] == {
        "kind": "scroll",
        "selector": "#virtual-list",
        "delta_x": -240,
        "delta_y": 1_375,
    }


@pytest.mark.parametrize("tier", ["secret", "handoff"])
async def test_only_sensitive_boundaries_compile_to_takeover(compile_env, tier):
    _public, _path, draft = await _draft(
        compile_env,
        [
            _record(1),
            _record(2, "input", tier=tier),
        ],
        {"source_step": 1},
        {"source_step": 2},
    )
    # takeover 是挂起点而不是终点，所以它在倒数第二位，末尾是续跑后的观察。
    assert draft["plan"][-2] == {"kind": "takeover", "reason": tier}
    assert draft["plan"][-1]["kind"] == "snapshot_full"


async def test_overlay_step_registers_a_handler_instead_of_a_positional_click(
    compile_env,
):
    """点「我知道了」应该变成处理器，而不是在固定位置点一次。

    弹窗什么时候来是不确定的：按固定位置插一步，回放时弹窗晚来一步就白点，
    而后面每个动作都会被遮罩吃掉。注册成处理器之后 Playwright 在**每次**
    actionability 检查前都会清它。
    """
    _public, _path, draft = await _draft(
        compile_env,
        [
            _record(1),
            _record(2, "click", selector="#announce-close", tag="button"),
            _record(3, "click", selector="#detail", tag="a"),
        ],
        {"source_step": 1},
        {"overlay_step": {"source_step": 2}},
        {"source_step": 3},
        slug="overlay-handler",
    )
    kinds = [step["kind"] for step in draft["plan"]]
    assert "handle_overlay" in kinds
    overlay = next(step for step in draft["plan"] if step["kind"] == "handle_overlay")
    assert overlay["selector"] == "#announce-close"
    # 它**不是**一次普通点击：计划里不该同时出现对同一元素的 click
    clicks = [
        step for step in draft["plan"]
        if step["kind"] == "click" and step.get("selector") == "#announce-close"
    ]
    assert clicks == []
    assert "handle_overlay" in draft["capabilities"]


async def test_overlay_step_rejects_model_supplied_selectors(compile_env):
    """遮挡处理器同样不接受模型自带的 selector。

    这一步会注册一个在整场回放里反复自动点击的处理器——如果模型能指定目标，
    页面正文里藏一句话就能让技能持续点击一个从未被演示过的元素。
    """
    compile_env["write_trace"](
        RECORDING,
        [_record(1), _record(2, "click", selector="#x", tag="button")],
    )
    for bad in (
        {"overlay_step": {"source_step": 2, "selector": "#evil"}},
        {"overlay_step": {"source_step": 0}},
        {"overlay_step": {}},
        {"overlay_step": {"source_step": 99}},
    ):
        result = await compile_env["compile"](
            _compile_args({"source_step": 1}, bad)
        )
        assert result.startswith("DRAFT_REJECTED: "), bad


async def test_assert_step_takes_its_selector_from_the_trace(compile_env):
    """断言目标来自轨迹，模型不能自己写 selector。

    这是整条编译链"注入面为零"的关键性质：模型的输入只有 step 序号和一个
    枚举值。断言若允许模型提供 selector，页面正文里藏一句话就能让技能去操作
    一个从未被演示过的元素。
    """
    _public, _path, draft = await _draft(
        compile_env,
        [
            _record(1),
            _record(2, "click", selector="#detail", tag="a"),
        ],
        {"source_step": 1},
        {"source_step": 2},
        {"assert_step": {"source_step": 2, "state": "visible"}},
        slug="assert-from-trace",
    )
    assert_steps = [step for step in draft["plan"] if step["kind"] == "assert_state"]
    assert len(assert_steps) == 1
    assert assert_steps[0]["selector"] == "#detail"
    assert assert_steps[0]["state"] == "visible"
    # 断言进入 capabilities，声明必须与 plan 精确一致
    assert "assert_state" in draft["capabilities"]


async def test_assert_step_can_repeat_a_source_step(compile_env):
    """断言天然要重复引用同一个元素：点之前确认可见、点之后确认消失。

    动作步骤不允许重复（会执行两次），断言允许——两者走的是不同的校验口径。
    """
    _public, _path, draft = await _draft(
        compile_env,
        [
            _record(1),
            _record(2, "click", selector="#detail", tag="a"),
        ],
        {"assert_step": {"source_step": 2, "state": "visible"}},
        {"source_step": 1},
        {"source_step": 2},
        {"assert_step": {"source_step": 2, "state": "hidden"}},
        slug="assert-repeat",
    )
    states = [
        step["state"] for step in draft["plan"] if step["kind"] == "assert_state"
    ]
    assert states == ["visible", "hidden"]


async def test_assert_step_rejects_unknown_state_and_model_selectors(compile_env):
    """非法状态与任何自带 selector 的形状都要在编译期就被拒。"""
    compile_env["write_trace"](
        RECORDING,
        [_record(1), _record(2, "click", selector="#detail", tag="a")],
    )
    for bad in (
        {"assert_step": {"source_step": 2, "state": "exists"}},
        {"assert_step": {"source_step": 2, "state": "visible", "selector": "#evil"}},
        {"assert_step": {"source_step": 0, "state": "visible"}},
        {"assert_step": {"state": "visible"}},
    ):
        result = await compile_env["compile"](
            _compile_args({"source_step": 1}, {"source_step": 2}, bad)
        )
        assert result.startswith("DRAFT_REJECTED: "), bad

    # 引用不存在的 trace step 同样拒绝
    result = await compile_env["compile"](
        _compile_args(
            {"source_step": 1},
            {"source_step": 2},
            {"assert_step": {"source_step": 99, "state": "visible"}},
        )
    )
    assert result == "DRAFT_REJECTED: source_step_not_found"


async def test_approval_scope_discloses_credential_fields(compile_env):
    """密码自动填必须是用户知情同意的，不能静默装进技能目录。

    用户授权了录制、授权了编译，都不等于授权"以后自动用我的密码登录"。安装是
    授权范围发生跃变的那一步，摘要必须把这件事说出来——而且只报字段数与显示名，
    不把凭据再抄一份到审批文案里。
    """
    credential = _as_v10(
        _record(
            1,
            "input",
            selector="#pwd",
            value="Tr0ub4dor&3",
            tag="input",
            input_type="text",
        )
    )
    credential["tier"] = "secret"
    credential["target"]["ariaLabel"] = "登录密码"
    public, _path, draft = await _draft(
        compile_env,
        [credential],
        {"source_step": 1},
        slug="discloses-credentials",
    )
    (spec,) = draft["inputs"].values()
    assert spec["credential"] is True

    # 安装不再弹确认（用户已经按了录制、又点了生成技能，第三次询问只是打断
    # 同一个意图）。范围摘要随安装结果回给模型，让它在对话里如实告诉用户
    # 装了什么——知情靠说清楚，不靠拦一下。
    assert compile_env["resolver"](_install_args(public)) is None
    installed = _payload(
        await compile_env["install"](_install_args(public)), "INSTALL_OK: "
    )
    assert "内含 1 个凭据字段的录制原值" in installed["scope"]
    assert "每次回放会自动填入" in installed["scope"]
    # 摘要里绝不能出现凭据本身
    assert "Tr0ub4dor" not in installed["scope"]


async def test_credential_flag_survives_install_into_the_artifact(compile_env):
    """凭据标记必须活着穿过 workflow_store。

    store 对 input spec 做严格字段校验，标记被静默丢掉的话，披露会永远
    报"不含凭据原值"——一个永远说"安全"的披露比没有披露更糟。
    """
    credential = _as_v10(
        _record(
            1,
            "input",
            selector="#pwd",
            value="s3cret-value",
            tag="input",
            input_type="text",
        )
    )
    credential["tier"] = "secret"
    public, _path, _unused = await _draft(
        compile_env,
        [credential],
        {"source_step": 1},
        slug="credential-flag-survives",
    )
    installed = _payload(
        await compile_env["install"](_install_args(public)), "INSTALL_OK: "
    )
    artifact = read_workflow(OWNER, installed["workflow_id"])
    specs = artifact.payload["inputs"]
    assert any(spec.get("credential") is True for spec in specs.values())


async def test_v10_secret_tier_is_executable_and_keeps_default(compile_env):
    """密码可复用，所以自动填是有效的。

    代价由两处承担：owner 私有 0600 存储，以及安装前把"内含 N 个凭据字段"
    明确写进审批摘要（见 test_approval_scope_discloses_credential_fields）。
    """
    complete = _as_v10(
        _record(
            1,
            "input",
            selector="#credential",
            value="exact recorded value",
            tag="input",
            input_type="text",
        )
    )
    complete["tier"] = "secret"
    _public, _path, draft = await _draft(
        compile_env,
        [complete],
        {"source_step": 1},
        slug="v10-secret-input",
    )
    assert draft["plan"][0]["kind"] == "fill_form"
    assert draft["inputs"]["recorded_target"]["default"] == (
        "exact recorded value"
    )
    assert all(step["kind"] != "takeover" for step in draft["plan"])


async def test_v10_handoff_tier_always_compiles_to_takeover(compile_env):
    """一次性凭据必须交还用户——自动填不是有风险，是必然失败。

    验证码用过即废。把录到的那一个码存成默认值再自动填，站点一定拒绝，而工作流
    不知道，会继续在登录页上跑完剩下的步骤，产出一串对不上任何元素的失败。
    这条同时也保证 artifact 里不会留下一个没人消费的入参。
    """
    complete = _as_v10(
        _record(
            1,
            "input",
            selector="#otp",
            value="482913",
            tag="input",
            input_type="text",
        )
    )
    complete["tier"] = "handoff"
    _public, _path, draft = await _draft(
        compile_env,
        [complete],
        {"source_step": 1},
        slug="v10-handoff-input",
    )
    assert draft["plan"][0] == {"kind": "takeover", "reason": "handoff"}
    # 录到的码既不进 plan 也不进 inputs
    assert draft["inputs"] == {}
    # 挂起之后仍有步骤：这正是「登录（填码）→ 继续读工单」得以成立的前提
    assert len(draft["plan"]) > 1
    assert "482913" not in json.dumps(draft, ensure_ascii=False)


@pytest.mark.parametrize(
    ("action", "kwargs", "reason"),
    [
        ("click", {}, "click_selector_missing"),
        ("input", {"tag": "input", "value": "x"}, "input_selector_missing"),
        ("key", {"key": ""}, "key_value_missing"),
    ],
)
async def test_executable_events_without_required_runtime_identity_are_rejected(
    compile_env,
    action,
    kwargs,
    reason,
):
    compile_env["write_trace"](RECORDING, [_record(1, action, **kwargs)])
    result = await compile_env["compile"](_compile_args({"source_step": 1}))
    assert result == f"DRAFT_REJECTED: {reason}"


async def test_recorder_v3_value_truncation_and_pre_field_migration(compile_env):
    current = _record(
        1,
        "input",
        selector="#large",
        value="x" * 4_096,
        value_truncated=True,
        tag="textarea",
    )
    _public, _path, draft = await _draft(
        compile_env,
        [current],
        {"source_step": 1},
    )
    assert draft["plan"][0]["kind"] == "fill_form"
    assert "x" * 128 not in json.dumps(draft)

    transitional = _record(
        1,
        "input",
        selector="#old",
        value="old",
        tag="input",
    )
    transitional.pop("valueTruncated")
    _public, _path, migrated = await _draft(
        compile_env,
        [transitional],
        {"source_step": 1},
    )
    assert migrated["plan"][0]["kind"] == "fill_form"


async def test_draft_is_owner_private_immutable_and_deterministic(compile_env):
    records = [_record(1)]
    first, path, payload = await _draft(
        compile_env,
        records,
        {"source_step": 1},
    )
    second, second_path, second_payload = await _draft(
        compile_env,
        records,
        {"source_step": 1},
    )
    assert first == second
    assert path == second_path
    assert payload == second_payload
    if os.name != "nt":
        assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600


async def test_strict_trace_shape_remains_but_values_are_not_size_rejected(
    compile_env,
):
    bad = {**_record(1), "page_owned_extra": "ignored?"}
    compile_env["write_trace"](RECORDING, [bad])
    assert await compile_env["compile"](
        _compile_args({"source_step": 1})
    ) == "DRAFT_REJECTED: trace_record_shape_invalid"

    oversized = _record(
        1,
        "input",
        selector="#field",
        value="x" * 4_097,
        tag="input",
    )
    _public, _path, draft = await _draft(
        compile_env,
        [oversized],
        {"source_step": 1},
    )
    assert draft["inputs"]["recorded_target"]["default"] == "x" * 4_097


async def test_incomplete_marker_and_missing_host_step_reject_compilation(compile_env):
    trace = compile_env["write_trace"](RECORDING, [_record(1), _record(2)])
    marker = trace.parent / "INCOMPLETE"
    marker.write_text("recording-incomplete\n", encoding="utf-8")
    marker.chmod(0o600)
    assert await compile_env["compile"](
        _compile_args({"source_step": 1}, {"source_step": 2})
    ) == "DRAFT_REJECTED: trace_incomplete"

    marker.unlink()
    compile_env["write_trace"](RECORDING, [_record(1), _record(3)])
    assert await compile_env["compile"](
        _compile_args({"source_step": 1}, {"source_step": 3})
    ) == "DRAFT_REJECTED: trace_steps_not_consecutive"

    compile_env["write_trace"](RECORDING, [_record(2)])
    assert await compile_env["compile"](
        _compile_args({"source_step": 2})
    ) == "DRAFT_REJECTED: trace_steps_not_consecutive"


async def test_recorder_v4_pointer_options_compile_to_exact_click(
    compile_env,
):
    click = _record(
        1,
        "click",
        selector="#open-in-new-tab",
        tag="button",
    )
    click.update(
        {
            "schemaVersion": 4,
            "dragTarget": None,
            "targetSelector": "",
            "clickButton": "middle",
            "clickCount": 37,
            "modifiers": ["Meta", "Shift"],
        }
    )
    _public, _path, draft = await _draft(
        compile_env,
        [click],
        {"source_step": 1},
        slug="exact-pointer-click",
    )
    assert draft["plan"][0] == {
        "kind": "click",
        "selector": "#open-in-new-tab",
        "button": "middle",
        "click_count": 37,
        "modifiers": ["Meta", "Shift"],
    }


async def test_recorder_v8_canvas_position_compiles_to_exact_locator_click(
    compile_env,
):
    click = _as_v8(
        _record(
            1,
            "click",
            selector="#chart",
            tag="canvas",
        ),
        position={"x": 127.5, "y": 42.25},
    )
    _public, _path, draft = await _draft(
        compile_env,
        [click],
        {"source_step": 1},
        slug="canvas-position-click",
    )
    assert draft["plan"][0] == {
        "kind": "click",
        "selector": "#chart",
        "button": "left",
        "click_count": 1,
        "modifiers": [],
        "position": {"x": 127.5, "y": 42.25},
    }


async def test_dialog_chain_is_armed_on_trigger_and_navigation_stays_causal(
    compile_env,
):
    click = _as_v9(
        _record(
            2,
            "click",
            url="https://app.example/start",
            selector="#dangerous-flow",
            tag="button",
        ),
        causal_id=42,
    )
    confirm = _as_v9(
        _record(3, "dialog", url="https://app.example/start"),
        causal_id=42,
    )
    confirm.update(
        {
            "dialogAction": "dismiss",
            "dialogType": "confirm",
            "dialogText": "",
        }
    )
    prompt = _as_v9(
        _record(4, "dialog", url="https://app.example/start"),
        causal_id=42,
    )
    prompt.update(
        {
            "dialogAction": "accept",
            "dialogType": "prompt",
            "dialogText": "",
        }
    )
    navigation = _as_v9(
        _record(5, "navigate", url="https://app.example/after-dialog")
    )
    _public, _path, draft = await _draft(
        compile_env,
        [
            _as_v9(_record(1, url="https://app.example/start")),
            click,
            confirm,
            prompt,
            navigation,
        ],
        *({"source_step": index} for index in range(1, 6)),
        slug="dialog-chain",
    )
    assert draft["plan"][1] == {
        "kind": "click",
        "selector": "#dangerous-flow",
        "dialogs": [
            {"type": "confirm", "accept": False, "text": ""},
            {"type": "prompt", "accept": True, "text": ""},
        ],
        "postconditions": [
            {
                "kind": "url",
                "target": "same_tab",
                "url": "https://app.example/after-dialog",
            }
        ],
    }
    assert all(step["kind"] != "dialog" for step in draft["plan"])


async def test_dialog_signal_is_hard_boundary_for_form_last_write_wins(
    compile_env,
):
    first = _as_v9(
        _record(
            1,
            "input",
            selector="#query",
            value="first",
            tag="input",
            input_type="text",
        ),
        causal_id=71,
    )
    dialog = _as_v9(_record(2, "dialog"), causal_id=71)
    dialog.update(
        {
            "dialogAction": "accept",
            "dialogType": "alert",
            "dialogText": "",
        }
    )
    second = _as_v9(
        _record(
            3,
            "input",
            selector="#query",
            value="second",
            tag="input",
            input_type="text",
        ),
        causal_id=72,
    )
    _public, _path, draft = await _draft(
        compile_env,
        [first, dialog, second],
        *({"source_step": index} for index in range(1, 4)),
        slug="dialog-form-boundary",
    )
    form_steps = [step for step in draft["plan"] if step["kind"] == "fill_form"]
    assert len(form_steps) == 2
    assert form_steps[0]["dialogs"] == [
        {"type": "alert", "accept": True, "text": ""}
    ]


async def test_dialog_uses_exact_group_causal_id_not_latest_action_or_time_window(
    compile_env,
):
    first = _as_v9(
        _record(2, "click", selector="#first", tag="button"),
        causal_id=11,
    )
    first["label"] = "opener-page"
    second = _as_v9(
        _record(3, "click", selector="#second", tag="button"),
        causal_id=12,
    )
    second["label"] = "opener-page"
    delayed = _as_v9(_record(4, "dialog"), causal_id=11)
    delayed.update(
        {
            "label": "popup-page",
            "timestamp": 10_000,
            "dialogAction": "accept",
            "dialogType": "alert",
            "dialogText": "",
        }
    )
    _public, _path, draft = await _draft(
        compile_env,
        [
            _as_v9(_record(1)),
            first,
            second,
            delayed,
        ],
        *({"source_step": index} for index in range(1, 5)),
        slug="exact-dialog-causal-owner",
    )
    first_step = next(
        step for step in draft["plan"] if step.get("selector") == "#first"
    )
    second_step = next(
        step for step in draft["plan"] if step.get("selector") == "#second"
    )
    assert first_step["dialogs"] == [
        {
            "type": "alert",
            "accept": True,
            "text": "",
            "page": "p2",
            "label": "popup-page",
            "opener_page": "p1",
        }
    ]
    assert "dialogs" not in second_step


async def test_cross_page_dialog_keeps_stable_page_route_and_diagnostic_label(
    compile_env,
):
    opener = _as_v9(
        _record(
            1,
            "click",
            selector="#open-popup",
            tag="button",
        ),
        causal_id=101,
    )
    opener["label"] = "opener"
    popup_dialog = _as_v9(_record(2, "dialog"), causal_id=101)
    popup_dialog.update(
        {
            "label": "popup",
            "dialogAction": "accept",
            "dialogType": "alert",
            "dialogText": "",
        }
    )
    _public, _path, draft = await _draft(
        compile_env,
        [opener, popup_dialog],
        {"source_step": 1},
        {"source_step": 2},
        slug="popup-dialog-route",
    )
    assert draft["plan"][0] == {
        "kind": "click",
        "selector": "#open-popup",
        "page": "p0",
        "dialogs": [
            {
                "type": "alert",
                "accept": True,
                "text": "",
                "page": "p1",
                "label": "popup",
                "opener_page": "p0",
            }
        ],
    }


async def test_v10_popup_topology_and_creation_causal_id_are_preserved(
    compile_env,
):
    opener = _as_v10(
        _record(
            1,
            "click",
            url="https://app.example/",
            selector="#open-popup",
            tag="button",
        ),
        causal_id=303,
    )
    opener["label"] = "p0"
    popup_anchor = _as_v10(
        _record(2, "navigate", url="about:blank"),
        opener_page="p0",
        popup_ordinal=7,
        created_by_causal_id=303,
    )
    popup_anchor["label"] = "p1"
    popup_dialog = _as_v10(
        _record(3, "dialog", url="about:blank"),
        opener_page="p0",
        popup_ordinal=7,
        created_by_causal_id=303,
    )
    popup_dialog.update(
        {
            "label": "p1",
            "dialogAction": "accept",
            "dialogType": "alert",
            "dialogText": "",
        }
    )
    _public, _path, draft = await _draft(
        compile_env,
        [opener, popup_anchor, popup_dialog],
        {"source_step": 1},
        {"source_step": 2},
        {"source_step": 3},
        slug="v10-popup-topology",
    )
    assert draft["plan"][0] == {
        "kind": "click",
        "selector": "#open-popup",
        "page": "p0",
        "dialogs": [
            {
                "type": "alert",
                "accept": True,
                "text": "",
                "page": "p1",
                "label": "p1",
                "opener_page": "p0",
                "popup_ordinal": 7,
            }
        ],
        "postconditions": [
            {
                "kind": "url",
                "target": "popup",
                "url": "about:blank",
                "activate": True,
                "page": "p1",
                "opener_page": "p0",
                "popup_ordinal": 7,
            }
        ],
    }


async def test_v10_standalone_popup_dialog_keeps_route_metadata(compile_env):
    dialog = _as_v10(
        _record(1, "dialog", url="about:blank"),
        opener_page="p0",
        popup_ordinal=2,
    )
    dialog.update(
        {
            "label": "p1",
            "dialogAction": "dismiss",
            "dialogType": "confirm",
            "dialogText": "",
        }
    )
    _public, _path, draft = await _draft(
        compile_env,
        [dialog],
        {"source_step": 1},
        slug="standalone-popup-dialog",
    )
    assert draft["plan"] == [
        {
            "kind": "dialog",
            "type": "confirm",
            "accept": False,
            "text": "",
            "page": "p1",
            "label": "p1",
            "opener_page": "p0",
            "popup_ordinal": 2,
        },
        {"kind": "snapshot_full"},
    ]


async def test_v9_rejects_v10_topology_fields_without_version_bump(compile_env):
    invalid = _as_v9(_record(1))
    invalid["openerPage"] = "p0"
    compile_env["write_trace"](RECORDING, [invalid])
    assert await compile_env["compile"](
        _compile_args({"source_step": 1})
    ) == "DRAFT_REJECTED: trace_record_shape_invalid"


async def test_compiler_has_no_trace_plan_selector_url_or_prompt_size_ceiling(
    compile_env,
):
    long_selector = (
        "css=section[data-payload=\""
        + "x" * 300_000
        + "\"] >> text=`literal ${value}`"
    )
    long_url = "data:text/plain," + "u" * 20_000
    long_prompt = "问" * 20_000
    records = [
        _as_v9(_record(1, url=long_url)),
        _as_v9(
            _record(
                2,
                "click",
                url=long_url,
                selector=long_selector,
                tag="button",
            ),
            causal_id=202,
        ),
    ]
    prompt = _as_v9(_record(3, "dialog", url=long_url), causal_id=202)
    prompt.update(
        {
            "dialogAction": "accept",
            "dialogType": "prompt",
            "dialogText": long_prompt,
        }
    )
    records.append(prompt)
    for step in range(4, 524):
        records.append(
            _as_v9(
                _record(
                    step,
                    "click",
                    url=long_url,
                    selector=f"#action-{step}",
                    tag="button",
                )
            )
        )

    public, _path, draft = await _draft(
        compile_env,
        records,
        *({"source_step": step} for step in range(1, 524)),
        slug="unbounded-recording",
    )
    assert public["step_count"] == 523
    assert draft["plan"][0]["url"] == long_url
    assert draft["plan"][1]["selector"] == long_selector
    assert draft["plan"][1]["dialogs"][0]["text"] == long_prompt
    assert len(draft["plan"]) == 523


async def test_compiler_reads_trace_larger_than_legacy_eight_megabytes(
    compile_env,
):
    record = _record(1)
    record["page"] = "p" * (8 * 1024 * 1024 + 1)
    public, _path, draft = await _draft(
        compile_env,
        [record],
        {"source_step": 1},
        slug="large-trace-evidence",
    )
    assert public["step_count"] == 2
    assert draft["plan"][0]["url"] == "https://oa.example/list"


async def test_nonzero_dialog_causal_id_without_unique_owner_is_rejected(
    compile_env,
):
    dialog = _as_v9(_record(2, "dialog"), causal_id=999)
    dialog.update(
        {
            "dialogAction": "dismiss",
            "dialogType": "confirm",
            "dialogText": "",
        }
    )
    compile_env["write_trace"](
        RECORDING,
        [_as_v9(_record(1)), dialog],
    )
    assert await compile_env["compile"](
        _compile_args({"source_step": 1}, {"source_step": 2})
    ) == "DRAFT_REJECTED: trace_dialog_causal_owner_missing"


async def test_recorder_v5_upload_becomes_runtime_file_parameter_and_chooser_trigger(
    compile_env,
):
    records = [
        _as_v5(_record(1, url="https://oa.example/form")),
        _as_v5(
            _record(
                2,
                "click",
                url="https://oa.example/form",
                selector="#styled-upload-button",
                tag="button",
            )
        ),
        _upload_record(
            3,
            mode="paths",
            paths=["/Users/recording-machine/private/report.pdf"],
        ),
    ]
    _public, path, draft = await _draft(
        compile_env,
        records,
        *({"source_step": index} for index in range(1, 4)),
        slug="recorded-file-upload",
    )
    upload = draft["plan"][1]
    assert upload == {
        "kind": "upload",
        "selector": "#attachment",
        "trigger_selector": "#styled-upload-button",
        "input_key": "recorded_target",
        "multiple": False,
        "accept": ".pdf",
    }
    assert draft["inputs"]["recorded_target"]["kind"] == "files"
    assert draft["inputs"]["recorded_target"]["required"] is True
    assert draft["inputs"]["recorded_target"]["default"] == [
        "/Users/recording-machine/private/report.pdf"
    ]
    serialized = path.read_text("utf-8")
    assert "/Users/recording-machine/private/report.pdf" in serialized


async def test_upload_trigger_absorbs_intervening_causal_dialog_and_navigation(
    compile_env,
):
    trigger = _as_v10(
        _record(
            1,
            "click",
            url="https://oa.example/form",
            selector="#styled-upload-button",
            tag="button",
        ),
        causal_id=501,
    )
    trigger["label"] = "p0"
    dialog = _as_v10(
        _record(2, "dialog", url="https://oa.example/form"),
        causal_id=501,
    )
    dialog.update(
        {
            "label": "p0",
            "dialogAction": "accept",
            "dialogType": "alert",
            "dialogText": "",
        }
    )
    popup_navigation = _as_v10(
        _record(3, "navigate", url="https://oa.example/upload-help"),
        causal_id=501,
        opener_page="p0",
        popup_ordinal=1,
        created_by_causal_id=501,
    )
    popup_navigation["label"] = "p1"
    upload = _as_v10(
        _record(
            4,
            "upload",
            url="https://oa.example/form",
            selector="#attachment",
            tag="input",
            input_type="file",
        ),
        causal_id=502,
    )
    upload["label"] = "p0"
    upload.update(
        {
            "uploadMode": "paths",
            "paths": ["/tmp/exact-report.pdf"],
            "fileCount": 1,
            "multiple": False,
            "accept": ".pdf",
        }
    )
    _public, _path, draft = await _draft(
        compile_env,
        [trigger, dialog, popup_navigation, upload],
        *({"source_step": index} for index in range(1, 5)),
        slug="atomic-upload-signals",
    )
    assert draft["plan"][0] == {
        "kind": "upload",
        "selector": "#attachment",
        "trigger_selector": "#styled-upload-button",
        "input_key": "recorded_target",
        "multiple": False,
        "accept": ".pdf",
        "page": "p0",
        "dialogs": [
            {"type": "alert", "accept": True, "text": ""}
        ],
        "postconditions": [
            {
                "kind": "url",
                "target": "popup",
                "url": "https://oa.example/upload-help",
                "activate": False,
                "page": "p1",
                "opener_page": "p0",
                "popup_ordinal": 1,
            }
        ],
    }


async def test_recorder_v5_handoff_upload_remains_replayable_with_new_local_files(
    compile_env,
):
    upload = _upload_record(
        2,
        mode="handoff",
        file_count=3,
        multiple=True,
        accept="image/*",
    )
    _public, _path, draft = await _draft(
        compile_env,
        [_as_v5(_record(1, url="https://oa.example/form")), upload],
        {"source_step": 1},
        {"source_step": 2},
        slug="handoff-file-upload",
    )
    assert draft["plan"][1] == {
        "kind": "upload",
        "selector": "#attachment",
        "input_key": "recorded_target",
        "multiple": True,
        "accept": "image/*",
    }
    assert draft["inputs"]["recorded_target"]["kind"] == "files"
    assert "recorded_count=3" in draft["inputs"]["recorded_target"]["recorded_hint"]


async def test_recorder_v5_clear_upload_is_exact_and_needs_no_runtime_input(
    compile_env,
):
    _public, _path, draft = await _draft(
        compile_env,
        [
            _as_v5(_record(1, url="https://oa.example/form")),
            _upload_record(2, mode="clear"),
        ],
        {"source_step": 1},
        {"source_step": 2},
        slug="clear-file-upload",
    )
    assert draft["inputs"] == {}
    assert draft["plan"][1] == {
        "kind": "upload",
        "selector": "#attachment",
        "files": [],
        "multiple": False,
        "accept": ".pdf",
    }


async def test_recorder_v5_upload_navigation_is_a_postcondition_not_second_open(
    compile_env,
):
    upload = _upload_record(
        2,
        mode="paths",
        paths=["/tmp/report.pdf"],
    )
    upload["timestamp"] = 1_000
    navigation = _as_v5(
        _record(3, url="https://oa.example/imported#/ready")
    )
    navigation["timestamp"] = 1_500
    _public, _path, draft = await _draft(
        compile_env,
        [
            _as_v5(_record(1, url="https://oa.example/form")),
            upload,
            navigation,
        ],
        *({"source_step": index} for index in range(1, 4)),
        slug="upload-navigation",
    )
    assert [step["kind"] for step in draft["plan"]] == [
        "navigate",
        "upload",
        "snapshot_full",
    ]
    assert draft["plan"][1]["postconditions"] == [
        {
            "kind": "url",
            "target": "same_tab",
            "url": "https://oa.example/imported#/ready",
        }
    ]


async def test_recorder_v5_rejects_partial_or_spoofed_native_path_resolution(
    compile_env,
):
    invalid = _upload_record(
        1,
        mode="paths",
        paths=["/tmp/only-one.pdf"],
        file_count=2,
    )
    compile_env["write_trace"](RECORDING, [invalid])
    assert await compile_env["compile"](
        _compile_args({"source_step": 1})
    ) == "DRAFT_REJECTED: trace_upload_invalid"


@pytest.mark.parametrize("failure_mode", ["return-false", "raise"])
async def test_install_failure_rolls_back_the_published_artifact(
    compile_env, monkeypatch, failure_mode
):
    """装技能失败（返回 False 或抛异常）时，刚发布的 owner 私有 artifact 必须回滚，不留孤儿。

    rollback_published_workflow 之前 export 了却零调用（死代码）。现在接回失败
    路径：先 publish 成功、再装技能失败，应回滚已发布的产物。不回滚不阻断重试
    （publish 幂等），但会在盘上留一个没有入口技能、workflow_id 不可知的孤儿。
    回滚放在 try 内部只覆盖 return-False；install_skill_from_dir 抛异常会跳到
    except，绕过回滚——两档参数钉住"两条失败路都回滚"。
    """
    from plugins.browser import compile_tool

    public, _path, draft = await _draft(
        compile_env,
        [
            _record(1),
            _record(2, "click", selector="#run", tag="button"),
        ],
        {"source_step": 1},
        {"source_step": 2},
        slug=f"rollback-on-install-{failure_mode}",
    )
    args = _install_args(public)

    def _boom(*a, **k):
        raise RuntimeError("装技能过程中的非常规异常")

    # 让治理安装这一步失败（publish 已经成功）
    monkeypatch.setattr(
        compile_tool,
        "install_skill_from_dir",
        (lambda *a, **k: False) if failure_mode == "return-false" else _boom,
    )

    result = await compile_env["install"](args)
    assert result.startswith("INSTALL_FAILED: ")

    # 发布的 artifact 已被回滚——read_workflow 找不到它
    with pytest.raises((OSError, WorkflowStoreError)):
        read_workflow(OWNER, draft["workflow_id"])


async def test_install_failure_survives_rollback_target_race(compile_env, monkeypatch):
    """回滚遇到并发目标替换时，install 仍要干净返回 INSTALL_FAILED，不能抛异常。

    rollback_published_workflow 在目标 inode 被并发换掉/变成软链时抛
    WorkflowStoreError——它是 ValueError 不是 OSError。回滚的 except 若只写
    OSError，这个异常会逃出 install_handler，把一次干净的 INSTALL_FAILED 变成
    未捕获异常抛给调用方。这条钉住 except 必须覆盖 WorkflowStoreError。
    """
    from plugins.browser import compile_tool

    public, _path, draft = await _draft(
        compile_env,
        [
            _record(1),
            _record(2, "click", selector="#run", tag="button"),
        ],
        {"source_step": 1},
        {"source_step": 2},
        slug="rollback-target-race",
    )
    args = _install_args(public)

    monkeypatch.setattr(compile_tool, "install_skill_from_dir", lambda *a, **k: False)

    def _target_changed(_published):
        raise WorkflowStoreError("workflow_target_changed")

    monkeypatch.setattr(compile_tool, "rollback_published_workflow", _target_changed)

    # 回滚放弃（目标已不是本次发布的 inode）不该炸掉整个调用：必须干净返回。
    result = await compile_env["install"](args)
    assert result.startswith("INSTALL_FAILED: ")


async def test_install_publishes_exact_v2_workflow(compile_env):
    public, _path, draft = await _draft(
        compile_env,
        [
            _record(1),
            _record(2, "click", selector="#run", tag="button"),
        ],
        {"source_step": 1},
        {"source_step": 2},
        slug="installed-functional-flow",
    )
    args = _install_args(public)
    # 安装不弹确认：范围摘要随结果回给模型用于汇报。
    assert compile_env["resolver"](args) is None
    installed = _payload(await compile_env["install"](args), "INSTALL_OK: ")
    assert "共 3 步" in installed["scope"]
    assert "不含凭据原值" in installed["scope"]
    artifact = read_workflow(OWNER, installed["workflow_id"])
    assert artifact.payload["plan"] == draft["plan"]
    assert artifact.payload["schema_version"] == "crew.browser.replay.v2"
    assert artifact.payload["capabilities"] == [
        "navigate",
        "click",
        "snapshot_full",
    ]
    frontmatter = json.loads(draft["preview"].split("---\n", 2)[1])
    assert frontmatter["metadata"]["browser_policy"] == {
        "schema_version": "crew.browser.policy.v2",
        "readonly": False,
        "capabilities": ["navigate", "click", "snapshot_full"],
    }
    assert "空 inputs 会使用录制时保存的精确默认值。" in draft["preview"]
    assert "传入对应 override" in draft["preview"]


async def test_v2_skill_activation_touches_no_runtime_gate(
    compile_env,
    monkeypatch,
):
    _public, draft_path, draft = await _draft(
        compile_env,
        [
            _record(1),
            _record(2, "click", selector="#run", tag="button"),
        ],
        {"source_step": 1},
        {"source_step": 2},
        slug="functional-policy-flow",
    )
    skill_dir = draft_path.parent / "functional-policy-flow"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(draft["preview"], encoding="utf-8")
    monkeypatch.setattr(
        "crew.gateway.ws.get_skills",
        lambda: {
            "functional-policy-flow": {
                "skill_dir": str(skill_dir),
            }
        },
    )

    # 技能激活不再改会话上的任何运行期档位：授权来自不可变 plan 与必须精确
    # 等于 plan 的 capabilities 声明，那是按这一次录制的实际动作推导出来的，
    # 比"这个会话只读"这种粗粒度档位准确得多，也不会在正常流程上产生阻碍。
    #
    # 这里只验证形状校验能跑过一份真实产出的技能而不抛异常、不改任何状态。
    class PolicyManager:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def __getattr__(self, name: str):
            def record(*args):
                self.calls.append((name, *args))

            return record

    manager = PolicyManager()
    crew = type("Crew", (), {"browser_manager": manager})()
    _apply_browser_skill_policy(
        crew,
        "functional-policy-flow",
        OWNER,
        SESSION,
    )
    assert manager.calls == []
