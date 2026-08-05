"""Anthropic Messages API Provider."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, AsyncIterator

import httpx

from crew.core.errors import ProviderError
from crew.core.interfaces import LLMProvider
from crew.core.types import ChatResponse, Message, StreamChunk, ToolCall
from crew.state.logging import llm_trace

_ANTHROPIC_VERSION = "2023-06-01"
_DEFAULT_BASE_URL = "https://api.anthropic.com"
_DEFAULT_MAX_TOKENS = 4096


def _current_session() -> str:
    try:
        from crew.core.runctx import current_session_id

        return current_session_id.get() or ""
    except Exception:  # noqa: BLE001
        return ""


def _endpoint(base_url: str | None) -> str:
    base = (base_url or _DEFAULT_BASE_URL).rstrip("/")
    return f"{base}/messages" if base.endswith("/v1") else f"{base}/v1/messages"


def _category(exc: Exception, status: int | None = None) -> str:
    if isinstance(exc, (httpx.TimeoutException,)):
        return "timeout"
    if isinstance(exc, httpx.TransportError):
        return "connection"
    if status == 401:
        return "auth"
    if status == 403:
        return "forbidden"
    if status == 429:
        return "rate_limit"
    if isinstance(status, int) and status >= 500:
        return "server"
    return "provider"


def _retryable(exc: Exception, status: int | None = None) -> bool:
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError)) or status == 429 or (
        isinstance(status, int) and status >= 500
    )


def _text_blocks(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": text}] if text else []


def _to_anthropic_tool(tool: dict[str, Any]) -> dict[str, Any]:
    if "input_schema" in tool:
        return tool
    fn = tool.get("function", {}) if tool.get("type") == "function" else tool
    return {
        "name": fn.get("name", ""),
        "description": fn.get("description", ""),
        "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
    }


def _messages_payload(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            if message.content:
                system_parts.append(message.content)
            continue
        if message.role == "tool":
            out.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.tool_call_id or "",
                            "content": message.content or "",
                        }
                    ],
                }
            )
            continue
        if message.role == "assistant":
            content = _text_blocks(message.content)
            content.extend(
                {
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.name,
                    "input": tc.arguments,
                }
                for tc in message.tool_calls
            )
            out.append({"role": "assistant", "content": content or _text_blocks("")})
            continue
        if message.content_parts:
            blocks: list[dict[str, Any]] = []
            for part in message.content_parts:
                if part.get("type") == "text":
                    text = str(part.get("text") or "")
                    if text:
                        blocks.append({"type": "text", "text": text})
                    continue
                if part.get("type") != "image_url":
                    continue
                image = part.get("image_url")
                url = image.get("url") if isinstance(image, dict) else image
                match = re.fullmatch(r"data:([^;,]+);base64,(.+)", str(url or ""), flags=re.DOTALL)
                if match:
                    blocks.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": match.group(1),
                                "data": match.group(2),
                            },
                        }
                    )
            out.append({"role": "user", "content": blocks or _text_blocks(message.text_content)})
        else:
            out.append({"role": "user", "content": _text_blocks(message.text_content)})
    return "\n\n".join(system_parts), out


def _parse_response(data: dict[str, Any]) -> ChatResponse:
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in data.get("content") or []:
        if block.get("type") == "text":
            text_parts.append(str(block.get("text") or ""))
        elif block.get("type") == "tool_use":
            raw_input = block.get("input")
            args = raw_input if isinstance(raw_input, dict) else {"_raw": raw_input}
            tool_calls.append(ToolCall(id=str(block.get("id") or ""), name=str(block.get("name") or ""), arguments=args))
    usage_raw = data.get("usage") or {}
    input_tokens = int(usage_raw.get("input_tokens") or 0)
    output_tokens = int(usage_raw.get("output_tokens") or 0)
    cache_creation = int(usage_raw.get("cache_creation_input_tokens") or 0)
    cache_read = int(usage_raw.get("cache_read_input_tokens") or 0)
    usage: dict[str, int] = {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
    }
    return ChatResponse(
        text="".join(text_parts),
        tool_calls=tool_calls,
        finish_reason=data.get("stop_reason"),
        usage=usage,
    )


class AnthropicProvider(LLMProvider):
    """Minimal Anthropic `/v1/messages` adapter for Crew's LLMProvider interface."""

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        model: str = "claude-sonnet-4-5",
        temperature: float = 0.7,
        max_tokens: int | None = None,
        timeout: float | httpx.Timeout = 120.0,
        vision: bool = True,
    ) -> None:
        if isinstance(timeout, (int, float)):
            timeout = httpx.Timeout(connect=10.0, read=float(timeout), write=10.0, pool=5.0)
        # 共享 cadata SSLContext：避免 cacert.pem 走 cafile 的 11.7 万次 2 字节微读
        # 拖慢启动（叠加安全软件扫描可卡 40s）。详见 crew.providers.ssl_context。
        from crew.providers.ssl_context import get_shared_ssl_context

        self._client = httpx.AsyncClient(
            timeout=timeout,
            verify=get_shared_ssl_context(),
        )
        self._close_lock = asyncio.Lock()
        self._closed = False
        self._url = _endpoint(base_url)
        self._headers = {
            "x-api-key": api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

    async def aclose(self) -> None:
        """Close the owned HTTP client exactly once, including concurrent callers."""
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            await self._client.aclose()

    def _payload(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_tokens_override: int | None = None,
    ) -> dict[str, Any]:
        system, converted = _messages_payload(messages)
        effective_max_tokens = (
            max_tokens_override if max_tokens_override is not None
            else (self.max_tokens or _DEFAULT_MAX_TOKENS)
        )
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": converted,
            "max_tokens": effective_max_tokens,
        }
        if system:
            # cache_control 断点缓存 system prompt（首轮 write 1.25×，后续 read 0.1×，净省）
            payload["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if tools:
            anth_tools = [_to_anthropic_tool(tool) for tool in tools]
            # 最后一个 tool 加 cache_control，缓存整段 tools 块（工具集通常每轮不变）
            if anth_tools:
                anth_tools[-1]["cache_control"] = {"type": "ephemeral"}
            payload["tools"] = anth_tools
        return payload

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_tokens: int | None = None,
    ) -> ChatResponse:
        payload = self._payload(messages, tools, max_tokens_override=max_tokens)
        session = _current_session()
        llm_trace("request", {"session_id": session, "model": self.model, "stream": False, "messages": payload["messages"]})
        try:
            response = await self._client.post(self._url, headers=self._headers, json=payload)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            llm_trace("error", {"session_id": session, "model": self.model, "error": exc.response.text})
            raise ProviderError(
                f"Anthropic 调用失败: HTTP {status}: {exc.response.text}",
                retryable=_retryable(exc, status),
                category=_category(exc, status),
            ) from exc
        except Exception as exc:  # noqa: BLE001
            llm_trace("error", {"session_id": session, "model": self.model, "error": str(exc)})
            raise ProviderError(
                f"Anthropic 调用失败: {exc}",
                retryable=_retryable(exc),
                category=_category(exc),
            ) from exc
        result = _parse_response(data)
        llm_trace(
            "response",
            {
                "session_id": session,
                "model": self.model,
                "stream": False,
                "text": result.text,
                "tool_calls": [{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in result.tool_calls],
                "finish_reason": result.finish_reason,
                "usage": result.usage,
            },
        )
        return result

    async def stream_chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamChunk]:
        payload = {**self._payload(messages, tools, max_tokens_override=max_tokens), "stream": True}
        session = _current_session()
        tool_acc: dict[int, dict[str, Any]] = {}
        assembled: list[ToolCall] = []
        text = ""
        finish_reason: str | None = None
        usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}

        def flush_tool(index: int) -> ToolCall | None:
            acc = tool_acc.get(index)
            if not acc:
                return None
            raw = acc.get("input_json") or "{}"
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                args = {"_raw": raw}
            tool = ToolCall(id=acc.get("id", ""), name=acc.get("name", ""), arguments=args)
            assembled.append(tool)
            # 残缺 JSON 仍需保留到 done 帧，供 executor 结合 stop_reason 进入截断恢复；
            # 但绝不能作为 ready_tool_call 提前派发，否则 file_read 等并发安全工具可能
            # 在 message_delta 告知 max_tokens 之前已经用半截参数开始执行。
            return None if set(args) == {"_raw"} else tool

        try:
            async with self._client.stream("POST", self._url, headers=self._headers, json=payload) as response:
                response.raise_for_status()
                event = ""
                data_lines: list[str] = []
                async for line in response.aiter_lines():
                    if not line:
                        if not data_lines:
                            continue
                        raw_data = "\n".join(data_lines)
                        data_lines = []
                        if raw_data == "[DONE]":
                            break
                        item = json.loads(raw_data)
                        if event == "message_start":
                            raw_usage = item.get("message", {}).get("usage") or {}
                            usage["prompt_tokens"] = int(raw_usage.get("input_tokens") or 0)
                            usage["cache_creation_input_tokens"] = int(raw_usage.get("cache_creation_input_tokens") or 0)
                            usage["cache_read_input_tokens"] = int(raw_usage.get("cache_read_input_tokens") or 0)
                        elif event == "content_block_start":
                            block = item.get("content_block") or {}
                            if block.get("type") == "tool_use":
                                idx = int(item.get("index") or 0)
                                bid = str(block.get("id") or "")
                                bname = str(block.get("name") or "")
                                tool_acc[idx] = {"id": bid, "name": bname, "input_json": ""}
                                # name 一出现即通知 executor 显示「工具参数生成中」卡片。
                                if bid and bname:
                                    yield StreamChunk(tool_call_generating=ToolCall(id=bid, name=bname, arguments={}))
                        elif event == "content_block_delta":
                            idx = int(item.get("index") or 0)
                            delta = item.get("delta") or {}
                            if delta.get("type") == "text_delta":
                                piece = str(delta.get("text") or "")
                                text += piece
                                if piece:
                                    yield StreamChunk(delta_text=piece)
                            elif delta.get("type") == "input_json_delta" and idx in tool_acc:
                                tool_acc[idx]["input_json"] += str(delta.get("partial_json") or "")
                        elif event == "content_block_stop":
                            ready = flush_tool(int(item.get("index") or 0))
                            if ready is not None:
                                yield StreamChunk(ready_tool_call=ready)
                        elif event == "message_delta":
                            delta = item.get("delta") or {}
                            finish_reason = delta.get("stop_reason") or finish_reason
                            raw_usage = item.get("usage") or {}
                            usage["completion_tokens"] = int(raw_usage.get("output_tokens") or usage["completion_tokens"])
                            # message_delta 的 usage 也可能更新 cache_read（累积值）
                            if raw_usage.get("cache_read_input_tokens") is not None:
                                usage["cache_read_input_tokens"] = int(raw_usage.get("cache_read_input_tokens") or 0)
                        event = ""
                        continue
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].strip())
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise ProviderError(
                f"Anthropic 流式调用失败: HTTP {status}: {exc.response.text}",
                retryable=_retryable(exc, status),
                category=_category(exc, status),
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(
                f"Anthropic 流式调用失败: {exc}",
                retryable=_retryable(exc),
                category=_category(exc),
            ) from exc

        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        llm_trace(
            "response",
            {
                "session_id": session,
                "model": self.model,
                "stream": True,
                "text": text,
                "tool_calls": [{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in assembled],
                "finish_reason": finish_reason,
                "usage": usage,
            },
        )
        yield StreamChunk(delta_text="", done=True, tool_calls=assembled, finish_reason=finish_reason, usage=usage)
