import json

from crew.browser.tools import BROWSER_SCHEMAS, register_browser_tools
from crew.core.types import Message, ToolCall
from crew.tools.registry import Registry, tool_result
from crew.tools.tool_search import (
    TOOL_SEARCH_NAME,
    ToolSearchConfig,
    assemble_tool_schemas,
    available_deferred_tools_message,
    dispatch_bridge_tool,
    expand_discovered_tool_schemas,
    extract_discovered_tool_names,
)


class _AvailableBrowserManager:
    @staticmethod
    def available() -> bool:
        return True

    @staticmethod
    def permission_for(*_args, **_kwargs):
        return None

    @staticmethod
    def confirm_approval(*_args, **_kwargs) -> bool:
        return True


def _registry() -> Registry:
    registry = Registry()
    registry.register(
        name="file_read",
        toolset="file",
        schema={"name": "file_read", "description": "Read files", "parameters": {"type": "object"}},
        handler=lambda _args: tool_result(content="file"),
    )
    registry.register(
        name="cron_create",
        toolset="cron",
        schema={
            "name": "cron_create",
            "description": "Create scheduled jobs",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
        handler=lambda args: tool_result(created=args.get("name")),
    )
    registry.register(
        name="web_search",
        toolset="web",
        schema={
            "name": "web_search",
            "description": "Search web pages",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        },
        handler=lambda _args: tool_result(results=[]),
        should_defer=True,
    )
    registry.register(
        name="demo__mcp",
        toolset="mcp:demo",
        schema={"name": "demo__mcp", "description": "MCP demo", "parameters": {"type": "object"}},
        handler=lambda _args: tool_result(ok=True),
        is_mcp=True,
    )
    registry.register(
        name="wiki_search",
        toolset="wiki.read",
        schema={
            "name": "wiki_search",
            "description": "Search Wiki pages",
            "parameters": {"type": "object"},
        },
        handler=lambda _args: tool_result(results=[]),
    )
    registry.register(
        name="wiki_create_page",
        toolset="wiki.manage",
        schema={
            "name": "wiki_create_page",
            "description": "Create a Wiki page",
            "parameters": {"type": "object"},
        },
        handler=lambda _args: tool_result(ok=True),
    )
    return registry


def _names(schemas):
    return {schema["function"]["name"] for schema in schemas}


def test_default_scope_defers_cron_and_wiki_toolsets():
    assembly = assemble_tool_schemas(_registry().list_schemas(), config=ToolSearchConfig())

    names = _names(assembly.tool_schemas)
    assert assembly.activated is True
    assert {"cron_create", "wiki_search", "wiki_create_page"}.isdisjoint(names)
    assert {"file_read", "web_search", "demo__mcp", TOOL_SEARCH_NAME} <= names
    assert "tool_describe" not in names
    assert "tool_call" not in names
    assert assembly.categories == {"cron": 1, "wiki.manage": 1, "wiki.read": 1}


def test_available_deferred_tools_message_lists_only_scoped_names():
    registry = _registry()
    scoped = registry.list_schemas(
        enabled_tools=["file_read", "cron_create", "wiki_search"],
    )
    assembly = assemble_tool_schemas(scoped, config=ToolSearchConfig())

    assert available_deferred_tools_message(assembly) == (
        "<available-deferred-tools>\n"
        "cron_create\n"
        "wiki_search\n"
        "</available-deferred-tools>"
    )


def test_tool_search_result_expands_real_schema_for_next_model_request():
    registry = _registry()
    assembly = assemble_tool_schemas(registry.list_schemas(), config=ToolSearchConfig())

    result = dispatch_bridge_tool(
        ToolCall("search", TOOL_SEARCH_NAME, {"query": "select:cron_create"}),
        original_tool_schemas=assembly.original_tool_schemas,
        config=assembly.config,
    )
    matches = json.loads(result.content)["matches"]
    discovered = {item["name"] for item in matches}
    expanded = expand_discovered_tool_schemas(assembly, discovered)

    assert discovered == {"cron_create"}
    assert "cron_create" in _names(expanded)
    cron_schema = next(s for s in expanded if s["function"]["name"] == "cron_create")
    assert cron_schema["function"]["parameters"]["required"] == ["name"]
    assert not any(key.startswith("_crew_") for key in cron_schema)


def test_search_history_restores_discovered_tools_on_next_user_turn():
    registry = _registry()
    assembly = assemble_tool_schemas(registry.list_schemas(), config=ToolSearchConfig())
    content = dispatch_bridge_tool(
        ToolCall("search", TOOL_SEARCH_NAME, {"query": "cron"}),
        original_tool_schemas=assembly.original_tool_schemas,
        config=assembly.config,
    ).content
    messages = [Message.tool("search", content, name=TOOL_SEARCH_NAME)]

    restored = extract_discovered_tool_names(
        messages,
        original_tool_schemas=assembly.original_tool_schemas,
        config=assembly.config,
    )

    assert restored == {"cron_create"}
    assert "cron_create" in _names(expand_discovered_tool_schemas(assembly, restored))


def test_search_cannot_widen_pre_filtered_session_scope():
    registry = _registry()
    scoped = registry.list_schemas(enabled_tools=["cron_create"])
    assembly = assemble_tool_schemas(scoped, config=ToolSearchConfig())

    result = dispatch_bridge_tool(
        ToolCall("search", TOOL_SEARCH_NAME, {"query": "select:web_search,cron_create"}),
        original_tool_schemas=assembly.original_tool_schemas,
        config=assembly.config,
    )

    assert [item["name"] for item in json.loads(result.content)["matches"]] == ["cron_create"]


def test_explicit_non_core_policy_can_still_defer_browser_tools():
    registry = Registry()
    register_browser_tools(registry, _AvailableBrowserManager())  # type: ignore[arg-type]
    config = ToolSearchConfig(
        enabled="on",
        core_toolsets=frozenset({"file"}),
        deferred_toolsets=frozenset(),
    )
    assembly = assemble_tool_schemas(registry.list_schemas(), config=config)

    assert not (set(BROWSER_SCHEMAS) & _names(assembly.tool_schemas))
    assert TOOL_SEARCH_NAME in _names(assembly.tool_schemas)


def test_explicit_off_disables_progressive_disclosure():
    assembly = assemble_tool_schemas(
        _registry().list_schemas(),
        config=ToolSearchConfig(enabled="off"),
    )

    names = _names(assembly.tool_schemas)
    assert {
        "file_read",
        "cron_create",
        "web_search",
        "demo__mcp",
        "wiki_search",
        "wiki_create_page",
    } <= names
    assert TOOL_SEARCH_NAME not in names
    assert assembly.activated is False


def test_plan_mode_has_no_deferred_tools_or_search_bridge():
    plan_schemas = [
        {
            "type": "function",
            "function": {"name": name, "description": name, "parameters": {"type": "object"}},
            "_crew_toolset": toolset,
        }
        for name, toolset in [
            ("file_read", "file"),
            ("terminal", "terminal"),
            ("file_write", "file"),
            ("exit_plan_mode", "plan"),
            ("todo", "todo"),
            ("ask_followup_question", "interaction"),
        ]
    ]
    assembly = assemble_tool_schemas(plan_schemas, config=ToolSearchConfig())

    assert _names(assembly.tool_schemas) == {
        "file_read",
        "terminal",
        "file_write",
        "exit_plan_mode",
        "todo",
        "ask_followup_question",
    }
    assert assembly.activated is False
