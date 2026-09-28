"""SQLite 会话存储。把 Message 列表序列化为 JSON 存表。

用于 Crew_state.py（其用 SQLite + FTS5），这里先做基础持久化，
全文检索等留作扩展点。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import uuid
from collections import OrderedDict
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


def _lease_holder_pid_alive(holder: str) -> bool:
    """探测租约持有者进程是否存活。可在 SQLite 写事务内调用，必须快、无子进程。

    holder 形如 "pid:token"（token 区分同进程多实例）；解析不出有效 pid 时按
    存活处理，保守退回 TTL 过期接管的老路径，绝不误抢。进程已死则其租约必然
    无人续约（心跳/写入都会消失），可立即视同过期。

    Windows 不用 os.kill(pid, 0)——它在 Windows 上的语义是 TerminateProcess，
    探测即杀人；改用 OpenProcess 句柄探测。
    """
    try:
        pid = int(holder.split(":", 1)[0])
    except ValueError:
        return True
    if pid <= 0:
        return True
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        # PROCESS_QUERY_LIMITED_INFORMATION：仅查询存在性所需的最小权限
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        # ERROR_ACCESS_DENIED：受保护进程存在但拒绝访问 → 仍算活着
        return ctypes.get_last_error() == 5
    try:
        os.kill(pid, 0)  # 信号 0：只探测存在性，不真正发信号
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但无权限 → 仍算活着
    except OSError:
        return False


class SessionEventType(str, Enum):
    """session_events 事件类型；durable=False 的瞬态事件不落盘。"""

    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_RESULT = "tool_result"
    SYSTEM_MESSAGE = "system_message"
    METER_CHECKPOINT = "meter_checkpoint"
    TURN_START = "turn_start"  # 回合边界（D4 断点扫描）
    TURN_END = "turn_end"
    ASSISTANT_TOOL_CALLS = "assistant_tool_calls"
    TOOL_EXECUTION_INTENT = "tool_execution_intent"
    TOOL_RESULT_COMMIT = "tool_result_commit"
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

# 角色事件类型值集合（窗口链走行时 O(1) 判定；每条 durable 消息恰为一个角色事件）
_ROLE_EVENT_TYPES_SET = frozenset(e.value for e in _ROLE_EVENT_TYPES.values())


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

    from_blob：投影来自 legacy blob 回退（无事件行）。此时「前缀一致 → 追加」
    的增量判定不成立——事件视图尚不存在，blob 前缀必须整链重写一次落进事件表，
    否则该前缀只存在于不再被读的 blob 列里（save 抢在后台回填之前的竞态，
    见 _save_write 的 is_append 守卫）。
    """

    __slots__ = ("messages", "seq", "generation", "from_blob")

    def __init__(self) -> None:
        self.messages: list[Message] = []
        self.seq: int = 0
        self.generation: int = 0
        self.from_blob: bool = False


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
        # P1-4：legacy blob 回填移出启动关键路径（健康检查可用时间与 legacy 数据量解耦）
        self._start_legacy_backfill()
        # 投影缓存 LRU：会话数不设上限时，多账号/长历史场景下内存随会话总数线性
        # 增长（每投影含全量消息）。淘汰只丢缓存不丢数据——未命中按事件增量重建。
        self._projections: OrderedDict[tuple[str, str], _SessionProjection] = OrderedDict()
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
        # 后台回填短暂让路：避免关连接与回填事务竞争（超时则由回填自身的
        # 异常兜底处理，下次启动重试）。
        if getattr(self, "_backfill_done", None) is not None:
            self._backfill_done.wait(5)
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

        持有者进程已死时（强杀/崩溃跳过了 close() 的租约清理）租约必然无人
        续约，视同过期立即接管，不等 TTL——重启后向旧会话写入不再白等或报错。
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
        holder_alive = _lease_holder_pid_alive(holder)
        if expires_at > now and holder_alive:
            raise SessionWriteConflict(
                f"会话 {session_id} 的写者租约被进程 {holder} 持有"
                f"（{expires_at - now:.0f}s 后到期）"
            )
        # 过期接管 / 持有者已死：条件 UPDATE + rows_affected 原子抢占，fence+1。
        # owner_pid = holder 把抢占钉死在探测时确认已死的那个持有者上：
        # 并发接管或持有者复活（PID 被复用）时条件不成立，rowcount=0 走冲突。
        cursor = conn.execute(
            "UPDATE writer_leases SET owner_pid = ?, fence = fence + 1, expires_at = ? "
            "WHERE owner_account_id = ? AND session_id = ? "
            "AND (expires_at <= ? OR owner_pid = ?)",
            (self._writer_pid, expires, owner, session_id, now, holder),
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
        # 清理已过期的写者租约：持有进程已死或租期已尽后无人续约，陈行只会在
        # 表里无限累积。活进程的租约必然未过期，不受影响。删除后 fence 从 1
        # 重新起算不影响防脑裂：续约与写入都对照当前行的 owner_pid/fence 校验，
        # 不依赖跨删除的全局单调性。
        conn.execute("DELETE FROM writer_leases WHERE expires_at < ?", (time.time(),))
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
        # fork 子会话枚举（list_branches）按 (owner, source_session_id) 等值查询
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(owner_account_id, source_session_id)")
        # 存储级 KV 标记（当前仅 legacy 回填完成标记，P1-4）
        conn.execute(
            "CREATE TABLE IF NOT EXISTS store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
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

    BACKFILL_DONE_KEY = "legacy_blob_backfill_done"

    def _backfill_marker_present(self, conn) -> bool:
        row = conn.execute(
            "SELECT value FROM store_meta WHERE key = ?", (self.BACKFILL_DONE_KEY,)
        ).fetchone()
        return row is not None

    def _mark_backfill_done(self, conn) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO store_meta (key, value) VALUES (?, ?)",
            (self.BACKFILL_DONE_KEY, str(time.time())),
        )

    def _start_legacy_backfill(self) -> None:
        """P1-4：legacy blob 回填移出启动关键路径，后台线程执行。

        - store_meta 完成标记跨重启幂等（已完成则本轮跳过）
        - 回填与常规写在 _writer 事务层串行，无并发竞争；回填期间的读路径
          经 blob 回退照常正确（回填只是把 legacy 数据搬进事件格式）
        - 失败不阻断：标记不落，下次启动重试
        """
        self._backfill_done = threading.Event()

        def _run() -> None:
            try:
                def _txn(conn) -> None:
                    if self._backfill_marker_present(conn):
                        return
                    self._backfill_legacy_blobs(conn)
                    self._mark_backfill_done(conn)

                self._writer.execute(_txn)
            except Exception:
                log.exception("legacy blob 回填失败（读路径 blob 回退仍可用，下次启动重试）")
            finally:
                self._backfill_done.set()

        threading.Thread(target=_run, name="crew-legacy-backfill", daemon=True).start()

    def wait_for_legacy_backfill(self, timeout: float = 30.0) -> bool:
        """等待后台回填完成（测试与关停场景用）；未启动回填时直接返回 True。"""
        event = getattr(self, "_backfill_done", None)
        return True if event is None else event.wait(timeout)

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
    PROJECTION_CACHE_LIMIT = 32

    def _cache_projection(self, key: tuple[str, str], proj: _SessionProjection) -> None:
        """写入投影缓存并按 LRU 淘汰最久未用的会话。"""
        self._projections[key] = proj
        self._projections.move_to_end(key)
        while len(self._projections) > self.PROJECTION_CACHE_LIMIT:
            self._projections.popitem(last=False)

    def _get_projection(self, owner: str, session_id: str) -> _SessionProjection:
        key = (owner, session_id)
        proj = self._projections.get(key)
        if proj is None:
            proj = self._build_projection(owner, session_id)
            self._cache_projection(key, proj)
        else:
            self._projections.move_to_end(key)
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

    # ---- 窗口读取（P1-1：首屏尾部窗口 + 游标翻页） ----

    WINDOW_MAX_LIMIT = 500
    # 链走行的分批取行大小：> 窗口上限，容纳回合标记 / 弃尾行等非角色事件混排
    WINDOW_FETCH_BATCH = 640

    def _fork_source_of(self, owner: str, session_id: str) -> tuple[str, int] | None:
        """读会话的 end_seed 前缀指针 (source_session_id, source_parent_seq)；无则 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM session_events "
                "WHERE owner_account_id = ? AND session_id = ? AND type = ? LIMIT 1",
                (owner, session_id, SessionEventType.END_SEED.value),
            ).fetchone()
        if row is None:
            return None
        try:
            meta = json.loads(row[0] or "{}")
        except json.JSONDecodeError as exc:
            raise SessionEventLogError(
                f"end_seed 载荷损坏（session={session_id}），拒绝窗口读取"
            ) from exc
        source_id = meta.get("source_session_id")
        source_seq = meta.get("source_parent_seq")
        if not isinstance(source_id, str) or not source_id or not isinstance(source_seq, int):
            raise SessionEventLogError(
                f"end_seed 载荷缺前缀指针（session={session_id}），拒绝窗口读取"
            )
        return source_id, source_seq

    def _fork_chain(self, owner: str, session_id: str) -> list[tuple[str, int | None]]:
        """fork 前缀链 [(sid, seq 上界)]，从本会话到根。

        本会话上界为 None（读自身 leaf）；祖先的上界来自子会话 end_seed 内联的
        source_parent_seq——它界定了「属于本 fork 分支」的源前缀终点。
        成环时截断返回（读取路径有独立 fail-closed 校验）。
        """
        chain: list[tuple[str, int | None]] = [(session_id, None)]
        seen = {session_id}
        hop = session_id
        while True:
            source = self._fork_source_of(owner, hop)
            if source is None or source[0] in seen:
                return chain
            seen.add(source[0])
            chain.append(source)
            hop = source[0]

    def _fetch_rows_desc(
        self, owner: str, session_id: str, from_seq: int, limit: int
    ) -> dict[int, tuple[int | None, str, str]]:
        """取 seq <= from_seq 的最后 limit 行 {seq: (parent_seq, type, payload)}（含非角色事件）。"""
        with self._lock:
            return {
                int(r[0]): (r[1], str(r[2]), str(r[3]))
                for r in self._conn.execute(
                    "SELECT seq, parent_seq, type, payload FROM session_events "
                    "WHERE owner_account_id = ? AND session_id = ? AND seq <= ? "
                    "ORDER BY seq DESC LIMIT ?",
                    (owner, session_id, from_seq, limit),
                ).fetchall()
            }

    def _walk_chain_window(
        self,
        owner: str,
        session_id: str,
        head_seq: int,
        exclusive_below: int | None,
    ):
        """沿活链从 head_seq 向旧走，产出角色事件 (sid, seq, Message)，从新到旧。

        - 按父指针走行：rewind 弃尾行不在链上，天然不进窗口
        - 遇 end_seed 按 (source, parent_seq) 跳源会话续走（fork 透明续接）；
          成环 / 载荷损坏 / 链断裂均 fail-closed
        - exclusive_below 仅作用于首个会话（游标翻页：跳过 seq >= 游标的事件），
          跳源会话后清零——源会话的 seq 命名空间独立，不可比
        - 分批取行（WINDOW_FETCH_BATCH），行不在批内则向更旧再取一批
        """
        batch: dict[int, tuple[int | None, str, str]] = {}
        cur_sid = session_id
        cur: int | None = head_seq
        skip_above = exclusive_below
        visited: set[str] = {session_id}
        while cur is not None:
            if cur not in batch:
                batch = self._fetch_rows_desc(owner, cur_sid, cur, self.WINDOW_FETCH_BATCH)
                if cur not in batch:
                    raise SessionEventLogError(
                        f"事件链断裂：{cur_sid}#{cur} 缺失，拒绝窗口读取"
                    )
            parent, etype, payload = batch[cur]
            if etype == SessionEventType.END_SEED.value:
                try:
                    meta = json.loads(payload)
                    next_sid = str(meta["source_session_id"])
                    next_seq = int(meta["source_parent_seq"])
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                    raise SessionEventLogError(
                        f"end_seed 载荷损坏（{cur_sid}#{cur}），拒绝窗口读取"
                    ) from exc
                if next_sid in visited:
                    raise SessionEventLogError(
                        f"fork 前缀成环：{next_sid}，拒绝窗口读取"
                    )
                visited.add(next_sid)
                cur_sid, cur = next_sid, next_seq
                batch = {}
                skip_above = None
                continue
            if etype in _ROLE_EVENT_TYPES_SET and (skip_above is None or cur < skip_above):
                yield cur_sid, cur, SQLiteSessionStore._message_from_dict(json.loads(payload))
            cur = parent if parent is not None else (cur - 1 if cur > 1 else None)

    @staticmethod
    def parse_window_cursor(before: str | None) -> tuple[str, int | None]:
        """解析不透明游标 "session_id:seq"；缺省返回 ("", None)。"""
        if not before:
            return "", None
        text = str(before)
        sid, sep, seq_text = text.rpartition(":")
        if not sep or not sid or not seq_text.isdigit():
            raise ValueError("游标格式必须为 session_id:seq")
        return sid, int(seq_text)

    def load_window(
        self,
        session_id: str,
        *,
        owner_account_id: str,
        limit: int,
        before: str | None = None,
    ) -> tuple[list[tuple[str, int, Message]], bool, str | None]:
        """窗口读取：沿活链倒序收集至多 limit 条消息。

        返回 (升序 [(sid, seq, msg)], has_more, next_before)。
        - 每条 durable 消息恰为一个角色事件，无需跨事件拼装消息边界
        - fork 透明续接（end_seed → 源会话前缀）；rewind 弃尾不进链
        - 游标 "{session_id}:{seq}" 对客户端不透明，原样回传即可；
          游标会话必须在本会话的 fork 前缀链上（防跨会话越权读取）
        """
        limit = max(1, min(int(limit), self.WINDOW_MAX_LIMIT))
        chain = self._fork_chain(owner_account_id, session_id)

        start_sid, start_seq = self.parse_window_cursor(before)
        start_at = 0
        if start_sid:
            if start_sid not in {sid for sid, _bound in chain}:
                raise SessionEventLogError(
                    f"游标会话 {start_sid} 不在 {session_id} 的 fork 前缀链上，拒绝读取"
                )
            start_at = next(i for i, (sid, _bound) in enumerate(chain) if sid == start_sid)

        head_sid, head_bound = chain[start_at]
        cursor = self._read_event_cursor(owner_account_id, head_sid)
        if cursor is None:
            return [], False, None
        if start_seq is not None:
            # 游标续页：head 即游标位置本身（F1）。分页期间源会话被 rewind 到
            # fork 边界之下时，min(bound, leaf) 会把 head 夹到回退后的 leaf，
            # 边界与游标之间的整段被跳过——游标是上一页实际走过的链位置，
            # 其下行路径不受源会话 leaf 移动影响（弃尾行仍在、fork 前缀链不变）；
            # 行若已不存在则链走行 fail-closed。
            head = start_seq
        else:
            head = cursor[0] if head_bound is None else min(head_bound, cursor[0])
        if head <= 0:
            return [], False, None

        collected: list[tuple[str, int, Message]] = []
        walker = self._walk_chain_window(owner_account_id, head_sid, head, start_seq)
        for entry in walker:
            collected.append(entry)
            if len(collected) >= limit:
                break
        has_more = next(walker, None) is not None if len(collected) >= limit else False

        messages = list(reversed(collected))  # 升序（旧 → 新）
        if not collected or not has_more:
            return messages, False, None
        oldest_sid, oldest_seq, _ = collected[-1]
        return messages, True, f"{oldest_sid}:{oldest_seq}"

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
            # blob 回退标记：下一次 save 必须整链重写（而非增量追加），
            # 否则 blob 前缀永远进不了事件视图（F2 竞态守卫）
            proj.from_blob = True
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
            self._cache_projection(key, proj)
            return proj
        if leaf == proj.seq:
            self._projections.move_to_end(key)
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
            self._cache_projection(key, rebuilt)
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

        前缀匹配用 ``>= / <`` 范围谓词而非 LIKE：LIKE 默认大小写不敏感、无法走
        复合主键 (owner, session_id) 索引；范围谓词把全表扫描降为主键区间扫描。
        上界取前缀末字符 +1（``::`` → ``:;``），精确覆盖所有 ``{parent}::`` 开头的 id。
        """
        prefix = f"{session_id}::"
        upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT session_id
                FROM sessions
                WHERE session_id >= ? AND session_id < ? AND owner_account_id = ?
                ORDER BY created_at ASC, updated_at ASC, session_id ASC
                """,
                (prefix, upper, owner_account_id),
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
        # from_blob 守卫（F2）：投影来自 blob 回退时事件视图尚不存在，「前缀一致」
        # 是假象——必须整链重写一次把 blob 前缀落进事件表，否则该前缀只留在
        # 不再被读的 blob 列里（save 抢在后台回填拿到写锁之前的竞态）。
        proj = self._get_projection(owner_account_id, session_id)
        prefix = len(proj.messages)
        is_append = not proj.from_blob and messages[:prefix] == proj.messages
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
            # P1-3 砍 blob 双写：messages 列不再随保存重写（写放大 O(N)/turn 的根源）。
            # 事件行是唯一事实源；messages 仅在 INSERT 时写入 '[]' 占位（NOT NULL 约束），
            # 读路径优先事件投影，仅无事件行的 legacy 会话回退读 blob（见 _build_projection）。
            conn.execute(
                "INSERT INTO sessions "
                "(session_id, owner_account_id, messages, updated_at, created_at, workspace_id, title, message_count, token_count, last_prompt_tokens, last_prompt_tokens_source, leaf_seq, events_generation) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(owner_account_id, session_id) DO UPDATE SET "
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
                    "[]",
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
        # 提交即意味着本次消息集已完整落进事件表（from_blob 触发的整链重写
        # 亦然），blob 回退标记随之清除。
        proj = self._get_projection(owner_account_id, session_id)
        proj.messages = list(messages)
        proj.seq = new_leaf
        proj.generation = new_generation
        proj.from_blob = False

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

    async def set_status_async(
        self, session_id: str, status: str, error: str = "", *, owner_account_id: str
    ) -> None:
        """set_status 的异步门面：写路径（BEGIN IMMEDIATE + busy-retry sleep）离开事件循环。"""
        await asyncio.to_thread(
            self.set_status, session_id, status, error, owner_account_id=owner_account_id
        )

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

        已存在的事件行一律不改写；messages blob 不再随切口重写（P1-3：事件是
        唯一事实源，message_count/token_count 按切口链重算即可）。
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
                "UPDATE sessions SET leaf_seq = ?, message_count = ?, "
                "token_count = ?, updated_at = ? "
                "WHERE owner_account_id = ? AND session_id = ? AND leaf_seq = ?",
                (
                    target_seq,
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
                    "[]",  # P1-3：前缀不落 blob，经 end_seed 共享源会话事件链
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
        self.record_durable_event(
            session_id,
            owner_account_id=owner_account_id,
            kind=kind,
            payload={"status": status} if status else {},
        )

    def record_durable_event(
        self,
        session_id: str,
        *,
        owner_account_id: str,
        kind: SessionEventType,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """追加一个不参与消息投影的 durable 边界事件。

        事件和 session leaf 在同一事务提交。工具 intent/result 使用独立事件类型，
        因而不会把临时执行元数据误当成可发送给 Provider 的 Message；最终历史仍由
        ``save_async`` 以消息事件形式提交。
        """
        if not kind.durable:
            raise ValueError(f"只能持久化 durable 事件: {kind}")
        now = time.time()
        encoded = json.dumps(
            {**(payload or {}), "recorded_at": now},
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
                (owner_account_id, session_id, base + 1, current_leaf or None, kind.value, encoded, now),
            )
            conn.execute(
                "UPDATE sessions SET leaf_seq = ?, updated_at = ? "
                "WHERE owner_account_id = ? AND session_id = ?",
                (base + 1, now, owner_account_id, session_id),
            )

        self._writer.execute(_write)

    async def record_durable_event_async(
        self,
        session_id: str,
        *,
        owner_account_id: str,
        kind: SessionEventType,
        payload: dict[str, Any] | None = None,
    ) -> None:
        await asyncio.to_thread(
            self.record_durable_event,
            session_id,
            owner_account_id=owner_account_id,
            kind=kind,
            payload=payload,
        )

    def reconcile_open_tool_events(
        self,
        session_id: str,
        *,
        owner_account_id: str,
    ) -> list[dict[str, Any]]:
        """把上次进程退出时没有结果的工具 intent 标记为 interrupted。

        这里只追加审计事件，不重放工具，也不伪造 Provider 消息；副作用状态保持
        ``unknown``，由下一轮模型或用户决定是否重新发起新调用。
        """
        cursor = self._read_event_cursor(owner_account_id, session_id)
        if cursor is None:
            return []
        rows = self._resolve_chain(owner_account_id, session_id, cursor[0])
        intents: dict[str, dict[str, Any]] = {}
        completed: set[str] = set()
        for _sid, _seq, event_type, encoded in rows:
            if event_type == SessionEventType.TOOL_EXECUTION_INTENT.value:
                try:
                    payload = json.loads(encoded)
                except json.JSONDecodeError:
                    continue
                call_id = str(payload.get("tool_call_id") or "").strip()
                if call_id:
                    intents[call_id] = payload
            elif event_type == SessionEventType.TOOL_RESULT_COMMIT.value:
                try:
                    payload = json.loads(encoded)
                except json.JSONDecodeError:
                    continue
                call_id = str(payload.get("tool_call_id") or "").strip()
                if call_id:
                    completed.add(call_id)
        pending = [payload for call_id, payload in intents.items() if call_id not in completed]
        for payload in pending:
            self.record_durable_event(
                session_id,
                owner_account_id=owner_account_id,
                kind=SessionEventType.TOOL_RESULT_COMMIT,
                payload={
                    "tool_call_id": str(payload.get("tool_call_id") or ""),
                    "name": str(payload.get("name") or ""),
                    "status": "interrupted",
                    "code": "interrupted",
                    "side_effect_state": "unknown",
                },
            )
        return pending

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

        N+1 治理：开放回合意味着回合从未收敛——last_status 必然停在 'running'
        （状态在回合起点写入、终态才覆盖）或为旧数据空值。先按状态过滤候选，
        把逐会话事件扫描从 O(全部会话) 收敛到 O(可能开放的会话)。
        """
        role_types = tuple(e.value for e in _ROLE_EVENT_TYPES.values())
        marks = ",".join("?" for _ in role_types)
        reports: list[dict[str, Any]] = []
        with self._lock:
            sessions = self._conn.execute(
                "SELECT session_id, title, leaf_seq FROM sessions "
                "WHERE owner_account_id = ? AND session_id NOT LIKE '%::%' AND leaf_seq > 0 "
                "AND last_status IN ('', 'running')",
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
                "SELECT last_prompt_tokens, last_prompt_tokens_source FROM sessions "
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

    def get_agent_configs(
        self, session_ids: list[str], owner_account_id: str
    ) -> dict[str, dict[str, Any]]:
        """批量读取会话 agent 配置：列表页 N+1 治理，一次 IN 查询替代逐会话查询。

        返回 {session_id: config}；无配置行的会话不在结果里（调用方按 None 语义处理）。
        """
        sids = [str(sid) for sid in dict.fromkeys(session_ids) if sid]
        if not sids:
            return {}
        marks = ",".join("?" for _ in sids)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT session_id, config_json, created_at, updated_at "
                f"FROM session_agent_config WHERE owner_account_id = ? AND session_id IN ({marks})",
                (owner_account_id, *sids),
            ).fetchall()
        out: dict[str, dict[str, Any]] = {}
        for sid, config_json, created_at, updated_at in rows:
            try:
                config = json.loads(config_json or "{}")
            except json.JSONDecodeError:
                config = {}
            config["_created_at"] = created_at
            config["_updated_at"] = updated_at
            out[str(sid)] = config
        return out

    def clear_agent_config(self, session_id: str, owner_account_id: str) -> None:
        def _write(conn):
            conn.execute(
                "DELETE FROM session_agent_config WHERE session_id = ? AND owner_account_id = ?",
                (session_id, owner_account_id),
            )
        self._writer.execute(_write)
