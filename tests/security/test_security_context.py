from __future__ import annotations

from pathlib import Path

import pytest

from crew.core.runctx import (
    current_agent_workdir,
    current_owner_account_id,
    current_request_id,
    current_session_id,
    current_task_runtime_id,
    current_workspace_id,
)
from crew.security.context import (
    SecurityContextError,
    build_gateway_security_context,
    build_security_context,
    path_is_in_workspace,
    resolve_requested_path,
)
from crew.security.local_path import LocalPathReference
from crew.security.launch import compile_process_launch
from crew.security.models import ConversationPermissionMode, FilesystemAccess
from crew.state.home import external_session_workspace_path, get_crew_home, task_workspace_path


class _WorkspaceStore:
    def __init__(self, roots: dict[tuple[str, str], Path | None]) -> None:
        self.roots = roots
        self.calls: list[tuple[str, str]] = []

    def get(self, workspace_id: str, owner_account_id: str = "") -> dict[str, str]:
        self.calls.append((owner_account_id, workspace_id))
        root = self.roots[(owner_account_id, workspace_id)]
        return {"id": workspace_id, "root_path": str(root) if root else ""}


def _set_runtime_context(*, owner: str, workspace: str, cwd: Path | None = None) -> None:
    current_owner_account_id.set(owner)
    current_workspace_id.set(workspace)
    current_session_id.set("session-1")
    current_request_id.set("request-1")
    current_task_runtime_id.set("task-1")
    current_agent_workdir.set(str(cwd) if cwd else "")


def test_context_uses_owner_scoped_store_root(tmp_path: Path) -> None:
    trusted_root = tmp_path / "trusted"
    attacker_root = tmp_path / "renderer-claimed"
    trusted_root.mkdir()
    attacker_root.mkdir()
    store = _WorkspaceStore({("acct-a", "project-1"): trusted_root})
    _set_runtime_context(owner="acct-a", workspace="project-1", cwd=trusted_root)

    context = build_security_context(store)

    assert context.owner_account_id == "acct-a"
    assert context.workspace_root == trusted_root.resolve()
    assert context.cwd == trusted_root.resolve()
    assert store.calls == [("acct-a", "project-1")]
    assert not path_is_in_workspace(context, attacker_root)


def test_normal_conversation_has_no_implicit_workspace_or_cwd() -> None:
    store = _WorkspaceStore({("acct-a", "default"): None})
    _set_runtime_context(owner="acct-a", workspace="default")

    context = build_security_context(store)

    # 合并保留了 dev 的 task workspace 兜底：未绑定项目的 workspace 仍有
    # 真实任务目录作为安全根；cwd 仍必须由运行时显式提供。
    from crew.state.home import task_workspace_path

    assert context.workspace_root == task_workspace_path(
        "default", owner_account_id="acct-a"
    ).resolve()
    assert context.cwd is None
    with pytest.raises(SecurityContextError, match="没有可信工作目录"):
        resolve_requested_path(
            context,
            LocalPathReference.parse("relative.txt"),
        )


def test_dotdot_is_classified_after_canonical_resolution(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = _WorkspaceStore({("acct-a", "project-1"): root})
    _set_runtime_context(owner="acct-a", workspace="project-1", cwd=root)
    context = build_security_context(store)

    target = resolve_requested_path(
        context,
        LocalPathReference.parse("../outside.txt"),
    )

    assert target == (tmp_path / "outside.txt").resolve()
    assert not path_is_in_workspace(context, target)


def test_symlink_escape_is_classified_by_final_target(tmp_path: Path) -> None:
    root = tmp_path / "project"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"当前 Windows 策略不允许创建测试 symlink: {exc}")
    store = _WorkspaceStore({("acct-a", "project-1"): root})
    _set_runtime_context(owner="acct-a", workspace="project-1", cwd=root)
    context = build_security_context(store)

    target = resolve_requested_path(
        context,
        LocalPathReference.from_host_path(link / "secret.txt"),
    )

    assert target == (outside / "secret.txt").resolve()
    assert not path_is_in_workspace(context, target)


def test_missing_trusted_owner_fails_closed(tmp_path: Path) -> None:
    store = _WorkspaceStore({})
    _set_runtime_context(owner="", workspace="project-1", cwd=tmp_path)

    with pytest.raises(SecurityContextError, match="可信账号"):
        build_security_context(store)


@pytest.fixture(autouse=True)
def _restore_process_context():
    original = {
        "owner": current_owner_account_id.get(),
        "workspace": current_workspace_id.get(),
        "session": current_session_id.get(),
        "request": current_request_id.get(),
        "task": current_task_runtime_id.get(),
        "cwd": current_agent_workdir.get(),
    }
    yield
    current_owner_account_id.set(original["owner"])
    current_workspace_id.set(original["workspace"])
    current_session_id.set(original["session"])
    current_request_id.set(original["request"])
    current_task_runtime_id.set(original["task"])
    current_agent_workdir.set(original["cwd"])


def test_normal_conversation_uses_owner_task_workspace_as_security_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CREW_HOME", str(tmp_path / "crew-home"))
    store = _WorkspaceStore({("acct-a", "default"): None})
    _set_runtime_context(owner="acct-a", workspace="default")

    context = build_security_context(store)

    from crew.state.home import task_workspace_path

    assert context.workspace_root == task_workspace_path(
        "default",
        owner_account_id="acct-a",
    ).resolve()
    assert context.cwd is None


def test_runtime_cwd_must_stay_inside_effective_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CREW_HOME", str(tmp_path / "crew-home"))
    outside = tmp_path / "outside"
    outside.mkdir()
    store = _WorkspaceStore({("acct-a", "default"): None})
    _set_runtime_context(owner="acct-a", workspace="default", cwd=outside)

    with pytest.raises(SecurityContextError, match="不属于已认证工作空间"):
        build_security_context(store)


def test_gateway_context_gives_unbound_workspace_an_explicit_task_root(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    monkeypatch.setenv("CREW_TASK_WORKSPACE_ROOT", str(tmp_path / "task-output"))
    store = _WorkspaceStore({("acct-a", "default"): None})
    task_root = task_workspace_path("default", owner_account_id="acct-a")
    external_cwd = external_session_workspace_path(
        "default",
        "session-1",
        "external-1",
        owner_account_id="acct-a",
    )

    context = build_gateway_security_context(
        store,
        owner_account_id="acct-a",
        workspace_id="default",
        session_id="session-1",
        cwd=external_cwd,
    )

    assert context.workspace_root == task_root.resolve()
    assert context.cwd == external_cwd.resolve()
    assert context.cwd.is_relative_to(context.workspace_root)
    launch = compile_process_launch(
        context,
        ConversationPermissionMode.REQUEST_APPROVAL,
        db_path=tmp_path / "crew.db",
    )
    assert any(
        entry.root == task_root.resolve() and entry.access is FilesystemAccess.READ_WRITE
        for entry in launch.profile.filesystem
    )


def test_task_workspace_is_not_under_a_crew_home_deny_root(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    monkeypatch.setenv("CREW_TASK_WORKSPACE_ROOT", str(tmp_path / "task-output"))
    store = _WorkspaceStore({("acct-a", "default"): None})
    task_root = task_workspace_path("default", owner_account_id="acct-a")
    context = build_gateway_security_context(
        store,
        owner_account_id="acct-a",
        workspace_id="default",
        session_id="session-1",
        cwd=task_root,
    )

    launch = compile_process_launch(
        context,
        ConversationPermissionMode.REQUEST_APPROVAL,
        db_path=tmp_path / "crew.db",
    )
    denied_roots = {
        entry.root
        for entry in launch.profile.filesystem
        if entry.access is FilesystemAccess.DENY
    }

    assert get_crew_home().resolve() not in denied_roots
    assert (get_crew_home() / "crew_data").resolve() in denied_roots
    assert (get_crew_home() / "logs").resolve() in denied_roots
    assert task_root.resolve() not in denied_roots


def test_active_unbound_context_reuses_task_root_for_file_policy(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    monkeypatch.setenv("CREW_TASK_WORKSPACE_ROOT", str(tmp_path / "task-output"))
    store = _WorkspaceStore({("acct-a", "default"): None})
    task_root = task_workspace_path("default", owner_account_id="acct-a")
    external_cwd = external_session_workspace_path(
        "default",
        "session-1",
        "external-1",
        owner_account_id="acct-a",
    )
    _set_runtime_context(owner="acct-a", workspace="default", cwd=external_cwd)

    context = build_security_context(store)

    assert context.workspace_root == task_root.resolve()
    assert context.cwd == external_cwd.resolve()


def test_gateway_context_rejects_cwd_outside_unbound_task_root(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    monkeypatch.setenv("CREW_TASK_WORKSPACE_ROOT", str(tmp_path / "task-output"))
    store = _WorkspaceStore({("acct-a", "default"): None})
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(SecurityContextError, match="cwd 不属于已认证工作空间"):
        build_gateway_security_context(
            store,
            owner_account_id="acct-a",
            workspace_id="default",
            session_id="session-1",
            cwd=outside,
        )
