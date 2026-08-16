"""SQLite 会话存储。把 Message 列表序列化为 JSON 存表。

用于 Crew_state.py（其用 SQLite + FTS5），这里先做基础持久化，
全文检索等留作扩展点。
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any, TypeVar

from crew.core.interfaces import SessionStore
from crew.core.types import Message, ToolCall
from crew.state._migration import rebuild_table_pk
from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite

T = TypeVar("T")


class SessionOwnershipError(ValueError):
    """Raised when a client-selected session id is already owned by another account."""


PLACEHOLDER_TITLES = frozenset({"", "新会话", "新对话"})


def is_placeholder_title(title: str | None) -> bool:
    """未自定义标题时的占位文案（空串或默认「新会话/新对话」）。"""
    normalized = (title or "").strip()
    return normalized in PLACEHOLDER_TITLES or normalized.lower().startswith("[fake]")


class SQLiteSessionStore(SessionStore):
    def __init__(self, db_path: str = "crew_data/crew.db", *, wal_enabled: bool = True) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = connect_sqlite(self._path, wal_enabled=wal_enabled)
        self._writer = SQLiteWriteHelper(self._conn, self._lock)
        self._writer.execute(self._init_schema)

    def transaction(self, fn: Callable[[Any], T]) -> T:
        """Run related session/workspace writes atomically on this store connection."""
        return self._writer.execute(fn)

    def _init_schema(self, conn) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id    TEXT PRIMARY KEY,
                owner_account_id TEXT NOT NULL DEFAULT '',
                messages      TEXT NOT NULL,
                updated_at    REAL NOT NULL,
                created_at    REAL NOT NULL DEFAULT 0,
                workspace_id  TEXT NOT NULL DEFAULT 'default',
                title         TEXT NOT NULL DEFAULT '',
                message_count INTEGER NOT NULL DEFAULT 0,
                token_count   INTEGER NOT NULL DEFAULT 0,
                last_status   TEXT NOT NULL DEFAULT '',
                last_error    TEXT NOT NULL DEFAULT '',
                archived      INTEGER NOT NULL DEFAULT 0,
                pinned        INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_agent_config (
                session_id  TEXT PRIMARY KEY,
                owner_account_id TEXT NOT NULL DEFAULT '',
                config_json TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS channel_session_routes (
                owner_account_id TEXT NOT NULL,
                session_key     TEXT NOT NULL,
                session_id      TEXT NOT NULL,
                updated_at      REAL NOT NULL,
                PRIMARY KEY (owner_account_id, session_key)
            )
            """
        )
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        migrations = {
            "owner_account_id": "ALTER TABLE sessions ADD COLUMN owner_account_id TEXT NOT NULL DEFAULT ''",
            "workspace_id": "ALTER TABLE sessions ADD COLUMN workspace_id TEXT NOT NULL DEFAULT 'default'",
            "title": "ALTER TABLE sessions ADD COLUMN title TEXT NOT NULL DEFAULT ''",
            "created_at": "ALTER TABLE sessions ADD COLUMN created_at REAL NOT NULL DEFAULT 0",
            "message_count": "ALTER TABLE sessions ADD COLUMN message_count INTEGER NOT NULL DEFAULT 0",
            "token_count": "ALTER TABLE sessions ADD COLUMN token_count INTEGER NOT NULL DEFAULT 0",
            "last_status": "ALTER TABLE sessions ADD COLUMN last_status TEXT NOT NULL DEFAULT ''",
            "last_error": "ALTER TABLE sessions ADD COLUMN last_error TEXT NOT NULL DEFAULT ''",
            "archived": "ALTER TABLE sessions ADD COLUMN archived INTEGER NOT NULL DEFAULT 0",
            "pinned": "ALTER TABLE sessions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0",
        }
        for col, ddl in migrations.items():
            if col not in cols:
                conn.execute(ddl)
        for table in ("session_agent_config",):
            table_cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if "owner_account_id" not in table_cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN owner_account_id TEXT NOT NULL DEFAULT ''")
        self._migrate_owned_aux_tables(conn)
        self._migrate_sessions_pk(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_owner_updated ON sessions(owner_account_id, updated_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_owner_workspace ON sessions(owner_account_id, workspace_id, updated_at DESC)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_channel_session_routes_active "
            "ON channel_session_routes(owner_account_id, session_id)"
        )

    def _migrate_sessions_pk(self, conn) -> None:
        """Migrate sessions from global session_id to owner-scoped identity."""

        rebuild_table_pk(
            conn,
            table="sessions",
            expected_pk=["owner_account_id", "session_id"],
            new_ddl="""
                CREATE TABLE sessions_new (
                    session_id    TEXT NOT NULL,
                    owner_account_id TEXT NOT NULL DEFAULT '',
                    messages      TEXT NOT NULL,
                    updated_at    REAL NOT NULL,
                    created_at    REAL NOT NULL DEFAULT 0,
                    workspace_id  TEXT NOT NULL DEFAULT 'default',
                    title         TEXT NOT NULL DEFAULT '',
                    message_count INTEGER NOT NULL DEFAULT 0,
                    token_count   INTEGER NOT NULL DEFAULT 0,
                    last_status   TEXT NOT NULL DEFAULT '',
                    last_error    TEXT NOT NULL DEFAULT '',
                    archived      INTEGER NOT NULL DEFAULT 0,
                    pinned        INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (owner_account_id, session_id)
                )
            """,
            copy_sql="""
                INSERT OR IGNORE INTO sessions_new (
                    session_id, owner_account_id, messages, updated_at, created_at,
                    workspace_id, title, message_count, token_count, last_status, last_error,
                    archived, pinned
                )
                SELECT
                    session_id, owner_account_id, messages, updated_at, created_at,
                    workspace_id, title, message_count, token_count, last_status, last_error,
                    COALESCE(archived, 0), COALESCE(pinned, 0)
                FROM sessions
            """,
        )

    def _migrate_owned_aux_tables(self, conn) -> None:
        """Ensure owner-scoped auxiliary tables use composite primary keys."""

        specs = {
            "session_agent_config": {
                "pk": ["owner_account_id", "session_id"],
                "ddl": """
                    CREATE TABLE session_agent_config_new (
                        session_id  TEXT NOT NULL,
                        owner_account_id TEXT NOT NULL DEFAULT '',
                        config_json TEXT NOT NULL,
                        created_at  TEXT NOT NULL,
                        updated_at  TEXT NOT NULL,
                        PRIMARY KEY (owner_account_id, session_id)
                    )
                """,
                "copy": (
                    "INSERT OR IGNORE INTO session_agent_config_new "
                    "(session_id, owner_account_id, config_json, created_at, updated_at) "
                    "SELECT session_id, owner_account_id, config_json, created_at, updated_at "
                    "FROM session_agent_config"
                ),
            },
        }
        for table, spec in specs.items():
            info = conn.execute(f"PRAGMA table_info({table})").fetchall()
            pk_columns = [
                row[1]
                for row in sorted((r for r in info if int(r[5] or 0) > 0), key=lambda r: int(r[5]))
            ]
            if pk_columns == spec["pk"]:
                continue
            conn.execute(spec["ddl"])
            conn.execute(spec["copy"])
            conn.execute(f"DROP TABLE {table}")
            conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")

    # ---- 序列化 ----
    @staticmethod
    def _dump(messages: list[Message]) -> str:
        return json.dumps([asdict(m) for m in messages], ensure_ascii=False)

    @staticmethod
    def _estimate_tokens(messages: list[Message]) -> int:
        """粗估 token 数：字符数 / 4。本地实现，避免 state 层反向依赖 agent 层。"""
        chars = 0
        for m in messages:
            chars += len(m.content or "")
            for tc in m.tool_calls:
                chars += len(tc.name) + len(str(tc.arguments))
        return chars // 4

    @staticmethod
    def _first_user_title(messages: list[Message]) -> str:
        """取首条非空、非 is_meta 的 user 消息作为标题 fallback（截断 40 字）。"""
        for m in messages:
            if m.role == "user" and m.content and not m.is_meta:
                return m.content[:40]
        return ""

    @staticmethod
    def _load(raw: str) -> list[Message]:
        out: list[Message] = []
        for d in json.loads(raw):
            tcs: list[ToolCall] = []
            for raw_tc in d.get("tool_calls", []):
                tc = dict(raw_tc)
                tc.pop("source", None)  # 兼容 2026-06-21 短暂写入过 source 的历史记录
                tcs.append(ToolCall(**tc))
            out.append(
                Message(
                    role=d["role"],
                    content=d.get("content", ""),
                    tool_calls=tcs,
                    tool_call_id=d.get("tool_call_id"),
                    name=d.get("name"),
                    model=d.get("model"),
                    is_meta=d.get("is_meta", False),  # 向后兼容：旧消息默认 False
                    timestamp=d.get("timestamp"),
                    turn_started_at=d.get("turn_started_at"),
                    turn_duration=d.get("turn_duration"),
                    turn_file_changes=d.get("turn_file_changes"),
                    thinking=d.get("thinking"),
                    content_parts=d.get("content_parts"),
                    attachment_type=d.get("attachment_type"),
                    attachment_data=d.get("attachment_data"),
                    communication_kind=d.get("communication_kind"),
                    communication_status=d.get("communication_status"),
                    request_id=d.get("request_id"),
                    reply_to=d.get("reply_to"),
                )
            )
        return out

    # ---- SessionStore 接口 ----
    def load(self, session_id: str, owner_account_id: str = "") -> list[Message]:
        with self._lock:
            row = self._conn.execute(
                "SELECT messages FROM sessions WHERE session_id = ? AND owner_account_id = ?",
                (session_id, owner_account_id),
            ).fetchone()
        return self._load(row[0]) if row else []

    def load_child_sessions(
        self,
        session_id: str,
        *,
        owner_account_id: str,
    ) -> list[tuple[str, list[Message]]]:
        """读取 Team 父会话下的内部子会话历史，供前端点击父会话时聚合回放。

        子会话 id 形如 ``{parent}::turn::...::leader`` / ``{parent}::member``，
        不直接出现在左侧会话列表，但它们承载了 Team 内 leader/成员的真实对话。
        """
        prefix = f"{session_id}::%"
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT session_id, messages
                FROM sessions
                WHERE session_id LIKE ? AND owner_account_id = ?
                ORDER BY created_at ASC, updated_at ASC, session_id ASC
                """,
                (prefix, owner_account_id),
            ).fetchall()
        return [(str(row[0]), self._load(row[1])) for row in rows]

    def save(
        self,
        session_id: str,
        messages: list[Message],
        workspace_id: str = "default",
        owner_account_id: str = "",
        *,
        title_fallback: str | None = None,
    ) -> None:
        now = time.time()
        # title_fallback=None 保持旧行为（首条 user 消息截断），兼容未传该参数的调用方；
        # title_fallback="" 显式留空占位，等 set_title 写入摘要标题（enable_title=True 时
        # 用，避免截断的用户原话抢占即将生成的摘要标题）。
        fallback_title = (
            title_fallback if title_fallback is not None else self._first_user_title(messages)
        )
        def _write(conn):
            conn.execute(
                "INSERT INTO sessions "
                "(session_id, owner_account_id, messages, updated_at, created_at, workspace_id, title, message_count, token_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(owner_account_id, session_id) DO UPDATE SET "
                "  messages = excluded.messages, "
                "  updated_at = excluded.updated_at, "
                # workspace_id 刻意不在 UPDATE 里回写：会话归属在首次创建（INSERT 或
                # ensure_session）时确定，每轮回写会用 envelope.workspace_id 覆盖掉已确定的
                # 归属，导致「test 工作空间会话刷新后漂到 default」。
                "  message_count = excluded.message_count, "
                "  token_count = excluded.token_count, "
                "  title = CASE "
                "WHEN sessions.title IS NULL OR TRIM(sessions.title) = '' "
                "OR sessions.title IN ('新会话', '新对话') "
                "THEN CASE WHEN excluded.title != '' THEN excluded.title ELSE sessions.title END "
                "ELSE sessions.title END "
                "WHERE sessions.owner_account_id = excluded.owner_account_id",
                (
                    session_id,
                    owner_account_id,
                    self._dump(messages),
                    now,
                    now,  # created_at：仅 INSERT 时写入，UPDATE 不覆盖
                    workspace_id,
                    fallback_title,
                    len(messages),
                    self._estimate_tokens(messages),
                ),
            )
        self._writer.execute(_write)

    def ensure_session(
        self,
        session_id: str,
        workspace_id: str = "default",
        title: str = "",
        owner_account_id: str = "",
    ) -> None:
        """创建一个空会话占位，用于派活后立即在侧栏展示。已有会话不覆盖。"""
        now = time.time()
        def _write(conn):
            conn.execute(
                """
                INSERT OR IGNORE INTO sessions (
                    session_id, owner_account_id, messages, updated_at, created_at, workspace_id,
                    title, message_count, token_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)
                """,
                (session_id, owner_account_id, "[]", now, now, workspace_id or "default", "" if is_placeholder_title(title) else title),
            )
        self._writer.execute(_write)

    def session_belongs_to(self, session_id: str, owner_account_id: str) -> bool:
        """Return whether a session row belongs to the given owner."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM sessions WHERE session_id = ? AND owner_account_id = ?",
                (session_id, owner_account_id),
            ).fetchone()
        return row is not None

    def append(self, session_id: str, messages: list[Message], owner_account_id: str = "") -> None:
        existing = self.load(session_id, owner_account_id=owner_account_id)
        existing.extend(messages)
        self.save(session_id, existing, owner_account_id=owner_account_id)

    def clear(self, session_id: str, owner_account_id: str = "") -> None:
        def _write(conn):
            conn.execute(
                "DELETE FROM sessions WHERE session_id = ? AND owner_account_id = ?",
                (session_id, owner_account_id),
            )
            conn.execute(
                "DELETE FROM session_agent_config WHERE session_id = ? AND owner_account_id = ?",
                (session_id, owner_account_id),
            )
        self._writer.execute(_write)

    def delete_sessions_for_workspace(
        self,
        workspace_id: str,
        owner_account_id: str = "",
        *,
        writer: Any | None = None,
    ) -> list[str]:
        """删除某工作空间下的全部会话，返回被删除的 session_id 列表。"""
        if writer is not None:
            rows = writer.execute(
                "SELECT session_id FROM sessions "
                "WHERE owner_account_id = ? AND workspace_id = ? AND session_id NOT LIKE '%::%'",
                (owner_account_id, workspace_id),
            ).fetchall()
            ids = [str(row[0]) for row in rows]
            for sid in ids:
                writer.execute(
                    "DELETE FROM sessions WHERE session_id = ? AND owner_account_id = ?",
                    (sid, owner_account_id),
                )
                writer.execute(
                    "DELETE FROM session_agent_config WHERE session_id = ? AND owner_account_id = ?",
                    (sid, owner_account_id),
                )
            return ids
        rows = self.list_sessions(workspace_id, owner_account_id=owner_account_id)
        ids = [str(r["session_id"]) for r in rows]
        for sid in ids:
            self.clear(sid, owner_account_id=owner_account_id)
        return ids

    def set_title(self, session_id: str, title: str, owner_account_id: str = "") -> None:
        def _write(conn):
            conn.execute(
                "UPDATE sessions SET title = ? WHERE session_id = ? AND owner_account_id = ?",
                (title, session_id, owner_account_id),
            )
        self._writer.execute(_write)

    def set_archived(self, session_id: str, archived: bool, owner_account_id: str = "") -> None:
        """归档 / 取消归档会话。归档会话从侧栏主列表隐藏，可在「归档」分区查看与恢复。
        归档时顺带清除置顶：置顶是主列表的排序提升，归档后不再出现在主列表，置顶无意义。"""
        archived_int = 1 if archived else 0

        def _write(conn):
            if archived:
                conn.execute(
                    "UPDATE sessions SET archived = ?, pinned = 0 "
                    "WHERE session_id = ? AND owner_account_id = ?",
                    (archived_int, session_id, owner_account_id),
                )
            else:
                conn.execute(
                    "UPDATE sessions SET archived = ? "
                    "WHERE session_id = ? AND owner_account_id = ?",
                    (archived_int, session_id, owner_account_id),
                )
        self._writer.execute(_write)

    def set_pinned(self, session_id: str, pinned: bool, owner_account_id: str = "") -> None:
        """置顶 / 取消置顶会话。置顶会话在主列表排在最前（pinned DESC, updated_at DESC）。"""
        pinned_int = 1 if pinned else 0

        def _write(conn):
            conn.execute(
                "UPDATE sessions SET pinned = ? WHERE session_id = ? AND owner_account_id = ?",
                (pinned_int, session_id, owner_account_id),
            )
        self._writer.execute(_write)

    def set_status(self, session_id: str, status: str, error: str = "", owner_account_id: str = "") -> None:
        """记录上一轮运行的 terminal 结果（completed / failed / running）。

        与 save() 解耦：save() 不碰 last_status/last_error，由 gateway 调度器单独写，
        避免被每轮整体保存覆盖。

        当状态为 running 时同步刷新 updated_at，避免长任务会话被后台过期清理误删。
        """
        now = time.time()

        def _write(conn):
            if status == "running":
                conn.execute(
                    "UPDATE sessions SET last_status = ?, last_error = ?, updated_at = ? "
                    "WHERE session_id = ? AND owner_account_id = ?",
                    (status, error, now, session_id, owner_account_id),
                )
            else:
                conn.execute(
                    "UPDATE sessions SET last_status = ?, last_error = ? "
                    "WHERE session_id = ? AND owner_account_id = ?",
                    (status, error, session_id, owner_account_id),
                )
        self._writer.execute(_write)

    def touch_session(self, session_id: str, owner_account_id: str = "") -> None:
        """刷新会话 updated_at，用于长任务运行期间保活。"""
        now = time.time()

        def _write(conn):
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ? AND owner_account_id = ?",
                (now, session_id, owner_account_id),
            )
        self._writer.execute(_write)

    def get_status(self, session_id: str, owner_account_id: str = "") -> tuple[str, str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_status, last_error FROM sessions WHERE session_id = ? AND owner_account_id = ?",
                (session_id, owner_account_id),
            ).fetchone()
        return (row[0], row[1]) if row else ("", "")

    def get_workspace_id(self, session_id: str, owner_account_id: str = "") -> str | None:
        """读取会话所属 workspace_id，不存在时返回 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT workspace_id FROM sessions WHERE session_id = ? AND owner_account_id = ?",
                (session_id, owner_account_id),
            ).fetchone()
        return str(row[0]) if row else None

    def total_usage(self, owner_account_id: str = "") -> dict[str, int]:
        """累计 token 估算与会话数（排除 Team 子会话）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(token_count), 0), COUNT(*) FROM sessions "
                "WHERE owner_account_id = ? AND session_id NOT LIKE '%::%'",
                (owner_account_id,),
            ).fetchone()
        return {"total_tokens": int(row[0] or 0), "session_count": int(row[1] or 0)}

    def context_usage(
        self,
        session_id: str,
        context_window: int | None,
        owner_account_id: str = "",
    ) -> dict[str, float | int]:
        """返回会话上下文 token 用量与占比。"""
        msgs = self.load(session_id, owner_account_id=owner_account_id)
        used = self._estimate_tokens(msgs)
        max_tokens = int(context_window or 128000)
        ratio = round(used / max_tokens, 4) if max_tokens > 0 else 0.0
        return {"used_tokens": used, "max_tokens": max_tokens, "ratio": ratio}

    def list_sessions(
        self,
        workspace_id: str | None = None,
        owner_account_id: str = "",
        *,
        include_archived: bool = False,
        exclude_channel_sessions: bool = True,
    ) -> list[dict]:
        # 排除内部子会话（Team 的 leader/teammate，id 含 "::"）
        # 直接取存储的元数据列，无需反序列化 messages（title/message_count 在 save 时写好）
        sql = (
            "SELECT session_id, title, message_count, updated_at, created_at, workspace_id, last_status, archived, pinned "
            "FROM sessions WHERE owner_account_id = ? AND session_id NOT LIKE '%::%'"
        )
        params: list = [owner_account_id]
        if exclude_channel_sessions:
            sql += " AND session_id NOT LIKE 'agent:main:%'"
        if not include_archived:
            sql += " AND archived = 0"
        if workspace_id is not None:
            sql += " AND workspace_id = ?"
            params.append(workspace_id)
        # 置顶优先，再按更新时间倒序
        sql += " ORDER BY pinned DESC, updated_at DESC"
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [
            {
                "session_id": sid,
                "title": (title or "新会话")[:40],
                "message_count": msg_count,
                "updated_at": updated_at,
                "created_at": created_at,
                "workspace_id": wid,
                "last_status": last_status,
                "archived": bool(archived),
                "pinned": bool(pinned),
            }
            for sid, title, msg_count, updated_at, created_at, wid, last_status, archived, pinned in rows
        ]

    def expire_idle_sessions(
        self,
        idle_seconds: float,
        exclude_session_ids: set[str] | None = None,
    ) -> int:
        """删除超过空闲阈值的会话。

        用于 SessionStore._is_session_expired（idle 模式）。
        正在运行中的会话（last_status 为空或 'completed'/'failed'/'stopped' 以外的）不会被删除。
        另外可通过 exclude_session_ids 显式排除当前 dispatcher 内存中 running/queued 的会话。

        Returns:
            删除的会话数量。
        """
        if idle_seconds <= 0:
            return 0
        exclude_session_ids = set(exclude_session_ids or ())
        # 把 exclude 集合也当作“不可删除”保护，即使 last_status 尚未写入 running
        safe_statuses = ("", "completed", "failed", "stopped")
        status_placeholders = ",".join("?" for _ in safe_statuses)
        params: list = [time.time() - idle_seconds, *safe_statuses]
        exclude_clause = ""
        if exclude_session_ids:
            exclude_placeholders = ",".join("?" for _ in exclude_session_ids)
            exclude_clause = f" AND session_id NOT IN ({exclude_placeholders})"
            params.extend(exclude_session_ids)

        def _write(conn):
            cursor = conn.execute(
                f"DELETE FROM sessions "
                f"WHERE updated_at < ? AND last_status IN ({status_placeholders}) "
                f"AND session_id NOT LIKE '%::%'" + exclude_clause,
                params,
            )
            return cursor.rowcount
        return self._writer.execute(_write)

    # ---- Session 级 AgentConfig ----
    @staticmethod
    def _now_iso() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def set_agent_config(
        self,
        session_id: str,
        config: dict[str, Any],
        owner_account_id: str = "",
    ) -> dict[str, Any]:
        """为某个 session 写入专属 agent.executor 配置。"""
        now = self._now_iso()
        payload = json.dumps(config, ensure_ascii=False)
        def _write(conn):
            row = conn.execute(
                "SELECT created_at FROM session_agent_config WHERE session_id = ? AND owner_account_id = ?",
                (session_id, owner_account_id),
            ).fetchone()
            created_at = row[0] if row else now
            conn.execute(
                """
                INSERT OR REPLACE INTO session_agent_config (
                    session_id, owner_account_id, config_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, owner_account_id, payload, created_at, now),
            )
        self._writer.execute(_write)
        return self.get_agent_config(session_id, owner_account_id=owner_account_id) or {}

    def update_agent_config(
        self,
        session_id: str,
        updater: Callable[[dict[str, Any]], dict[str, Any]],
        owner_account_id: str = "",
    ) -> dict[str, Any]:
        """Atomically read, transform, and persist one Session AgentConfig."""

        now = self._now_iso()

        def _write(conn):
            row = conn.execute(
                """
                SELECT config_json, created_at
                FROM session_agent_config
                WHERE session_id = ? AND owner_account_id = ?
                """,
                (session_id, owner_account_id),
            ).fetchone()
            try:
                current = json.loads(str(row[0] or "{}")) if row is not None else {}
            except json.JSONDecodeError:
                current = {}
            if not isinstance(current, dict):
                current = {}
            updated = updater(dict(current))
            if not isinstance(updated, dict):
                raise TypeError("AgentConfig updater 必须返回 dict")
            if updated == current:
                return current
            created_at = str(row[1] or now) if row is not None else now
            conn.execute(
                """
                INSERT OR REPLACE INTO session_agent_config (
                    session_id, owner_account_id, config_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    owner_account_id,
                    json.dumps(updated, ensure_ascii=False),
                    created_at,
                    now,
                ),
            )
            return updated

        self._writer.execute(_write)
        return self.get_agent_config(session_id, owner_account_id=owner_account_id) or {}

    def get_agent_config(self, session_id: str, owner_account_id: str = "") -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT config_json, created_at, updated_at FROM session_agent_config "
                "WHERE session_id = ? AND owner_account_id = ?",
                (session_id, owner_account_id),
            ).fetchone()
        if not row:
            return None
        try:
            config = json.loads(row[0] or "{}")
        except json.JSONDecodeError:
            config = {}
        config["_created_at"] = row[1]
        config["_updated_at"] = row[2]
        return config

    def clear_agent_config(self, session_id: str, owner_account_id: str = "") -> None:
        def _write(conn):
            conn.execute(
                "DELETE FROM session_agent_config WHERE session_id = ? AND owner_account_id = ?",
                (session_id, owner_account_id),
            )
        self._writer.execute(_write)

    # ---- 渠道稳定 key -> 当前实际 session ----
    def get_channel_session(self, session_key: str, owner_account_id: str = "") -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT session_id FROM channel_session_routes "
                "WHERE owner_account_id = ? AND session_key = ?",
                (owner_account_id, session_key),
            ).fetchone()
        return str(row[0]) if row else None

    def set_channel_session(
        self,
        session_key: str,
        session_id: str,
        owner_account_id: str = "",
    ) -> None:
        now = time.time()

        def _write(conn):
            conn.execute(
                """
                INSERT INTO channel_session_routes (
                    owner_account_id, session_key, session_id, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(owner_account_id, session_key) DO UPDATE SET
                    session_id = excluded.session_id,
                    updated_at = excluded.updated_at
                """,
                (owner_account_id, session_key, session_id, now),
            )

        self._writer.execute(_write)

    def get_channel_session_key(
        self,
        session_id: str,
        owner_account_id: str = "",
    ) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT session_key FROM channel_session_routes "
                "WHERE owner_account_id = ? AND session_id = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (owner_account_id, session_id),
            ).fetchone()
        return str(row[0]) if row else None
