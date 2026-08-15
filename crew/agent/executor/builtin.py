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
from typing import Any, AsyncIterator

from crew.agent.executor.base import AgentExecutor, ExecutionContext
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
    is_stream_interrupt_recoverable,
    provider_chain,
    should_continue,
)
from crew.agent.loop.tool_dispatch_helpers import plan_tool_calls
from crew.core.envelope import ResponseChunk
from crew.core.errors import ProviderError
from crew.core.interfaces import LLMProvider, ToolRegistry
from crew.core.types import Message, ToolResult
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
        # 用于检测 plan 模式是否刚退出，以便注入一次性 exit reminder。
        self._plan_was_active = False

    def _pre_final_chunks(
        self,
        rid: str,
        next_seq,
        session_id: str,
        owner_account_id: str = "",
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
            log.warning(
                "file_changes 对账失败 session=%s type=%s",
                session_id,
                type(exc).__name__,
            )
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

    # ------------------------------------------------------------------ #
    async def execute(self, ctx: ExecutionContext) -> AsyncIterator[ResponseChunk]:
        rid = ctx.request_id
        seq = 0

        def next_seq() -> int:
            nonlocal seq
            seq += 1
            return seq

        system_msg = Message.system(ctx.system_prompt)
        original_tools = ctx.tool_schemas or []
        # 披露模式不改变授权范围。DIRECT 直接发送全部已授权 schema；
        # PROGRESSIVE 才按全局 ToolSearch 配置装配。
        ts_config = (
            ToolSearchConfig(enabled="off")
            if ctx.tool_disclosure_mode is ToolDisclosureMode.DIRECT
            else None
        )
        tool_search_assembly = assemble_tool_schemas(original_tools, config=ts_config)
        deferred_tools_message = available_deferred_tools_message(tool_search_assembly)
        discovered_tool_names = extract_discovered_tool_names(
            ctx.messages,
            original_tool_schemas=tool_search_assembly.original_tool_schemas,
            config=tool_search_assembly.config,
        )
        tools = expand_discovered_tool_schemas(
            tool_search_assembly,
            discovered_tool_names,
        ) or None
        # 0 = 无限；靠 auto-compact 管上下文 + guardrail 防失控。
        # 用 None 判而非 `or`，避免 0 被 `or` 当 falsy 跳过。
        max_iter = ctx.max_iterations if ctx.max_iterations is not None else self.max_iterations
        control = ctx.control

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
        overflow_mode = False  # 命中上下文溢出后，发给 LLM 的视图持续走 force_compact

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
            tools = expand_discovered_tool_schemas(
                tool_search_assembly,
                runner.discovered_tool_names,
            ) or None
            used_grace = grace
            grace = False

            # ---- 中断检查（轮初安全点）----
            #   空 final：前端不覆盖已流式内容，仅结束本轮（保留之前已生成的部分）。
            if control is not None and control.interrupted:
                async for _fc in self._emit_final(rid, next_seq, ctx.session_id, owner_account_id, ""):
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
            #   overflow_mode 是 provider 报溢出后的紧急兜底，从全量 ctx.messages 重新激进压缩。
            if overflow_mode and self.compactor is not None:
                yield ResponseChunk.compaction_event(rid, True, next_seq())
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
                    view_messages = await force_compact(ctx.messages, ctx.session_id, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    log.warning("force_compact 失败，按原视图发送 type=%s", type(exc).__name__)
                    view_messages = list(ctx.messages)
                finally:
                    yield ResponseChunk.compaction_event(rid, False, next_seq())
            elif self.compactor is not None:
                will_compact_view = getattr(self.compactor, "will_compact_view", None)
                show_compaction = bool(will_compact_view and will_compact_view(
                    view_messages,
                    ctx.session_id,
                    owner_account_id=owner_account_id,
                ))
                if show_compaction:
                    yield ResponseChunk.compaction_event(rid, True, next_seq())
                try:
                    view_messages = await self.compactor.compact_view(
                        view_messages,
                        ctx.session_id,
                        owner_account_id=owner_account_id,
                    )
                finally:
                    if show_compaction:
                        yield ResponseChunk.compaction_event(rid, False, next_seq())
            view = view_messages
            api_messages = [system_msg]
            if deferred_tools_message:
                api_messages.append(Message.user(deferred_tools_message, is_meta=True))
            api_messages.extend(view)
            api_messages = self._maybe_append_todo_reminder(api_messages, ctx.session_id)
            hook_started = time.perf_counter()
            pre_llm_result = await self.plugins.pre_llm_call(ctx.session_id, api_messages)
            log.info(
                "[PERF] pre_llm_hooks    %.3fs  (messages=%d)",
                time.perf_counter() - hook_started,
                len(api_messages),
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

            _dump_prompt(ctx, view, tools, budget.used)

            # ---- 调模型（流式重试 + 溢出压缩 + provider 故障转移 + 流式中途中断）----
            result: dict[str, Any] = {}
            async for ev in self._call_model(
                api_messages,
                tools,
                rid,
                next_seq,
                result,
                control,
                runner,
                ctx.session_id,
                max_tokens=max_output_tokens_override,
            ):
                yield ev
            # 估算 prompt 固定开销（系统/技能·上下文/工具定义），并入 usage 透传到前端 breakdown
            result.setdefault("usage", {})["prompt_breakdown"] = _estimate_prompt_overhead(ctx, view, tools)
            if result.get("overflow"):
                if self.compactor is not None and not overflow_mode:
                    # 首次命中溢出：静默开启压缩模式并重试本轮（退还本轮预算）
                    overflow_mode = True
                    budget.refund()
                    log.info("命中上下文溢出，启用兜底压缩后重试 session=%s", ctx.session_id)
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

            # ---- 中断检查（模型刚产出后 / 流式被中途打断）----
            #   带上已生成的半截文本作 final：前端保留、历史持久化，优雅停止。
            if control is not None and control.interrupted:
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
                async for _fc in self._emit_final(
                    rid, next_seq, ctx.session_id, owner_account_id, text,
                    replace_content=content_replaced, usage=result.get("usage"),
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

            # Plan 模式提交审批后，本轮必须立即停住，等待用户 approve/reject。
            # 不能再把 exit_plan_mode 的工具结果喂回模型，否则弱模型可能继续执行计划。
            if self.plan_manager is not None and self.plan_manager.is_awaiting_approval(
                ctx.session_id,
                owner_account_id=owner_account_id,
            ):
                async for _fc in self._emit_final(rid, next_seq, ctx.session_id, owner_account_id, ""):
                    yield _fc
                return

            # 工具执行后再查一次中断（用户在工具运行期间点了停止）
            #   空 final：保留已显示的工具结果与文本，仅结束本轮。
            if control is not None and control.interrupted:
                async for _fc in self._emit_final(rid, next_seq, ctx.session_id, owner_account_id, ""):
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

    def _maybe_append_todo_reminder(self, api_messages: list, session_id: str) -> list:
        """审批通过后的内部 todo_reminder 末尾注入；不进入历史，不渲染给用户。"""
        if self.plan_manager is None:
            return api_messages
        from crew.core.runctx import current_owner_account_id

        owner = current_owner_account_id.get()
        text = self.plan_manager.take_todo_reminder(session_id, owner_account_id=owner)
        if not text:
            return api_messages
        return [*api_messages, Message.system_reminder(text)]

    # ------------------------------------------------------------------ #
    async def _call_model(
        self,
        api_messages: list,
        tools: list | None,
        rid: str,
        next_seq,
        result: dict,
        control=None,
        runner=None,
        session_id: str = "",
        max_tokens: int | None = None,
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

                request = {"messages": api_messages, "tools": tools}
                if max_tokens is not None:
                    request["max_tokens"] = max_tokens
                mw = await self.plugins.apply_llm_request_middleware(
                    request,
                    session_id=session_id,
                    request_id=rid,
                    provider_index=prov_idx,
                    model=_prov_model,
                    provider=_prov_name,
                    base_url=_prov_base_url,
                )
                effective_request = mw.payload if isinstance(mw.payload, dict) else request
                effective_tools = effective_request.get("tools", tools)

                def _stream(req, active_provider=provider):
                    messages_arg = req["messages"] if "messages" in req else api_messages
                    tools_arg = req["tools"] if "tools" in req else tools
                    max_tokens_arg = req.get("max_tokens", max_tokens)
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
                message_chars = sum(len(message.text_content) for message in api_messages)
                log.info(
                    "[PERF] llm prov=%d  middleware=%.3fs  first_event=%.3fs  "
                    "first_reasoning=%.3fs  first_text=%.3fs  ttft=%.3fs  total=%.3fs  "
                    "messages=%d  chars=%d  tools=%d  tokens_approx=%d",
                    prov_idx,
                    middleware_ready if middleware_ready is not None else -1.0,
                    first_event if first_event is not None else -1.0,
                    first_reasoning if first_reasoning is not None else -1.0,
                    first_text if first_text is not None else -1.0,
                    first_text or 0.0,
                    elapsed,
                    len(api_messages),
                    message_chars,
                    len(tools or []),
                    len(accumulated) // 4,
                )
                result.update(
                    text=accumulated, tool_calls=tool_calls,
                    reasoning=reasoning, finish_reason=finish_reason,
                    thinking_emitted=thinking_emitted,
                    started_tool_call_ids=started_tool_call_ids,
                    model=str(getattr(provider, "model", "") or ""),
                )
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
                    exc_info = type(exc).__name__
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
                        yield ResponseChunk.error(
                            rid,
                            "模型响应中断，已保留已生成内容。内部错误",
                            next_seq(),
                        )
                        result["error"] = True
                        return
                    result["stream_interrupt"] = True
                    return
                # 上下文溢出：静默交主循环压缩后重试本轮（不向用户吐 error 帧）
                if is_context_overflow(exc) and self.compactor is not None:
                    result["overflow"] = True
                    return
                retryable = isinstance(exc, ProviderError) and exc.retryable
                if retryable and attempt < self.max_retries:
                    attempt += 1
                    delay = self.backoff_seconds * (2 ** (attempt - 1))
                    if self.stream_retry_jitter:
                        delay = delay * (0.5 + random.random() * 0.5)
                    exc_info = type(exc).__name__
                    log.warning("LLM 瞬时失败，第 %d 次重试（%.1fs 后）：%s", attempt, delay, exc_info)
                    await asyncio.sleep(delay)
                    continue
                # 切下一个 fallback provider
                if prov_idx + 1 < len(providers):
                    prov_idx += 1
                    attempt = 0
                    exc_info = type(exc).__name__
                    log.warning("provider 故障，切换到 fallback #%d：%s", prov_idx, exc_info)
                    continue
                log.error("LLM 调用异常，无 fallback 可用 type=%s", type(exc).__name__)
                result["error"] = True
                yield ResponseChunk.error(rid, "模型调用失败：内部错误", next_seq())
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
