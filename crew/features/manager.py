"""Neutral Feature Runtime orchestration over lifecycle and service primitives."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeAlias

from crew.features.dependencies import (
    FeatureDependencyGraph,
    FeatureDependencyResolution,
    FeatureServiceDependencies,
)
from crew.features.runtime import (
    Disposer,
    FeatureActivationError,
    FeatureCleanupError,
    FeatureConfigRevisions,
    FeatureGeneration,
    RegistrationPhase,
    FeatureScope,
    FeatureState,
    FeatureTransaction,
    RegistrationToken,
    StaleFeatureGenerationError,
)
from crew.features.services import ServiceKey, ServiceRegistry, ServiceScopePath

InstallResult: TypeAlias = Awaitable[None] | None
FeatureInstaller: TypeAlias = Callable[["FeatureInstallContext"], InstallResult]


@dataclass(frozen=True, slots=True)
class FeatureDefinition:
    """Installable feature shape shared by built-in and external capabilities."""

    feature_id: str
    install: FeatureInstaller
    dependencies: FeatureServiceDependencies | None = None
    service_scope: ServiceScopePath = field(default_factory=ServiceScopePath.global_scope)
    desired_config_revision: int = 1

    def __post_init__(self) -> None:
        feature_id = self.feature_id.strip()
        if not feature_id:
            raise ValueError("feature_id must not be empty")
        if not callable(self.install):
            raise TypeError("feature installer must be callable")
        if self.desired_config_revision < 1:
            raise ValueError("desired config revision must be greater than zero")
        dependencies = self.dependencies or FeatureServiceDependencies(feature_id)
        if dependencies.feature_id != feature_id:
            raise ValueError("feature definition and dependency IDs must match")
        object.__setattr__(self, "feature_id", feature_id)
        object.__setattr__(self, "dependencies", dependencies)


@dataclass(slots=True)
class FeatureRecord:
    """Current diagnostic and lifecycle state for one feature ID."""

    definition: FeatureDefinition
    revisions: FeatureConfigRevisions
    state: FeatureState = FeatureState.DISCOVERED
    generation: FeatureGeneration | None = None
    scope: FeatureScope | None = None
    dependency_resolution: FeatureDependencyResolution | None = None
    error: BaseException | None = None

    @property
    def desired_config_revision(self) -> int:
        return self.revisions.desired_config_revision

    @property
    def effective_config_revision(self) -> int | None:
        return self.revisions.effective_config_revision


@dataclass(frozen=True, slots=True)
class FeatureInstallContext:
    """Capabilities available while one Feature Generation is installing."""

    definition: FeatureDefinition
    scope: FeatureScope
    services: ServiceRegistry
    dependencies: FeatureDependencyResolution

    @property
    def generation(self) -> FeatureGeneration:
        return self.scope.generation

    def register_disposer(
        self,
        disposer: Disposer,
        *,
        label: str,
        phase: RegistrationPhase = RegistrationPhase.RESOURCE,
    ) -> RegistrationToken:
        return self.scope.register(disposer, label=label, phase=phase)

    def resolve_service(self, key: ServiceKey[Any]) -> Any:
        return self.services.resolve(key, self.definition.service_scope)


class FeatureRuntime:
    """Activate, diagnose, and stop features through one reversible lifecycle."""

    def __init__(self, services: ServiceRegistry | None = None) -> None:
        self.services = services or ServiceRegistry()
        self.dependencies = FeatureDependencyGraph()
        self._records: dict[str, FeatureRecord] = {}
        self._sequences: dict[str, int] = {}

    @property
    def records(self) -> tuple[FeatureRecord, ...]:
        return tuple(self._records[key] for key in sorted(self._records))

    def get(self, feature_id: str) -> FeatureRecord | None:
        return self._records.get(feature_id)

    async def activate(self, definition: FeatureDefinition) -> FeatureRecord:
        """Resolve dependencies and transactionally install one feature."""
        self.dependencies.add(definition.dependencies)
        record = self._records.get(definition.feature_id)
        if record is None:
            record = FeatureRecord(
                definition=definition,
                revisions=FeatureConfigRevisions(
                    definition.feature_id,
                    desired_config_revision=definition.desired_config_revision,
                ),
            )
            self._records[definition.feature_id] = record
        elif record.state is FeatureState.ACTIVE:
            return record
        else:
            if record.scope is not None and record.scope.state is FeatureState.FAILED:
                return record
            record.definition = definition
            desired = definition.desired_config_revision
            current_desired = record.revisions.desired_config_revision
            if desired > current_desired:
                record.revisions.request(desired)
            elif desired < current_desired:
                record.state = FeatureState.FAILED
                record.error = StaleFeatureGenerationError(
                    f"feature {definition.feature_id} requested config revision {desired}, "
                    f"but desired revision is already {current_desired}"
                )
                return record

        resolution = self.dependencies.resolve(
            definition.feature_id,
            self.services,
            definition.service_scope,
        )
        record.dependency_resolution = resolution
        record.error = None
        if not resolution.ready:
            record.state = FeatureState.WAITING
            record.scope = None
            record.generation = None
            return record

        sequence = self._sequences.get(definition.feature_id, 0) + 1
        self._sequences[definition.feature_id] = sequence
        generation = record.revisions.new_generation(sequence)
        transaction = FeatureTransaction(generation)
        context = FeatureInstallContext(
            definition=definition,
            scope=transaction.scope,
            services=self.services,
            dependencies=resolution,
        )
        record.state = FeatureState.ACTIVATING
        record.generation = generation
        record.scope = transaction.scope
        try:
            async with transaction:
                result = definition.install(context)
                if inspect.isawaitable(result):
                    await result
                if (
                    generation.desired_config_revision
                    != record.revisions.desired_config_revision
                ):
                    raise StaleFeatureGenerationError(
                        f"feature {generation.key} no longer matches desired config revision"
                    )
                scope = transaction.commit()
                record.revisions.mark_effective(generation)
        except FeatureActivationError as error:
            record.state = FeatureState.FAILED
            record.error = error
            return record

        record.scope = scope
        record.state = FeatureState.ACTIVE
        return record

    async def deactivate(self, feature_id: str) -> bool:
        """Stop one feature and wait until all owned resources are quiescent."""
        record = self._records.get(feature_id)
        if record is None:
            return False
        if record.scope is None:
            record.state = FeatureState.DISCOVERED
            record.error = None
            return False
        record.state = FeatureState.STOPPING
        try:
            await record.scope.dispose()
        except FeatureCleanupError as error:
            record.state = FeatureState.FAILED
            record.error = error
            return False
        record.state = FeatureState.DISCOVERED
        record.scope = None
        record.error = None
        return True
