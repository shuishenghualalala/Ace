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
    build_security_context,
    path_is_in_workspace,
    resolve_requested_path,
)


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


def test_dotdot_is_classified_after_canonical_resolution(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = _WorkspaceStore({("acct-a", "project-1"): root})
    _set_runtime_context(owner="acct-a", workspace="project-1", cwd=root)
    context = build_security_context(store)

    target = resolve_requested_path(context, "../outside.txt")

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

    target = resolve_requested_path(context, link / "secret.txt")

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
