"""Feature-owned publication for the semantic subagent service."""

from __future__ import annotations

from dataclasses import dataclass

from crew.agent.subagent.service import SUBAGENT_SERVICE_KEY, SubagentService
from crew.features import (
    FeatureDefinition,
    FeatureInstallContext,
    FeatureServiceDependencies,
    FeatureStopPolicy,
    FeatureUpdateStrategy,
)

SUBAGENT_FEATURE_ID = "core.subagent"


@dataclass(frozen=True, slots=True)
class SubagentFeatureBundle:
    """Definition plus the service candidate published by this generation."""

    definition: FeatureDefinition
    service: SubagentService


def build_subagent_feature(
    service: SubagentService,
    *,
    desired_config_revision: int = 1,
) -> SubagentFeatureBundle:
    """Publish an assembled service without taking over its resources.

    The application still owns shutdown ordering for one-shot children. This
    narrow Feature lets optional consumers acquire the service through the
    generation-aware registry used by other cross-feature capabilities.
    """

    async def install(context: FeatureInstallContext) -> None:
        context.register_service(
            SUBAGENT_SERVICE_KEY,
            service,
            label="service:subagent",
        )

    definition = FeatureDefinition(
        SUBAGENT_FEATURE_ID,
        install,
        dependencies=FeatureServiceDependencies(
            SUBAGENT_FEATURE_ID,
            provides=(SUBAGENT_SERVICE_KEY,),
        ),
        desired_config_revision=desired_config_revision,
        stop_policy=FeatureStopPolicy.DRAIN,
        update_strategy=FeatureUpdateStrategy.REPLACE,
        required_by_product=True,
    )
    return SubagentFeatureBundle(definition=definition, service=service)


__all__ = [
    "SUBAGENT_FEATURE_ID",
    "SubagentFeatureBundle",
    "build_subagent_feature",
]
