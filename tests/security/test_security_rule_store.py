import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from crew.security.actions import normalize_exec_action, normalize_network_action
from crew.security.models import (
    AdditionalPermissionProfile,
    FilesystemAccess,
    FilesystemEntry,
    SandboxPermissions,
)
from crew.security.rule_store import RuleStoreCorruptError, SQLiteRuleStore
from crew.security.rules import ActionRule, RuleScope


def test_rules_are_isolated_by_os_user_owner_and_workspace(tmp_path: Path) -> None:
    store = SQLiteRuleStore(tmp_path / "crew.db")
    rule = ActionRule.exec_prefix(["git", "status"], cwd=tmp_path)
    store.create(rule, os_user="os-a", owner_account_id="owner-a", workspace_id="project-a")

    assert store.list(os_user="os-a", owner_account_id="owner-a", workspace_id="project-a") == [rule]
    assert store.list(os_user="os-b", owner_account_id="owner-a", workspace_id="project-a") == []
    assert store.list(os_user="os-a", owner_account_id="owner-b", workspace_id="project-a") == []
    assert store.list(os_user="os-a", owner_account_id="owner-a", workspace_id="project-b") == []
    store.close()


def test_expanding_rule_creates_new_record_instead_of_mutating(tmp_path: Path) -> None:
    store = SQLiteRuleStore(tmp_path / "crew.db")
    narrow = ActionRule.exec_prefix(["git", "status"], cwd=tmp_path)
    broad = ActionRule.exec_prefix(["git"], cwd=tmp_path)

    store.create(narrow, os_user="os-a", owner_account_id="owner-a", workspace_id="project-a")
    store.create(broad, os_user="os-a", owner_account_id="owner-a", workspace_id="project-a")
    rules = store.list(os_user="os-a", owner_account_id="owner-a", workspace_id="project-a")

    assert {rule.rule_id for rule in rules} == {narrow.rule_id, broad.rule_id}
    assert next(rule for rule in rules if rule.rule_id == narrow.rule_id).argv_prefix == ("git", "status")
    store.close()


def test_reopen_disables_legacy_allow_prefix_rules(tmp_path: Path) -> None:
    db = tmp_path / "crew.db"
    store = SQLiteRuleStore(db)
    legacy = ActionRule.exec_prefix(["git", "status"], cwd=tmp_path)
    store.create(
        legacy,
        os_user="os-a",
        owner_account_id="owner-a",
        workspace_id="project-a",
    )
    store.close()

    reopened = SQLiteRuleStore(db)
    assert reopened.list(
        os_user="os-a",
        owner_account_id="owner-a",
        workspace_id="project-a",
    ) == []
    assert reopened.list_with_status(
        os_user="os-a",
        owner_account_id="owner-a",
        workspace_id="project-a",
    ) == [(legacy, False)]
    reopened.close()


def test_reopen_keeps_host_validated_allow_prefix_rule(tmp_path: Path) -> None:
    db = tmp_path / "crew.db"
    store = SQLiteRuleStore(db)
    rule = ActionRule.exec_prefix(
        ["git", "status"],
        cwd=tmp_path,
        additional_permissions=AdditionalPermissionProfile(
            sandbox_permissions=SandboxPermissions.REQUIRE_ESCALATED,
        ),
        allow_authority=True,
        tool_name="terminal",
    )
    store.create(
        rule,
        os_user="os-a",
        owner_account_id="owner-a",
        workspace_id="project-a",
    )
    store.close()

    reopened = SQLiteRuleStore(db)
    assert reopened.list(
        os_user="os-a",
        owner_account_id="owner-a",
        workspace_id="project-a",
    ) == [rule]
    reopened.close()


def test_rule_round_trip_keeps_redacted_approval_description(tmp_path: Path) -> None:
    store = SQLiteRuleStore(tmp_path / "crew.db")
    rule = replace(
        ActionRule.exec_prefix(["git", "status"], cwd=tmp_path),
        action_summary="执行命令：git status",
        action_detail="具体命令：git status\n工作目录：D:/work",
    )

    store.create(rule, os_user="os-a", owner_account_id="owner-a", workspace_id="project-a")

    loaded = store.list(
        os_user="os-a",
        owner_account_id="owner-a",
        workspace_id="project-a",
    )
    assert loaded == [rule]
    store.close()


def test_rule_round_trip_keeps_exact_permission_overlay(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    permissions = AdditionalPermissionProfile(
        filesystem=(FilesystemEntry(outside, FilesystemAccess.READ_WRITE),)
    )
    store = SQLiteRuleStore(tmp_path / "crew.db")
    rule = ActionRule.exec_prefix(
        ["tool", "write"],
        cwd=tmp_path,
        additional_permissions=permissions,
    )

    store.create(rule, os_user="os-a", owner_account_id="owner-a", workspace_id="project-a")

    assert store.list(
        os_user="os-a",
        owner_account_id="owner-a",
        workspace_id="project-a",
    ) == [rule]
    store.close()


def test_only_always_rules_can_be_persisted(tmp_path: Path) -> None:
    store = SQLiteRuleStore(tmp_path / "crew.db")
    transient = ActionRule.exact(
        normalize_exec_action(["git", "status"], tmp_path),
        scope=RuleScope.SESSION,
    )

    with pytest.raises(ValueError, match="always"):
        store.create(transient, os_user="os-a", owner_account_id="owner-a", workspace_id="project-a")
    store.close()


def test_unknown_schema_or_corrupt_payload_fails_closed(tmp_path: Path) -> None:
    db = tmp_path / "crew.db"
    store = SQLiteRuleStore(db)
    rule = ActionRule.exact(
        normalize_network_action("api.example.com", 443, "https"),
        scope=RuleScope.ALWAYS,
    )
    store.create(rule, os_user="os-a", owner_account_id="owner-a", workspace_id="project-a")

    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE security_rules SET schema_version = 999 WHERE rule_id = ?", (rule.rule_id,))
    with pytest.raises(RuleStoreCorruptError, match="schema"):
        store.list(os_user="os-a", owner_account_id="owner-a", workspace_id="project-a")
    store.close()


def test_disable_and_delete_do_not_affect_other_owner(tmp_path: Path) -> None:
    store = SQLiteRuleStore(tmp_path / "crew.db")
    first = ActionRule.exec_prefix(["git", "status"], cwd=tmp_path)
    second = ActionRule.exec_prefix(["git", "diff"], cwd=tmp_path)
    store.create(first, os_user="os-a", owner_account_id="owner-a", workspace_id="project-a")
    store.create(second, os_user="os-a", owner_account_id="owner-b", workspace_id="project-a")

    assert store.set_enabled(
        first.rule_id,
        False,
        os_user="os-a",
        owner_account_id="owner-a",
        workspace_id="project-a",
    )
    assert store.list(os_user="os-a", owner_account_id="owner-a", workspace_id="project-a") == []
    assert store.list(os_user="os-a", owner_account_id="owner-b", workspace_id="project-a") == [second]
    assert not store.delete(
        second.rule_id,
        os_user="os-a",
        owner_account_id="owner-a",
        workspace_id="project-a",
    )
    store.close()
