"""T04-only regression tests for provenance, lifecycle and resource budgets."""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest


def test_hardened_runtime_manifest_requires_source_provenance(tmp_path, monkeypatch):
    from crew.security import launch

    helper = tmp_path / "ace-security-runtime"
    helper.write_bytes(b"runtime")
    (tmp_path / "runtime-manifest.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "binary_name": helper.name,
                "binary_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(launch, "_runtime_requires_hardened_directory", lambda _path: True)
    unrelated_temp_root = tmp_path / "unrelated-temp-root"
    unrelated_temp_root.mkdir()
    monkeypatch.setattr(
        launch.tempfile,
        "gettempdir",
        lambda: str(unrelated_temp_root),
    )
    monkeypatch.setattr(launch, "_desktop_runtime_binding", lambda: None)

    with pytest.raises(launch.HelperIntegrityError, match="source provenance"):
        launch.verify_helper_integrity(helper)


@pytest.mark.asyncio
async def test_runtime_inactivity_budget_is_monotonic_and_fail_closed():
    from crew.security.runtime_client import _activity_timeout

    now = asyncio.get_running_loop().time()
    assert _activity_timeout(now + 10, now, 2.0) <= 2.0
    with pytest.raises(TimeoutError):
        _activity_timeout(now + 10, now - 3, 2.0)


def test_agent_file_change_and_parallel_budgets_are_bounded(tmp_path):
    from crew.agent.file_changes import changes_between_snapshots
    from crew.agent.loop.tool_runner import ToolRunner

    before = {
        str(tmp_path / f"before-{index}"): (index, 1)
        for index in range(4)
    }
    after = {
        str(tmp_path / f"after-{index}"): (index, 1)
        for index in range(4)
    }
    assert len(changes_between_snapshots(before, after, max_items=2)) == 2
    runner = ToolRunner(None, None, None, max_parallel_tool_calls=999)
    assert runner.max_parallel_tool_calls == 8
