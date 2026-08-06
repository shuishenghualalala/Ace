from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from crew.browser.driver import BrowserDriverError
from crew.browser.manager import BrowserManager
from crew.browser.types import BrowserConfig
from crew.core.runctx import current_tool_call_id
from plugins.browser.tool import BROWSER_USE_SCHEMA, BrowserUseTool, validate_args
from tests.test_browser_manager_review import FakeProxy, ReviewDriver


class FillFormDriver(ReviewDriver):
    def __init__(self) -> None:
        super().__init__()
        self.snapshot_text = (
            '- textbox "Account name" [ref=e1]\n'
            '- generic "Rich biography" [ref=e2]\n'
            '- combobox "Country" [ref=e3]\n'
            '- checkbox "Accept terms" [ref=e4]\n'
            '- radio "Plan A" [ref=e5]\n'
            '- slider "Volume" [ref=e6]'
        )
        self.ref_action_kinds_override["@e2"] = "input"
        self.ref_content_editable_override["@e2"] = True
        self.ref_action_kinds_override["@e6"] = "input"
        self.form_payloads: list[list[dict[str, Any]]] = []
        self.form_timeouts: list[float] = []
        self.form_error: BrowserDriverError | None = None
        self.emit_derived_echoes = False

    async def fill_form(
        self,
        owner_session: str,
        profile_dir: Path,
        fields: list[dict[str, Any]],
        *,
        target_id: str,
        timeout: float,
        proxy_url: str = "",
        download_dir: Path | None = None,
    ) -> dict[str, Any]:
        del owner_session, profile_dir, target_id, proxy_url, download_dir
        self.form_timeouts.append(timeout)
        self.form_payloads.append([dict(field) for field in fields])
        if self.form_error is not None:
            raise self.form_error
        if self.emit_derived_echoes:
            # None of these strings equals the submitted source. Exact-value
            # replacement, URL decoding or case folding cannot prove them safe.
            formatted = "138 0013 8000"
            digest = "sha256:7a8b9c-derived"
            encoded = "MTM4MDAxMzgwMDA="
            self.snapshot_text = (
                f'- text "confirmed {formatted}"\n'
                f'- status "stored {digest}"\n'
                f'- note "encoded {encoded}"'
            )
            self.ref_action_kinds_override.clear()
            self.ref_content_editable_override.clear()
            page = self._active_page()
            page["title"] = f"saved:{formatted}:{digest}"
            page["url"] = f"https://example.com/result?derived={encoded}"
        return {"success": True, "data": {"completed_count": len(fields)}}


@pytest.fixture
def fill_form_env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "crew.browser.manager.get_owner_runtime_home",
        lambda owner: tmp_path / "accounts" / str(owner),
    )
    monkeypatch.setattr("crew.browser.manager.LoopbackPolicyProxy", FakeProxy)
    driver = FillFormDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    return manager, driver


def fields() -> list[dict[str, Any]]:
    return [
        {"type": "textbox", "ref": "p1:e1", "value": "private-account"},
        {"type": "textbox", "ref": "p1:e2", "value": "private-rich-text"},
        {
            "type": "combobox",
            "ref": "p1:e3",
            "value": "China",
            "select_by": "label",
        },
        {"type": "checkbox", "ref": "p1:e4", "value": True},
        {"type": "radio", "ref": "p1:e5", "value": True},
        {"type": "slider", "ref": "p1:e6", "value": "75"},
    ]


async def test_manager_batch_uses_one_native_transaction_without_extra_approval(
    fill_form_env,
):
    manager, driver = fill_form_env
    token = current_tool_call_id.set("fill-form-exact")
    try:
        await manager.startup()
        await manager.navigate("owner", "session", "https://example.com/form")
        args = {"fields": fields()}

        decision = manager.permission_for(
            "browser_fill_form", args, "owner", "session"
        )
        assert decision is None
        output = await manager.fill_form(
            "owner",
            "session",
            fields(),
        )

        assert "page_generation: p2" in output
        assert len(driver.form_payloads) == 1
        assert [field["ref"] for field in driver.form_payloads[0]] == [
            "@e1",
            "@e2",
            "@e3",
            "@e4",
            "@e5",
            "@e6",
        ]
        assert driver.form_payloads[0][2]["select_by"] == "label"
        assert driver.form_payloads[0][5] == {
            "type": "slider",
            "ref": "@e6",
            "value": "75",
        }
        session = manager._owners["owner"].sessions["session"]
        assert session.last_action == "批量填写 6 项"
        assert session.refs
        assert session.page_marker == ""
    finally:
        current_tool_call_id.reset(token)
        await manager.aclose()


async def test_manager_batch_rpc_budget_scales_for_sequential_slow_fields(
    fill_form_env,
):
    manager, driver = fill_form_env
    try:
        await manager.startup()
        await manager.navigate("owner", "session", "https://example.com/form")
        two_slow_fields = fields()[:2]

        await manager.fill_form("owner", "session", two_slow_fields)

        # Default per-action command budget is 30s. Two sequential native
        # actions get both budgets plus bridge response headroom.
        assert driver.form_timeouts[-1] == 65.0
        assert driver.form_payloads[-1] == [
            {"type": "textbox", "ref": "@e1", "value": "private-account"},
            {"type": "textbox", "ref": "@e2", "value": "private-rich-text"},
        ]
    finally:
        await manager.aclose()


async def test_empty_fill_form_is_an_official_noop(fill_form_env):
    manager, driver = fill_form_env
    try:
        await manager.startup()
        await manager.navigate("owner", "session", "https://example.com/form")

        output = await manager.fill_form("owner", "session", [])

        assert "page_generation: p2" in output
        assert driver.form_payloads[-1] == []
        assert manager._owners["owner"].sessions["session"].last_action == (
            "批量填写 0 项"
        )
    finally:
        await manager.aclose()


async def test_manager_rejects_stale_generation_and_defers_live_capability_to_driver(
    fill_form_env,
):
    manager, driver = fill_form_env
    token = current_tool_call_id.set("fill-form-prevalidation")
    try:
        await manager.startup()
        await manager.navigate("owner", "session", "https://example.com/form")

        stale = fields()
        stale[1]["ref"] = "p2:e2"
        denied = manager.permission_for(
            "browser_fill_form",
            {"fields": stale},
            "owner",
            "session",
        )
        assert denied is not None and denied.behavior == "deny"

        wrong_kind = fields()
        wrong_kind[0]["ref"] = "p1:e3"
        wrong_kind[2]["ref"] = "p1:e1"
        decision = manager.permission_for(
            "browser_fill_form",
            {"fields": wrong_kind},
            "owner",
            "session",
        )
        assert decision is None

        with pytest.raises(BrowserDriverError, match="ref 已失效"):
            await manager.fill_form("owner", "session", stale)
        assert driver.form_payloads == []
    finally:
        current_tool_call_id.reset(token)
        await manager.aclose()


async def test_manager_partial_batch_invalidates_refs_and_preserves_completed_count(
    fill_form_env,
):
    manager, driver = fill_form_env
    token = current_tool_call_id.set("fill-form-partial")
    try:
        await manager.startup()
        await manager.navigate("owner", "session", "https://example.com/form")
        driver.form_error = BrowserDriverError(
            "批量表单已确认完成 2/6 项；后续项失败，未自动提交",
            code="stale_ref_security",
            phase="partial",
            partial=True,
            completed_count=2,
        )

        with pytest.raises(BrowserDriverError) as raised:
            await manager.fill_form("owner", "session", fields())

        assert raised.value.partial is True
        assert raised.value.uncertain is False
        assert raised.value.completed_count == 2
        session = manager._owners["owner"].sessions["session"]
        assert session.refs == {}
        assert session.page_marker == ""
    finally:
        current_tool_call_id.reset(token)
        await manager.aclose()


def test_fill_form_schema_and_validation_are_strict():
    schema = BROWSER_USE_SCHEMA["parameters"]
    validator = Draft202012Validator(schema)
    valid = {"action": "fill_form", "fields": fields()}
    # Schema examples use arbitrary generation refs; semantic ownership is
    # enforced later by BrowserManager.
    assert list(validator.iter_errors(valid)) == []
    assert validate_args(valid) is None
    empty = {"action": "fill_form", "fields": []}
    assert list(validator.iter_errors(empty)) == []
    assert validate_args(empty) is None

    empty_option = {
        "action": "fill_form",
        "fields": [
            {
                "type": "combobox",
                "ref": "p1:e3",
                "value": "",
                "select_by": "value",
            },
        ],
    }
    assert list(validator.iter_errors(empty_option)) == []
    assert validate_args(empty_option) is None

    ambiguous_select = {
        "action": "fill_form",
        "fields": [
            {"type": "combobox", "ref": "p1:e3", "value": "China"},
        ],
    }
    assert list(validator.iter_errors(ambiguous_select))
    assert "select_by" in str(validate_args(ambiguous_select))

    coerced_boolean = {
        "action": "fill_form",
        "fields": [
            {"type": "checkbox", "ref": "p1:e4", "value": "true"},
        ],
    }
    assert list(validator.iter_errors(coerced_boolean))
    assert "boolean" in str(validate_args(coerced_boolean))

    duplicate = {
        "action": "fill_form",
        "fields": [
            {"type": "textbox", "ref": "p1:e1", "value": "a"},
            {"type": "textbox", "ref": "p1:e1", "value": "b"},
        ],
    }
    assert validate_args(duplicate) is None

    slider = {
        "action": "fill_form",
        "fields": [{"type": "slider", "ref": "p1:e6", "value": "42"}],
    }
    assert list(validator.iter_errors(slider)) == []
    assert validate_args(slider) is None


async def test_manager_preserves_empty_combobox_option_value(fill_form_env):
    manager, driver = fill_form_env
    try:
        await manager.startup()
        await manager.navigate("owner", "session", "https://example.com/form")

        await manager.fill_form(
            "owner",
            "session",
            [
                {
                    "type": "combobox",
                    "ref": "p1:e3",
                    "value": "",
                    "select_by": "value",
                }
            ],
        )

        assert driver.form_payloads[-1] == [
            {
                "type": "combobox",
                "ref": "@e3",
                "value": "",
                "select_by": "value",
            }
        ]
    finally:
        await manager.aclose()


async def test_batch_returns_real_final_snapshot_when_page_transforms_values(
    fill_form_env,
):
    manager, driver = fill_form_env
    token = current_tool_call_id.set("fill-form-derived-echo")
    try:
        await manager.startup()
        await manager.navigate("owner", "session", "https://example.com/form")
        driver.emit_derived_echoes = True
        args = {"fields": fields()}
        args["fields"][0]["value"] = "13800138000"
        assert manager.permission_for(
            "browser_fill_form", args, "owner", "session"
        ) is None
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        manager._subscribers[("owner", "session")] = {queue}

        output = await manager.fill_form(
            "owner",
            "session",
            args["fields"],
        )

        published = []
        while not queue.empty():
            published.append(queue.get_nowait())
        public_material = json.dumps(
            {"output": output, "events": published, "state": manager.state("owner", "session")},
            ensure_ascii=False,
        )
        assert "13800138000" not in public_material
        for derived in (
            "138 0013 8000",
            "sha256:7a8b9c-derived",
            "MTM4MDAxMzgwMDA=",
        ):
            assert derived in public_material
        assert "page_generation: p2" in output
        assert manager._owners["owner"].sessions["session"].page_marker == ""
    finally:
        current_tool_call_id.reset(token)
        await manager.aclose()


async def test_tool_batch_result_is_final_snapshot_only_and_failure_reports_count(
    fill_form_env,
):
    manager, _driver = fill_form_env
    tool = BrowserUseTool(manager, object(), None)
    snapshot = (
        "<untrusted_browser_content>\n"
        "page_generation: p2\n"
        '- textbox "Account" [ref=p2:e1]\n'
        "</untrusted_browser_content>"
    )
    assert tool._action_result("fill_form", snapshot) == snapshot

    error = BrowserDriverError(
        "批量表单已确认完成 2/6 项；后续项失败，未自动提交",
        code="stale_ref_security",
        phase="partial",
        partial=True,
        completed_count=2,
    )
    wrapped = await tool._failure_with_evidence(
        error,
        "fill_form",
        "owner",
        "session",
        "",
    )
    message = str(wrapped)
    assert "status: partial" in message
    assert "completed_count: 2" in message
    assert wrapped.completed_count == 2
