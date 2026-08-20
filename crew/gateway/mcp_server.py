"""MCP Server：把 Crew 的会话能力对外暴露成 MCP tools。

让任何标准 MCP 客户端都能列会话、读历史、发消息。
用官方 MCPServer + @mcp.tool()。

运行：``crew mcp serve``（stdio）。
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from typing import Any

from crew import __version__
from crew.app import CrewApp
from crew.core.envelope import Envelope
from crew.state.logging import get_logger

log = get_logger("gateway.mcp")


def _require_owner(owner_account_id: str) -> str:
    owner = str(owner_account_id or "").strip()
    if not owner:
        raise ValueError("MCP 调用必须传 owner_account_id")
    return owner


def build_mcp_server(crew: CrewApp) -> Any:
    """构建并返回 MCPServer。未安装 mcp 包则抛 ImportError。"""
    from mcp.server import MCPServer

    mcp = MCPServer("crew", version=__version__)

    @mcp.tool()
    async def sessions_list(owner_account_id: str, workspace_id: str | None = None) -> str:
        """列出会话（可选按 workspace 过滤）。返回 JSON。"""
        owner = _require_owner(owner_account_id)
        # list_sessions 走 SQLite 阻塞 I/O，丢到线程池避免阻塞事件循环。
        rows = await asyncio.to_thread(
            crew.session_store.list_sessions,
            workspace_id,
            owner,
        )
        return json.dumps(rows, ensure_ascii=False)

    @mcp.tool()
    async def session_history(session_id: str, owner_account_id: str) -> str:
        """读取某会话的对话历史（user/assistant 文本 + 工具调用名）。返回 JSON。"""
        owner = _require_owner(owner_account_id)

        def _build() -> list[dict[str, Any]]:
            items: list[dict[str, Any]] = []
            for m in crew.session_store.load(session_id, owner_account_id=owner):
                entry: dict[str, Any] = {"role": m.role, "content": m.content}
                if m.tool_calls:
                    entry["tool_calls"] = [tc.name for tc in m.tool_calls]
                items.append(entry)
            return items
        items = await asyncio.to_thread(_build)
        return json.dumps(items, ensure_ascii=False)

    @mcp.tool()
    async def send_message(session_id: str, query: str, owner_account_id: str) -> str:
        """向某会话发送一条消息，跑完一轮对话并返回最终文本。"""
        owner = _require_owner(owner_account_id)
        envelope = Envelope.of(query, session_id=session_id, user_id=owner, channel="mcp")
        final_text = ""
        error = ""
        # 走 dispatch（经 SessionDispatcher）：与 WS/飞书一致，获得 per-session 串行、
        # 忙时策略、agent:start/end hook 与状态落库；直接 crew.handle 会绕过这些。
        async for chunk in crew.dispatch(envelope):
            if chunk.kind == "final":
                final_text = chunk.body.get("text", "")
            elif chunk.kind == "error":
                error = chunk.body.get("message", "")
        if error:
            return json.dumps({"ok": False, "error": error}, ensure_ascii=False)
        return json.dumps({"ok": True, "text": final_text}, ensure_ascii=False)

    @mcp.tool()
    async def session_status(session_id: str, owner_account_id: str) -> str:
        """查询某会话上一轮运行结果（last_status / last_error）。返回 JSON。"""
        owner = _require_owner(owner_account_id)
        last_status, last_error = await asyncio.to_thread(
            crew.session_store.get_status,
            session_id,
            owner,
        )
        return json.dumps(
            {"session_id": session_id, "last_status": last_status, "last_error": last_error},
            ensure_ascii=False,
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
        if crew.team is None:
            return json.dumps({"ok": False, "error": "Team 模式未启用"}, ensure_ascii=False)
        fn = getattr(crew.team, "request_delegate", None)
        if not callable(fn):
            return json.dumps({"ok": False, "error": "当前 TeamManager 不支持 request_delegate"}, ensure_ascii=False)
        try:
            owner = _require_owner(owner_account_id)
            result = await fn(
                session_id,
                member=member,
                instruction=instruction,
                requester_member_id=requester_member_id,
                external_team_id=external_team_id,
                plan_node_id=plan_node_id,
                owner_account_id=owner,
            )
        except Exception as exc:  # noqa: BLE001 - MCP 工具返回结构化错误
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
        return json.dumps(result, ensure_ascii=False)

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
        if crew.team is None:
            return json.dumps({"ok": False, "error": "Team 模式未启用"}, ensure_ascii=False)
        fn = getattr(crew.team, "create_plan", None)
        if not callable(fn):
            return json.dumps({"ok": False, "error": "当前 TeamManager 不支持 create_plan"}, ensure_ascii=False)
        try:
            owner = _require_owner(owner_account_id)
            result = fn(
                session_id,
                goal=goal,
                nodes=nodes,
                edges=edges or [],
                external_team_id=external_team_id,
                owner_account_id=owner,
            )
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
        return json.dumps(result, ensure_ascii=False)

    @mcp.tool()
    async def team_plan_read(session_id: str, owner_account_id: str) -> str:
        """Read the current TeamPlan for a Crew team session. Returns JSON."""
        if crew.team is None:
            return json.dumps({"ok": False, "error": "Team 模式未启用"}, ensure_ascii=False)
        fn = getattr(crew.team, "read_plan", None)
        if not callable(fn):
            return json.dumps({"ok": False, "error": "当前 TeamManager 不支持 read_plan"}, ensure_ascii=False)
        try:
            result = fn(session_id, owner_account_id=_require_owner(owner_account_id))
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
        return json.dumps(result, ensure_ascii=False)

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
        if crew.team is None:
            return json.dumps({"ok": False, "error": "Team 模式未启用"}, ensure_ascii=False)
        fn = getattr(crew.team, "update_plan_node", None)
        if not callable(fn):
            return json.dumps({"ok": False, "error": "当前 TeamManager 不支持 update_plan_node"}, ensure_ascii=False)
        try:
            owner = _require_owner(owner_account_id)
            result = fn(
                session_id,
                node_id=node_id,
                status=status or None,
                result_summary=result_summary if result_summary else None,
                artifact_refs=artifact_refs,
                delegate_task_id=delegate_task_id if delegate_task_id else None,
                owner_account_id=owner,
            )
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
        return json.dumps(result, ensure_ascii=False)

    return mcp


def _post_internal(gateway_url: str, token: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{gateway_url.rstrip('/')}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=310) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Crew Gateway 返回 {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接 Crew Gateway: {exc.reason}") from exc


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
    from mcp.server import MCPServer

    if not gateway_url or not token:
        raise ValueError("interaction proxy 缺少 Gateway URL 或交互令牌")
    if context_type not in {"standalone", "team"}:
        raise ValueError(f"未知 interaction context: {context_type}")
    if context_type == "team" and team_role not in {"leader", "member"}:
        raise ValueError(f"未知 team role: {team_role or '<empty>'}")

    mcp = MCPServer("crew-interaction", version=__version__)

    if context_type == "standalone" or team_role == "leader":
        @mcp.tool()
        async def ask_followup_question(
            questions: list[dict[str, Any]],
            title: str = "",
        ) -> str:
            """Collect structured answers only when required information cannot be inferred."""
            response = await asyncio.to_thread(
                _post_interaction,
                gateway_url,
                token,
                {"title": title, "questions": questions},
            )
            if not response.get("ok"):
                raise RuntimeError(str(response.get("error") or "Crew 追问失败"))
            return json.dumps(response.get("result") or {}, ensure_ascii=False)

    if context_type == "team":
        @mcp.tool()
        async def team_mention(
            to: list[str],
            intent: str,
            content: str,
            node_id: str = "",
            task_id: str = "",
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
                    "task_id": task_id,
                    "result_status": result_status,
                    "artifact_refs": artifact_refs or [],
                    "questions": questions or [],
                    "title": title,
                },
            )
            return json.dumps(response, ensure_ascii=False)

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
            return json.dumps(response, ensure_ascii=False)

        @mcp.tool()
        async def team_plan_update(
            node_id: str,
            status: str = "",
            result_summary: str = "",
            artifact_refs: list[str] | None = None,
            delegate_task_id: str = "",
        ) -> str:
            """Update TeamPlan; members are restricted to their own assigned node."""
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
            return json.dumps(response, ensure_ascii=False)


    if context_type == "team" and team_role == "leader":
        @mcp.tool()
        async def team_plan_create(
            goal: str,
            nodes: list[dict[str, Any]],
            edges: list[Any] | None = None,
        ) -> str:
            """Create or replace the TeamPlan DAG for the current Crew team session."""
            response = await asyncio.to_thread(
                _post_internal,
                gateway_url,
                token,
                "/api/internal/team/plan/create",
                {"goal": goal, "nodes": nodes, "edges": edges or []},
            )
            return json.dumps(response, ensure_ascii=False)

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
