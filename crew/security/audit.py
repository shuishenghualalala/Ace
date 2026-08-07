"""Minimal structured security audit using the existing SQLite helpers."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from uuid import uuid4

from crew.security.actions import ActionKind, NormalizedAction
from crew.security.context import SecurityContext
from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite
from crew.tools.redact import redact_sensitive_display_text, redact_sensitive_text

_RETENTION_SECONDS = 30 * 86_400
_DURABLE_ACTION_TYPES = {
    "approval_decision",
    "rule_created",
    "rule_disabled",
    "rule_deleted",
    "secret_injection",
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
    ) -> AuditEvent:
        """Create an event with a compact summary and a forcibly redacted action detail."""
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
    ) -> AuditEvent:
        """Create a durable rule lifecycle event without exposing rule payloads.

        Rule creation can carry the already-redacted approval action so the lifecycle
        record remains understandable without requiring a second, approximate lookup.
        Disable/delete events intentionally stay rule-id-only because they have no new
        approved action attached to them.
        """
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
        )


@dataclass(frozen=True)
class AuditRecord(AuditEvent):
    timestamp: float = 0.0


class SQLiteSecurityAudit:
    """Write owner-scoped events and buffer only non-critical failures."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        wal_enabled: bool = True,
        max_buffer: int = 100,
    ) -> None:
        if max_buffer < 1:
            raise ValueError("max_buffer 必须大于 0")
        self._lock = threading.Lock()
        self._buffer_lock = threading.Lock()
        self._max_buffer = max_buffer
        self._buffer: list[tuple[AuditEvent, float]] = []
        self._conn = connect_sqlite(db_path, wal_enabled=wal_enabled, row_factory=True)
        self._writer = SQLiteWriteHelper(self._conn, self._lock)
        self._writer.execute(self._init_schema)

    @staticmethod
    def _init_schema(conn) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS security_audit_events (
                event_id TEXT PRIMARY KEY,
                timestamp REAL NOT NULL,
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
                approval_mode TEXT NOT NULL DEFAULT ''
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
        }
        for column, definition in migrations.items():
            if column not in columns:
                conn.execute(
                    f"ALTER TABLE security_audit_events ADD COLUMN {column} {definition}"
                )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_security_audit_owner_time "
            "ON security_audit_events(owner_account_id, timestamp DESC)"
        )

    def record(self, event: AuditEvent, *, timestamp: float | None = None) -> str:
        """Persist an event; critical authorization events never fall back to memory."""
        safe_event = _sanitize_event(event)
        occurred_at = time.time() if timestamp is None else float(timestamp)
        try:
            self._writer.execute(lambda conn: _insert_event(conn, safe_event, occurred_at))
        except Exception as exc:
            if safe_event.action_type in _DURABLE_ACTION_TYPES:
                raise AuditWriteError("安全审计事件持久化失败") from exc
            with self._buffer_lock:
                if len(self._buffer) >= self._max_buffer:
                    raise AuditBufferFullError("安全审计内存缓冲已满") from exc
                self._buffer.append((safe_event, occurred_at))
        else:
            # Write succeeded and DB is writable again: opportunistically drain any
            # buffered ordinary events. Best-effort — a flush failure must not undo
            # the successful record above; the buffer stays for the next attempt.
            self._flush_best_effort()
        return safe_event.event_id

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
                        _insert_event(conn, event, timestamp) for event, timestamp in pending
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
        owner = str(owner_account_id).strip()
        if not owner:
            raise ValueError("owner_account_id 不能为空")
        bounded_limit = max(1, min(int(limit), 100))
        bounded_offset = max(0, int(offset))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM security_audit_events WHERE owner_account_id = ? "
                "ORDER BY timestamp DESC, event_id DESC LIMIT ? OFFSET ?",
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
        sort: str = "newest",
    ) -> tuple[list[AuditRecord], int]:
        """Return one owner-scoped audit page and its total from one read snapshot."""
        owner = str(owner_account_id).strip()
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
        normalized_session = str(session_id).strip()
        if normalized_session:
            escaped_session = (
                normalized_session.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            filters.append("session_id LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped_session}%")
        where = " AND ".join(filters)
        order = "ASC" if sort == "oldest" else "DESC"
        with self._lock:
            total = int(
                self._conn.execute(
                    f"SELECT COUNT(*) FROM security_audit_events WHERE {where}",
                    tuple(params),
                ).fetchone()[0]
            )
            rows = self._conn.execute(
                f"SELECT * FROM security_audit_events WHERE {where} "
                f"ORDER BY timestamp {order}, event_id {order} LIMIT ? OFFSET ?",
                (*params, bounded_limit, bounded_offset),
            ).fetchall()
        return [_row_to_record(row) for row in rows], total

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
    ) -> int:
        """Purge retention-expired records globally or for one authenticated owner."""
        cutoff = (time.time() if now is None else float(now)) - _RETENTION_SECONDS
        owner = str(owner_account_id or "").strip()

        def _write(conn) -> int:
            if owner:
                cursor = conn.execute(
                    "DELETE FROM security_audit_events "
                    "WHERE timestamp < ? AND owner_account_id = ?",
                    (cutoff, owner),
                )
            else:
                cursor = conn.execute(
                    "DELETE FROM security_audit_events WHERE timestamp < ?", (cutoff,)
                )
            return cursor.rowcount

        return self._writer.execute(_write)

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
        additional_permissions_summary=redact_sensitive_text(
            event.additional_permissions_summary[:2000], force=True
        ),
        network_target_summary=redact_sensitive_text(event.network_target_summary[:1000], force=True),
        capabilities=tuple(item[:100] for item in event.capabilities[:100]),
        action_summary=redact_sensitive_display_text(event.action_summary)[:500],
        action_detail=redact_sensitive_display_text(event.action_detail)[:4000],
    )


def _insert_event(conn, event: AuditEvent, timestamp: float) -> None:
    conn.execute(
        "INSERT INTO security_audit_events "
        "(event_id, timestamp, os_user_hash, owner_account_id, workspace_id, "
        "session_id, task_id, request_id, action_type, normalized_action_hash, "
        "rule_id, rule_scope, permission_profile_hash, additional_permissions_summary, "
        "decision, decision_source, sandbox_backend, capabilities_json, "
        "network_target_summary, exit_code, stable_error_code, tool_name, "
        "action_summary, action_detail, approval_mode) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event.event_id,
            timestamp,
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
        ),
    )


def _row_to_record(row) -> AuditRecord:
    return AuditRecord(
        event_id=row["event_id"],
        timestamp=row["timestamp"],
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
    return _redact_action_text(f"联网访问：{target}", f"联网目标：{target}")


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
