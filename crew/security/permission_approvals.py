"""Turn/session-scoped capability requests, separate from action approvals."""

from __future__ import annotations

import math
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from uuid import uuid4

from crew.security.approvals import ApprovalDecision, ApprovalError
from crew.security.context import SecurityContext
from crew.security.grants import GrantRegistry, PermissionGrant
from crew.security.models import AdditionalPermissionProfile, PermissionGrantScope
from crew.security.policy import intersect_additional_permissions, normalize_additional_permissions

_MAX_REQUESTS = 1024
_MAX_PENDING_PER_SESSION = 128
_PRUNE_MIN_SIZE = 512
_PRUNE_INTERVAL_SECONDS = 30.0
_PRUNE_GRACE_SECONDS = 60.0
_MAX_REASON_LENGTH = 1000
_MAX_TOOL_NAME_LENGTH = 128


@dataclass(frozen=True)
class PermissionRequest:
    request_id: str
    nonce: str
    requested_permissions: AdditionalPermissionProfile
    reason: str
    tool_name: str
    os_user: str
    owner_account_id: str
    workspace_id: str
    workspace_root: Path | None
    session_id: str
    task_id: str
    created_monotonic: float
    expires_monotonic: float


@dataclass(frozen=True)
class PermissionOutcome:
    request: PermissionRequest
    decision: ApprovalDecision
    grant: PermissionGrant | None
    granted_permissions: AdditionalPermissionProfile
    scope: PermissionGrantScope | None


class PermissionApprovalManager:
    """Own the pending/requested/granted capability lifecycle."""

    def __init__(
        self,
        grants: GrantRegistry,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._grants = grants
        self._clock = clock
        self._lock = threading.Lock()
        self._requests: dict[str, PermissionRequest] = {}
        self._handled: set[str] = set()
        self._last_prune = clock()

    def create(
        self,
        context: SecurityContext,
        permissions: AdditionalPermissionProfile,
        *,
        reason: str = "",
        tool_name: str = "request_permissions",
        ttl_seconds: float = 300.0,
    ) -> PermissionRequest:
        now = self._clock()
        request = _new_request(
            context,
            permissions,
            reason=reason,
            tool_name=tool_name,
            ttl_seconds=ttl_seconds,
            now=now,
        )
        with self._lock:
            self._ensure_capacity(context, now)
            self._requests[request.request_id] = request
            self._maybe_prune(now)
        return request

    def create_or_get(
        self,
        context: SecurityContext,
        permissions: AdditionalPermissionProfile,
        *,
        reason: str = "",
        tool_name: str = "request_permissions",
        ttl_seconds: float = 300.0,
    ) -> tuple[PermissionRequest, bool]:
        """Atomically reuse one exact live capability request or create it."""
        now = self._clock()
        candidate = _new_request(
            context,
            permissions,
            reason=reason,
            tool_name=tool_name,
            ttl_seconds=ttl_seconds,
            now=now,
        )
        with self._lock:
            existing = next(
                (
                    request
                    for request in self._requests.values()
                    if request.request_id not in self._handled
                    and request.expires_monotonic > now
                    and request.requested_permissions == candidate.requested_permissions
                    and request.reason == candidate.reason
                    and request.tool_name == candidate.tool_name
                    and _request_context_matches(request, context)
                ),
                None,
            )
            if existing is not None:
                return existing, False
            self._ensure_capacity(context, now)
            self._requests[candidate.request_id] = candidate
            self._maybe_prune(now)
            return candidate, True

    def _ensure_capacity(self, context: SecurityContext, now: float) -> None:
        if len(self._requests) >= _MAX_REQUESTS:
            terminal = [
                request_id
                for request_id, request in self._requests.items()
                if request_id in self._handled or request.expires_monotonic <= now
            ]
            for request_id in terminal:
                self._requests.pop(request_id, None)
            self._handled.difference_update(terminal)
        pending_for_session = sum(
            request.request_id not in self._handled
            and request.expires_monotonic > now
            and _request_session_matches(request, context)
            for request in self._requests.values()
        )
        if pending_for_session >= _MAX_PENDING_PER_SESSION:
            raise ApprovalError("当前会话待审批权限请求过多，请先处理已有请求")
        if len(self._requests) >= _MAX_REQUESTS:
            raise ApprovalError("全局待审批权限请求过多，请先处理已有请求")

    def _maybe_prune(self, now: float) -> None:
        if len(self._requests) < _PRUNE_MIN_SIZE:
            return
        if now - self._last_prune < _PRUNE_INTERVAL_SECONDS:
            return
        self._last_prune = now
        cutoff = now - _PRUNE_GRACE_SECONDS
        expired = [
            request_id
            for request_id, request in self._requests.items()
            if request.expires_monotonic < cutoff
        ]
        for request_id in expired:
            self._requests.pop(request_id, None)
        self._handled.difference_update(expired)

    def get_pending(self, request_id: str, context: SecurityContext) -> PermissionRequest | None:
        now = self._clock()
        with self._lock:
            request = self._requests.get(str(request_id))
            if (
                request is None
                or request.request_id in self._handled
                or request.expires_monotonic <= now
                or not _request_context_matches(request, context)
            ):
                return None
            return request

    def list_pending(self, context: SecurityContext) -> list[PermissionRequest]:
        now = self._clock()
        with self._lock:
            # Desktop polling has no trusted agent task id. Visibility is
            # session-scoped; get_pending/decide remain task-scoped.
            return [
                request
                for request in self._requests.values()
                if request.request_id not in self._handled
                and request.expires_monotonic > now
                and _request_session_matches(request, context)
            ]

    def cancel(self, request_id: str, context: SecurityContext) -> bool:
        """Tombstone one request when its durable creation audit fails."""
        with self._lock:
            request = self._requests.get(str(request_id))
            if (
                request is None
                or request.request_id in self._handled
                or not _request_context_matches(request, context)
            ):
                return False
            self._handled.add(request.request_id)
            return True

    def cancel_pending(self, request_id: str) -> PermissionRequest | None:
        """Host-internal timeout cancellation keyed by an unguessable request ID."""
        with self._lock:
            request = self._requests.get(str(request_id))
            if request is None or request.request_id in self._handled:
                return None
            self._handled.add(request.request_id)
            return request

    def decide(
        self,
        request_id: str,
        nonce: str,
        decision: ApprovalDecision,
        context: SecurityContext,
        *,
        granted_permissions: AdditionalPermissionProfile | None = None,
    ) -> PermissionOutcome:
        if not isinstance(decision, ApprovalDecision):
            raise ApprovalError(f"未知批准决定: {decision!r}")
        with self._lock:
            request = self._requests.get(str(request_id))
            if request is None:
                raise ApprovalError("权限请求不存在", terminal=True)
            if request.request_id in self._handled:
                raise ApprovalError("权限请求已处理", terminal=True)
            if not secrets.compare_digest(request.nonce, str(nonce)):
                raise ApprovalError("权限请求 nonce 不匹配")
            if self._clock() >= request.expires_monotonic:
                self._handled.add(request.request_id)
                raise ApprovalError("权限请求已过期", terminal=True)
            if not _request_context_matches(request, context):
                raise ApprovalError("权限请求上下文不匹配")
            if decision is ApprovalDecision.ALWAYS:
                raise ApprovalError("额外权限不支持始终允许")
            if decision is ApprovalDecision.REJECT:
                self._handled.add(request.request_id)
                return PermissionOutcome(
                    request=request,
                    decision=decision,
                    grant=None,
                    granted_permissions=AdditionalPermissionProfile(),
                    scope=None,
                )

            requested = request.requested_permissions
            granted = requested if granted_permissions is None else granted_permissions
            effective = intersect_additional_permissions(requested, granted)
            self._handled.add(request.request_id)
            if effective.is_empty():
                return PermissionOutcome(
                    request=request,
                    decision=ApprovalDecision.REJECT,
                    grant=None,
                    granted_permissions=effective,
                    scope=None,
                )
            scope = (
                PermissionGrantScope.SESSION
                if decision is ApprovalDecision.SESSION
                else PermissionGrantScope.TURN
            )
            # Keep handled-state publication and grant publication atomic with
            # respect to session/owner lifecycle revocation.
            grant = self._grants.issue_permission(
                context,
                effective,
                scope,
                expires_monotonic=(
                    None
                    if scope is PermissionGrantScope.SESSION
                    else request.expires_monotonic
                ),
            )
            return PermissionOutcome(
                request=request,
                decision=decision,
                grant=grant,
                granted_permissions=effective,
                scope=scope,
            )

    def revoke_pending_session(self, context: SecurityContext) -> int:
        with self._lock:
            doomed = [
                request.request_id
                for request in self._requests.values()
                if request.request_id not in self._handled
                and _request_session_matches(request, context)
            ]
            self._handled.update(doomed)
            return len(doomed)

    def revoke_pending_task(self, owner_account_id: str, session_id: str, task_id: str) -> int:
        owner = str(owner_account_id).strip()
        session = str(session_id).strip()
        task = str(task_id).strip()
        if not owner or not session or not task:
            return 0
        with self._lock:
            doomed = [
                request.request_id
                for request in self._requests.values()
                if request.request_id not in self._handled
                and request.owner_account_id == owner
                and request.session_id == session
                and request.task_id == task
            ]
            self._handled.update(doomed)
            return len(doomed)

    def end_owned_session(self, owner_account_id: str, session_id: str) -> int:
        owner = str(owner_account_id).strip()
        session = str(session_id).strip()
        if not owner or not session:
            return 0
        with self._lock:
            doomed = [
                request.request_id
                for request in self._requests.values()
                if request.request_id not in self._handled
                and request.owner_account_id == owner
                and request.session_id == session
            ]
            self._handled.update(doomed)
            return len(doomed) + self._grants.revoke_owned_session(owner, session)

    def revoke_owner(self, owner_account_id: str) -> int:
        owner = str(owner_account_id).strip()
        if not owner:
            return 0
        with self._lock:
            doomed = [
                request.request_id
                for request in self._requests.values()
                if request.request_id not in self._handled
                and request.owner_account_id == owner
            ]
            self._handled.update(doomed)
            return len(doomed) + self._grants.revoke_owner(owner)


def _new_request(
    context: SecurityContext,
    permissions: AdditionalPermissionProfile,
    *,
    reason: str,
    tool_name: str,
    ttl_seconds: float,
    now: float,
) -> PermissionRequest:
    if not context.task_id:
        raise ApprovalError("权限请求缺少可信 task_id")
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, (int, float))
        or not math.isfinite(ttl_seconds)
        or ttl_seconds <= 0
    ):
        raise ValueError("permission request TTL 必须大于 0")
    normalized = normalize_additional_permissions(permissions)
    if normalized.is_empty():
        raise ValueError("权限请求不能为空")
    normalized_reason = _bounded_text(reason, "reason", _MAX_REASON_LENGTH, allow_empty=True)
    normalized_tool = _bounded_text(
        tool_name or "request_permissions",
        "tool_name",
        _MAX_TOOL_NAME_LENGTH,
        allow_empty=False,
    )
    return PermissionRequest(
        request_id=uuid4().hex,
        nonce=secrets.token_urlsafe(24),
        requested_permissions=normalized,
        reason=normalized_reason,
        tool_name=normalized_tool,
        os_user=context.os_user,
        owner_account_id=context.owner_account_id,
        workspace_id=context.workspace_id,
        workspace_root=context.workspace_root,
        session_id=context.session_id,
        task_id=context.task_id,
        created_monotonic=now,
        expires_monotonic=now + ttl_seconds,
    )


def _bounded_text(value: object, field: str, maximum: int, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是字符串")
    normalized = value.strip() if field == "tool_name" else value
    if "\x00" in normalized or len(normalized) > maximum or (not allow_empty and not normalized):
        raise ValueError(f"{field} 无效或超过 {maximum} 字符")
    return normalized


def _request_context_matches(request: PermissionRequest, context: SecurityContext) -> bool:
    return (
        request.os_user == context.os_user
        and request.owner_account_id == context.owner_account_id
        and request.workspace_id == context.workspace_id
        and request.workspace_root == context.workspace_root
        and request.session_id == context.session_id
        and request.task_id == context.task_id
    )


def _request_session_matches(request: PermissionRequest, context: SecurityContext) -> bool:
    return (
        request.os_user == context.os_user
        and request.owner_account_id == context.owner_account_id
        and request.workspace_id == context.workspace_id
        and request.workspace_root == context.workspace_root
        and request.session_id == context.session_id
    )
