"""H-4 regression: a grant must not be consumable before its durable audit commits.

SecurityApprovalService.decide issues the execution grant inside approvals.decide and
only then writes the durable ``approval_decision`` audit. Without the decision lock, a
concurrent ``authorize_*`` can consume a once grant during that window, turning the
fail-closed rollback into a no-op. The ``_decision_lock`` makes decide (issue→audit→
rollback) mutually exclusive with grant consumption.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from crew.security.actions import normalize_exec_action
from crew.security.approvals import ApprovalDecision, ApprovalError, ApprovalManager
from crew.security.audit import SQLiteSecurityAudit
from crew.security.context import SecurityContext
from crew.security.grants import GrantError, GrantRegistry
from crew.security.rule_store import SQLiteRuleStore
from crew.security.rules import ActionRule, RuleDecision, RuleScope
from crew.security.service import SecurityApprovalService


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _context(tmp_path: Path) -> SecurityContext:
    return SecurityContext(
        os_user="os-a",
        owner_account_id="owner-a",
        workspace_id="project-a",
        workspace_root=tmp_path,
        session_id="session-a",
        request_id="req-a",
        task_id="task-a",
        cwd=tmp_path,
    )


def _service(tmp_path: Path):
    clock = _Clock()
    grants = GrantRegistry(clock=clock)
    approvals = ApprovalManager(grants, clock=clock)
    rules = SQLiteRuleStore(tmp_path / "rules.db")
    audit = SQLiteSecurityAudit(tmp_path / "audit.db")
    service = SecurityApprovalService(approvals, grants, rules, audit, db_path=tmp_path / "crew.db")
    return approvals, grants, audit, service


def test_request_action_atomically_deduplicates_and_audits_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approvals, _grants, audit, service = _service(tmp_path)
    context = _context(tmp_path)
    action = normalize_exec_action(["git", "status"], tmp_path)
    barrier = threading.Barrier(16)
    original_list = approvals.list_pending

    def synchronized_list(request_context):
        result = original_list(request_context)
        barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(approvals, "list_pending", synchronized_list)
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(
            pool.map(
                lambda _index: service.request_action(
                    context,
                    action,
                    tool_name="terminal",
                    risk_class="shell_command",
                ),
                range(16),
            )
        )

    assert len({result["request_id"] for result in results}) == 1
    events = audit.query(owner_account_id=context.owner_account_id)
    assert sum(event.action_type == "approval_requested" for event in events) == 1


@pytest.mark.asyncio
async def test_recoverable_decision_error_keeps_waiter_until_valid_retry(tmp_path: Path) -> None:
    approvals, _grants, _audit, service = _service(tmp_path)
    context = _context(tmp_path)
    action = normalize_exec_action(["git", "status"], tmp_path)
    public = service.request_action(
        context,
        action,
        tool_name="terminal",
        risk_class="shell_command",
    )
    request = approvals.list_pending(context)[0]
    waiter = asyncio.create_task(service.await_decision(public["request_id"]))

    with pytest.raises(ApprovalError, match="token prefix"):
        service.decide(
            context,
            request_id=request.request_id,
            nonce=request.nonce,
            decision=ApprovalDecision.ALWAYS,
            always_argv_prefix=["git", "diff"],
        )
    assert not waiter.done()

    service.decide(
        context,
        request_id=request.request_id,
        nonce=request.nonce,
        decision=ApprovalDecision.ONCE,
    )
    outcome = await waiter
    assert outcome is not None and outcome.decision is ApprovalDecision.ONCE


def test_persistent_deny_beats_auto_allow_and_full_access(tmp_path: Path) -> None:
    _approvals, _grants, audit, service = _service(tmp_path)
    context = _context(tmp_path)
    action = normalize_exec_action(["git", "status"], tmp_path)
    deny = ActionRule.exact(
        action,
        scope=RuleScope.ALWAYS,
        decision=RuleDecision.DENY,
    )
    service.rules.create(
        deny,
        os_user=context.os_user,
        owner_account_id=context.owner_account_id,
        workspace_id=context.workspace_id,
    )

    authorized, request = service.authorize_exec_action(
        context,
        action,
        tool_name="terminal",
        risk_class="shell_command",
        auto_allow=True,
    )
    assert not authorized
    assert request is None
    assert any(
        event.action_type == "exec_decision"
        and event.decision == "deny"
        and event.decision_source == "always_deny_rule"
        for event in audit.query(owner_account_id=context.owner_account_id)
    )

    from crew.security.models import ConversationPermissionMode

    service.set_mode(context, ConversationPermissionMode.FULL_ACCESS)
    authorized, request = service.authorize_exec_action(
        context,
        action,
        tool_name="terminal",
        risk_class="shell_command",
        auto_allow=True,
    )
    assert not authorized
    assert request is None


def test_mode_change_keeps_session_grant_but_cancels_pending(tmp_path: Path) -> None:
    approvals, grants, _audit, service = _service(tmp_path)
    context = _context(tmp_path)
    allowed = normalize_exec_action(["git", "status"], tmp_path)
    pending_action = normalize_exec_action(["git", "diff"], tmp_path)
    approved = approvals.create(context, allowed, "terminal")
    grant = approvals.decide(
        approved.request_id,
        approved.nonce,
        ApprovalDecision.SESSION,
        context,
    ).grant
    pending = approvals.create(context, pending_action, "terminal")
    assert grant is not None

    from crew.security.models import ConversationPermissionMode

    assert service.set_mode(context, ConversationPermissionMode.AUTO_REVIEW) is True
    assert approvals.list_pending(context) == []
    grants.authorize(grant.grant_id, context, allowed)
    with pytest.raises(ApprovalError, match="已处理"):
        approvals.decide(pending.request_id, pending.nonce, ApprovalDecision.ONCE, context)

    assert service.set_mode(context, ConversationPermissionMode.AUTO_REVIEW) is False
    assert service.end_session(context.owner_account_id, context.session_id) >= 1
    with pytest.raises(GrantError, match="不存在"):
        grants.authorize(grant.grant_id, context, allowed)


def test_recent_user_rejection_suppresses_immediate_identical_exec_retry(
    tmp_path: Path,
) -> None:
    _approvals, _grants, audit, service = _service(tmp_path)
    context = _context(tmp_path)
    action = normalize_exec_action(["git", "status"], tmp_path)
    authorized, request = service.authorize_exec_action(
        context,
        action,
        tool_name="terminal",
        risk_class="shell_command",
    )
    assert authorized is False
    assert request is not None

    service.decide(
        context,
        request_id=request["request_id"],
        nonce=request["nonce"],
        decision=ApprovalDecision.REJECT,
    )
    authorized, repeated = service.authorize_exec_action(
        context,
        action,
        tool_name="terminal",
        risk_class="shell_command",
    )

    assert authorized is False
    assert repeated is None
    assert any(
        event.action_type == "exec_decision"
        and event.decision_source == "recent_user_rejection"
        for event in audit.query(owner_account_id=context.owner_account_id)
    )


@pytest.mark.parametrize("terminate", ["session", "owner"])
def test_identity_can_approve_again_after_real_session_termination(
    tmp_path: Path,
    terminate: str,
) -> None:
    """Logout/session end revokes old authority without permanently banning identity reuse."""
    approvals, grants, _audit, service = _service(tmp_path)
    context = _context(tmp_path)
    action = normalize_exec_action(["git", "status"], tmp_path)

    first = approvals.create(context, action, "terminal")
    approvals.decide(first.request_id, first.nonce, ApprovalDecision.SESSION, context)
    if terminate == "session":
        service.end_session(context.owner_account_id, context.session_id)
    else:
        service.revoke_owner(context.owner_account_id)
    assert grants.authorize_action(context, action) is None

    second = approvals.create(context, action, "terminal")
    service.decide(
        context,
        request_id=second.request_id,
        nonce=second.nonce,
        decision=ApprovalDecision.ONCE,
    )
    assert grants.authorize_action(context, action) is not None


def test_decide_blocks_authorize_until_audit_commits(tmp_path: Path) -> None:
    """While decide holds the lock across issue→audit, authorize must block, not consume."""
    approvals, _grants, audit, service = _service(tmp_path)
    context = _context(tmp_path)
    action = normalize_exec_action(["git", "diff"], tmp_path)
    request = approvals.create(context, action, "terminal", risk_class="dangerous_command")

    in_window = threading.Event()
    proceed = threading.Event()
    orig_record = audit.record

    def blocking_record(event):
        if getattr(event, "action_type", None) == "approval_decision":
            in_window.set()
            proceed.wait(timeout=5)
        return orig_record(event)

    audit.record = blocking_record

    decide_done: dict = {}

    def run_decide() -> None:
        try:
            service.decide(
                context, request_id=request.request_id, nonce=request.nonce, decision=ApprovalDecision.ONCE
            )
            decide_done["ok"] = True
        except Exception as exc:  # noqa: BLE001
            decide_done["exc"] = exc

    t_decide = threading.Thread(target=run_decide)
    t_decide.start()
    assert in_window.wait(timeout=5), "decide did not reach the audit window"

    auth_result: dict = {}

    def run_auth() -> None:
        auth_result["value"] = service.authorize_exec_action(
            context, action, tool_name="terminal", risk_class="dangerous_command"
        )

    t_auth = threading.Thread(target=run_auth)
    t_auth.start()
    t_auth.join(timeout=1.0)
    assert t_auth.is_alive(), "authorize must block while decide's durable audit is pending (H-4)"

    proceed.set()
    t_decide.join(timeout=5)
    t_auth.join(timeout=5)
    assert decide_done.get("ok"), decide_done
    assert not t_auth.is_alive()
    authorized, _ = auth_result["value"]
    assert authorized, "once grant becomes consumable only after the audit commits"


def test_decide_audit_failure_revokes_grant(tmp_path: Path) -> None:
    """If the durable audit fails, the issued grant is rolled back and not left consumable."""
    approvals, grants, audit, service = _service(tmp_path)
    context = _context(tmp_path)
    action = normalize_exec_action(["git", "diff"], tmp_path)
    request = approvals.create(context, action, "terminal", risk_class="dangerous_command")

    orig_record = audit.record

    def failing_record(event):
        if getattr(event, "action_type", None) == "approval_decision":
            raise RuntimeError("audit unavailable")
        return orig_record(event)

    audit.record = failing_record

    with pytest.raises(RuntimeError):
        service.decide(
            context, request_id=request.request_id, nonce=request.nonce, decision=ApprovalDecision.ONCE
        )
    assert grants.authorize_action(context, action) is None, "grant must be revoked on audit failure"


def test_end_session_cannot_insert_into_decide_issue_window(tmp_path: Path) -> None:
    """H-6: decide holds _decision_lock across grant issue→audit, so a concurrent
    end_session cannot insert between handled-set and issue. It blocks until decide
    releases the lock, then revokes the just-issued grant."""
    _approvals, grants, audit, service = _service(tmp_path)
    context = _context(tmp_path)
    action = normalize_exec_action(["git", "diff"], tmp_path)
    request = service.approvals.create(context, action, "terminal", risk_class="dangerous_command")

    in_window = threading.Event()
    proceed = threading.Event()
    orig_record = audit.record

    def blocking_record(event):
        if getattr(event, "action_type", None) == "approval_decision":
            in_window.set()
            proceed.wait(timeout=5)
        return orig_record(event)

    audit.record = blocking_record

    decide_done: dict = {}

    def run_decide() -> None:
        try:
            service.decide(
                context,
                request_id=request.request_id,
                nonce=request.nonce,
                decision=ApprovalDecision.SESSION,
            )
            decide_done["ok"] = True
        except Exception as exc:  # noqa: BLE001
            decide_done["exc"] = exc

    t_decide = threading.Thread(target=run_decide)
    t_decide.start()
    assert in_window.wait(timeout=5), "decide did not reach the audit window"

    es_done: dict = {}

    def run_end_session() -> None:
        service.end_session(context.owner_account_id, context.session_id)
        es_done["ok"] = True

    t_es = threading.Thread(target=run_end_session)
    t_es.start()
    t_es.join(timeout=1.0)
    assert t_es.is_alive(), "end_session must block while decide holds _decision_lock (H-6)"

    proceed.set()
    t_decide.join(timeout=5)
    t_es.join(timeout=5)
    assert decide_done.get("ok"), decide_done
    # Grant was issued, then end_session (having waited for the lock) revoked it.
    assert grants.authorize_action(context, action) is None


def test_always_rule_not_visible_until_audit_commits_and_rolled_back_on_failure(tmp_path: Path) -> None:
    """H-7: an ALWAYS rule is persisted before its durable audit. authorize() reads
    rules under the same _decision_lock decide() holds, so it cannot match the rule
    during the audit window; if the audit then fails and the rule is rolled back,
    authorize still cannot match it."""
    _approvals, _grants, audit, service = _service(tmp_path)
    context = _context(tmp_path)
    action = normalize_exec_action(["git", "fetch"], tmp_path)
    request = service.approvals.create(context, action, "terminal", risk_class="dangerous_command")

    in_window = threading.Event()
    proceed = threading.Event()
    fail_audit = threading.Event()
    orig_record = audit.record

    def blocking_record(event):
        if getattr(event, "action_type", None) == "approval_decision":
            in_window.set()
            proceed.wait(timeout=5)
            if fail_audit.is_set():
                raise RuntimeError("audit unavailable")
        return orig_record(event)

    audit.record = blocking_record

    decide_done: dict = {}

    def run_decide() -> None:
        try:
            service.decide(
                context,
                request_id=request.request_id,
                nonce=request.nonce,
                decision=ApprovalDecision.ALWAYS,
            )
            decide_done["ok"] = True
        except Exception as exc:  # noqa: BLE001
            decide_done["exc"] = exc

    t_decide = threading.Thread(target=run_decide)
    t_decide.start()
    assert in_window.wait(timeout=5), "decide did not reach the audit window"

    auth_result: dict = {}

    def run_auth() -> None:
        auth_result["value"] = service.authorize_exec_action(
            context, action, tool_name="terminal", risk_class="dangerous_command"
        )

    t_auth = threading.Thread(target=run_auth)
    t_auth.start()
    t_auth.join(timeout=1.0)
    assert t_auth.is_alive(), "authorize must block while decide's durable audit is pending (H-7)"

    fail_audit.set()
    proceed.set()
    t_decide.join(timeout=5)
    t_auth.join(timeout=5)
    authorized, _payload = auth_result["value"]
    assert not authorized, "ALWAYS rule must not survive a failed durable audit (H-7)"


def test_always_rule_keeps_approved_action_in_rule_and_creation_audit(tmp_path: Path) -> None:
    _approvals, _grants, audit, service = _service(tmp_path)
    context = _context(tmp_path)
    action = normalize_exec_action(
        ["pwsh", "-NoProfile", "-Command", "git status"],
        tmp_path,
        raw_command="git status",
    )
    request = service.approvals.create(context, action, "terminal", risk_class="shell_command")

    service.decide(
        context,
        request_id=request.request_id,
        nonce=request.nonce,
        decision=ApprovalDecision.ALWAYS,
    )

    rule = service.rules.list(
        os_user=context.os_user,
        owner_account_id=context.owner_account_id,
        workspace_id=context.workspace_id,
    )[0]
    assert rule.action_summary == "执行命令：git status"
    assert "工作目录" in rule.action_detail

    created = next(event for event in audit.query(owner_account_id=context.owner_account_id)
                   if event.action_type == "rule_created")
    assert created.action_summary == "执行命令：git status"
    assert "工作目录" in created.action_detail
