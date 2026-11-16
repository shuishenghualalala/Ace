"""Token 估算：三层压缩共用。

不依赖精确 tokenizer，采用分层保守启发式：
- ASCII 文本按字符数 / 4
- 非 ASCII（中文等）按 UTF-8 字节数 / 3（约 1 token/字）
- 取两者较大值后乘以 4/3 padding，覆盖格式化与隐性开销
- tool_call arguments 序列化为 JSON 后按 /2 估算（覆盖引号、转义、key 名）

需要精确计数时再换真正的 tokenizer（扩展点）。
"""

from __future__ import annotations

import json

from crew.core.types import Message


def _estimate_text_tokens(text: str) -> int:
    """对单段文本做保守 token 估算。

    - ASCII：字符数 / 4
    - 非 ASCII：UTF-8 字节数 / 3（中文通常 3 字节/字 ≈ 1 token）
    - 取较大值后乘以 4/3 padding
    """
    if not text:
        return 0

    byte_len = len(text.encode("utf-8"))
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    non_ascii = len(text) - ascii_chars

    bytes_estimate = byte_len // 3
    chars_estimate = ascii_chars // 4 + non_ascii

    base = max(bytes_estimate, chars_estimate)
    return (base * 4) // 3


def estimate_tokens(messages: list[Message]) -> int:
    """保守估算一批消息的 token 数。"""
    total = 0
    for m in messages:
        total += _estimate_text_tokens(m.content or "")
        for tc in m.tool_calls:
            total += _estimate_text_tokens(tc.name)
            # JSON 序列化后有引号、转义、key 名等开销，按 /2 估算
            args_json = json.dumps(tc.arguments, ensure_ascii=False)
            total += max(1, len(args_json) // 2)
    return total


def estimate_prompt_tokens(messages: list[Message], tools: list[dict] | None = None) -> int:
    """估算本次实际请求视图的 token 数。

    与只统计 canonical history 不同，这里把最终发送的 system/message 视图和
    tool schemas 一起纳入，作为 Provider 未返回 usage 时的本地上下文计数。
    """
    total = estimate_tokens(messages)
    if tools:
        total += _estimate_text_tokens(json.dumps(tools, ensure_ascii=False, separators=(",", ":")))
    return total
