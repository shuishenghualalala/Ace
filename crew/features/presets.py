"""Lifecycle-owned, product-neutral Agent preset contributions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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


def _items(values: tuple[str, ...] | list[str] | str | None) -> tuple[str, ...] | None:
    if values is None:
        return None
    if isinstance(values, str):
        values = (values,)
    result: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if item and item not in result:
            result.append(item)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class AgentPresetContribution:
    """Immutable model-facing policy for one named Agent preset.

    The contract intentionally contains only strings and immutable collections;
    Agent construction remains a host concern.  ``reserved_*`` capabilities are
    hidden from ordinary Agents and become available only when this preset is
    selected.
    """

    name: str
    description: str
    system_prompt: str
    agent_id: str | None = None
    fixed_skills: tuple[str, ...] | None = None
    toolsets: tuple[str, ...] | None = None
    tools: tuple[str, ...] | None = None
    toolset_additions: tuple[str, ...] = ()
    context_tags: tuple[str, ...] = ()
    disclosure_mode: str = "progressive"
    reserved_toolsets: tuple[str, ...] = ()
    reserved_skills: tuple[str, ...] = ()
    model: str = "inherit"
    max_iterations: int | None = None
    background: bool = False
    source: str = "feature"

    def __post_init__(self) -> None:
        name = str(self.name or "").strip()
        if not name:
            raise ValueError("agent preset name must not be empty")
        if not str(self.system_prompt or "").strip():
            raise ValueError("agent preset system prompt must not be empty")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "description", str(self.description or "").strip())
        object.__setattr__(self, "system_prompt", str(self.system_prompt).strip())
        object.__setattr__(self, "agent_id", str(self.agent_id or f"subagent:{name}").strip())
        for field_name in (
            "fixed_skills", "toolsets", "tools", "toolset_additions",
            "context_tags", "reserved_toolsets", "reserved_skills",
        ):
            object.__setattr__(self, field_name, _items(getattr(self, field_name)) or ())
        object.__setattr__(self, "disclosure_mode", str(self.disclosure_mode or "progressive").strip().lower())
        if self.disclosure_mode not in {"progressive", "direct"}:
            raise ValueError("agent preset disclosure mode must be progressive or direct")
        if self.max_iterations is not None:
            object.__setattr__(self, "max_iterations", int(self.max_iterations))
        object.__setattr__(self, "model", str(self.model or "inherit").strip() or "inherit")
        object.__setattr__(self, "source", str(self.source or "feature").strip())

    def as_spec(self) -> dict[str, Any]:
        return {
            "preset_name": self.name,
            "system_prompt": self.system_prompt,
            "agent_id": self.agent_id,
            "preset_skills": list(self.fixed_skills),
            "toolsets": list(self.toolsets) if self.toolsets else None,
            "tools": list(self.tools) if self.tools else None,
            "toolset_additions": list(self.toolset_additions),
            "context_tags": list(self.context_tags),
            "disclosure_mode": self.disclosure_mode,
            "reserved_toolsets": list(self.reserved_toolsets),
            "reserved_skills": list(self.reserved_skills),
            "model": self.model,
            "max_iterations": self.max_iterations,
            "background": self.background,
        }


@dataclass(frozen=True, slots=True)
class AgentPresetBinding:
    contribution: AgentPresetContribution
    generation: FeatureGeneration
    label: str
    _owner: FeatureScope = field(repr=False, compare=False)

    def acquire_lease(self, label: str) -> FeatureLease:
        return self._owner.acquire_lease(label)


@dataclass(slots=True)
class _Entry:
    contribution: AgentPresetContribution
    owner: FeatureScope
    token: RegistrationToken

    @property
    def visible(self) -> bool:
        return self.owner.state is FeatureState.ACTIVE and self.token.state is RegistrationState.ACTIVE

    def binding(self) -> AgentPresetBinding:
        return AgentPresetBinding(
            self.contribution,
            self.owner.generation,
            self.token.label,
            self.owner,
        )


class AgentPresetConflictError(CrewError):
    """A preset name is already owned by an unrelated generation."""


class AgentPresetUnavailableError(CrewError):
    """No active generation currently provides the requested preset."""

    code = "capability_unavailable"


class AgentPresetRegistry:
    """Generation-aware registry for model-facing Agent preset policies."""

    def __init__(self) -> None:
        self._entries: dict[str, list[_Entry]] = {}
        # Exclusive capability names remain deny-listed after disposal. This
        # prevents an unavailable preset's private tools/skills leaking into
        # ordinary Agents between generations.
        self._reserved_toolset_names: set[str] = set()
        self._reserved_skill_names: set[str] = set()

    def register(
        self,
        owner: FeatureScope,
        contribution: AgentPresetContribution,
        *,
        label: str | None = None,
    ) -> RegistrationToken:
        name = contribution.name
        current = self._entries.get(name, [])
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
            raise AgentPresetConflictError(
                f"agent preset {name!r} is already owned by {conflicting.owner.generation.key}"
            )
        self._reserved_toolset_names.update(contribution.reserved_toolsets)
        self._reserved_skill_names.update(contribution.reserved_skills)
        entry: _Entry

        def unregister() -> None:
            entries = self._entries.get(name)
            if entries is None:
                return
            try:
                entries.remove(entry)
            except ValueError:
                return
            if not entries:
                self._entries.pop(name, None)

        token = owner.register(
            unregister,
            label=label or f"agent-preset:{name}",
            phase=RegistrationPhase.CONTRIBUTION,
        )
        entry = _Entry(contribution, owner, token)
        self._entries.setdefault(name, []).append(entry)
        return token

    def resolve(self, name: str) -> AgentPresetBinding:
        normalized = str(name or "").strip()
        if not normalized:
            raise AgentPresetUnavailableError("agent preset name must not be empty")
        visible = [entry for entry in self._entries.get(normalized, ()) if entry.visible]
        if not visible:
            raise AgentPresetUnavailableError(f"agent preset {normalized!r} is unavailable")
        entry = max(
            visible,
            key=lambda item: (
                item.owner.generation.sequence,
                item.owner.generation.created_at,
            ),
        )
        return entry.binding()

    def bindings(self) -> tuple[AgentPresetBinding, ...]:
        result: list[AgentPresetBinding] = []
        for name in sorted(self._entries):
            try:
                result.append(self.resolve(name))
            except AgentPresetUnavailableError:
                continue
        return tuple(result)

    def list(self) -> list[AgentPresetContribution]:
        return [binding.contribution for binding in self.bindings()]

    def names(self) -> list[str]:
        return [binding.contribution.name for binding in self.bindings()]

    def reserved_toolsets(self) -> tuple[str, ...]:
        return tuple(sorted(self._reserved_toolset_names))

    def reserved_skills(self) -> tuple[str, ...]:
        return tuple(sorted(self._reserved_skill_names))


__all__ = [
    "AgentPresetBinding",
    "AgentPresetConflictError",
    "AgentPresetContribution",
    "AgentPresetRegistry",
    "AgentPresetUnavailableError",
]
