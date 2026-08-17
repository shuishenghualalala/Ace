"""Owner-authenticated security approval, rule, and audit endpoints."""

from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import time
from typing import Literal
import os
import platform
import tempfile
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from crew.gateway.auth import (
    AuthenticationError,
    account_from_request,
    require_admin,
)
from crew.gateway.helpers import safe_public_error
from crew.security.actions import normalize_exec_action, normalize_file_action
from crew.security.audit import AuditEvent, format_action_for_audit
from crew.security.alerts import SecurityAlertActionDenied
from crew.security.approvals import ApprovalDecision, ApprovalError
from crew.security.context import (
    SecurityContext,
    SecurityContextError,
    build_gateway_security_context,
    resolve_requested_path,
)
from crew.security.launch import shell_argv
from crew.security.local_path import LocalPathReference
from crew.security.models import ConversationPermissionMode
from crew.security.policy import deserialize_additional_permissions
from crew.security.runtime_client import (
    NativeRuntimeClient,
    NativeRuntimeError,
    RuntimeCapabilities,
)
from crew.security.settings import strict_security_enabled
from crew.tools.security_guard import _file_change_preview

_LOGGER = logging.getLogger(__name__)

AuditActionType = Literal[
    "",
    "approval_requested",
    "approval_decision",
    "permission_requested",
    "permission_decision",
    "exec_decision",
    "file_decision",
]
AuditDecision = Literal[
    "",
    "allow",
    "deny",
    "pending",
    "ask",
    "once",
    "session",
    "always",
    "reject",
]
AuditSort = Literal["newest", "oldest"]
_AUDIT_FILE_OPERATIONS = {
    "file_read": "read",
    "file_write": "write",
    "patch": "patch",
}


class _StrictSecurityInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class FakeExecutionInput(_StrictSecurityInput):
    workspace_id: str = Field(default="default", min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    task_id: str = Field(min_length=1, max_length=200)
    argv: list[str] = Field(min_length=1, max_length=256)
    cwd: str | None = Field(default=None, max_length=32_768)

    @field_validator("argv")
    @classmethod
    def _valid_argv(cls, value: list[str]) -> list[str]:
        if any(not item or "\x00" in item or len(item.encode("utf-8")) > 16_384 for item in value):
            raise ValueError("argv contains an invalid token")
        return value


class DecisionInput(_StrictSecurityInput):
    workspace_id: str = Field(default="default", min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    task_id: str = Field(min_length=1, max_length=200)
    nonce: str = Field(min_length=1, max_length=200)
    decision: Literal["once", "session", "always", "reject"]
    always_argv_prefix: list[str] | None = Field(default=None, min_length=1, max_length=256)
    permissions: dict | None = None

    @field_validator("always_argv_prefix")
    @classmethod
    def _valid_prefix(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and any(
            not item or "\x00" in item or len(item.encode("utf-8")) > 16_384 for item in value
        ):
            raise ValueError("always_argv_prefix contains an invalid token")
        return value

    @field_validator("permissions")
    @classmethod
    def _bounded_permissions(cls, value: dict | None) -> dict | None:
        if (
            value is not None
            and len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
            > 64 * 1024
        ):
            raise ValueError("permissions exceeds the size limit")
        return value


class AlertReportInput(_StrictSecurityInput):
    kind: Literal[
        "anomalous_denials",
        "sandbox_fallback",
        "manifest_mismatch",
        "orphan_process",
        "update_signature_failure",
        "audit_chain_break",
    ]
    detail: str = Field(default="", max_length=512)
    session_id: str = Field(default="", max_length=200)
    task_id: str = Field(default="", max_length=200)


class RuleMutationInput(_StrictSecurityInput):
    workspace_id: str = Field(default="default", min_length=1, max_length=200)
    enabled: bool | None = None


class SecurityModeInput(_StrictSecurityInput):
    workspace_id: str = Field(default="default", min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    mode: Literal["read_only", "request_approval", "auto_review", "full_access"]
    confirmation_nonce: str | None = Field(default=None, min_length=1, max_length=256)


def _history_action_index(messages, context: SecurityContext) -> dict[str, tuple[str, str]]:
    """Index safely displayable historical actions by their exact security digest."""
    actions: dict[str, tuple[str, str]] = {}
    for message in messages:
        for tool_call in message.tool_calls:
            args = tool_call.arguments
            try:
                if tool_call.name == "terminal":
                    command = str(args.get("command") or "").strip()
                    if not command or context.cwd is None:
                        continue
                    action = normalize_exec_action(
                        shell_argv(command),
                        context.cwd,
                        raw_command=command,
                    )
                elif tool_call.name in _AUDIT_FILE_OPERATIONS:
                    operation = _AUDIT_FILE_OPERATIONS[tool_call.name]
                    raw_path = args.get("path")
                    if raw_path is None or raw_path == "":
                        raw_path = "."
                    target = resolve_requested_path(
                        context,
                        LocalPathReference.parse(raw_path),
                    )
                    content_digest, _preview = _file_change_preview(args, operation)
                    action = normalize_file_action(
                        target,
                        operation,
                        offset=max(0, int(args.get("offset") or 0)),
                        limit=max(0, int(args.get("limit") or args.get("head_limit") or 0)),
                        content_digest=content_digest,
                    )
                else:
                    continue
            except (OSError, TypeError, ValueError):
                continue
            actions[action.digest] = format_action_for_audit(action)
    return actions


async def _require_desktop_proof(request: Request) -> None:
    """Require the one-time proof already consumed by Gateway middleware."""

    if not bool(getattr(request.state, "gateway_instance_authenticated", False)):
        raise HTTPException(status_code=403, detail="security desktop proof required")


async def _probe_runtime(
    helper_argv: tuple[str, ...],
    *,
    system: str,
    network_enabled: bool,
) -> RuntimeCapabilities | None:
    """Run a sandbox canary and return only capabilities backed by a live boundary."""

    with tempfile.TemporaryDirectory(prefix="ace-security-probe-") as parent_raw:
        parent = Path(parent_raw)
        workspace = parent / "workspace"
        workspace.mkdir()
        host_secret = parent / "host-secret"
        host_secret.write_text("probe", encoding="ascii")
        marker = workspace / "probe-marker"
        if system == "windows":
            # Keep both operands relative to the sandbox cwd.  Absolute quoted
            # paths are misparsed by cmd.exe after the Windows runner's argv
            # marshalling, while this form still proves write-inside/read-outside.
            command = (
                "cmd.exe",
                "/d",
                "/c",
                "echo ok>probe-marker & type ..\\host-secret",
            )
        elif system == "linux":
            command = (
                "/bin/sh",
                "-c",
                'printf ok > "$1"; cat "$2" >/dev/null',
                "ace-probe",
                str(marker),
                str(host_secret),
            )
        elif system == "darwin":
            command = (
                "/bin/sh",
                "-c",
                'printf ok > "$1"; cat "$2" >/dev/null',
                "ace-probe",
                str(marker),
                str(host_secret),
            )
        else:
            return None
        try:
            result = await NativeRuntimeClient(helper_argv).execute(
                command=command,
                cwd=workspace,
                writable_roots=(workspace,),
                network_enabled=network_enabled,
                timeout=10,
                max_output_bytes=4096,
            )
        except (NativeRuntimeError, OSError, ValueError):
            return None
        # The marker proves the child actually started and could write inside the declared
        # workspace. A Seatbelt/bwrap startup failure also returns non-zero, but leaves no
        # marker; treating that as a passing denial would produce a false "sandbox ready" state.
        # The sandbox account owns the marker and its lease may intentionally
        # leave the host without read access.  Directory visibility is enough:
        # only the sandboxed ``echo`` could have created this fixed name.
        try:
            marker_ready = marker.is_file()
        except OSError:
            marker_ready = False
        return result.capabilities if marker_ready and result.exit_code != 0 else None


async def _live_filesystem_runtime() -> tuple[RuntimeCapabilities | None, bool, bool, str]:
    """Return the live filesystem boundary plus package diagnostics."""
    from crew.security.launch import packaged_runtime_argv, runtime_source_stale

    helper_argv = packaged_runtime_argv()
    helper_present = Path(helper_argv[0]).is_file()
    stale = runtime_source_stale(helper_argv[0])
    system = platform.system().lower()
    if not helper_present or stale or system not in {"windows", "linux", "darwin"}:
        return None, helper_present, stale, system
    return (
        await _probe_runtime(helper_argv, system=system, network_enabled=False),
        helper_present,
        stale,
        system,
    )


def _state_dir_configured(system: str) -> bool:
    return system != "windows" or bool(os.environ.get("ACE_SECURITY_STATE_DIR", "").strip())


def create_security_router(crew) -> APIRouter:
    """Create host-authority routes; authentication alone cannot approve actions."""
    router = APIRouter(prefix="/api/security")
    full_access_challenges: dict[tuple[str, str, str], tuple[str, float]] = {}
    full_access_challenge_ttl = 60.0

    def issue_full_access_challenge(owner: str, workspace_id: str, session_id: str) -> str:
        now = time.monotonic()
        for key, (_nonce, expires_at) in list(full_access_challenges.items()):
            if expires_at <= now:
                full_access_challenges.pop(key, None)
        nonce = secrets.token_urlsafe(32)
        full_access_challenges[(owner, workspace_id, session_id)] = (
            nonce,
            now + full_access_challenge_ttl,
        )
        return nonce

    def consume_full_access_challenge(
        owner: str,
        workspace_id: str,
        session_id: str,
        presented: str | None,
    ) -> bool:
        record = full_access_challenges.pop((owner, workspace_id, session_id), None)
        if record is None or record[1] <= time.monotonic() or not presented:
            return False
        return hmac.compare_digest(record[0], presented)

    def context(request: Request, workspace_id: str, session_id: str, task_id: str = "", cwd=None):
        account = account_from_request(request)
        try:
            return build_gateway_security_context(
                crew.workspace_store,
                owner_account_id=account.owner_account_id,
                workspace_id=workspace_id,
                session_id=session_id,
                task_id=task_id,
                cwd=cwd,
            )
        except SecurityContextError as exc:
            raise HTTPException(
                status_code=400,
                detail=safe_public_error(exc, "安全上下文无效"),
            ) from exc

    @router.post("/fake-executions")
    async def create_fake_execution(request: Request, payload: FakeExecutionInput):
        await _require_desktop_proof(request)
        ctx = context(
            request, payload.workspace_id, payload.session_id, payload.task_id, payload.cwd
        )
        action = normalize_exec_action(payload.argv, ctx.cwd or ctx.workspace_root or ".")
        return crew.security_service.request_fake_execution(ctx, action)

    @router.get("/full-access-challenge")
    async def full_access_challenge(
        request: Request,
        workspace_id: str = Query("default", min_length=1, max_length=200),
        session_id: str = Query(..., min_length=1, max_length=200),
    ):
        await _require_desktop_proof(request)
        ctx = context(request, workspace_id, session_id)
        return {
            "nonce": issue_full_access_challenge(
                ctx.owner_account_id,
                ctx.workspace_id,
                ctx.session_id,
            ),
            "expires_in": int(full_access_challenge_ttl),
        }

    @router.put("/mode")
    async def set_mode(request: Request, payload: SecurityModeInput):
        await _require_desktop_proof(request)
        ctx = context(request, payload.workspace_id, payload.session_id)
        if payload.mode == ConversationPermissionMode.FULL_ACCESS.value and not consume_full_access_challenge(
            ctx.owner_account_id,
            ctx.workspace_id,
            ctx.session_id,
            payload.confirmation_nonce,
        ):
            raise HTTPException(
                status_code=409,
                detail="完全访问需要新的服务端二次确认 nonce",
            )
        if (
            payload.mode == ConversationPermissionMode.AUTO_REVIEW.value
            and strict_security_enabled()
        ):
            system = platform.system().lower()
            if not _state_dir_configured(system):
                raise HTTPException(
                    status_code=409,
                    detail="替我审批的 live probe 无法启动：当前 Gateway 未加载安全状态目录，请重启 Crew 后再试",
                )
            runtime, _present, _stale, _system = await _live_filesystem_runtime()
            if runtime is None:
                raise HTTPException(
                    status_code=409,
                    detail="替我审批需要已通过 live probe 的原生文件沙箱",
                )
        changed = crew.security_service.set_mode(
            ctx,
            ConversationPermissionMode(payload.mode),
            source=(
                "desktop_native_confirmation"
                if payload.mode == ConversationPermissionMode.FULL_ACCESS.value
                else "gateway_owner"
            ),
        )
        if (
            changed
            and crew.dispatcher.status(
                payload.session_id,
                owner_account_id=ctx.owner_account_id,
            ).get("live")
            != "idle"
        ):
            # 每个 turn 在入口捕获一次 ProcessLaunch。模式变化时终止活跃/排队 turn，
            # 否则后续工具仍会沿用旧的 managed/disabled 快照；idle 会话无需误触其它工作流。
            crew.dispatcher.stop(
                payload.session_id,
                reason="安全模式已切换，请重新执行当前操作",
                owner_account_id=ctx.owner_account_id,
            )
        if changed:
            # Mode revocation must reach already-started session resources too:
            # cancelling the model turn alone leaves background processes or
            # task-scoped MCP workers alive under the old authority.
            try:
                revoke_runtime_tools = getattr(
                    getattr(crew, "registry", None),
                    "revoke_runtime_tool_session",
                    None,
                )
                if callable(revoke_runtime_tools):
                    await revoke_runtime_tools(ctx.owner_account_id, ctx.session_id)
                from crew.tools.process_registry import process_registry

                await asyncio.to_thread(
                    process_registry.revoke_session,
                    ctx.owner_account_id,
                    ctx.session_id,
                    reason="SECURITY_MODE_CHANGED",
                )
            except Exception as exc:  # noqa: BLE001 - mode revocation fails closed
                raise HTTPException(
                    status_code=409,
                    detail="安全模式已切换，但会话运行资源回收未完成；请重试或重启 Gateway",
                ) from exc
        return {"mode": payload.mode}

    @router.get("/pending")
    async def pending(
        request: Request,
        workspace_id: str = Query("default"),
        session_id: str = Query(..., min_length=1),
        task_id: str = Query(""),
    ):
        await _require_desktop_proof(request)
        ctx = context(request, workspace_id, session_id, task_id)
        return {"requests": crew.security_service.pending(ctx, include_nonce=True)}

    @router.post("/requests/{request_id}/decision")
    async def decide(request_id: str, request: Request, payload: DecisionInput):
        await _require_desktop_proof(request)
        ctx = context(request, payload.workspace_id, payload.session_id, payload.task_id)
        try:
            pending_permission = next(
                (
                    item
                    for item in crew.security_service.pending_permissions(ctx)
                    if item.get("request_id") == request_id
                ),
                None,
            )
            if pending_permission is not None:
                granted_permissions = deserialize_additional_permissions(
                    payload.permissions
                    if payload.permissions is not None
                    else pending_permission.get("permissions")
                )
                return crew.security_service.decide_permissions(
                    ctx,
                    request_id=request_id,
                    nonce=payload.nonce,
                    decision=ApprovalDecision(payload.decision),
                    granted_permissions=granted_permissions,
                )
            return crew.security_service.decide(
                ctx,
                request_id=request_id,
                nonce=payload.nonce,
                decision=ApprovalDecision(payload.decision),
                always_argv_prefix=payload.always_argv_prefix,
            )
        except ApprovalError as exc:
            raise HTTPException(
                status_code=409,
                detail=safe_public_error(exc, "审批请求无效"),
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=safe_public_error(exc, "审批参数无效"),
            ) from exc

    @router.get("/rules")
    async def list_rules(request: Request, workspace_id: str = Query("default")):
        await _require_desktop_proof(request)
        ctx = context(request, workspace_id, "rules-ui")
        rules = crew.security_rules.list_with_status(
            os_user=ctx.os_user,
            owner_account_id=ctx.owner_account_id,
            workspace_id=ctx.workspace_id,
        )
        return {"rules": [{**rule.__dict__, "enabled": enabled} for rule, enabled in rules]}

    @router.patch("/rules/{rule_id}")
    async def mutate_rule(rule_id: str, request: Request, payload: RuleMutationInput):
        await _require_desktop_proof(request)
        if payload.enabled is None:
            raise HTTPException(status_code=400, detail="enabled is required")
        ctx = context(request, payload.workspace_id, "rules-ui")
        changed = crew.security_service.set_rule_enabled(ctx, rule_id, payload.enabled)
        return {"changed": changed}

    @router.delete("/rules/{rule_id}")
    async def delete_rule(
        rule_id: str,
        request: Request,
        workspace_id: str = Query("default"),
    ):
        await _require_desktop_proof(request)
        ctx = context(request, workspace_id, "rules-ui")
        changed = crew.security_service.delete_rule(ctx, rule_id)
        return {"changed": changed}

    @router.get("/audit")
    async def audit(
        request: Request,
        limit: int = Query(100, ge=1, le=100),
        offset: int = Query(0, ge=0, le=1_000_000),
        action_type: AuditActionType = Query(""),
        decision: AuditDecision = Query(""),
        session_id: str = Query("", max_length=160),
        workspace_id: str = Query("", max_length=200),
        task_id: str = Query("", max_length=200),
        start_time: float | None = Query(None),
        end_time: float | None = Query(None),
        sort: AuditSort = Query("newest"),
    ):
        await _require_desktop_proof(request)
        account = account_from_request(request)
        owner = account.owner_account_id
        records, total = crew.security_audit.query_page(
            owner_account_id=owner,
            limit=limit,
            offset=offset,
            action_type=action_type,
            decision=decision,
            session_id=session_id,
            workspace_id=workspace_id,
            task_id=task_id,
            start_time=start_time,
            end_time=end_time,
            sort=sort,
        )
        sessions = {
            str(item.get("session_id") or ""): item
            for item in crew.session_store.list_sessions(
                owner_account_id=owner,
                include_archived=True,
                exclude_channel_sessions=False,
            )
        }
        workspaces = {
            str(item.get("id") or ""): item
            for item in crew.workspace_store.list(owner_account_id=owner)
        }
        context_cache: dict[tuple[str, str], SecurityContext | None] = {}
        history_cache: dict[tuple[str, str], dict[str, tuple[str, str]]] = {}
        events = []
        for record in records:
            event = dict(record.__dict__)
            event_session_id = str(event.get("session_id") or "")
            event_workspace_id = str(event.get("workspace_id") or "default")
            session = sessions.get(event_session_id)
            if session is None and "::" in event_session_id:
                session = sessions.get(event_session_id.split("::", 1)[0])
            workspace = workspaces.get(event_workspace_id)
            cache_key = (event_session_id, event_workspace_id)
            if cache_key not in context_cache:
                try:
                    context_cache[cache_key] = build_gateway_security_context(
                        crew.workspace_store,
                        owner_account_id=owner,
                        workspace_id=event_workspace_id,
                        session_id=event_session_id,
                        task_id=str(event.get("task_id") or ""),
                        request_id=str(event.get("request_id") or ""),
                    )
                except SecurityContextError:
                    context_cache[cache_key] = None
            event_context = context_cache[cache_key]
            if not event.get("action_detail") and event_context is not None:
                if cache_key not in history_cache:
                    history_cache[cache_key] = _history_action_index(
                        crew.session_store.load(
                            event_session_id,
                            owner_account_id=owner,
                        ),
                        event_context,
                    )
                recovered = history_cache[cache_key].get(
                    str(event.get("normalized_action_hash") or "")
                )
                if recovered is not None:
                    event["action_summary"], event["action_detail"] = recovered
            event.update(
                session_title=str((session or {}).get("title") or ""),
                workspace_name=str((workspace or {}).get("name") or ""),
                workspace_root=str((workspace or {}).get("root_path") or ""),
                current_approval_mode=(
                    crew.security_service.mode_for(event_context).value
                    if event_context is not None
                    else ""
                ),
            )
            events.append(event)
        return {"events": events, "total": total}

    @router.get("/audit/export")
    async def export_audit(request: Request):
        await _require_desktop_proof(request)
        account = account_from_request(request)
        return {
            "jsonl": crew.security_audit.export_jsonl(owner_account_id=account.owner_account_id)
        }

    @router.post("/audit/purge-expired")
    async def purge_expired_audit(
        request: Request,
        workspace_id: str = Query("default", max_length=200),
    ):
        await _require_desktop_proof(request)
        ctx = context(request, workspace_id, "audit-ui")
        deleted = crew.security_audit.purge_expired(
            owner_account_id=ctx.owner_account_id,
            workspace_id=ctx.workspace_id,
        )
        crew.security_audit.record(
            AuditEvent.for_rule(
                ctx,
                rule_id="retention-30-days",
                action_type="audit_purged",
                decision=f"deleted:{deleted}",
            )
        )
        return {"deleted": deleted}

    @router.post("/alerts/report")
    async def report_alert(request: Request, payload: AlertReportInput):
        await _require_desktop_proof(request)
        account = account_from_request(request)
        registry = getattr(crew, "security_alerts", None)
        if registry is None:
            raise HTTPException(status_code=409, detail="安全告警服务不可用")
        try:
            alert = registry.report(
                payload.kind,
                account.owner_account_id,
                session_id=payload.session_id,
                task_id=payload.task_id,
                detail=payload.detail,
            )
        except (SecurityAlertActionDenied, ValueError) as exc:
            raise HTTPException(
                status_code=409,
                detail=safe_public_error(exc, "安全告警报告无效"),
            ) from exc
        return {
            "created": alert is not None,
            "alert": alert.public_dict() if alert is not None else None,
        }

    @router.get("/alerts")
    async def list_alerts(request: Request):
        await _require_desktop_proof(request)
        account = account_from_request(request)
        try:
            require_admin(account, crew.config)
            is_admin = True
        except AuthenticationError:
            is_admin = False
        registry = getattr(crew, "security_alerts", None)
        alerts = (
            registry.snapshot("" if is_admin else account.owner_account_id)
            if registry is not None
            else []
        )
        return {
            "admin": is_admin,
            "alerts": [alert.public_dict() for alert in alerts],
        }

    @router.post("/alerts/{alert_id}/isolate")
    async def isolate_alert(alert_id: str, request: Request):
        await _require_desktop_proof(request)
        account = account_from_request(request)
        registry = getattr(crew, "security_alerts", None)
        if registry is None:
            raise HTTPException(status_code=409, detail="安全告警服务不可用")
        alert = registry.get(alert_id)
        if alert is None or alert.resolved:
            raise HTTPException(status_code=404, detail="安全告警不存在或已解决")
        try:
            require_admin(account, crew.config)
        except AuthenticationError:
            if alert.owner_account_id != account.owner_account_id:
                raise HTTPException(status_code=403, detail="无权操作其他账号的告警")
        try:
            changed = registry.isolate(alert_id, require_ui=True)
        except SecurityAlertActionDenied as exc:
            raise HTTPException(status_code=409, detail=safe_public_error(exc, "告警操作不可用")) from exc
        return {"changed": changed}

    @router.post("/alerts/{alert_id}/revoke")
    async def revoke_alert(alert_id: str, request: Request):
        await _require_desktop_proof(request)
        account = account_from_request(request)
        registry = getattr(crew, "security_alerts", None)
        if registry is None:
            raise HTTPException(status_code=409, detail="安全告警服务不可用")
        alert = registry.get(alert_id)
        if alert is None or alert.resolved:
            raise HTTPException(status_code=404, detail="安全告警不存在或已解决")
        try:
            require_admin(account, crew.config)
        except AuthenticationError:
            if alert.owner_account_id != account.owner_account_id:
                raise HTTPException(status_code=403, detail="无权操作其他账号的告警")
        try:
            changed = registry.revoke(alert_id, require_ui=True)
        except SecurityAlertActionDenied as exc:
            raise HTTPException(status_code=409, detail=safe_public_error(exc, "告警操作不可用")) from exc
        return {"changed": changed}

    @router.post("/alerts/{alert_id}/resolve")
    async def resolve_alert(alert_id: str, request: Request):
        await _require_desktop_proof(request)
        account = account_from_request(request)
        registry = getattr(crew, "security_alerts", None)
        if registry is None:
            raise HTTPException(status_code=409, detail="安全告警服务不可用")
        alert = registry.get(alert_id)
        if alert is None:
            raise HTTPException(status_code=404, detail="安全告警不存在")
        try:
            require_admin(account, crew.config)
        except AuthenticationError:
            if alert.owner_account_id != account.owner_account_id:
                raise HTTPException(status_code=403, detail="无权操作其他账号的告警")
        return {"changed": registry.resolve(alert_id)}

    @router.get("/capabilities")
    async def capabilities(request: Request):
        await _require_desktop_proof(request)
        account_from_request(request)
        from crew.security.launch import packaged_runtime_argv

        filesystem_probe, helper_present, stale, system = await _live_filesystem_runtime()
        helper_argv = packaged_runtime_argv()
        state_dir_configured = _state_dir_configured(system)
        detail = "native security runtime 未随包安装"
        filesystem = filesystem_probe is not None
        network = False
        network_probe: RuntimeCapabilities | None = None
        if helper_present and stale:
            detail = "native security runtime 与当前源码不一致"
        elif filesystem_probe is not None:
            if filesystem:
                network_probe = await _probe_runtime(
                    helper_argv,
                    system=system,
                    network_enabled=True,
                )
                network = network_probe is not None and network_probe.managed_network
            detail = (
                "文件沙箱与联网管控 live probe 已通过"
                if network
                else (
                    "文件沙箱 live probe 已通过；联网管控探针失败"
                    if filesystem
                    else "原生防护 live probe 失败或主机读取隔离未生效"
                )
            )
        elif helper_present:
            detail = "当前设备不支持可用的原生安全运行组件"
        if helper_present and not state_dir_configured:
            detail = "当前 Gateway 未加载安全状态目录，请重启 Crew 后再试"
        try:
            probe_context = context(request, "default", "runtime-diagnostics")
            crew.security_audit.record(
                AuditEvent.for_runtime_diagnostic(
                    probe_context,
                    status="probe_ok" if filesystem else "probe_failed",
                    component="security-runtime-probe",
                    backend=system,
                    capabilities=tuple(
                        name
                        for name, enabled in (
                            ("filesystem_sandbox", filesystem),
                            ("managed_network", network),
                            (
                                "local_binding_control",
                                bool(
                                    network
                                    and network_probe
                                    and network_probe.local_binding_control
                                ),
                            ),
                        )
                        if enabled
                    ),
                    failure_code=(
                        ""
                        if filesystem
                        else ("runtime_stale" if stale else "probe_failed")
                    ),
                )
            )
        except Exception:  # noqa: BLE001 - probe diagnostics must not break the UI
            _LOGGER.exception("runtime probe diagnostic audit write failed")
        return {
            "platform": system,
            "helper_present": helper_present,
            "filesystem_sandbox": filesystem,
            "managed_network": network,
            "local_binding_control": bool(
                network and network_probe and network_probe.local_binding_control
            )
            if filesystem
            else False,
            "runtime_stale": bool(stale),
            "state_dir_configured": state_dir_configured,
            "detail": detail,
        }

    return router
