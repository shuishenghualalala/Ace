from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

import pytest

from crew.security.actions import normalize_exec_action
from crew.security.approvals import (
    ApprovalDecision,
    ApprovalError,
    ApprovalManager,
)
from crew.security.context import SecurityContext
from crew.security.grants import GrantError, GrantRegistry
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


def test_public_exec_approval_discloses_effective_profile_and_unknown_effects(
    tmp_path: Path,
) -> None:
    from crew.security.models import (
        AdditionalPermissionProfile,
        FilesystemAccess,
        FilesystemEntry,
        NetworkEntry,
        NetworkPolicy,
        PermissionProfile,
        PermissionProfileKind,
    )
    from crew.security.service import _public_request

    profile = PermissionProfile(
        kind=PermissionProfileKind.MANAGED,
        filesystem=(
            FilesystemEntry(tmp_path, FilesystemAccess.READ),
            FilesystemEntry(tmp_path / "out", FilesystemAccess.READ_WRITE),
        ),
        network=NetworkPolicy.RESTRICTED,
        network_entries=(NetworkEntry("api.example.com", 443, "https"),),
    )
    request = _manager(_Clock()).create(
        _context(tmp_path),
        normalize_exec_action(["git", "status"], tmp_path),
        "terminal",
        base_profile_hash="a" * 64,
        risk_class="shell_command",
        additional_permissions=AdditionalPermissionProfile(
            filesystem=(FilesystemEntry(tmp_path / "out", FilesystemAccess.READ_WRITE),)
        ),
        effective_profile=profile,
    )

    payload = _public_request(request, include_nonce=True)
    assert payload["effective_permissions"]["kind"] == "managed"
    assert payload["effect_disclosure"] == {
        "filesystem_write_roots": [str(tmp_path / "out")],
        "network_policy": "restricted",
        "network_entries": [
            {
                "host": "api.example.com",
                "port": 443,
                "protocol": "https",
                "access": "allow",
                "allow_private": False,
                "escalatable": True,
            }
        ],
        "unknown_side_effects": True,
    }


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


def test_deduplication_never_reuses_mismatched_display_or_policy_snapshot(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    manager = _manager(clock)
    context = _context(tmp_path)
    action = normalize_exec_action(["git", "status"], tmp_path)
    baseline, _ = manager.create_or_get(
        context,
        action,
        "terminal",
        base_profile_hash="a" * 64,
        risk_class="read_only",
        preview="git status",
    )

    changed_preview, _ = manager.create_or_get(
        context,
        action,
        "terminal",
        base_profile_hash="a" * 64,
        risk_class="read_only",
        preview="different display",
    )
    changed_risk, _ = manager.create_or_get(
        context,
        action,
        "terminal",
        base_profile_hash="a" * 64,
        risk_class="dangerous",
        preview="git status",
    )
    changed_profile, _ = manager.create_or_get(
        context,
        action,
        "terminal",
        base_profile_hash="b" * 64,
        risk_class="read_only",
        preview="git status",
    )

    assert len(
        {
            baseline.request_id,
            changed_preview.request_id,
            changed_risk.request_id,
            changed_profile.request_id,
        }
    ) == 4


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tool_name", "t" * 129),
        ("risk_class", "r" * 129),
        ("preview", "p" * 4001),
        ("base_profile_hash", "not-a-sha256"),
    ],
)
def test_approval_request_rejects_unbounded_or_malformed_display_fields(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    clock = _Clock()
    manager = _manager(clock)
    context = _context(tmp_path)
    kwargs = {
        "tool_name": "terminal",
        "risk_class": "shell_command",
        "preview": "",
        "base_profile_hash": "",
    }
    kwargs[field] = value

    with pytest.raises(ValueError):
        manager.create(
            context,
            normalize_exec_action(["git", "status"], tmp_path),
            kwargs.pop("tool_name"),
            **kwargs,
        )


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


def test_once_grant_is_consumed_atomically_under_concurrency(tmp_path: Path) -> None:
    clock = _Clock()
    grants = GrantRegistry(clock=clock)
    manager = ApprovalManager(grants, clock=clock)
    context = _context(tmp_path)
    action = normalize_exec_action(["git", "status"], tmp_path)
    request = manager.create(context, action, "terminal")
    grant = manager.decide(
        request.request_id,
        request.nonce,
        ApprovalDecision.ONCE,
        context,
    ).grant
    assert grant is not None

    barrier = threading.Barrier(32)

    def authorize_once(_index: int) -> bool:
        barrier.wait(timeout=5)
        try:
            grants.authorize(grant.grant_id, context, action)
        except GrantError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(authorize_once, range(32)))

    assert results.count(True) == 1


def test_session_grant_cannot_cross_owner_or_session(tmp_path: Path) -> None:
    clock = _Clock()
    grants = GrantRegistry(clock=clock)
    manager = ApprovalManager(grants, clock=clock)
    context = _context(tmp_path)
    action = normalize_exec_action(["git", "status"], tmp_path)
    request = manager.create(context, action, "terminal")
    grant = manager.decide(
        request.request_id,
        request.nonce,
        ApprovalDecision.SESSION,
        context,
    ).grant
    assert grant is not None

    for changed in (
        replace(context, owner_account_id="owner-b"),
        replace(context, session_id="session-b"),
    ):
        with pytest.raises(GrantError, match="上下文"):
            grants.authorize(grant.grant_id, changed, action)
        assert grants.authorize_action(changed, action) is None


def test_grant_expires_at_exact_monotonic_deadline(tmp_path: Path) -> None:
    clock = _Clock()
    grants = GrantRegistry(clock=clock)
    manager = ApprovalManager(grants, clock=clock)
    context = _context(tmp_path)
    action = normalize_exec_action(["git", "status"], tmp_path)
    request = manager.create(
        context,
        action,
        "terminal",
        ttl_seconds=10,
    )
    grant = manager.decide(
        request.request_id,
        request.nonce,
        ApprovalDecision.ONCE,
        context,
    ).grant
    assert grant is not None

    clock.now = request.expires_monotonic
    with pytest.raises(GrantError, match="过期"):
        grants.authorize(grant.grant_id, context, action)


def test_session_end_serializes_with_grant_issue(tmp_path: Path) -> None:
    clock = _Clock()
    grants = GrantRegistry(clock=clock)
    manager = ApprovalManager(grants, clock=clock)
    context = _context(tmp_path)
    action = normalize_exec_action(["git", "status"], tmp_path)
    request = manager.create(context, action, "terminal")
    issue_entered = threading.Event()
    release_issue = threading.Event()
    original_issue = grants.issue

    def blocking_issue(*args, **kwargs):
        issue_entered.set()
        assert release_issue.wait(timeout=5)
        return original_issue(*args, **kwargs)

    grants.issue = blocking_issue  # type: ignore[method-assign]
    decided: dict[str, object] = {}

    def decide() -> None:
        decided["outcome"] = manager.decide(
            request.request_id,
            request.nonce,
            ApprovalDecision.SESSION,
            context,
        )

    decide_thread = threading.Thread(target=decide)
    decide_thread.start()
    assert issue_entered.wait(timeout=5)

    ended = threading.Event()

    def end_session() -> None:
        manager.end_session(context)
        ended.set()

    end_thread = threading.Thread(target=end_session)
    end_thread.start()
    end_thread.join(timeout=0.2)
    end_waited_for_issue = end_thread.is_alive()
    release_issue.set()
    decide_thread.join(timeout=5)
    end_thread.join(timeout=5)
    assert end_waited_for_issue, "session end must wait until grant publication finishes"
    assert ended.is_set()
    assert grants.authorize_action(context, action) is None


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


def test_process_restart_discards_pending_requests_and_transient_grants(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    old_grants = GrantRegistry(clock=clock)
    old_manager = ApprovalManager(old_grants, clock=clock)
    context = _context(tmp_path)
    approved_action = normalize_exec_action(["git", "status"], tmp_path)
    approved = old_manager.create(context, approved_action, "terminal")
    old_manager.decide(
        approved.request_id,
        approved.nonce,
        ApprovalDecision.SESSION,
        context,
    )
    pending_action = normalize_exec_action(["git", "diff"], tmp_path)
    pending = old_manager.create(context, pending_action, "terminal")

    restarted_grants = GrantRegistry(clock=clock)
    restarted_manager = ApprovalManager(restarted_grants, clock=clock)

    assert restarted_manager.list_pending(context) == []
    with pytest.raises(ApprovalError, match="不存在"):
        restarted_manager.decide(
            pending.request_id,
            pending.nonce,
            ApprovalDecision.ONCE,
            context,
        )
    assert restarted_grants.authorize_action(context, approved_action) is None


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
    assert outcome.persistent_rule.argv_prefix == ("git", "status")


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


def test_invalid_always_prefix_does_not_consume_request(tmp_path: Path) -> None:
    clock = _Clock()
    manager = _manager(clock)
    context = _context(tmp_path)
    request = manager.create(
        context,
        normalize_exec_action(["git", "status"], tmp_path),
        "terminal",
    )

    with pytest.raises(ApprovalError, match="token prefix"):
        manager.decide(
            request.request_id,
            request.nonce,
            ApprovalDecision.ALWAYS,
            context,
            always_argv_prefix=["git", "diff"],
        )
    assert manager.decide(
        request.request_id,
        request.nonce,
        ApprovalDecision.ONCE,
        context,
    ).grant is not None


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
