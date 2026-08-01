"""Crew progressive tool disclosure for Crew.

The active access-control and plan filtering happens before this module
sees schemas. Tool search only defers tools that are already allowed in the
current turn, so it cannot widen the runnable tool surface.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Iterable

from crew.core.types import ToolCall, ToolResult
from crew.state.logging import get_logger

log = get_logger("tools.tool_search")

TOOL_SEARCH_NAME = "tool_search"
BRIDGE_TOOL_NAMES = frozenset({TOOL_SEARCH_NAME})

CHARS_PER_TOKEN = 4.0

DEFAULT_CORE_TOOLSETS = frozenset({"*"})
DEFAULT_DEFERRED_TOOLSETS = frozenset({"cron", "wiki.read", "wiki.manage"})
DEFAULT_CORE_TOOLS = frozenset()


@dataclass(frozen=True)
class ToolSearchConfig:
    # 默认 on：cron 与 Wiki 延迟加载，其余工具全部常驻。
    enabled: str = "on"  # on | off | auto
    threshold_pct: float = 10.0
    search_default_limit: int = 5
    max_search_limit: int = 20
    core_toolsets: frozenset[str] = DEFAULT_CORE_TOOLSETS
    deferred_toolsets: frozenset[str] = DEFAULT_DEFERRED_TOOLSETS
    core_tools: frozenset[str] = DEFAULT_CORE_TOOLS

    @classmethod
    def from_raw(cls, raw: Any) -> "ToolSearchConfig":
        if raw is False:
            return cls(enabled="off")
        if raw is True or raw is None:
            return cls()
        if not isinstance(raw, dict):
            return cls()

        enabled_raw = str(raw.get("enabled", "on")).strip().lower()
        if enabled_raw in {"1", "true", "yes"}:
            enabled = "on"
        elif enabled_raw in {"0", "false", "no"}:
            enabled = "off"
        elif enabled_raw in {"on", "off", "auto"}:
            enabled = enabled_raw
        else:
            enabled = "off"

        max_search_limit = max(1, min(50, _as_int(raw.get("max_search_limit"), 20)))
        default_limit = max(1, min(max_search_limit, _as_int(raw.get("search_default_limit"), 5)))
        threshold_pct = max(0.0, min(100.0, _as_float(raw.get("threshold_pct"), 10.0)))
        core_toolsets = _as_str_set(raw.get("core_toolsets"), DEFAULT_CORE_TOOLSETS)
        deferred_toolsets = _as_str_set(
            raw.get("deferred_toolsets"),
            DEFAULT_DEFERRED_TOOLSETS,
        )
        core_tools = _as_str_set(raw.get("core_tools"), DEFAULT_CORE_TOOLS)
        return cls(
            enabled=enabled,
            threshold_pct=threshold_pct,
            search_default_limit=default_limit,
            max_search_limit=max_search_limit,
            core_toolsets=frozenset(core_toolsets),
            deferred_toolsets=frozenset(deferred_toolsets),
            core_tools=frozenset(core_tools),
        )


@dataclass(frozen=True)
class ToolSearchAssembly:
    tool_schemas: list[dict[str, Any]]
    original_tool_schemas: list[dict[str, Any]]
    activated: bool
    deferred_count: int = 0
    deferred_tokens: int = 0
    categories: dict[str, int] | None = None
    config: ToolSearchConfig = ToolSearchConfig()


@dataclass(frozen=True)
class CatalogEntry:
    name: str
    description: str
    schema: dict[str, Any]
    toolset: str
    search_hint: str = ""


def load_config() -> ToolSearchConfig:
    try:
        from crew.state.config import load_config as _load_config

        cfg = _load_config()
        raw = (cfg.raw_config.get("tools") or {}).get("tool_search")
    except Exception:
        raw = None
    return ToolSearchConfig.from_raw(raw)


def _as_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _as_float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _as_str_set(value: Any, fallback: Iterable[str]) -> set[str]:
    if value is None:
        return {str(v) for v in fallback}
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, Iterable):
        return {str(v) for v in fallback}
    result = {str(v).strip() for v in value if str(v).strip()}
    return result or {str(v) for v in fallback}


def function_name(schema: dict[str, Any]) -> str:
    fn = schema.get("function") or {}
    return str(fn.get("name") or "")


def function_description(schema: dict[str, Any]) -> str:
    fn = schema.get("function") or {}
    return str(fn.get("description") or "")


def toolset_name(schema: dict[str, Any]) -> str:
    return str(schema.get("_crew_toolset") or "default")


def search_hint(schema: dict[str, Any]) -> str:
    return str(schema.get("_crew_search_hint") or "")


def _private_bool(schema: dict[str, Any], key: str) -> bool | None:
    if key not in schema:
        return None
    value = schema.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def should_defer_schema(schema: dict[str, Any], config: ToolSearchConfig) -> bool:
    name = function_name(schema)
    toolset = toolset_name(schema)
    if not name or name in BRIDGE_TOOL_NAMES:
        return False
    if _private_bool(schema, "_crew_always_load") is True:
        return False
    if toolset in config.deferred_toolsets:
        return True
    if name in config.core_tools:
        return False
    if "*" in config.core_toolsets or toolset in config.core_toolsets:
        return False
    if _private_bool(schema, "_crew_is_mcp") is True:
        return True
    explicit = _private_bool(schema, "_crew_should_defer")
    if explicit is not None:
        return explicit
    return True


def is_bridge_tool(name: str) -> bool:
    return name in BRIDGE_TOOL_NAMES


def classify_tool_schemas(
    schemas: list[dict[str, Any]],
    config: ToolSearchConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    visible: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    for schema in schemas:
        name = function_name(schema)
        if not name or name in BRIDGE_TOOL_NAMES:
            continue
        if should_defer_schema(schema, config):
            deferred.append(schema)
        else:
            visible.append(_strip_private(schema))
    return visible, deferred


def estimate_tokens_from_schemas(schemas: Iterable[dict[str, Any]]) -> int:
    total = 0
    for schema in schemas:
        try:
            total += len(json.dumps(_strip_private(schema), ensure_ascii=False, separators=(",", ":")))
        except (TypeError, ValueError):
            total += len(str(schema))
    return int(math.ceil(total / CHARS_PER_TOKEN))


def should_activate(config: ToolSearchConfig, deferred_tokens: int, context_window: int | None) -> bool:
    if config.enabled == "off" or deferred_tokens <= 0:
        return False
    if config.enabled == "on":
        return True
    if not context_window or context_window <= 0:
        return deferred_tokens >= 20_000
    return deferred_tokens >= int(context_window * (config.threshold_pct / 100.0))


def assemble_tool_schemas(
    schemas: list[dict[str, Any]],
    *,
    config: ToolSearchConfig | None = None,
    context_window: int | None = None,
) -> ToolSearchAssembly:
    config = config or load_config()
    original = list(schemas)
    visible, deferred = classify_tool_schemas(original, config)
    deferred_tokens = estimate_tokens_from_schemas(deferred)
    if not should_activate(config, deferred_tokens, context_window):
        return ToolSearchAssembly(
            tool_schemas=[_strip_private(s) for s in original if function_name(s) not in BRIDGE_TOOL_NAMES],
            original_tool_schemas=original,
            activated=False,
            deferred_count=len(deferred),
            deferred_tokens=deferred_tokens,
            categories=_category_counts(deferred),
            config=config,
        )
    categories = _category_counts(deferred)
    assembled = visible + bridge_tool_schemas(len(deferred), categories)
    log.info(
        "tool_search activated: visible=%d deferred=%d deferred_tokens~%d categories=%s",
        len(visible),
        len(deferred),
        deferred_tokens,
        ",".join(f"{k}:{v}" for k, v in categories.items()),
    )
    return ToolSearchAssembly(
        tool_schemas=assembled,
        original_tool_schemas=original,
        activated=True,
        deferred_count=len(deferred),
        deferred_tokens=deferred_tokens,
        categories=categories,
        config=config,
    )


def expand_discovered_tool_schemas(
    assembly: ToolSearchAssembly,
    discovered_tool_names: Iterable[str],
) -> list[dict[str, Any]]:
    """Return the provider tool list after ToolSearch discoveries.

    Crew makes a deferred tool directly callable after ToolSearch returns it.
    Provider-agnostic Crew mirrors that behavior by adding the matched tool's
    real schema to the next model request instead of requiring a ``tool_call``
    wrapper.
    """
    if not assembly.activated:
        return list(assembly.tool_schemas)

    discovered = {str(name) for name in discovered_tool_names if str(name)}
    if not discovered:
        return list(assembly.tool_schemas)

    existing = {function_name(schema) for schema in assembly.tool_schemas}
    expanded = list(assembly.tool_schemas)
    for schema in assembly.original_tool_schemas:
        name = function_name(schema)
        if (
            name in discovered
            and name not in existing
            and should_defer_schema(schema, assembly.config)
        ):
            expanded.append(_strip_private(schema))
            existing.add(name)
    return expanded


def available_deferred_tools_message(assembly: ToolSearchAssembly) -> str:
    """Build the ephemeral Crew-style deferred-tool inventory for the model.

    ``original_tool_schemas`` is already reduced to the current turn's
    authorized scope before ToolSearch sees it, so this inventory advertises
    capabilities without widening permissions or exposing full schemas.
    """
    if not assembly.activated:
        return ""

    _, deferred = classify_tool_schemas(
        assembly.original_tool_schemas,
        assembly.config,
    )
    names = sorted({function_name(schema) for schema in deferred if function_name(schema)})
    if not names:
        return ""
    return "<available-deferred-tools>\n" + "\n".join(names) + "\n</available-deferred-tools>"


def extract_discovered_tool_names(
    messages: Iterable[Any],
    *,
    original_tool_schemas: list[dict[str, Any]],
    config: ToolSearchConfig,
) -> set[str]:
    """Restore ToolSearch discoveries from canonical message history."""
    _, deferred = classify_tool_schemas(original_tool_schemas, config)
    allowed = {function_name(schema) for schema in deferred}
    discovered: set[str] = set()

    for message in messages:
        if getattr(message, "role", "") != "tool":
            continue
        tool_name = str(getattr(message, "name", "") or "")
        if tool_name != TOOL_SEARCH_NAME:
            continue
        try:
            payload = json.loads(str(getattr(message, "content", "") or ""))
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("error"):
            continue
        matches = payload.get("matches") or []
        for item in matches:
            name = item.get("name") if isinstance(item, dict) else item
            if str(name or "") in allowed:
                discovered.add(str(name))
    return discovered


def bridge_tool_schemas(deferred_count: int, categories: dict[str, int] | None = None) -> list[dict[str, Any]]:
    category_text = _format_categories(categories or {})
    search_desc = (
        f"Search and load {deferred_count} allowed deferred tools. "
        f"Always-visible deferred categories: {category_text}. "
        "Deferred tool names are listed in the hidden <available-deferred-tools> block. "
        "Before claiming a capability is unavailable, inspect that list and search for a relevant tool. "
        "Each matched tool's full schema is added to the next model request; "
        "after this search, call the matched tool directly by name. "
        "Only tools allowed by this session's current access policy can be returned."
    )
    return [
        {
            "type": "function",
            "function": {
                "name": TOOL_SEARCH_NAME,
                "description": search_desc,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Capability, tool name, or category to search for."},
                        "limit": {"type": "integer", "description": "Maximum result count. Default 5."},
                    },
                    "required": ["query"],
                },
            },
        },
    ]


def dispatch_bridge_tool(
    tc: ToolCall,
    *,
    original_tool_schemas: list[dict[str, Any]],
    config: ToolSearchConfig | None = None,
) -> ToolResult | ToolCall:
    config = config or load_config()
    if tc.name == TOOL_SEARCH_NAME:
        return ToolResult(tc.id, tc.name, _dispatch_search(tc.arguments, original_tool_schemas, config))
    return tc


def _dispatch_search(
    args: dict[str, Any],
    schemas: list[dict[str, Any]],
    config: ToolSearchConfig,
) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        return json.dumps({"error": "query is required"}, ensure_ascii=False)
    limit = max(1, min(config.max_search_limit, _as_int(args.get("limit"), config.search_default_limit)))
    _, deferred = classify_tool_schemas(schemas, config)
    catalog = build_catalog(deferred)
    hits = search_catalog(catalog, query, limit)
    return json.dumps(
        {
            "query": query,
            "categories": _category_counts(deferred),
            "total_available": len(catalog),
            "matches": [
                {
                    "name": hit.name,
                    "toolset": hit.toolset,
                    "description": hit.description[:400],
                    "search_hint": hit.search_hint[:240],
                }
                for hit in hits
            ],
        },
        ensure_ascii=False,
    )


def build_catalog(schemas: Iterable[dict[str, Any]]) -> list[CatalogEntry]:
    entries: list[CatalogEntry] = []
    for schema in schemas:
        name = function_name(schema)
        if not name:
            continue
        entries.append(
            CatalogEntry(
                name=name,
                description=function_description(schema),
                schema=schema,
                toolset=toolset_name(schema),
                search_hint=search_hint(schema),
            )
        )
    return entries


def search_catalog(catalog: list[CatalogEntry], query: str, limit: int) -> list[CatalogEntry]:
    query = query.strip()
    if query.lower().startswith("select:"):
        wanted = [item.strip() for item in query[7:].split(",") if item.strip()]
        wanted_lower = {item.lower() for item in wanted}
        return [entry for entry in catalog if entry.name.lower() in wanted_lower][:limit]
    terms = _terms(query)
    if not terms:
        return catalog[:limit]
    scored: list[tuple[float, CatalogEntry]] = []
    for entry in catalog:
        haystack = " ".join([entry.name, entry.toolset, entry.description, entry.search_hint]).lower()
        score = 0.0
        for term in terms:
            if term == entry.toolset.lower() or term == entry.name.lower():
                score += 5.0
            elif term in entry.name.lower():
                score += 3.0
            elif term in entry.search_hint.lower():
                score += 2.0
            elif term in haystack:
                score += 1.0
        if score > 0:
            scored.append((score, entry))
    scored.sort(key=lambda item: (-item[0], item[1].toolset, item[1].name))
    return [entry for _, entry in scored[:limit]]


def _terms(query: str) -> list[str]:
    return [t for t in re.split(r"[\s,，;；/]+", query.lower()) if t]


def _category_counts(schemas: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for schema in schemas:
        toolset = toolset_name(schema)
        counts[toolset] = counts.get(toolset, 0) + 1
    return dict(sorted(counts.items()))


def _format_categories(categories: dict[str, int]) -> str:
    if not categories:
        return "none"
    return ", ".join(f"{name}({count})" for name, count in categories.items())


def _strip_private(schema: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in schema.items() if not k.startswith("_crew_")}
