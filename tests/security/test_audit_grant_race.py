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

from crew.security.actions import (
    normalize_exec_action,
    normalize_file_action,
    normalize_network_action,
)
from crew.security.approvals import ApprovalDecision, ApprovalError, ApprovalManager
from crew.security.audit import SQLiteSecurityAudit
from crew.security.alerts import SecurityAlertKind, SecurityAlertRegistry
from crew.security.context import SecurityContext
from crew.security.file_policy import FilePolicyResult
from crew.security.grants import GrantError, GrantRegistry
from crew.security.models import (
    AdditionalPermissionProfile,
    GranularApprovalConfig,
    ConversationPermissionMode,
    FilesystemAccess,
    FilesystemEntry,
    NetworkAccess,
    NetworkEntry,
    PermissionGrantScope,
    PermissionProfile,
    PermissionProfileKind,
)
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
    service = SecurityApprovalService(
        approvals,
        grants,
        rules,
        audit,
        db_path=tmp_path / "crew.db",
        approval_ui_available=lambda: True,
    )
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


def test_active_security_alert_fails_closed_across_file_exec_network_and_permissions(
    tmp_path: Path,
) -> None:
    _approvals, _grants, audit, service = _service(tmp_path)
    alerts = SecurityAlertRegistry(
        ui_available=lambda: False,
        threshold=3,
    )
    service.set_alerts(alerts)
    context = _context(tmp_path)
    for _index in range(3):
        alerts.record(
            SecurityAlertKind.SANDBOX_FALLBACK,
            context.owner_account_id,
            context.session_id,
            context.task_id,
        )

    file_result, file_reason, _file_request = service.authorize_file_action(
        context,
        normalize_file_action(tmp_path / "outside.txt", "write"),
        tool_name="file_write",
    )
    assert file_result is FilePolicyResult.DENY
    assert file_reason == "安全告警已触发，请求自动拒绝"

    allowed, _exec_request = service.authorize_exec_action(
        context,
        normalize_exec_action(["git", "status"], tmp_path),
        tool_name="terminal",
        risk_class="shell_command",
    )
    assert allowed is False

    network_result, network_reason, _network_request = service.authorize_network_action(
        context,
        normalize_network_action("example.com", 443, "https"),
        tool_name="web_fetch",
    )
    assert network_result is FilePolicyResult.DENY
    assert network_reason == "安全告警已触发，请求自动拒绝"

    with pytest.raises(ApprovalError, match="安全告警已触发"):
        service.request_permissions(
            context,
            AdditionalPermissionProfile(),
            tool_name="request_permissions",
        )
    assert any(
        event.decision_source == "security_alert_auto_denied"
        for event in audit.query(owner_account_id=context.owner_account_id)
    )


def test_permission_request_atomically_deduplicates_and_audits_once(
    tmp_path: Path,
) -> None:
    _approvals, _grants, audit, service = _service(tmp_path)
    context = _context(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    permissions = AdditionalPermissionProfile(
        filesystem=(FilesystemEntry(outside, FilesystemAccess.READ_WRITE),)
    )

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(
            pool.map(
                lambda _index: service.request_permissions(
                    context,
                    permissions,
                    reason="write build output",
                ),
                range(100),
            )
        )

    assert len({result["request_id"] for result in results}) == 1
    events = audit.query(owner_account_id=context.owner_account_id)
    assert sum(event.action_type == "permission_requested" for event in events) == 1


def test_missing_approval_ui_rejects_without_leaving_pending_authority(
    tmp_path: Path,
) -> None:
    grants = GrantRegistry()
    approvals = ApprovalManager(grants)
    rules = SQLiteRuleStore(tmp_path / "rules.db")
    audit = SQLiteSecurityAudit(tmp_path / "audit.db")
    service = SecurityApprovalService(
        approvals,
        grants,
        rules,
        audit,
        db_path=tmp_path / "crew.db",
        approval_ui_available=lambda: False,
    )
    context = _context(tmp_path)
    action = normalize_exec_action(["git", "status"], tmp_path)

    authorized, request = service.authorize_exec_action(
        context,
        action,
        tool_name="terminal",
        risk_class="shell_command",
    )
    assert not authorized
    assert request is None
    assert approvals.list_pending(context) == []

    network_result, _reason, network_request = service.authorize_network_action(
        context,
        normalize_network_action("example.com", 443, "https"),
        tool_name="web_extract",
    )
    assert network_result is FilePolicyResult.DENY
    assert network_request is None

    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir(exist_ok=True)
    file_result, _reason, file_request = service.authorize_file_action(
        context,
        normalize_file_action(outside / "item.txt", "write"),
        tool_name="file_write",
    )
    assert file_result is FilePolicyResult.DENY
    assert file_request is None

    with pytest.raises(ApprovalError, match="审批界面"):
        service.request_permissions(
            context,
            AdditionalPermissionProfile(
                filesystem=(FilesystemEntry(outside, FilesystemAccess.READ_WRITE),)
            ),
        )
    assert service.pending(context, include_nonce=True) == []
    assert any(
        event.decision == "deny" and event.decision_source == "approval_ui_unavailable"
        for event in audit.query(owner_account_id=context.owner_account_id)
    )


def test_granular_approval_config_auto_rejects_only_disabled_channel(
    tmp_path: Path,
) -> None:
    grants = GrantRegistry()
    approvals = ApprovalManager(grants)
    rules = SQLiteRuleStore(tmp_path / "rules.db")
    audit = SQLiteSecurityAudit(tmp_path / "audit.db")
    service = SecurityApprovalService(
        approvals,
        grants,
        rules,
        audit,
        db_path=tmp_path / "crew.db",
        approval_ui_available=lambda: True,
        approval_config=GranularApprovalConfig(network=False),
    )
    context = _context(tmp_path)

    result, _reason, request = service.authorize_network_action(
        context,
        normalize_network_action("example.com", 443, "https"),
        tool_name="web_extract",
    )
    assert result is FilePolicyResult.DENY
    assert request is None

    action_request = service.request_action(
        context,
        normalize_exec_action(["git", "status"], tmp_path),
        tool_name="terminal",
        risk_class="shell_command",
    )
    assert action_request["request_id"]
    assert any(
        event.decision_source == "approval_channel_disabled"
        for event in audit.query(owner_account_id=context.owner_account_id)
    )


def test_network_session_grant_is_bound_to_the_approved_http_method(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    grants = GrantRegistry()
    get_action = normalize_network_action(
        "example.com",
        443,
        "https",
        method="GET",
    )
    post_action = normalize_network_action(
        "example.com",
        443,
        "https",
        method="POST",
    )
    grant = grants.issue(
        context,
        get_action,
        RuleScope.SESSION,
        expires_monotonic=None,
        additional_permissions=AdditionalPermissionProfile(
            network=(NetworkEntry("example.com", 443, "https"),),
        ),
    )

    assert get_action.digest != post_action.digest
    assert grants.authorize_action(context, get_action) is grant
    assert grants.authorize_action(context, post_action) is None
    assert grants.additional_permissions(context).network == ()


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


@pytest.mark.asyncio
async def test_wait_timeout_terminally_rejects_action_and_permission_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import crew.security.service as service_module

    monkeypatch.setattr(service_module, "_DECIDE_WAIT_TIMEOUT", 0.01)
    approvals, grants, audit, service = _service(tmp_path)
    context = _context(tmp_path)
    action = normalize_exec_action(["git", "status"], tmp_path)
    action_request = service.request_action(
        context,
        action,
        tool_name="terminal",
        risk_class="shell_command",
    )

    assert await service.await_decision(action_request["request_id"]) is None
    with pytest.raises(ApprovalError, match="已处理"):
        service.decide(
            context,
            request_id=action_request["request_id"],
            nonce=action_request["nonce"],
            decision=ApprovalDecision.ONCE,
        )
    assert grants.authorize_action(context, action) is None

    outside = tmp_path / "outside"
    outside.mkdir()
    permission_request = service.request_permissions(
        context,
        AdditionalPermissionProfile(
            filesystem=(FilesystemEntry(outside, FilesystemAccess.READ_WRITE),)
        ),
    )
    assert await service.await_permission_decision(permission_request["request_id"]) is None
    with pytest.raises(ApprovalError, match="已处理"):
        service.decide_permissions(
            context,
            request_id=permission_request["request_id"],
            nonce=permission_request["nonce"],
            decision=ApprovalDecision.SESSION,
        )
    assert grants.additional_permissions(context) == AdditionalPermissionProfile()
    sources = {
        event.decision_source
        for event in audit.query(owner_account_id=context.owner_account_id)
        if event.decision == "reject"
    }
    assert {"approval_timeout", "permission_timeout"}.issubset(sources)


@pytest.mark.asyncio
async def test_cancelled_wait_terminally_rejects_pending_request(
    tmp_path: Path,
) -> None:
    approvals, grants, audit, service = _service(tmp_path)
    context = _context(tmp_path)
    action = normalize_exec_action(["git", "status"], tmp_path)
    public = service.request_action(
        context,
        action,
        tool_name="terminal",
        risk_class="shell_command",
    )
    waiter = asyncio.create_task(service.await_decision(public["request_id"]))
    await asyncio.sleep(0)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    with pytest.raises(ApprovalError, match="已处理"):
        service.decide(
            context,
            request_id=public["request_id"],
            nonce=public["nonce"],
            decision=ApprovalDecision.ONCE,
        )
    assert grants.authorize_action(context, action) is None
    assert approvals.list_pending(context) == []
    assert any(
        event.decision == "reject" and event.decision_source == "approval_cancelled"
        for event in audit.query(owner_account_id=context.owner_account_id)
    )


@pytest.mark.asyncio
async def test_worker_thread_decision_wakes_waiter_without_cross_thread_future_access(
    tmp_path: Path,
) -> None:
    _approvals, _grants, _audit, service = _service(tmp_path)
    context = _context(tmp_path)
    public = service.request_action(
        context,
        normalize_exec_action(["git", "status"], tmp_path),
        tool_name="terminal",
        risk_class="shell_command",
    )
    waiter = asyncio.create_task(service.await_decision(public["request_id"]))
    await asyncio.sleep(0)

    loop = asyncio.get_running_loop()
    previous_debug = loop.get_debug()
    loop.set_debug(True)
    try:
        await asyncio.to_thread(
            service.decide,
            context,
            request_id=public["request_id"],
            nonce=public["nonce"],
            decision=ApprovalDecision.ONCE,
        )
    finally:
        loop.set_debug(previous_debug)

    outcome = await asyncio.wait_for(waiter, timeout=1)
    assert outcome is not None
    assert outcome.decision is ApprovalDecision.ONCE


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


def test_base_network_deny_beats_an_exact_runtime_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _approvals, grants, audit, service = _service(tmp_path)
    context = _context(tmp_path)
    action = normalize_network_action("example.com", 443, "https")
    denied_profile = PermissionProfile(
        kind=PermissionProfileKind.MANAGED,
        network_entries=(
            NetworkEntry(
                "example.com",
                443,
                "https",
                access=NetworkAccess.DENY,
            ),
        ),
    )
    monkeypatch.setattr(service, "_base_profile", lambda _context: denied_profile)
    grants.issue(
        context,
        action,
        RuleScope.ONCE,
        expires_monotonic=None,
    )

    result, _reason, request = service.authorize_network_action(
        context,
        action,
        tool_name="web_extract",
    )

    assert result is FilePolicyResult.DENY
    assert request is None
    assert any(
        event.decision == "deny" and event.decision_source == "immutable_policy"
        for event in audit.query(owner_account_id=context.owner_account_id)
    )


def test_persistent_network_deny_beats_base_profile_allow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _approvals, _grants, audit, service = _service(tmp_path)
    context = _context(tmp_path)
    action = normalize_network_action("example.com", 443, "https")
    allowed_profile = PermissionProfile(
        kind=PermissionProfileKind.MANAGED,
        network_entries=(NetworkEntry("example.com", 443, "https"),),
    )
    monkeypatch.setattr(service, "_base_profile", lambda _context: allowed_profile)
    service.rules.create(
        ActionRule.exact(
            action,
            scope=RuleScope.ALWAYS,
            decision=RuleDecision.DENY,
        ),
        os_user=context.os_user,
        owner_account_id=context.owner_account_id,
        workspace_id=context.workspace_id,
    )

    result, reason, request = service.authorize_network_action(
        context,
        action,
        tool_name="web_extract",
    )

    assert result is FilePolicyResult.DENY
    assert reason == "persistent_deny_rule"
    assert request is None
    assert any(
        event.decision == "deny" and event.decision_source == "always_deny_rule"
        for event in audit.query(owner_account_id=context.owner_account_id)
    )


def test_set_mode_revokes_pending_and_returns_idempotently(tmp_path: Path) -> None:
    """service.set_mode 接线到 revoke_pending_session，且重复切换幂等返回 False。

    会话 grant 存活、end_session 撤销等行为由 test_security_approvals.py 覆盖。
    """
    approvals, _grants, _audit, service = _service(tmp_path)
    context = _context(tmp_path)
    approvals.create(context, normalize_exec_action(["git", "diff"], tmp_path), "terminal")

    from crew.security.models import ConversationPermissionMode

    assert service.set_mode(context, ConversationPermissionMode.AUTO_REVIEW) is True
    assert approvals.list_pending(context) == []
    assert service.set_mode(context, ConversationPermissionMode.AUTO_REVIEW) is False


def test_mode_switch_revokes_session_authority_and_is_durably_audited(
    tmp_path: Path,
) -> None:
    approvals, grants, audit, service = _service(tmp_path)
    context = _context(tmp_path)
    action = normalize_exec_action(["git", "status"], tmp_path)
    pending = approvals.create(context, action, "terminal")
    service.decide(
        context,
        request_id=pending.request_id,
        nonce=pending.nonce,
        decision=ApprovalDecision.SESSION,
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    grants.issue_permission(
        context,
        AdditionalPermissionProfile(
            filesystem=(FilesystemEntry(outside, FilesystemAccess.READ_WRITE),)
        ),
        PermissionGrantScope.SESSION,
    )

    assert service.set_mode(
        context,
        ConversationPermissionMode.FULL_ACCESS,
        source="desktop_user",
        reason="owner explicitly selected dangerous mode",
    )

    assert grants.authorize_action(context, action) is None
    assert grants.additional_permissions(context) == AdditionalPermissionProfile()
    event = next(
        item
        for item in audit.query(owner_account_id=context.owner_account_id)
        if item.action_type == "security_mode_changed"
    )
    assert event.decision == ConversationPermissionMode.FULL_ACCESS.value
    assert event.decision_source == "desktop_user"
    assert event.rule_scope == "session"
    assert event.approval_mode == ConversationPermissionMode.FULL_ACCESS.value
    assert "request_approval" in event.additional_permissions_summary
    assert "owner explicitly selected dangerous mode" in event.additional_permissions_summary


def test_dangerous_exec_requires_fresh_confirmation_even_in_full_access(
    tmp_path: Path,
) -> None:
    _approvals, _grants, _audit, service = _service(tmp_path)
    context = _context(tmp_path)
    action = normalize_exec_action(["dangerous-tool", "--erase"], tmp_path)
    service.set_mode(context, ConversationPermissionMode.FULL_ACCESS)

    authorized, request = service.authorize_exec_action(
        context,
        action,
        tool_name="terminal",
        risk_class="dangerous_command",
        auto_allow=True,
    )
    assert not authorized
    assert request is not None

    with pytest.raises(ApprovalError, match="逐次批准"):
        service.decide(
            context,
            request_id=request["request_id"],
            nonce=request["nonce"],
            decision=ApprovalDecision.ALWAYS,
            always_argv_prefix=action.argv,
        )

    service.decide(
        context,
        request_id=request["request_id"],
        nonce=request["nonce"],
        decision=ApprovalDecision.ONCE,
    )
    authorized_again, request_again = service.authorize_exec_action(
        context,
        action,
        tool_name="terminal",
        risk_class="dangerous_command",
        auto_allow=True,
    )
    assert authorized_again
    assert request_again is not None
    authorized_third, request_third = service.authorize_exec_action(
        context,
        action,
        tool_name="terminal",
        risk_class="dangerous_command",
        auto_allow=True,
    )
    assert not authorized_third
    assert request_third is not None


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
        event.action_type == "exec_decision" and event.decision_source == "recent_user_rejection"
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


def test_turn_end_revokes_once_action_and_turn_permission_grants(
    tmp_path: Path,
) -> None:
    approvals, grants, _audit, service = _service(tmp_path)
    context = _context(tmp_path)
    action = normalize_exec_action(["git", "status"], tmp_path)
    pending = approvals.create(context, action, "terminal")
    grant = approvals.decide(
        pending.request_id,
        pending.nonce,
        ApprovalDecision.ONCE,
        context,
    ).grant
    assert grant is not None
    outside = tmp_path / "outside"
    outside.mkdir()
    grants.issue_permission(
        context,
        AdditionalPermissionProfile(
            filesystem=(FilesystemEntry(outside, FilesystemAccess.READ_WRITE),)
        ),
        PermissionGrantScope.TURN,
    )

    service.end_task(
        context.owner_account_id,
        context.session_id,
        context.task_id,
    )

    with pytest.raises(GrantError, match="不存在"):
        grants.authorize(grant.grant_id, context, action)
    assert grants.additional_permissions(context) == AdditionalPermissionProfile()


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
                context,
                request_id=request.request_id,
                nonce=request.nonce,
                decision=ApprovalDecision.ONCE,
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


def test_decide_cannot_race_ahead_of_request_creation_audit(tmp_path: Path) -> None:
    approvals, _grants, audit, service = _service(tmp_path)
    context = _context(tmp_path)
    action = normalize_exec_action(["git", "status"], tmp_path)
    in_creation_audit = threading.Event()
    release_creation_audit = threading.Event()
    original_record = audit.record

    def blocking_record(event):
        if getattr(event, "action_type", None) == "approval_requested":
            in_creation_audit.set()
            release_creation_audit.wait(timeout=5)
        return original_record(event)

    audit.record = blocking_record
    requested: dict[str, object] = {}

    def request_action() -> None:
        requested["public"] = service.request_action(
            context,
            action,
            tool_name="terminal",
            risk_class="shell_command",
        )

    request_thread = threading.Thread(target=request_action)
    request_thread.start()
    assert in_creation_audit.wait(timeout=5)
    pending = approvals.list_pending(context)[0]

    decided = threading.Event()

    def decide() -> None:
        service.decide(
            context,
            request_id=pending.request_id,
            nonce=pending.nonce,
            decision=ApprovalDecision.ONCE,
        )
        decided.set()

    decide_thread = threading.Thread(target=decide)
    decide_thread.start()
    decide_thread.join(timeout=0.2)
    decision_waited_for_audit = decide_thread.is_alive()
    release_creation_audit.set()
    request_thread.join(timeout=5)
    decide_thread.join(timeout=5)

    assert decision_waited_for_audit
    assert decided.is_set()


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
            context,
            request_id=request.request_id,
            nonce=request.nonce,
            decision=ApprovalDecision.ONCE,
        )
    assert grants.authorize_action(context, action) is None, (
        "grant must be revoked on audit failure"
    )


@pytest.mark.asyncio
async def test_decision_audit_failure_immediately_releases_waiter(
    tmp_path: Path,
) -> None:
    _approvals, _grants, audit, service = _service(tmp_path)
    context = _context(tmp_path)
    action = normalize_exec_action(["git", "diff"], tmp_path)
    public = service.request_action(
        context,
        action,
        tool_name="terminal",
        risk_class="dangerous_command",
    )
    waiter = asyncio.create_task(service.await_decision(public["request_id"]))
    original_record = audit.record

    def failing_record(event):
        if getattr(event, "action_type", None) == "approval_decision":
            raise RuntimeError("audit unavailable")
        return original_record(event)

    audit.record = failing_record
    with pytest.raises(RuntimeError, match="audit unavailable"):
        service.decide(
            context,
            request_id=public["request_id"],
            nonce=public["nonce"],
            decision=ApprovalDecision.ONCE,
        )

    assert await asyncio.wait_for(waiter, timeout=0.5) is None


def test_end_session_cannot_insert_into_decide_issue_window(tmp_path: Path) -> None:
    """H-6: decide holds _decision_lock across grant issue→audit, so a concurrent
    end_session cannot insert between handled-set and issue. It blocks until decide
    releases the lock, then revokes the just-issued grant."""
    _approvals, grants, audit, service = _service(tmp_path)
    context = _context(tmp_path)
    action = normalize_exec_action(["git", "diff"], tmp_path)
    request = service.approvals.create(context, action, "terminal", risk_class="shell_command")

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


def test_always_rule_not_visible_until_audit_commits_and_rolled_back_on_failure(
    tmp_path: Path,
) -> None:
    """H-7: an ALWAYS rule is persisted before its durable audit. authorize() reads
    rules under the same _decision_lock decide() holds, so it cannot match the rule
    during the audit window; if the audit then fails and the rule is rolled back,
    authorize still cannot match it."""
    _approvals, _grants, audit, service = _service(tmp_path)
    context = _context(tmp_path)
    action = normalize_exec_action(["git", "fetch"], tmp_path)
    request = service.approvals.create(context, action, "terminal", risk_class="shell_command")

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
            context, action, tool_name="terminal", risk_class="shell_command"
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


def test_rule_enable_is_not_visible_until_its_audit_commits(tmp_path: Path) -> None:
    _approvals, _grants, audit, service = _service(tmp_path)
    context = _context(tmp_path)
    action = normalize_exec_action(["git", "status"], tmp_path)
    rule = ActionRule.exact(action, scope=RuleScope.ALWAYS)
    service.rules.create(
        rule,
        os_user=context.os_user,
        owner_account_id=context.owner_account_id,
        workspace_id=context.workspace_id,
    )
    service.rules.set_enabled(
        rule.rule_id,
        False,
        os_user=context.os_user,
        owner_account_id=context.owner_account_id,
        workspace_id=context.workspace_id,
    )
    in_audit = threading.Event()
    release_audit = threading.Event()
    original_record = audit.record

    def failing_record(event):
        if getattr(event, "action_type", None) == "rule_created":
            in_audit.set()
            release_audit.wait(timeout=5)
            raise RuntimeError("audit unavailable")
        return original_record(event)

    audit.record = failing_record
    enable_result: dict[str, object] = {}

    def enable_rule() -> None:
        try:
            service.set_rule_enabled(context, rule.rule_id, True)
        except Exception as exc:  # noqa: BLE001
            enable_result["error"] = exc

    enable_thread = threading.Thread(target=enable_rule)
    enable_thread.start()
    assert in_audit.wait(timeout=5)

    authorization: dict[str, object] = {}

    def authorize() -> None:
        authorization["result"] = service.authorize_exec_action(
            context,
            action,
            tool_name="terminal",
            risk_class="shell_command",
        )

    authorize_thread = threading.Thread(target=authorize)
    authorize_thread.start()
    authorize_thread.join(timeout=0.2)
    authorization_waited = authorize_thread.is_alive()
    release_audit.set()
    enable_thread.join(timeout=5)
    authorize_thread.join(timeout=5)

    assert authorization_waited
    assert isinstance(enable_result.get("error"), RuntimeError)
    authorized, _request = authorization["result"]
    assert not authorized


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

    created = next(
        event
        for event in audit.query(owner_account_id=context.owner_account_id)
        if event.action_type == "rule_created"
    )
    assert created.action_summary == "执行命令：git status"
    assert "工作目录" in created.action_detail
