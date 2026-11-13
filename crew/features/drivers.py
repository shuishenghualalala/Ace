"""Lifecycle-owned execution drivers selected by open request mode identifiers."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import TypeAlias

from crew.core.envelope import Envelope, ResponseChunk
from crew.core.errors import CrewError
from crew.features.runtime import (
    FeatureGeneration,
    FeatureLease,
    FeatureScope,
    FeatureState,
    RegistrationPhase,
    RegistrationState,
    RegistrationToken,
)

ExecutionDriverHandler: TypeAlias = Callable[
    [Envelope],
    AsyncIterator[ResponseChunk],
]


def _normalize_mode(mode: str) -> str:
    normalized = str(mode or "").strip()
    if not normalized:
        raise ValueError("execution driver mode must not be empty")
    if normalized != normalized.lower() or any(char.isspace() for char in normalized):
        raise ValueError(
            "execution driver mode must be a lowercase identifier without whitespace"
        )
    return normalized


@dataclass(frozen=True, slots=True)
class ExecutionDriver:
    """One stream-producing execution capability addressable by request mode."""

    mode: str
    execute: ExecutionDriverHandler
    capabilities: tuple[str, ...] = ()
    description: str = ""

    def __post_init__(self) -> None:
        if not callable(self.execute):
            raise TypeError("execution driver handler must be callable")
        capabilities = tuple(
            dict.fromkeys(
                value
                for item in self.capabilities
                if (value := str(item or "").strip())
            )
        )
        object.__setattr__(self, "mode", _normalize_mode(self.mode))
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "description", str(self.description or "").strip())


@dataclass(frozen=True, slots=True)
class ExecutionDriverBinding:
    """Visible driver plus the Feature Generation that owns its lifetime."""

    driver: ExecutionDriver
    generation: FeatureGeneration
    label: str
    _owner: FeatureScope = field(repr=False, compare=False)

    def acquire_lease(self, label: str) -> FeatureLease:
        return self._owner.acquire_lease(label)


@dataclass(slots=True)
class _ExecutionDriverEntry:
    driver: ExecutionDriver
    owner: FeatureScope
    token: RegistrationToken

    @property
    def visible(self) -> bool:
        return (
            self.owner.state is FeatureState.ACTIVE
            and self.token.state is RegistrationState.ACTIVE
        )

    def binding(self) -> ExecutionDriverBinding:
        return ExecutionDriverBinding(
            driver=self.driver,
            generation=self.owner.generation,
            label=self.token.label,
            _owner=self.owner,
        )


class ExecutionDriverConflictError(CrewError):
    """A mode already has an owner that cannot be replaced by this scope."""


class ExecutionDriverUnavailableError(CrewError):
    """No active Feature Generation currently provides the requested mode."""

    code = "capability_unavailable"

    def __init__(self, mode: str) -> None:
        self.mode = str(mode or "").strip()
        super().__init__(f"execution mode {self.mode or '<empty>'!r} is unavailable")


class ExecutionDriverRegistry:
    """Resolve mode handlers while preserving FeatureScope ownership and drain."""

    def __init__(self) -> None:
        self._entries: dict[str, list[_ExecutionDriverEntry]] = {}

    def register(
        self,
        owner: FeatureScope,
        driver: ExecutionDriver,
        *,
        label: str | None = None,
    ) -> RegistrationToken:
        """Stage a driver contribution and remove only this exact generation."""
        mode = driver.mode
        current = self._entries.get(mode, [])
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
            raise ExecutionDriverConflictError(
                f"execution mode {mode!r} is already owned by "
                f"{conflicting.owner.generation.key}"
            )

        entry: _ExecutionDriverEntry

        def unregister() -> None:
            entries = self._entries.get(mode)
            if entries is None:
                return
            try:
                entries.remove(entry)
            except ValueError:
                return
            if not entries:
                self._entries.pop(mode, None)

        token = owner.register(
            unregister,
            label=label or f"execution-driver:{mode}",
            phase=RegistrationPhase.CONTRIBUTION,
        )
        entry = _ExecutionDriverEntry(driver=driver, owner=owner, token=token)
        self._entries.setdefault(mode, []).append(entry)
        return token

    def resolve(self, mode: str) -> ExecutionDriverBinding:
        try:
            normalized = _normalize_mode(mode)
        except ValueError as error:
            raise ExecutionDriverUnavailableError(mode) from error
        visible = [
            entry for entry in self._entries.get(normalized, ()) if entry.visible
        ]
        if not visible:
            raise ExecutionDriverUnavailableError(normalized)
        entry = max(
            visible,
            key=lambda item: (
                item.owner.generation.sequence,
                item.owner.generation.created_at,
            ),
        )
        return entry.binding()

    def get(self, mode: str) -> ExecutionDriverBinding | None:
        try:
            return self.resolve(mode)
        except ExecutionDriverUnavailableError:
            return None

    def modes(self) -> tuple[str, ...]:
        return tuple(
            mode
            for mode in sorted(self._entries)
            if any(entry.visible for entry in self._entries[mode])
        )

    def bindings(self) -> tuple[ExecutionDriverBinding, ...]:
        return tuple(self.resolve(mode) for mode in self.modes())

    async def dispatch(self, envelope: Envelope) -> AsyncIterator[ResponseChunk]:
        """Execute through the resolved Generation while holding its request lease."""
        binding = self.resolve(envelope.mode)
        async with binding.acquire_lease(f"execution:{envelope.request_id}"):
            stream = binding.driver.execute(envelope)
            try:
                async for chunk in stream:
                    yield chunk
            finally:
                close = getattr(stream, "aclose", None)
                if callable(close):
                    result = close()
                    if inspect.isawaitable(result):
                        await result
