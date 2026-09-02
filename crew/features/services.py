"""Scoped service ownership and feature dependency resolution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Generic, TypeVar, cast

from crew.core.errors import CrewError
from crew.features.runtime import (
    FeatureGeneration,
    FeatureScope,
    FeatureState,
    RegistrationState,
    RegistrationToken,
)

T = TypeVar("T")


def _normalize_identifier(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    return normalized


@dataclass(frozen=True, slots=True)
class ServiceKey(Generic[T]):
    """Typed, stable identity for a runtime capability."""

    name: str

    def __post_init__(self) -> None:
        normalized = self.name.strip()
        if not normalized:
            raise ValueError("service key must not be empty")
        object.__setattr__(self, "name", normalized)


class ServiceScopeKind(str, Enum):
    """Supported service visibility levels, from broadest to narrowest."""

    GLOBAL = "global"
    WORKSPACE = "workspace"
    USER = "user"
    SESSION = "session"


@dataclass(frozen=True, slots=True)
class ServiceScopePath:
    """Nested tenant path used for registration and nearest-scope lookup."""

    workspace_id: str | None = None
    user_id: str | None = None
    session_id: str | None = None

    def __post_init__(self) -> None:
        workspace_id = _normalize_identifier(self.workspace_id, "workspace_id")
        user_id = _normalize_identifier(self.user_id, "user_id")
        session_id = _normalize_identifier(self.session_id, "session_id")
        if user_id is not None and workspace_id is None:
            raise ValueError("user scope requires workspace_id")
        if session_id is not None and user_id is None:
            raise ValueError("session scope requires user_id")
        object.__setattr__(self, "workspace_id", workspace_id)
        object.__setattr__(self, "user_id", user_id)
        object.__setattr__(self, "session_id", session_id)

    @classmethod
    def global_scope(cls) -> ServiceScopePath:
        return cls()

    @classmethod
    def workspace(cls, workspace_id: str) -> ServiceScopePath:
        return cls(workspace_id=workspace_id)

    @classmethod
    def user(cls, workspace_id: str, user_id: str) -> ServiceScopePath:
        return cls(workspace_id=workspace_id, user_id=user_id)

    @classmethod
    def session(
        cls,
        workspace_id: str,
        user_id: str,
        session_id: str,
    ) -> ServiceScopePath:
        return cls(workspace_id=workspace_id, user_id=user_id, session_id=session_id)

    def at(self, kind: ServiceScopeKind) -> ServiceScopePath:
        """Return the canonical prefix represented by one scope kind."""
        if kind is ServiceScopeKind.GLOBAL:
            return ServiceScopePath.global_scope()
        if kind is ServiceScopeKind.WORKSPACE:
            if self.workspace_id is None:
                raise ValueError("workspace service registration requires workspace_id")
            return ServiceScopePath.workspace(self.workspace_id)
        if kind is ServiceScopeKind.USER:
            if self.workspace_id is None or self.user_id is None:
                raise ValueError("user service registration requires workspace_id and user_id")
            return ServiceScopePath.user(self.workspace_id, self.user_id)
        if self.workspace_id is None or self.user_id is None or self.session_id is None:
            raise ValueError(
                "session service registration requires workspace_id, user_id, and session_id"
            )
        return self

    def identity(self, kind: ServiceScopeKind) -> tuple[str, ...]:
        canonical = self.at(kind)
        return tuple(
            value
            for value in (
                canonical.workspace_id,
                canonical.user_id,
                canonical.session_id,
            )
            if value is not None
        )

    def resolution_order(self) -> tuple[tuple[ServiceScopeKind, ServiceScopePath], ...]:
        """Return nearest-to-broadest paths for service lookup."""
        candidates: list[tuple[ServiceScopeKind, ServiceScopePath]] = []
        if self.session_id is not None:
            candidates.append((ServiceScopeKind.SESSION, self.at(ServiceScopeKind.SESSION)))
        if self.user_id is not None:
            candidates.append((ServiceScopeKind.USER, self.at(ServiceScopeKind.USER)))
        if self.workspace_id is not None:
            candidates.append((ServiceScopeKind.WORKSPACE, self.at(ServiceScopeKind.WORKSPACE)))
        candidates.append((ServiceScopeKind.GLOBAL, ServiceScopePath.global_scope()))
        return tuple(candidates)


@dataclass(frozen=True, slots=True)
class ServiceBinding(Generic[T]):
    """Diagnostic snapshot of one registered service implementation."""

    key: ServiceKey[T]
    value: T
    scope_kind: ServiceScopeKind
    scope_path: ServiceScopePath
    generation: FeatureGeneration
    label: str
    visible: bool


@dataclass(slots=True)
class _ServiceEntry(Generic[T]):
    key: ServiceKey[T]
    value: T
    scope_kind: ServiceScopeKind
    scope_path: ServiceScopePath
    owner: FeatureScope
    token: RegistrationToken

    @property
    def visible(self) -> bool:
        return (
            self.owner.state is FeatureState.ACTIVE
            and self.token.state is RegistrationState.ACTIVE
        )

    def snapshot(self) -> ServiceBinding[T]:
        return ServiceBinding(
            key=self.key,
            value=self.value,
            scope_kind=self.scope_kind,
            scope_path=self.scope_path,
            generation=self.owner.generation,
            label=self.token.label,
            visible=self.visible,
        )


class ServiceConflictError(CrewError):
    """Two generations attempted to own the same service address."""


class ServiceNotFoundError(CrewError):
    """No visible service matched a requested key and tenant path."""


ServiceAddress = tuple[str, ServiceScopeKind, tuple[str, ...]]


class ServiceRegistry:
    """Own and resolve services across global, workspace, user, and session scopes."""

    def __init__(self) -> None:
        self._entries: dict[ServiceAddress, _ServiceEntry[Any]] = {}

    @staticmethod
    def _address(
        key: ServiceKey[Any],
        scope_kind: ServiceScopeKind,
        scope_path: ServiceScopePath,
    ) -> ServiceAddress:
        return (key.name, scope_kind, scope_path.identity(scope_kind))

    def register(
        self,
        owner: FeatureScope,
        key: ServiceKey[T],
        value: T,
        *,
        scope_kind: ServiceScopeKind = ServiceScopeKind.GLOBAL,
        scope_path: ServiceScopePath | None = None,
        label: str | None = None,
    ) -> RegistrationToken:
        """Register a service under FeatureScope ownership.

        Registrations made while a feature is activating remain invisible until
        that scope becomes active.  The returned token removes only this exact
        registration, so an old generation cannot unregister a later one.
        """
        path = (scope_path or ServiceScopePath.global_scope()).at(scope_kind)
        address = self._address(key, scope_kind, path)
        current = self._entries.get(address)
        if current is not None:
            raise ServiceConflictError(
                f"service {key.name!r} at {scope_kind.value}:{path.identity(scope_kind)!r} "
                f"is already owned by {current.owner.generation.key}"
            )

        entry: _ServiceEntry[T]

        def unregister() -> None:
            if self._entries.get(address) is entry:
                self._entries.pop(address, None)

        registration_label = label or f"service:{key.name}@{scope_kind.value}"
        token = owner.register(unregister, label=registration_label)
        entry = _ServiceEntry(
            key=key,
            value=value,
            scope_kind=scope_kind,
            scope_path=path,
            owner=owner,
            token=token,
        )
        self._entries[address] = entry
        return token

    def resolve_binding(
        self,
        key: ServiceKey[T],
        scope_path: ServiceScopePath | None = None,
    ) -> ServiceBinding[T]:
        """Resolve the nearest visible implementation for a tenant path."""
        path = scope_path or ServiceScopePath.global_scope()
        for scope_kind, candidate in path.resolution_order():
            entry = self._entries.get(self._address(key, scope_kind, candidate))
            if entry is not None and entry.visible:
                return cast(ServiceBinding[T], entry.snapshot())
        raise ServiceNotFoundError(
            f"service {key.name!r} is not available for scope {path!r}"
        )

    def resolve(
        self,
        key: ServiceKey[T],
        scope_path: ServiceScopePath | None = None,
    ) -> T:
        return self.resolve_binding(key, scope_path).value

    def get(
        self,
        key: ServiceKey[T],
        scope_path: ServiceScopePath | None = None,
        default: T | None = None,
    ) -> T | None:
        try:
            return self.resolve(key, scope_path)
        except ServiceNotFoundError:
            return default

    def contains(
        self,
        key: ServiceKey[Any],
        scope_path: ServiceScopePath | None = None,
    ) -> bool:
        try:
            self.resolve_binding(key, scope_path)
        except ServiceNotFoundError:
            return False
        return True

    def bindings(self, *, include_inactive: bool = True) -> tuple[ServiceBinding[Any], ...]:
        snapshots = (entry.snapshot() for entry in self._entries.values())
        if not include_inactive:
            snapshots = (binding for binding in snapshots if binding.visible)
        return tuple(
            sorted(
                snapshots,
                key=lambda item: (
                    item.key.name,
                    item.scope_kind.value,
                    item.scope_path.identity(item.scope_kind),
                ),
            )
        )
