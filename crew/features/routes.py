"""Lifecycle-owned route contributions with a runtime availability gate.

FastAPI 路由树在应用构造期固化（启动期装配），因此路由贡献的可逆性
不由「卸载路由」实现，而是由注册表所有权 + 出口闸门实现：FeatureScope
释放时注册项被摘除，闸门口径随即对该贡献返回统一的 capability_unavailable，
已挂载的旧路由对象不再被请求到达。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from crew.core.errors import CrewError
from crew.features.runtime import (
    FeatureGeneration,
    FeatureLease,
    FeatureLeaseUnavailableError,
    FeatureScope,
    FeatureState,
    RegistrationPhase,
    RegistrationState,
    RegistrationToken,
)


def _normalize_contribution_id(contribution_id: str) -> str:
    normalized = str(contribution_id or "").strip()
    if not normalized:
        raise ValueError("route contribution id must not be empty")
    if any(char.isspace() for char in normalized):
        raise ValueError("route contribution id must not contain whitespace")
    return normalized


@dataclass(frozen=True, slots=True)
class RouteContribution:
    """One mountable API surface addressable by a stable contribution id."""

    contribution_id: str
    router: Any
    prefix: str = ""
    description: str = ""

    def __post_init__(self) -> None:
        if self.router is None:
            raise TypeError("route contribution router must not be None")
        prefix = str(self.prefix or "").strip()
        if prefix and not prefix.startswith("/"):
            raise ValueError("route contribution prefix must start with '/'")
        object.__setattr__(
            self, "contribution_id", _normalize_contribution_id(self.contribution_id)
        )
        object.__setattr__(self, "prefix", prefix.rstrip("/"))
        object.__setattr__(self, "description", str(self.description or "").strip())


@dataclass(frozen=True, slots=True)
class RouteBinding:
    """Visible route contribution plus the Feature Generation that owns it."""

    contribution: RouteContribution
    generation: FeatureGeneration
    label: str
    _owner: FeatureScope = field(repr=False, compare=False)


@dataclass(slots=True)
class _RouteEntry:
    contribution: RouteContribution
    owner: FeatureScope
    token: RegistrationToken

    @property
    def visible(self) -> bool:
        return (
            self.owner.state is FeatureState.ACTIVE
            and self.token.state is RegistrationState.ACTIVE
        )

    def binding(self) -> RouteBinding:
        return RouteBinding(
            contribution=self.contribution,
            generation=self.owner.generation,
            label=self.token.label,
            _owner=self.owner,
        )


class RouteConflictError(CrewError):
    """A contribution id already has an owner that cannot be replaced by this scope."""


class RouteUnavailableError(CrewError):
    """No active Feature Generation currently provides the requested route surface."""

    code = "capability_unavailable"

    def __init__(self, contribution_id: str) -> None:
        self.contribution_id = str(contribution_id or "").strip()
        super().__init__(
            f"route contribution {self.contribution_id or '<empty>'!r} is unavailable"
        )


class RouteRegistry:
    """Mount-time assembly source and request-time gate for route contributions."""

    def __init__(self) -> None:
        self._entries: dict[str, list[_RouteEntry]] = {}

    def register(
        self,
        owner: FeatureScope,
        contribution: RouteContribution,
        *,
        label: str | None = None,
    ) -> RegistrationToken:
        """Stage a route contribution and remove only this exact generation."""
        contribution_id = contribution.contribution_id
        current = self._entries.get(contribution_id, [])
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
            raise RouteConflictError(
                f"route contribution {contribution_id!r} is already owned by "
                f"{conflicting.owner.generation.key}"
            )

        entry: _RouteEntry

        def unregister() -> None:
            entries = self._entries.get(contribution_id)
            if entries is None:
                return
            try:
                entries.remove(entry)
            except ValueError:
                return
            if not entries:
                self._entries.pop(contribution_id, None)

        token = owner.register(
            unregister,
            label=label or f"route:{contribution_id}",
            phase=RegistrationPhase.CONTRIBUTION,
        )
        entry = _RouteEntry(contribution=contribution, owner=owner, token=token)
        self._entries.setdefault(contribution_id, []).append(entry)
        return token

    def resolve(self, contribution_id: str) -> RouteBinding:
        normalized = _normalize_contribution_id(contribution_id)
        entry = self._select_entry(normalized)
        return entry.binding()

    def _select_entry(self, contribution_id: str) -> _RouteEntry:
        visible = [
            entry
            for entry in self._entries.get(contribution_id, ())
            if entry.visible
        ]
        if not visible:
            raise RouteUnavailableError(contribution_id)
        return max(
            visible,
            key=lambda item: (
                item.owner.generation.sequence,
                item.owner.generation.created_at,
            ),
        )

    def acquire_lease(
        self,
        contribution_id: str,
        *,
        label: str = "route-request",
    ) -> tuple[RouteBinding, FeatureLease] | None:
        """Resolve and claim one route generation as one atomic operation.

        A route gate must retain the exact generation selected for the request.
        Resolving first and checking ``is_available`` later leaves a race where
        a generation can drain between the check and the endpoint body.  This
        method performs the visibility lookup and lease admission without an
        await point, so the returned binding and lease always belong together.
        ``None`` means that no active generation currently owns the route.
        """
        normalized = _normalize_contribution_id(contribution_id)
        try:
            entry = self._select_entry(normalized)
        except RouteUnavailableError:
            return None
        try:
            lease = entry.owner.acquire_lease(label)
        except FeatureLeaseUnavailableError:
            # A concurrent synchronous lifecycle transition can close the
            # generation after selection.  Treat that as an unavailable route.
            return None
        return entry.binding(), lease

    def is_available(self, contribution_id: str) -> bool:
        """闸门口径：该贡献当前是否有可见的活跃 Generation。"""
        try:
            self.resolve(contribution_id)
        except (RouteUnavailableError, ValueError):
            return False
        return True

    def contribution_ids(self) -> tuple[str, ...]:
        return tuple(
            contribution_id
            for contribution_id in sorted(self._entries)
            if any(entry.visible for entry in self._entries[contribution_id])
        )

    def bindings(self) -> tuple[RouteBinding, ...]:
        """启动期装配来源：按 contribution_id 确定性排序的可见贡献。"""
        return tuple(self.resolve(key) for key in self.contribution_ids())
