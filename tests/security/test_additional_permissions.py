from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

import pytest

from crew.security.actions import (
    normalize_exec_action,
    normalize_file_action,
    normalize_network_action,
)
from crew.security.approvals import ApprovalDecision, ApprovalError, ApprovalManager
from crew.security.audit import SQLiteSecurityAudit
from crew.security.broker import ExecutionRequest, SecurityExecutionBroker
from crew.security.context import SecurityContext
from crew.security.file_policy import FilePolicyResult
from crew.security.grants import GrantError, GrantRegistry
from crew.security.launch import compile_process_launch
from crew.security.models import (
    AdditionalPermissionProfile,
    ConversationPermissionMode,
    FilesystemAccess,
    FilesystemEntry,
    NetworkEntry,
    PermissionGrantScope,
)
from crew.security.permission_approvals import PermissionApprovalManager
from crew.security.policy import (
    additional_permissions_for_exec_action,
    deserialize_additional_permissions,
    intersect_additional_permissions,
    normalize_additional_permissions,
    settings_for_mode,
)
from crew.security.rule_store import SQLiteRuleStore
from crew.security.rules import RuleScope
from crew.security.service import SecurityApprovalService
from crew.security.snapshot import issue_authorization_snapshot


def _context(tmp_path: Path) -> SecurityContext:
    return SecurityContext(
        os_user="os-a",
        owner_account_id="owner-a",
        workspace_id="workspace-a",
        workspace_root=tmp_path / "workspace",
        session_id="session-a",
        request_id="request-a",
        task_id="task-a",
        cwd=tmp_path / "workspace",
    )


def test_approved_grant_keeps_the_exact_additional_filesystem_permission(tmp_path: Path) -> None:
    def clock() -> float:
        return 100.0

    grants = GrantRegistry(clock=clock)
    approvals = ApprovalManager(grants, clock=clock)
    context = _context(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    action = normalize_exec_action(
        ["pwsh", "-Command", "Remove-Item", str(outside / "item.txt")],
        context.cwd,
        raw_command=f"Remove-Item {outside / 'item.txt'}",
        shell_kind="powershell",
    )
    additional = AdditionalPermissionProfile(
        filesystem=(FilesystemEntry(outside, FilesystemAccess.READ_WRITE),)
    )
    request = approvals.create(
        context,
        action,
        "terminal",
        additional_permissions=additional,
    )

    outcome = approvals.decide(
        request.request_id,
        request.nonce,
        ApprovalDecision.ONCE,
        context,
    )

    assert outcome.grant is not None
    assert outcome.grant.additional_permissions == additional


def test_session_permission_is_available_to_later_launches_and_revoked_at_session_end(
    tmp_path: Path,
) -> None:
    def clock() -> float:
        return 100.0

    grants = GrantRegistry(clock=clock)
    approvals = ApprovalManager(grants, clock=clock)
    context = _context(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    additional = AdditionalPermissionProfile(
        filesystem=(FilesystemEntry(outside, FilesystemAccess.READ_WRITE),)
    )
    action = normalize_exec_action(["remove-item", str(outside / "item.txt")], context.cwd)
    request = approvals.create(context, action, "terminal", additional_permissions=additional)
    outcome = approvals.decide(request.request_id, request.nonce, ApprovalDecision.SESSION, context)

    assert outcome.grant is not None
    assert grants.additional_permissions(context) == additional
    grants.revoke_session(context)
    assert grants.additional_permissions(context) == AdditionalPermissionProfile()


def test_session_permission_is_bound_to_the_workspace_root(tmp_path: Path) -> None:
    grants = GrantRegistry()
    approvals = ApprovalManager(grants)
    context = _context(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    additional = AdditionalPermissionProfile(
        filesystem=(FilesystemEntry(outside, FilesystemAccess.READ_WRITE),)
    )
    action = normalize_exec_action(["remove-item", str(outside / "item.txt")], context.cwd)
    request = approvals.create(context, action, "terminal", additional_permissions=additional)
    outcome = approvals.decide(request.request_id, request.nonce, ApprovalDecision.SESSION, context)
    assert outcome.grant is not None

    changed_workspace = replace(
        context,
        workspace_root=tmp_path / "other-workspace",
        cwd=tmp_path / "other-workspace",
    )
    assert grants.additional_permissions(changed_workspace) == AdditionalPermissionProfile()
    assert grants.authorize_action(changed_workspace, action) is None


def test_compile_process_launch_carries_extra_permissions(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    context = _context(tmp_path)
    launch = compile_process_launch(
        context,
        ConversationPermissionMode.AUTO_REVIEW,
        db_path=tmp_path / "crew.db",
        additional_permissions=AdditionalPermissionProfile(
            filesystem=(FilesystemEntry(outside, FilesystemAccess.READ_WRITE),)
        ),
    )

    assert launch.additional_permissions.filesystem == (
        FilesystemEntry(outside, FilesystemAccess.READ_WRITE),
    )


@pytest.mark.asyncio
async def test_broker_forwards_approved_extra_root_to_native_runtime(tmp_path: Path) -> None:
    class RecordingRuntime:
        async def execute_authorized(self, **kwargs):
            self.kwargs = kwargs
            return "result"

    outside = tmp_path / "outside"
    outside.mkdir()
    helper = tmp_path / "ace-security-runtime"
    helper.write_bytes(b"runtime")
    runtime = RecordingRuntime()
    profile = settings_for_mode(ConversationPermissionMode.AUTO_REVIEW, tmp_path).profile
    additional = AdditionalPermissionProfile(
        filesystem=(FilesystemEntry(outside, FilesystemAccess.READ_WRITE),)
    )
    action = normalize_exec_action(("pwsh",), tmp_path)
    authorization = issue_authorization_snapshot(
        context=replace(_context(tmp_path), workspace_root=tmp_path, cwd=tmp_path),
        action=action,
        profile=profile,
        additional_permissions=additional,
        argv=action.argv,
        cwd=tmp_path,
        environment={},
        helper_argv=(helper,),
    )
    await SecurityExecutionBroker(runtime).execute(
        ExecutionRequest(
            authorization_snapshot=authorization,
        )
    )

    assert runtime.kwargs["authorization"].snapshot.writable_roots == (
        str(tmp_path.resolve()),
        str(outside.resolve()),
    )


def test_additional_permission_never_overrides_protected_deny(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    protected = workspace / ".crew"
    profile = settings_for_mode(
        ConversationPermissionMode.REQUEST_APPROVAL,
        workspace,
        deny_entries=(FilesystemEntry(protected, FilesystemAccess.DENY, escalatable=False),),
    ).profile
    additional = AdditionalPermissionProfile(
        filesystem=(FilesystemEntry(workspace, FilesystemAccess.READ_WRITE),)
    )

    from crew.security.models import FilesystemOperation
    from crew.security.policy import filesystem_operation_allowed

    assert not filesystem_operation_allowed(
        profile,
        additional,
        protected / "audit.db",
        FilesystemOperation.WRITE,
    )


def test_literal_delete_gets_parent_root_but_wildcard_delete_gets_none(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    literal = normalize_exec_action(
        ["pwsh", "-Command", "Remove-Item", str(outside / "item.txt")],
        tmp_path,
        raw_command=f"Remove-Item {outside / 'item.txt'}",
        parsed_commands=(("Remove-Item", str(outside / "item.txt")),),
    )
    wildcard = normalize_exec_action(
        ["pwsh", "-Command", "Remove-Item", str(outside / "*.txt")],
        tmp_path,
        raw_command=f"Remove-Item {outside / '*.txt'}",
        parsed_commands=(("Remove-Item", str(outside / "*.txt")),),
    )

    assert additional_permissions_for_exec_action(literal).filesystem[0].root == outside
    assert additional_permissions_for_exec_action(wildcard).filesystem == ()


def test_posix_absolute_delete_is_not_mistaken_for_an_option(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    action = normalize_exec_action(
        ["bash", "-lc", f"rm {outside / 'item.txt'}"],
        tmp_path,
        raw_command=f"rm {outside / 'item.txt'}",
        parsed_commands=(("rm", str(outside / "item.txt")),),
    )
    assert additional_permissions_for_exec_action(action).filesystem[0].root == outside


def test_cmd_delete_wrapper_gets_the_same_parent_capability(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    action = normalize_exec_action(
        ["pwsh", "-Command", "cmd.exe", "/d", "/c", "del", str(outside / "item.txt")],
        tmp_path,
        raw_command=f"cmd.exe /d /c del {outside / 'item.txt'}",
        parsed_commands=(
            ("cmd.exe", "/d", "/c", "del", str(outside / "item.txt")),
        ),
    )

    assert additional_permissions_for_exec_action(action).filesystem[0].root == outside


def test_expired_session_permission_cannot_survive_execution_grant_expiry(tmp_path: Path) -> None:
    now = [100.0]

    def clock() -> float:
        return now[0]

    grants = GrantRegistry(clock=clock)
    context = _context(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    additional = AdditionalPermissionProfile(
        filesystem=(FilesystemEntry(outside, FilesystemAccess.READ_WRITE),)
    )
    action = normalize_exec_action(["remove-item", str(outside / "item.txt")], context.cwd)
    grant = grants.issue(
        context,
        action,
        RuleScope.SESSION,
        expires_monotonic=101.0,
        additional_permissions=additional,
    )
    assert grants.additional_permissions(context) == additional
    now[0] = 102.0
    with pytest.raises(GrantError, match="已过期"):
        grants.authorize(grant.grant_id, context, action)
    assert grants.additional_permissions(context) == AdditionalPermissionProfile()


def test_ungrantable_external_file_write_is_denied(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = replace(_context(tmp_path), workspace_root=workspace, cwd=workspace)
    action = normalize_file_action(tmp_path / "missing" / "nested.txt", "write")
    grants = GrantRegistry()
    approvals = ApprovalManager(grants)
    rules = SQLiteRuleStore(tmp_path / "rules.db")
    audit = SQLiteSecurityAudit(tmp_path / "audit.db")
    service = SecurityApprovalService(approvals, grants, rules, audit, db_path=tmp_path / "crew.db")
    try:
        result, reason, request = service.authorize_file_action(
            context,
            action,
            tool_name="file_write",
        )
        assert result.value == "deny"
        assert "额外权限根" in reason
        assert request is None
    finally:
        audit.close()
        rules.close()


def test_service_approval_request_carries_literal_delete_root_into_grant(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    context = _context(tmp_path)
    context = SecurityContext(**{**context.__dict__, "workspace_root": workspace, "cwd": workspace})
    action = normalize_exec_action(
        ["pwsh", "-Command", "Remove-Item", str(outside / "item.txt")],
        workspace,
        raw_command=f"Remove-Item {outside / 'item.txt'}",
        shell_kind="powershell",
        parsed_commands=(("Remove-Item", str(outside / "item.txt")),),
    )
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
    )
    try:
        authorized, request = service.authorize_exec_action(
            context,
            action,
            tool_name="terminal",
            risk_class="dangerous_command",
        )
        assert not authorized
        assert request is not None
        assert request["additional_permissions"]["filesystem"][0]["root"] == str(outside)

        outcome = service.decide(
            context,
            request_id=request["request_id"],
            nonce=request["nonce"],
            decision=ApprovalDecision.ONCE,
        )
        assert outcome["status"] == "authorized"

        authorized, grant_payload = service.authorize_exec_action(
            context,
            action,
            tool_name="terminal",
            risk_class="dangerous_command",
        )
        assert authorized
        assert grant_payload is not None
        assert grant_payload["additional_permissions"].filesystem[0].root == outside
    finally:
        audit.close()
        rules.close()


def test_network_approval_is_exact_and_rechecked_after_decision(tmp_path: Path) -> None:
    context = _context(tmp_path)
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
    )
    try:
        action = normalize_network_action("example.com", 443, "https")
        result, _reason, request = service.authorize_network_action(
            context,
            action,
            tool_name="web_extract",
        )
        assert result is FilePolicyResult.REQUIRE_APPROVAL
        assert request is not None
        assert request["additional_permissions"]["network"][0]["host"] == "example.com"

        outcome = service.decide(
            context,
            request_id=request["request_id"],
            nonce=request["nonce"],
            decision=ApprovalDecision.ONCE,
        )
        assert outcome["status"] == "authorized"

        result, _reason, _request = service.authorize_network_action(
            context,
            action,
            tool_name="web_extract",
        )
        assert result is FilePolicyResult.ALLOW

        other = normalize_network_action("evil.example", 443, "https")
        result, _reason, other_request = service.authorize_network_action(
            context,
            other,
            tool_name="web_extract",
        )
        assert result is FilePolicyResult.REQUIRE_APPROVAL
        assert other_request is not None
    finally:
        audit.close()
        rules.close()


def test_permission_intersection_rejects_broader_filesystem_and_network_grants(
    tmp_path: Path,
) -> None:
    requested_root = tmp_path / "outside"
    requested_root.mkdir()
    requested = AdditionalPermissionProfile(
        filesystem=(FilesystemEntry(requested_root, FilesystemAccess.READ_WRITE),),
        network=(NetworkEntry("example.com", 443, "https"),),
        allow_local_binding=True,
    )
    granted = AdditionalPermissionProfile(
        filesystem=(
            FilesystemEntry(requested_root / "child", FilesystemAccess.READ_WRITE),
            FilesystemEntry(tmp_path, FilesystemAccess.READ_WRITE),
        ),
        network=(
            NetworkEntry("example.com", 443, "https"),
            NetworkEntry("evil.example", 443, "https"),
        ),
        allow_local_binding=True,
    )

    effective = intersect_additional_permissions(requested, granted)

    assert effective.filesystem == (
        FilesystemEntry(requested_root / "child", FilesystemAccess.READ_WRITE),
    )
    assert effective.network == (NetworkEntry("example.com", 443, "https"),)
    assert effective.allow_local_binding is True


def test_permission_request_cannot_include_deny_entries(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="不支持 deny"):
        normalize_additional_permissions(
            AdditionalPermissionProfile(
                filesystem=(FilesystemEntry(tmp_path, FilesystemAccess.DENY),),
            )
        )


def test_deserialize_accepts_host_metadata_round_trip(tmp_path: Path) -> None:
    root = tmp_path / "outside"
    parsed = deserialize_additional_permissions(
        {
            "filesystem": [
                {"root": str(root), "access": "read_write", "escalatable": True}
            ],
            "network": [
                {
                    "host": "example.com",
                    "port": 443,
                    "protocol": "https",
                    "access": "allow",
                    "allow_private": False,
                    "escalatable": True,
                }
            ],
        }
    )
    assert parsed.filesystem[0].root == root
    assert parsed.network[0].host == "example.com"


@pytest.mark.parametrize(
    "payload",
    [
        {"filesystem": [{"root": 123, "access": "read"}]},
        {"filesystem": [{"root": "C:/safe", "access": 1}]},
        {
            "filesystem": [
                {"root": "C:/safe", "access": "read", "escalatable": False}
            ]
        },
        {"network": [{"host": 123, "port": 443, "protocol": "https"}]},
        {"network": [{"host": "example.com", "port": "443", "protocol": "https"}]},
        {"network": [{"host": "example.com", "port": 443.0, "protocol": "https"}]},
        {"network": [{"host": "example.com", "port": 443, "protocol": 123}]},
        {
            "network": [
                {
                    "host": "example.com",
                    "port": 443,
                    "protocol": "https",
                    "escalatable": False,
                }
            ]
        },
    ],
)
def test_deserialize_rejects_coerced_or_non_escalatable_inputs(payload: dict) -> None:
    with pytest.raises(ValueError):
        deserialize_additional_permissions(payload)


def test_turn_permission_is_not_visible_to_a_later_turn(tmp_path: Path) -> None:
    grants = GrantRegistry()
    context = _context(tmp_path)
    extra = AdditionalPermissionProfile(
        filesystem=(FilesystemEntry(tmp_path / "outside", FilesystemAccess.READ_WRITE),)
    )
    (tmp_path / "outside").mkdir()

    grants.issue_permission(context, extra, PermissionGrantScope.TURN)
    assert grants.additional_permissions(context) == extra
    assert grants.additional_permissions(replace(context, task_id="next-task")) == AdditionalPermissionProfile()
    assert grants.revoke_task(context) == 1
    assert grants.additional_permissions(context) == AdditionalPermissionProfile()


def test_permission_grants_cannot_cross_owner_session_or_turn(tmp_path: Path) -> None:
    grants = GrantRegistry()
    context = _context(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    extra = AdditionalPermissionProfile(
        filesystem=(FilesystemEntry(outside, FilesystemAccess.READ_WRITE),)
    )

    grants.issue_permission(context, extra, PermissionGrantScope.TURN)

    for changed in (
        replace(context, owner_account_id="owner-b"),
        replace(context, session_id="session-b"),
        replace(context, task_id="task-b"),
    ):
        assert grants.additional_permissions(changed) == AdditionalPermissionProfile()


def test_permission_requests_deduplicate_atomically_but_not_across_contexts(
    tmp_path: Path,
) -> None:
    grants = GrantRegistry()
    approvals = PermissionApprovalManager(grants)
    context = _context(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    requested = AdditionalPermissionProfile(
        filesystem=(FilesystemEntry(outside, FilesystemAccess.READ_WRITE),)
    )

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(
            pool.map(
                lambda _index: approvals.create_or_get(
                    context,
                    requested,
                    reason="write build output",
                    tool_name="request_permissions",
                ),
                range(100),
            )
        )

    assert len({request.request_id for request, _created in results}) == 1
    assert sum(created for _request, created in results) == 1
    for changed in (
        replace(context, owner_account_id="owner-b"),
        replace(context, session_id="session-b"),
        replace(context, task_id="task-b"),
    ):
        other, created = approvals.create_or_get(
            changed,
            requested,
            reason="write build output",
            tool_name="request_permissions",
        )
        assert created
        assert other.request_id != results[0][0].request_id


def test_permission_session_end_serializes_with_grant_issue(tmp_path: Path) -> None:
    grants = GrantRegistry()
    approvals = PermissionApprovalManager(grants)
    context = _context(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    requested = AdditionalPermissionProfile(
        filesystem=(FilesystemEntry(outside, FilesystemAccess.READ_WRITE),)
    )
    pending = approvals.create(context, requested)
    issue_entered = threading.Event()
    release_issue = threading.Event()
    original_issue = grants.issue_permission

    def blocking_issue(*args, **kwargs):
        issue_entered.set()
        assert release_issue.wait(timeout=5)
        return original_issue(*args, **kwargs)

    grants.issue_permission = blocking_issue  # type: ignore[method-assign]

    def decide() -> None:
        approvals.decide(
            pending.request_id,
            pending.nonce,
            ApprovalDecision.SESSION,
            context,
        )

    decide_thread = threading.Thread(target=decide)
    decide_thread.start()
    assert issue_entered.wait(timeout=5)

    ended = threading.Event()

    def end_session() -> None:
        approvals.end_owned_session(context.owner_account_id, context.session_id)
        ended.set()

    end_thread = threading.Thread(target=end_session)
    end_thread.start()
    end_thread.join(timeout=0.2)
    end_waited_for_issue = end_thread.is_alive()
    release_issue.set()
    decide_thread.join(timeout=5)
    end_thread.join(timeout=5)

    assert end_waited_for_issue, "session end must wait until permission publication finishes"
    assert ended.is_set()
    assert grants.additional_permissions(context) == AdditionalPermissionProfile()


def test_permission_approval_intersects_partial_grant_and_never_creates_always_rule(
    tmp_path: Path,
) -> None:
    grants = GrantRegistry()
    approvals = PermissionApprovalManager(grants)
    context = _context(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    requested = AdditionalPermissionProfile(
        filesystem=(FilesystemEntry(outside, FilesystemAccess.READ_WRITE),),
        network=(NetworkEntry("example.com", 443, "https"),),
    )
    pending = approvals.create(context, requested, reason="需要清理外部构建产物")

    outcome = approvals.decide(
        pending.request_id,
        pending.nonce,
        ApprovalDecision.SESSION,
        context,
        granted_permissions=AdditionalPermissionProfile(
            filesystem=(FilesystemEntry(outside / "child", FilesystemAccess.READ_WRITE),),
            network=(NetworkEntry("evil.example", 443, "https"),),
        ),
    )

    assert outcome.grant is not None
    assert outcome.granted_permissions.filesystem == (
        FilesystemEntry(outside / "child", FilesystemAccess.READ_WRITE),
    )
    # A grant outside the requested network target is discarded, so the partial
    # approval is rejected rather than silently becoming broader authority.
    assert outcome.granted_permissions.network == ()
    assert grants.additional_permissions(context).filesystem == (
        FilesystemEntry(outside / "child", FilesystemAccess.READ_WRITE),
    )
    assert approvals.get_pending(pending.request_id, context) is None


def test_permission_approval_rejects_always(tmp_path: Path) -> None:
    grants = GrantRegistry()
    approvals = PermissionApprovalManager(grants)
    context = _context(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    pending = approvals.create(
        context,
        AdditionalPermissionProfile(
            filesystem=(FilesystemEntry(outside, FilesystemAccess.READ_WRITE),),
        ),
    )
    with pytest.raises(ApprovalError, match="始终允许"):
        approvals.decide(
            pending.request_id,
            pending.nonce,
            ApprovalDecision.ALWAYS,
            context,
        )
