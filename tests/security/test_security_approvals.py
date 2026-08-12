from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from crew.security.actions import normalize_exec_action, normalize_file_action
from crew.security.approvals import (
    ApprovalDecision,
    ApprovalError,
    ApprovalManager,
)
from crew.security.context import SecurityContext
from crew.security.grants import GrantError, GrantRegistry
from crew.security.models import (
    AdditionalPermissionProfile,
    FilesystemAccess,
    FilesystemEntry,
    SandboxPermissions,
)
from crew.security.rules import RuleScope


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
        request_id="gateway-request-a",
        task_id="task-a",
        cwd=tmp_path,
    )


def _manager(clock: _Clock) -> ApprovalManager:
    return ApprovalManager(GrantRegistry(clock=clock), clock=clock)


def test_request_rejects_wrong_or_replayed_nonce(tmp_path: Path) -> None:
    clock = _Clock()
    manager = _manager(clock)
    context = _context(tmp_path)
    request = manager.create(context, normalize_exec_action(["git", "status"], tmp_path), "terminal")

    with pytest.raises(ApprovalError, match="nonce"):
        manager.decide(request.request_id, "wrong", ApprovalDecision.ONCE, context)
    outcome = manager.decide(request.request_id, request.nonce, ApprovalDecision.ONCE, context)
    assert outcome.grant is not None
    with pytest.raises(ApprovalError, match="已处理"):
        manager.decide(request.request_id, request.nonce, ApprovalDecision.ONCE, context)


def test_create_or_get_deduplicates_concurrent_pending_action(tmp_path: Path) -> None:
    clock = _Clock()
    manager = _manager(clock)
    context = _context(tmp_path)
    action = normalize_exec_action(["git", "status"], tmp_path)

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(
            pool.map(
                lambda _index: manager.create_or_get(context, action, "terminal"),
                range(100),
            )
        )

    assert len({request.request_id for request, _created in results}) == 1
    assert sum(created for _request, created in results) == 1


def test_create_or_get_does_not_share_pending_authority_across_tasks(tmp_path: Path) -> None:
    clock = _Clock()
    manager = _manager(clock)
    context = _context(tmp_path)
    action = normalize_exec_action(["git", "status"], tmp_path)

    first, _created = manager.create_or_get(context, action, "terminal")
    second, _created = manager.create_or_get(
        replace(context, task_id="task-b"),
        action,
        "terminal",
    )

    assert first.request_id != second.request_id


def test_pending_and_grant_bind_exact_additional_permissions(tmp_path: Path) -> None:
    clock = _Clock()
    grants = GrantRegistry(clock=clock)
    manager = ApprovalManager(grants, clock=clock)
    context = _context(tmp_path)
    action = normalize_exec_action(["tool", "write"], tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    additional = AdditionalPermissionProfile(
        filesystem=(FilesystemEntry(outside, FilesystemAccess.READ_WRITE),)
    )

    plain, _ = manager.create_or_get(context, action, "terminal")
    expanded, _ = manager.create_or_get(
        context,
        action,
        "terminal",
        additional_permissions=additional,
    )
    assert plain.request_id != expanded.request_id

    outcome = manager.decide(
        expanded.request_id,
        expanded.nonce,
        ApprovalDecision.SESSION,
        context,
    )
    assert outcome.grant is not None
    assert outcome.grant.additional_permissions == additional
    assert grants.authorize_action(context, action) is None
    assert grants.authorize_action(
        context,
        action,
        additional_permissions=additional,
    ) == outcome.grant


def test_pending_requests_have_a_hard_per_session_limit(tmp_path: Path) -> None:
    clock = _Clock()
    manager = _manager(clock)
    context = _context(tmp_path)
    for index in range(128):
        manager.create(
            context,
            normalize_exec_action(["git", "show", str(index)], tmp_path),
            "terminal",
        )

    with pytest.raises(ApprovalError, match="待审批请求过多"):
        manager.create(
            context,
            normalize_exec_action(["git", "show", "overflow"], tmp_path),
            "terminal",
        )

    assert len(manager.list_pending(context)) == 128


def test_pending_requests_have_a_hard_global_limit(tmp_path: Path) -> None:
    clock = _Clock()
    manager = _manager(clock)
    context = _context(tmp_path)
    for index in range(1024):
        request_context = replace(
            context,
            session_id=f"session-{index // 100}",
            task_id=f"task-{index}",
        )
        manager.create(
            request_context,
            normalize_exec_action(["git", "show", str(index)], tmp_path),
            "terminal",
        )

    with pytest.raises(ApprovalError, match="全局待审批请求过多"):
        manager.create(
            replace(context, session_id="overflow-session"),
            normalize_exec_action(["git", "show", "overflow"], tmp_path),
            "terminal",
        )


def test_request_expires_using_monotonic_clock(tmp_path: Path) -> None:
    clock = _Clock()
    manager = _manager(clock)
    context = _context(tmp_path)
    request = manager.create(
        context,
        normalize_exec_action(["git", "status"], tmp_path),
        "terminal",
        ttl_seconds=300,
    )
    clock.now += 301

    with pytest.raises(ApprovalError, match="过期"):
        manager.decide(request.request_id, request.nonce, ApprovalDecision.ONCE, context)


@pytest.mark.parametrize("field", ["os_user", "owner_account_id", "workspace_id", "session_id", "task_id"])
def test_context_field_change_rejects_decision(tmp_path: Path, field: str) -> None:
    clock = _Clock()
    manager = _manager(clock)
    context = _context(tmp_path)
    request = manager.create(context, normalize_exec_action(["git", "status"], tmp_path), "terminal")
    changed = replace(context, **{field: "other"})

    with pytest.raises(ApprovalError, match="上下文"):
        manager.decide(request.request_id, request.nonce, ApprovalDecision.ONCE, changed)


def test_once_grant_is_exact_and_consumed_once(tmp_path: Path) -> None:
    clock = _Clock()
    grants = GrantRegistry(clock=clock)
    manager = ApprovalManager(grants, clock=clock)
    context = _context(tmp_path)
    action = normalize_exec_action(["git", "status"], tmp_path)
    request = manager.create(context, action, "terminal")
    grant = manager.decide(request.request_id, request.nonce, ApprovalDecision.ONCE, context).grant
    assert grant is not None

    with pytest.raises(GrantError, match="动作"):
        grants.authorize(grant.grant_id, context, normalize_exec_action(["git", "diff"], tmp_path))
    grants.authorize(grant.grant_id, context, action)
    with pytest.raises(GrantError, match="不存在"):
        grants.authorize(grant.grant_id, context, action)


def test_session_grant_survives_pending_revocation_until_session_end(tmp_path: Path) -> None:
    clock = _Clock()
    grants = GrantRegistry(clock=clock)
    manager = ApprovalManager(grants, clock=clock)
    context = _context(tmp_path)
    action = normalize_exec_action(["git", "status"], tmp_path)
    request = manager.create(context, action, "terminal")
    grant = manager.decide(request.request_id, request.nonce, ApprovalDecision.SESSION, context).grant
    assert grant is not None

    grants.authorize(grant.grant_id, context, action)
    grants.authorize(grant.grant_id, context, action)
    # 模式切换只撤旧 pending；已批准的“本次对话允许”必须继续有效。
    assert manager.revoke_pending_session(context) == 0
    grants.authorize(grant.grant_id, context, action)

    # 真正结束会话才撤销 transient authority。
    manager.end_session(context)
    with pytest.raises(GrantError, match="不存在"):
        grants.authorize(grant.grant_id, context, action)


def test_session_filesystem_grant_covers_descendants_without_write_upgrade(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    grants = GrantRegistry(clock=clock)
    manager = ApprovalManager(grants, clock=clock)
    context = _context(tmp_path)
    root = tmp_path / "approved"
    root.mkdir()
    action = normalize_file_action(root, "read")
    permissions = AdditionalPermissionProfile(
        filesystem=(FilesystemEntry(root, FilesystemAccess.READ),)
    )
    request = manager.create(
        context,
        action,
        "file_read",
        additional_permissions=permissions,
    )
    manager.decide(request.request_id, request.nonce, ApprovalDecision.SESSION, context)

    assert grants.authorize_action(
        context,
        normalize_file_action(root / "nested.txt", "read"),
        additional_permissions=AdditionalPermissionProfile(
            filesystem=(FilesystemEntry(root / "nested.txt", FilesystemAccess.READ),)
        ),
    ) is not None
    assert grants.authorize_action(
        context,
        normalize_file_action(root / "nested.txt", "write"),
        additional_permissions=AdditionalPermissionProfile(
            filesystem=(FilesystemEntry(root / "nested.txt", FilesystemAccess.READ_WRITE),)
        ),
    ) is None


def test_session_escalated_grant_stays_bound_to_exact_command(tmp_path: Path) -> None:
    clock = _Clock()
    grants = GrantRegistry(clock=clock)
    manager = ApprovalManager(grants, clock=clock)
    context = _context(tmp_path)
    action = normalize_exec_action(["tool", "one"], tmp_path)
    permissions = AdditionalPermissionProfile(
        sandbox_permissions=SandboxPermissions.REQUIRE_ESCALATED
    )
    request = manager.create(
        context,
        action,
        "terminal",
        additional_permissions=permissions,
    )
    manager.decide(request.request_id, request.nonce, ApprovalDecision.SESSION, context)

    assert grants.authorize_action(
        context,
        action,
        additional_permissions=permissions,
    ) is not None
    assert grants.authorize_action(
        context,
        normalize_exec_action(["tool", "two"], tmp_path),
        additional_permissions=permissions,
    ) is None


def test_pending_revocation_does_not_revoke_session_grant(tmp_path: Path) -> None:
    clock = _Clock()
    grants = GrantRegistry(clock=clock)
    manager = ApprovalManager(grants, clock=clock)
    context = _context(tmp_path)
    allowed = normalize_exec_action(["git", "status"], tmp_path)
    pending_action = normalize_exec_action(["git", "diff"], tmp_path)
    approved = manager.create(context, allowed, "terminal")
    grant = manager.decide(
        approved.request_id,
        approved.nonce,
        ApprovalDecision.SESSION,
        context,
    ).grant
    pending = manager.create(context, pending_action, "terminal")
    assert grant is not None

    assert manager.revoke_pending_session(context) == 1
    assert manager.list_pending(context) == []
    grants.authorize(grant.grant_id, context, allowed)
    with pytest.raises(ApprovalError, match="已处理"):
        manager.decide(pending.request_id, pending.nonce, ApprovalDecision.ONCE, context)


def test_always_returns_persistent_rule_but_immediate_grant_stays_exact(tmp_path: Path) -> None:
    clock = _Clock()
    manager = _manager(clock)
    context = _context(tmp_path)
    action = normalize_exec_action(["git", "status", "--short"], tmp_path)
    request = manager.create(context, action, "terminal")

    outcome = manager.decide(
        request.request_id,
        request.nonce,
        ApprovalDecision.ALWAYS,
        context,
        always_argv_prefix=["git", "status"],
    )

    assert outcome.grant is not None
    assert outcome.persistent_rule is not None
    assert outcome.persistent_rule.scope is RuleScope.ALWAYS
    assert outcome.persistent_rule.exact_digest == action.digest
    assert outcome.persistent_rule.argv_prefix == ()


def test_shell_digest_is_stable_across_classifier_evidence(tmp_path: Path) -> None:
    base = normalize_exec_action(
        ["pwsh", "-NoProfile", "-Command", "git status"],
        tmp_path,
        raw_command="git status",
    )
    classified = normalize_exec_action(
        ["pwsh", "-NoProfile", "-Command", "git status"],
        tmp_path,
        raw_command="git status",
        shell_kind="powershell",
        parsed_commands=(("git", "status"),),
        canonical_digest="a" * 64,
    )
    assert base.digest == classified.digest


def test_shell_always_uses_exact_action_not_wrapper_prefix(tmp_path: Path) -> None:
    clock = _Clock()
    manager = _manager(clock)
    context = _context(tmp_path)
    action = normalize_exec_action(
        ["pwsh", "-NoProfile", "-Command", "git status"],
        tmp_path,
        raw_command="git status",
    )
    request = manager.create(context, action, "terminal")

    outcome = manager.decide(
        request.request_id,
        request.nonce,
        ApprovalDecision.ALWAYS,
        context,
        always_argv_prefix=["pwsh", "-NoProfile", "-Command"],
    )

    assert outcome.persistent_rule is not None
    assert outcome.persistent_rule.exact_digest == action.digest
    assert outcome.persistent_rule.argv_prefix == ()
    changed = normalize_exec_action(
        ["pwsh", "-NoProfile", "-Command", "Remove-Item -Recurse C:\\"],
        tmp_path,
        raw_command="Remove-Item -Recurse C:\\",
    )
    assert not outcome.persistent_rule.matches(changed)


def test_legacy_always_prefix_is_ignored_and_exact_action_is_persisted(tmp_path: Path) -> None:
    clock = _Clock()
    manager = _manager(clock)
    context = _context(tmp_path)
    request = manager.create(
        context,
        normalize_exec_action(["git", "status"], tmp_path),
        "terminal",
    )

    outcome = manager.decide(
        request.request_id,
        request.nonce,
        ApprovalDecision.ALWAYS,
        context,
        always_argv_prefix=["git", "diff"],
    )
    assert outcome.persistent_rule is not None
    assert outcome.persistent_rule.exact_digest == request.action_digest


def test_reject_creates_no_grant_or_persistent_deny(tmp_path: Path) -> None:
    clock = _Clock()
    manager = _manager(clock)
    context = _context(tmp_path)
    request = manager.create(context, normalize_exec_action(["git", "status"], tmp_path), "terminal")

    outcome = manager.decide(request.request_id, request.nonce, ApprovalDecision.REJECT, context)

    assert outcome.grant is None
    assert outcome.persistent_rule is None


def test_state_stays_bounded_after_massive_expiry(tmp_path: Path) -> None:
    clock = _Clock()
    manager = _manager(clock)
    context = _context(tmp_path)
    # 过 _PRUNE_MIN_SIZE 阈值但时钟冻结，prune 被 interval 节流，全部保留。
    for index in range(600):
        request_context = replace(
            context,
            session_id=f"session-{index // 100}",
            task_id=f"task-{index}",
        )
        request = manager.create(
            request_context,
            normalize_exec_action(["echo", str(index)], tmp_path),
            "terminal",
        )
        if index < 50:
            manager.decide(
                request.request_id,
                request.nonce,
                ApprovalDecision.ONCE,
                request_context,
            )
    assert len(manager._requests) == 600
    assert len(manager._handled) == 50

    # 推进过 TTL + grace + interval；下一次 create 触发 prune，清掉所有过期项与孤儿 tombstone。
    clock.now += 400
    manager.create(context, normalize_exec_action(["echo", "again"], tmp_path), "terminal")

    assert len(manager._requests) == 1
    assert len(manager._handled) == 0
