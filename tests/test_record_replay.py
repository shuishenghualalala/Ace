"""Executable Playwright workflow store and replay contracts."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from crew.browser.driver import BrowserDriverError
from crew.browser.manager import (
    BrowserManager,
    _Owner,
    _ReplayLease,
    _Session,
    _Tab,
)
from crew.browser.types import BrowserConfig
from crew.core.runctx import (
    current_owner_account_id,
    current_session_id,
    current_tool_call_id,
)
from plugins.browser.replay_tool import (
    REPLAY_SCHEMA,
    RecordReplayTool,
    ReplayInputsRequired,
    ReplayRejected,
    _resolve_inputs,
    _resolved_step,
    _validate_executable_capabilities,
)
from plugins.browser.workflow_store import (
    LEGACY_WORKFLOW_STORE_SCHEMA,
    WorkflowStoreError,
    _artifact_from_raw,
    _canonical_json,
    _owner_binding,
    build_workflow_artifact,
    publish_workflow,
    read_workflow,
    rollback_published_workflow,
)

OWNER = "owner-replay-a"
SESSION = "session-replay-a"


def _plan() -> tuple[list[dict], dict[str, dict]]:
    return (
        [
            {
                "kind": "navigate",
                "url": "https://id.example/login?client=crew#/start",
            },
            {
                "kind": "fill_form",
                "fields": [
                    {
                        "type": "textbox",
                        "selector": "#email",
                        "input_key": "email",
                    },
                    {
                        "type": "combobox",
                        "selector": "#department",
                        "input_key": "department",
                        "select_by": "value",
                    },
                    {
                        "type": "checkbox",
                        "selector": "#active",
                        "value": True,
                    },
                ],
            },
            {
                "kind": "click",
                "selector": "#continue",
                "postconditions": [
                    {
                        "kind": "url",
                        "target": "same_tab",
                        "url": "https://app.example/continued#/ready",
                    }
                ],
            },
            {"kind": "dblclick", "selector": "#open-details"},
            {
                "kind": "drag",
                "source_selector": "#card-a",
                "target_selector": "#column-done",
            },
            {
                "kind": "press",
                "selector": "#search",
                "key": "Enter",
            },
            {
                "kind": "scroll",
                "selector": "#virtual-list",
                "delta_x": 0,
                "delta_y": 1_200,
            },
            {
                "kind": "navigate",
                "url": "https://app.example/home?view=grid#/requests",
            },
            {"kind": "snapshot_full"},
        ],
        {
            "email": {
                "kind": "text",
                "required": True,
                "display_name": "Email",
                "recorded_hint": "input · email · name=email",
            },
            "department": {
                "kind": "select",
                "required": True,
                "display_name": "Department",
                "recorded_hint": "select · select-one · name=department",
            },
        },
    )


def _artifact(owner: str = OWNER):
    plan, inputs = _plan()
    return build_workflow_artifact(
        owner=owner,
        hosts=("app.example", "id.example"),
        inputs=inputs,
        plan=plan,
    )


def test_record_replay_schema_has_only_workflow_inputs_and_resume_token():
    parameters = REPLAY_SCHEMA["parameters"]
    # resume_token 是续跑凭证，可选：只在上一次返回 REPLAY_SUSPENDED 之后使用。
    # 它不是"又一个业务参数"——模型不能凭它绕过挂起，凭证由 manager 生成且一次性。
    assert set(parameters["properties"]) == {
        "workflow_id",
        "inputs",
        "resume_token",
    }
    assert parameters["required"] == ["workflow_id", "inputs"]
    assert parameters["additionalProperties"] is False
    input_schema = parameters["properties"]["inputs"]
    assert "maxProperties" not in input_schema
    scalar_schema, sequence_schema = input_schema[
        "additionalProperties"
    ]["oneOf"]
    assert scalar_schema == {"type": "string"}
    assert not {
        "minItems",
        "maxItems",
        "uniqueItems",
    } & sequence_schema.keys()
    assert sequence_schema["items"] == {"type": "string"}
    Draft202012Validator.check_schema(parameters)


def test_v2_store_accepts_complete_functional_plan():
    artifact = _artifact()
    assert artifact.payload["schema_version"] == "crew.browser.replay.v2"
    assert artifact.payload["capabilities"] == [
        "navigate",
        "click",
        "dblclick",
        "drag",
        "press",
        "fill_form",
        "scroll",
        "snapshot_full",
    ]
    assert [step["kind"] for step in artifact.payload["plan"]] == [
        "navigate",
        "fill_form",
        "click",
        "dblclick",
        "drag",
        "press",
        "scroll",
        "navigate",
        "snapshot_full",
    ]
    assert "?client=crew#/start" in artifact.payload["plan"][0]["url"]
    assert "default" not in artifact.payload["inputs"]["email"]


def test_v2_store_preserves_optional_recorded_defaults_exactly():
    text_default = "x" * 20_000 + "\n`literal ${value}`"
    file_default = [
        "/Users/example/" + "f" * 40_000 + ".txt",
        "/Volumes/archive/report.pdf",
    ]
    artifact = build_workflow_artifact(
        owner=OWNER,
        hosts=(),
        inputs={
            "notes": {
                "kind": "text",
                "required": True,
                "display_name": "Notes",
                "recorded_hint": "textarea",
                "default": text_default,
            },
            "choices": {
                "kind": "select",
                "required": True,
                "display_name": "Choices",
                "recorded_hint": "select-multiple",
                "default": ["", "alpha", "alpha"],
            },
            "files": {
                "kind": "files",
                "required": True,
                "display_name": "Files",
                "recorded_hint": "file upload",
                "default": file_default,
            },
        },
        plan=[
            {
                "kind": "fill_form",
                "fields": [
                    {
                        "type": "textbox",
                        "selector": "#notes",
                        "input_key": "notes",
                    }
                ],
            },
            {
                "kind": "select",
                "selector": "#choices",
                "input_key": "choices",
            },
            {
                "kind": "upload",
                "selector": "#files",
                "input_key": "files",
                "multiple": True,
                "accept": "",
            },
            {"kind": "snapshot_full"},
        ],
    )
    assert artifact.payload["inputs"]["notes"]["default"] == text_default
    assert artifact.payload["inputs"]["choices"]["default"] == [
        "",
        "alpha",
        "alpha",
    ]
    assert artifact.payload["inputs"]["files"]["default"] == file_default


def test_replay_defaults_are_exact_and_user_overrides_take_precedence():
    text_default = "录制默认值\n" + "x" * 600_000
    select_default = ["", "alpha", "alpha"] * 20
    file_default = [
        "/Users/example/" + "f" * 40_000 + ".txt",
        "/Users/example/" + "f" * 40_000 + ".txt",
    ]
    artifact = build_workflow_artifact(
        owner=OWNER,
        hosts=(),
        inputs={
            "notes": {
                "kind": "text",
                "required": True,
                "display_name": "Notes",
                "recorded_hint": "textarea",
                "default": text_default,
            },
            "choices": {
                "kind": "select",
                "required": True,
                "display_name": "Choices",
                "recorded_hint": "select-multiple",
                "default": select_default,
            },
            "files": {
                "kind": "files",
                "required": True,
                "display_name": "Files",
                "recorded_hint": "file upload",
                "default": file_default,
            },
        },
        plan=[
            {
                "kind": "fill",
                "selector": "#notes",
                "input_key": "notes",
            },
            {
                "kind": "select",
                "selector": "#choices",
                "input_key": "choices",
            },
            {
                "kind": "upload",
                "selector": "#files",
                "input_key": "files",
                "multiple": True,
                "accept": "",
            },
            {"kind": "snapshot_full"},
        ],
    )

    resolved_defaults = _resolve_inputs(artifact, {})
    assert resolved_defaults == {
        "choices": select_default,
        "files": file_default,
        "notes": text_default,
    }
    assert resolved_defaults["choices"] is not select_default
    assert resolved_defaults["files"] is not file_default

    resolved_overrides = _resolve_inputs(
        artifact,
        {
            "notes": "",
            "choices": [],
        },
    )
    assert resolved_overrides == {
        "choices": [],
        "files": file_default,
        "notes": "",
    }


def test_replay_only_requires_inputs_without_override_or_default():
    artifact = _artifact()

    with pytest.raises(ReplayInputsRequired) as exc_info:
        _resolve_inputs(
            artifact,
            {"email": "person@example.com"},
        )
    assert exc_info.value.keys == ("department",)

    with pytest.raises(ReplayRejected, match="replay_input_unknown"):
        _resolve_inputs(
            artifact,
            {
                "email": "person@example.com",
                "department": ["engineering"],
                "typo": "ignored?",
            },
        )


def test_resolved_form_step_preserves_page_and_dialog_metadata():
    dialogs = [
        {
            "type": "prompt",
            "accept": True,
            "text": "recorded answer",
            "page": "p1",
            "label": "child",
            "opener_page": "p0",
            "popup_ordinal": 0,
        }
    ]
    resolved = _resolved_step(
        {
            "kind": "fill",
            "selector": "#name",
            "input_key": "name",
            "page": "p1",
            "dialogs": dialogs,
        },
        {"name": "value"},
    )
    assert resolved == {
        "kind": "fill",
        "selector": "#name",
        "text": "value",
        "page": "p1",
        "dialogs": dialogs,
    }
    assert resolved["dialogs"] is not dialogs
    assert resolved["dialogs"][0] is not dialogs[0]


def test_v1_artifact_is_explicitly_read_compatible():
    core = {
        "schema_version": LEGACY_WORKFLOW_STORE_SCHEMA,
        "owner_binding": _owner_binding(OWNER),
        "hosts": ["oa.example"],
        "inputs": {},
        "plan": [
            {"kind": "navigate", "url": "https://oa.example/list"},
            {"kind": "snapshot_full"},
        ],
    }
    workflow_id = hashlib.sha256(_canonical_json(core).encode()).hexdigest()
    payload = {**core, "workflow_id": workflow_id}
    raw = (_canonical_json(payload) + "\n").encode()
    artifact = _artifact_from_raw(OWNER, workflow_id, raw)
    assert artifact.payload["schema_version"] == LEGACY_WORKFLOW_STORE_SCHEMA
    _validate_executable_capabilities(artifact)


def test_v1_artifact_cannot_gain_v2_mutation_capabilities():
    core = {
        "schema_version": LEGACY_WORKFLOW_STORE_SCHEMA,
        "owner_binding": _owner_binding(OWNER),
        "hosts": ["oa.example"],
        "inputs": {},
        "plan": [
            {"kind": "click", "selector": "#submit"},
            {"kind": "snapshot_full"},
        ],
    }
    workflow_id = hashlib.sha256(_canonical_json(core).encode()).hexdigest()
    raw = (
        _canonical_json({**core, "workflow_id": workflow_id}) + "\n"
    ).encode()
    artifact = _artifact_from_raw(OWNER, workflow_id, raw)
    with pytest.raises(ReplayRejected, match="legacy_readonly_action"):
        _validate_executable_capabilities(artifact)


def test_legacy_expected_attestation_has_one_explicit_migration_entry():
    legacy = {
        "kind": "fill",
        "selector": "#username",
        "text": "Ada",
        "expected_action_kind": "input",
        "expected_tag": "input",
        "expected_input_type": "text",
        "expected_role": "textbox",
        "expected_tier": "plain",
        "expected_document_host": "oa.example",
        "expected_document_origin": "https://oa.example",
        "expected_content_editable": False,
    }
    kind, migrated = BrowserManager._validated_replay_step(legacy)
    assert kind == "fill"
    assert migrated == {
        "kind": "fill",
        "selector": "#username",
        "text": "Ada",
    }
    assert not hasattr(BrowserManager, "_require_replay_target_semantics")
    assert not hasattr(BrowserManager, "_consume_atomic_replay_permit")


def test_workflow_artifact_is_canonical_owner_bound_and_atomic(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("CREW_HOME", str(tmp_path))
    first = _artifact()
    second = _artifact()
    assert first.workflow_id == second.workflow_id
    assert first.raw == second.raw
    assert first.workflow_id != _artifact("owner-replay-b").workflow_id

    published = publish_workflow(OWNER, first)
    assert published.created is True
    assert stat.S_IMODE(os.lstat(published.path).st_mode) == 0o600
    loaded = read_workflow(OWNER, first.workflow_id, expected_digest=first.digest)
    assert loaded.raw == first.raw
    same = publish_workflow(OWNER, first)
    assert same.created is False
    assert rollback_published_workflow(same) is False
    assert rollback_published_workflow(published) is True


def test_workflow_publish_has_one_concurrent_winner(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path))
    artifact = _artifact()
    with ThreadPoolExecutor(max_workers=8) as pool:
        published = list(
            pool.map(lambda _index: publish_workflow(OWNER, artifact), range(16))
        )
    assert sum(item.created for item in published) == 1
    assert len({item.identity for item in published}) == 1


def test_store_rejects_unknown_steps_and_missing_final_snapshot():
    with pytest.raises(WorkflowStoreError, match="workflow_plan_invalid"):
        build_workflow_artifact(
            owner=OWNER,
            hosts=("oa.example",),
            inputs={},
            plan=[
                {"kind": "javascript", "source": "alert(1)"},
                {"kind": "snapshot_full"},
            ],
        )
    with pytest.raises(
        WorkflowStoreError,
        match="workflow_final_observation_required",
    ):
        build_workflow_artifact(
            owner=OWNER,
            hosts=("oa.example",),
            inputs={},
            plan=[{"kind": "click", "selector": "#save"}],
        )


def test_workflow_store_preserves_exact_pointer_options():
    artifact = build_workflow_artifact(
        owner=OWNER,
        hosts=("oa.example",),
        inputs={},
        plan=[
            {
                "kind": "click",
                "selector": "#open-in-new-tab",
                "button": "middle",
                "click_count": 37,
                "modifiers": ["Meta", "Shift"],
                "position": {"x": 127.5, "y": 42.25},
            },
            {"kind": "snapshot_full"},
        ],
    )
    assert artifact.payload["plan"][0] == {
        "kind": "click",
        "selector": "#open-in-new-tab",
        "button": "middle",
        "click_count": 37,
        "modifiers": ["Meta", "Shift"],
        "position": {"x": 127.5, "y": 42.25},
    }


def test_workflow_store_dialog_metadata_is_optional_and_backward_compatible():
    legacy = build_workflow_artifact(
        owner=OWNER,
        hosts=(),
        inputs={},
        plan=[
            {
                "kind": "click",
                "selector": "#legacy-dialog",
                "dialogs": [
                    {"type": "confirm", "accept": False, "text": ""}
                ],
            },
            {"kind": "snapshot_full"},
        ],
    )
    assert legacy.payload["plan"][0]["dialogs"] == [
        {"type": "confirm", "accept": False, "text": ""}
    ]

    routed = build_workflow_artifact(
        owner=OWNER,
        hosts=(),
        inputs={},
        plan=[
            {
                "kind": "click",
                "selector": "#open-popup",
                "page": "p0",
                "dialogs": [
                    {
                        "type": "prompt",
                        "accept": True,
                        "text": "",
                        "page": "p1000000",
                        "label": "",
                        "opener_page": "p0",
                        "popup_ordinal": 1_000_000,
                    }
                ],
            },
            {"kind": "snapshot_full"},
        ],
    )
    assert routed.payload["plan"][0]["dialogs"] == [
        {
            "type": "prompt",
            "accept": True,
            "text": "",
            "page": "p1000000",
            "label": "",
            "opener_page": "p0",
            "popup_ordinal": 1_000_000,
        }
    ]


def test_workflow_store_preserves_large_executable_values_and_plan(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("CREW_HOME", str(tmp_path))
    selector = (
        "css=main[data-value=\""
        + "s" * 300_000
        + "\"] >> text=`literal ${value}`"
    )
    url = "data:text/plain," + "u" * 20_000
    prompt_text = "值" * 20_000
    plan = [
        {"kind": "navigate", "url": url},
        {
            "kind": "click",
            "selector": selector,
            "dialogs": [
                {
                    "type": "prompt",
                    "accept": True,
                    "text": prompt_text,
                }
            ],
        },
        *(
            {"kind": "click", "selector": f"#step-{index}"}
            for index in range(300)
        ),
        {"kind": "snapshot_full"},
    ]
    artifact = build_workflow_artifact(
        owner=OWNER,
        hosts=(),
        inputs={},
        plan=plan,
    )
    assert len(artifact.raw) > 256 * 1024
    assert artifact.payload["plan"][0]["url"] == url
    assert artifact.payload["plan"][1]["selector"] == selector
    assert artifact.payload["plan"][1]["dialogs"][0]["text"] == prompt_text
    assert len(artifact.payload["plan"]) == 303
    published = publish_workflow(OWNER, artifact)
    loaded = read_workflow(
        OWNER,
        artifact.workflow_id,
        expected_digest=artifact.digest,
    )
    assert loaded.raw == artifact.raw
    assert published.artifact.raw == artifact.raw


def test_workflow_store_accepts_more_than_legacy_input_and_form_limits():
    inputs = {
        f"field_{index}": {
            "kind": "text",
            "required": True,
            "display_name": "字段 " + "名" * 500,
            "recorded_hint": "textarea · " + "h" * 1_000,
        }
        for index in range(140)
    }
    fields = [
        {
            "type": "textbox",
            "selector": f"#field-{index}",
            "input_key": f"field_{index}",
        }
        for index in range(140)
    ]
    artifact = build_workflow_artifact(
        owner=OWNER,
        hosts=(),
        inputs=inputs,
        plan=[
            {"kind": "fill_form", "fields": fields},
            {"kind": "snapshot_full"},
        ],
    )
    assert len(artifact.payload["inputs"]) == 140
    assert len(artifact.payload["plan"][0]["fields"]) == 140
    assert artifact.payload["inputs"]["field_0"]["display_name"].endswith(
        "名" * 500
    )


def test_workflow_store_preserves_parameterized_upload_and_exact_clear():
    artifact = build_workflow_artifact(
        owner=OWNER,
        hosts=("oa.example",),
        inputs={
            "attachment": {
                "kind": "files",
                "required": True,
                "display_name": "Attachment",
                "recorded_hint": "file upload · multiple=true",
            }
        },
        plan=[
            {
                "kind": "upload",
                "selector": "#attachment",
                "trigger_selector": "#styled-upload-button",
                "input_key": "attachment",
                "multiple": True,
                "accept": ".pdf,image/*",
            },
            {
                "kind": "upload",
                "selector": "#temporary-attachment",
                "files": [],
                "multiple": False,
                "accept": "",
            },
            {"kind": "snapshot_full"},
        ],
    )
    assert artifact.payload["capabilities"] == ["upload", "snapshot_full"]
    assert artifact.payload["inputs"]["attachment"]["kind"] == "files"
    assert artifact.payload["plan"][:2] == [
        {
            "kind": "upload",
            "selector": "#attachment",
            "multiple": True,
            "accept": ".pdf,image/*",
            "input_key": "attachment",
            "trigger_selector": "#styled-upload-button",
        },
        {
            "kind": "upload",
            "selector": "#temporary-attachment",
            "multiple": False,
            "accept": "",
            "files": [],
        },
    ]


def test_workflow_hosts_are_diagnostic_not_navigation_capability():
    artifact = build_workflow_artifact(
        owner=OWNER,
        # This metadata intentionally omits both executable destinations.
        hosts=("recorded.example",),
        inputs={},
        plan=[
            {
                "kind": "click",
                "selector": "#open",
                "postconditions": [
                    {
                        "kind": "url",
                        "target": "same_tab",
                        "url": "https://app.example/details?id=42#/summary",
                    },
                    {
                        "kind": "url",
                        "target": "popup",
                        "url": "https://id.example/popup?flow=oauth#/ready",
                        "activate": True,
                    },
                ],
            },
            {"kind": "snapshot_full"},
        ],
    )
    assert artifact.payload["hosts"] == ["recorded.example"]
    assert artifact.payload["plan"][0]["postconditions"] == [
        {
            "kind": "url",
            "target": "same_tab",
            "url": "https://app.example/details?id=42#/summary",
        },
        {
            "kind": "url",
            "target": "popup",
            "url": "https://id.example/popup?flow=oauth#/ready",
            "activate": True,
        },
    ]

    navigation = build_workflow_artifact(
        owner=OWNER,
        hosts=(),
        inputs={},
        plan=[
            {"kind": "navigate", "url": "https://tenant.example/home"},
            {"kind": "snapshot_full"},
        ],
    )
    assert navigation.payload["hosts"] == []
    assert navigation.payload["plan"][0]["url"] == "https://tenant.example/home"

    with pytest.raises(
        WorkflowStoreError,
        match="workflow_postconditions_invalid",
    ):
        build_workflow_artifact(
            owner=OWNER,
            hosts=("app.example",),
            inputs={},
            plan=[
                {
                    "kind": "click",
                    "selector": "#open",
                    "postconditions": [
                        {
                            "kind": "url",
                            "target": "popup",
                            "url": "https://app.example/popup",
                            # activate is deliberately missing.
                        }
                    ],
                },
                {"kind": "snapshot_full"},
            ],
        )


def test_workflow_store_preserves_input_bound_and_volatile_url_pattern():
    artifact = build_workflow_artifact(
        owner=OWNER,
        hosts=("app.example",),
        inputs={
            "query": {
                "kind": "text",
                "required": True,
                "display_name": "Query",
                "recorded_hint": "input · search",
            }
        },
        plan=[
            {
                "kind": "fill_form",
                "fields": [
                    {
                        "type": "textbox",
                        "selector": "#query",
                        "input_key": "query",
                    }
                ],
            },
            {
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
                            {"input_key": "query", "encoding": "query"},
                            {"literal": "&state="},
                            {"wildcard": "query_value"},
                        ],
                    }
                ],
            },
            {"kind": "snapshot_full"},
        ],
    )
    assert artifact.payload["plan"][1]["postconditions"][0] == {
        "kind": "url",
        "target": "same_tab",
        "origin": "https://app.example",
        "url_pattern": [
            {"literal": "https://app.example/search?q="},
            {"input_key": "query", "encoding": "query"},
            {"literal": "&state="},
            {"wildcard": "query_value"},
        ],
    }
    resolved = _resolved_step(
        artifact.payload["plan"][1],
        {"query": "new query"},
    )
    assert resolved["postconditions"][0]["url_pattern"] == [
        {"literal": "https://app.example/search?q="},
        {"alternatives": ["new+query", "new%20query"]},
        {"literal": "&state="},
        {"wildcard": "query_value"},
    ]


async def test_replay_locator_waits_for_observed_async_render(
    tmp_path,
    monkeypatch,
):
    class DelayedLocatorDriver:
        def __init__(self) -> None:
            self.calls = 0

        async def execute_targeted(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls < 4:
                raise BrowserDriverError(
                    "not rendered yet",
                    code="selector_no_match",
                )
            return {"data": {"ref": "@s1"}}

    driver = DelayedLocatorDriver()
    manager = BrowserManager(
        BrowserConfig(
            command_timeout_seconds=1,
            navigation_timeout_seconds=1,
        ),
        driver=driver,
    )
    tab = _Tab(id="main", label="main", target_id="target-main")
    session = _Session(
        session_id=SESSION,
        owner=OWNER,
        tabs={"main": tab},
        active_label="main",
    )
    owner = _Owner(
        owner=OWNER,
        runtime_key="runtime",
        profile_dir=Path(tmp_path) / "profile",
        sessions={SESSION: session},
    )
    lease = _ReplayLease(
        workflow_id="a" * 64,
        workflow_digest="b" * 64,
        capability_generation=0,
        nonce="replay_nonce_" + "x" * 24,
        tool_call_id="call",
        allowed_hosts=frozenset({"app.example"}),
    )

    async def select(_owner, _session):
        return _session.tabs[_session.active_label], False

    monkeypatch.setattr(manager, "_select", select)
    monkeypatch.setattr(
        manager,
        "_require_replay_action_context",
        lambda *_args, **_kwargs: None,
    )
    native = await manager._replay_locator_native_locked(
        owner,
        session,
        lease,
        "#async-result",
        workdir="",
    )
    assert native == "@s1"
    assert driver.calls == 4


async def test_replay_popup_postcondition_allows_cross_host_and_activates_exact_descendant(
    tmp_path,
    monkeypatch,
):
    manager = BrowserManager(
        BrowserConfig(
            command_timeout_seconds=1,
            navigation_timeout_seconds=1,
        )
    )
    main = _Tab(id="main", label="main", target_id="target-main")
    popup = _Tab(
        id="popup",
        label="popup",
        target_id="target-popup",
        native_labeled=False,
    )
    stale_popup = _Tab(
        id="stale-popup",
        label="stale-popup",
        target_id="target-stale-popup",
        native_labeled=False,
    )
    session = _Session(
        session_id=SESSION,
        owner=OWNER,
        tabs={"main": main, "popup": popup, "stale-popup": stale_popup},
        active_label="main",
    )
    owner = _Owner(
        owner=OWNER,
        runtime_key="runtime",
        profile_dir=Path(tmp_path) / "profile",
        sessions={SESSION: session},
    )
    lease = _ReplayLease(
        workflow_id="a" * 64,
        workflow_digest="b" * 64,
        capability_generation=0,
        nonce="replay_nonce_" + "x" * 24,
        tool_call_id="call",
        allowed_hosts=frozenset({"app.example"}),
    )
    page_guard_calls = 0

    async def select(_owner, _session):
        return _session.tabs[_session.active_label], False

    async def run(_owner, _session, command, args, **_kwargs):
        assert (command, args) == ("tab", ["list"])
        return {
            "data": {
                "tabs": [
                    {
                        "tabId": "t1",
                        "label": "main",
                        "title": "",
                        "url": "https://app.example/start",
                        "type": "page",
                        "active": _session.active_label == "main",
                        "targetId": "target-main",
                        "openerTargetId": "",
                    },
                    {
                        "tabId": "t2",
                        "label": "",
                        "title": "",
                        "url": "https://id.example/popup",
                        "type": "page",
                        "active": _session.active_label == "popup",
                        "targetId": "target-popup",
                        "openerTargetId": "target-main",
                    },
                    {
                        "tabId": "t3",
                        "label": "",
                        "title": "",
                        "url": "https://id.example/popup#/ready",
                        "type": "page",
                        "active": _session.active_label == "stale-popup",
                        "targetId": "target-stale-popup",
                        "openerTargetId": "target-main",
                    },
                ]
            }
        }

    async def page_guard(_owner, _session, **_kwargs):
        nonlocal page_guard_calls
        page_guard_calls += 1
        active = _session.tabs[_session.active_label]
        href = (
            "https://id.example/popup#/ready"
            if active is stale_popup
            or active is popup
            and page_guard_calls >= 3
            else "about:blank"
            if active is popup
            else "https://app.example/start"
        )
        return json.dumps(
            {
                "token": "guard",
                "targetId": active.target_id,
                "frameId": "frame",
                "loaderId": "loader",
                "href": href,
                "timeOrigin": 1,
                "navigationEpoch": page_guard_calls,
                "navigationPending": False,
                "locationConsistent": True,
                "titleDigest": "title",
            }
        )

    monkeypatch.setattr(manager, "_select", select)
    monkeypatch.setattr(manager, "_run", run)
    monkeypatch.setattr(manager, "_page_guard", page_guard)
    monkeypatch.setattr(
        manager,
        "_require_replay_action_context",
        lambda *_args, **_kwargs: None,
    )

    await manager._await_replay_postconditions(
        owner,
        session,
        lease,
        [
            {
                "kind": "url",
                "target": "popup",
                "url": "https://id.example/popup#/ready",
                "activate": True,
            }
        ],
        source_target_id="target-main",
        pre_action_target_ids=frozenset(
            {"target-main", "target-stale-popup"}
        ),
        workdir="",
    )
    assert page_guard_calls >= 3
    assert session.active_label == "popup"


async def test_replay_popup_ordinal_uses_persistent_base_after_closed_history(
    tmp_path,
    monkeypatch,
):
    manager = BrowserManager(
        BrowserConfig(command_timeout_seconds=1, navigation_timeout_seconds=1)
    )
    main = _Tab(id="main", label="main", target_id="target-main")
    session = _Session(
        session_id=SESSION,
        owner=OWNER,
        tabs={"main": main},
        active_label="main",
    )
    owner = _Owner(
        owner=OWNER,
        runtime_key="runtime",
        profile_dir=Path(tmp_path) / "profile",
        sessions={SESSION: session},
    )
    lease = _ReplayLease(
        workflow_id="a" * 64,
        workflow_digest="b" * 64,
        capability_generation=0,
        nonce="replay_nonce_" + "x" * 24,
        tool_call_id="call",
        allowed_hosts=frozenset({"app.example"}),
        page_targets={"p0": "target-main"},
    )
    session.active_replay = lease
    after_action = False

    def rows() -> list[dict]:
        result = [
            {
                "tabId": "t1",
                "label": "main",
                "title": "",
                "url": "https://app.example/start",
                "type": "page",
                "active": session.active_label == "main",
                "targetId": "target-main",
                "openerTargetId": "",
                # Popup ordinal 1 was already consumed by a popup that closed.
                "popupOrdinalBase": 1,
            }
        ]
        if after_action:
            result.append(
                {
                    "tabId": "t2",
                    "label": "",
                    "title": "",
                    "url": "https://id.example/next",
                    "type": "page",
                    "active": session.active_label == "popup",
                    "targetId": "target-popup-next",
                    "openerTargetId": "target-main",
                    "popupOrdinal": 2,
                    "popupOrdinalBase": 0,
                }
            )
        return result

    async def select(_owner, _session):
        return _session.tabs[_session.active_label], False

    async def run(_owner, _session, command, args, **_kwargs):
        assert (command, args) == ("tab", ["list"])
        return {"data": {"tabs": rows()}}

    async def page_guard(_owner, _session, **_kwargs):
        active = _session.tabs[_session.active_label]
        return json.dumps(
            {
                "token": "guard",
                "targetId": active.target_id,
                "frameId": "frame",
                "loaderId": "loader",
                "href": (
                    "https://id.example/next"
                    if active.target_id == "target-popup-next"
                    else "https://app.example/start"
                ),
                "timeOrigin": 1,
                "navigationEpoch": 1,
                "navigationPending": False,
                "locationConsistent": True,
                "titleDigest": "title",
            }
        )

    monkeypatch.setattr(manager, "_select", select)
    monkeypatch.setattr(manager, "_run", run)
    monkeypatch.setattr(manager, "_page_guard", page_guard)
    monkeypatch.setattr(
        manager,
        "_require_replay_action_context",
        lambda *_args, **_kwargs: None,
    )

    source_target_id, pre_action_target_ids, _ = (
        await manager._replay_pre_action_targets(owner, session)
    )
    assert lease.popup_ordinal_bases == {"target-main": 1}
    assert manager._wire_expected_dialogs(
        session,
        [
            {
                "type": "confirm",
                "accept": True,
                "text": "",
                "opener_page": "p0",
                "popup_ordinal": 1,
            }
        ],
    ) == [
        {
            "type": "confirm",
            "accept": True,
            "text": "",
            "opener_target_id": "target-main",
            "popup_ordinal": 2,
        }
    ]

    after_action = True
    session.tabs["popup"] = _Tab(
        id="popup",
        label="popup",
        target_id="target-popup-next",
        native_labeled=False,
    )
    await manager._await_replay_postconditions(
        owner,
        session,
        lease,
        [
            {
                "kind": "url",
                "target": "popup",
                "url": "https://id.example/next",
                "page": "p1",
                "opener_page": "p0",
                # This is relative to the start of this replay, not the Host's
                # lifetime-wide popup counter.
                "popup_ordinal": 1,
                "activate": True,
            }
        ],
        source_target_id=source_target_id,
        pre_action_target_ids=pre_action_target_ids,
        workdir="",
    )
    assert lease.page_targets["p1"] == "target-popup-next"
    assert session.active_label == "popup"


def test_replay_allowed_hosts_are_best_effort_diagnostic_metadata():
    assert BrowserManager._normalized_replay_hosts(
        ["App.Example.", "bad/host", "", 42, "例子.测试"]
    ) == frozenset({"app.example", "xn--fsqu00a.xn--0zwm56d"})
    assert BrowserManager._normalized_replay_hosts([]) == frozenset()
    assert BrowserManager._normalized_replay_hosts(None) == frozenset()


def test_replay_url_pattern_matches_runtime_input_and_rotating_nonce():
    condition = {
        "kind": "url",
        "target": "same_tab",
        "origin": "https://app.example",
        "url_pattern": [
            {"literal": "https://app.example/search?q="},
            {"alternatives": ["new+query", "new%20query"]},
            {"literal": "&state="},
            {"wildcard": "query_value"},
            {"literal": "#/results"},
        ],
    }
    assert BrowserManager._replay_url_condition_matches(
        "https://app.example/search?q=new+query&state=ROTATED123#/results",
        condition,
    )
    assert BrowserManager._replay_url_condition_matches(
        "https://app.example/search?q=new%20query&state=other#/results",
        condition,
    )
    assert not BrowserManager._replay_url_condition_matches(
        "https://app.example/search?q=old+query&state=other#/results",
        condition,
    )


async def test_same_tab_pattern_waits_for_navigation_epoch_not_pre_action_url(
    tmp_path,
    monkeypatch,
):
    manager = BrowserManager(
        BrowserConfig(command_timeout_seconds=1, navigation_timeout_seconds=1)
    )
    main = _Tab(id="main", label="main", target_id="target-main")
    session = _Session(
        session_id=SESSION,
        owner=OWNER,
        tabs={"main": main},
        active_label="main",
    )
    owner = _Owner(
        owner=OWNER,
        runtime_key="runtime",
        profile_dir=Path(tmp_path) / "profile",
        sessions={SESSION: session},
    )
    lease = _ReplayLease(
        workflow_id="a" * 64,
        workflow_digest="b" * 64,
        capability_generation=0,
        nonce="replay_nonce_" + "x" * 24,
        tool_call_id="call",
        allowed_hosts=frozenset({"app.example"}),
    )
    guard_calls = 0
    destination = (
        "https://app.example/search?q=new+query&state=ROTATED#/results"
    )

    def marker(epoch: int) -> str:
        return json.dumps(
            {
                "token": "guard",
                "targetId": "target-main",
                "frameId": "frame",
                "loaderId": "loader",
                "href": destination,
                "timeOrigin": 1,
                "navigationEpoch": epoch,
                "navigationPending": False,
                "locationConsistent": True,
                "titleDigest": "title",
            }
        )

    async def select(_owner, _session):
        return main, False

    async def run(_owner, _session, command, args, **_kwargs):
        assert (command, args) == ("tab", ["list"])
        return {
            "data": {
                "tabs": [
                    {
                        "tabId": "t1",
                        "label": "main",
                        "title": "",
                        "url": destination,
                        "type": "page",
                        "active": True,
                        "targetId": "target-main",
                        "openerTargetId": "",
                    }
                ]
            }
        }

    async def page_guard(*_args, **_kwargs):
        nonlocal guard_calls
        guard_calls += 1
        return marker(1 if guard_calls < 3 else 2)

    monkeypatch.setattr(manager, "_select", select)
    monkeypatch.setattr(manager, "_run", run)
    monkeypatch.setattr(manager, "_page_guard", page_guard)
    monkeypatch.setattr(
        manager,
        "_require_replay_action_context",
        lambda *_args, **_kwargs: None,
    )
    await manager._await_replay_postconditions(
        owner,
        session,
        lease,
        [
            {
                "kind": "url",
                "target": "same_tab",
                "origin": "https://app.example",
                "url_pattern": [
                    {"literal": "https://app.example/search?q=new+query&state="},
                    {"wildcard": "query_value"},
                    {"literal": "#/results"},
                ],
            }
        ],
        source_target_id="target-main",
        pre_action_target_ids=frozenset({"target-main"}),
        pre_action_marker=marker(1),
        workdir="",
    )
    assert guard_calls >= 3


async def test_replay_fill_form_sends_one_ordered_selector_batch_to_host(
    tmp_path,
    monkeypatch,
):
    manager = BrowserManager(BrowserConfig(command_timeout_seconds=1))
    tab = _Tab(id="main", label="main", target_id="target-main")
    session = _Session(
        session_id=SESSION,
        owner=OWNER,
        tabs={"main": tab},
        active_label="main",
    )
    owner = _Owner(
        owner=OWNER,
        runtime_key="runtime",
        profile_dir=Path(tmp_path) / "profile",
        sessions={SESSION: session},
    )
    lease = _ReplayLease(
        workflow_id="a" * 64,
        workflow_digest="b" * 64,
        capability_generation=0,
        nonce="replay_nonce_" + "x" * 24,
        tool_call_id="call",
        allowed_hosts=frozenset({"app.example"}),
    )
    fields = [
        {
            "type": "combobox",
            "selector": "#country",
            "value": "cn",
            "select_by": "value",
        },
        {
            "type": "textbox",
            "selector": "#province",
            "value": "云南",
        },
    ]
    batches: list[list[dict]] = []

    async def pre_action_targets(_owner, _session, **_kwargs):
        return "target-main", frozenset({"target-main"}), ""

    async def run_fill_form(_owner, _session, wire_fields, *, workdir=""):
        assert workdir == ""
        batches.append(wire_fields)
        return {"data": {"completed_count": len(wire_fields)}}

    async def publish(*_args, **_kwargs):
        return None

    async def observe(*_args, **_kwargs):
        return "post-form-snapshot"

    async def forbidden_locate(*_args, **_kwargs):
        raise AssertionError("replay must not eagerly locate dependent form fields")

    monkeypatch.setattr(manager, "_replay_pre_action_targets", pre_action_targets)
    monkeypatch.setattr(manager, "_run_fill_form", run_fill_form)
    monkeypatch.setattr(manager, "_publish", publish)
    monkeypatch.setattr(manager, "_observe_after_replay_mutation", observe)
    monkeypatch.setattr(manager, "_replay_locator_native_locked", forbidden_locate)
    monkeypatch.setattr(
        manager,
        "_require_replay_action_context",
        lambda *_args, **_kwargs: None,
    )

    result = await manager._replay_fill_form_locked(
        owner,
        session,
        lease,
        step_index=0,
        step={"kind": "fill_form", "fields": fields},
        workdir="",
    )

    assert result == "post-form-snapshot"
    assert batches == [fields]
    assert batches[0][0] is not fields[0]


async def test_replay_upload_completes_exact_pending_filechooser(
    tmp_path,
    monkeypatch,
):
    manager = BrowserManager(BrowserConfig(command_timeout_seconds=1))
    tab = _Tab(id="main", label="main", target_id="target-main")
    session = _Session(
        session_id=SESSION,
        owner=OWNER,
        tabs={"main": tab},
        active_label="main",
    )
    owner = _Owner(
        owner=OWNER,
        runtime_key="runtime",
        profile_dir=Path(tmp_path) / "profile",
        sessions={SESSION: session},
    )
    lease = _ReplayLease(
        workflow_id="a" * 64,
        workflow_digest="b" * 64,
        capability_generation=0,
        nonce="replay_nonce_" + "x" * 24,
        tool_call_id="call",
        allowed_hosts=frozenset({"app.example"}),
    )
    upload = Path(tmp_path) / "report.pdf"
    upload.write_bytes(b"pdf")
    calls: list[dict[str, object]] = []

    async def pre_action_targets(_owner, _session, **_kwargs):
        return "target-main", frozenset({"target-main"}), ""

    async def upload_with_trigger(_owner, _session, **kwargs):
        calls.append(dict(kwargs))
        return {"data": {}}

    async def forbidden_locate(*_args, **_kwargs):
        raise AssertionError("atomic upload must send persisted selectors directly")

    async def observe(*_args, **_kwargs):
        return "post-upload-snapshot"

    monkeypatch.setattr(manager, "_replay_pre_action_targets", pre_action_targets)
    monkeypatch.setattr(manager, "_replay_locator_native_locked", forbidden_locate)
    monkeypatch.setattr(manager, "_run_upload_with_trigger", upload_with_trigger)
    monkeypatch.setattr(manager, "_observe_after_replay_mutation", observe)
    monkeypatch.setattr(
        manager,
        "_require_replay_action_context",
        lambda *_args, **_kwargs: None,
    )

    result = await manager._replay_upload_locked(
        owner,
        session,
        lease,
        step_index=0,
        step={
            "kind": "upload",
            "selector": "#attachment",
            "trigger_selector": "#styled-upload-button",
            "paths": [str(upload)],
            "multiple": False,
            "accept": ".pdf",
        },
        workdir=str(tmp_path),
    )
    assert result == "post-upload-snapshot"
    assert calls == [{
        "trigger_selector": "#styled-upload-button",
        "input_selector": "#attachment",
        "files": [str(upload.resolve())],
        "workdir": str(tmp_path),
    }]


async def test_replay_upload_delegates_trigger_fallback_to_one_host_transaction(
    tmp_path,
    monkeypatch,
):
    manager = BrowserManager(BrowserConfig(command_timeout_seconds=1))
    tab = _Tab(id="main", label="main", target_id="target-main")
    session = _Session(
        session_id=SESSION,
        owner=OWNER,
        tabs={"main": tab},
        active_label="main",
    )
    owner = _Owner(
        owner=OWNER,
        runtime_key="runtime",
        profile_dir=Path(tmp_path) / "profile",
        sessions={SESSION: session},
    )
    lease = _ReplayLease(
        workflow_id="a" * 64,
        workflow_digest="b" * 64,
        capability_generation=0,
        nonce="replay_nonce_" + "x" * 24,
        tool_call_id="call",
        allowed_hosts=frozenset({"app.example"}),
    )
    upload = Path(tmp_path) / "report.pdf"
    upload.write_bytes(b"pdf")
    calls: list[dict[str, object]] = []

    async def upload_with_trigger(_owner, _session, **kwargs):
        calls.append(dict(kwargs))
        return {"data": {}}

    async def pre_action_targets(_owner, _session, **_kwargs):
        return "target-main", frozenset({"target-main"}), ""

    async def observe(*_args, **_kwargs):
        return "post-upload-snapshot"

    monkeypatch.setattr(
        manager,
        "_replay_pre_action_targets",
        pre_action_targets,
    )
    monkeypatch.setattr(manager, "_run_upload_with_trigger", upload_with_trigger)
    monkeypatch.setattr(manager, "_observe_after_replay_mutation", observe)
    monkeypatch.setattr(
        manager,
        "_require_replay_action_context",
        lambda *_args, **_kwargs: None,
    )

    result = await manager._replay_upload_locked(
        owner,
        session,
        lease,
        step_index=0,
        step={
            "kind": "upload",
            "selector": "#attachment",
            "trigger_selector": "#reveal-upload",
            "paths": [str(upload)],
            "multiple": False,
            "accept": ".pdf",
        },
        workdir=str(tmp_path),
    )
    assert result == "post-upload-snapshot"
    assert calls == [{
        "trigger_selector": "#reveal-upload",
        "input_selector": "#attachment",
        "files": [str(upload.resolve())],
        "workdir": str(tmp_path),
    }]


class _ReplayManager:
    def __init__(self) -> None:
        self.generation = 11
        self.steps: list[dict] = []
        self.events: list[str] = []
        self.fail_kind = ""
        self.partial_count = 0
        self.end_ok = True
        # 非 None 表示"下一步之后进入挂起"。
        self.pending_suspension: dict | None = None
        self.next_resume_token = "tok" * 8
        self.resume_calls: list[dict] = []
        self.resume_error: Exception | None = None

    def capability_generation(self, _owner: str) -> int:
        return self.generation

    async def begin_replay(self, *_args, **_kwargs) -> None:
        self.events.append("begin")

    async def replay_step(self, *_args, step: dict, step_index: int = 0, **_kwargs) -> str:
        self.steps.append(step)
        # 照真实 manager 的行为：挂起态是**执行到挂起型 takeover 之后**才出现的，
        # 不是调用方预先设定的。夹具若一开始就报挂起，测出来的是错的断点。
        if (
            step["kind"] == "takeover"
            and step.get("reason") in {"handoff", "secret"}
        ):
            self.pending_suspension = {
                "resume_token": self.next_resume_token,
                "next_step": step_index + 1,
            }
        if step["kind"] == self.fail_kind:
            raise BrowserDriverError(
                "playwright action failed",
                code="playwright_timeout",
                partial=self.partial_count > 0,
                completed_count=self.partial_count,
            )
        return (
            "real-final-snapshot"
            if step["kind"] == "snapshot_full"
            else f"post-{step['kind']}-snapshot"
        )

    async def end_replay(self, *_args, **kwargs) -> bool:
        self.events.append(str(kwargs["reason"]))
        return self.end_ok

    # 挂起态由 manager 结构化提供，不从 replay_step 的返回串里解析。
    def suspended_replay(self, _owner: str, _session: str) -> dict | None:
        return dict(self.pending_suspension) if self.pending_suspension else None

    async def resume_replay(self, *_args, **kwargs) -> dict:
        self.events.append("resume")
        self.resume_calls.append(dict(kwargs))
        if self.resume_error is not None:
            raise self.resume_error
        resumed = dict(self.pending_suspension or {})
        self.pending_suspension = None
        return {
            "replay_nonce": "resumed-nonce",
            "next_step": int(resumed.get("next_step", 0)),
        }


@pytest.fixture
def replay_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path))
    artifact = _artifact()
    publish_workflow(OWNER, artifact)
    manager = _ReplayManager()
    capability = {"denied": ""}
    tool = RecordReplayTool(
        manager,
        capability_check=lambda: capability["denied"] or None,
    )
    tokens = [
        (current_owner_account_id, current_owner_account_id.set(OWNER)),
        (current_session_id, current_session_id.set(SESSION)),
        (current_tool_call_id, current_tool_call_id.set("replay-call")),
    ]
    try:
        yield tool, manager, artifact, capability
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


async def test_replay_needs_no_approval_and_returns_true_final_snapshot(
    replay_env,
):
    tool, manager, artifact, _capability = replay_env
    args = {
        "workflow_id": artifact.workflow_id,
        "inputs": {
            "email": "person@example.com",
            "department": ["engineering"],
        },
    }
    assert tool.permission_resolver(args) is None
    result = await tool.handler(args)
    payload = json.loads(result.removeprefix("REPLAY_OK: "))
    assert payload["snapshot"] == "real-final-snapshot"
    assert payload["executed_steps"] == len(artifact.payload["plan"])
    assert manager.events == ["begin", "completed"]
    assert [step["kind"] for step in manager.steps] == [
        "navigate",
        "fill_form",
        "click",
        "dblclick",
        "drag",
        "press",
        "scroll",
        "navigate",
        "snapshot_full",
    ]
    assert manager.steps[2]["postconditions"] == [
        {
            "kind": "url",
            "target": "same_tab",
            "url": "https://app.example/continued#/ready",
        }
    ]
    fields = manager.steps[1]["fields"]
    assert fields == [
        {
            "type": "textbox",
            "selector": "#email",
            "value": "person@example.com",
        },
        {
            "type": "combobox",
            "selector": "#department",
            "value": "engineering",
            "select_by": "value",
        },
        {
            "type": "checkbox",
            "selector": "#active",
            "value": True,
        },
    ]


async def test_replay_resolves_runtime_file_paths_and_exact_clear(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("CREW_HOME", str(tmp_path))
    artifact = build_workflow_artifact(
        owner=OWNER,
        hosts=("oa.example",),
        inputs={
            "attachments": {
                "kind": "files",
                "required": True,
                "display_name": "Attachments",
                "recorded_hint": "file upload · multiple=true",
            }
        },
        plan=[
            {
                "kind": "upload",
                "selector": "#attachments",
                "trigger_selector": "#choose-files",
                "input_key": "attachments",
                "multiple": True,
                "accept": ".pdf",
            },
            {
                "kind": "upload",
                "selector": "#temporary",
                "files": [],
                "multiple": False,
                "accept": "",
            },
            {"kind": "snapshot_full"},
        ],
    )
    publish_workflow(OWNER, artifact)
    manager = _ReplayManager()
    tool = RecordReplayTool(manager)
    tokens = [
        (current_owner_account_id, current_owner_account_id.set(OWNER)),
        (current_session_id, current_session_id.set(SESSION)),
        (current_tool_call_id, current_tool_call_id.set("upload-replay-call")),
    ]
    try:
        result = await tool.handler(
            {
                "workflow_id": artifact.workflow_id,
                "inputs": {
                    "attachments": [
                        str(Path(tmp_path) / "first.pdf"),
                        str(Path(tmp_path) / "second.pdf"),
                    ]
                },
            }
        )
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)
    assert result.startswith("REPLAY_OK: ")
    assert manager.steps[:2] == [
        {
            "kind": "upload",
            "selector": "#attachments",
            "paths": [
                str(Path(tmp_path) / "first.pdf"),
                str(Path(tmp_path) / "second.pdf"),
            ],
            "multiple": True,
            "accept": ".pdf",
            "trigger_selector": "#choose-files",
        },
        {
            "kind": "upload",
            "selector": "#temporary",
            "paths": [],
            "multiple": False,
            "accept": "",
        },
    ]


async def test_replay_reports_exact_missing_inputs_without_dispatch(replay_env):
    tool, manager, artifact, _capability = replay_env
    result = await tool.handler(
        {
            "workflow_id": artifact.workflow_id,
            "inputs": {"email": "person@example.com"},
        }
    )
    assert result == (
        'REPLAY_INPUTS_REQUIRED: {"inputs":'
        '{"department":{"display_name":"Department","kind":"select",'
        '"recorded_hint":"select · select-one · name=department"}}}'
    )
    assert manager.steps == []


async def test_replay_handler_uses_recorded_defaults_with_empty_inputs(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("CREW_HOME", str(tmp_path))
    recorded_text = "default\n" + "x" * 600_000
    recorded_select = ["", "alpha", "alpha"]
    recorded_files = [
        "/Users/example/first.pdf",
        "/Users/example/first.pdf",
    ]
    dialogs = [
        {
            "type": "prompt",
            "accept": True,
            "text": "dialog default",
            "page": "p0",
            "label": "main",
        }
    ]
    artifact = build_workflow_artifact(
        owner=OWNER,
        hosts=(),
        inputs={
            "notes": {
                "kind": "text",
                "required": True,
                "display_name": "Notes",
                "recorded_hint": "textarea",
                "default": recorded_text,
            },
            "choices": {
                "kind": "select",
                "required": True,
                "display_name": "Choices",
                "recorded_hint": "select-multiple",
                "default": recorded_select,
            },
            "files": {
                "kind": "files",
                "required": True,
                "display_name": "Files",
                "recorded_hint": "file upload",
                "default": recorded_files,
            },
        },
        plan=[
            {
                "kind": "fill",
                "selector": "#notes",
                "input_key": "notes",
                "page": "p0",
                "dialogs": dialogs,
            },
            {
                "kind": "select",
                "selector": "#choices",
                "input_key": "choices",
            },
            {
                "kind": "upload",
                "selector": "#files",
                "input_key": "files",
                "multiple": True,
                "accept": "",
            },
            {"kind": "snapshot_full"},
        ],
    )
    publish_workflow(OWNER, artifact)
    manager = _ReplayManager()
    tool = RecordReplayTool(manager)
    tokens = [
        (current_owner_account_id, current_owner_account_id.set(OWNER)),
        (current_session_id, current_session_id.set(SESSION)),
        (current_tool_call_id, current_tool_call_id.set("defaults-call")),
    ]
    try:
        result = await tool.handler(
            {"workflow_id": artifact.workflow_id, "inputs": {}}
        )
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)

    assert result.startswith("REPLAY_OK: ")
    assert manager.steps[:3] == [
        {
            "kind": "fill",
            "selector": "#notes",
            "text": recorded_text,
            "page": "p0",
            "dialogs": dialogs,
        },
        {
            "kind": "select",
            "selector": "#choices",
            "values": recorded_select,
        },
        {
            "kind": "upload",
            "selector": "#files",
            "paths": recorded_files,
            "multiple": True,
            "accept": "",
        },
    ]


async def test_replay_reports_partial_batch_completion_and_stops(replay_env):
    tool, manager, artifact, _capability = replay_env
    manager.fail_kind = "fill_form"
    manager.partial_count = 2
    result = await tool.handler(
        {
            "workflow_id": artifact.workflow_id,
            "inputs": {
                "email": "person@example.com",
                "department": "engineering",
            },
        }
    )
    assert result.startswith("REPLAY_PARTIAL: ")
    payload = json.loads(result.removeprefix("REPLAY_PARTIAL: "))
    assert payload["completed_fields"] == 2
    assert payload["executed_steps"] == 1
    assert [step["kind"] for step in manager.steps] == [
        "navigate",
        "fill_form",
    ]
    assert manager.events == ["begin", "failed"]


async def test_replay_halts_on_playwright_failure_without_retry(replay_env):
    tool, manager, artifact, _capability = replay_env
    manager.fail_kind = "click"
    result = await tool.handler(
        {
            "workflow_id": artifact.workflow_id,
            "inputs": {
                "email": "person@example.com",
                "department": "engineering",
            },
        }
    )
    assert result.startswith("REPLAY_HALTED: {")
    assert [step["kind"] for step in manager.steps].count("click") == 1
    assert manager.events == ["begin", "failed"]


async def test_replay_never_claims_success_if_lease_cleanup_fails(replay_env):
    tool, manager, artifact, _capability = replay_env
    manager.end_ok = False
    result = await tool.handler(
        {
            "workflow_id": artifact.workflow_id,
            "inputs": {
                "email": "person@example.com",
                "department": "engineering",
            },
        }
    )
    assert result == "REPLAY_HALTED: replay_lease_cleanup_failed"


async def test_secret_takeover_suspends_rather_than_terminating(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("CREW_HOME", str(tmp_path))
    artifact = build_workflow_artifact(
        owner=OWNER,
        hosts=("oa.example",),
        inputs={},
        plan=[
            {"kind": "navigate", "url": "https://oa.example/login"},
            {"kind": "takeover", "reason": "secret"},
        ],
    )
    publish_workflow(OWNER, artifact)
    manager = _ReplayManager()
    tool = RecordReplayTool(manager)
    tokens = [
        (current_owner_account_id, current_owner_account_id.set(OWNER)),
        (current_session_id, current_session_id.set(SESSION)),
        (current_tool_call_id, current_tool_call_id.set("secret-call")),
    ]
    try:
        result = await tool.handler(
            {"workflow_id": artifact.workflow_id, "inputs": {}}
        )
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)
    # secret/handoff 都是**挂起**而不是终止：用户做完那件只有他能做的事之后，
    # 工作流要能继续。终止只留给 explicit（"到此交还，结束"）。
    assert result.startswith("REPLAY_SUSPENDED: ")
    assert [step["kind"] for step in manager.steps] == [
        "navigate",
        "takeover",
    ]
    payload = json.loads(result.removeprefix("REPLAY_SUSPENDED: "))
    assert payload["resume_token"]
    # 挂起不清租约
    assert "completed" not in manager.events


async def test_assert_state_is_executable_and_declared_as_a_capability(
    tmp_path,
    monkeypatch,
):
    """断言必须是可执行步骤，且必须出现在 capabilities 里。

    replay 侧要求 capabilities **精确等于** plan 里的 step kinds。少声明会被
    `replay_capabilities_invalid` 拒——这条同时验证 assert_state 已经进了词表，
    否则整份 artifact 连发布都过不去。
    """
    monkeypatch.setenv("CREW_HOME", str(tmp_path))
    artifact = build_workflow_artifact(
        owner=OWNER,
        hosts=("oa.example",),
        inputs={},
        plan=[
            {"kind": "navigate", "url": "https://oa.example/list"},
            {"kind": "click", "selector": "#detail"},
            # 点进详情页之后确认真的到了：不确认的话，后面的步骤会在列表页上
            # 依次失败，而真正的原因被埋在最后。
            {"kind": "assert_state", "selector": "#applicant", "state": "visible"},
            {"kind": "snapshot_full"},
        ],
    )
    assert "assert_state" in artifact.payload["capabilities"]
    publish_workflow(OWNER, artifact)
    manager = _ReplayManager()
    tool = RecordReplayTool(manager)
    tokens = [
        (current_owner_account_id, current_owner_account_id.set(OWNER)),
        (current_session_id, current_session_id.set(SESSION)),
        (current_tool_call_id, current_tool_call_id.set("assert-call")),
    ]
    try:
        result = await tool.handler(
            {"workflow_id": artifact.workflow_id, "inputs": {}}
        )
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)
    assert result.startswith("REPLAY_OK: ")
    assert [step["kind"] for step in manager.steps] == [
        "navigate",
        "click",
        "assert_state",
        "snapshot_full",
    ]
    # 断言步骤的 selector 与 state 原样送达执行层
    assert manager.steps[2]["selector"] == "#applicant"
    assert manager.steps[2]["state"] == "visible"


async def test_handle_overlay_is_executable_and_declared(tmp_path, monkeypatch):
    """遮挡处理器是一等公民步骤，且必须出现在 capabilities 里。

    它给了工作流"点掉一个东西"的权力，不能作为隐式行为偷偷发生——
    replay 侧要求 capabilities 精确等于 plan 的 step kinds，这条同时钉住
    handle_overlay 已经进了词表。
    """
    monkeypatch.setenv("CREW_HOME", str(tmp_path))
    artifact = build_workflow_artifact(
        owner=OWNER,
        hosts=("oa.example",),
        inputs={},
        plan=[
            {"kind": "navigate", "url": "https://oa.example/list"},
            # 先注册再动作：公告弹窗可能在任何一步之前出现，注册晚了就白注册。
            {"kind": "handle_overlay", "selector": "#announce-close"},
            {"kind": "click", "selector": "#detail"},
            {"kind": "snapshot_full"},
        ],
    )
    assert "handle_overlay" in artifact.payload["capabilities"]
    publish_workflow(OWNER, artifact)
    manager = _ReplayManager()
    tool = RecordReplayTool(manager)
    tokens = [
        (current_owner_account_id, current_owner_account_id.set(OWNER)),
        (current_session_id, current_session_id.set(SESSION)),
        (current_tool_call_id, current_tool_call_id.set("overlay-call")),
    ]
    try:
        result = await tool.handler(
            {"workflow_id": artifact.workflow_id, "inputs": {}}
        )
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)
    assert result.startswith("REPLAY_OK: ")
    assert [step["kind"] for step in manager.steps] == [
        "navigate",
        "handle_overlay",
        "click",
        "snapshot_full",
    ]
    assert manager.steps[1]["selector"] == "#announce-close"


async def _run_replay(tool, args: dict, call_id: str) -> str:
    tokens = [
        (current_owner_account_id, current_owner_account_id.set(OWNER)),
        (current_session_id, current_session_id.set(SESSION)),
        (current_tool_call_id, current_tool_call_id.set(call_id)),
    ]
    try:
        return await tool.handler(args)
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


async def test_handoff_takeover_suspends_then_resumes_the_rest_of_the_plan(
    tmp_path,
    monkeypatch,
):
    """含验证码的登录流程必须端到端跑通：登录 → 停下等用户 → 续跑读工单。

    这是方案 B 最重要的一条。此前 takeover 是终止性的，`steps_after_takeover_forbidden`
    连编译都不让过——用户填完验证码之后工作流已经不存在了，「登录只需人工介入」
    这个需求在实现上是空的。

    挂起与失败必须是两种不同的结果：`REPLAY_HALTED` 要去排查，
    `REPLAY_SUSPENDED` 要去等人。混成一种，模型会对着一个健康的工作流反复重试。
    """
    monkeypatch.setenv("CREW_HOME", str(tmp_path))
    artifact = build_workflow_artifact(
        owner=OWNER,
        hosts=("oa.example",),
        inputs={},
        plan=[
            {"kind": "navigate", "url": "https://oa.example/login"},
            # 验证码：只有用户能填
            {"kind": "takeover", "reason": "handoff"},
            # **挂起之后仍有步骤**——这正是这条链得以成立的前提
            {"kind": "navigate", "url": "https://oa.example/todo"},
            {"kind": "snapshot_full"},
        ],
    )
    publish_workflow(OWNER, artifact)
    manager = _ReplayManager()
    tool = RecordReplayTool(manager)

    # 第一段：跑到 takeover 就挂起
    first = await _run_replay(
        tool, {"workflow_id": artifact.workflow_id, "inputs": {}}, "call-1"
    )
    assert first.startswith("REPLAY_SUSPENDED: ")
    payload = json.loads(first.removeprefix("REPLAY_SUSPENDED: "))
    assert payload["resume_token"] == "tok" * 8
    assert payload["next_step"] == 2
    assert payload["remaining_steps"] == 2
    # 挂起**不能**走 end_replay：租约要活下来等用户
    assert "completed" not in manager.events
    assert "failed" not in manager.events
    assert [step["kind"] for step in manager.steps] == ["navigate", "takeover"]

    # 第二段：用户填完码，带凭证续跑
    manager.steps.clear()
    second = await _run_replay(
        tool,
        {
            "workflow_id": artifact.workflow_id,
            "inputs": {},
            "resume_token": payload["resume_token"],
        },
        "call-2",
    )
    assert second.startswith("REPLAY_OK: ")
    # 续跑不 begin 新的一段，而是 resume
    assert "resume" in manager.events
    assert manager.events.count("begin") == 1
    # 从 next_step 接着跑，不重跑已经执行过的两步
    assert [step["kind"] for step in manager.steps] == ["navigate", "snapshot_full"]
    assert manager.resume_calls[0]["resume_token"] == payload["resume_token"]
    assert manager.resume_calls[0]["workflow_digest"] == artifact.digest


async def test_resume_failure_is_not_reported_as_success(tmp_path, monkeypatch):
    """凭证不对、过期、会话变了——一律不能当成跑成功。"""
    monkeypatch.setenv("CREW_HOME", str(tmp_path))
    artifact = build_workflow_artifact(
        owner=OWNER,
        hosts=("oa.example",),
        inputs={},
        plan=[
            {"kind": "navigate", "url": "https://oa.example/login"},
            {"kind": "takeover", "reason": "handoff"},
            {"kind": "snapshot_full"},
        ],
    )
    publish_workflow(OWNER, artifact)
    manager = _ReplayManager()
    manager.resume_error = BrowserDriverError(
        "续跑凭证与挂起的回放不匹配", code="replay_resume_mismatch"
    )
    tool = RecordReplayTool(manager)
    result = await _run_replay(
        tool,
        {
            "workflow_id": artifact.workflow_id,
            "inputs": {},
            "resume_token": "bad" * 8,
        },
        "call-bad",
    )
    assert result.startswith("REPLAY_HALTED: ")
    assert "replay_resume_mismatch" in result
    # 一步都没执行
    assert manager.steps == []
