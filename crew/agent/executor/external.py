"""外部 agent 执行器。

把别的 agent 当作 Crew 的执行内核接入，会话管理/记忆/上下文仍留在 Crew，
只委托「执行 agent 这一步」。两种接入形态：

  ClientExecutor —— 开源 agent（如 Hermes）进程内 import 直调其执行层。
                    当前支持一次性 prompt -> text 的最小调用。

  ExternalExecutor —— Runtime-backed 外部 agent（如 Kimi/Codex/Claude）
                     经统一 RuntimeAdapter 接入。协议差异只存在于 Adapter；
                     Executor 负责 Crew 会话、权限、MCP 与事件归一。
"""

from __future__ import annotations

import importlib
import inspect
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from crew.agent.external.cli_adapter import ExternalCliConfig, ExternalCliError, run_external_cli
from crew.agent.external.acp_adapter import (
    AcpAdapterError,
    AcpPermissionRequest,
)
from crew.agent.external.codex_adapter import CodexAdapterError
from crew.agent.external.runtime_adapter import (
    ExternalStreamEvent,
    RuntimeExecutionRequest,
    RuntimeResumeRejected,
    get_runtime_adapter,
)
from crew.agent.external.runtime_profile import canonical_runtime_model_id
from crew.agent.external.runtime_registry import resolve_runtime_adapter_id
from crew.agent.skills import SkillActivation
from crew.agent.executor.base import AgentExecutor, ExecutionContext
from crew.core.envelope import ResponseChunk
from crew.core.followup import drain_followup_answer_messages
from crew.core.runctx import current_owner_account_id
from crew.core.types import Message, ToolCall
from crew.state.home import get_owner_runtime_home
from crew.team.workspace_guard import classify_external_permission, check_workspace_guard


# ---------------------------------------------------------------------------
# 配置数据类
# ---------------------------------------------------------------------------
@dataclass
class ClientExecutorConfig:
    """开源 agent 进程内接入配置。"""

    module: str = ""          # 入口模块路径，如 "hermes.run_agent"
    function: str = ""        # 可选函数名；为空时自动尝试 run_agent/run/execute/main
    external_agent_id: str = ""
    cwd: str = "."
    external_store: Any = None
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExternalExecutorConfig:
    """Runtime-backed 外部 Agent 接入配置。"""

    command: str = ""         # 可执行命令，如 "codex"
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    external_agent_id: str = ""
    model: str = ""          # Session 级覆盖；为空时继承 AgentProfile 默认模型
    cwd: str = "."
    timeout: float = 120.0
    external_store: Any = None
    interaction_bridge: Any = None
    crew_session_id: str = ""  # Team 模式可显式传 member_session_id 作为原生 session 绑定键
    display_session_id: str = ""  # Team 模式下 MCP/followup 应回到父 Team session
    control_session_id: str = ""  # TeamPlan/delegate 等控制面仍绑定当前 Team 运行 session
    persist_runtime_session: bool = True  # 临时委派关闭后不保存原生 session/thread


def _coerce(config: Any, cls: type) -> Any:
    """把 dict / dataclass / None 归一成目标配置类。"""
    if isinstance(config, cls):
        return config
    if isinstance(config, dict):
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in config.items() if k in known})
    return cls()


PayloadMode = Literal["single_agent", "team_chat", "team_relay", "team_execute"]
TeamRole = Literal["none", "leader", "member"]


def _clean_text(value: Any, *, max_chars: int = 1200) -> str:
    text = " ".join(str(value or "").strip().split())
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars].rstrip()}..."


def _multiline(value: Any, *, max_chars: int = 1600) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars].rstrip()}..."


def _agent_label(agent: dict[str, Any], runtime: dict[str, Any]) -> tuple[str, str]:
    provider = str(agent.get("provider") or runtime.get("provider") or "external").strip()
    name = str(agent.get("name") or runtime.get("name") or provider).strip()
    return name or "External Agent", provider or "external"


def _provider_display_name(provider: str) -> str:
    value = str(provider or "external").strip()
    lower = value.lower()
    if lower == "kimi":
        return "Kimi"
    if lower == "claude":
        return "Claude"
    if lower == "codex":
        return "Codex"
    if lower == "hermes":
        return "Hermes"
    return value or "外部智能体"


def _effective_runtime_timeout(
    base_timeout: float,
    task_payload: "_ExternalTaskPayload",
    *,
    has_interaction_binding: bool,
) -> float:
    timeout = max(0.1, float(base_timeout or 120.0))
    if has_interaction_binding or task_payload.mode == "team_execute":
        return max(timeout, 330.0)
    return timeout


@dataclass(frozen=True)
class _ExternalTaskPayload:
    """Structured task contract for Runtime-backed external agents."""

    mode: PayloadMode
    team_role: TeamRole
    agent_name: str
    provider: str
    model: str
    request: str
    team_goal: str = ""
    member_id: str = ""
    current_node_id: str = ""
    current_node_title: str = ""
    current_node_detail: str = ""
    upstream_summary: str = ""
    upstream_artifacts: str = ""
    collaboration_mode: str = ""
    output_contract: str = ""
    budget: str = "focused"

    def render_prompt(self) -> str:
        if self.mode != "single_agent":
            lines = [
                "# Crew External Task Payload",
                f"- team_role: {self.team_role}",
                f"- agent: {self.agent_name}",
                f"- provider: {self.provider}",
                f"- model: {self.model or 'unknown'}",
                f"- member_id: {self.member_id or 'unknown'}",
                f"- collaboration_mode: {self.collaboration_mode or 'team'}",
                f"- budget: {self.budget}",
                "",
                "## Team Goal",
                self.team_goal or self.request,
                "",
                "## Current Message",
                self.request,
                "",
                "## Team Context Summary",
                self.upstream_summary or "无额外团队上下文摘要。",
                "",
                "## Upstream Artifacts",
                self.upstream_artifacts or "无上游产物引用。",
                "",
                "## Scope",
                *self._team_scope_lines(),
                "",
                "## Output Contract",
                self.output_contract or self._default_team_output_contract(),
            ]
            if self.mode == "team_execute":
                node_lines = [
                    "",
                    "## Current Execution Node",
                    f"- node_id: {self.current_node_id or 'none'}",
                    f"- title: {self.current_node_title or '当前团队任务'}",
                    self.current_node_detail or self.request,
                ]
                lines[14:14] = node_lines
            return "\n".join(lines).strip()

        lines = [
            "# Crew External Task Payload",
            "- team_role: none",
            f"- agent: {self.agent_name}",
            f"- provider: {self.provider}",
            f"- model: {self.model or 'unknown'}",
            f"- budget: {self.budget}",
            "",
            "## User Request",
            self.request,
            "",
            "## Scope",
            "- 直接回答当前用户请求。",
            "- 保持上下文连续，但不要复述无关历史。",
            "- 不要输出内部推理、工具 JSON 或无关长篇说明。",
            "",
            "## Output Contract",
            self.output_contract or "用简洁中文回答；需要执行步骤时只列关键步骤、结论和必要风险。",
        ]
        return "\n".join(lines).strip()

    def _team_scope_lines(self) -> list[str]:
        continuity_scope = [
            "- 以当前用户最新消息和 Current Message/Current Execution Node 为本轮任务目标。",
            "- 保持上下文连续；历史上下文只用于理解指代、延续和举例，不能替代本轮任务。",
        ]
        if self.mode == "team_chat":
            if self.team_role == "leader":
                return [
                    *continuity_scope,
                    "- 代表当前团队直接回答用户。",
                    "- 不派活，不创建 TeamPlan，不假装成员已经回复。",
                    "- 可以使用团队成员和历史上下文，但不要展开系统内部实现。",
                ]
            return [
                *continuity_scope,
                "- 你是在 Team 会话中被用户直接 @ 到的成员。",
                "- 以成员身份回答当前消息；不能创建、修改或重排 TeamPlan。",
                "- 如认为计划需要调整，只能使用 team_mention 向 Leader 提出建议。",
                "- 回复应进入团队群聊语境，简洁清楚。",
            ]
        if self.mode == "team_relay":
            if self.team_role == "leader":
                return [
                    *continuity_scope,
                    "- 这是轻协作，不是正式执行模式。",
                    "- 最多征询目标成员一次意见，再汇总给用户。",
                    "- 不创建 TeamPlan，不扩展为多成员工作流。",
                ]
            return [
                *continuity_scope,
                "- Leader 正在征询你一个轻量意见。",
                "- 只回答被征询的问题，短答，不扩展为完整方案。",
                "- 不主动派活；不能创建、修改或重排 TeamPlan。",
                "- 如认为计划需要调整，只能使用 team_mention 向 Leader 提出建议。",
            ]
        if self.team_role == "leader":
            return [
                *continuity_scope,
                "- 当前进入团队执行模式。",
                "- 拆解、推进和汇总必须与 Crew 控制面一致。",
                "- 如果涉及派活或计划状态，必须通过可用 MCP/控制面真实执行；不要在文本中伪造派活或成员结果。",
            ]
        return [
            *continuity_scope,
            "- 只完成 Current Execution Node，不扩展为完整项目计划。",
            "- 不能创建、修改或重排 TeamPlan；如认为计划需要调整，只能使用 team_mention 向 Leader 提出建议。",
            "- 不能直接询问用户；如需要用户补充信息，使用 team_mention 向 Leader 说明阻塞和建议追问内容。",
            "- 完成后使用 team_mention @leader 提交结果、风险、阻塞和产物引用。",
            "- 如果需要协作，只使用已提供的 Crew Team MCP 工具；不要在文本里假装已调用成员。",
            "- 不要输出内部推理、完整历史、工具 JSON 或无关长篇说明。",
        ]

    def _default_team_output_contract(self) -> str:
        if self.mode == "team_chat":
            return "用简洁中文回答用户；如涉及团队成员或历史状态，基于团队上下文说明。"
        if self.mode == "team_relay":
            return "输出轻量意见或汇总结论；如果不确定，明确需要补充的信息。"
        if self.team_role == "leader":
            return "输出执行模式下的推进结果、已派发/已完成事项、风险和下一步；不要伪造控制面状态。"
        return "用简洁中文给出执行结果、关键依据、风险/阻塞；如果产出失败，明确原因和下一步。"


def _build_external_task_payload(
    ctx: ExecutionContext,
    agent: dict[str, Any],
    runtime: dict[str, Any],
    prompt: str,
    model: str = "",
) -> _ExternalTaskPayload:
    params = dict(ctx.params or {})
    agent_name, provider = _agent_label(agent, runtime)
    team_session_id = str(params.get("team_session_id") or "").strip()
    is_team = bool(team_session_id)
    if is_team:
        mode = _team_payload_mode(params)
        team_role = _team_payload_role(params)
        return _ExternalTaskPayload(
            mode=mode,
            team_role=team_role,
            agent_name=agent_name,
            provider=provider,
            model=str(model or agent.get("model") or "").strip(),
            request=_multiline(prompt),
            team_goal=_multiline(params.get("team_goal") or params.get("query") or prompt, max_chars=1000),
            member_id=_clean_text(params.get("agent_id") or params.get("team_member_id"), max_chars=80),
            current_node_id=_clean_text(params.get("team_plan_node_id"), max_chars=80),
            current_node_title=_clean_text(params.get("team_node_title") or _first_line(prompt), max_chars=180),
            current_node_detail=_multiline(params.get("team_node_detail") or prompt, max_chars=1400),
            upstream_summary=_multiline(params.get("team_upstream_summary"), max_chars=4000),
            upstream_artifacts=_multiline(params.get("team_upstream_artifacts"), max_chars=2000),
            collaboration_mode=_clean_text(params.get("team_collaboration_mode"), max_chars=80),
            output_contract=_multiline(params.get("external_output_contract"), max_chars=700),
            budget=_clean_text(params.get("external_task_budget") or "focused", max_chars=40),
        )
    return _ExternalTaskPayload(
        mode="single_agent",
        team_role="none",
        agent_name=agent_name,
        provider=provider,
        model=str(model or agent.get("model") or "").strip(),
        request=_multiline(prompt, max_chars=3000),
        output_contract=_multiline(params.get("external_output_contract"), max_chars=700),
        budget=_clean_text(params.get("external_task_budget") or "focused", max_chars=40),
    )


def _build_structured_external_system_prompt(
    agent: dict[str, Any],
    runtime: dict[str, Any],
    *,
    mode: PayloadMode,
    team_role: TeamRole = "none",
    reset_memory: str = "",
    active_skills: tuple[SkillActivation, ...] = (),
) -> str:
    name, provider = _agent_label(agent, runtime)
    custom = _multiline(agent.get("system_prompt"), max_chars=1200)
    lines = [
        f"你是 Crew 通过本地 Runtime 连接的外部智能体「{name}」，provider={provider}。",
        "请严格按下方 Crew External Task Payload 执行，优先完成当前任务，不展开系统内部实现。",
    ]
    if mode != "single_agent":
        lines.extend([
            f"你处在 Crew Team 会话中，你的团队身份是 {team_role}。",
            "严格遵守 payload 中的 Scope；不要把澄清、说明或聊天自动扩展为执行计划。",
            "输出应适合在团队群聊中实时展示，避免长篇背景铺垫。",
        ])
        if team_role == "member":
            lines.extend([
                "成员不能创建、修改或重排 TeamPlan，不能委派其他成员，不能直接向用户发起 follow-up。",
                "如果需要用户补充信息、计划调整或改派，只能使用 team_mention 向 Leader 提出建议。",
            ])
        else:
            lines.extend([
                "如果必须向用户做结构化追问，只能调用工具列表中的 Crew Interaction MCP 工具 "
                "`crew_interaction.ask_followup_question`、`mcp__crew-interaction__ask_followup_question`"
                "（部分 runtime 显示为 `mcp_crew_interaction_ask_followup_question`）。"
                "禁止调用 runtime 内置 `AskUserQuestion`："
                "它不会映射到 Crew 前端，只会在 ACP 中返回 dismissed。若 Crew MCP 工具不存在，改用一句话提问；不要伪造用户选择。",
                "若涉及派活或 TeamPlan 状态变更，必须通过可用 Crew 控制面/MCP 真实执行；不要伪造派活、伪造成员回复或调用内部 delegate_to_teammate。",
            ])
    else:
        lines.extend([
            "这是单外部智能体会话：直接回答当前用户任务。",
            "如用户要求多智能体协作，简洁说明需要切换到 Crew 团队模式。",
            "需要用户补充关键信息时，只能调用工具列表中的 Crew Interaction MCP 工具 "
            "`crew_interaction.ask_followup_question`、`mcp__crew-interaction__ask_followup_question`"
            "（部分 runtime 显示为 `mcp_crew_interaction_ask_followup_question`）。"
            "禁止调用 runtime 内置 `AskUserQuestion`："
            "它不会映射到 Crew 前端，只会在 ACP 中返回 dismissed。若 Crew MCP 工具不存在，改用一句话提问。",
        ])
    if custom:
        lines.extend(["", "# 自定义智能体指令", custom])
    if active_skills:
        lines.extend([
            "",
            "# 当前轮激活的 Crew Skill",
            "以下 Skill 由用户在 Composer 中为当前轮显式激活；只能使用这里列出的 Skill。",
            "Skill 资源目录默认只读，产物请写入当前 Session 工作目录。",
        ])
        for skill in active_skills:
            lines.extend([
                "",
                f"## {skill.name} (`{skill.skill_id}`)",
                f"Skill 根目录：{skill.skill_root}",
                skill.instruction,
            ])
            if skill.entrypoints:
                lines.extend([
                    "",
                    "这是普通 Skill：使用当前 Runtime 自带的文件与 terminal 工具原生执行；"
                    "不要寻找 Crew 通用 Skill runner。",
                    "Skill 指令中的相对路径必须以该 Skill 根目录解析；"
                    "执行脚本时使用 Skill 根下的确定路径，不要把 `./scripts` 误当成 Session 工作目录。",
                    "Skill 根目录只读，产物写入当前 Session 工作目录；"
                    "写入其他目录必须通过 Crew 权限弹窗。",
                    "声明入口：" + "、".join(
                        f"{item.id}={item.path}" for item in skill.entrypoints
                    ),
                ])
            else:
                lines.append(
                    "这是指令型普通 Skill：由当前 Runtime 按上述指令原生执行。"
                )
    if reset_memory:
        lines.extend(["", reset_memory.strip()])
    return "\n".join(lines).strip()


def _team_payload_mode(params: dict[str, Any]) -> PayloadMode:
    raw = str(
        params.get("team_interaction_mode")
        or params.get("team_prompt_mode")
        or ""
    ).strip()
    if raw in {"team_chat", "team_relay", "team_execute"}:
        return raw  # type: ignore[return-value]
    if str(params.get("team_plan_node_id") or "").strip():
        return "team_execute"
    return "team_chat"


def _team_payload_role(params: dict[str, Any]) -> TeamRole:
    raw = str(params.get("external_team_role") or params.get("team_role") or "").strip()
    if raw in {"leader", "member"}:
        return raw  # type: ignore[return-value]
    member_id = str(params.get("agent_id") or params.get("team_member_id") or "").strip()
    return "leader" if member_id == "leader" else "member"


def _first_line(text: str) -> str:
    for line in str(text or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _looks_like_missing_followup_tool(output: str) -> bool:
    text = " ".join(str(output or "").strip().lower().split())
    if not text:
        return False
    if "ask_followup_question" not in text and "mcp_crew_interaction_ask_followup_question" not in text:
        return False
    missing_markers = (
        "没有",
        "不可用",
        "无法调用",
        "not available",
        "no tool",
        "tool is not available",
        "工具列表里没有",
    )
    return any(marker in text for marker in missing_markers)


def _followup_mcp_diagnostic(provider: str) -> str:
    provider_label = str(provider or "external").strip() or "external"
    return (
        "Crew Interaction MCP 工具未注册成功：当前外部 runtime "
        f"provider={provider_label!r} 没有暴露 `ask_followup_question` 或等价的 MCP 前缀工具"
        "（例如 `mcp_crew_interaction_ask_followup_question`），因此无法弹出选择框。"
        "请检查该外部 runtime 的 MCP 配置、RuntimeAdapter 的工具注入参数、"
        "以及 `crew-interaction` MCP server 是否成功启动并完成工具发现。"
    )


def _followup_cli_diagnostic(provider: str) -> str:
    provider_label = str(provider or "external").strip() or "external"
    return (
        "当前 Crew 通过 CLI runtime 运行 "
        f"provider={provider_label!r}；这条接入路径不会注入 Crew Interaction MCP，"
        "因此没有 `ask_followup_question`，也无法弹出选择框。"
        "这不是因为会话处于 Plan 模式，切换 Plan/Code 模式也不会让当前 CLI runtime 获得该工具。"
        "如需确认信息，只能在对话中用自然语言提问。"
    )


def _external_system_prompt(
    agent: dict[str, Any],
    runtime: dict[str, Any],
    model: str = "",
) -> str:
    """Compatibility prompt for legacy CLI/client runtimes."""
    provider = str(agent.get("provider") or runtime.get("provider") or "external").strip()
    name = str(agent.get("name") or runtime.get("name") or provider).strip()
    effective_model = str(model or agent.get("model") or "unknown").strip() or "unknown"
    custom = str(agent.get("system_prompt") or "").strip()
    base = (
        f"你是 Crew 当前连接的单个外部智能体「{name}」。\n"
        f"本轮运行时 provider={provider}，实际请求模型 model={effective_model}；"
        "用户询问模型时必须以这两个值为准。\n"
        "直接回答用户当前任务，不要描述 Crew 内部调度方式。\n"
        "不要展开解释系统内部实现，也不要列举当前对话外的能力清单。\n"
        "\n"
        "## 用户确认与追问\n"
        "如果需要用户补充信息或在多个选项中选择，请用自然语言简洁提问；"
        "不要假装已经获得用户答案，也不要替用户选择。"
    )
    if str(runtime.get("protocol") or "").strip().lower() == "cli":
        base += (
            "\n当前 Crew 使用 CLI runtime 接入你；这条路径不会注入 Crew Interaction MCP，"
            "因此你没有 `ask_followup_question`，也不能弹出选择框。"
            "如果用户询问或要求使用该工具，必须明确说明这是当前 CLI runtime 的接入限制；"
            "不要声称原因是会话处于 Plan 模式，也不要声称切换 Plan/Code 模式可以获得该工具。"
        )
    return f"{base}\n\n# 自定义智能体指令\n{custom}" if custom else base


def _display_session_id(session_id: str) -> str:
    return session_id.split("::", 1)[0]


def _attachment_readable_files(
    attachments: list[dict[str, Any]] | None,
    *,
    attachment_root: str,
) -> list[str]:
    """Return exact current-turn upload paths without trusting client-supplied locations."""
    try:
        trusted_root = Path(attachment_root).expanduser().resolve()
    except Exception:  # noqa: BLE001 - invalid roots disable automatic attachment access
        return []
    files: list[str] = []
    seen: set[str] = set()
    for attachment in attachments or []:
        if not isinstance(attachment, dict):
            continue
        raw = str(attachment.get("path") or "").strip()
        if not raw:
            continue
        try:
            path = Path(raw).expanduser().resolve()
            path.relative_to(trusted_root)
        except Exception:  # noqa: BLE001 - malformed attachment paths are ignored
            continue
        if not path.is_file():
            continue
        resolved = str(path)
        if resolved in seen:
            continue
        seen.add(resolved)
        files.append(resolved)
    return files


def _permission_guard(
    params: dict[str, Any],
    *,
    cwd: str,
    attachments: list[dict[str, Any]] | None = None,
    attachment_root: str = "",
    active_skills: tuple[SkillActivation, ...] = (),
) -> dict[str, Any]:
    readable_files = _attachment_readable_files(
        attachments,
        attachment_root=attachment_root,
    )
    referenced_files: list[str] = []
    referenced_roots: list[str] = []
    for item in params.get("referenced_paths") or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        if item.get("resource_type") == "directory":
            referenced_roots.append(path)
        else:
            referenced_files.append(path)
    configured = params.get("workspace_guard")
    if isinstance(configured, dict) and configured.get("enabled"):
        guard = dict(configured)
        readable_roots = [
            str(path)
            for path in guard.get("readable_roots", [])
            if str(path or "").strip()
        ]
        readable_roots.extend(
            skill.skill_root for skill in active_skills if skill.skill_root
        )
        readable_roots.extend(referenced_roots)
        guard["readable_roots"] = list(dict.fromkeys(readable_roots))
        guard["allowed_roots"] = list(dict.fromkeys([
            *(
                str(path)
                for path in guard.get("allowed_roots", [])
                if str(path or "").strip()
            ),
            *guard["readable_roots"],
        ]))
        guard["readable_files"] = list(dict.fromkeys([
            *(
                str(path)
                for path in guard.get("readable_files", [])
                if str(path or "").strip()
            ),
            *readable_files,
            *referenced_files,
        ]))
        guard["confirm_write_files"] = list(dict.fromkeys([
            *(str(path) for path in guard.get("confirm_write_files", [])),
            *referenced_files,
        ]))
        guard["confirm_write_roots"] = list(dict.fromkeys([
            *(str(path) for path in guard.get("confirm_write_roots", [])),
            *referenced_roots,
        ]))
        return guard
    readable_roots = [
        cwd,
        *(skill.skill_root for skill in active_skills if skill.skill_root),
        *referenced_roots,
    ]
    guard = {
        "enabled": True,
        "root": cwd,
        "readable_roots": list(dict.fromkeys(readable_roots)),
        "readable_files": list(dict.fromkeys([*readable_files, *referenced_files])),
        "writable_roots": [cwd],
        "allowed_roots": list(dict.fromkeys(readable_roots)),
        "confirm_write_files": referenced_files,
        "confirm_write_roots": referenced_roots,
    }
    workspace_root = str(params.get("workspace_root_path") or "").strip()
    if workspace_root:
        try:
            if Path(workspace_root).expanduser().resolve() == Path(cwd).expanduser().resolve():
                guard["confirm_delete_roots"] = [cwd]
        except Exception:  # noqa: BLE001 - invalid workspace roots grant nothing
            pass
    return guard


def _permission_question(
    *,
    reason: str,
    tool_name: str,
    target: str,
    operation: str,
    agent_name: str,
    member_id: str,
    node_id: str,
) -> str:
    normalized = str(tool_name or "").strip().lower()
    if operation == "read":
        action = "读取文件"
    elif operation == "write":
        action = "写入或修改文件"
    elif operation == "network":
        action = "执行外部其他操作"
    elif operation == "execute" or normalized == "terminal":
        action = "执行命令"
    elif normalized in {"file_write", "patch"}:
        action = "写入或修改文件"
    elif normalized in {"file_read", "search_files", "file_search"}:
        action = "读取文件"
    else:
        action = "执行外部其他操作"
    details = [f"即将执行：{action}"]
    if target:
        details.append(f"目标：{target}")
    if reason:
        details.extend(["", f"原因：{reason}"])
    details.append(f"智能体：{agent_name}")
    if member_id:
        details.append(f"成员：{member_id}")
    if node_id:
        details.append(f"节点：{node_id}")
    return "\n".join(details)


def _summarize_messages_for_runtime_reset(
    messages: list[Message],
    *,
    max_messages: int = 8,
    max_chars: int = 2400,
) -> str:
    """Build compact Crew memory for a fresh native session after resume fails."""
    rows: list[str] = []
    for msg in messages:
        text = (msg.text_content or "").strip()
        if not text:
            continue
        if msg.is_meta and "system-reminder" not in text:
            continue
        label = msg.role
        if msg.name:
            label = f"{label}:{msg.name}"
        if msg.tool_call_id:
            label = f"{label}:{msg.tool_call_id}"
        text = " ".join(text.split())
        if len(text) > 360:
            text = f"{text[:360]}..."
        rows.append(f"- {label}: {text}")
    if not rows:
        return ""
    summary = "\n".join(rows[-max_messages:])
    if len(summary) > max_chars:
        summary = summary[-max_chars:]
    return (
        "# Crew 侧连续上下文\n"
        "上一条外部 Runtime 原生会话无法安全续接，本轮已新建原生 session。"
        "下面是 Crew 保存的同一成员/团队上下文摘要，请据此延续任务，不要要求用户重复说明。\n"
        f"{summary}"
    )


async def _stream_runtime_with_safe_resume(
    adapter: Any,
    request: RuntimeExecutionRequest,
    *,
    reset_memory: str,
) -> AsyncIterator[ExternalStreamEvent]:
    """Retry once without native resume only before current-turn observable work."""

    emitted_work = False
    try:
        async for event in adapter.stream(request):
            if event.kind not in {"session", "usage"}:
                emitted_work = True
            yield event
        return
    except RuntimeResumeRejected:
        if not request.resume_session_id or emitted_work:
            raise

    system_prompt = request.system_prompt
    if reset_memory:
        system_prompt = "\n\n".join(
            part for part in [system_prompt.strip(), reset_memory.strip()] if part
        )
    fresh = replace(
        request,
        resume_session_id="",
        system_prompt=system_prompt,
    )
    async for event in adapter.stream(fresh):
        if event.kind == "session":
            yield replace(
                event,
                session_resumed=False,
                session_reset=True,
            )
        else:
            yield event


def _module_and_function(module: str, function: str = "") -> tuple[str, str]:
    module = module.strip()
    function = function.strip()
    if not module:
        return "", function
    if ":" in module:
        mod, func = module.rsplit(":", 1)
        return mod.strip(), function or func.strip()
    return module, function


async def _run_client_prompt(prompt: str, config: ClientExecutorConfig, ctx: ExecutionContext) -> str:
    module_name, function_name = _module_and_function(config.module, config.function)
    if not module_name and config.external_store is not None and config.external_agent_id:
        agent, runtime = config.external_store.agent_with_runtime(
            config.external_agent_id,
            owner_account_id=current_owner_account_id.get(),
        )
        module_name = str(runtime.get("executable_path") or "").strip()
        function_name = function_name or str(runtime.get("metadata", {}).get("function") or "").strip()
        prompt = f"{_external_system_prompt(agent, runtime)}\n\n---\n\n{prompt}"

    if not module_name:
        raise NotImplementedError("ClientExecutor 缺少 module 配置，无法调用 Hermes client")

    module = importlib.import_module(module_name)
    candidates = [function_name] if function_name else ["run_agent", "run", "execute", "main"]
    fn = None
    for name in candidates:
        if name and hasattr(module, name):
            fn = getattr(module, name)
            break
    if fn is None:
        raise NotImplementedError(f"ClientExecutor 在 {module_name} 中找不到可调用入口")

    kwargs = {
        "prompt": prompt,
        "system_prompt": ctx.system_prompt,
        "messages": ctx.messages,
        "cwd": ctx.cwd or config.cwd or ".",
        "options": config.options,
    }
    try:
        sig = inspect.signature(fn)
        has_varkw = any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values())
        accepted = {
            name: value
            for name, value in kwargs.items()
            if has_varkw or name in sig.parameters
        }
        result = fn(**accepted)
    except (TypeError, ValueError):
        result = fn(prompt)
    if inspect.isawaitable(result):
        result = await result
    return _stringify_client_result(result)


def _stringify_client_result(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for key in ("text", "output", "content", "result", "message"):
            value = result.get(key)
            if isinstance(value, str) and value:
                return value
    return str(result)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
class ClientExecutor(AgentExecutor):
    """开源 agent 进程内 import 直调。"""

    name = "client"

    def __init__(self, config: Any = None) -> None:
        self.config = _coerce(config, ClientExecutorConfig)

    async def execute(self, ctx: ExecutionContext) -> AsyncIterator[ResponseChunk]:
        try:
            output = await _run_client_prompt(ctx.query, self.config, ctx)
        except NotImplementedError as exc:
            raise exc
        except Exception as exc:  # noqa: BLE001 - 外部 client 失败需要转成对话错误帧
            yield ResponseChunk.error(ctx.request_id, f"ClientExecutor 调用失败：{exc}")
            return
        output = output or "ClientExecutor 已完成，但没有返回文本输出。"
        ctx.messages.append(Message.assistant(output))
        yield ResponseChunk.delta(ctx.request_id, output)
        yield ResponseChunk.final(ctx.request_id, output)


class ExternalExecutor(AgentExecutor):
    """Runtime-backed 外部 Agent 的协议中性执行入口。"""

    name = "external"

    def __init__(self, config: Any = None) -> None:
        self.config = _coerce(config, ExternalExecutorConfig)

    async def execute(self, ctx: ExecutionContext) -> AsyncIterator[ResponseChunk]:
        if not self.config.external_agent_id:
            yield ResponseChunk.error(ctx.request_id, "ExternalExecutor 缺少 external_agent_id")
            return
        if self.config.external_store is None:
            yield ResponseChunk.error(ctx.request_id, "ExternalExecutor 缺少 ExternalAgentStore")
            return

        owner_account_id = current_owner_account_id.get()
        try:
            agent, runtime = self.config.external_store.agent_with_runtime(
                self.config.external_agent_id,
                owner_account_id=owner_account_id,
            )
        except KeyError:
            yield ResponseChunk.error(ctx.request_id, f"外部智能体不存在: {self.config.external_agent_id}")
            return

        provider = str(agent.get("provider") or runtime.get("provider") or "").lower()
        effective_model = canonical_runtime_model_id(
            runtime,
            str(self.config.model or agent.get("model") or "").strip(),
        )
        prompt = ctx.query
        protocol = str(runtime.get("protocol") or "").lower()
        runtime_metadata = runtime.get("metadata") if isinstance(runtime.get("metadata"), dict) else {}
        adapter_id = resolve_runtime_adapter_id(
            provider=provider,
            protocol=protocol,
            metadata=runtime_metadata,
        )
        structured_adapter = bool(adapter_id)
        seq = 0
        runtime_binding_session_id = self.config.crew_session_id or _display_session_id(ctx.session_id)
        display_session_id = self.config.display_session_id or _display_session_id(ctx.session_id)
        control_session_id = self.config.control_session_id or display_session_id

        def next_seq() -> int:
            nonlocal seq
            seq += 1
            return seq

        def append_pending_followup_answers() -> None:
            for content in drain_followup_answer_messages(display_session_id):
                ctx.messages.append(Message.user(content))

        runtime_failure_binding_key: dict[str, str] | None = None
        runtime_failure_session_id = ""
        thinking_parts: list[str] = []
        persisted_tools: dict[str, ToolCall] = {}
        usage: dict[str, int] = {}
        try:
            if structured_adapter:
                parts: list[str] = []
                bridge = self.config.interaction_bridge
                cwd = str(ctx.cwd or self.config.cwd or ".")
                runtime_id = str(runtime.get("id") or agent.get("runtime_id") or "")
                resume_session_id = ""
                binding_key = {
                    "owner_account_id": owner_account_id,
                    "crew_session_id": runtime_binding_session_id,
                    "external_agent_id": self.config.external_agent_id,
                    "runtime_id": runtime_id,
                    "adapter_id": adapter_id,
                    "cwd": cwd,
                }
                runtime_failure_binding_key = (
                    binding_key if self.config.persist_runtime_session else None
                )
                binding = (
                    self.config.external_store.get_runtime_session_binding(**binding_key)
                    if self.config.persist_runtime_session
                    else None
                )
                dynamic_control_capable = (
                    bridge is not None
                    and adapter_id == "codex-app-server"
                    and callable(getattr(bridge, "dynamic_tool_specs", None))
                    and callable(getattr(bridge, "invoke_tool_json", None))
                )
                session_profile = (
                    # v2：dynamicTools 改平铺 function 项（codex 新版拒绝 namespace 包装），
                    # 旧的 namespace 会话绑定必须作废重开。
                    "codex-app-server:crew-dynamic-tools-v2"
                    if dynamic_control_capable
                    else ""
                )
                skipped_unsafe_binding = False
                incompatible_binding = bool(
                    binding
                    and session_profile
                    and str(binding.get("session_profile") or "") != session_profile
                )
                if (
                    binding
                    and str(binding.get("status") or "") != "unsafe_failed"
                    and not incompatible_binding
                ):
                    resume_session_id = str(binding.get("native_session_id") or "")
                    runtime_failure_session_id = resume_session_id
                elif binding:
                    skipped_unsafe_binding = True

                task_payload = _build_external_task_payload(
                    ctx,
                    agent,
                    runtime,
                    prompt,
                    model=effective_model,
                )
                binding_ttl = max(self.config.timeout, 330.0) + 30
                context_type = "standalone" if task_payload.mode == "single_agent" else "team"
                interaction_binding = bridge.create_binding(
                    owner_account_id=owner_account_id,
                    display_session_id=display_session_id,
                    control_session_id=control_session_id,
                    origin_session_id=ctx.session_id,
                    agent_name=str(agent.get("name") or provider or "External Agent"),
                    ttl_seconds=binding_ttl,
                    context_type=context_type,
                    team_session_id=(
                        str(ctx.params.get("team_session_id") or control_session_id)
                        if context_type == "team"
                        else ""
                    ),
                    member_id=task_payload.member_id if context_type == "team" else "",
                    team_role=task_payload.team_role if context_type == "team" else "",
                    cwd=cwd,
                    active_skills=ctx.active_skills,
                ) if bridge is not None else None
                interactive_timeout = _effective_runtime_timeout(
                    self.config.timeout,
                    task_payload,
                    has_interaction_binding=interaction_binding is not None,
                )
                permission_params = dict(ctx.params or {})
                permission_guard = _permission_guard(
                    permission_params,
                    cwd=cwd,
                    attachments=ctx.attachments,
                    attachment_root=str(get_owner_runtime_home(owner_account_id) / "uploads"),
                    active_skills=ctx.active_skills,
                )
                agent_display_name = str(agent.get("name") or provider or "External Agent").strip()
                team_display_name = str(permission_params.get("team_display_name") or "").strip()
                is_team_permission = task_payload.team_role in {"leader", "member"}
                permission_display_name = (
                    (team_display_name or "团队") if is_team_permission else agent_display_name
                )

                async def _handle_permission(request: AcpPermissionRequest) -> Literal["allow", "deny"]:
                    decision = classify_external_permission(
                        request.tool_call,
                        permission_guard,
                        cwd=cwd,
                    )
                    if decision.action == "allow":
                        return "allow"
                    if decision.action == "deny" or interaction_binding is None or bridge is None:
                        return "deny"
                    approved = await bridge.ask_permission(
                        interaction_binding.token,
                        title="操作权限确认",
                        question=_permission_question(
                            reason=decision.reason,
                            tool_name=decision.tool_name,
                            target=decision.target,
                            operation=decision.operation,
                            agent_name=agent_display_name,
                            member_id=task_payload.member_id if is_team_permission else "",
                            node_id=(
                                str(permission_params.get("team_plan_node_id") or "")
                                if is_team_permission
                                else ""
                            ),
                        ),
                        display_name=permission_display_name,
                        origin_type="team_control" if is_team_permission else "acp_permission",
                    )
                    return "allow" if approved else "deny"

                native_session_id = ""
                native_session_reset = False
                try:
                    use_dynamic_control_tools = (
                        interaction_binding is not None
                        and dynamic_control_capable
                    )
                    mcp_servers = (
                        []
                        if use_dynamic_control_tools
                        else (
                            [bridge.mcp_server_config(interaction_binding)]
                            if interaction_binding is not None
                            else []
                        )
                    )
                    dynamic_tools = (
                        bridge.dynamic_tool_specs(interaction_binding)
                        if use_dynamic_control_tools
                        else []
                    )
                    dynamic_tool_handler = None
                    if use_dynamic_control_tools:
                        async def dynamic_tool_handler(
                            tool_name: str,
                            arguments: dict[str, Any],
                            *,
                            namespace: str = "",
                        ) -> str:
                            qualified_name = f"{namespace}.{tool_name}" if namespace else tool_name
                            return await bridge.invoke_tool_json(
                                interaction_binding.token,
                                qualified_name,
                                arguments,
                            )
                    reset_memory = ""
                    if skipped_unsafe_binding:
                        reset_memory = _summarize_messages_for_runtime_reset(ctx.messages)
                    adapter_system_prompt = _build_structured_external_system_prompt(
                        agent,
                        runtime,
                        mode=task_payload.mode,
                        team_role=task_payload.team_role,
                        reset_memory=reset_memory,
                        active_skills=ctx.active_skills,
                    )
                    adapter = get_runtime_adapter(adapter_id)
                    configured_launch_args = (
                        runtime_metadata.get("launch_args")
                        if "launch_args" in runtime_metadata
                        else (["acp"] if adapter_id == "acp-stdio" else [])
                    )
                    execution_request = RuntimeExecutionRequest(
                        executable_path=runtime["executable_path"],
                        provider=provider,
                        prompt=task_payload.render_prompt(),
                        launch_args=[
                            str(item)
                            for item in (configured_launch_args or [])
                        ],
                        model=effective_model,
                        cwd=cwd,
                        system_prompt=adapter_system_prompt,
                        custom_args=agent.get("custom_args") or self.config.args,
                        custom_env=agent.get("custom_env") or self.config.env,
                        mcp_servers=mcp_servers,
                        dynamic_tools=dynamic_tools,
                        dynamic_tool_handler=dynamic_tool_handler,
                        resume_session_id=resume_session_id,
                        timeout=interactive_timeout,
                        permission_handler=_handle_permission,
                    )
                    resume_reset_memory = _summarize_messages_for_runtime_reset(ctx.messages)
                    async for event in _stream_runtime_with_safe_resume(
                        adapter,
                        execution_request,
                        reset_memory=resume_reset_memory,
                    ):
                        if event.kind == "session":
                            native_session_id = event.session_id
                            runtime_failure_session_id = native_session_id
                            native_session_reset = event.session_reset
                            if native_session_id and self.config.persist_runtime_session:
                                self.config.external_store.save_runtime_session_binding(
                                    **binding_key,
                                    native_session_id=native_session_id,
                                    session_profile=session_profile,
                                    status="active",
                                )
                            continue
                        if event.kind == "text" and event.text:
                            parts.append(event.text)
                            yield ResponseChunk.delta(ctx.request_id, event.text, next_seq())
                        elif event.kind == "thinking" and event.text:
                            thinking_parts.append(event.text)
                            yield ResponseChunk.thinking_event(ctx.request_id, event.text, next_seq())
                        elif event.kind == "error":
                            if adapter_id == "acp-stdio":
                                raise AcpAdapterError(event.text or "ACP stdout read failed")
                            raise ExternalCliError(event.text or "外部 Runtime 流读取失败")
                        elif event.kind == "usage" and event.usage:
                            usage.update(event.usage)
                        elif event.kind == "tool" and event.tool:
                            tool_id = event.tool.tool_call_id
                            if event.tool.phase == "start":
                                try:
                                    arguments = json.loads(event.tool.args or "{}")
                                except (json.JSONDecodeError, TypeError):
                                    arguments = {"raw": event.tool.args or ""}
                                if not isinstance(arguments, dict):
                                    arguments = {"value": arguments}
                                workspace_decision = check_workspace_guard(
                                    event.tool.name,
                                    arguments,
                                    permission_guard,
                                    cwd=cwd,
                                )
                                if not workspace_decision.allowed:
                                    yield ResponseChunk.error(ctx.request_id, workspace_decision.reason, next_seq())
                                    return
                                persisted_tools[tool_id] = ToolCall(
                                    id=tool_id,
                                    name=event.tool.name,
                                    arguments=arguments,
                                    status="running",
                                )
                            else:
                                tool_call = persisted_tools.get(tool_id)
                                if tool_call is None:
                                    tool_call = ToolCall(
                                        id=tool_id,
                                        name=event.tool.name,
                                    )
                                    persisted_tools[tool_id] = tool_call
                                tool_call.result = event.tool.detail or ""
                                tool_call.status = (
                                    "error" if event.tool.phase == "error" else "done"
                                )
                            yield ResponseChunk.tool_event(
                                ctx.request_id,
                                event.tool.name,
                                event.tool.phase,
                                event.tool.detail,
                                next_seq(),
                                tool_call_id=event.tool.tool_call_id,
                                args=event.tool.args,
                            )
                finally:
                    if interaction_binding is not None:
                        bridge.remove_binding(interaction_binding.token)
                if native_session_id and self.config.persist_runtime_session:
                    self.config.external_store.save_runtime_session_binding(
                        **binding_key,
                        native_session_id=native_session_id,
                        status="active",
                    )
                elif (
                    resume_session_id
                    and native_session_reset
                    and self.config.persist_runtime_session
                ):
                    self.config.external_store.delete_runtime_session_binding(**binding_key)
                output = "".join(parts).strip()
            elif protocol == "cli":
                output = await run_external_cli(
                    ExternalCliConfig(
                        provider=provider,
                        executable_path=runtime["executable_path"],
                        prompt=prompt,
                        model=effective_model,
                        cwd=ctx.cwd or self.config.cwd or ".",
                        system_prompt=_external_system_prompt(agent, runtime, effective_model),
                        custom_args=agent.get("custom_args") or self.config.args,
                        custom_env=agent.get("custom_env") or self.config.env,
                        timeout=self.config.timeout,
                    )
                )
            elif protocol == "client":
                output = await _run_client_prompt(
                    prompt,
                    ClientExecutorConfig(
                        module=str(runtime.get("executable_path") or ""),
                        function=str(runtime.get("metadata", {}).get("function") or ""),
                        external_agent_id=self.config.external_agent_id,
                        cwd=ctx.cwd or self.config.cwd or ".",
                        external_store=self.config.external_store,
                        options=runtime.get("metadata", {}),
                    ),
                    ctx,
                )
            else:
                output = f"暂不支持的外部智能体协议: {protocol or 'unknown'}"
        except AcpAdapterError as exc:
            if runtime_failure_binding_key is not None and runtime_failure_session_id:
                try:
                    self.config.external_store.save_runtime_session_binding(
                        **runtime_failure_binding_key,
                        native_session_id=runtime_failure_session_id,
                        status="unsafe_failed",
                    )
                except Exception:  # noqa: BLE001 - 不让清理失败遮蔽原始 ACP 错误
                    pass
            if provider == "hermes":
                detail = str(exc)
                if "ACP dependencies not installed" in detail:
                    append_pending_followup_answers()
                    yield ResponseChunk.error(
                        ctx.request_id,
                        "Hermes ACP 依赖未安装，请在 Hermes 的 Python 环境中执行：pip install -e '.[acp]'",
                    )
                    return
            append_pending_followup_answers()
            provider_name = _provider_display_name(provider)
            if "模型响应空闲超时" in str(exc):
                yield ResponseChunk.error(ctx.request_id, f"{provider_name} 模型响应空闲超时：{exc}")
            else:
                yield ResponseChunk.error(ctx.request_id, f"{provider_name} ACP 调用失败：{exc}")
            return
        except (ExternalCliError, CodexAdapterError) as exc:
            if runtime_failure_binding_key is not None and runtime_failure_session_id:
                try:
                    self.config.external_store.save_runtime_session_binding(
                        **runtime_failure_binding_key,
                        native_session_id=runtime_failure_session_id,
                        status="unsafe_failed",
                    )
                except Exception:
                    pass
            yield ResponseChunk.error(ctx.request_id, f"外部 CLI 调用失败：{exc}")
            return
        except Exception as exc:  # noqa: BLE001 - 外部 agent 失败需要转成对话错误帧
            yield ResponseChunk.error(ctx.request_id, f"外部智能体调用失败：{exc}")
            return

        tool_calls = list(persisted_tools.values()) if structured_adapter else []
        for tool_call in tool_calls:
            if tool_call.status == "running":
                tool_call.status = "done"
        append_pending_followup_answers()
        if _looks_like_missing_followup_tool(output):
            if structured_adapter:
                output = _followup_mcp_diagnostic(provider)
            elif protocol == "cli":
                output = _followup_cli_diagnostic(provider)
        assistant_message = Message.assistant(output, tool_calls, model=effective_model or None)
        if thinking_parts:
            assistant_message.thinking = "".join(thinking_parts).strip()
        ctx.messages.append(assistant_message)
        if not structured_adapter and output:
            yield ResponseChunk.delta(ctx.request_id, output, next_seq())
        yield ResponseChunk.final(ctx.request_id, output, next_seq(), usage=usage or None)


# 旧导入名仅用于平滑迁移；实现和配置均只有 ExternalExecutor 一套。
AcpExecutor = ExternalExecutor
AcpExecutorConfig = ExternalExecutorConfig
_build_compact_acp_system_prompt = _build_structured_external_system_prompt
