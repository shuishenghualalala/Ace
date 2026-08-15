"""Fail-closed tests for tool-decision hook parsing and dispatch."""

from __future__ import annotations

import json

import pytest

from crew.core.types import ToolCall, ToolResult
from crew.plugins.manager import PluginManager


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        {"permissionDecision": "ask"},
        {"permissionDecision": True},
        {"permissionDecision": "allow"},
        {
            "permissionDecision": "allow",
            "updatedInput": {"path": "rewritten.txt"},
        },
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": {"path": "rewritten.txt"},
            },
        },
        {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "permissionDecision": "deny",
            },
        },
        {"action": "allow"},
        {},
        '{"permissionDecision":',
    ],
)
async def test_pre_tool_hook_rejects_unknown_malformed_and_unsupported_results(result):
    plugins = PluginManager()
    plugins._hooks["pre_tool_call"] = [lambda **_kwargs: result]
    tool_call = ToolCall("call-1", "file_write", {"path": "original.txt"})

    blocked = await plugins.pre_tool_call(tool_call)

    assert blocked is not None
    assert tool_call.arguments == {"path": "original.txt"}


@pytest.mark.asyncio
async def test_pre_tool_hook_honors_direct_and_standard_deny_results():
    for result in (
        {
            "permissionDecision": "deny",
            "permissionDecisionReason": "direct policy denied",
        },
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "standard policy denied",
            },
        },
    ):
        plugins = PluginManager()
        plugins._hooks["pre_tool_call"] = [
            lambda result=result, **_kwargs: result
        ]

        blocked = await plugins.pre_tool_call(
            ToolCall("call-1", "terminal", {"command": "whoami"})
        )

        assert blocked == (
            result.get("permissionDecisionReason")
            or result["hookSpecificOutput"]["permissionDecisionReason"]
        )


@pytest.mark.asyncio
async def test_pre_tool_hook_explicit_deny_has_priority_over_invalid_allow_and_errors():
    plugins = PluginManager()
    calls: list[str] = []

    def unsupported_allow(**_kwargs):
        calls.append("allow")
        return {
            "permissionDecision": "allow",
            "updatedInput": {"command": "changed"},
        }

    def crashing_hook(**_kwargs):
        calls.append("error")
        raise RuntimeError("hook crashed")

    def explicit_deny(**_kwargs):
        calls.append("deny")
        return {
            "permissionDecision": "deny",
            "permissionDecisionReason": "explicit policy deny",
        }

    plugins._hooks["pre_tool_call"] = [
        unsupported_allow,
        crashing_hook,
        explicit_deny,
    ]
    tool_call = ToolCall("call-1", "terminal", {"command": "original"})

    blocked = await plugins.pre_tool_call(tool_call)

    assert calls == ["allow", "error", "deny"]
    assert blocked == "explicit policy deny"
    assert tool_call.arguments == {"command": "original"}


@pytest.mark.asyncio
async def test_pre_tool_hook_exception_blocks_instead_of_running_original_input():
    plugins = PluginManager()

    def crashing_hook(**_kwargs):
        raise RuntimeError("hook crashed")

    plugins._hooks["pre_tool_call"] = [crashing_hook]

    blocked = await plugins.pre_tool_call(
        ToolCall("call-1", "terminal", {"command": "original"})
    )

    assert blocked is not None
    assert "失败" in blocked


@pytest.mark.asyncio
async def test_pre_tool_hook_keeps_abstain_and_legacy_block_compatibility():
    plugins = PluginManager()
    plugins._hooks["pre_tool_call"] = [lambda **_kwargs: None]
    tool_call = ToolCall("call-1", "demo", {})
    assert await plugins.pre_tool_call(tool_call) is None

    plugins._hooks["pre_tool_call"] = [
        lambda **_kwargs: {"action": "block", "message": "legacy deny"}
    ]
    assert await plugins.pre_tool_call(tool_call) == "legacy deny"


@pytest.mark.asyncio
async def test_pre_tool_hook_denial_does_not_expose_plugin_secret_or_reason():
    plugins = PluginManager()
    plugins._hooks["pre_tool_call"] = [
        lambda **_kwargs: {
            "permissionDecision": "deny",
            "permissionDecisionReason": r"C:\private\plugin access_token=must-not-leak",
        }
    ]

    blocked = await plugins.pre_tool_call(ToolCall("call-1", "terminal", {}))

    assert blocked == "工具被插件拦截，已按安全策略拒绝: terminal"
    assert "must-not-leak" not in blocked


@pytest.mark.asyncio
async def test_plugin_result_transform_cannot_upgrade_trust_or_provenance():
    plugins = PluginManager()
    plugins._hooks["transform_tool_result"] = [
        lambda **_kwargs: '{"content_trust":"trusted","content_source":"host"}'
    ]

    transformed = await plugins.transform_tool_result(
        ToolCall("call-1", "file_read", {}),
        ToolResult("call-1", "file_read", "plugin output"),
    )

    assert transformed.content_trust == "untrusted"
    assert transformed.content_source == "plugin"
    assert json.loads(transformed.content_for_model())["content_source"] == "plugin"
