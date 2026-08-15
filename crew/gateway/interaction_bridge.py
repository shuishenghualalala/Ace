"""外部 Runtime 的受限 MCP 交互桥。

Gateway 进程持有前端连接和 FollowupWaiter；注入 Runtime 的 MCP Server 是独立子进程。
本模块用短期 token 把 MCP 请求安全地映射回发起任务的 Crew 主会话。
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from crew.agent.skills import SkillActivation
from crew.core.errors import ToolError
from crew.core.followup import CANCELLED_MARKER, send_followup_question_to, wait_for_answer
from crew.core.runctx import PushFn, current_owner_account_id
from crew.state.logging import get_logger
from crew.team.delegate_tool import TEAM_RESULT_STATUSES, require_team_result_status
from crew.gateway.helpers import safe_public_error

log = get_logger("interaction_bridge")


@dataclass(frozen=True)
class ExternalInteractionBinding:
    token: str
    owner_account_id: str
    display_session_id: str
    control_session_id: str
    origin_session_id: str
    agent_name: str
    context_type: Literal["standalone", "team"]
    team_session_id: str
    member_id: str
    team_role: Literal["", "leader", "member"]
    cwd: str
    active_skills: tuple[SkillActivation, ...]
    expires_at: float


class InteractionBridge:
    """管理外部 Runtime 与 Crew 主会话之间的短期绑定。"""

    def __init__(self) -> None:
        self._bindings: dict[str, ExternalInteractionBinding] = {}
        self._permission_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._push_fn: PushFn | None = None
        self._gateway_url = ""
        self._crew: Any | None = None

    def configure(self, *, push_fn: PushFn, gateway_url: str, crew: Any | None = None) -> None:
        self.clear()
        self._push_fn = push_fn
        self._gateway_url = gateway_url.rstrip("/")
        self._crew = crew

    @property
    def available(self) -> bool:
        return self._push_fn is not None and bool(self._gateway_url)

    def bind_crew(self, crew: Any | None) -> None:
        """Attach the in-process Crew control plane used by governed tools."""
        self._crew = crew

    def create_binding(
        self,
        *,
        owner_account_id: str | None = None,
        display_session_id: str,
        control_session_id: str = "",
        origin_session_id: str,
        agent_name: str,
        ttl_seconds: float,
        context_type: Literal["standalone", "team"] = "standalone",
        team_session_id: str = "",
        member_id: str = "",
        team_role: Literal["", "leader", "member"] = "",
        cwd: str = "",
        active_skills: tuple[SkillActivation, ...] | list[SkillActivation] = (),
    ) -> ExternalInteractionBinding | None:
        owner = str(owner_account_id or current_owner_account_id.get() or "").strip()
        if not self.available or not owner:
            return None
        if context_type not in {"standalone", "team"}:
            raise ValueError(f"未知 ExternalInteractionBinding 场景: {context_type}")
        if context_type == "team":
            if not team_session_id or not member_id or team_role not in {"leader", "member"}:
                raise ValueError("Team ExternalInteractionBinding 缺少 team_session_id/member_id/team_role")
        else:
            team_session_id = ""
            member_id = ""
            team_role = ""
        self._purge_expired()
        token = secrets.token_urlsafe(32)
        binding = ExternalInteractionBinding(
            token=token,
            owner_account_id=owner,
            display_session_id=display_session_id,
            control_session_id=control_session_id or display_session_id,
            origin_session_id=origin_session_id,
            agent_name=agent_name,
            context_type=context_type,
            team_session_id=team_session_id,
            member_id=member_id,
            team_role=team_role,
            cwd=str(cwd or "").strip(),
            active_skills=tuple(active_skills),
            expires_at=time.time() + max(1.0, ttl_seconds),
        )
        self._bindings[token] = binding
        return binding

    def remove_binding(self, token: str) -> None:
        self._bindings.pop(token, None)

    def remove_owner(self, owner_account_id: str) -> int:
        """Revoke every short-lived callback binding when its Owner starts logout."""
        owner = str(owner_account_id or "").strip()
        tokens = [
            token
            for token, binding in self._bindings.items()
            if binding.owner_account_id == owner
        ]
        for token in tokens:
            self._bindings.pop(token, None)
        return len(tokens)

    def resolve_binding(self, token: str) -> ExternalInteractionBinding | None:
        binding = self._bindings.get(token)
        if binding is not None and binding.expires_at <= time.time():
            self._bindings.pop(token, None)
            log.warning(
                "交互绑定已过期 owner=%s session=%s origin=%s",
                binding.owner_account_id,
                binding.display_session_id,
                binding.origin_session_id,
            )
            return None
        self._purge_expired()
        return binding

    def mcp_server_config(self, binding: ExternalInteractionBinding) -> dict[str, Any]:
        if getattr(sys, "frozen", False):
            args = ["mcp", "interaction-proxy"]
        else:
            args = ["-m", "crew.cli", "mcp", "interaction-proxy"]
        return {
            "name": "crew-interaction",
            "command": sys.executable,
            "args": args,
            # Kimi/Hermes ACP 的 McpServerStdio schema 要求 list[{name,value}]。
            "env": [
                {"name": "CREW_GATEWAY_URL", "value": self._gateway_url},
                {"name": "CREW_INTERACTION_TOKEN", "value": binding.token},
                {"name": "CREW_INTERACTION_CONTEXT", "value": binding.context_type},
                {"name": "CREW_INTERACTION_TEAM_ROLE", "value": binding.team_role},
            ],
        }

    @classmethod
    def dynamic_tool_specs(cls, binding: ExternalInteractionBinding) -> list[dict[str, Any]]:
        """Return the same governed Crew control surface for app-server clients.

        Codex app-server treats injected MCP tools as user-approved app tools and
        rejects them before the MCP proxy can enforce its binding. Dynamic tools
        use the app-server's explicit client callback while keeping all business
        authorization in :meth:`invoke_tool`.
        """
        tools: list[dict[str, Any]] = []
        if binding.context_type == "standalone" or binding.team_role == "leader":
            tools.append({
                "type": "function",
                "name": "ask_followup_question",
                "description": "Collect structured answers only when required information cannot be inferred.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "questions": {
                            "type": "array",
                            "items": {"type": "object", "additionalProperties": True},
                        },
                    },
                    "required": ["questions"],
                    "additionalProperties": False,
                },
            })
        if binding.context_type == "team":
            mention_intents = (
                ["assign", "submit", "review", "ask", "broadcast", "handoff"]
                if binding.team_role == "leader"
                else ["submit", "review", "ask", "handoff"]
            )
            tools.extend([
                {
                    "type": "function",
                    "name": "team_mention",
                    "description": (
                        "Send a governed mention inside the current Crew Team. "
                        "Use exactly one of the intent values allowed by the schema."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "to": {"type": "array", "items": {"type": "string"}},
                            "intent": {"type": "string", "enum": mention_intents},
                            "content": {"type": "string"},
                            "node_id": {"type": "string"},
                            "result_status": {
                                "type": "string",
                                "enum": list(TEAM_RESULT_STATUSES),
                                "description": "Required for submit: structured node acceptance status.",
                            },
                            "artifact_refs": {"type": "array", "items": {"type": "string"}},
                            "questions": {"type": "array", "items": {"type": "object"}},
                            "title": {"type": "string"},
                        },
                        "required": ["to", "intent", "content"],
                        "additionalProperties": False,
                    },
                },
                {
                    "type": "function",
                    "name": "team_plan_read",
                    "description": "Read the TeamPlan DAG for the current Crew team session.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                },
                {
                    "type": "function",
                    "name": "team_plan_update",
                    "description": "Update TeamPlan; members are restricted to their own assigned node.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "node_id": {"type": "string"},
                            "status": {"type": "string"},
                            "result_summary": {"type": "string"},
                            "artifact_refs": {"type": "array", "items": {"type": "string"}},
                            "delegate_task_id": {"type": "string"},
                        },
                        "required": ["node_id"],
                        "additionalProperties": False,
                    },
                },
            ])
        if binding.context_type == "team" and binding.team_role == "leader":
            tools.append({
                "type": "function",
                "name": "team_plan_create",
                "description": "Create or replace the TeamPlan DAG for the current Crew team session.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string"},
                        "nodes": {"type": "array", "items": {"type": "object"}},
                        "edges": {"type": "array", "items": {}},
                    },
                    "required": ["goal", "nodes"],
                    "additionalProperties": False,
                },
            })
        if not tools:
            return []
        # 平铺返回，不再包 namespace：新版 codex app-server 的 dynamicTools
        # 只接受 function 项（每项必须有 inputSchema，名称匹配 ^[a-zA-Z0-9_-]+$），
        # namespace 包装会被直接拒绝（missing field `inputSchema`）。
        return tools

    def _active_binding(self, token: str) -> ExternalInteractionBinding:
        binding = self.resolve_binding(token)
        if binding is None:
            raise PermissionError("交互绑定不存在或已过期")
        coordinator = getattr(self._crew, "logout_coordinator", None)
        allows_work = getattr(coordinator, "allows_work", None)
        if callable(allows_work) and not allows_work(binding.owner_account_id):
            raise PermissionError("交互令牌所属账号已退出或不再是 Active Owner")
        return binding

    async def invoke_tool(
        self,
        token: str,
        tool_name: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Invoke one bound Crew control tool for MCP proxy or app-server."""
        binding = self._active_binding(token)
        data = dict(payload or {})
        name = str(tool_name or "").rsplit(".", 1)[-1].strip()
        crew = self._crew

        if name == "ask_followup_question":
            if binding.context_type == "team" and binding.team_role != "leader":
                raise PermissionError("Team Member 不能直接询问用户，请通过 team_mention 请求 Leader")
            questions = data.get("questions")
            if not isinstance(questions, list) or not questions:
                raise ValueError("questions 必须是非空数组")
            result = await self.ask(
                token,
                title=str(data.get("title") or "").strip(),
                questions=questions,
            )
            return {"ok": True, "result": result}

        if binding.context_type != "team":
            raise PermissionError("当前 Binding 不属于 Team")
        team = getattr(crew, "team", None)
        if team is None:
            raise ValueError("Team 模式未启用")

        if name == "team_mention":
            intent = str(data.get("intent") or "broadcast").strip().lower()
            result_status = require_team_result_status(intent, data.get("result_status"))
            leader_intents = {"assign", "submit", "review", "ask", "broadcast", "handoff"}
            member_intents = {"submit", "review", "ask", "handoff"}
            allowed = leader_intents if binding.team_role == "leader" else member_intents
            if intent not in allowed:
                raise PermissionError(f"{binding.team_role or 'unknown'} 不允许 team_mention({intent})")
            targets = [str(item).strip() for item in list(data.get("to") or []) if str(item).strip()]
            if not targets:
                raise ValueError("to 不能为空")
            if "user" in targets and binding.team_role != "leader":
                raise PermissionError("只有 Leader 可以联系用户")
            content = str(data.get("content") or "").strip()
            if not content:
                raise ValueError("content 不能为空")
            fn = getattr(team, "external_team_mention", None)
            if not callable(fn):
                raise ValueError("当前 TeamManager 不支持 team_mention")
            result = await fn(
                binding.team_session_id,
                member_id=binding.member_id,
                to=targets,
                intent=intent,
                content=content,
                node_id=str(data.get("node_id") or "").strip(),
                result_status=result_status,
                artifacts=list(data.get("artifact_refs") or []),
                questions=list(data.get("questions") or []),
                title=str(data.get("title") or "").strip(),
                task_payload_meta=(
                    {"active_skills": [skill.to_dict() for skill in binding.active_skills]}
                    if binding.active_skills
                    else None
                ),
                owner_account_id=binding.owner_account_id,
            )
            return {"ok": True, "result": result}

        if name == "team_plan_create":
            if binding.team_role != "leader":
                raise PermissionError("只有 Team Leader 可以创建 TeamPlan")
            fn = getattr(team, "create_plan", None)
            if not callable(fn):
                raise ValueError("当前 TeamManager 不支持 create_plan")
            return fn(
                binding.team_session_id,
                goal=str(data.get("goal") or "").strip(),
                nodes=list(data.get("nodes") or []),
                edges=list(data.get("edges") or []),
                owner_account_id=binding.owner_account_id,
            )

        if name == "team_plan_read":
            fn = getattr(team, "read_plan", None)
            if not callable(fn):
                raise ValueError("当前 TeamManager 不支持 read_plan")
            return fn(binding.team_session_id, owner_account_id=binding.owner_account_id)

        if name == "team_plan_update":
            fn = getattr(team, "update_plan_node", None)
            if not callable(fn):
                raise ValueError("当前 TeamManager 不支持 update_plan_node")
            node_id = str(data.get("node_id") or "").strip()
            if binding.team_role == "member":
                read_plan = getattr(team, "read_plan", None)
                if not callable(read_plan):
                    raise ValueError("当前 TeamManager 不支持 read_plan")
                current = read_plan(binding.team_session_id, owner_account_id=binding.owner_account_id)
                plan = current.get("plan") if isinstance(current, dict) else None
                nodes = plan.get("nodes") if isinstance(plan, dict) else None
                node = (
                    nodes.get(node_id)
                    if isinstance(nodes, dict)
                    else next(
                        (
                            item
                            for item in (nodes or [])
                            if isinstance(item, dict)
                            and str(item.get("id") or item.get("node_id") or "") == node_id
                        ),
                        None,
                    )
                )
                assignee = str((node or {}).get("assignee") or (node or {}).get("assignee_id") or "")
                if not node or assignee != binding.member_id:
                    raise PermissionError("Team Member 只能更新分配给自己的节点")
            return fn(
                binding.team_session_id,
                node_id=node_id,
                status=str(data.get("status") or "").strip() or None,
                result_summary=(
                    str(data.get("result_summary")) if data.get("result_summary") is not None else None
                ),
                artifact_refs=list(data.get("artifact_refs") or []),
                delegate_task_id=str(data.get("delegate_task_id") or "").strip() or None,
                owner_account_id=binding.owner_account_id,
            )

        raise ValueError(f"未知 Crew Interaction 工具: {name or '<empty>'}")

    async def invoke_tool_json(
        self,
        token: str,
        tool_name: str,
        payload: dict[str, Any] | None = None,
    ) -> str:
        return json.dumps(
            await self.invoke_tool(token, tool_name, payload),
            ensure_ascii=False,
        )

    async def ask(
        self,
        token: str,
        *,
        title: str,
        questions: list[dict[str, Any]],
        origin: dict[str, Any] | None = None,
        record_history: bool = True,
    ) -> dict[str, Any]:
        binding = self.resolve_binding(token)
        if binding is None:
            raise PermissionError("交互绑定不存在或已过期")
        if self._push_fn is None:
            raise RuntimeError("Gateway 交互推送尚未初始化")

        push_fn: PushFn | Callable[[str, dict[str, Any]], Awaitable[None]]
        if binding.owner_account_id:
            async def push_fn(session_id: str, payload: dict[str, Any]) -> None:
                await self._push_fn(session_id, payload, owner_account_id=binding.owner_account_id)  # type: ignore[call-arg]
        else:
            push_fn = self._push_fn

        session_id, question_id = await send_followup_question_to(
            binding.display_session_id,
            questions,
            title=title,
            origin=origin or {
                "type": "external_runtime",
                "agent_name": binding.agent_name,
                "origin_session_id": binding.origin_session_id,
            },
            push_fn=push_fn,
            record_history=record_history,
        )
        answers = await wait_for_answer(session_id, question_id)
        cancelled = bool(answers) and answers[0].get("id") == CANCELLED_MARKER
        if cancelled:
            return {
                "success": False,
                "question_id": question_id,
                "answers": [],
                "note": "用户已取消选择。",
            }
        result: dict[str, Any] = {
            "success": True,
            "question_id": question_id,
            "answers": answers,
        }
        if not answers:
            result["note"] = "用户未在超时时间内回答"
        return result

    async def ask_permission(
        self,
        token: str,
        *,
        title: str,
        question: str,
        display_name: str,
        origin_type: str,
    ) -> bool:
        """Ask one serialized, history-free operational permission question."""
        binding = self.resolve_binding(token)
        if binding is None:
            log.warning("权限请求未找到有效交互绑定，已安全拒绝")
            return False
        key = (binding.owner_account_id, binding.display_session_id)
        lock = self._permission_locks.setdefault(key, asyncio.Lock())
        async with lock:
            result = await self.ask(
                token,
                title=title,
                questions=[{
                    "id": "permission",
                    "question": question,
                    "options": [
                        {"label": "允许本次操作", "value": "allow_once"},
                        {"label": "拒绝操作", "value": "deny"},
                    ],
                    "allowFreeText": False,
                    "multiSelect": False,
                }],
                origin={
                    "type": origin_type,
                    "agent_name": display_name,
                    "origin_session_id": binding.origin_session_id,
                },
                record_history=False,
            )
        if not result.get("success"):
            log.warning(
                "权限交互未成功 session=%s note=%s",
                binding.display_session_id,
                result.get("note") or "",
            )
            return False
        for answer in result.get("answers") or []:
            values = answer.get("answers") if isinstance(answer, dict) else None
            if isinstance(values, list) and "allow_once" in values:
                log.info("权限交互已允许本次操作 session=%s", binding.display_session_id)
                return True
        log.info("权限交互已拒绝操作 session=%s", binding.display_session_id)
        return False

    def clear(self) -> None:
        self._bindings.clear()
        self._permission_locks.clear()

    def _purge_expired(self) -> None:
        now = time.time()
        expired = [token for token, item in self._bindings.items() if item.expires_at <= now]
        for token in expired:
            self._bindings.pop(token, None)


interaction_bridge = InteractionBridge()


def create_interaction_router(bridge: InteractionBridge = interaction_bridge, crew: Any | None = None) -> APIRouter:
    router = APIRouter()
    bridge.bind_crew(crew)

    def authorized_binding(
        request: Request,
        *,
        local_only_error: str,
    ) -> tuple[ExternalInteractionBinding | None, JSONResponse | None]:
        """Authenticate a loopback callback against its live Owner-bound token."""
        client_host = request.client.host if request.client else ""
        if client_host not in {"127.0.0.1", "::1", "testclient"}:
            return None, JSONResponse(
                {"ok": False, "error": local_only_error},
                status_code=403,
            )
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not token:
            return None, JSONResponse(
                {"ok": False, "error": "缺少交互令牌"},
                status_code=401,
            )
        binding = bridge.resolve_binding(token)
        if binding is None:
            return None, JSONResponse(
                {"ok": False, "error": "交互令牌无效或已过期"},
                status_code=403,
            )
        coordinator = getattr(crew, "logout_coordinator", None)
        allows_work = getattr(coordinator, "allows_work", None)
        if not callable(allows_work) or not allows_work(binding.owner_account_id):
            return None, JSONResponse(
                {
                    "ok": False,
                    "error": "交互令牌所属账号已退出或不再是 Active Owner",
                    "code": "ACTIVE_OWNER_REQUIRED",
                },
                status_code=403,
            )
        return binding, None

    async def invoke_bound_tool(
        request: Request,
        payload: dict,
        *,
        tool_name: str,
        local_only_error: str,
    ) -> JSONResponse:
        binding, error = authorized_binding(
            request,
            local_only_error=local_only_error,
        )
        if error is not None:
            return error
        assert binding is not None
        try:
            result = await bridge.invoke_tool(
                binding.token,
                tool_name,
                payload,
            )
        except ToolError as exc:
            return JSONResponse({"ok": False, "error": safe_public_error(exc, "交互请求无效")}, status_code=400)
        except PermissionError as exc:
            return JSONResponse({"ok": False, "error": safe_public_error(exc, "交互请求被拒绝")}, status_code=403)
        except (TypeError, ValueError) as exc:
            return JSONResponse({"ok": False, "error": safe_public_error(exc, "交互请求无效")}, status_code=400)
        except Exception as exc:  # noqa: BLE001 - proxy must return a diagnosable error
            log.exception("External Runtime 控制工具失败 tool=%s", tool_name)
            return JSONResponse({"ok": False, "error": safe_public_error(exc, "交互服务内部错误")}, status_code=500)
        return JSONResponse(result)

    @router.post("/api/internal/interactions/ask")
    async def ask_followup(request: Request, payload: dict) -> JSONResponse:
        return await invoke_bound_tool(
            request,
            payload,
            tool_name="ask_followup_question",
            local_only_error="交互接口仅允许本机访问",
        )

    @router.post("/api/internal/team/mention")
    async def team_mention(request: Request, payload: dict) -> JSONResponse:
        return await invoke_bound_tool(
            request,
            payload,
            tool_name="team_mention",
            local_only_error="团队协作接口仅允许本机访问",
        )

    @router.post("/api/internal/team/plan/create")
    async def create_team_plan(request: Request, payload: dict) -> JSONResponse:
        return await invoke_bound_tool(
            request,
            payload,
            tool_name="team_plan_create",
            local_only_error="团队计划接口仅允许本机访问",
        )

    @router.post("/api/internal/team/plan/read")
    async def read_team_plan(request: Request, payload: dict) -> JSONResponse:
        return await invoke_bound_tool(
            request,
            payload,
            tool_name="team_plan_read",
            local_only_error="团队计划接口仅允许本机访问",
        )

    @router.post("/api/internal/team/plan/update")
    async def update_team_plan(request: Request, payload: dict) -> JSONResponse:
        return await invoke_bound_tool(
            request,
            payload,
            tool_name="team_plan_update",
            local_only_error="团队计划接口仅允许本机访问",
        )

    return router
