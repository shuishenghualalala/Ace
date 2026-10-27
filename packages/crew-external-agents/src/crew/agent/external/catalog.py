"""Stable contracts and shared identity for external agent catalogs.

The ``ExternalAgentCatalog`` protocol is neutral and lives in
:mod:`crew.core.interfaces`; it is re-exported here during the migration
(条件与删除时点见 interfaces.py 对应段落).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from crew.core.interfaces import ExternalAgentCatalog as ExternalAgentCatalog

CREW_BUILTIN_AGENT_ID = "crew::builtin"
LEGACY_CREW_BUILTIN_AGENT_ID = "crew"


def is_builtin_agent(agent_id: str) -> bool:
    return str(agent_id or "").strip() == CREW_BUILTIN_AGENT_ID


def builtin_agent_public() -> dict[str, Any]:
    return {
        "id": CREW_BUILTIN_AGENT_ID,
        "name": "Crew 内置智能体",
        "provider": "crew",
        "display_badge": "M",
        "runtime_id": "",
        "model": "builtin",
        "system_prompt": "",
        "custom_args": [],
        "custom_env": {},
        "created_at": "",
        "updated_at": "",
    }


@runtime_checkable
class ExternalAgentLifecycle(Protocol):
    """Narrow lifecycle surface consumed across the Team feature boundary.

    Cross-feature consumers resolve existence and deletion through this
    protocol instead of touching external tables directly. The catalog
    implementation cleans up observations, then session bindings, then the
    agent row itself.
    """

    def get_agent(self, agent_id: str, *, owner_account_id: str) -> dict[str, Any]: ...

    def delete_agent(self, agent_id: str, *, owner_account_id: str) -> None: ...
