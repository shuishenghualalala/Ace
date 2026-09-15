"""SQLite 会话存储。把 Message 列表序列化为 JSON 存表。

用于 Crew_state.py（其用 SQLite + FTS5），这里先做基础持久化，
全文检索等留作扩展点。
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

from crew.core.interfaces import SessionStore
from crew.core.types import Message, ToolCall
from crew.state._migration import backfill_empty_owner_rows, rebuild_table_pk
from crew.state.logging import get_logger
from crew.state.schema_version import stamp_baseline
from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite

log = get_logger("state.session_store")

T = TypeVar("T")

# sessions 域 schema 版本：v1 = 单表 blob 基线；v2 = 增量事件表（ADR-0042 W4）；
# v3 = 事件 parent_seq 链 + 会话树（rewind/fork，ADR-0042 W5）。
SESSIONS_SCHEMA_FEATURE = "sessions"
SESSIONS_SCHEMA_VERSION = 3


class SessionWriteConflict(RuntimeError):
    """另一进程持有该会话的有效 writer 租约，本进程写入被拒绝。"""


class SessionEventType(str, Enum):
    """session_events 事件类型；durable=False 的瞬态事件不落盘。"""

    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_RESULT = "tool_result"
    SYSTEM_MESSAGE = "system_message"
    METER_CHECKPOINT = "meter_checkpoint"
    TURN_START = "turn_start"  # 回合边界（D4 断点扫描）
    TURN_END = "turn_end"
    END_SEED = "end_seed"  # fork 切口标记：载荷内联 (source_session_id, parent_seq)
    COMPACTION = "compaction"  # 压缩自包含 checkpoint（replacement + 重锚定估算）
    TURN_PROGRESS = "turn_progress"  # 瞬态：进度推送
    ERROR = "error"  # 瞬态：错误通知
    QUEUE_STATE = "queue_state"  # 瞬态：调度队列变化

    @property
    def durable(self) -> bool:
        return self not in (
            SessionEventType.TURN_PROGRESS,
            SessionEventType.ERROR,
            SessionEventType.QUEUE_STATE,
        )


_ROLE_EVENT_TYPES = {
    "user": SessionEventType.USER_MESSAGE,
    "assistant": SessionEventType.ASSISTANT_MESSAGE,
    "tool": SessionEventType.TOOL_RESULT,
    "system": SessionEventType.SYSTEM_MESSAGE,
}


class SessionOwnershipError(ValueError):
    """Raised when a client-selected session id is already owned by another account."""


class SessionEventLogError(RuntimeError):
    """事件流与投影游标不一致（缺口/未知类型），拒绝加载。"""


PLACEHOLDER_TITLES = frozenset({"", "新会话", "新对话"})


def is_placeholder_title(title: str | None) -> bool:
    """未自定义标题时的占位文案（空串或默认「新会话/新对话」）。"""
    normalized = (title or "").strip()
    return normalized in PLACEHOLDER_TITLES or normalized.lower().startswith("[fake]")


class _SessionProjection:
    """per-session 内存投影：已解析的消息 + 已应用的事件游标 + 代际标记。

    events_generation 在事件流被整体重写时 +1：读者据此识别跨进程重写，
    整体重建投影，而不是把新尾部增量拼到已过时的前缀上。
    """

    __slots__ = ("messages", "seq", "generation")

    def __init__(self) -> None:
        self.messages: list[Message] = []
        self.seq: int = 0
        self.generation: int = 0


class _SessionWriteQueue:
    """per-session 单写队列：一个写 task + asyncio.Queue 串行落库。

    flush barrier = 带 ack 的往返：调用方 enqueue 后 await ack，leaf 指针与
    事件批次在同一事务提交后 ack 才完成，调用方 await 到 ack 即视为持久化。
    写失败保留队列项重试一次；队列 task 的注册返回 disposer（注册即 effect）。
    队列空转时按租约心跳周期续约本进程持有的 writer 租约。
    """

    def __init__(self, store: "SQLiteSessionStore", loop: asyncio.AbstractEventLoop) -> None:
        self._store = store
        self._writer = store._writer
        self._loop = loop
        self._queue: asyncio.Queue = asyncio.Queue()
        # 串行化「diff 计算 → 入队 → await ack」整段，防止并发 save_async 用同一
        # 过期投影各算各的 diff 导致事件重复。
        self.lock = asyncio.Lock()
        self._task = loop.create_task(self._run())
        self._disposed = False

    def enqueue(self, fn: Callable[[Any], T]) -> "asyncio.Future[T]":
        fut: asyncio.Future = self._loop.create_future()
        self._queue.put_nowait((fn, fut))
        return fut

    async def _run(self) -> None:
        while True:
            try:
                fn, fut = await asyncio.wait_for(
                    self._queue.get(), timeout=self._store._lease_heartbeat_seconds
                )
            except asyncio.TimeoutError:
                await self._renew_held_leases()
                continue
            try:
                result = await self._writer.execute_async(fn)
            except Exception:  # noqa: BLE001
                try:
                    result = await self._writer.execute_async(fn)
                except Exception as second_exc:  # noqa: BLE001
                    if not fut.done():
                        fut.set_exception(second_exc)
                    log.warning("会话事件写入重试一次后仍失败: %s", second_exc)
                    continue
            if not fut.done():
                fut.set_result(result)

    async def _renew_held_leases(self) -> None:
        """心跳续约：持有者仍是本进程且 fence 未变才续期；被接管则让出。"""
        store = self._store
        held = list(store._held_leases.items())

        def _renew(conn) -> None:
            now = time.time()
            for (owner, session_id), fence in held:
                cursor = conn.execute(
                    "UPDATE writer_leases SET expires_at = ? "
                    "WHERE owner_account_id = ? AND session_id = ? "
                    "AND owner_pid = ? AND fence = ? AND expires_at > ?",
                    (
                        now + store._lease_ttl_seconds,
                        owner,
                        session_id,
                        store._writer_pid,
                        fence,
                        now,
                    ),
                )
                if cursor.rowcount != 1:
                    # 租约已被抢占（fence 变化或过期接管），停止为其心跳
                    store._held_leases.pop((owner, session_id), None)

        try:
            await self._writer.execute_async(_renew)
        except Exception:  # noqa: BLE001
            pass

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._task.cancel()


class SQLiteSessionStore(SessionStore):
    # writer 租约：TTL 30s + 心跳 10s + fence 抢占（ADR-0042）。
    DEFAULT_LEASE_TTL_SECONDS = 30.0
    DEFAULT_LEASE_HEARTBEAT_SECONDS = 10.0

    def __init__(
        self,
        db_path: str = "crew_data/crew.db",
        *,
        wal_enabled: bool = True,
        lease_ttl_seconds: float | None = None,
        lease_heartbeat_seconds: float | None = None,
        read_mode: str = "auto",
    ) -> None:
        if read_mode not in ("auto", "blob"):
            raise ValueError(f"read_mode 只能是 auto/blob: {read_mode!r}")
        self._read_mode = read_mode
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # RLock：rewind/fork 的切口校验在写事务持锁期间递归读取事件链。
        self._lock = threading.RLock()
        self._conn = connect_sqlite(self._path, wal_enabled=wal_enabled)
        self._writer = SQLiteWriteHelper(self._conn, self._lock)
        self._writer.execute(self._init_schema)
        self._writer.execute(self._backfill_legacy_blobs)
        self._projections: dict[tuple[str, str], _SessionProjection] = {}
        self._queues: dict[tuple[str, str], tuple[asyncio.AbstractEventLoop, _SessionWriteQueue]] = {}
        # 进程内区分多个 store 实例（gateway/CLI 同进程双实例也互斥）
        self._writer_pid = f"{os.getpid()}:{uuid.uuid4().hex[:12]}"
        self._lease_ttl_seconds = (
            self.DEFAULT_LEASE_TTL_SECONDS if lease_ttl_seconds is None else lease_ttl_seconds
        )
        self._lease_heartbeat_seconds = (
            self.DEFAULT_LEASE_HEARTBEAT_SECONDS
            if lease_heartbeat_seconds is None
            else lease_heartbeat_seconds
        )
        self._held_leases: dict[tuple[str, str], int] = {}

    def transaction(self, fn: Callable[[Any], T]) -> T:
        """Run related session/workspace writes atomically on this store connection."""
        return self._writer.execute(fn)

    def close(self) -> None:
        """关闭底层 SQLite 连接（WAL 模式下每库持有多个 fd，必须显式释放）。"""
        for _, queue in list(self._queues.values()):
            queue.dispose()
        self._queues.clear()

        def _release(conn) -> None:
            conn.execute(
                "DELETE FROM writer_leases WHERE owner_pid = ?",
                (self._writer_pid,),
            )

        try:
            self._writer.execute(_release)
        except Exception:  # noqa: BLE001
            pass
        self._held_leases.clear()
        with self._lock:
            self._conn.close()

    def _ensure_writer_lease(self, conn, owner: str, session_id: str, now: float) -> int:
        """原子取租/续约，返回 fence。有效租约被其他进程持有时抛 SessionWriteConflict。

        每批写入都经此校验 fence：租约被抢占（fence 变化或过期接管）后，
        旧进程的下一次写入即被拒绝，防脑裂。
        """
        row = conn.execute(
            "SELECT owner_pid, fence, expires_at FROM writer_leases "
            "WHERE owner_account_id = ? AND session_id = ?",
            (owner, session_id),
        ).fetchone()
        expires = now + self._lease_ttl_seconds
        if row is None:
            conn.execute(
                "INSERT INTO writer_leases (owner_account_id, session_id, owner_pid, fence, expires_at) "
                "VALUES (?, ?, ?, 1, ?)",
                (owner, session_id, self._writer_pid, expires),
            )
            self._held_leases[(owner, session_id)] = 1
            return 1
        holder, fence, expires_at = str(row[0]), int(row[1]), float(row[2])
        if holder == self._writer_pid:
            # 续约；自己过期后重新拿起 fence+1，吊销期间可能发生的旧写入
            new_fence = fence if expires_at > now else fence + 1
            conn.execute(
                "UPDATE writer_leases SET fence = ?, expires_at = ? "
                "WHERE owner_account_id = ? AND session_id = ?",
                (new_fence, expires, owner, session_id),
            )
            self._held_leases[(owner, session_id)] = new_fence
            return new_fence
        if expires_at > now:
            raise SessionWriteConflict(
                f"会话 {session_id} 的写者租约被进程 {holder} 持有"
                f"（{expires_at - now:.0f}s 后到期）"
            )
        # 过期接管：条件 UPDATE + rows_affected 原子抢占，fence+1
        cursor = conn.execute(
            "UPDATE writer_leases SET owner_pid = ?, fence = fence + 1, expires_at = ? "
            "WHERE owner_account_id = ? AND session_id = ? AND expires_at <= ?",
            (self._writer_pid, expires, owner, session_id, now),
        )
        if cursor.rowcount != 1:
            raise SessionWriteConflict(f"会话 {session_id} 的写者租约接管失败（并发抢占）")
        self._held_leases[(owner, session_id)] = fence + 1
        return fence + 1

    def _get_write_queue(self, key: tuple[str, str]) -> _SessionWriteQueue:
        """取会话的单写队列；事件循环变化时重建旧队列（旧队列 dispose）。"""
        loop = asyncio.get_running_loop()
        existing = self._queues.get(key)
        if existing is not None and existing[0] is loop:
            return existing[1]
        if existing is not None:
            existing[1].dispose()
        queue = _SessionWriteQueue(self, loop)
        self._queues[key] = (loop, queue)
        return queue

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
                last_prompt_tokens INTEGER,
                last_prompt_tokens_source TEXT,
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
            CREATE TABLE IF NOT EXISTS session_events (
                owner_account_id TEXT NOT NULL DEFAULT '',
                session_id  TEXT NOT NULL,
                seq         INTEGER NOT NULL,
                parent_seq  INTEGER,
                type        TEXT NOT NULL,
                payload     TEXT NOT NULL,
                created_at  REAL NOT NULL,
                PRIMARY KEY (owner_account_id, session_id, seq)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS writer_leases (
                owner_account_id TEXT NOT NULL DEFAULT '',
                session_id  TEXT NOT NULL,
                owner_pid   TEXT NOT NULL,
                fence       INTEGER NOT NULL,
                expires_at  REAL NOT NULL,
                PRIMARY KEY (owner_account_id, session_id)
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
            "last_prompt_tokens": "ALTER TABLE sessions ADD COLUMN last_prompt_tokens INTEGER",
            "last_prompt_tokens_source": "ALTER TABLE sessions ADD COLUMN last_prompt_tokens_source TEXT",
            "last_status": "ALTER TABLE sessions ADD COLUMN last_status TEXT NOT NULL DEFAULT ''",
            "last_error": "ALTER TABLE sessions ADD COLUMN last_error TEXT NOT NULL DEFAULT ''",
            "archived": "ALTER TABLE sessions ADD COLUMN archived INTEGER NOT NULL DEFAULT 0",
            "pinned": "ALTER TABLE sessions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0",
            "leaf_seq": "ALTER TABLE sessions ADD COLUMN leaf_seq INTEGER NOT NULL DEFAULT 0",
            "events_generation": "ALTER TABLE sessions ADD COLUMN events_generation INTEGER NOT NULL DEFAULT 0",
            "source_session_id": "ALTER TABLE sessions ADD COLUMN source_session_id TEXT NOT NULL DEFAULT ''",
            "source_parent_seq": "ALTER TABLE sessions ADD COLUMN source_parent_seq INTEGER",
        }
        for col, ddl in migrations.items():
            if col not in cols:
                conn.execute(ddl)
        event_cols = {r[1] for r in conn.execute("PRAGMA table_info(session_events)").fetchall()}
        if "parent_seq" not in event_cols:
            conn.execute("ALTER TABLE session_events ADD COLUMN parent_seq INTEGER")
        for table in ("session_agent_config",):
            table_cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if "owner_account_id" not in table_cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN owner_account_id TEXT NOT NULL DEFAULT ''")
        self._migrate_owned_aux_tables(conn)
        self._migrate_sessions_pk(conn)
        # 历史 owner='' 行归属本机 local（owner 统一后不存在无主会话）。
        # channel_session_routes 已归位 channels 库（P2-7 + ADR-0038 拆库），
        # 由 channels 侧 store 自管建表与回填；主库若还留着拆库前的旧表，
        # 旧行作为回退备份原样保留，不再被本库触碰。
        backfill_empty_owner_rows(conn, ["sessions", "session_agent_config", "session_events", "writer_leases"])
        stamp_baseline(conn, SESSIONS_SCHEMA_FEATURE, version=SESSIONS_SCHEMA_VERSION)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_owner_updated ON sessions(owner_account_id, updated_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_owner_workspace ON sessions(owner_account_id, workspace_id, updated_at DESC)")

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
                    last_prompt_tokens INTEGER,
                    last_prompt_tokens_source TEXT,
                    last_status   TEXT NOT NULL DEFAULT '',
                    last_error    TEXT NOT NULL DEFAULT '',
                    archived      INTEGER NOT NULL DEFAULT 0,
                    pinned        INTEGER NOT NULL DEFAULT 0,
                    leaf_seq      INTEGER NOT NULL DEFAULT 0,
                    events_generation INTEGER NOT NULL DEFAULT 0,
                    source_session_id TEXT NOT NULL DEFAULT '',
                    source_parent_seq INTEGER,
                    PRIMARY KEY (owner_account_id, session_id)
                )
            """,
            copy_sql="""
                INSERT OR IGNORE INTO sessions_new (
                    session_id, owner_account_id, messages, updated_at, created_at,
                    workspace_id, title, message_count, token_count, last_prompt_tokens, last_prompt_tokens_source, last_status, last_error,
                    archived, pinned, leaf_seq, events_generation, source_session_id, source_parent_seq
                )
                SELECT
                    session_id, owner_account_id, messages, updated_at, created_at,
                    workspace_id, title, message_count, token_count, last_prompt_tokens, last_prompt_tokens_source, last_status, last_error,
                    COALESCE(archived, 0), COALESCE(pinned, 0), COALESCE(leaf_seq, 0), COALESCE(events_generation, 0),
                    COALESCE(source_session_id, ''), source_parent_seq
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

    def _backfill_legacy_blobs(self, conn) -> int:
        """一次性迁移：blob-only 的旧会话逐会话导入为事件行（幂等）。

        已有事件行的会话跳过（重复执行零重复行）；坏 blob 保持原样，
        读取端 blob 回退仍可用。返回本次导入的会话数。
        """
        rows = conn.execute(
            "SELECT owner_account_id, session_id, messages FROM sessions WHERE messages != '[]'"
        ).fetchall()
        now = time.time()
        imported = 0
        for owner, session_id, raw in rows:
            has_events = conn.execute(
                "SELECT 1 FROM session_events WHERE owner_account_id = ? AND session_id = ? LIMIT 1",
                (owner, session_id),
            ).fetchone()
            if has_events is not None:
                continue
            try:
                messages = self._load(str(raw))
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                log.warning(
                    "会话 %s 的 blob 迁移跳过（解析失败）: %s", session_id, exc
                )
                continue
            seq = 1
            for message in messages:
                encoded = self._event_row_for_message(message)
                if encoded is None:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO session_events "
                    "(owner_account_id, session_id, seq, parent_seq, type, payload, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (owner, session_id, seq, seq - 1 if seq > 1 else None, encoded[0], encoded[1], now),
                )
                seq += 1
            conn.execute(
                "UPDATE sessions SET leaf_seq = ? WHERE owner_account_id = ? AND session_id = ?",
                (seq - 1, owner, session_id),
            )
            imported += 1
        return imported

    # ---- 序列化 ----
    @staticmethod
    def _message_to_dict(m: Message) -> dict:
        return asdict(m)

    @classmethod
    def _dump(cls, messages: list[Message]) -> str:
        return json.dumps([cls._message_to_dict(m) for m in messages], ensure_ascii=False)

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
    def _message_from_dict(d: dict) -> Message:
        tcs: list[ToolCall] = []
        for raw_tc in d.get("tool_calls", []):
            tc = dict(raw_tc)
            tc.pop("source", None)  # 兼容 2026-06-21 短暂写入过 source 的历史记录
            tcs.append(ToolCall(**tc))
        return Message(
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
            communication_request_text=d.get("communication_request_text"),
        )

    @classmethod
    def _load(cls, raw: str) -> list[Message]:
        return [cls._message_from_dict(d) for d in json.loads(raw)]

    # ---- 事件投影 ----
    def _get_projection(self, owner: str, session_id: str) -> _SessionProjection:
        key = (owner, session_id)
        proj = self._projections.get(key)
        if proj is None:
            proj = self._build_projection(owner, session_id)
            self._projections[key] = proj
        return proj

    def _read_event_cursor(self, owner: str, session_id: str) -> tuple[int, int] | None:
        """读 (leaf_seq, events_generation) 快照；会话行不存在返回 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT leaf_seq, events_generation FROM sessions "
                "WHERE owner_account_id = ? AND session_id = ?",
                (owner, session_id),
            ).fetchone()
        if row is None:
            return None
        return int(row[0]), int(row[1])

    def _fetch_chain_rows(
        self, owner: str, session_id: str, upto_seq: int
    ) -> dict[int, tuple[int | None, str, str]]:
        """读 seq <= upto_seq 的事件行，返回 {seq: (parent_seq, type, payload)}。"""
        with self._lock:
            return {
                int(row[0]): (row[1], str(row[2]), str(row[3]))
                for row in self._conn.execute(
                    "SELECT seq, parent_seq, type, payload FROM session_events "
                    "WHERE owner_account_id = ? AND session_id = ? AND seq <= ?",
                    (owner, session_id, upto_seq),
                ).fetchall()
            }

    @staticmethod
    def _effective_parent(rows: dict[int, tuple[int | None, str, str]], seq: int) -> int | None:
        """链上父指针：W5 之前的行无 parent_seq，缺省语义为前一条（seq-1）。"""
        parent = rows[seq][0]
        if parent is not None:
            return int(parent)
        return seq - 1 if seq > 1 else None

    @classmethod
    def _ordered_chain(
        cls, rows: dict[int, tuple[int | None, str, str]], leaf_seq: int
    ) -> list[tuple[int, str, str]]:
        """从 leaf 沿父链反向走到链首，再反转为正序 [(seq, type, payload)]。

        父指针断裂（指向不存在的行）或成环即抛 SessionEventLogError——
        事件表只增不改，链上缺口只可能是数据损坏，fail-closed。
        """
        chain: list[tuple[int, str, str]] = []
        seen: set[int] = set()
        cur: int | None = leaf_seq
        while cur is not None:
            if cur in seen or cur not in rows:
                raise SessionEventLogError(
                    f"事件链断裂：seq={cur} 缺失或成环（leaf={leaf_seq}），拒绝加载"
                )
            seen.add(cur)
            _parent, etype, payload = rows[cur]
            chain.append((cur, etype, payload))
            cur = cls._effective_parent(rows, cur)
        chain.reverse()
        return chain

    def _resolve_chain(
        self,
        owner: str,
        session_id: str,
        upto_seq: int,
        _seen: frozenset[tuple[str, str]] = frozenset(),
    ) -> list[tuple[str, int, str, str]]:
        """正序解析投影链（含 fork 前缀拼接）：[(session_id, seq, type, payload)]。

        遇到 end_seed 事件时按其载荷内联的 (source_session_id, parent_seq)
        递归拼接源会话前缀——fork 在行模型下 O(1) 共享前缀、不复制行。
        """
        key = (owner, session_id)
        if key in _seen:
            raise SessionEventLogError(f"fork 前缀成环：{session_id}，拒绝加载")
        seen = _seen | {key}
        chain = self._ordered_chain(self._fetch_chain_rows(owner, session_id, upto_seq), upto_seq)
        resolved: list[tuple[str, int, str, str]] = []
        for seq, etype, payload in chain:
            if etype != SessionEventType.END_SEED.value:
                resolved.append((session_id, seq, etype, payload))
                continue
            try:
                meta = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise SessionEventLogError(f"end_seed 载荷损坏（seq={seq}），拒绝加载") from exc
            source_id = meta.get("source_session_id")
            source_seq = meta.get("source_parent_seq")
            if not isinstance(source_id, str) or not source_id or not isinstance(source_seq, int):
                raise SessionEventLogError(f"end_seed 载荷缺前缀指针（seq={seq}），拒绝加载")
            resolved.extend(self._resolve_chain(owner, source_id, source_seq, seen))
        return resolved

    @staticmethod
    def _apply_event_rows(proj: _SessionProjection, rows: list[tuple[str, int, str, str]]) -> None:
        for _sid, seq, etype, payload in rows:
            try:
                event_type = SessionEventType(etype)
            except ValueError as exc:
                raise SessionEventLogError(f"未知事件类型 {etype!r}（seq={seq}），拒绝加载") from exc
            if event_type in _ROLE_EVENT_TYPES.values():
                proj.messages.append(SQLiteSessionStore._message_from_dict(json.loads(payload)))

    def _build_projection(self, owner: str, session_id: str) -> _SessionProjection:
        """全量构建投影：事件链优先，无事件的旧会话回退读 blob。"""
        proj = _SessionProjection()
        cursor = self._read_event_cursor(owner, session_id)
        if cursor is None:
            return proj
        leaf, generation = cursor
        proj.generation = generation
        proj.seq = leaf
        rows = self._fetch_chain_rows(owner, session_id, leaf)
        if rows:
            self._apply_event_rows(proj, self._resolve_chain(owner, session_id, leaf))
            return proj
        with self._lock:
            row = self._conn.execute(
                "SELECT messages FROM sessions WHERE owner_account_id = ? AND session_id = ?",
                (owner, session_id),
            ).fetchone()
        if row is not None:
            proj.messages = self._load(row[0])
        return proj

    def _catch_up_projection(self, owner: str, session_id: str) -> _SessionProjection:
        """按 (leaf_seq, events_generation) 快照增量追赶：只解析链上新增事件。

        代际变化（跨进程整体重写）→ 全量重建；leaf 回退（本进程或跨进程
        rewind）→ 全量重建；链上新增 → 沿父链取 proj.seq 之后的后缀增量应用。
        链断裂或未知类型报错后全量重建重试一次，仍失败则 fail-closed 抛错。
        投影允许滞后、禁止超前。
        """
        key = (owner, session_id)
        proj = self._projections.get(key)
        cursor = self._read_event_cursor(owner, session_id)
        if cursor is None:
            # 会话行已不存在（被清理/过期）：缓存一并丢弃
            self._projections.pop(key, None)
            return _SessionProjection()
        leaf, generation = cursor
        if proj is None or proj.generation != generation or leaf < proj.seq:
            proj = self._build_projection(owner, session_id)
            self._projections[key] = proj
            return proj
        if leaf == proj.seq:
            return proj
        try:
            rows = self._fetch_chain_rows(owner, session_id, leaf)
            suffix: list[tuple[int, str, str]] = []
            cur: int | None = leaf
            while cur is not None and cur != proj.seq:
                if cur not in rows:
                    raise SessionEventLogError(
                        f"事件链断裂：seq={cur} 缺失（leaf={leaf}），拒绝加载"
                    )
                suffix.append((cur, rows[cur][1], rows[cur][2]))
                cur = self._effective_parent(rows, cur)
            if cur != proj.seq:
                raise SessionEventLogError("事件链与投影游标对账失败，拒绝加载")
            suffix.reverse()
            self._apply_event_rows(
                proj, [(session_id, seq, etype, payload) for seq, etype, payload in suffix]
            )
            proj.seq = leaf
            return proj
        except SessionEventLogError:
            # 缺口/坏行：全量重建重试一次，不可静默跳过
            rebuilt = self._build_projection(owner, session_id)
            self._projections[key] = rebuilt
            return rebuilt

    @staticmethod
    def _event_row_for_message(message: Message) -> tuple[str, str] | None:
        event_type = _ROLE_EVENT_TYPES.get(message.role)
        if event_type is None or not event_type.durable:
            return None
        return event_type.value, json.dumps(
            SQLiteSessionStore._message_to_dict(message), ensure_ascii=False
        )

    # ---- SessionStore 接口 ----
    def load(self, session_id: str, owner_account_id: str) -> list[Message]:
        if self._read_mode == "blob":
            # 双格式窗口的紧急回退：强制旧 blob 读取（save 仍双写，blob 保持最新）
            with self._lock:
                row = self._conn.execute(
                    "SELECT messages FROM sessions WHERE session_id = ? AND owner_account_id = ?",
                    (session_id, owner_account_id),
                ).fetchone()
            return self._load(row[0]) if row else []
        return list(self._catch_up_projection(owner_account_id, session_id).messages)

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
                SELECT session_id
                FROM sessions
                WHERE session_id LIKE ? AND owner_account_id = ?
                ORDER BY created_at ASC, updated_at ASC, session_id ASC
                """,
                (prefix, owner_account_id),
            ).fetchall()
        return [
            (
                str(row[0]),
                list(self._catch_up_projection(owner_account_id, str(row[0])).messages),
            )
            for row in rows
        ]

    def _save_write(
        self,
        session_id: str,
        messages: list[Message],
        workspace_id: str,
        *,
        owner_account_id: str,
        title_fallback: str | None,
        last_prompt_tokens: int | None,
        last_prompt_tokens_source: str | None,
    ) -> Callable[[Any], tuple[int, int]]:
        now = time.time()
        # title_fallback=None 保持旧行为（首条 user 消息截断），兼容未传该参数的调用方；
        # title_fallback="" 显式留空占位，等 set_title 写入摘要标题（enable_title=True 时
        # 用，避免截断的用户原话抢占即将生成的摘要标题）。
        fallback_title = (
            title_fallback if title_fallback is not None else self._first_user_title(messages)
        )
        # 增量 diff：投影前缀与传入消息一致 → 只把新增尾部转事件（O(新事件)）；
        # 不一致（调用方整体改写历史）→ 重写该会话的全部事件行。
        proj = self._get_projection(owner_account_id, session_id)
        prefix = len(proj.messages)
        is_append = messages[:prefix] == proj.messages
        tail = messages[prefix:] if is_append else messages
        event_rows: list[tuple[str, str]] = []
        for message in tail:
            encoded = self._event_row_for_message(message)
            if encoded is not None:
                event_rows.append(encoded)
        checkpoint_payload: str | None = None
        if last_prompt_tokens is not None:
            checkpoint_payload = json.dumps(
                {
                    "prompt_tokens": last_prompt_tokens,
                    "source": last_prompt_tokens_source,
                    "recorded_at": now,
                },
                ensure_ascii=False,
            )

        def _write(conn) -> tuple[int, int, int]:
            self._ensure_writer_lease(conn, owner_account_id, session_id, now)
            base_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0), "
                "(SELECT events_generation FROM sessions "
                "WHERE owner_account_id = ? AND session_id = ?), "
                "(SELECT leaf_seq FROM sessions "
                "WHERE owner_account_id = ? AND session_id = ?) "
                "FROM session_events WHERE owner_account_id = ? AND session_id = ?",
                (
                    owner_account_id, session_id,
                    owner_account_id, session_id,
                    owner_account_id, session_id,
                ),
            ).fetchone()
            base = int(base_row[0])
            current_generation = int(base_row[1] or 0)
            current_leaf = int(base_row[2] or 0)
            new_generation = current_generation + (0 if is_append else 1)
            if is_append:
                seq = base + 1
                # 链锚 = 当前 leaf：rewind 之后的新事件挂在回退点上形成新分支，
                # seq 取 MAX+1 保证旧分支行（seq > leaf）不被覆盖。
                parent: int | None = current_leaf or None
            else:
                conn.execute(
                    "DELETE FROM session_events WHERE owner_account_id = ? AND session_id = ?",
                    (owner_account_id, session_id),
                )
                seq = 1
                parent = None

            def _insert(etype: str, payload: str) -> None:
                nonlocal seq, parent
                # (owner, session, seq) 主键唯一约束兜底去重
                conn.execute(
                    "INSERT OR IGNORE INTO session_events "
                    "(owner_account_id, session_id, seq, parent_seq, type, payload, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (owner_account_id, session_id, seq, parent, etype, payload, now),
                )
                parent = seq
                seq += 1

            for etype, payload in event_rows:
                _insert(etype, payload)
            if checkpoint_payload is not None:
                _insert(SessionEventType.METER_CHECKPOINT.value, checkpoint_payload)
            new_leaf = seq - 1
            conn.execute(
                "INSERT INTO sessions "
                "(session_id, owner_account_id, messages, updated_at, created_at, workspace_id, title, message_count, token_count, last_prompt_tokens, last_prompt_tokens_source, leaf_seq, events_generation) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(owner_account_id, session_id) DO UPDATE SET "
                "  messages = excluded.messages, "
                "  updated_at = excluded.updated_at, "
                # workspace_id 刻意不在 UPDATE 里回写：会话归属在首次创建（INSERT 或
                # ensure_session）时确定，每轮回写会用 envelope.workspace_id 覆盖掉已确定的
                # 归属，导致「test 工作空间会话刷新后漂到 default」。
                "  message_count = excluded.message_count, "
                "  token_count = excluded.token_count, "
                "  last_prompt_tokens = COALESCE(excluded.last_prompt_tokens, sessions.last_prompt_tokens), "
                "  last_prompt_tokens_source = CASE WHEN excluded.last_prompt_tokens IS NOT NULL "
                "THEN excluded.last_prompt_tokens_source ELSE sessions.last_prompt_tokens_source END, "
                "  leaf_seq = excluded.leaf_seq, "
                "  events_generation = excluded.events_generation, "
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
                    last_prompt_tokens,
                    last_prompt_tokens_source,
                    new_leaf,
                    new_generation,
                ),
            )
            return new_leaf, len(event_rows), new_generation

        return _write

    def _apply_saved_projection(
        self,
        owner_account_id: str,
        session_id: str,
        messages: list[Message],
        new_leaf: int,
        new_generation: int,
    ) -> None:
        # 写序不变量：先持久化（事务已提交）→ 再改内存投影。
        proj = self._get_projection(owner_account_id, session_id)
        proj.messages = list(messages)
        proj.seq = new_leaf
        proj.generation = new_generation

    def save(
        self,
        session_id: str,
        messages: list[Message],
        workspace_id: str = "default",
        *,
        owner_account_id: str,
        title_fallback: str | None = None,
        last_prompt_tokens: int | None = None,
        last_prompt_tokens_source: str | None = None,
    ) -> None:
        new_leaf, _, new_generation = self._writer.execute(
            self._save_write(
                session_id,
                messages,
                workspace_id,
                owner_account_id=owner_account_id,
                title_fallback=title_fallback,
                last_prompt_tokens=last_prompt_tokens,
                last_prompt_tokens_source=last_prompt_tokens_source,
            )
        )
        self._apply_saved_projection(owner_account_id, session_id, messages, new_leaf, new_generation)

    async def save_async(
        self,
        session_id: str,
        messages: list[Message],
        workspace_id: str = "default",
        *,
        owner_account_id: str,
        title_fallback: str | None = None,
        last_prompt_tokens: int | None = None,
        last_prompt_tokens_source: str | None = None,
    ) -> None:
        queue = self._get_write_queue((owner_account_id, session_id))
        # flush barrier：leaf 与事件批次同事务提交、ack 往返完成后才视为持久化。
        async with queue.lock:
            fn = await asyncio.to_thread(
                self._save_write,
                session_id,
                messages,
                workspace_id,
                owner_account_id=owner_account_id,
                title_fallback=title_fallback,
                last_prompt_tokens=last_prompt_tokens,
                last_prompt_tokens_source=last_prompt_tokens_source,
            )
            new_leaf, _, new_generation = await queue.enqueue(fn)
        self._apply_saved_projection(
            owner_account_id, session_id, messages, new_leaf, new_generation
        )

    async def load_async(self, session_id: str, *, owner_account_id: str) -> list[Message]:
        return await asyncio.to_thread(self.load, session_id, owner_account_id=owner_account_id)

    def clear_prompt_usage(self, session_id: str, owner_account_id: str) -> None:
        """清除上一轮 Provider usage，避免新回合暂未返回 usage 时显示旧值。"""
        def _write(conn):
            conn.execute(
                "UPDATE sessions SET last_prompt_tokens = NULL, last_prompt_tokens_source = NULL "
                "WHERE session_id = ? AND owner_account_id = ?",
                (session_id, owner_account_id),
            )

        self._writer.execute(_write)

    def load_meter_checkpoint(
        self, session_id: str, owner_account_id: str
    ) -> dict[str, Any] | None:
        """最新 meter_checkpoint 事件 payload（TokenMeter 跨重启重锚定用）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM session_events "
                "WHERE owner_account_id = ? AND session_id = ? AND type = ? "
                "ORDER BY seq DESC LIMIT 1",
                (owner_account_id, session_id, SessionEventType.METER_CHECKPOINT.value),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row[0])
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    # ---- compaction 自包含 checkpoint（codex 对照修订第 3 条） ----
    def record_compaction_checkpoint(
        self,
        session_id: str,
        *,
        owner_account_id: str,
        summary: str,
        covered_count: int,
        view_estimate: int,
    ) -> None:
        """压缩落库时内联一条自包含 compaction 事件：replacement 摘要 + 重锚定
        token 估算。事件表只增不改，旧 checkpoint 永不覆盖；恢复 = 定位最近
        compaction 事件后正序重放（load_compaction_checkpoint）。"""
        now = time.time()
        payload = json.dumps(
            {
                "summary": summary,
                "covered_count": int(covered_count),
                "view_estimate": int(view_estimate),
                "recorded_at": now,
            },
            ensure_ascii=False,
        )

        def _write(conn) -> None:
            self._ensure_writer_lease(conn, owner_account_id, session_id, now)
            base_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0), "
                "(SELECT leaf_seq FROM sessions "
                "WHERE owner_account_id = ? AND session_id = ?) "
                "FROM session_events WHERE owner_account_id = ? AND session_id = ?",
                (owner_account_id, session_id, owner_account_id, session_id),
            ).fetchone()
            base = int(base_row[0])
            current_leaf = int(base_row[1] or 0)
            conn.execute(
                "INSERT OR IGNORE INTO session_events "
                "(owner_account_id, session_id, seq, parent_seq, type, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    owner_account_id,
                    session_id,
                    base + 1,
                    current_leaf or None,
                    SessionEventType.COMPACTION.value,
                    payload,
                    now,
                ),
            )
            conn.execute(
                "UPDATE sessions SET leaf_seq = ? WHERE owner_account_id = ? AND session_id = ?",
                (base + 1, owner_account_id, session_id),
            )

        self._writer.execute(_write)

    def load_compaction_checkpoint(
        self, session_id: str, owner_account_id: str
    ) -> dict[str, Any] | None:
        """最近一条 compaction 事件 payload（摘要状态跨重启恢复 + 计量重锚定）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM session_events "
                "WHERE owner_account_id = ? AND session_id = ? AND type = ? "
                "ORDER BY seq DESC LIMIT 1",
                (owner_account_id, session_id, SessionEventType.COMPACTION.value),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row[0])
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def record_meter_checkpoint(
        self,
        session_id: str,
        *,
        owner_account_id: str,
        prompt_tokens: int,
        source: str = "provider",
        fingerprint: str = "",
        baseline_estimate: int | None = None,
    ) -> None:
        """追加带完整请求信封信息的 meter_checkpoint 事件（增量计量重锚定）。"""
        now = time.time()
        payload = json.dumps(
            {
                "prompt_tokens": int(prompt_tokens),
                "source": source,
                "fingerprint": fingerprint,
                "baseline_estimate": (
                    int(prompt_tokens) if baseline_estimate is None else int(baseline_estimate)
                ),
                "recorded_at": now,
            },
            ensure_ascii=False,
        )

        def _write(conn):
            self._ensure_writer_lease(conn, owner_account_id, session_id, now)
            base_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0), "
                "(SELECT leaf_seq FROM sessions "
                "WHERE owner_account_id = ? AND session_id = ?) "
                "FROM session_events WHERE owner_account_id = ? AND session_id = ?",
                (owner_account_id, session_id, owner_account_id, session_id),
            ).fetchone()
            base = int(base_row[0])
            current_leaf = int(base_row[1] or 0)
            conn.execute(
                "INSERT OR IGNORE INTO session_events "
                "(owner_account_id, session_id, seq, parent_seq, type, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    owner_account_id,
                    session_id,
                    base + 1,
                    current_leaf or None,
                    SessionEventType.METER_CHECKPOINT.value,
                    payload,
                    now,
                ),
            )
            conn.execute(
                "UPDATE sessions SET leaf_seq = ? WHERE owner_account_id = ? AND session_id = ?",
                (base + 1, owner_account_id, session_id),
            )

        self._writer.execute(_write)

    # ---- 会话树：rewind / fork / 分支枚举（ADR-0042 W5，D2/D3） ----
    def _chain_messages_upto(
        self, owner: str, session_id: str, upto_seq: int
    ) -> list[Message]:
        """解析链上 seq <= upto_seq 的消息（正序），供切口校验与 blob 回写。"""
        resolved = self._resolve_chain(owner, session_id, upto_seq)
        role_types = {e.value for e in _ROLE_EVENT_TYPES.values()}
        return [
            self._message_from_dict(json.loads(payload))
            for _sid, _seq, etype, payload in resolved
            if etype in role_types
        ]

    def _validate_cut(self, owner: str, session_id: str, target_seq: int) -> None:
        """校验 rewind/fork 切口合法：行存在、不在开放回合中、工具配对平衡。"""
        if target_seq < 1:
            raise ValueError(f"切口 seq 必须 >= 1: {target_seq}")
        rows = self._fetch_chain_rows(owner, session_id, target_seq)
        if target_seq not in rows:
            raise ValueError(f"切口 seq={target_seq} 不存在于会话链上")
        chain = self._ordered_chain(rows, target_seq)
        last_turn: str | None = None
        balance = 0
        for _seq, etype, payload in chain:
            if etype in (SessionEventType.TURN_START.value, SessionEventType.TURN_END.value):
                last_turn = etype
            elif etype == SessionEventType.ASSISTANT_MESSAGE.value:
                balance += len(json.loads(payload).get("tool_calls") or [])
            elif etype == SessionEventType.TOOL_RESULT.value:
                balance -= 1
        if last_turn == SessionEventType.TURN_START.value:
            raise ValueError(f"切口 seq={target_seq} 落在开放回合中（turn_start 未闭合）")
        if balance != 0:
            raise ValueError(f"切口 seq={target_seq} 落在未闭合的工具调用回合中")

    def rewind(
        self,
        session_id: str,
        target_seq: int,
        *,
        owner_account_id: str,
    ) -> None:
        """回退：leaf 指针 CAS 移动到 target_seq，旧分支行原样保留可导航回来。

        已存在的事件行一律不改写；blob 列随切口回写以维持双格式窗口一致。
        校验在写事务外完成（事件只增不改，切口合法性不会被并发追加推翻），
        事务内只做 leaf CAS：并发移动（leaf 与预期不符）抛 SessionWriteConflict。
        """
        key = (owner_account_id, session_id)
        now = time.time()
        cursor = self._read_event_cursor(owner_account_id, session_id)
        if cursor is None:
            raise ValueError(f"会话不存在: {session_id}")
        expected_leaf = cursor[0]
        self._validate_cut(owner_account_id, session_id, target_seq)
        messages = self._chain_messages_upto(owner_account_id, session_id, target_seq)

        def _write(conn) -> None:
            self._ensure_writer_lease(conn, owner_account_id, session_id, now)
            cursor = conn.execute(
                "UPDATE sessions SET leaf_seq = ?, messages = ?, message_count = ?, "
                "token_count = ?, updated_at = ? "
                "WHERE owner_account_id = ? AND session_id = ? AND leaf_seq = ?",
                (
                    target_seq,
                    self._dump(messages),
                    len(messages),
                    self._estimate_tokens(messages),
                    now,
                    owner_account_id,
                    session_id,
                    expected_leaf,
                ),
            )
            if cursor.rowcount != 1:
                raise SessionWriteConflict(
                    f"会话 {session_id} 的 leaf 在校验后被并发移动（预期 {expected_leaf}），请重试"
                )

        self._writer.execute(_write)
        self._projections.pop(key, None)

    def fork(
        self,
        session_id: str,
        boundary_seq: int,
        *,
        owner_account_id: str,
        new_session_id: str | None = None,
        title: str | None = None,
    ) -> str:
        """分叉：新会话行 + end_seed 切口事件，前缀经 (source_session_id, parent_seq)
        二元组共享——不复制源会话任何事件行（O(1)）。"""
        new_id = new_session_id or uuid.uuid4().hex
        key = (owner_account_id, new_id)
        now = time.time()
        self._validate_cut(owner_account_id, session_id, boundary_seq)
        prefix = self._chain_messages_upto(owner_account_id, session_id, boundary_seq)

        def _write(conn) -> str:
            self._ensure_writer_lease(conn, owner_account_id, new_id, now)
            src = conn.execute(
                "SELECT workspace_id, title FROM sessions "
                "WHERE owner_account_id = ? AND session_id = ?",
                (owner_account_id, session_id),
            ).fetchone()
            if src is None:
                raise ValueError(f"会话不存在: {session_id}")
            fork_title = title if title is not None else (str(src[1] or "") + " · 分支")
            seed_payload = json.dumps(
                {
                    "source_session_id": session_id,
                    "source_parent_seq": boundary_seq,
                    "created_at": now,
                },
                ensure_ascii=False,
            )
            conn.execute(
                "INSERT INTO sessions "
                "(session_id, owner_account_id, messages, updated_at, created_at, workspace_id, "
                "title, message_count, token_count, leaf_seq, events_generation, "
                "source_session_id, source_parent_seq) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?)",
                (
                    new_id,
                    owner_account_id,
                    self._dump(prefix),
                    now,
                    now,
                    str(src[0] or "default"),
                    fork_title,
                    len(prefix),
                    self._estimate_tokens(prefix),
                    session_id,
                    boundary_seq,
                ),
            )
            conn.execute(
                "INSERT INTO session_events "
                "(owner_account_id, session_id, seq, parent_seq, type, payload, created_at) "
                "VALUES (?, ?, 1, NULL, ?, ?, ?)",
                (owner_account_id, new_id, SessionEventType.END_SEED.value, seed_payload, now),
            )
            return new_id

        self._writer.execute(_write)
        self._projections.pop(key, None)
        return new_id

    def list_branches(self, session_id: str, *, owner_account_id: str) -> list[dict[str, Any]]:
        """枚举会话的分支：fork 子会话 + rewind 后留在 leaf 之外的旧分支尾巴。"""
        branches: list[dict[str, Any]] = []
        with self._lock:
            fork_rows = self._conn.execute(
                "SELECT session_id, source_parent_seq, leaf_seq, title, created_at "
                "FROM sessions WHERE owner_account_id = ? AND source_session_id = ?",
                (owner_account_id, session_id),
            ).fetchall()
        for sid, parent_seq, leaf, title, created_at in fork_rows:
            branches.append(
                {
                    "kind": "fork",
                    "session_id": str(sid),
                    "parent_seq": int(parent_seq),
                    "tip_seq": int(leaf),
                    "title": str(title or ""),
                    "created_at": float(created_at or 0),
                }
            )
        with self._lock:
            cursor = self._conn.execute(
                "SELECT leaf_seq FROM sessions WHERE owner_account_id = ? AND session_id = ?",
                (owner_account_id, session_id),
            ).fetchone()
            if cursor is None:
                return branches
            leaf = int(cursor[0])
            all_rows = [
                (int(r[0]), r[1])
                for r in self._conn.execute(
                    "SELECT seq, parent_seq FROM session_events "
                    "WHERE owner_account_id = ? AND session_id = ?",
                    (owner_account_id, session_id),
                ).fetchall()
            ]
        if not all_rows:
            return branches
        by_seq = {seq: parent for seq, parent in all_rows}

        # 当前链上的行集合：从 leaf 沿父链走到底；其余即被回退遗弃的分支尾巴
        chained: set[int] = set()
        cur: int | None = leaf
        while cur is not None and cur in by_seq and cur not in chained:
            chained.add(cur)
            parent = by_seq[cur]
            cur = parent if parent is not None else cur - 1

        def _entry_of(seq: int) -> int:
            cur = seq
            while cur in by_seq and cur not in chained:
                parent = by_seq[cur]
                eff = parent if parent is not None else cur - 1
                if eff is None or eff in chained:
                    return cur
                cur = eff
            return seq

        groups: dict[int, list[int]] = {}
        for seq, _parent in all_rows:
            if seq in chained:
                continue
            groups.setdefault(_entry_of(seq), []).append(seq)
        for entry, members in groups.items():
            cut = by_seq[entry]
            while cut is not None and cut not in chained:
                parent = by_seq.get(cut)
                cut = parent if parent is not None else cut - 1
            branches.append(
                {
                    "kind": "tail",
                    "cut_seq": int(cut) if cut is not None else 0,
                    "tip_seq": max(members),
                    "event_count": len(members),
                }
            )
        branches.sort(key=lambda b: (b["kind"], b.get("parent_seq", b.get("cut_seq", 0))))
        return branches

    def record_turn_event(
        self,
        session_id: str,
        *,
        owner_account_id: str,
        kind: SessionEventType,
        status: str = "",
    ) -> None:
        """追加回合边界事件（turn_start/turn_end，D3/D4 断点扫描的判据）。"""
        if kind not in (SessionEventType.TURN_START, SessionEventType.TURN_END):
            raise ValueError(f"回合事件类型只能是 turn_start/turn_end: {kind}")
        now = time.time()
        payload = json.dumps(
            {"status": status, "recorded_at": now} if status else {"recorded_at": now},
            ensure_ascii=False,
        )

        def _write(conn) -> None:
            self._ensure_writer_lease(conn, owner_account_id, session_id, now)
            base_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0), "
                "(SELECT leaf_seq FROM sessions "
                "WHERE owner_account_id = ? AND session_id = ?) "
                "FROM session_events WHERE owner_account_id = ? AND session_id = ?",
                (owner_account_id, session_id, owner_account_id, session_id),
            ).fetchone()
            base = int(base_row[0])
            current_leaf = int(base_row[1] or 0)
            conn.execute(
                "INSERT OR IGNORE INTO session_events "
                "(owner_account_id, session_id, seq, parent_seq, type, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (owner_account_id, session_id, base + 1, current_leaf or None, kind.value, payload, now),
            )
            conn.execute(
                "UPDATE sessions SET leaf_seq = ? WHERE owner_account_id = ? AND session_id = ?",
                (base + 1, owner_account_id, session_id),
            )

        self._writer.execute(_write)

    def close_open_turn(
        self,
        session_id: str,
        *,
        owner_account_id: str,
        status: str = "interrupted",
    ) -> bool:
        """若会话链上最后一个回合边界是 turn_start，追加 turn_end 闭合它。

        崩溃恢复（D3）在冷读配平后用 interrupted 状态闭合悬挂回合；
        返回是否实际闭合了一个开放回合。
        """
        leaf = self._read_event_cursor(owner_account_id, session_id)
        if leaf is None:
            return False
        last_turn = self._last_turn_event(owner_account_id, session_id, leaf[0])
        if last_turn != SessionEventType.TURN_START.value:
            return False
        self.record_turn_event(
            session_id,
            owner_account_id=owner_account_id,
            kind=SessionEventType.TURN_END,
            status=status,
        )
        return True

    def _last_turn_event(self, owner: str, session_id: str, upto_seq: int) -> str | None:
        """链上 seq <= upto_seq 的最后一个回合边界事件类型。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT type FROM session_events "
                "WHERE owner_account_id = ? AND session_id = ? AND seq <= ? "
                "AND type IN (?, ?) ORDER BY seq DESC LIMIT 1",
                (
                    owner, session_id, upto_seq,
                    SessionEventType.TURN_START.value, SessionEventType.TURN_END.value,
                ),
            ).fetchone()
        return str(row[0]) if row is not None else None

    def scan_breakpoints(self, owner_account_id: str) -> list[dict[str, Any]]:
        """扫描各会话开放回合（turn_start 无 turn_end），产出断点报告（D4）。

        「上次会话在第 N 步被中断」：N = 开放回合内已落盘的消息事件数。
        只报告、不自动续跑；Team 子会话（'::'）与无事件会话跳过。
        """
        role_types = tuple(e.value for e in _ROLE_EVENT_TYPES.values())
        marks = ",".join("?" for _ in role_types)
        reports: list[dict[str, Any]] = []
        with self._lock:
            sessions = self._conn.execute(
                "SELECT session_id, title, leaf_seq FROM sessions "
                "WHERE owner_account_id = ? AND session_id NOT LIKE '%::%' AND leaf_seq > 0",
                (owner_account_id,),
            ).fetchall()
        for sid, title, leaf in sessions:
            sid = str(sid)
            leaf = int(leaf)
            if self._last_turn_event(owner_account_id, sid, leaf) != SessionEventType.TURN_START.value:
                continue
            with self._lock:
                turn_row = self._conn.execute(
                    "SELECT MAX(seq) FROM session_events "
                    "WHERE owner_account_id = ? AND session_id = ? AND seq <= ? AND type = ?",
                    (owner_account_id, sid, leaf, SessionEventType.TURN_START.value),
                ).fetchone()
                step_row = self._conn.execute(
                    f"SELECT COUNT(*) FROM session_events "
                    f"WHERE owner_account_id = ? AND session_id = ? AND seq > ? AND seq <= ? "
                    f"AND type IN ({marks})",
                    (owner_account_id, sid, int(turn_row[0]), leaf, *role_types),
                ).fetchone()
            reports.append(
                {
                    "session_id": sid,
                    "title": str(title or ""),
                    "turn_start_seq": int(turn_row[0]),
                    "last_event_seq": leaf,
                    "step": int(step_row[0]),
                }
            )
        return reports

    def ensure_session(
        self,
        session_id: str,
        workspace_id: str = "default",
        title: str = "",
        *,
        owner_account_id: str,
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

    def append(self, session_id: str, messages: list[Message], owner_account_id: str) -> None:
        existing = self.load(session_id, owner_account_id=owner_account_id)
        existing.extend(messages)
        self.save(session_id, existing, owner_account_id=owner_account_id)

    def clear(self, session_id: str, owner_account_id: str) -> None:
        def _write(conn):
            conn.execute(
                "DELETE FROM session_events WHERE session_id = ? AND owner_account_id = ?",
                (session_id, owner_account_id),
            )
            conn.execute(
                "DELETE FROM writer_leases WHERE session_id = ? AND owner_account_id = ?",
                (session_id, owner_account_id),
            )
            conn.execute(
                "DELETE FROM sessions WHERE session_id = ? AND owner_account_id = ?",
                (session_id, owner_account_id),
            )
            conn.execute(
                "DELETE FROM session_agent_config WHERE session_id = ? AND owner_account_id = ?",
                (session_id, owner_account_id),
            )

        self._writer.execute(_write)
        self._projections.pop((owner_account_id, session_id), None)

    @staticmethod
    def _delete_session_rows(conn, session_id: str, owner_account_id: str) -> None:
        conn.execute(
            "DELETE FROM session_events WHERE session_id = ? AND owner_account_id = ?",
            (session_id, owner_account_id),
        )
        conn.execute(
            "DELETE FROM writer_leases WHERE session_id = ? AND owner_account_id = ?",
            (session_id, owner_account_id),
        )
        conn.execute(
            "DELETE FROM sessions WHERE session_id = ? AND owner_account_id = ?",
            (session_id, owner_account_id),
        )
        conn.execute(
            "DELETE FROM session_agent_config WHERE session_id = ? AND owner_account_id = ?",
            (session_id, owner_account_id),
        )

    def delete_sessions_for_workspace(
        self,
        workspace_id: str,
        owner_account_id: str,
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
                self._delete_session_rows(writer, sid, owner_account_id)
            return ids
        rows = self.list_sessions(workspace_id, owner_account_id=owner_account_id)
        ids = [str(r["session_id"]) for r in rows]

        def _write(conn):
            for sid in ids:
                self._delete_session_rows(conn, sid, owner_account_id)

        self._writer.execute(_write)
        for sid in ids:
            self._projections.pop((owner_account_id, sid), None)
        return ids

    def set_title(self, session_id: str, title: str, owner_account_id: str) -> None:
        def _write(conn):
            conn.execute(
                "UPDATE sessions SET title = ? WHERE session_id = ? AND owner_account_id = ?",
                (title, session_id, owner_account_id),
            )
        self._writer.execute(_write)

    def set_archived(self, session_id: str, archived: bool, owner_account_id: str) -> None:
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

    def set_pinned(self, session_id: str, pinned: bool, owner_account_id: str) -> None:
        """置顶 / 取消置顶会话。置顶会话在主列表排在最前（pinned DESC, updated_at DESC）。"""
        pinned_int = 1 if pinned else 0

        def _write(conn):
            conn.execute(
                "UPDATE sessions SET pinned = ? WHERE session_id = ? AND owner_account_id = ?",
                (pinned_int, session_id, owner_account_id),
            )
        self._writer.execute(_write)

    def set_status(self, session_id: str, status: str, error: str = "", *, owner_account_id: str) -> None:
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

    def touch_session(self, session_id: str, owner_account_id: str) -> None:
        """刷新会话 updated_at，用于长任务运行期间保活。"""
        now = time.time()

        def _write(conn):
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ? AND owner_account_id = ?",
                (now, session_id, owner_account_id),
            )
        self._writer.execute(_write)

    def get_status(self, session_id: str, owner_account_id: str) -> tuple[str, str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_status, last_error FROM sessions WHERE session_id = ? AND owner_account_id = ?",
                (session_id, owner_account_id),
            ).fetchone()
        return (row[0], row[1]) if row else ("", "")

    def get_workspace_id(self, session_id: str, owner_account_id: str) -> str | None:
        """读取会话所属 workspace_id，不存在时返回 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT workspace_id FROM sessions WHERE session_id = ? AND owner_account_id = ?",
                (session_id, owner_account_id),
            ).fetchone()
        return str(row[0]) if row else None

    def resolve_owner_account_id(self, session_id: str) -> str:
        """仅有 session_id 时反查所属 owner（通知来源回调等场景），取最近更新的一行。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT owner_account_id FROM sessions WHERE session_id = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return str(row[0]) if row else ""

    def total_usage(self, owner_account_id: str) -> dict[str, int]:
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
        owner_account_id: str,
    ) -> dict[str, float | int | str | None]:
        """返回 Provider 最近一次真实 prompt 用量；没有真实 usage 时明确不可用。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT messages, last_prompt_tokens, last_prompt_tokens_source FROM sessions "
                "WHERE session_id = ? AND owner_account_id = ?",
                (session_id, owner_account_id),
            ).fetchone()
        if row is None:
            return {
                "available": False,
                "used_tokens": None,
                "max_tokens": int(context_window or 128000),
                "ratio": None,
                "source": "unavailable",
                "warning": "会话不存在或尚未产生 Provider usage",
            }
        max_tokens = int(context_window or 128000)
        actual = int(row[1]) if row[1] is not None else None
        if actual is None:
            return {
                "available": False,
                "used_tokens": None,
                "max_tokens": max_tokens,
                "ratio": None,
                "source": "unavailable",
                "warning": "尚未计算本次实际请求视图的上下文用量",
            }
        source = str(row[2] or "provider")
        ratio = round(actual / max_tokens, 4) if max_tokens > 0 else None
        return {
            "available": True,
            "used_tokens": actual,
            "max_tokens": max_tokens,
            "ratio": ratio,
            "source": source,
            **({
                "warning": "Provider 未返回 prompt_tokens，当前为按本次实际请求视图计算值",
            } if source == "request_view" else {}),
        }

    def list_sessions(
        self,
        workspace_id: str | None = None,
        *,
        owner_account_id: str,
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
            where = (
                f"updated_at < ? AND last_status IN ({status_placeholders}) "
                f"AND session_id NOT LIKE '%::%'" + exclude_clause
            )
            expired = conn.execute(
                f"SELECT owner_account_id, session_id FROM sessions WHERE {where}", params
            ).fetchall()
            # 先清事件与租约（子查询仍能命中待删的 sessions 行），再删会话本体。
            conn.execute(
                "DELETE FROM session_events WHERE (owner_account_id, session_id) IN "
                f"(SELECT owner_account_id, session_id FROM sessions WHERE {where})",
                params,
            )
            conn.execute(
                "DELETE FROM writer_leases WHERE (owner_account_id, session_id) IN "
                f"(SELECT owner_account_id, session_id FROM sessions WHERE {where})",
                params,
            )
            cursor = conn.execute(f"DELETE FROM sessions WHERE {where}", params)
            self._expired_keys = [(str(o), str(s)) for o, s in expired]
            return cursor.rowcount

        deleted = self._writer.execute(_write)
        for key in getattr(self, "_expired_keys", ()):
            self._projections.pop(key, None)
        self._expired_keys = []
        return deleted

    # ---- Session 级 AgentConfig ----
    @staticmethod
    def _now_iso() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def set_agent_config(
        self,
        session_id: str,
        config: dict[str, Any],
        owner_account_id: str,
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
        owner_account_id: str,
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

    def get_agent_config(self, session_id: str, owner_account_id: str) -> dict[str, Any] | None:
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

    def clear_agent_config(self, session_id: str, owner_account_id: str) -> None:
        def _write(conn):
            conn.execute(
                "DELETE FROM session_agent_config WHERE session_id = ? AND owner_account_id = ?",
                (session_id, owner_account_id),
            )
        self._writer.execute(_write)
