"""Session-scoped model bindings for members of an external Team."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from crew.agent.external.runtime_profile import canonical_runtime_model_id
from crew.team.agent_profile import RUNTIME_DEFAULT_MODEL_ID, canonical_profile_model_id
from crew.team.roles import is_crew_builtin_agent


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _revision(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, default)


def visible_team_session_id(session_id: str) -> str:
    """Normalize internal turn/member sidechains to the visible Team Session."""

    sid = str(session_id or "").strip()
    return sid.split("::turn::", 1)[0] if "::turn::" in sid else sid


@dataclass
class TeamMemberModelBindingError(ValueError):
    code: str
    message: str

    def __str__(self) -> str:
        return self.message


def _team_config(config: dict[str, Any]) -> dict[str, Any]:
    return dict(config.get("team")) if isinstance(config.get("team"), dict) else {}


def _binding_defaults(
    external_store: Any,
    team: dict[str, Any],
    *,
    owner_account_id: str,
    builtin_model_id: str,
) -> dict[str, dict[str, Any]]:
    defaults: dict[str, dict[str, Any]] = {}
    now = _now()
    for member in team.get("members") or []:
        if not isinstance(member, dict):
            continue
        agent_id = str(member.get("agent_id") or "").strip()
        if not agent_id or agent_id in defaults:
            continue
        if is_crew_builtin_agent(agent_id):
            defaults[agent_id] = {
                "agent_id": agent_id,
                "runtime_id": "builtin",
                "model_id": str(builtin_model_id or "").strip(),
                "binding_source": "inherited_at_session_creation",
                "revision": 1,
                "selected_at": now,
            }
            continue
        agent, runtime = external_store.agent_with_runtime(
            agent_id,
            owner_account_id=owner_account_id,
        )
        overlay_model_id = canonical_profile_model_id(agent, runtime)
        selected_model_id = "" if overlay_model_id == RUNTIME_DEFAULT_MODEL_ID else overlay_model_id
        defaults[agent_id] = {
            "agent_id": agent_id,
            "runtime_id": str(runtime.get("id") or agent.get("runtime_id") or ""),
            "model_id": selected_model_id,
            "binding_source": "inherited_at_session_creation",
            "revision": 1,
            "selected_at": now,
        }
    return defaults


def materialize_team_member_model_bindings(
    session_store: Any,
    external_store: Any,
    session_id: str,
    *,
    owner_account_id: str,
    builtin_model_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Persist explicit bindings for every current Team member exactly once."""

    visible_session_id = visible_team_session_id(session_id)
    raw = session_store.get_agent_config(
        visible_session_id,
        owner_account_id=owner_account_id,
    ) or {}
    if str(raw.get("executor") or "").strip().lower() != "team":
        raise TeamMemberModelBindingError("not_team_session", "当前会话不是 Team Session")
    raw_team_config = _team_config(raw)
    external_team_id = str(raw_team_config.get("external_team_id") or "").strip()
    if not external_team_id:
        raise TeamMemberModelBindingError("not_external_team", "当前 Team 未绑定外部团队")
    try:
        team = external_store.get_team(external_team_id, owner_account_id=owner_account_id)
        defaults = _binding_defaults(
            external_store,
            team,
            owner_account_id=owner_account_id,
            builtin_model_id=builtin_model_id,
        )
    except KeyError as exc:
        raise TeamMemberModelBindingError(
            "team_member_not_found",
            "外部团队、成员或运行时不存在",
        ) from exc
    runtimes_by_id = {
        runtime_id: external_store.get_runtime(runtime_id)
        for runtime_id in {
            str(binding.get("runtime_id") or "")
            for binding in defaults.values()
            if str(binding.get("runtime_id") or "") not in {"", "builtin"}
        }
    }

    def updater(current: dict[str, Any]) -> dict[str, Any]:
        current_team = _team_config(current)
        if str(current.get("executor") or "").strip().lower() != "team" or str(
            current_team.get("external_team_id") or ""
        ).strip() != external_team_id:
            raise TeamMemberModelBindingError("team_binding_changed", "Team Session 绑定已变化，请重试")
        existing = current_team.get("member_model_bindings")
        bindings = {
            str(key): dict(value)
            for key, value in (existing or {}).items()
            if str(key) and isinstance(value, dict)
        } if isinstance(existing, dict) else {}
        changed = False
        for agent_id, default in defaults.items():
            binding = bindings.get(agent_id)
            if binding is None:
                bindings[agent_id] = dict(default)
                changed = True
                continue
            normalized_binding_revision = max(1, _revision(binding.get("revision"), 1))
            if (
                str(binding.get("agent_id") or "") != agent_id
                or binding.get("revision") != normalized_binding_revision
            ):
                binding = {
                    **binding,
                    "agent_id": agent_id,
                    "revision": normalized_binding_revision,
                }
                bindings[agent_id] = binding
                changed = True
            if is_crew_builtin_agent(agent_id):
                continue
            current_runtime_id = str(default.get("runtime_id") or "")
            current_model_id = str(binding.get("model_id") or "").strip()
            canonical_model_id = canonical_runtime_model_id(
                runtimes_by_id.get(current_runtime_id),
                current_model_id,
            ) or current_model_id
            replacement_reason = ""
            if str(binding.get("runtime_id") or "") != current_runtime_id:
                replacement_reason = "runtime_replaced"
            if canonical_model_id != current_model_id:
                replacement_reason = "runtime_model_migration"
            if replacement_reason:
                bindings[agent_id] = {
                    **binding,
                    "agent_id": agent_id,
                    "runtime_id": current_runtime_id,
                    "model_id": canonical_model_id,
                    "binding_source": replacement_reason,
                    "revision": max(1, _revision(binding.get("revision"), 1) + 1),
                    "selected_at": _now(),
                }
                changed = True
        revision = _revision(current_team.get("model_binding_revision"))
        if bindings and revision == 0:
            changed = True
        if changed:
            revision = revision + 1 if revision else 1
        current["executor"] = "team"
        current["team"] = {
            **current_team,
            "external_team_id": external_team_id,
            "member_model_bindings": bindings,
            "model_binding_revision": revision,
        }
        return current

    updated = session_store.update_agent_config(
        visible_session_id,
        updater,
        owner_account_id=owner_account_id,
    )
    return updated, team


def set_team_member_model_binding(
    session_store: Any,
    session_id: str,
    *,
    owner_account_id: str,
    agent_id: str,
    runtime_id: str,
    model_id: str,
    binding_source: str = "session_override",
    expected_revision: int | None = None,
) -> dict[str, Any]:
    """Atomically update one materialized member binding and Team revision."""

    visible_session_id = visible_team_session_id(session_id)
    target_agent_id = str(agent_id or "").strip()
    selected_model_id = str(model_id or "").strip()
    selected_runtime_id = str(runtime_id or "").strip()
    if not target_agent_id:
        raise TeamMemberModelBindingError("member_required", "member_id 必填")

    def updater(current: dict[str, Any]) -> dict[str, Any]:
        if str(current.get("executor") or "").strip().lower() != "team":
            raise TeamMemberModelBindingError("not_team_session", "当前会话不是 Team Session")
        current_team = _team_config(current)
        existing = current_team.get("member_model_bindings")
        bindings = {
            str(key): dict(value)
            for key, value in (existing or {}).items()
            if str(key) and isinstance(value, dict)
        } if isinstance(existing, dict) else {}
        binding = bindings.get(target_agent_id)
        if binding is None:
            raise TeamMemberModelBindingError("session_or_member_not_found", "Team 成员不存在")
        team_revision = _revision(current_team.get("model_binding_revision"))
        if expected_revision is not None and expected_revision != team_revision:
            raise TeamMemberModelBindingError("model_binding_stale", "模型绑定已被其他请求更新")
        if (
            str(binding.get("model_id") or "") == selected_model_id
            and str(binding.get("runtime_id") or "") == selected_runtime_id
        ):
            return current
        bindings[target_agent_id] = {
            **binding,
            "agent_id": target_agent_id,
            "runtime_id": selected_runtime_id,
            "model_id": selected_model_id,
            "binding_source": str(binding_source or "session_override"),
            "revision": max(1, _revision(binding.get("revision"), 1) + 1),
            "selected_at": _now(),
        }
        current["team"] = {
            **current_team,
            "member_model_bindings": bindings,
            "model_binding_revision": team_revision + 1 if team_revision else 1,
        }
        return current

    return session_store.update_agent_config(
        visible_session_id,
        updater,
        owner_account_id=owner_account_id,
    )
