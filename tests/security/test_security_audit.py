import threading
import time
from pathlib import Path

import pytest

from crew.security.actions import normalize_exec_action
from crew.security.audit import (
    AuditBufferFullError,
    AuditEvent,
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


def test_retention_purges_events_older_than_30_days(tmp_path: Path) -> None:
    audit = SQLiteSecurityAudit(tmp_path / "crew.db")
    old = _event(tmp_path)
    audit.record(old, timestamp=1_000.0)
    audit.record(_event(tmp_path), timestamp=1_000.0 + 31 * 86_400)

    removed = audit.purge_expired(now=1_000.0 + 31 * 86_400)

    assert removed == 1
    assert len(audit.query(owner_account_id="owner-a")) == 1
    audit.close()


def test_durable_security_event_failure_is_fail_closed(tmp_path: Path, monkeypatch) -> None:
    audit = SQLiteSecurityAudit(tmp_path / "crew.db")

    def fail(_fn):
        raise OSError("disk unavailable")

    monkeypatch.setattr(audit._writer, "execute", fail)
    with pytest.raises(AuditWriteError, match="持久化"):
        audit.record(_event(tmp_path, action_type="approval_decision"))
    audit.close()


def test_normal_event_buffers_then_flushes(tmp_path: Path, monkeypatch) -> None:
    audit = SQLiteSecurityAudit(tmp_path / "crew.db", max_buffer=2)
    original = audit._writer.execute

    def fail(_fn):
        raise OSError("disk unavailable")

    monkeypatch.setattr(audit._writer, "execute", fail)
    audit.record(_event(tmp_path))
    monkeypatch.setattr(audit._writer, "execute", original)

    assert audit.flush() == 1
    assert len(audit.query(owner_account_id="owner-a")) == 1
    audit.close()


def test_buffer_exhaustion_rejects_new_event(tmp_path: Path, monkeypatch) -> None:
    audit = SQLiteSecurityAudit(tmp_path / "crew.db", max_buffer=1)

    def fail(_fn):
        raise OSError("disk unavailable")

    monkeypatch.setattr(audit._writer, "execute", fail)
    audit.record(_event(tmp_path))
    with pytest.raises(AuditBufferFullError, match="缓冲"):
        audit.record(_event(tmp_path))
    audit.close()


def test_successful_record_auto_flushes_buffered_events(tmp_path: Path, monkeypatch) -> None:
    audit = SQLiteSecurityAudit(tmp_path / "crew.db")
    original = audit._writer.execute

    def fail(_fn):
        raise OSError("disk unavailable")

    # Ordinary event buffers when the writer is unavailable.
    monkeypatch.setattr(audit._writer, "execute", fail)
    audit.record(_event(tmp_path))
    monkeypatch.setattr(audit._writer, "execute", original)

    # Once the writer recovers, the next successful record opportunistically drains
    # the buffer — no explicit flush() call needed.
    audit.record(_event(tmp_path))

    rows = audit.query(owner_account_id="owner-a", limit=20)
    assert len(rows) == 2
    audit.close()


def test_concurrent_flush_inserts_buffered_event_once(tmp_path: Path, monkeypatch) -> None:
    audit = SQLiteSecurityAudit(tmp_path / "crew.db")
    original = audit._writer.execute

    def fail(_fn):
        raise OSError("disk unavailable")

    monkeypatch.setattr(audit._writer, "execute", fail)
    audit.record(_event(tmp_path))

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
    audit.record(_event(tmp_path))
    monkeypatch.setattr(audit._writer, "execute", original)

    audit.close()

    # Re-open the same DB file and confirm the close-time flush persisted the event.
    replay = SQLiteSecurityAudit(tmp_path / "crew.db")
    assert len(replay.query(owner_account_id="owner-a")) == 1
    replay.close()
