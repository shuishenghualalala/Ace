"""Short-lived exact execution grants issued only by host approval state."""

from __future__ import annotations

import threading
import time
import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from crew.security.actions import ActionKind, ActionScope, NormalizedAction, security_context_digest
from crew.security.context import SecurityContext
from crew.security.models import (
    EMPTY_ADDITIONAL_PERMISSIONS,
    AdditionalPermissionProfile,
    FilesystemEntry,
    PermissionGrantScope,
    SandboxPermissions,
    additional_permissions_cover,
    merge_additional_permissions,
)
from crew.security.policy import normalize_additional_permissions
from crew.security.rules import RuleScope


class GrantError(RuntimeError):
    """Raised when an execution grant cannot authorize the requested action."""


@dataclass(frozen=True)
class ExecutionGrant:
    grant_id: str
    scope: RuleScope
    action_digest: str
    action_kind: ActionKind
    os_user: str
    owner_account_id: str
    workspace_id: str
    workspace_root: Path | None
    session_id: str
    task_id: str
    expires_monotonic: float | None
    additional_permissions: AdditionalPermissionProfile = AdditionalPermissionProfile()
    permission_grant_id: str | None = None
    action_scope_digest: str = ""
    context_digest: str = ""
    tool_name: str = ""

    @property
    def scope_digest(self) -> str:
        return self.action_scope_digest


@dataclass(frozen=True)
class PermissionGrant:
    grant_id: str
    scope: PermissionGrantScope
    additional_permissions: AdditionalPermissionProfile
    os_user: str
    owner_account_id: str
    workspace_id: str
    workspace_root: Path | None
    session_id: str
    task_id: str
    expires_monotonic: float | None
    tool_name: str = ""


class GrantRegistry:
    """Keep once/session grants in host memory so restart revokes them."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._grants: dict[str, ExecutionGrant] = {}
        self._permission_grants: dict[str, PermissionGrant] = {}

    def issue(
        self,
        context: SecurityContext,
        action: NormalizedAction,
        scope: RuleScope,
        *,
        expires_monotonic: float | None = None,
        additional_permissions: AdditionalPermissionProfile = AdditionalPermissionProfile(),
        action_scope: ActionScope | None = None,
        ttl_seconds: float | None = None,
        tool_name: str = "",
    ) -> ExecutionGrant:
        if scope not in {RuleScope.ONCE, RuleScope.SESSION}:
            raise ValueError("execution grant 只支持 once/session")
        if ttl_seconds is not None and (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, (int, float))
            or not math.isfinite(ttl_seconds)
            or ttl_seconds <= 0
        ):
            raise GrantError("grant TTL 必须大于 0")
        if ttl_seconds is not None and expires_monotonic is not None:
            raise GrantError("grant 不能同时指定 TTL 和 expiry")
        if expires_monotonic is not None and (
            isinstance(expires_monotonic, bool)
            or not isinstance(expires_monotonic, (int, float))
            or not math.isfinite(expires_monotonic)
        ):
            raise ValueError("grant expiry 必须是有限单调时钟值")
        _validate_bound_context(context, require_task=scope is RuleScope.ONCE)
        if action_scope is not None and (
            not isinstance(action_scope, ActionScope) or action_scope.action.digest != action.digest
        ):
            raise GrantError("grant action scope 不匹配")
        if ttl_seconds is not None:
            expires_monotonic = self._clock() + float(ttl_seconds)
        additional_permissions = normalize_additional_permissions(additional_permissions)
        grant_id = uuid4().hex
        permission_grant_id = None
        if scope is RuleScope.SESSION and (
            additional_permissions.filesystem
            or (
                additional_permissions.network
                and action.kind is not ActionKind.NETWORK
            )
            or additional_permissions.allow_local_binding
        ):
            permission_grant_id = uuid4().hex
        grant = ExecutionGrant(
            grant_id=grant_id,
            scope=scope,
            action_digest=action.digest,
            action_kind=action.kind,
            os_user=context.os_user,
            owner_account_id=context.owner_account_id,
            workspace_id=context.workspace_id,
            workspace_root=context.workspace_root,
            session_id=context.session_id,
            task_id=context.task_id,
            expires_monotonic=expires_monotonic,
            additional_permissions=additional_permissions,
            permission_grant_id=permission_grant_id,
            action_scope_digest=action_scope.digest if action_scope is not None else "",
            context_digest=security_context_digest(context),
            tool_name=str(tool_name).strip(),
        )
        with self._lock:
            self._grants[grant.grant_id] = grant
            if permission_grant_id is not None:
                self._permission_grants[permission_grant_id] = PermissionGrant(
                    grant_id=permission_grant_id,
                    scope=PermissionGrantScope.SESSION,
                    additional_permissions=additional_permissions,
                    os_user=context.os_user,
                    owner_account_id=context.owner_account_id,
                    workspace_id=context.workspace_id,
                    workspace_root=context.workspace_root,
                    session_id=context.session_id,
                    task_id="",
                    expires_monotonic=expires_monotonic,
                )
        return grant

    def issue_permission(
        self,
        context: SecurityContext,
        additional_permissions: AdditionalPermissionProfile,
        scope: PermissionGrantScope,
        *,
        expires_monotonic: float | None = None,
    ) -> PermissionGrant:
        """Issue a capability grant whose lifetime is turn- or session-scoped."""
        if not isinstance(scope, PermissionGrantScope):
            raise GrantError("权限 grant scope 无效")
        if scope is PermissionGrantScope.TURN and not context.task_id:
            raise GrantError("turn 权限缺少可信 task_id")
        if expires_monotonic is not None and (
            isinstance(expires_monotonic, bool)
            or not isinstance(expires_monotonic, (int, float))
            or not math.isfinite(expires_monotonic)
        ):
            raise GrantError("权限 grant expiry 必须是有限单调时钟值")
        additional_permissions = normalize_additional_permissions(additional_permissions)
        if additional_permissions.is_empty():
            raise GrantError("权限 grant 不能为空")
        grant = PermissionGrant(
            grant_id=uuid4().hex,
            scope=scope,
            additional_permissions=additional_permissions,
            os_user=context.os_user,
            owner_account_id=context.owner_account_id,
            workspace_id=context.workspace_id,
            workspace_root=context.workspace_root,
            session_id=context.session_id,
            task_id=context.task_id if scope is PermissionGrantScope.TURN else "",
            expires_monotonic=expires_monotonic,
        )
        with self._lock:
            self._permission_grants[grant.grant_id] = grant
        return grant

    def additional_permissions(self, context: SecurityContext) -> AdditionalPermissionProfile:
        """Return active capability grants for this exact turn/session."""
        with self._lock:
            now = self._clock()
            entries: list[FilesystemEntry] = []
            network = []
            allow_local_binding = False
            for grant_id, grant in list(self._permission_grants.items()):
                if grant.expires_monotonic is not None and now >= grant.expires_monotonic:
                    self._permission_grants.pop(grant_id, None)
                    continue
                if not _permission_context_matches(grant, context):
                    continue
                for entry in grant.additional_permissions.filesystem:
                    if entry not in entries:
                        entries.append(entry)
                for entry in grant.additional_permissions.network:
                    if entry not in network:
                        network.append(entry)
                allow_local_binding = allow_local_binding or grant.additional_permissions.allow_local_binding
        return AdditionalPermissionProfile(
            filesystem=tuple(entries),
            network=tuple(network),
            allow_local_binding=allow_local_binding,
        )

    def revoke_task(self, context: SecurityContext) -> int:
        """Drop turn-scoped capability grants when a turn finishes."""
        if not context.task_id:
            return 0
        return self.revoke_task_identity(
            context.owner_account_id,
            context.session_id,
            context.task_id,
        )

    def revoke_task_identity(
        self,
        owner_account_id: str,
        session_id: str,
        task_id: str,
    ) -> int:
        """Revoke a turn grant from dispatcher cleanup without trusting renderer data."""
        owner = str(owner_account_id).strip()
        session = str(session_id).strip()
        task = str(task_id).strip()
        if not owner or not session or not task:
            return 0
        with self._lock:
            execution_ids = [
                grant_id
                for grant_id, grant in self._grants.items()
                if grant.scope is RuleScope.ONCE
                and grant.owner_account_id == owner
                and grant.session_id == session
                and grant.task_id == task
            ]
            for grant_id in execution_ids:
                self._remove_execution_grant_locked(grant_id)
            permission_ids = [
                grant_id
                for grant_id, grant in self._permission_grants.items()
                if grant.scope is PermissionGrantScope.TURN
                and grant.owner_account_id == owner
                and grant.session_id == session
                and grant.task_id == task
            ]
            for grant_id in permission_ids:
                self._permission_grants.pop(grant_id, None)
            return len(execution_ids) + len(permission_ids)

    def authorize(
        self,
        grant_id: str,
        context: SecurityContext,
        action: NormalizedAction,
        *,
        action_scope: ActionScope | None = None,
    ) -> ExecutionGrant:
        """Validate an exact action and consume a once grant only after success."""
        with self._lock:
            grant = self._grants.get(grant_id)
            if grant is None:
                raise GrantError("grant 不存在或已消费")
            if grant.expires_monotonic is not None and self._clock() >= grant.expires_monotonic:
                self._remove_execution_grant_locked(grant_id)
                raise GrantError("grant 已过期")
            if not _context_matches(grant, context):
                raise GrantError("grant 上下文不匹配")
            if action.digest != grant.action_digest:
                raise GrantError("grant 动作不匹配")
            if grant.context_digest != security_context_digest(context):
                raise GrantError("grant 上下文 digest 不匹配")
            if grant.action_scope_digest and (
                action_scope is None
                or not isinstance(action_scope, ActionScope)
                or action_scope.digest != grant.action_scope_digest
            ):
                raise GrantError("grant action scope 不匹配")
            if grant.scope is RuleScope.ONCE:
                self._remove_execution_grant_locked(grant_id)
            return grant

    def revoke_session(self, context: SecurityContext) -> int:
        return self.revoke_owned_session(context.owner_account_id, context.session_id)

    def revoke_context_session(self, context: SecurityContext) -> int:
        """Revoke authority for one exact OS-user/owner/workspace/session tuple."""
        with self._lock:
            execution_ids = [
                grant_id
                for grant_id, grant in self._grants.items()
                if _same_session(grant, context)
            ]
            for grant_id in execution_ids:
                self._remove_execution_grant_locked(grant_id)
            permission_ids = [
                grant_id
                for grant_id, grant in self._permission_grants.items()
                if _same_permission_session(grant, context)
            ]
            for grant_id in permission_ids:
                self._permission_grants.pop(grant_id, None)
            return len(execution_ids) + len(permission_ids)

    def revoke_owned_session(self, owner_account_id: str, session_id: str) -> int:
        """Revoke transient grants when an authenticated conversation truly ends."""
        owner = str(owner_account_id).strip()
        session = str(session_id).strip()
        if not owner or not session:
            return 0
        with self._lock:
            doomed = [
                grant_id
                for grant_id, grant in self._grants.items()
                if grant.owner_account_id == owner and grant.session_id == session
            ]
            for grant_id in doomed:
                self._remove_execution_grant_locked(grant_id)
            permission_count = 0
            for grant_id, grant in list(self._permission_grants.items()):
                if grant.owner_account_id == owner and grant.session_id == session:
                    self._permission_grants.pop(grant_id, None)
                    permission_count += 1
            return len(doomed) + permission_count

    def revoke_owner(self, owner_account_id: str) -> int:
        """Revoke every transient grant belonging to one logged-out product owner."""
        owner = str(owner_account_id).strip()
        if not owner:
            return 0
        with self._lock:
            doomed = [
                grant_id
                for grant_id, grant in self._grants.items()
                if grant.owner_account_id == owner
            ]
            for grant_id in doomed:
                self._remove_execution_grant_locked(grant_id)
            permission_count = 0
            for grant_id, grant in list(self._permission_grants.items()):
                if grant.owner_account_id == owner:
                    self._permission_grants.pop(grant_id, None)
                    permission_count += 1
            return len(doomed) + permission_count

    def authorize_action(
        self,
        context: SecurityContext,
        action: NormalizedAction,
        *,
        action_scope: ActionScope | None = None,
        additional_permissions: AdditionalPermissionProfile = EMPTY_ADDITIONAL_PERMISSIONS,
        tool_name: str = "",
    ) -> ExecutionGrant | None:
        """Authorize by exact action when a tool retry does not carry a grant ID."""
        with self._lock:
            now = self._clock()
            for grant_id, grant in list(self._grants.items()):
                if grant.expires_monotonic is not None and now >= grant.expires_monotonic:
                    self._remove_execution_grant_locked(grant_id)
                    continue
                if not _context_matches(grant, context):
                    continue
                if tool_name and grant.tool_name and grant.tool_name != tool_name:
                    continue
                exact_action_required = (
                    grant.scope is RuleScope.ONCE
                    or grant.additional_permissions.empty
                    or additional_permissions.empty
                    or grant.additional_permissions.sandbox_permissions
                    is SandboxPermissions.REQUIRE_ESCALATED
                )
                if exact_action_required and grant.action_digest != action.digest:
                    continue
                if exact_action_required and grant.additional_permissions != additional_permissions:
                    continue
                if not exact_action_required and grant.action_kind is not action.kind:
                    continue
                if not additional_permissions_cover(
                    grant.additional_permissions,
                    additional_permissions,
                ):
                    continue
                if grant.context_digest != security_context_digest(context):
                    continue
                if grant.action_scope_digest and (
                    action_scope is None or action_scope.digest != grant.action_scope_digest
                ):
                    continue
                if grant.scope is RuleScope.ONCE:
                    self._remove_execution_grant_locked(grant_id)
                return grant
        return None

    def session_permissions(self, context: SecurityContext) -> AdditionalPermissionProfile:
        """Return sandbox permissions approved for the rest of this conversation.

        Unlike an execution approval, a filesystem/network permission describes
        a reusable capability.  Keep it independent from the original action so
        later commands can consume the approved scope without asking the model to
        repeat the same ``additional_permissions`` payload.  Unsandboxed grants
        remain exact-command only and are never included here.
        """
        with self._lock:
            now = self._clock()
            profiles: list[AdditionalPermissionProfile] = []
            for grant_id, grant in list(self._grants.items()):
                if grant.expires_monotonic is not None and now > grant.expires_monotonic:
                    self._grants.pop(grant_id, None)
                    continue
                if grant.scope is not RuleScope.SESSION or not _same_session(grant, context):
                    continue
                if (
                    grant.additional_permissions.empty
                    or grant.additional_permissions.sandbox_permissions
                    is SandboxPermissions.REQUIRE_ESCALATED
                ):
                    continue
                profiles.append(grant.additional_permissions)
        return merge_additional_permissions(*profiles)

    def revoke(self, grant_id: str) -> bool:
        """Revoke one grant during fail-closed coordination rollback."""
        with self._lock:
            return self._remove_execution_grant_locked(str(grant_id))

    def revoke_permission(self, grant_id: str) -> bool:
        """Rollback one capability grant when durable authorization audit fails."""
        with self._lock:
            return self._permission_grants.pop(str(grant_id), None) is not None

    def _remove_execution_grant_locked(self, grant_id: str) -> bool:
        grant = self._grants.pop(grant_id, None)
        if grant is None:
            return False
        if grant.permission_grant_id is not None:
            self._permission_grants.pop(grant.permission_grant_id, None)
        return True


def _context_matches(grant: ExecutionGrant, context: SecurityContext) -> bool:
    if not _same_session(grant, context):
        return False
    return grant.scope is RuleScope.SESSION or grant.task_id == context.task_id


def _same_session(grant: ExecutionGrant, context: SecurityContext) -> bool:
    return (
        grant.os_user == context.os_user
        and grant.owner_account_id == context.owner_account_id
        and grant.workspace_id == context.workspace_id
        and grant.workspace_root == context.workspace_root
        and grant.session_id == context.session_id
    )


def _permission_context_matches(grant: PermissionGrant, context: SecurityContext) -> bool:
    return (
        _same_permission_session(grant, context)
        and (
            grant.scope is PermissionGrantScope.SESSION
            or grant.task_id == context.task_id
        )
    )


def _same_permission_session(grant: PermissionGrant, context: SecurityContext) -> bool:
    return (
        grant.os_user == context.os_user
        and grant.owner_account_id == context.owner_account_id
        and grant.workspace_id == context.workspace_id
        and grant.workspace_root == context.workspace_root
        and grant.session_id == context.session_id
    )


def _validate_bound_context(context: SecurityContext, *, require_task: bool) -> None:
    for field in ("owner_account_id", "workspace_id", "session_id"):
        if not isinstance(getattr(context, field, None), str) or not getattr(context, field).strip():
            raise GrantError(f"grant context {field} is required")
    if require_task and (
        not isinstance(getattr(context, "task_id", None), str) or not context.task_id.strip()
    ):
        raise GrantError("grant context task_id is required")


Grant = ExecutionGrant
