from crew.tools.policy import (
    ToolDisclosureMode,
    exclude_toolsets,
    extend_with_toolsets,
    select_requested_tools,
)
from crew.tools.registry import Registry, tool_result


def _registry() -> Registry:
    registry = Registry()
    for name, toolset in (
        ("file_read", "file"),
        ("terminal", "terminal"),
        ("delegate_task", "subagent"),
        ("wiki_search", "wiki.read"),
        ("wiki_create_page", "wiki.manage"),
    ):
        registry.register(
            name=name,
            toolset=toolset,
            schema={"name": name, "parameters": {"type": "object"}},
            handler=lambda _args: tool_result(ok=True),
        )
    return registry


def test_child_request_only_narrows_parent_snapshot():
    registry = _registry()
    selected = select_requested_tools(
        registry,
        ["file_read"],
        requested_toolsets=["file", "terminal"],
        requested_tools=["file_read", "terminal"],
    )
    assert selected == ["file_read"]


def test_regular_child_excludes_nested_and_wiki_toolsets():
    registry = _registry()
    filtered = exclude_toolsets(
        registry,
        registry.names(),
        exact={"subagent", "wiki.read", "wiki.manage"},
    )
    assert filtered == ["file_read", "terminal"]


def test_wiki_policy_adds_wiki_to_main_scope_without_replacing_it():
    registry = _registry()
    resolved = extend_with_toolsets(
        registry,
        ["file_read", "terminal"],
        ["wiki.read", "wiki.manage"],
    )
    assert resolved == [
        "file_read",
        "terminal",
        "wiki_search",
        "wiki_create_page",
    ]


def test_disclosure_mode_names_behavior_instead_of_negated_flag():
    assert ToolDisclosureMode.PROGRESSIVE == "progressive"
    assert ToolDisclosureMode.DIRECT == "direct"
