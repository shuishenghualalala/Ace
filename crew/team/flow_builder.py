"""Default TeamPlan DAG builder."""

from __future__ import annotations

import re
from typing import Any

from crew.team.capabilities import normalize_capabilities
from crew.team.models import TeamMemberSpec
from crew.team.result_presenter import workflow_lane_order
from crew.team.roles import (
    compile_role_responsibility,
    infer_role_key,
    infer_workflow_lane,
    public_responsibility,
    role_matches_text,
    role_node_label,
    role_scope_key,
    workflow_lane_for_role,
)
from crew.team.team_spec import build_team_spec


def role_hint(member: TeamMemberSpec) -> str:
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


def workflow_lane(member: TeamMemberSpec) -> str:
    meta = member.metadata or {}
    lane = str(meta.get("workflow_lane") or "").strip().lower()
    if lane:
        return lane
    key = role_key(member)
    if key:
        return workflow_lane_for_role(key)
    hint = role_hint(member)
    return infer_workflow_lane(hint, default="build")


def role_key(member: TeamMemberSpec) -> str:
    return str((member.metadata or {}).get("role_key") or "").strip().lower()


def role_label(member: TeamMemberSpec) -> str:
    label = str((member.metadata or {}).get("role_label") or "").strip()
    return label or member.role or member.name or member.member_id


def role_slug(member: TeamMemberSpec, fallback: str) -> str:
    key = role_key(member)
    if key:
        return key.replace("-", "_")
    return f"{fallback}_{member.member_id}".replace("-", "_")


def node_metadata(lane: str, *, label: str = "", key: str = "") -> dict[str, Any]:
    normalized_lane = str(lane or "other").strip().lower() or "other"
    return {
        "workflow_lane": normalized_lane,
        "display_order": workflow_lane_order(normalized_lane),
        "role_label": label or "",
        "role_key": key or "",
    }


def planning_modes(execution_profile: dict[str, Any] | None = None) -> dict[str, str]:
    profile = execution_profile if isinstance(execution_profile, dict) else {}
    planning = profile.get("planning") if isinstance(profile.get("planning"), dict) else {}

    def _mode(name: str, default: str, allowed: set[str]) -> str:
        value = str(planning.get(name) or profile.get(name) or default).strip().lower()
        return value if value in allowed else default

    return {
        "build_plan_mode": _mode("build_plan_mode", "auto", {"auto", "required", "skip"}),
        "verify_plan_mode": _mode("verify_plan_mode", "required", {"auto", "required", "skip"}),
        "user_review_gate": _mode("user_review_gate", "on_risk", {"never", "on_risk", "always"}),
    }


def member_node_metadata(member: TeamMemberSpec, lane: str | None = None) -> dict[str, Any]:
    node_lane = lane or workflow_lane(member)
    metadata = node_metadata(node_lane, label=role_label(member), key=role_key(member))
    assigned_capabilities = normalize_capabilities(member.capabilities or [])
    capability_source = "formation_role"
    if not assigned_capabilities:
        contract_role_key = role_key(member) or infer_role_key(role_hint(member))
        assigned_capabilities = normalize_capabilities(
            compile_role_responsibility(role_key=contract_role_key).get("_assigned_capabilities") or []
        )
        capability_source = "role_catalog"
    if assigned_capabilities:
        metadata.update({
            "required_capabilities": assigned_capabilities,
            "capability_source": capability_source,
        })
    formation_responsibility = (member.metadata or {}).get("formation_responsibility")
    if isinstance(formation_responsibility, dict) and formation_responsibility:
        metadata.update({
            "formation_plan_version": int((member.metadata or {}).get("formation_plan_version") or 1),
            "formation_scope_key": role_scope_key(role_key(member)),
            "responsibility_mission": str(formation_responsibility.get("mission") or "").strip(),
            "expected_outputs": [
                str(item).strip()
                for item in (formation_responsibility.get("deliverables") or [])
                if str(item).strip()
            ],
        })
    return metadata


def _formation_responsibility(member: TeamMemberSpec, goal: str) -> dict[str, Any] | None:
    metadata = member.metadata if isinstance(member.metadata, dict) else {}
    if not metadata.get("formation_plan_version"):
        return None
    stored = metadata.get("formation_responsibility")
    if isinstance(stored, dict) and stored:
        return public_responsibility(stored)
    key = role_key(member)
    if not key:
        return None
    return public_responsibility(compile_role_responsibility(
        role_key=key,
        team_goal=goal,
        assigned_capabilities=list(member.capabilities or []),
        is_leader=False,
    ))


def _formation_member_signature(member: TeamMemberSpec, lane: str) -> tuple[str, str, tuple[str, ...]]:
    return (
        str(lane or workflow_lane(member)).strip().lower(),
        role_scope_key(role_key(member)),
        tuple(sorted(str(item) for item in (member.capabilities or []) if str(item))),
    )


def _dedupe_formation_members(members: list[TeamMemberSpec], lane: str) -> list[TeamMemberSpec]:
    """Avoid one deterministic node per duplicate Formation responsibility."""

    kept: list[TeamMemberSpec] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    for member in members:
        metadata = member.metadata if isinstance(member.metadata, dict) else {}
        if not metadata.get("formation_plan_version"):
            kept.append(member)
            continue
        signature = _formation_member_signature(member, lane)
        if signature in seen:
            continue
        seen.add(signature)
        kept.append(member)
    return kept


def _formation_node_text(
    member: TeamMemberSpec,
    *,
    task_title: str,
    goal: str,
    phase: str,
    default_title: str,
    default_detail: str,
) -> tuple[str, str]:
    responsibility = _formation_responsibility(member, goal)
    if responsibility is None:
        return default_title, default_detail
    label = role_node_label(role_key(member))
    title = f"{label}：{task_title}"
    if phase == "plan":
        title = f"{label}：{task_title}"
    elif phase == "design":
        title = f"{label}方案：{task_title}"
    elif phase == "build":
        title = f"{label}：{task_title}"
    elif phase == "docs":
        title = f"{label}：{task_title}"
    deliverables = [
        str(item).strip()
        for item in (responsibility.get("deliverables") or [])
        if str(item).strip()
    ]
    detail = (
        f"{responsibility.get('mission') or default_detail}\n"
        f"本轮交付：{'、'.join(deliverables) or '与职责匹配的可检查结果'}。\n"
        f"用户目标：{goal}"
    )
    return title, detail


def verify_role_template(member: TeamMemberSpec, task_title: str, goal: str) -> dict[str, str]:
    key = role_key(member)
    label = role_label(member)
    capabilities = " ".join(member.capabilities or []).lower()
    structured_hint = " ".join([key, label, member.name, capabilities]).lower()
    if key == "qa_engineer":
        return {
            "plan_title": f"测试方案：{task_title}",
            "plan_detail": f"只设计初版测试方案：功能路径、回归用例、通过标准、失败场景和缺陷记录方式；若实现产物尚未完成，明确标注方案需在开发完成后按实际产物格式复核/补充；先不要执行测试或验证：{goal}",
            "verify_title": f"测试验证：{task_title}",
            "verify_detail": f"先根据实际开发产物复核并必要时补充测试方案，再执行功能验证、回归检查、缺陷复现和验收结论整理：{goal}",
            "submit_noun": "测试方案",
            "complete_noun": "测试验证",
        }
    if key == "security_engineer":
        return {
            "plan_title": f"安全方案：{task_title}",
            "plan_detail": f"只设计安全方案：权限、隐私、异常输入、工具输出暴露和安全边界检查项；先不要执行安全验证：{goal}",
            "verify_title": f"安全验证：{task_title}",
            "verify_detail": f"按安全方案验证权限边界、隐私暴露、异常路径和风险处置结论：{goal}",
            "submit_noun": "安全方案",
            "complete_noun": "安全验证",
        }
    if role_matches_text("security_engineer", structured_hint):
        return {
            "plan_title": f"安全方案：{task_title}",
            "plan_detail": f"只设计安全方案：权限、隐私、异常输入、工具输出暴露和安全边界检查项；先不要执行安全验证：{goal}",
            "verify_title": f"安全验证：{task_title}",
            "verify_detail": f"按安全方案验证权限边界、隐私暴露、异常路径和风险处置结论：{goal}",
            "submit_noun": "安全方案",
            "complete_noun": "安全验证",
        }
    if role_matches_text("qa_engineer", structured_hint):
        return {
            "plan_title": f"测试方案：{task_title}",
            "plan_detail": f"只设计初版测试方案：功能路径、回归用例、通过标准、失败场景和缺陷记录方式；若实现产物尚未完成，明确标注方案需在开发完成后按实际产物格式复核/补充；先不要执行测试或验证：{goal}",
            "verify_title": f"测试验证：{task_title}",
            "verify_detail": f"先根据实际开发产物复核并必要时补充测试方案，再执行功能验证、回归检查、缺陷复现和验收结论整理：{goal}",
            "submit_noun": "测试方案",
            "complete_noun": "测试验证",
        }
    return {
        "plan_title": f"{label}方案：{task_title}",
        "plan_detail": f"只设计{label}方案：路径、通过标准、边界场景和失败场景；先不要执行验证：{goal}",
        "verify_title": f"{label}验证：{task_title}",
        "verify_detail": f"结合{label}职责验证产物是否满足目标、边界和交付标准：{goal}",
        "submit_noun": f"{label}方案",
        "complete_noun": f"{label}验证",
    }


def goal_title(goal: str) -> str:
    title = " ".join(str(goal or "").strip().split())
    if not title:
        return "当前任务"
    for prefix in ("请帮我", "帮我", "请", "麻烦", "写一个", "做一个", "实现一个", "开发一个"):
        if title.startswith(prefix) and len(title) > len(prefix):
            title = title[len(prefix):].strip(" ：:，,。")
            break
    return title[:36]


def goal_needs_build(goal: str) -> bool:
    spec = build_team_spec(goal)
    return "build" in set(spec.team_requirements.get("workflow_lanes") or [])


def team_goal_uses_shared_workspace(goal: str) -> bool:
    text = str(goal or "").strip()
    lowered = text.lower()
    if re.search(r"\.(py|js|ts|tsx|jsx|html|css|md|json|ya?ml|toml|txt|docx?|xlsx?|pptx?)\b", lowered):
        return True
    shared_markers = (
        "之前",
        "上次",
        "刚刚",
        "前面",
        "已有",
        "现有",
        "当前项目",
        "项目中",
        "仓库",
        "代码",
        "文件",
        "页面",
        "组件",
        "接口",
        "开发的",
        "实现的",
        "修复",
        "修改",
        "部署",
    )
    return any(marker in text for marker in shared_markers)


def build_default_workflow_nodes(
    team: Any,
    goal: str,
    *,
    team_spec: Any | None = None,
) -> tuple[list[dict[str, Any]], list[Any]]:
    members = list(team.members.values())
    if not members:
        return [], []
    team_spec = team_spec if team_spec is not None else build_team_spec({"goal": goal})
    execution_profile = team_spec.execution_profile
    planning = planning_modes(execution_profile)
    required_roles = list(team_spec.team_requirements.get("roles") or [])
    required_lanes = set(team_spec.team_requirements.get("workflow_lanes") or [])
    task_title = goal_title(goal)
    nodes: list[dict[str, Any]] = [
        {
            "id": "leader_plan",
            "title": f"Leader 拆分任务：{task_title}",
            "detail": (
                f"根据用户目标拆分团队任务、确定依赖、验收标准和协作顺序：{goal}\n"
                f"TeamSpec：{execution_profile.get('intent', 'mixed')}/{execution_profile.get('complexity', 'focused')}，"
                f"建议协作模式 {team_spec.collaboration_mode}，角色 {'、'.join(required_roles) or '按现有成员'}。"
            ),
            "assignee": "leader",
            "metadata": node_metadata("lead", label="拆解任务、派活跟踪、汇总反馈", key="team_lead"),
        }
    ]
    edges: list[Any] = []
    lanes: dict[str, list[TeamMemberSpec]] = {}
    for member in members:
        lanes.setdefault(workflow_lane(member), []).append(member)
    lanes = {
        lane: _dedupe_formation_members(lane_members, lane)
        for lane, lane_members in lanes.items()
    }

    def _add_node(
        node_id: str,
        title: str,
        detail: str,
        assignee: str,
        parents: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        nodes.append({
            "id": node_id,
            "title": title,
            "detail": detail,
            "assignee": assignee,
            "metadata": metadata or {},
        })
        for parent in parents or []:
            edges.append([parent, node_id])
        return node_id

    def _add_member_node(
        *,
        member: TeamMemberSpec,
        phase: str,
        node_id: str,
        title: str,
        detail: str,
        parents: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        node_title, node_detail = _formation_node_text(
            member,
            task_title=task_title,
            goal=goal,
            phase=phase,
            default_title=title,
            default_detail=detail,
        )
        return _add_node(
            node_id,
            node_title,
            node_detail,
            member.member_id,
            parents,
            metadata or member_node_metadata(member, phase),
        )

    plan_ids = [
        _add_member_node(
            member=member,
            phase="plan",
            node_id=f"plan_{index + 1}",
            title=f"规划：{task_title}",
            detail=f"明确目标、交付物、关键约束、任务拆解和验收标准：{goal}",
            parents=["leader_plan"],
            metadata=member_node_metadata(member, "plan"),
        )
        for index, member in enumerate(lanes.get("plan", []))
    ]
    design_ids = [
        _add_member_node(
            member=member,
            phase="design",
            node_id=f"design_{index + 1}",
            title=f"设计：{task_title}",
            detail=f"输出与目标匹配的交互、视觉、关键状态和实现约束：{goal}",
            parents=["leader_plan"],
            metadata=member_node_metadata(member, "design"),
        )
        for index, member in enumerate(lanes.get("design", []))
    ]
    needs_build = "build" in required_lanes
    build_members = lanes.get("build", []) if needs_build else []
    if needs_build and not build_members:
        build_members = [
            member
            for lane in ("lead", "docs", "release", "verify", "plan")
            for member in lanes.get(lane, [])
        ][:1]
    build_design_ids: list[str] = []
    should_plan_build = needs_build and planning["build_plan_mode"] != "skip"
    if should_plan_build:
        for index, member in enumerate(build_members):
            metadata = member_node_metadata(member, "design")
            metadata["build_plan_mode"] = planning["build_plan_mode"]
            metadata["user_review_gate"] = planning["user_review_gate"]
            build_design_ids.append(_add_member_node(
                member=member,
                phase="design",
                node_id=f"build_design_{index + 1}",
                title=f"实现方案：{task_title}",
                detail=f"只写实现方案，说明文件结构、关键交互、实现步骤、自测方式和风险；先不要编码或改文件：{goal}",
                parents=[*plan_ids, *design_ids] or ["leader_plan"],
                metadata=metadata,
            ))
    build_parent_ids = [*plan_ids, *design_ids, *build_design_ids] or ["leader_plan"]
    build_ids = [
        _add_member_node(
            member=member,
            phase="build",
            node_id=f"build_{index + 1}",
            title=f"实现：{task_title}",
            detail=f"完成本角色负责的实现、集成和可验证产物：{goal}",
            parents=build_parent_ids,
            metadata=member_node_metadata(member, "build"),
        )
        for index, member in enumerate(build_members)
    ]
    needs_verification = "verify" in required_lanes
    verify_members = lanes.get("verify", []) if needs_verification else []
    verify_templates = {
        member.member_id: verify_role_template(member, task_title, goal)
        for member in verify_members
    }
    test_plan_ids: list[str] = []
    for index, member in enumerate(verify_members):
        template = verify_templates[member.member_id]
        slug = role_slug(member, "verify")
        if planning["verify_plan_mode"] != "skip":
            metadata = member_node_metadata(member, "plan")
            metadata["verify_plan_mode"] = planning["verify_plan_mode"]
            metadata["user_review_gate"] = planning["user_review_gate"]
            test_plan_ids.append(_add_node(
                f"{slug}_plan_{index + 1}",
                template["plan_title"],
                template["plan_detail"],
                member.member_id,
                ["leader_plan"],
                metadata,
            ))
    review_plan_parents = [*test_plan_ids, *build_design_ids, *design_ids, *plan_ids]
    review_after_plan_ids = [
        _add_node(
            "leader_review",
            f"Leader 审阅方案：{task_title}",
            f"审阅成员提交的方案/计划，确认覆盖范围、风险和后续执行条件：{goal}",
            "leader",
            review_plan_parents,
            {
                **node_metadata("lead", label="拆解任务、派活跟踪、汇总反馈", key="team_lead"),
                "build_plan_mode": planning["build_plan_mode"],
                "verify_plan_mode": planning["verify_plan_mode"],
                "user_review_gate": planning["user_review_gate"],
            },
        )
    ] if review_plan_parents else []
    for review_id in review_after_plan_ids:
        for build_id in build_ids:
            edges.append([review_id, build_id])
    verify_refine_ids: list[str] = []
    if needs_build and test_plan_ids and build_ids:
        for index, member in enumerate(verify_members):
            template = verify_templates[member.member_id]
            slug = role_slug(member, "verify")
            metadata = member_node_metadata(member, "plan")
            metadata["verify_plan_mode"] = "refine_after_build"
            metadata["user_review_gate"] = planning["user_review_gate"]
            verify_refine_ids.append(_add_node(
                f"{slug}_refine_{index + 1}",
                f"测试方案复核：{task_title}",
                (
                    f"基于已完成实现和初版测试方案，判断测试方案是否需要微调；"
                    f"如需调整，补充实际产物路径、关键风险、回归范围和验证步骤，再进入验证：{goal}"
                ),
                member.member_id,
                [*build_ids, test_plan_ids[index] if index < len(test_plan_ids) else test_plan_ids[-1], *review_after_plan_ids],
                {
                    **metadata,
                    "plan_title": template["plan_title"],
                },
            ))
    verify_ids: list[str] = []
    for index, member in enumerate(verify_members):
        template = verify_templates[member.member_id]
        slug = role_slug(member, "verify")
        verify_ids.append(_add_node(
            f"{slug}_verify_{index + 1}",
            template["verify_title"],
            template["verify_detail"],
            member.member_id,
            [verify_refine_ids[index] if index < len(verify_refine_ids) else ""]
            if verify_refine_ids
            else ([*build_ids, *review_after_plan_ids] or [*test_plan_ids] or ["leader_plan"]),
            member_node_metadata(member, "verify"),
        ))
    should_leader_review = bool(build_ids) and not verify_ids and not review_after_plan_ids
    leader_review_parents = verify_ids or build_ids or design_ids or plan_ids or ["leader_plan"]
    if "leader_plan" not in leader_review_parents:
        leader_review_parents = ["leader_plan", *leader_review_parents]
    leader_review_ids = [
        _add_node(
            "leader_review",
            f"Leader 验收：{task_title}",
            f"对成员交付物进行功能测试、质量验收、风险检查和最终反馈：{goal}",
            "leader",
            leader_review_parents,
            node_metadata("lead", label="拆解任务、派活跟踪、汇总反馈", key="team_lead"),
        )
    ] if should_leader_review else []
    leader_review_ids = [*review_after_plan_ids, *leader_review_ids]
    docs_release_members = (
        [*lanes.get("docs", []), *lanes.get("release", [])]
        if ("docs" in required_lanes or "release" in required_lanes or needs_build)
        else []
    )
    docs_parent_ids = verify_ids or leader_review_ids or build_ids or design_ids or ["leader_plan"]
    docs_ids = [
        _add_member_node(
            member=member,
            phase="docs",
            node_id=f"handoff_{index + 1}",
            title=f"交付整理：{task_title}",
            detail=f"整理开发记录、测试结论、产物引用、风险和下一步建议：{goal}",
            parents=docs_parent_ids,
            metadata=member_node_metadata(member, workflow_lane(member)),
        )
        for index, member in enumerate(docs_release_members)
    ]
    terminal_ids = docs_ids or verify_ids or leader_review_ids or build_ids or design_ids or plan_ids or ["leader_plan"]
    if "leader_plan" not in terminal_ids:
        terminal_ids = ["leader_plan", *terminal_ids]
    _add_node(
        "leader_summary",
        f"Leader 汇总：{task_title}",
        f"汇总所有成员结果，形成面向用户的最终交付说明：{goal}",
        "leader",
        terminal_ids,
        node_metadata("summary", label="汇总结论、验收反馈", key="team_lead"),
    )
    if not needs_build and not needs_verification and not ({"docs", "release"} & required_lanes):
        return nodes, edges
    if len(nodes) > 2:
        return nodes, edges

    researcher = next(
        (
            member
            for member in members
            if workflow_lane(member) in {"plan", "docs"}
        ),
        None,
    )
    implementer = next(
        (
            member
            for member in members
            if workflow_lane(member) == "build"
        ),
        None,
    )
    tester = next(
        (
            member
            for member in members
            if workflow_lane(member) == "verify"
        ),
        None,
    )
    if researcher is not None and implementer is not None and researcher.member_id != implementer.member_id:
        nodes = [
            {
                "id": "plan",
                "title": f"规划：{task_title}",
                "detail": f"围绕用户目标梳理执行方案、关键约束和交付标准：{goal}",
                "assignee": researcher.member_id,
                "metadata": member_node_metadata(researcher, "plan"),
            },
            {
                "id": "execute",
                "title": f"实现：{task_title}",
                "detail": f"基于方案完成主要执行、验证与产物整理：{goal}",
                "assignee": implementer.member_id,
                "metadata": member_node_metadata(implementer, "build"),
            },
        ]
        edges = [["plan", "execute"]]
        if tester is not None and tester.member_id not in {researcher.member_id, implementer.member_id}:
            nodes.append({
                "id": "verify",
                "title": f"验证：{task_title}",
                "detail": f"验证主要产物是否满足目标、边界和交付标准：{goal}",
                "assignee": tester.member_id,
                "metadata": member_node_metadata(tester, "verify"),
            })
            edges.append(["execute", "verify"])
        return nodes, edges
    if implementer is not None and tester is not None and implementer.member_id != tester.member_id:
        return [
            {
                "id": "execute",
                "title": f"实现：{task_title}",
                "detail": f"完成主要实现并整理可验证产物：{goal}",
                "assignee": implementer.member_id,
                "metadata": member_node_metadata(implementer, "build"),
            },
            {
                "id": "verify",
                "title": f"验证：{task_title}",
                "detail": f"验证产物是否满足目标、边界和交付标准：{goal}",
                "assignee": tester.member_id,
                "metadata": member_node_metadata(tester, "verify"),
            },
        ], [["execute", "verify"]]
    fallback_member = implementer or researcher or members[0]
    assignee = fallback_member.member_id
    fallback_lane = "build" if implementer is not None else workflow_lane(fallback_member)
    return [
        {
            "id": "execute",
            "title": f"实现：{task_title}",
            "detail": goal,
            "assignee": assignee,
            "metadata": member_node_metadata(fallback_member, fallback_lane),
        }
    ], []
