"""Post-Compact 受保护工具结果恢复。

压缩后，恢复仍然决定后续行为的 Skill 指令和重要结论（来自本次会话已成功返回的
工具结果，不绕过工具权限），以及最近读取的文件——文件按 path 去重、从磁盘重读
最新内容（不回放压缩时的旧快照），只处理本会话 file_read 已成功读取过的路径。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from crew.core.interfaces import ToolResultPolicy, ToolResultRetention
from crew.core.types import Message
from crew.tools.file_utils import (
    MAX_READ_FILE_BYTES,
    _apply_line_pagination,
    _has_binary_extension,
    _normalize_read_pagination,
    _resolve_base_dir,
    read_verified_bytes,
)


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


def _parse_file_attachment_meta(content: str) -> list[tuple[str, dict[str, object]]]:
    """解析上一轮压缩生成的文件附件 meta，让文件恢复跨多次压缩存活。"""
    lines = content.splitlines()
    if len(lines) < 2:
        return []
    try:
        meta = json.loads(lines[1])
    except (TypeError, ValueError):
        return []
    files = meta.get("files") if isinstance(meta, dict) else None
    if not isinstance(files, list):
        return []
    result: list[tuple[str, dict[str, object]]] = []
    for item in files:
        if isinstance(item, dict) and item.get("path"):
            args = {k: item[k] for k in ("offset", "limit") if item.get(k) is not None}
            result.append((str(item["path"]), args))
    return result


def collect_recent_file_reads(
    messages: list[Message],
    *,
    max_files: int = 3,
) -> list[tuple[str, dict[str, object]]]:
    """从消息列表收集最近读取过的文件，按 path 去重（分片合并为文件级）。

    同一 path 只保留最后一次调用的 offset/limit；同时识别上一轮压缩生成的
    文件附件（跨多次压缩存活）。返回按最后读取顺序排列的 ``[(path, args)]``，
    最多 max_files 个。

    Args:
        messages: 待扫描的消息列表（通常是被压缩掉的 old 段）。
        max_files: 最多保留几个文件。
    """
    tool_call_map = _build_tool_call_map(messages)
    reads: dict[str, dict[str, object]] = {}

    def _touch(path: str, arguments: dict[str, object]) -> None:
        # 重新插入以同步 dict 顺序；最后的 key 才是真正最近读取的文件。
        reads.pop(path, None)
        reads[path] = arguments

    for m in messages:
        if m.role == "system" and m.content.startswith(POST_COMPACT_FILES_MARKER):
            for path, arguments in _parse_file_attachment_meta(m.content):
                _touch(path, arguments)
            continue
        if m.role != "tool":
            continue
        tool_name, arguments = tool_call_map.get(
            m.tool_call_id or "", (m.name or "", {})
        )
        path = _extract_file_path(tool_name, arguments)
        if path is None:
            continue
        _touch(path, dict(arguments or {}))

    items = list(reads.items())
    if max_files > 0:
        items = items[-max_files:]
    return items


def _reread_file_for_restore(
    path: str,
    arguments: dict[str, object],
    *,
    max_chars: int,
) -> str | None:
    """压缩后从磁盘重读文件最新内容（不回放压缩时的旧快照）。

    只处理本会话 file_read 已成功读取过的路径；读取走 read_verified_bytes
    （身份校验句柄，拒绝符号链接/非常规文件，防 TOCTOU），并按原 offset/limit
    分页。任何失败返回 None，由调用方跳过，不影响主流程。
    """
    try:
        resolved = Path(path).expanduser()
        if not resolved.is_absolute():
            resolved = _resolve_base_dir() / resolved
        resolved = resolved.parent.resolve(strict=False) / resolved.name
        if not resolved.is_file() or _has_binary_extension(resolved):
            return None
        raw = read_verified_bytes(resolved, max_bytes=MAX_READ_FILE_BYTES)
        text = raw.decode("utf-8", errors="replace")
        total_lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
        offset, limit = _normalize_read_pagination(
            total_lines, arguments.get("offset"), arguments.get("limit")
        )
        sliced = _apply_line_pagination(text, offset, limit)
        body = _trim_attachment(sliced, max_chars)
        return (
            f"### 文件：{resolved}（压缩后从磁盘重新读取；第 {offset} 行起，共 {total_lines} 行）\n"
            f"{body}"
        )
    except Exception:  # noqa: BLE001 - 恢复失败不能影响主对话
        return None


def build_post_compact_file_attachments(
    messages: list[Message],
    *,
    max_files: int = 3,
    max_chars_per_file: int = 5000,
) -> list[Message]:
    """构造压缩后需要恢复的文件附件消息列表。

    按 path 去重后从磁盘重读最新内容；文件已删除/不可读/二进制时跳过。
    如果没有可恢复的文件，返回空列表。
    """
    reads = collect_recent_file_reads(messages, max_files=max_files)
    if not reads:
        return []

    sections: list[str] = []
    metas: list[dict[str, object]] = []
    for path, arguments in reads:
        section = _reread_file_for_restore(path, arguments, max_chars=max_chars_per_file)
        if section is None:
            continue
        sections.append(section)
        metas.append(
            {
                "path": path,
                "offset": arguments.get("offset"),
                "limit": arguments.get("limit"),
            }
        )
    if not sections:
        return []

    meta = json.dumps({"files": metas}, ensure_ascii=False, sort_keys=True)
    return [Message.system("\n\n".join([f"{POST_COMPACT_FILES_MARKER}\n{meta}", *sections]))]


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
    max_instructions: int = _MAX_INSTRUCTIONS,
    max_important: int = _MAX_IMPORTANT_RESULTS,
    max_instruction_chars: int = _MAX_INSTRUCTION_CHARS,
    max_important_chars: int = _MAX_IMPORTANT_CHARS,
    max_total_chars: int = _MAX_TOTAL_ATTACHMENT_CHARS,
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
        *(grouped[ToolResultRetention.INSTRUCTION][-max_instructions:] if max_instructions > 0 else []),
        *(grouped[ToolResultRetention.IMPORTANT][-max_important:] if max_important > 0 else []),
        *recent_resources,
    ]
    selected.sort(key=lambda item: item[0])

    attachments: list[Message] = []
    total_chars = 0
    for _index, retention, tool_name, identity, content in selected:
        if retention is ToolResultRetention.INSTRUCTION:
            max_chars = max_instruction_chars
        elif retention is ToolResultRetention.IMPORTANT:
            max_chars = max_important_chars
        else:
            max_chars = max_chars_per_resource
        trimmed = _trim_attachment(content, max_chars)
        remaining = max_total_chars - total_chars
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
