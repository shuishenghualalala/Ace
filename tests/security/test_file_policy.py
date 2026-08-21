from __future__ import annotations

import asyncio
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from crew.app import build_app

from crew.security.context import SecurityContext
from crew.security.file_policy import (
    FilePolicyResult,
    FilesystemAccess,
    _discovered_sensitive_entries,
    _protected_entries,
    assess_file_action,
)
from crew.security.actions import ActionKind, NormalizedAction, normalize_file_action
from crew.security.models import ConversationPermissionMode
from crew.core.runctx import (
    current_agent_workdir,
    current_owner_account_id,
    current_push_fn,
    current_request_id,
    current_session_id,
    current_task_runtime_id,
    current_workspace_id,
)
from crew.core.types import ToolCall
from crew.security.approvals import ApprovalDecision
from crew.security.context import build_security_context
from crew.security.launch import compile_process_launch
from crew.state.config import Config


@pytest.mark.skipif(sys.platform.startswith("linux"), reason="non-Linux discovery path")
def test_unreadable_workspace_directory_is_denied_instead_of_aborting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    (tmp_path / ".env").write_text("secret", encoding="utf-8")
    real_iterdir = Path.iterdir

    def iterdir(self: Path):
        if self == locked:
            raise PermissionError(5, "Access is denied")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    context = SecurityContext(
        os_user="user",
        owner_account_id="owner",
        workspace_id="workspace",
        workspace_root=tmp_path,
        session_id="session",
        request_id="request",
        task_id="task",
        cwd=tmp_path,
    )

    entries = _discovered_sensitive_entries(context)

    assert any(
        entry.root == locked
        and entry.access is FilesystemAccess.DENY
        and not entry.escalatable
        for entry in entries
    )
    assert any(entry.root == tmp_path / ".env" for entry in entries)


def test_non_linux_sensitive_discovery_is_part_of_file_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    secret = nested / ".env.local"
    secret.write_text("TOKEN=secret", encoding="utf-8")
    monkeypatch.setattr("crew.security.file_policy.sys.platform", "win32")
    context = SecurityContext(
        os_user="user",
        owner_account_id="owner",
        workspace_id="workspace",
        workspace_root=tmp_path,
        session_id="session",
        request_id="request",
        task_id="task",
        cwd=tmp_path,
    )

    protected = _protected_entries(context, tmp_path / "crew.db")

    assert any(entry.root == secret and entry.access is FilesystemAccess.DENY for entry in protected)


def test_file_policy_denies_link_and_special_file_targets(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "target.txt"
    target.write_text("safe", encoding="utf-8")
    link = workspace / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation unavailable")

    context = SecurityContext(
        os_user="user",
        owner_account_id="owner",
        workspace_id="workspace",
        workspace_root=workspace,
        session_id="session",
        request_id="request",
        task_id="task",
        cwd=workspace,
    )
    link_assessment = assess_file_action(
        context,
        NormalizedAction(kind=ActionKind.FILE, path=str(link), operation="read"),
        ConversationPermissionMode.READ_ONLY,
        db_path=tmp_path / "crew.db",
    )
    assert link_assessment.result is FilePolicyResult.DENY

    if hasattr(os, "mkfifo"):
        fifo = workspace / "pipe"
        os.mkfifo(fifo)
        try:
            special_assessment = assess_file_action(
                context,
                NormalizedAction(kind=ActionKind.FILE, path=str(fifo), operation="read"),
                ConversationPermissionMode.READ_ONLY,
                db_path=tmp_path / "crew.db",
            )
            assert special_assessment.result is FilePolicyResult.DENY
        finally:
            fifo.unlink()


@contextmanager
def _security_context(root, *, owner="owner-a", session="session-a"):
    tokens = [
        (current_owner_account_id, current_owner_account_id.set(owner)),
        (current_workspace_id, current_workspace_id.set("default")),
        (current_session_id, current_session_id.set(session)),
        (current_request_id, current_request_id.set("request-a")),
        (current_task_runtime_id, current_task_runtime_id.set("task-a")),
        (current_agent_workdir, current_agent_workdir.set(str(root))),
        # 合并后安全语义：无审批 UI（push 通道）时审批请求直接 fail-closed，
        # 不会阻塞等待 owner；测试注入 push 通道让审批流可被驱动。
        (current_push_fn, current_push_fn.set(lambda session_id, payload: True)),
    ]
    try:
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


async def _wait_for_pending(app, context, tool_name, timeout_s: float = 2.0):
    """轮询直到指定工具的 pending 审批请求出现（工具已阻塞在 await_decision）。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        match = [r for r in app.security_approvals.list_pending(context) if r.tool_name == tool_name]
        if match:
            return match[0]
        await asyncio.sleep(0.01)
    raise AssertionError(f"未出现 {tool_name} 的审批请求（工具未阻塞等待审批）")


async def _drive(app, tc: ToolCall, decision: ApprovalDecision, *, always_argv_prefix=None):
    """启动一个会阻塞在审批上的工具调用，驱动决策，返回其结果。

    新契约下工具不再"立即抛 SECURITY_APPROVAL_REQUIRED"，而是挂起等待 owner 决策：
    批准则继续执行（消费 once grant），拒绝则回灌"用户未批准"干净错误。
    """
    context = build_security_context(app.workspace_store)
    task = asyncio.ensure_future(app.registry.execute(tc))
    request = await _wait_for_pending(app, context, tc.name)
    app.security_service.decide(
        context,
        request_id=request.request_id,
        nonce=request.nonce,
        decision=decision,
        always_argv_prefix=always_argv_prefix,
    )
    return await task


@pytest.fixture
def secured_app(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / "home"))
    app = build_app(
        Config(
            db_path=str(tmp_path / "crew.db"),
            plugins_enabled=[],
            security_enabled=True,
        ),
        enable_team=False,
    )
    project = tmp_path / "project"
    project.mkdir()
    app.workspace_store.get("default", owner_account_id="owner-a")
    app.workspace_store.update("default", owner_account_id="owner-a", root_path=str(project))
    yield app, project
    app.security_rules.close()
    app.security_audit.close()


@pytest.mark.asyncio
async def test_external_read_is_allowed_without_approval(secured_app, tmp_path):
    app, project = secured_app
    outside = tmp_path / "outside.txt"
    outside.write_text("hello-outer", encoding="utf-8")
    with _security_context(project):
        result = await app.registry.execute(
            ToolCall("r1", "file_read", {"path": str(outside)})
        )
    assert not result.is_error
    assert "hello-outer" in result.content


@pytest.mark.asyncio
async def test_request_mode_allows_workspace_write_and_traversal_read(secured_app, tmp_path):
    app, project = secured_app
    inside = project / "inside.txt"
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    with _security_context(project):
        written = await app.registry.execute(
            ToolCall("w1", "file_write", {"path": str(inside), "content": "inside"})
        )
        escaped = await app.registry.execute(
            ToolCall("r1", "file_read", {"path": "../outside.txt"})
        )
    assert not written.is_error
    assert not escaped.is_error
    assert "outside" in escaped.content


@pytest.mark.asyncio
async def test_once_external_file_write_approval_is_consumed_only_by_matching_action(
    secured_app,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    app, project = secured_app
    outside = tmp_path / "outside.txt"
    outside.write_text("initial", encoding="utf-8")
    with _security_context(project):
        # r1 阻塞→批准 ONCE→本次调用消费 grant 并成功。
        first = await _drive(
            app,
            ToolCall("w1", "file_write", {"path": str(outside), "content": "one"}),
            ApprovalDecision.ONCE,
        )
        # r2 不同 action（offset=2）不被 r1 的 once grant 覆盖→阻塞→拒绝。
        other = await _drive(
            app,
            ToolCall("w2", "file_write", {"path": str(outside), "content": "two"}),
            ApprovalDecision.REJECT,
        )
        # r3 与 r1 同 action，但 once grant 已被 r1 消费→再次阻塞→拒绝。
        replay = await _drive(
            app,
            ToolCall("w3", "file_write", {"path": str(outside), "content": "one"}),
            ApprovalDecision.REJECT,
        )
    assert not first.is_error
    assert other.is_error and "用户未批准" in other.content
    assert replay.is_error and "用户未批准" in replay.content


@pytest.mark.asyncio
async def test_project_metadata_is_approvable_but_internal_database_is_not(secured_app):
    app, project = secured_app
    git_dir = project / ".git"
    git_dir.mkdir()
    config = git_dir / "config"
    config.write_text("safe", encoding="utf-8")
    with _security_context(project):
        read = await app.registry.execute(ToolCall("r1", "file_read", {"path": str(config)}))
        write = await _drive(
            app,
            ToolCall("w1", "file_write", {"path": str(config), "content": "changed"}),
            ApprovalDecision.ONCE,
        )
        database = await app.registry.execute(
            ToolCall("r2", "file_read", {"path": app.config.db_path})
        )
    assert not read.is_error
    assert not write.is_error
    assert database.is_error and "SECURITY_FILE_DENIED" in database.content
    assert config.read_text(encoding="utf-8") == "changed"


@pytest.mark.asyncio
async def test_symlink_read_escape_is_allowed_by_broad_read_policy(secured_app, tmp_path):
    app, project = secured_app
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = project / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with _security_context(project):
        result = await app.registry.execute(ToolCall("r1", "file_read", {"path": str(link)}))
    assert not result.is_error
    assert "secret" in result.content


@pytest.mark.asyncio
async def test_auto_review_allows_external_read_but_requires_external_write_approval(
    secured_app,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    app, project = secured_app
    outside = tmp_path / "outside.txt"
    outside.write_text("safe", encoding="utf-8")
    with _security_context(project):
        context = build_security_context(app.workspace_store)
        app.security_service.set_mode(context, ConversationPermissionMode.AUTO_REVIEW)
        read = await app.registry.execute(
            ToolCall("r1", "file_read", {"path": str(outside)})
        )
        write = await _drive(
            app,
            ToolCall("w1", "file_write", {"path": str(outside), "content": "changed"}),
            ApprovalDecision.REJECT,
        )
    assert not read.is_error and "safe" in read.content
    assert write.is_error and "用户未批准" in write.content


@pytest.mark.asyncio
async def test_full_access_uses_host_authority_except_permanent_root_hardline(
    secured_app,
    tmp_path,
):
    app, project = secured_app
    outside = tmp_path / "outside.txt"
    with _security_context(project):
        context = build_security_context(app.workspace_store)
        app.security_service.set_mode(context, ConversationPermissionMode.FULL_ACCESS)
        # 合并后安全语义：FULL_ACCESS 的结构化文件工具仍绑定 workspace 剖面，
        # 越界写必须走审批（宿主越权唯一出口是获批授权），不再直接以宿主身份落盘。
        write = await _drive(
            app,
            ToolCall("w1", "file_write", {"path": str(outside), "content": "changed"}),
            ApprovalDecision.ONCE,
        )
        root_assessment = app.security_service.authorize_file_action(
            context,
            normalize_file_action(project.anchor, "write"),
            tool_name="file_write",
        )
        launch = compile_process_launch(
            context,
            ConversationPermissionMode.FULL_ACCESS,
            db_path=Path(app.config.db_path),
        )
    assert not write.is_error
    assert outside.read_text(encoding="utf-8") == "changed"
    assert root_assessment[0].value == "deny"
    # 合并后安全语义：FULL_ACCESS 一律编译为 managed 启动（沙箱内执行），
    # 宿主越权只能经获批的 require_escalated。
    assert launch.managed


@pytest.mark.asyncio
async def test_patch_and_search_use_the_same_evaluator(secured_app, tmp_path):
    app, project = secured_app
    source = project / "source.py"
    source.write_text("value = 'old'\n", encoding="utf-8")
    external = tmp_path / "external"
    external.mkdir()
    (external / "outside.py").write_text("needle\n", encoding="utf-8")
    with _security_context(project):
        patched = await app.registry.execute(
            ToolCall(
                "p1",
                "patch",
                {"path": str(source), "old": "old", "new": "new"},
            )
        )
        inside_search = await app.registry.execute(
            ToolCall("g1", "grep", {"path": str(project), "pattern": "new"})
        )
        outside_glob = await app.registry.execute(
            ToolCall("g2", "glob", {"path": str(external), "pattern": "*.py"})
        )
        approved_glob = await app.registry.execute(
            ToolCall("g3", "glob", {"path": str(external), "pattern": "*.py"})
        )
        enlarged = await app.registry.execute(
            ToolCall("g4", "glob", {"path": str(tmp_path), "pattern": "*.py"})
        )
    assert not patched.is_error and '"diff"' in patched.content
    assert not inside_search.is_error
    assert not outside_glob.is_error
    assert not approved_glob.is_error
    assert not enlarged.is_error
