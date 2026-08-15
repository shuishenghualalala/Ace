"""Sensitive configuration failures must stay explicit instead of widening defaults."""

from pathlib import Path

import yaml
import pytest

from crew.state.config import (
    Config,
    _atomic_write_yaml,
    _read_yaml_file_snapshot,
    load_config,
)
from crew.tools.file_utils import FileConflictError


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("auth:\n  mode: enterprise\n", "auth.mode 不支持"),
        ("auth: []\n", "auth 配置必须是对象"),
        ("- one\n- two\n", "顶层必须是对象"),
        ("llm: [broken\n", "解析失败"),
    ],
)
def test_security_config_failure_is_explicit(
    tmp_path: Path,
    monkeypatch,
    content: str,
    message: str,
) -> None:
    monkeypatch.setenv("CREW_HOME", str(tmp_path / "home"))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(content, encoding="utf-8")

    with pytest.raises(RuntimeError, match=message):
        load_config(config_path=config_path)


def test_missing_optional_security_config_still_uses_safe_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / "home"))
    config_path = tmp_path / "config.yaml"

    config = load_config(config_path=config_path)

    assert config.auth_mode in {"local", "email", "remote"}


def test_config_write_rejects_corrupt_existing_document(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    original = "- one\n- two\n"
    config_path.write_text(original, encoding="utf-8")
    config = Config()
    config.config_path = str(config_path)

    with pytest.raises(RuntimeError, match="顶层必须是对象"):
        config.persist_model_profiles()

    assert config_path.read_text(encoding="utf-8") == original


def test_stale_concurrent_config_writer_cannot_publish_over_new_snapshot(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("llm:\n  active: base\n", encoding="utf-8")
    stale_expected = _read_yaml_file_snapshot(config_path)[1]

    _atomic_write_yaml(
        config_path,
        {"llm": {"active": "writer-a"}},
        stale_expected,
    )
    with pytest.raises(FileConflictError):
        _atomic_write_yaml(
            config_path,
            {"llm": {"active": "writer-b"}},
            stale_expected,
        )

    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted == {"llm": {"active": "writer-a"}}
    assert list(tmp_path.glob("*.tmp")) == []


def test_corrupt_rule_store_never_becomes_empty_allow_policy(tmp_path, monkeypatch):
    import sqlite3

    from crew.security.actions import normalize_exec_action, normalize_network_action
    from crew.security.approvals import ApprovalManager
    from crew.security.audit import SQLiteSecurityAudit
    from crew.security.context import SecurityContext
    from crew.security.grants import GrantRegistry
    from crew.security.rule_store import RuleStoreCorruptError, SQLiteRuleStore
    from crew.security.rules import ActionRule, RuleScope
    from crew.security.service import SecurityApprovalService

    monkeypatch.setenv("CREW_HOME", str(tmp_path / "home"))
    rules = SQLiteRuleStore(tmp_path / "rules.db")
    grants = GrantRegistry()
    audit = SQLiteSecurityAudit(tmp_path / "audit.db")
    service = SecurityApprovalService(
        ApprovalManager(grants),
        grants,
        rules,
        audit,
        db_path=tmp_path / "crew.db",
        approval_ui_available=lambda: True,
    )
    context = SecurityContext(
        os_user="os-a",
        owner_account_id="owner-a",
        workspace_id="project-a",
        workspace_root=tmp_path,
        session_id="session-a",
        request_id="request-a",
        task_id="task-a",
        cwd=tmp_path,
    )
    rule = ActionRule.exact(
        normalize_network_action("api.example.com", 443, "https"),
        scope=RuleScope.ALWAYS,
    )
    rules.create(
        rule,
        os_user="os-a",
        owner_account_id="owner-a",
        workspace_id="project-a",
    )
    with sqlite3.connect(tmp_path / "rules.db") as conn:
        conn.execute(
            "UPDATE security_rules SET schema_version = 999 WHERE rule_id = ?",
            (rule.rule_id,),
        )

    with pytest.raises(RuleStoreCorruptError, match="schema"):
        service.authorize_network_action(
            context,
            normalize_network_action("api.example.com", 443, "https"),
            tool_name="web_fetch",
        )
    with pytest.raises(RuleStoreCorruptError, match="schema"):
        service.authorize_exec_action(
            context,
            normalize_exec_action(["git", "status"], tmp_path),
            tool_name="terminal",
            risk_class="normal",
        )
