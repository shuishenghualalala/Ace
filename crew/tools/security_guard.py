"""Adapter from built-in file tools to the host security policy service."""

from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path
from typing import Any

from crew.core.errors import ToolError
from crew.security.actions import normalize_file_action
from crew.security.approvals import ApprovalDecision
from crew.security.context import (
    SecurityContextError,
    build_security_context,
    resolve_requested_path,
)
from crew.security.file_policy import FilePolicyResult

_PREVIEW_LIMIT = 4000


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
) -> Path:
    """Return the authorized canonical path or raise a stable ToolError.

    当策略要求审批时，**阻塞等待 owner 决策**而非抛 ``SECURITY_APPROVAL_REQUIRED``
    错误：抛错会被模型复述进正文、且 turn 结束后 grant 无人消费（"对话停了"）。
    阻塞期间 agent 循环挂起在工具调用上，决策到达后自然恢复——批准则继续执行，
    拒绝则回灌干净错误让模型自适应（对齐 codex/opencode 的 deny 语义）。
    """
    raw_path = str(args.get("path") or ".")
    if workspace_store is None or security_service is None:
        # Unit registries may intentionally omit host assembly. Production
        # build_app always injects both dependencies.
        from crew.tools.file_utils import _resolve_path

        return _resolve_path(raw_path)
    try:
        context = build_security_context(workspace_store)
        target = resolve_requested_path(context, raw_path)
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
    except (SecurityContextError, TypeError, ValueError) as exc:
        raise ToolError(f"安全文件上下文无效: {exc}") from exc
    if result is FilePolicyResult.DENY:
        raise ToolError(
            json.dumps(
                {"code": "SECURITY_FILE_DENIED", "reason": reason, "path": str(target)},
                ensure_ascii=False,
            )
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
            return target
        # 罕见竞态（grant 在 decide 与重检之间过期或被撤销）→ fail-closed，模型可重试。
        raise ToolError("批准后授权校验失败，请重试")
    return target
