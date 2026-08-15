"""Minimal structured security audit using the existing SQLite helpers."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import stat
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable
from uuid import uuid4

from crew.security.actions import ActionKind, NormalizedAction
from crew.security.context import SecurityContext
from crew.security.models import PERMISSION_PROFILE_SCHEMA_VERSION, RULE_SCHEMA_VERSION
from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite
from crew.tools.redact import redact_sensitive_display_text

_RETENTION_SECONDS = 30 * 86_400
_BUFFERABLE_ACTION_TYPES = {
    "diagnostic",
    "health_check",
    "runtime_diagnostic",
}
_LOGGER = logging.getLogger(__name__)
_SENSITIVE_CLI_OPTION_NAMES = {
    "api-key",
    "apikey",
    "auth",
    "authorization",
    "client-secret",
    "credential",
    "password",
    "passwd",
    "proxy-user",
    "secret",
    "token",
    "u",
    "user",
}
_SENSITIVE_CLI_ASSIGNMENT_RE = re.compile(
    r"(?i)(--?(?:api[-_]?key|auth(?:orization)?|client[-_]?secret|credential|"
    r"password|passwd|proxy[-_]?user|secret|token|u|user))"
    r"(\s+|=)(\"[^\"]*\"|'[^']*'|[^\s]+)"
)
_SENSITIVE_HTTP_HEADER_RE = re.compile(
    r"(?i)((?:api[-_]?key|authorization|cookie|proxy[-_]?authorization|"
    r"set[-_]?cookie|x[-_]?api[-_]?key|x[-_]?auth[-_]?token)\s*:\s*)"
    r"[^\"'\r\n]+"
)


class AuditWriteError(RuntimeError):
    """Raised when an authorization-significant event cannot be persisted."""


class AuditBufferFullError(AuditWriteError):
    """Raised when the bounded ordinary-event buffer has no remaining capacity."""


class AuditIntegrityError(RuntimeError):
    """Raised when the persisted audit chain cannot be authenticated."""


@dataclass(frozen=True)
class AuditEvent:
    event_id: str
    os_user_hash: str
    owner_account_id: str
    workspace_id: str
    session_id: str
    task_id: str
    request_id: str
    action_type: str
    normalized_action_hash: str
    rule_id: str
    rule_scope: str
    permission_profile_hash: str
    additional_permissions_summary: str
    decision: str
    decision_source: str
    sandbox_backend: str
    capabilities: tuple[str, ...]
    network_target_summary: str
    exit_code: int | None
    stable_error_code: str
    tool_name: str = ""
    action_summary: str = ""
    action_detail: str = ""
    approval_mode: str = ""
    turn_id: str = ""
    policy_version: str = ""
    build_version: str = ""
    model_id: str = ""

    @classmethod
    def for_action(
        cls,
        context: SecurityContext,
        action: NormalizedAction,
        *,
        action_type: str,
        decision: str,
        decision_source: str,
        rule_id: str = "",
        rule_scope: str = "",
        permission_profile_hash: str = "",
        additional_permissions_summary: str = "",
        sandbox_backend: str = "",
        capabilities: tuple[str, ...] = (),
        network_target_summary: str = "",
        exit_code: int | None = None,
        stable_error_code: str = "",
        tool_name: str = "",
        approval_mode: str = "",
        turn_id: str = "",
        policy_version: str = "",
        build_version: str = "",
        model_id: str = "",
    ) -> AuditEvent:
        """Create an event with a compact summary and a forcibly redacted action detail."""
        if not build_version:
            from crew import __version__

            build_version = str(__version__)
        action_summary, action_detail = format_action_for_audit(action)
        return cls(
            event_id=uuid4().hex,
            os_user_hash=hashlib.sha256(context.os_user.encode("utf-8")).hexdigest(),
            owner_account_id=context.owner_account_id,
            workspace_id=context.workspace_id,
            session_id=context.session_id,
            task_id=context.task_id,
            request_id=context.request_id,
            action_type=str(action_type),
            normalized_action_hash=action.digest,
            rule_id=str(rule_id),
            rule_scope=str(rule_scope),
            permission_profile_hash=str(permission_profile_hash),
            additional_permissions_summary=str(additional_permissions_summary),
            decision=str(decision),
            decision_source=str(decision_source),
            sandbox_backend=str(sandbox_backend),
            capabilities=tuple(str(item) for item in capabilities),
            network_target_summary=str(network_target_summary),
            exit_code=exit_code,
            stable_error_code=str(stable_error_code),
            tool_name=str(tool_name),
            action_summary=action_summary,
            action_detail=action_detail,
            approval_mode=str(approval_mode),
            turn_id=str(turn_id or context.turn_id),
            policy_version=str(policy_version or PERMISSION_PROFILE_SCHEMA_VERSION),
            build_version=str(build_version),
            model_id=str(model_id or context.model_id),
        )

    @classmethod
    def for_rule(
        cls,
        context: SecurityContext,
        *,
        rule_id: str,
        action_type: str,
        decision: str,
        action: NormalizedAction | None = None,
        policy_version: str = "",
    ) -> AuditEvent:
        """Create a durable rule lifecycle event without exposing rule payloads.

        Rule creation can carry the already-redacted approval action so the lifecycle
        record remains understandable without requiring a second, approximate lookup.
        Disable/delete events intentionally stay rule-id-only because they have no new
        approved action attached to them.
        """
        from crew import __version__

        if action is None:
            action_summary = f"授权规则：{rule_id[:8]}"
            action_detail = f"规则 ID：{rule_id}"
            normalized_action_hash = hashlib.sha256(rule_id.encode("utf-8")).hexdigest()
        else:
            action_summary, action_detail = format_action_for_audit(action)
            normalized_action_hash = action.digest
        return cls(
            event_id=uuid4().hex,
            os_user_hash=hashlib.sha256(context.os_user.encode("utf-8")).hexdigest(),
            owner_account_id=context.owner_account_id,
            workspace_id=context.workspace_id,
            session_id=context.session_id,
            task_id=context.task_id,
            request_id=context.request_id,
            action_type=action_type,
            normalized_action_hash=normalized_action_hash,
            rule_id=rule_id,
            rule_scope="always",
            permission_profile_hash="",
            additional_permissions_summary="",
            decision=decision,
            decision_source="desktop_user",
            sandbox_backend="",
            capabilities=(),
            network_target_summary="",
            exit_code=None,
            stable_error_code="",
            tool_name="",
            action_summary=action_summary,
            action_detail=action_detail,
            approval_mode="",
            turn_id=context.turn_id,
            policy_version=str(policy_version or RULE_SCHEMA_VERSION),
            build_version=str(__version__),
            model_id=context.model_id,
        )

    @classmethod
    def for_tool_decision(
        cls,
        context: SecurityContext,
        *,
        tool_name: str,
        args: object,
        decision: str,
        decision_source: str,
    ) -> AuditEvent:
        """Create a bounded tool-permission event without exposing raw arguments."""
        from crew import __version__

        canonical = json.dumps(
            args if isinstance(args, dict) else {},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return cls(
            event_id=uuid4().hex,
            os_user_hash=hashlib.sha256(context.os_user.encode("utf-8")).hexdigest(),
            owner_account_id=context.owner_account_id,
            workspace_id=context.workspace_id,
            session_id=context.session_id,
            task_id=context.task_id,
            request_id=context.request_id,
            action_type="tool_permission_decision",
            normalized_action_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            rule_id="",
            rule_scope="",
            permission_profile_hash="",
            additional_permissions_summary="",
            decision=str(decision),
            decision_source=str(decision_source),
            sandbox_backend="",
            capabilities=(),
            network_target_summary="",
            exit_code=None,
            stable_error_code="",
            tool_name=str(tool_name),
            action_summary="",
            action_detail="",
            approval_mode="",
            turn_id=context.turn_id,
            policy_version=PERMISSION_PROFILE_SCHEMA_VERSION,
            build_version=str(__version__),
            model_id=context.model_id,
        )

    @classmethod
    def for_permission(
        cls,
        context: SecurityContext,
        permissions: object,
        *,
        action_type: str,
        decision: str,
        decision_source: str,
        rule_scope: str = "",
        additional_permissions_summary: str = "",
        tool_name: str = "",
        approval_mode: str = "",
    ) -> AuditEvent:
        """Record a capability request without pretending it was a file action."""
        from crew import __version__

        summary = "权限申请"
        detail = f"权限：{additional_permissions_summary}" if additional_permissions_summary else summary
        payload = json.dumps(additional_permissions_summary, ensure_ascii=False, sort_keys=True)
        return cls(
            event_id=uuid4().hex,
            os_user_hash=hashlib.sha256(context.os_user.encode("utf-8")).hexdigest(),
            owner_account_id=context.owner_account_id,
            workspace_id=context.workspace_id,
            session_id=context.session_id,
            task_id=context.task_id,
            request_id=context.request_id,
            action_type=str(action_type),
            normalized_action_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            rule_id="",
            rule_scope=str(rule_scope),
            permission_profile_hash="",
            additional_permissions_summary=str(additional_permissions_summary),
            decision=str(decision),
            decision_source=str(decision_source),
            sandbox_backend="",
            capabilities=(),
            network_target_summary="",
            exit_code=None,
            stable_error_code="",
            tool_name=str(tool_name),
            action_summary=summary,
            action_detail=detail,
            approval_mode=str(approval_mode),
            turn_id=context.turn_id,
            policy_version=PERMISSION_PROFILE_SCHEMA_VERSION,
            build_version=str(__version__),
            model_id=context.model_id,
        )

    @classmethod
    def for_mode_change(
        cls,
        context: SecurityContext,
        *,
        previous_mode: str,
        current_mode: str,
        decision_source: str,
        reason: str,
    ) -> AuditEvent:
        """Create a durable owner/session-bound permission-mode transition."""
        from crew import __version__

        provenance = json.dumps(
            {
                "old": str(previous_mode),
                "new": str(current_mode),
                "reason": str(reason),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        digest = hashlib.sha256(provenance.encode("utf-8")).hexdigest()
        return cls(
            event_id=uuid4().hex,
            os_user_hash=hashlib.sha256(context.os_user.encode("utf-8")).hexdigest(),
            owner_account_id=context.owner_account_id,
            workspace_id=context.workspace_id,
            session_id=context.session_id,
            task_id=context.task_id,
            request_id=context.request_id,
            action_type="security_mode_changed",
            normalized_action_hash=digest,
            rule_id="",
            rule_scope="session",
            permission_profile_hash="",
            additional_permissions_summary=provenance,
            decision=str(current_mode),
            decision_source=str(decision_source),
            sandbox_backend="",
            capabilities=(),
            network_target_summary="",
            exit_code=None,
            stable_error_code="",
            tool_name="security_mode",
            action_summary=f"安全模式：{previous_mode} → {current_mode}",
            action_detail=f"安全模式变更原因：{reason}" if reason else "安全模式变更",
            approval_mode=str(current_mode),
            turn_id=context.turn_id,
            policy_version=PERMISSION_PROFILE_SCHEMA_VERSION,
            build_version=str(__version__),
            model_id=context.model_id,
        )

    @classmethod
    def for_runtime_diagnostic(
        cls,
        context: SecurityContext,
        *,
        status: str,
        component: str = "security-runtime",
        backend: str = "",
        version: str = "",
        manifest_digest: str = "",
        capabilities: tuple[str, ...] = (),
        failure_code: str = "",
        failure_detail: str = "",
    ) -> AuditEvent:
        """Record a sanitized runtime startup/probe diagnostic in the audit chain."""
        from crew import __version__

        component = str(component).strip()[:128] or "security-runtime"
        status = str(status).strip()[:64] or "unknown"
        backend = str(backend).strip()[:128]
        version = str(version).strip()[:128]
        manifest_digest = str(manifest_digest).strip()[:128]
        capability_list = tuple(
            str(item).strip()[:128]
            for item in capabilities
            if str(item).strip()
        )
        failure_code = str(failure_code).strip()[:128]
        failure_detail = str(failure_detail).strip()[:256]
        canonical = json.dumps(
            {
                "component": component,
                "status": status,
                "backend": backend,
                "version": version,
                "manifest_digest": manifest_digest,
                "capabilities": capability_list,
                "failure_code": failure_code,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        summary = f"{component} {status}"
        detail_parts = [
            f"backend={backend}" if backend else "",
            f"version={version}" if version else "",
            (
                f"manifest={manifest_digest[:16]}"
                if len(manifest_digest) == 64 and manifest_digest.isalnum()
                else ""
            ),
            f"failure={failure_code}" if failure_code else "",
        ]
        detail = " ".join(part for part in detail_parts if part) or summary
        if failure_detail:
            detail = f"{detail} detail={failure_detail}"[:1024]
        return cls(
            event_id=uuid4().hex,
            os_user_hash=hashlib.sha256(context.os_user.encode("utf-8")).hexdigest(),
            owner_account_id=context.owner_account_id,
            workspace_id=context.workspace_id,
            session_id=context.session_id,
            task_id=context.task_id,
            request_id=context.request_id,
            action_type="runtime_diagnostic",
            normalized_action_hash=digest,
            rule_id="runtime-boundary",
            rule_scope="runtime-diagnostic",
            permission_profile_hash="",
            additional_permissions_summary="",
            decision=status,
            decision_source="runtime_client",
            sandbox_backend=backend,
            capabilities=capability_list,
            network_target_summary="",
            exit_code=None,
            stable_error_code=failure_code,
            tool_name="security_runtime",
            action_summary=summary,
            action_detail=detail,
            approval_mode="",
            turn_id=context.turn_id,
            policy_version=PERMISSION_PROFILE_SCHEMA_VERSION,
            build_version=str(__version__),
            model_id=context.model_id,
        )


@dataclass(frozen=True)
class AuditRecord(AuditEvent):
    timestamp: float = 0.0
    sequence: int = 0
    previous_mac: str = ""
    event_mac: str = ""
    integrity_key_id: str = ""


class SQLiteSecurityAudit:
    """Write owner-scoped events and buffer only non-critical failures."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        wal_enabled: bool = True,
        max_buffer: int = 100,
        integrity_key: bytes | None = None,
        integrity_key_id: str = "",
        archive_dir: str | Path | None = None,
        event_listener: Callable[[AuditEvent], object] | None = None,
    ) -> None:
        if max_buffer < 1:
            raise ValueError("max_buffer 必须大于 0")
        database_path = Path(db_path)
        key = (
            _validated_integrity_key(integrity_key)
            if integrity_key is not None
            else _load_or_create_integrity_key(
                Path(f"{database_path}.audit-integrity.key")
            )
        )
        key_id = integrity_key_id.strip() or hashlib.sha256(key).hexdigest()[:16]
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key_id):
            raise ValueError("integrity_key_id 必须是安全的非空标识")
        self._lock = threading.Lock()
        self._buffer_lock = threading.Lock()
        self._rotation_lock = threading.Lock()
        self._max_buffer = max_buffer
        self._buffer: list[tuple[AuditEvent, float]] = []
        self._integrity_key = key
        self._integrity_key_id = key_id
        self._archive_dir = (
            Path(archive_dir)
            if archive_dir is not None
            else database_path.parent / "security-audit-archives"
        )
        self._event_listener = event_listener
        self._conn = connect_sqlite(
            database_path,
            wal_enabled=wal_enabled,
            row_factory=True,
        )
        self._writer = SQLiteWriteHelper(self._conn, self._lock)
        missing_audit_columns = self._missing_audit_columns()
        self._writer.execute(self._init_schema)
        if missing_audit_columns and self._audit_event_count():
            self._writer.execute(self._reseal_audit_chain)
        self._writer.execute(self._initialize_chain)

    def _missing_audit_columns(self) -> set[str]:
        try:
            existing = {
                str(row["name"])
                for row in self._conn.execute(
                    "PRAGMA table_info(security_audit_events)"
                ).fetchall()
            }
        except Exception:  # noqa: BLE001 - table absent means nothing to migrate
            return set()
        return {"turn_id", "policy_version", "build_version", "model_id"} - existing

    def _audit_event_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS count FROM security_audit_events"
        ).fetchone()
        return int(row["count"]) if row is not None else 0

    def _reseal_audit_chain(self, conn) -> None:
        """Recompute the MAC chain after adding MAC-covered audit columns."""
        rows = conn.execute(
            "SELECT rowid FROM security_audit_events ORDER BY rowid ASC"
        ).fetchall()
        previous_mac = "0" * 64
        last_sequence = 0
        for row in rows:
            record = conn.execute(
                "SELECT * FROM security_audit_events WHERE rowid = ?",
                (row["rowid"],),
            ).fetchone()
            event_mac = self._event_mac(
                _row_to_event(record),
                timestamp=float(record["timestamp"]),
                sequence=int(record["sequence"]),
                previous_mac=previous_mac,
            )
            conn.execute(
                "UPDATE security_audit_events "
                "SET previous_mac = ?, event_mac = ? WHERE rowid = ?",
                (previous_mac, event_mac, row["rowid"]),
            )
            previous_mac = event_mac
            last_sequence = int(record["sequence"])
        conn.execute(
            "UPDATE security_audit_chain_state "
            "SET last_sequence = ?, last_mac = ?, integrity_key_id = ?, state_mac = ? "
            "WHERE singleton = 1",
            (
                last_sequence,
                previous_mac,
                self._integrity_key_id,
                self._state_mac(last_sequence, previous_mac),
            ),
        )

    @staticmethod
    def _init_schema(conn) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS security_audit_events (
                event_id TEXT PRIMARY KEY,
                timestamp REAL NOT NULL,
                sequence INTEGER NOT NULL,
                previous_mac TEXT NOT NULL,
                event_mac TEXT NOT NULL,
                integrity_key_id TEXT NOT NULL,
                os_user_hash TEXT NOT NULL,
                owner_account_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                normalized_action_hash TEXT NOT NULL,
                rule_id TEXT NOT NULL,
                rule_scope TEXT NOT NULL,
                permission_profile_hash TEXT NOT NULL,
                additional_permissions_summary TEXT NOT NULL,
                decision TEXT NOT NULL,
                decision_source TEXT NOT NULL,
                sandbox_backend TEXT NOT NULL,
                capabilities_json TEXT NOT NULL,
                network_target_summary TEXT NOT NULL,
                exit_code INTEGER,
                stable_error_code TEXT NOT NULL,
                tool_name TEXT NOT NULL DEFAULT '',
                action_summary TEXT NOT NULL DEFAULT '',
                action_detail TEXT NOT NULL DEFAULT '',
                approval_mode TEXT NOT NULL DEFAULT '',
                turn_id TEXT NOT NULL DEFAULT '',
                policy_version TEXT NOT NULL DEFAULT '',
                build_version TEXT NOT NULL DEFAULT '',
                model_id TEXT NOT NULL DEFAULT ''
            )
            """
        )
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(security_audit_events)").fetchall()
        }
        migrations = {
            "tool_name": "TEXT NOT NULL DEFAULT ''",
            "action_summary": "TEXT NOT NULL DEFAULT ''",
            "action_detail": "TEXT NOT NULL DEFAULT ''",
            "approval_mode": "TEXT NOT NULL DEFAULT ''",
            "sequence": "INTEGER NOT NULL DEFAULT 0",
            "previous_mac": "TEXT NOT NULL DEFAULT ''",
            "event_mac": "TEXT NOT NULL DEFAULT ''",
            "integrity_key_id": "TEXT NOT NULL DEFAULT ''",
            "turn_id": "TEXT NOT NULL DEFAULT ''",
            "policy_version": "TEXT NOT NULL DEFAULT ''",
            "build_version": "TEXT NOT NULL DEFAULT ''",
            "model_id": "TEXT NOT NULL DEFAULT ''",
        }
        for column, definition in migrations.items():
            if column not in columns:
                conn.execute(
                    f"ALTER TABLE security_audit_events ADD COLUMN {column} {definition}"
                )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS security_audit_chain_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                last_sequence INTEGER NOT NULL,
                last_mac TEXT NOT NULL,
                integrity_key_id TEXT NOT NULL,
                state_mac TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS security_audit_archives (
                archive_id TEXT PRIMARY KEY,
                first_sequence INTEGER NOT NULL UNIQUE,
                last_sequence INTEGER NOT NULL UNIQUE,
                previous_mac TEXT NOT NULL,
                terminal_mac TEXT NOT NULL,
                integrity_key_id TEXT NOT NULL,
                archive_filename TEXT NOT NULL UNIQUE,
                archive_sha256 TEXT NOT NULL,
                archive_mac TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_security_audit_owner_time "
            "ON security_audit_events(owner_account_id, timestamp DESC)"
        )

    def _initialize_chain(self, conn) -> None:
        state = conn.execute(
            "SELECT * FROM security_audit_chain_state WHERE singleton = 1"
        ).fetchone()
        if state is not None:
            self._verify_state_row(state)
            if state["integrity_key_id"] != self._integrity_key_id:
                raise AuditIntegrityError(
                    "安全审计 HMAC 密钥标识与持久化链不一致"
                )
            self._verify_integrity_connection(conn)
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_security_audit_sequence "
                "ON security_audit_events(sequence)"
            )
            return

        rows = conn.execute(
            "SELECT * FROM security_audit_events ORDER BY rowid ASC"
        ).fetchall()
        archives = conn.execute(
            "SELECT 1 FROM security_audit_archives LIMIT 1"
        ).fetchone()
        if rows or archives is not None:
            raise AuditIntegrityError(
                "安全审计链状态缺失，拒绝为现有事件重新建立可信链"
            )
        sequence = 0
        previous_mac = "0" * 64
        for row in rows:
            sequence += 1
            event = _row_to_event(row)
            event_mac = self._event_mac(
                event,
                timestamp=float(row["timestamp"]),
                sequence=sequence,
                previous_mac=previous_mac,
            )
            conn.execute(
                "UPDATE security_audit_events SET sequence = ?, previous_mac = ?, "
                "event_mac = ?, integrity_key_id = ? WHERE event_id = ?",
                (
                    sequence,
                    previous_mac,
                    event_mac,
                    self._integrity_key_id,
                    event.event_id,
                ),
            )
            previous_mac = event_mac
        conn.execute(
            "INSERT INTO security_audit_chain_state "
            "(singleton, last_sequence, last_mac, integrity_key_id, state_mac) "
            "VALUES (1, ?, ?, ?, ?)",
            (
                sequence,
                previous_mac,
                self._integrity_key_id,
                self._state_mac(sequence, previous_mac),
            ),
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_security_audit_sequence "
            "ON security_audit_events(sequence)"
        )

    def _state_mac(self, sequence: int, last_mac: str) -> str:
        payload = json.dumps(
            {
                "integrity_key_id": self._integrity_key_id,
                "last_mac": last_mac,
                "last_sequence": sequence,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hmac.new(self._integrity_key, payload, hashlib.sha256).hexdigest()

    def _event_mac(
        self,
        event: AuditEvent,
        *,
        timestamp: float,
        sequence: int,
        previous_mac: str,
    ) -> str:
        payload = json.dumps(
            {
                "event": asdict(event),
                "integrity_key_id": self._integrity_key_id,
                "previous_mac": previous_mac,
                "sequence": sequence,
                "timestamp": timestamp,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hmac.new(self._integrity_key, payload, hashlib.sha256).hexdigest()

    def _verify_state_row(self, row) -> None:
        if str(row["integrity_key_id"]) != self._integrity_key_id:
            raise AuditIntegrityError("安全审计链状态 HMAC 密钥标识不一致")
        expected = self._state_mac(int(row["last_sequence"]), str(row["last_mac"]))
        if not hmac.compare_digest(str(row["state_mac"]), expected):
            raise AuditIntegrityError("安全审计链状态 HMAC 校验失败")

    def _insert_event(self, conn, event: AuditEvent, timestamp: float) -> None:
        state = conn.execute(
            "SELECT * FROM security_audit_chain_state WHERE singleton = 1"
        ).fetchone()
        if state is None:
            raise AuditIntegrityError("安全审计链状态缺失")
        self._verify_state_row(state)
        if state["integrity_key_id"] != self._integrity_key_id:
            raise AuditIntegrityError("安全审计 HMAC 密钥标识不一致")
        sequence = int(state["last_sequence"]) + 1
        previous_mac = str(state["last_mac"])
        event_mac = self._event_mac(
            event,
            timestamp=timestamp,
            sequence=sequence,
            previous_mac=previous_mac,
        )
        _insert_event(
            conn,
            event,
            timestamp,
            sequence=sequence,
            previous_mac=previous_mac,
            event_mac=event_mac,
            integrity_key_id=self._integrity_key_id,
        )
        conn.execute(
            "UPDATE security_audit_chain_state SET last_sequence = ?, last_mac = ?, "
            "integrity_key_id = ?, state_mac = ? WHERE singleton = 1",
            (
                sequence,
                event_mac,
                self._integrity_key_id,
                self._state_mac(sequence, event_mac),
            ),
        )

    def record(self, event: AuditEvent, *, timestamp: float | None = None) -> str:
        """Persist an event; critical authorization events never fall back to memory."""
        safe_event = _sanitize_event(event)
        occurred_at = time.time() if timestamp is None else float(timestamp)
        try:
            self._writer.execute(
                lambda conn: self._insert_event(conn, safe_event, occurred_at)
            )
        except AuditIntegrityError as exc:
            self._notify_listener(
                replace(safe_event, stable_error_code="audit_chain_break")
            )
            raise AuditWriteError("安全审计完整性验证失败") from exc
        except Exception as exc:
            # Unknown event types are security-significant by default. Only the
            # explicitly diagnostic classes may degrade into the bounded buffer.
            if safe_event.action_type not in _BUFFERABLE_ACTION_TYPES:
                raise AuditWriteError("安全审计事件持久化失败") from exc
            with self._buffer_lock:
                if len(self._buffer) >= self._max_buffer:
                    raise AuditBufferFullError("安全审计内存缓冲已满") from exc
                self._buffer.append((safe_event, occurred_at))
            _LOGGER.warning(
                "non-critical audit event buffered after persistence failure",
                exc_info=True,
            )
        else:
            # Write succeeded and DB is writable again: opportunistically drain any
            # buffered ordinary events. Best-effort — a flush failure must not undo
            # the successful record above; the buffer stays for the next attempt.
            self._flush_best_effort()
        self._notify_listener(safe_event)
        return safe_event.event_id

    def set_event_listener(
        self,
        listener: Callable[[AuditEvent], object] | None,
    ) -> None:
        """Replace the alert/side-channel observer without reopening the audit DB."""
        self._event_listener = listener

    def _notify_listener(self, event: AuditEvent) -> None:
        listener = self._event_listener
        if listener is None:
            return
        try:
            listener(event)
        except Exception:  # noqa: BLE001 - alerting must never break the audit chain
            _LOGGER.exception("security audit event listener failed")

    def record_permission(
        self,
        context: SecurityContext,
        requested_permissions: object,
        *,
        action_type: str,
        decision: str,
        decision_source: str,
        rule_scope: str = "",
        approval_mode: str = "",
        tool_name: str = "",
        granted_permissions: object | None = None,
        reason: str = "",
    ) -> str:
        """Persist a capability lifecycle event with requested/granted provenance."""
        from crew.security.policy import serialize_additional_permissions

        summary = json.dumps(
            {
                "requested": serialize_additional_permissions(requested_permissions),
                "granted": serialize_additional_permissions(granted_permissions)
                if granted_permissions is not None
                else {},
                "reason": str(reason),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return self.record(
            AuditEvent.for_permission(
                context,
                requested_permissions,
                action_type=action_type,
                decision=decision,
                decision_source=decision_source,
                rule_scope=rule_scope,
                additional_permissions_summary=summary,
                tool_name=tool_name,
                approval_mode=approval_mode,
            )
        )

    def _flush_best_effort(self) -> None:
        """Drain the ordinary-event buffer without surfacing failures to the caller."""
        try:
            self.flush()
        except AuditWriteError:
            _LOGGER.debug("opportunistic audit flush failed, buffer retained", exc_info=True)

    def flush(self) -> int:
        with self._buffer_lock:
            pending = list(self._buffer)
            if not pending:
                return 0
            try:
                self._writer.execute(
                    lambda conn: [
                        self._insert_event(conn, event, timestamp)
                        for event, timestamp in pending
                    ]
                )
            except Exception as exc:
                raise AuditWriteError("安全审计缓冲刷新失败") from exc
            del self._buffer[: len(pending)]
        return len(pending)

    def query(
        self,
        *,
        owner_account_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditRecord]:
        owner = _sanitize_identity(owner_account_id)
        if not owner:
            raise ValueError("owner_account_id 不能为空")
        bounded_limit = max(1, min(int(limit), 100))
        bounded_offset = max(0, int(offset))
        with self._lock:
            self._verify_integrity_connection(self._conn)
            rows = self._conn.execute(
                "SELECT * FROM security_audit_events WHERE owner_account_id = ? "
                "ORDER BY sequence DESC LIMIT ? OFFSET ?",
                (owner, bounded_limit, bounded_offset),
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def query_page(
        self,
        *,
        owner_account_id: str,
        limit: int = 100,
        offset: int = 0,
        action_type: str = "",
        decision: str = "",
        session_id: str = "",
        workspace_id: str = "",
        task_id: str = "",
        start_time: float | None = None,
        end_time: float | None = None,
        sort: str = "newest",
    ) -> tuple[list[AuditRecord], int]:
        """Return one owner-scoped audit page and its total from one read snapshot."""
        owner = _sanitize_identity(owner_account_id)
        if not owner:
            raise ValueError("owner_account_id 不能为空")
        bounded_limit = max(1, min(int(limit), 100))
        bounded_offset = max(0, int(offset))
        filters = ["owner_account_id = ?"]
        params: list[object] = [owner]
        for column, value in (
            ("action_type", action_type),
            ("decision", decision),
        ):
            normalized = str(value).strip()
            if normalized:
                filters.append(f"{column} = ?")
                params.append(normalized)
        normalized_session = _sanitize_identity(session_id) if str(session_id).strip() else ""
        if normalized_session:
            escaped_session = (
                normalized_session.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            filters.append("session_id LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped_session}%")
        for column, value in (
            ("workspace_id", workspace_id),
            ("task_id", task_id),
        ):
            if str(value).strip():
                filters.append(f"{column} = ?")
                params.append(_sanitize_identity(value))
        normalized_start = float(start_time) if start_time is not None else None
        normalized_end = float(end_time) if end_time is not None else None
        if normalized_start is not None and not math.isfinite(normalized_start):
            raise ValueError("audit start_time must be finite")
        if normalized_end is not None and not math.isfinite(normalized_end):
            raise ValueError("audit end_time must be finite")
        if (
            normalized_start is not None
            and normalized_end is not None
            and normalized_start > normalized_end
        ):
            raise ValueError("audit time range is invalid")
        if normalized_start is not None:
            filters.append("timestamp >= ?")
            params.append(normalized_start)
        if normalized_end is not None:
            filters.append("timestamp <= ?")
            params.append(normalized_end)
        where = " AND ".join(filters)
        order = "ASC" if sort == "oldest" else "DESC"
        with self._lock:
            self._verify_integrity_connection(self._conn)
            total = int(
                self._conn.execute(
                    f"SELECT COUNT(*) FROM security_audit_events WHERE {where}",
                    tuple(params),
                ).fetchone()[0]
            )
            rows = self._conn.execute(
                f"SELECT * FROM security_audit_events WHERE {where} "
                f"ORDER BY sequence {order} LIMIT ? OFFSET ?",
                (*params, bounded_limit, bounded_offset),
            ).fetchall()
        return [_row_to_record(row) for row in rows], total

    def verify_integrity(self) -> None:
        """Authenticate the complete persisted sequence and fail on any mutation."""
        with self._lock:
            self._verify_integrity_connection(self._conn)

    def _verify_integrity_connection(self, conn) -> None:
        state = conn.execute(
            "SELECT * FROM security_audit_chain_state WHERE singleton = 1"
        ).fetchone()
        if state is None:
            raise AuditIntegrityError("安全审计链状态缺失")
        self._verify_state_row(state)
        previous_mac = "0" * 64
        expected_sequence = 1
        archives = conn.execute(
            "SELECT * FROM security_audit_archives ORDER BY first_sequence ASC"
        ).fetchall()
        for archive in archives:
            first_sequence = int(archive["first_sequence"])
            last_sequence = int(archive["last_sequence"])
            if first_sequence != expected_sequence or last_sequence < first_sequence:
                raise AuditIntegrityError("audit archive sequence 不连续")
            if archive["integrity_key_id"] != self._integrity_key_id:
                raise AuditIntegrityError("audit archive HMAC 密钥标识不一致")
            if not hmac.compare_digest(
                str(archive["previous_mac"]),
                previous_mac,
            ):
                raise AuditIntegrityError("audit archive 链前驱不一致")
            filename = str(archive["archive_filename"])
            if Path(filename).name != filename or not filename.endswith(".json"):
                raise AuditIntegrityError("audit archive 文件名不安全")
            archive_path = self._archive_dir / filename
            try:
                _verify_integrity_path(archive_path)
                raw = archive_path.read_bytes()
            except PermissionError as exc:
                raise AuditIntegrityError("audit archive 权限不安全") from exc
            except OSError as exc:
                raise AuditIntegrityError("audit archive 文件缺失") from exc
            if len(raw) > 512 * 1024 * 1024:
                raise AuditIntegrityError("audit archive 文件超过验证上限")
            if not hmac.compare_digest(
                hashlib.sha256(raw).hexdigest(),
                str(archive["archive_sha256"]),
            ):
                raise AuditIntegrityError("audit archive 文件摘要不一致")
            try:
                payload = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AuditIntegrityError("audit archive JSON 无效") from exc
            if not isinstance(payload, dict):
                raise AuditIntegrityError("audit archive 根节点无效")
            archive_mac = str(payload.pop("archive_mac", ""))
            expected_archive_mac = self._archive_mac(payload)
            if (
                not hmac.compare_digest(archive_mac, expected_archive_mac)
                or not hmac.compare_digest(archive_mac, str(archive["archive_mac"]))
            ):
                raise AuditIntegrityError("audit archive HMAC 校验失败")
            expected_metadata = {
                "schema": "ace.security.audit-archive.v1",
                "archive_filename": filename,
                "first_sequence": first_sequence,
                "last_sequence": last_sequence,
                "previous_mac": previous_mac,
                "terminal_mac": str(archive["terminal_mac"]),
                "integrity_key_id": self._integrity_key_id,
            }
            if any(payload.get(key) != value for key, value in expected_metadata.items()):
                raise AuditIntegrityError("audit archive 元数据不一致")
            records = payload.get("records")
            if not isinstance(records, list) or len(records) != last_sequence - first_sequence + 1:
                raise AuditIntegrityError("audit archive 记录数量不一致")
            for record in records:
                if not isinstance(record, dict):
                    raise AuditIntegrityError("audit archive 记录结构无效")
                sequence, previous_mac = self._verify_record_mapping(
                    record,
                    expected_sequence=expected_sequence,
                    previous_mac=previous_mac,
                    source="audit archive",
                )
                expected_sequence = sequence + 1
            if not hmac.compare_digest(previous_mac, str(archive["terminal_mac"])):
                raise AuditIntegrityError("audit archive 尾部 HMAC 不一致")

        for row in conn.execute(
            "SELECT * FROM security_audit_events ORDER BY sequence ASC"
        ).fetchall():
            record = asdict(_row_to_record(row))
            sequence, previous_mac = self._verify_record_mapping(
                record,
                expected_sequence=expected_sequence,
                previous_mac=previous_mac,
                source="安全审计",
            )
            expected_sequence = sequence + 1
        last_sequence = expected_sequence - 1
        if (
            int(state["last_sequence"]) != last_sequence
            or not hmac.compare_digest(str(state["last_mac"]), previous_mac)
        ):
            raise AuditIntegrityError("安全审计链状态与事件尾部不一致")

    def _verify_record_mapping(
        self,
        record: dict[str, object],
        *,
        expected_sequence: int,
        previous_mac: str,
        source: str,
    ) -> tuple[int, str]:
        try:
            sequence = int(record["sequence"])
            timestamp = float(record["timestamp"])
            record_previous_mac = str(record["previous_mac"])
            event_mac = str(record["event_mac"])
            key_id = str(record["integrity_key_id"])
            event = _mapping_to_event(record)
        except (KeyError, TypeError, ValueError) as exc:
            raise AuditIntegrityError(f"{source} 记录结构无效") from exc
        if sequence != expected_sequence:
            raise AuditIntegrityError(f"{source} sequence 不连续")
        if key_id != self._integrity_key_id:
            raise AuditIntegrityError(f"{source} HMAC 密钥标识不一致")
        if not hmac.compare_digest(record_previous_mac, previous_mac):
            raise AuditIntegrityError(f"{source} HMAC 链前驱不一致")
        expected_mac = self._event_mac(
            event,
            timestamp=timestamp,
            sequence=sequence,
            previous_mac=previous_mac,
        )
        if not hmac.compare_digest(event_mac, expected_mac):
            raise AuditIntegrityError(f"{source} 事件 HMAC 校验失败")
        return sequence, expected_mac

    def _archive_mac(self, payload: dict[str, object]) -> str:
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hmac.new(self._integrity_key, canonical, hashlib.sha256).hexdigest()

    def export_jsonl(self, *, owner_account_id: str) -> str:
        records: list[AuditRecord] = []
        offset = 0
        while True:
            page = self.query(owner_account_id=owner_account_id, limit=100, offset=offset)
            records.extend(page)
            if len(page) < 100:
                break
            offset += len(page)
        return "\n".join(
            json.dumps(asdict(record), ensure_ascii=False, sort_keys=True) for record in records
        )

    def purge_expired(
        self,
        *,
        now: float | None = None,
        owner_account_id: str | None = None,
        workspace_id: str | None = None,
    ) -> int:
        """Rotate one contiguous expired prefix into an authenticated archive."""
        cutoff = (time.time() if now is None else float(now)) - _RETENTION_SECONDS
        # The authenticated chain is global. Select only a contiguous prefix;
        # deleting scoped rows in the middle would create unverifiable holes. A
        # caller-scoped rotation stops before the first row outside its authority.
        owner = str(owner_account_id or "").strip()
        workspace = str(workspace_id or "").strip()
        with self._rotation_lock:
            with self._lock:
                candidates = self._conn.execute(
                    "SELECT * FROM security_audit_events ORDER BY sequence ASC"
                ).fetchall()
            rows = []
            for row in candidates:
                if float(row["timestamp"]) >= cutoff:
                    break
                if owner and str(row["owner_account_id"]) != owner:
                    break
                if workspace and str(row["workspace_id"]) != workspace:
                    break
                rows.append(row)
            if not rows:
                return 0
            return self._archive_rows(rows)

    def rotate(self, *, max_live_events: int) -> int:
        """Rotate the oldest records while retaining a bounded live SQLite tail."""
        if max_live_events < 1:
            raise ValueError("max_live_events 必须大于 0")
        with self._rotation_lock:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM security_audit_events ORDER BY sequence ASC"
                ).fetchall()
            overflow = len(rows) - max_live_events
            if overflow <= 0:
                return 0
            return self._archive_rows(rows[:overflow])

    def _archive_rows(self, rows) -> int:
        try:
            with self._lock:
                self._verify_integrity_connection(self._conn)
        except Exception as exc:
            raise AuditWriteError(
                "安全审计完整性验证失败，拒绝 archive 旋转"
            ) from exc

        records = [_row_to_record(row) for row in rows]
        first = records[0]
        last = records[-1]
        filename = f"audit-{first.sequence:020d}-{last.sequence:020d}.json"
        payload: dict[str, object] = {
            "schema": "ace.security.audit-archive.v1",
            "archive_filename": filename,
            "first_sequence": first.sequence,
            "last_sequence": last.sequence,
            "previous_mac": first.previous_mac,
            "terminal_mac": last.event_mac,
            "integrity_key_id": self._integrity_key_id,
            "records": [asdict(record) for record in records],
        }
        archive_mac = self._archive_mac(payload)
        serialized = json.dumps(
            {**payload, "archive_mac": archive_mac},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8") + b"\n"
        _ensure_private_directory(self._archive_dir)
        archive_path = self._archive_dir / filename
        _write_private_file_atomic(archive_path, serialized)
        archive_sha256 = hashlib.sha256(serialized).hexdigest()

        def _commit(conn) -> int:
            self._verify_integrity_connection(conn)
            current = conn.execute(
                "SELECT * FROM security_audit_events "
                "WHERE sequence BETWEEN ? AND ? ORDER BY sequence ASC",
                (first.sequence, last.sequence),
            ).fetchall()
            if [_row_to_record(row) for row in current] != records:
                raise AuditIntegrityError("audit archive 旋转期间记录发生变化")
            conn.execute(
                "INSERT INTO security_audit_archives "
                "(archive_id, first_sequence, last_sequence, previous_mac, terminal_mac, "
                "integrity_key_id, archive_filename, archive_sha256, archive_mac) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    hashlib.sha256(
                        f"{first.sequence}:{last.sequence}:{archive_mac}".encode("utf-8")
                    ).hexdigest(),
                    first.sequence,
                    last.sequence,
                    first.previous_mac,
                    last.event_mac,
                    self._integrity_key_id,
                    filename,
                    archive_sha256,
                    archive_mac,
                ),
            )
            cursor = conn.execute(
                "DELETE FROM security_audit_events WHERE sequence BETWEEN ? AND ?",
                (first.sequence, last.sequence),
            )
            if cursor.rowcount != len(records):
                raise AuditIntegrityError("audit archive 旋转删除数量不一致")
            return cursor.rowcount

        try:
            return self._writer.execute(_commit)
        except Exception as exc:
            raise AuditWriteError("安全审计 archive 旋转提交失败") from exc

    def close(self) -> None:
        # Best-effort final drain of buffered ordinary events before teardown; a
        # failure here is non-critical (these are by definition non-durable events).
        try:
            self.flush()
        except AuditWriteError:
            _LOGGER.debug("final audit flush failed on close, buffer dropped", exc_info=True)
        with self._lock:
            self._conn.close()


def _sanitize_event(event: AuditEvent) -> AuditEvent:
    return replace(
        event,
        event_id=_sanitize_event_id(event.event_id),
        os_user_hash=_sanitize_digest(event.os_user_hash),
        owner_account_id=_sanitize_identity(event.owner_account_id),
        workspace_id=_sanitize_identity(event.workspace_id),
        session_id=_sanitize_identity(event.session_id),
        task_id=_sanitize_identity(event.task_id),
        request_id=_sanitize_identity(event.request_id),
        action_type=_sanitize_field(event.action_type, 256),
        normalized_action_hash=_sanitize_digest(event.normalized_action_hash),
        rule_id=_sanitize_field(event.rule_id, 512),
        rule_scope=_sanitize_field(event.rule_scope, 256),
        permission_profile_hash=_sanitize_digest(
            event.permission_profile_hash,
            allow_empty=True,
        ),
        additional_permissions_summary=_sanitize_field(
            event.additional_permissions_summary,
            2000,
        ),
        decision=_sanitize_field(event.decision, 1000),
        decision_source=_sanitize_field(event.decision_source, 1000),
        sandbox_backend=_sanitize_field(event.sandbox_backend, 1000),
        capabilities=tuple(
            _sanitize_field(item, 100)
            for item in event.capabilities[:100]
        ),
        network_target_summary=_sanitize_field(event.network_target_summary, 1000),
        stable_error_code=_sanitize_field(event.stable_error_code, 1000),
        tool_name=_sanitize_field(event.tool_name, 500),
        action_summary=_sanitize_field(event.action_summary, 500),
        action_detail=_sanitize_field(event.action_detail, 4000),
        approval_mode=_sanitize_field(event.approval_mode, 500),
        turn_id=_sanitize_identity(event.turn_id),
        policy_version=_sanitize_field(event.policy_version, 256),
        build_version=_sanitize_field(event.build_version, 256),
        model_id=_sanitize_field(event.model_id, 256),
    )


def _sanitize_field(value: object, limit: int) -> str:
    return redact_sensitive_display_text(str(value)[:limit])


def _sanitize_identity(value: object) -> str:
    raw = str(value).strip()
    safe = _sanitize_field(raw, 256)
    if safe != raw or len(raw) > 256:
        return f"sha256:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"
    return safe


def _sanitize_event_id(value: object) -> str:
    raw = str(value).strip()
    safe = _sanitize_field(raw, 128)
    if safe == raw and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", raw):
        return raw
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _sanitize_digest(value: object, *, allow_empty: bool = False) -> str:
    raw = str(value).strip()
    if allow_empty and not raw:
        return ""
    if re.fullmatch(r"[0-9A-Fa-f]{64}", raw):
        return raw.lower()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _validated_integrity_key(value: bytes) -> bytes:
    key = bytes(value)
    if len(key) < 32:
        raise ValueError("安全审计 HMAC 密钥至少需要 32 字节")
    return key


def _load_or_create_integrity_key(path: Path) -> bytes:
    """Load an owner-protected local key, creating it atomically when absent."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags | no_follow)
    except FileNotFoundError:
        create_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | no_follow
        )
        key = secrets.token_bytes(32)
        try:
            descriptor = os.open(path, create_flags, 0o600)
        except FileExistsError:
            descriptor = os.open(path, flags | no_follow)
        else:
            try:
                written = os.write(descriptor, key)
                if written != len(key):
                    raise OSError("安全审计 HMAC 密钥写入不完整")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            _protect_integrity_path(path)
            return key

    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PermissionError("安全审计 HMAC 密钥必须是单链接普通文件")
        key = os.read(descriptor, 4096)
        if os.read(descriptor, 1):
            raise PermissionError("安全审计 HMAC 密钥文件过大")
    finally:
        os.close(descriptor)
    _verify_integrity_path(path)
    return _validated_integrity_key(key)


def _protect_integrity_path(path: Path) -> None:
    if os.name == "nt":
        from crew.gateway.windows_acl import protect_path

        protect_path(path, directory=False)
    else:
        path.chmod(0o600)
    _verify_integrity_path(path)


def _verify_integrity_path(path: Path) -> None:
    if os.name == "nt":
        from crew.gateway.windows_acl import path_is_secure

        if not path_is_secure(path, directory=False):
            raise PermissionError("安全审计 HMAC 密钥 DACL 不安全")
        return
    info = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
        or (hasattr(os, "getuid") and info.st_uid != os.getuid())
    ):
        raise PermissionError("安全审计 HMAC 密钥权限必须为 owner-only")


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        from crew.gateway.windows_acl import path_is_secure, protect_path

        protect_path(path, directory=True)
        if not path_is_secure(path, directory=True):
            raise PermissionError("安全审计 archive 目录 DACL 不安全")
        return
    path.chmod(0o700)
    info = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or (hasattr(os, "getuid") and info.st_uid != os.getuid())
    ):
        raise PermissionError("安全审计 archive 目录权限必须为 owner-only")


def _write_private_file_atomic(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.read_bytes() != payload:
            raise AuditIntegrityError("audit archive 目标已存在且内容不一致")
        _verify_integrity_path(path)
        return
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(temporary, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("安全审计 archive 写入不完整")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        _protect_integrity_path(temporary)
        if path.exists():
            if path.read_bytes() != payload:
                raise AuditIntegrityError("audit archive 并发目标内容不一致")
            temporary.unlink()
        else:
            os.replace(temporary, path)
        _protect_integrity_path(path)
        if os.name == "posix":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _insert_event(
    conn,
    event: AuditEvent,
    timestamp: float,
    *,
    sequence: int,
    previous_mac: str,
    event_mac: str,
    integrity_key_id: str,
) -> None:
    conn.execute(
        "INSERT INTO security_audit_events "
        "(event_id, timestamp, sequence, previous_mac, event_mac, integrity_key_id, "
        "os_user_hash, owner_account_id, workspace_id, "
        "session_id, task_id, request_id, action_type, normalized_action_hash, "
        "rule_id, rule_scope, permission_profile_hash, additional_permissions_summary, "
        "decision, decision_source, sandbox_backend, capabilities_json, "
        "network_target_summary, exit_code, stable_error_code, tool_name, "
        "action_summary, action_detail, approval_mode, turn_id, policy_version, "
        "build_version, model_id) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event.event_id,
            timestamp,
            sequence,
            previous_mac,
            event_mac,
            integrity_key_id,
            event.os_user_hash,
            event.owner_account_id,
            event.workspace_id,
            event.session_id,
            event.task_id,
            event.request_id,
            event.action_type,
            event.normalized_action_hash,
            event.rule_id,
            event.rule_scope,
            event.permission_profile_hash,
            event.additional_permissions_summary,
            event.decision,
            event.decision_source,
            event.sandbox_backend,
            json.dumps(event.capabilities, ensure_ascii=False),
            event.network_target_summary,
            event.exit_code,
            event.stable_error_code,
            event.tool_name,
            event.action_summary,
            event.action_detail,
            event.approval_mode,
            event.turn_id,
            event.policy_version,
            event.build_version,
            event.model_id,
        ),
    )


def _row_to_event(row) -> AuditEvent:
    return AuditEvent(
        event_id=row["event_id"],
        os_user_hash=row["os_user_hash"],
        owner_account_id=row["owner_account_id"],
        workspace_id=row["workspace_id"],
        session_id=row["session_id"],
        task_id=row["task_id"],
        request_id=row["request_id"],
        action_type=row["action_type"],
        normalized_action_hash=row["normalized_action_hash"],
        rule_id=row["rule_id"],
        rule_scope=row["rule_scope"],
        permission_profile_hash=row["permission_profile_hash"],
        additional_permissions_summary=row["additional_permissions_summary"],
        decision=row["decision"],
        decision_source=row["decision_source"],
        sandbox_backend=row["sandbox_backend"],
        capabilities=tuple(json.loads(row["capabilities_json"])),
        network_target_summary=row["network_target_summary"],
        exit_code=row["exit_code"],
        stable_error_code=row["stable_error_code"],
        tool_name=row["tool_name"],
        action_summary=row["action_summary"],
        action_detail=row["action_detail"],
        approval_mode=row["approval_mode"],
        turn_id=str(row["turn_id"]),
        policy_version=str(row["policy_version"]),
        build_version=str(row["build_version"]),
        model_id=str(row["model_id"]),
    )


def _mapping_to_event(value: dict[str, object]) -> AuditEvent:
    exit_code = value.get("exit_code")
    return AuditEvent(
        event_id=str(value["event_id"]),
        os_user_hash=str(value["os_user_hash"]),
        owner_account_id=str(value["owner_account_id"]),
        workspace_id=str(value["workspace_id"]),
        session_id=str(value["session_id"]),
        task_id=str(value["task_id"]),
        request_id=str(value["request_id"]),
        action_type=str(value["action_type"]),
        normalized_action_hash=str(value["normalized_action_hash"]),
        rule_id=str(value["rule_id"]),
        rule_scope=str(value["rule_scope"]),
        permission_profile_hash=str(value["permission_profile_hash"]),
        additional_permissions_summary=str(value["additional_permissions_summary"]),
        decision=str(value["decision"]),
        decision_source=str(value["decision_source"]),
        sandbox_backend=str(value["sandbox_backend"]),
        capabilities=tuple(str(item) for item in value["capabilities"]),  # type: ignore[arg-type]
        network_target_summary=str(value["network_target_summary"]),
        exit_code=None if exit_code is None else int(exit_code),
        stable_error_code=str(value["stable_error_code"]),
        tool_name=str(value["tool_name"]),
        action_summary=str(value["action_summary"]),
        action_detail=str(value["action_detail"]),
        approval_mode=str(value["approval_mode"]),
        turn_id=str(value.get("turn_id", "")),
        policy_version=str(value.get("policy_version", "")),
        build_version=str(value.get("build_version", "")),
        model_id=str(value.get("model_id", "")),
    )


def _row_to_record(row) -> AuditRecord:
    return AuditRecord(
        **asdict(_row_to_event(row)),
        timestamp=float(row["timestamp"]),
        sequence=int(row["sequence"]),
        previous_mac=row["previous_mac"],
        event_mac=row["event_mac"],
        integrity_key_id=row["integrity_key_id"],
    )


def format_action_for_audit(action: NormalizedAction) -> tuple[str, str]:
    """Return bounded human-readable text; persistence applies forced redaction again."""
    if action.kind is ActionKind.EXEC:
        command = action.raw_command or " ".join(action.argv)
        detail = [f"具体命令：{command}"]
        if action.raw_command and action.argv:
            detail.append(
                "最终执行参数："
                + json.dumps(
                    _redact_cli_argv(action.argv),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        if action.cwd:
            detail.append(f"工作目录：{action.cwd}")
        summary, action_detail = _redact_action_text(
            f"执行命令：{command}",
            "\n".join(detail),
        )
        return summary[:180], action_detail
    if action.kind is ActionKind.FILE:
        operation = {
            "read": "读取文件",
            "write": "写入文件",
            "patch": "修改文件",
            "delete": "删除文件",
        }.get(action.operation, "文件操作")
        detail = [f"文件：{action.path}", f"操作：{operation}"]
        if action.offset:
            detail.append(f"偏移：{action.offset}")
        if action.limit:
            detail.append(f"长度：{action.limit}")
        return _redact_action_text(f"{operation}：{action.path}", "\n".join(detail))
    target = f"{action.protocol}://{action.host}:{action.port}"
    method = f"{action.method} " if action.method else ""
    return _redact_action_text(
        f"联网访问：{method}{target}",
        f"联网目标：{method}{target}",
    )


def _redact_cli_argv(argv: tuple[str, ...]) -> list[str]:
    """Mask values paired with canonical secret-bearing command options."""
    redacted: list[str] = []
    redact_next = False
    for token in argv:
        if redact_next:
            redacted.append("***")
            redact_next = False
            continue
        option, separator, _value = token.partition("=")
        normalized = option.lstrip("-").lower().replace("_", "-")
        if normalized in _SENSITIVE_CLI_OPTION_NAMES:
            if separator:
                redacted.append(f"{option}=***")
            else:
                redacted.append(token)
                redact_next = True
            continue
        redacted.append(redact_sensitive_display_text(token))
    return redacted


def _redact_action_text(summary: str, detail: str) -> tuple[str, str]:
    """Apply shape-based and option-name redaction before an AuditEvent can escape."""

    def redact(value: str) -> str:
        value = _SENSITIVE_CLI_ASSIGNMENT_RE.sub(r"\1\2***", value)
        value = _SENSITIVE_HTTP_HEADER_RE.sub(r"\1***", value)
        return redact_sensitive_display_text(value)

    return redact(summary), redact(detail)
