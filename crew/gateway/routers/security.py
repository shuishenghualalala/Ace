"""Owner-authenticated security approval, rule, and audit endpoints."""

from __future__ import annotations

from typing import Literal
import os
import platform
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from crew.gateway.auth import account_from_request
from crew.gateway.instance_auth import verify_desktop_security_proof
from crew.security.actions import normalize_exec_action, normalize_file_action
from crew.security.audit import AuditEvent, format_action_for_audit
from crew.security.approvals import ApprovalDecision, ApprovalError
from crew.security.context import (
    SecurityContext,
    SecurityContextError,
    build_gateway_security_context,
    resolve_requested_path,
)
from crew.security.launch import shell_argv
from crew.security.models import ConversationPermissionMode
from crew.security.runtime_client import NativeRuntimeClient, NativeRuntimeError, RuntimeCapabilities
from crew.security.settings import strict_security_enabled
from crew.tools.security_guard import _file_change_preview

_PROOF_HEADER = "X-Crew-Security-Proof"
AuditActionType = Literal[
    "",
    "approval_requested",
    "approval_decision",
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


class FakeExecutionInput(BaseModel):
    workspace_id: str = "default"
    session_id: str = Field(min_length=1)
    task_id: str = ""
    argv: list[str] = Field(min_length=1)
    cwd: str | None = None


class DecisionInput(BaseModel):
    workspace_id: str = "default"
    session_id: str = Field(min_length=1)
    task_id: str = ""
    nonce: str = Field(min_length=1)
    decision: Literal["once", "session", "always", "reject"]
    always_argv_prefix: list[str] | None = None


class RuleMutationInput(BaseModel):
    workspace_id: str = "default"
    enabled: bool | None = None


class SecurityModeInput(BaseModel):
    workspace_id: str = "default"
    session_id: str = Field(min_length=1)
    mode: Literal["request_approval", "auto_review", "full_access"]


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
                    target = resolve_requested_path(context, str(args.get("path") or "."))
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
    body = await request.body()
    if not verify_desktop_security_proof(
        request.headers.get(_PROOF_HEADER, ""),
        method=request.method,
        path=request.url.path,
        body=body,
    ):
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
            command = (
                "cmd.exe",
                "/d",
                "/c",
                f'echo ok>"{marker}" & type "{host_secret}"',
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
        try:
            marker_ready = marker.is_file() and marker.read_text(encoding="ascii").startswith("ok")
        except (OSError, UnicodeError):
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
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/fake-executions")
    async def create_fake_execution(request: Request, payload: FakeExecutionInput):
        await _require_desktop_proof(request)
        ctx = context(request, payload.workspace_id, payload.session_id, payload.task_id, payload.cwd)
        action = normalize_exec_action(payload.argv, ctx.cwd or ctx.workspace_root or ".")
        return crew.security_service.request_fake_execution(ctx, action)

    @router.put("/mode")
    async def set_mode(request: Request, payload: SecurityModeInput):
        await _require_desktop_proof(request)
        ctx = context(request, payload.workspace_id, payload.session_id)
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
        )
        if changed and crew.dispatcher.status(
            payload.session_id,
            owner_account_id=ctx.owner_account_id,
        ).get("live") != "idle":
            # 每个 turn 在入口捕获一次 ProcessLaunch。模式变化时终止活跃/排队 turn，
            # 否则后续工具仍会沿用旧的 managed/disabled 快照；idle 会话无需误触其它工作流。
            crew.dispatcher.stop(
                payload.session_id,
                reason="安全模式已切换，请重新执行当前操作",
                owner_account_id=ctx.owner_account_id,
            )
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
            return crew.security_service.decide(
                ctx,
                request_id=request_id,
                nonce=payload.nonce,
                decision=ApprovalDecision(payload.decision),
                always_argv_prefix=payload.always_argv_prefix,
            )
        except ApprovalError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/rules")
    async def list_rules(request: Request, workspace_id: str = Query("default")):
        await _require_desktop_proof(request)
        ctx = context(request, workspace_id, "rules-ui")
        rules = crew.security_rules.list_with_status(
            os_user=ctx.os_user,
            owner_account_id=ctx.owner_account_id,
            workspace_id=ctx.workspace_id,
        )
        return {
            "rules": [{**rule.__dict__, "enabled": enabled} for rule, enabled in rules]
        }

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
        offset: int = Query(0, ge=0),
        action_type: AuditActionType = Query(""),
        decision: AuditDecision = Query(""),
        session_id: str = Query("", max_length=160),
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
        return {"jsonl": crew.security_audit.export_jsonl(owner_account_id=account.owner_account_id)}

    @router.post("/audit/purge-expired")
    async def purge_expired_audit(request: Request, workspace_id: str = Query("default")):
        await _require_desktop_proof(request)
        ctx = context(request, workspace_id, "audit-ui")
        deleted = crew.security_audit.purge_expired(owner_account_id=ctx.owner_account_id)
        crew.security_audit.record(
            AuditEvent.for_rule(
                ctx,
                rule_id="retention-30-days",
                action_type="audit_purged",
                decision=f"deleted:{deleted}",
            )
        )
        return {"deleted": deleted}

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
