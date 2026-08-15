"""MCP Server：把 Crew 的会话能力对外暴露成 MCP tools。

让任何标准 MCP 客户端都能列会话、读历史、发消息。
用官方 MCPServer + @mcp.tool()。

运行：``crew mcp serve``（stdio）。
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import secrets
from typing import Any

from crew import __version__
from crew.app import CrewApp
from crew.core.envelope import Envelope
from crew.security.outbound import (
    OutboundContext,
    OutboundDenied,
    OutboundGrantRegistry,
    OutboundHttpClient,
    OutboundPolicy,
)
from crew.state.logging import get_logger

log = get_logger("gateway.mcp")

_MCP_MAX_ID_CHARS = 256
_MCP_MAX_REQUEST_ID_CHARS = 128
_MCP_MAX_QUERY_CHARS = 128 * 1024
_MCP_MAX_TEXT_CHARS = 64 * 1024
_MCP_MAX_PLAN_CHARS = 256 * 1024
_MCP_MAX_RESULT_BYTES = 1024 * 1024
_MCP_MAX_LIST_ITEMS = 100
_INTERNAL_GRANTS = OutboundGrantRegistry()
_INTERNAL_POLICY = OutboundPolicy(grants=_INTERNAL_GRANTS)
_INTERNAL_HTTP = OutboundHttpClient(_INTERNAL_POLICY)


def _resolve_bound_owner(crew: CrewApp, owner_account_id: str | None) -> str:
    owner = str(owner_account_id or os.environ.get("CREW_OWNER_ACCOUNT_ID") or "").strip()
    if owner:
        return owner
    active_owner = getattr(crew, "active_owner", None)
    current = getattr(active_owner, "current", None)
    if callable(current):
        lease = current()
        owner = str(getattr(lease, "owner_account_id", "") or "").strip()
        if owner:
            return owner
    config = getattr(crew, "config", None)
    if getattr(config, "gateway_dev_mode", False):
        owner = str(getattr(config, "gateway_dev_account", "") or "").strip()
        if owner:
            return owner
    if str(getattr(config, "auth_mode", "local") or "local").strip().lower() == "local":
        return "local"
    raise ValueError("MCP server 缺少已认证 owner binding")


def _require_owner(owner_account_id: str, bound_owner_account_id: str) -> str:
    owner = _require_identifier(owner_account_id, "owner_account_id")
    if owner != bound_owner_account_id:
        raise ValueError("MCP 调用与已认证 owner binding 不匹配")
    return bound_owner_account_id


def _require_identifier(value: object, field: str, *, maximum: int = _MCP_MAX_ID_CHARS) -> str:
    text = str(value or "")
    if (
        not text
        or text != text.strip()
        or len(text) > maximum
        or "\x00" in text
        or any(ord(char) < 0x20 for char in text)
    ):
        raise ValueError(f"MCP {field} 无效")
    return text


def _require_text(
    value: object,
    field: str,
    *,
    maximum: int = _MCP_MAX_TEXT_CHARS,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"MCP {field} 无效")
    if (
        (not allow_empty and not value.strip())
        or len(value) > maximum
        or "\x00" in value
        or any(ord(char) < 0x20 and char not in "\t\r\n" for char in value)
    ):
        raise ValueError(f"MCP {field} 无效")
    return value


def _require_owned_session(crew: CrewApp, session_id: str, owner_account_id: str) -> str:
    session = _require_identifier(session_id, "session_id")
    if "::turn::" in session:
        raise ValueError("MCP session_id 无效")
    belongs = getattr(crew.session_store, "session_belongs_to", None)
    try:
        owned = bool(callable(belongs) and belongs(session, owner_account_id))
    except Exception:
        log.exception("MCP session ownership 查询失败")
        raise ValueError("MCP session 不存在或不可访问") from None
    if not owned:
        raise ValueError("MCP session 不存在或不可访问")
    return session


def _bounded_json(value: object) -> str:
    fallback = {"ok": False, "code": "RESULT_INVALID", "error": "响应不符合协议限制"}
    try:
        text = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, OverflowError):
        return json.dumps(fallback, ensure_ascii=False)
    if len(text.encode("utf-8")) > _MCP_MAX_RESULT_BYTES:
        return json.dumps(
            {"ok": False, "code": "RESULT_TOO_LARGE", "error": "响应超出协议限制"},
            ensure_ascii=False,
        )
    return text


def _safe_failure(code: str = "MCP_REQUEST_FAILED") -> str:
    return _bounded_json({"ok": False, "code": code, "error": "MCP 请求处理失败"})


def _new_mcp_server(name: str) -> Any:
    """Construct the installed official MCP SDK without pinning one API generation."""
    try:
        from mcp.server import MCPServer
    except ImportError:
        from mcp.server import FastMCP

        return FastMCP(name)
    return MCPServer(name, version=__version__)


def _harden_mcp_tool_schemas(server: Any) -> None:
    """Make the pinned MCP SDK reject rather than silently discard unknown arguments."""
    manager = getattr(server, "_tool_manager", None)
    tools = getattr(manager, "_tools", None)
    if not isinstance(tools, dict):
        raise RuntimeError("MCP SDK 不支持严格工具 schema")
    for tool in tools.values():
        parameters = getattr(tool, "parameters", None)
        metadata = getattr(tool, "fn_metadata", None)
        model = getattr(metadata, "arg_model", None)
        if not isinstance(parameters, dict) or model is None:
            raise RuntimeError("MCP SDK 不支持严格工具 schema")
        parameters["additionalProperties"] = False
        model.model_config = {**model.model_config, "extra": "forbid"}
        model.model_rebuild(force=True)


def build_mcp_server(
    crew: CrewApp,
    *,
    owner_account_id: str | None = None,
) -> Any:
    """构建并返回 MCPServer。未安装 mcp 包则抛 ImportError。"""
    bound_owner = _resolve_bound_owner(crew, owner_account_id)
    mcp = _new_mcp_server("crew")

    @mcp.tool()
    async def sessions_list(owner_account_id: str, workspace_id: str | None = None) -> str:
        """列出会话（可选按 workspace 过滤）。返回 JSON。"""
        owner = _require_owner(owner_account_id, bound_owner)
        if workspace_id is not None:
            workspace_id = _require_identifier(workspace_id, "workspace_id")
        # list_sessions 走 SQLite 阻塞 I/O，丢到线程池避免阻塞事件循环。
        try:
            rows = await asyncio.to_thread(
                crew.session_store.list_sessions,
                workspace_id,
                owner,
            )
        except Exception:
            log.exception("MCP sessions_list 失败")
            return _safe_failure()
        return _bounded_json(rows)

    @mcp.tool()
    async def session_history(session_id: str, owner_account_id: str) -> str:
        """读取某会话的对话历史（user/assistant 文本 + 工具调用名）。返回 JSON。"""
        owner = _require_owner(owner_account_id, bound_owner)
        session_id = _require_owned_session(crew, session_id, owner)

        def _build() -> list[dict[str, Any]]:
            items: list[dict[str, Any]] = []
            for m in crew.session_store.load(session_id, owner_account_id=owner):
                entry: dict[str, Any] = {"role": m.role, "content": m.content}
                if m.tool_calls:
                    entry["tool_calls"] = [tc.name for tc in m.tool_calls]
                items.append(entry)
            return items
        try:
            items = await asyncio.to_thread(_build)
        except Exception:
            log.exception("MCP session_history 失败")
            return _safe_failure()
        return _bounded_json(items)

    @mcp.tool()
    async def send_message(
        session_id: str,
        query: str,
        request_id: str,
        owner_account_id: str,
    ) -> str:
        """向某会话发送一条消息，跑完一轮对话并返回最终文本。"""
        owner = _require_owner(owner_account_id, bound_owner)
        session_id = _require_owned_session(crew, session_id, owner)
        query = _require_text(query, "query", maximum=_MCP_MAX_QUERY_CHARS)
        request_id = _require_identifier(
            request_id,
            "request_id",
            maximum=_MCP_MAX_REQUEST_ID_CHARS,
        )
        envelope = Envelope.of(
            query,
            session_id=session_id,
            user_id=owner,
            channel="mcp",
            request_id=request_id,
        )
        final_text = ""
        error = ""
        error_category = ""
        # 走 dispatch（经 SessionDispatcher）：与 WS/飞书一致，获得 per-session 串行、
        # 忙时策略、agent:start/end hook 与状态落库；直接 crew.handle 会绕过这些。
        try:
            async for chunk in crew.dispatch(envelope):
                if chunk.kind == "final":
                    final_text = str(chunk.body.get("text", ""))
                elif chunk.kind == "error":
                    error = str(chunk.body.get("message", ""))
                    error_category = str(chunk.body.get("category") or "")
        except Exception:
            log.exception("MCP send_message 调度失败 owner=%s session=%s", owner, session_id)
            return _safe_failure("MESSAGE_FAILED")
        if error:
            if error_category == "protocol":
                return _bounded_json(
                    {
                        "ok": False,
                        "code": "REPLAY_DETECTED",
                        "error": "重复请求已拒绝",
                    }
                )
            log.warning("MCP send_message 失败 owner=%s session=%s", owner, session_id)
            return _safe_failure("MESSAGE_FAILED")
        return _bounded_json({"ok": True, "text": final_text})

    @mcp.tool()
    async def session_status(session_id: str, owner_account_id: str) -> str:
        """查询某会话上一轮运行结果（last_status / last_error）。返回 JSON。"""
        owner = _require_owner(owner_account_id, bound_owner)
        session_id = _require_owned_session(crew, session_id, owner)
        try:
            last_status, last_error = await asyncio.to_thread(
                crew.session_store.get_status,
                session_id,
                owner,
            )
        except Exception:
            log.exception("MCP session_status 失败")
            return _safe_failure()
        return _bounded_json(
            {"session_id": session_id, "last_status": last_status, "last_error": last_error},
        )

    @mcp.tool()
    async def team_request_delegate(
        session_id: str,
        owner_account_id: str,
        member: str,
        instruction: str,
        requester_member_id: str = "mcp",
        external_team_id: str = "",
        plan_node_id: str = "",
    ) -> str:
        """请求 Crew Team Runtime 把任务派给团队成员，并快速返回已创建的 task。返回 JSON。

        这是给外部 MCP 客户端使用的受控入口；不会把内部
        delegate_to_teammate 工具直接暴露给外部 agent。
        """
        owner = _require_owner(owner_account_id, bound_owner)
        session_id = _require_owned_session(crew, session_id, owner)
        member = _require_identifier(member, "member")
        instruction = _require_text(instruction, "instruction")
        requester_member_id = _require_identifier(requester_member_id, "requester_member_id")
        if external_team_id:
            external_team_id = _require_identifier(external_team_id, "external_team_id")
        if plan_node_id:
            plan_node_id = _require_identifier(plan_node_id, "plan_node_id")
        if crew.team is None:
            return _safe_failure("TEAM_UNAVAILABLE")
        fn = getattr(crew.team, "request_delegate", None)
        if not callable(fn):
            return _safe_failure("TEAM_UNAVAILABLE")
        try:
            result = await fn(
                session_id,
                member=member,
                instruction=instruction,
                requester_member_id=requester_member_id,
                external_team_id=external_team_id,
                plan_node_id=plan_node_id,
                owner_account_id=owner,
            )
        except Exception:  # noqa: BLE001 - MCP 工具返回结构化错误
            log.exception("MCP team delegate 失败")
            return _safe_failure()
        return _bounded_json(result)

    @mcp.tool()
    async def team_plan_create(
        session_id: str,
        owner_account_id: str,
        goal: str,
        nodes: list[dict[str, Any]],
        edges: list[Any] | None = None,
        external_team_id: str = "",
    ) -> str:
        """Create or replace a TeamPlan DAG for a Crew team session. Returns JSON."""
        owner = _require_owner(owner_account_id, bound_owner)
        session_id = _require_owned_session(crew, session_id, owner)
        goal = _require_text(goal, "goal", maximum=_MCP_MAX_PLAN_CHARS)
        if not isinstance(nodes, list) or not isinstance(edges, (list, type(None))):
            raise ValueError("MCP team plan 列表无效")
        if len(nodes) > _MCP_MAX_LIST_ITEMS or len(edges or []) > _MCP_MAX_LIST_ITEMS:
            raise ValueError("MCP team plan 列表超限")
        if external_team_id:
            external_team_id = _require_identifier(external_team_id, "external_team_id")
        if crew.team is None:
            return _safe_failure("TEAM_UNAVAILABLE")
        fn = getattr(crew.team, "create_plan", None)
        if not callable(fn):
            return _safe_failure("TEAM_UNAVAILABLE")
        try:
            result = fn(
                session_id,
                goal=goal,
                nodes=nodes,
                edges=edges or [],
                external_team_id=external_team_id,
                owner_account_id=owner,
            )
        except Exception:  # noqa: BLE001
            log.exception("MCP team plan create 失败")
            return _safe_failure()
        return _bounded_json(result)

    @mcp.tool()
    async def team_plan_read(session_id: str, owner_account_id: str) -> str:
        """Read the current TeamPlan for a Crew team session. Returns JSON."""
        owner = _require_owner(owner_account_id, bound_owner)
        session_id = _require_owned_session(crew, session_id, owner)
        if crew.team is None:
            return _safe_failure("TEAM_UNAVAILABLE")
        fn = getattr(crew.team, "read_plan", None)
        if not callable(fn):
            return _safe_failure("TEAM_UNAVAILABLE")
        try:
            result = fn(
                session_id,
                owner_account_id=owner,
            )
        except Exception:  # noqa: BLE001
            log.exception("MCP team plan read 失败")
            return _safe_failure()
        return _bounded_json(result)

    @mcp.tool()
    async def team_plan_update(
        session_id: str,
        owner_account_id: str,
        node_id: str,
        status: str = "",
        result_summary: str = "",
        artifact_refs: list[str] | None = None,
        delegate_task_id: str = "",
    ) -> str:
        """Update one TeamPlan node status/result/artifacts. Returns JSON."""
        owner = _require_owner(owner_account_id, bound_owner)
        session_id = _require_owned_session(crew, session_id, owner)
        node_id = _require_identifier(node_id, "node_id")
        if status:
            status = _require_identifier(status, "status")
        if result_summary:
            result_summary = _require_text(
                result_summary,
                "result_summary",
                maximum=_MCP_MAX_PLAN_CHARS,
            )
        if not isinstance(artifact_refs, (list, type(None))):
            raise ValueError("MCP artifact_refs 列表无效")
        if len(artifact_refs or []) > _MCP_MAX_LIST_ITEMS:
            raise ValueError("MCP artifact_refs 列表超限")
        artifact_refs = [
            _require_identifier(item, "artifact_ref", maximum=4096)
            for item in (artifact_refs or [])
        ]
        if delegate_task_id:
            delegate_task_id = _require_identifier(delegate_task_id, "delegate_task_id")
        if crew.team is None:
            return _safe_failure("TEAM_UNAVAILABLE")
        fn = getattr(crew.team, "update_plan_node", None)
        if not callable(fn):
            return _safe_failure("TEAM_UNAVAILABLE")
        try:
            result = fn(
                session_id,
                node_id=node_id,
                status=status or None,
                result_summary=result_summary if result_summary else None,
                artifact_refs=artifact_refs or None,
                delegate_task_id=delegate_task_id if delegate_task_id else None,
                owner_account_id=owner,
            )
        except Exception:  # noqa: BLE001
            log.exception("MCP team plan update 失败")
            return _safe_failure()
        return _bounded_json(result)

    _harden_mcp_tool_schemas(mcp)
    return mcp


def _post_internal(gateway_url: str, token: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        raise RuntimeError("Crew Gateway 请求无效") from None
    if len(body) > _MCP_MAX_RESULT_BYTES:
        raise RuntimeError("Crew Gateway 请求超出协议限制")
    if not str(path).startswith("/api/internal/"):
        raise RuntimeError("Crew Gateway 请求路径无效")
    try:
        _parsed, base = _INTERNAL_POLICY.canonicalize_url(
            gateway_url,
            method="POST",
            allowed_schemes=frozenset({"http"}),
        )
        if base.path != "/" or base.query:
            raise OutboundDenied("internal_gateway_origin_invalid")
        try:
            address = ipaddress.ip_address(base.host)
        except ValueError:
            if base.host != "localhost":
                raise OutboundDenied("internal_gateway_not_loopback") from None
        else:
            mapped = (
                address.ipv4_mapped
                if isinstance(address, ipaddress.IPv6Address)
                else None
            )
            if not address.is_loopback and not (mapped and mapped.is_loopback):
                raise OutboundDenied("internal_gateway_not_loopback")
        context = OutboundContext(
            owner=hashlib.sha256(str(token).encode("utf-8")).hexdigest(),
            session="interaction-proxy",
            task="gateway-callback",
            request=secrets.token_hex(16),
            source="mcp-interaction-proxy",
        )
        grant = _INTERNAL_GRANTS.issue_private(
            context,
            host=base.host,
            port=base.port,
            scheme=base.scheme,
            method="POST",
        )
        plan = _INTERNAL_POLICY.plan_url(
            f"{base.scheme}://{base.authority}{path}",
            method="POST",
            context=context,
            private_grant=grant,
        )
        response = _INTERNAL_HTTP.fetch_plan(
            plan,
            method="POST",
            body=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=310.0,
            max_bytes=_MCP_MAX_RESULT_BYTES,
            max_request_bytes=_MCP_MAX_RESULT_BYTES,
            context=context,
        )
        if not 200 <= response.status < 300:
            log.warning(
                "Crew Gateway HTTP 请求失败 status=%s path=%s",
                response.status,
                path,
            )
            raise RuntimeError("Crew Gateway 请求失败")
        result = json.loads(response.body.decode(response.charset))
        if not isinstance(result, dict):
            raise RuntimeError("Crew Gateway 响应无效")
        return result
    except OutboundDenied as exc:
        log.warning(
            "Crew Gateway 网络策略拒绝 reason=%s path=%s",
            exc.code,
            path,
        )
        raise RuntimeError("无法连接 Crew Gateway") from None
    except (UnicodeError, json.JSONDecodeError):
        raise RuntimeError("Crew Gateway 响应无效") from None


def _post_interaction(gateway_url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    return _post_internal(gateway_url, token, "/api/internal/interactions/ask", payload)


def build_interaction_mcp_server(
    gateway_url: str,
    token: str,
    *,
    context_type: str = "standalone",
    team_role: str = "",
) -> Any:
    """按服务端 Binding 场景构建最小 External Interaction MCP 工具面。"""
    if not gateway_url or not token:
        raise ValueError("interaction proxy 缺少 Gateway URL 或交互令牌")
    if context_type not in {"standalone", "team"}:
        raise ValueError(f"未知 interaction context: {context_type}")
    if context_type == "team" and team_role not in {"leader", "member"}:
        raise ValueError(f"未知 team role: {team_role or '<empty>'}")

    mcp = _new_mcp_server("crew-interaction")

    if context_type == "standalone" or team_role == "leader":
        @mcp.tool()
        async def ask_followup_question(
            questions: list[dict[str, Any]],
            title: str = "",
        ) -> str:
            """Collect structured answers only when required information cannot be inferred."""
            if (
                not isinstance(questions, list)
                or len(questions) > _MCP_MAX_LIST_ITEMS
                or any(not isinstance(item, dict) for item in questions)
            ):
                raise ValueError("MCP questions 列表无效")
            title = _require_text(title, "title", maximum=256, allow_empty=True)
            response = await asyncio.to_thread(
                _post_interaction,
                gateway_url,
                token,
                {"title": title, "questions": questions},
            )
            if not response.get("ok"):
                return _safe_failure("INTERACTION_FAILED")
            return _bounded_json(response.get("result") or {})

    if context_type == "team":
        @mcp.tool()
        async def team_mention(
            to: list[str],
            intent: str,
            content: str,
            node_id: str = "",
            result_status: str = "",
            artifact_refs: list[str] | None = None,
            questions: list[dict[str, Any]] | None = None,
            title: str = "",
        ) -> str:
            """Send a governed mention inside the current Crew Team.

            Identity and role come from the server-side binding. Members use
            submit/review/ask/handoff; submit requires result_status
            pass/fail/blocked. Only the Leader may assign or contact user.
            """
            if not isinstance(to, list) or len(to) > _MCP_MAX_LIST_ITEMS:
                raise ValueError("MCP to 列表无效")
            to = [_require_identifier(item, "to") for item in to]
            intent = _require_identifier(intent, "intent")
            content = _require_text(content, "content", maximum=_MCP_MAX_PLAN_CHARS)
            if node_id:
                node_id = _require_identifier(node_id, "node_id")
            if result_status:
                result_status = _require_identifier(result_status, "result_status")
            if not isinstance(artifact_refs, (list, type(None))) or len(artifact_refs or []) > _MCP_MAX_LIST_ITEMS:
                raise ValueError("MCP artifact_refs 列表无效")
            artifact_refs = [
                _require_identifier(item, "artifact_ref", maximum=4096)
                for item in (artifact_refs or [])
            ]
            if (
                not isinstance(questions, (list, type(None)))
                or len(questions or []) > _MCP_MAX_LIST_ITEMS
                or any(not isinstance(item, dict) for item in (questions or []))
            ):
                raise ValueError("MCP questions 列表无效")
            title = _require_text(title, "title", maximum=256, allow_empty=True)
            response = await asyncio.to_thread(
                _post_internal,
                gateway_url,
                token,
                "/api/internal/team/mention",
                {
                    "to": to,
                    "intent": intent,
                    "content": content,
                    "node_id": node_id,
                    "result_status": result_status,
                    "artifact_refs": artifact_refs or [],
                    "questions": questions or [],
                    "title": title,
                },
            )
            return _bounded_json(response)

        @mcp.tool()
        async def team_plan_read() -> str:
            """Read the TeamPlan DAG for the current Crew team session."""
            response = await asyncio.to_thread(
                _post_internal,
                gateway_url,
                token,
                "/api/internal/team/plan/read",
                {},
            )
            return _bounded_json(response)

        @mcp.tool()
        async def team_plan_update(
            node_id: str,
            status: str = "",
            result_summary: str = "",
            artifact_refs: list[str] | None = None,
            delegate_task_id: str = "",
        ) -> str:
            """Update TeamPlan; members are restricted to their own assigned node."""
            node_id = _require_identifier(node_id, "node_id")
            if status:
                status = _require_identifier(status, "status")
            if result_summary:
                result_summary = _require_text(
                    result_summary,
                    "result_summary",
                    maximum=_MCP_MAX_PLAN_CHARS,
                )
            if not isinstance(artifact_refs, (list, type(None))) or len(artifact_refs or []) > _MCP_MAX_LIST_ITEMS:
                raise ValueError("MCP artifact_refs 列表无效")
            artifact_refs = [
                _require_identifier(item, "artifact_ref", maximum=4096)
                for item in (artifact_refs or [])
            ]
            if delegate_task_id:
                delegate_task_id = _require_identifier(delegate_task_id, "delegate_task_id")
            response = await asyncio.to_thread(
                _post_internal,
                gateway_url,
                token,
                "/api/internal/team/plan/update",
                {
                    "node_id": node_id,
                    "status": status,
                    "result_summary": result_summary,
                    "artifact_refs": artifact_refs or [],
                    "delegate_task_id": delegate_task_id,
                },
            )
            return _bounded_json(response)


    if context_type == "team" and team_role == "leader":
        @mcp.tool()
        async def team_plan_create(
            goal: str,
            nodes: list[dict[str, Any]],
            edges: list[Any] | None = None,
        ) -> str:
            """Create or replace the TeamPlan DAG for the current Crew team session."""
            goal = _require_text(goal, "goal", maximum=_MCP_MAX_PLAN_CHARS)
            if (
                not isinstance(nodes, list)
                or not isinstance(edges, (list, type(None)))
                or len(nodes) > _MCP_MAX_LIST_ITEMS
                or len(edges or []) > _MCP_MAX_LIST_ITEMS
            ):
                raise ValueError("MCP team plan 列表无效")
            response = await asyncio.to_thread(
                _post_internal,
                gateway_url,
                token,
                "/api/internal/team/plan/create",
                {"goal": goal, "nodes": nodes, "edges": edges or []},
            )
            return _bounded_json(response)

    _harden_mcp_tool_schemas(mcp)
    return mcp


def serve(crew: CrewApp | None = None) -> None:
    """启动 MCP server（stdio）。供 `crew mcp serve` 调用。"""
    from crew.app import build_app

    crew = crew or build_app()
    server = build_mcp_server(crew)
    log.info("Crew MCP Server 启动（stdio）")
    server.run()


def serve_interaction_proxy() -> None:
    """启动外部 Runtime 专属的最小 Interaction MCP proxy（stdio）。"""
    gateway_url = os.environ.get("CREW_GATEWAY_URL", "")
    token = os.environ.get("CREW_INTERACTION_TOKEN", "")
    context_type = os.environ.get("CREW_INTERACTION_CONTEXT", "standalone")
    team_role = os.environ.get("CREW_INTERACTION_TEAM_ROLE", "")
    server = build_interaction_mcp_server(
        gateway_url,
        token,
        context_type=context_type,
        team_role=team_role,
    )
    log.info("Crew Interaction MCP Proxy 启动（stdio）")
    server.run()
