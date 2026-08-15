"""Disconnect/reconnect authority lifecycle contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from crew.security.actions import normalize_exec_action
from crew.security.approvals import ApprovalDecision, ApprovalError, ApprovalManager
from crew.security.audit import SQLiteSecurityAudit
from crew.security.context import SecurityContext
from crew.security.grants import GrantRegistry
from crew.security.rule_store import SQLiteRuleStore
from crew.security.service import SecurityApprovalService


def _context(tmp_path: Path) -> SecurityContext:
    return SecurityContext(
        os_user="os-user",
        owner_account_id="owner-a",
        workspace_id="workspace-a",
        workspace_root=tmp_path,
        session_id="session-a",
        request_id="request-a",
        task_id="task-a",
        cwd=tmp_path,
    )


def _service(tmp_path: Path) -> tuple[GrantRegistry, SecurityApprovalService]:
    grants = GrantRegistry()
    approvals = ApprovalManager(grants)
    service = SecurityApprovalService(
        approvals,
        grants,
        SQLiteRuleStore(tmp_path / "rules.db"),
        SQLiteSecurityAudit(
            tmp_path / "audit.db",
            integrity_key=b"d" * 32,
        ),
        db_path=tmp_path / "crew.db",
        approval_ui_available=lambda: True,
    )
    return grants, service


def test_disconnect_revokes_grants_pending_requests_and_blocks_new_authority(tmp_path):
    grants, service = _service(tmp_path)
    context = _context(tmp_path)
    granted_action = normalize_exec_action(["git", "status"], tmp_path)
    pending_action = normalize_exec_action(["git", "diff"], tmp_path)

    request = service.request_action(
        context,
        granted_action,
        tool_name="shell",
        risk_class="exec",
    )
    service.decide(
        context,
        request_id=request["request_id"],
        nonce=request["nonce"],
        decision=ApprovalDecision.SESSION,
    )
    service.request_action(
        context,
        pending_action,
        tool_name="shell",
        risk_class="exec",
    )

    assert service.freeze_session(context.owner_account_id, context.session_id) >= 2
    assert service.session_is_frozen(context.owner_account_id, context.session_id)
    assert grants.authorize_action(context, granted_action) is None
    assert service.pending(context, include_nonce=True) == []
    with pytest.raises(ApprovalError, match="连接已断开"):
        service.request_action(
            context,
            granted_action,
            tool_name="shell",
            risk_class="exec",
        )

    assert service.resume_session(context.owner_account_id, context.session_id) is True
    resumed = service.request_action(
        context,
        granted_action,
        tool_name="shell",
        risk_class="exec",
    )
    assert resumed["request_id"]
