"""L1 MicroCompact：按工具结果生命周期执行的无 LLM 预压。

工具结果不能只按时间统一清理：Skill 指令、用户回答和子 Agent 结论即使较早，
仍可能决定后续行为。本层只压缩明确声明为临时过程的旧结果；资源和指令按稳定
标识保留最近版本，重要结论保持完整，直到 L2/L3 结构化摘要。

特性：
- 纯函数，不修改入参（只 copy 被改写的消息）。
- 保留 tool 消息本身与 ``tool_call_id``，不破坏 assistant(tool_calls)↔tool 配对。
- 幂等：已清理的消息保持清理。
- 护栏：可清理的 tool 消息不足时原样返回（瞬时 no-op）。
- 资源去重：相同资源只保留最近版本，旧版本替换为明确的替换标记。
- 安全默认：未知工具按重要结果保留，插件需显式声明后才能被 L1 清理。
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Callable

from crew.core.interfaces import ToolResultPolicy, ToolResultRetention
from crew.core.types import Message

CLEARED_PLACEHOLDER = "[旧工具结果已清理]"
FILE_UNCHANGED_STUB = "[file_read {path} 自上次读取以来内容未改变]"
RESOURCE_REPLACED_STUB = "[资源旧版本已替换: {identity}]"
INSTRUCTION_REPLACED_STUB = "[已加载指令的旧版本已替换: {identity}]"
# 信息性摘要统一前缀：供模型识别「这是压缩摘要」并保证 micro_compact 幂等。
# 用于 _summarize_tool_result（agent/context_compressor.py:400），适配 Crew 工具名。
TOOL_SUMMARY_PREFIX = "[已压缩工具摘要] "


ResultPolicyResolver = Callable[[str, dict[str, Any]], ToolResultPolicy]


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


def _resolve_result_policy(
    resolver: ResultPolicyResolver | None,
    tool_name: str,
    arguments: dict[str, Any] | None,
) -> ToolResultPolicy:
    """安全解析结果策略；未知或异常均按重要结果保留。"""
    if resolver is None or not tool_name:
        return ToolResultPolicy()
    try:
        policy = resolver(tool_name, arguments or {})
    except Exception:  # noqa: BLE001 - 生命周期解析失败不能破坏对话
        return ToolResultPolicy()
    return policy if isinstance(policy, ToolResultPolicy) else ToolResultPolicy()


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
    result_policy_resolver: ResultPolicyResolver | None = None,
) -> list[Message]:
    """按生命周期压缩工具结果。

    ``keep_recent_tools`` 只计算 TEMPORARY 工具，不会因为后续临时调用较多而挤掉
    INSTRUCTION / IMPORTANT。RESOURCE 和 INSTRUCTION 按 identity 只保留最近版本。
    未提供 resolver 或工具未声明策略时，结果按 IMPORTANT 原样保留。

    当 ``max_tool_result_chars > 0`` 时，单条 tool result 超过此长度会被截断
    （保留前后片段），避免单条结果撑爆上下文。

    返回新列表；无可清理项时返回原列表（同一引用）。
    """
    keep = max(0, keep_recent_tools)
    tool_call_map = _build_tool_call_map(messages)

    details: dict[int, tuple[str, dict[str, Any] | None, ToolResultPolicy]] = {}
    temporary_indices: list[int] = []
    latest_scoped_result: dict[tuple[ToolResultRetention, str, str], int] = {}
    for index, message in enumerate(messages):
        if message.role != "tool":
            continue
        tool_name, arguments = tool_call_map.get(
            message.tool_call_id or "", (message.name or "", None)
        )
        policy = _resolve_result_policy(result_policy_resolver, tool_name, arguments)
        details[index] = (tool_name, arguments, policy)
        if policy.retention is ToolResultRetention.TEMPORARY:
            temporary_indices.append(index)
        elif (
            policy.retention
            in {ToolResultRetention.RESOURCE, ToolResultRetention.INSTRUCTION}
            and policy.identity
        ):
            latest_scoped_result[(policy.retention, tool_name, policy.identity)] = index

    clear_indices = set(
        temporary_indices[: max(0, len(temporary_indices) - keep)]
    )
    if not clear_indices and not latest_scoped_result and max_tool_result_chars <= 0:
        return messages

    result: list[Message] = []
    changed = False
    for i, m in enumerate(messages):
        content = m.content
        if m.role == "tool" and content and max_tool_result_chars > 0:
            content = _truncate_tool_result(content, max_tool_result_chars)
        replacement = content

        detail = details.get(i)
        if detail is not None and content:
            tool_name, arguments, policy = detail
            already_compacted = content.startswith(
                (
                    TOOL_SUMMARY_PREFIX,
                    FILE_UNCHANGED_STUB.split("{", 1)[0],
                    RESOURCE_REPLACED_STUB.split("{", 1)[0],
                    INSTRUCTION_REPLACED_STUB.split("{", 1)[0],
                )
            )
            scoped_key = (policy.retention, tool_name, policy.identity)
            is_replaced_scoped_result = bool(
                policy.identity
                and policy.retention
                in {ToolResultRetention.RESOURCE, ToolResultRetention.INSTRUCTION}
                and latest_scoped_result.get(scoped_key) != i
            )

            if is_replaced_scoped_result and not already_compacted:
                latest_index = latest_scoped_result[scoped_key]
                latest_content = messages[latest_index].content
                if (
                    policy.retention is ToolResultRetention.RESOURCE
                    and tool_name == "file_read"
                    and latest_content == m.content
                ):
                    path = str((arguments or {}).get("path") or policy.identity)
                    replacement = FILE_UNCHANGED_STUB.format(path=path)
                elif policy.retention is ToolResultRetention.RESOURCE:
                    replacement = RESOURCE_REPLACED_STUB.format(identity=policy.identity)
                else:
                    replacement = INSTRUCTION_REPLACED_STUB.format(
                        identity=policy.identity
                    )
            elif i in clear_indices and not already_compacted:
                replacement = (
                    _summarize_tool_result(tool_name, arguments, content)
                    if tool_name
                    else CLEARED_PLACEHOLDER
                )

        if replacement != m.content:
            changed = True
            result.append(replace(m, content=replacement))
        else:
            result.append(m)
    return result if changed else messages
