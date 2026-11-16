"""Lifecycle-owned request context contributions with deterministic ordering."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TypeAlias

from crew.core.envelope import Envelope
from crew.core.errors import CrewError
from crew.core.types import Message
from crew.features.runtime import (
    FeatureGeneration,
    FeatureScope,
    FeatureState,
    RegistrationPhase,
    RegistrationState,
    RegistrationToken,
)


class ContextPhase(str, Enum):
    """Stable points where request context can be extended."""

    REQUEST = "request"
    PROMPT = "prompt"


class ContextFailurePolicy(str, Enum):
    """Whether one contributor failure aborts or degrades the request."""

    DEGRADE = "degrade"
    FAIL = "fail"


_CONTEXT_PHASE_ORDER = {
    ContextPhase.REQUEST: 100,
    ContextPhase.PROMPT: 200,
}


@dataclass(frozen=True, slots=True)
class ContextContribution:
    """One contributor's parameters, prompt fragments, and persistent messages."""

    params: Mapping[str, Any] = field(default_factory=dict)
    prompt_parts: tuple[str, ...] = ()
    messages: tuple[Message, ...] = ()

    def __post_init__(self) -> None:
        params = {str(key): value for key, value in self.params.items() if str(key)}
        prompt_parts = tuple(
            text
            for item in self.prompt_parts
            if (text := str(item or "").strip())
        )
        messages = tuple(self.messages)
        if any(not isinstance(message, Message) for message in messages):
            raise TypeError("context contribution messages must contain Message values")
        object.__setattr__(self, "params", params)
        object.__setattr__(self, "prompt_parts", prompt_parts)
        object.__setattr__(self, "messages", messages)


ContextContributorHandler: TypeAlias = Callable[
    [Envelope],
    Awaitable[ContextContribution | None],
]
ContextContributorPredicate: TypeAlias = Callable[[Envelope], bool]


def _normalize_contributor_id(contributor_id: str) -> str:
    normalized = str(contributor_id or "").strip()
    if not normalized:
        raise ValueError("context contributor id must not be empty")
    if normalized != normalized.lower() or any(char.isspace() for char in normalized):
        raise ValueError(
            "context contributor id must be a lowercase identifier without whitespace"
        )
    return normalized


@dataclass(frozen=True, slots=True)
class ContextContributor:
    """A scoped producer of request parameters or model-visible prompt context."""

    contributor_id: str
    handler: ContextContributorHandler
    phase: ContextPhase = ContextPhase.REQUEST
    priority: int = 100
    failure_policy: ContextFailurePolicy = ContextFailurePolicy.DEGRADE
    timeout_seconds: float | None = None
    predicate: ContextContributorPredicate | None = None
    model_visible: bool = True
    persistent: bool = False
    description: str = ""

    def __post_init__(self) -> None:
        if not callable(self.handler):
            raise TypeError("context contributor handler must be callable")
        if self.predicate is not None and not callable(self.predicate):
            raise TypeError("context contributor predicate must be callable")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("context contributor timeout must be positive")
        if self.persistent and not self.model_visible:
            raise ValueError("persistent prompt context must be model visible")
        object.__setattr__(
            self,
            "contributor_id",
            _normalize_contributor_id(self.contributor_id),
        )
        object.__setattr__(self, "phase", ContextPhase(self.phase))
        object.__setattr__(self, "priority", int(self.priority))
        object.__setattr__(
            self,
            "failure_policy",
            ContextFailurePolicy(self.failure_policy),
        )
        object.__setattr__(self, "description", str(self.description or "").strip())


@dataclass(frozen=True, slots=True)
class ContextContributionFailure:
    """Serializable evidence for one degraded contribution."""

    contributor_id: str
    generation: FeatureGeneration
    message: str
    timed_out: bool = False


@dataclass(frozen=True, slots=True)
class ContextContributionReport:
    """Merged context output in deterministic contributor order."""

    params: Mapping[str, Any]
    prompt_parts: tuple[str, ...]
    persistent_prompt_parts: tuple[str, ...]
    persistent_messages: tuple[Message, ...]
    failures: tuple[ContextContributionFailure, ...]


@dataclass(frozen=True, slots=True)
class ContextContributorBinding:
    contributor: ContextContributor
    generation: FeatureGeneration
    label: str
    _owner: FeatureScope = field(repr=False, compare=False)


@dataclass(slots=True)
class _ContextContributorEntry:
    contributor: ContextContributor
    owner: FeatureScope
    token: RegistrationToken

    @property
    def visible(self) -> bool:
        return (
            self.owner.state is FeatureState.ACTIVE
            and self.token.state is RegistrationState.ACTIVE
        )

    def binding(self) -> ContextContributorBinding:
        return ContextContributorBinding(
            contributor=self.contributor,
            generation=self.owner.generation,
            label=self.token.label,
            _owner=self.owner,
        )


class ContextContributorConflictError(CrewError):
    """A contributor id already belongs to an unrelated Feature."""


class ContextContributionFailedError(CrewError):
    """A fail-closed contributor prevented request context construction."""

    code = "context_contribution_failed"

    def __init__(
        self,
        binding: ContextContributorBinding,
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
            f"context contributor {self.contributor_id!r} "
            f"from {self.generation.key} {reason}"
        )


class ContextContributorRegistry:
    """Run active contributors by priority under their owning Generation lease."""

    def __init__(self) -> None:
        self._entries: dict[str, list[_ContextContributorEntry]] = {}

    def register(
        self,
        owner: FeatureScope,
        contributor: ContextContributor,
        *,
        label: str | None = None,
    ) -> RegistrationToken:
        contributor_id = contributor.contributor_id
        current = self._entries.get(contributor_id, [])
        conflicting = next(
            (
                entry
                for entry in current
                if entry.owner.generation.feature_id
                != owner.generation.feature_id
                or entry.owner is owner
            ),
            None,
        )
        if current and owner.state is not FeatureState.ACTIVATING:
            conflicting = conflicting or current[0]
        if conflicting is not None:
            raise ContextContributorConflictError(
                f"context contributor {contributor_id!r} is already owned by "
                f"{conflicting.owner.generation.key}"
            )

        entry: _ContextContributorEntry

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
            label=label or f"context-contributor:{contributor_id}",
            phase=RegistrationPhase.CONTRIBUTION,
        )
        entry = _ContextContributorEntry(
            contributor=contributor,
            owner=owner,
            token=token,
        )
        self._entries.setdefault(contributor_id, []).append(entry)
        return token

    def bindings(
        self,
        phase: ContextPhase | str | None = None,
    ) -> tuple[ContextContributorBinding, ...]:
        selected_phase = ContextPhase(phase) if phase is not None else None
        bindings: list[ContextContributorBinding] = []
        for contributor_id in sorted(self._entries):
            visible = [
                entry
                for entry in self._entries[contributor_id]
                if entry.visible
                and (
                    selected_phase is None
                    or entry.contributor.phase is selected_phase
                )
            ]
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
                    _CONTEXT_PHASE_ORDER[binding.contributor.phase],
                    binding.contributor.priority,
                    binding.contributor.contributor_id,
                ),
            )
        )

    async def contribute(
        self,
        envelope: Envelope,
        *,
        phase: ContextPhase | str = ContextPhase.REQUEST,
    ) -> ContextContributionReport:
        """Collect one phase; later contributors deterministically override params."""
        params: dict[str, Any] = {}
        prompt_parts: list[str] = []
        persistent_prompt_parts: list[str] = []
        persistent_messages: list[Message] = []
        failures: list[ContextContributionFailure] = []

        for binding in self.bindings(phase):
            contributor = binding.contributor
            try:
                if contributor.predicate is not None and not contributor.predicate(envelope):
                    continue
                async with binding._owner.acquire_lease(
                    f"context:{contributor.contributor_id}:{envelope.request_id}"
                ):
                    pending = contributor.handler(envelope)
                    if not inspect.isawaitable(pending):
                        raise TypeError("context contributor handler must return an awaitable")
                    if contributor.timeout_seconds is None:
                        result = await pending
                    else:
                        result = await asyncio.wait_for(
                            pending,
                            timeout=contributor.timeout_seconds,
                        )
                if result is None:
                    continue
                if not isinstance(result, ContextContribution):
                    raise TypeError(
                        "context contributor handler must return ContextContribution or None"
                    )
                if result.messages and not contributor.persistent:
                    raise TypeError(
                        "context contribution messages require a persistent contributor"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                timed_out = isinstance(error, asyncio.TimeoutError)
                failure = ContextContributionFailure(
                    contributor_id=contributor.contributor_id,
                    generation=binding.generation,
                    message=str(error),
                    timed_out=timed_out,
                )
                if contributor.failure_policy is ContextFailurePolicy.FAIL:
                    raise ContextContributionFailedError(
                        binding,
                        error,
                        timed_out=timed_out,
                    ) from error
                failures.append(failure)
                continue

            params.update(result.params)
            if contributor.model_visible:
                prompt_parts.extend(result.prompt_parts)
                if contributor.persistent:
                    persistent_prompt_parts.extend(result.prompt_parts)
                persistent_messages.extend(result.messages)

        return ContextContributionReport(
            params=params,
            prompt_parts=tuple(prompt_parts),
            persistent_prompt_parts=tuple(persistent_prompt_parts),
            persistent_messages=tuple(persistent_messages),
            failures=tuple(failures),
        )
