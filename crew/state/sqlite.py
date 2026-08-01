"""SQLite concurrency helpers used by Crew stores.

Design constraints:
- Kept as small reusable helpers instead of a monolithic SessionDB class.
- Stores keep their existing schemas and call ``execute_write`` for writes.
"""

from __future__ import annotations

import logging
import random
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Optional, TypeVar

log = logging.getLogger(__name__)
T = TypeVar("T")

_WAL_INCOMPAT_MARKERS = (
    "locking protocol",
    "not authorized",
)
_wal_fallback_warned_paths: set[str] = set()
_wal_fallback_warned_lock = threading.Lock()

_WRITE_MAX_RETRIES = 15
_WRITE_RETRY_MIN_S = 0.020
_WRITE_RETRY_MAX_S = 0.150
_CHECKPOINT_EVERY_N_WRITES = 50


def _on_disk_journal_mode(conn: sqlite3.Connection) -> Optional[str]:
    try:
        row = conn.execute("PRAGMA journal_mode").fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    mode = row[0]
    if isinstance(mode, bytes):
        mode = mode.decode("utf-8", "replace")
    return str(mode).lower()


def apply_wal_with_fallback(conn: sqlite3.Connection, *, db_label: str) -> None:
    """Set WAL mode, falling back to DELETE on WAL-incompatible filesystems."""
    try:
        current = _on_disk_journal_mode(conn)
    except sqlite3.OperationalError:
        current = None
    if current == "wal":
        return
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        return
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if not any(marker in msg for marker in _WAL_INCOMPAT_MARKERS):
            raise
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
        except Exception:
            pass
        with _wal_fallback_warned_lock:
            if db_label not in _wal_fallback_warned_paths:
                _wal_fallback_warned_paths.add(db_label)
                log.warning("%s: WAL unsupported on this filesystem (%s); using DELETE journal mode", db_label, exc)


def connect_sqlite(path: str | Path, *, wal_enabled: bool = True, row_factory: bool = False) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(db_path),
        check_same_thread=False,
        timeout=1.0,
        isolation_level=None,
    )
    if row_factory:
        conn.row_factory = sqlite3.Row
    if wal_enabled:
        apply_wal_with_fallback(conn, db_label=db_path.name)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class SQLiteWriteHelper:
    """Crew BEGIN IMMEDIATE + jitter retry write wrapper."""

    def __init__(self, conn: sqlite3.Connection, lock: threading.Lock):
        self._conn = conn
        self._lock = lock
        self._write_count = 0

    def execute(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        last_err: Optional[Exception] = None
        for attempt in range(_WRITE_MAX_RETRIES):
            try:
                with self._lock:
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        result = fn(self._conn)
                        self._conn.commit()
                    except BaseException:
                        try:
                            self._conn.rollback()
                        except Exception:
                            pass
                        raise
                self._write_count += 1
                if self._write_count % _CHECKPOINT_EVERY_N_WRITES == 0:
                    self.checkpoint()
                return result
            except sqlite3.OperationalError as exc:
                msg = str(exc).lower()
                if "locked" in msg or "busy" in msg:
                    last_err = exc
                    if attempt < _WRITE_MAX_RETRIES - 1:
                        time.sleep(random.uniform(_WRITE_RETRY_MIN_S, _WRITE_RETRY_MAX_S))
                        continue
                raise
        raise last_err or sqlite3.OperationalError("database is locked after max retries")

    def checkpoint(self) -> None:
        try:
            with self._lock:
                self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        except Exception:
            pass
