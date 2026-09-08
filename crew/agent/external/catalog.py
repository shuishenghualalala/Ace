"""Stable contracts and shared identity for external agent catalogs."""

from __future__ import annotations

from typing import Any, Protocol

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


class ExternalAgentCatalog(Protocol):
    """External Runtime, Agent, Profile, observation and session persistence."""

    def upsert_runtime(self, runtime: dict[str, Any]) -> dict[str, Any]: ...

    def sync_runtimes(self, runtimes: list[dict[str, Any]]) -> list[dict[str, Any]]: ...

    def list_runtimes(self) -> list[dict[str, Any]]: ...

    def get_runtime(self, runtime_id: str) -> dict[str, Any]: ...

    def delete_runtime(self, runtime_id: str) -> None: ...

    def create_agent(
        self,
        *,
        owner_account_id: str,
        name: str,
        runtime_id: str,
        model: str = "",
        system_prompt: str = "",
        custom_args: list[str] | None = None,
        custom_env: dict[str, str] | None = None,
    ) -> dict[str, Any]: ...

    def get_or_create_managed_agent(
        self,
        *,
        owner_account_id: str,
        managed_kind: str,
        managed_key: str,
        name: str,
        runtime_id: str,
        model: str = "",
        system_prompt: str = "",
    ) -> dict[str, Any]: ...

    def list_agents(
        self,
        *,
        owner_account_id: str,
        include_managed: bool = True,
    ) -> list[dict[str, Any]]: ...

    def get_agent(self, agent_id: str, *, owner_account_id: str) -> dict[str, Any]: ...

    def delete_agent(self, agent_id: str, *, owner_account_id: str) -> None: ...

    def agent_with_runtime(
        self,
        agent_id: str,
        *,
        owner_account_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]: ...

    def refresh_agent_profile(
        self,
        agent_id: str,
        *,
        runtime: dict[str, Any] | None = None,
        owner_account_id: str,
    ) -> dict[str, Any]: ...

    def resolve_agent_profile(
        self,
        agent_id: str,
        model_id: str,
        *,
        owner_account_id: str,
    ) -> dict[str, Any]: ...

    def record_agent_profile_observation(
        self,
        *,
        owner_account_id: str,
        external_agent_id: str,
        source_run_id: str,
        source_node_id: str,
        source_attempt_id: str,
        capabilities: list[str],
        assessment_source: str,
        outcome: str,
        quality_weight: float,
        failure_kind: str = "",
        observed_at: str | None = None,
        runtime_id: str = "",
        model_id: str = "",
        model_fingerprint: str = "",
        model_binding_source: str = "",
    ) -> dict[str, Any]: ...

    def list_agent_profile_observations(
        self,
        external_agent_id: str,
        *,
        owner_account_id: str,
    ) -> list[dict[str, Any]]: ...

    def get_runtime_session_binding(
        self,
        *,
        owner_account_id: str,
        crew_session_id: str,
        external_agent_id: str,
        runtime_id: str,
        adapter_id: str,
        cwd: str = "",
    ) -> dict[str, Any] | None: ...

    def save_runtime_session_binding(
        self,
        *,
        owner_account_id: str,
        crew_session_id: str,
        external_agent_id: str,
        runtime_id: str,
        adapter_id: str,
        native_session_id: str,
        cwd: str = "",
        session_profile: str | None = None,
        status: str = "active",
    ) -> dict[str, Any]: ...

    def delete_runtime_session_binding(
        self,
        *,
        owner_account_id: str,
        crew_session_id: str,
        external_agent_id: str,
        runtime_id: str,
        adapter_id: str,
        cwd: str = "",
    ) -> None: ...

    def delete_runtime_bindings_for_session(
        self,
        crew_session_id: str,
        *,
        owner_account_id: str,
    ) -> int: ...
