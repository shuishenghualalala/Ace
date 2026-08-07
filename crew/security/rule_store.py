"""Owner/project-scoped persistence for explicit always rules."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from crew.security.actions import ActionKind
from crew.security.rules import ActionRule, RuleDecision, RuleScope
from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite

_SCHEMA_VERSION = 1
_MAX_ACTION_SUMMARY_LENGTH = 500
_MAX_ACTION_DETAIL_LENGTH = 4000


class RuleStoreCorruptError(RuntimeError):
    """Raised when persisted authorization data cannot be interpreted safely."""


class SQLiteRuleStore:
    """Persist immutable always rules using the existing SQLite write discipline."""

    def __init__(self, db_path: str | Path, *, wal_enabled: bool = True) -> None:
        self._lock = threading.Lock()
        self._conn = connect_sqlite(db_path, wal_enabled=wal_enabled, row_factory=True)
        self._writer = SQLiteWriteHelper(self._conn, self._lock)
        self._writer.execute(self._init_schema)

    @staticmethod
    def _init_schema(conn) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS security_rules (
                rule_id          TEXT PRIMARY KEY,
                os_user          TEXT NOT NULL,
                owner_account_id TEXT NOT NULL,
                workspace_id     TEXT NOT NULL,
                schema_version   INTEGER NOT NULL,
                payload_json     TEXT NOT NULL,
                enabled          INTEGER NOT NULL DEFAULT 1,
                created_at       REAL NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_security_rules_scope "
            "ON security_rules(os_user, owner_account_id, workspace_id, enabled)"
        )

    def create(
        self,
        rule: ActionRule,
        *,
        os_user: str,
        owner_account_id: str,
        workspace_id: str,
    ) -> ActionRule:
        """Insert one immutable always rule; widening requires another rule ID."""
        if rule.scope is not RuleScope.ALWAYS:
            raise ValueError("只有 always 规则可以持久化")
        identity = _identity(os_user, owner_account_id, workspace_id)
        payload = json.dumps(
            {
                "scope": rule.scope.value,
                "decision": rule.decision.value,
                "kind": rule.kind.value,
                "exact_digest": rule.exact_digest,
                "argv_prefix": list(rule.argv_prefix),
                "cwd": rule.cwd,
                "action_summary": _display_text(
                    rule.action_summary,
                    "action_summary",
                    _MAX_ACTION_SUMMARY_LENGTH,
                ),
                "action_detail": _display_text(
                    rule.action_detail,
                    "action_detail",
                    _MAX_ACTION_DETAIL_LENGTH,
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        def _write(conn) -> None:
            conn.execute(
                "INSERT INTO security_rules "
                "(rule_id, os_user, owner_account_id, workspace_id, schema_version, "
                "payload_json, enabled, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
                (rule.rule_id, *identity, _SCHEMA_VERSION, payload, time.time()),
            )

        self._writer.execute(_write)
        return rule

    def list(
        self,
        *,
        os_user: str,
        owner_account_id: str,
        workspace_id: str,
        include_disabled: bool = False,
    ) -> list[ActionRule]:
        """List only rules owned by the exact OS-user/account/workspace tuple."""
        identity = _identity(os_user, owner_account_id, workspace_id)
        enabled_clause = "" if include_disabled else " AND enabled = 1"
        with self._lock:
            rows = self._conn.execute(
                "SELECT rule_id, schema_version, payload_json FROM security_rules "
                "WHERE os_user = ? AND owner_account_id = ? AND workspace_id = ?"
                + enabled_clause
                + " ORDER BY created_at ASC, rule_id ASC",
                identity,
            ).fetchall()
        return [_decode_rule(row) for row in rows]

    def list_with_status(
        self,
        *,
        os_user: str,
        owner_account_id: str,
        workspace_id: str,
    ) -> list[tuple[ActionRule, bool]]:
        """List immutable rules with their mutable enabled flag for owner UI."""
        identity = _identity(os_user, owner_account_id, workspace_id)
        with self._lock:
            rows = self._conn.execute(
                "SELECT rule_id, schema_version, payload_json, enabled FROM security_rules "
                "WHERE os_user = ? AND owner_account_id = ? AND workspace_id = ? "
                "ORDER BY created_at ASC, rule_id ASC",
                identity,
            ).fetchall()
        return [(_decode_rule(row), bool(row["enabled"])) for row in rows]

    def set_enabled(
        self,
        rule_id: str,
        enabled: bool,
        *,
        os_user: str,
        owner_account_id: str,
        workspace_id: str,
    ) -> bool:
        identity = _identity(os_user, owner_account_id, workspace_id)

        def _write(conn) -> bool:
            cursor = conn.execute(
                "UPDATE security_rules SET enabled = ? WHERE rule_id = ? "
                "AND os_user = ? AND owner_account_id = ? AND workspace_id = ?",
                (1 if enabled else 0, rule_id, *identity),
            )
            return cursor.rowcount == 1

        return self._writer.execute(_write)

    def delete(
        self,
        rule_id: str,
        *,
        os_user: str,
        owner_account_id: str,
        workspace_id: str,
    ) -> bool:
        identity = _identity(os_user, owner_account_id, workspace_id)

        def _write(conn) -> bool:
            cursor = conn.execute(
                "DELETE FROM security_rules WHERE rule_id = ? "
                "AND os_user = ? AND owner_account_id = ? AND workspace_id = ?",
                (rule_id, *identity),
            )
            return cursor.rowcount == 1

        return self._writer.execute(_write)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _decode_rule(row) -> ActionRule:
    rule_id = str(row["rule_id"])
    if row["schema_version"] != _SCHEMA_VERSION:
        raise RuleStoreCorruptError(f"规则 {rule_id} 使用未知 schema version")
    try:
        payload = json.loads(row["payload_json"])
        prefix = tuple(payload["argv_prefix"])
        if not all(isinstance(token, str) and token for token in prefix):
            raise ValueError("argv_prefix 非字符串数组")
        rule = ActionRule(
            scope=RuleScope(payload["scope"]),
            decision=RuleDecision(payload["decision"]),
            kind=ActionKind(payload["kind"]),
            exact_digest=str(payload["exact_digest"]),
            argv_prefix=prefix,
            cwd=str(payload["cwd"]),
            rule_id=rule_id,
            action_summary=_display_text(
                payload.get("action_summary", ""),
                "action_summary",
                _MAX_ACTION_SUMMARY_LENGTH,
            ),
            action_detail=_display_text(
                payload.get("action_detail", ""),
                "action_detail",
                _MAX_ACTION_DETAIL_LENGTH,
            ),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuleStoreCorruptError(f"规则 {rule_id} payload 损坏") from exc
    if rule.scope is not RuleScope.ALWAYS:
        raise RuleStoreCorruptError(f"规则 {rule_id} 不是 always scope")
    return rule


def _identity(os_user: str, owner_account_id: str, workspace_id: str) -> tuple[str, str, str]:
    values = tuple(str(value).strip() for value in (os_user, owner_account_id, workspace_id))
    if not all(values):
        raise ValueError("os_user、owner_account_id、workspace_id 均不能为空")
    return values


def _display_text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"{field} 必须是长度不超过 {maximum} 的字符串")
    return value
