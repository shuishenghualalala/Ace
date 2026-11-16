"""Team / External Agent 相关 payload 投影函数。

这些函数属于 `crew.team` 包，被 Gateway Router 与 CLI 共同消费：
CLI 不依赖 Gateway Router，同时保持 Runtime 端点的展示行为不变。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
from typing import Any

from crew.agent.external.runtime_registry import resolve_runtime_display_badge


def _runtime_availability(runtime: dict[str, Any]) -> dict[str, Any]:
    """Expose the persisted probe state, with a live executable-path guard."""
    payload = dict(runtime)
    target = str(runtime.get("executable_path") or "").strip()
    protocol = str(runtime.get("protocol") or "").strip().lower()
    available = False
    if target:
        if protocol == "client":
            try:
                available = importlib.util.find_spec(target) is not None
            except (ImportError, AttributeError, ValueError):
                available = False
        else:
            resolved = target if os.path.isabs(target) else shutil.which(target)
            available = bool(resolved and os.path.isfile(resolved) and os.access(resolved, os.X_OK))
    metadata = dict(runtime.get("metadata")) if isinstance(runtime.get("metadata"), dict) else {}
    display_badge = resolve_runtime_display_badge(
        provider=str(runtime.get("provider") or ""),
        metadata=metadata,
    )
    metadata["display_badge"] = display_badge
    status = str(metadata.get("availability_status") or "").strip()
    if not available:
        status = "unavailable"
    elif status not in {"ready", "degraded", "unavailable"}:
        status = "degraded"
    payload["available"] = status == "ready"
    payload["availability_status"] = status
    payload["display_badge"] = display_badge
    payload["metadata"] = metadata
    return payload


def _external_agent_payloads(
    store: Any,
    agents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project RuntimeDescriptor presentation fields onto persisted Agents."""

    runtimes = {
        str(runtime.get("id") or ""): runtime
        for runtime in store.list_runtimes()
    }
    payloads: list[dict[str, Any]] = []
    for agent in agents:
        runtime = runtimes.get(str(agent.get("runtime_id") or ""))
        metadata = (
            runtime.get("metadata")
            if isinstance(runtime, dict) and isinstance(runtime.get("metadata"), dict)
            else {}
        )
        payloads.append({
            **agent,
            "display_badge": resolve_runtime_display_badge(
                provider=str(
                    (runtime or {}).get("provider")
                    or agent.get("provider")
                    or ""
                ),
                metadata=metadata,
            ),
        })
    return payloads


def _managed_temporary_agent_ids(store: Any, *, owner_account_id: str) -> set[str]:
    """Return IDs hidden from durable user-facing Agent catalogs.

    This includes active Formation temporary members and Runtime staffing managed
    Agents; neither is a durable user-created Formation candidate.
    """

    hidden = {
        str(member.get("agent_id") or "")
        for team in store.list_teams(owner_account_id=owner_account_id)
        for member in (
            team.get("formation_plan", {}).get("members", [])
            if isinstance(team.get("formation_plan"), dict)
            else []
        )
        if isinstance(member, dict)
        and member.get("selection_source") == "ai_temporary"
        and str(member.get("agent_id") or "")
    }
    hidden.update(
        str(agent.get("id") or "")
        for agent in store.list_agents(owner_account_id=owner_account_id)
        if str(agent.get("managed_kind") or "")
        and str(agent.get("id") or "")
    )
    return hidden


def _external_team_payloads(
    store: Any,
    teams: list[dict[str, Any]],
    *,
    owner_account_id: str,
) -> list[dict[str, Any]]:
    """Project member Agent badges while keeping Team persistence unchanged."""

    agents = _external_agent_payloads(
        store,
        store.list_agents(owner_account_id=owner_account_id),
    )
    badge_by_agent_id = {
        str(agent.get("id") or ""): str(agent.get("display_badge") or "?")
        for agent in agents
    }
    badge_by_agent_id["crew::builtin"] = "M"
    return [
        {
            **team,
            "display_badge": "T",
            "members": [
                {
                    **member,
                    "display_badge": badge_by_agent_id.get(
                        str(member.get("agent_id") or ""),
                        "?",
                    ),
                }
                for member in (team.get("members") or [])
                if isinstance(member, dict)
            ],
        }
        for team in teams
    ]
