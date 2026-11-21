"""Startup warm-up behavior that does not gate readiness or admission."""

from __future__ import annotations

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
