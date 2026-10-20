"""Ownership and rollback primitives for the unified Feature Runtime.

This module deliberately contains no directory-plugin discovery or product feature
knowledge.  Every runtime contribution is represented by a registration token owned
by one feature generation, so activation rollback and shutdown use the same path.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import TracebackType
from typing import Any, TypeAlias

from crew.core.errors import CrewError

DisposeResult: TypeAlias = Awaitable[None] | None
Disposer: TypeAlias = Callable[[], DisposeResult]


class FeatureState(str, Enum):
    """Lifecycle states visible to runtime diagnostics."""

    DISCOVERED = "discovered"
    WAITING = "waiting"
    ACTIVATING = "activating"
    ACTIVE = "active"
    DRAINING = "draining"
    STOPPING = "stopping"
    FAILED = "failed"
    DISPOSED = "disposed"


class FeatureStopPolicy(str, Enum):
    """How one active generation reaches its teardown boundary."""

    DRAIN = "drain"
    CANCEL = "cancel"
    IMMEDIATE = "immediate"
    RESTART_REQUIRED = "restart_required"


class RegistrationState(str, Enum):
    """Lifecycle state of one owned registration."""

    ACTIVE = "active"
    DISPOSING = "disposing"
    DISPOSED = "disposed"
    FAILED = "failed"


class RegistrationPhase(str, Enum):
    """Teardown phase: close public contributions before owned resources."""

    CONTRIBUTION = "contribution"
    RESOURCE = "resource"


@dataclass(frozen=True, slots=True)
class FeatureGeneration:
    """Identity of one concrete feature activation."""

    feature_id: str
    sequence: int
    desired_config_revision: int = 1
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        normalized_id = self.feature_id.strip()
        if not normalized_id:
            raise ValueError("feature_id must not be empty")
        if self.sequence < 1:
            raise ValueError("generation sequence must be greater than zero")
        if self.desired_config_revision < 1:
            raise ValueError("desired config revision must be greater than zero")
        object.__setattr__(self, "feature_id", normalized_id)

    @property
    def key(self) -> str:
        """Stable diagnostic key for this activation."""
        return f"{self.feature_id}@g{self.sequence}"


class StaleFeatureGenerationError(CrewError):
    """A generation no longer represents the desired configuration."""


class FeatureConfigRevisions:
    """Track requested and currently effective configuration revisions."""

    def __init__(
        self,
        feature_id: str,
        *,
        desired_config_revision: int = 1,
        effective_config_revision: int | None = None,
    ) -> None:
        normalized_id = feature_id.strip()
        if not normalized_id:
            raise ValueError("feature_id must not be empty")
        if desired_config_revision < 1:
            raise ValueError("desired config revision must be greater than zero")
        if effective_config_revision is not None and not (
            1 <= effective_config_revision <= desired_config_revision
        ):
            raise ValueError("effective config revision must be between one and desired")
        self.feature_id = normalized_id
        self._desired_config_revision = desired_config_revision
        self._effective_config_revision = effective_config_revision

    @property
    def desired_config_revision(self) -> int:
        return self._desired_config_revision

    @property
    def effective_config_revision(self) -> int | None:
        return self._effective_config_revision

    def request(self, revision: int) -> None:
        """Record a newer user-requested configuration revision."""
        if revision <= self._desired_config_revision:
            raise ValueError("requested config revision must increase monotonically")
        self._desired_config_revision = revision

    def new_generation(self, sequence: int) -> FeatureGeneration:
        """Create a generation for the configuration currently desired."""
        return self.generation_for(
            sequence,
            self._desired_config_revision,
        )

    def generation_for(self, sequence: int, config_revision: int) -> FeatureGeneration:
        """Create a generation for a validated desired or recovery revision."""
        if not 1 <= config_revision <= self._desired_config_revision:
            raise ValueError("generation config revision must be between one and desired")
        return FeatureGeneration(
            feature_id=self.feature_id,
            sequence=sequence,
            desired_config_revision=config_revision,
        )

    def mark_effective(self, generation: FeatureGeneration) -> None:
        """Publish a generation only if it still matches the latest request."""
        if generation.feature_id != self.feature_id:
            raise ValueError(
                f"generation {generation.key} belongs to a different feature"
            )
        if generation.desired_config_revision != self._desired_config_revision:
            raise StaleFeatureGenerationError(
                f"generation {generation.key} targets config revision "
                f"{generation.desired_config_revision}, but desired revision is "
                f"{self._desired_config_revision}"
            )
        self._effective_config_revision = generation.desired_config_revision

    def mark_restored(self, generation: FeatureGeneration) -> None:
        """Publish a recovery generation while retaining a newer desired revision."""
        if generation.feature_id != self.feature_id:
            raise ValueError(
                f"generation {generation.key} belongs to a different feature"
            )
        if not 1 <= generation.desired_config_revision <= self._desired_config_revision:
            raise ValueError("restored config revision must be between one and desired")
        self._effective_config_revision = generation.desired_config_revision


@dataclass(frozen=True, slots=True)
class FeatureCleanupIssue:
    """One labeled cleanup failure within a feature generation."""

    label: str
    error: BaseException


class FeatureCleanupError(CrewError):
    """Aggregated cleanup failures after every registration was attempted."""

    def __init__(
        self,
        generation: FeatureGeneration,
        issues: tuple[FeatureCleanupIssue, ...],
    ) -> None:
        self.generation = generation
        self.issues = issues
        labels = ", ".join(issue.label for issue in issues)
        super().__init__(
            f"feature {generation.key} cleanup failed for {len(issues)} registration(s): {labels}"
        )


class FeatureActivationError(CrewError):
    """Activation failure after its owned registrations have been rolled back."""

    def __init__(
        self,
        generation: FeatureGeneration,
        cause: BaseException,
        cleanup_error: FeatureCleanupError | None = None,
    ) -> None:
        self.generation = generation
        self.cause = cause
        self.cleanup_error = cleanup_error
        cleanup_suffix = " with cleanup failures" if cleanup_error else ""
        super().__init__(f"feature {generation.key} activation failed{cleanup_suffix}: {cause}")


@dataclass(frozen=True, slots=True)
class FeatureLeaseSnapshot:
    """Serializable identity for one request using a feature generation."""

    label: str
    acquired_at: datetime
    cancel_requested: bool


@dataclass(slots=True)
class FeatureStopDiagnostic:
    """Last stop attempt, including timeout and forced-cleanup evidence."""

    policy: FeatureStopPolicy
    timeout_seconds: float | None
    active_at_start: tuple[str, ...]
    cancel_signalled: bool = False
    timed_out: bool = False
    forced_leases: tuple[str, ...] = ()
    restart_required: bool = False


class FeatureLeaseUnavailableError(CrewError):
    """A request tried to enter a generation that no longer accepts work."""

    def __init__(self, generation: FeatureGeneration, state: FeatureState) -> None:
        self.generation = generation
        self.state = state
        super().__init__(
            f"feature {generation.key} does not accept new requests while {state.value}"
        )


class FeatureDrainTimeoutError(CrewError):
    """A stop attempt reached its deadline while requests still held leases."""

    def __init__(
        self,
        generation: FeatureGeneration,
        policy: FeatureStopPolicy,
        timeout_seconds: float,
        leases: tuple[FeatureLeaseSnapshot, ...],
    ) -> None:
        self.generation = generation
        self.policy = policy
        self.timeout_seconds = timeout_seconds
        self.leases = leases
        labels = ", ".join(lease.label for lease in leases)
        super().__init__(
            f"feature {generation.key} {policy.value} timed out after "
            f"{timeout_seconds:g}s with {len(leases)} active lease(s): {labels}"
        )


class FeatureRestartRequiredError(CrewError):
    """An active generation can only be removed at a host restart boundary."""

    def __init__(self, generation: FeatureGeneration) -> None:
        self.generation = generation
        super().__init__(f"feature {generation.key} requires a host restart to deactivate")


class FeatureLease:
    """Single request claim that keeps one feature generation alive."""

    def __init__(self, scope: FeatureScope, label: str) -> None:
        normalized_label = label.strip()
        if not normalized_label:
            raise ValueError("feature lease label must not be empty")
        self._scope = scope
        self.label = normalized_label
        self.acquired_at = datetime.now(timezone.utc)
        self.cancel_event = asyncio.Event()
        try:
            self._owner_task = asyncio.current_task()
        except RuntimeError:
            self._owner_task = None
        self._released = False

    @property
    def cancel_requested(self) -> bool:
        return self.cancel_event.is_set()

    @property
    def released(self) -> bool:
        return self._released

    @property
    def generation(self) -> FeatureGeneration:
        """Generation kept alive by this lease."""
        return self._scope.generation

    def snapshot(self) -> FeatureLeaseSnapshot:
        return FeatureLeaseSnapshot(
            label=self.label,
            acquired_at=self.acquired_at,
            cancel_requested=self.cancel_requested,
        )

    def request_cancel(self) -> None:
        """Notify cooperative work and interrupt its owning asyncio task."""
        if self._released:
            return
        self.cancel_event.set()
        owner = self._owner_task
        if owner is not None and owner is not asyncio.current_task() and not owner.done():
            owner.cancel()

    def detach_owner(self) -> None:
        """Disable task interruption for work with an explicit cancel owner."""
        self._owner_task = None

    def release(self) -> None:
        """Release this claim once; repeated release is a no-op."""
        if self._released:
            return
        self._released = True
        self._scope._release_lease(self)

    async def __aenter__(self) -> FeatureLease:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        self.release()
        return False


class RegistrationToken:
    """Single-shot, awaitable ownership token for one runtime contribution."""

    def __init__(
        self,
        generation: FeatureGeneration,
        label: str,
        disposer: Disposer,
        phase: RegistrationPhase = RegistrationPhase.RESOURCE,
    ) -> None:
        normalized_label = label.strip()
        if not normalized_label:
            raise ValueError("registration label must not be empty")
        if not callable(disposer):
            raise TypeError("registration disposer must be callable")
        self.generation = generation
        self.label = normalized_label
        self.phase = phase
        self.registered_at = datetime.now(timezone.utc)
        self._disposer = disposer
        self._state = RegistrationState.ACTIVE
        self._disposal_task: asyncio.Task[None] | None = None
        self._start_lock = asyncio.Lock()

    @property
    def state(self) -> RegistrationState:
        return self._state

    @property
    def error(self) -> BaseException | None:
        task = self._disposal_task
        if task is None or not task.done() or task.cancelled():
            return None
        return task.exception()

    async def dispose(self) -> None:
        """Run cleanup once; concurrent and repeated callers join the same work."""
        async with self._start_lock:
            if self._disposal_task is None:
                self._state = RegistrationState.DISPOSING
                self._disposal_task = asyncio.create_task(
                    self._run_disposer(),
                    name=f"feature-dispose:{self.generation.key}:{self.label}",
                )
            task = self._disposal_task
        await asyncio.shield(task)

    async def _run_disposer(self) -> None:
        try:
            result = self._disposer()
            if inspect.isawaitable(result):
                await result
        except BaseException:
            self._state = RegistrationState.FAILED
            raise
        else:
            self._state = RegistrationState.DISPOSED


class FeatureScope:
    """Own every registration created by one feature generation."""

    def __init__(self, generation: FeatureGeneration) -> None:
        self.generation = generation
        self._state = FeatureState.ACTIVATING
        self._registrations: list[RegistrationToken] = []
        self._leases: set[FeatureLease] = set()
        self._tasks: dict[asyncio.Task[Any], str] = {}
        self._task_admission_open = True
        self._task_owner_registered = False
        self._leases_drained = asyncio.Event()
        self._leases_drained.set()
        self._stop_task: asyncio.Task[None] | None = None
        self._stop_diagnostic: FeatureStopDiagnostic | None = None
        self._disposal_task: asyncio.Task[None] | None = None
        self._start_lock = asyncio.Lock()
        self._state_observer: Callable[[FeatureState], None] | None = None

    @property
    def state(self) -> FeatureState:
        return self._state

    @property
    def registrations(self) -> tuple[RegistrationToken, ...]:
        """Ordered registration snapshot for diagnostics."""
        return tuple(self._registrations)

    @property
    def active_leases(self) -> tuple[FeatureLeaseSnapshot, ...]:
        """Current request claims, oldest first, for diagnostics."""
        return tuple(
            lease.snapshot()
            for lease in sorted(self._leases, key=lambda item: item.acquired_at)
        )

    @property
    def stop_diagnostic(self) -> FeatureStopDiagnostic | None:
        return self._stop_diagnostic

    def observe_state(
        self,
        observer: Callable[[FeatureState], None] | None,
    ) -> None:
        """Mirror future scope transitions into an owning runtime record."""
        self._state_observer = observer

    def _set_state(self, state: FeatureState) -> None:
        self._state = state
        if self._state_observer is not None:
            self._state_observer(state)

    def acquire_lease(self, label: str = "request") -> FeatureLease:
        """Accept one request only while this generation is fully active."""
        if self._state is not FeatureState.ACTIVE:
            raise FeatureLeaseUnavailableError(self.generation, self._state)
        lease = FeatureLease(self, label)
        self._leases.add(lease)
        self._leases_drained.clear()
        return lease

    def create_task(
        self,
        awaitable: Coroutine[Any, Any, Any],
        *,
        name: str,
    ) -> asyncio.Task[Any]:
        """Create a background task owned by this Generation.

        Task admission closes as soon as the scope starts draining.  Every task
        gets its cancellation disposer before it is scheduled, so a failed
        installation cannot leave an unowned task behind.  Disposal cancels
        outstanding tasks and waits for their completion; the task factory is
        intended for work that escaped its request call stack, not temporary
        fan-out that callers already await.
        """
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise ValueError("feature task name must not be empty")
        if not self._task_admission_open or self._state not in {
            FeatureState.ACTIVATING,
            FeatureState.ACTIVE,
        }:
            awaitable.close()
            raise FeatureLeaseUnavailableError(self.generation, self._state)

        if not self._task_owner_registered:
            try:
                self.register(
                    self._stop_tasks,
                    label="tasks:feature-scope",
                    phase=RegistrationPhase.RESOURCE,
                )
            except BaseException:
                awaitable.close()
                raise
            self._task_owner_registered = True
        try:
            task = asyncio.create_task(awaitable, name=normalized_name)
        except BaseException:
            awaitable.close()
            raise
        self._tasks[task] = normalized_name

        def consume_done(done: asyncio.Task[Any]) -> None:
            self._tasks.pop(done, None)
            if done.cancelled():
                return
            try:
                done.exception()
            except BaseException:
                # A task's result is intentionally consumed here so a detached
                # background failure does not become an unhandled loop warning.
                return

        task.add_done_callback(consume_done)
        return task

    async def _stop_tasks(self) -> None:
        """Cancel and join the detached tasks currently owned by this scope."""
        tasks = tuple(task for task in self._tasks if not task.done())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _release_lease(self, lease: FeatureLease) -> None:
        self._leases.discard(lease)
        if not self._leases:
            self._leases_drained.set()

    def register(
        self,
        disposer: Disposer,
        *,
        label: str,
        phase: RegistrationPhase = RegistrationPhase.RESOURCE,
    ) -> RegistrationToken:
        """Assign a labeled contribution to this generation."""
        if self._state not in {FeatureState.ACTIVATING, FeatureState.ACTIVE}:
            raise RuntimeError(
                f"feature {self.generation.key} cannot register while {self._state.value}"
            )
        token = RegistrationToken(self.generation, label, disposer, phase)
        self._registrations.append(token)
        return token

    def activate(self) -> None:
        """Publish that installation completed successfully."""
        if self._state is not FeatureState.ACTIVATING:
            raise RuntimeError(
                f"feature {self.generation.key} cannot activate while {self._state.value}"
            )
        self._set_state(FeatureState.ACTIVE)

    async def rollback(self) -> None:
        """Dispose a partially installed generation."""
        await self.dispose()

    def begin_draining(self) -> None:
        """Synchronously close the lease gate before an atomic registry switch."""
        if self._state is FeatureState.DRAINING:
            return
        if self._state is not FeatureState.ACTIVE:
            raise RuntimeError(
                f"feature {self.generation.key} cannot drain while {self._state.value}"
            )
        self._task_admission_open = False
        self._set_state(FeatureState.DRAINING)

    def resume_active(self) -> None:
        """Reopen a timed-out update drain while all resources are still intact."""
        if self._state is not FeatureState.DRAINING:
            raise RuntimeError(
                f"feature {self.generation.key} cannot resume while {self._state.value}"
            )
        if self._disposal_task is not None:
            raise RuntimeError(f"feature {self.generation.key} already started disposal")
        self._task_admission_open = True
        self._set_state(FeatureState.ACTIVE)

    async def stop(
        self,
        policy: FeatureStopPolicy | str = FeatureStopPolicy.DRAIN,
        *,
        timeout_seconds: float | None = 30.0,
    ) -> None:
        """Reject new leases, settle existing work, then dispose owned effects."""
        stop_policy = FeatureStopPolicy(policy)
        if timeout_seconds is not None and timeout_seconds < 0:
            raise ValueError("feature stop timeout must not be negative")
        async with self._start_lock:
            if self._stop_task is None:
                self._stop_task = asyncio.create_task(
                    self._run_stop(stop_policy, timeout_seconds),
                    name=f"feature-scope-stop:{self.generation.key}",
                )
            task = self._stop_task
        try:
            await asyncio.shield(task)
        except BaseException:
            if task.done():
                async with self._start_lock:
                    if self._stop_task is task:
                        self._stop_task = None
            raise

    async def _run_stop(
        self,
        policy: FeatureStopPolicy,
        timeout_seconds: float | None,
    ) -> None:
        if self._state is FeatureState.DISPOSED:
            return
        active_at_start = tuple(lease.label for lease in self.active_leases)
        diagnostic = FeatureStopDiagnostic(
            policy=policy,
            timeout_seconds=timeout_seconds,
            active_at_start=active_at_start,
        )
        self._stop_diagnostic = diagnostic
        if policy is FeatureStopPolicy.RESTART_REQUIRED:
            diagnostic.restart_required = True
            raise FeatureRestartRequiredError(self.generation)

        self.begin_draining()
        if policy is FeatureStopPolicy.IMMEDIATE:
            diagnostic.forced_leases = tuple(
                lease.label for lease in self.active_leases
            )
            await self.dispose()
            return

        if policy is FeatureStopPolicy.CANCEL and self._leases:
            diagnostic.cancel_signalled = True
            for lease in tuple(self._leases):
                lease.request_cancel()

        if self._leases:
            try:
                if timeout_seconds is None:
                    await self._leases_drained.wait()
                else:
                    await asyncio.wait_for(
                        self._leases_drained.wait(),
                        timeout=timeout_seconds,
                    )
            except TimeoutError as error:
                diagnostic.timed_out = True
                raise FeatureDrainTimeoutError(
                    self.generation,
                    policy,
                    timeout_seconds if timeout_seconds is not None else 0,
                    self.active_leases,
                ) from error
        await self.dispose()

    async def dispose(self) -> None:
        """Close registration, then await all disposers in reverse order."""
        async with self._start_lock:
            if self._disposal_task is None:
                self._set_state(FeatureState.STOPPING)
                self._disposal_task = asyncio.create_task(
                    self._dispose_all(),
                    name=f"feature-scope-dispose:{self.generation.key}",
                )
            task = self._disposal_task
        await asyncio.shield(task)

    async def _dispose_all(self) -> None:
        self._task_admission_open = False
        issues: list[FeatureCleanupIssue] = []
        ordered = [
            token
            for phase in (RegistrationPhase.CONTRIBUTION, RegistrationPhase.RESOURCE)
            for token in reversed(self._registrations)
            if token.phase is phase
        ]
        for token in ordered:
            try:
                await token.dispose()
            except (Exception, asyncio.CancelledError) as error:
                issues.append(FeatureCleanupIssue(label=token.label, error=error))
        if issues:
            self._set_state(FeatureState.FAILED)
            raise FeatureCleanupError(self.generation, tuple(issues))
        self._set_state(FeatureState.DISPOSED)


class FeatureTransaction:
    """Activation boundary that rolls uncommitted registrations back."""

    def __init__(self, generation: FeatureGeneration) -> None:
        self.scope = FeatureScope(generation)
        self._committed = False

    @property
    def generation(self) -> FeatureGeneration:
        return self.scope.generation

    @property
    def committed(self) -> bool:
        return self._committed

    def register(
        self,
        disposer: Disposer,
        *,
        label: str,
        phase: RegistrationPhase = RegistrationPhase.RESOURCE,
    ) -> RegistrationToken:
        return self.scope.register(disposer, label=label, phase=phase)

    def commit(self) -> FeatureScope:
        if self._committed:
            raise RuntimeError(f"feature transaction {self.generation.key} already committed")
        self.scope.activate()
        self._committed = True
        return self.scope

    async def rollback(self) -> None:
        if self._committed:
            raise RuntimeError(f"feature transaction {self.generation.key} already committed")
        await self.scope.rollback()

    async def __aenter__(self) -> FeatureTransaction:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        if self._committed:
            return False
        cleanup_error: FeatureCleanupError | None = None
        try:
            await self.scope.rollback()
        except FeatureCleanupError as error:
            cleanup_error = error
        if exc is not None:
            raise FeatureActivationError(self.generation, exc, cleanup_error) from exc
        if cleanup_error is not None:
            raise cleanup_error
        return False
