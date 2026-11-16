"""Feature dependency declarations, runtime resolution, and startup planning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from crew.features.services import (
    ServiceBinding,
    ServiceKey,
    ServiceNotFoundError,
    ServiceRegistry,
    ServiceScopePath,
)


def _unique_service_keys(
    values: tuple[ServiceKey[Any], ...],
    field_name: str,
) -> tuple[ServiceKey[Any], ...]:
    normalized = tuple(values)
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{field_name} contains duplicate service keys")
    return normalized


@dataclass(frozen=True, slots=True)
class FeatureServiceDependencies:
    """Static required, optional, and provided service declarations."""

    feature_id: str
    requires: tuple[ServiceKey[Any], ...] = ()
    optional: tuple[ServiceKey[Any], ...] = ()
    provides: tuple[ServiceKey[Any], ...] = ()

    def __post_init__(self) -> None:
        feature_id = self.feature_id.strip()
        if not feature_id:
            raise ValueError("feature_id must not be empty")
        requires = _unique_service_keys(tuple(self.requires), "requires")
        optional = _unique_service_keys(tuple(self.optional), "optional")
        provides = _unique_service_keys(tuple(self.provides), "provides")
        # ServiceKey 的 eq/hash 含 version：同名不同版本是不同服务，
        # 交叉约束（required/optional/provides 互斥）必须按完整 key 判断。
        required_keys = set(requires)
        optional_keys = set(optional)
        provided_keys = set(provides)
        if required_keys & optional_keys:
            raise ValueError("a service cannot be both required and optional")
        if required_keys & provided_keys:
            raise ValueError("a feature cannot require a service it provides")
        object.__setattr__(self, "feature_id", feature_id)
        object.__setattr__(self, "requires", requires)
        object.__setattr__(self, "optional", optional)
        object.__setattr__(self, "provides", provides)


@dataclass(frozen=True, slots=True)
class FeatureDependencyResolution:
    """Resolved and missing services for one feature at one tenant path."""

    feature_id: str
    required: tuple[ServiceBinding[Any], ...]
    optional: tuple[ServiceBinding[Any], ...]
    missing_required: tuple[ServiceKey[Any], ...]
    missing_optional: tuple[ServiceKey[Any], ...]

    @property
    def ready(self) -> bool:
        return not self.missing_required


@dataclass(frozen=True, slots=True)
class FeatureDependencyBlock:
    """Static activation blocker after all resolvable providers were considered."""

    feature_id: str
    unavailable_services: tuple[ServiceKey[Any], ...]


@dataclass(frozen=True, slots=True)
class FeatureActivationPlan:
    """Best-effort startup batches plus features blocked by missing paths or cycles."""

    batches: tuple[tuple[str, ...], ...]
    blocked: tuple[FeatureDependencyBlock, ...]

    @property
    def complete(self) -> bool:
        return not self.blocked


class FeatureDependencyGraph:
    """Bipartite graph between features and their declared service capabilities.

    Provider edges are indexed by the full ``ServiceKey`` (name and major
    version), so a v2 provider never satisfies a v1 consumer.
    """

    def __init__(self) -> None:
        self._features: dict[str, FeatureServiceDependencies] = {}
        self._providers: dict[ServiceKey[Any], set[str]] = {}

    def add(self, dependencies: FeatureServiceDependencies) -> None:
        current = self._features.get(dependencies.feature_id)
        if current is not None:
            if current == dependencies:
                return
            raise ValueError(
                f"feature {dependencies.feature_id!r} already has different dependencies"
            )
        self._features[dependencies.feature_id] = dependencies
        for key in dependencies.provides:
            self._providers.setdefault(key, set()).add(dependencies.feature_id)

    def remove(self, feature_id: str) -> bool:
        dependencies = self._features.pop(feature_id, None)
        if dependencies is None:
            return False
        for key in dependencies.provides:
            providers = self._providers.get(key)
            if providers is None:
                continue
            providers.discard(feature_id)
            if not providers:
                self._providers.pop(key, None)
        return True

    def get(self, feature_id: str) -> FeatureServiceDependencies:
        try:
            return self._features[feature_id]
        except KeyError as error:
            raise KeyError(f"unknown feature {feature_id!r}") from error

    def providers(self, key: ServiceKey[Any]) -> tuple[str, ...]:
        return tuple(sorted(self._providers.get(key, ())))

    def resolve(
        self,
        feature_id: str,
        registry: ServiceRegistry,
        scope_path: ServiceScopePath | None = None,
    ) -> FeatureDependencyResolution:
        dependencies = self.get(feature_id)
        required: list[ServiceBinding[Any]] = []
        optional: list[ServiceBinding[Any]] = []
        missing_required: list[ServiceKey[Any]] = []
        missing_optional: list[ServiceKey[Any]] = []
        for key in dependencies.requires:
            try:
                required.append(registry.resolve_binding(key, scope_path))
            except ServiceNotFoundError:
                missing_required.append(key)
        for key in dependencies.optional:
            try:
                optional.append(registry.resolve_binding(key, scope_path))
            except ServiceNotFoundError:
                missing_optional.append(key)
        return FeatureDependencyResolution(
            feature_id=feature_id,
            required=tuple(required),
            optional=tuple(optional),
            missing_required=tuple(missing_required),
            missing_optional=tuple(missing_optional),
        )

    def audit(
        self,
        registry: ServiceRegistry,
        scope_path: ServiceScopePath | None = None,
    ) -> tuple[FeatureDependencyResolution, ...]:
        return tuple(
            self.resolve(feature_id, registry, scope_path)
            for feature_id in sorted(self._features)
        )

    def plan_activation(
        self,
        *,
        host_services: tuple[ServiceKey[Any], ...] = (),
    ) -> FeatureActivationPlan:
        """Build deterministic batches using required service declarations.

        This static plan is a startup diagnostic, not a substitute for scoped
        runtime resolution. A provider failure can still leave later features
        waiting even when they appear in a planned batch.
        """
        available = set(host_services)
        remaining = dict(self._features)
        batches: list[tuple[str, ...]] = []
        while remaining:
            ready = tuple(
                sorted(
                    feature_id
                    for feature_id, dependencies in remaining.items()
                    if all(key in available for key in dependencies.requires)
                )
            )
            if not ready:
                break
            batches.append(ready)
            for feature_id in ready:
                dependencies = remaining.pop(feature_id)
                available.update(dependencies.provides)

        blocked = tuple(
            FeatureDependencyBlock(
                feature_id=feature_id,
                unavailable_services=tuple(
                    key for key in dependencies.requires if key not in available
                ),
            )
            for feature_id, dependencies in sorted(remaining.items())
        )
        return FeatureActivationPlan(batches=tuple(batches), blocked=blocked)

    def undeclared_required_services(
        self,
        *,
        host_services: tuple[ServiceKey[Any], ...] = (),
    ) -> dict[str, tuple[ServiceKey[Any], ...]]:
        host_keys = set(host_services)
        missing: dict[str, tuple[ServiceKey[Any], ...]] = {}
        for feature_id, dependencies in self._features.items():
            keys = tuple(
                key
                for key in dependencies.requires
                if key not in self._providers and key not in host_keys
            )
            if keys:
                missing[feature_id] = keys
        return dict(sorted(missing.items()))
