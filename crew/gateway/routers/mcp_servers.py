"""通用 MCP Server 管理 API：增删改查 + 状态 + 单 server 重连。

供桌面端「MCP 服务」管理面板调用。MCP server 配置在 config.yaml 的 mcp_servers 段是
全局共享、无 owner 维度；读取对所有登录用户开放，写操作仅允许 Gateway 管理员。
登录校验由 gateway require_gateway_login 中间件保证，未登录返回 401。

密钥处理：env/headers 中的敏感值只写入平台凭据库；config.yaml 仅保存绑定标记，
GET 响应始终脱敏为 ***。
"""

from __future__ import annotations

import asyncio
import getpass
import hashlib
import json
import logging
import re
import secrets
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from crew.gateway.auth import AuthenticationError, account_from_request, require_admin
from crew.gateway.helpers import safe_public_error
from crew.security.audit import AuditEvent
from crew.security.mcp_secrets import mcp_field_is_sensitive
from crew.tools.redact import argv_contains_sensitive_value

# 合法 server 名：防注入 Registry 命名 {server}__{tool}，且作 yaml key 安全。
_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_VALID_TRANSPORTS = {"stdio", "http", "sse"}
_LOGGER = logging.getLogger(__name__)


def _redact_env(env: Any) -> dict[str, Any]:
    """脱敏 env 里的敏感值为 ***，供 GET 响应。"""
    if not isinstance(env, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in env.items():
        if mcp_field_is_sensitive("env", str(k)):
            if isinstance(v, dict) and v.get("source") == "local":
                out[str(k)] = {"source": "local", "value": "***"}
            else:
                out[str(k)] = "***"
        else:
            out[str(k)] = v
    return out


def _redact_headers(headers: Any) -> dict[str, Any]:
    if not isinstance(headers, dict):
        return {}
    return {
        str(key): "***" if mcp_field_is_sensitive("headers", str(key)) else value
        for key, value in headers.items()
    }


def _redact_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """脱敏单个 server 配置。"""
    out = dict(cfg)
    out["env"] = _redact_env(cfg.get("env"))
    out["headers"] = _redact_headers(cfg.get("headers"))
    return out


def _config_digest(name: str, cfg: dict[str, Any] | None) -> str:
    """Hash only the redacted canonical config; never put MCP secrets in audit."""
    payload = json.dumps(
        {"name": str(name), "config": _redact_config(cfg or {})},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validated_mapping(
    raw: Any,
    *,
    section: str,
    existing: dict[str, Any] | None,
) -> tuple[dict[str, str] | None, str | None]:
    if not isinstance(raw, dict):
        return None, f"{section} 必须是对象"
    values: dict[str, str] = {}
    normalized_names: set[str] = set()
    previous = existing or {}
    for raw_name, raw_value in raw.items():
        name = str(raw_name)
        normalized = name.casefold()
        if (
            not name
            or "\x00" in name
            or normalized in normalized_names
            or not isinstance(raw_value, str)
            or "\x00" in raw_value
        ):
            return None, f"{section} 包含非法或重复字段"
        normalized_names.add(normalized)
        value = raw_value
        if value == "***" and mcp_field_is_sensitive(section, name):
            old_value = previous.get(name)
            if not isinstance(old_value, str):
                return None, f"{section} 脱敏字段缺少原值"
            value = old_value
        values[name] = value
    return values, None


def _validated_stdio_env(
    raw: Any,
    *,
    source: str,
    existing: dict[str, Any] | None,
) -> tuple[dict[str, dict[str, str]] | None, str | None]:
    if not isinstance(raw, dict):
        return None, "env 必须是对象"
    values: dict[str, dict[str, str]] = {}
    normalized_names: set[str] = set()
    previous = existing or {}
    for raw_name, raw_value in raw.items():
        name = str(raw_name)
        normalized = name.casefold()
        if not name or "\x00" in name or normalized in normalized_names:
            return None, "env 包含非法或重复字段"
        normalized_names.add(normalized)
        if isinstance(raw_value, str):
            if source != "local" or "\x00" in raw_value:
                return None, "env 本地值与 stdio 来源不匹配"
            entry: dict[str, str] = {"source": "local", "value": raw_value}
        elif isinstance(raw_value, dict):
            entry_source = str(raw_value.get("source") or "").strip().lower()
            expected_keys = (
                {"source", "value"} if entry_source == "local" else {"source"}
            )
            if (
                entry_source not in {"local", "remote"}
                or set(raw_value) != expected_keys
                or entry_source != source
            ):
                return None, "env 来源声明与 stdio 来源不匹配"
            if entry_source == "remote":
                entry = {"source": "remote"}
            else:
                entry_value = raw_value.get("value")
                if not isinstance(entry_value, str) or "\x00" in entry_value:
                    return None, "env 包含非法值"
                entry = {"source": "local", "value": entry_value}
        else:
            return None, "env 值必须包含显式来源"

        if (
            entry.get("value") == "***"
            and mcp_field_is_sensitive("env", name)
        ):
            old_value = previous.get(name)
            if isinstance(old_value, dict):
                old_value = old_value.get("value")
            if not isinstance(old_value, str):
                return None, "env 脱敏字段缺少原值"
            entry["value"] = old_value
        values[name] = entry
    for raw_name, raw_value in previous.items():
        name = str(raw_name)
        if name in values or not mcp_field_is_sensitive("env", name):
            continue
        if isinstance(raw_value, dict):
            values[name] = dict(raw_value)
        elif isinstance(raw_value, str) and source == "local":
            values[name] = {"source": "local", "value": raw_value}
    return values, None


def _validate_server_payload(
    payload: dict[str, Any],
    *,
    existing: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """校验新增/编辑 payload，返回 (规范化配置, 错误信息)。"""
    cfg: dict[str, Any] = {}
    command = str(payload.get("command") or "").strip()
    url = str(payload.get("url") or "").strip()
    transport = str(payload.get("transport") or "stdio").strip().lower()

    if command:
        if transport != "stdio":
            return None, "command 传输必须使用 stdio"
        stdio_source = str(payload.get("stdio_source") or "local").strip().lower()
        if stdio_source not in {"local", "remote"}:
            return None, "stdio_source 必须是 local 或 remote"
        if stdio_source != "local":
            return None, "remote MCP stdio executor 尚不可用"
        if "\x00" in command:
            return None, "command 包含非法字符"
        raw_args = payload.get("args") or []
        if (
            not isinstance(raw_args, list)
            or not all(
                isinstance(value, str) and "\x00" not in value
                for value in raw_args
            )
        ):
            return None, "args 必须是无 NUL 的字符串数组"
        if argv_contains_sensitive_value((command, *raw_args)):
            return None, "MCP 凭据不得通过 argv 传递"
        try:
            from crew.tools.mcp_client import (
                MCP_COMMAND_MAX_BYTES,
                _resolve_command,
            )
            from crew.tools.file_utils import read_verified_bytes

            resolved_command = _resolve_command(command)
            command_digest = hashlib.sha256(
                read_verified_bytes(
                    Path(resolved_command),
                    max_bytes=MCP_COMMAND_MAX_BYTES,
                    reject_hard_links=False,
                )
            ).hexdigest()
        except (OSError, RuntimeError, ValueError):
            return None, "command 必须是可验证的绝对本地可执行文件"
        supplied_digest = payload.get("command_sha256")
        if supplied_digest is not None and (
            not isinstance(supplied_digest, str)
            or not secrets.compare_digest(
                supplied_digest.casefold(),
                command_digest,
            )
        ):
            return None, "command_sha256 与可执行文件不匹配"
        cfg["command"] = resolved_command
        cfg["command_sha256"] = command_digest
        cfg["args"] = list(raw_args)
        cfg["stdio_source"] = stdio_source
        raw_cwd = payload.get("cwd")
        if raw_cwd is not None:
            if not isinstance(raw_cwd, str) or "\x00" in raw_cwd:
                return None, "cwd 必须是无 NUL 的字符串"
            cfg["cwd"] = raw_cwd
        # transport 仅作记录，stdio 时忽略
    elif url:
        if "\x00" in url:
            return None, "url 包含非法字符"
        if transport not in _VALID_TRANSPORTS:
            return None, f"transport 必须是 {sorted(_VALID_TRANSPORTS)} 之一"
        if argv_contains_sensitive_value((url,)):
            return None, "MCP 凭据不得嵌入 URL"
        cfg["url"] = url
        cfg["transport"] = transport
        headers = payload.get("headers")
        if headers is not None:
            values, error = _validated_mapping(
                headers,
                section="headers",
                existing=(
                    existing.get("headers")
                    if isinstance(existing, dict)
                    and isinstance(existing.get("headers"), dict)
                    else None
                ),
            )
            if error is not None:
                return None, error
            cfg["headers"] = values
    else:
        return None, "必须提供 command（stdio）或 url（http/sse）"

    env = payload.get("env")
    if env is not None:
        if command:
            values, error = _validated_stdio_env(
                env,
                source=str(cfg.get("stdio_source") or "local"),
                existing=(
                    existing.get("env")
                    if isinstance(existing, dict)
                    and isinstance(existing.get("env"), dict)
                    else None
                ),
            )
        else:
            values, error = _validated_mapping(
                env,
                section="env",
                existing=(
                    existing.get("env")
                    if isinstance(existing, dict)
                    and isinstance(existing.get("env"), dict)
                    else None
                ),
            )
        if error is not None:
            return None, error
        cfg["env"] = values
    return cfg, None


def create_mcp_servers_router(crew) -> APIRouter:
    router = APIRouter()

    def _audit_admin_denial(
        request: Request,
        *,
        owner_account_id: str,
        action: str,
        server_name: str = "",
    ) -> None:
        safe_name = server_name if _NAME_RE.fullmatch(server_name) else ""
        canonical = f"{request.method.upper()}\n{action}\n{safe_name}".encode()
        try:
            os_user = getpass.getuser()
        except (OSError, RuntimeError):
            os_user = "unknown"
        event = AuditEvent(
            event_id=uuid4().hex,
            os_user_hash=hashlib.sha256(os_user.encode("utf-8")).hexdigest(),
            owner_account_id=owner_account_id,
            workspace_id="gateway",
            session_id="",
            task_id="",
            request_id="",
            action_type="mcp_server_admin_denied",
            normalized_action_hash=hashlib.sha256(canonical).hexdigest(),
            rule_id="",
            rule_scope="",
            permission_profile_hash="",
            additional_permissions_summary="",
            decision="deny",
            decision_source="gateway_admin_policy",
            sandbox_backend="",
            capabilities=(),
            network_target_summary="",
            exit_code=None,
            stable_error_code="gateway_admin_required",
            tool_name="gateway:mcp_servers",
            action_summary=f"MCP server {action} denied",
            action_detail=(
                f"{request.method.upper()} action={action}"
                + (f" server={safe_name}" if safe_name else "")
            ),
        )
        try:
            crew.security_audit.record(event)
        except Exception:
            _LOGGER.exception("failed to record MCP admin denial audit")

    def _record_admin_action(
        request: Request,
        *,
        action: str,
        server_name: str,
        old_digest: str,
        new_digest: str,
        outcome: str,
        stable_error_code: str = "",
    ) -> bool:
        account = account_from_request(request)
        safe_name = server_name if _NAME_RE.fullmatch(server_name) else ""
        canonical = json.dumps(
            {
                "action": action,
                "method": request.method.upper(),
                "new_digest": new_digest,
                "old_digest": old_digest,
                "owner": account.owner_account_id,
                "outcome": outcome,
                "server": safe_name,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            os_user = getpass.getuser()
        except (OSError, RuntimeError):
            os_user = "unknown"
        event = AuditEvent(
            event_id=uuid4().hex,
            os_user_hash=hashlib.sha256(os_user.encode("utf-8")).hexdigest(),
            owner_account_id=account.owner_account_id,
            workspace_id="gateway",
            session_id="",
            task_id="",
            request_id="",
            action_type="mcp_server_admin_action",
            normalized_action_hash=hashlib.sha256(canonical).hexdigest(),
            rule_id="gateway-admin",
            rule_scope="global",
            permission_profile_hash="",
            additional_permissions_summary=(
                f"old_digest={old_digest} new_digest={new_digest} outcome={outcome}"
            ),
            decision="allow" if outcome in {"requested", "succeeded"} or outcome.endswith("_succeeded") else "error",
            decision_source="gateway_admin_policy",
            sandbox_backend="",
            capabilities=("mcp_server_admin",),
            network_target_summary="",
            exit_code=None,
            stable_error_code=stable_error_code,
            tool_name="gateway:mcp_servers",
            action_summary=f"MCP server {action} {outcome}",
            action_detail=(
                f"{request.method.upper()} action={action} server={safe_name} "
                f"old_digest={old_digest} new_digest={new_digest} outcome={outcome}"
            ),
        )
        try:
            crew.security_audit.record(event)
        except Exception:  # noqa: BLE001 - sensitive mutation must fail closed
            _LOGGER.exception("failed to record MCP admin action audit")
            return False
        return True

    def _audit_requested(
        request: Request,
        *,
        action: str,
        server_name: str,
        old_digest: str,
        new_digest: str,
    ) -> JSONResponse | None:
        if _record_admin_action(
            request,
            action=action,
            server_name=server_name,
            old_digest=old_digest,
            new_digest=new_digest,
            outcome="requested",
        ):
            return None
        return JSONResponse(
            {"ok": False, "error": "security audit unavailable"},
            status_code=503,
        )

    async def _run_and_audit(
        request: Request,
        *,
        action: str,
        server_name: str,
        old_digest: str,
        new_digest: str,
        operation,
    ) -> None:
        try:
            succeeded = bool(await operation())
        except Exception:  # noqa: BLE001 - terminal state is recorded without details
            _LOGGER.exception("MCP server %s operation failed name=%s", action, server_name)
            succeeded = False
        outcome = f"{action}_{'succeeded' if succeeded else 'failed'}"
        _record_admin_action(
            request,
            action=action,
            server_name=server_name,
            old_digest=old_digest,
            new_digest=new_digest,
            outcome=outcome,
            stable_error_code="" if succeeded else f"mcp_{action}_failed",
        )

    def _admin_or_403(
        request: Request,
        *,
        action: str,
        server_name: str = "",
    ) -> JSONResponse | None:
        account = account_from_request(request)
        try:
            require_admin(account, crew.config)
        except AuthenticationError as exc:
            _audit_admin_denial(
                request,
                owner_account_id=account.owner_account_id,
                action=action,
                server_name=server_name,
            )
            return JSONResponse({"ok": False, "error": safe_public_error(exc, "权限不足")}, status_code=403)
        return None

    async def _ensure_mgr_started():
        """确保 mcp_manager 已 start（注入 registry）。

        生产路径下 startup() 已调 start()，此处为 no-op；测试或不启 lifespan 的场景下
        首次管理操作触发 start，使 add_server/reload_one 能拿到 registry。
        """
        mgr = crew.mcp_manager
        if mgr is not None and getattr(mgr, "_registry", None) is None:
            await mgr.start(crew.registry)

    def _servers_view() -> list[dict[str, Any]]:
        mgr = crew.mcp_manager
        if mgr is None:
            return []
        return [
            {
                "name": row["name"],
                "transport": row["transport"],
                "connected": row["connected"],
                "error": row["error"],
                "tools": row["tools"],
                "config": _redact_config(row["config"]),
            }
            for row in mgr.status()
        ]

    @router.get("/api/mcp/servers")
    async def list_servers() -> JSONResponse:
        return JSONResponse({"ok": True, "servers": _servers_view()})

    @router.post("/api/mcp/servers")
    async def create_server(
        request: Request,
        payload: dict[str, Any] | None = None,
    ) -> JSONResponse:
        denied = _admin_or_403(request, action="create")
        if denied is not None:
            return denied
        payload = payload or {}
        name = str(payload.get("name") or "").strip()
        if not name or not _NAME_RE.match(name):
            return JSONResponse({"ok": False, "error": "name 非法（仅字母数字下划线连字符）"}, status_code=400)
        if name in (crew.config.mcp_servers or {}):
            return JSONResponse({"ok": False, "error": f"MCP server 已存在: {name}"}, status_code=409)
        cfg, err = _validate_server_payload(payload)
        if err is not None:
            return JSONResponse({"ok": False, "error": err}, status_code=400)

        new_digest = _config_digest(name, cfg)
        audit_error = _audit_requested(
            request,
            action="create",
            server_name=name,
            old_digest="",
            new_digest=new_digest,
        )
        if audit_error is not None:
            return audit_error

        crew.config.set_mcp_server(name, cfg)
        try:
            crew.config.persist_mcp_servers()
        except Exception:  # noqa: BLE001
            crew.config.remove_mcp_server(name)
            _record_admin_action(
                request,
                action="create",
                server_name=name,
                old_digest="",
                new_digest=new_digest,
                outcome="failed",
                stable_error_code="mcp_config_persistence_failed",
            )
            _LOGGER.error("MCP server create persistence failed name=%s", name)
            return JSONResponse(
                {"ok": False, "error": "MCP 配置持久化失败"},
                status_code=500,
            )
        persisted_cfg = dict(crew.config.mcp_servers[name])
        if not _record_admin_action(
            request,
            action="create",
            server_name=name,
            old_digest="",
            new_digest=new_digest,
            outcome="succeeded",
        ):
            crew.config.remove_mcp_server(name)
            try:
                crew.config.persist_mcp_servers()
            except Exception:
                _LOGGER.exception("MCP create audit rollback persistence failed name=%s", name)
            return JSONResponse(
                {"ok": False, "error": "security audit unavailable"},
                status_code=503,
            )

        # 增量启动单 server（后台连接，不阻塞响应）：worker.start() 最多等 30s
        # 启动超时，若同步等待会让前端 create 请求 hang 30s，弹层不关、列表不刷新。
        # 改为 fire-and-forget：配置已持久化，立即返回 201，连接在后台进行；前端刷新
        # 列表时该 server 会以 connected=false 出现，连上后下次 status() 轮询转为 true。
        await _ensure_mgr_started()
        if crew.mcp_manager is not None:
            crew.mcp_manager.register_pending(name, persisted_cfg)
            asyncio.create_task(
                _run_and_audit(
                    request,
                    action="connect",
                    server_name=name,
                    old_digest="",
                    new_digest=new_digest,
                    operation=lambda: crew.mcp_manager.add_server(name, persisted_cfg),
                )
            )

        return JSONResponse({"ok": True, "servers": _servers_view()}, status_code=201)

    @router.put("/api/mcp/servers/{name}")
    async def update_server(
        name: str,
        request: Request,
        payload: dict[str, Any] | None = None,
    ) -> JSONResponse:
        denied = _admin_or_403(request, action="update", server_name=name)
        if denied is not None:
            return denied
        if name not in (crew.config.mcp_servers or {}):
            return JSONResponse({"ok": False, "error": f"MCP server 不存在: {name}"}, status_code=404)
        payload = payload or {}
        previous_cfg = dict(crew.config.mcp_servers[name])
        cfg, err = _validate_server_payload(payload, existing=previous_cfg)
        if err is not None:
            return JSONResponse({"ok": False, "error": err}, status_code=400)

        old_digest = _config_digest(name, previous_cfg)
        new_digest = _config_digest(name, cfg)
        audit_error = _audit_requested(
            request,
            action="update",
            server_name=name,
            old_digest=old_digest,
            new_digest=new_digest,
        )
        if audit_error is not None:
            return audit_error

        crew.config.set_mcp_server(name, cfg)
        try:
            crew.config.persist_mcp_servers()
        except Exception:  # noqa: BLE001
            crew.config.set_mcp_server(name, previous_cfg)
            _record_admin_action(
                request,
                action="update",
                server_name=name,
                old_digest=old_digest,
                new_digest=new_digest,
                outcome="failed",
                stable_error_code="mcp_config_persistence_failed",
            )
            _LOGGER.error("MCP server update persistence failed name=%s", name)
            return JSONResponse(
                {"ok": False, "error": "MCP 配置持久化失败"},
                status_code=500,
            )
        persisted_cfg = dict(crew.config.mcp_servers[name])
        if not _record_admin_action(
            request,
            action="update",
            server_name=name,
            old_digest=old_digest,
            new_digest=new_digest,
            outcome="succeeded",
        ):
            crew.config.set_mcp_server(name, previous_cfg)
            try:
                crew.config.persist_mcp_servers()
            except Exception:
                _LOGGER.exception("MCP update audit rollback persistence failed name=%s", name)
            return JSONResponse(
                {"ok": False, "error": "security audit unavailable"},
                status_code=503,
            )

        # 增量重连单 server（后台进行，不阻塞响应，理由同 create）
        await _ensure_mgr_started()
        if crew.mcp_manager is not None:
            try:
                quiesce = getattr(crew.mcp_manager, "quiesce_server", None)
                if callable(quiesce):
                    await quiesce(name)
            except Exception:  # noqa: BLE001 - do not leave the new config half-live
                _LOGGER.exception("MCP server quiesce failed name=%s", name)
                crew.config.set_mcp_server(name, previous_cfg)
                try:
                    crew.config.persist_mcp_servers()
                except Exception:
                    _LOGGER.exception("MCP update quiesce rollback persistence failed name=%s", name)
                _record_admin_action(
                    request,
                    action="reload",
                    server_name=name,
                    old_digest=old_digest,
                    new_digest=new_digest,
                    outcome="reload_failed",
                    stable_error_code="mcp_quiesce_failed",
                )
                return JSONResponse(
                    {"ok": False, "error": "MCP 旧连接回收失败"},
                    status_code=503,
                )
            asyncio.create_task(
                _run_and_audit(
                    request,
                    action="reload",
                    server_name=name,
                    old_digest=old_digest,
                    new_digest=new_digest,
                    operation=lambda: crew.mcp_manager.reload_one(name, persisted_cfg),
                )
            )

        return JSONResponse({"ok": True, "servers": _servers_view()})

    @router.delete("/api/mcp/servers/{name}")
    async def delete_server(name: str, request: Request) -> JSONResponse:
        denied = _admin_or_403(request, action="delete", server_name=name)
        if denied is not None:
            return denied
        if name not in (crew.config.mcp_servers or {}):
            return JSONResponse({"ok": False, "error": f"MCP server 不存在: {name}"}, status_code=404)

        previous_cfg = dict(crew.config.mcp_servers[name])
        old_digest = _config_digest(name, previous_cfg)
        audit_error = _audit_requested(
            request,
            action="delete",
            server_name=name,
            old_digest=old_digest,
            new_digest="",
        )
        if audit_error is not None:
            return audit_error
        crew.config.remove_mcp_server(name)
        try:
            crew.config.persist_mcp_servers()
        except Exception:  # noqa: BLE001
            crew.config.set_mcp_server(name, previous_cfg)
            _record_admin_action(
                request,
                action="delete",
                server_name=name,
                old_digest=old_digest,
                new_digest="",
                outcome="failed",
                stable_error_code="mcp_config_persistence_failed",
            )
            _LOGGER.error("MCP server delete persistence failed name=%s", name)
            return JSONResponse(
                {"ok": False, "error": "MCP 配置持久化失败"},
                status_code=500,
            )

        if not _record_admin_action(
            request,
            action="delete",
            server_name=name,
            old_digest=old_digest,
            new_digest="",
            outcome="succeeded",
        ):
            crew.config.set_mcp_server(name, previous_cfg)
            try:
                crew.config.persist_mcp_servers()
            except Exception:
                _LOGGER.exception("MCP delete audit rollback persistence failed name=%s", name)
            return JSONResponse(
                {"ok": False, "error": "security audit unavailable"},
                status_code=503,
            )

        await _ensure_mgr_started()
        if crew.mcp_manager is not None:
            try:
                removed = await crew.mcp_manager.remove_server(name)
            except Exception:  # noqa: BLE001 - failed cleanup must stay fail-closed
                _LOGGER.exception("MCP server remove failed name=%s", name)
                crew.config.set_mcp_server(name, previous_cfg)
                try:
                    crew.config.persist_mcp_servers()
                except Exception:
                    _LOGGER.exception("MCP delete rollback persistence failed name=%s", name)
                _record_admin_action(
                    request,
                    action="delete",
                    server_name=name,
                    old_digest=old_digest,
                    new_digest="",
                    outcome="failed",
                    stable_error_code="mcp_remove_failed",
                )
                _record_admin_action(
                    request,
                    action="remove",
                    server_name=name,
                    old_digest=old_digest,
                    new_digest="",
                    outcome="remove_failed",
                    stable_error_code="mcp_remove_failed",
                )
                return JSONResponse(
                    {"ok": False, "error": "MCP 资源回收失败"},
                    status_code=503,
                )
            _record_admin_action(
                request,
                action="remove",
                server_name=name,
                old_digest=old_digest,
                new_digest="",
                outcome="remove_succeeded" if removed else "remove_failed",
                stable_error_code="" if removed else "mcp_remove_failed",
            )

        return JSONResponse({"ok": True, "servers": _servers_view()})

    @router.post("/api/mcp/servers/{name}/reload")
    async def reload_server(name: str, request: Request) -> JSONResponse:
        denied = _admin_or_403(request, action="reload", server_name=name)
        if denied is not None:
            return denied
        if name not in (crew.config.mcp_servers or {}):
            return JSONResponse({"ok": False, "error": f"MCP server 不存在: {name}"}, status_code=404)
        await _ensure_mgr_started()
        if crew.mcp_manager is None:
            return JSONResponse({"ok": False, "error": "MCP 管理器未初始化"}, status_code=500)
        current_digest = _config_digest(name, crew.config.mcp_servers[name])
        audit_error = _audit_requested(
            request,
            action="reload",
            server_name=name,
            old_digest=current_digest,
            new_digest=current_digest,
        )
        if audit_error is not None:
            return audit_error
        try:
            quiesce = getattr(crew.mcp_manager, "quiesce_server", None)
            if callable(quiesce):
                await quiesce(name)
        except Exception:  # noqa: BLE001 - old handler must not survive a failed cutover
            _LOGGER.exception("MCP server reload quiesce failed name=%s", name)
            _record_admin_action(
                request,
                action="reload",
                server_name=name,
                old_digest=current_digest,
                new_digest=current_digest,
                outcome="reload_failed",
                stable_error_code="mcp_quiesce_failed",
            )
            return JSONResponse(
                {"ok": False, "error": "MCP 旧连接回收失败"},
                status_code=503,
            )
        # 后台重连，不阻塞响应（reload_one 最多等 30s 启动超时）
        asyncio.create_task(
            _run_and_audit(
                request,
                action="reload",
                server_name=name,
                old_digest=current_digest,
                new_digest=current_digest,
                operation=lambda: crew.mcp_manager.reload_one(name),
            )
        )
        return JSONResponse({"ok": True, "servers": _servers_view()})

    return router
