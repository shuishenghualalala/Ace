"""主 agent 调用子 agent 的两个工具。

- delegate_task：Crew自定义委派。主 agent 传 goal/context/toolsets，
  临时拼一个子 agent 去执行，用完即弃；支持 tasks[] 批量并行。
- run_agent：Crew 风格预设调用。主 agent 传 agent_type + goal，调用 frontmatter
  预定义好的子 agent（固定身份/prompt/工具/模型）。

两者通过 SubagentService 共享语义 Runtime；并发、发布与超时由 Runtime/Provider
负责，工具层只保留参数校验、后台兼容适配和稳定 JSON 输出。初期禁止嵌套——
AgentFactory 装配时从子 agent 工具范围剔除 subagent/external_agent 工具集。
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from crew.agent.subagent.definition import build_preset_spec
from crew.agent.prompt_builder import SUBAGENT_HANDOFF_GUIDANCE
from crew.agent.subagent.providers import InProcessSpawnProvider
from crew.agent.subagent.registry import SubagentRegistry
from crew.agent.subagent.runtime import (
    BatchSchedulePolicy,
    LegacySubagentBackgroundRuntime,
    SubagentRuntime,
    summary_policy_from_spec,
)
from crew.agent.subagent.service import StartRequest, SubagentService
from crew.features import AgentPresetBinding, AgentPresetRegistry, AgentPresetUnavailableError

if TYPE_CHECKING:
    from crew.agent.subagent.definition import SubagentDefinition
    from crew.core.interfaces import TaskManager
from crew.core.interfaces import Agent
from crew.core.runctx import (
    current_owner_account_id,
    current_parent_task_id,
    current_session_id,
    current_subagent_notify_session,
    current_workspace_id,
    normalize_owner_account_id,
    touch_current_task_activity,
)
from crew.state.logging import get_logger
from crew.core.errors import ToolError
from crew.tools.registry import Registry, tool_error, tool_result

log = get_logger("subagent")

# build_child(spec: dict) -> SingleAgent。spec 键：
#   system_prompt / toolsets / tools / model / max_iterations
BuildChild = Callable[[dict[str, Any]], Agent]
# launch_background(coro) -> None：把协程作为后台 asyncio 任务起飞并持有强引用
LaunchBackground = Callable[[Awaitable[Any]], None]

SUBAGENT_TOOLSET = "subagent"
RUN_AGENT_TOOLSET = "subagent.preset"  # run_agent / collect_subagent 独立 toolset，便于按需禁用

class ActiveSubagents:
    """按父 session 跟踪正在运行的子 agent。

    服务两件事：
      1. 中断级联——父被软中断（CrewApp.interrupt）时，把信号传给运行中的子 agent
         （Crew 的 interrupt 是协作式标志，父阻塞在 await child.run 时看不到自己的标志，
          故需主动下发给子 agent）。
      2. 可观测——gateway/UI 可查询某 session 下有哪些子 agent 在跑。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, dict[str, dict[str, Any]]] = {}

    def register(self, parent_session_id: str, child_id: str, record: dict[str, Any]) -> None:
        if not parent_session_id:
            return
        with self._lock:
            self._active.setdefault(parent_session_id, {})[child_id] = record

    def unregister(self, parent_session_id: str, child_id: str) -> None:
        if not parent_session_id:
            return
        with self._lock:
            children = self._active.get(parent_session_id)
            if not children:
                return
            children.pop(child_id, None)
            if not children:
                self._active.pop(parent_session_id, None)

    def interrupt(self, parent_session_id: str, message: str | None = None) -> bool:
        with self._lock:
            records = list(self._active.get(parent_session_id, {}).values())
        did = False
        for rec in records:
            agent = rec.get("agent")
            fn = getattr(agent, "interrupt", None)
            if callable(fn):
                fn(message)
                did = True
        return did

    def interrupt_child(self, child_id: str, message: str | None = None) -> bool:
        with self._lock:
            records = [
                record
                for children in self._active.values()
                for key, record in children.items()
                if key == child_id
            ]
        did = False
        for record in records:
            fn = getattr(record.get("agent"), "interrupt", None)
            if callable(fn):
                fn(message)
                did = True
        return did

    def snapshot(
        self,
        parent_session_id: str | None = None,
        *,
        owner_account_id: str | None = None,
    ) -> Any:
        def _public(rec: dict[str, Any]) -> dict[str, Any]:
            return {k: v for k, v in rec.items() if k != "agent"}

        owner = normalize_owner_account_id(owner_account_id) if owner_account_id else None

        def _visible(rec: dict[str, Any]) -> bool:
            return owner is None or normalize_owner_account_id(
                rec.get("owner_account_id")
            ) == owner

        with self._lock:
            if parent_session_id is not None:
                return [
                    _public(record)
                    for record in self._active.get(parent_session_id, {}).values()
                    if _visible(record)
                ]
            return {
                sid: [_public(record) for record in children.values() if _visible(record)]
                for sid, children in self._active.items()
                if any(_visible(record) for record in children.values())
            }

_EPHEMERAL_PROMPT = (
    "You are a focused subagent working on a specific delegated task. "
    "Complete the task using the tools available to you. "
    "When finished, provide a clear, concise summary with this structure:\n"
    "- What you did\n"
    "- What you found or accomplished\n"
    "- Any files you created or modified\n"
    "- Any issues encountered\n\n"
    "Be thorough but concise — your response is returned to the parent agent as a summary.\n\n"
    + SUBAGENT_HANDOFF_GUIDANCE
)


def build_delegate_task_schema(
    max_concurrent: int = 3,
    max_tasks: int = 8,
    max_batch_items: int = 128,
) -> dict[str, Any]:
    """Build the delegate_task tool schema with current runtime limits.

    The model needs to know its actual ceilings so it doesn't self-cap.
    Called on every registry rebuild so limits reflect live config.
    """
    task_props = {
        "goal": {
            "type": "string",
            "description": (
                "What the subagent should accomplish. Be specific and "
                "self-contained — the subagent knows nothing about your "
                "conversation history."
            ),
        },
        "context": {
            "type": "string",
            "description": (
                "Background information the subagent needs: file paths, "
                "error messages, project structure, constraints. The more "
                "specific you are, the better the subagent performs. "
                "Subagents have NO memory of your conversation — pass all "
                "relevant info here."
            ),
        },
        "toolsets": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Toolsets to enable for this subagent. "
                "Omit to inherit the default toolsets. "
                "Pass an empty array for no tools. "
                "Subagents can never gain tools the parent lacks."
            ),
        },
        "skills": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Skills (by name/slug) to inherit from the parent agent. "
                "Omit to inherit all of the parent's active skills; "
                "pass [] to inherit none. "
                "Subagents need the skills toolset to load skill content — "
                "if you restrict toolsets, do not exclude the skills toolset."
            ),
        },
    }
    return {
        "name": "delegate_task",
        "description": (
            "Spawn one or more ephemeral subagents to work on tasks in isolated contexts. "
            "Each subagent gets its own conversation and toolset. "
            "Only the final summary is returned — intermediate tool results "
            "never enter your context window.\n\n"
            "THREE MODES (exactly one of 'goal' / 'tasks' / 'prompt_template+items'):\n"
            "1. Single task: provide 'goal' (+ optional context, toolsets, skills)\n"
            f"2. Heterogeneous batch: provide 'tasks' array with up to {max_tasks} "
            f"items; up to {max_concurrent} run concurrently.\n"
            f"3. Homogeneous batch: provide 'prompt_template' (must contain the "
            f"placeholder {{item}}) plus 'items' (up to {max_batch_items} strings); "
            "one subagent runs per item with the placeholder substituted — use it "
            "when the same workflow applies to a list of targets.\n\n"
            "WHEN TO USE delegate_task:\n"
            "- Reasoning-heavy subtasks (debugging, code review, research synthesis)\n"
            "- Tasks that would flood your context with intermediate data\n"
            "- Parallel independent workstreams (research A and B simultaneously)\n\n"
            "WHEN NOT TO USE:\n"
            "- Single tool call — just call the tool directly\n"
            "- Tasks needing user interaction — subagents cannot ask the user\n"
            "- Trivial mechanical work you can do in one or two tool calls\n\n"
            "IMPORTANT:\n"
            "- Subagents have NO memory of your conversation. Pass all relevant "
            "info (file paths, error messages, constraints) via the 'context' field "
            "or inside the template.\n"
            "- Subagent summaries are SELF-REPORTS, not verified facts. A subagent "
            "that claims \"file written\" or \"search complete\" may be wrong. "
            "For critical operations, verify the result yourself before relying on it.\n"
            "- Subagents CANNOT delegate further (no nested subagents).\n"
            "- Results are always returned as an array, one entry per task, in the "
            "original order.\n\n"
            "BACKGROUND MODE:\n"
            "- Set run_in_background=true to launch a SINGLE task asynchronously: "
            "returns task_id immediately, you can keep working with the user, and the "
            "result is auto-injected into your next turn (or fetch via "
            "collect_subagent(task_id)).\n"
            "- Background is single-task only: combining 'tasks' or 'items' (batch) "
            "with run_in_background is rejected -- delegate one goal at a time, or "
            "drop run_in_background to run the batch synchronously."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                **task_props,
                "run_in_background": {
                    "type": "boolean",
                    "description": (
                        "Optional: run a single task asynchronously in background; "
                        "returns task_id immediately. Result is auto-injected into the "
                        "next turn or via collect_subagent. Rejected when combined with "
                        "'tasks'/'items' (batch)."
                    ),
                },
                "tasks": {
                    "type": "array",
                    "description": (
                        f"Heterogeneous batch mode: tasks to run in parallel (up to "
                        f"{max_tasks} items; up to {max_concurrent} run concurrently). "
                        "Each gets its own subagent with isolated context. "
                        "When provided, top-level goal/context/toolsets/skills are ignored."
                    ),
                    "items": {
                        "type": "object",
                        "properties": task_props,
                        "required": ["goal"],
                    },
                },
                "prompt_template": {
                    "type": "string",
                    "description": (
                        "Homogeneous batch mode: goal template containing the "
                        "placeholder {item}. Each entry of 'items' is substituted "
                        "into the template to build that subagent's goal. "
                        "Mutually exclusive with 'goal' and 'tasks'."
                    ),
                },
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        f"One entry per subagent for the template expansion "
                        f"(up to {max_batch_items}). Results keep this order."
                    ),
                },
            },
            # goal / tasks / prompt_template+items 三选一：顶层不强制字段，
            # 空输入与互斥校验的兜底在 handler 里（tool_error）。
            "required": [],
        },
    }


def build_run_agent_schema(
    agents: list["SubagentDefinition"] | list[str],
    *,
    allow_model_override: bool = True,
    model_override_agents: set[str] | None = None,
    self_contained_brief: bool = False,
) -> dict[str, Any]:
    """Build the run_agent tool schema.

    Injects each preset agent's name + description (whenToUse) into the tool
    description so the main agent knows which agents are available and when to
    call each one (aligns with Crew's Agent tool description injection).
    Falls back to name-only for string lists.
    """
    names: list[str] = []
    roster_lines: list[str] = []
    for a in agents:
        if isinstance(a, str):
            names.append(a)
            roster_lines.append(f"- {a}")
        else:
            names.append(a.name)
            roster_lines.append(f"- {a.name}: {a.description}")
    roster = "\n".join(roster_lines) if roster_lines else "(no preset subagents available)"

    properties: dict[str, Any] = {
        "agent_type": {
            "type": "string",
            "enum": names,
            "description": "Name of the preset subagent to call (see tool description for each agent's purpose)",
        },
        "goal": {"type": "string", "description": "Task description for the subagent"},
        "context": {"type": "string", "description": "Optional background information"},
        "run_in_background": {
            "type": "boolean",
            "description": "Optional: run asynchronously in background, returns task_id immediately; use collect_subagent to retrieve results later. Good for long-running tasks.",
        },
    }
    if allow_model_override:
        properties["model"] = {
            "type": "string",
            "description": "Optional: override the agent's model (model profile id from config.yaml, or 'inherit' to use the parent's model)",
        }
    context_note = (
        " The selected agent cannot see this conversation; provide a self-contained goal and context."
        if self_contained_brief
        else ""
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": ["agent_type", "goal"],
    }
    capable_names = set(model_override_agents or ())
    if capable_names and capable_names != set(names):
        common = {key: value for key, value in properties.items() if key != "model"}
        parameters["properties"] = common
        model_schema = {
            "type": "string",
            "description": "Optional: override the agent's model (model profile id from config.yaml, or 'inherit' to use the parent's model)",
        }
        parameters["oneOf"] = [
            {
                "type": "object",
                "properties": {
                    **common,
                    "agent_type": {
                        **common["agent_type"],
                        "enum": [name for name in names if name in capable_names],
                    },
                    "model": model_schema,
                },
                "required": ["agent_type", "goal"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    **common,
                    "agent_type": {
                        **common["agent_type"],
                        "enum": [name for name in names if name not in capable_names],
                    },
                },
                "required": ["agent_type", "goal"],
                "additionalProperties": False,
            },
        ]
    return {
        "name": "run_agent",
        "description": (
            "Call a preset subagent to execute a task (pre-configured identity, "
            "prompt, tools, and model). Use when a specialized role does a better "
            "job than a generic ephemeral subagent. Available preset subagents:\n"
            f"{roster}{context_note}"
        ),
        "parameters": parameters,
    }


def build_collect_subagent_schema() -> dict[str, Any]:
    return {
        "name": "collect_subagent",
        "description": (
            "Retrieve the result of a background subagent (launched via "
            "run_agent or delegate_task with run_in_background=true). "
            "wait=false returns current status immediately; "
            "wait=true blocks until the subagent completes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task_id returned by run_in_background"},
                "wait": {
                    "type": "boolean",
                    "description": "true=block until completion; false=return current status immediately (default false)",
                },
            },
            "required": ["task_id"],
        },
    }


def build_send_message_schema() -> dict[str, Any]:
    return {
        "name": "send_message",
        "description": (
            "Send a message to a continuable subagent owned by this parent session. "
            "Set trigger_turn=false to queue the message without starting a new turn."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "subagent_id": {"type": "string"},
                "message": {"type": "string"},
                "trigger_turn": {"type": "boolean"},
            },
            "required": ["subagent_id", "message"],
        },
    }


def build_list_agents_schema() -> dict[str, Any]:
    return {
        "name": "list_agents",
        "description": "List continuable subagents directly owned by this parent session.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        },
    }


def build_interrupt_agent_schema() -> dict[str, Any]:
    return {
        "name": "interrupt_agent",
        "description": "Interrupt a currently running subagent owned by this parent session.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "subagent_id": {"type": "string"},
                "message": {"type": "string"},
            },
            "required": ["subagent_id"],
        },
    }


def _compose_goal(goal: str, context: str | None) -> str:
    goal = (goal or "").strip()
    context = (context or "").strip()
    if context:
        return f"CONTEXT:\n{context}\n\nYOUR TASK:\n{goal}"
    return goal


def _expand_template_items(
    prompt_template: Any,
    items: Any,
    *,
    max_batch_items: int,
) -> list[str]:
    """同构批量：模板必须含 {item} 占位、条目数受限、展开后重复即拒绝。"""

    template = str(prompt_template or "").strip()
    if not template:
        raise ToolError("prompt_template 不能为空")
    if "{item}" not in template:
        raise ToolError("prompt_template 必须包含 {item} 占位符（每个 item 会替换它）")
    if not isinstance(items, list) or not items:
        raise ToolError("items 必须是非空字符串数组")
    if any(not isinstance(item, str) or not item.strip() for item in items):
        raise ToolError("items 的每一项都必须是非空字符串")
    if len(items) > max_batch_items:
        raise ToolError(
            f"items 超出同构批量上限：max {max_batch_items}, got {len(items)}。"
            "请分批委派。"
        )
    expanded = [template.replace("{item}", item) for item in items]
    if len({goal.strip() for goal in expanded}) != len(expanded):
        raise ToolError("模板展开后的任务目标存在重复项，请修正 items 或模板")
    return expanded


def _normalize_skills(skills: Any) -> list[str]:
    """把 delegate_task 的 skills 参数归一化为 slug 列表。

    模型可能传字符串、带前导 / 的 slug、或 frontmatter name；统一去前导 /、去空白、去重。
    不在此处做父级范围交集——交集由 AgentFactory 装配时按父 skill 范围裁剪。
    """
    if isinstance(skills, str):
        skills = [skills]
    if not isinstance(skills, (list, tuple)):
        return []
    seen: list[str] = []
    for s in skills:
        name = str(s or "").strip().lstrip("/")
        if name and name not in seen:
            seen.append(name)
    return seen


def _build_runtime(
    build_child: BuildChild,
    *,
    active: ActiveSubagents | None,
    idle_timeout: float,
    max_runtime: float,
) -> SubagentRuntime:
    provider = InProcessSpawnProvider(
        build_child,
        active=active,
        idle_timeout=idle_timeout,
        max_runtime=max_runtime,
    )
    return SubagentRuntime(provider)


def _start_request(
    *,
    label: str,
    spec: dict[str, Any],
    goal_text: str,
    parent_session_id: str,
    lifecycle: str = "one_shot",
) -> StartRequest:
    raw_options = spec.get("_agent_options")
    requests_tool_filter = _spec_requests_tool_policy(spec)
    return StartRequest(
        label=label,
        goal_text=goal_text,
        parent_session_id=parent_session_id,
        spec=spec,
        provider=str(spec.get("provider") or "").strip(),
        agent_options=(dict(raw_options) if isinstance(raw_options, dict) else None),
        # The in-process factory still resolves the concrete tool names from
        # ``spec``.  The empty tuple is a capability demand at the semantic
        # seam, ensuring transports that cannot honor tool scoping fail before
        # publication instead of silently ignoring the request.
        tool_filter=() if requests_tool_filter else None,
        owner_account_id=current_owner_account_id.get(),
        workspace_id=current_workspace_id.get(),
        lifecycle=lifecycle,  # type: ignore[arg-type]
    )


def _provider_for_preset(service: SubagentService, preset: Any) -> Any | None:
    name = str(getattr(preset, "provider", "") or "inprocess-spawn").strip()
    return service.get_provider(
        name or "inprocess-spawn",
        owner_account_id=current_owner_account_id.get(),
    )


def _spec_requests_tool_policy(spec: dict[str, Any]) -> bool:
    """Return whether a preset asks the transport to enforce child scope."""

    return any(
        spec.get(key) not in (None, [], (), "")
        for key in (
            "toolsets",
            "tools",
            "toolset_additions",
            "skills",
            "preset_skills",
            "reserved_toolsets",
            "reserved_skills",
        )
    )


def _available_presets(
    service: SubagentService,
    presets: list[Any],
) -> list[Any]:
    return [preset for preset in presets if _provider_for_preset(service, preset) is not None]


def _preset_schema(
    service: SubagentService,
    presets: list[Any],
) -> dict[str, Any]:
    visible = _available_presets(service, presets)
    providers = [_provider_for_preset(service, preset) for preset in visible]
    model_override_agents = {
        str(getattr(preset, "name", "") or preset).strip()
        for preset, provider in zip(visible, providers, strict=True)
        if provider is not None and provider.capabilities.agent_options
    }
    return build_run_agent_schema(
        visible,
        allow_model_override=bool(model_override_agents),
        model_override_agents=model_override_agents,
        self_contained_brief=any(
            not bool(getattr(provider, "inherits_parent_context", False))
            for provider in providers
            if provider is not None
        ),
    )


async def _execute_child(
    runtime: SubagentService,
    *,
    label: str,
    spec: dict[str, Any],
    goal_text: str,
    parent_session_id: str,
    idle_timeout: float,
    max_runtime: float,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    preset_binding: AgentPresetBinding | None = None,
    summary_floor: dict[str, int] | None = None,
) -> dict[str, Any]:
    options: dict[str, Any] = {
        "idle_timeout": idle_timeout,
        "max_runtime": max_runtime,
        "progress_callback": progress_callback,
    }
    summary_policy = summary_policy_from_spec(spec, summary_floor)
    if summary_policy is not None:
        options["summary_policy"] = summary_policy
    if preset_binding is not None:
        options["lease_factory"] = lambda: preset_binding.acquire_lease(
            f"agent-preset:{label}"
        )
        trusted = spec.get("_preset_spec")
        if isinstance(trusted, dict):
            options["trusted_preset_spec"] = dict(trusted)
    result = await runtime.execute(
        _start_request(
            label=label,
            spec=spec,
            goal_text=goal_text,
            parent_session_id=parent_session_id,
            lifecycle=(
                "continuable"
                if bool(getattr(runtime, "continuable_enabled", False))
                else "one_shot"
            ),
        ),
        runtime_options=options,
    )
    return result.to_legacy_dict(agent=label)


async def _run_one_child_unleased(
    *,
    label: str,
    spec: dict[str, Any],
    goal_text: str,
    build_child: BuildChild,
    parent_session_id: str,
    active: ActiveSubagents | None,
    idle_timeout: float,
    max_runtime: float,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    runtime: SubagentService | None = None,
) -> dict[str, Any]:
    """Compatibility wrapper over the semantic runtime."""

    runtime = runtime or _build_runtime(
        build_child,
        active=active,
        idle_timeout=idle_timeout,
        max_runtime=max_runtime,
    )
    return await _execute_child(
        runtime,
        label=label,
        spec=spec,
        goal_text=goal_text,
        parent_session_id=parent_session_id,
        idle_timeout=idle_timeout,
        max_runtime=max_runtime,
        progress_callback=progress_callback,
    )


async def _run_one_child(
    *,
    label: str,
    spec: dict[str, Any],
    goal_text: str,
    build_child: BuildChild,
    parent_session_id: str,
    active: ActiveSubagents | None,
    idle_timeout: float,
    max_runtime: float,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    preset_binding: AgentPresetBinding | None = None,
    runtime: SubagentService | None = None,
) -> dict[str, Any]:
    """Run a child while retaining its selected preset Generation lease."""
    runtime = runtime or _build_runtime(
        build_child,
        active=active,
        idle_timeout=idle_timeout,
        max_runtime=max_runtime,
    )
    return await _execute_child(
        runtime,
        label=label,
        spec=spec,
        goal_text=goal_text,
        parent_session_id=parent_session_id,
        idle_timeout=idle_timeout,
        max_runtime=max_runtime,
        progress_callback=progress_callback,
        preset_binding=preset_binding,
    )


async def _run_children(
    specs: list[dict[str, Any]],
    *,
    build_child: BuildChild | None,
    max_concurrent: int,
    active: ActiveSubagents | None,
    idle_timeout: float,
    max_runtime: float,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    runtime: SubagentService | None = None,
    batch_policy: BatchSchedulePolicy | None = None,
    summary_floor: dict[str, int] | None = None,
) -> str:
    """并发跑多个子 agent，结构化汇总（JSON）。specs 每项含 label/goal_text/spec。

    批量走语义 runtime 的调度器（爬坡 + 限流退避 + 定序聚合）；无调度器时
    回退 ``execute_many``（仅并发上限）。
    """
    parent_session_id = current_session_id.get() or ""
    if runtime is None:
        if build_child is None:
            raise RuntimeError("subagent execution requires a service or child factory")
        runtime = _build_runtime(
            build_child,
            active=active,
            idle_timeout=idle_timeout,
            max_runtime=max_runtime,
        )
    requests: list[tuple[StartRequest, dict[str, Any]]] = []
    lifecycle = (
        "continuable"
        if bool(getattr(runtime, "continuable_enabled", False))
        else "one_shot"
    )
    for item in specs:
        options: dict[str, Any] = {
            "idle_timeout": idle_timeout,
            "max_runtime": max_runtime,
            "progress_callback": progress_callback,
        }
        summary_policy = summary_policy_from_spec(item["spec"], summary_floor)
        if summary_policy is not None:
            options["summary_policy"] = summary_policy
        binding = item.get("preset_binding")
        if binding is not None:
            options["lease_factory"] = lambda binding=binding, label=item["label"]: (
                binding.acquire_lease(f"agent-preset:{label}")
            )
            trusted = item["spec"].get("_preset_spec")
            if isinstance(trusted, dict):
                options["trusted_preset_spec"] = dict(trusted)
        requests.append((
            _start_request(
                label=item["label"],
                spec=item["spec"],
                goal_text=item["goal_text"],
                parent_session_id=parent_session_id,
                lifecycle=lifecycle,
            ),
            options,
        ))
    execute_batch = getattr(runtime, "execute_batch", None)
    if batch_policy is not None and callable(execute_batch):
        results = await execute_batch(requests, policy=batch_policy)
    else:
        results = await runtime.execute_many(
            requests,
            max_concurrent=max_concurrent,
        )
    return tool_result({
        "results": [
            result.to_legacy_dict(agent=item["label"])
            for item, result in zip(specs, results, strict=True)
        ]
    })


def build_subagent_tools(
    registry: Registry,
    sub_registry: SubagentRegistry,
    service: SubagentService,
    *,
    build_child: BuildChild | None = None,
    max_concurrent: int = 3,
    max_tasks: int = 8,
    idle_timeout: float = 120.0,
    max_runtime: float = 1800.0,
    active: ActiveSubagents | None = None,
    default_toolsets: list[str] | None = None,
    tasks: "TaskManager | None" = None,
    launch_background: LaunchBackground | None = None,
    on_background_done: Callable[[str, dict[str, Any]], None] | None = None,
    background_capacity: Callable[[], bool] | None = None,
    on_collected: Callable[[str, str, str], None] | None = None,
    preset_registry: AgentPresetRegistry | None = None,
    batch_policy: "BatchSchedulePolicy | None" = None,
    max_batch_items: int = 128,
    summary_defaults: dict[str, int] | None = None,
    register_tool: Callable[..., None] | None = None,
) -> None:
    """注册 delegate_task / run_agent / collect_subagent 工具到 toolset='subagent'。

    后台异步（run_agent 的 run_in_background / frontmatter background）需要 tasks +
    launch_background；未提供时自动降级为同步执行。``batch_policy`` 控制批量的
    爬坡与限流退避（缺省仅并发上限）；``summary_defaults`` 提供摘要兜底的全局
    默认阈值（preset 可按名覆盖）。
    """
    subagent_service = service
    publish_tool = register_tool or registry.register
    schedule = batch_policy or BatchSchedulePolicy(max_concurrent=max_concurrent)
    summary_floor = dict(summary_defaults or {})
    backgrounds = (
        LegacySubagentBackgroundRuntime(
            subagent_service,
            tasks=tasks,
            launch_background=launch_background,
            idle_timeout=idle_timeout,
            max_runtime=max_runtime,
            on_done=on_background_done,
            on_collected=on_collected,
            capacity=background_capacity,
            summary_floor=summary_floor,
        )
        if tasks is not None and launch_background is not None
        else None
    )

    # ---- delegate_task：自定义临时委派 ----
    async def handle_delegate_task(args: dict[str, Any]) -> str:
        template_mode = (
            args.get("prompt_template") is not None or args.get("items") is not None
        )
        if template_mode:
            if args.get("goal") or args.get("tasks"):
                raise ToolError(
                    "prompt_template+items 与 goal/tasks 互斥：同构批量用模板，"
                    "异构批量用 tasks，单任务用 goal。"
                )
            expanded = _expand_template_items(
                args.get("prompt_template"),
                args.get("items"),
                max_batch_items=max_batch_items,
            )
            task_list = [
                {
                    "goal": goal,
                    "context": args.get("context"),
                    "toolsets": args.get("toolsets"),
                    "skills": args.get("skills"),
                }
                for goal in expanded
            ]
        else:
            raw_tasks = args.get("tasks")
            if isinstance(raw_tasks, list) and raw_tasks:
                task_list = raw_tasks
            else:
                task_list = [{
                    "goal": args.get("goal"),
                    "context": args.get("context"),
                    "toolsets": args.get("toolsets"),
                }]

        if max_tasks and len(task_list) > max_tasks:
            # 超限是硬性拒绝（用于 守卫即报错）→ 抛 ToolError，registry 标记 is_error
            raise ToolError(
                f"Too many tasks: max {max_tasks}, got {len(task_list)}. Split into smaller batches."
            )

        specs: list[dict[str, Any]] = []
        for idx, t in enumerate(task_list):
            goal = str((t or {}).get("goal") or "").strip()
            if not goal:
                return tool_error("Each task must provide a non-empty goal")
            toolsets = t.get("toolsets") or default_toolsets
            # skills：None=继承主 agent 全部；list=指定（空 list=不要）。与 toolsets 用法对齐。
            # 仅当键存在时才标记继承——run_agent 路径不写 inherit_skills，维持不注入现状。
            skills_raw = t.get("skills")
            inherit_skills = skills_raw is not None
            skills = _normalize_skills(skills_raw) if inherit_skills else None
            specs.append({
                "label": f"task#{idx}",
                "goal_text": _compose_goal(goal, t.get("context")),
                "spec": {
                    "system_prompt": _EPHEMERAL_PROMPT,
                    "toolsets": toolsets,
                    "tools": None,
                    "model": "inherit",
                    "max_iterations": None,
                    "inherit_skills": inherit_skills,
                    "skills": skills,
                },
            })
        # 后台模式：仅支持单任务。batch + run_in_background 拒绝--多 task_id 管理 +
        # 多路通知聚合会让后台管道复杂化，且后台典型为长任务、batch 为并行短任务，语义不同。
        want_bg = bool(args.get("run_in_background"))
        if want_bg and backgrounds is not None:
            if len(specs) > 1:
                raise ToolError(
                    "后台模式不支持批量任务：请逐个委派（每次一个 goal + run_in_background），"
                    "或去掉 run_in_background 走同步并行。"
                )
            item = dict(specs[0])
            # 后台通知里展示的 agent 名用 goal 摘要，比默认的 task#0 更可读
            item["label"] = (item["goal_text"] or "")[:40] or item["label"]
            return tool_result(backgrounds.launch(
                item=item,
                parent_session_id=(
                    current_subagent_notify_session.get()
                    or current_session_id.get()
                    or ""
                ),
                parent_task_id=current_parent_task_id.get(),
                owner_account_id=current_owner_account_id.get(),
                workspace_id=current_workspace_id.get(),
            ))
        return await _run_children(
            specs,
            build_child=build_child,
            max_concurrent=max_concurrent,
            active=active,
            idle_timeout=idle_timeout,
            max_runtime=max_runtime,
            progress_callback=lambda progress: touch_current_task_activity({
                "last_tool": "delegate_task",
                "tool_phase": "progress",
                "subagent": progress.get("agent", ""),
                "subagent_last_tool": progress.get("last_tool", ""),
                "subagent_last_chunk": progress.get("last_chunk", ""),
            }),
            runtime=subagent_service,
            batch_policy=schedule,
            summary_floor=summary_floor,
        )

    publish_tool(
        name="delegate_task",
        toolset=SUBAGENT_TOOLSET,
        schema=build_delegate_task_schema(
            max_concurrent=max_concurrent,
            max_tasks=max_tasks,
            max_batch_items=max_batch_items,
        ),
        handler=handle_delegate_task,
        is_async=True,
        display_name="委派子任务",
        ui_label_template="委派子任务 {title}",
        should_defer=True,
        search_hint="delegate subagent parallel task research review isolated context",
        result_retention="important",
    )

    # ---- run_agent：调用预设 agent ----
    async def handle_run_agent(args: dict[str, Any]) -> str:
        agent_type = str(args.get("agent_type") or "").strip()
        goal = str(args.get("goal") or "").strip()
        if not agent_type:
            return tool_error("agent_type is required")
        if not goal:
            return tool_error("goal is required")
        binding = None
        if preset_registry is not None:
            try:
                binding = preset_registry.resolve(agent_type)
                spec = binding.contribution.as_spec()
                spec["model"] = str(args.get("model") or "").strip() or spec["model"]
                spec["preset_generation"] = binding.generation.key
                spec["_preset_spec"] = dict(spec)
            except AgentPresetUnavailableError:
                return tool_error(f"Unknown or unavailable preset subagent: {agent_type}")
            definition = None
        else:
            definition = sub_registry.get(agent_type)
        if definition is None and binding is None:
            return tool_error(
                f"Unknown preset subagent: {agent_type}. Available: {sub_registry.names()}"
            )

        if binding is None:
            spec = build_preset_spec(definition, model_override=str(args.get("model") or ""))
        provider_name = str(spec.get("provider") or "inprocess-spawn").strip()
        provider = subagent_service.get_provider(
            provider_name,
            owner_account_id=current_owner_account_id.get(),
        )
        if provider is None:
            return tool_error(
                f"Preset subagent {agent_type!r} is unavailable because provider "
                f"{provider_name!r} is not active"
            )
        explicit_model = str(args.get("model") or "").strip()
        if explicit_model:
            if not provider.capabilities.agent_options:
                return tool_error(
                    f"Preset subagent {agent_type!r} does not support model override"
                )
            spec["model"] = explicit_model
            spec["_agent_options"] = {"model": explicit_model}
        elif (
            str(spec.get("model") or "inherit").strip() not in {"", "inherit"}
            and not provider.capabilities.agent_options
        ):
            return tool_error(
                f"Preset subagent {agent_type!r} configures a model override that "
                f"provider {provider_name!r} cannot honor"
            )
        requests_tool_filter = _spec_requests_tool_policy(spec)
        if requests_tool_filter and not provider.capabilities.tool_filter:
            return tool_error(
                f"Preset subagent {agent_type!r} configures a tool policy that "
                f"provider {provider_name!r} cannot honor"
            )
        if spec.get("max_iterations") is not None and not provider.capabilities.agent_options:
            return tool_error(
                f"Preset subagent {agent_type!r} configures max_iterations that "
                f"provider {provider_name!r} cannot honor"
            )
        if not spec["system_prompt"]:
            spec["system_prompt"] = _EPHEMERAL_PROMPT
        item = {
            "label": agent_type,
            "goal_text": _compose_goal(goal, args.get("context")),
            "spec": spec,
            "preset_binding": binding,
        }

        # 后台异步：run_in_background 参数 或 预设 frontmatter background=true
        want_bg = bool(args.get("run_in_background")) or bool(
            spec.get("background")
        )
        if want_bg and backgrounds is not None:
            return tool_result(backgrounds.launch(
                item=item,
                parent_session_id=(
                    current_subagent_notify_session.get()
                    or current_session_id.get()
                    or ""
                ),
                parent_task_id=current_parent_task_id.get(),
                owner_account_id=current_owner_account_id.get(),
                workspace_id=current_workspace_id.get(),
            ))

        return await _run_children(
            [item],
            build_child=build_child,
            max_concurrent=max_concurrent,
            active=active,
            idle_timeout=idle_timeout,
            max_runtime=max_runtime,
            runtime=subagent_service,
            batch_policy=schedule,
            summary_floor=summary_floor,
        )

    # ---- collect_subagent：取回后台子任务结果 ----
    async def handle_collect(args: dict[str, Any]) -> str:
        if backgrounds is None:
            return tool_error("Background subagent support is not enabled")
        task_id = str(args.get("task_id") or "").strip()
        if not task_id:
            return tool_error("task_id is required")
        try:
            return await backgrounds.collect(
                task_id,
                wait=bool(args.get("wait")),
                owner_account_id=current_owner_account_id.get(),
            )
        except KeyError:
            return tool_error(f"Task not found: {task_id}")

    # 无预设子智能体时不注册 run_agent——否则 agent_type 的 enum 为空数组，
    # 模型无合法值可选、部分 provider 也会拒绝空 enum。delegate_task 仍可用。
    available_initial = _available_presets(
        subagent_service,
        preset_registry.list() if preset_registry is not None else sub_registry.list(),
    )
    if available_initial:
        publish_tool(
            name="run_agent",
            toolset=RUN_AGENT_TOOLSET,
            schema=_preset_schema(subagent_service, available_initial),
            schema_resolver=(
                (lambda: _preset_schema(subagent_service, preset_registry.list()))
                if preset_registry is not None else None
            ),
            check_fn=(
                (lambda: bool(_available_presets(subagent_service, preset_registry.list())))
                if preset_registry is not None else None
            ),
            handler=handle_run_agent,
            is_async=True,
            display_name="运行子智能体",
            ui_label_template="运行子智能体 {agent_type}",
            should_defer=True,
            search_hint="run preset subagent specialist background task 运行 预设 子智能体 委派 知识库 wiki",
            result_retention="important",
        )
        # collect 仅在支持后台时才有意义
        if tasks is not None:
            publish_tool(
                name="collect_subagent",
                toolset=RUN_AGENT_TOOLSET,
                schema=build_collect_subagent_schema(),
                handler=handle_collect,
                is_async=True,
                display_name="收集子智能体结果",
                ui_label_template="收集子智能体结果 {task_id}",
                should_defer=True,
                search_hint="collect background subagent task result wait status",
                result_retention="important",
            )
    else:
        log.info("无预设子智能体，跳过注册 run_agent（delegate_task 仍可用）")

    # ---- continuable control plane ----
    if bool(getattr(subagent_service, "continuable_enabled", False)) and all(
        callable(getattr(subagent_service, name, None))
        for name in ("send_message", "list_agents", "interrupt_agent")
    ):
        async def handle_send_message(args: dict[str, Any]) -> str:
            subagent_id = str(args.get("subagent_id") or "").strip()
            message = str(args.get("message") or "").strip()
            if not subagent_id or not message:
                return tool_error("subagent_id and message are required")
            try:
                result = await subagent_service.send_message(
                    subagent_id=subagent_id,
                    message=message,
                    parent_session_id=current_session_id.get() or "",
                    owner_account_id=current_owner_account_id.get(),
                    trigger_turn=bool(args.get("trigger_turn", True)),
                )
            except PermissionError:
                return tool_error("Subagent is not owned by this parent session")
            except KeyError:
                return tool_error(f"Unknown subagent: {subagent_id}")
            except ValueError as exc:
                return tool_error(str(exc))
            return tool_result(result)

        async def handle_list_agents(_args: dict[str, Any]) -> str:
            return tool_result(subagent_service.list_agents(
                parent_session_id=current_session_id.get() or "",
                owner_account_id=current_owner_account_id.get(),
            ))

        async def handle_interrupt_agent(args: dict[str, Any]) -> str:
            subagent_id = str(args.get("subagent_id") or "").strip()
            if not subagent_id:
                return tool_error("subagent_id is required")
            accepted = subagent_service.interrupt_agent(
                subagent_id=subagent_id,
                parent_session_id=current_session_id.get() or "",
                owner_account_id=current_owner_account_id.get(),
                message=str(args.get("message") or "").strip() or None,
            )
            return tool_result({
                "status": "interrupted" if accepted else "not_running",
                "subagent_id": subagent_id,
            })

        publish_tool(
            name="send_message",
            toolset=SUBAGENT_TOOLSET,
            schema=build_send_message_schema(),
            handler=handle_send_message,
            is_async=True,
            display_name="发送给子智能体",
            ui_label_template="发送给子智能体 {subagent_id}",
            should_defer=True,
            result_retention="important",
        )
        publish_tool(
            name="list_agents",
            toolset=SUBAGENT_TOOLSET,
            schema=build_list_agents_schema(),
            handler=handle_list_agents,
            is_async=True,
            display_name="列出子智能体",
            should_defer=True,
            result_retention="important",
        )
        publish_tool(
            name="interrupt_agent",
            toolset=SUBAGENT_TOOLSET,
            schema=build_interrupt_agent_schema(),
            handler=handle_interrupt_agent,
            is_async=True,
            display_name="中断子智能体",
            ui_label_template="中断子智能体 {subagent_id}",
            should_defer=True,
            result_retention="important",
        )


def register_subagent_tools(
    registry: Registry,
    sub_registry: SubagentRegistry,
    build_child: BuildChild,
    **kwargs: Any,
) -> None:
    """Compatibility adapter for the pre-Service registration signature."""

    service = kwargs.pop("service", None)
    if service is None:
        service = _build_runtime(
            build_child,
            active=kwargs.get("active"),
            idle_timeout=float(kwargs.get("idle_timeout", 120.0)),
            max_runtime=float(kwargs.get("max_runtime", 1800.0)),
        )
    build_subagent_tools(
        registry,
        sub_registry,
        service,
        build_child=build_child,
        **kwargs,
    )
