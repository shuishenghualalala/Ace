"""Interactive CLI slash command contract."""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from crew.cli import chat


class FakePrompt:
    def __init__(self, lines: list[str]) -> None:
        self.lines = list(lines)
        self.prompts: list[str] = []

    async def prompt_async(self, message: str) -> str:
        self.prompts.append(message)
        if not self.lines:
            raise EOFError
        return self.lines.pop(0)


class FakePlan:
    def __init__(self) -> None:
        self.entered: list[tuple[str, str]] = []
        self.resets: list[tuple[str, str]] = []

    def enter(self, session_id: str, *, owner_account_id: str) -> None:
        self.entered.append((session_id, owner_account_id))

    def reset(self, session_id: str, *, owner_account_id: str) -> None:
        self.resets.append((session_id, owner_account_id))


class FakePlugins:
    def __init__(self) -> None:
        self.commands: list[str] = []

    async def run_plugin_command(self, text: str, **_kwargs):
        self.commands.append(text)
        return None


@pytest.mark.asyncio
async def test_repl_dispatches_every_builtin_slash_command(monkeypatch) -> None:
    prompt = FakePrompt(["/help", "/new", "/team", "/agent", "/plan", "/todo", "/compact", "/unknown", "/quit"])
    plan = FakePlan()
    plugins = FakePlugins()
    printed: list[str] = []
    todos: list[str] = []
    compacted: list[str] = []

    monkeypatch.setattr(chat, "PromptSession", lambda: prompt)
    monkeypatch.setattr(chat, "patch_stdout", lambda: nullcontext())
    monkeypatch.setattr(chat.console, "print", lambda *args, **_kwargs: printed.append(" ".join(map(str, args))))
    monkeypatch.setattr(chat, "_print_todos", lambda app, session_id, owner: todos.append(session_id))
    async def fake_compact(app, session_id, owner):
        compacted.append(session_id)
    monkeypatch.setattr(chat, "_compact_context", fake_compact)

    app = SimpleNamespace(
        config=SimpleNamespace(has_llm_key=False),
        plan_manager=plan,
        plugins=plugins,
    )
    ctx = SimpleNamespace(owner="local", workspace_id="default")
    await chat._repl(app, ctx, session_id="cli_original")

    assert plan.resets[0][0] == "cli_original"
    assert len(plan.entered) == 1
    new_session, owner = plan.entered[0]
    assert new_session.startswith("cli_") and new_session != "cli_original"
    assert owner == "local"
    assert todos == [new_session]
    assert compacted == [new_session]
    assert plugins.commands == ["/unknown"]
    assert any("[team]" in line for line in prompt.prompts)
    assert any("[agent]" in line for line in prompt.prompts)
    assert any("未知命令" in line for line in printed)
    assert any("已新建会话" in line for line in printed)


@pytest.mark.asyncio
async def test_repl_plan_without_manager_and_exit_alias(monkeypatch) -> None:
    prompt = FakePrompt(["/plan", "/exit"])
    plugins = FakePlugins()
    printed: list[str] = []
    monkeypatch.setattr(chat, "PromptSession", lambda: prompt)
    monkeypatch.setattr(chat, "patch_stdout", lambda: nullcontext())
    monkeypatch.setattr(chat.console, "print", lambda *args, **_kwargs: printed.append(" ".join(map(str, args))))
    app = SimpleNamespace(config=SimpleNamespace(has_llm_key=False), plan_manager=None, plugins=plugins)
    await chat._repl(app, SimpleNamespace(owner="local", workspace_id="default"), session_id="cli_edge")

    assert any("Plan 模式不可用" in line for line in printed)
    assert plugins.commands == []
    assert len(prompt.prompts) == 2
