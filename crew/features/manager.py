"""Neutral Feature Runtime orchestration over lifecycle and service primitives."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeAlias

from crew.core.errors import CrewError

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
from crew.features.services import (
    ServiceKey,
    ServiceRegistry,
    ServiceScopeKind,
    ServiceScopePath,
)

InstallResult: TypeAlias = Awaitable[None] | None
FeatureInstaller: TypeAlias = Callable[["FeatureInstallContext"], InstallResult]


class MissingProvidedServicesError(CrewError):
    """A feature completed installation without publishing promised services."""

    def __init__(self, feature_id: str, missing: tuple[ServiceKey[Any], ...]) -> None:
        self.feature_id = feature_id
        self.missing = missing
        names = ", ".join(key.name for key in missing)
        super().__init__(f"feature {feature_id!r} did not register provided services: {names}")


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
class FeatureRegistrationDiagnostic:
    """Serializable registration ownership entry for one generation."""

    label: str
    phase: str
    state: str

    def as_dict(self) -> dict[str, str]:
        return {"label": self.label, "phase": self.phase, "state": self.state}


@dataclass(frozen=True, slots=True)
class FeatureDiagnostic:
    """Stable runtime snapshot used by startup audit and debugging surfaces."""

    feature_id: str
    state: str
    generation: str | None
    desired_config_revision: int
    effective_config_revision: int | None
    requires: tuple[str, ...]
    optional: tuple[str, ...]
    provides: tuple[str, ...]
    missing_required: tuple[str, ...]
    missing_optional: tuple[str, ...]
    registrations: tuple[FeatureRegistrationDiagnostic, ...]
    error: str | None

    @property
    def blocks_startup(self) -> bool:
        return self.state in {FeatureState.WAITING.value, FeatureState.FAILED.value}

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.feature_id,
            "state": self.state,
            "generation": self.generation,
            "config": {
                "desired_revision": self.desired_config_revision,
                "effective_revision": self.effective_config_revision,
            },
            "services": {
                "requires": list(self.requires),
                "optional": list(self.optional),
                "provides": list(self.provides),
                "missing_required": list(self.missing_required),
                "missing_optional": list(self.missing_optional),
            },
            "registrations": [item.as_dict() for item in self.registrations],
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class FeatureStartupAudit:
    """Settled startup report containing every selected feature and blocker."""

    features: tuple[FeatureDiagnostic, ...]

    @property
    def issues(self) -> tuple[FeatureDiagnostic, ...]:
        return tuple(feature for feature in self.features if feature.blocks_startup)

    @property
    def healthy(self) -> bool:
        return not self.issues

    def as_dict(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "issues": [feature.feature_id for feature in self.issues],
            "features": [feature.as_dict() for feature in self.features],
        }


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

    def get_service(self, key: ServiceKey[Any], default: Any = None) -> Any:
        return self.services.get(key, self.definition.service_scope, default)

    def register_service(
        self,
        key: ServiceKey[Any],
        value: Any,
        *,
        scope_kind: ServiceScopeKind = ServiceScopeKind.GLOBAL,
        scope_path: ServiceScopePath | None = None,
        label: str | None = None,
    ) -> RegistrationToken:
        """Publish a service implementation owned by this generation."""
        return self.services.register(
            self.scope,
            key,
            value,
            scope_kind=scope_kind,
            scope_path=scope_path or self.definition.service_scope,
            label=label,
        )


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

    def discover(self, definition: FeatureDefinition) -> FeatureRecord:
        """Register one definition and its dependency edges without installing it."""
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
        elif record.state is not FeatureState.ACTIVE:
            record.definition = definition
        return record

    async def activate_many(
        self,
        definitions: tuple[FeatureDefinition, ...] | list[FeatureDefinition],
    ) -> tuple[FeatureRecord, ...]:
        """Discover first, then activate deterministically by declared services."""
        selected: dict[str, FeatureDefinition] = {}
        for definition in definitions:
            if definition.feature_id in selected:
                raise ValueError(f"duplicate feature definition {definition.feature_id!r}")
            selected[definition.feature_id] = definition
            self.discover(definition)

        plan = self.dependencies.plan_activation(
            host_services=self.services.available_keys(),
        )
        ordered_ids = [
            feature_id
            for batch in plan.batches
            for feature_id in batch
            if feature_id in selected
        ]
        ordered_ids.extend(
            block.feature_id
            for block in plan.blocked
            if block.feature_id in selected
        )
        records: list[FeatureRecord] = []
        for feature_id in ordered_ids:
            records.append(await self.activate(selected[feature_id]))
        return tuple(records)

    def startup_audit(
        self,
        feature_ids: tuple[str, ...] | list[str] | None = None,
    ) -> FeatureStartupAudit:
        """Return a deterministic, serializable audit of settled feature state."""
        selected = set(feature_ids) if feature_ids is not None else None
        diagnostics: list[FeatureDiagnostic] = []
        for record in self.records:
            if selected is not None and record.definition.feature_id not in selected:
                continue
            dependencies = record.definition.dependencies
            resolution = self.dependencies.resolve(
                record.definition.feature_id,
                self.services,
                record.definition.service_scope,
            )
            registrations = tuple(
                FeatureRegistrationDiagnostic(
                    label=token.label,
                    phase=token.phase.value,
                    state=token.state.value,
                )
                for token in (record.scope.registrations if record.scope else ())
            )
            diagnostics.append(
                FeatureDiagnostic(
                    feature_id=record.definition.feature_id,
                    state=record.state.value,
                    generation=record.generation.key if record.generation else None,
                    desired_config_revision=record.desired_config_revision,
                    effective_config_revision=record.effective_config_revision,
                    requires=tuple(key.name for key in dependencies.requires),
                    optional=tuple(key.name for key in dependencies.optional),
                    provides=tuple(key.name for key in dependencies.provides),
                    missing_required=tuple(
                        key.name for key in resolution.missing_required
                    ),
                    missing_optional=tuple(
                        key.name for key in resolution.missing_optional
                    ),
                    registrations=registrations,
                    error=str(record.error) if record.error else None,
                )
            )
        return FeatureStartupAudit(tuple(diagnostics))

    async def activate(self, definition: FeatureDefinition) -> FeatureRecord:
        """Resolve dependencies and transactionally install one feature."""
        record = self.discover(definition)
        if record.state is FeatureState.ACTIVE:
            return record
        if record.scope is not None and record.scope.state is FeatureState.FAILED:
            return record
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
                published = {
                    binding.key.name
                    for binding in self.services.bindings_owned_by(transaction.scope)
                }
                missing_provided = tuple(
                    key
                    for key in definition.dependencies.provides
                    if key.name not in published
                )
                if missing_provided:
                    raise MissingProvidedServicesError(
                        definition.feature_id,
                        missing_provided,
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
