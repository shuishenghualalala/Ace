"""OpenAI 兼容 Provider。

base_url 可指向 OpenAI / OpenRouter / DeepSeek / 本地 vLLM 等任意兼容端点。
只负责"翻译"：内核 Message <-> OpenAI wire 格式，不掺杂业务逻辑。
"""

import asyncio
import json
import logging
import re
import time
from typing import Any, AsyncIterator

import httpx

from crew.core.errors import (
    ProviderError,
    contains_image_input,
    is_unsupported_image_input_error,
)
from crew.core.interfaces import LLMProvider
from crew.core.types import (
    IMAGE_INPUT_UNAVAILABLE_NOTICE,
    ChatResponse,
    Message,
    StreamChunk,
    ToolCall,
)
from crew.security.provider_proxy import provider_policy_proxy
from crew.state.logging import llm_trace
from crew.tools.redact import redact_secret_values, redact_sensitive_text


_PARTIAL_STRING_KEYS = ("path", "file_path", "command", "query", "url", "name")
_LEAKED_PARAMETER_SUFFIX = re.compile(r"(?:</parameter>?\s*)+$")
_LEAKED_PARAMETER_TOKEN = re.compile(r"</parameter>?")
_LEAKED_SCALAR_ECHO = re.compile(
    r'\b(?P<value>true|false|null)"(?P=value)</parameter>?(?P=value)\b'
)
log = logging.getLogger(__name__)


def _sanitize_tool_arguments(value: Any) -> tuple[Any, int]:
    """递归移除 OpenAI 兼容模型泄漏到参数值末尾的 XML 协议标签。"""
    if isinstance(value, str):
        cleaned, count = _LEAKED_PARAMETER_SUFFIX.subn("", value)
        return cleaned, count
    if isinstance(value, list):
        cleaned_items = []
        total = 0
        for item in value:
            cleaned, count = _sanitize_tool_arguments(item)
            cleaned_items.append(cleaned)
            total += count
        return cleaned_items, total
    if isinstance(value, dict):
        cleaned_dict = {}
        total = 0
        for key, item in value.items():
            cleaned, count = _sanitize_tool_arguments(item)
            cleaned_dict[key] = cleaned
            total += count
        return cleaned_dict, total
    return value, 0


def _normalize_tool_arguments(value: Any, tool_name: str) -> Any:
    cleaned, count = _sanitize_tool_arguments(value)
    if count:
        log.warning(
            "清理模型工具参数中的泄漏标签 tool=%s count=%d",
            tool_name,
            count,
        )
    return cleaned


def _repair_leaked_parameter_json(raw: str) -> str | None:
    """修复 MiniMax 工具协议标签泄漏造成的有限几类 JSON 损坏。"""
    if "</parameter" not in raw:
        return None
    repaired = _LEAKED_SCALAR_ECHO.sub(r"\g<value>", raw)
    repaired = _LEAKED_PARAMETER_TOKEN.sub("", repaired)
    return repaired if repaired != raw else None


def _parse_tool_arguments(raw: str, tool_name: str) -> Any:
    """解析工具参数；仅在检测到已知协议泄漏时尝试一次受限修复。"""
    try:
        return _normalize_tool_arguments(json.loads(raw or "{}"), tool_name)
    except json.JSONDecodeError as original_exc:
        repaired = _repair_leaked_parameter_json(raw)
        if repaired is None:
            raise
        try:
            parsed = json.loads(repaired)
        except json.JSONDecodeError:
            raise original_exc
        log.warning("修复模型泄漏标签导致的损坏 JSON tool=%s", tool_name)
        return _normalize_tool_arguments(parsed, tool_name)


def _merge_tool_argument_fragment(current: str, fragment: str) -> tuple[str, str]:
    """合并标准 delta，并兼容返回完整累计 JSON 前缀的网关。

    OpenAI 流协议中的 ``function.arguments`` 是纯增量，哪怕当前串末尾与
    下一片段开头字符相同也必须原样追加。部分兼容网关会改为
    重发从 JSON 起点开始的累计快照；只有新片段包含当前 *完整* JSON 前缀
    时才能安全识别为快照。不能做任意 suffix/prefix 重叠去重，否则
    ``AI-P`` + ``PT`` 会被错误合并成 ``AI-PT``。
    """
    if not current:
        return fragment, "initial"
    if fragment.startswith(current):
        return fragment, "cumulative_snapshot"
    return current + fragment, "delta"


def _extract_partial_json_string(raw: str, key: str) -> str | None:
    """Extract a completed string value from partial JSON arguments.

    Large file_write calls keep ``content`` open for a long time, so the whole
    JSON object is invalid while earlier fields such as ``path`` are already
    complete.  This mirrors Crew' presentation-only tool generation preview:
    best-effort data for UI, never for execution.
    """
    if not raw:
        return None
    match = re.search(rf'"{re.escape(key)}"\s*:\s*"((?:\\.|[^"\\])*)"', raw)
    if not match:
        return None
    try:
        value = json.loads(f'"{match.group(1)}"')
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, str) and value.strip() else None


def _best_effort_tool_args(tool_name: str, raw_arguments: str) -> dict[str, Any]:
    """Return UI-only arguments from full or partial tool-call JSON."""
    if not raw_arguments:
        return {}
    try:
        parsed = _parse_tool_arguments(raw_arguments, tool_name)
        if not isinstance(parsed, dict):
            return {}
        if tool_name == "file_write":
            return {k: parsed[k] for k in ("path", "file_path", "append") if k in parsed}
        return parsed
    except json.JSONDecodeError:
        pass

    keys = (
        ("path", "file_path")
        if tool_name in {"file_write", "file_delete", "file_read", "patch"}
        else _PARTIAL_STRING_KEYS
    )
    out: dict[str, Any] = {}
    for key in keys:
        value = _extract_partial_json_string(raw_arguments, key)
        if value:
            out[key] = value
    return out


def _current_session() -> str:
    """读取运行期会话 id（builtin executor 在循环前已 set），仅用于 trace 标记。"""
    try:
        from crew.core.runctx import current_session_id

        return current_session_id.get() or ""
    except Exception:  # noqa: BLE001
        return ""


def _is_blank_text_message(message: Message) -> bool:
    """无多模态、无 tool_calls、文本为空的 user/system——发给 MiniMax 等会 400。"""
    if message.role not in ("user", "system"):
        return False
    if message.content_parts:
        return False
    if message.tool_calls:
        return False
    text = message.content if isinstance(message.content, str) else message.text_content
    return not (text or "").strip()


def _messages_for_openai(messages: list[Message], *, vision: bool = True) -> list[dict[str, Any]]:
    """序列化并丢弃空白 user/system，避免历史污染导致 ChatBody validation 400。

    Args:
        vision: 当前模型是否支持 image_url 多模态输入。False 时会把 content_parts
            中的图片块降级为文本占位，避免纯文本模型因收到 image_url 而 400。
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        if _is_blank_text_message(m):
            continue
        msg = m.to_openai()
        if not vision and m.content_parts:
            # 过滤 image_url，只保留 text；无 text 时补一个占位说明，避免空 content。
            removed_image = any(
                p.get("type") in {"image", "image_url", "input_image"}
                for p in m.content_parts
            )
            text_parts = [
                p.get("text", "")
                for p in m.content_parts
                if p.get("type") == "text" and p.get("text")
            ]
            if removed_image:
                text_parts.append(IMAGE_INPUT_UNAVAILABLE_NOTICE)
            if not text_parts:
                text_parts = [IMAGE_INPUT_UNAVAILABLE_NOTICE]
            msg["content"] = "\n".join(text_parts)
        out.append(msg)
    return out


def _error_category(exc: Exception) -> str:
    """将 openai 异常映射为 gateway 出站 error.category。"""
    # httpx 原生异常优先用 isinstance 判定：openai SDK 流式消费时常让底层
    # httpx 异常（如 RemoteProtocolError）原样冒泡，类名字符串匹配会漏判。
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.TransportError):  # 含 RemoteProtocolError / ReadError / ConnectError 等
        return "connection"
    # openai SDK 常把底层 httpx 断连/超时包成 APIError，原异常挂在 __cause__ 上——
    # 递归看 cause，并把消息文本也匹配上（"peer closed"/"incomplete chunked read"/"read timed out"）。
    cause = exc.__cause__
    if isinstance(cause, httpx.TimeoutException):
        return "timeout"
    if isinstance(cause, httpx.TransportError):
        return "connection"
    msg = str(exc).lower()
    if any(s in msg for s in ("read timed out", "timed out", "timeout")):
        return "timeout"
    if any(s in msg for s in ("peer closed", "incomplete chunked read", "connection", "read error", "remote protocol")):
        return "connection"
    name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    if name == "AuthenticationError" or status == 401:
        return "auth"
    if name == "PermissionDeniedError" or status == 403:
        return "forbidden"
    if name == "RateLimitError" or status == 429:
        return "rate_limit"
    if name in ("APITimeoutError", "TimeoutError"):
        return "timeout"
    if name == "APIConnectionError":
        return "connection"
    if name == "InternalServerError" or (isinstance(status, int) and status >= 500):
        return "server"
    return "provider"


def _is_retryable(exc: Exception) -> bool:
    """按异常类型判定是否瞬时错误（可重试）。

    限流 / 超时 / 连接 / 5xx 视为可重试；鉴权、请求非法等不可重试。
    用类名 + status_code 判定，避免硬依赖 openai 异常类（不同版本路径不一）。
    httpx 原生异常（流式时 SDK 常原样冒泡）用 isinstance 兜底，避免漏判。
    openai SDK 包成 APIError 时，看 __cause__ 与消息文本兜底。
    """
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    cause = exc.__cause__
    if isinstance(cause, (httpx.TimeoutException, httpx.TransportError)):
        return True
    name = type(exc).__name__
    if name in (
        "RateLimitError", "APITimeoutError", "APIConnectionError", "InternalServerError",
        "ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout", "TimeoutException",
        "RemoteProtocolError", "ConnectError",
    ):
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and (status == 429 or status >= 500):
        return True
    # 消息文本兜底：SDK 包装后的连接/超时类错误（peer closed / read timed out 等）
    msg = str(exc).lower()
    if any(s in msg for s in ("read timed out", "timed out", "peer closed", "incomplete chunked read", "read error", "remote protocol")):
        return True
    return False


def _provider_error(
    prefix: str,
    exc: Exception,
    request_messages: list[dict[str, Any]],
    safe_detail: str = "",
) -> ProviderError:
    unsupported_image = is_unsupported_image_input_error(
        exc,
        request_has_images=contains_image_input(request_messages),
    )
    detail = safe_detail or (str(exc) or "<无消息>")
    return ProviderError(
        f"{prefix}: {detail}",
        retryable=False if unsupported_image else _is_retryable(exc),
        category="unsupported_capability" if unsupported_image else _error_category(exc),
        capability="vision" if unsupported_image else None,
    )


def _is_valid_openai_message(msg: dict[str, Any]) -> bool:
    """过滤掉 OpenAI/DeepSeek 等兼容端点不接受的非法 assistant 消息。"""
    if msg.get("role") != "assistant":
        return True
    content = msg.get("content") or ""
    if isinstance(content, list):
        has_content = bool(content)
    else:
        has_content = bool(content.strip())
    has_tool_calls = bool(msg.get("tool_calls"))
    return has_content or has_tool_calls


def _filter_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """移除非法 assistant 消息（无 content 且无 tool_calls）。"""
    return [m for m in messages if _is_valid_openai_message(m)]


class OpenAIProvider(LLMProvider):
    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        model: str = "gpt-4o-mini",
        temperature: float = 0.7,
        max_tokens: int | None = None,
        timeout: float | httpx.Timeout = 120.0,
        vision: bool = True,
    ) -> None:
        # 延迟导入，避免未装 openai 时整个包不可用
        from openai import AsyncOpenAI

        if isinstance(timeout, (int, float)):
            # 流式场景下 read 超时需要更长（token 间隔可能很久，尤其是推理模型）
            timeout = httpx.Timeout(
                connect=10.0,
                read=float(timeout),
                write=10.0,
                pool=5.0,
            )
        # 共享 cadata SSLContext：避免 cacert.pem 走 cafile 的 11.7 万次 2 字节微读
        # 拖慢启动（叠加安全软件扫描可卡 40s）。详见 crew.providers.ssl_context。
        from crew.providers.ssl_context import get_shared_ssl_context

        proxy_config = provider_policy_proxy(
            base_url or "https://api.openai.com/v1",
            allow_private=base_url is not None,
        )
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            http_client=httpx.AsyncClient(
                verify=get_shared_ssl_context(),
                timeout=timeout,
                trust_env=False,
                follow_redirects=False,
                transport=proxy_config.httpx_transport(
                    verify=get_shared_ssl_context(),
                ),
            ),
        )
        self._close_lock = asyncio.Lock()
        self._closed = False
        self._secret_values = (api_key, proxy_config.password)
        self.base_url = base_url or ""
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.vision = vision

    async def aclose(self) -> None:
        """Close the owned SDK client exactly once, including concurrent callers."""
        async with self._close_lock:
            if self._closed:
                return
            # Mark before awaiting so a failed SDK close is not retried unpredictably by
            # another owner; the caller logs the failure and continues closing siblings.
            self._closed = True
            client = self._client
            try:
                await client.close()
            finally:
                self._client = None  # type: ignore[assignment]
                self._secret_values = ()

    def _safe_error(self, exc: BaseException) -> str:
        text = redact_secret_values(str(exc), self._secret_values) or ""
        return redact_sensitive_text(text, force=True)

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_tokens: int | None = None,
    ) -> ChatResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": _filter_messages(_messages_for_openai(messages, vision=self.vision)),
            "temperature": self.temperature,
        }
        effective_max_tokens = max_tokens if max_tokens is not None else self.max_tokens
        if effective_max_tokens is not None:
            payload["max_tokens"] = effective_max_tokens
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        session = _current_session()
        llm_trace("request", {
            "session_id": session, "model": self.model, "stream": False,
            "messages": payload["messages"],
            "tools": [t.get("function", {}).get("name") for t in (tools or [])],
        })

        try:
            resp = await self._client.chat.completions.create(**payload)
        except Exception as exc:  # noqa: BLE001 - 统一包装成 ProviderError
            safe_error = self._safe_error(exc)
            llm_trace("error", {"session_id": session, "model": self.model, "error": safe_error})
            raise _provider_error(
                "LLM 调用失败", exc, payload["messages"], safe_detail=safe_error
            ) from exc

        choice = resp.choices[0]
        msg = choice.message

        tool_calls: list[ToolCall] = []
        for tc in msg.tool_calls or []:
            try:
                args = _parse_tool_arguments(
                    tc.function.arguments or "{}",
                    tc.function.name,
                )
            except json.JSONDecodeError:
                args = {"_raw": tc.function.arguments}
            tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))

        usage: dict[str, int] = {}
        if resp.usage:
            cached = 0
            # OpenAI 标准：prompt_tokens_details.cached_tokens；DeepSeek：usage 顶层 prompt_cache_hit_tokens
            ptd = getattr(resp.usage, "prompt_tokens_details", None)
            if ptd is not None and getattr(ptd, "cached_tokens", None) is not None:
                cached = int(ptd.cached_tokens or 0)
            elif getattr(resp.usage, "prompt_cache_hit_tokens", None) is not None:
                cached = int(resp.usage.prompt_cache_hit_tokens or 0)
            usage = {
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
                "total_tokens": resp.usage.total_tokens,
                "cached_tokens": cached,
            }

        # 提取推理/思考内容（DeepSeek 等模型返回 reasoning_content）
        reasoning_content = ""
        if hasattr(msg, "reasoning_content") and msg.reasoning_content:
            reasoning_content = msg.reasoning_content

        llm_trace("response", {
            "session_id": session, "model": self.model, "stream": False,
            "text": msg.content or "",
            "tool_calls": [{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in tool_calls],
            "finish_reason": choice.finish_reason, "usage": usage, "reasoning": reasoning_content,
        })

        return ChatResponse(
            text=msg.content or "",
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason,
            usage=usage,
            reasoning_content=reasoning_content,
        )

    async def stream_chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """流式补全，逐 token 返回增量文本。"""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": _filter_messages(_messages_for_openai(messages, vision=self.vision)),
            "temperature": self.temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        effective_max_tokens = max_tokens if max_tokens is not None else self.max_tokens
        if effective_max_tokens is not None:
            payload["max_tokens"] = effective_max_tokens
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        session = _current_session()
        llm_trace("request", {
            "session_id": session, "model": self.model, "stream": True,
            "messages": payload["messages"],
            "tools": [t.get("function", {}).get("name") for t in (tools or [])],
        })

        try:
            stream = await self._client.chat.completions.create(**payload)
        except Exception as exc:
            safe_error = self._safe_error(exc)
            llm_trace("error", {"session_id": session, "model": self.model, "error": safe_error})
            raise _provider_error(
                "LLM 流式调用失败", exc, payload["messages"], safe_detail=safe_error
            ) from exc

        # 累积 tool_calls（流式中 tool_calls 是分段到达的）
        tool_call_accumulators: dict[int, dict[str, Any]] = {}
        emitted_ready: set[int] = set()  # 已作为 ready_tool_call 提前派发的 index
        emitted_generating: dict[int, str] = {}  # idx -> last UI-only args signature
        tried_parse_len: dict[int, int] = {}  # 增量解析去重：idx -> 上次尝试时 arguments 串长度
        finish_reason: str | None = None
        reasoning_content = ""
        full_text = ""  # 累积完整回复文本，仅用于 trace
        usage: dict[str, int] = {}  # 末尾帧（choices==[]）携带的 usage
        stream_started_at = time.perf_counter()
        reasoning_milestone_emitted = False
        named_tool_milestones: set[int] = set()
        ready_tool_milestones: set[int] = set()

        def _build_tool_call(acc: dict[str, Any]) -> ToolCall | None:
            """把累积器组装成可执行 ToolCall；id/name 缺失或参数 JSON 不完整则返回 None。

            用于流式提前派发：只有参数 JSON 完整、可安全执行的工具才提前 yield。
            """
            if not acc.get("id") or not acc.get("name"):
                return None
            try:
                args = _parse_tool_arguments(
                    acc["arguments"] or "{}",
                    str(acc["name"]),
                )
            except json.JSONDecodeError:
                return None
            return ToolCall(id=acc["id"], name=acc["name"], arguments=args)

        def _build_generating_tool_call(acc: dict[str, Any]) -> ToolCall | None:
            """工具参数生成中的 UI-only ToolCall。不会用于执行。"""
            if not acc.get("id") or not acc.get("name"):
                return None
            args = _best_effort_tool_args(str(acc["name"]), str(acc.get("arguments") or ""))
            return ToolCall(id=acc["id"], name=acc["name"], arguments=args)

        def _ensure_visible_tool_id(idx: int, acc: dict[str, Any]) -> str:
            """为只到达 name、尚未到达 provider id 的工具生成稳定展示 id。

            某些 OpenAI-compatible provider 会先流出 function.name，再长时间流出
            arguments，真实 tool_call id 可能到最后才给。UI 的 tool/start 需要稳定
            id；一旦使用临时 id，后续 ready/final/result 也沿用它，避免重复工具卡。
            """
            if acc.get("id"):
                return str(acc["id"])
            synthetic = str(acc.get("synthetic_id") or f"call_stream_{idx}")
            acc["synthetic_id"] = synthetic
            acc["id"] = synthetic
            return synthetic

        try:
            # 流式中途中断（如网关提前断连）时，openai SDK 常让底层 httpx 异常原样冒泡，
            # 且 str(exc) 可能为空——这里带类型名包装成 ProviderError，便于上层日志诊断。
            async for chunk in stream:
                # OpenAI 末尾帧 choices==[] 但携带 usage —— 直接 continue 会把 usage 丢掉
                if not chunk.choices:
                    if chunk.usage is not None:
                        ptd = getattr(chunk.usage, "prompt_tokens_details", None)
                        cached = 0
                        if ptd is not None and getattr(ptd, "cached_tokens", None) is not None:
                            cached = int(ptd.cached_tokens or 0)
                        elif getattr(chunk.usage, "prompt_cache_hit_tokens", None) is not None:
                            cached = int(chunk.usage.prompt_cache_hit_tokens or 0)
                        usage = {
                            "prompt_tokens": int(chunk.usage.prompt_tokens or 0),
                            "completion_tokens": int(chunk.usage.completion_tokens or 0),
                            "total_tokens": int(chunk.usage.total_tokens or 0),
                            "cached_tokens": cached,
                        }
                    continue
                choice = chunk.choices[0]

                delta = choice.delta
                delta_text = delta.content or ""
                if delta_text:
                    full_text += delta_text

                # 累积 reasoning_content（DeepSeek 等）：保留增量片段供下方单独 yield
                reasoning_delta = ""
                if hasattr(delta, "reasoning_content") and delta.reasoning_content:
                    reasoning_delta = delta.reasoning_content
                    reasoning_content += reasoning_delta
                    if not reasoning_milestone_emitted:
                        reasoning_milestone_emitted = True
                        llm_trace("stream_milestone", {
                            "session_id": session,
                            "model": self.model,
                            "milestone": "first_reasoning",
                            "elapsed_ms": round((time.perf_counter() - stream_started_at) * 1000),
                        })

                # 累积 tool_calls 增量
                # 注意：name/id 用赋值（=）而非追加（+=），因为 MiniMax 等
                #  provider 会在每个 chunk 中重发完整 name（#8259）。
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        acc = tool_call_accumulators.get(idx)
                        if acc is None:
                            acc = tool_call_accumulators[idx] = {
                                "id": tc.id or "",
                                "name": tc.function.name or "",
                                "arguments": tc.function.arguments or "",
                            }
                        else:
                            if tc.id:
                                if acc.get("synthetic_id"):
                                    acc["provider_id"] = tc.id
                                else:
                                    acc["id"] = tc.id
                            if tc.function.name:
                                acc["name"] = tc.function.name
                            if tc.function.arguments:
                                merged, mode = _merge_tool_argument_fragment(
                                    acc["arguments"],
                                    tc.function.arguments,
                                )
                                log.debug(
                                    "合并工具参数片段 tool=%s index=%s mode=%s current_len=%d fragment_len=%d merged_len=%d",
                                    acc.get("name") or "",
                                    idx,
                                    mode,
                                    len(acc["arguments"]),
                                    len(tc.function.arguments),
                                    len(merged),
                                )
                                acc["arguments"] = merged

                # name 一出现即通知 executor 显示「工具参数生成中」卡片；随后 arguments
                # 里 path 等字段若提前完整，也会更新同一卡片。执行仍等下方
                # ready_tool_call 用完整参数，不会误执行。
                for idx in sorted(tool_call_accumulators):
                    acc = tool_call_accumulators[idx]
                    if not acc.get("name"):
                        continue
                    if idx not in named_tool_milestones:
                        named_tool_milestones.add(idx)
                        llm_trace("stream_milestone", {
                            "session_id": session,
                            "model": self.model,
                            "milestone": "tool_name",
                            "tool_index": idx,
                            "name": acc["name"],
                            "elapsed_ms": round((time.perf_counter() - stream_started_at) * 1000),
                        })
                    _ensure_visible_tool_id(idx, acc)
                    generating = _build_generating_tool_call(acc)
                    if generating is None:
                        continue
                    signature = json.dumps(
                        {"name": generating.name, "args": generating.arguments},
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    if emitted_generating.get(idx) == signature:
                        continue
                    emitted_generating[idx] = signature
                    yield StreamChunk(tool_call_generating=generating)

                # 流式提前派发（两路信号，都仅对尚未提前派发的 index 生效）：
                #  ① 更大 index 到达 → 前序 index 视为拼完（近似 Anthropic content_block_stop）；
                #  ② arguments 增量使 JSON 首次可解析 → 该 index 视为拼完。content 字符串未闭合
                #    时整段 JSON 不合法、json.loads 必失败，故不会误判；覆盖「最后一个工具没有
                #    后续 index 触发」的场景——模型仍在生成 HTML content 的数十秒里，提前 yield
                #    ready 让前端立即看到工具卡。tried_parse_len 记上次解析长度，串未变长则跳过
                #    重复解析（content 极长时避免逐 token json.loads）。
                if tool_call_accumulators:
                    max_idx = max(tool_call_accumulators)
                    for idx in sorted(tool_call_accumulators):
                        if idx in emitted_ready:
                            continue
                        acc = tool_call_accumulators[idx]
                        args_str = acc["arguments"]
                        # 仅在串变长时才尝试解析（避免对同一截断串反复 json.loads）
                        idx_changed = len(args_str) != tried_parse_len.get(idx, -1)
                        if not (idx < max_idx or (args_str and idx_changed)):
                            continue
                        ready = _build_tool_call(acc)
                        if ready is None:
                            tried_parse_len[idx] = len(args_str)  # 标记已试，下帧未变长不再试
                            continue
                        emitted_ready.add(idx)
                        tried_parse_len[idx] = len(args_str)
                        if idx not in ready_tool_milestones:
                            ready_tool_milestones.add(idx)
                            llm_trace("stream_milestone", {
                                "session_id": session,
                                "model": self.model,
                                "milestone": "tool_arguments_ready",
                                "tool_index": idx,
                                "name": ready.name,
                                "elapsed_ms": round((time.perf_counter() - stream_started_at) * 1000),
                            })
                        yield StreamChunk(ready_tool_call=ready)

                if choice.finish_reason is not None:
                    finish_reason = choice.finish_reason

                if reasoning_delta:
                    yield StreamChunk(reasoning_content=reasoning_delta)

                if delta_text:
                    yield StreamChunk(delta_text=delta_text)
        except Exception as exc:
            # 流式中途中断：保留上层 builtin 的续写判定（retryable/category），
            # 仅补全类型名让 WARNING 日志可读。未 emit 任何 chunk 时上层按普通失败重试。
            safe_error = self._safe_error(exc)
            llm_trace("error", {
                "session_id": session, "model": self.model, "stream": True,
                "error_type": type(exc).__name__,
                "error": safe_error,
            })
            # 采用 PARTIAL_STREAM_STUB：流式中断若发生在 tool_call 生成阶段
            # （accumulators 非空且文本极少，说明模型在产工具参数而非正文），不丢半截
            # tool args、不误走文本续写——改为产出 length 截断信号（partial tool_calls
            # 走 _raw 兜底 + finish_reason="length"），交主循环截断自愈（bump-retry +
            # split-guidance）。仅对可恢复中断（timeout/connection）生效，auth 等不转。
            recoverable = _is_retryable(exc) and _error_category(exc) in (
                "timeout", "connection", "server", "rate_limit",
            )
            partial_tools = [
                acc for acc in tool_call_accumulators.values()
                if acc.get("id") or acc.get("name") or acc.get("arguments")
            ]
            if recoverable and partial_tools and len(full_text) < 200:
                partial_assembled = [
                    ToolCall(id=acc["id"], name=acc["name"], arguments={"_raw": acc["arguments"]})
                    for acc in partial_tools
                ]
                llm_trace("response", {
                    "session_id": session, "model": self.model, "stream": True,
                    "text": full_text, "stream_interrupt_to_length": True,
                    "tool_calls": [{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in partial_assembled],
                    "finish_reason": "length", "reasoning": reasoning_content,
                })
                yield StreamChunk(
                    delta_text="",
                    done=True,
                    tool_calls=partial_assembled,
                    finish_reason="length",
                    reasoning_content=reasoning_content,
                )
                return
            raise _provider_error(
                f"LLM 流式中断: {type(exc).__name__}",
                exc,
                payload["messages"],
                safe_detail=safe_error,
            ) from exc

        # 流结束，组装完整 tool_calls 并发送最终 chunk
        assembled_tool_calls: list[ToolCall] = []
        for idx in sorted(tool_call_accumulators):
            acc = tool_call_accumulators[idx]
            try:
                args = _parse_tool_arguments(
                    acc["arguments"] or "{}",
                    str(acc["name"]),
                )
            except json.JSONDecodeError:
                args = {"_raw": acc["arguments"]}
            assembled_tool_calls.append(
                ToolCall(id=acc["id"], name=acc["name"], arguments=args)
            )
            # 兜底：任何尚未提前派发的工具（如 provider 一次性吐完整 args、或 args
            # 拼到最后一帧才合法），流结束时补一帧 ready_tool_call，统一走「提前派发」路径。
            if idx not in emitted_ready:
                ready = _build_tool_call(acc)
                if ready is not None:
                    emitted_ready.add(idx)
                    yield StreamChunk(ready_tool_call=ready)

        llm_trace("response", {
            "session_id": session, "model": self.model, "stream": True,
            "text": full_text,
            "tool_calls": [{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in assembled_tool_calls],
            "finish_reason": finish_reason, "reasoning": reasoning_content,
        })

        yield StreamChunk(
            delta_text="",
            done=True,
            tool_calls=assembled_tool_calls,
            finish_reason=finish_reason,
            reasoning_content=reasoning_content,
            usage=usage,
        )
