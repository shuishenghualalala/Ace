"""安全命令：审批模式、待审批决策、规则、审计与原生能力探针。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from crew.cli.app import CliContext, CliError, CliResult
from crew.security.approvals import ApprovalDecision, ApprovalError
from crew.security.context import SecurityContextError, build_gateway_security_context
from crew.security.models import ConversationPermissionMode, serialize_additional_permissions
from crew.security.settings import strict_security_enabled


def register(subparsers, handlers: dict[str, Any]) -> None:
    parser = subparsers.add_parser("security", help="安全审批、规则与审计")
    cmds = parser.add_subparsers(dest="security_cmd")

    mode = cmds.add_parser("mode", help="会话审批模式")
    mode_cmds = mode.add_subparsers(dest="security_mode_cmd")
    get_mode = mode_cmds.add_parser("get")
    _add_session_args(get_mode)
    get_mode.set_defaults(handler=_security_mode_get)
    set_mode = mode_cmds.add_parser("set")
    _add_session_args(set_mode)
    set_mode.add_argument(
        "--mode",
        required=True,
        choices=("request_approval", "auto_review", "full_access"),
    )
    set_mode.set_defaults(handler=_security_mode_set)

    pending = cmds.add_parser("pending", help="列出待审批请求")
    _add_session_args(pending)
    pending.set_defaults(handler=_security_pending)

    decide = cmds.add_parser("decide", help="审批/拒绝一个请求")
    _add_session_args(decide)
    decide.add_argument("--request-id", required=True)
    decide.add_argument("--nonce", required=True)
    decide.add_argument("--decision", required=True, choices=("once", "session", "always", "reject"))
    decide.add_argument("--always-argv-prefix", default="", help="逗号分隔的 argv 前缀")
    decide.set_defaults(handler=_security_decide)

    rules = cmds.add_parser("rules", help="审批规则")
    rules_cmds = rules.add_subparsers(dest="security_rules_cmd")
    rules_list = rules_cmds.add_parser("list")
    rules_list.add_argument("--workspace-id", default="default")
    rules_list.set_defaults(handler=_security_rules_list)
    for action in ("enable", "disable", "delete"):
        sub = rules_cmds.add_parser(action)
        sub.add_argument("--id", dest="rule_id", required=True)
        sub.add_argument("--workspace-id", default="default")
        sub.set_defaults(handler=_security_rule_action(action))

    audit = cmds.add_parser("audit", help="安全审计")
    audit_cmds = audit.add_subparsers(dest="security_audit_cmd")
    audit_list = audit_cmds.add_parser("list")
    audit_list.add_argument("--limit", type=int, default=100)
    audit_list.add_argument("--offset", type=int, default=0)
    audit_list.add_argument("--action-type", default="")
    audit_list.add_argument("--decision", default="")
    audit_list.add_argument("--session-id", default="")
    audit_list.add_argument("--sort", choices=("newest", "oldest"), default="newest")
    audit_list.set_defaults(handler=_security_audit_list)
    audit_cmds.add_parser("export").set_defaults(handler=_security_audit_export)
    purge = audit_cmds.add_parser("purge-expired")
    purge.add_argument("--workspace-id", default="default")
    purge.set_defaults(handler=_security_audit_purge)

    caps = cmds.add_parser("capabilities", help="原生安全能力探针")
    caps.set_defaults(handler=_security_capabilities)

    fake = cmds.add_parser("fake-executions", help="制造一次 exec 审批请求（测试用）")
    fake.add_argument("--session-id", required=True)
    fake.add_argument("--argv", nargs="+", required=True)
    fake.add_argument("--workspace-id", default="default")
    fake.add_argument("--task-id", default="")
    fake.add_argument("--cwd", default="")
    fake.add_argument(
        "--decision",
        choices=("once", "session", "always", "reject"),
        default="",
        help="同一进程内立即决策（跨 CLI 进程的审批状态不共享，测试用）",
    )
    fake.set_defaults(handler=_security_fake_execution)

    check = cmds.add_parser("check-terminal", help="终端命令安全判定")
    check.add_argument("--command", required=True)
    check.set_defaults(handler=_security_check_terminal)

    check_file = cmds.add_parser("check-file", help="文件操作安全判定")
    check_file.add_argument("--path", required=True)
    check_file.add_argument("--operation", required=True, choices=("read", "write", "delete", "patch"))
    check_file.add_argument("--offset", type=int, default=0)
    check_file.add_argument("--limit", type=int, default=0)
    check_file.add_argument("--content-digest", default="")
    check_file.add_argument("--session-id", default="check-file")
    check_file.set_defaults(handler=_security_check_file)

    check_network = cmds.add_parser("check-network", help="网络目标安全判定")
    check_network.add_argument("--url", required=True)
    check_network.add_argument("--session-id", default="check-network")
    check_network.set_defaults(handler=_security_check_network)

    fake_file = cmds.add_parser("fake-file-actions", help="制造一次文件审批请求（测试用）")
    fake_file.add_argument("--path", required=True)
    fake_file.add_argument("--operation", required=True, choices=("read", "write", "delete", "patch"))
    fake_file.add_argument("--session-id", required=True)
    fake_file.add_argument(
        "--decision",
        choices=("once", "session", "always", "reject"),
        default="",
    )
    fake_file.set_defaults(handler=_security_fake_file_action)

    fake_network = cmds.add_parser("fake-network-actions", help="制造一次网络审批请求（测试用）")
    fake_network.add_argument("--host", required=True)
    fake_network.add_argument("--port", type=int, required=True)
    fake_network.add_argument("--protocol", required=True, choices=("http", "https", "tcp", "udp", "socks5_tcp", "socks5_udp"))
    fake_network.add_argument("--session-id", required=True)
    fake_network.add_argument(
        "--decision",
        choices=("once", "session", "always", "reject"),
        default="",
    )
    fake_network.set_defaults(handler=_security_fake_network_action)

    sandbox = cmds.add_parser("sandbox-run", help="通过原生沙箱执行命令（测试边界）")
    sandbox.add_argument("--argv", nargs="+", required=True)
    sandbox.add_argument("--cwd", default="")
    sandbox.add_argument("--writable-root", action="append", default=[])
    sandbox.add_argument("--readable-root", action="append", default=[])
    sandbox.add_argument("--readonly-root", action="append", default=[])
    sandbox.add_argument("--full-disk-read", action="store_true")
    sandbox.add_argument("--network", action="store_true")
    sandbox.add_argument("--timeout", type=float, default=30.0)
    sandbox.add_argument("--max-output-bytes", type=int, default=2 * 1024 * 1024)
    sandbox.set_defaults(handler=_security_sandbox_run)


def _add_session_args(parser) -> None:
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--workspace-id", default="default")
    parser.add_argument("--task-id", default="")


def _security_context(app: Any, ctx: CliContext, session_id: str, task_id: str = "") -> Any:
    try:
        return build_gateway_security_context(
            app.workspace_store,
            owner_account_id=ctx.owner,
            workspace_id=ctx.workspace_id,
            session_id=session_id,
            task_id=task_id,
        )
    except SecurityContextError as exc:
        raise CliError(str(exc)) from exc


def _security_mode_get(args: Any, ctx: CliContext) -> CliResult:
    app = ctx.app
    sec_ctx = _security_context(app, ctx, args.session_id, args.task_id)
    mode = app.security_service.mode_for(sec_ctx).value
    return CliResult(data={"mode": mode, "session_id": args.session_id}, text=mode)


def _security_mode_set(args: Any, ctx: CliContext) -> CliResult:
    app = ctx.app
    sec_ctx = _security_context(app, ctx, args.session_id, args.task_id)
    requested = ConversationPermissionMode(args.mode)
    if requested == ConversationPermissionMode.AUTO_REVIEW and strict_security_enabled():
        from crew.gateway.routers.security import _live_filesystem_runtime

        runtime, _present, _stale, _system = _live_filesystem_runtime()
        if runtime is None:
            raise CliError("auto_review 需要已通过 live probe 的原生文件沙箱", exit_code=409)
    changed = app.security_service.set_mode(sec_ctx, requested)
    effective = app.security_service.mode_for(sec_ctx).value
    if changed and app.dispatcher.status(args.session_id, owner_account_id=ctx.owner).get("live") != "idle":
        app.dispatcher.stop(
            args.session_id,
            reason="安全模式已切换，请重新执行当前操作",
            owner_account_id=ctx.owner,
        )
    return CliResult(data={"mode": effective, "changed": changed}, text=effective)


def _security_pending(args: Any, ctx: CliContext) -> CliResult:
    app = ctx.app
    sec_ctx = _security_context(app, ctx, args.session_id, args.task_id)
    requests = app.security_service.pending(sec_ctx, include_nonce=True)
    text = "\n".join(
        f"{item.get('request_id')}  {item.get('action_summary', '')}  nonce={item.get('nonce', '')}"
        for item in requests
    )
    return CliResult(data={"requests": requests}, text=text or "(无待审批请求)")


def _security_decide(args: Any, ctx: CliContext) -> CliResult:
    app = ctx.app
    sec_ctx = _security_context(app, ctx, args.session_id, args.task_id)
    always_argv_prefix = (
        [item.strip() for item in args.always_argv_prefix.split(",") if item.strip()]
        if args.always_argv_prefix
        else None
    )
    try:
        result = app.security_service.decide(
            sec_ctx,
            request_id=args.request_id,
            nonce=args.nonce,
            decision=ApprovalDecision(args.decision),
            always_argv_prefix=always_argv_prefix,
        )
    except ApprovalError as exc:
        raise CliError(str(exc), exit_code=409) from exc
    return CliResult(data=result, text=f"已处理 {args.request_id} -> {args.decision}")


def _security_rules_list(args: Any, ctx: CliContext) -> CliResult:
    app = ctx.app
    sec_ctx = _security_context(app, ctx, "rules-ui")
    rules = app.security_rules.list_with_status(
        os_user=sec_ctx.os_user,
        owner_account_id=sec_ctx.owner_account_id,
        workspace_id=sec_ctx.workspace_id,
    )
    items = [
        {
            **rule.__dict__,
            "additional_permissions": serialize_additional_permissions(rule.additional_permissions),
            "enabled": enabled,
        }
        for rule, enabled in rules
    ]
    text = "\n".join(
        f"{item.get('rule_id')}  {item.get('action_summary', '')}  enabled={item.get('enabled')}"
        for item in items
    )
    return CliResult(data={"rules": items}, text=text or "(无规则)")


def _security_rule_action(action: str):
    async def _handler(args: Any, ctx: CliContext) -> CliResult:
        app = ctx.app
        sec_ctx = _security_context(app, ctx, "rules-ui")
        if action == "delete":
            changed = app.security_service.delete_rule(sec_ctx, args.rule_id)
        else:
            changed = app.security_service.set_rule_enabled(sec_ctx, args.rule_id, action == "enable")
        if not changed:
            raise CliError(f"规则不存在: {args.rule_id}", exit_code=404)
        return CliResult(data={"ok": True, "rule_id": args.rule_id, "action": action}, text=f"规则已{action}")

    return _handler


def _security_audit_list(args: Any, ctx: CliContext) -> CliResult:
    records, total = ctx.app.security_audit.query_page(
        owner_account_id=ctx.owner,
        limit=args.limit,
        offset=args.offset,
        action_type=args.action_type,
        decision=args.decision,
        session_id=args.session_id,
        sort=args.sort,
    )
    events = [dict(record.__dict__) for record in records]
    text = "\n".join(
        f"{event.get('id', '')}  {event.get('action_type', '')}  {event.get('decision', '')}  "
        f"{event.get('action_summary', '')}"
        for event in events
    )
    return CliResult(data={"events": events, "total": total}, text=text or "(无审计记录)")


def _security_audit_export(args: Any, ctx: CliContext) -> CliResult:
    return CliResult(data={"jsonl": ctx.app.security_audit.export_jsonl(owner_account_id=ctx.owner)})


def _security_audit_purge(args: Any, ctx: CliContext) -> CliResult:
    from crew.security.audit import AuditEvent

    app = ctx.app
    sec_ctx = _security_context(app, ctx, "audit-ui")
    deleted = app.security_audit.purge_expired(owner_account_id=sec_ctx.owner_account_id)
    app.security_audit.record(
        AuditEvent.for_rule(
            sec_ctx,
            rule_id="retention-30-days",
            action_type="audit_purged",
            decision=f"deleted:{deleted}",
        )
    )
    return CliResult(data={"deleted": deleted}, text=f"已清理 {deleted} 条过期审计")


async def _security_capabilities(args: Any, ctx: CliContext) -> CliResult:
    import platform

    from crew.gateway.routers.security import _live_filesystem_runtime, _probe_runtime
    from crew.security.launch import packaged_runtime_argv

    filesystem_probe, helper_present, stale, system = await _live_filesystem_runtime()
    helper_argv = packaged_runtime_argv()
    detail = "native security runtime 未随包安装"
    filesystem = filesystem_probe is not None
    network = False
    if helper_present and stale:
        detail = "native security runtime 与当前源码不一致"
    elif filesystem_probe is not None:
        if filesystem:
            network_probe = await _probe_runtime(helper_argv, system=system, network_enabled=True)
            network = network_probe is not None and network_probe.managed_network
            detail = (
                "文件沙箱与联网管控 live probe 已通过"
                if network
                else (
                    "文件沙箱 live probe 已通过；联网管控探针失败"
                    if filesystem
                    else "原生防护 live probe 失败或只读基线未生效"
                )
            )
        elif helper_present:
            detail = "当前设备不支持可用的原生安全运行组件"
    return CliResult(
        data={
            "platform": platform.system().lower(),
            "helper_present": helper_present,
            "stale": stale,
            "filesystem": filesystem,
            "network": network,
            "detail": detail,
        }
    )


def _security_fake_execution(args: Any, ctx: CliContext) -> CliResult:
    from crew.security.actions import normalize_exec_action

    app = ctx.app
    sec_ctx = _security_context(app, ctx, args.session_id, args.task_id)
    # 审批请求要求非空 task_id；CLI 操作员命令用固定标识兜底。
    sec_ctx = replace(sec_ctx, task_id=sec_ctx.task_id or "cli-approval")
    action = normalize_exec_action(
        args.argv,
        sec_ctx.cwd or sec_ctx.workspace_root or ".",
    )
    # CLI 本身就是审批面：无渲染器连接时也允许创建待审批请求（操作员随后 decide）。
    with app.security_service.operator_approval_surface():
        result = app.security_service.request_fake_execution(sec_ctx, action)
    if not args.decision:
        return CliResult(data=result, text=f"已创建审批请求 {result.get('request_id')}")
    decision = app.security_service.decide(
        sec_ctx,
        request_id=result["request_id"],
        nonce=result["nonce"],
        decision=ApprovalDecision(args.decision),
    )
    return CliResult(
        data={"request": result, "decision": decision},
        text=f"已创建并{args.decision}请求 {result.get('request_id')}",
    )


def _security_check_terminal(args: Any, ctx: CliContext) -> CliResult:
    from crew.tools.terminal_guard import (
        classify_command,
        detect_dangerous_command,
        detect_hardline_command,
    )

    hardline, hardline_reason = detect_hardline_command(args.command)
    if hardline:
        return CliResult(
            data={
                "verdict": "blocked",
                "stage": "hardline",
                "reason": hardline_reason,
                "command": args.command,
            },
            text=f"blocked (hardline): {hardline_reason}",
        )
    dangerous, dangerous_reason = detect_dangerous_command(args.command)
    if dangerous:
        return CliResult(
            data={
                "verdict": "ask",
                "stage": "dangerous",
                "reason": dangerous_reason,
                "command": args.command,
            },
            text=f"ask (dangerous): {dangerous_reason}",
        )
    verdict, reason = classify_command(args.command)
    return CliResult(
        data={
            "verdict": "ask" if verdict == "ask" else "allow",
            "stage": "classify",
            "classify_verdict": verdict,
            "reason": reason,
            "command": args.command,
        },
        text=f"{verdict}: {reason}",
    )


def _security_check_file(args: Any, ctx: CliContext) -> CliResult:
    from crew.security.actions import normalize_file_action

    app = ctx.app
    sec_ctx = replace(
        _security_context(app, ctx, args.session_id),
        task_id="cli-approval",
    )
    action = normalize_file_action(
        args.path,
        args.operation,
        offset=args.offset,
        limit=args.limit,
        content_digest=args.content_digest,
    )
    # 干跑查询：require_approval 时也要能创建请求并返回给操作员（CLI 即审批面），
    # 而不是因无渲染器直接 deny。
    with app.security_service.operator_approval_surface():
        result, reason, request = app.security_service.authorize_file_action(
            sec_ctx,
            action,
            tool_name="security_check",
        )
    return CliResult(
        data={"result": result.value, "reason": reason, "request": request},
        text=f"{result.value}: {reason}",
    )


def _security_check_network(args: Any, ctx: CliContext) -> CliResult:
    from crew.security.actions import normalize_network_action
    from crew.security.file_policy import FilePolicyResult
    from crew.security.outbound import parse_public_http_target

    app = ctx.app
    try:
        target = parse_public_http_target(args.url)
    except ValueError as exc:
        raise CliError(f"联网目标无效: {exc}") from exc
    sec_ctx = replace(
        _security_context(app, ctx, args.session_id),
        task_id="cli-approval",
    )
    action = normalize_network_action(target.host, target.port, target.protocol)
    # 干跑查询同 check-file：require_approval 也返回请求给操作员，不因无渲染器 deny。
    with app.security_service.operator_approval_surface():
        decision, reason, request = app.security_service.authorize_network_action(
            sec_ctx,
            action,
            tool_name="security_check",
            public_target=True,
        )
    return CliResult(
        data={
            "allowed": decision is FilePolicyResult.ALLOW,
            "reason": reason,
            "request": request,
            "target": str(target),
        },
        text="allow" if decision is FilePolicyResult.ALLOW else "ask/deny",
    )


def _request_fake_and_maybe_decide(
    app: Any,
    sec_ctx: Any,
    action: Any,
    *,
    tool_name: str,
    risk_class: str,
    decision: str,
) -> dict[str, Any]:
    # 审批请求要求非空 task_id（绑定审批到任务）；CLI 操作员命令没有任务上下文，
    # 用固定标识兜底。CLI 本身就是审批面：无渲染器连接时也允许创建待审批请求。
    sec_ctx = replace(sec_ctx, task_id=sec_ctx.task_id or "cli-approval")
    with app.security_service.operator_approval_surface():
        result = app.security_service.request_action(
            sec_ctx,
            action,
            tool_name=tool_name,
            risk_class=risk_class,
        )
    if not decision:
        return {"request": result}
    decision_result = app.security_service.decide(
        sec_ctx,
        request_id=result["request_id"],
        nonce=result["nonce"],
        decision=ApprovalDecision(decision),
    )
    return {"request": result, "decision": decision_result}


def _security_fake_file_action(args: Any, ctx: CliContext) -> CliResult:
    from crew.security.actions import normalize_file_action

    app = ctx.app
    sec_ctx = _security_context(app, ctx, args.session_id)
    action = normalize_file_action(args.path, args.operation)
    data = _request_fake_and_maybe_decide(
        app,
        sec_ctx,
        action,
        tool_name="security_fake_file",
        risk_class="fake_file",
        decision=args.decision,
    )
    return CliResult(
        data=data,
        text=f"已创建文件审批请求 {data['request'].get('request_id')}",
    )


def _security_fake_network_action(args: Any, ctx: CliContext) -> CliResult:
    from crew.security.actions import normalize_network_action

    app = ctx.app
    sec_ctx = _security_context(app, ctx, args.session_id)
    action = normalize_network_action(args.host, args.port, args.protocol)
    data = _request_fake_and_maybe_decide(
        app,
        sec_ctx,
        action,
        tool_name="security_fake_network",
        risk_class="fake_network",
        decision=args.decision,
    )
    return CliResult(
        data=data,
        text=f"已创建网络审批请求 {data['request'].get('request_id')}",
    )


async def _security_sandbox_run(args: Any, ctx: CliContext) -> CliResult:
    from dataclasses import asdict
    from pathlib import Path

    from crew.security.launch import packaged_runtime_argv, runtime_source_stale
    from crew.security.runtime_client import NativeRuntimeClient, NativeRuntimeError

    helper_argv = packaged_runtime_argv()
    if not Path(helper_argv[0]).is_file():
        raise CliError("native security runtime 未随包安装", exit_code=503)
    if runtime_source_stale(helper_argv[0]):
        raise CliError("native security runtime 与当前源码不一致，请重新构建", exit_code=503)
    cwd = Path(args.cwd or ".").expanduser().resolve()
    writable_roots = (
        [Path(item).expanduser().resolve() for item in args.writable_root]
        if args.writable_root
        else [cwd]
    )
    readable_roots = [Path(item).expanduser().resolve() for item in args.readable_root]
    readonly_roots = [Path(item).expanduser().resolve() for item in args.readonly_root]
    try:
        result = await NativeRuntimeClient(helper_argv).execute(
            command=args.argv,
            cwd=cwd,
            writable_roots=writable_roots,
            readable_roots=readable_roots,
            readonly_roots=readonly_roots,
            full_disk_read=args.full_disk_read,
            network_enabled=args.network,
            timeout=args.timeout,
            max_output_bytes=args.max_output_bytes,
        )
    except (NativeRuntimeError, OSError, ValueError) as exc:
        raise CliError(str(exc)) from exc
    return CliResult(
        data={
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "capabilities": asdict(result.capabilities),
        },
        text=f"exit={result.exit_code}",
    )


__all__ = ["register"]
