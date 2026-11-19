"""Owner-authenticated security approval, rule, and audit endpoints."""

from __future__ import annotations

import asyncio
import os
import subprocess
from typing import Literal
import platform
import tempfile
from pathlib import Path
from collections.abc import Callable

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from crew.gateway.auth import account_from_request
from crew.gateway.instance_auth import verify_desktop_security_proof
from crew.security.actions import normalize_exec_action, normalize_file_action
from crew.security.audit import AuditEvent, format_action_for_audit
from crew.security.approvals import ApprovalDecision, ApprovalError, ApprovalSubmission
from crew.security.context import (
    SecurityContext,
    SecurityContextError,
    build_gateway_security_context,
    resolve_requested_path,
)
from crew.security.launch import shell_argv
from crew.security.models import ConversationPermissionMode
from crew.security.process_lifecycle import terminate_process_tree
from crew.security.runtime_client import NativeRuntimeClient, NativeRuntimeError, RuntimeCapabilities
from crew.security.workspace import prepare_new_workspace
from crew.state.logging import get_logger
from crew.tools.security_guard import _file_change_preview

log = get_logger("gateway.security")
_PROOF_HEADER = "X-Crew-Security-Proof"
_ACL_RECOVERY_TIMEOUT = 600
AuditActionType = Literal[
    "",
    "approval_requested",
    "approval_decision",
    "exec_decision",
    "file_decision",
    "network_decision",
    "exec_result",
    "rule_created",
    "rule_disabled",
    "rule_deleted",
    "audit_purged",
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
    "completed",
    "failed",
    "cancelled",
    "error",
    "enabled",
    "disabled",
    "deleted",
]
AuditSort = Literal["newest", "oldest"]
_AUDIT_FILE_OPERATIONS = {
    "file_read": "read",
    "file_write": "write",
    "file_delete": "delete",
    "patch": "patch",
}


class FakeExecutionInput(BaseModel):
    workspace_id: str = "default"
    session_id: str = Field(min_length=1)
    task_id: str = ""
    argv: list[str] = Field(min_length=1)
    cwd: str | None = None


class ApprovalIntentInput(BaseModel):
    workspace_id: str = "default"
    session_id: str = Field(min_length=1)
    task_id: str = ""
    decision: Literal["once", "session", "always", "reject"]


class DecisionInput(ApprovalIntentInput):
    nonce: str = Field(min_length=1)
    always_argv_prefix: list[str] | None = None
    submission_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class DecisionReceiptInput(ApprovalIntentInput):
    prefix_hash: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")
    submission_id: str = Field(pattern=r"^[a-f0-9]{32}$")


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
    network_enabled: bool = False,
    network_access: bool = False,
    on_error: Callable[[NativeRuntimeError], None] | None = None,
) -> RuntimeCapabilities | None:
    """Run a sandbox canary and return only capabilities backed by a live boundary."""

    with tempfile.TemporaryDirectory(prefix="ace-security-probe-") as parent_raw:
        parent = Path(parent_raw)
        workspace = parent / "workspace"
        workspace.mkdir()
        prepare_new_workspace(workspace)
        outside_readable = parent / "readable"
        outside_readable.mkdir()
        host_secret = outside_readable / "host-secret"
        host_secret.write_text("probe", encoding="ascii")
        outside_marker = parent / "outside-marker"
        marker = workspace / "probe-marker"
        metadata_root = workspace / ".git"
        metadata_root.mkdir()
        metadata_file = metadata_root / "config"
        metadata_sentinel = "ace-security-readonly-probe"
        metadata_file.write_text(metadata_sentinel, encoding="ascii")
        if system == "windows":
            command = (
                "cmd.exe",
                "/d",
                "/c",
                r"echo ok>probe-marker & echo changed>.git\config & "
                r"echo changed>..\outside-marker & type ..\readable\host-secret",
            )
        elif system in {"linux", "darwin"}:
            command = (
                "/bin/sh",
                "-c",
                'printf ok > "$1"; printf changed > "$2" 2>/dev/null || true; '
                'printf changed > "$3" 2>/dev/null || true; cat "$4"',
                "ace-probe",
                str(marker),
                str(metadata_file),
                str(outside_marker),
                str(host_secret),
            )
        else:
            return None
        # Grant exactly the fixture read on Windows. A live probe must not
        # touch the user's profile tree or require its ACL propagation.
        readable_roots = (outside_readable,) if system == "windows" else ()
        try:
            result = await NativeRuntimeClient(helper_argv).execute(
                command=command,
                cwd=workspace,
                writable_roots=(workspace,),
                readable_roots=readable_roots,
                readonly_roots=(metadata_root,),
                full_disk_read=True,
                network_enabled=network_enabled,
                network_access=network_access,
                timeout=10,
                max_output_bytes=4096,
            )
        except NativeRuntimeError as exc:
            log.warning("安全运行组件探针失败 [%s]：%s", exc.code.value, exc)
            if on_error is not None:
                on_error(exc)
            return None
        except (OSError, ValueError) as exc:
            log.warning("安全运行组件探针启动失败：%s", type(exc).__name__)
            return None
        # The marker proves the child actually started and could write inside the declared
        # workspace. A Seatbelt/bwrap startup failure also returns non-zero, but leaves no
        # marker; treating that as a passing denial would produce a false "sandbox ready" state.
        try:
            marker_ready = marker.is_file() and marker.read_text(encoding="ascii").startswith("ok")
            metadata_ready = metadata_file.read_text(encoding="ascii") == metadata_sentinel
            outside_write_denied = not outside_marker.exists()
        except (OSError, UnicodeError):
            marker_ready = False
            metadata_ready = False
            outside_write_denied = False
        return (
            result.capabilities
            if (
                marker_ready
                and metadata_ready
                and outside_write_denied
                and result.exit_code == 0
                and "probe" in result.stdout
                and result.capabilities.full_disk_read
            )
            else None
        )


async def _live_filesystem_runtime(
    *,
    network_access: bool = False,
    on_error: Callable[[NativeRuntimeError], None] | None = None,
) -> tuple[RuntimeCapabilities | None, bool, bool, str]:
    """Return the live filesystem boundary plus package diagnostics."""
    from crew.security.launch import packaged_runtime_argv, runtime_source_stale

    helper_argv = packaged_runtime_argv()
    helper_present = bool(helper_argv and Path(helper_argv[0]).is_file())
    stale = runtime_source_stale(helper_argv[0]) if helper_argv else False
    system = platform.system().lower()
    if not helper_present or stale or system not in {"windows", "linux", "darwin"}:
        return None, helper_present, stale, system
    return (
        await _probe_runtime(
            helper_argv, system=system, network_access=network_access, on_error=on_error,
        ),
        helper_present,
        stale,
        system,
    )


async def _recover_windows_acl(helper_argv: tuple[str, ...], state_dir: Path) -> str | None:
    """Finish a protected native ACL journal outside the short sandbox canary."""
    from crew.security.launch import HelperIntegrityError, verify_helper_integrity

    try:
        await asyncio.to_thread(verify_helper_integrity, helper_argv[0])
    except (HelperIntegrityError, OSError) as exc:
        log.warning("Windows sandbox ACL recovery skipped: invalid helper (%s)", type(exc).__name__)
        return "windows_acl_recovery_failed"
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            *helper_argv, "--windows-recover-acl", str(state_dir),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
        )
        code = await asyncio.wait_for(process.wait(), timeout=_ACL_RECOVERY_TIMEOUT)
    except asyncio.CancelledError:
        if process is not None:
            await terminate_process_tree(process)
        raise
    except (OSError, TimeoutError) as exc:
        if process is not None:
            await terminate_process_tree(process)
        log.warning("Windows sandbox ACL recovery failed (%s)", type(exc).__name__)
        return "windows_acl_recovery_failed"
    if code == 0:
        return None
    if code == 3:
        return "windows_sandbox_busy"
    log.warning("Windows sandbox ACL recovery failed (exit=%d)", code)
    return "windows_acl_recovery_failed"


class _WindowsAclRecovery:
    """Keep one recovery alive between capability requests from the desktop."""

    def __init__(self) -> None:
        self._task: asyncio.Task[str | None] | None = None

    def status(self, helper_argv: tuple[str, ...], state_dir: Path) -> str | None:
        if self._task is not None:
            if not self._task.done():
                return "windows_acl_recovery"
            finished = self._task
            self._task = None
            return finished.result() if not finished.cancelled() else "windows_acl_recovery_failed"
        # Presence only schedules the native check. The helper validates the
        # journal and takes the same lock as normal runs before touching ACLs.
        journal = state_dir / "windows-acl-state.json"
        cleanup_log = state_dir / "windows-acl-cleanup.log"
        try:
            needs_recovery = journal.exists() or journal.with_suffix(".bak").exists() or cleanup_log.exists()
        except OSError:
            return "windows_acl_recovery_failed"
        if not needs_recovery:
            return None
        self._task = asyncio.create_task(_recover_windows_acl(helper_argv, state_dir))
        return "windows_acl_recovery"


async def probe_security_capabilities(*, workspace_network_access: bool) -> dict[str, object]:
    from crew.security.launch import packaged_runtime_argv

    errors: list[NativeRuntimeError] = []
    filesystem_probe, helper_present, stale, system = await _live_filesystem_runtime(
        on_error=errors.append,
    )
    filesystem = filesystem_probe is not None
    network = False
    direct_network = False
    local_binding = False
    detail = "native security runtime 未随包安装"
    if helper_present and stale:
        detail = "native security runtime 与当前源码不一致"
    elif filesystem:
        helper_argv = packaged_runtime_argv()
        network_probe = await _probe_runtime(
            helper_argv, system=system, network_enabled=True, on_error=errors.append,
        )
        network = network_probe is not None and network_probe.managed_network
        local_binding = network and bool(network_probe and network_probe.local_binding_control)
        detail = (
            "文件沙箱与联网管控 live probe 已通过"
            if network
            else "文件沙箱 live probe 已通过；联网管控探针失败"
        )
        if workspace_network_access:
            direct_probe = await _probe_runtime(
                helper_argv, system=system, network_access=True, on_error=errors.append,
            )
            direct_network = direct_probe is not None and direct_probe.direct_network
            if direct_network:
                detail = "文件沙箱与直连模式 live probe 已通过"
            elif system == "windows":
                detail = "直连模式的 Windows 沙箱尚未就绪，请安装或修复防护"
            else:
                detail = "直连模式的原生文件沙箱探针失败"
    elif helper_present:
        detail = "原生安全运行组件已找到，文件沙箱自检未通过"
    ready = filesystem and (direct_network if workspace_network_access else network)
    setup_required = (
        not ready and helper_present and not stale and system == "windows"
        and any(_windows_sandbox_setup_required(error) for error in errors)
    )
    if setup_required:
        detail = "Windows 沙箱需要完成一次防护设置"
    return {
        "platform": system,
        "backend": filesystem_probe.backend if filesystem_probe is not None else None,
        "status": "ready" if ready else "setup_required" if setup_required else "unavailable",
        "setup_required": setup_required,
        "helper_present": helper_present,
        "filesystem_sandbox": filesystem,
        "managed_network": network,
        "workspace_network_access": workspace_network_access,
        "direct_network": direct_network,
        "local_binding_control": local_binding,
        "runtime_stale": stale,
        "detail": detail,
    }


def _windows_sandbox_setup_required(error: NativeRuntimeError) -> bool:
    """Recognize setup failures reported by installed Windows helper versions."""
    detail = str(error)
    return detail.startswith((
        "Windows sandbox identity is not installed:",
        "Windows sandbox identity is invalid:",
        "Windows sandbox identity version mismatch",
        "Windows direct network requires sandbox setup or repair",
        "Windows sandbox direct network identity is invalid",
        "FwpmFilterGetByKey0; run Windows sandbox setup or repair",
        "Windows sandbox firewall policy does not match the sandbox account",
    ))


def create_security_router(crew) -> APIRouter:
    """Create host-authority routes; authentication alone cannot approve actions."""
    router = APIRouter(prefix="/api/security")
    acl_recovery = _WindowsAclRecovery()
    active_probes: dict[bool, asyncio.Task[dict[str, object]]] = {}

    async def read_capabilities(network_access: bool) -> dict[str, object]:
        from crew.security.launch import packaged_runtime_argv, runtime_source_stale

        state_dir = Path(os.environ.get("ACE_SECURITY_STATE_DIR", ""))
        if platform.system() == "Windows" and state_dir.is_absolute():
            helper = packaged_runtime_argv()
            if helper and Path(helper[0]).is_file() and not runtime_source_stale(helper[0]):
                reason = acl_recovery.status(helper, state_dir)
                if reason:
                    return {
                        "platform": "windows",
                        "helper_present": True,
                        "runtime_stale": False,
                        "filesystem_sandbox": False,
                        "managed_network": False,
                        "workspace_network_access": network_access,
                        "direct_network": False,
                        "setup_required": False,
                        "status": "recovering" if reason == "windows_acl_recovery" else "unavailable",
                        "reason": reason,
                    }
        return await probe_security_capabilities(workspace_network_access=network_access)

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
        requested_mode = ConversationPermissionMode(payload.mode)
        if crew.security_service.mode_for(ctx) is requested_mode:
            return {"mode": requested_mode.value}
        if (
            requested_mode is ConversationPermissionMode.AUTO_REVIEW
            and crew.security_service.security_enabled
        ):
            runtime, _present, _stale, _system = await _live_filesystem_runtime(
                network_access=crew.security_service.workspace_network_access,
            )
            if runtime is None:
                raise HTTPException(
                    status_code=409,
                    detail="替我审批需要已通过 live probe 的原生文件沙箱",
                )
        changed = crew.security_service.set_mode(
            ctx,
            requested_mode,
        )
        effective_mode = crew.security_service.mode_for(ctx)
        if changed and crew.dispatcher.status(
            payload.session_id,
            owner_account_id=ctx.owner_account_id,
        ).get("live") != "idle":
            # 每个 turn 在入口捕获一次 ProcessLaunch。模式变化时终止活跃/排队 turn，
            # 否则后续工具仍会沿用旧的 managed/disabled 快照；idle 会话无需误触其它工作流。
            await crew.dispatcher.stop(
                payload.session_id,
                reason="安全模式已切换，请重新执行当前操作",
                owner_account_id=ctx.owner_account_id,
            )
        return {"mode": effective_mode.value}

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
                submission_id=payload.submission_id or "",
            )
        except ApprovalError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/requests/{request_id}/decision-receipt")
    async def decision_receipt(request_id: str, request: Request, payload: DecisionReceiptInput):
        await _require_desktop_proof(request)
        ctx = context(request, payload.workspace_id, payload.session_id, payload.task_id)
        submission = ApprovalSubmission(
            request_id, payload.submission_id, ApprovalDecision(payload.decision),
            payload.prefix_hash,
        )
        return crew.security_service.decision_receipt(ctx, submission)

    @router.get("/rules")
    async def list_rules(request: Request, workspace_id: str = Query("default")):
        from crew.security.models import serialize_additional_permissions

        await _require_desktop_proof(request)
        ctx = context(request, workspace_id, "rules-ui")
        rules = crew.security_rules.list_with_status(
            os_user=ctx.os_user,
            owner_account_id=ctx.owner_account_id,
            workspace_id=ctx.workspace_id,
        )
        return {
            "rules": [
                {
                    **rule.__dict__,
                    "additional_permissions": serialize_additional_permissions(
                        rule.additional_permissions
                    ),
                    "enabled": enabled,
                }
                for rule, enabled in rules
            ]
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
        network_access = (
            crew.security_service.security_enabled and crew.security_service.workspace_network_access
        )
        probe = active_probes.get(network_access)
        if probe is None or probe.done():
            probe = asyncio.create_task(read_capabilities(network_access))
            active_probes[network_access] = probe
        return await asyncio.shield(probe)

    return router
