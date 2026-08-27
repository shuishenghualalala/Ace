"""SQLite persistence for the Companion domain.

Conversation messages stay in SessionStore.  This store owns only Companion
relationships, public Agent mappings, room membership, link outbox and private
Agent-run projections.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _decode(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


class CompanionStore:
    def __init__(self, db_path: str, *, wal_enabled: bool = True) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = connect_sqlite(self._path, wal_enabled=wal_enabled)
        self._writer = SQLiteWriteHelper(self._conn, self._lock)
        self._writer.execute(self._init_schema)

    @staticmethod
    def _init_schema(conn) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS companion_profile (
              owner_account_id TEXT NOT NULL,
              display_name TEXT NOT NULL DEFAULT '',
              avatar TEXT NOT NULL DEFAULT '',
              discoverable INTEGER NOT NULL DEFAULT 1,
              revision INTEGER NOT NULL DEFAULT 1,
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL,
              PRIMARY KEY (owner_account_id)
            );

            CREATE TABLE IF NOT EXISTS companion_agent_publication (
              owner_account_id TEXT NOT NULL,
              public_agent_id TEXT NOT NULL,
              source_kind TEXT NOT NULL,
              source_id TEXT NOT NULL,
              display_name TEXT NOT NULL,
              description TEXT NOT NULL DEFAULT '',
              capabilities_json TEXT NOT NULL DEFAULT '[]',
              enabled INTEGER NOT NULL DEFAULT 1,
              revision INTEGER NOT NULL DEFAULT 1,
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL,
              PRIMARY KEY (owner_account_id, public_agent_id),
              UNIQUE (owner_account_id, source_kind, source_id)
            );

            CREATE TABLE IF NOT EXISTS companion_conversation_binding (
              owner_account_id TEXT NOT NULL,
              conversation_kind TEXT NOT NULL,
              target_id TEXT NOT NULL,
              session_id TEXT NOT NULL,
              workspace_id TEXT NOT NULL,
              title TEXT NOT NULL DEFAULT '',
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL,
              PRIMARY KEY (owner_account_id, conversation_kind, target_id),
              UNIQUE (owner_account_id, session_id)
            );

            CREATE TABLE IF NOT EXISTS companion_peer (
              owner_account_id TEXT NOT NULL,
              peer_id TEXT NOT NULL,
              profile_json TEXT NOT NULL DEFAULT '{}',
              relationship TEXT NOT NULL DEFAULT 'nearby',
              connection_state TEXT NOT NULL DEFAULT 'unavailable',
              last_seen REAL NOT NULL DEFAULT 0,
              updated_at REAL NOT NULL,
              PRIMARY KEY (owner_account_id, peer_id)
            );

            CREATE TABLE IF NOT EXISTS companion_room (
              owner_account_id TEXT NOT NULL,
              room_id TEXT NOT NULL,
              name TEXT NOT NULL,
              owner_peer_id TEXT NOT NULL DEFAULT '',
              revision INTEGER NOT NULL DEFAULT 1,
              updated_at REAL NOT NULL,
              PRIMARY KEY (owner_account_id, room_id)
            );

            CREATE TABLE IF NOT EXISTS companion_room_member (
              owner_account_id TEXT NOT NULL,
              room_id TEXT NOT NULL,
              member_kind TEXT NOT NULL,
              member_id TEXT NOT NULL,
              owner_peer_id TEXT NOT NULL DEFAULT '',
              state TEXT NOT NULL DEFAULT 'active',
              profile_json TEXT NOT NULL DEFAULT '{}',
              updated_at REAL NOT NULL,
              PRIMARY KEY (owner_account_id, room_id, member_kind, member_id)
            );

            CREATE TABLE IF NOT EXISTS companion_outbox (
              owner_account_id TEXT NOT NULL,
              event_id TEXT NOT NULL,
              conversation_kind TEXT NOT NULL,
              target_id TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'queued',
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL,
              PRIMARY KEY (owner_account_id, event_id)
            );
            CREATE INDEX IF NOT EXISTS idx_companion_outbox_status
              ON companion_outbox(owner_account_id, status, created_at);

            CREATE TABLE IF NOT EXISTS companion_agent_run (
              owner_account_id TEXT NOT NULL,
              run_id TEXT NOT NULL,
              room_id TEXT NOT NULL,
              public_agent_id TEXT NOT NULL,
              source_message_id TEXT NOT NULL,
              child_session_id TEXT NOT NULL,
              status TEXT NOT NULL,
              final_text TEXT NOT NULL DEFAULT '',
              error_text TEXT NOT NULL DEFAULT '',
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL,
              PRIMARY KEY (owner_account_id, run_id)
            );
            """
        )

    def ensure_profile(self, owner_account_id: str) -> dict[str, Any]:
        now = time.time()
        self._writer.execute(
            lambda conn: conn.execute(
                "INSERT OR IGNORE INTO companion_profile "
                "(owner_account_id, created_at, updated_at) VALUES (?, ?, ?)",
                (owner_account_id, now, now),
            )
        )
        with self._lock:
            row = self._conn.execute(
                "SELECT display_name, avatar, discoverable, revision, created_at, updated_at "
                "FROM companion_profile WHERE owner_account_id = ?",
                (owner_account_id,),
            ).fetchone()
        return {
            "display_name": row[0],
            "avatar": row[1],
            "discoverable": bool(row[2]),
            "revision": int(row[3]),
            "created_at": float(row[4]),
            "updated_at": float(row[5]),
        }

    def update_profile(self, owner_account_id: str, **fields: Any) -> dict[str, Any]:
        self.ensure_profile(owner_account_id)
        allowed = {
            key: value
            for key, value in fields.items()
            if key in {"display_name", "avatar", "discoverable"} and value is not None
        }
        if "discoverable" in allowed:
            allowed["discoverable"] = 1 if bool(allowed["discoverable"]) else 0
        if allowed:
            sets = ", ".join(f"{key} = ?" for key in allowed)
            self._writer.execute(
                lambda conn: conn.execute(
                    f"UPDATE companion_profile SET {sets}, revision = revision + 1, updated_at = ? "
                    "WHERE owner_account_id = ?",
                    (*allowed.values(), time.time(), owner_account_id),
                )
            )
        return self.ensure_profile(owner_account_id)

    def upsert_publication(
        self,
        owner_account_id: str,
        *,
        source_kind: str,
        source_id: str,
        display_name: str,
        description: str = "",
        capabilities: list[str] | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            existing = self._conn.execute(
                "SELECT public_agent_id FROM companion_agent_publication "
                "WHERE owner_account_id = ? AND source_kind = ? AND source_id = ?",
                (owner_account_id, source_kind, source_id),
            ).fetchone()
        public_id = str(existing[0]) if existing else f"agent_{uuid.uuid4().hex}"

        def _write(conn) -> None:
            conn.execute(
                """
                INSERT INTO companion_agent_publication (
                  owner_account_id, public_agent_id, source_kind, source_id,
                  display_name, description, capabilities_json, enabled,
                  revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(owner_account_id, source_kind, source_id) DO UPDATE SET
                  display_name = excluded.display_name,
                  description = excluded.description,
                  capabilities_json = excluded.capabilities_json,
                  enabled = excluded.enabled,
                  revision = companion_agent_publication.revision + 1,
                  updated_at = excluded.updated_at
                """,
                (
                    owner_account_id,
                    public_id,
                    source_kind,
                    source_id,
                    display_name,
                    description,
                    _json(capabilities or []),
                    1 if enabled else 0,
                    now,
                    now,
                ),
            )

        self._writer.execute(_write)
        return self.get_publication(owner_account_id, public_id)

    def set_publications_enabled(self, owner_account_id: str, source_refs: set[tuple[str, str]]) -> None:
        now = time.time()

        def _write(conn) -> None:
            rows = conn.execute(
                "SELECT source_kind, source_id, enabled FROM companion_agent_publication "
                "WHERE owner_account_id = ?",
                (owner_account_id,),
            ).fetchall()
            for kind, source_id, enabled in rows:
                next_enabled = 1 if (str(kind), str(source_id)) in source_refs else 0
                if int(enabled) == next_enabled:
                    continue
                conn.execute(
                    "UPDATE companion_agent_publication SET enabled = ?, revision = revision + 1, "
                    "updated_at = ? WHERE owner_account_id = ? AND source_kind = ? AND source_id = ?",
                    (next_enabled, now, owner_account_id, kind, source_id),
                )

        self._writer.execute(_write)

    def get_publication(self, owner_account_id: str, public_agent_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT public_agent_id, source_kind, source_id, display_name, description, "
                "capabilities_json, enabled, revision, created_at, updated_at "
                "FROM companion_agent_publication WHERE owner_account_id = ? AND public_agent_id = ?",
                (owner_account_id, public_agent_id),
            ).fetchone()
        if not row:
            raise KeyError(public_agent_id)
        return self._publication_row(row)

    def list_publications(self, owner_account_id: str, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = (
            "SELECT public_agent_id, source_kind, source_id, display_name, description, "
            "capabilities_json, enabled, revision, created_at, updated_at "
            "FROM companion_agent_publication WHERE owner_account_id = ?"
        )
        params: tuple[Any, ...] = (owner_account_id,)
        if enabled_only:
            sql += " AND enabled = 1"
        sql += " ORDER BY created_at ASC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._publication_row(row) for row in rows]

    @staticmethod
    def _publication_row(row) -> dict[str, Any]:
        return {
            "public_agent_id": row[0],
            "source_kind": row[1],
            "source_id": row[2],
            "display_name": row[3],
            "description": row[4],
            "capabilities": _decode(row[5], []),
            "enabled": bool(row[6]),
            "revision": int(row[7]),
            "created_at": float(row[8]),
            "updated_at": float(row[9]),
        }

    def bind_conversation(
        self,
        owner_account_id: str,
        *,
        kind: str,
        target_id: str,
        session_id: str,
        workspace_id: str,
        title: str,
    ) -> dict[str, Any]:
        now = time.time()
        self._writer.execute(
            lambda conn: conn.execute(
                """
                INSERT INTO companion_conversation_binding (
                  owner_account_id, conversation_kind, target_id, session_id,
                  workspace_id, title, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_account_id, conversation_kind, target_id) DO UPDATE SET
                  workspace_id = excluded.workspace_id,
                  title = CASE WHEN excluded.title <> '' THEN excluded.title ELSE companion_conversation_binding.title END,
                  updated_at = excluded.updated_at
                """,
                (owner_account_id, kind, target_id, session_id, workspace_id, title, now, now),
            )
        )
        return self.get_binding(owner_account_id, kind=kind, target_id=target_id)

    def get_binding(self, owner_account_id: str, *, kind: str, target_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT conversation_kind, target_id, session_id, workspace_id, title, created_at, updated_at "
                "FROM companion_conversation_binding WHERE owner_account_id = ? "
                "AND conversation_kind = ? AND target_id = ?",
                (owner_account_id, kind, target_id),
            ).fetchone()
        if not row:
            raise KeyError(f"{kind}:{target_id}")
        return self._binding_row(row)

    def binding_for_session(self, owner_account_id: str, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT conversation_kind, target_id, session_id, workspace_id, title, created_at, updated_at "
                "FROM companion_conversation_binding WHERE owner_account_id = ? AND session_id = ?",
                (owner_account_id, session_id),
            ).fetchone()
        return self._binding_row(row) if row else None

    def list_bindings(self, owner_account_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT conversation_kind, target_id, session_id, workspace_id, title, created_at, updated_at "
                "FROM companion_conversation_binding WHERE owner_account_id = ? ORDER BY updated_at DESC",
                (owner_account_id,),
            ).fetchall()
        return [self._binding_row(row) for row in rows]

    @staticmethod
    def _binding_row(row) -> dict[str, Any]:
        return {
            "kind": row[0],
            "target_id": row[1],
            "session_id": row[2],
            "workspace_id": row[3],
            "title": row[4],
            "created_at": float(row[5]),
            "updated_at": float(row[6]),
        }

    def upsert_peer(
        self,
        owner_account_id: str,
        peer_id: str,
        *,
        profile: dict[str, Any],
        relationship: str = "nearby",
        connection_state: str = "unavailable",
        last_seen: float | None = None,
    ) -> None:
        now = time.time()
        self._writer.execute(
            lambda conn: conn.execute(
                """
                INSERT INTO companion_peer (
                  owner_account_id, peer_id, profile_json, relationship,
                  connection_state, last_seen, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_account_id, peer_id) DO UPDATE SET
                  profile_json = excluded.profile_json,
                  relationship = excluded.relationship,
                  connection_state = excluded.connection_state,
                  last_seen = excluded.last_seen,
                  updated_at = excluded.updated_at
                """,
                (
                    owner_account_id,
                    peer_id,
                    _json(profile),
                    relationship,
                    connection_state,
                    float(last_seen if last_seen is not None else now),
                    now,
                ),
            )
        )

    def list_peers(self, owner_account_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT peer_id, profile_json, relationship, connection_state, last_seen, updated_at "
                "FROM companion_peer WHERE owner_account_id = ? ORDER BY last_seen DESC",
                (owner_account_id,),
            ).fetchall()
        return [
            {
                "peer_id": row[0],
                "profile": _decode(row[1], {}),
                "relationship": row[2],
                "connection_state": row[3],
                "last_seen": float(row[4]),
                "updated_at": float(row[5]),
            }
            for row in rows
        ]

    def upsert_room(
        self,
        owner_account_id: str,
        room_id: str,
        *,
        name: str,
        owner_peer_id: str = "",
        revision: int = 1,
        human_member_ids: list[str] | None = None,
    ) -> None:
        now = time.time()

        def _write(conn) -> None:
            conn.execute(
                """
                INSERT INTO companion_room (owner_account_id, room_id, name, owner_peer_id, revision, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_account_id, room_id) DO UPDATE SET
                  name = excluded.name,
                  owner_peer_id = excluded.owner_peer_id,
                  revision = MAX(companion_room.revision, excluded.revision),
                  updated_at = excluded.updated_at
                """,
                (owner_account_id, room_id, name, owner_peer_id, max(1, int(revision)), now),
            )
            for peer_id in human_member_ids or []:
                conn.execute(
                    """
                    INSERT INTO companion_room_member (
                      owner_account_id, room_id, member_kind, member_id,
                      owner_peer_id, state, profile_json, updated_at
                    ) VALUES (?, ?, 'human', ?, ?, 'active', '{}', ?)
                    ON CONFLICT(owner_account_id, room_id, member_kind, member_id) DO UPDATE SET
                      state = 'active', updated_at = excluded.updated_at
                    """,
                    (owner_account_id, room_id, peer_id, peer_id, now),
                )

        self._writer.execute(_write)

    def list_rooms(self, owner_account_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rooms = self._conn.execute(
                "SELECT room_id, name, owner_peer_id, revision, updated_at FROM companion_room "
                "WHERE owner_account_id = ? ORDER BY updated_at DESC",
                (owner_account_id,),
            ).fetchall()
            members = self._conn.execute(
                "SELECT room_id, member_kind, member_id, owner_peer_id, state, profile_json "
                "FROM companion_room_member WHERE owner_account_id = ?",
                (owner_account_id,),
            ).fetchall()
        by_room: dict[str, list[dict[str, Any]]] = {}
        for row in members:
            by_room.setdefault(str(row[0]), []).append(
                {
                    "kind": row[1],
                    "id": row[2],
                    "owner_peer_id": row[3],
                    "state": row[4],
                    "profile": _decode(row[5], {}),
                }
            )
        return [
            {
                "room_id": row[0],
                "name": row[1],
                "owner_peer_id": row[2],
                "revision": int(row[3]),
                "updated_at": float(row[4]),
                "members": by_room.get(str(row[0]), []),
            }
            for row in rooms
        ]

    def enqueue(
        self,
        owner_account_id: str,
        *,
        kind: str,
        target_id: str,
        payload: dict[str, Any],
        event_id: str | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        event = event_id or f"evt_{uuid.uuid4().hex}"
        self._writer.execute(
            lambda conn: conn.execute(
                "INSERT OR IGNORE INTO companion_outbox "
                "(owner_account_id, event_id, conversation_kind, target_id, payload_json, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)",
                (owner_account_id, event, kind, target_id, _json(payload), now, now),
            )
        )
        return {"event_id": event, "status": "queued"}

    def claim_outbox(self, owner_account_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        now = time.time()

        def _write(conn):
            rows = conn.execute(
                "SELECT event_id, conversation_kind, target_id, payload_json, created_at "
                "FROM companion_outbox WHERE owner_account_id = ? AND status = 'queued' "
                "ORDER BY created_at ASC LIMIT ?",
                (owner_account_id, limit),
            ).fetchall()
            if rows:
                conn.executemany(
                    "UPDATE companion_outbox SET status = 'sending', updated_at = ? "
                    "WHERE owner_account_id = ? AND event_id = ? AND status = 'queued'",
                    [(now, owner_account_id, row[0]) for row in rows],
                )
            return rows

        rows = self._writer.execute(_write)
        return [
            {
                "event_id": row[0],
                "kind": row[1],
                "target_id": row[2],
                "payload": _decode(row[3], {}),
                "created_at": float(row[4]),
            }
            for row in rows
        ]

    def settle_outbox(self, owner_account_id: str, event_id: str, *, delivered: bool) -> None:
        self.set_outbox_status(
            owner_account_id,
            event_id,
            status="delivered" if delivered else "queued",
        )

    def set_outbox_status(
        self,
        owner_account_id: str,
        event_id: str,
        *,
        status: str,
    ) -> str:
        if status not in {"queued", "sending", "sent", "delivered", "failed"}:
            raise ValueError("无效的同伴消息投递状态")
        allowed_from = {
            "queued": ("queued", "sending", "sent", "failed"),
            "sending": ("queued",),
            "sent": ("queued", "sending", "sent", "failed"),
            "delivered": ("queued", "sending", "sent", "failed", "delivered"),
            "failed": ("queued", "sending", "sent", "failed"),
        }[status]

        def _write(conn) -> str:
            placeholders = ", ".join("?" for _ in allowed_from)
            conn.execute(
                "UPDATE companion_outbox SET status = ?, updated_at = ? "
                f"WHERE owner_account_id = ? AND event_id = ? AND status IN ({placeholders})",
                (status, time.time(), owner_account_id, event_id, *allowed_from),
            )
            row = conn.execute(
                "SELECT status FROM companion_outbox WHERE owner_account_id = ? AND event_id = ?",
                (owner_account_id, event_id),
            ).fetchone()
            return str(row[0]) if row else status

        return self._writer.execute(_write)

    def create_run(
        self,
        owner_account_id: str,
        *,
        room_id: str,
        public_agent_id: str,
        source_message_id: str,
        child_session_id: str,
    ) -> dict[str, Any]:
        now = time.time()
        run_id = f"run_{uuid.uuid4().hex}"
        self._writer.execute(
            lambda conn: conn.execute(
                "INSERT INTO companion_agent_run "
                "(owner_account_id, run_id, room_id, public_agent_id, source_message_id, child_session_id, "
                "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?)",
                (
                    owner_account_id,
                    run_id,
                    room_id,
                    public_agent_id,
                    source_message_id,
                    child_session_id,
                    now,
                    now,
                ),
            )
        )
        return {
            "run_id": run_id,
            "room_id": room_id,
            "public_agent_id": public_agent_id,
            "source_message_id": source_message_id,
            "child_session_id": child_session_id,
            "status": "running",
        }

    def finish_run(
        self,
        owner_account_id: str,
        run_id: str,
        *,
        final_text: str = "",
        error_text: str = "",
    ) -> None:
        status = "failed" if error_text else "completed"
        self._writer.execute(
            lambda conn: conn.execute(
                "UPDATE companion_agent_run SET status = ?, final_text = ?, error_text = ?, updated_at = ? "
                "WHERE owner_account_id = ? AND run_id = ?",
                (status, final_text, error_text, time.time(), owner_account_id, run_id),
            )
        )

    def list_runs(
        self, owner_account_id: str, room_id: str | None = None
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT run_id, room_id, public_agent_id, source_message_id, child_session_id, status, "
            "final_text, error_text, created_at, updated_at FROM companion_agent_run "
            "WHERE owner_account_id = ?"
        )
        params: tuple[Any, ...] = (owner_account_id,)
        if room_id:
            query += " AND room_id = ?"
            params = (owner_account_id, room_id)
        query += " ORDER BY created_at ASC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            {
                "run_id": row[0],
                "room_id": row[1],
                "public_agent_id": row[2],
                "source_message_id": row[3],
                "child_session_id": row[4],
                "status": row[5],
                "final_text": row[6],
                "error_text": row[7],
                "created_at": float(row[8]),
                "updated_at": float(row[9]),
            }
            for row in rows
        ]
