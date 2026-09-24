"""三层渐进式压缩流水线：L1 规则 → L2 增量复用 → L3 全量 LLM。

对外暴露 ``ContextCompactor``，保持 ``maybe_compact`` / ``force_compact`` 接口。
压缩只作用于「发给 LLM 的视图」，不破坏 canonical 历史（调用方负责持久化完整历史）。

三层成本递增：
- L1 MicroCompact：纯规则清理旧工具结果，零 LLM，每轮跑。
- L2 增量摘要：复用缓存的旧摘要，只摘要新增轮次（新增很少时纯规则、零 LLM）。
- L3 全量摘要：无可复用摘要时，调 LLM 摘要整段较早历史。

另含两项稳健性机制：
- 防抖（anti-thrash）：连续两次压缩各省 <10% 时跳过摘要，避免反复无效压缩。
- L2 缓存持久化：摘要状态可落 SQLite（SummaryStore），跨重启零推理复用。

todo 状态不在此处重注入：Crew 由 runtime._plan_reminder_blocks 每轮注入
最新 todo 快照（与压缩解耦），压缩后下一轮请求构建时即重新带上，无空窗。
故压缩器保持单一职责，不感知 plan/todo。
"""

from __future__ import annotations

import asyncio
from functools import wraps
from collections.abc import Callable

from crew.agent.compact.file_manifest import (
    is_file_manifest_message,
    shadowed_manifest_message,
)
from crew.agent.compact.meter import CheckpointLoader, TokenMeter
from crew.agent.compact.microcompact import (
    ResultPolicyResolver,
    micro_compact,
    prune_tool_results,
)

from crew.agent.compact.post_compact import (
    build_post_compact_attachments,
    build_post_compact_file_attachments,
)
from crew.agent.compact.store import SummaryState, SummaryStore
from crew.agent.compact.summary import (
    SUMMARY_MARKER,
    summarize_full,
    summarize_incremental,
)
from crew.agent.compact.tokens import estimate_tokens
from crew.core.interfaces import LLMProvider
from crew.core.runctx import current_owner_account_id
from crew.core.observability import SYSTEM_OWNER_ACCOUNT_ID, span
from crew.core.types import Message
from crew.state.logging import get_logger

log = get_logger("agent.compact")

# 防抖：单次压缩省比低于此值算「无效」；连续 2 次无效则跳过摘要。
_INEFFECTIVE_SAVINGS_PCT = 0.10
_MAX_INEFFECTIVE = 2

# 断路器：连续 N 次摘要失败则暂停摘要，避免反复调用 LLM 死循环。
_MAX_CONSECUTIVE_FAILURES = 3

# old 段少于此条数时跳过 L2/L3 摘要：摘要 1-2 条消息的 LLM 开销 > 收益
# （摘要本身长度可能≈原文），等 micro_compact + 溢出兜底处理即可。
_MIN_OLD_FOR_SUMMARY = 3
SummaryKey = tuple[str, str]

# compaction 事件自包含落库（session_events 只增不改）：
# sink(session_id, owner_account_id, summary, covered_count, view_estimate)。
CompactionEventSink = Callable[[str, str, str, int, int], None]


def _observed_compaction(operation: str):
    """Add one stable observation boundary to each public compaction operation."""
    def decorate(function):
        @wraps(function)
        async def wrapped(self, *args, **kwargs):
            raw_session_id = kwargs.get("session_id")
            if raw_session_id is None and len(args) > 1:
                raw_session_id = args[1]
            session_id = raw_session_id if isinstance(raw_session_id, str) else ""
            owner = kwargs.get("owner_account_id")
            if owner is None and len(args) > 2:
                owner = args[2]
            if not isinstance(owner, str) or not owner:
                owner = SYSTEM_OWNER_ACCOUNT_ID
            with span(
                "context.compact",
                module="agent",
                component="compact",
                operation=operation,
                owner_account_id=owner,
                session_id=session_id,
                attributes={"session_id": session_id},
            ):
                return await function(self, *args, **kwargs)
        return wrapped
    return decorate


class ContextCompactor:
    """三层渐进式上下文压缩。

    保留最近 keep_recent 条，并向最近的安全边界（user/assistant）对齐——
    避免切断 assistant(tool_calls) 与其 tool 结果的配对（否则 OpenAI 接口报错）。
    """

    def __init__(
        self,
        provider: LLMProvider,
        *,
        enabled: bool = True,
        token_budget: int = 128000,
        keep_recent: int = 8,
        keep_recent_tools: int = 6,
        l2_incremental: bool = True,
        l2_delta_threshold: int = 2000,
        post_compact_files: int = 3,
        post_compact_max_chars_per_file: int = 5000,
        post_compact_max_instructions: int = 5,
        post_compact_max_important: int = 8,
        post_compact_max_instruction_chars: int = 20000,
        post_compact_max_important_chars: int = 5000,
        post_compact_max_total_chars: int = 140000,
        max_tool_result_chars: int = 0,
        history_db_path: str = "",
        store: SummaryStore | None = None,
        result_policy_resolver: ResultPolicyResolver | None = None,
        max_overflow_retries: int = 1,
        summary_max_tokens: int = 8192,
        summary_prefix_reuse: bool = True,
        meter_checkpoint_loader: CheckpointLoader | None = None,
        event_sink: CompactionEventSink | None = None,
    ) -> None:
        self.provider = provider
        self.enabled = enabled
        self.token_budget = token_budget
        self.keep_recent = keep_recent
        self.keep_recent_tools = keep_recent_tools
        self.l2_incremental = l2_incremental
        self.l2_delta_threshold = l2_delta_threshold
        self.post_compact_files = post_compact_files
        self.post_compact_max_chars_per_file = post_compact_max_chars_per_file
        self.post_compact_max_instructions = post_compact_max_instructions
        self.post_compact_max_important = post_compact_max_important
        self.post_compact_max_instruction_chars = post_compact_max_instruction_chars
        self.post_compact_max_important_chars = post_compact_max_important_chars
        self.post_compact_max_total_chars = post_compact_max_total_chars
        self.max_tool_result_chars = max_tool_result_chars
        self.history_db_path = history_db_path
        self.store = store
        self.result_policy_resolver = result_policy_resolver
        # overflow 后 compact-retry 次数上限（每次 overflow 序列只压缩重试一次）。
        self.max_overflow_retries = max(0, max_overflow_retries)
        # 摘要调用策略：maxTokens 封顶（截断视为失败）；prefix_reuse 决定复用会话
        # 前缀（KV cache）还是隔离调用，按 provider 能力再细化。
        self.summary_max_tokens = max(0, summary_max_tokens)
        self.summary_prefix_reuse = summary_prefix_reuse
        # 水位判断的消费入口：无锚点时退化为 estimate_tokens，行为与旧实现一致。
        self._meter = TokenMeter(meter_checkpoint_loader)
        # compaction 事件自包含落库 sink（SQLiteSessionStore.record_compaction_checkpoint
        # 形态）；None 时压缩结果只进 SummaryStore，不进会话事件流。
        self._event_sink = event_sink
        # store 为 None 时退化为进程内缓存（重启即失，自动降级 L3）。
        self._mem: dict[SummaryKey, SummaryState] = {}
        # 每个 session 连续摘要失败次数，用于断路器。
        self._failure_counts: dict[SummaryKey, int] = {}
        # compact_view 专用防抖状态（与 canonical L2 state 隔离，不读写 store）。
        # key 带 __view__:: 前缀，与 canonical _key 不冲突，可安全共用 _failure_counts。
        self._view_mem: dict[SummaryKey, SummaryState] = {}

    def record_meter_usage(
        self,
        session_id: str,
        owner_account_id: str,
        *,
        prompt_tokens: int,
        source: str,
        fingerprint: str,
        view_estimate: int,
    ) -> bool:
        """把本轮真实 usage 记为计量锚点（delta 自此按视图增量累加）。"""
        return self._meter.record_usage(
            session_id,
            owner_account_id,
            prompt_tokens=prompt_tokens,
            source=source,
            fingerprint=fingerprint,
            view_estimate=view_estimate,
        )

    def _measured_tokens(
        self, session_id: str | None, owner_account_id: str | None, messages: list[Message]
    ) -> int:
        if not session_id:
            return estimate_tokens(messages)
        owner = current_owner_account_id.get() if owner_account_id is None else owner_account_id
        return self._meter.measure(session_id, owner or "", messages).tokens

    # ---- 摘要状态读写：有 store 走 SQLite，否则走进程内 ---- #
    @staticmethod
    def _key(session_id: str | None, owner_account_id: str | None = None) -> SummaryKey | None:
        if not session_id:
            return None
        owner = current_owner_account_id.get() if owner_account_id is None else owner_account_id
        return owner or "", session_id

    def _get_state(self, session_id: str | None, owner_account_id: str | None = None) -> SummaryState | None:
        key = self._key(session_id, owner_account_id)
        if key is None:
            return None
        if self.store is not None:
            return self.store.get(key[1], owner_account_id=key[0])
        return self._mem.get(key)

    def _put_state(
        self,
        session_id: str | None,
        state: SummaryState,
        owner_account_id: str | None = None,
    ) -> None:
        key = self._key(session_id, owner_account_id)
        if key is None:
            return
        if self.store is not None:
            self.store.put(key[1], state, owner_account_id=key[0])
        else:
            self._mem[key] = state

    @staticmethod
    def _pairing_balances(messages: list[Message]) -> list[int]:
        """工具配对余额前缀和：assistant 按 tool-call 块数 +N，tool result −1。

        切点（cut before i）平衡当且仅当 bal[i] == 0——切点两侧不拆散
        toolCall 与其 results 组，否则下一轮请求违反 provider 配对约束。
        """
        bal = [0] * (len(messages) + 1)
        for i, m in enumerate(messages):
            if m.role == "assistant":
                delta = len(m.tool_calls)
            elif m.role == "tool":
                delta = -1
            else:
                delta = 0
            bal[i + 1] = bal[i] + delta
        return bal

    @staticmethod
    def _safe_split(messages: list[Message], keep_recent: int) -> int:
        """返回 recent 段起始下标，确保不切断 assistant(tool_calls)↔tool 配对。

        安全边界为「user 或 assistant 且配对余额为 0」的消息：recent 以
        assistant 开头时，其 tool_calls 的结果紧随其后、同在 recent 内，配对完整。
        长回合内只有回合开头一个 user 边界，接受 assistant 边界才能让回合内早期
        迭代被摘要——否则整个回合都受保护，回合内 L3 永不触发。

        策略（按优先级）：
        1. 从倒数 keep_recent 处出发；
        2. 若该处已是安全边界，直接返回；
        3. 优先向后（往最近消息）找安全边界：收缩 recent，让 old 可压缩；
        4. 向后找不到则向前（往更早消息）找：扩展 recent，保住完整配对；
        5. 保底：找不到安全边界则返回 0（安全降级，不压缩）。
        """
        n = len(messages)
        if n <= keep_recent:
            return 0

        bal = ContextCompactor._pairing_balances(messages)

        def _is_boundary(index: int) -> bool:
            return messages[index].role in ("user", "assistant") and bal[index] == 0

        start = max(0, n - keep_recent)
        if _is_boundary(start):
            return start

        # 向后收缩：找下一个安全边界，让 old 以完整配对结尾
        for i in range(start, n):
            if _is_boundary(i):
                return i

        # 向前扩展：找上一个安全边界，保住完整配对
        for i in range(start - 1, -1, -1):
            if _is_boundary(i):
                return i

        # 无安全边界，安全降级
        return 0

    @staticmethod
    def _view_key(session_id: str | None, owner_account_id: str | None = None) -> SummaryKey | None:
        """compact_view 专用 key：与 canonical _key 隔离，避免污染 L2 state。"""
        base = ContextCompactor._key(session_id, owner_account_id)
        if base is None:
            return None
        owner, sid = base
        return owner, f"__view__::{sid}"

    def will_compact_view(
        self,
        messages: list[Message],
        session_id: str | None = None,
        owner_account_id: str | None = None,
        prompt_overhead_tokens: int = 0,
    ) -> bool:
        """返回本轮是否会调用摘要模型，用于发送准确的前端活动提示。"""
        if not self.enabled:
            return False
        view = self.compact_preview_view(messages)
        if self._measured_tokens(session_id, owner_account_id, view) + max(0, int(prompt_overhead_tokens)) <= self.token_budget:
            return False
        vkey = self._view_key(session_id, owner_account_id)
        failure_key = vkey or ("", "")
        if self._failure_counts.get(failure_key, 0) >= _MAX_CONSECUTIVE_FAILURES:
            return False
        state = self._view_mem.get(vkey) if vkey is not None else None
        if state is not None and state.ineffective_count >= _MAX_INEFFECTIVE:
            return False
        split = self._safe_split(view, self.keep_recent)
        return split >= _MIN_OLD_FOR_SUMMARY

    @_observed_compaction("context.compact.view")
    async def compact_view(
        self,
        messages: list[Message],
        session_id: str | None = None,
        owner_account_id: str | None = None,
        prompt_overhead_tokens: int = 0,
        system_prompt: str = "",
        tools: list[dict] | None = None,
    ) -> list[Message]:
        """executor 循环内每轮调用的视图压缩。

        与 ``maybe_compact`` 的区别：stateless 相对 canonical L2——不读写
        ``SummaryStore`` / ``_mem``（canonical 的 covered_count 索引假设「列表是
        上次调用的 prefix-superset」，而 in-place 压缩后 view 的 index 体系整体
        前移，复用 canonical state 会切错片/越界）。防抖走独立 ``_view_mem``，
        断路器复用 ``_failure_counts`` 但用 ``_view_key`` 隔离。

        水位节流：未超 ``token_budget`` 时只跑便宜的 L1 micro_compact 直接返回，
        故每轮调用近乎零成本，仅在超水位时触发一次 L3 全量摘要。
        """
        if not self.enabled:
            return messages
        messages = self.compact_preview_view(messages)
        if self._measured_tokens(session_id, owner_account_id, messages) + max(0, int(prompt_overhead_tokens)) <= self.token_budget:
            return messages  # L1 已够，无需 LLM 摘要

        vkey = self._view_key(session_id, owner_account_id)
        failure_key = vkey or ("", "")
        if self._failure_counts.get(failure_key, 0) >= _MAX_CONSECUTIVE_FAILURES:
            log.warning(
                "compact_view session=%s 连续 %d 次摘要失败，断路器跳过",
                session_id,
                _MAX_CONSECUTIVE_FAILURES,
            )
            return messages

        vstate = self._view_mem.get(vkey) if vkey is not None else None
        if vstate is not None and vstate.ineffective_count >= _MAX_INEFFECTIVE:
            log.warning(
                "compact_view 连续 %d 次压缩省 <10%%，跳过 session=%s",
                vstate.ineffective_count,
                session_id,
            )
            return messages

        before = estimate_tokens(messages)
        # state=None → _summarize_old 直走 L3 全量，不触碰 canonical L2 缓存
        result, new_state, skipped = await self._summarize_old(
            messages, None, self.keep_recent,
            session_id=session_id, system_prompt=system_prompt, tools=tools,
        )
        if skipped:
            return result  # old 段太小主动跳过，不计失败
        if new_state is None:
            self._failure_counts[failure_key] = self._failure_counts.get(failure_key, 0) + 1
            return result

        self._failure_counts[failure_key] = 0
        after = estimate_tokens(result)
        savings = (before - after) / before if before else 0.0
        prev = vstate.ineffective_count if vstate is not None else 0
        new_state.ineffective_count = 0 if savings >= _INEFFECTIVE_SAVINGS_PCT else prev + 1
        if vkey is not None:
            self._view_mem[vkey] = new_state
        return result

    def compact_preview_view(self, messages: list[Message]) -> list[Message]:
        """返回无需模型调用即可确定的 L1 请求视图。

        会话打开时不能为了显示用量发起摘要请求，但也不能直接统计未经 L1
        清理的 canonical history。预览与真实 compact 路径共用这个入口，确保
        旧工具结果清理、最近工具保留数和单条截断规则完全一致。
        """
        if not self.enabled:
            return messages
        return micro_compact(
            messages,
            self.keep_recent_tools,
            max_tool_result_chars=self.max_tool_result_chars,
            result_policy_resolver=self.result_policy_resolver,
        )

    def _emit_compaction_event(
        self,
        session_id: str | None,
        owner_account_id: str | None,
        state: SummaryState,
        view_estimate: int,
    ) -> None:
        """压缩成功落库后内联自包含 compaction 事件（事件表只增不改）。"""
        if self._event_sink is None or not session_id:
            return
        try:
            self._event_sink(
                session_id,
                owner_account_id or "",
                state.text,
                state.covered_count,
                int(view_estimate),
            )
        except Exception:  # noqa: BLE001
            log.warning("compaction 事件落库失败 session=%s", session_id)

    @_observed_compaction("context.compact.maybe")
    async def maybe_compact(
        self,
        messages: list[Message],
        session_id: str | None = None,
        owner_account_id: str | None = None,
        system_prompt: str = "",
        tools: list[dict] | None = None,
    ) -> list[Message]:
        """预检式压缩：L1 每轮跑；仍超预算才走 L2/L3（带防抖 + 断路器）。"""
        if not self.enabled:
            return messages
        messages = self.compact_preview_view(messages)
        if self._measured_tokens(session_id, owner_account_id, messages) <= self.token_budget:
            return messages  # L1 已够

        key = self._key(session_id, owner_account_id)
        state = self._get_state(session_id, owner_account_id)
        # 断路器：连续摘要失败多次则暂停，避免反复调用 LLM 死循环
        failure_key = key or ("", "")
        if self._failure_counts.get(failure_key, 0) >= _MAX_CONSECUTIVE_FAILURES:
            log.warning(
                "session=%s 连续 %d 次摘要失败，断路器跳过摘要",
                session_id,
                _MAX_CONSECUTIVE_FAILURES,
            )
            return messages

        # 防抖：连续多次无效压缩则跳过摘要，避免烧钱空转
        if state is not None and state.ineffective_count >= _MAX_INEFFECTIVE:
            log.warning(
                "连续 %d 次压缩省 <10%%，跳过摘要 session=%s",
                state.ineffective_count,
                session_id,
            )
            return messages

        before = estimate_tokens(messages)
        result, new_state, skipped = await self._summarize_old(
            messages, state, self.keep_recent,
            session_id=session_id, system_prompt=system_prompt, tools=tools,
        )
        if skipped:
            return result  # old 段太小主动跳过，不计失败、不触发断路器
        if new_state is None:
            # 摘要失败：递增断路器计数
            self._failure_counts[failure_key] = self._failure_counts.get(failure_key, 0) + 1
            return result

        # 摘要成功：清零断路器计数
        self._failure_counts[failure_key] = 0
        after = estimate_tokens(result)
        savings = (before - after) / before if before else 0.0
        prev = state.ineffective_count if state is not None else 0
        new_state.ineffective_count = 0 if savings >= _INEFFECTIVE_SAVINGS_PCT else prev + 1
        self._put_state(session_id, new_state, owner_account_id)
        self._emit_compaction_event(session_id, owner_account_id, new_state, after)
        return result

    @_observed_compaction("context.compact.force")
    async def force_compact(
        self,
        messages: list[Message],
        session_id: str | None = None,
        owner_account_id: str | None = None,
        system_prompt: str = "",
        tools: list[dict] | None = None,
    ) -> list[Message]:
        """兜底式压缩：上下文溢出时调用，比预检更激进（保留窗口砍半）。

        先无模型剪枝超长 tool result（阈值 8192、头 4096/尾 1024），再 L1 清理、
        L2/L3 摘要；不受防抖与断路器限制——溢出时必须尽力压缩。
        调用方必须用「历史投影确实前进」（压缩后视图 token 数变小）来门控重试，
        否则无进展压缩 + 重试会形成死循环。
        """
        if not self.enabled:
            return messages
        messages = prune_tool_results(messages)
        keep_tools = max(2, self.keep_recent_tools // 2)
        messages = micro_compact(
            messages,
            keep_tools,
            max_tool_result_chars=self.max_tool_result_chars,
            result_policy_resolver=self.result_policy_resolver,
        )
        keep = max(2, self.keep_recent // 2)
        state = self._get_state(session_id, owner_account_id)
        result, new_state, _skipped = await self._summarize_old(
            messages, state, keep,
            session_id=session_id, system_prompt=system_prompt, tools=tools,
        )
        if new_state is not None:
            # 兜底压缩成功即视为「有效压缩」，清零防抖计数与断路器计数
            new_state.ineffective_count = 0
            self._put_state(session_id, new_state, owner_account_id)
            self._failure_counts[self._key(session_id, owner_account_id) or ("", "")] = 0
            self._emit_compaction_event(
                session_id, owner_account_id, new_state, estimate_tokens(result)
            )
        return result

    @_observed_compaction("context.compact.now")
    async def compact_now(
        self,
        messages: list[Message],
        session_id: str | None = None,
        owner_account_id: str | None = None,
        system_prompt: str = "",
        tools: list[dict] | None = None,
    ) -> tuple[list[Message], bool]:
        """手动压缩入口（idle 维护窗口）：不依赖水位、防抖与断路器，立即压缩一段历史。

        调用方必须保证会话处于空闲维护窗口（无正在执行的回合），忙时不得调用；
        压缩结果写入 L2 摘要状态，下一轮按需复用。canonical 历史不被修改。
        返回 (压缩后视图, 是否发生了压缩)。
        """
        if not self.enabled:
            return messages, False
        messages = self.compact_preview_view(messages)
        state = self._get_state(session_id, owner_account_id)
        result, new_state, skipped = await self._summarize_old(
            messages, state, self.keep_recent,
            session_id=session_id, system_prompt=system_prompt, tools=tools,
        )
        if new_state is None:
            return result, False
        new_state.ineffective_count = 0
        self._put_state(session_id, new_state, owner_account_id)
        self._failure_counts[self._key(session_id, owner_account_id) or ("", "")] = 0
        self._emit_compaction_event(
            session_id, owner_account_id, new_state, estimate_tokens(result)
        )
        return result, not skipped

    def _history_hint(self, session_id: str | None) -> str:
        """摘要消息尾部附完整历史回溯指引（canonical 历史 append-only 落库）。"""
        if not self.history_db_path or not session_id:
            return ""
        return (
            "\n\n> 压缩前的完整对话原文未丢失：已持久化在本地会话数据库 "
            f"`{self.history_db_path}` 的 sessions 表中（session_id=`{session_id}`，"
            "messages 字段为 JSON）。如确需压缩前某段的原文细节，可用 terminal "
            "以只读方式查询该库，或直接向用户确认。"
        )

    async def _summarize_old(
        self,
        messages: list[Message],
        state: SummaryState | None,
        keep_recent: int,
        session_id: str | None = None,
        system_prompt: str = "",
        tools: list[dict] | None = None,
    ) -> tuple[list[Message], SummaryState | None, bool]:
        """把 keep_recent 之前的历史摘要成一条 system 消息（L2 复用 / L3 全量）。

        返回 (压缩后的消息, 新摘要状态, skipped)。
        - skipped=True：old 段太小（< _MIN_OLD_FOR_SUMMARY）主动跳过，不计失败、不触发断路器。
        - new_state=None 且 skipped=False：摘要失败（LLM 报错/返回空），调用方计失败。
        防抖计数由调用方设置，这里只继承旧值。
        """
        if len(messages) <= keep_recent:
            return messages, None, True
        split = self._safe_split(messages, keep_recent)
        # 事务提交前双侧再校验：切点绝不落在 toolResult 上（余额非 0 或非消息边界
        # 则向头部回退），回退不到平衡切点则整个事务放弃。
        bal = self._pairing_balances(messages)
        while split > 0 and (
            bal[split] != 0 or messages[split].role not in ("user", "assistant")
        ):
            split -= 1
        if split <= 0:
            return messages, None, True  # 无可压缩的旧消息

        old, recent = messages[:split], messages[split:]
        # old 段太小不值得调 LLM 摘要（摘要 1-2 条收益 < 开销），交给 micro_compact + 溢出兜底
        if len(old) < _MIN_OLD_FOR_SUMMARY:
            log.info("old 段仅 %d 条，跳过 L2/L3 摘要", len(old))
            return messages, None, True

        summary: str | None = None

        # ---- L2：有可复用的旧摘要 ----
        if self.l2_incremental and state is not None and state.covered_count <= split:
            new_old = old[state.covered_count:]
            if estimate_tokens(new_old) < self.l2_delta_threshold:
                # 新增很少：纯规则复用旧摘要，零 LLM
                summary = state.text
                log.info("L2 纯规则复用 session新增=%d 条", len(new_old))
            else:
                summary = await summarize_incremental(
                    self.provider, state.text, new_old,
                    system_prompt=system_prompt, tools=tools,
                    max_tokens=self.summary_max_tokens,
                    prefix_reuse=None if self.summary_prefix_reuse else False,
                )
                if summary:
                    log.info("L2 增量摘要 新增=%d 条", len(new_old))

        # ---- L3：无可复用摘要（或 L2 失败）→ 全量 ----
        if summary is None:
            summary = await summarize_full(
                self.provider, old,
                system_prompt=system_prompt, tools=tools,
                max_tokens=self.summary_max_tokens,
                prefix_reuse=None if self.summary_prefix_reuse else False,
            )
            if summary:
                log.info("L3 全量摘要 旧=%d 条", len(old))

        if not summary:
            return messages, None, False  # 压缩失败，原样返回，不影响主流程

        prev_ineffective = state.ineffective_count if state is not None else 0
        new_state = SummaryState(
            text=summary, covered_count=split, ineffective_count=prev_ineffective
        )
        log.info("上下文压缩：%d 条旧消息 -> 摘要，保留最近 %d 条", len(old), len(recent))

        # Post-compact 恢复：保留 Skill 指令、不可重放的重要结论，
        # 以及最近读取的文件——文件按 path 去重后从磁盘重读最新内容，
        # 不回放压缩时的旧快照（磁盘 I/O 丢线程池，避免阻塞事件循环）。
        attachments = (
            build_post_compact_attachments(
                old,
                result_policy_resolver=self.result_policy_resolver,
                max_resources=self.post_compact_files,
                max_chars_per_resource=self.post_compact_max_chars_per_file,
                max_instructions=self.post_compact_max_instructions,
                max_important=self.post_compact_max_important,
                max_instruction_chars=self.post_compact_max_instruction_chars,
                max_important_chars=self.post_compact_max_important_chars,
                max_total_chars=self.post_compact_max_total_chars,
            )
            if self.result_policy_resolver is not None
            else []
        )
        file_attachments = await asyncio.to_thread(
            build_post_compact_file_attachments,
            old,
            max_files=self.post_compact_files,
            max_chars_per_file=self.post_compact_max_chars_per_file,
        )
        attachments = [*attachments, *file_attachments]
        summary_message = Message.system(
            f"{SUMMARY_MARKER}\n{summary}{self._history_hint(session_id)}"
        )
        if attachments:
            log.info(
                "压缩后恢复 %d 个受保护工具结果，单资源上限 %d 字符",
                len(attachments),
                self.post_compact_max_chars_per_file,
            )
        compacted = [summary_message, *attachments, *recent]
        # 文件清单被摘要遮蔽时重新注入视图：它是持久会话信息（canonical 每轮刷新），
        # 压缩后「你读过/改过这些文件」不能丢。
        if not any(is_file_manifest_message(m) for m in recent):
            manifest = shadowed_manifest_message(old)
            if manifest is not None:
                compacted = [summary_message, manifest, *attachments, *recent]
        # 「摘要必须更小」校验：压缩后视图不小于原视图则整个事务失败，历史不变。
        before_tokens = estimate_tokens(messages)
        after_tokens = estimate_tokens(compacted)
        if after_tokens >= before_tokens:
            log.warning(
                "压缩事务放弃：摘要后视图未变小（%d → %d tokens）session=%s",
                before_tokens,
                after_tokens,
                session_id,
            )
            return messages, None, False
        return compacted, new_state, False
