"""Build security identity and workspace facts only from host-owned runtime state."""

from __future__ import annotations

import getpass
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from crew.core.runctx import (
    current_agent_workdir,
    current_owner_account_id,
    current_request_id,
    current_session_id,
    current_task_runtime_id,
    current_workspace_id,
)
from crew.state.home import task_workspace_path
from crew.state.workspace_store import _normalize_root_path


class SecurityContextError(RuntimeError):
    """Raised when trusted runtime facts are absent or inconsistent."""


@dataclass(frozen=True)
class SecurityContext:
    """Host-derived identity and filesystem anchors for one execution decision."""

    os_user: str
    owner_account_id: str
    workspace_id: str
    workspace_root: Path | None
    session_id: str
    request_id: str
    task_id: str
    cwd: Path | None


def build_security_context(workspace_store: Any) -> SecurityContext:
    """Read owner/session/task contextvars and the owner-scoped workspace store."""
    owner = current_owner_account_id.get().strip()
    if not owner:
        raise SecurityContextError("缺少可信账号上下文")
    workspace_id = current_workspace_id.get().strip() or "default"
    try:
        workspace = workspace_store.get(workspace_id, owner_account_id=owner)
    except (KeyError, OSError, ValueError) as exc:
        raise SecurityContextError("可信工作空间不存在或不可用") from exc

    normalized_root = _normalize_root_path(str(workspace.get("root_path") or ""))
    cwd = _canonical_runtime_cwd(current_agent_workdir.get())
    workspace_root = Path(normalized_root) if normalized_root else None
    if workspace_root is None and cwd is not None:
        task_root = task_workspace_path(workspace_id, owner_account_id=owner)
        try:
            cwd.relative_to(task_root)
        except ValueError:
            pass
        else:
            workspace_root = task_root
    return SecurityContext(
        os_user=getpass.getuser(),
        owner_account_id=owner,
        workspace_id=workspace_id,
        workspace_root=workspace_root,
        session_id=current_session_id.get().strip(),
        request_id=current_request_id.get().strip(),
        task_id=current_task_runtime_id.get().strip(),
        cwd=cwd,
    )


def build_gateway_security_context(
    workspace_store: Any,
    *,
    owner_account_id: str,
    workspace_id: str,
    session_id: str,
    task_id: str = "",
    request_id: str = "",
    cwd: str | Path | None = None,
) -> SecurityContext:
    """Build a context from authenticated Gateway identity and owner-scoped storage.

    The caller must supply the owner from ``request.state.account``. Workspace
    facts are reloaded with that owner instead of trusting renderer payloads.
    """
    owner = str(owner_account_id).strip()
    workspace_key = str(workspace_id).strip() or "default"
    session = str(session_id).strip()
    if not owner or not session:
        raise SecurityContextError("owner_account_id 和 session_id 不能为空")
    try:
        workspace = workspace_store.get(workspace_key, owner_account_id=owner)
    except (KeyError, OSError, ValueError) as exc:
        raise SecurityContextError("可信工作空间不存在或不可用") from exc
    normalized_root = _normalize_root_path(str(workspace.get("root_path") or ""))
    # An unbound workspace still has a host-owned task root. Keep that root in
    # the same security context as a user-bound project root so managed ACP/CLI
    # sessions can use their isolated child cwd without widening permissions.
    root = (
        Path(normalized_root)
        if normalized_root
        else task_workspace_path(workspace_key, owner_account_id=owner)
    )
    requested_cwd = Path(cwd).expanduser().resolve(strict=False) if cwd else root
    if requested_cwd is not None:
        try:
            requested_cwd.relative_to(root)
        except ValueError as exc:
            raise SecurityContextError("cwd 不属于已认证工作空间") from exc
    return SecurityContext(
        os_user=getpass.getuser(),
        owner_account_id=owner,
        workspace_id=workspace_key,
        workspace_root=root,
        session_id=session,
        request_id=str(request_id).strip(),
        task_id=str(task_id).strip(),
        cwd=requested_cwd,
    )


def resolve_requested_path(context: SecurityContext, requested: str | Path) -> Path:
    """Resolve a requested path through existing links before applying policy."""
    target = Path(requested).expanduser()
    if not target.is_absolute():
        if context.cwd is None:
            raise SecurityContextError("普通对话没有可信工作目录，不能解析相对路径")
        target = context.cwd / target
    try:
        return target.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SecurityContextError("路径无法安全解析") from exc


def path_is_in_workspace(context: SecurityContext, target: str | Path) -> bool:
    """Return whether the final canonical target is under the trusted workspace root."""
    if context.workspace_root is None:
        return False
    try:
        resolve_requested_path(context, target).relative_to(context.workspace_root)
    except (SecurityContextError, ValueError):
        return False
    return True


def _canonical_runtime_cwd(value: str) -> Path | None:
    raw = value.strip()
    if not raw:
        return None
    try:
        cwd = Path(raw).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SecurityContextError("可信工作目录不存在或无法解析") from exc
    if not cwd.is_dir():
        raise SecurityContextError("可信工作目录不是目录")
    return cwd
