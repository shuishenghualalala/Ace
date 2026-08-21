"""单 Agent 运行时：会话编排器。

职责（对照 Hermes run_conversation 的外层）：
  load 历史 -> 追加 user(含附件) -> 记忆预取 -> 构建 system(静态) + reminder(动态)
  -> 委托 AgentExecutor 执行（产出帧透传）-> 保存会话 -> 记忆写入 -> 会话标题

「具体怎么执行 agent」交给可插拔的 AgentExecutor（见 crew/agent/executor.py）：
默认 BuiltinExecutor（手搓循环），也可换成开源/闭源 agent 的外部执行器。

Leader/Teammate 复用本类，仅通过 system_prompt + tool_filter 区分能力。
"""

from __future__ import annotations

import asyncio
import base64
import time
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from crew.core.envelope import Envelope, ResponseChunk
from crew.core.runctx import (
    current_active_skill_packages,
    current_agent_workdir,
    current_agent_id,
    current_owner_account_id,
    current_session_id,
    current_session_source,
    current_skill_scope,
    current_subagent_notify_session,
    current_user_type,
    current_workspace_id,
)
from crew.core.interfaces import (
    Agent,
    LLMProvider,
    MemoryProvider,
    SessionStore,
    ToolRegistry,
)
from crew.core.types import Message, tool_arguments_for_history
from crew.agent.auxiliary import generate_session_title
from crew.agent.compact import ContextCompactor
from crew.agent.executor import AgentExecutor, BuiltinExecutor, ExecutionContext
from crew.tools.policy import ToolDisclosureMode
from crew.tools.file_utils import read_verified_bytes, stat_verified_file
from crew.tools.redact import safe_public_error
from crew.agent.loop.control import TurnControl
from crew.agent.plan import get_plan_mode_attachment_messages
from crew.wiki.attachments import get_wiki_agent_attachment_messages
from crew.agent.prompt_builder import DEFAULT_AGENT_IDENTITY, build_prompt_parts
from crew.gateway.context import REFERENCE_INJECTORS
from crew.gateway.session_context import (
    SessionContext,
    SessionSource,
    build_session_context_prompt,
    session_context_from_envelope,
)
from crew.plugins.manager import PluginManager, TerminalOutcome
from crew.state.logging import get_logger, llm_trace
from crew.state.home import external_session_workspace_path, task_workspace_path, safe_path_segment

log = get_logger("agent")


def _session_source_for_run(envelope: Envelope) -> dict[str, Any]:
    """Return a serializable source snapshot for tools invoked in this turn."""
    ctx_raw = envelope.params.get("session_context")
    if isinstance(ctx_raw, SessionContext):
        return ctx_raw.source.to_dict()
    if isinstance(ctx_raw, dict):
        try:
            raw_source = ctx_raw.get("source", ctx_raw)
            return SessionSource.from_dict(raw_source).to_dict()
        except Exception:  # noqa: BLE001
            pass
    return session_context_from_envelope(envelope, []).source.to_dict()


# ---------------------------------------------------------------------------
# 附件文件内容读取
# ---------------------------------------------------------------------------

# 文本文件扩展名集合
_TEXT_EXTS = {
    ".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".json",
    ".yaml", ".yml", ".toml", ".csv", ".html", ".css", ".scss",
    ".sh", ".bash", ".zsh", ".sql", ".xml", ".ini", ".cfg",
    ".go", ".rs", ".java", ".c", ".cpp", ".h", ".hpp",
    ".log", ".env", ".gitignore", ".dockerignore",
    ".rb", ".php", ".swift", ".kt", ".scala", ".lua",
    ".r", ".R", ".m", ".mm", ".pl", ".pm",
}

# 图像文件扩展名 → MIME 类型映射（用于构建 base64 data URL）
_IMAGE_MIME: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def _read_image_as_data_url(path: str) -> str | None:
    """读取图像文件并编码为 base64 data URL。失败返回 None。"""
    from pathlib import Path

    p = Path(path)
    if not p.is_file():
        return None
    ext = p.suffix.lower()
    mime = _IMAGE_MIME.get(ext)
    if mime is None:
        return None
    try:
        data = read_verified_bytes(p, max_bytes=20 * 1024 * 1024)
        b64 = base64.b64encode(data).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except Exception:
        return None


def _read_attachment(path: str) -> str:
    """读取附件文件内容，返回文本。非文本文件返回摘要信息（含完整路径）。"""
    from pathlib import Path

    p = Path(path)
    if not p.is_file():
        return f"[文件不存在: {path}]"

    ext = p.suffix.lower()
    if ext in _TEXT_EXTS:
        try:
            return read_verified_bytes(p, max_bytes=8 * 1024 * 1024).decode(
                "utf-8", errors="replace"
            )[:8000]
        except Exception as exc:
            return f"[读取失败: {safe_public_error(exc, '读取失败')}]"

    # xlsx/docx 等二进制文件：返回摘要（含完整路径，方便 Agent 用工具进一步读取）
    size = stat_verified_file(p).st_size
    return f"[二进制文件: {p.name}, 大小: {size} 字节, 类型: {ext or '未知'}, 完整路径: {path}]"


def _format_browser_tab_references(refs: object) -> str:
    """把 @browser_tab 引用的标签页正文格式化为可注入上下文的块。"""
    if not isinstance(refs, list) or not refs:
        return ""
    lines = [
        "# 用户引用的浏览器标签页",
        "用户在消息中通过 @browser_tab 显式引用了以下标签页，正文为发送时的只读快照：",
    ]
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        tab_id = str(ref.get("tab_id") or "")
        error = str(ref.get("error") or "").strip()
        if error:
            lines.append(f"\n## 标签页 {tab_id}\n（浏览器标签页内容不可用：{error}）")
            continue
        title = str(ref.get("title") or "").strip() or "(无标题)"
        url = str(ref.get("url") or "").strip()
        text = str(ref.get("text") or "").strip() or "(页面正文为空)"
        header = f"\n## {title}"
        if url:
            header += f"\nURL: {url}"
        lines.append(f"{header}\n{text}")
    return "\n".join(lines)


def _format_subagent_notifications(pending: object) -> str:
    """把后台子 agent 的完成结果格式化为可注入上下文的 system-reminder 块。"""
    if not isinstance(pending, list) or not pending:
        return ""
    lines = [
        "# 后台子任务完成通知",
        "以下后台子智能体（你之前用 run_agent / delegate_task 的 run_in_background 启动）已完成，结果如下：",
    ]
    for r in pending:
        if not isinstance(r, dict):
            continue
        agent = r.get("agent", "子智能体")
        status = r.get("status", "")
        dur = r.get("duration_seconds", "")
        summary = str(r.get("summary", "")).strip()
        lines.append(f"\n## [{agent}] status={status} 用时={dur}s\n{summary}")
    return "\n".join(lines)


def _format_process_notifications(pending: object) -> str:
    """把后台进程的 watch/完成通知格式化为可注入上下文的 system-reminder 块。"""
    if not isinstance(pending, list) or not pending:
        return ""
    from crew.tools.process_registry import format_process_notification

    lines = [
        "# 后台进程通知",
        "以下后台进程（你之前用 terminal(background=true) 启动）有新动态：",
    ]
    for evt in pending:
        if not isinstance(evt, dict):
            continue
        text = format_process_notification(evt)
        if text:
            lines.append(f"\n{text}")
    return "\n".join(lines)


def _format_task_notifications(pending: object) -> str:
    if not isinstance(pending, list) or not pending:
        return ""
    lines = [
        "# 后台任务完成通知",
        "以下后台任务已结束。请读取结果，判断是否继续原任务、修复失败或向用户汇报。",
    ]
    for task in pending:
        if not isinstance(task, dict):
            continue
        lines.append(
            "\n## "
            f"{task.get('task_id', '')} kind={task.get('kind', '')} status={task.get('status', '')}\n"
            f"{task.get('result') or task.get('error') or '(无结果)'}"
        )
    return "\n".join(lines)


def _format_client_intent(envelope: Envelope) -> str:
    """把前端会话调度意图格式化为仅供模型读取的本轮提醒。"""
    intent = str(envelope.params.get("client_intent") or "").strip()
    if intent != "revision":
        return ""
    return (
        "【修订式中断】当前用户消息是在上一条回复生成期间由用户点击“引导/steer”提升而来。"
        "上一条回复可能是不完整草稿，也可能被用户补充、否决或要求改写。"
        "请结合会话历史中刚刚保存的上一条 assistant 回复和当前用户消息，输出修订后的最终答案；"
        "不要单独解释中断机制。"
    )


class SingleAgent(Agent):
    """会话编排器：拼上下文 + 委托 executor + 落库/记忆/标题。"""

    def __init__(
        self,
        provider: LLMProvider,
        registry: ToolRegistry,
        session_store: SessionStore,
        memory: MemoryProvider,
        plugins: PluginManager,
        *,
        system_prompt: str = DEFAULT_AGENT_IDENTITY,
        tool_filter: list[str] | None = None,
        max_iterations: int = 20,
        executor: AgentExecutor | None = None,
        compactor: ContextCompactor | None = None,
        enable_title: bool = False,
        user_type: str = "internal",
        profile_path: str | None = None,
        lightweight: bool = False,
        plan_manager: Any = None,
        wiki_manager: Any = None,
        tool_disclosure_mode: ToolDisclosureMode = ToolDisclosureMode.PROGRESSIVE,
        agent_id: str = "default",
        enabled_skills: list[str] | None = None,
        disabled_skills: list[str] | None = None,
        inject_skills: bool = False,
        include_optional_skills: bool = False,
        model_fallback_notice: str | None = None,
        owned_providers: list[LLMProvider] | None = None,
        model_capabilities: list[str] | None = None,
        evolution_manager: Any = None,
        evolution_full_cycle: bool = False,
        evolution_visible: bool = False,
        evolution_queue: Any = None,
        subagent_drain_fn: Callable[[str, str], list] | None = None,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.session_store = session_store
        self.memory = memory
        self.plugins = plugins
        self.system_prompt = system_prompt
        self.tool_filter = tool_filter  # None=全部工具；否则只暴露子集
        self.max_iterations = max_iterations
        # 未注入 executor 时默认走自带循环，保持向后兼容（Team 即走此路径）
        self.executor = executor or BuiltinExecutor(
            provider, registry, plugins, max_iterations=max_iterations
        )
        self.compactor = compactor
        self.enable_title = enable_title
        self.user_type = user_type
        self.profile_path = profile_path
        self.enabled_skills = enabled_skills
        self.disabled_skills = disabled_skills
        # 是否向 lightweight 子 agent 注入 skills 索引。主 agent 走非 lightweight 路径
        # 总会注入；子 agent 默认 False（保持现状），仅 delegate_task 继承技能时置 True。
        self.inject_skills = inject_skills
        self.include_optional_skills = include_optional_skills
        # Plan 模式管理器（仅主 agent 注入；子 agent / Team 为 None）。
        self.plan_manager = plan_manager
        # Wiki Agent 会话管理器（仅 Wiki 预设注入）。
        self.wiki_manager = wiki_manager
        # 工具授权与披露分离：Wiki 使用 direct，其余默认 progressive。
        self.tool_disclosure_mode = ToolDisclosureMode(tool_disclosure_mode)
        self.agent_id = safe_path_segment(agent_id, "default")
        # team member 派活前 drain 自己后台通知的回调（仅 team member 注入；主/子 agent 为 None）
        self.subagent_drain_fn = subagent_drain_fn
        # 装配期会话模型回退说明：首轮 run 推 status，避免用户只看 UI 绑定误以为已切换成功。
        self.model_fallback_notice = str(model_fallback_notice or "").strip() or None
        self.model_capabilities = (
            tuple(str(item).strip().lower() for item in model_capabilities if str(item).strip())
            if model_capabilities is not None
            else None
        )
        # 轻量模式（子 agent）：跳过全局 SOUL/MEMORY/USER/上下文文件/skills 注入，
        # 对照 Hermes 子 agent 的 skip_memory / skip_context_files。
        self.lightweight = lightweight
        # 本 Agent 的可控性句柄（steer / interrupt）。AgentManager 按 session 缓存 Agent，
        # 故一个实例对应一个 session；gateway 经 CrewApp.steer/interrupt 路由到这里。
        self.control = TurnControl()
        # 后台标题生成任务引用集合：防止 fire-and-forget task 被 GC 中断，
        # done 后自动清出。标题生成不得阻塞 final 帧发送（见 _spawn_title_task）。
        self._title_tasks: set[asyncio.Task] = set()
        # 同一 (owner, title_sid) 在途标题任务去重，避免 early + 回合末 fallback 双发 LLM。
        self._title_inflight: set[tuple[str, str]] = set()
        # Provider ownership is declared by the composition root. Executor/compactor and
        # ``self.provider`` may all reference borrowed App resources, so references alone
        # must never imply ownership.
        self._owned_providers = list(owned_providers or [])
        self._close_lock = asyncio.Lock()
        self._closed = False
        # Skill 自进化管理器（仅主 agent 注入；子 agent / Team 为 None）。
        self.evolution_manager = evolution_manager
        # 是否在每轮结束后跑完整进化流程（extract→optimize→generate）。
        self.evolution_full_cycle = evolution_full_cycle
        # 是否在回复末尾 yield evolution_footer chunk，让前端渲染自进化状态卡片。
        self.evolution_visible = evolution_visible
        # 进化队列（EvolutionQueue 实例），由 App 级别持有，跨 agent 共享。
        self.evolution_queue = evolution_queue
        # 后台进化任务引用集合：防止 fire-and-forget task 被 GC 中断。
        self._evolution_bg_tasks: set[asyncio.Task] = set()

    async def aclose(self) -> None:
        """Close explicitly owned providers once without touching borrowed resources.

        Providers are deduplicated by object identity because a primary/fallback instance
        may also be referenced by the executor or compactor. One failed close is logged and
        does not prevent remaining resources from being released.
        """
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True

            # Title generation is fire-and-forget but still borrows the same Provider.
            # Cancel and join it before closing owned clients so eviction/model switching
            # cannot race a late title request.
            title_tasks = {task for task in self._title_tasks if not task.done()}
            for task in title_tasks:
                task.cancel()
            if title_tasks:
                await asyncio.gather(*title_tasks, return_exceptions=True)

            seen: set[int] = set()
            close_failures: list[Exception] = []
            for provider in self._owned_providers:
                identity = id(provider)
                if identity in seen:
                    continue
                seen.add(identity)
                close = getattr(provider, "aclose", None)
                if not callable(close):
                    continue
                try:
                    await close()
                except Exception as exc:  # noqa: BLE001 - release siblings first
                    close_failures.append(exc)
                    log.error(
                        "关闭 Agent-owned Provider 失败 provider=%s error_type=%s",
                        type(provider).__name__,
                        type(exc).__name__,
                    )
            if close_failures:
                raise RuntimeError("Agent-owned Provider close failed") from close_failures[0]

    # ---- 可控性入口（供 CrewApp.steer/interrupt 调用）----
    def steer(self, text: str) -> bool:
        """注入补充指令到运行中的本轮对话。"""
        return self.control.steer(text)

    def interrupt(self, message: str | None = None) -> None:
        """请求在下一个安全点优雅中断本轮对话。"""
        self.control.interrupt(message)

    def _build_user_text(
        self, envelope: Envelope
    ) -> tuple[str, list[str], list[dict[str, Any]] | None]:
        """拼接用户文本（含附件引用），返回 (user_text, 附件内容块列表, 多模态 content_parts)。

        当附件中包含图像文件时，自动编码为 base64 data URL 并构建
        OpenAI Vision 格式的 content_parts，供 Message.to_openai() 使用。
        """
        user_text = envelope.query
        att_contents: list[str] = []
        content_parts: list[dict[str, Any]] | None = None

        if envelope.attachments:
            att_descriptions: list[str] = []
            image_parts: list[dict[str, Any]] = []

            for att in envelope.attachments:
                name = att.get("name", "未知文件")
                path = att.get("path", "")
                content = att.get("content", "")
                att_type = att.get("type", "")
                att_descriptions.append(f"附件「{name}」位于: {path}")

                # 图像附件：编码为 base64 data URL
                if att_type == "image" or (path and Path(path).suffix.lower() in _IMAGE_MIME):
                    data_url = _read_image_as_data_url(path) if path else None
                    if data_url:
                        image_parts.append({
                            "type": "image_url",
                            "image_url": {"url": data_url, "detail": "auto"},
                        })
                        continue  # 图像不再作为文本附件注入

                # 非图像附件：按原逻辑读取文本内容
                if not content and path:
                    content = _read_attachment(path)
                if content:
                    att_contents.append(f"### 附件: {name} (路径: {path})\n```\n{content}\n```")

            if att_descriptions:
                user_text = "\n".join(att_descriptions) + "\n\n" + user_text

            # 构建 OpenAI Vision 多模态 content_parts
            if image_parts:
                content_parts = [{"type": "text", "text": user_text}]
                content_parts.extend(image_parts)

        return user_text, att_contents, content_parts

    async def _build_prompts(
        self, envelope: Envelope, att_contents: list[str], cwd: str | None = None,
        *, task_sid: str | None = None,
    ) -> tuple[str, str]:
        """组装 system_static + user_reminder 两部分。

        Returns:
            (system_static, user_reminder_content)
            - system_static: 几乎不变的静态 prompt，可走 KV Cache
            - user_reminder_content: 每轮可能变的动态内容，通过 <system-reminder> 注入
        """
        # task_sid：网关 sidechain 路径下的稳定主会话 id（无 sidechain 时退化为 session_id）。
        # memory 必须按稳定 id 召回，否则会落到 ::turn:: 临时 id 下对不上。
        mem_sid = task_sid or envelope.session_id
        memory_text = "" if self.lightweight else await self.memory.prefetch(
            mem_sid, envelope.query
        )
        ws_instructions = envelope.params.get("workspace_instructions", "")
        prompt_parts = build_prompt_parts(
            workspace_instructions=ws_instructions,
            memory_text=memory_text,
            cwd=cwd,
            profile_path=self.profile_path,
            lightweight=self.lightweight,
            enabled_skills=self.enabled_skills,
            disabled_skills=self.disabled_skills,
            inject_skills=self.inject_skills,
            include_optional_skills=self.include_optional_skills,
            user_type=self.user_type,
        )

        system_static = prompt_parts["system_static"]
        # 自定义 system_prompt 追加到静态部分末尾，使其成为最靠近用户问题的身份指令，
        # 从而覆盖 SOUL.md 中的通用 Crew 身份。
        if self.system_prompt and self.system_prompt != DEFAULT_AGENT_IDENTITY:
            system_static = f"{system_static}\n\n{self.system_prompt}"

        # 会话来源上下文（B1）：gateway 经 envelope.params["session_context"] 注入
        ctx_raw = envelope.params.get("session_context")
        if isinstance(ctx_raw, SessionContext):
            ctx_prompt = build_session_context_prompt(ctx_raw, workspace_path=cwd)
            system_static = f"{system_static}\n\n{ctx_prompt}"
        elif isinstance(ctx_raw, dict):
            try:
                from crew.gateway.session_context import SessionSource

                source = SessionSource.from_dict(ctx_raw.get("source", ctx_raw))
                ctx = SessionContext(
                    source=source,
                    connected_platforms=list(ctx_raw.get("connected_platforms") or []),
                    shared_multi_user=bool(ctx_raw.get("shared_multi_user")),
                    session_id=str(ctx_raw.get("session_id") or envelope.session_id),
                    workspace_id=str(ctx_raw.get("workspace_id") or envelope.workspace_id),
                )
                system_static = f"{system_static}\n\n{build_session_context_prompt(ctx, workspace_path=cwd)}"
            except Exception:  # noqa: BLE001
                log.debug("session_context 解析失败，跳过注入")

        # 动态内容 + 附件
        reminder_parts = [prompt_parts["user_reminder"]]
        # 渠道注入的系统级能力提示（如 IM 渠道的 [FILE:路径] 发文件约定）。
        # 通过 system-reminder 注入 LLM，不写入 canonical history，避免泄露到对话历史。
        channel_hint = envelope.params.get("channel_system_hint")
        if channel_hint:
            reminder_parts.append(str(channel_hint))
        if att_contents:
            reminder_parts.extend(att_contents)
        wiki_confirmation_id = str(envelope.params.get("wiki_confirmation_id") or "").strip()
        if wiki_confirmation_id:
            reminder_parts.append(
                "【Wiki 确认回合】用户已通过确认卡确认本次操作。"
                f"一次性 confirmation_id={wiki_confirmation_id}。"
                "仅将此 ID 传给上一回合对应的 Wiki 执行工具，不得改动目标或参数。"
            )
        # 定时任务触发轮：明确告知"此刻正在执行定时任务"，否则 agent 只从历史看到
        # "有个任务定在某时刻"、却不知道当前这轮就是它到点触发（query 含糊时尤其会
        # 反问"是你提前来问还是任务触发了"）。配合上面 reminder 里的当前时间一起兜底。
        if getattr(envelope, "channel", "") == "cron":
            job_name = str(envelope.params.get("cron_job_name") or "").strip()
            name_part = f"「{job_name}」" if job_name else ""
            reminder_parts.append(
                f"【定时任务触发】你之前创建的定时任务{name_part}现在已到点执行。"
                "请直接完成下面这条任务内容、并输出要发送给用户的话——"
                "不要询问是否到时间、不要反问是不是用户提前来问、也不要解释这是定时任务。"
            )
        # 后台子 agent 完成通知：自动注入本轮上下文（无需模型主动 collect）
        bg_block = _format_subagent_notifications(envelope.params.get("subagent_notifications"))
        if bg_block:
            reminder_parts.append(bg_block)
        # 后台进程 watch/完成通知：同样自动注入本轮上下文
        proc_block = _format_process_notifications(envelope.params.get("process_notifications"))
        if proc_block:
            reminder_parts.append(proc_block)
        task_block = _format_task_notifications(envelope.params.get("task_notifications"))
        if task_block:
            reminder_parts.append(task_block)
        # 对话 @引用：发送时解析注入的正文快照块（含失败占位），按注册表顺序拼接
        for injector in REFERENCE_INJECTORS:
            if injector.formatter is None:
                continue
            ref_block = injector.formatter(envelope.params.get(injector.params_key))
            if ref_block:
                reminder_parts.append(ref_block)
        client_intent_block = _format_client_intent(envelope)
        if client_intent_block:
            reminder_parts.append(client_intent_block)
        # Plan 模式：当前待办快照与审批通过执行提示仍是动态 reminder；
        # Plan workflow / revision / exit / todo_reminder 等事件走 hidden attachment。
        if self.plan_manager is not None:
            reminder_parts.extend(self._plan_reminder_blocks(mem_sid, owner_account_id=envelope.user_id))
        user_reminder = "\n\n".join(p for p in reminder_parts if p)

        return system_static, user_reminder

    def _plan_reminder_blocks(self, session_id: str, owner_account_id: str = "") -> list[str]:
        """Plan 模式相关的动态 reminder 块（前置注入部分）。

        注意：plan 工作流与审批/修订/退出事件不在这里——它们由 hidden attachment
        写入 canonical history。这里只放当前 todo 状态与审批通过执行提示。
        """
        from crew.agent.plan import (
            PLAN_APPROVED_REMINDER,
            format_approved_plan_content,
            plan_display_path,
            read_plan,
        )

        blocks: list[str] = []
        pm = self.plan_manager
        if not pm.is_active(session_id, owner_account_id=owner_account_id) and pm.take_just_approved(
            session_id,
            owner_account_id=owner_account_id,
        ):
            # 注入落盘后的批准正文（含看板手改），避免模型沿用对话里的旧稿；超长截断保 context。
            plan_content = format_approved_plan_content(
                read_plan(session_id, owner_account_id=owner_account_id) or "(plan file empty)"
            )
            blocks.append(
                PLAN_APPROVED_REMINDER.format(
                    plan_file=plan_display_path(session_id, owner_account_id=owner_account_id),
                    plan_content=plan_content,
                )
            )
        todo_block = pm.todo_store(session_id, owner_account_id=owner_account_id).format_for_injection()
        if todo_block:
            blocks.append(todo_block)
        return blocks

    def _effective_tool_filter(self, session_id: str, owner_account_id: str = "") -> list[str] | None:
        """Plan 模式激活时把工具集收窄到只读白名单。

        未激活时剔除 Plan 模式入口/切换工具，避免模型误调用。
        """
        # Plan 模式优先
        if self.plan_manager is not None and self.plan_manager.is_active(
            session_id, owner_account_id=owner_account_id
        ):
            from crew.agent.plan import PLAN_MODE_TOOLS

            if self.tool_filter is None:
                return list(PLAN_MODE_TOOLS)
            allowed = set(self.tool_filter)
            return [t for t in PLAN_MODE_TOOLS if t in allowed]

        # 普通模式：隐藏 Plan 模式入口工具。
        base = self.tool_filter if self.tool_filter is not None else self.registry.names()
        return [t for t in base if t not in {"enter_plan_mode", "exit_plan_mode"}]

    def _resolve_agent_workdir(self, envelope: Envelope, *, task_session_id: str = "") -> str:
        """解析本轮 agent 的文件系统工作目录（Layer 3：backing workspace_id）。

        work_dir = {task_workspace_root}/{workspace_id}/
        同一 workspace 下所有 session/agent 共享此目录，产物统一落这里。
        params.cwd 可临时覆盖（仅 e2e 测试或显式指定场景）。

        专用 Wiki Agent 仍使用 workspace 目录；知识库检索必须显式调用
        wiki_search / wiki_read。
        """
        explicit = str(envelope.params.get("cwd") or "").strip()
        if explicit:
            path = Path(explicit).expanduser()
            path.mkdir(parents=True, exist_ok=True)
            return str(path.resolve())
        root = str(envelope.params.get("workspace_root_path") or "").strip()
        if root:
            path = Path(root).expanduser()
            if path.is_dir():
                return str(path.resolve())

        if self.executor.name == "external":
            config = getattr(self.executor, "config", None)
            external_agent_id = str(getattr(config, "external_agent_id", "") or "").strip()
            external_store = getattr(config, "external_store", None)
            stable_session_id = str(task_session_id or envelope.session_id)
            latest_binding = getattr(
                external_store,
                "latest_runtime_session_binding_for_agent",
                None,
            )
            if external_agent_id and callable(latest_binding):
                try:
                    binding = latest_binding(
                        owner_account_id=envelope.user_id,
                        crew_session_id=stable_session_id,
                        external_agent_id=external_agent_id,
                    )
                    legacy_cwd_raw = str((binding or {}).get("cwd") or "").strip()
                    if legacy_cwd_raw:
                        legacy_cwd = Path(legacy_cwd_raw).expanduser()
                        if legacy_cwd.is_dir():
                            return str(legacy_cwd.resolve())
                except Exception:  # noqa: BLE001 - compatibility lookup must not block a turn
                    pass
            if external_agent_id:
                return str(external_session_workspace_path(
                    envelope.workspace_id,
                    stable_session_id,
                    external_agent_id,
                    owner_account_id=envelope.user_id,
                ))

        return str(task_workspace_path(envelope.workspace_id, owner_account_id=envelope.user_id))

    async def run(self, envelope: Envelope) -> AsyncIterator[ResponseChunk]:
        from crew.core.runctx import current_model_id, current_turn_id

        from crew.security.launch import (
            bind_process_launch_task,
            current_process_launch,
        )

        launch = envelope.params.get("_security_process_launch")
        if launch is not None and not str(getattr(launch, "task_id", "") or "").strip():
            launch = bind_process_launch_task(launch, envelope.request_id)
        launch_token = current_process_launch.set(launch)
        turn_token = current_turn_id.set(
            str(envelope.params.get("turn_id") or envelope.request_id or "").strip()
        )
        model_token = current_model_id.set(
            str(getattr(self.provider, "model", "") or "").strip()
        )
        runtime_tool_leases: tuple[tuple[Any, object], ...] = ()
        try:
            activate_runtime_tools = getattr(
                self.registry,
                "activate_runtime_tool_context",
                None,
            )
            if callable(activate_runtime_tools):
                task_sid = str(
                    envelope.params.get("task_session_id") or envelope.session_id
                )
                cwd = self._resolve_agent_workdir(
                    envelope,
                    task_session_id=task_sid,
                )
                runtime_tool_leases = await activate_runtime_tools(
                    process_launch=launch,
                    cwd=cwd,
                )
            turn_stream = self._run_turn(envelope)
            try:
                async for chunk in turn_stream:
                    yield chunk
            finally:
                # 消费者收到 final 后可能立即 break/aclose 外层生成器；若不显式关闭
                # 内层 _run_turn，其 finally（落库 + 标题调度）会被推迟到 GC/事件循环
                # 终结器，硬停场景下带着 CancelledError 执行，既不落库也不生成标题。
                # 在外层 finally 内同步关闭，保证 dev 的“final 后即可关闭流”契约。
                try:
                    await turn_stream.aclose()
                except asyncio.CancelledError:
                    raise
                except BaseException:  # noqa: BLE001 - 内层清理异常不阻断外层释放
                    log.exception("关闭内层 run 流失败 session=%s", envelope.session_id)
        finally:
            try:
                release_runtime_tools = getattr(
                    self.registry,
                    "release_runtime_tool_context",
                    None,
                )
                if runtime_tool_leases and callable(release_runtime_tools):
                    release_task = asyncio.create_task(
                        release_runtime_tools(runtime_tool_leases)
                    )
                    try:
                        await asyncio.shield(release_task)
                    except asyncio.CancelledError:
                        await asyncio.shield(release_task)
                        raise
            finally:
                current_process_launch.reset(launch_token)
                current_turn_id.reset(turn_token)
                current_model_id.reset(model_token)

    async def _run_turn(self, envelope: Envelope) -> AsyncIterator[ResponseChunk]:
        if self._closed:
            raise RuntimeError("SingleAgent 已关闭，不能继续执行")
        sid = envelope.session_id
        task_sid = str(envelope.params.get("task_session_id") or sid)
        cwd = self._resolve_agent_workdir(envelope, task_session_id=task_sid)
        # 提前设置运行期会话 id，使 compactor.maybe_compact() 中的 LLM trace 也能带上 session_id
        from crew.core.runctx import (
            current_attachment_files,
            current_attachment_paths,
            current_model_capabilities,
            current_parent_task_id,
            current_request_id,
            current_workspace_guard,
        )

        current_session_id.set(task_sid)
        # team member 执行工具时，后台子 agent 通知按 member 子会话隔离，
        # 使完成通知能回到发起 member；主 agent 该值为空（回退 current_session_id）。
        notify_session = str(envelope.params.get("member_session_id") or "")
        current_subagent_notify_session.set(notify_session)
        # team member 派活前 drain 自己的后台完成通知注入本轮上下文
        #（team 模式下 member.run 不经 app.handle 的 drain；主 agent 无 member_session_id）
        if notify_session and self.subagent_drain_fn is not None:
            _pending = self.subagent_drain_fn(notify_session, envelope.user_id)
            if _pending:
                envelope.params["subagent_notifications"] = _pending
        current_request_id.set(envelope.request_id)
        current_parent_task_id.set(str(envelope.params.get("sidechain_task_id") or ""))
        current_workspace_id.set(envelope.workspace_id)
        current_owner_account_id.set(envelope.user_id)
        # 热刷新当前 owner 的运行期 env：config/.env + owner .env + session.json。
        from crew.state.home import refresh_owner_runtime_env

        refresh_owner_runtime_env(envelope.user_id)
        current_agent_workdir.set(cwd)
        guard = envelope.params.get("workspace_guard")
        current_workspace_guard.set(guard if isinstance(guard, dict) else None)
        current_agent_id.set(str(envelope.params.get("agent_id") or self.agent_id or "default"))
        current_session_source.set(_session_source_for_run(envelope))
        current_attachment_paths.set(tuple(
            str(att.get("path") or "")
            for att in (envelope.attachments or [])
            if isinstance(att, dict) and str(att.get("path") or "").strip()
        ))
        current_attachment_files.set(tuple(
            (str(att.get("path") or ""), str(att.get("name") or ""))
            for att in (envelope.attachments or [])
            if isinstance(att, dict) and str(att.get("path") or "").strip()
        ))
        # 暴露当前 user_type，供子 agent 继承父权限上限（防越权）
        current_user_type.set(self.user_type)
        # 子 Agent 的 ``model=inherit`` 读取父 Agent 实际生效能力；会话绑定模型、
        # owner 模型与全局模型因此走同一条能力约束链。
        current_model_capabilities.set(self.model_capabilities)
        # 同理继承父 Agent 实际生效的 Provider：父会话可能绑定 owner 级模型，
        # 此时 app 级 provider 是无 Key 的 FakeProvider，不能让子 Agent 继承它。
        from crew.core.runctx import current_provider

        current_provider.set(self.provider)
        # 暴露当前生效 skill 范围，供 delegate_task 子 agent 继承父（含 expert）的技能
        current_skill_scope.set((self.enabled_skills, self.disabled_skills))
        # 同步当前已展开的 skill packages，供 build_skills_index_prompt 展开内部 skills
        active_packages = envelope.params.get("active_skill_packages") or []
        current_active_skill_packages.set(set(active_packages))
        # 专用 Wiki Agent 自行建立 KB 状态；普通会话不创建 Wiki 会话状态。
        if (
            self.wiki_manager is not None
            and self.tool_disclosure_mode is ToolDisclosureMode.DIRECT
        ):
            wiki_kb_id = str(envelope.params.get("wiki_kb_id") or "").strip() or "default"
            self.wiki_manager.set_kb_id(task_sid, wiki_kb_id, owner_account_id=envelope.user_id)
        t0 = time.perf_counter()

        # 装配期模型回退：只打日志用户无感，首轮推 status 让 UI「执行过程」可见。
        if self.model_fallback_notice:
            yield ResponseChunk.status_event(envelope.request_id, self.model_fallback_notice)
            self.model_fallback_notice = None

        # 新一轮：重置可控性状态；dispatcher 排队期缓存的 steer 文本（envelope.params
        # ["steer_text"]）在此预置为本轮的首个 steer。
        self.control.reset()
        pre_steer = envelope.params.get("steer_text", "")
        if pre_steer:
            self.control.steer(pre_steer)

        t = time.perf_counter()
        owner = envelope.user_id
        history = self.session_store.load(sid, owner_account_id=owner)
        log.info("[PERF] history_load       %.3fs  (msgs=%d)", time.perf_counter() - t, len(history))
        is_new = not history
        if is_new:
            await self.plugins.on_session_start(
                sid,
                owner_account_id=owner,
                workspace_id=envelope.workspace_id,
                channel=envelope.channel,
                mode=envelope.mode,
            )

        # 1. 用户消息（含附件） + 2. prompt 构建
        #    history 是「canonical 全量历史」，从此定型，只追加不被压缩覆盖。
        turn_started_at = time.time()
        user_text, att_contents, content_parts = self._build_user_text(envelope)
        llm_trace("user", {"session_id": sid, "text": user_text})
        user_message = Message.user(
            user_text,
            is_meta=bool(envelope.params.get("internal_task_resume")),
        )
        if content_parts:
            user_message.content_parts = content_parts
        user_message.timestamp = turn_started_at
        history.append(user_message)

        if is_new and self.enable_title and not self.lightweight:
            # 只落占位标题，让会话立刻出现在列表里；标题生成延后到主响应结束后
            # 由 finally 块调度（见下方 _spawn_title_task 调用），不抢占主推理窗口。
            if not self._session_needs_title(task_sid, owner):
                try:
                    self.session_store.save(
                        task_sid,
                        history,
                        workspace_id=envelope.workspace_id,
                        owner_account_id=owner,
                        title_fallback="",
                    )
                except Exception:  # noqa: BLE001
                    log.debug("创建会话标题占位失败 session=%s", task_sid)

        # Skill 展开内容写入 canonical history（is_meta=True，前端不渲染但模型可见）
        skill_meta = envelope.params.get("skill_meta")
        if skill_meta:
            history.append(Message.user(skill_meta, is_meta=True))

        # OCC-style hidden plan attachments: persist in canonical history so
        # future turns can see prior reminders and throttle by user turns.
        if self.plan_manager is not None:
            history.extend(
                get_plan_mode_attachment_messages(
                    history,
                    task_sid,
                    self.plan_manager,
                    owner_account_id=owner,
                )
            )

        # 专用 Wiki Agent 每轮注入活跃知识库与 KB 列表上下文。
        if self.wiki_manager is not None:
            history.extend(
                get_wiki_agent_attachment_messages(
                    task_sid,
                    self.wiki_manager,
                    owner_account_id=owner,
                )
            )

        t = time.perf_counter()
        system_static, user_reminder = await self._build_prompts(
            envelope, att_contents, cwd, task_sid=task_sid
        )
        log.info("[PERF] build_prompts      %.3fs  (static=%d, reminder=%d)",
                 time.perf_counter() - t, len(system_static), len(user_reminder))

        # 3. 上下文压缩：仅作用于「发给 LLM 的视图」llm_messages，
        #    不破坏 history（旧的详细历史仍完整持久化）。
        llm_messages = list(history)  # 拷贝，避免 executor 追加时污染 canonical
        if self.compactor is not None:
            t = time.perf_counter()
            llm_messages = await self.compactor.maybe_compact(
                llm_messages,
                task_sid,
                owner_account_id=owner,
            )
            log.info("[PERF] compactor          %.3fs", time.perf_counter() - t)

        # 注入动态上下文：通过 <system-reminder> 作为首条 user 消息插入 llm_messages
        # 注意：不写入 canonical history，不影响持久化
        if user_reminder:
            reminder_msg = Message.system_reminder(user_reminder)
            llm_messages = [reminder_msg, *llm_messages]

        # skill 展开内容已在 history 中（is_meta=True），llm_messages 是 history 的拷贝，
        # 所以模型可以看到。无需额外插入。

        prefix_len = len(llm_messages)  # 记录执行前长度，用于回收本轮新增

        log.info(
            "[PERF] pre_llm_setup      %.3fs  total request_id=%s session=%s",
            time.perf_counter() - t0,
            envelope.request_id,
            sid,
        )

        # 4. 组执行上下文，委托 executor（executor 把本轮新消息追加到 llm_messages）
        effective_tool_filter = self._effective_tool_filter(task_sid, owner_account_id=owner)
        authorized_tool_names = frozenset(
            effective_tool_filter if effective_tool_filter is not None else self.registry.names()
        )
        from crew.core.runctx import current_authorized_tool_names

        current_authorized_tool_names.set(authorized_tool_names)
        from crew.agent.skills import skill_activations_from_params

        ctx = ExecutionContext(
            session_id=task_sid,
            request_id=envelope.request_id,
            system_prompt=system_static,
            messages=llm_messages,
            query=user_text,
            attachments=[
                dict(attachment)
                for attachment in (envelope.attachments or [])
                if isinstance(attachment, dict)
            ],
            tool_schemas=self.registry.list_schemas(effective_tool_filter),
            authorized_tool_names=authorized_tool_names,
            enforce_tool_scope=True,
            params=dict(envelope.params),
            active_skills=skill_activations_from_params(envelope.params),
            cwd=cwd,
            max_iterations=self.max_iterations,
            control=self.control,
            tool_disclosure_mode=self.tool_disclosure_mode,
        )
        t_exec = time.perf_counter()
        interrupted = False
        terminal_outcome: TerminalOutcome = "failed"
        terminal_error_summary = "Executor ended without a terminal response"
        # 拦截 final 帧：进化 visible 模式下需在 final 之前 yield evolution_footer，
        # 因此先把 final 暂存，等进化流程跑完再投递。
        _held_final = None
        try:
            async for chunk in self.executor.execute(ctx):
                if chunk.kind == "error" or chunk.status == "failed":
                    terminal_outcome = "failed"
                    terminal_error_summary = str(
                        chunk.body.get("message") or chunk.body.get("text") or "Agent execution failed"
                    )
                elif chunk.kind == "final" or (chunk.is_final and chunk.status == "succeeded"):
                    terminal_outcome = "completed"
                    terminal_error_summary = ""
                    if self.evolution_visible and self.evolution_manager:
                        _held_final = chunk
                        continue
                yield chunk
            if self.control.interrupted:
                interrupted = True
                terminal_outcome = "interrupted"
                terminal_error_summary = ""
        except asyncio.CancelledError:
            # 硬停（dispatcher.stop → task.cancel）：标记中断，仍在 finally 里落库后再抛出，
            # 否则这一轮的 user 消息与工具调用永不持久化——刷新即丢、下一轮无上下文。
            interrupted = True
            terminal_outcome = "interrupted"
            terminal_error_summary = ""
            raise
        except Exception as exc:
            terminal_outcome = "failed"
            terminal_error_summary = (
                f"{type(exc).__name__}: "
                f"{safe_public_error(exc, '执行失败')}"
            )
            raise
        finally:
            log.info("[PERF] executor_total     %.3fs", time.perf_counter() - t_exec)
            # 本轮新增消息回灌 canonical 历史（含尾部悬空 tool_call 清洗）+ 持久化。
            # 无论正常结束、硬停（CancelledError）还是异常，都先把历史落库。
            persisted = False
            try:
                await self._persist_turn(
                    envelope, history, ctx, prefix_len, turn_started_at,
                    interrupted=interrupted, task_sid=task_sid
                )
                persisted = True
            finally:
                try:
                    await self.plugins.on_session_end(
                        task_sid,
                        outcome=terminal_outcome,
                        error_summary=terminal_error_summary,
                    )
                finally:
                    # final 是流式协议终态，合法消费者可能收到后立即 break/aclose。
                    # 标题调度必须在受保护的 finally 内，且位于持久化成功之后；这样既不
                    # 进入主响应关键窗口，也不会因生成器关闭而跳过。这里只创建后台任务，
                    # 不等待标题 LLM，所以 final 的交付仍不受辅助请求阻塞。
                    if (
                        persisted
                        and terminal_outcome == "completed"
                        and not interrupted
                        and self.enable_title
                        and not self.lightweight
                        and self._session_needs_title(task_sid, owner)
                    ):
                        from crew.core.runctx import current_push_fn

                        self._spawn_title_task(
                            task_sid,
                            owner,
                            history,
                            current_push_fn.get(),
                        )
        log.info("[PERF] run_total          %.3fs", time.perf_counter() - t0)

        # ── Skill 自进化 ──────────────────────────────────────────
        # 仅在正常完成（非中断）、主 agent（非 lightweight）且注入了 evolution_manager 时触发。
        if (
            not interrupted
            and terminal_outcome == "completed"
            and not self.lightweight
            and self.evolution_manager is not None
        ):
            if self.evolution_visible:
                # Demo 模式：前台可见地执行，输出状态帧并同步等待结果。
                # 注意：visible 模式下 evolution 在 runtime.run() 内部同步执行，
                # 此时 dispatcher 的 finally 块尚未将 sidechain 历史合并回主会话。
                # 因此必须用 sid（envelope.session_id，即 sidechain_id）加载轨迹，
                # 而非 task_sid（主会话）——_persist_turn 已将完整对话（含 tool_calls）
                # 保存到 envelope.session_id。
                async for chunk in self._run_evolution_visible(
                    sid, owner, envelope.request_id,
                    conversation_id=task_sid,
                ):
                    yield chunk
            elif self.evolution_queue is not None:
                # ── 异步模式：drain 上一轮的 pending evolution 结果，再入队本周任务 ──
                # evolution 在后台按序执行，不阻塞当前轮响应；
                # 结果在下一轮交互的 evolution_footer 中体现。
                pending = self.evolution_queue.drain_results(task_sid)
                for result in pending:
                    yield ResponseChunk.evolution_footer(
                        envelope.request_id,
                        result["text"],
                        evolution_status="done",
                        created_skills=result.get("created_skills"),
                        evolved_skills=result.get("evolved_skills"),
                    )
                # 入队本轮 evolution 任务（fire-and-forget）
                await self.evolution_queue.enqueue(
                    session_id=task_sid,
                    owner_account_id=owner,
                    conversation_id=task_sid,
                    manager=self.evolution_manager,
                    full_cycle=self.evolution_full_cycle,
                )
            else:
                # 兼容模式：无队列时走原有 fire-and-forget 后台任务
                self._trigger_evolution(task_sid, owner, conversation_id=task_sid)
                yield ResponseChunk.evolution_footer(
                    envelope.request_id,
                    "Skill 自进化已启用 · 正在后台分析本轮轨迹",
                    evolution_status="active",
                )

        # 放行拦截的 final chunk；evolution_footer（如有）已在 final 之前 yield，
        # 前端收到 final 后解除 busy。异步模式下 evolution 在后台进行，不阻塞前端。
        if _held_final is not None:
            yield _held_final

    def _trigger_evolution(self, session_id: str, owner_account_id: str, conversation_id: str = "") -> None:
        """在后台 fire-and-forget 触发 evolution 轨迹提取（或完整周期）。

        使用 asyncio 后台任务执行，不阻塞用户响应流；异常仅记录日志。
        conversation_id 为主会话 ID，用于 evolution_log 文件命名。
        """
        mgr = self.evolution_manager
        full = self.evolution_full_cycle

        async def _evolution_task() -> None:
            try:
                if full:
                    await asyncio.to_thread(
                        mgr.run_full_cycle,
                        owner_account_id=owner_account_id,
                        session_id=session_id,
                        auto_create=True,
                        dry_run_optimize=False,
                        conversation_id=conversation_id,
                    )
                    log.info("[EVOLUTION] 完整周期已执行: session=%s owner=%s", session_id, owner_account_id)
                else:
                    log_id = await asyncio.to_thread(
                        mgr.extract_session,
                        session_id,
                        owner_account_id,
                    )
                    if log_id:
                        log.info("[EVOLUTION] 轨迹已提取: session=%s log_id=%s", session_id, log_id)
                    else:
                        log.debug("[EVOLUTION] 无可提取轨迹: session=%s", session_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("[EVOLUTION] 后台触发失败: session=%s err=%s", session_id, exc)

        task = asyncio.create_task(_evolution_task())
        self._evolution_bg_tasks.add(task)
        task.add_done_callback(self._evolution_bg_tasks.discard)

    # 各阶段独立超时（秒），单阶段超时不会阻塞整个会话
    # 按阶段配置：阶段3涉及多次 LLM 调用（语义评估 + 结构化生成），需要更长
    _EVOLUTION_PHASE_TIMEOUT_EXTRACT = 120   # 阶段1：轨迹提取
    _EVOLUTION_PHASE_TIMEOUT_OPTIMIZE = 300  # 阶段2：技能优化
    _EVOLUTION_PHASE_TIMEOUT_GENERATE = 600  # 阶段3：技能生成（多次 LLM 调用）
    # 向后兼容
    _EVOLUTION_PHASE_TIMEOUT = 300

    async def _run_evolution_visible(
        self, session_id: str, owner_account_id: str, request_id: str,
        conversation_id: str = "",
    ) -> AsyncIterator[ResponseChunk]:
        """前台可见地执行 Skill 自进化流程，yield evolution_footer 状态帧。

        三阶段串行执行，每阶段独立超时：
          1. 轨迹提取（extract）— 从本轮对话中抽取 query clusters
          2. 技能优化（optimize）— 对已有技能打补丁
          3. 技能生成（generate）— 基于聚类创建新技能 / 进化已有技能
        每阶段开始/结束都 yield evolution_footer chunk，让前端实时展示进度。
        """
        mgr = self.evolution_manager
        if mgr is None:
            return

        timeout_extract = self._EVOLUTION_PHASE_TIMEOUT_EXTRACT
        timeout_optimize = self._EVOLUTION_PHASE_TIMEOUT_OPTIMIZE
        timeout_generate = self._EVOLUTION_PHASE_TIMEOUT_GENERATE

        # ── 阶段 1：轨迹提取 ─────────────────────────────────────
        yield ResponseChunk.evolution_footer(
            request_id,
            "正在分析本轮对话，提取可复用模式…",
            evolution_status="active", phase="extract",
        )
        try:
            log_id = await asyncio.wait_for(
                asyncio.to_thread(
                    mgr.extract_session,
                    session_id,
                    owner_account_id,
                ),
                timeout=timeout_extract,
            )
            log.info("[EVOLUTION] 阶段1-轨迹提取(visible): session=%s log_id=%s", session_id, log_id)
        except asyncio.TimeoutError:
            log.warning("[EVOLUTION] 阶段1-轨迹提取超时(visible): session=%s", session_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("[EVOLUTION] 阶段1-轨迹提取失败(visible): session=%s err=%s", session_id, exc)

        # ── 阶段 2：技能优化（对已有技能打补丁）──────────────────
        yield ResponseChunk.evolution_footer(
            request_id,
            "正在优化已有技能，补充缺失能力…",
            evolution_status="active", phase="optimize",
        )
        # 阶段2的优化结果（仅用于 footer 展示，不与阶段3的 evolved_skills 混淆）
        optimized_skills: list[dict] = []
        try:
            optimize_result = await asyncio.wait_for(
                asyncio.to_thread(
                    mgr.optimize_all,
                    current_session_id=session_id,
                ),
                timeout=timeout_optimize,
            )
            if optimize_result:
                # optimize_all 返回 dict[str, list[str]]，格式为 {slug: [patch1, patch2, ...]}
                for slug, patches in optimize_result.items():
                    optimized_skills.append({
                        "slug": slug,
                        "name": slug,
                        "patches": [p[:200] for p in patches] if patches else [],
                    })
                # 阶段2优化成功时输出中间 footer
                if optimized_skills:
                    yield ResponseChunk.evolution_footer(
                        request_id,
                        f"已优化 {len(optimized_skills)} 项技能",
                        evolution_status="active", phase="optimize",
                        optimized_skills=optimized_skills,
                    )
            log.info(
                "[EVOLUTION] 阶段2-技能优化(visible): session=%s optimized=%d",
                session_id, len(optimized_skills),
            )
        except asyncio.TimeoutError:
            log.warning("[EVOLUTION] 阶段2-技能优化超时(visible): session=%s", session_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("[EVOLUTION] 阶段2-技能优化失败(visible): session=%s err=%s", session_id, exc)

        # ── 阶段 3：技能生成（创建新技能 / 进化已有技能）─────────────────
        yield ResponseChunk.evolution_footer(
            request_id,
            "正在识别可复用模式，生成新技能提案…",
            evolution_status="active", phase="generate",
        )
        created_skills: list[dict] = []
        # 命名变量：propose() 内部会将进化建议追加到此列表
        evolution_suggestions: list = []
        try:
            proposals = await asyncio.wait_for(
                asyncio.to_thread(
                    mgr.generate_proposals,
                    min_queries=2,
                    max_proposals=5,
                    current_session_id=session_id,
                    evolution_suggestions=evolution_suggestions,
                    conversation_id=conversation_id,
                    session_id=session_id or "",
                ),
                timeout=timeout_generate,
            )
            # 自动创建技能文件
            if proposals:
                for proposal in proposals:
                    path = await asyncio.wait_for(
                        asyncio.to_thread(
                            mgr.create_skill,
                            proposal,
                            conversation_id=conversation_id,
                            session_id=session_id or "",
                        ),
                        timeout=timeout_generate,
                    )
                    if path:
                        # 从 LLM 生成的结构化内容中提取优质元数据
                        # create() 内部会用 LLM 生成 structured_content 并回写到 proposal
                        meta: dict = {}
                        if proposal.structured_content:
                            meta = proposal.structured_content.get("metadata", {})
                        # 从实际路径提取 slug（LLM 生成的名称可能与提案不同）
                        actual_slug = ""
                        try:
                            actual_slug = Path(path).parent.name
                        except Exception:
                            pass
                        created_skills.append({
                            "name": meta.get("name") or proposal.proposed_name or proposal.proposed_slug,
                            "slug": actual_slug or proposal.proposed_slug,
                            "path": path,
                            "description": meta.get("zh_description") or meta.get("description") or proposal.description or proposal.zh_description or "",
                            "zh_name": meta.get("zh_name") or proposal.zh_name or proposal.proposed_name or "",
                        })
            # 应用跨轮次进化建议（evolution_suggestions 在 generate_proposals 中收集）
            # _build_evolve_suggestion 在 propose() 内部已调用 evolve_skill
            # 完成了 SKILL.md 更新，这里收集结果用于 footer 展示
            evolved_skills: list[dict] = []
            for sug in evolution_suggestions:
                if getattr(sug, "suggestion_type", "") == "evolve":
                    evolved_skills.append({
                        "slug": sug.skill_slug,
                        "name": sug.skill_name,
                        "patches": [sug.suggested_value[:200]] if sug.suggested_value else [sug.reason[:200]],
                    })

            # 构建最终摘要：进化成功或技能创建成功即输出 footer，
            # 两者都无（无进化）或失败时不显示，避免给用户输出无意义信息
            if created_skills or evolved_skills:
                parts = []
                if created_skills:
                    parts.append(f"新增 {len(created_skills)} 项技能")
                if evolved_skills:
                    parts.append(f"进化 {len(evolved_skills)} 项技能")
                yield ResponseChunk.evolution_footer(
                    request_id,
                    f"Skill 自进化完成 — {' · '.join(parts)}",
                    evolution_status="done", phase="done",
                    created_skills=created_skills if created_skills else None,
                    evolved_skills=evolved_skills if evolved_skills else None,
                )
            log.info(
                "[EVOLUTION] 阶段3-技能生成(visible): session=%s proposals=%d created=%d",
                session_id, len(proposals) if proposals else 0, len(created_skills),
            )
        except asyncio.TimeoutError:
            # 超时视为失败：不输出 footer
            log.warning("[EVOLUTION] 阶段3-技能生成超时(visible): session=%s", session_id)
        except Exception as exc:  # noqa: BLE001
            # 失败时不输出 footer
            log.warning("[EVOLUTION] 阶段3-技能生成失败(visible): session=%s err=%s", session_id, exc)

    def _session_needs_title(self, session_id: str, owner: str) -> bool:
        """占位标题的会话在首轮结束后仍应尝试生成摘要标题。"""
        try:
            # 不排除渠道会话（agent:main:*）：它们同样需要首轮后的自动摘要标题。
            rows = self.session_store.list_sessions(
                owner_account_id=owner, exclude_channel_sessions=False
            )
        except Exception:  # noqa: BLE001
            return False
        from crew.state.session_store import is_placeholder_title

        for row in rows:
            if row.get("session_id") == session_id:
                return not bool(row.get("manual_title")) and is_placeholder_title(
                    str(row.get("title") or "")
                )
        return False

    def _spawn_title_task(
        self,
        title_sid: str,
        owner: str,
        history: list[Message],
        push_fn,
    ) -> None:
        """后台生成会话标题并推送，不阻塞主推理或 final 帧发送。

        仅在主响应结束后调度（见 run 的 finally 块），history 已含本轮 assistant
        内容。同一 (owner, title_sid) 在途任务去重，避免重复 LLM 调用。
        """
        inflight_key = (owner, title_sid)
        if inflight_key in self._title_inflight:
            return
        self._title_inflight.add(inflight_key)

        async def _run() -> None:
            try:
                title = await generate_session_title(
                    self.provider,
                    history,
                )
                if not title and self._session_needs_title(title_sid, owner):
                    title = await generate_session_title(
                        self.provider,
                        history,
                    )
                if not title:
                    return
                try:
                    if not self._session_needs_title(title_sid, owner):
                        return
                    self.session_store.set_title(title_sid, title, owner_account_id=owner)
                except Exception as exc:  # noqa: BLE001
                    log.debug("写入会话标题失败：%s", exc)
                    return
                if push_fn is not None:
                    try:
                        await push_fn(
                            title_sid,
                            {
                                "kind": "session_title",
                                "session_id": title_sid,
                                "body": {"title": title},
                                "is_final": False,
                                "sequence": 0,
                            },
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.debug("推送会话标题失败：%s", exc)
            except Exception as exc:  # noqa: BLE001
                log.debug("后台标题生成失败：%s", exc)
            finally:
                self._title_inflight.discard(inflight_key)

        task = asyncio.create_task(_run())
        self._title_tasks.add(task)
        task.add_done_callback(self._title_tasks.discard)

    async def _persist_turn(
        self,
        envelope: Envelope,
        history: list[Message],
        ctx: ExecutionContext,
        prefix_len: int,
        turn_started_at: float,
        *,
        interrupted: bool,
        task_sid: str | None = None,
    ) -> None:
        """把 executor 本轮新增消息回灌 canonical 历史并持久化。

        无论本轮是正常结束、被硬停（CancelledError）还是异常，都会调用，保证
        user 消息与已完成的工具调用一定落库（否则停止后刷新即丢、下一轮无上下文）。
        持久化用同步 session_store.save（不可被取消打断）；memory.write 用 shield
        兜底，被取消时 best-effort。子 agent（lightweight）用完即弃，不落库不写记忆。
        session_store 按 sidechain 的 session_id 存（transcript 隔离机制，不能改）；
        memory 按 task_sid（稳定主会话 id）存，否则会落到 ::turn:: 临时 id 下。
        """
        sid = envelope.session_id
        mem_sid = task_sid or sid
        # 本轮新增的 assistant/tool 消息回灌到 canonical 全量历史
        # 跳过 reminder 等已插入的非历史消息（通过 prefix_len 偏移）
        new_from_executor = ctx.messages[prefix_len:]
        turn_finished_at = time.time()
        turn_duration = turn_finished_at - turn_started_at
        for message in new_from_executor:
            if message.is_meta:
                continue
            if message.timestamp is None:
                message.timestamp = turn_finished_at
            if message.role == "assistant":
                message.turn_started_at = turn_started_at
                message.turn_duration = turn_duration
                for tool_call in message.tool_calls:
                    # 仅兜底 started_at；duration 由 ToolRunner 按单次执行写入，不能复用整回合耗时
                    if tool_call.started_at is None:
                        tool_call.started_at = turn_started_at
                    # Browser form values and credential-bearing URLs are
                    # needed during execution, but not in durable canonical
                    # history.  Reduce them only after the executor has
                    # finished so the live tool protocol still sees the exact
                    # arguments it was asked to run.
                    tool_call.arguments = tool_arguments_for_history(
                        tool_call.name,
                        tool_call.arguments,
                    )
        # 本轮文件改动摘要：挂到本轮最后一条 assistant，供历史回放「已编辑文件」卡。
        if self.plan_manager is not None:
            try:
                from crew.core.runctx import current_owner_account_id

                # ToolRunner 按 ExecutionContext.session_id（task_sid）记录文件摘要。
                # sidechain 场景下 envelope.session_id 是 ::turn:: 临时会话；若用 sid
                # drain，实时文件卡虽然完整，落库却为空，重启后只能从 tool_calls
                # 反推过程文件，终端间接生成的最终产物会消失。
                turn_files = self.plan_manager.drain_turn_file_changes(
                    mem_sid,
                    owner_account_id=current_owner_account_id.get(),
                )
            except Exception:  # noqa: BLE001
                turn_files = []
            if turn_files:
                for message in reversed(new_from_executor):
                    if message.is_meta or message.role != "assistant":
                        continue
                    message.turn_file_changes = turn_files
                    break
        # 只回灌非 is_meta 的消息；硬停可能停在工具执行中途，需清洗尾部悬空 tool_call
        new_msgs = self._drop_dangling_tool_calls(
            [m for m in new_from_executor if not m.is_meta]
        )
        history.extend(new_msgs)

        # 子 agent（lightweight）的会话用完即弃：不落库、不写记忆，避免 SQLite 堆积
        # 一次性 uuid 会话（对照 Hermes 子 agent 的 ephemeral 会话）。
        if not self.lightweight:
            t = time.perf_counter()
            try:
                # title_fallback：enable_title=True 时留空占位，等下方 generate_session_title
                # 生成摘要后由 set_title 写入；否则保留旧行为（首条 user 消息截断作标题）。
                # 避免「先 save 写入截断用户原话 → 摘要生成失败 → 标题永久停在原话」。
                self.session_store.save(
                    sid,
                    history,
                    workspace_id=envelope.workspace_id,
                    owner_account_id=envelope.user_id,
                    title_fallback="" if self.enable_title else None,
                )
            except Exception:  # noqa: BLE001
                log.exception("会话落库失败 session=%s", sid)
            log.info("[PERF] session_save       %.3fs", time.perf_counter() - t)

            t = time.perf_counter()
            try:
                # shield：即便本轮被取消，记忆写入仍尽量完成（best-effort）
                # mem_sid：稳定主会话 id（sidechain 路径下不随 turn 变化）
                await asyncio.shield(self.memory.write(mem_sid, history))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("记忆写入失败 session=%s", sid)
            log.info("[PERF] memory_write       %.3fs", time.perf_counter() - t)

    @staticmethod
    def _drop_dangling_tool_calls(messages: list[Message]) -> list[Message]:
        """丢弃尾部缺少配对 tool 结果的 assistant.tool_calls 消息（中断残留）。

        硬停可能停在工具执行中途：assistant 已带 tool_calls 入历史，但对应的 tool
        结果还没回填。若直接落库，下一轮 history 里就有 dangling tool_call，OpenAI 接口
        会因「tool_call 无对应响应」报错。从尾部向前剥掉这类未被全部回填的 assistant 消息。
        """
        if not messages:
            return messages
        answered = {
            m.tool_call_id for m in messages if m.role == "tool" and m.tool_call_id
        }
        result = list(messages)
        while result:
            last = result[-1]
            if last.role == "assistant" and last.tool_calls:
                ids = {tc.id for tc in last.tool_calls}
                missing = ids - answered
                # Builtin/OpenAI tool_calls 必须有后续 Message.tool 配对；硬停在工具执行前
                # 可能留下这类悬空 assistant，需要剥离。ACP 外部智能体的工具调用则作为
                # assistant.tool_calls 的展示元数据持久化，结果已写在 ToolCall.result 中，
                # 不会再追加 Message.tool；这种完整外部工具记录不能被误删，否则 Kimi 在
                # ask_followup_question 后的 assistant 文本会从历史回放中消失。
                missing_without_result = [
                    tc for tc in last.tool_calls if tc.id in missing and not tc.result
                ]
                if missing_without_result:
                    result.pop()
                    continue
            break
        return result
