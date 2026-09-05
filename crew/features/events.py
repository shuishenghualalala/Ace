"""Lifecycle-owned feature event sources with deterministic dispatch."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TypeAlias

from crew.core.envelope import Envelope
from crew.core.errors import CrewError
from crew.features.runtime import (
    FeatureGeneration,
    FeatureLease,
    FeatureScope,
    FeatureState,
    RegistrationPhase,
    RegistrationState,
    RegistrationToken,
    FeatureLeaseUnavailableError,
)


def _normalize_identifier(value: str, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"feature event {label} must not be empty")
    if normalized != normalized.lower() or any(char.isspace() for char in normalized):
        raise ValueError(
            f"feature event {label} must be a lowercase identifier without whitespace"
        )
    return normalized


@dataclass(frozen=True, slots=True)
class FeatureEvent:
    """One versioned event in the stable ``feature_event`` envelope."""

    feature: str
    event: str
    version: int
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValueError("feature event version must be a positive integer")
        if not isinstance(self.payload, Mapping):
            raise TypeError("feature event payload must be a mapping")
        object.__setattr__(self, "feature", _normalize_identifier(self.feature, "feature"))
        object.__setattr__(self, "event", _normalize_identifier(self.event, "event"))
        object.__setattr__(self, "version", int(self.version))
        object.__setattr__(self, "payload", dict(self.payload))

    def as_body(self) -> dict[str, Any]:
        """Return the stable event body without adding a protocol ``kind``."""
        return {
            "feature": self.feature,
            "event": self.event,
            "version": self.version,
            "payload": dict(self.payload),
        }


FeatureEventResult: TypeAlias = FeatureEvent | Iterable[FeatureEvent] | None
FeatureEventContributorHandler: TypeAlias = Callable[
    [Envelope], FeatureEventResult | Awaitable[FeatureEventResult]
]
FeatureEventPredicate: TypeAlias = Callable[[Envelope], bool]
FeatureEventSink: TypeAlias = Callable[[FeatureEvent], Awaitable[None]]


class EventFailurePolicy(str, Enum):
    """Whether a source failure is isolated or raised to its caller."""

    DEGRADE = "degrade"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class FeatureEventContributor:
    """A scoped source of events for one request/session boundary."""

    contributor_id: str
    handler: FeatureEventContributorHandler
    priority: int = 100
    failure_policy: EventFailurePolicy = EventFailurePolicy.DEGRADE
    timeout_seconds: float | None = None
    predicate: FeatureEventPredicate | None = None
    description: str = ""

    def __post_init__(self) -> None:
        if not callable(self.handler):
            raise TypeError("feature event contributor handler must be callable")
        if self.predicate is not None and not callable(self.predicate):
            raise TypeError("feature event contributor predicate must be callable")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("feature event contributor timeout must be positive")
        object.__setattr__(
            self,
            "contributor_id",
            _normalize_identifier(self.contributor_id, "contributor id"),
        )
        object.__setattr__(self, "priority", int(self.priority))
        object.__setattr__(self, "failure_policy", EventFailurePolicy(self.failure_policy))
        object.__setattr__(self, "description", str(self.description or "").strip())


@dataclass(frozen=True, slots=True)
class FeatureEventBinding:
    """Visible event source and the Generation that owns its lifetime."""

    contributor: FeatureEventContributor
    generation: FeatureGeneration
    label: str
    _owner: FeatureScope = field(repr=False, compare=False)

    def acquire_lease(self, label: str) -> FeatureLease:
        return self._owner.acquire_lease(label)


@dataclass(frozen=True, slots=True)
class FeatureEventContributionFailure:
    """Serializable evidence for an isolated source failure."""

    contributor_id: str
    generation: FeatureGeneration
    message: str
    timed_out: bool = False


@dataclass(frozen=True, slots=True)
class FeatureEventDispatchReport:
    """Successfully delivered events and isolated source failures."""

    events: tuple[FeatureEvent, ...]
    failures: tuple[FeatureEventContributionFailure, ...]


class FeatureEventContributorConflictError(CrewError):
    """A contributor id already belongs to an unrelated Feature Generation."""


class FeatureEventContributionFailedError(CrewError):
    """A fail-closed source prevented event dispatch."""

    code = "feature_event_contribution_failed"

    def __init__(
        self,
        binding: FeatureEventBinding,
        cause: BaseException,
        *,
        timed_out: bool = False,
    ) -> None:
        self.contributor_id = binding.contributor.contributor_id
        self.generation = binding.generation
        self.cause = cause
        self.timed_out = timed_out
        reason = "timed out" if timed_out else f"failed: {cause}"
        super().__init__(
            f"feature event contributor {self.contributor_id!r} "
            f"from {self.generation.key} {reason}"
        )


@dataclass(slots=True)
class _FeatureEventEntry:
    contributor: FeatureEventContributor
    owner: FeatureScope
    token: RegistrationToken

    @property
    def visible(self) -> bool:
        return (
            self.owner.state is FeatureState.ACTIVE
            and self.token.state is RegistrationState.ACTIVE
        )

    def binding(self) -> FeatureEventBinding:
        return FeatureEventBinding(
            contributor=self.contributor,
            generation=self.owner.generation,
            label=self.token.label,
            _owner=self.owner,
        )


class FeatureEventContributorRegistry:
    """Dispatch active sources while retaining their owning Generation lease."""

    def __init__(self) -> None:
        self._entries: dict[str, list[_FeatureEventEntry]] = {}

    def register(
        self,
        owner: FeatureScope,
        contributor: FeatureEventContributor,
        *,
        label: str | None = None,
    ) -> RegistrationToken:
        contributor_id = contributor.contributor_id
        current = self._entries.get(contributor_id, [])
        conflicting = next(
            (
                entry
                for entry in current
                if entry.owner.generation.feature_id != owner.generation.feature_id
                or entry.owner is owner
            ),
            None,
        )
        if current and owner.state is not FeatureState.ACTIVATING:
            conflicting = conflicting or current[0]
        if conflicting is not None:
            raise FeatureEventContributorConflictError(
                f"feature event contributor {contributor_id!r} is already owned by "
                f"{conflicting.owner.generation.key}"
            )

        entry: _FeatureEventEntry

        def unregister() -> None:
            entries = self._entries.get(contributor_id)
            if entries is None:
                return
            try:
                entries.remove(entry)
            except ValueError:
                return
            if not entries:
                self._entries.pop(contributor_id, None)

        token = owner.register(
            unregister,
            label=label or f"feature-event-contributor:{contributor_id}",
            phase=RegistrationPhase.CONTRIBUTION,
        )
        entry = _FeatureEventEntry(contributor=contributor, owner=owner, token=token)
        self._entries.setdefault(contributor_id, []).append(entry)
        return token

    def bindings(self) -> tuple[FeatureEventBinding, ...]:
        """Return one newest active binding for each contributor id."""
        bindings: list[FeatureEventBinding] = []
        for contributor_id in sorted(self._entries):
            visible = [entry for entry in self._entries[contributor_id] if entry.visible]
            if not visible:
                continue
            entry = max(
                visible,
                key=lambda item: (
                    item.owner.generation.sequence,
                    item.owner.generation.created_at,
                ),
            )
            bindings.append(entry.binding())
        return tuple(
            sorted(
                bindings,
                key=lambda binding: (
                    binding.contributor.priority,
                    binding.contributor.contributor_id,
                ),
            )
        )

    async def dispatch(
        self,
        envelope: Envelope,
        sink: FeatureEventSink,
    ) -> FeatureEventDispatchReport:
        """Generate and deliver events in source order under each Generation lease."""
        if not callable(sink):
            raise TypeError("feature event sink must be callable")
        sent_events: list[FeatureEvent] = []
        failures: list[FeatureEventContributionFailure] = []
        for binding in self.bindings():
            contributor = binding.contributor
            try:
                if contributor.predicate is not None and not contributor.predicate(envelope):
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as error:
                timed_out = isinstance(error, asyncio.TimeoutError)
                failure = FeatureEventContributionFailure(
                    contributor_id=contributor.contributor_id,
                    generation=binding.generation,
                    message=str(error),
                    timed_out=timed_out,
                )
                if contributor.failure_policy is EventFailurePolicy.FAIL:
                    raise FeatureEventContributionFailedError(
                        binding,
                        error,
                        timed_out=timed_out,
                    ) from error
                failures.append(failure)
                continue

            try:
                lease = binding.acquire_lease(
                    f"feature-events:{contributor.contributor_id}:{envelope.request_id}"
                )
            except FeatureLeaseUnavailableError:
                # A concurrent drain closed the source between snapshot and lease.
                continue

            async with lease:
                try:
                    result = contributor.handler(envelope)
                    if contributor.timeout_seconds is None:
                        result = await result if inspect.isawaitable(result) else result
                    else:
                        awaitable = result if inspect.isawaitable(result) else _completed(result)
                        result = await asyncio.wait_for(
                            awaitable,
                            timeout=contributor.timeout_seconds,
                        )
                    if result is None:
                        source_events = ()
                    elif isinstance(result, FeatureEvent):
                        source_events = (result,)
                    else:
                        if isinstance(result, (str, bytes, Mapping)):
                            raise TypeError("feature event handler must return FeatureEvent values")
                        source_events = tuple(result)
                    if any(not isinstance(event, FeatureEvent) for event in source_events):
                        raise TypeError("feature event handler must return FeatureEvent values")
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    timed_out = isinstance(error, asyncio.TimeoutError)
                    failure = FeatureEventContributionFailure(
                        contributor_id=contributor.contributor_id,
                        generation=binding.generation,
                        message=str(error),
                        timed_out=timed_out,
                    )
                    if contributor.failure_policy is EventFailurePolicy.FAIL:
                        raise FeatureEventContributionFailedError(
                            binding,
                            error,
                            timed_out=timed_out,
                        ) from error
                    failures.append(failure)
                    continue
                for event in source_events:
                    delivery = sink(event)
                    if not inspect.isawaitable(delivery):
                        raise TypeError("feature event sink must return an awaitable")
                    await delivery
                    sent_events.append(event)
        return FeatureEventDispatchReport(tuple(sent_events), tuple(failures))

async def _completed(value: FeatureEventResult) -> FeatureEventResult:
    return value


__all__ = [
    "EventFailurePolicy",
    "FeatureEvent",
    "FeatureEventBinding",
    "FeatureEventDispatchReport",
    "FeatureEventContributionFailedError",
    "FeatureEventContributionFailure",
    "FeatureEventContributor",
    "FeatureEventContributorConflictError",
    "FeatureEventContributorHandler",
    "FeatureEventContributorRegistry",
    "FeatureEventPredicate",
    "FeatureEventResult",
    "FeatureEventSink",
]
