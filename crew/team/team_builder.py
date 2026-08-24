"""Team 成员规格、模型绑定和 Agent 实例构建。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from crew.agent.executor import create_executor
from crew.agent.runtime import SingleAgent
from crew.core.errors import ToolError
from crew.core.interfaces import LLMProvider, MemoryProvider, SessionStore
from crew.plugins.manager import PluginManager
from crew.state.config import Config
from crew.state.team_member_model import materialize_team_member_model_bindings
from crew.team.models import TeamMemberSpec
from crew.team.roles import CREW_BUILTIN_AGENT_ID, DEFAULT_MEMBERS, is_crew_builtin_agent
from crew.tools.registry import Registry


class TeamMemberFactory:
    """构建 Team 成员的稳定输入和执行 Agent。

    TeamManager 负责 TeamBus、delegate 和计划回调；这个工厂只处理成员事实、
    Session 级模型绑定、工具 Registry 克隆和 Agent executor 装配。
    """

    def __init__(
        self,
        *,
        base_registry: Registry,
        session_store: SessionStore,
        memory: MemoryProvider,
        plugins: PluginManager,
        config: Config,
        external_store: Any | None,
        external_store_provider: Callable[[], Any | None] | None,
        interaction_bridge: Any | None,
        provider_for_member: Callable[[TeamMemberSpec, str], LLMProvider],
        drain_subagent_notifications: Callable[[str, str], list] | None,
    ) -> None:
        self.base_registry = base_registry
        self.session_store = session_store
        self.memory = memory
        self.plugins = plugins
        self.config = config
        self.external_store = external_store
        self.external_store_provider = external_store_provider
        self.interaction_bridge = interaction_bridge
        self.provider_for_member = provider_for_member
        self.drain_subagent_notifications = drain_subagent_notifications

    def _external_store(self) -> Any | None:
        if callable(self.external_store_provider):
            return self.external_store_provider()
        return self.external_store

    def model_bindings_for_session(
        self,
        session_id: str,
        external_team_id: str,
        *,
        owner_account_id: str,
    ) -> dict[str, Any]:
        external_store = self._external_store()
        if external_store is None or not external_team_id:
            return {}
        getter = getattr(self.session_store, "get_agent_config", None)
        stored_config = (
            getter(
                session_id.split("::turn::", 1)[0],
                owner_account_id=owner_account_id,
            )
            if callable(getter)
            else None
        )
        stored_team = (
            stored_config.get("team")
            if isinstance(stored_config, dict) and isinstance(stored_config.get("team"), dict)
            else {}
        )
        if str(stored_team.get("external_team_id") or "").strip() != external_team_id:
            return {}
        materialized, _ = materialize_team_member_model_bindings(
            self.session_store,
            external_store,
            session_id,
            owner_account_id=owner_account_id,
            builtin_model_id=self.config.owner_default_model_id(owner_account_id),
        )
        materialized_team = materialized.get("team") if isinstance(materialized.get("team"), dict) else {}
        bindings = materialized_team.get("member_model_bindings")
        return dict(bindings) if isinstance(bindings, dict) else {}

    def external_team_specs(
        self,
        external_team_id: str,
        *,
        owner_account_id: str = "",
        model_bindings: dict[str, Any] | None = None,
    ) -> tuple[list[TeamMemberSpec], TeamMemberSpec | None]:
        external_store = self._external_store()
        if not external_team_id or external_store is None:
            return [], None
        external_team = external_store.get_team(
            external_team_id,
            owner_account_id=owner_account_id,
        )
        leader_agent_id = str(external_team.get("leader_agent_id") or "").strip()
        formation_plan = (
            external_team.get("formation_plan")
            if isinstance(external_team.get("formation_plan"), dict)
            else {}
        )
        formation_members = {
            str(item.get("agent_id") or ""): item
            for item in (formation_plan.get("members") or [])
            if isinstance(item, dict) and str(item.get("agent_id") or "")
        }
        formation_version = max(1, int(formation_plan.get("version") or 1)) if formation_plan else 0
        members: list[TeamMemberSpec] = []
        leader_spec: TeamMemberSpec | None = None
        bindings = model_bindings if isinstance(model_bindings, dict) else {}
        for row in external_team.get("members") or []:
            agent_id = str(row.get("agent_id") or "").strip()
            binding = bindings.get(agent_id) if isinstance(bindings.get(agent_id), dict) else {}
            formation_member = formation_members.get(agent_id, {})
            responsibility = (
                formation_member.get("responsibility")
                if isinstance(formation_member.get("responsibility"), dict)
                else {}
            )
            is_builtin = is_crew_builtin_agent(agent_id)
            member_id = CREW_BUILTIN_AGENT_ID if is_builtin else str(row.get("agent_name") or agent_id)
            spec = TeamMemberSpec.from_config({
                "member_id": member_id,
                "name": str(row.get("agent_name") or ("Crew 内置智能体" if is_builtin else agent_id)),
                "role": str(row.get("role") or ""),
                "executor": "builtin" if is_builtin else "external",
                "external_agent_id": agent_id,
                "model": str(binding.get("model_id") or ""),
                "capabilities": row.get("capabilities") or [],
                "metadata": {
                    "role_key": row.get("role_key") or "",
                    "role_label": row.get("role_label") or "",
                    "workflow_lane": row.get("workflow_lane") or "",
                    **(
                        {
                            "formation_plan_version": formation_version,
                            "formation_responsibility": dict(responsibility),
                            "formation_responsibility_markdown": str(
                                formation_member.get("responsibility_markdown") or row.get("role") or ""
                            ),
                            "formation_locked": bool(formation_member.get("locked")),
                        }
                        if formation_member
                        else {}
                    ),
                },
            })
            if agent_id and agent_id == leader_agent_id:
                leader_spec = TeamMemberSpec(
                    member_id="leader",
                    name=spec.name,
                    role=spec.role or "负责拆解、派活、跟踪任务、汇总最终结果",
                    executor=spec.executor,
                    external_agent_id="" if is_builtin else spec.external_agent_id,
                    model=spec.model,
                    capabilities=list(spec.capabilities),
                    workspace_policy=spec.workspace_policy,
                    session_policy=spec.session_policy,
                    permission_policy=spec.permission_policy,
                    system_prompt=spec.system_prompt,
                    metadata=dict(spec.metadata),
                )
            else:
                members.append(spec)
        return members, leader_spec

    def members(
        self,
        external_team_id_override: str = "",
        *,
        owner_account_id: str = "",
    ) -> list[TeamMemberSpec]:
        team_cfg = self.config.team_config or {}
        external_team_id = str(
            external_team_id_override or team_cfg.get("external_team_id") or ""
        ).strip()
        if external_team_id and self._external_store() is not None:
            try:
                members, leader_spec = self.external_team_specs(
                    external_team_id,
                    owner_account_id=owner_account_id,
                )
                if leader_spec is not None:
                    members = [leader_spec, *members]
                if members:
                    return members
            except Exception as exc:  # noqa: BLE001
                raise ToolError(f"读取外部团队失败：{external_team_id}") from exc

        return [TeamMemberSpec.from_config(dict(member)) for member in (team_cfg.get("members") or DEFAULT_MEMBERS)]

    def clone_registry(self) -> Registry:
        registry = Registry()
        for name in self.base_registry.names():
            registry.register(self.base_registry.get(name))
        return registry

    def executor_config(
        self,
        spec: TeamMemberSpec,
        *,
        member_session_id: str,
        team_session_id: str,
    ) -> dict[str, Any]:
        if spec.executor == "builtin":
            return {}
        config: dict[str, Any] = dict(spec.metadata.get(spec.executor) or {})
        if spec.external_agent_id:
            config["external_agent_id"] = spec.external_agent_id
        if spec.model:
            config["model"] = spec.model
        external_store = self._external_store()
        if external_store is not None:
            config["external_store"] = external_store
        if self.interaction_bridge is not None:
            config["interaction_bridge"] = self.interaction_bridge
        config["crew_session_id"] = member_session_id
        config["display_session_id"] = team_session_id.split("::turn::", 1)[0]
        config["control_session_id"] = team_session_id
        return config

    def new_agent(
        self,
        registry: Registry,
        system_prompt: str,
        *,
        spec: TeamMemberSpec,
        member_session_id: str,
        team_session_id: str,
        owner_account_id: str = "",
        tool_filter: list[str] | None = None,
    ) -> SingleAgent:
        from crew.tools.policy import exclude_toolsets

        base_tools = registry.names() if tool_filter is None else tool_filter
        filtered_tools = exclude_toolsets(
            registry,
            base_tools,
            exact={"wiki.read", "wiki.manage"},
        )
        provider = self.provider_for_member(spec, owner_account_id)
        executor_kind = "external" if spec.executor in {"acp", "cli", "external"} else spec.executor
        executor = create_executor(
            executor_kind,
            provider=provider,
            registry=registry,
            plugins=self.plugins,
            config=self.executor_config(
                spec,
                member_session_id=member_session_id,
                team_session_id=team_session_id,
            ),
            max_iterations=self.config.max_iterations,
            max_retries=self.config.retry_max,
            backoff_seconds=self.config.retry_backoff,
            parallel_tools=self.config.parallel_tools,
            empty_retry_max=self.config.empty_retry_max,
            continuation_max=self.config.continuation_max,
            max_parallel_tool_calls=self.config.max_parallel_tool_calls,
            max_delegate_tool_calls=(
                self.config.team_max_concurrent_children if spec.member_id == "leader" else 0
            ),
        )
        return SingleAgent(
            provider=provider,
            registry=registry,
            session_store=self.session_store,
            memory=self.memory,
            plugins=self.plugins,
            system_prompt=system_prompt,
            tool_filter=filtered_tools,
            max_iterations=self.config.max_iterations,
            executor=executor,
            agent_id=spec.member_id,
            subagent_drain_fn=self.drain_subagent_notifications,
        )
