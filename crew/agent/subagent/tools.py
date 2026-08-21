"""主 agent 调用子 agent 的两个工具。

- delegate_task：Crew自定义委派。主 agent 传 goal/context/toolsets，
  临时拼一个子 agent 去执行，用完即弃；支持 tasks[] 批量并行。
- run_agent：Crew 风格预设调用。主 agent 传 agent_type + goal，调用 frontmatter
  预定义好的子 agent（固定身份/prompt/工具/模型）。

两者底层共享同一套 child runner：build_child 由 app 注入（复用 SingleAgent 装配机器），
并发用 asyncio.Semaphore + gather（Crew 原生 asyncio）。初期禁止嵌套——子 agent 的
工具集由 app 侧 tool_filter 强制剔除 subagent/external_agent 工具集。
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from crew.agent.subagent.definition import build_preset_spec
from crew.agent.subagent.registry import SubagentRegistry

if TYPE_CHECKING:
    from crew.agent.subagent.definition import SubagentDefinition
    from crew.core.interfaces import TaskManager
from crew.core.envelope import Envelope
from crew.core.interfaces import Agent
from crew.core.runctx import (
    current_owner_account_id,
    current_parent_task_id,
    current_session_id,
    current_subagent_notify_session,
    current_workspace_id,
)
from crew.state.logging import get_logger
from crew.core.errors import ToolError
from crew.tools.registry import Registry, tool_error, tool_result
from crew.tools.redact import redact_sensitive_display_text

log = get_logger("subagent")

# build_child(spec: dict) -> SingleAgent。spec 键：
#   system_prompt / toolsets / tools / model / max_iterations
BuildChild = Callable[[dict[str, Any]], Agent]
# launch_background(coro) -> None：把协程作为后台 asyncio 任务起飞并持有强引用
LaunchBackground = Callable[[Awaitable[Any]], None]

SUBAGENT_TOOLSET = "subagent"
RUN_AGENT_TOOLSET = "subagent.preset"  # run_agent / collect_subagent 独立 toolset，便于按需禁用

# 结构化 status → 任务看板 status
_STATUS_TO_TASK = {"completed": "done", "timeout": "failed", "error": "failed"}


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

    def snapshot(self, parent_session_id: str | None = None) -> Any:
        def _public(rec: dict[str, Any]) -> dict[str, Any]:
            return {k: v for k, v in rec.items() if k != "agent"}

        with self._lock:
            if parent_session_id is not None:
                return [_public(r) for r in self._active.get(parent_session_id, {}).values()]
            return {
                sid: [_public(r) for r in children.values()]
                for sid, children in self._active.items()
            }

_EPHEMERAL_PROMPT = (
    "You are a focused subagent working on a specific delegated task. "
    "Complete the task using the tools available to you. "
    "When finished, provide a clear, concise summary with this structure:\n"
    "- What you did\n"
    "- What you found or accomplished\n"
    "- Any files you created or modified\n"
    "- Any issues encountered\n\n"
    "Be thorough but concise — your response is returned to the parent agent as a summary. "
    "Do only what was delegated, do not overstep, and do not delegate to others."
)


def build_delegate_task_schema(
    max_concurrent: int = 3,
    max_tasks: int = 8,
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
            "TWO MODES (one of 'goal' or 'tasks' is required):\n"
            "1. Single task: provide 'goal' (+ optional context, toolsets, skills)\n"
            f"2. Batch (parallel): provide 'tasks' array with up to {max_tasks} "
            f"items; up to {max_concurrent} run concurrently.\n\n"
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
            "info (file paths, error messages, constraints) via the 'context' field.\n"
            "- Subagent summaries are SELF-REPORTS, not verified facts. A subagent "
            "that claims \"file written\" or \"search complete\" may be wrong. "
            "For critical operations, verify the result yourself before relying on it.\n"
            "- Subagents CANNOT delegate further (no nested subagents).\n"
            "- Results are always returned as an array, one entry per task.\n\n"
            "BACKGROUND MODE:\n"
            "- Set run_in_background=true to launch a SINGLE task asynchronously: "
            "returns task_id immediately, you can keep working with the user, and the "
            "result is auto-injected into your next turn (or fetch via "
            "collect_subagent(task_id)).\n"
            "- Background is single-task only: combining 'tasks' (batch) with "
            "run_in_background is rejected -- delegate one goal at a time, or drop "
            "run_in_background to run the batch synchronously."
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
                        "'tasks' (batch)."
                    ),
                },
                "tasks": {
                    "type": "array",
                    "description": (
                        f"Batch mode: tasks to run in parallel (up to {max_tasks} "
                        f"items; up to {max_concurrent} run concurrently). "
                        "Each gets its own subagent with isolated context. "
                        "When provided, top-level goal/context/toolsets/skills are ignored."
                    ),
                    "items": {
                        "type": "object",
                        "properties": task_props,
                        "required": ["goal"],
                    },
                },
            },
            # goal / tasks 二选一：顶层不强制 goal，批量模式（仅传 tasks）才能过校验。
            # 空输入的兜底在 handler 里（goal 空且无 tasks -> tool_error）。
            # 用于 delegate_tool.py:2799（顶层 required: []）。
            "required": [],
        },
    }


def build_run_agent_schema(
    agents: list["SubagentDefinition"] | list[str],
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

    return {
        "name": "run_agent",
        "description": (
            "Call a preset subagent to execute a task (pre-configured identity, "
            "prompt, tools, and model). Use when a specialized role does a better "
            "job than a generic ephemeral subagent. Available preset subagents:\n"
            f"{roster}"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent_type": {
                    "type": "string",
                    "enum": names,
                    "description": "Name of the preset subagent to call (see tool description for each agent's purpose)",
                },
                "goal": {"type": "string", "description": "Task description for the subagent"},
                "context": {"type": "string", "description": "Optional background information"},
                "model": {
                    "type": "string",
                    "description": "Optional: override the agent's model (model profile id from config.yaml, or 'inherit' to use the parent's model)",
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": "Optional: run asynchronously in background, returns task_id immediately; use collect_subagent to retrieve results later. Good for long-running tasks.",
                },
            },
            "required": ["agent_type", "goal"],
        },
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


def _compose_goal(goal: str, context: str | None) -> str:
    goal = (goal or "").strip()
    context = (context or "").strip()
    if context:
        return f"CONTEXT:\n{context}\n\nYOUR TASK:\n{goal}"
    return goal


def _normalize_skills(skills: Any) -> list[str]:
    """把 delegate_task 的 skills 参数归一化为 slug 列表。

    模型可能传字符串、带前导 / 的 slug、或 frontmatter name；统一去前导 /、去空白、去重。
    不在此处做父级范围交集——交集在 _make_subagent 里按父 skill 范围裁剪。
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


_PARTIAL_CAP = 2000   # 部分输出缓冲上限（保留尾部）
_PARTIAL_TAIL = 800   # 中止时附带的部分输出尾部长度
_SUBAGENT_TEXT_CAP = 128 * 1024


def _safe_subagent_text(value: Any, *, limit: int = _SUBAGENT_TEXT_CAP) -> str:
    """子 agent 输出是内容，不是策略；展示/回传前只做脱敏和有界化。"""
    return redact_sensitive_display_text(str(value or ""))[:limit]


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
) -> dict[str, Any]:
    """跑一个子 agent，返回结构化结果。

    超时模型（对照 Crew：靠迭代上限 + per-step 有界，而非整段总时长一刀切）：
    - idle_timeout：逐 chunk 空闲超时——只有 N 秒内零输出（真卡死）才中止；
      正常逐步推进的 agent 持续吐 chunk，永不触发。<=0 表示不设空闲超时。
    - max_runtime：绝对运行上限兜底（用户要的全局超时）；<=0 表示不限。
    中止/出错时返回部分进度 + 诊断（last_tool / tool_calls / 部分输出尾部）。
    """
    child = build_child(spec)
    # 子会话用完即弃，用 uuid 命名保证隔离与唯一（不依赖父 session_id）
    child_id = f"sub::{label}::{uuid.uuid4().hex[:8]}"
    sub_env = Envelope.of(
        goal_text,
        session_id=child_id,
        params={"task_session_id": parent_session_id or child_id, "agent_id": label},
        channel="subagent",
        mode="agent",
        workspace_id=current_workspace_id.get(),
        user_id=current_owner_account_id.get(),
    )
    if active is not None:
        active.register(parent_session_id, child_id, {
            "child_id": child_id,
            "label": label,
            "agent": child,
            "started_at": time.time(),
        })

    started = time.perf_counter()
    final_text = ""
    partial = ""
    last_tool = ""
    tool_calls = 0
    status = "completed"
    abort_reason = ""  # idle | runtime

    gen = child.run(sub_env).__aiter__()
    try:
        while True:
            # 本轮等待预算 = 空闲超时，且不超过绝对上限剩余
            remaining: float | None = idle_timeout if idle_timeout and idle_timeout > 0 else None
            timeout_reason = "idle"
            if max_runtime and max_runtime > 0:
                left = max_runtime - (time.perf_counter() - started)
                if left <= 0:
                    status, abort_reason = "timeout", "runtime"
                    break
                if remaining is None or left <= remaining:
                    remaining = left
                    timeout_reason = "runtime"

            try:
                if remaining is None:
                    chunk = await gen.__anext__()
                else:
                    chunk = await asyncio.wait_for(gen.__anext__(), timeout=remaining)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                # 事件循环计时器可能略早唤醒；按本轮实际采用的较短预算分类，
                # 不再用事后 elapsed 容差猜测是 idle 还是 runtime。
                abort_reason = timeout_reason
                status = "timeout"
                break

            # —— 处理 chunk；每个 chunk 自然重置下一轮 idle 计时 ——
            if chunk.kind == "final":
                final_text = chunk.body.get("text", "")
            elif chunk.kind == "delta":
                t = chunk.body.get("text", "")
                if t:
                    partial += t
                    if len(partial) > _PARTIAL_CAP:
                        partial = partial[-_PARTIAL_CAP:]
            elif chunk.kind == "tool" and chunk.body.get("phase") == "start":
                tool_calls += 1
                last_tool = chunk.body.get("name", "") or last_tool
            elif chunk.kind == "error":
                status = "error"
                final_text = "子智能体执行失败"
                break
            if progress_callback is not None:
                progress_callback({
                    "tool_calls": tool_calls,
                    "last_tool": last_tool,
                    "partial_output": _safe_subagent_text(
                        final_text or partial, limit=_PARTIAL_TAIL
                    ),
                    "last_chunk": chunk.kind,
                })
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 单个子任务异常不连累其他并行任务
        status = "error"
        final_text = "子智能体执行失败：内部错误"
        log.error("子智能体 %s 执行异常：%s", label, type(exc).__name__)
    finally:
        try:
            await gen.aclose()
        except Exception:  # noqa: BLE001
            pass
        if status == "timeout":
            interrupt_fn = getattr(child, "interrupt", None)
            if callable(interrupt_fn):
                interrupt_fn("子任务超时")
        close_fn = getattr(child, "aclose", None)
        if callable(close_fn):
            try:
                await close_fn()
            except Exception:  # noqa: BLE001 - 子 Agent 清理失败不覆盖原执行结果
                log.exception("关闭子智能体 %s 的 owned 资源失败", label)
        if active is not None:
            active.unregister(parent_session_id, child_id)

    summary = _build_summary(status, final_text, partial, tool_calls, last_tool,
                             abort_reason, started, idle_timeout, max_runtime)
    if status == "timeout":
        log.warning("子智能体 %s 中止（%s）：%d 次工具调用，最后工具=%s",
                    label, abort_reason, tool_calls, last_tool or "-")

    return {
        "agent": label,
        "status": status,
        "summary": _safe_subagent_text(summary),
        "duration_seconds": round(time.perf_counter() - started, 2),
        "tool_calls": tool_calls,
        "last_tool": last_tool,
        "content_trust": "untrusted",
        "content_source": "subagent",
    }


def _build_summary(
    status: str, final_text: str, partial: str, tool_calls: int, last_tool: str,
    abort_reason: str, started: float, idle_timeout: float, max_runtime: float,
) -> str:
    """成功直接返回最终文本；中止/出错附诊断 + 部分输出尾部。"""
    if status == "completed":
        return _safe_subagent_text(final_text)
    if status == "timeout":
        elapsed = time.perf_counter() - started
        why = "无活动超时" if abort_reason == "idle" else "达到运行上限"
        diag = f"子任务于 {elapsed:.0f}s {why}中止：已执行 {tool_calls} 次工具调用"
        if last_tool:
            diag += f"（最后工具：{last_tool}）"
    else:  # error
        diag = final_text or "子智能体执行出错"
    tail = _safe_subagent_text(final_text or partial, limit=_PARTIAL_TAIL).strip()
    safe_diag = _safe_subagent_text(diag)
    if tail and status != "completed" and tail != safe_diag:
        return f"{safe_diag}\n部分输出：\n{tail}"
    return safe_diag


async def _run_children(
    specs: list[dict[str, Any]],
    *,
    build_child: BuildChild,
    max_concurrent: int,
    active: ActiveSubagents | None,
    idle_timeout: float,
    max_runtime: float,
) -> str:
    """并发跑多个子 agent，结构化汇总（JSON）。specs 每项含 label/goal_text/spec。"""
    parent_session_id = current_session_id.get() or ""
    sem = asyncio.Semaphore(max(1, max_concurrent))

    async def _guarded(item: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            return await _run_one_child(
                label=item["label"],
                spec=item["spec"],
                goal_text=item["goal_text"],
                build_child=build_child,
                parent_session_id=parent_session_id,
                active=active,
                idle_timeout=idle_timeout,
                max_runtime=max_runtime,
            )

    results = await asyncio.gather(*(_guarded(item) for item in specs))
    return tool_result({"results": results})


async def _run_background(
    *,
    task_id: str,
    item: dict[str, Any],
    parent_session_id: str,
    build_child: BuildChild,
    active: ActiveSubagents | None,
    idle_timeout: float,
    max_runtime: float,
    tasks: "TaskManager",
    done_event: asyncio.Event,
    on_done: Callable[[str, dict[str, Any]], None] | None,
) -> None:
    """后台跑一个子 agent：完成后落任务看板 + 回调通知。fire-and-forget。"""
    result: dict[str, Any]
    try:
        result = await _run_one_child(
            label=item["label"],
            spec=item["spec"],
            goal_text=item["goal_text"],
            build_child=build_child,
            parent_session_id=parent_session_id,
            active=active,
            idle_timeout=idle_timeout,
            max_runtime=max_runtime,
            progress_callback=(
                lambda progress: tasks.touch_activity(task_id, progress)
                if hasattr(tasks, "touch_activity")
                else None
            ),
        )
    except asyncio.CancelledError:
        result = {"agent": item["label"], "status": "cancelled", "summary": "已取消",
                  "duration_seconds": 0, "tool_calls": 0}
        raise
    except Exception as exc:  # noqa: BLE001
        result = {"agent": item["label"], "status": "error",
                  "summary": "子智能体执行失败：内部错误", "duration_seconds": 0,
                  "tool_calls": 0, "content_trust": "untrusted",
                  "content_source": "subagent"}
        log.error("后台子智能体 %s 执行异常：%s", item["label"], type(exc).__name__)
    finally:
        # 落任务看板（result 存结构化 JSON）
        try:
            tasks.update_status(
                task_id,
                _STATUS_TO_TASK.get(result["status"], "failed"),
                tool_result(result),
            )
        except Exception:  # noqa: BLE001
            log.exception("后台子任务落库失败 task_id=%s", task_id)
        done_event.set()

    # 完成回调：由 app 决定推送 WS + 入队到下一轮主 agent 上下文（best-effort）
    if on_done is not None and parent_session_id:
        try:
            result["task_id"] = task_id
            on_done(parent_session_id, result)
        except Exception:  # noqa: BLE001
            log.debug("后台子任务完成回调失败 task_id=%s", task_id)


def register_subagent_tools(
    registry: Registry,
    sub_registry: SubagentRegistry,
    build_child: BuildChild,
    *,
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
    on_collected: Callable[[str, str], None] | None = None,
) -> None:
    """注册 delegate_task / run_agent / collect_subagent 工具到 toolset='subagent'。

    后台异步（run_agent 的 run_in_background / frontmatter background）需要 tasks +
    launch_background；未提供时自动降级为同步执行。
    """
    # 后台任务的完成事件表：collect(wait=true) 据此阻塞等待
    bg_events: dict[str, asyncio.Event] = {}

    def _launch_one_bg(
        *,
        item: dict[str, Any],
        goal_text: str,
        agent_label: str,
    ) -> str:
        """把单个子 agent 起飞为后台任务，立即返回 launched。

        run_agent（预设）与 delegate_task（临时委派）共用此路径，避免两套后台管道。
        调用方需先判 can_bg（tasks / launch_background 就绪）。
        """
        if background_capacity is not None and not background_capacity():
            return tool_result({
                "status": "rejected",
                "reason": "后台子任务已达并发上限，请先用 collect_subagent 取回已完成的，或稍后再试",
            })
        # 优先用 member 子会话隔离（team 场景），主 agent 回退 current_session_id
        parent_session_id = (
            current_subagent_notify_session.get() or current_session_id.get() or ""
        )
        create_runtime = getattr(tasks, "create_runtime", None)
        if callable(create_runtime):
            task = create_runtime(
                kind="subagent",
                session_id=parent_session_id or "subagent",
                parent_task_id=current_parent_task_id.get(),
                title=goal_text[:40],
                detail=goal_text,
                assignee=agent_label,
                inactivity_timeout=idle_timeout,
                execution_timeout=max_runtime,
                backgrounded=True,
                owner_account_id=current_owner_account_id.get(),
            )
            tasks.mark_running(task["task_id"])
        else:
            task = tasks.create(
                parent_session_id or "subagent",
                title=goal_text[:40],
                detail=goal_text,
                assignee=agent_label,
                owner_account_id=current_owner_account_id.get(),
            )
        task_id = task["id"]
        done_event = asyncio.Event()
        bg_events[task_id] = done_event
        launch_background(_run_background(
            task_id=task_id,
            item=item,
            parent_session_id=parent_session_id,
            build_child=build_child,
            active=active,
            idle_timeout=idle_timeout,
            max_runtime=max_runtime,
            tasks=tasks,
            done_event=done_event,
            on_done=on_background_done,
        ))
        return tool_result({
            "status": "launched",
            "task_id": task_id,
            "agent": agent_label,
            "hint": "用 collect_subagent(task_id) 取结果；完成时也会推送通知",
        })

    # ---- delegate_task：自定义临时委派 ----
    async def handle_delegate_task(args: dict[str, Any]) -> str:
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
        if want_bg and tasks is not None and launch_background is not None:
            if len(specs) > 1:
                raise ToolError(
                    "后台模式不支持批量任务：请逐个委派（每次一个 goal + run_in_background），"
                    "或去掉 run_in_background 走同步并行。"
                )
            item = dict(specs[0])
            # 后台通知里展示的 agent 名用 goal 摘要，比默认的 task#0 更可读
            item["label"] = (item["goal_text"] or "")[:40] or item["label"]
            return _launch_one_bg(
                item=item,
                goal_text=item["goal_text"],
                agent_label=item["label"],
            )
        return await _run_children(
            specs,
            build_child=build_child,
            max_concurrent=max_concurrent,
            active=active,
            idle_timeout=idle_timeout,
            max_runtime=max_runtime,
        )

    registry.register(
        name="delegate_task",
        toolset=SUBAGENT_TOOLSET,
        schema=build_delegate_task_schema(
            max_concurrent=max_concurrent,
            max_tasks=max_tasks,
        ),
        handler=handle_delegate_task,
        is_async=True,
        display_name="委派子任务",
        ui_label_template="委派子任务 {title}",
        should_defer=True,
        search_hint="delegate subagent parallel task research review isolated context",
    )

    # ---- run_agent：调用预设 agent ----
    async def handle_run_agent(args: dict[str, Any]) -> str:
        agent_type = str(args.get("agent_type") or "").strip()
        goal = str(args.get("goal") or "").strip()
        if not agent_type:
            return tool_error("agent_type is required")
        if not goal:
            return tool_error("goal is required")
        definition = sub_registry.get(agent_type)
        if definition is None:
            return tool_error(
                f"Unknown preset subagent: {agent_type}. Available: {sub_registry.names()}"
            )

        # Wiki 页面等持久化预设会话也复用同一规格构建函数。
        spec = build_preset_spec(
            definition,
            model_override=str(args.get("model") or ""),
        )
        if not spec["system_prompt"]:
            spec["system_prompt"] = _EPHEMERAL_PROMPT
        item = {
            "label": agent_type,
            "goal_text": _compose_goal(goal, args.get("context")),
            "spec": spec,
        }

        # 后台异步：run_in_background 参数 或 预设 frontmatter background=true
        want_bg = bool(args.get("run_in_background")) or definition.background
        if want_bg and tasks is not None and launch_background is not None:
            return _launch_one_bg(
                item=item,
                goal_text=item["goal_text"],
                agent_label=item["label"],
            )

        return await _run_children(
            [item],
            build_child=build_child,
            max_concurrent=max_concurrent,
            active=active,
            idle_timeout=idle_timeout,
            max_runtime=max_runtime,
        )

    # ---- collect_subagent：取回后台子任务结果 ----
    async def handle_collect(args: dict[str, Any]) -> str:
        if tasks is None:
            return tool_error("Background subagent support is not enabled")
        task_id = str(args.get("task_id") or "").strip()
        if not task_id:
            return tool_error("task_id is required")
        try:
            task = tasks.get(task_id, owner_account_id=current_owner_account_id.get())
        except KeyError:
            return tool_error(f"Task not found: {task_id}")

        def _consume(t: dict[str, Any]) -> str:
            # 取走完成结果后，从「下一轮自动注入」队列移除，避免重复消费
            bg_events.pop(task_id, None)
            if on_collected is not None:
                on_collected(
                    str(t.get("session_id") or ""),
                    task_id,
                    str(t.get("owner_account_id") or current_owner_account_id.get() or ""),
                )
            return t.get("result") or tool_result({"status": t.get("status")})

        if task.get("status") in ("done", "completed", "failed", "cancelled", "timed_out"):
            return _consume(task)

        # 仍在运行
        if not bool(args.get("wait")):
            return tool_result({"status": "running", "task_id": task_id})

        ev = bg_events.get(task_id)
        if ev is not None:
            try:
                # 子 agent 自身有 idle/max 超时兜底，这里再加缓冲避免永久阻塞
                _cap = max(idle_timeout or 0, max_runtime or 0)
                if _cap > 0:
                    await asyncio.wait_for(ev.wait(), timeout=_cap + 30)
                else:
                    await ev.wait()
            except asyncio.TimeoutError:
                return tool_result({"status": "running", "task_id": task_id,
                                    "note": "等待超时，稍后再 collect"})
        return _consume(tasks.get(task_id, owner_account_id=current_owner_account_id.get()))

    # 无预设子智能体时不注册 run_agent——否则 agent_type 的 enum 为空数组，
    # 模型无合法值可选、部分 provider 也会拒绝空 enum。delegate_task 仍可用。
    if sub_registry.names():
        registry.register(
            name="run_agent",
            toolset=RUN_AGENT_TOOLSET,
            schema=build_run_agent_schema(sub_registry.list()),
            handler=handle_run_agent,
            is_async=True,
            display_name="运行子智能体",
            ui_label_template="运行子智能体 {agent_type}",
            should_defer=True,
            search_hint="run preset subagent specialist background task 运行 预设 子智能体 委派 知识库 wiki",
        )
        # collect 仅在支持后台时才有意义
        if tasks is not None:
            registry.register(
                name="collect_subagent",
                toolset=RUN_AGENT_TOOLSET,
                schema=build_collect_subagent_schema(),
                handler=handle_collect,
                is_async=True,
                display_name="收集子智能体结果",
                ui_label_template="收集子智能体结果 {task_id}",
                should_defer=True,
                search_hint="collect background subagent task result wait status",
            )
    else:
        log.info("无预设子智能体，跳过注册 run_agent（delegate_task 仍可用）")
