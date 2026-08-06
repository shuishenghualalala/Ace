"""Team 角色定义：默认成员、标准角色目录与 prompt 模板。"""

from __future__ import annotations

from typing import Any

CREW_BUILTIN_AGENT_ID = "crew::builtin"
LEGACY_CREW_BUILTIN_AGENT_ID = "crew"


def is_crew_builtin_agent(agent_id: str) -> bool:
    return str(agent_id or "").strip() == CREW_BUILTIN_AGENT_ID


def is_crew_builtin_display_id(agent_id: str) -> bool:
    return str(agent_id or "").strip() in {CREW_BUILTIN_AGENT_ID, LEGACY_CREW_BUILTIN_AGENT_ID}


def crew_builtin_agent_public() -> dict[str, Any]:
    return {
        "id": CREW_BUILTIN_AGENT_ID,
        "name": "Crew 内置智能体",
        "provider": "crew",
        "display_badge": "M",
        "runtime_id": "",
        "model": "builtin",
        "system_prompt": "",
        "custom_args": [],
        "custom_env": {},
        "created_at": "",
        "updated_at": "",
    }


# 默认团队成员（config.yaml 的 team.members 可覆盖）
DEFAULT_MEMBERS = [
    {"name": "researcher", "role": "负责查资料、读取文件、搜集与整理信息"},
    {"name": "coder", "role": "负责编写代码、执行命令、文件读写等工程操作"},
]


TEAM_ROLE_PRESETS: list[dict[str, Any]] = [
    {
        "key": "tech_lead",
        "label": "技术负责人",
        "description": "负责技术拆解、架构取舍、任务验收和跨成员技术协同。",
        "capabilities": ["planning", "review"],
        "workflow_lane": "lead",
        "scope_key": "technical_leadership",
        "node_label": "技术方案与验收",
        "deliverables": ["技术拆解与架构决策", "风险与依赖说明", "技术验收结论"],
        "collaboration": "向 Leader 提交技术方案、风险和验收意见，协调跨成员技术边界。",
        "keywords": ["leader", "lead", "架构", "技术负责人", "负责人", "拆解", "汇总", "验收"],
    },
    {
        "key": "project_manager",
        "label": "项目经理",
        "description": "负责排期、依赖、风险、状态同步和阶段交付跟踪。",
        "capabilities": ["planning"],
        "workflow_lane": "lead",
        "scope_key": "project_coordination",
        "node_label": "计划与协同",
        "deliverables": ["阶段计划", "依赖与风险清单", "进展与交付状态"],
        "collaboration": "协调成员依赖和阶段交付，向 Leader 汇报风险与进展。",
        "keywords": ["项目", "pm", "排期", "风险", "跟踪", "进度"],
    },
    {
        "key": "product_manager",
        "label": "产品经理",
        "description": "负责需求澄清、范围定义、验收标准和用户价值判断。",
        "capabilities": ["requirements", "analysis"],
        "workflow_lane": "plan",
        "scope_key": "requirements",
        "node_label": "需求与验收",
        "deliverables": ["需求范围", "业务或玩法规则", "验收清单"],
        "collaboration": "向 Leader 提交清晰的需求范围、规则和验收边界。",
        "keywords": ["产品", "需求", "验收标准", "用户价值", "prd", "范围", "方案"],
    },
    {
        "key": "ux_designer",
        "label": "UX 设计师",
        "description": "负责用户路径、信息架构、交互流程和可用性检查。",
        "capabilities": ["design", "analysis"],
        "workflow_lane": "design",
        "scope_key": "user_experience",
        "node_label": "体验与交互设计",
        "deliverables": ["用户路径", "信息架构与交互流程", "可用性检查结论"],
        "collaboration": "向 Leader 和实现成员提交交互约束，并检查实现后的可用性。",
        "keywords": ["ux", "交互", "用户路径", "信息架构", "可用性"],
    },
    {
        "key": "ui_designer",
        "label": "UI 设计师",
        "description": "负责视觉风格、组件状态、布局细节和界面一致性。",
        "capabilities": ["design"],
        "workflow_lane": "design",
        "scope_key": "visual_design",
        "node_label": "视觉与界面设计",
        "deliverables": ["视觉规范", "组件与状态设计", "界面一致性检查"],
        "collaboration": "向 Leader 和实现成员提交视觉约束，并检查界面还原质量。",
        "keywords": ["ui", "视觉", "界面", "组件", "样式", "像素风"],
    },
    {
        "key": "frontend_developer",
        "label": "前端开发",
        "description": "负责前端页面、交互、状态管理、浏览器端集成和视觉还原。",
        "capabilities": ["frontend", "implementation"],
        "workflow_lane": "build",
        "scope_key": "frontend",
        "node_label": "前端实现",
        "deliverables": ["可运行前端代码", "关键交互与状态实现", "前端自检结果"],
        "collaboration": "接收设计和接口约束，向 Leader 提交可运行前端产物和验证结果。",
        "keywords": ["前端", "react", "vue", "页面", "浏览器", "css", "typescript", "ui"],
    },
    {
        "key": "backend_developer",
        "label": "后端开发",
        "description": "负责 API、服务逻辑、数据模型、权限边界和后端集成。",
        "capabilities": ["backend", "implementation"],
        "workflow_lane": "build",
        "scope_key": "backend",
        "node_label": "后端实现",
        "deliverables": ["可运行后端代码", "接口与数据变更说明", "后端自检结果"],
        "collaboration": "接收业务和接口约束，向 Leader 提交可运行后端产物和验证结果。",
        "keywords": ["后端", "api", "接口", "数据库", "服务", "权限"],
    },
    {
        "key": "server_developer",
        "label": "服务端开发",
        "description": "负责服务端运行时、任务调度、并发控制、可靠性和观测日志。",
        "capabilities": ["backend", "implementation"],
        "workflow_lane": "build",
        "scope_key": "server_runtime",
        "node_label": "服务端实现",
        "deliverables": ["服务端实现", "调度与可靠性说明", "运行日志或验证结果"],
        "collaboration": "向 Leader 提交服务端运行产物、可靠性证据和已知风险。",
        "keywords": ["服务端", "runtime", "调度", "并发", "队列", "日志"],
    },
    {
        "key": "desktop_developer",
        "label": "桌面端开发",
        "description": "负责桌面端窗口、系统集成、本地文件、进程和跨平台体验。",
        "capabilities": ["implementation"],
        "workflow_lane": "build",
        "scope_key": "desktop",
        "node_label": "桌面端实现",
        "deliverables": ["可运行桌面端实现", "系统集成说明", "桌面端自检结果"],
        "collaboration": "向 Leader 提交桌面端产物、系统集成结果和平台风险。",
        "keywords": ["桌面", "electron", "mac", "windows", "本地", "窗口"],
    },
    {
        "key": "platform_developer",
        "label": "平台开发",
        "description": "负责平台界面、设备适配、交互和端侧能力接入。",
        "capabilities": ["implementation", "design"],
        "workflow_lane": "build",
        "scope_key": "platform",
        "node_label": "平台实现",
        "deliverables": ["可运行平台实现", "设备适配说明", "端侧自检结果"],
        "collaboration": "向 Leader 提交平台产物、适配结果和端侧风险。",
        "keywords": ["平台", "ios", "android", "app", "触控"],
    },
    {
        "key": "fullstack_developer",
        "label": "全栈开发",
        "description": "负责端到端实现、前后端衔接、基础验证和交付整理。",
        "capabilities": ["frontend", "backend", "implementation"],
        "workflow_lane": "build",
        "scope_key": "fullstack",
        "node_label": "端到端实现",
        "deliverables": ["可运行端到端实现", "集成说明", "基础验证结果"],
        "collaboration": "接收需求和设计约束，向 Leader 提交完整实现与验证结果。",
        "keywords": ["全栈", "开发", "实现", "编码", "代码", "小游戏", "功能"],
    },
    {
        "key": "qa_engineer",
        "label": "测试工程师",
        "description": "负责测试计划、测试路径、用例设计、回归验证、缺陷定位和质量结论。",
        "capabilities": ["testing", "verification"],
        "workflow_lane": "verify",
        "scope_key": "quality_assurance",
        "node_label": "测试与质量验证",
        "deliverables": ["测试方案与用例", "回归验证结果", "缺陷与质量结论"],
        "collaboration": "接收实际产物后独立验证，向 Leader 回传缺陷、影响范围和质量结论。",
        "keywords": ["测试", "qa", "验证", "回归", "缺陷", "质量"],
    },
    {
        "key": "devops_engineer",
        "label": "DevOps 工程师",
        "description": "负责构建、部署、环境配置、CI 检查和发布可靠性。",
        "capabilities": ["implementation", "verification"],
        "workflow_lane": "release",
        "scope_key": "release",
        "node_label": "构建与发布",
        "deliverables": ["构建或部署配置", "环境与发布说明", "发布验证结果"],
        "collaboration": "接收实现产物，向 Leader 提交可复现的构建发布结果和环境风险。",
        "keywords": ["devops", "部署", "ci", "构建", "发布", "环境"],
    },
    {
        "key": "security_engineer",
        "label": "安全工程师",
        "description": "负责权限、输入输出风险、数据安全和安全审查建议。",
        "capabilities": ["review", "verification"],
        "workflow_lane": "verify",
        "scope_key": "security",
        "node_label": "安全审查",
        "deliverables": ["安全检查方案", "权限与数据风险清单", "安全验证结论"],
        "collaboration": "独立检查权限、隐私和异常路径，向 Leader 提交风险与处置建议。",
        "keywords": ["安全", "权限", "隐私", "风控", "审计"],
    },
    {
        "key": "research_analyst",
        "label": "研究分析",
        "description": "负责检索和筛选信息、研究分析、观点比较和证据整理。",
        "capabilities": ["information_retrieval", "research", "analysis"],
        "workflow_lane": "plan",
        "scope_key": "research",
        "node_label": "调研与风险分析",
        "deliverables": ["调研结论", "参考依据", "风险与建议"],
        "collaboration": "按 Leader 给出的范围提交来源、分析结论和不确定性。",
        "keywords": ["检索", "搜索", "调研", "研究", "文献", "资料", "搜集", "分析", "比较", "咨询"],
    },
    {
        "key": "technical_writer",
        "label": "技术文档",
        "description": "负责开发记录、用户说明、测试记录和交付文档整理。",
        "capabilities": ["documentation", "synthesis"],
        "workflow_lane": "docs",
        "scope_key": "documentation",
        "node_label": "文档与交付整理",
        "deliverables": ["开发或使用说明", "测试与风险记录", "可追踪交付文档"],
        "collaboration": "汇集 Leader 和成员结果，整理结构清晰且可追踪的交付材料。",
        "keywords": ["文档", "说明", "记录", "总结", "交付", "写作", "报告", "材料", "整理"],
    },
    {
        "key": "independent_reviewer",
        "label": "独立审阅",
        "description": "负责独立复核关键结论、来源、证据和风险，指出冲突与不确定性。",
        "capabilities": ["review", "verification"],
        "workflow_lane": "verify",
        "scope_key": "independent_review",
        "node_label": "独立审阅",
        "deliverables": ["独立审阅结论", "冲突与遗漏清单", "通过或修订建议"],
        "collaboration": "不替代原成员重做任务，基于已提交结果向 Leader 给出独立判断。",
        "keywords": ["审阅", "复核", "核验", "交叉检查", "证据", "风险", "反方"],
    },
]


def role_preset(role_key: str) -> dict[str, Any]:
    key = str(role_key or "").strip()
    for preset in TEAM_ROLE_PRESETS:
        if preset["key"] == key:
            return dict(preset)
    return dict(TEAM_ROLE_PRESETS[0])


def role_keywords(role_key: str) -> list[str]:
    return [str(item).lower() for item in role_preset(role_key).get("keywords", [])]


def role_capabilities(role_key: str) -> list[str]:
    return [str(item).lower() for item in role_preset(role_key).get("capabilities", [])]


def role_scope_key(role_key: str) -> str:
    preset = role_preset(role_key)
    return str(preset.get("scope_key") or preset.get("key") or "general").strip().lower() or "general"


def role_node_label(role_key: str) -> str:
    preset = role_preset(role_key)
    return str(preset.get("node_label") or preset.get("label") or "任务").strip() or "任务"


def workflow_lane_for_role(role_key: str, default: str = "build") -> str:
    lane = str(role_preset(role_key).get("workflow_lane") or "").strip().lower()
    return lane or default


def _text_matches_any(text: str, words: list[str]) -> bool:
    haystack = str(text or "").lower()
    return any(word and word in haystack for word in words)


def role_matches_text(role_key: str, text: str) -> bool:
    preset = role_preset(role_key)
    signals = [
        str(preset.get("key") or ""),
        str(preset.get("label") or ""),
        *[str(item) for item in preset.get("keywords") or []],
        *[str(item) for item in preset.get("capabilities") or []],
    ]
    return _text_matches_any(text, [item.lower() for item in signals])


def infer_workflow_lane(text: str, default: str = "build") -> str:
    haystack = str(text or "").lower()
    for preset in TEAM_ROLE_PRESETS:
        if role_matches_text(str(preset["key"]), haystack):
            return str(preset.get("workflow_lane") or default).strip().lower() or default
    return default


def role_keys_for_text(
    text: str,
    *,
    default: list[str] | None = None,
    limit: int = 6,
    include_lead: bool = False,
) -> list[str]:
    """Return matching role keys from the shared role catalog.

    This keeps Gateway suggestions, TeamSpec, and TeamPlan generation from
    maintaining separate keyword tables for the same role vocabulary.
    """
    haystack = str(text or "").lower()
    roles: list[str] = []
    for preset in TEAM_ROLE_PRESETS:
        if not include_lead and str(preset.get("workflow_lane") or "") == "lead":
            continue
        role_key = str(preset["key"])
        if role_matches_text(role_key, haystack):
            roles.append(role_key)
    if not roles and default:
        roles = list(default)
    return list(dict.fromkeys(roles))[:limit]


def infer_role_key(text: str, *, is_leader: bool = False) -> str:
    roles = role_keys_for_text(text, limit=1)
    if roles:
        return roles[0]
    return "project_manager" if is_leader else "fullstack_developer"


def role_public_payload(preset: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": preset["key"],
        "label": preset["label"],
        "description": preset["description"],
        "capabilities": list(preset.get("capabilities") or []),
        "workflow_lane": preset.get("workflow_lane") or "build",
    }


def all_role_public_payloads() -> list[dict[str, Any]]:
    return [role_public_payload(preset) for preset in TEAM_ROLE_PRESETS]


def compile_role_responsibility(
    *,
    role_key: str,
    team_goal: str = "",
    assigned_capabilities: list[str] | None = None,
    is_leader: bool = False,
) -> dict[str, Any]:
    """Compile the shared role catalog into FormationPlan responsibility fields."""

    preset = role_preset(role_key)
    goal = str(team_goal or "当前团队目标").strip()
    capabilities = [
        str(item).strip()
        for item in (assigned_capabilities or preset.get("capabilities") or [])
        if str(item).strip()
    ]
    if is_leader:
        mission = f"围绕「{goal}」承担团队目标确认、任务拆解、成员协调、结果验收和最终汇总。"
        deliverables = ["团队目标与边界", "成员协作与验收安排", "最终汇总结论"]
        collaboration = "作为团队控制面驱动协作；成员提交结果、风险或阻塞后决定继续、返工或汇总。"
    else:
        description = str(preset.get("description") or "").strip()
        mission = f"围绕「{goal}」，{description}" if description else f"围绕「{goal}」承担{preset['label']}职责。"
        deliverables = [
            str(item).strip()
            for item in (preset.get("deliverables") or [])
            if str(item).strip()
        ]
        collaboration = str(preset.get("collaboration") or "").strip()
    boundaries = [
        "只承担 FormationPlan 分配的团队常驻职责，不擅自改变用户确认的成员、角色和范围。",
        "遇到阻塞、权限或信息不足时及时向 Leader 说明，不绕过团队治理。",
    ]
    return {
        "mission": mission,
        "boundaries": boundaries,
        "deliverables": deliverables or ["与角色职责匹配的可检查成果", "验证或自检结果", "风险与下一步建议"],
        "collaboration": collaboration or "接收 Leader 派活，提交可验收成果、证据和风险。",
        "_scope_key": "team_lead" if is_leader else role_scope_key(str(preset["key"])),
        "_node_label": "团队规划与验收" if is_leader else role_node_label(str(preset["key"])),
        "_assigned_capabilities": list(dict.fromkeys(capabilities)),
    }


def public_responsibility(responsibility: dict[str, Any]) -> dict[str, Any]:
    """Return the stable persisted FormationPlan responsibility shape."""

    return {
        "mission": str(responsibility.get("mission") or "").strip(),
        "boundaries": [
            str(item).strip()
            for item in (responsibility.get("boundaries") or [])
            if str(item).strip()
        ],
        "deliverables": [
            str(item).strip()
            for item in (responsibility.get("deliverables") or [])
            if str(item).strip()
        ],
        "collaboration": str(responsibility.get("collaboration") or "").strip(),
    }


def responsibility_signature(
    *,
    role_key: str,
    assigned_capabilities: list[str] | None = None,
    responsibility: dict[str, Any] | None = None,
) -> tuple[str, str, tuple[str, ...], tuple[str, ...]]:
    """Build a deterministic, non-persisted signature for exact responsibility overlap."""

    preset = role_preset(role_key)
    compiled = responsibility or compile_role_responsibility(
        role_key=str(preset["key"]),
        assigned_capabilities=assigned_capabilities,
    )
    return (
        str(preset.get("workflow_lane") or "build").strip().lower(),
        str(compiled.get("_scope_key") or role_scope_key(str(preset["key"]))).strip().lower(),
        tuple(sorted(str(item) for item in (assigned_capabilities or compiled.get("_assigned_capabilities") or []))),
        tuple(str(item).strip().lower() for item in (compiled.get("deliverables") or []) if str(item).strip()),
    )


def intelligent_role_markdown(
    *,
    role_key: str,
    agent_name: str = "",
    team_goal: str = "",
    workflow: str = "",
    is_leader: bool = False,
    assigned_capabilities: list[str] | None = None,
    responsibility: dict[str, Any] | None = None,
) -> str:
    preset = role_preset(role_key)
    label = str(preset["label"])
    agent = str(agent_name or "该成员").strip()
    compiled = responsibility or compile_role_responsibility(
        role_key=str(preset["key"]),
        team_goal=str(team_goal or workflow or "当前团队任务"),
        assigned_capabilities=assigned_capabilities,
        is_leader=is_leader,
    )
    stable = public_responsibility(compiled)
    capabilities = "、".join(
        str(item)
        for item in (assigned_capabilities or preset.get("capabilities") or [])
        if str(item)
    )
    lines = [
        f"### {label} - {agent}",
        "",
        "#### 工作原则",
        "- 先确认目标、输入、输出和验收标准，再执行。",
        "- 优先小步交付可验证结果，避免一次性做大而不可检查。",
        "- 遇到失败、阻塞或信息不足时，明确说明原因和建议动作。",
        "",
        "#### 职责",
        f"- {stable['mission']}",
        f"- 重点能力：{capabilities or preset['description']}。",
    ]
    lines.extend(f"- 边界：{item}" for item in stable["boundaries"])
    lines.extend([
        "",
        "#### 团队协作关系",
        f"- {stable['collaboration']}",
        "",
        "#### 输出格式",
    ])
    lines.extend(f"- {item}" for item in stable["deliverables"])
    lines.extend([
        "- 下一负责人：下一步应由 Leader 或具体成员继续。",
        "- 下一动作：明确可执行的下一步。",
        "- 风险/阻塞：缺少的信息、依赖、权限或失败原因。",
        "",
        "#### 工作安排",
        "- 启动：确认本节点目标、依赖、交付物和验收点。",
        "- 执行：按角色完成专业产出，并保留可复核过程。",
        "- 汇总：交付结果、风险和建议后续动作。",
    ])
    return "\n".join(lines)


def leader_prompt(members: list[dict], *, mode: str = "leader_mesh") -> str:
    roster = "\n".join(
        f"  - {m.get('member_id') or m.get('name')}: {m.get('role') or m.get('description') or ''}"
        for m in members
    )
    return (
        "你是一个智能体团队的 Leader（队长）。你不亲自执行具体操作，而是：\n"
        "1. 把用户任务拆解成子任务；\n"
        "2. 对已存在的 TeamPlan 节点，通过 `team_mention(intent=\"assign\", to=[成员], node_id=...)` 委派给最合适的队友；\n"
        "3. 如果当前 DAG 缺少必要工作，不要直接绕过 DAG 派活，先调用 `request_plan_change(change_type=add_node, ...)` 请求新增节点；\n"
        "4. 用 Team Bus 跟踪内部消息、进展、阻塞和产物；\n"
        "5. 收齐所有队友的执行结果后，汇总成给用户的最终答案。\n\n"
        f"你的队友：\n{roster}\n\n"
        f"当前协作模式：{mode}。\n"
        "leader_relay 表示成员主要与 Leader 沟通；leader_mesh 表示成员可通过 Team Bus 互相沟通，"
        "但用户沟通与最终交付仍由 Leader 负责；swarm 表示允许更多内部提议/投票/接力，但仍要遵守治理边界。\n"
        "注意：能委派就委派，不要自己空想结果。最终答案要整合队友的实际产出。\n"
        "TeamPlan 已存在时，DAG 是执行权威；新增任务必须先变更 DAG，再用 mention assign 触发现有节点执行。\n"
        "追问规则：除非继续执行会造成不可逆风险、权限问题，或必须由用户做结构化选择，否则不要向用户追问。"
        "对于开发/实现/继续开发类任务，应采用合理默认范围持续推进到可交付产物；"
        "如果确需用户选择，必须调用 ask_followup_question，并在超时或取消后采用安全默认值继续推进。"
    )


def teammate_prompt(name: str, role: str) -> str:
    return (
        f"你是智能体团队的成员「{name}」，职责：{role}。\n"
        "你会收到 Leader 委派的具体子任务。请专注完成它，必要时调用工具，"
        "然后给出清晰、可直接使用的结果。只做被委派的事，不要越界。\n"
        "如果需要和其他成员沟通或向 Leader 提交结果，只能使用 team_mention；"
        "如需查看自己的团队 mailbox，可使用 team_read_messages；"
        "如果产出了可复用结果，使用 team_add_artifact 登记产物引用。"
    )
