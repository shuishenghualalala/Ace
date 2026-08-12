"""Team policy diagnostics.

This module never mutates a user-defined team. It only reports role mismatch,
missing capability, and consent-required actions so the Leader/runtime can
explain the tradeoff and ask before changing staffing.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from crew.team.models import TeamMemberSpec
from crew.team import flow_builder
from crew.team.roles import role_preset
from crew.team.team_spec import TeamSpec


@dataclass(frozen=True)
class TeamPolicyWarning:
    code: str
    message: str
    severity: str = "info"
    member_id: str = ""
    suggested_action: str = ""
    consent_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TeamPolicyReport:
    warnings: list[TeamPolicyWarning] = field(default_factory=list)
    missing_capabilities: list[str] = field(default_factory=list)
    role_coverage: dict[str, list[str]] = field(default_factory=dict)
    user_team_locked: bool = True
    staffing_strategy: str = "suggest_only"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["warnings"] = [item.to_dict() for item in self.warnings]
        return data


def _member_capability_text(member: TeamMemberSpec) -> str:
    meta = member.metadata or {}
    return " ".join([
        member.member_id,
        member.name,
        member.role,
        " ".join(member.capabilities or []),
        str(meta.get("role_key") or ""),
        str(meta.get("role_label") or ""),
        str(meta.get("workflow_lane") or ""),
    ]).lower()


def _member_matches_role(member: TeamMemberSpec, role_key: str) -> bool:
    lane = flow_builder.workflow_lane(member)
    preset = role_preset(role_key)
    expected_lane = str(preset.get("workflow_lane") or "").strip().lower()
    if expected_lane and lane == expected_lane:
        return True
    hint = _member_capability_text(member)
    signals = [
        str(preset.get("key") or ""),
        str(preset.get("label") or ""),
        *[str(item) for item in preset.get("capabilities") or []],
        *[str(item) for item in preset.get("keywords") or []],
    ]
    return any(signal and signal.lower() in hint for signal in signals)


def analyze_team_policy(
    *,
    spec: TeamSpec,
    members: list[TeamMemberSpec],
) -> TeamPolicyReport:
    """Return staffing diagnostics without changing the team."""

    warnings: list[TeamPolicyWarning] = []
    role_coverage: dict[str, list[str]] = {}
    required_roles = list(spec.team_requirements.get("roles") or [])
    for role_key in required_roles:
        matched = [
            member.member_id
            for member in members
            if _member_matches_role(member, role_key)
        ]
        role_coverage[role_key] = matched
        if not matched:
            preset = role_preset(role_key)
            warnings.append(TeamPolicyWarning(
                code="missing_role",
                severity="warning",
                message=f"当前团队缺少「{preset['label']}」能力，系统会先按用户团队执行，但建议补充对应成员。",
                suggested_action=f"建议增加或指定一名成员承担「{preset['label']}」。",
                consent_required=True,
            ))

    leader_like = [member for member in members if member.member_id == "leader" or flow_builder.workflow_lane(member) == "lead"]
    verify_members = [member for member in members if flow_builder.workflow_lane(member) == "verify"]
    required_lanes = set(spec.team_requirements.get("workflow_lanes") or [])
    if "verify" in required_lanes and not verify_members:
        warnings.append(TeamPolicyWarning(
            code="leader_testing_conflict",
            severity="warning",
            member_id=leader_like[0].member_id if leader_like else "leader",
            message="当前任务需要独立验证，但团队没有明确测试成员；Leader 兼职测试会降低验收独立性。",
            suggested_action="建议用户确认是否补充 QA，或明确接受 Leader 临时承担验证。",
            consent_required=True,
        ))

    if len(members) <= 1 and str(spec.execution_profile.get("complexity") or "focused") != "simple":
        warnings.append(TeamPolicyWarning(
            code="understaffed_team",
            severity="warning",
            message="当前团队成员较少，复杂任务可能出现排队、上下文压力或验收不独立。",
            suggested_action="建议执行中如出现阻塞，再询问用户是否补员或拆分任务。",
            consent_required=True,
        ))

    missing_capabilities = [
        role_key for role_key, matched in role_coverage.items() if not matched
    ]
    consent_required_actions = list(spec.policy.get("consent_required_actions") or [])
    if consent_required_actions:
        for action in consent_required_actions:
            warnings.append(TeamPolicyWarning(
                code="consent_required",
                severity="info",
                message=action,
                suggested_action="需要调整团队、补员或高影响操作时先向用户确认。",
                consent_required=True,
            ))

    return TeamPolicyReport(
        warnings=warnings,
        missing_capabilities=missing_capabilities,
        role_coverage=role_coverage,
        user_team_locked=bool(spec.policy.get("user_team_locked", True)),
        staffing_strategy=str(spec.policy.get("staffing_strategy") or "suggest_only"),
    )
