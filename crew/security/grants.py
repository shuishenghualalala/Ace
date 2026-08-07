"""Short-lived exact execution grants issued only by host approval state."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable
from uuid import uuid4

from crew.security.actions import NormalizedAction
from crew.security.context import SecurityContext
from crew.security.rules import RuleScope


class GrantError(RuntimeError):
    """Raised when an execution grant cannot authorize the requested action."""


@dataclass(frozen=True)
class ExecutionGrant:
    grant_id: str
    scope: RuleScope
    action_digest: str
    os_user: str
    owner_account_id: str
    workspace_id: str
    session_id: str
    task_id: str
    expires_monotonic: float | None


class GrantRegistry:
    """Keep once/session grants in host memory so restart revokes them."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._grants: dict[str, ExecutionGrant] = {}

    def issue(
        self,
        context: SecurityContext,
        action: NormalizedAction,
        scope: RuleScope,
        *,
        expires_monotonic: float | None,
    ) -> ExecutionGrant:
        if scope not in {RuleScope.ONCE, RuleScope.SESSION}:
            raise ValueError("execution grant 只支持 once/session")
        grant = ExecutionGrant(
            grant_id=uuid4().hex,
            scope=scope,
            action_digest=action.digest,
            os_user=context.os_user,
            owner_account_id=context.owner_account_id,
            workspace_id=context.workspace_id,
            session_id=context.session_id,
            task_id=context.task_id,
            expires_monotonic=expires_monotonic,
        )
        with self._lock:
            self._grants[grant.grant_id] = grant
        return grant

    def authorize(
        self,
        grant_id: str,
        context: SecurityContext,
        action: NormalizedAction,
    ) -> ExecutionGrant:
        """Validate an exact action and consume a once grant only after success."""
        with self._lock:
            grant = self._grants.get(grant_id)
            if grant is None:
                raise GrantError("grant 不存在或已消费")
            if grant.expires_monotonic is not None and self._clock() > grant.expires_monotonic:
                self._grants.pop(grant_id, None)
                raise GrantError("grant 已过期")
            if not _context_matches(grant, context):
                raise GrantError("grant 上下文不匹配")
            if action.digest != grant.action_digest:
                raise GrantError("grant 动作不匹配")
            if grant.scope is RuleScope.ONCE:
                self._grants.pop(grant_id, None)
            return grant

    def revoke_session(self, context: SecurityContext) -> int:
        return self.revoke_owned_session(context.owner_account_id, context.session_id)

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
                self._grants.pop(grant_id, None)
            return len(doomed)

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
                self._grants.pop(grant_id, None)
            return len(doomed)

    def authorize_action(
        self,
        context: SecurityContext,
        action: NormalizedAction,
    ) -> ExecutionGrant | None:
        """Authorize by exact action when a tool retry does not carry a grant ID."""
        with self._lock:
            now = self._clock()
            for grant_id, grant in list(self._grants.items()):
                if grant.expires_monotonic is not None and now > grant.expires_monotonic:
                    self._grants.pop(grant_id, None)
                    continue
                if grant.action_digest != action.digest or not _context_matches(grant, context):
                    continue
                if grant.scope is RuleScope.ONCE:
                    self._grants.pop(grant_id, None)
                return grant
        return None

    def revoke(self, grant_id: str) -> bool:
        """Revoke one grant during fail-closed coordination rollback."""
        with self._lock:
            return self._grants.pop(str(grant_id), None) is not None


def _context_matches(grant: ExecutionGrant, context: SecurityContext) -> bool:
    if not _same_session(grant, context):
        return False
    return grant.scope is RuleScope.SESSION or grant.task_id == context.task_id


def _same_session(grant: ExecutionGrant, context: SecurityContext) -> bool:
    return (
        grant.os_user == context.os_user
        and grant.owner_account_id == context.owner_account_id
        and grant.workspace_id == context.workspace_id
        and grant.session_id == context.session_id
    )
