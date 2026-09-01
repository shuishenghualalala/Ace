"""Post-Compact 受保护工具结果恢复。

压缩后，恢复仍然决定后续行为的 Skill 指令、资源快照和重要结论。恢复内容来自
本次会话中已经成功返回的工具结果，不绕过工具权限重新读取外部资源。
"""

from __future__ import annotations

import json
from typing import Any, Callable

from crew.core.interfaces import ToolResultPolicy, ToolResultRetention
from crew.core.types import Message


# 附件消息前缀，与 SUMMARY_MARKER 风格一致，便于前端识别。
POST_COMPACT_FILES_MARKER = "【压缩后恢复的文件】"
POST_COMPACT_RESULTS_MARKER = "【压缩后保留的工具结果】"

_MAX_INSTRUCTIONS = 5
_MAX_IMPORTANT_RESULTS = 8
_MAX_INSTRUCTION_CHARS = 20_000
_MAX_IMPORTANT_CHARS = 5_000
_MAX_TOTAL_ATTACHMENT_CHARS = 140_000

ResultPolicyResolver = Callable[[str, dict[str, Any]], ToolResultPolicy]


def _build_tool_call_map(
    messages: list[Message],
) -> dict[str, tuple[str, dict[str, object]]]:
    """构建 tool_call_id → (tool_name, arguments) 映射。"""
    mapping: dict[str, tuple[str, dict[str, object]]] = {}
    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            for tc in m.tool_calls:
                mapping[tc.id] = (tc.name, tc.arguments or {})
    return mapping


def _extract_file_path(tool_name: str, arguments: dict[str, object]) -> str | None:
    """从工具参数中提取文件路径。目前支持 file_read。"""
    if tool_name != "file_read" or not arguments:
        return None
    path = arguments.get("path")
    return str(path) if path is not None else None


def collect_recent_file_contents(
    messages: list[Message],
    *,
    max_files: int = 3,
    max_chars_per_file: int = 5000,
) -> dict[str, str]:
    """从消息列表中收集最近读取的文件内容。

    对每条 file_read 工具结果，按 (tool_call_id) 找到对应工具调用的 path，
    保留每个 path 最近一次（按消息顺序）的完整内容。返回 `{path: content}`。

    Args:
        messages: 待扫描的消息列表（通常是被压缩掉的 old 段）。
        max_files: 最多保留几个文件。
        max_chars_per_file: 单个文件内容最大字符数，超长的截断并加省略号。
    """
    tool_call_map = _build_tool_call_map(messages)
    file_contents: dict[str, str] = {}

    for m in messages:
        if m.role != "tool" or not m.content:
            continue
        tool_name, arguments = tool_call_map.get(
            m.tool_call_id or "", (m.name or "", {})
        )
        path = _extract_file_path(tool_name, arguments)
        if path is None:
            continue
        # 同一文件保留最近一次内容；先出现的是旧内容，后出现的是新内容，
        # 所以直接覆盖即可得到最近值。
        # 重新插入以同步 dict 顺序；最后的 key 才是真正最近读取的文件。
        file_contents.pop(path, None)
        file_contents[path] = m.content

    paths = list(file_contents.keys())
    if len(paths) > max_files:
        paths = paths[-max_files:]

    result: dict[str, str] = {}
    for path in paths:
        content = file_contents[path]
        if len(content) > max_chars_per_file:
            content = content[:max_chars_per_file] + "\n...（内容已截断）"
        result[path] = content
    return result


def build_post_compact_file_attachments(
    messages: list[Message],
    *,
    max_files: int = 3,
    max_chars_per_file: int = 5000,
) -> list[Message]:
    """构造压缩后需要恢复的文件附件消息列表。

    如果没有可恢复的文件，返回空列表。
    """
    file_contents = collect_recent_file_contents(
        messages,
        max_files=max_files,
        max_chars_per_file=max_chars_per_file,
    )
    if not file_contents:
        return []

    lines = [POST_COMPACT_FILES_MARKER]
    for path, content in file_contents.items():
        lines.append(f"\n### 文件：{path}")
        lines.append(content)

    return [Message.system("\n".join(lines))]


def _parse_protected_attachment(message: Message) -> tuple[dict[str, str], str] | None:
    if message.role != "system" or not message.content.startswith(
        POST_COMPACT_RESULTS_MARKER
    ):
        return None
    lines = message.content.splitlines()
    if len(lines) < 4:
        return None
    try:
        meta = json.loads(lines[1])
    except (TypeError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    payload_start = 3 if lines[2] == "" else 2
    return {str(k): str(v) for k, v in meta.items()}, "\n".join(lines[payload_start:])


def _attachment_message(
    *,
    retention: ToolResultRetention,
    tool_name: str,
    identity: str,
    content: str,
) -> Message:
    meta = json.dumps(
        {
            "retention": retention.value,
            "tool": tool_name,
            "identity": identity,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return Message.system(f"{POST_COMPACT_RESULTS_MARKER}\n{meta}\n\n{content}")


def _trim_attachment(content: str, max_chars: int) -> str:
    if max_chars <= 0 or len(content) <= max_chars:
        return content
    marker = "\n...（压缩后保留内容已截断）"
    if max_chars <= len(marker):
        return marker[:max_chars]
    return content[: max_chars - len(marker)] + marker


def build_post_compact_attachments(
    messages: list[Message],
    *,
    result_policy_resolver: ResultPolicyResolver,
    max_resources: int = 3,
    max_chars_per_resource: int = 5000,
) -> list[Message]:
    """恢复指令、资源和重要结果，并让附件能够跨多次压缩继续存活。"""
    tool_call_map = _build_tool_call_map(messages)
    records: dict[
        tuple[ToolResultRetention, str, str],
        tuple[int, ToolResultRetention, str, str, str],
    ] = {}

    for index, message in enumerate(messages):
        parsed = _parse_protected_attachment(message)
        if parsed is not None:
            meta, content = parsed
            try:
                retention = ToolResultRetention(meta.get("retention", ""))
            except ValueError:
                continue
            tool_name = meta.get("tool", "")
            identity = meta.get("identity", "")
            if retention is ToolResultRetention.TEMPORARY or not identity:
                continue
            records[(retention, tool_name, identity)] = (
                index,
                retention,
                tool_name,
                identity,
                content,
            )
            continue

        if message.role != "tool" or not message.content:
            continue
        tool_name, arguments = tool_call_map.get(
            message.tool_call_id or "", (message.name or "", {})
        )
        try:
            policy = result_policy_resolver(tool_name, dict(arguments or {}))
        except Exception:  # noqa: BLE001 - 恢复失败不能影响主对话
            policy = ToolResultPolicy()
        if not isinstance(policy, ToolResultPolicy):
            policy = ToolResultPolicy()
        if policy.retention is ToolResultRetention.TEMPORARY:
            continue
        if message.content.startswith(
            (
                "[已压缩工具摘要] ",
                "[file_read ",
                "[资源旧版本已替换: ",
                "[已加载指令的旧版本已替换: ",
            )
        ):
            continue

        identity = policy.identity
        if not identity:
            identity = f"call={message.tool_call_id or index}"
        key = (policy.retention, tool_name, identity)
        records[key] = (
            index,
            policy.retention,
            tool_name,
            identity,
            message.content,
        )

    grouped: dict[ToolResultRetention, list[tuple[int, ToolResultRetention, str, str, str]]] = {
        retention: [] for retention in ToolResultRetention
    }
    for record in records.values():
        grouped[record[1]].append(record)
    for values in grouped.values():
        values.sort(key=lambda item: item[0])

    recent_resources = (
        grouped[ToolResultRetention.RESOURCE][-max_resources:]
        if max_resources > 0
        else []
    )
    selected = [
        *grouped[ToolResultRetention.INSTRUCTION][-_MAX_INSTRUCTIONS:],
        *grouped[ToolResultRetention.IMPORTANT][-_MAX_IMPORTANT_RESULTS:],
        *recent_resources,
    ]
    selected.sort(key=lambda item: item[0])

    attachments: list[Message] = []
    total_chars = 0
    for _index, retention, tool_name, identity, content in selected:
        if retention is ToolResultRetention.INSTRUCTION:
            max_chars = _MAX_INSTRUCTION_CHARS
        elif retention is ToolResultRetention.IMPORTANT:
            max_chars = _MAX_IMPORTANT_CHARS
        else:
            max_chars = max_chars_per_resource
        trimmed = _trim_attachment(content, max_chars)
        remaining = _MAX_TOTAL_ATTACHMENT_CHARS - total_chars
        if remaining <= 0:
            break
        trimmed = _trim_attachment(trimmed, remaining)
        total_chars += len(trimmed)
        attachments.append(
            _attachment_message(
                retention=retention,
                tool_name=tool_name,
                identity=identity,
                content=trimmed,
            )
        )
    return attachments
