import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from crew.security.actions import normalize_exec_action, normalize_network_action
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


def test_persisted_rule_payload_rejects_unknown_fields(tmp_path: Path) -> None:
    db = tmp_path / "crew.db"
    store = SQLiteRuleStore(db)
    rule = ActionRule.exec_prefix(["git", "status"], cwd=tmp_path)
    store.create(
        rule,
        os_user="os-a",
        owner_account_id="owner-a",
        workspace_id="project-a",
    )
    with sqlite3.connect(db) as conn:
        payload = json.loads(
            conn.execute(
                "SELECT payload_json FROM security_rules WHERE rule_id = ?",
                (rule.rule_id,),
            ).fetchone()[0]
        )
        payload["unexpected_authority"] = True
        conn.execute(
            "UPDATE security_rules SET payload_json = ? WHERE rule_id = ?",
            (json.dumps(payload), rule.rule_id),
        )

    with pytest.raises(RuleStoreCorruptError, match="payload"):
        store.list(
            os_user="os-a",
            owner_account_id="owner-a",
            workspace_id="project-a",
        )
    store.close()


def test_rule_store_rejects_non_boolean_mutation_inputs(tmp_path: Path) -> None:
    store = SQLiteRuleStore(tmp_path / "crew.db")
    rule = ActionRule.exec_prefix(["git", "status"], cwd=tmp_path)
    store.create(
        rule,
        os_user="os-a",
        owner_account_id="owner-a",
        workspace_id="project-a",
    )

    with pytest.raises(ValueError, match="enabled"):
        store.set_enabled(
            rule.rule_id,
            "false",  # type: ignore[arg-type]
            os_user="os-a",
            owner_account_id="owner-a",
            workspace_id="project-a",
        )
    store.close()


def test_rule_store_rejects_invalid_persisted_enabled_state(tmp_path: Path) -> None:
    db = tmp_path / "crew.db"
    store = SQLiteRuleStore(db)
    rule = ActionRule.exec_prefix(["git", "status"], cwd=tmp_path)
    store.create(
        rule,
        os_user="os-a",
        owner_account_id="owner-a",
        workspace_id="project-a",
    )
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE security_rules SET enabled = 2 WHERE rule_id = ?",
            (rule.rule_id,),
        )

    with pytest.raises(RuleStoreCorruptError, match="enabled"):
        store.list_with_status(
            os_user="os-a",
            owner_account_id="owner-a",
            workspace_id="project-a",
        )
    store.close()
