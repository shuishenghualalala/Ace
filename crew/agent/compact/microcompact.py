"""L1 MicroCompact：纯规则、无 LLM 的廉价预压。

旧工具结果（Read/Bash/Grep 等的大段输出）是上下文里最占 token 的部分，
且越早的越没有参考价值。本层把较早的 ``role=="tool"`` 消息内容替换成占位符，
只保留最近 ``keep_recent_tools`` 条原样。

特性：
- 纯函数，不修改入参（只 copy 被改写的消息）。
- 保留 tool 消息本身与 ``tool_call_id``，不破坏 assistant(tool_calls)↔tool 配对。
- 幂等：已清理的消息保持清理。
- 护栏：可清理的 tool 消息不足时原样返回（瞬时 no-op）。
- file_read 去重：相同路径重复读取且内容未变时，旧结果替换为
  ``FILE_UNCHANGED_STUB``，让模型能区分"文件未变"与"内容被清理"。
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from crew.core.types import Message

CLEARED_PLACEHOLDER = "[旧工具结果已清理]"
FILE_UNCHANGED_STUB = "[file_read {path} 自上次读取以来内容未改变]"
# 信息性摘要统一前缀：供模型识别「这是压缩摘要」并保证 micro_compact 幂等。
# 用于 _summarize_tool_result（agent/context_compressor.py:400），适配 Crew 工具名。
TOOL_SUMMARY_PREFIX = "[已压缩工具摘要] "


def _extract_tool_path(tool_name: str, arguments: dict[str, Any] | None) -> str | None:
    """从工具参数中提取用于去重的路径标识。

    目前仅支持 ``file_read`` 的 ``path`` 参数；未来可扩展其他 path-scoped 工具。
    """
    if tool_name != "file_read" or not arguments:
        return None
    path = arguments.get("path")
    return str(path) if path is not None else None


def _build_tool_call_map(
    messages: list[Message],
) -> dict[str, tuple[str, dict[str, Any] | None]]:
    """构建 ``tool_call_id → (tool_name, arguments)`` 映射。

    扫描消息列表中的 assistant 消息，提取 ``tool_calls`` 信息，供后续 tool 消息
    查找对应的工具名和参数。
    """
    mapping: dict[str, tuple[str, dict[str, Any] | None]] = {}
    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            for tc in m.tool_calls:
                mapping[tc.id] = (tc.name, tc.arguments)
    return mapping


def _truncate_tool_result(content: str, max_chars: int) -> str:
    """截断超长 tool result，保留前后片段以便定位。

    扣除标记长度后再对半切，保证截断结果总长 ≤ max_chars，从而幂等
    （二次压缩时 len(content) ≤ max_chars 不再截断，且「原长度」标注不漂移）。
    """
    if max_chars <= 0 or len(content) <= max_chars:
        return content
    marker = f"\n...（tool result 已截断，原长度 {len(content)} 字符）\n"
    half = max(1, (max_chars - len(marker)) // 2)
    return content[:half] + marker + content[-half:]


def _summarize_tool_result(
    tool_name: str, arguments: dict[str, Any] | None, content: str
) -> str:
    """为旧工具结果生成 1 行信息性摘要（纯规则，不调 LLM）。

    用于 ``_summarize_tool_result``（agent/context_compressor.py:400），
    适配 Crew 工具名。用 ``TOOL_SUMMARY_PREFIX`` 前缀以保证 micro_compact 幂等。

    返回如：``[已压缩工具摘要] [terminal] ran `npm test` -> exit 0, 47 lines output``
    """
    args = arguments or {}
    content = content or ""
    content_len = len(content)
    line_count = content.count("\n") + 1 if content.strip() else 0

    if tool_name == "terminal":
        cmd = str(args.get("command", ""))
        if len(cmd) > 80:
            cmd = cmd[:77] + "..."
        exit_match = re.search(r"exit[_ ]?code[\"':\s]+(-?\d+)", content, re.I)
        exit_code = exit_match.group(1) if exit_match else "?"
        body = f"[terminal] ran `{cmd}` -> exit {exit_code}, {line_count} lines output"
    elif tool_name == "file_read":
        path = args.get("path", "?")
        offset = args.get("offset", 1)
        body = f"[file_read] read {path} from line {offset} ({content_len:,} chars)"
    elif tool_name == "file_write":
        path = args.get("path", "?")
        written = args.get("content", "")
        written_lines = written.count("\n") + 1 if written else "?"
        body = f"[file_write] wrote to {path} ({written_lines} lines)"
    elif tool_name == "glob":
        pattern = args.get("pattern", "?")
        path = args.get("path", ".")
        body = f"[glob] find '{pattern}' in {path} -> ~{line_count} entries"
    elif tool_name == "grep":
        pattern = args.get("pattern", "?")
        path = args.get("path", ".")
        output_mode = args.get("output_mode", "files_with_matches")
        body = f"[grep] search '{pattern}' in {path} ({output_mode}) -> ~{line_count} lines"
    elif tool_name == "patch":
        path = args.get("path", "?")
        mode = args.get("mode", "replace")
        body = f"[patch] {mode} in {path} ({content_len:,} chars result)"
    elif tool_name == "web_search":
        query = args.get("query", "?")
        body = f"[web_search] query='{query}' ({content_len:,} chars result)"
    elif tool_name == "web_extract":
        urls = args.get("urls", [])
        url_desc = urls[0] if isinstance(urls, list) and urls else "?"
        if isinstance(urls, list) and len(urls) > 1:
            url_desc += f" (+{len(urls) - 1} more)"
        body = f"[web_extract] {url_desc} ({content_len:,} chars)"
    else:
        # 通用 fallback：保留工具名 + 前 2 个参数 + 内容长度
        first_arg = ""
        for k, v in list(args.items())[:2]:
            first_arg += f" {k}={str(v)[:40]}"
        body = f"[{tool_name}]{first_arg} ({content_len:,} chars result)"

    return f"{TOOL_SUMMARY_PREFIX}{body}"


def micro_compact(
    messages: list[Message],
    keep_recent_tools: int = 6,
    max_tool_result_chars: int = 0,
) -> list[Message]:
    """清理较早的工具结果内容，保留最近 ``keep_recent_tools`` 条。

    对 ``file_read`` 增加去重：若旧 tool 结果与最近一次的完整内容相同，替换为
    ``FILE_UNCHANGED_STUB``；其他工具仍用 ``CLEARED_PLACEHOLDER``。

    当 ``max_tool_result_chars > 0`` 时，单条 tool result 超过此长度会被截断
    （保留前后片段），避免单条结果撑爆上下文。

    返回新列表；无可清理项时返回原列表（同一引用）。
    """
    keep = max(0, keep_recent_tools)

    # 收集所有 tool 消息的下标（按出现顺序）
    tool_indices = [i for i, m in enumerate(messages) if m.role == "tool"]
    if len(tool_indices) <= keep and max_tool_result_chars <= 0:
        return messages  # 没有可清理项且无截断，瞬时返回

    # 最近 keep 条保留，更早的清理
    clear_indices = set(tool_indices[: max(0, len(tool_indices) - keep)])

    # 构建 tool_call_id → (tool_name, arguments) 映射
    tool_call_map = _build_tool_call_map(messages)

    # 记录每个 (tool_name, path) 在待清理范围内最近一次的完整内容
    last_seen_content: dict[tuple[str, str], str] = {}

    result: list[Message] = []
    for i, m in enumerate(messages):
        # 先处理单条 tool result 长度上限（即使保留的也截断）
        content = m.content
        if m.role == "tool" and content and max_tool_result_chars > 0:
            content = _truncate_tool_result(content, max_tool_result_chars)

        if i in clear_indices and content and content != CLEARED_PLACEHOLDER:
            # 已是压缩产物（信息摘要 / file_read stub）的消息保持幂等
            if content.startswith(TOOL_SUMMARY_PREFIX) or content.startswith(
                FILE_UNCHANGED_STUB.split("{", 1)[0]
            ):
                result.append(replace(m, content=content) if content != m.content else m)
                continue

            # 通过 tool_call_id 获取工具名和参数；失败时降级用 Message.name
            tool_name, arguments = tool_call_map.get(
                m.tool_call_id or "", (m.name or "", None)
            )

            path = _extract_tool_path(tool_name, arguments)
            if path is not None:
                dedup_key = (tool_name, path)
                last_content = last_seen_content.get(dedup_key)
                if last_content is not None and last_content == content:
                    stub = FILE_UNCHANGED_STUB.format(path=path)
                    result.append(replace(m, content=stub))
                    continue
                # 首次见到此 (tool_name, path) 或内容已变：保留原内容并更新记录
                last_seen_content[dedup_key] = content
                result.append(replace(m, content=content) if content != m.content else m)
                continue

            # 非 file_read 工具 → 信息性摘要（保留工具名/命令/结果概要，用于）；
            # 拿不到工具名时降级为纯占位符
            if tool_name:
                result.append(
                    replace(m, content=_summarize_tool_result(tool_name, arguments, content))
                )
            else:
                result.append(replace(m, content=CLEARED_PLACEHOLDER))
        else:
            result.append(replace(m, content=content) if content != m.content else m)
    return result
