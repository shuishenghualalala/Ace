"""记忆工具：memory。

轻量持久记忆，SQLite 后端，存储在 CREW_HOME 下。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from crew.core.errors import ToolError
from crew.state.home import get_owner_runtime_home
from crew.tools.registry import Registry, tool_result


MEMORY_SCHEMA = {
    "name": "memory",
    "description": "读写轻量持久记忆。",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["write", "search", "list", "clear"]},
            "text": {"type": "string", "description": "write 时写入的记忆内容"},
            "query": {"type": "string", "description": "search 时搜索关键词"},
        },
        "required": ["action"],
    },
}


def _memory_db_path() -> Path:
    """返回 memory 工具 SQLite 数据库路径。"""
    return Path(get_owner_runtime_home()) / "tool_memory.db"


def handle_memory(args: dict[str, Any]) -> str:
    db_path = _memory_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)  # 防御：CREW_HOME 目录可能尚未创建
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS memory (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT)")
        conn.commit()

        action = str(args.get("action", "")).strip()
        if action == "write":
            text = str(args.get("text", "")).strip()
            if not text:
                raise ToolError("text 不能为空")
            cursor = conn.execute("INSERT INTO memory (text) VALUES (?)", (text,))
            conn.commit()
            return tool_result(success=True, item={"id": cursor.lastrowid, "text": text})

        if action == "search":
            query = str(args.get("query", "")).strip()
            if query:
                rows = conn.execute(
                    "SELECT id, text FROM memory WHERE text LIKE ?", (f"%{query}%",)
                ).fetchall()
            else:
                rows = conn.execute("SELECT id, text FROM memory").fetchall()
            hits = [{"id": r[0], "text": r[1]} for r in rows]
            return tool_result(success=True, memories=hits)

        if action == "list":
            rows = conn.execute("SELECT id, text FROM memory").fetchall()
            items = [{"id": r[0], "text": r[1]} for r in rows]
            return tool_result(success=True, memories=items)

        if action == "clear":
            cursor = conn.execute("DELETE FROM memory")
            conn.commit()
            return tool_result(success=True, cleared=cursor.rowcount)

        raise ToolError(f"不支持的 action: {action}")
    finally:
        conn.close()


def register_memory_tools(registry: Registry) -> None:
    registry.register(
        name="memory",
        toolset="memory",
        schema=MEMORY_SCHEMA,
        handler=handle_memory,
        is_async=False,
        display_name="操作记忆",
        ui_label_template="记忆 {action}",
        always_load=True,
        search_hint="memory remember search list clear persistent notes",
    )
