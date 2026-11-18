"""Durable claims for background terminal processes sharing one Crew home."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

import psutil

from crew.security.process_lifecycle import read_process_start_time


class ProcessCheckpoints:
    def __init__(self) -> None:
        self._host_pid = 0
        self._identity()

    def _identity(self) -> tuple[str, int, str | None]:
        """A forked Gateway must acquire its own claims even when modules are inherited."""
        pid = os.getpid()
        if self._host_pid != pid:
            self._registry_id = uuid.uuid4().hex
            self._host_started = read_process_start_time(pid)
            self._host_pid = pid
        return self._registry_id, pid, self._host_started

    def record(self, path: Path, session_id: str, child_started: str, data: dict) -> None:
        registry_id, pid, started = self._identity()
        if not started:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS processes ("
                "session_id TEXT PRIMARY KEY, registry_id TEXT NOT NULL, host_pid INTEGER NOT NULL, "
                "host_started TEXT NOT NULL, child_started TEXT NOT NULL, data TEXT NOT NULL)"
            )
            db.execute("INSERT INTO processes VALUES (?, ?, ?, ?, ?, ?)", (
                session_id, registry_id, pid, started, child_started, json.dumps(data),
            ))

    def forget(self, path: Path, session_id: str) -> None:
        registry_id, _, _ = self._identity()
        if not path.is_file():
            return
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("DELETE FROM processes WHERE session_id=? AND registry_id=?",
                       (session_id, registry_id))

    def claim(self, path: Path) -> list[tuple[dict, str]]:
        """Transfer each surviving child once, using the observed registry as a compare-and-swap."""
        registry_id, pid, started = self._identity()
        if not path.is_file() or not started:
            return []
        claimed = []
        with closing(sqlite3.connect(path)) as db, db:
            rows = db.execute(
                "SELECT session_id, registry_id, host_pid, host_started, child_started, data "
                "FROM processes WHERE registry_id != ?", (registry_id,),
            ).fetchall()
            for session_id, previous, host_pid, host_started, child_started, raw in rows:
                current_host = read_process_start_time(host_pid)
                if current_host == host_started or (current_host is None and psutil.pid_exists(host_pid)):
                    continue
                try:
                    data = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if not isinstance(data, dict) or type(data.get("pid")) is not int or data["pid"] <= 0:
                    continue
                if not isinstance(data.get("command"), str) or not data.get("owner_account_id"):
                    continue
                data["id"] = session_id
                child_pid = data["pid"]
                current_child = read_process_start_time(child_pid)
                if current_child != child_started:
                    if current_child is not None or not psutil.pid_exists(child_pid):
                        db.execute("DELETE FROM processes WHERE session_id=? AND registry_id=?",
                                   (session_id, previous))
                    continue
                changed = db.execute(
                    "UPDATE processes SET registry_id=?, host_pid=?, host_started=? "
                    "WHERE session_id=? AND registry_id=?",
                    (registry_id, pid, started, session_id, previous),
                )
                if changed.rowcount:
                    claimed.append((data, child_started))
        return claimed
