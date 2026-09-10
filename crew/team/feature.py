"""Team Product Feature Bundle 与 Generation 拥有的生命周期边界。

将 InProcessTeamManager 的构造、mode=team 的 ExecutionDriver 注册以及
App-owned owner Provider 缓存统一纳入 Feature Runtime，使 Team 能力像
Work/Dynamic Kanban/External Agents 一样可被声明、更新、停用与审计。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from crew.core.envelope import Envelope, ResponseChunk
from crew.core.interfaces import LLMProvider
from crew.features import (
    ExecutionDriver,
    FeatureDefinition,
    FeatureInstallContext,
    FeatureRuntime,
    FeatureServiceDependencies,
    FeatureState,
    FeatureStopPolicy,
    FeatureUpdateStrategy,
    RegistrationPhase,
    ServiceKey,
    run_async_compat,
)
from crew.team.team_manager import InProcessTeamManager

TEAM_FEATURE_ID = "product.team"
TEAM_MODE = "team"
TEAM_SERVICE_KEY: ServiceKey[InProcessTeamManager] = ServiceKey("team")


class TeamFeatureHost(Protocol):
    """Team Bundle 所需的宿主兼容表面。"""

    team: InProcessTeamManager | None
    provider: LLMProvider
    config: Any
    plugins: Any
    interaction_bridge: Any | None


async def _run_team_execution_driver(
    host: Any,
    manager: Any,
    envelope: Envelope,
) -> AsyncIterator[ResponseChunk]:
    """Team mode 的执行体：回填 session agent config 里的 external_team_id 后透传。"""

    if not envelope.params.get("external_team_id"):
        session_agent_config = getattr(host, "_session_agent_config", None)
        if callable(session_agent_config):
            config = session_agent_config(
                envelope.session_id,
                owner_account_id=envelope.user_id,
            )
            team_config = config.get("team") if isinstance(config, dict) else {}
            if not isinstance(team_config, dict):
                team_config = {}
            external_team_id = str(team_config.get("external_team_id") or "").strip()
            if external_team_id:
                envelope.params["external_team_id"] = external_team_id
    async for chunk in manager.interact(envelope):
        yield chunk


@dataclass(frozen=True, slots=True)
class TeamFeatureBundle:
    """Feature definition 与首次候选 Manager 的兼容视图。"""

    definition: FeatureDefinition
    manager: InProcessTeamManager | None


def build_team_feature(
    host: TeamFeatureHost,
    *,
    registry: Any,
    session_store: Any,
    memory: Any,
    plugins: Any,
    tasks: Any,
    config: Any,
    external_store: Any | None = None,
    external_store_provider: Callable[[], Any | None] | None = None,
    external_services_acquirer: Callable[[], Any | None] | None = None,
    interaction_bridge: Any | None = None,
    kanban_store: Any | None = None,
    kanban_consumer_provider: Callable[[], Any | None] | None = None,
    context_contributors: Any | None = None,
    provider: LLMProvider,
    provider_for_owner: Callable[[str], LLMProvider] | None = None,
    provider_for_member_model: Callable[[str, str], LLMProvider] | None = None,
    provider_factory: Callable[[Any], LLMProvider] | None = None,
    enabled: bool = True,
    desired_config_revision: int = 1,
    runtime: FeatureRuntime | None = None,
    activate: bool = False,
) -> TeamFeatureBundle:
    """构造 Team Feature Bundle；激活后 Manager 与 Driver 由 Generation 持有。

    ``provider_factory`` 用于按 owner/model profile 构造独立 Provider；为 None 时
    所有成员回退到传入的 ``provider``。``enabled=False`` 时不注册任何贡献。
    """

    def make_manager() -> InProcessTeamManager:
        return InProcessTeamManager(
            provider=provider,
            registry=registry,
            session_store=session_store,
            memory=memory,
            plugins=plugins,
            tasks=tasks,
            config=config,
            external_store=external_store,
            external_store_provider=external_store_provider,
            external_services_acquirer=external_services_acquirer,
            interaction_bridge=interaction_bridge,
            kanban_store=kanban_store,
            kanban_consumer_provider=kanban_consumer_provider,
            context_contributors=context_contributors,
            provider_factory=provider_factory,
            provider_for_owner=provider_for_owner,
            provider_for_member_model=provider_for_member_model,
        )

    candidate = make_manager() if enabled else None
    pending: list[InProcessTeamManager] = [candidate] if candidate is not None else []

    def next_manager() -> InProcessTeamManager:
        return pending.pop() if pending else make_manager()

    async def install(context: FeatureInstallContext) -> None:
        if not enabled:
            return
        manager = next_manager()

        # 先注册资源清理，再发布贡献；激活失败时 Runtime 会按逆序回滚。
        context.register_disposer(
            manager.shutdown,
            label="resource:team.manager",
            phase=RegistrationPhase.RESOURCE,
        )

        async def execute(envelope: Envelope) -> AsyncIterator[ResponseChunk]:
            async for chunk in _run_team_execution_driver(host, manager, envelope):
                yield chunk

        context.register_execution_driver(
            ExecutionDriver(
                mode=TEAM_MODE,
                execute=execute,
                capabilities=("team.coordinate",),
                description="Team coordination execution",
            ),
            label="execution-driver:team",
        )
        context.register_service(
            TEAM_SERVICE_KEY,
            manager,
            label="service:team",
        )

    definition = FeatureDefinition(
        TEAM_FEATURE_ID,
        install,
        dependencies=FeatureServiceDependencies(
            TEAM_FEATURE_ID,
            provides=(TEAM_SERVICE_KEY,) if enabled else (),
        ),
        desired_config_revision=desired_config_revision,
        stop_policy=FeatureStopPolicy.DRAIN,
        update_strategy=FeatureUpdateStrategy.RESTART,
    )

    bundle = TeamFeatureBundle(definition=definition, manager=candidate)
    if activate:
        if runtime is None:
            raise ValueError("activate=True requires a FeatureRuntime")
        record = run_async_compat(runtime.activate(definition))
        if record.state is not FeatureState.ACTIVE or record.scope is None:
            raise RuntimeError(
                f"Team Feature 激活失败: state={record.state.value} error={record.error}"
            )

    return bundle


__all__ = [
    "TEAM_FEATURE_ID",
    "TEAM_MODE",
    "TEAM_SERVICE_KEY",
    "TeamFeatureBundle",
    "TeamFeatureHost",
    "_run_team_execution_driver",
    "build_team_feature",
]
