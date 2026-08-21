"""Adapter from built-in file tools to the host security policy service."""

from __future__ import annotations

import difflib
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from crew.core.errors import ToolError
from crew.security.actions import normalize_file_action, normalize_network_action
from crew.security.approvals import ApprovalDecision
from crew.security.context import (
    SecurityContextError,
    build_security_context,
    resolve_requested_path,
)
from crew.security.file_policy import FilePolicyResult
from crew.security.local_path import LocalPathReference
from crew.security.outbound import (
    ConnectionPlan,
    OutboundDenied,
    OutboundHttpClient,
    OutboundHttpResponse,
    OutboundPolicy,
)
from crew.tools.file_utils import (
    FileConflictError,
    FileIdentity,
    capture_file_identity,
)
from crew.tools.redact import safe_public_error

_PREVIEW_LIMIT = 4000
_OUTBOUND_POLICY = OutboundPolicy()
_OUTBOUND_HTTP = OutboundHttpClient(_OUTBOUND_POLICY)


@dataclass(frozen=True)
class AuthorizedFileTarget:
    """Canonical path plus the immutable leaf identity approved for use."""

    path: Path
    identity: FileIdentity


def _file_change_preview(args: dict[str, Any], operation: str) -> tuple[str, str]:
    """Bind approvals to exact proposed content without reading the target first."""
    if operation == "write":
        content = str(args.get("content", ""))
        append = bool(args.get("append", False))
        bound = json.dumps(
            {"append": append, "content": content},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        label = "待追加内容" if append else "待写入内容"
        preview = f"{label}：\n{content}"
    elif operation == "patch":
        old = str(args.get("old", ""))
        new = str(args.get("new", ""))
        count = int(args.get("count", 1))
        bound = json.dumps(
            {"count": count, "new": new, "old": old},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        preview = "".join(
            difflib.unified_diff(
                old.splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile="待替换文本",
                tofile="替换后文本",
            )
        )
    else:
        return "", ""
    digest = hashlib.sha256(bound.encode("utf-8")).hexdigest()
    if len(preview) > _PREVIEW_LIMIT:
        preview = f"{preview[:_PREVIEW_LIMIT]}\n…（预览已截断，授权仍绑定完整内容摘要）"
    return digest, preview


async def authorize_file_tool(
    args: dict[str, Any],
    *,
    operation: str,
    tool_name: str,
    workspace_store: Any | None,
    security_service: Any | None,
    bind_identity: bool = False,
) -> Path | AuthorizedFileTarget:
    """Return the authorized canonical path or raise a stable ToolError.

    当策略要求审批时，**阻塞等待 owner 决策**而非抛 ``SECURITY_APPROVAL_REQUIRED``
    错误：抛错会被模型复述进正文、且 turn 结束后 grant 无人消费（"对话停了"）。
    阻塞期间 agent 循环挂起在工具调用上，决策到达后自然恢复——批准则继续执行，
    拒绝则回灌干净错误让模型自适应（对齐 codex/opencode 的 deny 语义）。
    """
    raw_path = args.get("path")
    if raw_path is None or raw_path == "":
        raw_path = "."
    if workspace_store is None or security_service is None:
        raise ToolError("文件工具缺少安全授权上下文")
    try:
        path_reference = LocalPathReference.parse(raw_path)
        context = build_security_context(workspace_store)
        target = resolve_requested_path(context, path_reference)
        identity = capture_file_identity(target) if bind_identity else None
        content_digest, preview = _file_change_preview(args, operation)
        action = normalize_file_action(
            target,
            operation,
            offset=max(0, int(args.get("offset") or 0)),
            limit=max(0, int(args.get("limit") or args.get("head_limit") or 0)),
            content_digest=content_digest,
        )
        result, reason, request = security_service.authorize_file_action(
            context,
            action,
            tool_name=tool_name,
            preview=preview,
        )
    except (
        FileConflictError,
        OSError,
        SecurityContextError,
        TypeError,
        ValueError,
    ) as exc:
        raise ToolError(safe_public_error(exc, "安全文件上下文无效")) from exc
    if result is FilePolicyResult.DENY:
        raise ToolError(
            json.dumps(
                {"code": "SECURITY_FILE_DENIED", "reason": reason, "path": str(target)},
                ensure_ascii=False,
            )
        )
    if result is FilePolicyResult.ALLOW:
        return (
            AuthorizedFileTarget(path=target, identity=identity)
            if identity is not None
            else target
        )
    if result is FilePolicyResult.REQUIRE_APPROVAL:
        assert request is not None
        outcome = await security_service.await_decision(request["request_id"])
        if outcome is None or outcome.decision is ApprovalDecision.REJECT:
            # 干净错误：不带 path/request_id，避免被模型复述成正文污染对话。
            raise ToolError("用户未批准该文件访问")
        # 批准：grant/rule 已由 decide() 落地；重跑 authorize_file_action 走既有路径
        # 消费 once grant 或命中 always/session 规则，避免在此重复授权逻辑。
        result2, _reason2, _req2 = security_service.authorize_file_action(
            context,
            action,
            tool_name=tool_name,
            preview=preview,
        )
        if result2 is FilePolicyResult.ALLOW:
            return (
                AuthorizedFileTarget(path=target, identity=identity)
                if identity is not None
                else target
            )
        # 罕见竞态（grant 在 decide 与重检之间过期或被撤销）→ fail-closed，模型可重试。
    raise ToolError("批准后授权校验失败，请重试")


def _outbound_tool_error(exc: OutboundDenied) -> ToolError:
    return ToolError(
        json.dumps(
            {
                "code": "SECURITY_OUTBOUND_DENIED",
                "reason": exc.code,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


@dataclass(frozen=True)
class NetworkAuthorization:
    """An approved exact origin that may mint one-use plans for its requests."""

    scheme: str
    host: str
    port: int
    method: str
    _policy: OutboundPolicy = field(repr=False, compare=False)

    @property
    def origin(self) -> tuple[str, str, int]:
        return self.scheme, self.host, self.port

    def plan(self, url: str, *, method: str = "GET") -> ConnectionPlan:
        try:
            _parsed, target = self._policy.canonicalize_url(url, method=method)
            if (
                (target.scheme, target.host, target.port) != self.origin
                or target.method != self.method
            ):
                raise OutboundDenied("authorization_mismatch")
            return self._policy.plan_url(
                target.canonical_url,
                method=target.method,
            )
        except OutboundDenied as exc:
            raise _outbound_tool_error(exc) from exc


def validate_public_url(url: str) -> tuple[str, int, str]:
    """Reject the legacy DNS precheck; callers must request an authorized plan."""
    del url
    raise ToolError(
        '{"code":"SECURITY_OUTBOUND_DENIED","reason":"authorization_required"}'
    )


PublicHttpResponse = OutboundHttpResponse


def fetch_public_url(
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
    max_bytes: int = 2_000_000,
    reject_redirects: bool = True,
) -> PublicHttpResponse:
    """Reject the legacy URL-only adapter; callers must consume an approved plan."""
    del url, method, body, headers, timeout, max_bytes, reject_redirects
    raise ToolError(
        '{"code":"SECURITY_OUTBOUND_DENIED","reason":"authorization_required"}'
    )


def fetch_authorized_url(
    plan: ConnectionPlan,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
    max_bytes: int = 2_000_000,
    max_request_bytes: int = 10_000_000,
    reject_redirects: bool = True,
) -> PublicHttpResponse:
    """Consume one approved, DNS-pinned plan without parsing or resolving again."""
    try:
        response = _OUTBOUND_HTTP.fetch_plan(
            plan,
            method=plan.target.method,
            body=body,
            headers=headers,
            timeout=timeout,
            max_bytes=max_bytes,
            max_request_bytes=max_request_bytes,
            context=plan.context,
        )
        if (
            reject_redirects
            and response.status in OutboundHttpClient._REDIRECT_STATUSES
        ):
            raise OutboundDenied("redirect_forbidden")
        return response
    except OutboundDenied as exc:
        raise _outbound_tool_error(exc) from exc


async def authorize_network_origin(
    url: str,
    *,
    method: str = "GET",
    tool_name: str,
    workspace_store: Any | None,
    security_service: Any | None,
) -> NetworkAuthorization:
    """Approve one exact normalized origin without resolving DNS."""
    if workspace_store is None or security_service is None:
        raise ToolError(
            '{"code":"SECURITY_OUTBOUND_DENIED",'
            '"reason":"authorization_unavailable"}'
        )
    try:
        _parsed, target = _OUTBOUND_POLICY.canonicalize_url(url, method=method)
        context = build_security_context(workspace_store)
        action = normalize_network_action(
            target.host,
            target.port,
            target.scheme,
            method=target.method,
        )
        result, reason, request = security_service.authorize_network_action(
            context,
            action,
            tool_name=tool_name,
        )
    except OutboundDenied as exc:
        raise _outbound_tool_error(exc) from exc
    except (SecurityContextError, TypeError, ValueError) as exc:
        raise ToolError(safe_public_error(exc, "安全网络上下文无效")) from exc
    if result is FilePolicyResult.DENY:
        raise ToolError(json.dumps({"code": "SECURITY_NETWORK_DENIED", "reason": reason}))
    if result is FilePolicyResult.REQUIRE_APPROVAL:
        assert request is not None
        outcome = await security_service.await_decision(request["request_id"])
        if outcome is None or outcome.decision is ApprovalDecision.REJECT:
            raise ToolError("用户未批准该网络访问")
        result2, _reason2, _request2 = security_service.authorize_network_action(
            context,
            action,
            tool_name=tool_name,
        )
        if result2 is not FilePolicyResult.ALLOW:
            raise ToolError("批准后网络授权校验失败，请重试")
    return NetworkAuthorization(
        scheme=target.scheme,
        host=target.host,
        port=target.port,
        method=target.method,
        _policy=_OUTBOUND_POLICY,
    )


async def authorize_network_url(
    url: str,
    *,
    method: str = "GET",
    tool_name: str,
    workspace_store: Any | None,
    security_service: Any | None,
) -> ConnectionPlan:
    """Approve one exact origin, then resolve and pin its one-use connect plan."""
    authorization = await authorize_network_origin(
        url,
        method=method,
        tool_name=tool_name,
        workspace_store=workspace_store,
        security_service=security_service,
    )
    return authorization.plan(
        url,
        method=method,
    )


async def authorize_network_tool(
    url: str,
    *,
    tool_name: str,
    workspace_store: Any | None,
    security_service: Any | None,
) -> None:
    """Authorize a public HTTP(S) target and wait for an owner decision if needed."""
    from crew.security.outbound import parse_public_http_target

    try:
        parse_public_http_target(url)
    except ValueError as exc:
        raise ToolError(f"联网目标无效: {exc}") from exc
    if workspace_store is None or security_service is None:
        return
    try:
        context = build_security_context(workspace_store)
    except (SecurityContextError, TypeError, ValueError) as exc:
        raise ToolError(f"安全联网上下文无效: {exc}") from exc
    await authorize_network_target(
        url,
        tool_name=tool_name,
        security_service=security_service,
        security_context=context,
    )


async def authorize_network_target(
    url: str,
    *,
    tool_name: str,
    security_service: Any,
    security_context: Any,
) -> None:
    """Authorize an exact public target for a host-built security context."""
    from crew.security.outbound import parse_public_http_target

    try:
        target = parse_public_http_target(url)
        action = normalize_network_action(target.host, target.port, target.protocol)
        result = security_service.authorize_network_action(
            security_context,
            action,
            tool_name=tool_name,
            public_target=True,
        )
    except (SecurityContextError, TypeError, ValueError) as exc:
        raise ToolError(f"安全联网上下文无效: {exc}") from exc
    if result.allowed:
        return
    if result.request is None:
        raise ToolError("联网请求已被安全策略拒绝")
    outcome = await security_service.await_decision(result.request["request_id"])
    if outcome is None:
        raise ToolError(
            '{"error_code":"approval_expired","error":"联网审批已过期或会话已变更"}'
        )
    if outcome.decision is ApprovalDecision.REJECT:
        raise ToolError(
            '{"error_code":"approval_rejected","error":"用户拒绝了该联网请求"}'
        )
    result2 = security_service.authorize_network_action(
        security_context,
        action,
        tool_name=tool_name,
        public_target=True,
    )
    if not result2.allowed:
        raise ToolError("批准后联网授权校验失败，请重试")


async def authorize_configured_mcp_call(
    url: str,
    *,
    tool_name: str,
    args: dict[str, Any],
    security_service: Any,
    security_context: Any,
) -> None:
    """Bind one remote MCP call and its configured network target to one approval."""
    from crew.security.actions import normalize_exec_action
    from crew.security.models import (
        AdditionalPermissionProfile,
        NetworkEntry,
        SandboxPermissions,
    )

    value = str(url or "").strip()
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ToolError("MCP URL 包含控制字符")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ToolError("MCP 远程端点仅支持 http/https")
    if parsed.username is not None or parsed.password is not None:
        raise ToolError("MCP URL 不允许内嵌用户名或密码")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        additional = AdditionalPermissionProfile(
            network=(NetworkEntry(
                parsed.hostname.rstrip(".").lower(),
                port,
                parsed.scheme,
                allow_private=True,
            ),),
            sandbox_permissions=SandboxPermissions.WITH_ADDITIONAL_PERMISSIONS,
        )
        canonical_args = json.dumps(
            args,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        action = normalize_exec_action(
            ("mcp-call", tool_name, hashlib.sha256(canonical_args.encode("utf-8")).hexdigest()),
            security_context.workspace_root or Path.cwd(),
        )
        result = security_service.authorize_exec_action(
            security_context,
            action,
            tool_name=tool_name,
            risk_class="mcp_remote_tool",
            requires_approval=True,
            additional_permissions=additional,
            preview=f"远程 MCP 工具：{tool_name}\n端点：{parsed.scheme}://{parsed.hostname}:{port}",
        )
    except (SecurityContextError, TypeError, ValueError) as exc:
        raise ToolError(f"安全联网上下文无效: {exc}") from exc
    if result.allowed:
        return
    if result.request is None:
        raise ToolError("MCP 联网请求已被安全策略拒绝")
    outcome = await security_service.await_decision(result.request["request_id"])
    if outcome is None:
        raise ToolError(
            '{"error_code":"approval_expired","error":"MCP 联网审批已过期或会话已变更"}'
        )
    if outcome.decision is ApprovalDecision.REJECT:
        raise ToolError(
            '{"error_code":"approval_rejected","error":"用户拒绝了该 MCP 联网请求"}'
        )
    result2 = security_service.authorize_exec_action(
        security_context,
        action,
        tool_name=tool_name,
        risk_class="mcp_remote_tool",
        requires_approval=True,
        additional_permissions=additional,
        preview=f"远程 MCP 工具：{tool_name}\n端点：{parsed.scheme}://{parsed.hostname}:{port}",
    )
    if not result2.allowed:
        raise ToolError("批准后 MCP 联网授权校验失败，请重试")


async def authorize_exec_tool(
    argv: Sequence[str],
    *,
    cwd: str | Path,
    tool_name: str,
    workspace_store: Any | None,
    security_service: Any | None,
    security_context: Any | None = None,
    preview: str = "",
) -> None:
    """Authorize one exact direct-argv build command and wait when required."""
    if workspace_store is None or security_service is None:
        return
    from crew.security.actions import normalize_exec_action

    try:
        context = security_context or build_security_context(workspace_store)
        action = normalize_exec_action(tuple(argv), Path(cwd))
        result = security_service.authorize_exec_action(
            context,
            action,
            tool_name=tool_name,
            risk_class="site_build",
            requires_approval=False,
            preview=preview,
        )
    except (SecurityContextError, TypeError, ValueError) as exc:
        raise ToolError(f"安全构建上下文无效: {exc}") from exc
    if result.allowed:
        return
    if result.request is None:
        raise ToolError("站点构建已被安全策略拒绝")
    outcome = await security_service.await_decision(result.request["request_id"])
    if outcome is None:
        raise ToolError(
            '{"error_code":"approval_expired","error":"站点构建审批已过期或会话已变更"}'
        )
    if outcome.decision is ApprovalDecision.REJECT:
        raise ToolError(
            '{"error_code":"approval_rejected","error":"用户拒绝了站点构建"}'
        )
    result2 = security_service.authorize_exec_action(
        context,
        action,
        tool_name=tool_name,
        risk_class="site_build",
        requires_approval=True,
        preview=preview,
    )
    if not result2.allowed:
        raise ToolError("批准后构建授权校验失败，请重试")


def authorize_user_initiated_exec(
    argv: Sequence[str],
    *,
    cwd: str | Path,
    tool_name: str,
    security_service: Any,
    security_context: Any,
) -> None:
    """Apply rules and audit an authenticated Desktop command without a second prompt."""
    from crew.security.actions import normalize_exec_action

    try:
        action = normalize_exec_action(tuple(argv), Path(cwd))
        result = security_service.authorize_user_initiated_exec_action(
            security_context,
            action,
            tool_name=tool_name,
        )
    except (SecurityContextError, TypeError, ValueError) as exc:
        raise ToolError(f"安全构建上下文无效: {exc}") from exc
    if not result.allowed:
        raise ToolError("站点构建已被安全规则拒绝")
