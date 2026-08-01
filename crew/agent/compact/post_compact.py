"""Post-Compact 文件附件恢复。

压缩后，把最近读取的文件内容附加回上下文，避免模型在摘要后丢失
"刚才文件里写了什么"这类关键信息。
"""

from __future__ import annotations

from crew.core.types import Message


# 附件消息前缀，与 SUMMARY_MARKER 风格一致，便于前端识别。
POST_COMPACT_FILES_MARKER = "【压缩后恢复的文件】"


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
        file_contents[path] = m.content

    # 按路径排序后取最近 N 个（顺序稳定）
    paths = sorted(file_contents.keys())
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
