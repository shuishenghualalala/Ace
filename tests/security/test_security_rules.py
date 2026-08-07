from pathlib import Path

import pytest

from crew.security.actions import (
    normalize_exec_action,
    normalize_file_action,
    normalize_network_action,
)
from crew.security.rules import ActionRule, RuleDecision, RuleScope, choose_rule


def test_once_rule_requires_exact_normalized_action(tmp_path: Path) -> None:
    action = normalize_exec_action(["git", "status"], tmp_path)
    rule = ActionRule.exact(action, scope=RuleScope.ONCE)

    assert rule.matches(action)
    assert not rule.matches(normalize_exec_action(["git", "diff"], tmp_path))
    assert not rule.matches(normalize_exec_action(["git", "status", "--short"], tmp_path))
    assert not rule.matches(normalize_exec_action(["git", "status"], tmp_path / "other"))


def test_exact_file_rule_changes_when_path_or_operation_changes(tmp_path: Path) -> None:
    read = normalize_file_action(tmp_path / "one.txt", "read")
    rule = ActionRule.exact(read, scope=RuleScope.SESSION)

    assert rule.matches(read)
    assert not rule.matches(normalize_file_action(tmp_path / "two.txt", "read"))
    assert not rule.matches(normalize_file_action(tmp_path / "one.txt", "write"))


def test_always_exec_rule_matches_only_displayed_token_prefix(tmp_path: Path) -> None:
    rule = ActionRule.exec_prefix(["git", "status"], cwd=tmp_path)

    assert rule.matches(normalize_exec_action(["git", "status"], tmp_path))
    assert rule.matches(normalize_exec_action(["git", "status", "--short"], tmp_path))
    assert not rule.matches(normalize_exec_action(["git", "status-all"], tmp_path))
    assert not rule.matches(normalize_exec_action(["git", "diff"], tmp_path))
    assert not rule.matches(normalize_exec_action(["git", "status"], tmp_path / "other"))


@pytest.mark.parametrize("host", ["*", "*.example.com", "https://example.com", "example.com/path"])
def test_network_action_rejects_global_or_pattern_hosts(host: str) -> None:
    with pytest.raises(ValueError):
        normalize_network_action(host, 443, "https")


def test_network_rule_is_exact_across_host_port_and_protocol() -> None:
    action = normalize_network_action("Api.Example.com.", 443, "https")
    rule = ActionRule.exact(action, scope=RuleScope.ALWAYS)

    assert rule.matches(normalize_network_action("api.example.com", 443, "https"))
    assert not rule.matches(normalize_network_action("api.example.com", 8443, "https"))
    assert not rule.matches(normalize_network_action("api.example.com", 443, "tcp"))


def test_more_specific_scope_wins_and_deny_wins_ties(tmp_path: Path) -> None:
    action = normalize_exec_action(["git", "status"], tmp_path)
    always = ActionRule.exec_prefix(["git"], cwd=tmp_path)
    session_deny = ActionRule.exact(
        action,
        scope=RuleScope.SESSION,
        decision=RuleDecision.DENY,
    )
    session_allow = ActionRule.exact(action, scope=RuleScope.SESSION)

    assert choose_rule([always, session_allow], action) == session_allow
    assert choose_rule([always, session_allow, session_deny], action) == session_deny


def test_deny_wins_over_allow_at_equal_specificity_across_scopes(tmp_path: Path) -> None:
    # 同 specificity（同 argv_prefix）时，ALWAYS DENY 不得被 ONCE ALLOW 压倒。
    # 旧实现把 scope_rank 排在 deny 之前，会使 ONCE(3) > ALWAYS(1) 让 allow 赢——
    # P3 exec deny 规则一旦落地即变漏洞。
    from crew.security.actions import ActionKind

    action = normalize_exec_action(["git", "status"], tmp_path)
    always_deny = ActionRule(
        scope=RuleScope.ALWAYS, decision=RuleDecision.DENY, kind=ActionKind.EXEC,
        argv_prefix=("git",), cwd=str(tmp_path),
    )
    once_allow = ActionRule(
        scope=RuleScope.ONCE, decision=RuleDecision.ALLOW, kind=ActionKind.EXEC,
        argv_prefix=("git",), cwd=str(tmp_path),
    )
    assert choose_rule([once_allow, always_deny], action) == always_deny
