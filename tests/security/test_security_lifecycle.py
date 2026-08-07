"""Owner and process lifecycle boundaries revoke only transient authority."""

from crew.security.actions import normalize_exec_action
from crew.security.approvals import ApprovalDecision, ApprovalManager
from crew.security.context import SecurityContext
from crew.security.grants import GrantError, GrantRegistry
from crew.security.rules import RuleScope


def _context(owner: str, session: str) -> SecurityContext:
    return SecurityContext(
        os_user="local-user",
        owner_account_id=owner,
        workspace_id="default",
        session_id=session,
        task_id="task",
        request_id="request",
        workspace_root="D:/workspace",
        cwd="D:/workspace",
    )


def test_owner_logout_revokes_only_that_owners_pending_and_granted_authority():
    grants = GrantRegistry()
    approvals = ApprovalManager(grants)
    owner_a = _context("owner-a", "session-a")
    owner_b = _context("owner-b", "session-b")
    action = normalize_exec_action(["echo", "safe"], "D:/workspace")
    request_a = approvals.create(owner_a, action, "terminal")
    request_b = approvals.create(owner_b, action, "terminal")
    grant_a = approvals.decide(
        request_a.request_id, request_a.nonce, ApprovalDecision.SESSION, owner_a
    ).grant
    grant_b = approvals.decide(
        request_b.request_id, request_b.nonce, ApprovalDecision.SESSION, owner_b
    ).grant

    assert grant_a is not None and grant_b is not None
    assert approvals.revoke_owner("owner-a") == 1
    try:
        grants.authorize(grant_a.grant_id, owner_a, action)
    except GrantError:
        pass
    else:
        raise AssertionError("logged-out owner grant remained usable")
    assert grants.authorize(grant_b.grant_id, owner_b, action).scope is RuleScope.SESSION
