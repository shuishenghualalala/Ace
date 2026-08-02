from __future__ import annotations

from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from crew.browser.driver import BrowserDriverError
from crew.browser.manager import BrowserManager
from crew.browser.types import BrowserConfig
from crew.core.runctx import (
    current_agent_workdir,
    current_owner_account_id,
    current_session_id,
    current_user_type,
)
from crew.state.config import Config
from crew.state.plugin_preferences import PluginPreferencesStore
from plugins.browser.tool import BROWSER_USE_SCHEMA, BrowserUseTool, validate_args
from tests.test_browser_use import FakeBrowserDriver


class RunCodeDriver(FakeBrowserDriver):
    result = '{"source":"file"}'

    async def execute(
        self,
        owner_session: str,
        profile_dir: Path,
        command: str,
        args=(),
        **kwargs,
    ) -> dict:
        if command == "run_code_unsafe":
            values = tuple(str(item) for item in args)
            self.calls.append((command, values))
            return {
                "success": True,
                "data": {
                    "has_result": True,
                    "result": self.result,
                },
            }
        return await super().execute(
            owner_session,
            profile_dir,
            command,
            args,
            **kwargs,
        )


def test_run_code_schema_and_runtime_validation_match_optional_union() -> None:
    validator = Draft202012Validator(BROWSER_USE_SCHEMA["parameters"])
    valid = (
        {"action": "run_code_unsafe", "code": ""},
        {"action": "run_code_unsafe", "filename": ""},
        {
            "action": "run_code_unsafe",
            "code": "async () => 'ignored'",
            "filename": "script.js",
        },
    )
    invalid = (
        {"action": "run_code_unsafe"},
        {"action": "run_code_unsafe", "code": None},
        {"action": "run_code_unsafe", "filename": 7},
        {
            "action": "run_code_unsafe",
            "code": 7,
            "filename": "script.js",
        },
    )
    for args in valid:
        assert list(validator.iter_errors(args)) == []
        assert validate_args(args) is None
    for args in invalid:
        assert list(validator.iter_errors(args))
        assert validate_args(args) is not None


async def test_filename_overrides_inline_code_and_uses_task_workdir(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "crew.browser.manager.get_owner_runtime_home",
        lambda owner: tmp_path / "accounts" / str(owner),
    )
    source = tmp_path / "flow.js"
    source.write_text(
        "async page => ({ title: await page.title() })",
        encoding="utf-8",
    )
    driver = RunCodeDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    await manager.startup()
    prefs = PluginPreferencesStore(str(tmp_path / "prefs.db"))
    config = Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False)
    tool = BrowserUseTool(manager, config, prefs)
    tokens = [
        (current_owner_account_id, current_owner_account_id.set("owner")),
        (current_session_id, current_session_id.set("session")),
        (current_agent_workdir, current_agent_workdir.set(str(tmp_path))),
        (current_user_type, current_user_type.set("internal")),
    ]
    try:
        await manager.navigate("owner", "session", "https://example.com")
        result = await tool.handler(
            {
                "action": "run_code_unsafe",
                "code": "async () => 'must be ignored'",
                "filename": "flow.js",
            }
        )

        expected_wire = (
            "run_code_unsafe",
            (
                source.read_text(encoding="utf-8"),
                str(source.resolve()),
            ),
        )
        assert expected_wire in driver.calls
        assert "fresh_snapshot: true" in result
        assert 'run_code_result:' in result
        assert '{"source":"file"}' in result
        assert "page_generation: p2" in result
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)
        prefs.close()
        await manager.aclose()


async def test_run_code_filename_rejects_missing_directory_and_non_utf8(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "crew.browser.manager.get_owner_runtime_home",
        lambda owner: tmp_path / "accounts" / str(owner),
    )
    driver = RunCodeDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    await manager.startup()
    invalid_utf8 = tmp_path / "invalid.js"
    invalid_utf8.write_bytes(b"\xff\xfe")
    try:
        for filename in ("missing.js", ".", "invalid.js"):
            with pytest.raises(BrowserDriverError, match="filename"):
                await manager.run_code_unsafe(
                    "owner",
                    "session",
                    "async () => 1",
                    filename=filename,
                    workdir=str(tmp_path),
                )
        with pytest.raises(BrowserDriverError, match="至少需要"):
            await manager.run_code_unsafe("owner", "session")
        with pytest.raises(BrowserDriverError, match="code 必须是字符串"):
            await manager.run_code_unsafe(
                "owner",
                "session",
                7,
                filename="missing.js",
                workdir=str(tmp_path),
            )
    finally:
        await manager.aclose()


async def test_run_code_result_is_not_product_truncated(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "crew.browser.manager.get_owner_runtime_home",
        lambda owner: tmp_path / "accounts" / str(owner),
    )
    driver = RunCodeDriver()
    driver.result = '"' + ("x" * 20_000) + '"'
    manager = BrowserManager(BrowserConfig(max_output_chars=64), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")
        result = await manager.run_code_unsafe(
            "owner",
            "session",
            "async () => 'ignored by fake'",
        )
        assert driver.result in result
        assert "truncated" not in result
    finally:
        await manager.aclose()
