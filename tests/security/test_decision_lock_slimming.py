"""A7 回归：决策锁瘦身——authorize_* 决策锁内不再做 SQLite I/O。

- 授权规则经内存快照读取；写路径先持久化、再失效快照，下一次 authorize 重建；
- 普通决策审计异步入队：flush barrier 之前不落库，close() barrier 全部落库；
- durable 事件（approval_decision / rule_created）仍同步落盘，fail-closed
  回滚语义不变。
"""

from __future__ import annotations

from pathlib import Path

from crew.security.actions import normalize_exec_action
from crew.security.approvals import ApprovalDecision, ApprovalManager
from crew.security.audit import SQLiteSecurityAudit
from crew.security.context import SecurityContext
from crew.security.grants import GrantRegistry
from crew.security.rule_store import SQLiteRuleStore
from crew.security.rules import ActionRule, RuleDecision, RuleScope
from crew.security.service import SecurityApprovalService


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
    )
    return approvals, grants, rules, audit, service


def _deny_rule(context: SecurityContext, action) -> ActionRule:
    return ActionRule.exact(
        action,
        scope=RuleScope.ALWAYS,
        decision=RuleDecision.DENY,
        tool_name="terminal",
    )


def test_authorize_reads_rules_from_memory_snapshot(tmp_path: Path) -> None:
    """同一 identity 的第二次 authorize 不再触碰 SQLiteRuleStore.list。"""
    _approvals, _grants, rules, _audit, service = _service(tmp_path)
    context = _context(tmp_path)
    action = normalize_exec_action(["git", "status"], tmp_path)
    calls = {"count": 0}
    original = rules.list

    def counting_list(**kwargs):
        calls["count"] += 1
        return original(**kwargs)

    rules.list = counting_list  # type: ignore[method-assign]

    service.authorize_exec_action(
        context, action, tool_name="terminal", risk_class="shell_command"
    )
    assert calls["count"] == 1
    for _ in range(5):
        service.authorize_exec_action(
            context, action, tool_name="terminal", risk_class="shell_command"
        )
    assert calls["count"] == 1, "授权规则应来自内存快照，而不是每次读 SQLite"


def test_decide_published_rule_invalidates_snapshot(tmp_path: Path) -> None:
    """decide 落库 ALWAYS 规则后，下一次 authorize 必须命中新规则（缓存已失效）。"""
    _approvals, _grants, _rules, audit, service = _service(tmp_path)
    context = _context(tmp_path)
    action = normalize_exec_action(["git", "status"], tmp_path)

    authorized, request = service.authorize_exec_action(
        context, action, tool_name="terminal", risk_class="shell_command"
    )
    assert not authorized and request is not None
    service.decide(
        context,
        request_id=request["request_id"],
        nonce=request["nonce"],
        decision=ApprovalDecision.ALWAYS,
    )

    authorized, request = service.authorize_exec_action(
        context, action, tool_name="terminal", risk_class="shell_command"
    )
    assert authorized and request is None
    service.flush_audit()
    assert any(
        event.action_type == "exec_decision" and event.decision_source == "always_rule"
        for event in audit.query(owner_account_id=context.owner_account_id)
    )


def test_set_rule_enabled_invalidates_snapshot(tmp_path: Path) -> None:
    _approvals, _grants, rules, _audit, service = _service(tmp_path)
    context = _context(tmp_path)
    action = normalize_exec_action(["git", "status"], tmp_path)

    authorized, request = service.authorize_exec_action(
        context, action, tool_name="terminal", risk_class="shell_command"
    )
    assert not authorized and request is not None
    service.decide(
        context,
        request_id=request["request_id"],
        nonce=request["nonce"],
        decision=ApprovalDecision.ALWAYS,
    )
    rule_id = rules.list(
        os_user=context.os_user,
        owner_account_id=context.owner_account_id,
        workspace_id=context.workspace_id,
    )[0].rule_id
    # 消费 decide 随 ALWAYS 规则一并签发的 once grant。
    authorized, _request = service.authorize_exec_action(
        context, action, tool_name="terminal", risk_class="shell_command"
    )
    assert authorized

    assert service.set_rule_enabled(context, rule_id, False) is True
    # ALWAYS 决策还会签发一次性的 once grant（rule 命中优先、未消费），
    # 结束会话清掉 transient grant 后，禁用规则才独立可验证。
    service.end_session(context.owner_account_id, context.session_id)
    authorized, request = service.authorize_exec_action(
        context, action, tool_name="terminal", risk_class="shell_command"
    )
    assert not authorized and request is not None, "禁用规则后快照必须失效并重新审批"

    assert service.set_rule_enabled(context, rule_id, True) is True
    authorized, _request = service.authorize_exec_action(
        context, action, tool_name="terminal", risk_class="shell_command"
    )
    assert authorized, "重新启用规则后快照必须反映可用状态"


def test_authorize_audit_is_queued_until_flush_barrier(tmp_path: Path) -> None:
    """普通决策审计异步入队：barrier 前不落库；同步 audit.record 不得被 authorize 调用。"""
    _approvals, _grants, rules, audit, service = _service(tmp_path)
    # 关掉后台自动 flush，保证「barrier 前不落库」可确定性断言。
    service._audit_sink._flush_interval = 3600.0
    context = _context(tmp_path)
    action = normalize_exec_action(["git", "status"], tmp_path)
    rules.create(
        _deny_rule(context, action),
        os_user=context.os_user,
        owner_account_id=context.owner_account_id,
        workspace_id=context.workspace_id,
    )

    original_record = audit.record

    def forbidden_record(*_args, **_kwargs):
        raise AssertionError("authorize 不得在同步路径调用 audit.record")

    audit.record = forbidden_record  # type: ignore[method-assign]
    try:
        authorized, request = service.authorize_exec_action(
            context, action, tool_name="terminal", risk_class="shell_command"
        )
    finally:
        audit.record = original_record  # type: ignore[method-assign]

    assert not authorized and request is None
    assert audit.query(owner_account_id=context.owner_account_id) == []

    assert service.flush_audit() >= 1
    assert any(
        event.action_type == "exec_decision"
        and event.decision == "deny"
        and event.decision_source == "always_deny_rule"
        for event in audit.query(owner_account_id=context.owner_account_id)
    )


def test_close_flushes_queued_audit_before_exit(tmp_path: Path) -> None:
    """退出前 flush barrier：close() 后重开同一 DB 必须能看到全部决策事件。"""
    _approvals, _grants, rules, audit, service = _service(tmp_path)
    service._audit_sink._flush_interval = 3600.0
    context = _context(tmp_path)
    action = normalize_exec_action(["git", "status"], tmp_path)
    rules.create(
        _deny_rule(context, action),
        os_user=context.os_user,
        owner_account_id=context.owner_account_id,
        workspace_id=context.workspace_id,
    )
    service.authorize_exec_action(
        context, action, tool_name="terminal", risk_class="shell_command"
    )

    service.close()
    audit.close()

    replay = SQLiteSecurityAudit(tmp_path / "audit.db")
    try:
        events = replay.query(owner_account_id=context.owner_account_id)
        assert any(event.action_type == "exec_decision" for event in events)
    finally:
        replay.close()
