"""Lightweight Team turn classification before workflow planning."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from crew.core.types import Message
from crew.core.text_parsing import extract_json_object
from crew.providers import stream_aux


TurnKind = Literal["direct_chat", "status_query", "new_workflow", "uncertain"]
ExecutionMode = Literal["direct", "fast", "standard", "ai"]

TEAM_TURN_DECISION_TIMEOUT = 4.0
TEAM_TURN_DECISION_MAX_TOKENS = 512

# 路由决策缓存：同 roster（context 摘要）+ 同问题形态（归一化 user message）命中
# 即跳过 LLM。只缓存成功决策；短 TTL 避免「status_query 结论」长期过期。
TEAM_TURN_DECISION_CACHE_TTL_SECONDS = 120.0
_TEAM_TURN_DECISION_CACHE_MAX_ENTRIES = 512
_team_turn_decision_cache: dict[str, tuple[float, TeamTurnDecision]] = {}


def _turn_decision_cache_key(user_message: str, context: dict[str, Any]) -> str:
    normalized = " ".join(str(user_message or "").split()).lower()[:240]
    try:
        context_json = json.dumps(context, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        context_json = repr(sorted(context))
    digest = hashlib.sha256(context_json.encode("utf-8")).hexdigest()[:16]
    return f"{digest}:{normalized}"


def _cache_get_turn_decision(key: str) -> TeamTurnDecision | None:
    entry = _team_turn_decision_cache.get(key)
    if entry is None:
        return None
    cached_at, decision = entry
    if time.monotonic() - cached_at > TEAM_TURN_DECISION_CACHE_TTL_SECONDS:
        _team_turn_decision_cache.pop(key, None)
        return None
    _team_turn_decision_cache[key] = (time.monotonic(), decision)
    return decision


def _cache_put_turn_decision(key: str, decision: TeamTurnDecision) -> None:
    if len(_team_turn_decision_cache) >= _TEAM_TURN_DECISION_CACHE_MAX_ENTRIES:
        oldest = min(_team_turn_decision_cache, key=lambda k: _team_turn_decision_cache[k][0])
        _team_turn_decision_cache.pop(oldest, None)
    _team_turn_decision_cache[key] = (time.monotonic(), decision)


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
    diagnostics: dict[str, Any] = {
        "has_existing_workflow": bool(context.get("has_existing_workflow")),
    }
    cache_key = _turn_decision_cache_key(user_message, context)
    cached = _cache_get_turn_decision(cache_key)
    if cached is not None:
        return replace(
            cached,
            elapsed_ms=0,
            diagnostics={**cached.diagnostics, "status": "cache_hit", "cache_hit": True},
        )
    messages = team_turn_decision_messages(user_message=user_message, context=context)
    try:
        response = await asyncio.wait_for(
            _chat(provider, messages, max_tokens=TEAM_TURN_DECISION_MAX_TOKENS),
            timeout=max(0.2, float(timeout_s or TEAM_TURN_DECISION_TIMEOUT)),
        )
        text = str(getattr(response, "text", "") or "")
        diagnostics["partial_chars"] = len(text)
        data = _json_from_text(text)
        decision = coerce_team_turn_decision(data, has_existing_workflow=bool(context.get("has_existing_workflow")))
        resolved = TeamTurnDecision(
            turn_kind=decision.turn_kind,
            execution_mode=decision.execution_mode,
            reason=decision.reason,
            status_query=decision.status_query,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            diagnostics={**diagnostics, "status": "success"},
        )
        _cache_put_turn_decision(cache_key, resolved)
        return resolved
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
                "error": str(exc)[:240],
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
    return await stream_aux(
        provider,
        messages,
        purpose="team-turn-decision",
        max_tokens=max_tokens,
        retry=0,
    )


def _json_from_text(text: str) -> dict[str, Any]:
    body = str(text or "").strip()
    parsed = extract_json_object(text)
    if parsed is None:
        message = "empty team turn decision response" if not body else "invalid team turn decision JSON"
        raise ValueError(message)
    return parsed
