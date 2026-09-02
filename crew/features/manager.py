"""Neutral Feature Runtime orchestration over lifecycle and service primitives."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
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
    FeatureDrainTimeoutError,
    FeatureGeneration,
    FeatureLease,
    FeatureRestartRequiredError,
    RegistrationPhase,
    FeatureScope,
    FeatureState,
    FeatureStopPolicy,
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


class FeatureUpdateStrategy(str, Enum):
    """How a validated configuration becomes the active generation."""

    REPLACE = "replace"
    RESTART = "restart"


class FeatureUpdateError(CrewError):
    """A configuration update failed, optionally after restoring the old config."""

    def __init__(
        self,
        feature_id: str,
        strategy: FeatureUpdateStrategy,
        cause: BaseException,
        *,
        restored: bool = False,
        recovery_error: BaseException | None = None,
    ) -> None:
        self.feature_id = feature_id
        self.strategy = strategy
        self.cause = cause
        self.restored = restored
        self.recovery_error = recovery_error
        if recovery_error is not None:
            suffix = f"; recovery failed: {recovery_error}"
        elif restored:
            suffix = "; previous config restored"
        else:
            suffix = ""
        super().__init__(
            f"feature {feature_id!r} {strategy.value} update failed: {cause}{suffix}"
        )


class FeatureUpdateRejectedError(CrewError):
    """A requested update violates the active feature's stable contract."""


@dataclass(frozen=True, slots=True)
class FeatureUpdateResult:
    """Outcome of one desired configuration revision request."""

    feature_id: str
    strategy: FeatureUpdateStrategy
    previous_generation: str
    current_generation: str | None
    updated: bool
    restored: bool = False
    error: BaseException | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.feature_id,
            "strategy": self.strategy.value,
            "previous_generation": self.previous_generation,
            "current_generation": self.current_generation,
            "updated": self.updated,
            "restored": self.restored,
            "error": str(self.error) if self.error else None,
        }


@dataclass(slots=True)
class RetiringFeatureGeneration:
    """An old generation still draining after a successful replace switch."""

    scope: FeatureScope
    policy: FeatureStopPolicy
    timeout_seconds: float | None
    error: BaseException | None = None


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
    stop_policy: FeatureStopPolicy = FeatureStopPolicy.DRAIN
    drain_timeout_seconds: float | None = 30.0
    update_strategy: FeatureUpdateStrategy = FeatureUpdateStrategy.RESTART

    def __post_init__(self) -> None:
        feature_id = self.feature_id.strip()
        if not feature_id:
            raise ValueError("feature_id must not be empty")
        if not callable(self.install):
            raise TypeError("feature installer must be callable")
        if self.desired_config_revision < 1:
            raise ValueError("desired config revision must be greater than zero")
        try:
            stop_policy = FeatureStopPolicy(self.stop_policy)
        except ValueError as error:
            raise ValueError(f"unsupported feature stop policy {self.stop_policy!r}") from error
        if self.drain_timeout_seconds is not None and self.drain_timeout_seconds < 0:
            raise ValueError("feature drain timeout must not be negative")
        try:
            update_strategy = FeatureUpdateStrategy(self.update_strategy)
        except ValueError as error:
            raise ValueError(
                f"unsupported feature update strategy {self.update_strategy!r}"
            ) from error
        if (
            update_strategy is FeatureUpdateStrategy.REPLACE
            and stop_policy is FeatureStopPolicy.RESTART_REQUIRED
        ):
            raise ValueError("replace update cannot use restart_required stop policy")
        dependencies = self.dependencies or FeatureServiceDependencies(feature_id)
        if dependencies.feature_id != feature_id:
            raise ValueError("feature definition and dependency IDs must match")
        object.__setattr__(self, "feature_id", feature_id)
        object.__setattr__(self, "dependencies", dependencies)
        object.__setattr__(self, "stop_policy", stop_policy)
        object.__setattr__(self, "update_strategy", update_strategy)


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
    restart_required: bool = False
    retiring: list[RetiringFeatureGeneration] = field(default_factory=list)

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
    stop_policy: str
    update_strategy: str
    active_leases: tuple[str, ...]
    retiring_generations: tuple[str, ...]
    restart_required: bool
    last_stop: dict[str, Any] | None
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
            "lifecycle": {
                "stop_policy": self.stop_policy,
                "update_strategy": self.update_strategy,
                "active_leases": list(self.active_leases),
                "retiring_generations": list(self.retiring_generations),
                "restart_required": self.restart_required,
                "last_stop": self.last_stop,
            },
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

    def acquire_lease(self, label: str = "request") -> FeatureLease:
        """Hold this generation active for one externally visible operation."""
        return self.scope.acquire_lease(label)

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
        self._operation_locks: dict[str, asyncio.Lock] = {}

    @property
    def records(self) -> tuple[FeatureRecord, ...]:
        return tuple(self._records[key] for key in sorted(self._records))

    def get(self, feature_id: str) -> FeatureRecord | None:
        return self._records.get(feature_id)

    def _operation_lock(self, feature_id: str) -> asyncio.Lock:
        return self._operation_locks.setdefault(feature_id, asyncio.Lock())

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
            stop_diagnostic = record.scope.stop_diagnostic if record.scope else None
            last_stop = None
            if stop_diagnostic is not None:
                last_stop = {
                    "policy": stop_diagnostic.policy.value,
                    "timeout_seconds": stop_diagnostic.timeout_seconds,
                    "active_at_start": list(stop_diagnostic.active_at_start),
                    "cancel_signalled": stop_diagnostic.cancel_signalled,
                    "timed_out": stop_diagnostic.timed_out,
                    "forced_leases": list(stop_diagnostic.forced_leases),
                    "restart_required": stop_diagnostic.restart_required,
                }
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
                    stop_policy=record.definition.stop_policy.value,
                    update_strategy=record.definition.update_strategy.value,
                    active_leases=tuple(
                        [
                            lease.label
                            for lease in (
                                record.scope.active_leases if record.scope else ()
                            )
                        ]
                        + [
                            f"{retiring.scope.generation.key}:{lease.label}"
                            for retiring in record.retiring
                            for lease in retiring.scope.active_leases
                        ]
                    ),
                    retiring_generations=tuple(
                        retiring.scope.generation.key for retiring in record.retiring
                    ),
                    restart_required=record.restart_required,
                    last_stop=last_stop,
                    error=str(record.error) if record.error else None,
                )
            )
        return FeatureStartupAudit(tuple(diagnostics))

    async def _install_generation(
        self,
        definition: FeatureDefinition,
        record: FeatureRecord,
        resolution: FeatureDependencyResolution,
        *,
        config_revision: int,
        observe_record: bool,
        require_desired: bool = True,
    ) -> tuple[FeatureScope, FeatureGeneration]:
        sequence = self._sequences.get(definition.feature_id, 0) + 1
        self._sequences[definition.feature_id] = sequence
        generation = record.revisions.generation_for(sequence, config_revision)
        transaction = FeatureTransaction(generation)
        if observe_record:
            transaction.scope.observe_state(lambda state: setattr(record, "state", state))
            record.state = FeatureState.ACTIVATING
            record.generation = generation
            record.scope = transaction.scope
        context = FeatureInstallContext(
            definition=definition,
            scope=transaction.scope,
            services=self.services,
            dependencies=resolution,
        )
        async with transaction:
            result = definition.install(context)
            if inspect.isawaitable(result):
                await result
            if (
                require_desired
                and generation.desired_config_revision
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
        return scope, generation

    async def activate(self, definition: FeatureDefinition) -> FeatureRecord:
        """Resolve dependencies and transactionally install one feature."""
        record = self.discover(definition)
        async with self._operation_lock(definition.feature_id):
            return await self._activate_locked(definition, record)

    async def _activate_locked(
        self,
        definition: FeatureDefinition,
        record: FeatureRecord,
    ) -> FeatureRecord:
        if record.state is FeatureState.ACTIVE:
            return record
        if record.scope is not None and record.scope.state in {
            FeatureState.ACTIVATING,
            FeatureState.DRAINING,
            FeatureState.STOPPING,
        }:
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
        record.restart_required = False
        if not resolution.ready:
            record.state = FeatureState.WAITING
            record.scope = None
            record.generation = None
            return record

        try:
            scope, generation = await self._install_generation(
                definition,
                record,
                resolution,
                config_revision=record.desired_config_revision,
                observe_record=True,
            )
            record.revisions.mark_effective(generation)
        except FeatureActivationError as error:
            record.state = FeatureState.FAILED
            record.error = error
            return record

        record.scope = scope
        record.state = FeatureState.ACTIVE
        return record

    @staticmethod
    def _validate_update_contract(
        current: FeatureDefinition,
        requested: FeatureDefinition,
    ) -> None:
        if current.dependencies != requested.dependencies:
            raise FeatureUpdateRejectedError(
                "feature service dependencies cannot change in a config update"
            )
        if current.service_scope != requested.service_scope:
            raise FeatureUpdateRejectedError(
                "feature service scope cannot change in a config update"
            )
        if current.update_strategy is not requested.update_strategy:
            raise FeatureUpdateRejectedError(
                "feature update strategy cannot change in a config update"
            )

    async def update(self, definition: FeatureDefinition) -> FeatureUpdateResult:
        """Apply one newer immutable configuration using replace or restart."""
        record = self._records.get(definition.feature_id)
        if record is None:
            raise KeyError(f"unknown feature {definition.feature_id!r}")
        async with self._operation_lock(definition.feature_id):
            return await self._update_locked(definition, record)

    async def _update_locked(
        self,
        definition: FeatureDefinition,
        record: FeatureRecord,
    ) -> FeatureUpdateResult:
        current = record.definition
        self._validate_update_contract(current, definition)
        if record.state is not FeatureState.ACTIVE or record.scope is None:
            raise FeatureUpdateRejectedError(
                f"feature {definition.feature_id!r} cannot update while {record.state.value}"
            )
        if record.retiring:
            pending = ", ".join(item.scope.generation.key for item in record.retiring)
            raise FeatureUpdateRejectedError(
                f"feature {definition.feature_id!r} still retires {pending}"
            )
        if definition.desired_config_revision < record.desired_config_revision:
            raise FeatureUpdateRejectedError(
                "feature config revision must increase monotonically"
            )
        if (
            definition.desired_config_revision == record.desired_config_revision
            and record.effective_config_revision == record.desired_config_revision
        ):
            raise FeatureUpdateRejectedError("feature config revision is already effective")
        previous_scope = record.scope
        previous_generation = previous_scope.generation
        previous_effective = record.effective_config_revision
        if previous_effective is None:
            raise FeatureUpdateRejectedError("active feature has no effective config revision")
        if definition.desired_config_revision > record.desired_config_revision:
            record.revisions.request(definition.desired_config_revision)
        record.error = None
        record.restart_required = False
        if current.update_strategy is FeatureUpdateStrategy.REPLACE:
            return await self._replace_locked(
                definition,
                record,
                current,
                previous_scope,
                previous_generation,
            )
        return await self._restart_locked(
            definition,
            record,
            current,
            previous_scope,
            previous_generation,
            previous_effective,
        )

    async def _replace_locked(
        self,
        definition: FeatureDefinition,
        record: FeatureRecord,
        previous_definition: FeatureDefinition,
        previous_scope: FeatureScope,
        previous_generation: FeatureGeneration,
    ) -> FeatureUpdateResult:
        resolution = self.dependencies.resolve(
            definition.feature_id,
            self.services,
            definition.service_scope,
        )
        if not resolution.ready:
            error = FeatureUpdateRejectedError("required services are unavailable")
            record.error = error
            return FeatureUpdateResult(
                definition.feature_id,
                definition.update_strategy,
                previous_generation.key,
                previous_generation.key,
                False,
                error=error,
            )
        try:
            scope, generation = await self._install_generation(
                definition,
                record,
                resolution,
                config_revision=record.desired_config_revision,
                observe_record=False,
            )
            record.revisions.mark_effective(generation)
        except FeatureActivationError as cause:
            error = FeatureUpdateError(
                definition.feature_id,
                definition.update_strategy,
                cause,
            )
            record.error = error
            return FeatureUpdateResult(
                definition.feature_id,
                definition.update_strategy,
                previous_generation.key,
                previous_generation.key,
                False,
                error=error,
            )

        previous_scope.observe_state(None)
        previous_scope.begin_draining()
        scope.observe_state(lambda state: setattr(record, "state", state))
        record.definition = definition
        record.dependency_resolution = resolution
        record.generation = generation
        record.scope = scope
        record.state = FeatureState.ACTIVE
        retiring = RetiringFeatureGeneration(
            previous_scope,
            FeatureStopPolicy.DRAIN,
            previous_definition.drain_timeout_seconds,
        )
        record.retiring.append(retiring)
        try:
            await retiring.scope.stop(
                retiring.policy,
                timeout_seconds=retiring.timeout_seconds,
            )
        except (FeatureDrainTimeoutError, FeatureCleanupError) as cause:
            retiring.error = cause
            error = FeatureUpdateError(
                definition.feature_id,
                definition.update_strategy,
                cause,
            )
            record.error = error
            return FeatureUpdateResult(
                definition.feature_id,
                definition.update_strategy,
                previous_generation.key,
                generation.key,
                True,
                error=error,
            )
        record.retiring.remove(retiring)
        record.error = None
        return FeatureUpdateResult(
            definition.feature_id,
            definition.update_strategy,
            previous_generation.key,
            generation.key,
            True,
        )

    async def _restart_locked(
        self,
        definition: FeatureDefinition,
        record: FeatureRecord,
        previous_definition: FeatureDefinition,
        previous_scope: FeatureScope,
        previous_generation: FeatureGeneration,
        previous_effective: int,
    ) -> FeatureUpdateResult:
        try:
            await previous_scope.stop(
                previous_definition.stop_policy,
                timeout_seconds=previous_definition.drain_timeout_seconds,
            )
        except (FeatureRestartRequiredError, FeatureDrainTimeoutError, FeatureCleanupError) as cause:
            if isinstance(cause, FeatureDrainTimeoutError):
                previous_scope.resume_active()
            record.state = previous_scope.state
            record.restart_required = isinstance(cause, FeatureRestartRequiredError)
            error = FeatureUpdateError(
                definition.feature_id,
                definition.update_strategy,
                cause,
            )
            record.error = error
            return FeatureUpdateResult(
                definition.feature_id,
                definition.update_strategy,
                previous_generation.key,
                previous_generation.key,
                False,
                error=error,
            )

        record.scope = None
        resolution = self.dependencies.resolve(
            definition.feature_id,
            self.services,
            definition.service_scope,
        )
        try:
            if not resolution.ready:
                raise FeatureUpdateRejectedError("required services are unavailable")
            scope, generation = await self._install_generation(
                definition,
                record,
                resolution,
                config_revision=record.desired_config_revision,
                observe_record=True,
            )
            record.revisions.mark_effective(generation)
        except (FeatureActivationError, FeatureUpdateRejectedError) as cause:
            recovery_resolution = self.dependencies.resolve(
                previous_definition.feature_id,
                self.services,
                previous_definition.service_scope,
            )
            try:
                if not recovery_resolution.ready:
                    raise FeatureUpdateRejectedError(
                        "required services are unavailable for recovery"
                    )
                recovery_scope, recovery_generation = await self._install_generation(
                    previous_definition,
                    record,
                    recovery_resolution,
                    config_revision=previous_effective,
                    observe_record=True,
                    require_desired=False,
                )
                record.revisions.mark_restored(recovery_generation)
            except (FeatureActivationError, FeatureUpdateRejectedError) as recovery_cause:
                error = FeatureUpdateError(
                    definition.feature_id,
                    definition.update_strategy,
                    cause,
                    recovery_error=recovery_cause,
                )
                record.definition = previous_definition
                record.state = FeatureState.FAILED
                record.error = error
                return FeatureUpdateResult(
                    definition.feature_id,
                    definition.update_strategy,
                    previous_generation.key,
                    record.generation.key if record.generation else None,
                    False,
                    error=error,
                )
            error = FeatureUpdateError(
                definition.feature_id,
                definition.update_strategy,
                cause,
                restored=True,
            )
            record.definition = previous_definition
            record.dependency_resolution = recovery_resolution
            record.scope = recovery_scope
            record.generation = recovery_generation
            record.state = FeatureState.ACTIVE
            record.error = error
            return FeatureUpdateResult(
                definition.feature_id,
                definition.update_strategy,
                previous_generation.key,
                recovery_generation.key,
                False,
                restored=True,
                error=error,
            )

        record.definition = definition
        record.dependency_resolution = resolution
        record.scope = scope
        record.generation = generation
        record.state = FeatureState.ACTIVE
        record.error = None
        return FeatureUpdateResult(
            definition.feature_id,
            definition.update_strategy,
            previous_generation.key,
            generation.key,
            True,
        )

    async def deactivate(
        self,
        feature_id: str,
        *,
        policy: FeatureStopPolicy | str | None = None,
        timeout_seconds: float | None = None,
    ) -> bool:
        """Stop one feature and wait until all owned resources are quiescent."""
        record = self._records.get(feature_id)
        if record is None:
            return False
        async with self._operation_lock(feature_id):
            return await self._deactivate_locked(
                record,
                policy=policy,
                timeout_seconds=timeout_seconds,
            )

    async def _deactivate_locked(
        self,
        record: FeatureRecord,
        *,
        policy: FeatureStopPolicy | str | None,
        timeout_seconds: float | None,
    ) -> bool:
        if record.scope is None and not record.retiring:
            record.state = FeatureState.DISCOVERED
            record.error = None
            return False
        stop_policy = FeatureStopPolicy(policy or record.definition.stop_policy)
        stop_timeout = (
            record.definition.drain_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        if record.scope is not None:
            record.state = (
                FeatureState.ACTIVE
                if stop_policy is FeatureStopPolicy.RESTART_REQUIRED
                else FeatureState.DRAINING
            )
            try:
                await record.scope.stop(stop_policy, timeout_seconds=stop_timeout)
            except FeatureRestartRequiredError as error:
                record.state = FeatureState.ACTIVE
                record.restart_required = True
                record.error = error
                return False
            except FeatureDrainTimeoutError as error:
                record.state = record.scope.state
                record.error = error
                return False
            except FeatureCleanupError as error:
                record.state = FeatureState.FAILED
                record.error = error
                return False
            record.scope = None
        for retiring in tuple(record.retiring):
            retiring_policy = FeatureStopPolicy(policy or retiring.policy)
            retiring_timeout = (
                retiring.timeout_seconds
                if timeout_seconds is None
                else timeout_seconds
            )
            try:
                await retiring.scope.stop(
                    retiring_policy,
                    timeout_seconds=retiring_timeout,
                )
            except (FeatureRestartRequiredError, FeatureDrainTimeoutError, FeatureCleanupError) as error:
                retiring.error = error
                record.state = retiring.scope.state
                record.error = error
                return False
            record.retiring.remove(retiring)
        record.state = FeatureState.DISCOVERED
        record.error = None
        record.restart_required = False
        return True
