"""Structured file tools share one owner/workspace-aware policy evaluator."""

from __future__ import annotations

import asyncio
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from crew.app import build_app
from crew.core.errors import ToolError
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
from crew.security.actions import normalize_file_action
from crew.security.approvals import ApprovalDecision
from crew.security.context import build_security_context
from crew.security.launch import compile_process_launch
from crew.security.models import ConversationPermissionMode
from crew.state.config import Config
from crew.tools.security_guard import authorize_file_tool


@contextmanager
def _security_context(root, *, owner="owner-a", session="session-a"):
    tokens = [
        (current_owner_account_id, current_owner_account_id.set(owner)),
        (current_workspace_id, current_workspace_id.set("default")),
        (current_session_id, current_session_id.set(session)),
        (current_request_id, current_request_id.set("request-a")),
        (current_task_runtime_id, current_task_runtime_id.set("task-a")),
        (current_agent_workdir, current_agent_workdir.set(str(root))),
        (current_push_fn, current_push_fn.set(lambda *_args, **_kwargs: None)),
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
        match = [
            r for r in app.security_approvals.list_pending(context) if r.tool_name == tool_name
        ]
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("workspace_store", "security_service"),
    [
        (None, object()),
        (object(), None),
        (None, None),
    ],
    ids=["missing-workspace-store", "missing-security-service", "missing-both"],
)
async def test_file_authorization_missing_dependencies_never_falls_back_to_path_resolution(
    monkeypatch,
    workspace_store,
    security_service,
) -> None:
    fallback_calls = 0

    def unexpected_fallback(_raw_path):
        nonlocal fallback_calls
        fallback_calls += 1
        raise AssertionError("missing security dependencies must not call _resolve_path")

    monkeypatch.setattr("crew.tools.file_utils._resolve_path", unexpected_fallback)

    with pytest.raises(ToolError, match="缺少安全授权上下文"):
        await authorize_file_tool(
            {"path": "model-controlled.txt"},
            operation="read",
            tool_name="file_read",
            workspace_store=workspace_store,
            security_service=security_service,
        )

    assert fallback_calls == 0


@pytest.fixture
def secured_app(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / "home"))
    app = build_app(
        Config(db_path=str(tmp_path / "crew.db"), plugins_enabled=[]),
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
async def test_blocked_external_read_resumes_on_approve_and_cleanly_errors_on_reject(
    secured_app, tmp_path
):
    """核心新契约：审批阻塞-恢复。批准→读到内容；拒绝→干净错误（无 path/request_id 泄漏）。"""
    app, project = secured_app
    outside = tmp_path / "outside.txt"
    outside.write_text("hello-outer", encoding="utf-8")
    with _security_context(project):
        approved = await _drive(
            app,
            ToolCall("r1", "file_read", {"path": str(outside)}),
            ApprovalDecision.ONCE,
        )
        rejected = await _drive(
            app,
            ToolCall("r2", "file_read", {"path": str(outside)}),
            ApprovalDecision.REJECT,
        )
    assert not approved.is_error
    assert "hello-outer" in approved.content
    assert rejected.is_error
    assert "用户未批准" in rejected.content
    # 关键：不再把 SECURITY_APPROVAL_REQUIRED 的 JSON 回灌模型（那会被复述成正文）。
    assert "SECURITY_APPROVAL_REQUIRED" not in rejected.content
    assert str(outside) not in rejected.content


@pytest.mark.asyncio
async def test_project_file_allowed_and_traversal_outside_requests_approval(secured_app, tmp_path):
    app, project = secured_app
    inside = project / "private" / "nested" / "inside.txt"
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    with _security_context(project):
        written = await app.registry.execute(
            ToolCall("w1", "file_write", {"path": str(inside), "content": "inside"})
        )
        escaped = await _drive(
            app,
            ToolCall("r1", "file_read", {"path": "../outside.txt"}),
            ApprovalDecision.REJECT,
        )
    assert not written.is_error
    assert inside.read_text(encoding="utf-8") == "inside"
    assert escaped.is_error
    assert "用户未批准" in escaped.content


@pytest.mark.asyncio
async def test_exact_once_file_approval_is_consumed_only_by_matching_read(secured_app, tmp_path):
    app, project = secured_app
    outside = tmp_path / "outside.txt"
    outside.write_text("one\ntwo\nthree\n", encoding="utf-8")
    with _security_context(project):
        # r1 阻塞→批准 ONCE→本次调用消费 grant 并成功。
        first = await _drive(
            app,
            ToolCall("r1", "file_read", {"path": str(outside), "offset": 1, "limit": 1}),
            ApprovalDecision.ONCE,
        )
        # r2 不同 action（offset=2）不被 r1 的 once grant 覆盖→阻塞→拒绝。
        other = await _drive(
            app,
            ToolCall("r2", "file_read", {"path": str(outside), "offset": 2, "limit": 1}),
            ApprovalDecision.REJECT,
        )
        # r3 与 r1 同 action，但 once grant 已被 r1 消费→再次阻塞→拒绝。
        replay = await _drive(
            app,
            ToolCall("r3", "file_read", {"path": str(outside), "offset": 1, "limit": 1}),
            ApprovalDecision.REJECT,
        )
    assert not first.is_error
    assert other.is_error and "用户未批准" in other.content
    assert replay.is_error and "用户未批准" in replay.content


@pytest.mark.asyncio
async def test_protected_metadata_and_internal_database_are_not_approvable(secured_app):
    app, project = secured_app
    git_dir = project / ".git"
    git_dir.mkdir()
    config = git_dir / "config"
    config.write_text("safe", encoding="utf-8")
    with _security_context(project):
        read = await app.registry.execute(ToolCall("r1", "file_read", {"path": str(config)}))
        write = await app.registry.execute(
            ToolCall("w1", "file_write", {"path": str(config), "content": "changed"})
        )
        database = await app.registry.execute(
            ToolCall("r2", "file_read", {"path": app.config.db_path})
        )
    assert not read.is_error
    assert write.is_error and "SECURITY_FILE_DENIED" in write.content
    assert database.is_error and "SECURITY_FILE_DENIED" in database.content
    assert str(config) not in write.content
    assert str(Path(app.config.db_path)) not in database.content
    assert config.read_text(encoding="utf-8") == "safe"


@pytest.mark.asyncio
async def test_environment_and_credential_files_are_non_escalatable(secured_app):
    app, project = secured_app
    dotenv = project / ".env.production"
    private_key = project / "id_ed25519"
    nested_dotenv = project.joinpath(*(f"level-{index}" for index in range(8))) / ".env.local"
    nested_dotenv.parent.mkdir(parents=True)
    dotenv.write_text("TOKEN=workspace-secret", encoding="utf-8")
    private_key.write_text("private-key", encoding="utf-8")
    nested_dotenv.write_text("TOKEN=nested-secret", encoding="utf-8")

    with _security_context(project):
        dotenv_read = await app.registry.execute(
            ToolCall("r-env", "file_read", {"path": str(dotenv)})
        )
        dotenv_write = await app.registry.execute(
            ToolCall("w-env", "file_write", {"path": str(dotenv), "content": "changed"})
        )
        key_read = await app.registry.execute(
            ToolCall("r-key", "file_read", {"path": str(private_key)})
        )
        context = build_security_context(app.workspace_store)
        launch = compile_process_launch(
            context,
            ConversationPermissionMode.REQUEST_APPROVAL,
            db_path=Path(app.config.db_path),
        )

    assert dotenv_read.is_error and "SECURITY_FILE_DENIED" in dotenv_read.content
    assert dotenv_write.is_error and "SECURITY_FILE_DENIED" in dotenv_write.content
    assert key_read.is_error and "SECURITY_FILE_DENIED" in key_read.content
    assert any(
        entry.root == dotenv and entry.access.value == "deny" for entry in launch.profile.filesystem
    )
    assert any(
        entry.root == private_key and entry.access.value == "deny"
        for entry in launch.profile.filesystem
    )
    if sys.platform.startswith("linux"):
        assert {entry.pattern for entry in launch.profile.filesystem_globs} >= {
            "**/.env.*",
            "**/id_ed25519",
        }
    else:
        assert launch.profile.filesystem_globs == ()
        assert any(
            entry.root == nested_dotenv and entry.access.value == "deny"
            for entry in launch.profile.filesystem
        )


@pytest.mark.asyncio
async def test_read_only_mode_rejects_workspace_writes_without_prompt(secured_app):
    app, project = secured_app
    source = project / "source.txt"
    source.write_text("unchanged", encoding="utf-8")

    with _security_context(project):
        context = build_security_context(app.workspace_store)
        app.security_service.set_mode(context, ConversationPermissionMode.READ_ONLY)
        read = await app.registry.execute(ToolCall("read", "file_read", {"path": str(source)}))
        write = await app.registry.execute(
            ToolCall("write", "file_write", {"path": str(source), "content": "changed"})
        )

    assert not read.is_error
    assert write.is_error and "SECURITY_FILE_DENIED" in write.content
    assert source.read_text(encoding="utf-8") == "unchanged"


@pytest.mark.asyncio
async def test_symlink_escape_is_evaluated_by_final_target(secured_app, tmp_path):
    app, project = secured_app
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = project / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with _security_context(project):
        result = await _drive(
            app, ToolCall("r1", "file_read", {"path": str(link)}), ApprovalDecision.REJECT
        )
    assert result.is_error
    assert "用户未批准" in result.content


@pytest.mark.asyncio
async def test_auto_review_still_requires_approval_for_external_read_and_write(
    secured_app, tmp_path
):
    app, project = secured_app
    outside = tmp_path / "outside.txt"
    outside.write_text("safe", encoding="utf-8")
    with _security_context(project):
        context = build_security_context(app.workspace_store)
        app.security_service.set_mode(context, ConversationPermissionMode.AUTO_REVIEW)
        read = await _drive(
            app,
            ToolCall("r1", "file_read", {"path": str(outside)}),
            ApprovalDecision.REJECT,
        )
        write = await _drive(
            app,
            ToolCall("w1", "file_write", {"path": str(outside), "content": "changed"}),
            ApprovalDecision.REJECT,
        )
    assert read.is_error and "用户未批准" in read.content
    assert write.is_error and "用户未批准" in write.content


@pytest.mark.asyncio
async def test_full_access_is_managed_and_cannot_modify_security_control_plane(
    secured_app,
    tmp_path,
    monkeypatch,
):
    app, project = secured_app
    outside = Path.home() / f"ace-full-access-{tmp_path.name}.txt"
    runtime_root = tmp_path / "bundled-python"
    runtime_root.mkdir()
    monkeypatch.setattr(
        "crew.state.home.bundled_runtime_roots",
        lambda: (runtime_root.resolve(strict=True),),
        raising=False,
    )
    try:
        with _security_context(project):
            context = build_security_context(app.workspace_store)
            app.security_service.set_mode(context, ConversationPermissionMode.FULL_ACCESS)
            write = await app.registry.execute(
                ToolCall("w1", "file_write", {"path": str(outside), "content": "changed"})
            )
            database = await app.registry.execute(
                ToolCall("w2", "file_write", {"path": app.config.db_path, "content": "tampered"})
            )
            assessment = app.security_service.authorize_file_action(
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
        assert database.is_error and "SECURITY_FILE_DENIED" in database.content
        assert assessment[0].value == "deny"
        assert launch.managed
        from crew.agent.skills import get_builtin_skills_dir

        assert set(launch.trusted_readable_roots) == {
            get_builtin_skills_dir().resolve(strict=True),
            runtime_root.resolve(strict=True),
        }
        assert all(
            entry.root not in launch.trusted_readable_roots for entry in launch.profile.filesystem
        )
    finally:
        outside.unlink(missing_ok=True)


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
        # 项目外 glob 阻塞→批准 SESSION→本次及同会话同路径后续都命中 session grant。
        outside_glob = await _drive(
            app,
            ToolCall("g2", "glob", {"path": str(external), "pattern": "*.py"}),
            ApprovalDecision.SESSION,
        )
        approved_glob = await app.registry.execute(
            ToolCall("g3", "glob", {"path": str(external), "pattern": "*.py"})
        )
        # 父目录（不同路径）不被 session grant 覆盖→阻塞→拒绝。
        enlarged = await _drive(
            app,
            ToolCall("g4", "glob", {"path": str(tmp_path), "pattern": "*.py"}),
            ApprovalDecision.REJECT,
        )
    assert not patched.is_error and '"diff"' in patched.content
    assert not inside_search.is_error
    assert not outside_glob.is_error
    assert not approved_glob.is_error
    assert enlarged.is_error and "用户未批准" in enlarged.content
