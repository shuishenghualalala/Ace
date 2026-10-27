"""Pure subagent capability and tool-policy helpers.

The application resolves runtime inputs such as access-control snapshots,
reserved toolsets, and model capabilities.  This module only combines those
already-resolved values.  Keeping the policy algebra here makes the safety
boundary deterministic and independently testable.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from crew.tools.policy import (
    ToolCatalog,
    exclude_toolsets,
    ordered_intersection,
    select_requested_tools,
)

# These defaults are intentionally owned by the subagent policy rather than by
# the tool implementation.  A caller can pass a narrower set, but cannot use
# this helper to widen the parent snapshot.
SUBAGENT_BLOCKED_TOOLSETS = frozenset(
    {
        "subagent",
        "subagent.preset",
        "subagent.output",
        "external_agent",
        "memory",
        "cron",
        "tasks",
    }
)
SUBAGENT_BLOCKED_TOOLSET_PREFIXES = ("feishu",)
READ_ONLY_BLOCKED_TOOLSETS = frozenset({"terminal"})
READ_ONLY_BLOCKED_TOOLS = frozenset(
    {"terminal", "file_write", "write_file", "file_delete", "patch", "publish_site"}
)


# 子智能体可见的委派工具集：allow_nested 且深度未达上限时才从黑名单放行。
# 其余条目（外援/记忆/cron/任务面板）在任何深度都不放行——子级不能借外援
# 通道绕过审批升级（结构性收窄，D16）。
_NESTING_ALLOWED_TOOLSETS = frozenset({"subagent", "subagent.preset"})


def delegation_blocked_toolsets(
    depth: int,
    *,
    allow_nested: bool,
    max_depth: int = 3,
) -> frozenset[str]:
    """Compute the child-side blocked toolset snapshot for one depth.

    真值表：``allow_nested=false`` 或 ``depth >= max_depth`` → 全量黑名单
    （与旧行为逐字一致）；否则只保留非委派条目。可见性是运行时策略而不是
    开关藏刀——深度由账本单调记账，超限的子级看得见工具但启动会被拒。
    """

    base = set(SUBAGENT_BLOCKED_TOOLSETS)
    if allow_nested and 0 <= int(depth) < int(max_depth):
        base -= _NESTING_ALLOWED_TOOLSETS
    return frozenset(base)


def normalize_policy_names(value: Any) -> list[str] | None:
    """Normalize a model-supplied toolset/tool value without changing meaning.

    The public schema uses arrays, while models occasionally emit a scalar
    string.  Treating a scalar as one name preserves the historical behavior;
    unsupported values are treated as an omitted restriction and are therefore
    never an expansion of the parent authorization snapshot.
    """

    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(item) for item in value]
    return None


def resolve_subagent_tool_filter(
    catalog: ToolCatalog,
    parent_allowed: Iterable[str],
    *,
    requested_toolsets: Any = None,
    requested_tools: Any = None,
    blocked_toolsets: Iterable[str] = SUBAGENT_BLOCKED_TOOLSETS,
    blocked_toolset_prefixes: Iterable[str] = SUBAGENT_BLOCKED_TOOLSET_PREFIXES,
    reserved_toolsets: Iterable[str] = (),
) -> list[str]:
    """Return a child tool snapshot that can only narrow its parent.

    ``parent_allowed`` is already the parent's effective authorization.  The
    requested toolsets/tools are intersections, then the subagent blacklist
    and feature-owned reserved toolsets are removed.  The catalog's order is
    retained so schema ordering remains stable across the compatibility path.
    """

    requested_toolsets = normalize_policy_names(requested_toolsets)
    requested_tools = normalize_policy_names(requested_tools)
    allowed = ordered_intersection(catalog.names(), parent_allowed)
    allowed = select_requested_tools(
        catalog,
        allowed,
        requested_toolsets=requested_toolsets,
        requested_tools=requested_tools,
    )
    allowed = exclude_toolsets(
        catalog,
        allowed,
        exact=blocked_toolsets,
        prefixes=blocked_toolset_prefixes,
    )
    return exclude_toolsets(catalog, allowed, exact=reserved_toolsets)


def apply_preset_tool_policy(
    catalog: ToolCatalog,
    base_tools: Iterable[str],
    preset: dict[str, Any],
    *,
    allow_additions: bool = True,
    addition_ceiling: Iterable[str] = (),
) -> list[str]:
    """Apply a preset policy without exceeding the parent authorization ceiling.

    Feature-owned presets may opt into dedicated toolsets that are hidden from
    ordinary children.  Those additions are admitted only when the same tool
    was present in ``addition_ceiling`` before reserved toolsets were hidden.
    """

    selected = select_requested_tools(
        catalog,
        base_tools,
        requested_toolsets=normalize_policy_names(preset.get("toolsets")),
        requested_tools=normalize_policy_names(preset.get("tools")),
    )
    result = list(dict.fromkeys(selected))
    if allow_additions:
        # Additions are still expected to be resolved by the caller against
        # the ordinary child boundary.  This function only handles the
        # preset's explicit policy and preserves the existing tool ordering.
        seen = set(result)
        ceiling = set(addition_ceiling)
        additions = normalize_policy_names(preset.get("toolset_additions")) or []
        for toolset in additions:
            for name in catalog.names_for_toolset(toolset):
                if name in ceiling and name not in seen:
                    result.append(name)
                    seen.add(name)
    if bool(preset.get("read_only")):
        result = exclude_toolsets(
            catalog,
            result,
            exact=READ_ONLY_BLOCKED_TOOLSETS,
        )
        result = [name for name in result if name not in READ_ONLY_BLOCKED_TOOLS]
    return result


def apply_model_capability_policy(
    tool_names: Iterable[str] | None,
    capabilities: Iterable[str] | None,
    *,
    vision_tool_name: str = "browser_vision",
) -> list[str] | None:
    """Apply model capability gates without consulting runtime state.

    A model without the general ``tools`` capability receives no tools.  A
    text-only model keeps ordinary tools but loses the vision tool.  ``None``
    capabilities means the provider did not declare a restriction and retains
    the caller's original ``None``/list shape.
    """

    if capabilities is None:
        return None if tool_names is None else list(tool_names)
    enabled = {str(item).strip().lower() for item in capabilities}
    if "tools" not in enabled:
        return []
    names = list(tool_names) if tool_names is not None else None
    if "vision" in enabled or names is None:
        return names
    return [name for name in names if name != vision_tool_name]


# Short aliases keep the policy vocabulary discoverable for callers migrating
# from the old CrewApp methods.
resolve_tool_filter = resolve_subagent_tool_filter
filter_model_capabilities = apply_model_capability_policy


__all__ = [
    "SUBAGENT_BLOCKED_TOOLSETS",
    "SUBAGENT_BLOCKED_TOOLSET_PREFIXES",
    "READ_ONLY_BLOCKED_TOOLSETS",
    "READ_ONLY_BLOCKED_TOOLS",
    "apply_model_capability_policy",
    "apply_preset_tool_policy",
    "delegation_blocked_toolsets",
    "filter_model_capabilities",
    "normalize_policy_names",
    "resolve_subagent_tool_filter",
    "resolve_tool_filter",
]
