"""Ownership and rollback primitives for the unified Feature Runtime.

This module deliberately contains no directory-plugin discovery or product feature
knowledge.  Every runtime contribution is represented by a registration token owned
by one feature generation, so activation rollback and shutdown use the same path.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import TracebackType
from typing import TypeAlias

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


class RegistrationState(str, Enum):
    """Lifecycle state of one owned registration."""

    ACTIVE = "active"
    DISPOSING = "disposing"
    DISPOSED = "disposed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class FeatureGeneration:
    """Identity of one concrete feature activation."""

    feature_id: str
    sequence: int
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        normalized_id = self.feature_id.strip()
        if not normalized_id:
            raise ValueError("feature_id must not be empty")
        if self.sequence < 1:
            raise ValueError("generation sequence must be greater than zero")
        object.__setattr__(self, "feature_id", normalized_id)

    @property
    def key(self) -> str:
        """Stable diagnostic key for this activation."""
        return f"{self.feature_id}@{self.sequence}"


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


class RegistrationToken:
    """Single-shot, awaitable ownership token for one runtime contribution."""

    def __init__(
        self,
        generation: FeatureGeneration,
        label: str,
        disposer: Disposer,
    ) -> None:
        normalized_label = label.strip()
        if not normalized_label:
            raise ValueError("registration label must not be empty")
        if not callable(disposer):
            raise TypeError("registration disposer must be callable")
        self.generation = generation
        self.label = normalized_label
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
        self._disposal_task: asyncio.Task[None] | None = None
        self._start_lock = asyncio.Lock()

    @property
    def state(self) -> FeatureState:
        return self._state

    @property
    def registrations(self) -> tuple[RegistrationToken, ...]:
        """Ordered registration snapshot for diagnostics."""
        return tuple(self._registrations)

    def register(self, disposer: Disposer, *, label: str) -> RegistrationToken:
        """Assign a labeled contribution to this generation."""
        if self._state not in {FeatureState.ACTIVATING, FeatureState.ACTIVE}:
            raise RuntimeError(
                f"feature {self.generation.key} cannot register while {self._state.value}"
            )
        token = RegistrationToken(self.generation, label, disposer)
        self._registrations.append(token)
        return token

    def activate(self) -> None:
        """Publish that installation completed successfully."""
        if self._state is not FeatureState.ACTIVATING:
            raise RuntimeError(
                f"feature {self.generation.key} cannot activate while {self._state.value}"
            )
        self._state = FeatureState.ACTIVE

    async def rollback(self) -> None:
        """Dispose a partially installed generation."""
        await self.dispose()

    async def dispose(self) -> None:
        """Close registration, then await all disposers in reverse order."""
        async with self._start_lock:
            if self._disposal_task is None:
                self._state = FeatureState.STOPPING
                self._disposal_task = asyncio.create_task(
                    self._dispose_all(),
                    name=f"feature-scope-dispose:{self.generation.key}",
                )
            task = self._disposal_task
        await asyncio.shield(task)

    async def _dispose_all(self) -> None:
        issues: list[FeatureCleanupIssue] = []
        for token in reversed(self._registrations):
            try:
                await token.dispose()
            except (Exception, asyncio.CancelledError) as error:
                issues.append(FeatureCleanupIssue(label=token.label, error=error))
        if issues:
            self._state = FeatureState.FAILED
            raise FeatureCleanupError(self.generation, tuple(issues))
        self._state = FeatureState.DISPOSED


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

    def register(self, disposer: Disposer, *, label: str) -> RegistrationToken:
        return self.scope.register(disposer, label=label)

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
