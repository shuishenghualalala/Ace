"""Session-scoped capability profiles for the built-in Agent.

Profiles describe optional Feature-owned tools, Skills and prompt fragments.  They
never execute work and never override access control: callers must resolve the
normal authorization snapshot first, then use the resolved profile only to select
from that snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable


LEGACY_CAPABILITY_FLAGS = {
    "inspiration_creation": "sites.authoring",
    "site_creation": "sites.authoring",
}


@dataclass(frozen=True)
class CapabilityDisplay:
    name: str
    provider: str
    display_badge: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "provider": self.provider,
            "display_badge": self.display_badge,
        }


@dataclass(frozen=True)
class CapabilityProfile:
    id: str
    feature: str
    toolsets: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    prompt: str = ""
    includes: tuple[str, ...] = ()
    executors: tuple[str, ...] = ("builtin",)
    display: CapabilityDisplay | None = None

    def normalized(self) -> "CapabilityProfile":
        profile_id = str(self.id or "").strip()
        feature = str(self.feature or "").strip()
        if not profile_id or not feature:
            raise ValueError("Capability Profile 必须提供 id 和 feature")
        return replace(
            self,
            id=profile_id,
            feature=feature,
            toolsets=_unique_strings(self.toolsets),
            skills=_unique_strings(self.skills),
            includes=_unique_strings(self.includes),
            executors=tuple(value.lower() for value in _unique_strings(self.executors))
            or ("builtin",),
            prompt=str(self.prompt or "").strip(),
        )


@dataclass(frozen=True)
class ResolvedCapabilities:
    profile_ids: tuple[str, ...] = ()
    toolsets: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    prompts: tuple[str, ...] = ()
    features: tuple[str, ...] = ()

    @property
    def prompt(self) -> str:
        return "\n\n".join(self.prompts)


def _unique_strings(values: Iterable[Any]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def merge_disabled_skills(
    base_disabled: Iterable[str] | None,
    extra_disabled: Iterable[str],
) -> list[str] | None:
    """Merge Skill denials without weakening the special ``["*"]`` scope."""

    base = _unique_strings(base_disabled or ())
    if "*" in base:
        return ["*"]
    merged = _unique_strings([*base, *extra_disabled])
    return list(merged) if merged else None


def capability_profile_ids(config: dict[str, Any] | None) -> tuple[str, ...]:
    """Read explicit profiles, falling back to historical Site session flags."""

    value = config if isinstance(config, dict) else {}
    raw = value.get("capability_profiles")
    if isinstance(raw, (list, tuple)):
        return _unique_strings(raw)
    if raw is not None:
        return ()
    return _unique_strings(
        profile_id
        for flag, profile_id in LEGACY_CAPABILITY_FLAGS.items()
        if bool(value.get(flag))
    )


class CapabilityProfileRegistry:
    """Ordered registry and dependency resolver for Agent capability profiles."""

    def __init__(self) -> None:
        self._profiles: dict[str, CapabilityProfile] = {}
        # Ownership claims outlive an unregister within the current process.  During
        # Feature teardown the profile and tools cannot disappear atomically; keeping
        # the claim makes any briefly orphaned tool/Skill fail closed.
        self._claimed_toolsets: tuple[str, ...] = ()
        self._claimed_skills: tuple[str, ...] = ()

    def register(self, profile: CapabilityProfile) -> None:
        normalized = profile.normalized()
        if normalized.id in self._profiles:
            raise ValueError(f"Capability Profile 已注册：{normalized.id}")
        self._profiles[normalized.id] = normalized
        self._claimed_toolsets = _unique_strings(
            [*self._claimed_toolsets, *normalized.toolsets]
        )
        self._claimed_skills = _unique_strings([*self._claimed_skills, *normalized.skills])

    def unregister(self, profile_id: str) -> bool:
        return self._profiles.pop(str(profile_id).strip(), None) is not None

    def get(self, profile_id: str) -> CapabilityProfile | None:
        return self._profiles.get(str(profile_id).strip())

    def ids(self) -> tuple[str, ...]:
        return tuple(self._profiles)

    def owned_toolsets(self) -> tuple[str, ...]:
        return self._claimed_toolsets

    def owned_skills(self) -> tuple[str, ...]:
        return self._claimed_skills

    def resolve(
        self,
        profile_ids: Iterable[str],
        *,
        executor: str = "builtin",
        strict: bool = True,
    ) -> ResolvedCapabilities:
        ordered_profiles: list[CapabilityProfile] = []
        visited: set[str] = set()
        visiting: set[str] = set()
        normalized_executor = str(executor or "builtin").strip().lower()

        def visit(profile_id: str) -> None:
            if profile_id in visited:
                return
            if profile_id in visiting:
                raise ValueError(f"Capability Profile 存在循环依赖：{profile_id}")
            profile = self.get(profile_id)
            if profile is None:
                if strict:
                    raise ValueError(f"未知 Capability Profile：{profile_id}")
                return
            if normalized_executor not in profile.executors:
                if strict:
                    raise ValueError(
                        f"Capability Profile {profile.id} 不支持 executor={normalized_executor}"
                    )
                return
            visiting.add(profile.id)
            for dependency in profile.includes:
                visit(dependency)
            visiting.remove(profile.id)
            visited.add(profile.id)
            ordered_profiles.append(profile)

        for profile_id in _unique_strings(profile_ids):
            visit(profile_id)

        return ResolvedCapabilities(
            profile_ids=tuple(profile.id for profile in ordered_profiles),
            toolsets=_unique_strings(
                toolset for profile in ordered_profiles for toolset in profile.toolsets
            ),
            skills=_unique_strings(skill for profile in ordered_profiles for skill in profile.skills),
            prompts=_unique_strings(profile.prompt for profile in ordered_profiles if profile.prompt),
            features=_unique_strings(profile.feature for profile in ordered_profiles),
        )

    def display_for(self, profile_ids: Iterable[str]) -> CapabilityDisplay | None:
        """Return display metadata for the first explicitly attached profile."""

        for profile_id in _unique_strings(profile_ids):
            profile = self.get(profile_id)
            if profile is not None and profile.display is not None:
                return profile.display
        return None

    def filter_authorized_tools(
        self,
        catalog: Any,
        authorized_tools: Iterable[str],
        resolved: ResolvedCapabilities,
    ) -> list[str]:
        """Hide inactive Feature toolsets without expanding authorization."""

        owned = set(self.owned_toolsets())
        active = set(resolved.toolsets)
        result: list[str] = []
        for name in authorized_tools:
            toolset = catalog.toolset_for(name) or ""
            if toolset not in owned or toolset in active:
                result.append(name)
        return result

    def disabled_skills_for(
        self,
        base_disabled: Iterable[str] | None,
        resolved: ResolvedCapabilities,
    ) -> list[str] | None:
        """Hide inactive Feature Skills while preserving policy denials."""

        active = set(resolved.skills)
        blocked = [skill for skill in self.owned_skills() if skill not in active]
        return merge_disabled_skills(base_disabled, blocked)


def canonicalize_capability_config(
    config: dict[str, Any],
    registry: CapabilityProfileRegistry,
) -> dict[str, Any]:
    """Validate and persist the canonical profile list for a Session config."""

    value = dict(config)
    raw = value.get("capability_profiles")
    if raw is not None and not isinstance(raw, list):
        raise ValueError("capability_profiles 必须是字符串数组")
    if isinstance(raw, list) and any(
        not isinstance(item, str) or not item.strip() for item in raw
    ):
        raise ValueError("capability_profiles 必须是字符串数组")

    ids = capability_profile_ids(value)
    executor = str(value.get("executor") or "builtin").strip().lower()
    registry.resolve(ids, executor=executor, strict=True)
    if ids or raw is not None or any(value.get(flag) for flag in LEGACY_CAPABILITY_FLAGS):
        value["capability_profiles"] = list(ids)
    for flag in LEGACY_CAPABILITY_FLAGS:
        value.pop(flag, None)
    return value
