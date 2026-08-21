"""简单记忆实现。

- NullMemory：空实现（默认）。
- SQLiteMemory：把每轮 user 输入存入 SQLite，prefetch 时按关键词朴素召回。
  足够 demo 演示"跨会话记忆"概念，向量检索等留作扩展点。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from crew.core.runctx import current_owner_account_id
from crew.core.interfaces import MemoryProvider
from crew.core.types import Message
from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite


class NullMemory(MemoryProvider):
    async def prefetch(self, session_id: str, query: str) -> str:
        return ""

    async def write(self, session_id: str, messages: list[Message]) -> None:
        return None

    async def delete(self, session_id: str, owner_account_id: str | None = None) -> None:
        return None


class SQLiteMemory(MemoryProvider):
    def __init__(
        self,
        db_path: str = "crew_data/memory.db",
        top_k: int = 3,
        *,
        wal_enabled: bool = True,
    ) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._top_k = top_k
        self._lock = threading.Lock()
        self._conn = connect_sqlite(self._path, wal_enabled=wal_enabled)
        self._writer = SQLiteWriteHelper(self._conn, self._lock)
        self._writer.execute(self._init_schema)

    def close(self) -> None:
        """关闭底层 SQLite 连接（WAL 模式下每库持有多个 fd，必须显式释放）。"""
        with self._lock:
            self._conn.close()

    def _init_schema(self, conn) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS memory ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, owner_account_id TEXT NOT NULL DEFAULT '', session_id TEXT, text TEXT, ts REAL)"
        )
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memory)").fetchall()}
        if "owner_account_id" not in cols:
            conn.execute("ALTER TABLE memory ADD COLUMN owner_account_id TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def _owner() -> str:
        return str(current_owner_account_id.get() or "").strip()

    async def prefetch(self, session_id: str, query: str) -> str:
        terms = [t for t in query.split() if len(t) >= 2]
        if not terms:
            return ""
        # 按会话隔离 + LIKE 下推到 SQL，避免全表捞回 Python 端过滤。
        like_clauses = " OR ".join("text LIKE ?" for _ in terms)
        params: list[str] = [self._owner(), session_id, *[f"%{t}%" for t in terms]]
        sql = (
            "SELECT text FROM memory WHERE owner_account_id = ? AND session_id = ? "
            f"AND ({like_clauses}) ORDER BY ts DESC LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(sql, [*params, self._top_k]).fetchall()
        return "\n".join(f"- {r[0]}" for r in rows)

    async def write(self, session_id: str, messages: list[Message]) -> None:
        users = [m.content for m in messages if m.role == "user" and m.content]
        if not users:
            return

        owner = self._owner()

        def _write(conn):
            conn.execute(
                "INSERT INTO memory (owner_account_id, session_id, text, ts) VALUES (?, ?, ?, ?)",
                (owner, session_id, users[-1], time.time()),
            )
        self._writer.execute(_write)

    async def delete(self, session_id: str, owner_account_id: str | None = None) -> None:
        """删除某会话的全部记忆行，避免删会话后库膨胀。"""
        owner = (
            str(owner_account_id).strip()
            if owner_account_id is not None
            else self._owner()
        )

        def _write(conn):
            conn.execute(
                "DELETE FROM memory WHERE owner_account_id = ? AND session_id = ?",
                (owner, session_id),
            )

        self._writer.execute(_write)
