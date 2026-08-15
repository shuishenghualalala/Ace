"""Lightweight Team turn classification before workflow planning."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from crew.core.types import Message


TurnKind = Literal["direct_chat", "status_query", "new_workflow", "uncertain"]
ExecutionMode = Literal["direct", "fast", "standard", "ai"]

TEAM_TURN_DECISION_TIMEOUT = 4.0
TEAM_TURN_DECISION_MAX_TOKENS = 512


@dataclass(frozen=True)
class TeamStatusQuery:
    question: str = ""
    scope: str = "latest_turn"
    needs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TeamTurnDecision:
    turn_kind: TurnKind = "uncertain"
    execution_mode: ExecutionMode = "standard"
    reason: str = ""
    status_query: TeamStatusQuery | None = None
    elapsed_ms: int = 0
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def is_status_query(self) -> bool:
        return self.turn_kind == "status_query"

    @property
    def is_direct_chat(self) -> bool:
        return self.turn_kind == "direct_chat"

    @property
    def is_new_workflow(self) -> bool:
        return self.turn_kind == "new_workflow"


def team_turn_decision_messages(
    *,
    user_message: str,
    context: dict[str, Any],
) -> list[Message]:
    system = """你是 Crew TeamTurnDecision，只做本轮团队消息分类。
只输出 JSON，不输出解释。

分类边界：
- direct_chat：用户只是寒暄、确认、轻量聊天或不需要团队工作流的直接问答。
- status_query：用户在询问已有团队运行事实、进度、耗时、成员贡献、节点状态、失败/阻塞原因、规划结果或最近事件。
- new_workflow：用户提出新的执行目标，需要创建或继续一个新的团队工作流。
- uncertain：无法可靠判断。

约束：
- 你不生成 DAG，不生成 work_units，不分配成员。
- 只有 context.has_existing_workflow=true 时才能输出 status_query。
- status_query 只能读取已有事实，不能要求执行新任务。

输出 schema：
{
  "turn_kind": "direct_chat|status_query|new_workflow|uncertain",
  "execution_mode": "direct|fast|standard|ai",
  "reason": "简短原因",
  "status_query": {
    "question": "用户想知道什么",
    "scope": "latest_turn|current_workflow|session",
    "needs": ["duration","members","nodes","planning","errors","latest_events"]
  }
}
"""
    payload = {
        "user_message": str(user_message or "")[:1200],
        "context": context,
    }
    return [
        Message.system(system),
        Message.user(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
    ]


async def decide_team_turn(
    provider: Any,
    *,
    user_message: str,
    context: dict[str, Any],
    timeout_s: float = TEAM_TURN_DECISION_TIMEOUT,
) -> TeamTurnDecision:
    started = time.perf_counter()
    messages = team_turn_decision_messages(user_message=user_message, context=context)
    diagnostics: dict[str, Any] = {
        "has_existing_workflow": bool(context.get("has_existing_workflow")),
    }
    try:
        response = await asyncio.wait_for(
            _chat(provider, messages, max_tokens=TEAM_TURN_DECISION_MAX_TOKENS),
            timeout=max(0.2, float(timeout_s or TEAM_TURN_DECISION_TIMEOUT)),
        )
        text = str(getattr(response, "text", "") or "")
        diagnostics["partial_chars"] = len(text)
        data = _json_from_text(text)
        decision = coerce_team_turn_decision(data, has_existing_workflow=bool(context.get("has_existing_workflow")))
        return TeamTurnDecision(
            turn_kind=decision.turn_kind,
            execution_mode=decision.execution_mode,
            reason=decision.reason,
            status_query=decision.status_query,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            diagnostics={**diagnostics, "status": "success"},
        )
    except Exception as exc:  # noqa: BLE001 - caller should fall back to existing routing
        return TeamTurnDecision(
            turn_kind="uncertain",
            execution_mode="standard",
            reason="team_turn_decision_failed",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            diagnostics={
                **diagnostics,
                "status": "fallback",
                "error_type": exc.__class__.__name__,
                "error": "团队回合判断失败",
            },
        )


def coerce_team_turn_decision(data: dict[str, Any], *, has_existing_workflow: bool) -> TeamTurnDecision:
    raw_kind = str(data.get("turn_kind") or "uncertain").strip()
    turn_kind: TurnKind = raw_kind if raw_kind in {"direct_chat", "status_query", "new_workflow", "uncertain"} else "uncertain"  # type: ignore[assignment]
    if turn_kind == "status_query" and not has_existing_workflow:
        turn_kind = "uncertain"
    raw_mode = str(data.get("execution_mode") or "standard").strip()
    execution_mode: ExecutionMode = raw_mode if raw_mode in {"direct", "fast", "standard", "ai"} else "standard"  # type: ignore[assignment]
    status_query = None
    if turn_kind == "status_query":
        raw_query = data.get("status_query") if isinstance(data.get("status_query"), dict) else {}
        needs = [
            str(item or "").strip()
            for item in list(raw_query.get("needs") or [])
            if str(item or "").strip()
        ][:8]
        status_query = TeamStatusQuery(
            question=str(raw_query.get("question") or data.get("reason") or "").strip(),
            scope=str(raw_query.get("scope") or "latest_turn").strip() or "latest_turn",
            needs=needs,
        )
        execution_mode = "direct"
    elif turn_kind == "direct_chat":
        execution_mode = "direct"
    return TeamTurnDecision(
        turn_kind=turn_kind,
        execution_mode=execution_mode,
        reason=str(data.get("reason") or "").strip(),
        status_query=status_query,
    )


def direct_chat_decision(reason: str = "") -> TeamTurnDecision:
    return TeamTurnDecision(
        turn_kind="direct_chat",
        execution_mode="direct",
        reason=reason or "direct_chat",
        diagnostics={"source": "team_turn_router"},
    )


def new_workflow_decision(
    execution_mode: ExecutionMode = "standard",
    reason: str = "",
    *,
    source: str = "team_turn_router",
) -> TeamTurnDecision:
    mode: ExecutionMode = execution_mode if execution_mode in {"fast", "standard", "ai"} else "standard"
    return TeamTurnDecision(
        turn_kind="new_workflow",
        execution_mode=mode,
        reason=reason or source,
        diagnostics={"source": source},
    )


async def _chat(provider: Any, messages: list[Message], *, max_tokens: int) -> Any:
    try:
        return await provider.chat(messages, tools=None, max_tokens=max_tokens)
    except TypeError as exc:
        if "max_tokens" not in str(exc):
            raise
        return await provider.chat(messages, tools=None)


def _json_from_text(text: str) -> dict[str, Any]:
    body = str(text or "").strip()
    if not body:
        raise ValueError("empty team turn decision response")
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", body, flags=re.DOTALL)
    if fenced:
        body = fenced.group(1)
    elif "{" in body and "}" in body:
        body = body[body.find("{"):body.rfind("}") + 1]
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise ValueError("team turn decision response must be a JSON object")
    return parsed
