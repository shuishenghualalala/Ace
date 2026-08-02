"""Tool authorization and disclosure policy primitives.

This module owns set algebra only.  It does not inspect sessions, plugins, or
model state, and it never executes tools.  Callers resolve those runtime inputs
first, then use these helpers to produce one explicit authorization snapshot.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Protocol


class ToolCatalog(Protocol):
    def names(self) -> list[str]: ...

    def names_for_toolset(self, toolset: str) -> list[str]: ...

    def toolset_for(self, name: str) -> str | None: ...


class ToolDisclosureMode(StrEnum):
    """How authorized tool schemas are exposed to a model."""

    PROGRESSIVE = "progressive"
    DIRECT = "direct"


def ordered_intersection(names: Iterable[str], allowed: Iterable[str]) -> list[str]:
    allowed_set = set(allowed)
    return [name for name in names if name in allowed_set]


def exclude_toolsets(
    catalog: ToolCatalog,
    names: Iterable[str],
    *,
    exact: Iterable[str] = (),
    prefixes: Iterable[str] = (),
) -> list[str]:
    exact_set = set(exact)
    prefix_tuple = tuple(prefixes)
    result: list[str] = []
    for name in names:
        toolset = catalog.toolset_for(name) or ""
        if toolset in exact_set or any(toolset.startswith(prefix) for prefix in prefix_tuple):
            continue
        result.append(name)
    return result


def select_requested_tools(
    catalog: ToolCatalog,
    authorized_parent_tools: Iterable[str],
    *,
    requested_toolsets: Iterable[str] | None = None,
    requested_tools: Iterable[str] | None = None,
) -> list[str]:
    """Narrow a parent snapshot; requests can never expand it."""

    parent = list(dict.fromkeys(authorized_parent_tools))
    if requested_toolsets:
        requested_by_toolset = {
            name
            for toolset in requested_toolsets
            for name in catalog.names_for_toolset(str(toolset))
        }
        parent = ordered_intersection(parent, requested_by_toolset)
    if requested_tools:
        parent = ordered_intersection(parent, requested_tools)
    return parent


def extend_with_toolsets(
    catalog: ToolCatalog,
    base_tools: Iterable[str],
    extra_toolsets: Iterable[str],
) -> list[str]:
    """Append dedicated capabilities without disturbing base ordering."""

    result = list(dict.fromkeys(base_tools))
    seen = set(result)
    for toolset in extra_toolsets:
        for name in catalog.names_for_toolset(str(toolset)):
            if name not in seen:
                result.append(name)
                seen.add(name)
    return result
