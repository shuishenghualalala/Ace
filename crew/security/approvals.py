"""Host-owned approval request lifecycle; model output is never an authority."""

from __future__ import annotations

import json
import math
import secrets
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from crew.security.actions import ActionKind, ActionScope, NormalizedAction, security_context_digest
from crew.security.context import SecurityContext
from crew.security.grants import ExecutionGrant, GrantRegistry
from crew.security.models import (
    EMPTY_ADDITIONAL_PERMISSIONS,
    AdditionalPermissionProfile,
    PermissionProfile,
    SandboxPermissions,
)
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

    def __init__(self, message: str, *, terminal: bool = False, status: ApprovalStatus | None = None) -> None:
        super().__init__(message)
        self.terminal = terminal
        self.status = status or ApprovalStatus.REPLAY


class ApprovalDecision(StrEnum):
    ONCE = "once"
    SESSION = "session"
    ALWAYS = "always"
    REJECT = "reject"


class ApprovalStatus(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    SCOPE_CONTEXT_MISMATCH = "scope_context_mismatch"
    REPLAY = "replay"


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
    action_scope_digest: str = ""
    context_digest: str = ""
    proposed_argv_prefix: tuple[str, ...] = ()

    @property
    def scope_digest(self) -> str:
        return self.action_scope_digest


@dataclass(frozen=True)
class ApprovalOutcome:
    request: ApprovalRequest
    decision: ApprovalDecision
    grant: ExecutionGrant | None = None
    persistent_rule: ActionRule | None = None

    @property
    def status(self) -> ApprovalStatus:
        return ApprovalStatus.DENIED if self.decision is ApprovalDecision.REJECT else ApprovalStatus.APPROVED


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
        action_scope: ActionScope | None = None,
        proposed_argv_prefix: Sequence[str] | None = None,
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
            action_scope=action_scope,
            now=now,
            proposed_argv_prefix=proposed_argv_prefix,
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
        action_scope: ActionScope | None = None,
        proposed_argv_prefix: Sequence[str] | None = None,
    ) -> tuple[ApprovalRequest, bool]:
        """Atomically reuse one live session/tool/action request or create it."""
        now = self._clock()
        if ttl_seconds <= 0:
            raise ValueError("approval TTL 必须大于 0")
        _validate_prefix_tool(
            str(tool_name).strip(),
            proposed_argv_prefix,
            additional_permissions,
        )
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
            action_scope=action_scope,
            now=now,
            proposed_argv_prefix=proposed_argv_prefix,
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
                    and request.action_scope_digest == candidate.action_scope_digest
                    and request.proposed_argv_prefix
                    == candidate.proposed_argv_prefix
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
        action_scope: ActionScope | None = None,
    ) -> ApprovalOutcome:
        if not isinstance(decision, ApprovalDecision):
            raise ApprovalError(f"未知批准决定: {decision!r}")
        with self._lock:
            request = self._requests.get(request_id)
            if request is None:
                raise ApprovalError("批准请求不存在", terminal=True, status=ApprovalStatus.REPLAY)
            if request_id in self._handled:
                raise ApprovalError("批准请求已处理", terminal=True, status=ApprovalStatus.REPLAY)
            if not secrets.compare_digest(request.nonce, str(nonce)):
                raise ApprovalError("批准请求 nonce 不匹配", status=ApprovalStatus.REPLAY)
            if self._clock() >= request.expires_monotonic:
                self._handled.add(request_id)
                raise ApprovalError("批准请求已过期", terminal=True, status=ApprovalStatus.EXPIRED)
            if not _request_context_matches(request, context):
                raise ApprovalError("批准请求上下文不匹配", status=ApprovalStatus.SCOPE_CONTEXT_MISMATCH)
            if request.context_digest != security_context_digest(context):
                raise ApprovalError("批准请求上下文 digest 不匹配", status=ApprovalStatus.SCOPE_CONTEXT_MISMATCH)
            if request.action_scope_digest and (
                action_scope is None
                or not isinstance(action_scope, ActionScope)
                or action_scope.digest != request.action_scope_digest
            ):
                raise ApprovalError("批准请求 action scope 不匹配", status=ApprovalStatus.SCOPE_CONTEXT_MISMATCH)
            if request.action.digest != request.action_digest:
                raise ApprovalError("批准请求动作完整性校验失败", status=ApprovalStatus.SCOPE_CONTEXT_MISMATCH)
            if decision is ApprovalDecision.ALWAYS and (
                request.additional_permissions.filesystem
                or request.additional_permissions.network
                or request.additional_permissions.allow_local_binding
            ):
                raise ApprovalError("带额外权限的请求只能批准一次或本次对话", status=ApprovalStatus.DENIED)
                raise ApprovalError("批准请求动作完整性校验失败")
            if always_argv_prefix is not None and tuple(always_argv_prefix) != request.proposed_argv_prefix:
                raise ApprovalError("始终允许前缀与审批时展示的建议不一致")
            persistent_rule = (
                _always_rule(
                    request.action,
                    always_argv_prefix,
                    request.additional_permissions,
                    request.tool_name,
                )
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
                tool_name=request.tool_name,
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


def _always_rule(
    action: NormalizedAction,
    prefix: Sequence[str] | None,
    additional_permissions: AdditionalPermissionProfile,
    tool_name: str,
) -> ActionRule:
    validated_prefix = _validated_exec_prefix(action, prefix)
    if validated_prefix:
        return ActionRule.exec_prefix(
            validated_prefix,
            cwd=action.cwd,
            additional_permissions=additional_permissions,
            allow_authority=True,
            tool_name=tool_name,
        )
    return ActionRule.exact(
        action,
        scope=RuleScope.ALWAYS,
        additional_permissions=additional_permissions,
        tool_name=tool_name,
    )


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
    action_scope: ActionScope | None = None,
    proposed_argv_prefix: Sequence[str] | None = None,
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
    if action_scope is not None and (
        not isinstance(action_scope, ActionScope) or action_scope.action.digest != action.digest
    ):
        raise ValueError("approval action scope 与 action 不匹配")
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
    _validate_prefix_tool(
        normalized_tool,
        proposed_argv_prefix,
        additional_permissions,
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
        action_scope_digest=action_scope.digest if action_scope is not None else "",
        context_digest=security_context_digest(context),
        proposed_argv_prefix=_validated_exec_prefix(action, proposed_argv_prefix),
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


def _validate_prefix_tool(
    tool_name: str,
    prefix: Sequence[str] | None,
    additional_permissions: AdditionalPermissionProfile,
) -> None:
    """Ace exposes prefix authority only for terminal host-escalation requests."""
    if prefix is None:
        return
    if tool_name != "terminal":
        raise ValueError("只有 terminal 工具可以申请命令前缀规则")
    if (
        additional_permissions.sandbox_permissions
        is not SandboxPermissions.REQUIRE_ESCALATED
    ):
        raise ValueError("prefix_rule 只能与 require_escalated 一起使用")


_UNSAFE_PREFIX_EXECUTABLES = frozenset(
    {
        "bash",
        "cmd",
        "cmd.exe",
        "del",
        "erase",
        "node",
        "perl",
        "powershell",
        "powershell.exe",
        "pwsh",
        "python",
        "python3",
        "remove-item",
        "rm",
        "rmdir",
        "ruby",
        "sh",
        "sudo",
        "unlink",
        "zsh",
    }
)


def _validated_exec_prefix(
    action: NormalizedAction,
    prefix: Sequence[str] | None,
) -> tuple[str, ...]:
    """Validate a persistent prefix against one statically parsed command."""
    if prefix is None:
        return ()
    if action.kind is not ActionKind.EXEC:
        raise ApprovalError("只有命令执行支持前缀规则")
    values = tuple(str(token) for token in prefix)
    if len(values) < 2 or any(not token or "\x00" in token for token in values):
        raise ApprovalError("命令前缀至少需要两个非空参数")
    candidates = action.parsed_commands or (action.argv,)
    if len(candidates) != 1 or tuple(candidates[0][: len(values)]) != values:
        raise ApprovalError("命令前缀必须匹配唯一且静态解析的完整命令")
    executable = values[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    if executable in _UNSAFE_PREFIX_EXECUTABLES:
        raise ApprovalError("通用解释器、提权器和删除命令不能保存为始终允许前缀")
    return values


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
