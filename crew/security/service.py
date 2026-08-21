"""Fail-closed coordination for approval, rules, audit, and the P1 fake runtime."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import threading
import time
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable

from crew.security.actions import ActionKind, NormalizedAction
from crew.security.approvals import (
    ApprovalDecision,
    ApprovalError,
    ApprovalManager,
    ApprovalOutcome,
)
from crew.security.audit import AuditEvent, SQLiteSecurityAudit, format_action_for_audit
from crew.security.alerts import SecurityAlertRegistry
from crew.security.context import SecurityContext
from crew.security.file_policy import (
    FilePolicyResult,
    _protected_entries,
    _protected_globs,
    approvable_file_permission_root,
    assess_file_action,
)
from crew.security.grants import GrantRegistry
from crew.security.permission_approvals import PermissionApprovalManager
from crew.security.models import (
    EMPTY_ADDITIONAL_PERMISSIONS,
    AdditionalPermissionProfile,
    ApprovalChannel,
    ConversationPermissionMode,
    FilesystemAccess,
    FilesystemEntry,
    FilesystemOperation,
    GranularApprovalConfig,
    NetworkEntry,
    PermissionProfile,
    SandboxPermissions,
    additional_permissions_cover,
    merge_additional_permissions,
)
from crew.security.rule_store import SQLiteRuleStore
from crew.security.policy import (
    exec_mutation_permissions_ungrantable,
    exec_permissions_needed_for_action,
    filesystem_operation_allowed,
    inferred_exec_mutation_targets,
    network_operation_allowed,
    network_operation_explicitly_denied,
    network_permissions_needed_for_action,
    normalize_additional_permissions,
    permissions_needed_for_action,
    serialize_additional_permissions,
    settings_for_mode,
)
from crew.security.rules import ActionRule, RuleDecision, RuleScope, choose_rule

# 与请求 TTL 对齐的等待上限：超过则按 fail-closed 处理（等同拒绝），避免工具永久挂起。
_DECIDE_WAIT_TIMEOUT = 300.0
_REJECTION_COOLDOWN_SECONDS = 3.0
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecAuthorization:
    """An exec decision plus the exact overlay that may accompany its launch."""

    allowed: bool
    request: dict | None = None
    additional_permissions: AdditionalPermissionProfile = EMPTY_ADDITIONAL_PERMISSIONS

    def __iter__(self):
        # Preserve the historical two-value unpacking used by integrations.
        yield self.allowed
        yield self.request


class _ApprovalWaiter:
    """按 request_id 挂起工具调用，直到 owner 下达决策或授权被撤销。

    作用等同于 codex 的 ``oneshot::Sender`` / opencode 的 ``Deferred``：工具不在
    ``ToolError`` 里把审批请求塞给模型，而是阻塞在 future 上；``decide()`` 落地后
    唤醒，工具据此继续执行或回灌干净错误。这避免了"模型复述审批文本→污染正文"
    和"对话停了无人恢复"两类问题（见 AGENTS.md §2.2 回归自查）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._futures: dict[str, asyncio.Future[ApprovalOutcome | None]] = {}
        self._loops: dict[str, asyncio.AbstractEventLoop] = {}
        # A result is published synchronously under a thread lock before the
        # event-loop wakeup is scheduled. Timeout arbitration can therefore see a
        # decision made by a worker thread even if its callback has not run yet.
        self._results: dict[str, ApprovalOutcome | None] = {}
        # request_id -> (session_key, owner, task_id)，用于生命周期结束时成批唤醒。
        self._meta: dict[str, tuple[tuple[str, ...], str, str]] = {}

    def register(
        self,
        request_id: str,
        *,
        session_key: tuple[str, ...],
        owner_account_id: str,
        task_id: str = "",
    ) -> None:
        """为新建的 pending 请求登记一个 future；复用请求不重复登记。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # 无事件循环（单元 registry 直测）→ 不阻塞，decide 仍可正常落地。
            return
        future = loop.create_future()
        with self._lock:
            if request_id in self._futures:
                return
            self._futures[request_id] = future
            self._loops[request_id] = loop
            self._meta[request_id] = (
                session_key,
                str(owner_account_id),
                str(task_id).strip(),
            )

    async def wait(self, request_id: str) -> ApprovalOutcome | None:
        """阻塞至决策到达；请求不存在/超时/被撤销均返回 None（按拒绝处理）。"""
        with self._lock:
            future = self._futures.get(request_id)
        if future is None:
            return None
        done, _pending = await asyncio.wait(
            (future,),
            timeout=_DECIDE_WAIT_TIMEOUT,
        )
        if not done:
            # Keep the future registered. SecurityApprovalService takes its
            # decision lock after timeout, so it can distinguish "timeout won"
            # from a concurrent decide() that already published a result.
            return None
        try:
            return future.result()
        finally:
            self._cleanup(request_id)

    def take_result(
        self,
        request_id: str,
    ) -> tuple[bool, ApprovalOutcome | None]:
        """Take a result that raced with timeout without cancelling its future."""
        pending_future: asyncio.Future[ApprovalOutcome | None] | None = None
        with self._lock:
            if request_id in self._results:
                outcome = self._results[request_id]
                future = self._futures.get(request_id)
                if future is not None and not future.done():
                    # take_result() is called by the owning event-loop coroutine
                    # after its timeout. Complete the shared future before removing
                    # its registry entry so any concurrent waiter sees the same
                    # terminal decision rather than timing out independently.
                    pending_future = future
                self._cleanup_locked(request_id)
            else:
                future = self._futures.get(request_id)
                if future is None or not future.done():
                    return False, None
                outcome = future.result()
                self._cleanup_locked(request_id)
        if pending_future is not None:
            pending_future.set_result(outcome)
        return True, outcome

    def resolve(self, request_id: str, outcome: ApprovalOutcome | None) -> None:
        """Resolve without removing; wait() is the single cleanup owner.

        Gateway may decide after register() but before the tool coroutine actually
        enters wait(). Removing here would make that later wait observe no future
        and treat a valid approval as rejection.
        """
        with self._lock:
            future = self._futures.get(request_id)
            if future is None or request_id in self._results:
                return
            self._results[request_id] = outcome
            loop = self._loops[request_id]
        try:
            loop.call_soon_threadsafe(self._deliver, request_id)
        except RuntimeError:
            # A closed loop has no live waiter to wake. Keep the synchronous result
            # available for terminal arbitration and lifecycle cleanup.
            return

    def _deliver(self, request_id: str) -> None:
        """Set an asyncio future only from its owning event-loop thread."""
        with self._lock:
            future = self._futures.get(request_id)
            if future is None or request_id not in self._results:
                return
            outcome = self._results[request_id]
        if not future.done():
            future.set_result(outcome)

    def cancel_for_session(self, session_key: tuple[str, ...]) -> int:
        """模式切换：把该精确会话上下文下的 pending 等待按 None 唤醒。"""
        with self._lock:
            request_ids = [rid for rid, (sk, _o, _t) in self._meta.items() if sk == session_key]
        return self._cancel(request_ids)

    def cancel_for_owned_session(self, owner_account_id: str, session_id: str) -> int:
        """真正会话结束：跨 workspace key 清理 owner/session 的等待方。"""
        owner = str(owner_account_id).strip()
        session = str(session_id).strip()
        with self._lock:
            request_ids = [
                rid
                for rid, (key, request_owner, _task) in self._meta.items()
                if request_owner == owner and len(key) >= 4 and key[3] == session
            ]
        return self._cancel(request_ids)

    def cancel_for_task(self, owner_account_id: str, session_id: str, task_id: str) -> int:
        owner = str(owner_account_id).strip()
        session = str(session_id).strip()
        task = str(task_id).strip()
        if not owner or not session or not task:
            return 0
        with self._lock:
            request_ids = [
                rid
                for rid, (key, request_owner, request_task) in self._meta.items()
                if request_owner == owner
                and request_task == task
                and len(key) >= 4
                and key[3] == session
            ]
        return self._cancel(request_ids)

    def cancel_for_owner(self, owner_account_id: str) -> int:
        owner = str(owner_account_id).strip()
        with self._lock:
            request_ids = [rid for rid, (_sk, o, _task) in self._meta.items() if o == owner]
        return self._cancel(request_ids)

    def _cancel(self, request_ids: list[str]) -> int:
        for rid in request_ids:
            self.resolve(rid, None)
        return len(request_ids)

    def _cleanup(self, request_id: str) -> None:
        with self._lock:
            self._cleanup_locked(request_id)

    def _cleanup_locked(self, request_id: str) -> None:
        self._futures.pop(request_id, None)
        self._loops.pop(request_id, None)
        self._results.pop(request_id, None)
        self._meta.pop(request_id, None)


class SecurityApprovalService:
    """Coordinate authorization state without starting a host process."""

    def __init__(
        self,
        approvals: ApprovalManager,
        grants: GrantRegistry,
        rules: SQLiteRuleStore,
        audit: SQLiteSecurityAudit,
        *,
        db_path: str | Path,
        approval_ui_available: Callable[[], bool] | None = None,
        approval_config: GranularApprovalConfig | None = None,
        alerts: SecurityAlertRegistry | None = None,
        security_enabled: bool = True,
    ) -> None:
        self.approvals = approvals
        self.grants = grants
        self.permission_approvals = PermissionApprovalManager(grants)
        self.rules = rules
        self.audit = audit
        self.db_path = Path(db_path)
        self._approval_ui_available = approval_ui_available or _current_approval_ui_available
        self._approval_config = approval_config or GranularApprovalConfig()
        self.alerts = alerts
        # O 基线：安全管控强制启用（settings.strict_security_enabled），装配参数仅作兼容。
        self.security_enabled = True
        self._mode_lock = threading.Lock()
        # Serializes approval-decide (grant issue → durable audit → rollback) against
        # grant consumption in authorize_*. Without this, a concurrent authorize could
        # consume a once grant during the audit window, making the fail-closed rollback
        # a no-op (H-4). RLock so decide's internal audit/rule calls remain re-entrant.
        self._decision_lock = threading.RLock()
        # 阻塞等待中的工具调用；decide/撤销时唤醒。把"审批请求"与"工具执行"重新接通，
        # 否则工具只能抛 ToolError 让模型复述，污染正文且 turn 结束后无人恢复。
        self._waiters = _ApprovalWaiter()
        self._permission_waiters = _ApprovalWaiter()
        self._session_modes: dict[tuple[str, str, str, str], ConversationPermissionMode] = {}
        self._recent_rejections: dict[tuple[str, str, str, str], float] = {}
        # Last-observer disconnect is a resumable transport boundary: pending
        # decisions and transient grants are revoked, and no new authority may
        # be issued until an authenticated socket subscribes again.
        self._frozen_sessions: set[tuple[str, str]] = set()

    @contextmanager
    def operator_approval_surface(self):
        """把调用方登记为可用的审批界面（CLI 等操作员驱动面）。

        运行时工具路径必须有在线渲染器才允许创建待审批请求（fail-closed，
        防止无人应答的挂起）；但 `crew security fake-*` 这类操作员命令本身就是
        审批面——操作员随后用 `security decide` 应答。在这些入口临时放行。
        """
        original = self._approval_ui_available
        self._approval_ui_available = lambda: True
        try:
            yield
        finally:
            self._approval_ui_available = original

    def set_alerts(self, alerts: SecurityAlertRegistry | None) -> None:
        """Attach the owner/admin alert kill switch after host callbacks exist."""
        self.alerts = alerts

    def _alert_denial_source(self, context: SecurityContext) -> str | None:
        if self.alerts is None:
            return None
        source = self.alerts.should_deny(
            context.owner_account_id,
            context.session_id,
            context.task_id,
        )
        return source or None

    def set_mode(
        self,
        context: SecurityContext,
        mode: ConversationPermissionMode,
        *,
        source: str = "gateway_owner",
        reason: str = "",
    ) -> bool:
        """Durably audit a mode transition, then revoke the old session authority.

        The Gateway uses the return value to freeze the current turn. Revoking
        pending requests and transient grants here prevents a retry captured under
        the old policy from becoming authority after the switch.
        """
        if not isinstance(mode, ConversationPermissionMode):
            raise ValueError(f"未知对话安全模式: {mode!r}")
        normalized_source = _bounded_mode_metadata(
            source,
            "mode source",
            128,
            allow_empty=False,
        )
        normalized_reason = _bounded_mode_metadata(
            reason,
            "mode reason",
            1000,
            allow_empty=True,
        )
        key = _session_key(context)
        with self._decision_lock:
            with self._mode_lock:
                previous = self._session_modes.get(
                    key,
                    self._default_mode,
                )
                if previous is mode:
                    return False
            self.audit.record(
                AuditEvent.for_mode_change(
                    context,
                    previous_mode=previous.value,
                    current_mode=mode.value,
                    decision_source=normalized_source,
                    reason=normalized_reason,
                )
            )
            # Held under _decision_lock so a concurrent decide() cannot publish a
            # grant between the terminal sweep and the mode publication.
            self.approvals.revoke_pending_session(context)
            self.permission_approvals.revoke_pending_session(context)
            self.grants.revoke_context_session(context)
            self._waiters.cancel_for_session(key)
            self._permission_waiters.cancel_for_session(key)
            with self._mode_lock:
                self._session_modes[key] = mode
            return True

    def freeze_session(self, owner_account_id: str, session_id: str) -> int:
        """Freeze a disconnected session and revoke every transient authority."""

        owner = str(owner_account_id).strip()
        session = str(session_id).strip()
        if not owner or not session:
            return 0
        with self._decision_lock:
            self._frozen_sessions.add((owner, session))
            self._waiters.cancel_for_owned_session(owner, session)
            self._permission_waiters.cancel_for_owned_session(owner, session)
            return (
                self.approvals.end_owned_session(owner, session)
                + self.permission_approvals.end_owned_session(owner, session)
                + self.grants.revoke_owned_session(owner, session)
            )

    def resume_session(self, owner_account_id: str, session_id: str) -> bool:
        """Resume only after a newly authenticated socket claims the session."""

        owner = str(owner_account_id).strip()
        session = str(session_id).strip()
        if not owner or not session:
            return False
        with self._decision_lock:
            was_frozen = (owner, session) in self._frozen_sessions
            self._frozen_sessions.discard((owner, session))
            return was_frozen

    def session_is_frozen(self, owner_account_id: str, session_id: str) -> bool:
        owner = str(owner_account_id).strip()
        session = str(session_id).strip()
        with self._decision_lock:
            return (owner, session) in self._frozen_sessions

    def _context_is_frozen(self, context: SecurityContext) -> bool:
        return (context.owner_account_id, context.session_id) in self._frozen_sessions

    def end_session(self, owner_account_id: str, session_id: str) -> int:
        """Revoke transient authority when an authenticated session truly ends."""
        owner = str(owner_account_id).strip()
        session = str(session_id).strip()
        if not owner or not session:
            return 0
        with self._decision_lock:
            self._frozen_sessions.discard((owner, session))
            with self._mode_lock:
                mode_keys = [
                    key for key in self._session_modes if key[1] == owner and key[3] == session
                ]
                for key in mode_keys:
                    self._session_modes.pop(key, None)
            self._recent_rejections = {
                key: expiry
                for key, expiry in self._recent_rejections.items()
                if not (key[0] == owner and key[2] == session)
            }
            self._waiters.cancel_for_owned_session(owner, session)
            self._permission_waiters.cancel_for_owned_session(owner, session)
            return (
                len(mode_keys)
                + self.approvals.end_owned_session(owner, session)
                + self.permission_approvals.end_owned_session(owner, session)
                + self.grants.revoke_owned_session(owner, session)
            )

    def end_task(self, owner_account_id: str, session_id: str, task_id: str) -> int:
        """Revoke turn-scoped capabilities and wake pending requests on turn end."""
        owner = str(owner_account_id).strip()
        session = str(session_id).strip()
        task = str(task_id).strip()
        if not owner or not session or not task:
            return 0
        with self._decision_lock:
            self._waiters.cancel_for_task(owner, session, task)
            self._permission_waiters.cancel_for_task(owner, session, task)
            return (
                self.approvals.revoke_pending_task(owner, session, task)
                + self.grants.revoke_task_identity(owner, session, task)
                + self.permission_approvals.revoke_pending_task(owner, session, task)
            )

    def mode_for(self, context: SecurityContext) -> ConversationPermissionMode:
        with self._mode_lock:
            return self._session_modes.get(
                _session_key(context),
                self._default_mode,
            )

    def _base_profile(self, context: SecurityContext):
        return settings_for_mode(
            self.mode_for(context),
            context.workspace_root,
            deny_entries=_protected_entries(context, self.db_path),
            deny_globs=_protected_globs(context),
        ).profile

    @property
    def _default_mode(self) -> ConversationPermissionMode:
        return (
            ConversationPermissionMode.REQUEST_APPROVAL
            if self.security_enabled
            else ConversationPermissionMode.FULL_ACCESS
        )

    def revoke_owner(self, owner_account_id: str) -> int:
        """Drop owner-scoped in-memory modes, pending approvals, and transient grants."""
        owner = str(owner_account_id).strip()
        with self._decision_lock:
            self._frozen_sessions = {key for key in self._frozen_sessions if key[0] != owner}
            with self._mode_lock:
                mode_keys = [key for key in self._session_modes if key[1] == owner]
                for key in mode_keys:
                    self._session_modes.pop(key, None)
            self._recent_rejections = {
                key: expiry for key, expiry in self._recent_rejections.items() if key[0] != owner
            }
            self._waiters.cancel_for_owner(owner)
            self._permission_waiters.cancel_for_owner(owner)
            return (
                len(mode_keys)
                + self.approvals.revoke_owner(owner)
                + self.permission_approvals.revoke_owner(owner)
                + self.grants.revoke_owner(owner)
            )

    def request_permissions(
        self,
        context: SecurityContext,
        permissions: AdditionalPermissionProfile,
        *,
        reason: str = "",
        tool_name: str = "request_permissions",
    ) -> dict:
        with self._decision_lock:
            return self._request_permissions_locked(
                context,
                permissions,
                reason=reason,
                tool_name=tool_name,
            )

    def _request_permissions_locked(
        self,
        context: SecurityContext,
        permissions: AdditionalPermissionProfile,
        *,
        reason: str = "",
        tool_name: str = "request_permissions",
    ) -> dict:
        """Create a Codex-style capability request for the current turn."""
        if self._context_is_frozen(context):
            raise ApprovalError("会话连接已断开，重新认证连接后才能请求权限", terminal=True)
        alert_source = self._alert_denial_source(context)
        if alert_source is not None:
            self.audit.record_permission(
                context,
                normalize_additional_permissions(permissions),
                action_type="permission_decision",
                decision="reject",
                decision_source=alert_source,
                approval_mode=self.mode_for(context).value,
                tool_name=tool_name,
                granted_permissions=AdditionalPermissionProfile(),
                reason=reason,
            )
            raise ApprovalError("安全告警已触发，权限请求自动拒绝", terminal=True)
        normalized = normalize_additional_permissions(permissions)
        if self.mode_for(context) is ConversationPermissionMode.READ_ONLY and any(
            entry.access is FilesystemAccess.READ_WRITE for entry in normalized.filesystem
        ):
            raise ApprovalError("只读模式不可升级为文件写入权限", terminal=True)
        if normalized.allow_local_binding and os.name == "nt":
            raise ApprovalError("当前 Windows 原生运行时不支持本地端口监听授权")
        if not _additional_permissions_safe(context, normalized, self.db_path):
            raise ApprovalError("额外权限包含不可升级的运行时路径")
        if not self._has_approval_ui(ApprovalChannel.PERMISSION):
            self.audit.record_permission(
                context,
                normalized,
                action_type="permission_decision",
                decision="reject",
                decision_source=self._approval_denial_source(ApprovalChannel.PERMISSION),
                approval_mode=self.mode_for(context).value,
                tool_name=tool_name,
                granted_permissions=AdditionalPermissionProfile(),
                reason=reason,
            )
            raise ApprovalError("当前没有可用审批界面，权限请求已自动拒绝", terminal=True)
        request, created = self.permission_approvals.create_or_get(
            context,
            normalized,
            reason=reason,
            tool_name=tool_name,
        )
        public = _public_permission_request(request)
        self._permission_waiters.register(
            request.request_id,
            session_key=_session_key(context),
            owner_account_id=context.owner_account_id,
            task_id=context.task_id,
        )
        if not created:
            return public
        try:
            self.audit.record_permission(
                replace(context, request_id=request.request_id),
                normalized,
                action_type="permission_requested",
                decision="pending",
                decision_source="gateway",
                approval_mode=self.mode_for(context).value,
                tool_name=request.tool_name,
                granted_permissions=AdditionalPermissionProfile(),
                reason=request.reason,
            )
        except Exception:
            self.permission_approvals.cancel(request.request_id, context)
            self._permission_waiters.resolve(request.request_id, None)
            raise
        if not self._push_pending_approval(context, public) and not self._has_approval_ui(
            ApprovalChannel.PERMISSION
        ):
            self.permission_approvals.cancel(request.request_id, context)
            self._permission_waiters.resolve(request.request_id, None)
            self.audit.record_permission(
                replace(context, request_id=request.request_id),
                normalized,
                action_type="permission_decision",
                decision="reject",
                decision_source=self._approval_denial_source(ApprovalChannel.PERMISSION),
                approval_mode=self.mode_for(context).value,
                tool_name=request.tool_name,
                granted_permissions=AdditionalPermissionProfile(),
                reason=request.reason,
            )
            raise ApprovalError("审批界面不可用，权限请求已自动拒绝", terminal=True)
        return public

    def pending_permissions(self, context: SecurityContext) -> list[dict]:
        return [
            _public_permission_request(request)
            for request in self.permission_approvals.list_pending(context)
        ]

    def decide_permissions(
        self,
        context: SecurityContext,
        *,
        request_id: str,
        nonce: str,
        decision: ApprovalDecision,
        granted_permissions: AdditionalPermissionProfile | None = None,
    ) -> dict:
        with self._decision_lock:
            if self._context_is_frozen(context):
                raise ApprovalError("会话连接已断开，旧审批不可继续", terminal=True)
            try:
                outcome = self.permission_approvals.decide(
                    request_id,
                    nonce,
                    decision,
                    context,
                    granted_permissions=granted_permissions,
                )
            except ApprovalError as exc:
                if exc.terminal:
                    self._permission_waiters.resolve(request_id, None)
                raise
            try:
                self.audit.record_permission(
                    replace(context, request_id=outcome.request.request_id),
                    outcome.request.requested_permissions,
                    action_type="permission_decision",
                    decision=outcome.decision.value,
                    decision_source="desktop_user",
                    rule_scope=outcome.scope.value if outcome.scope else "",
                    approval_mode=self.mode_for(context).value,
                    tool_name=outcome.request.tool_name,
                    granted_permissions=outcome.granted_permissions,
                    reason=outcome.request.reason,
                )
            except Exception:
                if outcome.grant is not None:
                    self.grants.revoke_permission(outcome.grant.grant_id)
                self._permission_waiters.resolve(outcome.request.request_id, None)
                raise
            self._permission_waiters.resolve(outcome.request.request_id, outcome)
            return {
                "status": "authorized" if outcome.grant is not None else "rejected",
                "decision": outcome.decision.value,
                "scope": outcome.scope.value if outcome.scope else "turn",
                "permissions": serialize_additional_permissions(outcome.granted_permissions),
            }

    async def await_permission_decision(self, request_id: str):
        try:
            outcome = await self._permission_waiters.wait(request_id)
        except asyncio.CancelledError:
            with self._decision_lock:
                request = self.permission_approvals.cancel_pending(request_id)
                if request is not None:
                    self._permission_waiters.resolve(request_id, None)
                    self._permission_waiters.take_result(request_id)
                    self.audit.record_permission(
                        _context_for_permission_request(request),
                        request.requested_permissions,
                        action_type="permission_decision",
                        decision="reject",
                        decision_source="permission_cancelled",
                        approval_mode=self.mode_for(_context_for_permission_request(request)).value,
                        tool_name=request.tool_name,
                        granted_permissions=AdditionalPermissionProfile(),
                        reason=request.reason,
                    )
            raise
        if outcome is not None:
            return outcome
        with self._decision_lock:
            resolved, raced = self._permission_waiters.take_result(request_id)
            if resolved:
                return raced
            request = self.permission_approvals.cancel_pending(request_id)
            if request is None:
                resolved, raced = self._permission_waiters.take_result(request_id)
                return raced if resolved else None
            self._permission_waiters.resolve(request_id, None)
            self._permission_waiters.take_result(request_id)
            request_context = _context_for_permission_request(request)
            self.audit.record_permission(
                request_context,
                request.requested_permissions,
                action_type="permission_decision",
                decision="reject",
                decision_source="permission_timeout",
                approval_mode=self.mode_for(request_context).value,
                tool_name=request.tool_name,
                granted_permissions=AdditionalPermissionProfile(),
                reason=request.reason,
            )
        return None

    def request_fake_execution(
        self,
        context: SecurityContext,
        action: NormalizedAction,
        *,
        tool_name: str = "security_fake_exec",
    ) -> dict:
        """Create a real approval request but deliberately perform no execution."""
        return self.request_action(
            context,
            action,
            tool_name=tool_name,
            risk_class="fake_execution",
        )

    def request_action(
        self,
        context: SecurityContext,
        action: NormalizedAction,
        *,
        tool_name: str,
        risk_class: str,
        preview: str = "",
        additional_permissions: AdditionalPermissionProfile = EMPTY_ADDITIONAL_PERMISSIONS,
        proposed_argv_prefix: Sequence[str] | None = None,
    ) -> dict:
        with self._decision_lock:
            return self._request_action_locked(
                context,
                action,
                tool_name=tool_name,
                risk_class=risk_class,
                preview=preview,
                additional_permissions=additional_permissions,
                proposed_argv_prefix=proposed_argv_prefix,
            )

    def _request_action_locked(
        self,
        context: SecurityContext,
        action: NormalizedAction,
        *,
        tool_name: str,
        risk_class: str,
        preview: str = "",
        additional_permissions: AdditionalPermissionProfile = EMPTY_ADDITIONAL_PERMISSIONS,
        proposed_argv_prefix: Sequence[str] | None = None,
    ) -> dict:
        """Create or reuse one exact pending request without performing the action."""
        if self._context_is_frozen(context):
            raise ApprovalError("会话连接已断开，重新认证连接后才能请求审批", terminal=True)
        channel = _approval_channel_for_action(action)
        base_profile = self._base_profile(context)
        effective_additional_permissions = merge_additional_permissions(
            self.grants.additional_permissions(context),
            additional_permissions,
        )
        effective_profile = replace(
            base_profile,
            filesystem=(*base_profile.filesystem, *effective_additional_permissions.filesystem),
            network_entries=(*base_profile.network_entries, *effective_additional_permissions.network),
            allow_local_binding=(
                base_profile.allow_local_binding
                or effective_additional_permissions.allow_local_binding
            ),
        )
        if not self._has_approval_ui(channel):
            self.audit.record(
                AuditEvent.for_action(
                    context,
                    action,
                    action_type="approval_decision",
                    decision="reject",
                    decision_source=self._approval_denial_source(channel),
                    permission_profile_hash=_permission_profile_hash(base_profile),
                    approval_mode=self.mode_for(context).value,
                    tool_name=tool_name,
                    additional_permissions_summary=_permissions_summary(additional_permissions),
                )
            )
            raise ApprovalError("当前没有可用审批界面，请求已自动拒绝", terminal=True)
        request, created = self.approvals.create_or_get(
            context,
            action,
            tool_name,
            base_profile_hash=_permission_profile_hash(self._base_profile(context)),
            risk_class=risk_class,
            preview=preview,
            additional_permissions=additional_permissions,
            effective_profile=effective_profile,
            proposed_argv_prefix=proposed_argv_prefix,
        )
        # Register even for a reused request. The prior caller may have timed out or
        # been cancelled while the approval itself remains pending; a new caller must
        # still be attachable and resumable instead of receiving an immediate None.
        self._waiters.register(
            request.request_id,
            session_key=_session_key(context),
            owner_account_id=context.owner_account_id,
            task_id=context.task_id,
        )
        if not created:
            return _public_request(request, include_nonce=True)
        event_context = replace(context, request_id=request.request_id)
        try:
            self.audit.record(
                AuditEvent.for_action(
                    event_context,
                    action,
                    action_type="approval_requested",
                    decision="pending",
                    decision_source="gateway",
                    permission_profile_hash=request.base_profile_hash,
                    approval_mode=self.mode_for(context).value,
                    tool_name=tool_name,
                    additional_permissions_summary=_permissions_summary(additional_permissions),
                )
            )
        except Exception:
            self.approvals.cancel(request.request_id, context)
            self._waiters.resolve(request.request_id, None)
            raise
        public = _public_request(request, include_nonce=True)
        if not self._push_pending_approval(context, public) and not self._has_approval_ui(channel):
            self.approvals.cancel(request.request_id, context)
            self._waiters.resolve(request.request_id, None)
            self.audit.record(
                AuditEvent.for_action(
                    event_context,
                    action,
                    action_type="approval_decision",
                    decision="reject",
                    decision_source=self._approval_denial_source(channel),
                    permission_profile_hash=request.base_profile_hash,
                    approval_mode=self.mode_for(context).value,
                    tool_name=tool_name,
                    additional_permissions_summary=_permissions_summary(additional_permissions),
                )
            )
            raise ApprovalError("审批界面不可用，请求已自动拒绝", terminal=True)
        return public

    def authorize_file_action(
        self,
        context: SecurityContext,
        action: NormalizedAction,
        *,
        tool_name: str,
        preview: str = "",
    ) -> tuple[FilePolicyResult, str, dict | None]:
        """Evaluate base policy, explicit rules, grants, and auto-review in order."""
        if self.session_is_frozen(context.owner_account_id, context.session_id):
            self._audit_file(context, action, "deny", "session_disconnected", tool_name)
            return FilePolicyResult.DENY, "session_disconnected", None
        alert_source = self._alert_denial_source(context)
        if alert_source is not None:
            self._audit_file(context, action, "deny", alert_source, tool_name)
            return FilePolicyResult.DENY, "安全告警已触发，请求自动拒绝", None
        mode = self.mode_for(context)
        base_profile = self._base_profile(context)
        requested_permissions = permissions_needed_for_action(base_profile, action)
        active_permissions = self.grants.additional_permissions(context)
        target = Path(action.path).expanduser().resolve(strict=False)
        operation = (
            FilesystemOperation.READ if action.operation == "read" else FilesystemOperation.WRITE
        )
        if mode is ConversationPermissionMode.READ_ONLY and operation is FilesystemOperation.WRITE:
            self._audit_file(context, action, "deny", "read_only_mode", tool_name)
            return FilePolicyResult.DENY, "只读模式禁止文件写入", None
        if not requested_permissions.filesystem and not filesystem_operation_allowed(
            base_profile, active_permissions, target, operation
        ):
            self._audit_file(context, action, "deny", "ungrantable_permissions", tool_name)
            return FilePolicyResult.DENY, "目标路径无法映射为安全的额外权限根", None
        if not _additional_permissions_safe(context, requested_permissions, self.db_path):
            self._audit_file(context, action, "deny", "immutable_policy", tool_name)
            return FilePolicyResult.DENY, "目标权限范围包含不可升级的运行时路径", None
        assessment = assess_file_action(
            context,
            action,
            mode,
            db_path=self.db_path,
            additional=active_permissions,
        )
        if assessment.result is FilePolicyResult.DENY:
            self._audit_file(context, action, "deny", "immutable_policy", tool_name)
            return assessment.result, assessment.reason, None
        if (
            mode is not ConversationPermissionMode.FULL_ACCESS
            and self._rejection_cooldown_active(context, action)
        ):
            self._audit_file(context, action, "deny", "recent_user_rejection", tool_name)
            return FilePolicyResult.DENY, "recent_user_rejection", None

        # Rule + grant evaluation runs under _decision_lock so it is mutually
        # exclusive with decide()'s rule-create→audit→rollback and grant-issue→
        # audit→rollback. Otherwise a concurrent decide could publish an ALWAYS rule
        # that this authorize matches before its durable audit commits, then roll it
        # back after execution has already started (H-7).
        with self._decision_lock:
            if self._context_is_frozen(context):
                self._audit_file(
                    context,
                    action,
                    "deny",
                    "session_disconnected",
                    tool_name,
                )
                return FilePolicyResult.DENY, "session_disconnected", None
            session_permissions = self.grants.session_permissions(context)
            rules = self.rules.list(
                os_user=context.os_user,
                owner_account_id=context.owner_account_id,
                workspace_id=context.workspace_id,
            )
            # Persisted DENY is explicit owner policy and must short-circuit base
            # allow, FULL_ACCESS, allow rules, and transient grants.
            if any(
                rule.decision is RuleDecision.DENY
                and _rule_applies_to_tool(rule, tool_name)
                and rule.matches(action)
                for rule in rules
            ):
                self._audit_file(context, action, "deny", "always_deny_rule", tool_name)
                return FilePolicyResult.DENY, "persistent_deny_rule", None
            if assessment.result is FilePolicyResult.ALLOW:
                granted_reason = (
                    "session_permissions"
                    if _has_additional_permissions(active_permissions)
                    else assessment.reason
                )
                self._audit_file(
                    context,
                    action,
                    "allow",
                    "granted_permissions" if active_permissions.filesystem else "base_profile",
                    tool_name,
                    additional_permissions=active_permissions,
                )
                return assessment.result, granted_reason, None

            selected = choose_rule(
                (rule for rule in rules if _rule_applies_to_tool(rule, tool_name)),
                action,
            )
            if (
                selected is not None
                and selected.decision is RuleDecision.ALLOW
                and not _has_additional_permissions(requested_permissions)
            ):
                self._audit_file(context, action, "allow", "always_rule", tool_name)
                return FilePolicyResult.ALLOW, "always_rule", None
            requested_permissions = _file_action_permissions(
                action,
                permission_root=approvable_file_permission_root(
                    context,
                    action.path,
                    db_path=self.db_path,
                ),
            )
            if additional_permissions_cover(session_permissions, requested_permissions):
                self._audit_file(context, action, "allow", "session_permissions", tool_name)
                return FilePolicyResult.ALLOW, "session_permissions", None
            grant = self.grants.authorize_action(
                context,
                action,
                additional_permissions=requested_permissions,
                tool_name=tool_name,
            )
            if grant is not None:
                self._audit_file(
                    context,
                    action,
                    "allow",
                    "runtime_grant",
                    tool_name,
                    additional_permissions=grant.additional_permissions,
                )
                return FilePolicyResult.ALLOW, "runtime_grant", None

        # Reaching this point means the base profile did not cover the requested
        # write or the path is protected; the approval carries that exact scope.
        # AUTO_REVIEW no longer treats every host-external read as low risk. Without
        # a proven public-file classifier that would include SSH/cloud/browser
        # credentials. Exact rules/session grants still bypass prompts above.
        if not self._has_approval_ui(ApprovalChannel.FILE):
            self._audit_file(
                context,
                action,
                "deny",
                self._approval_denial_source(ApprovalChannel.FILE),
                tool_name,
                additional_permissions=requested_permissions,
            )
            return FilePolicyResult.DENY, "当前没有可用审批界面，已自动拒绝", None
        request = self.request_action(
            context,
            action,
            tool_name=tool_name,
            risk_class=(
                "external_file_write" if action.operation != "read" else "external_file_read"
            ),
            preview=preview,
            additional_permissions=requested_permissions,
        )
        return FilePolicyResult.REQUIRE_APPROVAL, assessment.reason, request

    def authorize_exec_action(
        self,
        context: SecurityContext,
        action: NormalizedAction,
        *,
        tool_name: str,
        risk_class: str,
        requires_approval: bool = True,
        auto_allow: bool = False,
        preview: str = "",
        additional_permissions: AdditionalPermissionProfile = EMPTY_ADDITIONAL_PERMISSIONS,
        proposed_argv_prefix: Sequence[str] | None = None,
    ) -> ExecAuthorization:
        """Authorize an exact dangerous command.

        Exec-side hard boundaries (recursive root deletion, disk format, fork bombs;
        spec §4.3) are deliberately NOT enforced by command-string inspection in this
        layer: a naive argv blacklist is trivially bypassable and §4.3 explicitly
        forbids judging by command name. They are enforced by (a) the file-side policy
        for structured file tools (``file_policy`` denies writes/deletes on protected
        roots) and (b) the native OS sandbox + process-tree kill + wall timeout for
        arbitrary exec, uniformly across modes including FULL_ACCESS. The native
        runtime is the enforcement boundary; this method must not replace it with
        command-string heuristics.
        """
        if self.session_is_frozen(context.owner_account_id, context.session_id):
            self._audit_exec(context, action, "deny", "session_disconnected", tool_name)
            return ExecAuthorization(False)
        alert_source = self._alert_denial_source(context)
        if alert_source is not None:
            self._audit_exec(context, action, "deny", alert_source, tool_name)
            return ExecAuthorization(False)
        mode = self.mode_for(context)
        requires_fresh_confirmation = risk_class == "dangerous_command"
        base_profile = self._base_profile(context)
        active_permissions = self.grants.additional_permissions(context)
        effective_additional_permissions = merge_additional_permissions(
            active_permissions,
            additional_permissions,
        )
        effective_profile = replace(
            base_profile,
            filesystem=(*base_profile.filesystem, *effective_additional_permissions.filesystem),
            network_entries=(*base_profile.network_entries, *effective_additional_permissions.network),
            allow_local_binding=(
                base_profile.allow_local_binding
                or effective_additional_permissions.allow_local_binding
            ),
        )
        requested_permissions = exec_permissions_needed_for_action(effective_profile, action)
        if mode is ConversationPermissionMode.READ_ONLY and any(
            entry.access is FilesystemAccess.READ_WRITE
            for entry in requested_permissions.filesystem
        ):
            self._audit_exec(context, action, "deny", "read_only_mode", tool_name)
            return ExecAuthorization(False)
        mutation_targets = inferred_exec_mutation_targets(action)
        if not requested_permissions.filesystem and exec_mutation_permissions_ungrantable(
            effective_profile, mutation_targets
        ):
            self._audit_exec(context, action, "deny", "ungrantable_permissions", tool_name)
            return ExecAuthorization(False)
        if not _additional_permissions_safe(
            context,
            requested_permissions,
            self.db_path,
            mutation_targets=mutation_targets,
        ):
            self._audit_exec(context, action, "deny", "immutable_policy", tool_name)
            return ExecAuthorization(False)
        if (
            mode is not ConversationPermissionMode.FULL_ACCESS
            and self._rejection_cooldown_active(context, action)
        ):
            self._audit_exec(context, action, "deny", "recent_user_rejection", tool_name)
            return ExecAuthorization(False)
        # Rule + grant evaluation under _decision_lock: mutually exclusive with
        # decide()'s rule-create/grant-issue → audit → rollback (H-7), and with the
        # end_session/logout terminal sweep so a grant published in the issue window
        # cannot be consumed after the session ended (H-6).
        with self._decision_lock:
            if self._context_is_frozen(context):
                self._audit_exec(
                    context,
                    action,
                    "deny",
                    "session_disconnected",
                    tool_name,
                )
                return ExecAuthorization(False)
            session_permissions = self.grants.session_permissions(context)
            rules = self.rules.list(
                os_user=context.os_user,
                owner_account_id=context.owner_account_id,
                workspace_id=context.workspace_id,
            )
            if any(
                rule.decision is RuleDecision.DENY
                and _rule_applies_to_tool(rule, tool_name)
                and rule.matches(action)
                for rule in rules
            ):
                self._audit_exec(context, action, "deny", "always_deny_rule", tool_name)
                return ExecAuthorization(False)
            if (
                mode is ConversationPermissionMode.FULL_ACCESS
                and not requires_fresh_confirmation
                and not _has_additional_permissions(requested_permissions)
            ):
                self._audit_exec(context, action, "allow", "full_access", tool_name)
                return ExecAuthorization(True)
            selected = choose_rule(
                (
                    rule
                    for rule in rules
                    if rule.decision is RuleDecision.ALLOW
                    and _rule_applies_to_tool(rule, tool_name)
                    and (
                        additional_permissions.empty
                        or additional_permissions_cover(
                            rule.additional_permissions,
                            additional_permissions,
                        )
                    )
                ),
                action,
            )
            if (
                selected is not None
                and selected.decision is RuleDecision.ALLOW
                and not requires_fresh_confirmation
                and not _has_additional_permissions(requested_permissions)
            ):
                self._audit_exec(context, action, "allow", "always_rule", tool_name)
                return ExecAuthorization(
                    True,
                    additional_permissions=merge_additional_permissions(
                        session_permissions,
                        selected.additional_permissions,
                    )
                    if selected.additional_permissions.sandbox_permissions
                    is not SandboxPermissions.REQUIRE_ESCALATED
                    else selected.additional_permissions,
                )
            grant = self.grants.authorize_action(
                context,
                action,
                additional_permissions=additional_permissions,
                tool_name=tool_name,
            )
            if grant is not None and (
                not requires_fresh_confirmation or grant.scope is RuleScope.ONCE
            ):
                self._audit_exec(
                    context,
                    action,
                    "allow",
                    "runtime_grant",
                    tool_name,
                    additional_permissions=grant.additional_permissions,
                )
                return ExecAuthorization(
                    True,
                    additional_permissions=(
                        grant.additional_permissions
                        if grant.additional_permissions.sandbox_permissions
                        is SandboxPermissions.REQUIRE_ESCALATED
                        else merge_additional_permissions(
                            session_permissions,
                            grant.additional_permissions,
                        )
                    ),
                )
            if (
                mode in {
                    ConversationPermissionMode.REQUEST_APPROVAL,
                    ConversationPermissionMode.AUTO_REVIEW,
                }
                and not requires_approval
                and additional_permissions.empty
            ):
                self._audit_exec(context, action, "allow", "base_profile", tool_name)
                return ExecAuthorization(True, additional_permissions=session_permissions)
            if (
                not additional_permissions.empty
                and additional_permissions.sandbox_permissions
                is not SandboxPermissions.REQUIRE_ESCALATED
                and additional_permissions_cover(session_permissions, additional_permissions)
            ):
                self._audit_exec(context, action, "allow", "session_permissions", tool_name)
                return ExecAuthorization(
                    True,
                    additional_permissions=merge_additional_permissions(
                        session_permissions,
                        additional_permissions,
                    ),
                )
        if (
            auto_allow
            and not requires_fresh_confirmation
            and not _has_additional_permissions(requested_permissions)
        ):
            self._audit_exec(context, action, "allow", "auto_review", tool_name)
            return ExecAuthorization(True)
        if not self._has_approval_ui(ApprovalChannel.EXEC):
            self._audit_exec(
                context,
                action,
                "deny",
                self._approval_denial_source(ApprovalChannel.EXEC),
                tool_name,
                additional_permissions=requested_permissions,
            )
            return ExecAuthorization(False)
        request = self.request_action(
            context,
            action,
            tool_name=tool_name,
            risk_class=risk_class,
            additional_permissions=additional_permissions,
            preview=preview,
            proposed_argv_prefix=proposed_argv_prefix,
        )
        self._audit_exec(
            context,
            action,
            "ask",
            "approval_required",
            tool_name,
            additional_permissions=requested_permissions,
        )
        return ExecAuthorization(False, request)

    def authorize_user_initiated_exec_action(
        self,
        context: SecurityContext,
        action: NormalizedAction,
        *,
        tool_name: str,
    ) -> ExecAuthorization:
        """Authorize an exact command already initiated by an authenticated UI gesture."""
        if self.session_is_frozen(context.owner_account_id, context.session_id):
            self._audit_exec(context, action, "deny", "session_disconnected", tool_name)
            return ExecAuthorization(False)
        alert_source = self._alert_denial_source(context)
        if alert_source is not None:
            self._audit_exec(context, action, "deny", alert_source, tool_name)
            return ExecAuthorization(False)
        with self._decision_lock:
            rules = self.rules.list(
                os_user=context.os_user,
                owner_account_id=context.owner_account_id,
                workspace_id=context.workspace_id,
            )
            if any(
                rule.decision is RuleDecision.DENY
                and _rule_applies_to_tool(rule, tool_name)
                and rule.matches(action)
                for rule in rules
            ):
                self._audit_exec(context, action, "deny", "always_deny_rule", tool_name)
                return ExecAuthorization(False)
        self._audit_exec(context, action, "allow", "desktop_user_gesture", tool_name)
        return ExecAuthorization(True)

    def authorize_network_action(
        self,
        context: SecurityContext,
        action: NormalizedAction,
        *,
        tool_name: str,
        public_target: bool | None = None,
    ) -> tuple[FilePolicyResult, str, dict | None]:
        """Authorize one exact network destination before host-mediated I/O.

        ``public_target`` 仅影响审批 risk_class 标注（CLI security-check 使用）；
        判定语义统一走 O 代 fail-closed 链路。
        """
        if action.kind is not ActionKind.NETWORK:
            raise ValueError("网络授权动作类型无效")
        if self.session_is_frozen(context.owner_account_id, context.session_id):
            self._audit_network(context, action, "deny", "session_disconnected", tool_name)
            return FilePolicyResult.DENY, "session_disconnected", None
        alert_source = self._alert_denial_source(context)
        if alert_source is not None:
            self._audit_network(context, action, "deny", alert_source, tool_name)
            return FilePolicyResult.DENY, "安全告警已触发，请求自动拒绝", None
        base_profile = self._base_profile(context)
        if network_operation_explicitly_denied(base_profile, action):
            self._audit_network(
                context,
                action,
                "deny",
                "immutable_policy",
                tool_name,
            )
            return FilePolicyResult.DENY, "网络目标被基础策略明确拒绝", None
        requested_permissions = network_permissions_needed_for_action(base_profile, action)
        if self._rejection_cooldown_active(context, action):
            self._audit_network(context, action, "deny", "recent_user_rejection", tool_name)
            return FilePolicyResult.DENY, "recent_user_rejection", None
        with self._decision_lock:
            if self._context_is_frozen(context):
                self._audit_network(
                    context,
                    action,
                    "deny",
                    "session_disconnected",
                    tool_name,
                )
                return FilePolicyResult.DENY, "session_disconnected", None
            active_permissions = self.grants.additional_permissions(context)
            rules = self.rules.list(
                os_user=context.os_user,
                owner_account_id=context.owner_account_id,
                workspace_id=context.workspace_id,
            )
            if any(rule.decision is RuleDecision.DENY and rule.matches(action) for rule in rules):
                self._audit_network(context, action, "deny", "always_deny_rule", tool_name)
                return FilePolicyResult.DENY, "persistent_deny_rule", None
            if network_operation_allowed(base_profile, active_permissions, action):
                self._audit_network(context, action, "allow", "base_or_granted", tool_name)
                return FilePolicyResult.ALLOW, "base_or_granted", None
            selected = choose_rule(rules, action)
            if (
                selected is not None
                and selected.decision is RuleDecision.ALLOW
                and not requested_permissions.network
            ):
                self._audit_network(context, action, "allow", "always_rule", tool_name)
                return FilePolicyResult.ALLOW, "always_rule", None
            grant = self.grants.authorize_action(context, action)
            if grant is not None:
                self._audit_network(context, action, "allow", "runtime_grant", tool_name)
                return FilePolicyResult.ALLOW, "runtime_grant", None
        if not self._has_approval_ui(ApprovalChannel.NETWORK):
            self._audit_network(
                context,
                action,
                "deny",
                self._approval_denial_source(ApprovalChannel.NETWORK),
                tool_name,
            )
            return FilePolicyResult.DENY, "当前没有可用审批界面，已自动拒绝", None
        request = self.request_action(
            context,
            action,
            tool_name=tool_name,
            risk_class=(
                "public_network" if public_target else "private_network"
            )
            if public_target is not None
            else "network_request",
            additional_permissions=requested_permissions,
        )
        self._audit_network(context, action, "ask", "approval_required", tool_name)
        return FilePolicyResult.REQUIRE_APPROVAL, "网络目标未获授权", request

    def _audit_file(
        self,
        context: SecurityContext,
        action: NormalizedAction,
        decision: str,
        source: str,
        tool_name: str,
        additional_permissions: AdditionalPermissionProfile = AdditionalPermissionProfile(),
    ) -> None:
        self.audit.record(
            AuditEvent.for_action(
                context,
                action,
                action_type="file_decision",
                decision=decision,
                decision_source=source,
                approval_mode=self.mode_for(context).value,
                tool_name=tool_name,
                additional_permissions_summary=_permissions_summary(additional_permissions),
            )
        )

    def _audit_exec(
        self,
        context: SecurityContext,
        action: NormalizedAction,
        decision: str,
        source: str,
        tool_name: str,
        additional_permissions: AdditionalPermissionProfile = AdditionalPermissionProfile(),
    ) -> None:
        self.audit.record(
            AuditEvent.for_action(
                context,
                action,
                action_type="exec_decision",
                decision=decision,
                decision_source=source,
                approval_mode=self.mode_for(context).value,
                tool_name=tool_name,
                additional_permissions_summary=_permissions_summary(additional_permissions),
            )
        )

    def _audit_network(
        self,
        context: SecurityContext,
        action: NormalizedAction,
        decision: str,
        source: str,
        tool_name: str,
    ) -> None:
        self.audit.record(
            AuditEvent.for_action(
                context,
                action,
                action_type="network_decision",
                decision=decision,
                decision_source=source,
                approval_mode=self.mode_for(context).value,
                tool_name=tool_name,
            )
        )

    def _audit_network(
        self,
        context: SecurityContext,
        action: NormalizedAction,
        decision: str,
        source: str,
        tool_name: str,
    ) -> None:
        self.audit.record(
            AuditEvent.for_action(
                context,
                action,
                action_type="network_decision",
                decision=decision,
                decision_source=source,
                approval_mode=self.mode_for(context).value,
                network_target_summary=f"{action.host}:{action.port}/{action.protocol}",
                tool_name=tool_name,
            )
        )

    def pending(self, context: SecurityContext, *, include_nonce: bool) -> list[dict]:
        return [
            _public_request(request, include_nonce=include_nonce)
            for request in self.approvals.list_pending(context)
        ] + [
            _public_permission_request(request, include_nonce=include_nonce)
            for request in self.permission_approvals.list_pending(context)
        ]

    async def await_decision(self, request_id: str) -> ApprovalOutcome | None:
        """阻塞发起审批请求的工具，直到 owner 下达决策。

        返回 ``ApprovalOutcome`` 表示已决策（批准则继续、拒绝则由调用方回灌干净错误）；
        返回 ``None`` 表示超时、被撤销或请求失效，一律按拒绝处理（fail-closed）。
        这是把"审批请求"与"工具执行"重新接通的关键：工具不再抛 ToolError 让模型
        复述，而是挂起等待，决策到达后自然恢复 agent 循环。
        """
        try:
            outcome = await self._waiters.wait(request_id)
        except asyncio.CancelledError:
            with self._decision_lock:
                request = self.approvals.cancel_pending(request_id)
                if request is not None:
                    self._waiters.resolve(request_id, None)
                    self._waiters.take_result(request_id)
                    request_context = _context_for_action_request(request)
                    self.audit.record(
                        AuditEvent.for_action(
                            request_context,
                            request.action,
                            action_type="approval_decision",
                            decision="reject",
                            decision_source="approval_cancelled",
                            permission_profile_hash=request.base_profile_hash,
                            approval_mode=self.mode_for(request_context).value,
                            tool_name=request.tool_name,
                            additional_permissions_summary=_permissions_summary(
                                request.additional_permissions
                            ),
                        )
                    )
            raise
        if outcome is not None:
            return outcome
        with self._decision_lock:
            resolved, raced = self._waiters.take_result(request_id)
            if resolved:
                return raced
            request = self.approvals.cancel_pending(request_id)
            if request is None:
                resolved, raced = self._waiters.take_result(request_id)
                return raced if resolved else None
            self._waiters.resolve(request_id, None)
            self._waiters.take_result(request_id)
            request_context = _context_for_action_request(request)
            self.audit.record(
                AuditEvent.for_action(
                    request_context,
                    request.action,
                    action_type="approval_decision",
                    decision="reject",
                    decision_source="approval_timeout",
                    permission_profile_hash=request.base_profile_hash,
                    approval_mode=self.mode_for(request_context).value,
                    tool_name=request.tool_name,
                    additional_permissions_summary=_permissions_summary(
                        request.additional_permissions
                    ),
                )
            )
        return None

    def decide(
        self,
        context: SecurityContext,
        *,
        request_id: str,
        nonce: str,
        decision: ApprovalDecision,
        always_argv_prefix: Sequence[str] | None = None,
    ) -> dict:
        """Apply one decision under the decision lock."""
        permission_request = self.permission_approvals.get_pending(request_id, context)
        if permission_request is not None:
            return self.decide_permissions(
                context,
                request_id=request_id,
                nonce=nonce,
                decision=decision,
            )
        pending = self.approvals.get_pending(request_id, context)
        if (
            pending is not None
            and pending.tool_name == "security_fake_exec"
            and decision in {ApprovalDecision.SESSION, ApprovalDecision.ALWAYS}
        ):
            raise ApprovalError("fake execution 只允许 once 或 reject")
        if (
            pending is not None
            and pending.risk_class == "dangerous_command"
            and decision not in {ApprovalDecision.ONCE, ApprovalDecision.REJECT}
        ):
            raise ApprovalError("高风险命令必须逐次批准")
        with self._decision_lock:
            try:
                return self._decide_locked(
                    context,
                    request_id=request_id,
                    nonce=nonce,
                    decision=decision,
                    always_argv_prefix=always_argv_prefix,
                )
            except ApprovalError as exc:
                # 只有不存在/已处理/过期这类终态才唤醒等待方。nonce/context/prefix
                # 校验错误仍可在 TTL 内由用户修正重试，不能提前把正在等的工具判拒绝。
                if exc.terminal:
                    self._waiters.resolve(request_id, None)
                raise

    def _decide_locked(
        self,
        context: SecurityContext,
        *,
        request_id: str,
        nonce: str,
        decision: ApprovalDecision,
        always_argv_prefix: Sequence[str] | None = None,
    ) -> dict:
        """Apply one decision and compensate grants/rules if durable audit fails."""
        if self._context_is_frozen(context):
            raise ApprovalError("会话连接已断开，旧审批不可继续", terminal=True)
        outcome = self.approvals.decide(
            request_id,
            nonce,
            decision,
            context,
            always_argv_prefix=always_argv_prefix,
        )
        stored_rule = False
        persisted_rule = outcome.persistent_rule
        try:
            if persisted_rule is not None:
                action_summary, action_detail = format_action_for_audit(outcome.request.action)
                persisted_rule = replace(
                    persisted_rule,
                    action_summary=action_summary,
                    action_detail=action_detail,
                )
                self.rules.create(
                    persisted_rule,
                    os_user=context.os_user,
                    owner_account_id=context.owner_account_id,
                    workspace_id=context.workspace_id,
                )
                stored_rule = True
            event_context = replace(context, request_id=outcome.request.request_id)
            self.audit.record(
                AuditEvent.for_action(
                    event_context,
                    outcome.request.action,
                    action_type="approval_decision",
                    decision=outcome.decision.value,
                    decision_source="desktop_user",
                    rule_id=persisted_rule.rule_id if persisted_rule else "",
                    rule_scope=(
                        persisted_rule.scope.value
                        if persisted_rule
                        else outcome.grant.scope.value
                        if outcome.grant
                        else ""
                    ),
                    permission_profile_hash=outcome.request.base_profile_hash,
                    approval_mode=self.mode_for(context).value,
                    tool_name=outcome.request.tool_name,
                    additional_permissions_summary=_permissions_summary(
                        outcome.request.additional_permissions
                    ),
                )
            )
            if persisted_rule is not None:
                self.audit.record(
                    AuditEvent.for_rule(
                        event_context,
                        rule_id=persisted_rule.rule_id,
                        action_type="rule_created",
                        decision="allow",
                        action=outcome.request.action,
                    )
                )
        except Exception:
            if stored_rule and outcome.persistent_rule is not None:
                self.rules.delete(
                    outcome.persistent_rule.rule_id,
                    os_user=context.os_user,
                    owner_account_id=context.owner_account_id,
                    workspace_id=context.workspace_id,
                )
            if outcome.grant is not None:
                self.grants.revoke(outcome.grant.grant_id)
            self._waiters.resolve(outcome.request.request_id, None)
            raise
        if outcome.decision is ApprovalDecision.REJECT:
            self._recent_rejections[_rejection_key(context, outcome.request.action)] = (
                time.monotonic() + _REJECTION_COOLDOWN_SECONDS
            )
        # 唤醒阻塞在 await_decision 的工具调用：批准则 outcome.grant 非 None，调用方据此继续执行。
        self._waiters.resolve(outcome.request.request_id, outcome)
        if outcome.request.tool_name == "security_fake_exec":
            return self._fake_result(context, outcome)
        return {
            "status": "authorized" if outcome.grant is not None else "rejected",
            "started_process": False,
            "decision": outcome.decision.value,
        }

    def _rejection_cooldown_active(
        self,
        context: SecurityContext,
        action: NormalizedAction,
    ) -> bool:
        key = _rejection_key(context, action)
        with self._decision_lock:
            expiry = self._recent_rejections.get(key, 0.0)
            if expiry <= time.monotonic():
                self._recent_rejections.pop(key, None)
                return False
            return True

    def _has_approval_ui(self, channel: ApprovalChannel) -> bool:
        if not self._approval_config.allows(channel):
            return False
        try:
            return bool(self._approval_ui_available())
        except Exception:
            log.exception("检查安全审批界面状态失败")
            return False

    def _approval_denial_source(self, channel: ApprovalChannel) -> str:
        if not self._approval_config.allows(channel):
            return "approval_channel_disabled"
        return "approval_ui_unavailable"

    @staticmethod
    def _push_pending_approval(context: SecurityContext, request: dict) -> bool:
        """Wake the renderer immediately; polling remains the disconnect fallback."""
        from crew.core.runctx import current_push_fn

        push = current_push_fn.get()
        if push is None:
            return False
        payload = {
            "kind": "security_approval",
            "session_id": context.session_id,
            "request_id": request["request_id"],
        }
        try:
            result = push(context.session_id, payload)
        except Exception:
            log.exception("安全审批主动推送失败")
            return False
        if inspect.isawaitable(result):

            async def finish_push() -> None:
                try:
                    await result
                except Exception:
                    log.exception("安全审批主动推送失败")

            asyncio.create_task(finish_push())
        return True

    def set_rule_enabled(self, context: SecurityContext, rule_id: str, enabled: bool) -> bool:
        with self._decision_lock:
            return self._set_rule_enabled_locked(context, rule_id, enabled)

    def _set_rule_enabled_locked(
        self,
        context: SecurityContext,
        rule_id: str,
        enabled: bool,
    ) -> bool:
        """Mutate one owner rule and restore its previous state if audit fails."""
        current = self.rules.list(
            os_user=context.os_user,
            owner_account_id=context.owner_account_id,
            workspace_id=context.workspace_id,
            include_disabled=True,
        )
        if not any(rule.rule_id == rule_id for rule in current):
            return False
        changed = self.rules.set_enabled(
            rule_id,
            enabled,
            os_user=context.os_user,
            owner_account_id=context.owner_account_id,
            workspace_id=context.workspace_id,
        )
        if not changed:
            return False
        try:
            self.audit.record(
                AuditEvent.for_rule(
                    context,
                    rule_id=rule_id,
                    action_type="rule_created" if enabled else "rule_disabled",
                    decision="enabled" if enabled else "disabled",
                )
            )
        except Exception:
            self.rules.set_enabled(
                rule_id,
                not enabled,
                os_user=context.os_user,
                owner_account_id=context.owner_account_id,
                workspace_id=context.workspace_id,
            )
            raise
        return True

    def delete_rule(self, context: SecurityContext, rule_id: str) -> bool:
        with self._decision_lock:
            return self._delete_rule_locked(context, rule_id)

    def _delete_rule_locked(self, context: SecurityContext, rule_id: str) -> bool:
        """Delete one owner rule, restoring it when durable audit cannot commit."""
        matches = [
            rule
            for rule in self.rules.list(
                os_user=context.os_user,
                owner_account_id=context.owner_account_id,
                workspace_id=context.workspace_id,
                include_disabled=True,
            )
            if rule.rule_id == rule_id
        ]
        if not matches:
            return False
        was_enabled = any(
            rule.rule_id == rule_id
            for rule in self.rules.list(
                os_user=context.os_user,
                owner_account_id=context.owner_account_id,
                workspace_id=context.workspace_id,
            )
        )
        changed = self.rules.delete(
            rule_id,
            os_user=context.os_user,
            owner_account_id=context.owner_account_id,
            workspace_id=context.workspace_id,
        )
        if not changed:
            return False
        try:
            self.audit.record(
                AuditEvent.for_rule(
                    context,
                    rule_id=rule_id,
                    action_type="rule_deleted",
                    decision="deleted",
                )
            )
        except Exception:
            self.rules.create(
                matches[0],
                os_user=context.os_user,
                owner_account_id=context.owner_account_id,
                workspace_id=context.workspace_id,
            )
            if not was_enabled:
                self.rules.set_enabled(
                    rule_id,
                    False,
                    os_user=context.os_user,
                    owner_account_id=context.owner_account_id,
                    workspace_id=context.workspace_id,
                )
            raise
        return True

    def _fake_result(self, context: SecurityContext, outcome: ApprovalOutcome) -> dict:
        if outcome.decision is ApprovalDecision.REJECT:
            return {"status": "rejected", "runtime": "fake", "started_process": False}
        assert outcome.grant is not None
        self.grants.authorize(outcome.grant.grant_id, context, outcome.request.action)
        return {
            "status": "completed",
            "runtime": "fake",
            "started_process": False,
            "action_digest": outcome.request.action_digest,
            "decision": outcome.decision.value,
        }


def _public_request(request, *, include_nonce: bool) -> dict:
    action = asdict(request.action)
    payload = {
        "request_id": request.request_id,
        "action": action,
        "action_digest": request.action_digest,
        "tool_name": request.tool_name,
        "risk_class": request.risk_class,
        "additional_permissions": serialize_additional_permissions(request.additional_permissions),
        "base_profile_hash": request.base_profile_hash,
        **(
            {"effective_permissions": _serialize_permission_profile(request.effective_profile)}
            if request.effective_profile is not None
            else {}
        ),
        "workspace_id": request.workspace_id,
        "session_id": request.session_id,
        "task_id": request.task_id,
        "expires_in_seconds": max(0, int(request.expires_monotonic - request.created_monotonic)),
        "additional_permissions": serialize_additional_permissions(
            request.additional_permissions
        ),
    }
    if request.preview:
        payload["preview"] = request.preview
    if request.proposed_argv_prefix:
        payload["proposed_argv_prefix"] = list(request.proposed_argv_prefix)
    if include_nonce:
        payload["nonce"] = request.nonce
    if request.effective_profile is not None and action.get("kind") == ActionKind.EXEC.value:
        profile = request.effective_profile
        payload["effect_disclosure"] = {
            "filesystem_write_roots": [
                str(entry.root)
                for entry in profile.filesystem
                if entry.access is FilesystemAccess.READ_WRITE
            ],
            "network_policy": profile.network.value,
            "network_entries": serialize_additional_permissions(
                AdditionalPermissionProfile(network=profile.network_entries)
            )["network"],
            "unknown_side_effects": True,
        }
    return payload


def _serialize_permission_profile(profile: PermissionProfile) -> dict:
    return {
        "kind": profile.kind.value,
        "filesystem": [
            {
                "root": str(entry.root),
                "access": entry.access.value,
                "escalatable": entry.escalatable,
            }
            for entry in profile.filesystem
        ],
        "filesystem_globs": [
            {
                "root": str(entry.root),
                "pattern": entry.pattern,
                "access": entry.access.value,
            }
            for entry in profile.filesystem_globs
        ],
        "network_policy": profile.network.value,
        "network": serialize_additional_permissions(
            AdditionalPermissionProfile(network=profile.network_entries)
        )["network"],
        "allow_local_binding": profile.allow_local_binding,
    }


def _public_permission_request(request, *, include_nonce: bool = True) -> dict:
    permissions = serialize_additional_permissions(request.requested_permissions)
    payload = {
        "request_type": "permission",
        "request_id": request.request_id,
        "action": {"kind": "permission", "operation": "grant"},
        "action_digest": "",
        "tool_name": request.tool_name,
        "risk_class": "permission_request",
        "reason": request.reason,
        "additional_permissions": permissions,
        "permissions": permissions,
        "workspace_id": request.workspace_id,
        "session_id": request.session_id,
        "task_id": request.task_id,
        "expires_in_seconds": max(0, int(request.expires_monotonic - request.created_monotonic)),
    }
    if include_nonce:
        payload["nonce"] = request.nonce
    return payload


def _permissions_summary(value: AdditionalPermissionProfile) -> str:
    if not value.filesystem and not value.network and not value.allow_local_binding:
        return ""
    return json.dumps(serialize_additional_permissions(value), ensure_ascii=False, sort_keys=True)


def _has_additional_permissions(value: AdditionalPermissionProfile) -> bool:
    return bool(value.filesystem or value.network or value.allow_local_binding)


def _additional_permissions_safe(
    context: SecurityContext,
    value: AdditionalPermissionProfile,
    db_path: str | Path,
    *,
    mutation_targets: tuple[Path, ...] = (),
) -> bool:
    """Reject extra roots that would turn a protected subtree into a writable root."""
    protected = tuple(
        entry.root for entry in _protected_entries(context, db_path) if not entry.escalatable
    )
    if any(_paths_overlap(root, target) for root in protected for target in mutation_targets):
        return False
    for entry in value.filesystem:
        for root in protected:
            try:
                entry.root.relative_to(root)
            except ValueError:
                continue
            return False
    return True


def _is_under(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root)
    except ValueError:
        return False
    return True


def _paths_overlap(left: Path, right: Path) -> bool:
    return _is_under(left, right) or _is_under(right, left)


def _file_action_permissions(
    action: NormalizedAction,
    *,
    permission_root: Path | None = None,
) -> AdditionalPermissionProfile:
    access = (
        FilesystemAccess.READ
        if action.operation == "read"
        else FilesystemAccess.READ_WRITE
    )
    return AdditionalPermissionProfile(
        filesystem=(FilesystemEntry(permission_root or Path(action.path), access),)
    )


def _network_action_permissions(
    action: NormalizedAction,
    public_target: bool,
) -> AdditionalPermissionProfile:
    return AdditionalPermissionProfile(
        network=(
            NetworkEntry(
                host=action.host,
                port=action.port,
                protocol=action.protocol,
                allow_private=not public_target,
            ),
        )
    )


def _session_key(context: SecurityContext) -> tuple[str, str, str, str]:
    return (
        context.os_user,
        context.owner_account_id,
        context.workspace_id,
        context.session_id,
    )


def _rule_applies_to_tool(rule: ActionRule, tool_name: str) -> bool:
    """Keep Ace action rules scoped to the tool surface that displayed them.

    Historical deny rules remain global so an owner block cannot disappear after
    migration. Historical allow rules without a tool identity carry no authority.
    """
    if rule.tool_name:
        return rule.tool_name == str(tool_name).strip()
    return rule.decision is RuleDecision.DENY


def _rejection_key(
    context: SecurityContext,
    action: NormalizedAction,
) -> tuple[str, str, str, str]:
    return (
        context.owner_account_id,
        context.workspace_id,
        context.session_id,
        action.digest,
    )


def _bounded_mode_metadata(
    value: object,
    field: str,
    maximum: int,
    *,
    allow_empty: bool,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是字符串")
    normalized = value.strip()
    if "\x00" in normalized or len(normalized) > maximum or (not allow_empty and not normalized):
        raise ValueError(f"{field} 无效或超过 {maximum} 字符")
    return normalized


def _current_approval_ui_available() -> bool:
    from crew.core.runctx import current_push_fn

    return current_push_fn.get() is not None


def _approval_channel_for_action(action: NormalizedAction) -> ApprovalChannel:
    return {
        ActionKind.EXEC: ApprovalChannel.EXEC,
        ActionKind.FILE: ApprovalChannel.FILE,
        ActionKind.NETWORK: ApprovalChannel.NETWORK,
    }[action.kind]


def _permission_profile_hash(profile: PermissionProfile) -> str:
    payload = {
        "kind": profile.kind.value,
        "filesystem": [
            {
                "root": str(entry.root),
                "access": entry.access.value,
                "escalatable": entry.escalatable,
            }
            for entry in profile.filesystem
        ],
        "filesystem_globs": [
            {
                "root": str(entry.root),
                "pattern": entry.pattern,
                "access": entry.access.value,
            }
            for entry in profile.filesystem_globs
        ],
        "network": profile.network.value,
        "network_entries": [
            {
                "host": entry.host,
                "port": entry.port,
                "protocol": entry.protocol,
                "access": entry.access.value,
                "allow_private": entry.allow_private,
                "escalatable": entry.escalatable,
            }
            for entry in profile.network_entries
        ],
        "allow_local_binding": profile.allow_local_binding,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _context_for_action_request(request) -> SecurityContext:
    return SecurityContext(
        os_user=request.os_user,
        owner_account_id=request.owner_account_id,
        workspace_id=request.workspace_id,
        workspace_root=request.workspace_root,
        session_id=request.session_id,
        request_id=request.request_id,
        task_id=request.task_id,
        cwd=Path(request.action.cwd) if request.action.cwd else request.workspace_root,
    )


def _context_for_permission_request(request) -> SecurityContext:
    return SecurityContext(
        os_user=request.os_user,
        owner_account_id=request.owner_account_id,
        workspace_id=request.workspace_id,
        workspace_root=request.workspace_root,
        session_id=request.session_id,
        request_id=request.request_id,
        task_id=request.task_id,
        cwd=request.workspace_root,
    )
