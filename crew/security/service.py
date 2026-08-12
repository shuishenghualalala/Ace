"""Fail-closed coordination for approval, rules, audit, and the P1 fake runtime."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import threading
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from crew.security.actions import NormalizedAction
from crew.security.approvals import (
    ApprovalDecision,
    ApprovalError,
    ApprovalManager,
    ApprovalOutcome,
)
from crew.security.audit import AuditEvent, SQLiteSecurityAudit, format_action_for_audit
from crew.security.context import SecurityContext
from crew.security.file_policy import FilePolicyResult, assess_file_action
from crew.security.grants import GrantRegistry
from crew.security.models import (
    EMPTY_ADDITIONAL_PERMISSIONS,
    AdditionalPermissionProfile,
    ConversationPermissionMode,
    FilesystemAccess,
    FilesystemEntry,
    NetworkEntry,
    serialize_additional_permissions,
)
from crew.security.rule_store import SQLiteRuleStore
from crew.security.rules import RuleDecision, choose_rule

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
        self._futures: dict[str, asyncio.Future[ApprovalOutcome | None]] = {}
        # request_id -> (session_key, owner)，用于 logout / 模式切换时成批唤醒。
        self._meta: dict[str, tuple[tuple[str, ...], str]] = {}

    def register(
        self,
        request_id: str,
        *,
        session_key: tuple[str, ...],
        owner_account_id: str,
    ) -> None:
        """为新建的 pending 请求登记一个 future；复用请求不重复登记。"""
        if request_id in self._futures:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # 无事件循环（单元 registry 直测）→ 不阻塞，decide 仍可正常落地。
            return
        self._futures[request_id] = loop.create_future()
        self._meta[request_id] = (session_key, str(owner_account_id))

    async def wait(self, request_id: str) -> ApprovalOutcome | None:
        """阻塞至决策到达；请求不存在/超时/被撤销均返回 None（按拒绝处理）。"""
        future = self._futures.get(request_id)
        if future is None:
            return None
        try:
            return await asyncio.wait_for(future, timeout=_DECIDE_WAIT_TIMEOUT)
        except TimeoutError:
            return None
        finally:
            self._futures.pop(request_id, None)
            self._meta.pop(request_id, None)

    def resolve(self, request_id: str, outcome: ApprovalOutcome | None) -> None:
        """Resolve without removing; wait() is the single cleanup owner.

        Gateway may decide after register() but before the tool coroutine actually
        enters wait(). Removing here would make that later wait observe no future
        and treat a valid approval as rejection.
        """
        future = self._futures.get(request_id)
        if future is not None and not future.done():
            future.set_result(outcome)

    def cancel_for_session(self, session_key: tuple[str, ...]) -> int:
        """模式切换：把该精确会话上下文下的 pending 等待按 None 唤醒。"""
        return self._cancel(
            [rid for rid, (sk, _o) in self._meta.items() if sk == session_key]
        )

    def cancel_for_owned_session(self, owner_account_id: str, session_id: str) -> int:
        """真正会话结束：跨 workspace key 清理 owner/session 的等待方。"""
        owner = str(owner_account_id).strip()
        session = str(session_id).strip()
        return self._cancel(
            [
                rid
                for rid, (key, request_owner) in self._meta.items()
                if request_owner == owner and len(key) >= 4 and key[3] == session
            ]
        )

    def cancel_for_owner(self, owner_account_id: str) -> int:
        owner = str(owner_account_id).strip()
        return self._cancel([rid for rid, (_sk, o) in self._meta.items() if o == owner])

    def _cancel(self, request_ids: list[str]) -> int:
        for rid in request_ids:
            self.resolve(rid, None)
        return len(request_ids)


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
    ) -> None:
        self.approvals = approvals
        self.grants = grants
        self.rules = rules
        self.audit = audit
        self.db_path = Path(db_path)
        self._mode_lock = threading.Lock()
        # Serializes approval-decide (grant issue → durable audit → rollback) against
        # grant consumption in authorize_*. Without this, a concurrent authorize could
        # consume a once grant during the audit window, making the fail-closed rollback
        # a no-op (H-4). RLock so decide's internal audit/rule calls remain re-entrant.
        self._decision_lock = threading.RLock()
        # 阻塞等待中的工具调用；decide/撤销时唤醒。把"审批请求"与"工具执行"重新接通，
        # 否则工具只能抛 ToolError 让模型复述，污染正文且 turn 结束后无人恢复。
        self._waiters = _ApprovalWaiter()
        self._session_modes: dict[tuple[str, str, str, str], ConversationPermissionMode] = {}
        self._recent_rejections: dict[tuple[str, str, str, str], float] = {}

    def set_mode(self, context: SecurityContext, mode: ConversationPermissionMode) -> bool:
        """Set one conversation mode without revoking prior SESSION grants.

        Returns whether the mode changed.  A change invalidates only pending
        decisions from the old policy; the Gateway uses the return value to stop
        the current turn so its captured ProcessLaunch cannot outlive the switch.
        """
        key = _session_key(context)
        with self._decision_lock:
            with self._mode_lock:
                previous = self._session_modes.get(
                    key,
                    ConversationPermissionMode.REQUEST_APPROVAL,
                )
                if previous is mode:
                    return False
                self._session_modes[key] = mode
            # pending 属于旧模式，必须按拒绝唤醒；显式 SESSION grant 属于对话，不在此撤销。
            # Held under _decision_lock so a concurrent decide() cannot publish a grant
            # between the mode flip and the pending sweep (mode-switch race, H-6).
            self.approvals.revoke_pending_session(context)
            self._waiters.cancel_for_session(key)
            return True

    def end_session(self, owner_account_id: str, session_id: str) -> int:
        """Revoke transient authority when an authenticated session truly ends."""
        owner = str(owner_account_id).strip()
        session = str(session_id).strip()
        if not owner or not session:
            return 0
        with self._decision_lock:
            with self._mode_lock:
                mode_keys = [
                    key
                    for key in self._session_modes
                    if key[1] == owner and key[3] == session
                ]
                for key in mode_keys:
                    self._session_modes.pop(key, None)
            self._recent_rejections = {
                key: expiry
                for key, expiry in self._recent_rejections.items()
                if not (key[0] == owner and key[2] == session)
            }
            self._waiters.cancel_for_owned_session(owner, session)
            return len(mode_keys) + self.approvals.end_owned_session(owner, session)

    def mode_for(self, context: SecurityContext) -> ConversationPermissionMode:
        with self._mode_lock:
            return self._session_modes.get(
                _session_key(context),
                ConversationPermissionMode.REQUEST_APPROVAL,
            )

    def revoke_owner(self, owner_account_id: str) -> int:
        """Drop owner-scoped in-memory modes, pending approvals, and transient grants."""
        owner = str(owner_account_id).strip()
        with self._decision_lock:
            with self._mode_lock:
                mode_keys = [key for key in self._session_modes if key[1] == owner]
                for key in mode_keys:
                    self._session_modes.pop(key, None)
            self._recent_rejections = {
                key: expiry
                for key, expiry in self._recent_rejections.items()
                if key[0] != owner
            }
            self._waiters.cancel_for_owner(owner)
            return len(mode_keys) + self.approvals.revoke_owner(owner)

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
    ) -> dict:
        """Create or reuse one exact pending request without performing the action."""
        request, created = self.approvals.create_or_get(
            context,
            action,
            tool_name,
            risk_class=risk_class,
            preview=preview,
            additional_permissions=additional_permissions,
        )
        # Register even for a reused request. The prior caller may have timed out or
        # been cancelled while the approval itself remains pending; a new caller must
        # still be attachable and resumable instead of receiving an immediate None.
        self._waiters.register(
            request.request_id,
            session_key=_session_key(context),
            owner_account_id=context.owner_account_id,
        )
        if not created:
            return _public_request(request, include_nonce=True)
        event_context = replace(context, request_id=request.request_id)
        self.audit.record(
            AuditEvent.for_action(
                event_context,
                action,
                action_type="approval_requested",
                decision="pending",
                decision_source="gateway",
                approval_mode=self.mode_for(context).value,
                tool_name=tool_name,
                additional_permissions_summary=_permissions_summary(additional_permissions),
            )
        )
        public = _public_request(request, include_nonce=True)
        self._push_pending_approval(context, public)
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
        mode = self.mode_for(context)
        assessment = assess_file_action(context, action, mode, db_path=self.db_path)
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
            rules = self.rules.list(
                os_user=context.os_user,
                owner_account_id=context.owner_account_id,
                workspace_id=context.workspace_id,
            )
            # Persisted DENY is explicit owner policy and must short-circuit base
            # allow, FULL_ACCESS, allow rules, and transient grants.
            if any(rule.decision is RuleDecision.DENY and rule.matches(action) for rule in rules):
                self._audit_file(context, action, "deny", "always_deny_rule", tool_name)
                return FilePolicyResult.DENY, "persistent_deny_rule", None
            asks_for_workspace_write = (
                mode is ConversationPermissionMode.REQUEST_APPROVAL
                and action.operation != "read"
            )
            if assessment.result is FilePolicyResult.ALLOW and not asks_for_workspace_write:
                self._audit_file(context, action, "allow", "base_profile", tool_name)
                return assessment.result, assessment.reason, None

            selected = choose_rule(rules, action)
            if selected is not None and selected.decision is RuleDecision.ALLOW:
                self._audit_file(context, action, "allow", "always_rule", tool_name)
                return FilePolicyResult.ALLOW, "always_rule", None
            requested_permissions = _file_action_permissions(action)
            grant = self.grants.authorize_action(
                context,
                action,
                additional_permissions=requested_permissions,
            )
            if grant is not None:
                self._audit_file(context, action, "allow", "runtime_grant", tool_name)
                return FilePolicyResult.ALLOW, "runtime_grant", None

        # Reaching this point means the base profile did not cover the requested
        # write or the path is protected; the approval carries that exact scope.
        request = self.request_action(
            context,
            action,
            tool_name=tool_name,
            risk_class=(
                "workspace_file_write"
                if asks_for_workspace_write
                else "external_file_write" if action.operation != "read" else "external_file_read"
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
        additional_permissions: AdditionalPermissionProfile = EMPTY_ADDITIONAL_PERMISSIONS,
        preview: str = "",
    ) -> ExecAuthorization:
        """Authorize one exact command according to the selected conversation mode."""
        mode = self.mode_for(context)
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
            rules = self.rules.list(
                os_user=context.os_user,
                owner_account_id=context.owner_account_id,
                workspace_id=context.workspace_id,
            )
            if any(rule.decision is RuleDecision.DENY and rule.matches(action) for rule in rules):
                self._audit_exec(context, action, "deny", "always_deny_rule", tool_name)
                return ExecAuthorization(False)
            if mode is ConversationPermissionMode.FULL_ACCESS:
                self._audit_exec(context, action, "allow", "full_access", tool_name)
                return ExecAuthorization(True)
            selected = choose_rule(
                (
                    rule
                    for rule in rules
                    if rule.decision is RuleDecision.ALLOW
                    and rule.additional_permissions == additional_permissions
                ),
                action,
            )
            if (
                selected is not None
                and selected.decision is RuleDecision.ALLOW
                and selected.additional_permissions == additional_permissions
            ):
                self._audit_exec(context, action, "allow", "always_rule", tool_name)
                return ExecAuthorization(True, additional_permissions=selected.additional_permissions)
            grant = self.grants.authorize_action(
                context,
                action,
                additional_permissions=additional_permissions,
            )
            if grant is not None:
                self._audit_exec(context, action, "allow", "runtime_grant", tool_name)
                return ExecAuthorization(True, additional_permissions=grant.additional_permissions)
        if (
            mode is ConversationPermissionMode.AUTO_REVIEW
            and not requires_approval
            and additional_permissions.empty
        ):
            self._audit_exec(context, action, "allow", "auto_review", tool_name)
            return ExecAuthorization(True)
        request = self.request_action(
            context,
            action,
            tool_name=tool_name,
            risk_class=risk_class,
            additional_permissions=additional_permissions,
            preview=preview,
        )
        self._audit_exec(context, action, "ask", "approval_required", tool_name)
        return ExecAuthorization(False, request)

    def authorize_user_initiated_exec_action(
        self,
        context: SecurityContext,
        action: NormalizedAction,
        *,
        tool_name: str,
    ) -> ExecAuthorization:
        """Authorize an exact command already initiated by an authenticated UI gesture."""
        with self._decision_lock:
            rules = self.rules.list(
                os_user=context.os_user,
                owner_account_id=context.owner_account_id,
                workspace_id=context.workspace_id,
            )
            if any(rule.decision is RuleDecision.DENY and rule.matches(action) for rule in rules):
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
        public_target: bool,
    ) -> ExecAuthorization:
        """Authorize one exact outbound target for an in-process network tool."""
        mode = self.mode_for(context)
        if (
            mode is not ConversationPermissionMode.FULL_ACCESS
            and self._rejection_cooldown_active(context, action)
        ):
            self._audit_network(context, action, "deny", "recent_user_rejection", tool_name)
            return ExecAuthorization(False)
        with self._decision_lock:
            rules = self.rules.list(
                os_user=context.os_user,
                owner_account_id=context.owner_account_id,
                workspace_id=context.workspace_id,
            )
            if any(rule.decision is RuleDecision.DENY and rule.matches(action) for rule in rules):
                self._audit_network(context, action, "deny", "always_deny_rule", tool_name)
                return ExecAuthorization(False)
            selected = choose_rule(
                (rule for rule in rules if rule.decision is RuleDecision.ALLOW),
                action,
            )
            if selected is not None:
                self._audit_network(context, action, "allow", "always_rule", tool_name)
                return ExecAuthorization(True)
            requested_permissions = _network_action_permissions(action, public_target)
            grant = self.grants.authorize_action(
                context,
                action,
                additional_permissions=requested_permissions,
            )
            if grant is not None:
                self._audit_network(context, action, "allow", "runtime_grant", tool_name)
                return ExecAuthorization(True)
        if mode is ConversationPermissionMode.FULL_ACCESS:
            self._audit_network(context, action, "allow", "full_access", tool_name)
            return ExecAuthorization(True)
        if public_target and mode in {
            ConversationPermissionMode.AUTO_REVIEW,
        }:
            self._audit_network(context, action, "allow", "public_auto_review", tool_name)
            return ExecAuthorization(True)
        request = self.request_action(
            context,
            action,
            tool_name=tool_name,
            risk_class="public_network" if public_target else "private_network",
            additional_permissions=requested_permissions,
        )
        self._audit_network(context, action, "ask", "approval_required", tool_name)
        return ExecAuthorization(False, request)

    def _audit_file(
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
                action_type="file_decision",
                decision=decision,
                decision_source=source,
                approval_mode=self.mode_for(context).value,
                tool_name=tool_name,
            )
        )

    def _audit_exec(
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
                action_type="exec_decision",
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
        ]

    async def await_decision(self, request_id: str) -> ApprovalOutcome | None:
        """阻塞发起审批请求的工具，直到 owner 下达决策。

        返回 ``ApprovalOutcome`` 表示已决策（批准则继续、拒绝则由调用方回灌干净错误）；
        返回 ``None`` 表示超时、被撤销或请求失效，一律按拒绝处理（fail-closed）。
        这是把"审批请求"与"工具执行"重新接通的关键：工具不再抛 ToolError 让模型
        复述，而是挂起等待，决策到达后自然恢复 agent 循环。
        """
        return await self._waiters.wait(request_id)

    def decide(
        self,
        context: SecurityContext,
        *,
        request_id: str,
        nonce: str,
        decision: ApprovalDecision,
        always_argv_prefix: Sequence[str] | None = None,
    ) -> dict:
        """Apply one decision under the decision lock.

        Holding the lock from grant issue through the durable audit (and any rollback)
        guarantees no concurrent ``authorize_*`` can consume the grant before its audit
        commits — closing the H-4 fail-closed rollback race. Fake-execution requests are
        diagnostic only and may never create reusable production authority.
        """
        pending = self.approvals.get_pending(request_id, context)
        if (
            pending is not None
            and pending.tool_name == "security_fake_exec"
            and decision in {ApprovalDecision.SESSION, ApprovalDecision.ALWAYS}
        ):
            raise ApprovalError("fake execution 只允许 once 或 reject")
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
                        else outcome.grant.scope.value if outcome.grant else ""
                    ),
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

    @staticmethod
    def _push_pending_approval(context: SecurityContext, request: dict) -> None:
        """Wake the renderer immediately; polling remains the disconnect fallback."""
        from crew.core.runctx import current_push_fn

        push = current_push_fn.get()
        if push is None:
            return
        payload = {
            "kind": "security_approval",
            "session_id": context.session_id,
            "request_id": request["request_id"],
        }
        try:
            result = push(context.session_id, payload)
        except Exception:
            log.exception("安全审批主动推送失败")
            return
        if inspect.isawaitable(result):

            async def finish_push() -> None:
                try:
                    await result
                except Exception:
                    log.exception("安全审批主动推送失败")

            asyncio.create_task(finish_push())

    def set_rule_enabled(self, context: SecurityContext, rule_id: str, enabled: bool) -> bool:
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
        "workspace_id": request.workspace_id,
        "session_id": request.session_id,
        "task_id": request.task_id,
        "risk_class": request.risk_class,
        "expires_in_seconds": max(0, int(request.expires_monotonic - request.created_monotonic)),
        "additional_permissions": serialize_additional_permissions(
            request.additional_permissions
        ),
    }
    if request.preview:
        payload["preview"] = request.preview
    if include_nonce:
        payload["nonce"] = request.nonce
    return payload


def _permissions_summary(profile: AdditionalPermissionProfile) -> str:
    if profile.empty:
        return ""
    return json.dumps(
        serialize_additional_permissions(profile),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _file_action_permissions(action: NormalizedAction) -> AdditionalPermissionProfile:
    access = (
        FilesystemAccess.READ
        if action.operation == "read"
        else FilesystemAccess.READ_WRITE
    )
    return AdditionalPermissionProfile(
        filesystem=(FilesystemEntry(Path(action.path), access),)
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
