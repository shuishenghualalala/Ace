"""L2 摘要缓存的 SQLite 持久化。

把每个 session 的「结构化摘要 + 覆盖范围 + 防抖计数」落盘，
使「跨重启的零推理复用」成立——重启后历史从 SessionStore 完整恢复，
covered_count 仍指向相同的前缀位置（canonical 历史只追加），故缓存依旧有效。

复用 crew.state.sqlite 的连接与写入助手（与 SessionStore/SQLiteMemory 同一套并发策略）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path

from crew.state._migration import rebuild_table_pk
from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite


@dataclass
class SummaryState:
    """某个 session 已有的摘要、覆盖范围与防抖计数。"""

    text: str
    covered_count: int  # 已折叠进摘要的前缀消息数
    ineffective_count: int = 0  # 连续无效压缩次数（省 <10%）


class SummaryStore:
    """compaction 摘要缓存的持久化存储。"""

    def __init__(
        self, db_path: str = "crew_data/crew.db", *, wal_enabled: bool = True
    ) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = connect_sqlite(self._path, wal_enabled=wal_enabled)
        self._writer = SQLiteWriteHelper(self._conn, self._lock)
        self._writer.execute(self._init_schema)

    def _init_schema(self, conn) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS compaction_summaries (
                owner_account_id  TEXT NOT NULL DEFAULT '',
                session_id        TEXT NOT NULL,
                summary_text      TEXT NOT NULL,
                covered_count     INTEGER NOT NULL,
                ineffective_count INTEGER NOT NULL DEFAULT 0,
                updated_at        REAL NOT NULL,
                PRIMARY KEY (owner_account_id, session_id)
            )
            """
        )
        rebuild_table_pk(
            conn,
            table="compaction_summaries",
            expected_pk=["owner_account_id", "session_id"],
            new_ddl="""
                CREATE TABLE compaction_summaries_new (
                    owner_account_id  TEXT NOT NULL DEFAULT '',
                    session_id        TEXT NOT NULL,
                    summary_text      TEXT NOT NULL,
                    covered_count     INTEGER NOT NULL,
                    ineffective_count INTEGER NOT NULL DEFAULT 0,
                    updated_at        REAL NOT NULL,
                    PRIMARY KEY (owner_account_id, session_id)
                )
            """,
            copy_sql="""
                INSERT OR IGNORE INTO compaction_summaries_new (
                    owner_account_id, session_id, summary_text, covered_count,
                    ineffective_count, updated_at
                )
                SELECT '', session_id, summary_text, covered_count, ineffective_count, updated_at
                FROM compaction_summaries
            """,
        )

    def get(self, session_id: str, owner_account_id: str = "") -> SummaryState | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT summary_text, covered_count, ineffective_count "
                "FROM compaction_summaries WHERE owner_account_id = ? AND session_id = ?",
                (owner_account_id, session_id),
            ).fetchone()
        if not row:
            return None
        return SummaryState(text=row[0], covered_count=int(row[1]), ineffective_count=int(row[2]))

    def put(self, session_id: str, state: SummaryState, owner_account_id: str = "") -> None:
        def _write(conn):
            conn.execute(
                "INSERT INTO compaction_summaries "
                "(owner_account_id, session_id, summary_text, covered_count, ineffective_count, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(owner_account_id, session_id) DO UPDATE SET "
                "summary_text=excluded.summary_text, "
                "covered_count=excluded.covered_count, "
                "ineffective_count=excluded.ineffective_count, "
                "updated_at=excluded.updated_at",
                (owner_account_id, session_id, state.text, state.covered_count, state.ineffective_count, time.time()),
            )

        self._writer.execute(_write)

    def delete(self, session_id: str, owner_account_id: str = "") -> None:
        def _write(conn):
            conn.execute(
                "DELETE FROM compaction_summaries WHERE owner_account_id = ? AND session_id = ?",
                (owner_account_id, session_id),
            )

        self._writer.execute(_write)
