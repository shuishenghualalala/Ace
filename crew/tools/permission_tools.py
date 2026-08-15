"""Model-facing capability request tool, matching Codex request_permissions semantics."""

from __future__ import annotations

import json
from typing import Any

from crew.core.errors import ToolError
from crew.security.approvals import ApprovalError
from crew.security.context import SecurityContextError, build_security_context
from crew.security.policy import deserialize_additional_permissions, serialize_additional_permissions
from crew.tools.redact import safe_public_error


REQUEST_PERMISSIONS_SCHEMA = {
    "name": "request_permissions",
    "description": (
        "申请本回合或本对话需要的额外文件系统、网络或本地端口监听权限。"
        "用户只会批准精确列出的能力；额外权限不会成为永久授权。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "permissions": {
                "type": "object",
                "description": "精确的额外权限对象，包含 filesystem、network、allow_local_binding。",
                "properties": {
                    "filesystem": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "root": {"type": "string"},
                                "access": {"type": "string", "enum": ["read", "read_write"]},
                            },
                            "required": ["root", "access"],
                            "additionalProperties": False,
                        },
                    },
                    "network": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "host": {"type": "string"},
                                "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                                "protocol": {"type": "string"},
                                "allow_private": {"type": "boolean"},
                            },
                            "required": ["host", "port", "protocol"],
                            "additionalProperties": False,
                        },
                    },
                    "allow_local_binding": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
            "reason": {"type": "string", "maxLength": 1000},
        },
        "required": ["permissions"],
        "additionalProperties": False,
    },
}


async def handle_request_permissions(
    args: dict[str, Any],
    *,
    workspace_store: Any | None = None,
    security_service: Any | None = None,
) -> str:
    if workspace_store is None or security_service is None:
        raise ToolError("安全权限服务不可用，已拒绝额外权限申请")
    try:
        context = build_security_context(workspace_store)
        permissions = deserialize_additional_permissions(args.get("permissions"))
        reason = str(args.get("reason") or "").strip()
        if len(reason) > 1000:
            raise ValueError("reason 过长")
        request = security_service.request_permissions(
            context,
            permissions,
            reason=reason,
            tool_name="request_permissions",
        )
        outcome = await security_service.await_permission_decision(request["request_id"])
    except (ApprovalError, SecurityContextError, TypeError, ValueError) as exc:
        raise ToolError(safe_public_error(exc, "权限申请无效")) from exc
    if outcome is None or outcome.grant is None:
        raise ToolError("用户未批准额外权限")
    return json.dumps(
        {
            "granted": True,
            "scope": outcome.scope.value if outcome.scope else "turn",
            "permissions": serialize_additional_permissions(outcome.granted_permissions),
        },
        ensure_ascii=False,
    )
