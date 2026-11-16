"""统一管理外部 Runtime 的空闲、硬截止与交互等待策略。

``timeout`` 在历史代码里同时表示过空闲等待和整次进程执行时长，
导致 ACP、CLI、Followup 各自维护一套隐式规则。本模块只负责把配置
解析成明确的 budget；适配器仍可保留各自的协议实现。

配置约定：

* ``hard_timeout_seconds`` 缺省时沿用现有协议的兼容规则；
* 显式设置 ``hard_timeout_seconds: 0`` 表示不设置硬截止；
* ``idle_timeout_seconds`` 仍是无输出/无事件的等待窗口。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Mapping

DEFAULT_EXTERNAL_IDLE_SECONDS = 120.0
DEFAULT_INTERACTIVE_IDLE_FLOOR_SECONDS = 330.0
DEFAULT_INTERACTION_TIMEOUT_SECONDS = 300.0
DEFAULT_BINDING_GRACE_SECONDS = 30.0
DEFAULT_ACP_HARD_MULTIPLIER = 4.0
DEFAULT_ACP_HARD_ADD_SECONDS = 900.0

_MISSING = object()


def _finite_positive(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed) or parsed <= 0:
        return default
    return parsed


def _optional_non_negative(value: Any) -> float | None | object:
    if value is None:
        return _MISSING
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return _MISSING
    if not math.isfinite(parsed) or parsed < 0:
        return _MISSING
    return parsed


@dataclass(frozen=True)
class TimeoutBudget:
    """One external execution's resolved timeout contract."""

    idle_seconds: float
    hard_seconds: float | None
    interaction_seconds: float
    binding_grace_seconds: float = DEFAULT_BINDING_GRACE_SECONDS

    def hard_deadline(self, started_at: float | None = None) -> float | None:
        """Return a monotonic absolute deadline, or ``None`` for unlimited."""
        if self.hard_seconds is None:
            return None
        return (time.monotonic() if started_at is None else started_at) + self.hard_seconds

    @property
    def binding_ttl_seconds(self) -> float:
        """Keep callback tokens alive through the execution plus a small grace period."""
        base = self.hard_seconds
        if base is None:
            base = max(self.idle_seconds, self.interaction_seconds)
        return max(1.0, base + self.binding_grace_seconds)


@dataclass(frozen=True)
class TimeoutPolicy:
    """Central policy with compatibility defaults for existing runtimes."""

    external_idle_seconds: float = DEFAULT_EXTERNAL_IDLE_SECONDS
    interactive_idle_floor_seconds: float = DEFAULT_INTERACTIVE_IDLE_FLOOR_SECONDS
    interaction_timeout_seconds: float = DEFAULT_INTERACTION_TIMEOUT_SECONDS
    binding_grace_seconds: float = DEFAULT_BINDING_GRACE_SECONDS
    hard_timeout_seconds: float | None = None
    hard_timeout_configured: bool = False
    acp_hard_multiplier: float = DEFAULT_ACP_HARD_MULTIPLIER
    acp_hard_add_seconds: float = DEFAULT_ACP_HARD_ADD_SECONDS
    external_idle_configured: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "TimeoutPolicy":
        raw = value if isinstance(value, Mapping) else {}

        def pick(*names: str) -> Any:
            for name in names:
                if name in raw:
                    return raw[name]
            return _MISSING

        idle_value = pick("external_idle_seconds", "idle_timeout_seconds", "idle_seconds")
        idle = _finite_positive(
            idle_value,
            DEFAULT_EXTERNAL_IDLE_SECONDS,
        )
        floor = _finite_positive(
            pick("interactive_idle_floor_seconds", "team_idle_floor_seconds"),
            DEFAULT_INTERACTIVE_IDLE_FLOOR_SECONDS,
        )
        interaction = _finite_positive(
            pick("interaction_timeout_seconds", "followup_timeout_seconds"),
            DEFAULT_INTERACTION_TIMEOUT_SECONDS,
        )
        grace = _finite_positive(
            pick("binding_grace_seconds", "interaction_binding_grace_seconds"),
            DEFAULT_BINDING_GRACE_SECONDS,
        )
        hard_value = pick("hard_timeout_seconds", "external_hard_timeout_seconds", "hard_timeout")
        parsed_hard = _optional_non_negative(hard_value)
        hard = None if parsed_hard is _MISSING else parsed_hard
        hard_configured = parsed_hard is not _MISSING
        return cls(
            external_idle_seconds=idle,
            interactive_idle_floor_seconds=floor,
            interaction_timeout_seconds=interaction,
            binding_grace_seconds=grace,
            hard_timeout_seconds=hard if isinstance(hard, (int, float)) else None,
            hard_timeout_configured=hard_configured,
            acp_hard_multiplier=_finite_positive(
                pick("acp_hard_multiplier"),
                DEFAULT_ACP_HARD_MULTIPLIER,
            ),
            acp_hard_add_seconds=_finite_positive(
                pick("acp_hard_add_seconds"),
                DEFAULT_ACP_HARD_ADD_SECONDS,
            ),
            external_idle_configured=idle_value is not _MISSING,
        )

    def resolve_external(
        self,
        base_timeout: float,
        *,
        mode: str = "single_agent",
        has_interaction_binding: bool = False,
        protocol: str = "",
        hard_timeout: float | None | object = _MISSING,
    ) -> TimeoutBudget:
        """Resolve an external execution without scattering protocol constants."""
        idle = _finite_positive(
            self.external_idle_seconds if self.external_idle_configured else base_timeout,
            self.external_idle_seconds,
        )
        if has_interaction_binding or mode == "team_execute":
            idle = max(idle, self.interactive_idle_floor_seconds)

        explicit = hard_timeout
        if explicit is _MISSING:
            explicit = self.hard_timeout_seconds if self.hard_timeout_configured else _MISSING
        if explicit is not _MISSING:
            parsed = _optional_non_negative(explicit)
            if parsed is _MISSING:
                explicit = _MISSING
            else:
                hard = None if parsed == 0 else float(parsed)
        if explicit is _MISSING:
            normalized = str(protocol or "").strip().lower()
            if normalized in {"cli", "command", "external-cli"}:
                # CLI capture historically used timeout as its wall-clock budget.
                hard = idle
            elif normalized in {"acp", "acp-stdio"}:
                # Preserve the ACP watchdog while moving its formula here.
                hard = max(idle * self.acp_hard_multiplier, idle + self.acp_hard_add_seconds)
            else:
                # Codex app-server and managed adapters historically had no
                # adapter-level hard deadline; the parent TaskRuntime remains
                # their outer guard.
                hard = None
        return TimeoutBudget(
            idle_seconds=idle,
            hard_seconds=hard,
            interaction_seconds=self.interaction_timeout_seconds,
            binding_grace_seconds=self.binding_grace_seconds,
        )


def remaining_seconds(deadline: float | None, *, now: float | None = None) -> float | None:
    """Return remaining monotonic time; ``None`` denotes unlimited."""
    if deadline is None:
        return None
    remaining = deadline - (time.monotonic() if now is None else now)
    if remaining <= 0:
        return 0.0
    return remaining
