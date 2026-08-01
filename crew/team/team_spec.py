"""TeamSpec: shared task intake profile for team suggestion and execution.

The first version is deliberately local and deterministic. It gives the
JiuwenSwarm-style flow a shared structure without making team creation wait on
an LLM call: understand the user goal, choose collaboration shape, then let
team suggestion and TeamPlan generation consume the same profile.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from crew.team.capabilities import capabilities_from_text, normalize_capabilities


TeamIntent = Literal["chat", "question", "research", "implementation", "testing", "documentation", "mixed"]
TeamComplexity = Literal["simple", "focused", "multi_role"]
PlanStrategy = Literal["direct", "rule_dag", "llm_dag", "planning_role_first", "require_user_review"]
StaffingStrategy = Literal["fixed_team", "suggest_only", "ask_before_fill"]
ReflectionPolicy = Literal["none", "on_failure", "after_planning", "before_final", "high_risk"]


@dataclass(frozen=True)
class TeamSpec:
    version: int
    goal: str
    collaboration_mode: str = "leader_mesh"
    execution_profile: dict[str, Any] = field(default_factory=dict)
    team_requirements: dict[str, Any] = field(default_factory=dict)
    planning: dict[str, Any] = field(default_factory=dict)
    policy: dict[str, Any] = field(default_factory=dict)
    deliverables: list[dict[str, str]] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    risk_level: str = "low"
    uncertainty: str = "low"
    planner_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _contains_any(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _signal_goal(goal: str) -> str:
    """Use the first three structured goal points for staffing inference.

    The fourth point describes acceptance governance.  Generic phrases such as
    "Leader 审阅并汇总" must not fabricate a reviewer or writer requirement.
    """

    lines = str(goal or "").splitlines()
    structured: dict[int, str] = {}
    prefix: list[str] = []
    for line in lines:
        match = re.match(
            r"^\s*([1-4])[.、]\s*(?:负责范围|所需能力|交付结果|验收标准)\s*[:：]?\s*(.*)$",
            line,
        )
        if match:
            structured[int(match.group(1))] = match.group(2).strip()
        elif not structured and line.strip():
            prefix.append(line.strip())
    if structured:
        return "\n".join([*prefix, *(structured.get(index, "") for index in (1, 2, 3))])
    return str(goal or "")


def _make_team_spec(
    *,
    goal: str,
    intent: TeamIntent,
    complexity: TeamComplexity,
    collaboration_mode: str = "leader_mesh",
    roles: list[str] | None = None,
    lanes: list[str] | None = None,
    capabilities: list[str] | None = None,
    needs_build: bool = False,
    needs_verification: bool = False,
    needs_docs: bool = False,
    reasons: list[str] | None = None,
    plan_strategy: PlanStrategy = "rule_dag",
    staffing_strategy: StaffingStrategy = "suggest_only",
    reflection_policy: ReflectionPolicy = "on_failure",
    constraints: list[str] | None = None,
    success_criteria: list[str] | None = None,
    risk_flags: list[str] | None = None,
    missing_info: list[str] | None = None,
    consent_required_actions: list[str] | None = None,
) -> TeamSpec:
    normalized_lanes = _unique(list(lanes or []))[:6]
    deliverables: list[dict[str, str]] = []
    if needs_build:
        deliverables.append({"type": "code", "description": "可运行、可验证的实现产物"})
    if needs_verification:
        deliverables.append({"type": "test_report", "description": "验证路径、结果和质量结论"})
    if needs_docs:
        deliverables.append({"type": "documentation", "description": "可交接的说明、记录或报告"})
    if not deliverables and intent not in {"chat"}:
        deliverables.append({"type": "answer", "description": "针对用户目标的可检查结论"})
    normalized_risks = _unique(list(risk_flags or []))[:6]
    normalized_missing = _unique(list(missing_info or []))[:6]
    return TeamSpec(
        version=3,
        goal=goal,
        collaboration_mode=collaboration_mode,
        execution_profile={
            "intent": intent,
            "complexity": complexity,
            "deliverable_shape": "unknown",
            "needs_build": bool(needs_build),
            "needs_verification": bool(needs_verification),
            "needs_docs": bool(needs_docs),
            "required_lanes": normalized_lanes,
        },
        team_requirements={
            "roles": _unique(list(roles or []))[:6],
            "workflow_lanes": normalized_lanes,
            "capabilities": normalize_capabilities(capabilities or []),
        },
        planning={
            "strategy": plan_strategy,
            "reflection_policy": reflection_policy,
            "missing_info": normalized_missing,
            "build_plan_mode": "auto",
            "verify_plan_mode": "required",
            "user_review_gate": "on_risk",
        },
        policy={
            "user_team_locked": True,
            "staffing_strategy": staffing_strategy,
            "constraints": _unique(list(constraints or []))[:6],
            "risk_flags": normalized_risks,
            "consent_required_actions": _unique(list(consent_required_actions or []))[:6],
        },
        deliverables=deliverables,
        success_criteria=_unique(list(success_criteria or []))[:6],
        risk_level="high" if normalized_risks else ("medium" if complexity == "multi_role" else "low"),
        uncertainty="high" if normalized_missing else "low",
        planner_notes=_unique(list(reasons or []))[:6],
    )


def build_team_spec(goal: str) -> TeamSpec:
    """Build a deterministic TeamSpec from the user goal.

    The role vocabulary intentionally matches ``crew.team.roles`` presets so it
    can be consumed by both Gateway team suggestion and TeamManager DAG building.
    """
    raw_goal = str(goal or "").strip()
    signal_goal = _signal_goal(raw_goal)
    text = " ".join(signal_goal.split()).lower()
    if not text:
        return TeamSpec(
            version=3,
            goal=raw_goal,
            execution_profile={
                "intent": "question",
                "complexity": "simple",
                "deliverable_shape": "unknown",
                "needs_build": False,
                "needs_verification": False,
                "needs_docs": False,
                "required_lanes": [],
            },
            team_requirements={"roles": [], "workflow_lanes": [], "capabilities": []},
            planning={
                "strategy": "direct",
                "reflection_policy": "none",
                "success_criteria": [],
                "missing_info": ["用户目标为空"],
                "build_plan_mode": "auto",
                "verify_plan_mode": "required",
                "user_review_gate": "on_risk",
            },
            policy={"user_team_locked": True, "staffing_strategy": "suggest_only", "constraints": [], "risk_flags": [], "consent_required_actions": []},
            planner_notes=["用户输入为空，需要先追问目标。"],
        )

    simple_phrases = {
        "你好", "您好", "hello", "hi", "hey", "谢谢", "感谢", "辛苦", "在吗",
        "收到", "好的", "好", "ok", "确认", "继续", "谢谢你", "辛苦了",
    }
    normalized_simple = text.strip(" ，,。.!！?？;；:：~～")
    if normalized_simple in simple_phrases:
        return TeamSpec(
            version=3,
            goal=raw_goal,
            execution_profile={
                "intent": "chat",
                "complexity": "simple",
                "deliverable_shape": "unknown",
                "needs_build": False,
                "needs_verification": False,
                "needs_docs": False,
                "required_lanes": [],
            },
            team_requirements={"roles": [], "workflow_lanes": [], "capabilities": []},
            planning={
                "strategy": "direct",
                "reflection_policy": "none",
                "success_criteria": [],
                "missing_info": [],
                "build_plan_mode": "auto",
                "verify_plan_mode": "required",
                "user_review_gate": "on_risk",
            },
            policy={"user_team_locked": True, "staffing_strategy": "suggest_only", "constraints": [], "risk_flags": [], "consent_required_actions": []},
            planner_notes=["这是轻量聊天，不需要创建团队任务。"],
        )

    roles: list[str] = []
    lanes: list[str] = []
    reasons: list[str] = []
    constraints: list[str] = []
    success_criteria: list[str] = []
    capabilities: list[str] = []
    risk_flags: list[str] = []
    consent_actions: list[str] = []
    needs_build = False
    needs_verification = False
    needs_docs = False
    task_kind: TeamIntent = "mixed"
    testing_intent = _contains_any(text, ("测试", "验证", "验收", "检查", "回归", "质量", "评测", "test", "verify", "qa"))
    explicit_no_build = _contains_any(text, ("不需要开发", "无需开发", "不用开发", "不要开发", "不开发", "不需要开发新功能"))
    historical_build_ref = _contains_any(text, (
        "之前开发", "已经开发", "已开发", "原来开发", "以前开发",
        "开发的", "实现的", "写好的", "做好的", "现有的", "已有的",
    ))
    build_intent = _contains_any(
        text,
        ("开发", "实现", "编码", "写一个", "做一个", "修复", "改造", "新增", "重构", "bug", "工程", "代码", "执行", "跑", "完成", "build", "implement", "fix", "code"),
    )
    collaboration_intent = _contains_any(text, ("组队", "协作", "团队执行", "派活"))
    collaboration_requires_build = collaboration_intent and not testing_intent

    if _contains_any(text, ("产品", "需求", "prd", "用户故事", "验收标准", "范围", "方案", "规划")):
        roles.append("product_manager")
        lanes.append("plan")
        capabilities.extend(["requirements", "analysis"])
        success_criteria.append("需求边界、交付物和验收标准清晰。")
        reasons.append("识别到需求/方案拆解，需要规划角色。")
    if _contains_any(text, ("调研", "研究", "资料", "竞品", "分析")):
        roles.append("research_analyst")
        roles.append("technical_writer")
        lanes.extend(["plan", "docs"])
        capabilities.extend(["information_retrieval", "research", "analysis", "synthesis"])
        success_criteria.append("结论有来源、分析过程可追踪。")
        task_kind = "research"
        reasons.append("识别到研究分析任务，需要资料整理和结论输出。")
    if _contains_any(text, ("ui", "ux", "界面", "交互", "视觉", "像素", "设计")):
        roles.append("ui_designer")
        lanes.append("design")
        capabilities.append("design")
        reasons.append("识别到界面/体验诉求，需要设计约束。")
    if _contains_any(text, ("前端", "react", "vue", "浏览器", "css", "web", "页面")):
        roles.append("frontend_developer")
        lanes.append("build")
        capabilities.extend(["frontend", "implementation"])
        needs_build = True
    if _contains_any(text, ("后端", "接口", "api", "数据库", "服务端", "鉴权", "登录")):
        roles.append("backend_developer")
        lanes.append("build")
        capabilities.extend(["backend", "implementation"])
        needs_build = True
    if build_intent and not (testing_intent and (explicit_no_build or historical_build_ref) and not _contains_any(text, ("修复", "改造", "新增", "实现"))):
        if not any(role in roles for role in ("frontend_developer", "backend_developer")):
            roles.append("fullstack_developer")
        lanes.append("build")
        needs_build = True
        task_kind = "implementation"
        capabilities.append("implementation")
        success_criteria.append("实现产物可运行、可验证，并说明修改范围。")
        reasons.append("识别到实现/修复类任务，需要工程执行。")
    if collaboration_requires_build and not roles:
        roles.append("fullstack_developer")
        lanes.append("build")
        needs_build = True
        task_kind = "implementation"
        capabilities.append("implementation")
        reasons.append("识别到团队执行/派活诉求，默认配置执行角色。")
    if testing_intent:
        roles.append("qa_engineer")
        lanes.append("verify")
        needs_verification = True
        capabilities.extend(["testing", "verification"])
        success_criteria.append("测试路径、失败场景和验收结论明确。")
        if not needs_build:
            task_kind = "testing"
        reasons.append("识别到测试/验证诉求，需要质量验证角色。")
    if _contains_any(text, ("文档", "报告", "总结", "综述", "写作", "说明", "记录", "材料")):
        roles.append("technical_writer")
        lanes.append("docs")
        needs_docs = True
        capabilities.extend(["documentation", "synthesis"])
        success_criteria.append("交付记录、产物引用和风险说明完整。")
        if task_kind == "mixed":
            task_kind = "documentation"
        reasons.append("识别到文档/交付诉求，需要整理输出。")

    if _contains_any(text, ("权限", "隐私", "安全", "生产", "支付", "删除", "迁移", "上线", "发布")):
        risk_flags.append("high_impact_or_security_sensitive")
        consent_actions.append("高影响或安全敏感任务发生补员、改派或执行破坏性操作前需要用户确认。")
    if _contains_any(text, ("不要", "不让", "指定", "必须", "只能", "固定团队", "就用这些人")):
        constraints.append("用户表达了成员或执行方式偏好，团队调整必须先告知并征得确认。")
        consent_actions.append("不得绕过用户指定团队直接换人或补员。")

    detected_capabilities = capabilities_from_text(text)
    if not needs_build:
        detected_capabilities = [item for item in detected_capabilities if item != "implementation"]
    capabilities.extend(detected_capabilities)
    if not roles and set(capabilities) & {"information_retrieval", "research", "analysis", "synthesis"}:
        roles.append("research_analyst")
        lanes.append("plan")
        if set(capabilities) & {"synthesis", "documentation"}:
            roles.append("technical_writer")
            lanes.append("docs")
        task_kind = "research"
        reasons.append("识别到通用检索或分析能力需求，配置研究分析角色。")
    if "review" in capabilities and "independent_reviewer" not in roles:
        roles.append("independent_reviewer")
        lanes.append("verify")

    if not roles:
        roles = ["technical_writer"]
        lanes = ["docs"]
        task_kind = "question"
        reasons.append("未识别到多角色执行目标，优先由 Leader 或整理角色处理。")

    if needs_build and not needs_verification:
        roles.append("qa_engineer")
        lanes.append("verify")
        needs_verification = True
        capabilities.extend(["testing", "verification"])
        reasons.append("开发任务默认补充验证角色，保证可交付。")

    roles = _unique(roles)
    lanes = _unique(lanes)
    if len(roles) <= 1 and not needs_build:
        complexity: TeamComplexity = "focused"
        collaboration_mode = "leader_relay"
    elif len(roles) <= 2:
        complexity = "focused"
        collaboration_mode = "leader_mesh"
    else:
        complexity = "multi_role"
        collaboration_mode = "swarm"

    if risk_flags:
        reflection_policy: ReflectionPolicy = "high_risk"
    elif needs_build or needs_verification:
        reflection_policy = "on_failure"
    else:
        reflection_policy = "before_final"
    plan_strategy: PlanStrategy = "rule_dag"
    if complexity == "multi_role":
        plan_strategy = "llm_dag"
    if task_kind in {"research", "mixed"} and "plan" in lanes:
        plan_strategy = "planning_role_first"
    if task_kind == "question" and not needs_build and not needs_verification and not needs_docs:
        reasons.append("轻量问题不需要完整 Standard DAG，可由 TeamTurnDecision 选择 Fast 协作路径。")
    return _make_team_spec(
        goal=raw_goal,
        intent=task_kind,
        complexity=complexity,
        collaboration_mode=collaboration_mode,
        roles=roles[:6],
        lanes=lanes[:6],
        needs_build=needs_build,
        needs_verification=needs_verification,
        needs_docs=needs_docs,
        reasons=reasons[:6],
        plan_strategy=plan_strategy,
        staffing_strategy="suggest_only",
        reflection_policy=reflection_policy,
        constraints=_unique(constraints)[:6],
        success_criteria=_unique(success_criteria)[:6],
        capabilities=_unique(capabilities),
        risk_flags=_unique(risk_flags)[:6],
        consent_required_actions=_unique(consent_actions)[:6],
    )
