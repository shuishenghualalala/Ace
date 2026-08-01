"""工具批次执行器：把「执行一批 tool_calls」从主循环抽出，统一串起
guardrails（防循环）、plugins（pre/post/transform）、TurnControl（中断）与并行调度。

并行判定实现了 ``agent/tool_dispatch_helpers.py``，并适配 Crew 工具名。
并行时仍按原始顺序回灌结果，保持 OpenAI 的
assistant(tool_calls) ↔ tool 配对顺序。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import mimetypes
import time
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from crew.agent.loop.tool_dispatch_helpers import (
    is_tool_parallel_safe,
    segment_consecutive_safe,
    should_parallelize as should_parallelize,
    should_parallelize_tool_batch,
)
from crew.agent.loop.tool_guardrails import ToolCallGuardrailController, append_toolguard_guidance, toolguard_synthetic_result
from crew.core.envelope import ResponseChunk
from crew.core.followup import (
    drain_followup_answer_messages,
    send_followup_question,
    wait_for_answer,
)
from crew.core.interfaces import ToolRegistry
from crew.core.types import MediaPart, Message, ToolCall, ToolPermissionDecision, ToolResult, tool_arguments_for_ui
from crew.plugins.manager import PluginManager
from crew.state.logging import get_logger, llm_trace
from crew.tools.file_utils import _has_binary_extension
from crew.tools.pipeline import (
    check_permission,
    grant_session_allow,
    should_block_for_tool_call,
)
from crew.team.workspace_guard import check_workspace_guard
from crew.tools.tool_search import ToolSearchConfig, dispatch_bridge_tool, is_bridge_tool

log = get_logger("agent.tool_runner")
_MAX_TOOL_WORKERS = 8  # Crew run_agent.py / agent.tool_executor default
_TERMINAL_SNAPSHOT_MAX_FILES = 20_000
_TERMINAL_SNAPSHOT_SKIP_DIRS = frozenset({
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
})

TerminalFileSnapshot = dict[str, tuple[int, int]]


class ToolRunner:
    """执行一批工具调用并产出 ResponseChunk 帧；原地把结果回灌进 messages。"""

    def __init__(
        self,
        registry: ToolRegistry,
        plugins: PluginManager,
        guardrails: ToolCallGuardrailController,
        *,
        parallel_enabled: bool = True,
        max_parallel_tool_calls: int = _MAX_TOOL_WORKERS,
        session_id: str = "",
        control: Any = None,
        plan_manager: Any = None,
        tool_search_schemas: list[dict[str, Any]] | None = None,
        tool_search_config: ToolSearchConfig | None = None,
        authorized_tool_names: frozenset[str] | None = None,
        allowed_tool_names: set[str] | frozenset[str] | None = None,
        direct_tool_names: set[str] | frozenset[str] | None = None,
        discovered_tool_names: set[str] | frozenset[str] | None = None,
    ) -> None:
        self.registry = registry
        self.plugins = plugins
        self.guardrails = guardrails
        self.parallel_enabled = parallel_enabled
        self.max_parallel_tool_calls = max(1, int(max_parallel_tool_calls or _MAX_TOOL_WORKERS))
        self.session_id = session_id
        self.control = control
        self.plan_manager = plan_manager
        self.tool_search_schemas = list(tool_search_schemas or [])
        self.tool_search_config = tool_search_config
        self.authorized_tool_names = authorized_tool_names
        self.allowed_tool_names = (
            frozenset(allowed_tool_names) if allowed_tool_names is not None else None
        )
        # Names actually sent to the provider this turn. Deferred catalog
        # entries become direct-callable only after tool_search discovers them;
        # a provider must not bypass progressive disclosure by guessing a name.
        self.direct_tool_names = set(direct_tool_names) if direct_tool_names is not None else None
        self.discovered_tool_names = set(discovered_tool_names or ())
        if self.direct_tool_names is not None:
            self.direct_tool_names.update(self.discovered_tool_names)
        # 流式提前派发缓存（Crew earlyExecutions）：tc.id -> 正在执行/已完成的 asyncio.Task。
        # 流式期间 prewarm() 把 safe 工具提前跑起来，run_batch 时命中即 await，通常已就绪。
        self._prewarm: dict[str, asyncio.Task] = {}
        self._prewarm_keys: set[tuple[str, str]] = set()  # (name, repr(args)) 去重
        self._sem: asyncio.Semaphore | None = None  # 全轮共享并发闸门，懒创建
        self._pending_media: list[tuple[str, str, MediaPart]] = []

    @staticmethod
    def _extract_mcp_images(content: str) -> tuple[list[dict[str, str]], str] | None:
        """识别 MCP 工具返回的混合结果（文本+图片）。

        仅当 content 为 ``{"text": ..., "images": [...]}`` 格式且含有效图片时返回
        ``(图片内容块列表, 文本部分)``；否则返回 None。纯文本模型使用 ax 模式时
        不会触发此路径。
        """
        if not content or not content.startswith("{"):
            return None
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        images = payload.get("images")
        if not isinstance(images, list) or not images:
            return None
        valid: list[dict[str, str]] = []
        for img in images:
            if isinstance(img, dict) and img.get("url"):
                valid.append({"type": "image_url", "image_url": {"url": str(img["url"])}})
        if not valid:
            return None
        text_payload = str(payload.get("text") or "")
        return valid, text_payload

    def _is_mcp_tool(self, name: str) -> bool:
        """通过注册表 toolset 判断工具是否来自 MCP server。"""
        fn = getattr(self.registry, "toolset_for", None)
        if callable(fn):
            return str(fn(name) or "").startswith("mcp:")
        # 兼容没有 toolset_for 的 Registry 实现：按命名约定兜底。
        return "__" in name and not name.startswith("builtin__")

    def _attach_mcp_images(self, tc: ToolCall, result: ToolResult, messages: list[Message]) -> None:
        """若 MCP 工具结果包含图片，追加一条 meta user message 注入多模态 content_parts。

        为避免同一张图片 base64 在 tool message 与多模态 user message 中重复传递，
        会把刚追加的 tool message 内容改写为仅保留文本骨架（images 置空）。
        """
        if result.is_error:
            return
        if not self._is_mcp_tool(tc.name):
            return
        extracted = self._extract_mcp_images(result.content)
        if extracted is None:
            return
        image_parts, text_payload = extracted

        # 改写刚追加的 tool message，移除图片 base64，仅保留文本骨架。
        if messages and messages[-1].role == "tool" and messages[-1].tool_call_id == tc.id:
            messages[-1].content = json.dumps(
                {"text": text_payload, "images": []},
                ensure_ascii=False,
            )

        caption = f"[MCP 工具 {tc.name} 返回的截图]"
        if text_payload:
            caption += (f"\n{text_payload}")[:2000]
        parts: list[dict[str, Any]] = [{"type": "text", "text": caption}]
        parts.extend(image_parts)
        messages.append(Message(role="user", content="", content_parts=parts, is_meta=True))

    async def run_batch(
        self,
        tool_calls: list,
        messages: list[Message],
        rid: str,
        next_seq: Callable[[], int],
        *,
        started_tool_call_ids: set[str] | None = None,
    ) -> AsyncIterator[ResponseChunk]:
        """执行整批工具调用。next_seq() 由主循环提供，统一分配 sequence。

        分段策略：
          - 全批可并行（全 safe / 全 distinct-delegate）→ 单段并发，保持既有行为；
          - 混合批 → 按「连续 safe 并发段 + unsafe 独占段」切分，保证顺序安全；
          - parallel_enabled=False → 全部退化为逐个串行。
        safe 工具若已在流式期间 prewarm，命中缓存即时返回，执行已与流重叠。
        started_tool_call_ids 用于 UI 已提前收到 start 帧的工具，避免 run_batch 重复发 start。
        """
        segments = self._plan_segments(tool_calls)
        started_ids = set(started_tool_call_ids or set())
        llm_trace("tool_batch_plan", {
            "session_id": self.session_id,
            "segments": [{"safe": s, "tools": [getattr(tc, "name", "") for tc in c]} for s, c in segments],
            "tool_count": len(tool_calls),
            "max_parallel_tool_calls": self.max_parallel_tool_calls,
            "prewarmed": list(self._prewarm.keys()),
        })
        self._pending_media = []
        try:
            for safe, calls in segments:
                if safe and len(calls) > 1:
                    async for chunk in self._run_parallel_segment(calls, messages, rid, next_seq, started_ids):
                        yield chunk
                else:
                    async for chunk in self._run_sequential_segment(calls, messages, rid, next_seq, started_ids):
                        yield chunk
            self._append_pending_media(messages)
        finally:
            # 清理本轮未被消费的 prewarm（被 plan_tool_calls 去重/裁剪掉的工具）。
            await self.cancel_prewarms()

    def _plan_segments(self, tool_calls: list) -> list[tuple[bool, list]]:
        """把工具序列规划成执行段。"""
        if not self.parallel_enabled:
            return [(False, [tc]) for tc in tool_calls]
        if should_parallelize_tool_batch(tool_calls).parallel:
            return [(True, list(tool_calls))]
        return segment_consecutive_safe(tool_calls)

    # ---- prewarm（流式提前派发）------------------------------------------ #
    def prewarm(self, tc) -> bool:
        """流式期间提前派发一个 safe 工具，结果缓存到 self._prewarm[tc.id]。

        仅派发并发安全工具（写/命令等不安全工具忽略，留给 run_batch 顺序执行）。
        同一 (name, args) 去重，避免模型重复调用时重复执行/重复计 guardrail。
        返回 True 表示本次确实新建了提前执行任务，可安全向 UI 发送 start 帧。
        """
        if self._interrupted or tc.id in self._prewarm:
            return False
        # 未授权调用不得进入提前执行，也不得借 start 帧把参数写进 trace/UI。
        if not self._is_authorized(tc):
            return False
        if (
            not is_bridge_tool(tc.name)
            and self.direct_tool_names is not None
            and tc.name not in self.direct_tool_names
        ):
            return False
        if not is_tool_parallel_safe(tc):
            return False
        key = (tc.name, repr(tc.arguments))
        if key in self._prewarm_keys:
            return False
        self._prewarm_keys.add(key)
        sem = self._ensure_sem()

        async def _run() -> ToolResult:
            async with sem:
                if self._interrupted:
                    return self._cancelled_result(tc)
                started = self._mark_tool_started(tc)
                try:
                    return await self._execute_one_body(tc)
                finally:
                    self._mark_tool_finished(tc, started=started)

        self._prewarm[tc.id] = asyncio.create_task(_run())
        return True

    async def cancel_prewarms(self) -> None:
        """取消并清空所有未消费的 prewarm 任务（重试/收尾时调用）。"""
        tasks = [t for t in self._prewarm.values() if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._prewarm.clear()
        self._prewarm_keys.clear()

    def _ensure_sem(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.max_parallel_tool_calls)
        return self._sem

    def _is_authorized(self, tc) -> bool:
        """Check the current turn's immutable tool authorization snapshot."""
        return self.authorized_tool_names is None or tc.name in self.authorized_tool_names

    def _record_bridge_discovery(self, bridge_name: str, result: ToolResult) -> None:
        """Record schemas made callable by tool_search."""
        if result.is_error or bridge_name != "tool_search":
            return
        try:
            payload = json.loads(result.content)
        except (TypeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict) or payload.get("error"):
            return

        names: set[str] = set()
        for item in payload.get("matches") or []:
            name = item.get("name") if isinstance(item, dict) else item
            if str(name or ""):
                names.add(str(name))

        if not names:
            return
        self.discovered_tool_names.update(names)
        if self.direct_tool_names is not None:
            self.direct_tool_names.update(names)

    # ------------------------------------------------------------------ #
    async def _run_sequential_segment(
        self,
        calls,
        messages,
        rid,
        next_seq,
        started_tool_call_ids: set[str],
    ) -> AsyncIterator[ResponseChunk]:
        for tc in calls:
            if self._interrupted:
                result = self._cancelled_result(tc)
                if tc.id not in started_tool_call_ids:
                    yield self._start_event(tc, rid, next_seq)
                self._mark_tool_finished(tc)
                yield self._result_event(tc, result, rid, next_seq, status="cancelled")
                messages.append(Message.tool(tc.id, result.content, name=tc.name))
                self._append_followup_answers(messages)
                continue
            if tc.id not in started_tool_call_ids:
                yield self._start_event(tc, rid, next_seq)
            before = self._read_file_before(tc) if tc.name == "file_write" else None
            terminal_before = self._terminal_workspace_snapshot(tc)
            result = await self._resolve(tc)
            status = "cancelled" if self._interrupted else ("error" if result.is_error else "ok")
            yield self._result_event(tc, result, rid, next_seq, status=status)
            messages.append(Message.tool(tc.id, result.content, name=tc.name))
            self._attach_mcp_images(tc, result, messages)
            self._queue_media(tc, result)
            self._append_followup_answers(messages)
            if tc.name == "todo":
                yield self._todo_snapshot_event(rid, next_seq)
            if tc.name == "file_write":
                yield self._file_change_event(tc, before, rid, next_seq)
            elif tc.name == "terminal":
                terminal_event = self._terminal_file_change_event(
                    tc, terminal_before, result, rid, next_seq,
                )
                if terminal_event is not None:
                    yield terminal_event

    async def _run_parallel_segment(
        self,
        calls,
        messages,
        rid,
        next_seq,
        started_tool_call_ids: set[str],
    ) -> AsyncIterator[ResponseChunk]:
        # 先发本段全部 start 帧（保持顺序），再并发执行，最后按序回灌结果。
        for tc in calls:
            if tc.id not in started_tool_call_ids:
                yield self._start_event(tc, rid, next_seq)
        # 并行段默认不接 file_write（非 parallel-safe，由 sequential 段处理），
        # 但保留 before 读取与 file_change 广播，防御未来放宽安全判定时漏播。
        before_map: dict[str, Any] = {}
        for tc in calls:
            if tc.name == "file_write":
                before_map[tc.id] = self._read_file_before(tc)
        results = await self._resolve_parallel(calls)
        for tc, result in zip(calls, results):
            status = "cancelled" if "用户中断" in result.content else ("error" if result.is_error else "ok")
            yield self._result_event(tc, result, rid, next_seq, status=status)
            messages.append(Message.tool(tc.id, result.content, name=tc.name))
            self._attach_mcp_images(tc, result, messages)
            self._queue_media(tc, result)
            self._append_followup_answers(messages)
            if tc.name == "todo":
                yield self._todo_snapshot_event(rid, next_seq)
            if tc.name == "file_write":
                yield self._file_change_event(tc, before_map.get(tc.id), rid, next_seq)

    def _append_followup_answers(self, messages: list[Message]) -> None:
        """把追问选择作为 user 消息插入 tool result 之后，确保 history 可回放。"""
        if not self.session_id:
            return
        for content in drain_followup_answer_messages(self.session_id):
            messages.append(Message.user(content))

    async def _resolve(self, tc) -> ToolResult:
        """取单工具结果：命中 prewarm 缓存则 await 缓存任务，否则现场执行（带并发闸门）。"""
        task = self._prewarm.pop(tc.id, None)
        if task is not None:
            try:
                return await task
            except asyncio.CancelledError:
                return self._cancelled_result(tc)
            except Exception as exc:  # noqa: BLE001
                return ToolResult(tc.id, tc.name, f"工具异常: {exc}", is_error=True)
        async with self._ensure_sem():
            if self._interrupted:
                return self._cancelled_result(tc)
            return await self._execute_one(tc)

    async def _resolve_parallel(self, calls) -> list[ToolResult]:
        tasks = [asyncio.create_task(self._resolve(tc)) for tc in calls]
        pending: set[asyncio.Task] = set(tasks)
        while pending:
            done, pending = await asyncio.wait(pending, timeout=0.05, return_when=asyncio.FIRST_COMPLETED)
            if self._interrupted and pending:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                break
            if not done and not pending:
                break

        results: list[ToolResult] = []
        for tc, task in zip(calls, tasks):
            if task.cancelled() or not task.done():
                results.append(self._cancelled_result(tc))
                continue
            try:
                results.append(task.result())
            except asyncio.CancelledError:
                results.append(self._cancelled_result(tc))
            except Exception as exc:  # noqa: BLE001
                results.append(ToolResult(tc.id, tc.name, f"工具异常: {exc}", is_error=True))
        return results

    # ------------------------------------------------------------------ #
    async def _check_permission(self, tc) -> str | None:
        """权限规则匹配 + 交互确认。

        - 无规则 / allow → 放行（返回 None）
        - deny → 返回拒绝原因（作为 is_error 结果回灌）
        - ask → 弹出「允许一次 / 始终允许 / 拒绝」三选一，record_history=False
          不把答案写进 canonical history（权限确认是 side-channel）
        - 当前环境无法交互（无 push_fn，如子 agent / 测试）触发 ask 时按 deny 处理（fail-closed）
        """
        resolver = getattr(self.registry, "resolve_permission", None)
        dynamic: ToolPermissionDecision | None = None
        if callable(resolver):
            try:
                dynamic = await resolver(tc)
            except Exception:  # noqa: BLE001 - permission resolver must fail closed
                log.exception("工具动态权限判定失败: %s", tc.name)
                return json.dumps({"error": "工具权限判定失败，已按拒绝处理"}, ensure_ascii=False)
        if dynamic is not None:
            if dynamic.behavior == "allow":
                return None
            if dynamic.behavior == "deny":
                return json.dumps({"error": dynamic.reason or "该浏览器动作已被安全策略拒绝"}, ensure_ascii=False)
            blocked = await self._ask_permission(
                tc,
                dynamic.reason or "该动作可能产生外部副作用",
                "",
                allow_always=dynamic.allow_always,
            )
            if blocked is not None:
                return blocked
            confirmer = getattr(self.registry, "confirm_permission", None)
            if dynamic.approval_token and (
                not callable(confirmer) or not await confirmer(tc, dynamic)
            ):
                return json.dumps(
                    {"error": "页面或目标在审批后已变化，一次性审批已失效，请重新观察"},
                    ensure_ascii=False,
                )
            return None
        if not should_block_for_tool_call(tc):
            return None  # 只读类工具默认放行，不打扰用户
        behavior, reason, suggested = check_permission(
            tc.name, tc.arguments, session_id=self.session_id
        )
        if behavior == "allow":
            return None
        if behavior == "deny":
            return json.dumps(
                {"error": f"权限拒绝：{reason or '匹配 deny 规则'}"}, ensure_ascii=False
            )
        # ask
        return await self._ask_permission(tc, reason, suggested)

    async def _ask_permission(
        self,
        tc,
        reason: str,
        suggested: str,
        *,
        allow_always: bool = True,
    ) -> str | None:
        """弹出权限确认框并等待用户选择。返回 None 表示放行，否则返回拒绝原因。"""
        from crew.tools.pipeline import extract_match_key

        key = extract_match_key(tc.name, tc.arguments)
        # Follow-up cards render plain text. Keep this free of Markdown fences
        # so permission details are readable in every client.
        question_text = f"即将执行：{key}"
        if reason:
            question_text += f"\n\n原因：{reason}"
        if suggested:
            question_text += f"\n\n始终允许规则：{tc.name}({suggested})"
        options = [{"label": "允许一次", "value": "allow_once"}]
        if allow_always:
            options.append({"label": "始终允许", "value": "always"})
        options.append({"label": "拒绝", "value": "deny"})
        questions = [{
            "id": "perm",
            "question": question_text,
            "options": options,
            "multiSelect": False,
            "allowFreeText": False,
        }]
        try:
            session_id, qid = await send_followup_question(
                questions, title=f"权限确认 · {tc.name}", record_history=False,
            )
        except Exception as exc:  # noqa: BLE001 - 无 push_fn 等无法交互环境 → fail-closed
            log.info("权限 ask 无法交互（%s），按拒绝处理: %s", type(exc).__name__, tc.name)
            return json.dumps(
                {"error": f"需要权限确认但当前环境无法交互：{exc}"}, ensure_ascii=False
            )
        answers = await wait_for_answer(session_id, qid)
        choice = ""
        if answers and isinstance(answers[0], dict):
            vals = answers[0].get("answers")
            if isinstance(vals, list) and vals:
                choice = str(vals[0])
        if choice == "allow_once":
            return None
        if choice == "always":
            grant_session_allow(self.session_id, tc.name, suggested or "*")
            return None
        # deny / 超时 / 取消
        if choice == "deny":
            return json.dumps({"error": "用户拒绝了该工具调用"}, ensure_ascii=False)
        return json.dumps(
            {"error": "权限确认未得到明确许可（超时或未选择），按拒绝处理"}, ensure_ascii=False
        )

    def _queue_media(self, tc, result: ToolResult) -> None:
        for part in result.media:
            self._pending_media.append((tc.id, tc.name, part))

    def _append_pending_media(self, messages: list[Message]) -> None:
        """Append hidden multimodal messages only after all tool results.

        Provider protocols require every assistant tool call to receive its
        complete tool result before a new user message. Browser screenshots are
        therefore queued during execution and appended at the batch boundary.
        """
        for tool_call_id, tool_name, part in self._pending_media:
            data_url = part.data_url
            if not data_url and part.path:
                try:
                    path = Path(part.path)
                    raw = path.read_bytes()
                    mime = part.mime_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                    data_url = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
                except OSError as exc:
                    log.warning("读取工具媒体失败 tool=%s: %s", tool_name, type(exc).__name__)
                    continue
            if not data_url:
                continue
            text = part.alt or f"{tool_name} 生成的视觉输入"
            messages.append(
                Message(
                    role="user",
                    content=text,
                    is_meta=True,
                    content_parts=[
                        {"type": "text", "text": text},
                        {"type": "image_url", "image_url": {"url": data_url, "detail": part.detail}},
                    ],
                    attachment_type="tool_media",
                    attachment_data={"tool_call_id": tool_call_id, "tool_name": tool_name},
                )
            )
        self._pending_media = []

    def _install_progress_sink(self, tc):
        """注入进度回调，工具执行中可经 emit_tool_progress 发增量。

        无 push_fn（CLI/测试）时注入一个 no-op sink，避免 handler 里到处判空。
        返回 contextvar token，调用方在 finally 里 reset。
        """
        from crew.core.runctx import current_push_fn, current_request_id, current_tool_progress_fn

        push = current_push_fn.get()
        rid = current_request_id.get() or ""
        sid = self.session_id

        if push is None:
            async def _noop(_text: str) -> None:
                return
            return current_tool_progress_fn.set(_noop)

        async def _sink(text: str) -> None:
            try:
                await push(sid, {
                    "kind": "tool_event",
                    "body": {
                        "tool_call_id": tc.id,
                        "name": tc.name,
                        "phase": "progress",
                        "text": text,
                    },
                    "is_final": False,
                    "sequence": 0,
                    "request_id": rid,
                    "session_id": sid,
                })
            except Exception:  # noqa: BLE001
                pass

        return current_tool_progress_fn.set(_sink)

    async def _execute_one(self, tc, *, started_at: float | None = None) -> ToolResult:
        """单个工具：guardrails.before → plugins.pre → execute → transform → guardrails.after → plugins.post。"""
        started = started_at if started_at is not None else self._mark_tool_started(tc)
        try:
            return await self._execute_one_body(tc)
        finally:
            self._mark_tool_finished(tc, started=started)

    async def _execute_one_body(self, tc) -> ToolResult:
        """单个工具：guardrails.before → plugins.pre → execute → transform → guardrails.after → plugins.post。"""
        from crew.core.runctx import current_tool_call_id

        tool_token = current_tool_call_id.set(tc.id)
        try:
            # tool_search 只负责发现工具；命中的真实 schema 会在下一轮模型请求中加载。
            # 0. plugin request middleware：工具参数脱敏/改写必须早于 guardrail、hook 与真实执行。
            resolved_from_bridge = False
            if is_bridge_tool(tc.name):
                bridge_name = tc.name
                bridge_result = dispatch_bridge_tool(
                    tc,
                    original_tool_schemas=self.tool_search_schemas,
                    config=self.tool_search_config,
                )
                if isinstance(bridge_result, ToolResult):
                    self._record_bridge_discovery(bridge_name, bridge_result)
                    return bridge_result
                tc = bridge_result
                resolved_from_bridge = True
                self.discovered_tool_names.add(tc.name)
                if self.direct_tool_names is not None:
                    self.direct_tool_names.add(tc.name)

            if (
                not resolved_from_bridge
                and self.direct_tool_names is not None
                and tc.name not in self.direct_tool_names
            ):
                log.warning("拒绝直接执行未披露的延迟工具: %s", tc.name)
                return ToolResult(
                    tc.id,
                    tc.name,
                    "该工具按需加载，必须先通过 tool_search 加载后再直接调用，已拒绝执行。",
                    is_error=True,
                )

            # Schema filtering is the model-facing boundary; this is the execution boundary.
            # Some providers can still hallucinate a hidden tool name, so never dispatch a
            # call that was not part of this Agent's effective per-turn tool scope.
            if self.allowed_tool_names is not None and tc.name not in self.allowed_tool_names:
                log.warning("拒绝执行未暴露给当前 Agent 的工具: %s", tc.name)
                return ToolResult(
                    tc.id,
                    tc.name,
                    "工具不在当前 Agent 的允许范围内，已拒绝执行。",
                    is_error=True,
                )

            if not self._is_authorized(tc):
                log.warning(
                    "拒绝未授权工具调用 session=%s tool=%s",
                    self.session_id,
                    tc.name,
                )
                return ToolResult(
                    tc.id,
                    tc.name,
                    json.dumps(
                        {
                            "error": {
                                "code": "TOOL_NOT_AUTHORIZED",
                                "message": f"Tool {tc.name!r} is not authorized in the current turn.",
                            }
                        },
                        ensure_ascii=False,
                    ),
                    is_error=True,
                )

            # 0. plugin request middleware：参数脱敏/改写必须早于 guardrail、hook 与真实执行。
            mw = await self.plugins.apply_tool_request_middleware(
                tc.name,
                tc.arguments,
                tool_call=tc,
                tool_call_id=tc.id,
                session_id=self.session_id,
            )
            if mw.changed and isinstance(mw.payload, dict):
                tc.arguments = mw.payload

            from crew.core.runctx import current_agent_workdir, current_workspace_guard

            workspace_decision = check_workspace_guard(
                tc.name,
                tc.arguments,
                current_workspace_guard.get(),
                cwd=current_agent_workdir.get(),
            )
            if not workspace_decision.allowed:
                log.info("workspace guard blocked tool %s: %s", tc.name, workspace_decision.reason)
                return ToolResult(tc.id, tc.name, workspace_decision.reason, is_error=True)

            # 1. guardrail 拦截（同参失败 N 次 / 只读无进展）
            decision = self.guardrails.before_call(tc.name, tc.arguments)
            if decision.should_halt:
                log.info("guardrail 拦截工具 %s：%s", tc.name, decision.code)
                return ToolResult(tc.id, tc.name, toolguard_synthetic_result(decision), is_error=True)

            # 1.5 plan 模式只读门控：仅放行对计划文件的写（落实 Crew「除计划文件外只读」）
            plan_block = self._plan_mode_block(tc)
            if plan_block is not None:
                log.info("plan 模式拦截写操作 %s", tc.name)
                return ToolResult(tc.id, tc.name, plan_block, is_error=True)

            # 2. plugin 前置拦截
            block_message = await self.plugins.pre_tool_call(tc, session_id=self.session_id)
            if block_message:
                return ToolResult(tc.id, tc.name, block_message, is_error=True)

            # 权限检查：allow/deny/ask 规则匹配，ask 走 followup 交互确认
            perm_block = await self._check_permission(tc)
            if perm_block is not None:
                return ToolResult(tc.id, tc.name, perm_block, is_error=True)

            # 3. 真正执行
            t = time.perf_counter()
            # 注入进度 sink，长任务（如 terminal 前台命令）
            # 可在执行中向前端流式发射增量。无 push_fn 时 sink 内部 no-op。
            progress_token = self._install_progress_sink(tc)
            try:
                async def _execute_with_args(args):
                    exec_tc = tc
                    if args is not tc.arguments:
                        exec_tc = ToolCall(
                            id=tc.id,
                            name=tc.name,
                            arguments=args,
                            started_at=tc.started_at,
                            duration=tc.duration,
                            result=tc.result,
                            status=tc.status,
                        )
                    return await self.registry.execute(exec_tc)

                result = await self.plugins.run_tool_execution_middleware(
                    tc.name,
                    tc.arguments,
                    _execute_with_args,
                    tool_call=tc,
                    tool_call_id=tc.id,
                    session_id=self.session_id,
                    original_args=mw.original_payload,
                )
            finally:
                from crew.core.runctx import current_tool_progress_fn

                current_tool_progress_fn.reset(progress_token)
            log.info("[PERF] tool %-20s  %.3fs", tc.name, time.perf_counter() - t)

            # 4. plugin 结果变换
            result = await self.plugins.transform_tool_result(tc, result)

            # 5. guardrail 事后记账（warn/halt → 把指引贴到结果上）
            failed = True if result.is_error else None
            post = self.guardrails.after_call(tc.name, tc.arguments, result.content, failed=failed)
            if post.action in ("warn", "halt"):
                result = ToolResult(
                    tc.id,
                    tc.name,
                    append_toolguard_guidance(result.content, post),
                    is_error=result.is_error,
                    media=list(result.media),
                )

            # 6. plugin 后置观测
            await self.plugins.post_tool_call(tc, result, session_id=self.session_id)
            return result
        finally:
            current_tool_call_id.reset(tool_token)

    @property
    def _interrupted(self) -> bool:
        return bool(self.control is not None and getattr(self.control, "interrupted", False))

    @staticmethod
    def _cancelled_result(tc) -> ToolResult:
        return ToolResult(tc.id, tc.name, "工具调用因用户中断而取消。", is_error=True)

    def _plan_mode_block(self, tc) -> str | None:
        """Plan 模式下的写操作门控。返回拦截原因（JSON 字符串），放行则返回 None。

        plan 激活时工具集已收窄到只读白名单（见 SingleAgent._effective_tool_filter），
        这里再对 file_write 做硬约束：只允许写当前会话的计划文件，其余一律拒绝。
        """
        from crew.core.runctx import current_owner_account_id

        owner = current_owner_account_id.get()
        if (
            self.plan_manager is None
            or not self.plan_manager.is_active(self.session_id, owner_account_id=owner)
        ):
            return None
        if tc.name != "file_write":
            return None
        from pathlib import Path

        from crew.core.runctx import current_agent_workdir
        from crew.agent.plan import plan_display_path, plan_path

        target = Path(str((tc.arguments or {}).get("path", ""))).expanduser()
        if not target.is_absolute():
            cwd = current_agent_workdir.get()
            if cwd:
                target = Path(cwd).expanduser() / target
        allowed = plan_path(self.session_id, owner_account_id=owner)
        try:
            same = target.resolve() == allowed.resolve()
        except OSError:
            same = str(target) == str(allowed)
        if same:
            return None
        return json.dumps(
            {
                "error": (
                    f"plan 模式为只读：只能写计划文件 {plan_display_path(self.session_id, owner_account_id=owner)}，"
                    "不能写其他文件。"
                    "请把计划内容写入计划文件，完成后调用 exit_plan_mode 请求审批。"
                )
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _mark_tool_started(tc) -> float:
        """记录单次工具实际开始执行的时刻（秒，与 runtime 落库一致）。"""
        started = time.time()
        tc.started_at = started
        return started

    @staticmethod
    def _mark_tool_finished(tc, *, started: float | None = None) -> None:
        """把 started→now 的耗时写回 ToolCall.duration（秒）。"""
        base = started if started is not None else tc.started_at
        if base is None:
            return
        tc.duration = max(0.0, time.time() - base)

    # ------------------------------------------------------------------ #
    def _start_event(self, tc, rid, next_seq) -> ResponseChunk:
        authorized = self._is_authorized(tc)
        ui_args = (
            tool_arguments_for_ui(tc.name, getattr(tc, "arguments", {}) or {})
            if authorized
            else {}
        )
        args_str = json.dumps(ui_args, ensure_ascii=False) if ui_args else ""
        ui_label = self._tool_ui_label(tc) if authorized else ""
        tc.ui_label = ui_label
        trace_payload = {
            "session_id": self.session_id,
            "tool_call_id": tc.id,
            "name": tc.name,
            "ui_label": ui_label,
        }
        if authorized:
            trace_payload["arguments"] = tc.arguments
        llm_trace("tool_start", trace_payload)
        return ResponseChunk.tool_event(
            rid, tc.name, "start", str(ui_args), next_seq(),
            tool_call_id=tc.id, args=args_str, ui_label=ui_label,
        )

    def _generating_event(self, tc, rid, next_seq) -> ResponseChunk:
        """Presentation-only event while the model is still generating args."""
        ui_args = tool_arguments_for_ui(tc.name, getattr(tc, "arguments", {}) or {})
        args_str = json.dumps(ui_args, ensure_ascii=False) if ui_args else ""
        ui_label = self._tool_generating_label(tc)
        tc.ui_label = ui_label
        llm_trace("tool_generating", {
            "session_id": self.session_id,
            "tool_call_id": tc.id,
            "name": tc.name,
            "arguments": ui_args,
            "ui_label": ui_label,
        })
        return ResponseChunk.tool_event(
            rid, tc.name, "generating", "模型正在生成工具参数", next_seq(),
            tool_call_id=tc.id, args=args_str, ui_label=ui_label,
        )

    def _result_event(self, tc, result: ToolResult, rid, next_seq, *, status: str = "ok") -> ResponseChunk:
        from crew.agent.loop.tool_result_display import tool_result_detail_for_ui

        llm_trace("tool_result", {
            "session_id": self.session_id,
            "tool_call_id": tc.id,
            "name": tc.name,
            "status": status,
            "is_error": result.is_error,
            "content": "<browser_content_redacted>" if str(tc.name).startswith("browser_") else result.content,
        })
        detail = tool_result_detail_for_ui(tc.name, result.content)
        return ResponseChunk.tool_event(
            rid, tc.name, "result", detail, next_seq(),
            tool_call_id=tc.id,
            ui_label=self._tool_ui_label(tc) if self._is_authorized(tc) else "",
        )

    def _tool_ui_label(self, tc) -> str:
        render = getattr(self.registry, "render_ui_label", None)
        if not callable(render):
            return ""
        try:
            # Labels are persisted and emitted separately from the already
            # redacted ``args`` field.  Render them from the same safe view so
            # a credential-bearing browser URL cannot leak through the title.
            safe_args = tool_arguments_for_ui(tc.name, getattr(tc, "arguments", {}) or {})
            return str(render(tc.name, safe_args) or "")
        except Exception:
            log.debug("工具 UI 标题渲染失败: %s", getattr(tc, "name", ""), exc_info=True)
            return ""

    def _tool_generating_label(self, tc) -> str:
        args = getattr(tc, "arguments", {}) or {}
        path = args.get("path") or args.get("file_path")
        if tc.name == "file_write":
            return f"正在写入 {path}" if path else "正在准备写入文件"
        ui_label = self._tool_ui_label(tc)
        if ui_label:
            return f"正在准备 {ui_label}"
        meta_fn = getattr(self.registry, "ui_meta", None)
        if callable(meta_fn):
            try:
                meta = meta_fn(tc.name) or {}
                display_name = str(meta.get("display_name") or "").strip()
                if display_name:
                    return f"正在准备 {display_name}"
            except Exception:
                log.debug("工具生成中标题渲染失败: %s", getattr(tc, "name", ""), exc_info=True)
        return f"正在准备 {tc.name}"

    def _todo_snapshot_event(self, rid: str, next_seq: Callable[[], int]) -> ResponseChunk:
        """todo 工具执行后，把当前任务清单快照广播给前端（Inspector Plan tab 同步用）。

        直接读 plan_manager.todo_store(session_id).read() 取最新状态——todo 工具
        handler 已在 registry.execute 里更新过 store。summary 计数交给前端算，
        后端不重复 todo_tool 的统计逻辑。
        """
        items: list = []
        if self.plan_manager is not None:
            try:
                from crew.core.runctx import current_owner_account_id

                items = self.plan_manager.todo_store(
                    self.session_id,
                    owner_account_id=current_owner_account_id.get(),
                ).read()
            except Exception:  # noqa: BLE001 — 广播失败不得影响主循环
                items = []
        return ResponseChunk(
            rid, kind="todo_updated", body={"todos": items}, sequence=next_seq(),
        )

    def _read_file_before(self, tc) -> str | None:
        """file_write 执行前读原文件内容（算 diff 的 before）。不存在 / 读失败返回 None。"""
        from pathlib import Path
        from crew.core.runctx import current_agent_workdir

        raw = str((tc.arguments or {}).get("path", ""))
        if not raw:
            return None
        target = Path(raw).expanduser()
        if not target.is_absolute():
            cwd = current_agent_workdir.get()
            if cwd:
                target = Path(cwd).expanduser() / target
        try:
            return target.read_text(encoding="utf-8", errors="replace") if target.is_file() else None
        except Exception:  # noqa: BLE001
            return None

    def _file_change_event(self, tc, before, rid: str, next_seq: Callable[[], int]) -> ResponseChunk:
        """file_write 后：读 after、算 unified diff、存入 file_change_store、广播 file_changes 帧。"""
        import difflib
        from pathlib import Path
        from crew.core.runctx import current_agent_workdir

        raw = str((tc.arguments or {}).get("path", ""))
        target = Path(raw).expanduser()
        if not target.is_absolute():
            cwd = current_agent_workdir.get()
            if cwd:
                target = Path(cwd).expanduser() / target

        after = None
        try:
            if target.is_file():
                after = target.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            after = None

        before_lines = (before or "").splitlines()
        after_lines = (after or "").splitlines()
        diff_rows: list = []
        added = 0
        removed = 0
        for line in difflib.unified_diff(before_lines, after_lines, lineterm=""):
            if line.startswith("@@") or line.startswith("---") or line.startswith("+++"):
                diff_rows.append({"line": 0, "kind": "meta", "text": line})
            elif line.startswith("+"):
                added += 1
                diff_rows.append({"line": 0, "kind": "add", "text": line[1:]})
            elif line.startswith("-"):
                removed += 1
                diff_rows.append({"line": 0, "kind": "del", "text": line[1:]})
            else:
                diff_rows.append({"line": 0, "kind": "ctx", "text": line[1:]})

        status = "added" if before is None else ("deleted" if not after else "modified")
        change = {
            "path": str(target),
            "name": target.name or str(target),
            "added": added,
            "removed": removed,
            "status": status,
            "diff": diff_rows[:200],
        }
        # 本会话内首次写入时 before 为空 → 记 created_in_session。
        # 后续同路径再写会变成 modified，但对账时若文件已不存在仍应整条剔除（临时脚本写了又删）。
        if before is None:
            change["created_in_session"] = True

        items = [change]
        if self.plan_manager is not None:
            try:
                from crew.core.runctx import current_owner_account_id

                owner = current_owner_account_id.get()
                try:
                    store = self.plan_manager.file_change_store(
                        self.session_id,
                        owner_account_id=owner,
                    )
                    prev = next((c for c in store if c.get("path") == change["path"]), None)
                    if prev and prev.get("created_in_session"):
                        change["created_in_session"] = True
                    store[:] = [c for c in store if c.get("path") != change["path"]]
                    store.append(change)
                    items = list(store)
                except Exception:  # noqa: BLE001 — 累计 store 失败仍广播本次 change
                    pass
                # 本轮摘要与累计 store 解耦：即使 store 失败也要缓冲，供历史落库。
                try:
                    self.plan_manager.record_turn_file_change(
                        self.session_id,
                        change,
                        owner_account_id=owner,
                    )
                except Exception:  # noqa: BLE001 — 摘要缓冲失败不影响广播
                    pass
            except Exception:  # noqa: BLE001 — 存储失败仍把本次 change 广播出去
                pass

        return ResponseChunk(
            rid, kind="file_changes", body={"files": items}, sequence=next_seq(),
        )

    @staticmethod
    def _workspace_snapshot(root: Path) -> TerminalFileSnapshot | None:
        """读取工作区文件元数据，用于识别 terminal 间接生成的结果文件。"""
        snapshot: TerminalFileSnapshot = {}
        if not root.is_dir():
            return snapshot
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in _TERMINAL_SNAPSHOT_SKIP_DIRS)
            for filename in sorted(filenames):
                path = Path(dirpath) / filename
                try:
                    if path.is_symlink() or not path.is_file():
                        continue
                    stat = path.stat()
                except OSError:
                    continue
                snapshot[str(path)] = (stat.st_mtime_ns, stat.st_size)
                if len(snapshot) >= _TERMINAL_SNAPSHOT_MAX_FILES:
                    log.warning("terminal 文件快照达到上限 root=%s limit=%s", root, _TERMINAL_SNAPSHOT_MAX_FILES)
                    return None
        return snapshot

    def _terminal_workspace_snapshot(self, tc) -> tuple[Path, TerminalFileSnapshot] | None:
        """仅为可能写盘的前台 terminal 建快照；只读命令与后台任务不增加扫描开销。"""
        if tc.name != "terminal" or is_tool_parallel_safe(tc) or bool((tc.arguments or {}).get("background")):
            return None
        from crew.core.runctx import current_agent_workdir

        raw = current_agent_workdir.get()
        if not raw:
            return None
        root = Path(raw).expanduser().resolve()
        snapshot = self._workspace_snapshot(root)
        return (root, snapshot) if snapshot is not None else None

    @staticmethod
    def _terminal_change(path_text: str, status: str) -> dict[str, Any]:
        path = Path(path_text)
        binary = _has_binary_extension(path)
        added = 0
        diff_rows: list[dict[str, Any]] = []
        if status == "added" and not binary:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                lines = text.splitlines()
                added = len(lines)
                diff_rows = [{"line": 0, "kind": "add", "text": line} for line in lines[:200]]
            except OSError:
                pass
        change: dict[str, Any] = {
            "path": str(path),
            "name": path.name or str(path),
            "added": added,
            "removed": 0,
            "status": status,
            "diff": diff_rows,
        }
        if binary:
            change["binary"] = True
        if status == "added":
            change["created_in_session"] = True
        return change

    def _terminal_file_change_event(
        self,
        tc,
        before_state: tuple[Path, TerminalFileSnapshot] | None,
        result: ToolResult,
        rid: str,
        next_seq: Callable[[], int],
    ) -> ResponseChunk | None:
        """把前台 terminal 在工作区内造成的文件变化合并进既有 file_changes。"""
        if before_state is None or result.is_error:
            return None
        try:
            payload = json.loads(result.content)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, dict) or not payload.get("success") or payload.get("background"):
            return None

        root, before = before_state
        after = self._workspace_snapshot(root)
        if after is None:
            return None
        changed_paths = sorted(
            path for path, meta in after.items()
            if path not in before or before[path] != meta
        )
        deleted_paths = sorted(path for path in before if path not in after)
        if not changed_paths and not deleted_paths:
            return None

        changes = [
            self._terminal_change(path, "added" if path not in before else "modified")
            for path in changed_paths
        ]
        changes.extend(self._terminal_change(path, "deleted") for path in deleted_paths)

        items = list(changes)
        if self.plan_manager is not None:
            try:
                from crew.core.runctx import current_owner_account_id

                owner = current_owner_account_id.get()
                store = self.plan_manager.file_change_store(
                    self.session_id,
                    owner_account_id=owner,
                )
                for change in changes:
                    prev = next((c for c in store if c.get("path") == change["path"]), None)
                    if prev and prev.get("created_in_session"):
                        change["created_in_session"] = True
                    store[:] = [c for c in store if c.get("path") != change["path"]]
                    store.append(change)
                    self.plan_manager.record_turn_file_change(
                        self.session_id,
                        change,
                        owner_account_id=owner,
                    )
                items = list(store)
            except Exception:  # noqa: BLE001 — 采集失败不影响 terminal 主结果
                log.warning("terminal 文件变更合并失败 session=%s", self.session_id, exc_info=True)

        return ResponseChunk(
            rid, kind="file_changes", body={"files": items}, sequence=next_seq(),
        )
