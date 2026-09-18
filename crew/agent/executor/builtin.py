"""默认执行内核：Crew 自带手搓对话循环（编排 crew/agent/loop 的鲁棒性/可控性组件）。

主循环每轮做这些事（用于 run_conversation 的内层）：
  预算 consume → 中断检查 → drain steer 注入 → 调模型(流式重试/溢出压缩/故障转移)
  → resilience 校验(空响应重试 / 截断续写) → 无工具则 final → 有工具交 ToolRunner
  执行(含 guardrails 防循环)回灌 → 下一轮。

各能力拆在 crew/agent/loop 子包，本文件只负责把它们串起来，保持精简可读。

canonical 历史契约：本 executor 只 **append** 到 ctx.messages（SingleAgent 据此回灌历史）。
上下文溢出兜底压缩只作用于「发给 LLM 的视图」，不改 ctx.messages。
"""

from __future__ import annotations

import asyncio
import inspect
import random
import time
from dataclasses import replace
from typing import Any, AsyncIterator

from crew.agent.executor.base import AgentExecutor, ExecutionContext, FinalRequestView
from crew.agent.compact.tokens import estimate_tokens
from crew.agent.loop import (
    CONTINUATION_PROMPT,
    EMPTY_RETRY_NUDGE,
    ESCALATED_MAX_OUTPUT_TOKENS,
    STREAM_INTERRUPT_PROMPT,
    STREAM_INTERRUPT_STATUS_MESSAGE,
    TOOL_ARGUMENTS_RECOVERY_LIMIT,
    TOOL_ARGUMENTS_RECOVERY_PROMPT,
    IterationBudget,
    ToolCallGuardrailConfig,
    ToolCallGuardrailController,
    ToolRunner,
    has_truncated_tool_args,
    is_context_overflow,
    is_empty_response,
    is_max_tokens_finish,
    is_stream_interrupt_recoverable,
    provider_chain,
    should_continue,
)
from crew.agent.loop.tool_dispatch_helpers import plan_tool_calls
from crew.core.envelope import ResponseChunk
from crew.core.errors import CrewError, CrewErrorKind, ProviderError
from crew.core.interfaces import LLMProvider, ToolRegistry
from crew.core.types import IMAGE_INPUT_UNAVAILABLE_NOTICE, Message, ToolResult
from crew.plugins.manager import PluginManager
from crew.state.logging import get_logger
from crew.tools.policy import ToolDisclosureMode
from crew.tools.tool_search import (
    ToolSearchConfig,
    assemble_tool_schemas,
    available_deferred_tools_message,
    expand_discovered_tool_schemas,
    extract_discovered_tool_names,
    is_bridge_tool,
)

log = get_logger("agent.executor")

VISION_CAPABILITY_RECOVERY_PROMPT = (
    "当前模型刚刚拒绝了图片输入，本轮已切换为非视觉模式。不要声称已经看过图片。"
    "如果任务涉及网页，请优先使用 browser snapshot、DOM、文本提取等非视觉方式继续；"
    "如果图片内容无法通过文本方式获得，请明确告知用户当前配置的模型没有视觉能力，"
    "需要切换到支持视觉的模型。"
)


def _accepts_prefix_kwargs(fn: Any) -> bool:
    """探测压缩类回调是否接受 ``system_prompt`` / ``tools`` 关键字。

    executor 注入的 compactor 可能是测试替身或旧实现，签名探测失败时
    按支持处理，保证生产实现（pipeline.Compactor）能拿到前缀与工具清单。
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return True
    return "system_prompt" in params and "tools" in params


def _without_image_inputs(messages: list[Message]) -> list[Message]:
    """Build a request-only text view while preserving canonical multimodal history."""
    sanitized: list[Message] = []
    for message in messages:
        parts = message.content_parts or []
        if not any(
            str(part.get("type") or "").lower() in {"image", "image_url", "input_image"}
            for part in parts
        ):
            sanitized.append(message)
            continue
        text_parts = [
            str(part.get("text") or "").strip()
            for part in parts
            if part.get("type") == "text" and str(part.get("text") or "").strip()
        ]
        text_parts.append(IMAGE_INPUT_UNAVAILABLE_NOTICE)
        sanitized.append(
            replace(message, content="\n".join(text_parts), content_parts=None)
        )
    return sanitized


# 服务端建议退避（Retry-After / body retry_delay）的封顶秒数：防异常大值拖死重试循环。
_RETRY_DELAY_CAP_SECONDS = 60.0


def _llm_error_chunk(
    rid: str,
    exc: BaseException,
    sequence: int,
    message: str | None = None,
) -> ResponseChunk:
    """LLM 路径的 error 帧：CrewError 带结构化 kind(code)/retryable/retry_delay，
    非类型化异常保持纯 message 帧。message 覆盖默认的 str(exc)（友好文案场景）。"""
    text = str(exc) if message is None else message
    if isinstance(exc, CrewError):
        return ResponseChunk.error(
            rid,
            text,
            sequence,
            code=exc.kind.value,
            retryable=exc.is_retryable(),
            retry_delay=exc.retry_delay,
        )
    return ResponseChunk.error(rid, text, sequence)


def _dump_prompt(ctx: ExecutionContext, view: list, tools: list | None, iteration: int) -> None:
    """DEBUG 级别：打印本轮发送给 LLM 的完整 prompt（system + messages + tools）。"""
    if not log.isEnabledFor(10):  # DEBUG = 10
        return
    sep = "=" * 60
    lines = [
        f"\n{sep}",
        f"[PROMPT] iteration={iteration}  session={ctx.session_id}",
        f"--- SYSTEM ({len(ctx.system_prompt)} chars) ---",
        ctx.system_prompt,
        f"--- MESSAGES ({len(view)} msgs) ---",
    ]
    for i, m in enumerate(view):
        preview = (m.content or "")[:1500]
        lines.append(f"  [{i}] {m.role}: {preview}")
    if tools:
        lines.append(f"--- TOOLS ({len(tools)} schemas) ---")
        for t in tools:
            fn = t.get("function", {})
            lines.append(f"  {fn.get('name', '?')}: {fn.get('description', '')[:80]}")
    lines.append(sep)
    log.debug("\n".join(lines))


def _estimate_prompt_overhead(ctx: ExecutionContext, view: list, tools: list | None) -> dict[str, int]:
    """估算 prompt 固定开销（系统提示 / 技能·上下文 / 工具定义）的 token 数。

    供前端 Inspector breakdown 拆分显示；与 provider 返回的 prompt_tokens 独立，
    用 chars//4 粗估（与 session_store._estimate_tokens 同口径）。
    """
    import json as _json
    sys_chars = len(ctx.system_prompt or "")
    rem_chars = sum(
        len(m.content or "") for m in view
        if str(getattr(m, "role", "")) in ("system", "system_reminder")
    )
    tool_chars = len(_json.dumps(tools, ensure_ascii=False)) if tools else 0
    return {"system": sys_chars // 4, "reminder": rem_chars // 4, "tools": tool_chars // 4}


_INTERRUPT_MARKER_TEMPLATE = "[回合中断] {message}"


def _inject_steer(messages: list[Message], steer: str) -> None:
    """把 steer 文本注入对话：优先贴到最近一条 tool 结果后；无 tool 则作为 user 追加。

    使用 <system-reminder> 标签包裹，明确告知模型这是系统注入的补充指令。
    queued_command 以 attachment 形式注入当前执行上下文。
    """
    marker = f"\n\n<system-reminder>用户补充指令：{steer}</system-reminder>"
    for m in reversed(messages):
        if m.role == "tool":
            m.content = (m.content or "") + marker
            return
    messages.append(Message.user(f"<system-reminder>用户补充指令：{steer}</system-reminder>", is_meta=True))


class BuiltinExecutor(AgentExecutor):
    name = "builtin"

    def __init__(
        self,
        provider: LLMProvider,
        registry: ToolRegistry,
        plugins: PluginManager,
        *,
        max_iterations: int = 20,
        max_retries: int = 2,
        backoff_seconds: float = 1.0,
        guardrail_config: ToolCallGuardrailConfig | None = None,
        parallel_tools: bool = True,
        fallback_providers: list[LLMProvider] | None = None,
        compactor: Any = None,
        empty_retry_max: int = 2,
        continuation_max: int = 2,
        max_parallel_tool_calls: int = 8,
        max_delegate_tool_calls: int = 3,
        plan_manager: Any = None,
        stream_continuation_max: int = 2,
        stream_retry_jitter: bool = True,
        tool_execution_timeout_seconds: float = 600.0,
        turn_deadline_seconds: float = 0.0,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.plugins = plugins
        self.max_iterations = max_iterations
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.guardrail_config = guardrail_config or ToolCallGuardrailConfig()
        self.parallel_tools = parallel_tools
        self.fallback_providers = fallback_providers or []
        self.compactor = compactor  # 需有 force_compact(messages)；None 则关闭溢出兜底
        self.empty_retry_max = empty_retry_max
        self.continuation_max = continuation_max
        self.max_parallel_tool_calls = max(1, int(max_parallel_tool_calls or 8))
        self.max_delegate_tool_calls = max(1, int(max_delegate_tool_calls or 3))
        self.plan_manager = plan_manager
        self.stream_continuation_max = stream_continuation_max
        self.stream_retry_jitter = stream_retry_jitter
        # 执行段看门狗（秒）：经 ToolRunner 包裹单工具执行，超时合成 timed-out 输出，0=关闭。
        self.tool_execution_timeout_seconds = max(0.0, float(tool_execution_timeout_seconds or 0.0))
        # 整回合 deadline（秒）：回合累计时长上限，到点走 interrupt 同款优雅收尾，0=不限。
        self.turn_deadline_seconds = max(0.0, float(turn_deadline_seconds or 0.0))
        # 用于检测 plan 模式是否刚退出，以便注入一次性 exit reminder。
        self._plan_was_active = False

    def build_request_view(
        self,
        system_prompt: str,
        messages: list[Message],
        tool_schemas: list[dict[str, Any]],
        session_id: str,
        tool_disclosure_mode: ToolDisclosureMode = ToolDisclosureMode.PROGRESSIVE,
        *,
        tool_search_assembly: Any = None,
        discovered_tool_names: Any = None,
        consume_transient: bool = True,
        vision_downgraded: bool = False,
        max_output_tokens: int | None = None,
    ) -> FinalRequestView:
        """唯一的 builtin request-view 装配入口。

        实际发送与 session preview 都调用这里。compact 负责先选择 ``messages``
        视图；本方法负责把 system、tool-search、动态 schema、todo reminder 与
        vision recovery 收敛成一个 ``FinalRequestView``。
        """
        system_msg = Message.system(system_prompt)
        assembly = tool_search_assembly
        if assembly is None:
            ts_config = (
                ToolSearchConfig(enabled="off")
                if tool_disclosure_mode is ToolDisclosureMode.DIRECT
                else None
            )
            assembly = assemble_tool_schemas(tool_schemas, config=ts_config)
        deferred_tools_message = available_deferred_tools_message(assembly)
        if discovered_tool_names is None:
            discovered_tool_names = extract_discovered_tool_names(
                messages,
                original_tool_schemas=assembly.original_tool_schemas,
                config=assembly.config,
            )
        tools = expand_discovered_tool_schemas(assembly, discovered_tool_names) or None
        api_messages = [system_msg]
        if deferred_tools_message:
            api_messages.append(Message.user(deferred_tools_message, is_meta=True))
        api_messages.extend(messages)
        todo_reminder = self._todo_reminder(session_id, consume=consume_transient)
        if todo_reminder:
            api_messages.append(Message.system_reminder(todo_reminder))
        if vision_downgraded:
            api_messages = _without_image_inputs(api_messages)
            api_messages.append(Message.system_reminder(VISION_CAPABILITY_RECOVERY_PROMPT))
        return FinalRequestView.create(
            api_messages,
            tools,
            max_output_tokens=max_output_tokens,
        )

    async def finalize_request_view(
        self,
        view: FinalRequestView,
        *,
        session_id: str,
        request_id: str,
        provider_index: int = 0,
        provider: Any = None,
    ) -> tuple[FinalRequestView, Any]:
        """应用 request middleware，返回 Provider 前的最终统一快照。"""
        active_provider = provider or self.provider
        model = str(getattr(active_provider, "model", "") or "")
        provider_name = type(active_provider).__name__
        base_url = str(getattr(active_provider, "base_url", "") or "")
        request = view.to_payload()
        middleware_result = await self.plugins.apply_llm_request_middleware(
            request,
            session_id=session_id,
            request_id=request_id,
            provider_index=provider_index,
            model=model,
            provider=provider_name,
            base_url=base_url,
        )
        payload = (
            middleware_result.payload
            if isinstance(middleware_result.payload, dict)
            else request
        )
        return FinalRequestView.from_payload(
            payload,
            view,
            model=model,
            provider=provider_name,
            base_url=base_url,
            provider_index=provider_index,
        ), middleware_result

    def _pre_final_chunks(
        self,
        rid: str,
        next_seq,
        session_id: str,
        owner_account_id: str,
    ) -> list[ResponseChunk]:
        """final 前对账文件改动：剔除本轮新建又已删的路径，并广播最新累计列表。"""
        if self.plan_manager is None:
            return []
        try:
            if not self.plan_manager.has_file_change_records(
                session_id,
                owner_account_id=owner_account_id,
            ):
                return []
            files = self.plan_manager.reconcile_file_changes(
                session_id,
                owner_account_id=owner_account_id,
            )
        except Exception as exc:  # noqa: BLE001 — 对账失败不得阻断 final
            log.warning("file_changes 对账失败 session=%s: %s", session_id, exc)
            return []
        return [
            ResponseChunk(
                rid,
                kind="file_changes",
                body={"files": files},
                sequence=next_seq(),
            )
        ]

    async def _emit_final(
        self,
        rid: str,
        next_seq,
        session_id: str,
        owner_account_id: str,
        text: str,
        *,
        replace_content: bool = False,
        reason: str | None = None,
        usage: dict[str, int] | None = None,
    ) -> AsyncIterator[ResponseChunk]:
        """对账文件改动后再发 final，保证前端差集与落库摘要一致。"""
        for chunk in self._pre_final_chunks(rid, next_seq, session_id, owner_account_id):
            yield chunk
        yield ResponseChunk.final(
            rid,
            text,
            next_seq(),
            replace_content=replace_content,
            reason=reason,
            usage=usage,
        )

    async def _interrupt_notice_chunks(
        self,
        control,
        rid: str,
        next_seq,
        ctx: ExecutionContext,
    ) -> AsyncIterator[ResponseChunk]:
        """消费 interrupt_message：写历史标记 + status 帧透出（各一次）。

        TurnControl.interrupt(message=...) 的消息此前只存不取；回合收尾时 drain
        取出并清空（reset() 再兜底，不泄漏到下一回合）。非空时作为一条 user 标记
        消息追加到 canonical 历史（is_meta=False：落库且下回合进入 LLM 视图，模型
        据此知晓上轮为何停止），并经既有 status 通道透出给前端。
        """
        if control is None:
            return
        message = control.drain_interrupt_message()
        if not message:
            return
        ctx.messages.append(Message.user(_INTERRUPT_MARKER_TEMPLATE.format(message=message)))
        yield ResponseChunk.status_event(rid, message, next_seq())

    # ------------------------------------------------------------------ #
    async def execute(self, ctx: ExecutionContext) -> AsyncIterator[ResponseChunk]:
        rid = ctx.request_id
        seq = 0

        def next_seq() -> int:
            nonlocal seq
            seq += 1
            return seq

        original_tools = ctx.tool_schemas or []
        # 披露模式不改变授权范围。DIRECT 直接发送全部已授权 schema；
        # PROGRESSIVE 才按全局 ToolSearch 配置装配。
        ts_config = (
            ToolSearchConfig(enabled="off")
            if ctx.tool_disclosure_mode is ToolDisclosureMode.DIRECT
            else None
        )
        tool_search_assembly = assemble_tool_schemas(original_tools, config=ts_config)
        discovered_tool_names = extract_discovered_tool_names(
            ctx.messages,
            original_tool_schemas=tool_search_assembly.original_tool_schemas,
            config=tool_search_assembly.config,
        )
        # 0 = 无限；靠 auto-compact 管上下文 + guardrail 防失控。
        # 用 None 判而非 `or`，避免 0 被 `or` 当 falsy 跳过。
        max_iter = ctx.max_iterations if ctx.max_iterations is not None else self.max_iterations
        control = ctx.control

        # 整回合 deadline（0=不限）：与 interrupt 共用检查点。到点转成 interrupt
        # （带 deadline exceeded 消息 + interrupt_event），在途工具随即走上一批次的
        # aborted 语义，本轮回合按 interrupt 路径优雅收尾，而非异常崩溃。
        turn_deadline = self.turn_deadline_seconds
        turn_deadline_started = time.perf_counter()

        def _deadline_hit() -> bool:
            """deadline 到点则写入 interrupt 并返回 True；未配置/未到点/已有
            用户 interrupt（消息以用户为准）返回 False。"""
            if turn_deadline <= 0:
                return False
            if time.perf_counter() - turn_deadline_started < turn_deadline:
                return False
            if control is not None:
                if control.interrupted:
                    return False
                log.warning(
                    "整回合 deadline 触发（%.1fs），优雅收尾 session=%s",
                    turn_deadline,
                    ctx.session_id,
                )
                control.interrupt(
                    f"deadline exceeded：回合超过整回合时限（{turn_deadline:.0f}s），已自动停止"
                )
            return True

        # view token 估算轻缓存：同一迭代内 provisional/overflow 两处全量估算
        # 合并为一次。key = 消息数 + 末条消息长度；视图在本迭代内不变，下轮消息
        # 追加自然失配，不会把旧估算错套到新视图上。
        _view_token_cache: dict[tuple[int, int], int] = {}

        def _cached_view_tokens(messages: list[Message]) -> int:
            key = (len(messages), len(messages[-1].content) if messages else 0)
            if key not in _view_token_cache:
                _view_token_cache.clear()
                _view_token_cache[key] = estimate_tokens(messages)
            return _view_token_cache[key]

        # Plan 模式 per-turn 收紧：exit_plan_mode 反复失败（plan 文件为空时模型不死心）会
        # 死循环——同一无参工具失败 N 次后才 halt 太晚。plan 激活时临时加严 guardrail 阈值
        # 并收窄迭代上限，不动全局 config（避免影响普通执行容错）。plan 应是
        # 探索+写计划+一次澄清/审批的短流程，无需 60 轮预算。
        from crew.core.runctx import current_owner_account_id

        owner_account_id = current_owner_account_id.get()
        plan_active = self.plan_manager is not None and self.plan_manager.is_active(
            ctx.session_id,
            owner_account_id=owner_account_id,
        )
        guard_cfg = self.guardrail_config
        if plan_active:
            from dataclasses import replace as _dc_replace

            guard_cfg = _dc_replace(
                guard_cfg,
                hard_stop_enabled=True,
                exact_failure_block_after=2,  # 同参失败 2 次即 block（exit_plan_mode 无参→失败1次后第2次即拦）
                same_tool_failure_halt_after=3,  # 同名失败 3 次即 halt 收尾
            )
            max_iter = 12 if not max_iter else min(max_iter, 12)

        budget = IterationBudget(max_iter)
        guardrails = ToolCallGuardrailController(guard_cfg)
        runner = ToolRunner(
            self.registry,
            self.plugins,
            guardrails,
            parallel_enabled=self.parallel_tools,
            max_parallel_tool_calls=self.max_parallel_tool_calls,
            session_id=ctx.session_id,
            control=control,
            plan_manager=self.plan_manager,
            tool_execution_timeout_seconds=self.tool_execution_timeout_seconds,
            tool_search_schemas=tool_search_assembly.original_tool_schemas,
            tool_search_config=tool_search_assembly.config,
            authorized_tool_names=ctx.authorized_tool_names,
            allowed_tool_names=(
                {
                    str((schema.get("function") or {}).get("name") or "")
                    for schema in tool_search_assembly.original_tool_schemas
                    if str((schema.get("function") or {}).get("name") or "")
                }
                if ctx.enforce_tool_scope
                else None
            ),
            direct_tool_names=(
                {
                    str((schema.get("function") or {}).get("name") or "")
                    for schema in tool_search_assembly.tool_schemas
                    if str((schema.get("function") or {}).get("name") or "")
                    and not is_bridge_tool(
                        str((schema.get("function") or {}).get("name") or "")
                    )
                }
                if ctx.enforce_tool_scope and tool_search_assembly.original_tool_schemas
                else None
            ),
            discovered_tool_names=discovered_tool_names,
        )
        empty_retries = 0
        continuation_count = 0
        tool_args_recovery_count = 0
        max_output_tokens_override: int | None = None
        max_output_tokens_escalated = False
        stream_continuation_count = 0
        streamed_text = ""  # 流式中断续写累计文本
        overflow_mode = False  # 命中上下文溢出且兜底压缩使投影前进后，后续轮持续走 force_compact
        overflow_pending = False  # 命中溢出待压缩：下一轮开头先做剪枝+摘要，再决定是否重试
        overflow_retries = 0  # 本轮已消耗的 overflow compact-retry 次数（上限 compactor.max_overflow_retries）
        vision_downgraded = False  # 上游实际拒绝图片后，本轮余下请求只发送文本视图

        from crew.core.runctx import current_agent_workdir, current_session_id
        current_session_id.set(ctx.session_id)
        if ctx.cwd:
            current_agent_workdir.set(ctx.cwd)

        # view/canonical 分离，每轮按水位压缩视图：
        #   - ctx.messages：本轮全量 append-only 日志，供 _persist_turn 回灌 canonical 历史，永不被压缩。
        #   - view_messages：发给 LLM 的视图，每轮 compact_view 可把旧段摘要掉。与 ctx.messages 共享
        #     最近消息的 Message 对象引用（in-place 编辑如 steer 自动传播）；旧段被摘要后用新对象替换。
        view_messages: list[Message] = list(ctx.messages)

        grace = False  # 预算耗尽后允许的最后一轮宽限（用于收尾文本）
        while budget.consume() or grace:
            used_grace = grace
            grace = False

            # ---- 中断/deadline 检查（轮初安全点）----
            #   空 final：前端不覆盖已流式内容，仅结束本轮（保留之前已生成的部分）。
            deadline_now = _deadline_hit()
            if deadline_now or (control is not None and control.interrupted):
                async for _nc in self._interrupt_notice_chunks(control, rid, next_seq, ctx):
                    yield _nc
                async for _fc in self._emit_final(
                    rid, next_seq, ctx.session_id, owner_account_id, "",
                    reason="deadline" if deadline_now else None,
                ):
                    yield _fc
                return

            # ---- drain steer：注入最近一条 tool 消息 ----
            #   注入 view_messages（LLM 视图）。in-place 编辑共享 tool 对象会传播到 ctx.messages；
            #   无 tool 时追加 is_meta user，_persist_turn 会过滤，不入 canonical（与原行为一致）。
            if control is not None:
                steer = control.drain_steer()
                if steer:
                    _inject_steer(view_messages, steer)
                    log.info("steer 已注入 session=%s", ctx.session_id)

            # ---- 组装发给 LLM 的视图 ----
            #   每轮 compact_view 做水位压缩，未触水位时近乎零成本。
            #   overflow_pending/overflow_mode 是 provider 报溢出后的紧急兜底：先做无模型
            #   剪枝（超长 tool result 头 4096/尾 1024）再摘要；仅当历史投影确实前进
            #   （压缩后视图 token 数变小）才重试，否则不再重试，直接报错。
            provisional_view = self.build_request_view(
                ctx.system_prompt,
                view_messages,
                original_tools,
                ctx.session_id,
                ctx.tool_disclosure_mode,
                tool_search_assembly=tool_search_assembly,
                discovered_tool_names=runner.discovered_tool_names,
                consume_transient=False,
                vision_downgraded=vision_downgraded,
                max_output_tokens=max_output_tokens_override,
            )
            prompt_overhead_tokens = max(
                0,
                provisional_view.estimated_prompt_tokens() - _cached_view_tokens(view_messages),
            )
            if (overflow_mode or overflow_pending) and self.compactor is not None:
                overflow_pending = False
                overflow_before = _cached_view_tokens(view_messages)
                overflow_count_before = len(view_messages)
                yield ResponseChunk.compaction_event(rid, True, next_seq())
                compact_interrupted = False
                try:
                    from crew.core.runctx import current_owner_account_id

                    force_compact = self.compactor.force_compact
                    kwargs: dict[str, Any] = {}
                    try:
                        params = inspect.signature(force_compact).parameters
                        accepts_owner = "owner_account_id" in params or any(
                            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
                        )
                    except (TypeError, ValueError):
                        accepts_owner = True
                    if accepts_owner:
                        kwargs["owner_account_id"] = current_owner_account_id.get()
                    if _accepts_prefix_kwargs(force_compact):
                        kwargs["system_prompt"] = ctx.system_prompt
                        kwargs["tools"] = original_tools
                    # compact 段中断检查（前）：已在压缩前被中断则不启动本次压缩。
                    wait_interrupted = getattr(control, "wait_interrupted", None)
                    if control is not None and control.interrupted:
                        compact_interrupted = True
                    elif control is not None and callable(wait_interrupted):
                        # 摘要调用与 interrupt 事件竞争：interrupt 先到即取消在途
                        # 摘要（await 点可被取消，不侵入 compactor 内部流水线），
                        # 放弃本次压缩走回合收尾。
                        compact_task = asyncio.create_task(
                            force_compact(ctx.messages, ctx.session_id, **kwargs)
                        )
                        interrupt_task = asyncio.create_task(wait_interrupted())
                        try:
                            done, _pending = await asyncio.wait(
                                {compact_task, interrupt_task},
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                        finally:
                            interrupt_task.cancel()
                        if compact_task in done:
                            view_messages = compact_task.result()
                            # compact 段中断检查（后）：压缩完成前/完成时到达的
                            # interrupt 同样放弃本次压缩结果，走回合收尾。
                            if control.interrupted:
                                compact_interrupted = True
                        else:
                            compact_task.cancel()
                            await asyncio.gather(compact_task, return_exceptions=True)
                            compact_interrupted = True
                    else:
                        view_messages = await force_compact(
                            ctx.messages, ctx.session_id, **kwargs
                        )
                except Exception as exc:  # noqa: BLE001
                    log.warning("force_compact 失败，按原视图发送：%s", exc)
                    view_messages = list(ctx.messages)
                finally:
                    yield ResponseChunk.compaction_event(rid, False, next_seq())
                if compact_interrupted:
                    log.info("压缩期间被 interrupt，放弃本次压缩并收尾 session=%s", ctx.session_id)
                    async for _nc in self._interrupt_notice_chunks(control, rid, next_seq, ctx):
                        yield _nc
                    async for _fc in self._emit_final(rid, next_seq, ctx.session_id, owner_account_id, ""):
                        yield _fc
                    return
                overflow_after = estimate_tokens(view_messages)
                overflow_count_after = len(view_messages)
                if overflow_after >= overflow_before and overflow_count_after >= overflow_count_before:
                    # 投影未前进：再发一次同样的请求必然再溢出，不再重试。
                    # token 估算在极小历史上会退化为 0，用消息数兜底判断。
                    log.warning(
                        "overflow 兜底压缩未使历史投影前进（%d → %d tokens），不再重试 session=%s",
                        overflow_before,
                        overflow_after,
                        ctx.session_id,
                    )
                    yield ResponseChunk.error(rid, "上下文超长且无法进一步压缩", next_seq())
                    return
                overflow_mode = True
                log.info(
                    "overflow 兜底压缩后投影前进（%d → %d tokens），重试本轮 session=%s",
                    overflow_before,
                    overflow_after,
                    ctx.session_id,
                )
            elif self.compactor is not None:
                will_compact_view = getattr(self.compactor, "will_compact_view", None)
                show_compaction = bool(
                    will_compact_view
                    and will_compact_view(
                        view_messages,
                        ctx.session_id,
                        owner_account_id=owner_account_id,
                        prompt_overhead_tokens=prompt_overhead_tokens,
                    )
                )
                if show_compaction:
                    yield ResponseChunk.compaction_event(rid, True, next_seq())
                try:
                    compact_kwargs: dict[str, Any] = {}
                    if _accepts_prefix_kwargs(self.compactor.compact_view):
                        compact_kwargs["system_prompt"] = ctx.system_prompt
                        compact_kwargs["tools"] = original_tools
                    view_messages = await self.compactor.compact_view(
                        view_messages,
                        ctx.session_id,
                        owner_account_id=owner_account_id,
                        prompt_overhead_tokens=prompt_overhead_tokens,
                        **compact_kwargs,
                    )
                finally:
                    if show_compaction:
                        yield ResponseChunk.compaction_event(rid, False, next_seq())
            view = view_messages
            request_view = self.build_request_view(
                ctx.system_prompt,
                view,
                original_tools,
                ctx.session_id,
                ctx.tool_disclosure_mode,
                tool_search_assembly=tool_search_assembly,
                discovered_tool_names=runner.discovered_tool_names,
                consume_transient=True,
                vision_downgraded=vision_downgraded,
                max_output_tokens=max_output_tokens_override,
            )
            api_messages = list(request_view.messages)
            tools = list(request_view.tools) if request_view.tools is not None else None
            hook_started = time.perf_counter()
            pre_llm_result = await self.plugins.pre_llm_call(ctx.session_id, api_messages)
            log.info(
                "[PERF] pre_llm_hooks    %.3fs  (messages=%d) request_id=%s session=%s",
                time.perf_counter() - hook_started,
                len(api_messages),
                rid,
                ctx.session_id,
            )
            if isinstance(pre_llm_result, dict) and pre_llm_result.get("action") == "block":
                block_text = pre_llm_result.get("response", "")
                log.info("pre_llm_call 拦截，跳过 LLM 调用 session=%s", ctx.session_id)
                #yield ResponseChunk.status_event(rid, "安全策略检查中…", next_seq())
                assistant_msg = Message.assistant(block_text)
                ctx.messages.append(assistant_msg)
                async for _fc in self._emit_final(rid, next_seq, ctx.session_id, owner_account_id, block_text):
                    yield _fc
                return

            request_view = request_view.with_messages(api_messages)

            _dump_prompt(ctx, view, tools, budget.used)

            # ---- 调模型（流式重试 + 溢出压缩 + provider 故障转移 + 流式中途中断）----
            result: dict[str, Any] = {}
            async for ev in self._call_model(
                request_view,
                rid,
                next_seq,
                result,
                control,
                runner,
                ctx.session_id,
            ):
                yield ev
            usage = result.get("usage")
            if not isinstance(usage, dict):
                usage = {}
                result["usage"] = usage
            usage["prompt_breakdown"] = _estimate_prompt_overhead(ctx, view, tools)
            unsupported_capability = str(result.get("unsupported_capability") or "")
            if unsupported_capability:
                if unsupported_capability == "vision" and not vision_downgraded:
                    vision_downgraded = True
                    budget.refund()
                    from crew.core.runctx import current_model_capabilities

                    capabilities = current_model_capabilities.get()
                    if capabilities is not None:
                        current_model_capabilities.set(tuple(
                            item for item in capabilities
                            if str(item).strip().lower() != "vision"
                        ))
                    log.warning(
                        "模型拒绝图片输入，切换为非视觉模式后重试 session=%s",
                        ctx.session_id,
                    )
                    yield ResponseChunk.status_event(
                        rid,
                        "当前模型不支持图片，正在改用非视觉方式继续…",
                        next_seq(),
                    )
                    continue
                yield ResponseChunk.error(
                    rid,
                    str(result.get("provider_error") or "当前模型不支持所需能力"),
                    next_seq(),
                )
                return
            if result.get("overflow"):
                max_overflow_retries = (
                    getattr(self.compactor, "max_overflow_retries", 1)
                    if self.compactor is not None
                    else 0
                )
                if self.compactor is not None and overflow_retries < max_overflow_retries:
                    # 首次/第 N 次命中溢出：先剪枝再摘要，仅当投影前进才重试（防无进展死循环）
                    overflow_retries += 1
                    overflow_pending = True
                    budget.refund()
                    log.info(
                        "命中上下文溢出，启用兜底压缩后重试（第 %d/%d 次）session=%s",
                        overflow_retries,
                        max_overflow_retries,
                        ctx.session_id,
                    )
                    continue
                yield ResponseChunk.error(rid, "上下文超长且无法进一步压缩", next_seq())
                return
            if result.get("error"):
                return  # 已 emit error 帧，结束

            # ---- 流式中断续写（Crew partial stream stub + continuation）----
            #   _call_model 已 emit 过 delta 后遭遇可恢复异常，保留已生成文本，
            #   追加为 assistant message，再注入续写提示后重试。
            if result.get("stream_interrupt"):
                text = result.get("text", "")
                reasoning = result.get("reasoning", "")
                streamed_text += text
                assistant_msg = Message.assistant(text, model=result.get("model") or None)
                if reasoning:
                    assistant_msg.thinking = reasoning
                ctx.messages.append(assistant_msg)
                view_messages.append(assistant_msg)
                if stream_continuation_count >= self.stream_continuation_max:
                    final_text = streamed_text + "\n\n（模型响应多次中断，已保留已生成内容）"
                    async for _fc in self._emit_final(
                        rid, next_seq, ctx.session_id, owner_account_id, final_text,
                    ):
                        yield _fc
                    return
                stream_continuation_count += 1
                budget.refund()
                ctx.messages.append(Message.user(STREAM_INTERRUPT_PROMPT, is_meta=True))
                view_messages.append(Message.user(STREAM_INTERRUPT_PROMPT, is_meta=True))
                yield ResponseChunk.status_event(
                    rid,
                    (
                        f"{STREAM_INTERRUPT_STATUS_MESSAGE}，"
                        f"正在第 {stream_continuation_count}/{self.stream_continuation_max} 次续写"
                    ),
                    next_seq(),
                )
                log.info(
                    "流式中断，第 %d/%d 次续写 session=%s",
                    stream_continuation_count,
                    self.stream_continuation_max,
                    ctx.session_id,
                )
                continue

            text = result.get("text", "")
            if streamed_text:
                text = streamed_text + text
                streamed_text = ""
            tool_calls = result.get("tool_calls", [])
            reasoning = result.get("reasoning", "")
            finish_reason = result.get("finish_reason")
            if has_truncated_tool_args(tool_calls, finish_reason):
                await runner.cancel_prewarms()
                log.warning(
                    "拒绝执行截断的工具参数 session=%s tool_count=%d",
                    ctx.session_id,
                    len(tool_calls),
                )
                configured_max_tokens = getattr(self.provider, "max_tokens", None)
                if (
                    not max_output_tokens_escalated
                    and (
                        not isinstance(configured_max_tokens, int)
                        or configured_max_tokens < ESCALATED_MAX_OUTPUT_TOKENS
                    )
                ):
                    max_output_tokens_escalated = True
                    max_output_tokens_override = ESCALATED_MAX_OUTPUT_TOKENS
                    budget.refund()
                    log.info(
                        "工具参数被截断，提高输出上限至 %d 后重试 session=%s",
                        ESCALATED_MAX_OUTPUT_TOKENS,
                        ctx.session_id,
                    )
                    continue
                max_output_tokens_override = None
                if tool_args_recovery_count < TOOL_ARGUMENTS_RECOVERY_LIMIT:
                    tool_args_recovery_count += 1
                    budget.refund()
                    recovery_message = Message.user(
                        TOOL_ARGUMENTS_RECOVERY_PROMPT,
                        is_meta=True,
                    )
                    ctx.messages.append(recovery_message)
                    view_messages.append(recovery_message)
                    log.info(
                        "工具参数截断，第 %d/%d 次拆分续写 session=%s",
                        tool_args_recovery_count,
                        TOOL_ARGUMENTS_RECOVERY_LIMIT,
                        ctx.session_id,
                    )
                    continue
                yield ResponseChunk.error(
                    rid,
                    "TOOL_ARGUMENTS_INCOMPLETE: 模型输出的工具参数不完整，未执行任何工具。",
                    next_seq(),
                )
                return
            if tool_calls and is_max_tokens_finish(finish_reason):
                # stop_reason=length 时消息必然不完整：整批拒执行，历史只保留文本前缀，
                # 随后走截断续写让模型重新发起完整调用（决策与写入历史同源）。
                await runner.cancel_prewarms()
                log.warning(
                    "截断消息拒执行工具 session=%s tool_count=%d finish_reason=%s",
                    ctx.session_id,
                    len(tool_calls),
                    finish_reason,
                )
                tool_calls = []
            if tool_calls:
                tool_calls = plan_tool_calls(
                    tool_calls,
                    max_delegate_calls=self.max_delegate_tool_calls,
                )
            pre_transform_text = text
            text = await self.plugins.transform_llm_output(
                ctx.session_id,
                text,
                messages=ctx.messages,
                tool_calls=tool_calls,
                reasoning=reasoning,
                finish_reason=finish_reason,
            )
            content_replaced = text != pre_transform_text
            assistant_msg = Message.assistant(text, tool_calls, model=result.get("model") or None)
            # 保存 thinking 内容到 assistant 消息，用于历史回放
            if reasoning:
                assistant_msg.thinking = reasoning
            ctx.messages.append(assistant_msg)
            view_messages.append(assistant_msg)

            if reasoning and not result.get("thinking_emitted"):
                yield ResponseChunk.thinking_event(rid, reasoning, next_seq())

            # ---- 中断/deadline 检查（模型刚产出后 / 流式被中途打断）----
            #   带上已生成的半截文本作 final：前端保留、历史持久化，优雅停止。
            deadline_now = _deadline_hit()
            if deadline_now or (control is not None and control.interrupted):
                if tool_calls:
                    # 中断收尾：未派发的工具调用不写入历史，部分消息只保留文本前缀。
                    tool_calls = []
                    assistant_msg.tool_calls = []
                await self.plugins.post_llm_call(
                    ctx.session_id,
                    ctx.messages,
                    {
                        "text": text,
                        "tool_calls": tool_calls,
                        "reasoning": reasoning,
                        "finish_reason": finish_reason,
                    },
                )
                async for _nc in self._interrupt_notice_chunks(control, rid, next_seq, ctx):
                    yield _nc
                async for _fc in self._emit_final(
                    rid, next_seq, ctx.session_id, owner_account_id, text,
                    replace_content=content_replaced, usage=result.get("usage"),
                    reason="deadline" if deadline_now else None,
                ):
                    yield _fc
                return

            # ---- 空响应重试：既无文本也无工具调用 ----
            if is_empty_response(text, tool_calls, reasoning):
                if empty_retries < self.empty_retry_max:
                    empty_retries += 1
                    budget.refund()  # 空轮不计入预算
                    ctx.messages.append(Message.user(EMPTY_RETRY_NUDGE))
                    view_messages.append(Message.user(EMPTY_RETRY_NUDGE))
                    log.info("空响应，第 %d 次重试 session=%s", empty_retries, ctx.session_id)
                    continue
                async for _fc in self._emit_final(
                    rid, next_seq, ctx.session_id, owner_account_id,
                    "（模型多次未产出有效内容，请重试或调整提问）",
                ):
                    yield _fc
                return

            # ---- late steer：模型调用期间到达的补充指令 ----
            # 当前这次 LLM 请求已经发出，无法在请求中途修改 prompt；若模型本轮没有
            # 产出工具调用且即将 final，则把补充指令接到刚生成的 assistant 后面，再续
            # 一轮模型调用，避免用户点击「引导」后文本只停在 TurnControl 里随 turn 结束丢失。
            if not tool_calls and control is not None:
                late_steer = control.drain_steer()
                if late_steer:
                    _inject_steer(view_messages, late_steer)
                    budget.refund()
                    empty_retries = 0
                    log.info("steer 已在模型回复后接续注入 session=%s", ctx.session_id)
                    continue

            # ---- 无工具调用：可能是截断续写，否则 final ----
            if not tool_calls:
                if should_continue(finish_reason, tool_calls) and continuation_count < self.continuation_max:
                    continuation_count += 1
                    budget.refund()
                    ctx.messages.append(Message.user(CONTINUATION_PROMPT))
                    view_messages.append(Message.user(CONTINUATION_PROMPT))
                    log.info("回复被截断，第 %d 次续写 session=%s", continuation_count, ctx.session_id)
                    continue
                async for _fc in self._emit_final(
                    rid, next_seq, ctx.session_id, owner_account_id, text,
                    replace_content=content_replaced, usage=result.get("usage"),
                ):
                    yield _fc
                return

            # ---- 执行工具（含 guardrails 防循环）----
            #   ToolRunner 把 tool 结果 append 到 ctx.messages；同步到 view_messages（共享对象）。
            _pre_batch_len = len(ctx.messages)
            async for ev in runner.run_batch(
                tool_calls,
                ctx.messages,
                rid,
                next_seq,
                started_tool_call_ids=result.get("started_tool_call_ids"),
            ):
                yield ev
            view_messages.extend(ctx.messages[_pre_batch_len:])

            # 用户拒绝的是本轮目标的执行边界。继续把拒绝结果交给模型会诱发它改用
            # 另一种工具完成同一目标，因此拒绝后直接结束本轮。
            if runner.approval_rejected:
                async for _fc in self._emit_final(
                    rid,
                    next_seq,
                    ctx.session_id,
                    owner_account_id,
                    "操作未执行：你拒绝了本轮安全审批。",
                ):
                    yield _fc
                return
            if runner.security_boundary_failed:
                async for _fc in self._emit_final(
                    rid,
                    next_seq,
                    ctx.session_id,
                    owner_account_id,
                    "操作未执行：安全运行时发生故障，请检查运行时状态后重试。",
                ):
                    yield _fc
                return

            # Plan 模式提交审批后，本轮必须立即停住，等待用户 approve/reject。
            # 不能再把 exit_plan_mode 的工具结果喂回模型，否则弱模型可能继续执行计划。
            if self.plan_manager is not None and self.plan_manager.is_awaiting_approval(
                ctx.session_id,
                owner_account_id=owner_account_id,
            ):
                async for _fc in self._emit_final(rid, next_seq, ctx.session_id, owner_account_id, ""):
                    yield _fc
                return

            # 工具执行后再查一次中断/deadline（用户在工具运行期间点了停止）
            #   空 final：保留已显示的工具结果与文本，仅结束本轮。
            deadline_now = _deadline_hit()
            if deadline_now or (control is not None and control.interrupted):
                async for _nc in self._interrupt_notice_chunks(control, rid, next_seq, ctx):
                    yield _nc
                async for _fc in self._emit_final(
                    rid, next_seq, ctx.session_id, owner_account_id, "",
                    reason="deadline" if deadline_now else None,
                ):
                    yield _fc
                return

            # guardrail 硬停：相同工具失败/无进展达上限 → 收尾
            # 采用 conversation_loop.py:3967-3988：
            #   - 英文 guidance 以 assistant 消息写入对话历史（ctx + view），给模型下回合看
            #   - 用户只看到中文状态消息，看不到给模型的英文指令
            if guardrails.halt_decision is not None:
                decision = guardrails.halt_decision
                guidance = ToolCallGuardrailController.controlled_halt_response(decision)
                ctx.messages.append(Message.assistant(guidance))
                view_messages.append(Message.assistant(guidance))
                user_msg = (
                    f"工具 {decision.tool_name} 已连续失败 {decision.count} 次，"
                    "已自动停止本轮调用。请尝试换一种方式继续。"
                )
                async for _fc in self._emit_final(rid, next_seq, ctx.session_id, owner_account_id, user_msg):
                    yield _fc
                return

            # 预算用尽时再宽限一轮，让模型基于工具结果给出收尾文本
            if budget.remaining == 0 and not used_grace:
                grace = True

        # 达到最大迭代次数（主 agent 默认无限，仅 subagent/显式设上限时触发）
        async for _fc in self._emit_final(
            rid, next_seq, ctx.session_id, owner_account_id,
            "（已达到最大迭代次数，任务可能未完全完成）",
            reason="max_iterations", usage=result.get("usage"),
        ):
            yield _fc

    def _todo_reminder(self, session_id: str, *, consume: bool) -> str | None:
        """读取本 step 的 todo reminder；preview 只读，实际发送才消费。"""
        if self.plan_manager is None:
            return None
        from crew.core.runctx import current_owner_account_id

        owner = current_owner_account_id.get()
        if consume:
            return self.plan_manager.take_todo_reminder(
                session_id, owner_account_id=owner
            )
        peek = getattr(self.plan_manager, "peek_todo_reminder", None)
        return peek(session_id, owner_account_id=owner) if callable(peek) else None

    # ------------------------------------------------------------------ #
    async def _call_model(
        self,
        request_view: FinalRequestView,
        rid: str,
        next_seq,
        result: dict,
        control=None,
        runner=None,
        session_id: str = "",
    ) -> AsyncIterator[ResponseChunk]:
        """调模型一轮：流式 yield delta，结果写入 result。

        流式提前派发（Crew StreamingToolExecutor）：流中每收到一帧 ready_tool_call
        （某工具参数已拼完），立即交给 runner.prewarm() 把 safe 工具跑起来——与流式
        剩余部分重叠执行。响应被丢弃/重试时，先 cancel 掉本 attempt 的 prewarm。

        四重保护（前两种仅在「尚未 emit 过 delta」时才可恢复）：
          - 瞬时错误（retryable）→ 同 provider 指数退避重试（可选 jitter）；
          - 重试耗尽 / 非瞬时错误 → 切到下一个 fallback provider；
          - 上下文溢出错误 → 置 result["overflow"]=True，交由主循环压缩后重试；
          - 已 emit delta 后失败 → 保留已生成文本，标记 result["stream_interrupt"]=True，
            由主循环注入续写提示后重试（Crew partial stream stub + continuation）。
        失败时 yield 一帧 error。

        流式中途中断：用户点停止时，每吐一段就检查 control.interrupted，命中即跳出流，
        保留已 emit 的半截文本（result["text"]），由主循环优雅收尾——实现「立刻停 + 留内容」。
        """
        providers = provider_chain(self.provider, self.fallback_providers)
        prov_idx = 0
        attempt = 0
        while True:
            provider = providers[prov_idx]
            accumulated = ""
            tool_calls: list[Any] = []
            reasoning = ""
            finish_reason = None
            emitted = False
            thinking_emitted = False
            started_tool_call_ids: set[str] = set()
            generating_tool_call_signatures: dict[str, str] = {}
            visible_started_tool_calls: dict[str, Any] = {}
            t0 = time.perf_counter()
            middleware_ready: float | None = None
            first_event: float | None = None
            first_reasoning: float | None = None
            first_text: float | None = None
            try:
                _prov_model = getattr(provider, "model", "") or ""
                _prov_name = type(provider).__name__
                _prov_base_url = getattr(provider, "base_url", "") or ""

                middleware_view, mw = await self.finalize_request_view(
                    request_view,
                    session_id=session_id,
                    request_id=rid,
                    provider_index=prov_idx,
                    provider=provider,
                )
                effective_request = middleware_view.to_payload()
                sent_view_holder = [middleware_view]

                def _stream(req, active_provider=provider):
                    final_view = FinalRequestView.from_payload(
                        req if isinstance(req, dict) else {},
                        middleware_view,
                        model=_prov_model,
                        provider=_prov_name,
                        base_url=_prov_base_url,
                        provider_index=prov_idx,
                    )
                    sent_view_holder[0] = final_view
                    messages_arg = list(final_view.messages)
                    tools_arg = list(final_view.tools) if final_view.tools is not None else None
                    max_tokens_arg = final_view.max_output_tokens
                    if max_tokens_arg is None:
                        return active_provider.stream_chat(messages_arg, tools=tools_arg)
                    try:
                        stream_params = inspect.signature(active_provider.stream_chat).parameters
                        accepts_max_tokens = "max_tokens" in stream_params or any(
                            param.kind == inspect.Parameter.VAR_KEYWORD
                            for param in stream_params.values()
                        )
                    except (TypeError, ValueError):
                        accepts_max_tokens = True
                    if accepts_max_tokens:
                        return active_provider.stream_chat(
                            messages_arg,
                            tools=tools_arg,
                            max_tokens=max_tokens_arg,
                        )
                    return active_provider.stream_chat(messages_arg, tools=tools_arg)

                stream = await self.plugins.run_llm_execution_middleware(
                    effective_request,
                    _stream,
                    session_id=session_id,
                    request_id=rid,
                    provider_index=prov_idx,
                    original_request=mw.original_payload,
                    model=_prov_model,
                    provider=_prov_name,
                    base_url=_prov_base_url,
                )
                middleware_ready = time.perf_counter() - t0
                async for chunk in stream:
                    event_elapsed = time.perf_counter() - t0
                    if first_event is None:
                        first_event = event_elapsed
                        first_kind = (
                            "reasoning" if chunk.reasoning_content else
                            "text" if chunk.delta_text else
                            "tool" if chunk.tool_call_generating or chunk.ready_tool_call else
                            "done" if chunk.done else "other"
                        )
                        log.info(
                            "[PERF] llm_first_event request_id=%s session=%s provider=%s model=%s "
                            "elapsed=%.3fs kind=%s",
                            rid,
                            session_id,
                            _prov_name,
                            _prov_model,
                            event_elapsed,
                            first_kind,
                        )
                    if chunk.reasoning_content and first_reasoning is None:
                        first_reasoning = event_elapsed
                    if chunk.reasoning_content and not chunk.done:
                        merged_reasoning = self._merge_streaming_reasoning(
                            reasoning,
                            chunk.reasoning_content,
                        )
                        if merged_reasoning != reasoning:
                            reasoning = merged_reasoning
                            thinking_emitted = True
                            yield ResponseChunk.thinking_event(rid, reasoning, next_seq())
                    if chunk.delta_text:
                        if first_text is None:
                            first_text = event_elapsed
                            log.info(
                                "[PERF] llm_first_text request_id=%s session=%s provider=%s model=%s "
                                "elapsed=%.3fs",
                                rid,
                                session_id,
                                _prov_name,
                                _prov_model,
                                event_elapsed,
                            )
                        accumulated += chunk.delta_text
                        emitted = True
                        yield ResponseChunk.delta(rid, chunk.delta_text, next_seq())
                        # 流式中途中断：保留已吐文本，停止消费剩余流
                        if control is not None and control.interrupted:
                            log.info("流式中途被用户中断 session=%s", rid)
                            break
                    # Crew：模型已经开始生成工具参数，但完整 args 未到齐。这里只
                    # 显示 generating 卡片，不执行、不占用 started_tool_call_ids；ready 到来
                    # 时会再发 start，表示工具真正进入可执行阶段。旧 provider 的
                    # tool_call_seen 也映射到 generating，保持兼容。
                    generating_tc = chunk.tool_call_generating or chunk.tool_call_seen
                    if generating_tc is not None and runner is not None:
                        tc = generating_tc
                        signature = f"{tc.name}:{repr(tc.arguments)}"
                        if generating_tool_call_signatures.get(tc.id) != signature:
                            generating_tool_call_signatures[tc.id] = signature
                            visible_started_tool_calls[tc.id] = tc
                            yield runner._generating_event(tc, rid, next_seq)
                    # 参数拼完 → prewarm safe 工具（用完整参数起跑）；所有工具都发
                    # start，让 UI 从“生成参数中”切到“执行中”。若 provider 没有提前
                    # 生成中信号，ready 时也会兜底发 start。
                    if chunk.ready_tool_call is not None and runner is not None:
                        tc = chunk.ready_tool_call
                        runner.prewarm(tc)  # unsafe 返回 False 无妨；safe 用完整参数起跑
                        if tc.id not in started_tool_call_ids:
                            visible_started_tool_calls[tc.id] = tc
                            started_tool_call_ids.add(tc.id)
                            yield runner._start_event(tc, rid, next_seq)
                        else:
                            # 已经发过 start；用完整参数覆盖 visible，使后续重试补
                            # cancelled result 时带完整参数更可读。
                            visible_started_tool_calls[tc.id] = tc
                    if chunk.done:
                        tool_calls = chunk.tool_calls
                        final_reasoning = chunk.reasoning_content or reasoning
                        if final_reasoning != reasoning:
                            reasoning = final_reasoning
                            if thinking_emitted:
                                yield ResponseChunk.thinking_event(rid, reasoning, next_seq())
                        finish_reason = chunk.finish_reason
                        if chunk.usage:
                            result["usage"] = chunk.usage
                elapsed = time.perf_counter() - t0
                sent_view = sent_view_holder[0]
                sent_messages = list(sent_view.messages)
                sent_tools = list(sent_view.tools) if sent_view.tools is not None else None
                request_view_tokens = sent_view.estimated_prompt_tokens()
                message_chars = sum(len(message.text_content) for message in sent_messages)
                log.info(
                    "[PERF] llm prov=%d  middleware=%.3fs  first_event=%.3fs  "
                    "first_reasoning=%.3fs  first_text=%.3fs  ttft=%.3fs  total=%.3fs  "
                    "messages=%d  chars=%d  tools=%d  tokens_approx=%d  request_id=%s session=%s "
                    "provider=%s model=%s",
                    prov_idx,
                    middleware_ready if middleware_ready is not None else -1.0,
                    first_event if first_event is not None else -1.0,
                    first_reasoning if first_reasoning is not None else -1.0,
                    first_text if first_text is not None else -1.0,
                    first_text or 0.0,
                    elapsed,
                    len(sent_messages),
                    message_chars,
                    len(sent_tools or []),
                    len(accumulated) // 4,
                    rid,
                    session_id,
                    _prov_name,
                    _prov_model,
                )
                result.update(
                    text=accumulated, tool_calls=tool_calls,
                    reasoning=reasoning, finish_reason=finish_reason,
                    thinking_emitted=thinking_emitted,
                    started_tool_call_ids=started_tool_call_ids,
                    model=str(getattr(provider, "model", "") or ""),
                )
                usage = result.get("usage")
                if not isinstance(usage, dict):
                    usage = {}
                    result["usage"] = usage
                usage["request_view_tokens"] = request_view_tokens
                if not isinstance(usage.get("prompt_tokens"), (int, float)):
                    usage["prompt_tokens"] = request_view_tokens
                    usage["prompt_tokens_source"] = "request_view"
                else:
                    usage["prompt_tokens_source"] = "provider"
                await self.plugins.post_api_request(
                    session_id=session_id,
                    model=getattr(provider, "model", ""),
                    provider=type(provider).__name__,
                    usage=result.get("usage") or {},
                    api_duration=elapsed,
                    finish_reason=finish_reason or "",
                )
                return  # 成功
            except Exception as exc:  # noqa: BLE001
                # 本 attempt 的响应将被丢弃/重试：取消已提前派发的工具，避免悬挂任务与
                # 重复执行（reads 幂等，取消是为干净与省资源）。成功路径不会走到这里。
                if runner is not None:
                    await runner.cancel_prewarms()
                    if visible_started_tool_calls:
                        for tc in visible_started_tool_calls.values():
                            yield runner._result_event(
                                tc,
                                ToolResult(
                                    tc.id,
                                    tc.name,
                                    "模型流中断，本次提前显示的工具调用已取消。",
                                    is_error=True,
                                ),
                                rid,
                                next_seq,
                                status="cancelled",
                            )
                        visible_started_tool_calls.clear()
                        started_tool_call_ids.clear()
                if emitted:
                    if control is not None and control.interrupted:
                        # 用户停止/引导导致底层流被关闭时，provider 可能抛 timeout/connection 类异常。
                        # 这不是需要续写的网络故障；应保留已吐文本并让外层中断检查封口。
                        log.info(
                            "LLM 流式在用户中断后结束（已 emit %d 字符），跳过续写 session=%s",
                            len(accumulated),
                            session_id,
                        )
                        result.update(
                            text=accumulated,
                            tool_calls=[],
                            reasoning=reasoning,
                            finish_reason="interrupt",
                            model=str(getattr(provider, "model", "") or ""),
                        )
                        return
                    # 流式中途失败：保留已生成文本，尝试续写
                    exc_info = f"{type(exc).__name__}: {str(exc) or '(无详情)'}"
                    log.warning(
                        "LLM 流式中途失败（已 emit %d 字符），原因：%s，尝试续写",
                        len(accumulated), exc_info,
                    )
                    result.update(
                        text=accumulated,
                        tool_calls=[],
                        reasoning=reasoning,
                        finish_reason="interrupt",
                        model=str(getattr(provider, "model", "") or ""),
                    )
                    if not is_stream_interrupt_recoverable(exc):
                        yield _llm_error_chunk(
                            rid,
                            exc,
                            next_seq(),
                            message=f"模型响应中断，已保留已生成内容。错误：{exc}",
                        )
                        result["error"] = True
                        return
                    result["stream_interrupt"] = True
                    return
                # 上下文溢出：静默交主循环压缩后重试本轮（不向用户吐 error 帧）
                if is_context_overflow(exc) and self.compactor is not None:
                    result["overflow"] = True
                    return
                if (
                    isinstance(exc, ProviderError)
                    and exc.kind == CrewErrorKind.UNSUPPORTED_CAPABILITY
                    and exc.capability
                ):
                    result.update(
                        unsupported_capability=exc.capability,
                        provider_error=str(exc),
                    )
                    return
                retryable = isinstance(exc, CrewError) and exc.is_retryable()
                if retryable and attempt < self.max_retries:
                    attempt += 1
                    delay = self.backoff_seconds * (2 ** (attempt - 1))
                    if self.stream_retry_jitter:
                        delay = delay * (0.5 + random.random() * 0.5)
                    # 服务端建议的退避（Retry-After / body retry_delay）优先于本地
                    # 指数退避，并封顶防异常大值拖死重试循环。
                    retry_delay = exc.retry_delay if isinstance(exc, CrewError) else None
                    if retry_delay is not None and retry_delay > 0:
                        delay = min(retry_delay, _RETRY_DELAY_CAP_SECONDS)
                    exc_info = f"{type(exc).__name__}: {str(exc) or '(无详情)'}"
                    log.warning("LLM 瞬时失败，第 %d 次重试（%.1fs 后）：%s", attempt, delay, exc_info)
                    await asyncio.sleep(delay)
                    continue
                # 切下一个 fallback provider
                if prov_idx + 1 < len(providers):
                    prov_idx += 1
                    attempt = 0
                    exc_info = f"{type(exc).__name__}: {str(exc) or '(无详情)'}"
                    log.warning("provider 故障，切换到 fallback #%d：%s", prov_idx, exc_info)
                    continue
                log.exception("LLM 调用异常，无 fallback 可用")
                result["error"] = True
                yield _llm_error_chunk(rid, exc, next_seq())
                return

    @staticmethod
    def _merge_streaming_reasoning(current: str, incoming: str) -> str:
        """合并 provider 的 reasoning 流式片段，兼容“增量片段”和“累计全文”两种形态。"""
        if not incoming:
            return current
        if not current:
            return incoming
        if incoming == current:
            return current
        if incoming.startswith(current):
            return incoming
        return current + incoming
