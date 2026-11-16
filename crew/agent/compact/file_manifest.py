"""文件清单：从工具调用历史提取 read/modified 文件清单，作为持久会话信息跨轮继承。

清单以 is_meta user 消息写入 canonical 历史（前端不渲染、模型可见），每轮在
压缩前原地刷新；压缩把清单摘要进 old 段时，压缩管线会把它重新注入视图
（见 pipeline._summarize_old），因此「压缩后你读过/改过这些文件」不丢失。
"""

from __future__ import annotations

from crew.core.types import Message

# 清单消息前缀标记：压缩管线据此在压缩后重新注入视图。
FILE_MANIFEST_MARKER = "【文件清单】"

# 单类清单最多保留的路径条数（按首次出现顺序，超出截断）。
_MAX_MANIFEST_PATHS = 50

# 工具名 → 清单类别。读取类与修改类分开呈现，帮助模型区分「看过」与「动过」。
_READ_TOOLS = frozenset({"file_read"})
_MODIFIED_TOOLS = frozenset({"file_write", "patch"})


def extract_file_manifest(messages: list[Message]) -> tuple[list[str], list[str]]:
    """扫描 assistant 消息的工具调用历史，提取 read/modified 文件清单。

    返回 (read_paths, modified_paths)：按首次出现顺序去重，各自封顶
    ``_MAX_MANIFEST_PATHS`` 条；无 path 参数或为空串的调用跳过。
    """
    read: list[str] = []
    modified: list[str] = []
    seen_read: set[str] = set()
    seen_modified: set[str] = set()
    for m in messages:
        if m.role != "assistant" or not m.tool_calls:
            continue
        for tc in m.tool_calls:
            path = ""
            if tc.arguments and isinstance(tc.arguments, dict):
                raw = tc.arguments.get("path")
                if isinstance(raw, str):
                    path = raw.strip()
            if not path:
                continue
            if tc.name in _READ_TOOLS and path not in seen_read:
                seen_read.add(path)
                read.append(path)
            elif tc.name in _MODIFIED_TOOLS and path not in seen_modified:
                seen_modified.add(path)
                modified.append(path)
    return read[:_MAX_MANIFEST_PATHS], modified[:_MAX_MANIFEST_PATHS]


def is_file_manifest_message(message: Message) -> bool:
    """一条消息是否为文件清单持久信息。"""
    return bool(message.content) and message.content.startswith(FILE_MANIFEST_MARKER)


def build_manifest_message(read: list[str], modified: list[str]) -> Message:
    """把 read/modified 清单渲染成持久会话信息消息（is_meta，前端不渲染）。"""
    def _lines(paths: list[str]) -> str:
        return "\n".join(f"- {p}" for p in paths) if paths else "（暂无）"

    content = (
        f"{FILE_MANIFEST_MARKER}（会话持久信息，跨上下文压缩保留）\n"
        "你此前读取过这些文件：\n"
        f"{_lines(read)}\n"
        "你此前修改过这些文件：\n"
        f"{_lines(modified)}"
    )
    return Message.user(content, is_meta=True)


def upsert_file_manifest(history: list[Message]) -> list[Message]:
    """从历史提取最新清单并写入 canonical：已有清单消息原地刷新，否则追加一条。

    清单消息本身是 is_meta user 消息，随 canonical 历史持久化（save 整表写回），
    下一轮 load 后在此刷新——不绕过会话直接改请求。还没有任何文件操作时不动历史，
    避免给新会话注入空清单噪音。
    """
    existing: Message | None = None
    for message in reversed(history):
        if is_file_manifest_message(message):
            existing = message
            break
    read, modified = extract_file_manifest(history)
    if existing is None and not read and not modified:
        return history
    latest = build_manifest_message(read, modified)
    if existing is not None:
        if existing.content != latest.content:
            existing.content = latest.content
        return history
    history.append(latest)
    return history


def shadowed_manifest_message(messages: list[Message]) -> Message | None:
    """取消息列表中最后一条文件清单消息（压缩管线用它把清单重新注入视图）。"""
    for message in reversed(messages):
        if is_file_manifest_message(message):
            return message
    return None
