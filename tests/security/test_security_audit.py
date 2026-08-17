import json
import os
import stat
import threading
import time
import sqlite3
from dataclasses import asdict, replace
from pathlib import Path

import pytest

import crew
import crew.security.audit as audit_module
from crew.security.actions import normalize_exec_action
from crew.security.audit import (
    AuditBufferFullError,
    AuditEvent,
    AuditIntegrityError,
    AuditWriteError,
    SQLiteSecurityAudit,
)
from crew.security.context import SecurityContext


def _context(tmp_path: Path, owner: str = "owner-a") -> SecurityContext:
    return SecurityContext(
        os_user="os-a",
        owner_account_id=owner,
        workspace_id="project-a",
        workspace_root=tmp_path,
        session_id="session-a",
        request_id="request-a",
        task_id="task-a",
        cwd=tmp_path,
    )


def _event(tmp_path: Path, owner: str = "owner-a", *, action_type: str = "execution") -> AuditEvent:
    return AuditEvent.for_action(
        _context(tmp_path, owner),
        normalize_exec_action(
            [
                "git",
                "status",
                "--token",
                "sk-example-secret-token-123456",
                "--password",
                "ordinary-secret",
                "-u",
                "account:basic-auth-secret",
                "-H",
                "X-Api-Key: plain-header-secret",
            ],
            tmp_path,
            raw_command=(
                "git status --token sk-example-secret-token-123456 "
                "--password ordinary-secret -u account:basic-auth-secret "
                '-H "X-Api-Key: plain-header-secret"'
            ),
        ),
        action_type=action_type,
        decision="allow",
        decision_source="rule",
        additional_permissions_summary="api_key=sk-example-secret-token-123456",
        approval_mode="auto_review",
        tool_name="terminal",
    )


def test_runtime_diagnostic_factory_records_sanitized_fields(tmp_path: Path) -> None:
    event = AuditEvent.for_runtime_diagnostic(
        _context(tmp_path),
        status="failed",
        component="security-runtime",
        backend="fake",
        version="2",
        manifest_digest="b" * 64,
        capabilities=("duplex_stdio_v1", "cap-with-\x00-control"),
        failure_code="sandbox_unavailable",
        failure_detail="helper probe \x00 failed",
    )
    audit = SQLiteSecurityAudit(
        tmp_path / "crew.db",
        integrity_key=b"audit-test-key-material-that-is-32-bytes",
    )
    audit.record(event)

    exported = audit.export_jsonl(owner_account_id="owner-a")
    assert '"action_type": "runtime_diagnostic"' in exported
    assert '"decision": "failed"' in exported
    assert '"sandbox_backend": "fake"' in exported
    assert "duplex_stdio_v1" in exported
    assert "\x00" not in exported
    assert '"stable_error_code": "sandbox_unavailable"' in exported
    audit.close()


def test_audit_event_exposes_structured_security_provenance(tmp_path: Path) -> None:
    event = AuditEvent.for_action(
        _context(tmp_path),
        normalize_exec_action(["echo", "ok"], tmp_path),
        action_type="execution",
        decision="deny",
        decision_source="policy",
        stable_error_code="approval_required",
        tool_name="terminal",
    )

    assert event.actor["owner_account_id"] == "owner-a"
    assert event.action["type"] == "execution"
    assert event.action["digest"] == event.normalized_action_hash
    assert event.resource["workspace_id"] == "project-a"
    assert event.outcome == {
        "decision": "deny",
        "exit_code": None,
        "stable_error_code": "approval_required",
    }
    assert event.provenance["decision_source"] == "policy"
    assert event.provenance["policy_version"] == "ace.security.profile.v1"


def test_structured_audit_fields_are_redacted_before_persistence(tmp_path: Path) -> None:
    secret = "sk-structured-audit-canary-123456"
    event = replace(
        _event(tmp_path),
        actor={"owner_account_id": secret, "note": "Authorization: Bearer " + secret},
        action={"type": "execution", "command": "--token " + secret},
        resource={"path": "C:\\Users\\alice\\secret.txt", "url": "https://u:p@example.test/?token=" + secret},
        outcome={"decision": "deny", "error": secret},
        provenance={"source": "https://u:p@example.test/?token=" + secret},
    )
    audit = SQLiteSecurityAudit(
        tmp_path / "crew.db",
        integrity_key=b"audit-test-key-material-that-is-32-bytes",
    )

    audit.record(event)

    row = audit.query(owner_account_id="owner-a")[0]
    assert row.outcome["decision"] == "deny"
    assert "source" in row.provenance
    exported = audit.export_jsonl(owner_account_id="owner-a")
    assert secret not in exported
    assert "C:\\Users\\alice\\secret.txt" not in exported
    audit.verify_integrity()
    audit.close()


def test_audit_is_owner_scoped_and_redacts_action_details(tmp_path: Path) -> None:
    audit = SQLiteSecurityAudit(tmp_path / "crew.db")
    audit.record(_event(tmp_path, "owner-a"))
    audit.record(_event(tmp_path, "owner-b"))

    rows = audit.query(owner_account_id="owner-a", limit=20)

    assert len(rows) == 1
    assert rows[0].owner_account_id == "owner-a"
    assert rows[0].tool_name == "terminal"
    assert rows[0].action_summary.startswith("执行命令：git status")
    assert "具体命令：git status" in rows[0].action_detail
    assert rows[0].approval_mode == "auto_review"
    assert "sk-example-secret" not in rows[0].additional_permissions_summary
    assert "sk-example-secret" not in rows[0].action_detail
    assert "ordinary-secret" not in rows[0].action_detail
    assert "basic-auth-secret" not in rows[0].action_detail
    assert "plain-header-secret" not in rows[0].action_detail
    exported = audit.export_jsonl(owner_account_id="owner-a")
    assert "git status" in exported
    assert "sk-example-secret" not in exported
    assert "ordinary-secret" not in exported
    assert "basic-auth-secret" not in exported
    assert "plain-header-secret" not in exported
    audit.close()


def test_audit_records_form_a_sequence_bound_hmac_chain_and_detect_tampering(
    tmp_path: Path,
) -> None:
    database = tmp_path / "crew.db"
    audit = SQLiteSecurityAudit(
        database,
        integrity_key=b"audit-test-key-material-that-is-32-bytes",
        integrity_key_id="release-key-2026-08",
    )
    audit.record(_event(tmp_path, action_type="approval_requested"), timestamp=2_000)
    audit.record(_event(tmp_path, action_type="approval_decision"), timestamp=1_000)

    rows, _total = audit.query_page(owner_account_id="owner-a", sort="oldest")

    assert [row.sequence for row in rows] == [1, 2]
    assert rows[0].previous_mac == "0" * 64
    assert rows[1].previous_mac == rows[0].event_mac
    assert {row.integrity_key_id for row in rows} == {"release-key-2026-08"}
    audit.verify_integrity()

    with sqlite3.connect(database) as tamper:
        tamper.execute(
            "UPDATE security_audit_events SET decision = 'deny' WHERE sequence = 2"
        )
        tamper.commit()

    with pytest.raises(AuditIntegrityError, match="HMAC"):
        audit.verify_integrity()

    with sqlite3.connect(database) as repair:
        repair.execute(
            "UPDATE security_audit_events SET decision = 'allow' WHERE sequence = 2"
        )
        repair.commit()
    audit.verify_integrity()
    audit.close()


def test_empty_chain_state_key_identity_is_authenticated(tmp_path: Path) -> None:
    database = tmp_path / "crew.db"
    audit = SQLiteSecurityAudit(
        database,
        integrity_key=b"audit-test-key-material-that-is-32-bytes",
        integrity_key_id="release-key-2026-08",
    )
    with sqlite3.connect(database) as tamper:
        tamper.execute(
            "UPDATE security_audit_chain_state SET integrity_key_id = 'attacker-key'"
        )
        tamper.commit()

    with pytest.raises(AuditIntegrityError, match="密钥标识"):
        audit.verify_integrity()
    audit.close()


def test_missing_chain_state_cannot_reseal_existing_events(tmp_path: Path) -> None:
    database = tmp_path / "crew.db"
    integrity_key = b"audit-test-key-material-that-is-32-bytes"
    audit = SQLiteSecurityAudit(database, integrity_key=integrity_key)
    audit.record(_event(tmp_path))
    audit.close()

    with sqlite3.connect(database) as tamper:
        tamper.execute("DELETE FROM security_audit_chain_state")
        tamper.commit()

    with pytest.raises(AuditIntegrityError, match="链状态缺失"):
        SQLiteSecurityAudit(database, integrity_key=integrity_key)


def test_audit_sanitizes_every_untrusted_diagnostic_field_before_hmac(
    tmp_path: Path,
) -> None:
    secret = "sk-audit-field-canary-1234567890"
    event = replace(
        _event(tmp_path),
        event_id=f"event-{secret}",
        os_user_hash=secret,
        workspace_id=f"workspace-{secret}",
        session_id=f"session-{secret}",
        task_id=f"task-{secret}",
        request_id=f"request-{secret}",
        action_type=f"diagnostic-{secret}",
        normalized_action_hash=secret,
        rule_id=f"rule-{secret}",
        rule_scope=f"scope-{secret}",
        permission_profile_hash=secret,
        additional_permissions_summary=f"token={secret}",
        decision=f"decision-{secret}",
        decision_source=f"password={secret}",
        sandbox_backend=f"Authorization: Bearer {secret}",
        capabilities=(f"capability-{secret}",),
        network_target_summary=f"https://user:{secret}@example.test/path?token={secret}",
        stable_error_code=f'{{"client_secret":"{secret}"}}',
        tool_name=f"tool-{secret}",
        action_summary=f"summary-{secret}",
        action_detail=f"detail-{secret}",
        approval_mode=f"mode-{secret}",
        turn_id=f"turn-{secret}",
        policy_version=f"policy-{secret}",
        build_version=f"build-{secret}",
        model_id=f"model-{secret}",
    )
    audit = SQLiteSecurityAudit(
        tmp_path / "crew.db",
        integrity_key=b"audit-test-key-material-that-is-32-bytes",
    )

    audit.record(event)

    exported = audit.export_jsonl(owner_account_id="owner-a")
    assert secret not in exported
    audit.verify_integrity()
    audit.close()


def test_audit_reads_fail_closed_when_the_chain_is_tampered(tmp_path: Path) -> None:
    database = tmp_path / "crew.db"
    audit = SQLiteSecurityAudit(
        database,
        integrity_key=b"audit-test-key-material-that-is-32-bytes",
    )
    audit.record(_event(tmp_path))
    with sqlite3.connect(database) as tamper:
        tamper.execute(
            "UPDATE security_audit_events SET decision = 'deny' WHERE sequence = 1"
        )
        tamper.commit()

    with pytest.raises(AuditIntegrityError, match="HMAC"):
        audit.query(owner_account_id="owner-a")
    with pytest.raises(AuditIntegrityError, match="HMAC"):
        audit.query_page(owner_account_id="owner-a")
    with pytest.raises(AuditIntegrityError, match="HMAC"):
        audit.export_jsonl(owner_account_id="owner-a")
    audit.close()


def test_audit_records_turn_and_build_identity(tmp_path: Path) -> None:
    audit = SQLiteSecurityAudit(
        tmp_path / "crew.db",
        integrity_key=b"audit-test-key-material-that-is-32-bytes",
    )
    context = replace(
        _context(tmp_path),
        turn_id="turn-42",
        model_id="gpt-4o-mini",
    )
    audit.record(
        AuditEvent.for_action(
            context,
            normalize_exec_action(["echo", "ok"], tmp_path),
            action_type="execution",
            decision="allow",
            decision_source="policy",
        )
    )

    rows = audit.query(owner_account_id="owner-a")
    assert rows[0].turn_id == "turn-42"
    assert rows[0].build_version == crew.__version__
    assert rows[0].model_id == "gpt-4o-mini"
    assert rows[0].policy_version == "ace.security.profile.v1"
    audit.verify_integrity()
    audit.close()


def test_rule_events_carry_rule_schema_version_and_identity(tmp_path: Path) -> None:
    context = replace(
        _context(tmp_path),
        turn_id="turn-rule-1",
        model_id="gpt-4o-mini",
    )
    event = AuditEvent.for_rule(
        context,
        rule_id="rule-42",
        action_type="rule_created",
        decision="allow",
    )

    assert event.turn_id == "turn-rule-1"
    assert event.model_id == "gpt-4o-mini"
    assert event.policy_version == "ace.security.rule.v1"
    assert event.build_version == crew.__version__

    overridden = AuditEvent.for_rule(
        context,
        rule_id="rule-43",
        action_type="rule_deleted",
        decision="deleted",
        policy_version="ace.security.rule.v9",
    )
    assert overridden.policy_version == "ace.security.rule.v9"


def test_critical_factories_never_leave_policy_or_build_empty(tmp_path: Path) -> None:
    context = _context(tmp_path)
    events = [
        AuditEvent.for_action(
            context,
            normalize_exec_action(["echo", "ok"], tmp_path),
            action_type="execution",
            decision="allow",
            decision_source="policy",
        ),
        AuditEvent.for_rule(
            context,
            rule_id="rule-1",
            action_type="rule_created",
            decision="allow",
        ),
        AuditEvent.for_tool_decision(
            context,
            tool_name="browser_use",
            args={},
            decision="deny",
            decision_source="browser_policy",
        ),
        AuditEvent.for_permission(
            context,
            {},
            action_type="permission_requested",
            decision="deny",
            decision_source="policy",
        ),
        AuditEvent.for_mode_change(
            context,
            previous_mode="request_approval",
            current_mode="read_only",
            decision_source="desktop_user",
            reason="test",
        ),
    ]
    for event in events:
        assert event.policy_version
        assert event.build_version == crew.__version__


def test_audit_identity_fields_are_consistent_across_decision_chain(tmp_path: Path) -> None:
    audit = SQLiteSecurityAudit(
        tmp_path / "crew.db",
        integrity_key=b"audit-test-key-material-that-is-32-bytes",
    )
    context = replace(
        _context(tmp_path),
        turn_id="turn-chain-1",
        model_id="gpt-4o-mini",
    )
    audit.record(
        AuditEvent.for_action(
            context,
            normalize_exec_action(["echo", "ok"], tmp_path),
            action_type="approval_decision",
            decision="allow",
            decision_source="desktop_user",
        )
    )
    audit.record(
        AuditEvent.for_tool_decision(
            context,
            tool_name="browser_use",
            args={"action": "navigate", "url": "https://example.test/"},
            decision="allow",
            decision_source="browser_policy",
        )
    )
    audit.record(
        AuditEvent.for_permission(
            context,
            {},
            action_type="permission_requested",
            decision="deny",
            decision_source="policy",
        )
    )
    audit.record(
        AuditEvent.for_mode_change(
            context,
            previous_mode="request_approval",
            current_mode="full_access",
            decision_source="desktop_user",
            reason="chain",
        )
    )

    rows = audit.query(owner_account_id="owner-a")
    assert len(rows) == 4
    assert {row.turn_id for row in rows} == {"turn-chain-1"}
    assert {row.model_id for row in rows} == {"gpt-4o-mini"}
    assert {row.build_version for row in rows} == {crew.__version__}
    assert {row.session_id for row in rows} == {"session-a"}
    assert {row.task_id for row in rows} == {"task-a"}
    assert {row.request_id for row in rows} == {"request-a"}
    audit.verify_integrity()
    audit.close()


def test_legacy_unchained_audit_events_migrate_to_hmac_chain(tmp_path: Path) -> None:
    database = tmp_path / "crew.db"
    key = b"audit-test-key-material-that-is-32-bytes"
    audit = SQLiteSecurityAudit(database, integrity_key=key)
    audit.record(_event(tmp_path, action_type="approval_requested"), timestamp=1_000)
    audit.record(_event(tmp_path, action_type="approval_decision"), timestamp=2_000)
    audit.close()

    with sqlite3.connect(database) as legacy:
        legacy.execute("DROP INDEX idx_security_audit_sequence")
        for column in (
            "sequence",
            "previous_mac",
            "event_mac",
            "integrity_key_id",
        ):
            legacy.execute(f"ALTER TABLE security_audit_events DROP COLUMN {column}")
        legacy.execute("DROP TABLE security_audit_chain_state")
        legacy.commit()

    migrated = SQLiteSecurityAudit(database, integrity_key=key)
    rows, _total = migrated.query_page(owner_account_id="owner-a", sort="oldest")

    assert [row.sequence for row in rows] == [1, 2]
    assert rows[0].previous_mac == "0" * 64
    assert rows[1].previous_mac == rows[0].event_mac
    assert {row.integrity_key_id for row in rows} == {migrated._integrity_key_id}
    migrated.verify_integrity()
    migrated.close()


def test_failed_prechain_migration_is_recovered_once(tmp_path: Path) -> None:
    database = tmp_path / "crew.db"
    key = b"audit-test-key-material-that-is-32-bytes"
    audit = SQLiteSecurityAudit(database, integrity_key=key)
    audit.record(_event(tmp_path), timestamp=1_000)
    audit.record(_event(tmp_path), timestamp=2_000)
    audit.close()

    with sqlite3.connect(database) as partial:
        partial.execute("DROP INDEX idx_security_audit_sequence")
        partial.execute("DELETE FROM security_audit_chain_state")
        partial.execute(
            "UPDATE security_audit_events "
            "SET sequence = 0, integrity_key_id = ''"
        )
        partial.commit()

    recovered = SQLiteSecurityAudit(database, integrity_key=key)
    recovered.verify_integrity()
    recovered.close()


def test_audit_migration_adds_identity_columns_and_reseals_chain(tmp_path: Path) -> None:
    database = tmp_path / "crew.db"
    key = b"audit-test-key-material-that-is-32-bytes"
    audit = SQLiteSecurityAudit(database, integrity_key=key, integrity_key_id="legacy-key")
    audit.record(_event(tmp_path))
    audit.verify_integrity()
    audit.close()

    with sqlite3.connect(database) as legacy:
        for column in ("turn_id", "policy_version", "build_version", "model_id"):
            legacy.execute(
                f"ALTER TABLE security_audit_events DROP COLUMN {column}"
            )
        legacy.commit()

    reopened = SQLiteSecurityAudit(database, integrity_key=key, integrity_key_id="legacy-key")
    reopened.verify_integrity()
    with sqlite3.connect(database) as migrated:
        columns = {
            row[1]
            for row in migrated.execute(
                "PRAGMA table_info(security_audit_events)"
            ).fetchall()
        }
    assert {"turn_id", "policy_version", "build_version", "model_id"} <= columns
    reopened.close()


def test_for_tool_decision_hashes_args_without_storing_secrets(tmp_path: Path) -> None:
    context = replace(
        _context(tmp_path),
        turn_id="turn-tool-1",
        model_id="gpt-4o-mini",
    )
    event = AuditEvent.for_tool_decision(
        context,
        tool_name="browser_use",
        args={"action": "navigate", "url": "https://user:password@example.test/"},
        decision="deny",
        decision_source="browser_policy",
    )

    assert event.action_type == "tool_permission_decision"
    assert event.turn_id == "turn-tool-1"
    assert event.model_id == "gpt-4o-mini"
    assert event.policy_version == "ace.security.profile.v1"
    assert event.build_version == crew.__version__
    assert "password" not in json.dumps(asdict(event), ensure_ascii=False)

    audit = SQLiteSecurityAudit(
        tmp_path / "crew.db",
        integrity_key=b"audit-test-key-material-that-is-32-bytes",
    )
    audit.record(event)
    assert "password" not in audit.export_jsonl(owner_account_id="owner-a")
    audit.verify_integrity()
    audit.close()


def test_integrity_failure_never_degrades_into_the_diagnostic_buffer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit = SQLiteSecurityAudit(tmp_path / "crew.db")

    def fail(_fn):
        raise AuditIntegrityError("chain state invalid")

    monkeypatch.setattr(audit._writer, "execute", fail)
    with pytest.raises(AuditWriteError, match="完整性"):
        audit.record(_event(tmp_path, action_type="diagnostic"))
    assert audit._buffer == []
    audit.close()


def test_audit_query_page_filters_and_sorts_server_side(tmp_path: Path) -> None:
    audit = SQLiteSecurityAudit(tmp_path / "crew.db")
    audit.record(_event(tmp_path, action_type="approval_requested"), timestamp=1_000)
    audit.record(_event(tmp_path, action_type="approval_decision"), timestamp=2_000)
    audit.record(_event(tmp_path, action_type="approval_decision"), timestamp=3_000)

    rows, total = audit.query_page(
        owner_account_id="owner-a",
        action_type="approval_decision",
        decision="allow",
        session_id="session-a",
        sort="oldest",
    )

    assert total == 2
    assert [row.action_type for row in rows] == [
        "approval_decision",
        "approval_decision",
    ]
    assert [row.timestamp for row in rows] == [2_000, 3_000]
    audit.close()


def test_query_page_returns_owner_scoped_total(tmp_path: Path) -> None:
    audit = SQLiteSecurityAudit(tmp_path / "crew.db")
    for _ in range(3):
        audit.record(_event(tmp_path, "owner-a"))
    audit.record(_event(tmp_path, "owner-b"))

    rows, total = audit.query_page(owner_account_id="owner-a", limit=2, offset=2)

    assert len(rows) == 1
    assert total == 3
    assert {row.owner_account_id for row in rows} == {"owner-a"}
    audit.close()


def test_query_page_scopes_workspace_task_and_time_range(tmp_path: Path) -> None:
    audit = SQLiteSecurityAudit(tmp_path / "crew.db")
    audit.record(
        replace(_event(tmp_path), workspace_id="workspace-a", task_id="task-a"),
        timestamp=1_000,
    )
    audit.record(
        replace(_event(tmp_path), workspace_id="workspace-a", task_id="task-b"),
        timestamp=2_000,
    )
    audit.record(
        replace(_event(tmp_path), workspace_id="workspace-b", task_id="task-a"),
        timestamp=3_000,
    )

    rows, total = audit.query_page(
        owner_account_id="owner-a",
        workspace_id="workspace-a",
        task_id="task-a",
        start_time=900,
        end_time=1_100,
    )

    assert total == 1
    assert [(row.workspace_id, row.task_id, row.timestamp) for row in rows] == [
        ("workspace-a", "task-a", 1_000),
    ]
    with pytest.raises(ValueError, match="time"):
        audit.query_page(
            owner_account_id="owner-a",
            start_time=2_000,
            end_time=1_000,
        )
    audit.close()


def test_retention_purges_events_older_than_30_days(tmp_path: Path) -> None:
    audit = SQLiteSecurityAudit(tmp_path / "crew.db")
    old = _event(tmp_path)
    audit.record(old, timestamp=1_000.0)
    audit.record(_event(tmp_path), timestamp=1_000.0 + 31 * 86_400)

    removed = audit.purge_expired(now=1_000.0 + 31 * 86_400)

    assert removed == 1
    assert len(audit.query(owner_account_id="owner-a")) == 1
    audit.close()


def test_retention_rotation_archives_a_verified_prefix_with_private_permissions(
    tmp_path: Path,
) -> None:
    database = tmp_path / "crew.db"
    archive_dir = tmp_path / "audit-archives"
    audit = SQLiteSecurityAudit(
        database,
        archive_dir=archive_dir,
        integrity_key=b"audit-test-key-material-that-is-32-bytes",
        integrity_key_id="rotation-key",
    )
    audit.record(_event(tmp_path, action_type="approval_requested"), timestamp=1_000)
    audit.record(_event(tmp_path, action_type="approval_decision"), timestamp=2_000)
    audit.record(_event(tmp_path, action_type="exec_decision"), timestamp=4_000_000)

    removed = audit.purge_expired(now=4_000_000)

    assert removed == 2
    archives = list(archive_dir.glob("audit-*.json"))
    assert len(archives) == 1
    payload = json.loads(archives[0].read_text(encoding="utf-8"))
    assert payload["schema"] == "ace.security.audit-archive.v1"
    assert payload["first_sequence"] == 1
    assert payload["last_sequence"] == 2
    assert payload["previous_mac"] == "0" * 64
    assert payload["terminal_mac"]
    assert payload["archive_mac"]
    if os.name == "posix":
        assert stat.S_IMODE(archives[0].stat().st_mode) == 0o600
        assert stat.S_IMODE(archive_dir.stat().st_mode) == 0o700
    audit.verify_integrity()
    assert [row.sequence for row in audit.query(owner_account_id="owner-a")] == [3]

    original = archives[0].read_bytes()
    archives[0].write_bytes(original.replace(b'"decision":"allow"', b'"decision":"deny"', 1))
    with pytest.raises(AuditIntegrityError, match="archive"):
        audit.verify_integrity()
    archives[0].write_bytes(original)
    audit.verify_integrity()
    audit.close()


def test_archive_permission_drift_invalidates_integrity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_dir = tmp_path / "audit-archives"
    audit = SQLiteSecurityAudit(
        tmp_path / "crew.db",
        archive_dir=archive_dir,
        integrity_key=b"audit-test-key-material-that-is-32-bytes",
    )
    audit.record(_event(tmp_path), timestamp=1_000)
    assert audit.purge_expired(now=1_000 + 31 * 86_400) == 1

    original = audit_module._verify_integrity_path

    def reject_archive(path: Path) -> None:
        if path.suffix == ".json":
            raise PermissionError("archive ACL drift")
        original(path)

    monkeypatch.setattr(audit_module, "_verify_integrity_path", reject_archive)
    with pytest.raises(AuditIntegrityError, match="archive.*权限"):
        audit.verify_integrity()
    audit.close()


def test_rotation_fails_closed_before_archiving_a_tampered_live_prefix(
    tmp_path: Path,
) -> None:
    database = tmp_path / "crew.db"
    archive_dir = tmp_path / "audit-archives"
    audit = SQLiteSecurityAudit(
        database,
        archive_dir=archive_dir,
        integrity_key=b"audit-test-key-material-that-is-32-bytes",
    )
    audit.record(_event(tmp_path, action_type="approval_requested"), timestamp=1_000)
    audit.record(_event(tmp_path, action_type="approval_decision"), timestamp=2_000)
    with sqlite3.connect(database) as tamper:
        tamper.execute(
            "UPDATE security_audit_events SET decision = 'deny' WHERE sequence = 1"
        )
        tamper.commit()

    with pytest.raises(AuditWriteError, match="完整性"):
        audit.rotate(max_live_events=1)

    with sqlite3.connect(database) as inspect:
        assert inspect.execute(
            "SELECT COUNT(*) FROM security_audit_events"
        ).fetchone()[0] == 2
        assert inspect.execute(
            "SELECT COUNT(*) FROM security_audit_archives"
        ).fetchone()[0] == 0
    assert list(archive_dir.glob("*.json")) == []
    audit.close()


def test_retention_rotation_cannot_archive_another_owner_or_workspace(
    tmp_path: Path,
) -> None:
    audit = SQLiteSecurityAudit(
        tmp_path / "crew.db",
        integrity_key=b"audit-test-key-material-that-is-32-bytes",
        archive_dir=tmp_path / "archives",
    )
    old = 1_000.0
    audit.record(
        replace(
            _event(tmp_path),
            owner_account_id="owner-b",
            workspace_id="project-a",
        ),
        timestamp=old,
    )
    audit.record(_event(tmp_path), timestamp=old + 1)

    removed = audit.purge_expired(
        now=old + 31 * 86_400,
        owner_account_id="owner-a",
        workspace_id="project-a",
    )

    assert removed == 0
    assert len(audit.query(owner_account_id="owner-a")) == 1
    assert len(audit.query(owner_account_id="owner-b")) == 1
    assert list((tmp_path / "archives").glob("*.json")) == []
    audit.verify_integrity()
    audit.close()


def test_durable_security_event_failure_is_fail_closed(tmp_path: Path, monkeypatch) -> None:
    audit = SQLiteSecurityAudit(tmp_path / "crew.db")

    def fail(_fn):
        raise OSError("disk unavailable")

    monkeypatch.setattr(audit._writer, "execute", fail)
    with pytest.raises(AuditWriteError, match="持久化"):
        audit.record(_event(tmp_path, action_type="approval_decision"))
    audit.close()


@pytest.mark.parametrize(
    "action_type",
    ["approval_requested", "execution", "exec_decision", "file_decision", "network_decision"],
)
def test_every_security_decision_fails_closed_instead_of_buffering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action_type: str,
) -> None:
    audit = SQLiteSecurityAudit(
        tmp_path / f"{action_type}.db",
        integrity_key=b"audit-test-key-material-that-is-32-bytes",
    )
    original = audit._writer.execute

    def fail(_fn):
        raise OSError("disk unavailable")

    monkeypatch.setattr(audit._writer, "execute", fail)
    with pytest.raises(AuditWriteError, match="持久化"):
        audit.record(_event(tmp_path, action_type=action_type))
    assert audit._buffer == []

    monkeypatch.setattr(audit._writer, "execute", original)
    audit.close()


def test_normal_event_buffers_then_flushes(tmp_path: Path, monkeypatch) -> None:
    audit = SQLiteSecurityAudit(tmp_path / "crew.db", max_buffer=2)
    original = audit._writer.execute

    def fail(_fn):
        raise OSError("disk unavailable")

    monkeypatch.setattr(audit._writer, "execute", fail)
    audit.record(_event(tmp_path, action_type="diagnostic"))
    monkeypatch.setattr(audit._writer, "execute", original)

    assert audit.flush() == 1
    assert len(audit.query(owner_account_id="owner-a")) == 1
    audit.close()


def test_buffer_exhaustion_rejects_new_event(tmp_path: Path, monkeypatch) -> None:
    audit = SQLiteSecurityAudit(tmp_path / "crew.db", max_buffer=1)

    def fail(_fn):
        raise OSError("disk unavailable")

    monkeypatch.setattr(audit._writer, "execute", fail)
    audit.record(_event(tmp_path, action_type="diagnostic"))
    with pytest.raises(AuditBufferFullError, match="缓冲"):
        audit.record(_event(tmp_path, action_type="diagnostic"))
    audit.close()


def test_successful_record_auto_flushes_buffered_events(tmp_path: Path, monkeypatch) -> None:
    audit = SQLiteSecurityAudit(tmp_path / "crew.db")
    original = audit._writer.execute

    def fail(_fn):
        raise OSError("disk unavailable")

    # Ordinary event buffers when the writer is unavailable.
    monkeypatch.setattr(audit._writer, "execute", fail)
    audit.record(_event(tmp_path, action_type="diagnostic"))
    monkeypatch.setattr(audit._writer, "execute", original)

    # Once the writer recovers, the next successful record opportunistically drains
    # the buffer — no explicit flush() call needed.
    audit.record(_event(tmp_path, action_type="diagnostic"))

    rows = audit.query(owner_account_id="owner-a", limit=20)
    assert len(rows) == 2
    audit.close()


def test_concurrent_flush_inserts_buffered_event_once(tmp_path: Path, monkeypatch) -> None:
    audit = SQLiteSecurityAudit(tmp_path / "crew.db")
    original = audit._writer.execute

    def fail(_fn):
        raise OSError("disk unavailable")

    monkeypatch.setattr(audit._writer, "execute", fail)
    audit.record(_event(tmp_path, action_type="diagnostic"))

    entered = threading.Event()

    def delayed(fn):
        entered.set()
        time.sleep(0.03)
        return original(fn)

    monkeypatch.setattr(audit._writer, "execute", delayed)
    results: list[int] = []
    errors: list[BaseException] = []

    def run_flush() -> None:
        try:
            results.append(audit.flush())
        except BaseException as exc:  # pragma: no cover - asserted empty
            errors.append(exc)

    first = threading.Thread(target=run_flush)
    second = threading.Thread(target=run_flush)
    first.start()
    assert entered.wait(timeout=1)
    second.start()
    first.join(timeout=2)
    second.join(timeout=2)

    assert errors == []
    assert sorted(results) == [0, 1]
    assert len(audit.query(owner_account_id="owner-a")) == 1
    audit.close()


def test_close_drains_buffered_events_best_effort(tmp_path: Path, monkeypatch) -> None:
    audit = SQLiteSecurityAudit(tmp_path / "crew.db")
    original = audit._writer.execute

    def fail(_fn):
        raise OSError("disk unavailable")

    monkeypatch.setattr(audit._writer, "execute", fail)
    audit.record(_event(tmp_path, action_type="diagnostic"))
    monkeypatch.setattr(audit._writer, "execute", original)

    audit.close()

    # Re-open the same DB file and confirm the close-time flush persisted the event.
    replay = SQLiteSecurityAudit(tmp_path / "crew.db")
    assert len(replay.query(owner_account_id="owner-a")) == 1
    replay.close()
