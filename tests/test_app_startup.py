"""Startup warm-up behavior that does not gate readiness or admission."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from crew.app import CrewApp


async def test_skills_warmup_uses_the_process_skill_index(monkeypatch):
    calls: list[str] = []

    class Index:
        async def aget(self) -> dict:
            calls.append("aget")
            return {}

    monkeypatch.setattr("crew.agent.skills.index.skill_index", lambda: Index())
    app = object.__new__(CrewApp)

    await app._warm_skills_index()

    assert calls == ["aget"]


async def test_skills_warmup_failure_is_best_effort(monkeypatch):
    class Index:
        async def aget(self) -> dict:
            raise RuntimeError("skill scan failed")

    monkeypatch.setattr("crew.agent.skills.index.skill_index", lambda: Index())
    app = object.__new__(CrewApp)

    await app._warm_skills_index()


def test_subagent_contract_import_does_not_eagerly_load_implementations():
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    sources = [str(root), *(str(path) for path in sorted((root / "packages").glob("*/src")))]
    if env.get("PYTHONPATH"):
        sources.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(sources)
    code = """
import sys
from crew.agent.subagent.service import SUBAGENT_SERVICE_KEY, SubagentService
import crew.agent.subagent as package
assert SUBAGENT_SERVICE_KEY.name == 'subagent'
assert package.SubagentService is SubagentService
assert 'crew.agent.subagent.feature' not in sys.modules
assert 'crew.agent.subagent.runtime' not in sys.modules
assert 'crew.agent.subagent.tools' not in sys.modules
assert package.build_subagent_feature is not None
assert 'crew.agent.subagent.feature' in sys.modules
for name in package.__all__:
    getattr(package, name)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
