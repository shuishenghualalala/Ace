"""Host-owned approval request lifecycle; model output is never an authority."""

from __future__ import annotations

import json
import math
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable, Sequence
from uuid import uuid4

from crew.security.actions import ActionKind, NormalizedAction
from crew.security.context import SecurityContext
from crew.security.grants import ExecutionGrant, GrantRegistry
from crew.security.models import AdditionalPermissionProfile, PermissionProfile
from crew.security.policy import normalize_additional_permissions
from crew.security.rules import ActionRule, RuleScope

# Bounded in-memory state for a long-running gateway. Pending/handled requests are
# only dropped once they are well past expiry, so a late decide() still resolves the
# waiter to a terminal state (not_exist/expired are both terminal; see service.py).
_PRUNE_MIN_SIZE = 512
_PRUNE_INTERVAL_SECONDS = 30.0
_PRUNE_GRACE_SECONDS = 60.0
_MAX_REQUESTS = 1024
_MAX_PENDING_PER_SESSION = 128
_MAX_TOOL_NAME_LENGTH = 128
_MAX_RISK_CLASS_LENGTH = 128
_MAX_PREVIEW_LENGTH = 4000
_MAX_ACTION_PAYLOAD_BYTES = 65_536


class ApprovalError(RuntimeError):
    """Approval failure classified by whether the pending request reached a terminal state."""

    def __init__(self, message: str, *, terminal: bool = False) -> None:
        super().__init__(message)
        self.terminal = terminal


class ApprovalDecision(StrEnum):
    ONCE = "once"
    SESSION = "session"
    ALWAYS = "always"
    REJECT = "reject"


@dataclass(frozen=True)
class ApprovalRequest:
    request_id: str
    nonce: str
    action: NormalizedAction
    action_digest: str
    tool_name: str
    os_user: str
    owner_account_id: str
    workspace_id: str
    workspace_root: Path | None
    session_id: str
    task_id: str
    base_profile_hash: str
    risk_class: str
    preview: str
    created_monotonic: float
    expires_monotonic: float
    additional_permissions: AdditionalPermissionProfile = AdditionalPermissionProfile()
    effective_profile: PermissionProfile | None = None


@dataclass(frozen=True)
class ApprovalOutcome:
    request: ApprovalRequest
    decision: ApprovalDecision
    grant: ExecutionGrant | None = None
    persistent_rule: ActionRule | None = None


class ApprovalManager:
    """Create and decide exact requests, keeping all authority in host memory."""

    def __init__(
        self,
        grants: GrantRegistry,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._grants = grants
        self._clock = clock
        self._lock = threading.Lock()
        self._requests: dict[str, ApprovalRequest] = {}
        self._handled: set[str] = set()
        self._last_prune = clock()

    def create(
        self,
        context: SecurityContext,
        action: NormalizedAction,
        tool_name: str,
        *,
        base_profile_hash: str = "",
        risk_class: str = "unknown",
        preview: str = "",
        ttl_seconds: float = 300.0,
        additional_permissions: AdditionalPermissionProfile = AdditionalPermissionProfile(),
        effective_profile: PermissionProfile | None = None,
    ) -> ApprovalRequest:
        now = self._clock()
        request = _new_request(
            context,
            action,
            tool_name,
            base_profile_hash=base_profile_hash,
            risk_class=risk_class,
            preview=preview,
            ttl_seconds=ttl_seconds,
            additional_permissions=additional_permissions,
            effective_profile=effective_profile,
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
        action: NormalizedAction,
        tool_name: str,
        *,
        base_profile_hash: str = "",
        risk_class: str = "unknown",
        preview: str = "",
        ttl_seconds: float = 300.0,
        additional_permissions: AdditionalPermissionProfile = AdditionalPermissionProfile(),
        effective_profile: PermissionProfile | None = None,
    ) -> tuple[ApprovalRequest, bool]:
        """Atomically reuse one live session/tool/action request or create it."""
        now = self._clock()
        candidate = _new_request(
            context,
            action,
            tool_name,
            base_profile_hash=base_profile_hash,
            risk_class=risk_class,
            preview=preview,
            ttl_seconds=ttl_seconds,
            additional_permissions=additional_permissions,
            effective_profile=effective_profile,
            now=now,
        )
        with self._lock:
            existing = next(
                (
                    request
                    for request_id, request in self._requests.items()
                    if request_id not in self._handled
                    and request.expires_monotonic > now
                    and request.tool_name == candidate.tool_name
                    and request.action_digest == candidate.action_digest
                    and request.additional_permissions == candidate.additional_permissions
                    and request.effective_profile == candidate.effective_profile
                    and request.base_profile_hash == candidate.base_profile_hash
                    and request.risk_class == candidate.risk_class
                    and request.preview == candidate.preview
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
        """Keep request/tombstone state bounded; reject instead of evicting live authority."""
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
            request_id not in self._handled
            and request.expires_monotonic > now
            and _request_session_matches(request, context)
            for request_id, request in self._requests.items()
        )
        if pending_for_session >= _MAX_PENDING_PER_SESSION:
            raise ApprovalError("当前会话待审批请求过多，请先处理已有请求")
        if len(self._requests) >= _MAX_REQUESTS:
            raise ApprovalError("全局待审批请求过多，请先处理已有请求")

    def decide(
        self,
        request_id: str,
        nonce: str,
        decision: ApprovalDecision,
        context: SecurityContext,
        *,
        always_argv_prefix: Sequence[str] | None = None,
    ) -> ApprovalOutcome:
        if not isinstance(decision, ApprovalDecision):
            raise ApprovalError(f"未知批准决定: {decision!r}")
        with self._lock:
            request = self._requests.get(request_id)
            if request is None:
                raise ApprovalError("批准请求不存在", terminal=True)
            if request_id in self._handled:
                raise ApprovalError("批准请求已处理", terminal=True)
            if not secrets.compare_digest(request.nonce, str(nonce)):
                raise ApprovalError("批准请求 nonce 不匹配")
            if self._clock() >= request.expires_monotonic:
                self._handled.add(request_id)
                raise ApprovalError("批准请求已过期", terminal=True)
            if not _request_context_matches(request, context):
                raise ApprovalError("批准请求上下文不匹配")
            if request.action.digest != request.action_digest:
                raise ApprovalError("批准请求动作完整性校验失败")
            if decision is ApprovalDecision.ALWAYS and (
                request.additional_permissions.filesystem
                or request.additional_permissions.network
                or request.additional_permissions.allow_local_binding
            ):
                raise ApprovalError("带额外权限的请求只能批准一次或本次对话")
            persistent_rule = (
                _always_rule(request.action, always_argv_prefix)
                if decision is ApprovalDecision.ALWAYS
                else None
            )
            self._handled.add(request_id)
            # Grant publication is part of the same lifecycle critical section as
            # marking the request handled. Session/owner cancellation takes this
            # lock before revoking grants, so it cannot slip into the handled→issue
            # window and leave authority alive after cancellation returned.
            if decision is ApprovalDecision.REJECT:
                return ApprovalOutcome(request=request, decision=decision)
            if decision is ApprovalDecision.SESSION:
                grant_scope = RuleScope.SESSION
                expires = None
            else:
                grant_scope = RuleScope.ONCE
                expires = request.expires_monotonic
            grant = self._grants.issue(
                context,
                request.action,
                grant_scope,
                expires_monotonic=expires,
                additional_permissions=request.additional_permissions,
            )
            return ApprovalOutcome(
                request=request,
                decision=decision,
                grant=grant,
                persistent_rule=persistent_rule,
            )

    def _maybe_prune(self, now: float) -> None:
        """Bound _requests/_handled growth in a long-running gateway.

        Throttled so small installations never scan and busy ones scan at most every
        _PRUNE_INTERVAL_SECONDS. Entries are only dropped once they are past
        expiry + grace, which keeps any in-flight late decide() on a terminal path.
        Must be called under self._lock.
        """
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
        if not expired:
            return
        for request_id in expired:
            del self._requests[request_id]
        # _handled only matters for requests still in _requests (decide() checks
        # _requests first); drop tombstones that no longer reference a live request.
        self._handled.difference_update(expired)

    def get_pending(self, request_id: str, context: SecurityContext) -> ApprovalRequest | None:
        """Return one live context-bound request for host-side decision constraints."""
        now = self._clock()
        with self._lock:
            request = self._requests.get(str(request_id))
            if (
                request is None
                or request.request_id in self._handled
                or request.expires_monotonic <= now
                or not _request_session_matches(request, context)
            ):
                return None
            return request

    def list_pending(self, context: SecurityContext) -> list[ApprovalRequest]:
        now = self._clock()
        with self._lock:
            return [
                request
                for request_id, request in self._requests.items()
                if request_id not in self._handled
                and request.expires_monotonic > now
                and _request_session_matches(request, context)
            ]

    def cancel(self, request_id: str, context: SecurityContext) -> bool:
        """Atomically tombstone one still-pending exact request."""
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

    def cancel_pending(self, request_id: str) -> ApprovalRequest | None:
        """Host-internal timeout cancellation keyed by an unguessable request ID."""
        with self._lock:
            request = self._requests.get(str(request_id))
            if request is None or request.request_id in self._handled:
                return None
            self._handled.add(request.request_id)
            return request

    def revoke_pending_session(self, context: SecurityContext) -> int:
        """Invalidate only pending requests when a conversation mode changes.

        A SESSION grant represents an explicit "allow for this conversation"
        decision.  Changing the mode controls future approval/sandbox policy but
        does not end that conversation authority; only :meth:`end_session` does.
        """
        with self._lock:
            pending = [
                request_id
                for request_id, request in self._requests.items()
                if request_id not in self._handled and _request_session_matches(request, context)
            ]
            self._handled.update(pending)
        return len(pending)

    def end_session(self, context: SecurityContext) -> int:
        """Revoke pending requests and transient grants at true session end."""
        return self.end_owned_session(context.owner_account_id, context.session_id)

    def revoke_pending_task(
        self,
        owner_account_id: str,
        session_id: str,
        task_id: str,
    ) -> int:
        """Invalidate pending action requests when one turn ends."""
        owner = str(owner_account_id).strip()
        session = str(session_id).strip()
        task = str(task_id).strip()
        if not owner or not session or not task:
            return 0
        with self._lock:
            pending = [
                request_id
                for request_id, request in self._requests.items()
                if request_id not in self._handled
                and request.owner_account_id == owner
                and request.session_id == session
                and request.task_id == task
            ]
            self._handled.update(pending)
        return len(pending)

    def end_owned_session(self, owner_account_id: str, session_id: str) -> int:
        """End one authenticated conversation without trusting renderer workspace data."""
        owner = str(owner_account_id).strip()
        session = str(session_id).strip()
        if not owner or not session:
            return 0
        with self._lock:
            pending = [
                request_id
                for request_id, request in self._requests.items()
                if request_id not in self._handled
                and request.owner_account_id == owner
                and request.session_id == session
            ]
            self._handled.update(pending)
        return len(pending) + self._grants.revoke_owned_session(owner, session)

    def revoke_session(self, context: SecurityContext) -> int:
        """Backward-compatible alias for true session-end cleanup."""
        return self.end_session(context)

    def revoke_owner(self, owner_account_id: str) -> int:
        """Invalidate pending requests and grants for one product-account logout."""
        owner = str(owner_account_id).strip()
        if not owner:
            return 0
        with self._lock:
            pending = [
                request_id
                for request_id, request in self._requests.items()
                if request_id not in self._handled and request.owner_account_id == owner
            ]
            self._handled.update(pending)
        return len(pending) + self._grants.revoke_owner(owner)


def _always_rule(action: NormalizedAction, prefix: Sequence[str] | None) -> ActionRule:
    if action.kind is not ActionKind.EXEC or action.raw_command:
        # Shell wrappers (pwsh -Command / bash -lc) make argv-prefix authority unsafe:
        # the wrapper prefix is identical for every future script. Bind the complete
        # user-visible command + final argv digest instead; direct argv may still use
        # the narrower structured prefix path below.
        return ActionRule.exact(action, scope=RuleScope.ALWAYS)
    chosen = tuple(prefix) if prefix is not None else action.argv
    rule = ActionRule.exec_prefix(chosen, cwd=action.cwd)
    if not rule.matches(action):
        raise ApprovalError("always argv prefix 不是已批准命令的 token prefix")
    return rule


def _new_request(
    context: SecurityContext,
    action: NormalizedAction,
    tool_name: str,
    *,
    base_profile_hash: str,
    risk_class: str,
    preview: str,
    ttl_seconds: float,
    additional_permissions: AdditionalPermissionProfile,
    effective_profile: PermissionProfile | None,
    now: float,
) -> ApprovalRequest:
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, (int, float))
        or not math.isfinite(ttl_seconds)
        or ttl_seconds <= 0
    ):
        raise ValueError("approval TTL 必须大于 0")
    if not isinstance(action, NormalizedAction):
        raise ValueError("approval action 必须是规范化 action")
    for field in (
        "os_user",
        "owner_account_id",
        "workspace_id",
        "session_id",
        "task_id",
    ):
        value = getattr(context, field)
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ValueError(f"approval context {field} 无效")
    normalized_tool = _bounded_text(
        tool_name,
        "tool_name",
        _MAX_TOOL_NAME_LENGTH,
        allow_empty=False,
        strip=True,
    )
    normalized_risk = _bounded_text(
        risk_class,
        "risk_class",
        _MAX_RISK_CLASS_LENGTH,
        allow_empty=False,
        strip=True,
    )
    normalized_preview = _bounded_text(
        preview,
        "preview",
        _MAX_PREVIEW_LENGTH,
        allow_empty=True,
        strip=False,
    )
    if not isinstance(base_profile_hash, str):
        raise ValueError("base_profile_hash 必须是字符串")
    normalized_profile_hash = base_profile_hash.strip().lower()
    if normalized_profile_hash and (
        len(normalized_profile_hash) != 64
        or any(char not in "0123456789abcdef" for char in normalized_profile_hash)
    ):
        raise ValueError("base_profile_hash 必须是 SHA-256 hex")
    action_payload = json.dumps(
        asdict(action),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(action_payload) > _MAX_ACTION_PAYLOAD_BYTES:
        raise ValueError("approval action 结构化内容过大")
    normalized_permissions = normalize_additional_permissions(additional_permissions)
    if effective_profile is not None and not isinstance(effective_profile, PermissionProfile):
        raise ValueError("effective_profile 必须是 PermissionProfile")
    return ApprovalRequest(
        request_id=uuid4().hex,
        nonce=secrets.token_urlsafe(24),
        action=action,
        action_digest=action.digest,
        tool_name=normalized_tool,
        os_user=context.os_user,
        owner_account_id=context.owner_account_id,
        workspace_id=context.workspace_id,
        workspace_root=context.workspace_root,
        session_id=context.session_id,
        task_id=context.task_id,
        base_profile_hash=normalized_profile_hash,
        risk_class=normalized_risk,
        preview=normalized_preview,
        created_monotonic=now,
        expires_monotonic=now + ttl_seconds,
        additional_permissions=normalized_permissions,
        effective_profile=effective_profile,
    )


def _bounded_text(
    value: object,
    field: str,
    maximum: int,
    *,
    allow_empty: bool,
    strip: bool,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是字符串")
    normalized = value.strip() if strip else value
    if (
        "\x00" in normalized
        or len(normalized) > maximum
        or (not allow_empty and not normalized)
    ):
        raise ValueError(f"{field} 无效或超过 {maximum} 字符")
    return normalized


def _request_context_matches(request: ApprovalRequest, context: SecurityContext) -> bool:
    return _request_session_matches(request, context) and request.task_id == context.task_id


def _request_session_matches(request: ApprovalRequest, context: SecurityContext) -> bool:
    return (
        request.os_user == context.os_user
        and request.owner_account_id == context.owner_account_id
        and request.workspace_id == context.workspace_id
        and request.workspace_root == context.workspace_root
        and request.session_id == context.session_id
    )
