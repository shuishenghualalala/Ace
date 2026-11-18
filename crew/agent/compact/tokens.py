"""Token 估算：三层压缩共用。

不依赖精确 tokenizer，采用保守启发式：
- 取 UTF-8 字节数 / 3 与（ASCII 字符数 / 4 + 非 ASCII 字符数）的较大值
- 文本估算再乘以 4/3，覆盖格式化与隐性开销
- tool_call arguments 序列化为 JSON 后按 /2 估算（覆盖引号、转义、key 名）

需要精确计数时再换真正的 tokenizer（扩展点）。
"""

from __future__ import annotations

import json

from crew.core.types import IMAGE_INPUT_UNAVAILABLE_NOTICE, Message, ToolCall


def estimate_text_tokens(text: str) -> int:
    """对单段文本做保守 token 估算。

    无需编码或遍历 ASCII 字符串即可得到相同结果。
    """
    if not text:
        return 0
    if text.isascii():
        return (len(text) // 3) * 4 // 3

    byte_len = len(text.encode("utf-8", errors="replace"))
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    non_ascii = len(text) - ascii_chars

    bytes_estimate = byte_len // 3
    chars_estimate = ascii_chars // 4 + non_ascii

    base = max(bytes_estimate, chars_estimate)
    return (base * 4) // 3


def estimate_tokens(messages: list[Message]) -> int:
    """Canonical history estimate, kept stable for persisted meter checkpoints."""
    return sum(
        estimate_text_tokens(message.content or "") + sum(map(_tool_tokens, message.tool_calls))
        for message in messages
    )


def _tool_tokens(call: ToolCall) -> int:
    arguments = json.dumps(call.arguments, ensure_ascii=False)
    return estimate_text_tokens(call.name) + max(1, len(arguments) // 2)


_IMAGE_PART_TYPES = frozenset({"image", "image_url", "input_image"})
# The precise image tokenization is model-specific. Budget decoded images
# separately so the encoder's base64 spelling is neither counted nor free.
_IMAGE_PART_TOKENS = 2048


def _request_content_tokens(message: Message, include_images: bool) -> int:
    if not message.content_parts or message.role == "tool":
        return estimate_text_tokens(message.content or "")
    total = 0
    removed_image = False
    for part in message.content_parts:
        kind = part.get("type")
        if kind in _IMAGE_PART_TYPES:
            if include_images:
                total += _IMAGE_PART_TOKENS
            else:
                removed_image = True
        elif kind == "text":
            total += estimate_text_tokens(str(part.get("text") or ""))
        else:
            total += estimate_text_tokens(json.dumps(part, ensure_ascii=False))
    if removed_image:
        total += estimate_text_tokens(IMAGE_INPUT_UNAVAILABLE_NOTICE)
    return total


def estimate_prompt_tokens(
    messages: list[Message],
    tools: list[dict] | None = None,
    *,
    include_images: bool = True,
    reasoning_echo: bool = False,
) -> int:
    """Estimate the assembled request with media, tool arguments and framing."""
    total = 3  # Assistant priming at the end of a chat request.
    for message in messages:
        total += 4 + _request_content_tokens(message, include_images)
        total += estimate_text_tokens(message.name or "") + estimate_text_tokens(message.tool_call_id or "")
        total += sum(6 + estimate_text_tokens(call.id) + _tool_tokens(call) for call in message.tool_calls)
        if reasoning_echo and message.role == "assistant":
            total += estimate_text_tokens(message.thinking or "")
    if tools:
        total += estimate_text_tokens(json.dumps(tools, ensure_ascii=False, separators=(",", ":")))
    return total
