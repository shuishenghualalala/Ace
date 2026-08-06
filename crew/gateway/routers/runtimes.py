"""外部 runtime / agent / team 管理路由 + 智能组队建议。"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import shutil
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from crew.agent.external.detector import discover_local_runtimes
from crew.agent.external.runtime_profile import normalize_runtime_models
from crew.agent.external.runtime_registry import resolve_runtime_display_badge
from crew.core.errors import ProviderError
from crew.core.interfaces import LLMProvider
from crew.core.types import Message
from crew.gateway.auth import AuthenticationError, account_from_request, require_admin
from crew.gateway.helpers import (
    build_team_draft,
    confirmed_formation_plan,
    extract_json_object,
    fast_team_suggestion,
    require_external_agents_enabled,
    suggest_role_description,
)
from crew.state.logging import get_logger
from crew.team.formation import (
    apply_formation_ai_audit,
    formation_ai_context,
    formation_auto_decision,
)
from crew.team.roles import (
    all_role_public_payloads,
    crew_builtin_agent_public,
    intelligent_role_markdown,
    role_preset,
)

log = get_logger("gateway.runtimes")

# 用户输入字段的单字段截断上限（4 KB），避免单条描述/工作流就把 prompt 撑爆或注入超长指令。
SUGGEST_FIELD_CHAR_CAP = 4096
# 组装后的 prompt 总字符数上限（含可用智能体清单），防止 megabyte 级 payload 把 LLM 成本打爆。
SUGGEST_PROMPT_CHAR_CAP = 32_000
TEAM_DRAFT_CACHE_TTL_S = 300.0
TEAM_DRAFT_CACHE_MAX_ENTRIES = 128
FORMATION_MODES = frozenset({"fast", "ai", "auto"})


class _TeamDraftCache:
    def __init__(self, ttl_s: float = TEAM_DRAFT_CACHE_TTL_S, max_entries: int = TEAM_DRAFT_CACHE_MAX_ENTRIES) -> None:
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self._items: dict[str, tuple[float, dict[str, Any]]] = {}

    def get(self, key: str) -> dict[str, Any] | None:
        item = self._items.get(key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at <= time.monotonic():
            self._items.pop(key, None)
            return None
        return copy.deepcopy(value)

    def put(self, key: str, value: dict[str, Any]) -> None:
        now = time.monotonic()
        expired = [item_key for item_key, (expires_at, _) in self._items.items() if expires_at <= now]
        for item_key in expired:
            self._items.pop(item_key, None)
        while len(self._items) >= self.max_entries:
            oldest_key = min(self._items, key=lambda item_key: self._items[item_key][0])
            self._items.pop(oldest_key, None)
        self._items[key] = (now + self.ttl_s, copy.deepcopy(value))


def _normalized_draft_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _draft_cache_key(
    kind: str,
    payload: dict[str, Any],
    *,
    owner_account_id: str,
    catalogs: list[Any] | None = None,
) -> str:
    normalized = {
        "kind": kind,
        "owner_account_id": owner_account_id,
        "name": _normalized_draft_text(payload.get("name")),
        "description": _normalized_draft_text(payload.get("description")),
        "leader_agent_id": _normalized_draft_text(payload.get("leader_agent_id")),
        "catalogs": catalogs or [],
    }
    serialized = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _draft_stream_line(event: dict[str, Any]) -> str:
    return json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"


def _runtime_availability(runtime: dict[str, Any]) -> dict[str, Any]:
    """Expose the persisted probe state, with a live executable-path guard."""
    payload = dict(runtime)
    target = str(runtime.get("executable_path") or "").strip()
    protocol = str(runtime.get("protocol") or "").strip().lower()
    available = False
    if target:
        if protocol == "client":
            try:
                available = importlib.util.find_spec(target) is not None
            except (ImportError, AttributeError, ValueError):
                available = False
        else:
            resolved = target if os.path.isabs(target) else shutil.which(target)
            available = bool(resolved and os.path.isfile(resolved) and os.access(resolved, os.X_OK))
    metadata = dict(runtime.get("metadata")) if isinstance(runtime.get("metadata"), dict) else {}
    display_badge = resolve_runtime_display_badge(
        provider=str(runtime.get("provider") or ""),
        metadata=metadata,
    )
    metadata["display_badge"] = display_badge
    status = str(metadata.get("availability_status") or "").strip()
    if not available:
        status = "unavailable"
    elif status not in {"ready", "degraded", "unavailable"}:
        status = "degraded"
    payload["available"] = status == "ready"
    payload["availability_status"] = status
    payload["display_badge"] = display_badge
    payload["metadata"] = metadata
    return payload


def _external_agent_payloads(
    store: Any,
    agents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project RuntimeDescriptor presentation fields onto persisted Agents."""

    runtimes = {
        str(runtime.get("id") or ""): runtime
        for runtime in store.list_runtimes()
    }
    payloads: list[dict[str, Any]] = []
    for agent in agents:
        runtime = runtimes.get(str(agent.get("runtime_id") or ""))
        metadata = (
            runtime.get("metadata")
            if isinstance(runtime, dict) and isinstance(runtime.get("metadata"), dict)
            else {}
        )
        payloads.append({
            **agent,
            "display_badge": resolve_runtime_display_badge(
                provider=str(
                    (runtime or {}).get("provider")
                    or agent.get("provider")
                    or ""
                ),
                metadata=metadata,
            ),
        })
    return payloads


def _managed_temporary_agent_ids(store: Any, *, owner_account_id: str) -> set[str]:
    """Return managed Agents that must stay out of the user's normal catalog."""

    hidden = {
        str(member.get("agent_id") or "")
        for team in store.list_teams(owner_account_id=owner_account_id)
        for member in (
            team.get("formation_plan", {}).get("members", [])
            if isinstance(team.get("formation_plan"), dict)
            else []
        )
        if isinstance(member, dict)
        and member.get("selection_source") == "ai_temporary"
        and str(member.get("agent_id") or "")
    }
    hidden.update(
        str(agent.get("id") or "")
        for agent in store.list_agents(owner_account_id=owner_account_id)
        if str(agent.get("managed_kind") or "")
        and str(agent.get("id") or "")
    )
    return hidden



def _external_team_payloads(
    store: Any,
    teams: list[dict[str, Any]],
    *,
    owner_account_id: str,
) -> list[dict[str, Any]]:
    """Project member Agent badges while keeping Team persistence unchanged."""

    agents = _external_agent_payloads(
        store,
        store.list_agents(owner_account_id=owner_account_id),
    )
    badge_by_agent_id = {
        str(agent.get("id") or ""): str(agent.get("display_badge") or "?")
        for agent in agents
    }
    badge_by_agent_id["crew::builtin"] = "M"
    return [
        {
            **team,
            "display_badge": "T",
            "members": [
                {
                    **member,
                    "display_badge": badge_by_agent_id.get(
                        str(member.get("agent_id") or ""),
                        "?",
                    ),
                }
                for member in (team.get("members") or [])
                if isinstance(member, dict)
            ],
        }
        for team in teams
    ]


def _truncate_user_payload(payload: dict) -> dict:
    """截断用户输入字段，防止 prompt 注入 / 成本爆破。

    对 payload 里每个 string 值裁到 SUGGEST_FIELD_CHAR_CAP；非 str 值原样保留
    （如 attachments 列表），因为只把它们 json.dumps 进 prompt，不会逐字符注入。
    """
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, str):
            out[key] = value[:SUGGEST_FIELD_CHAR_CAP]
        else:
            out[key] = value
    return out


def _formation_response_payload(
    suggestion: dict[str, Any],
    *,
    requested_mode: str,
    selected_mode: str,
    started_at: float,
    fast_ms: int,
    ai_ms: int = 0,
    fallback_reason: str = "",
) -> dict[str, Any]:
    """Return one stable Formation response shape for every strategy."""

    payload = dict(suggestion)
    payload.pop("mode", None)
    plan = payload.get("formation_plan") if isinstance(payload.get("formation_plan"), dict) else {}
    payload.update({
        "requested_formation_mode": requested_mode,
        "selected_formation_mode": selected_mode,
        "fallback_reason": fallback_reason,
        "timing": {
            "fast_ms": max(0, int(fast_ms)),
            "ai_ms": max(0, int(ai_ms)),
            "total_ms": max(0, int((time.perf_counter() - started_at) * 1000)),
        },
        "warnings": list(plan.get("warnings") or []),
    })
    payload.setdefault("decision_required", False)
    payload.setdefault("required_agent_conflicts", [])
    payload.setdefault("staffing_decision_required", False)
    payload.setdefault("staffing_gaps", [])
    payload.setdefault("staffing_only_improvement", False)
    payload.setdefault("ai_material_improvements", [])
    return payload


def _formation_ai_prompt(context: dict[str, Any]) -> tuple[str, str]:
    """Compile the compact audit contract shared by AI and Auto modes."""

    context_json = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    prompt = (
        "你是 Formation AI，基于 Fast 草案做一次最小变更审计，不重新生成完整团队。"
        "Leader、locked_agent_ids、excluded_agent_ids 是不可修改约束。"
        "member_changes 只填写确需新增、调整或移除的常驻成员；未提及成员由后端保留。"
        "只能引用 available_agents 和 standard_roles。不要输出分析过程、完整职责、工作流、"
        "交付物、Provider 偏好或普通业务追问。新增需求和独立分工约束必须在 evidence_quote "
        "中逐字引用 team_input 的依据；没有依据就不要新增。"
        "若 Fast 已经最小充分，staffing_plan.required=false。"
        "若仍有已知必要能力无法由当前成员可靠覆盖，直接给出合并后的最小临时成员方案；"
        "优化顺序固定为：先覆盖必要能力，再最小化新增成员人数，最后缩小每名成员职责范围。"
        "team_requirements.roles 和 standard_roles 只是能力/职责标签，不代表必须一角一人；"
        "默认把设计、实现、测试、文档等相容能力合并给同一名临时成员，绝不能按角色逐项补员。"
        "只有用户原文明确要求独立负责人、职责隔离或真实并行时，才允许拆成多名成员，并必须"
        "同时输出有原文 evidence_quote 的 separation_constraints。Runtime/model "
        "只能引用 ready_runtime_options。只返回严格 JSON，不要 Markdown 包裹。\n"
        "JSON 结构："
        "{\"requirement_audit\":{\"required_roles\":[{\"role_key\":\"...\","
        "\"required_capabilities\":[\"...\"],\"evidence_quote\":\"...\"}]},"
        "\"member_changes\":{\"remove_agent_ids\":[\"...\"],"
        "\"upsert_members\":[{\"agent_id\":\"...\",\"role_key\":\"...\","
        "\"assigned_capabilities\":[\"...\"],\"evidence_quote\":\"...\"}]},"
        "\"staffing_plan\":{\"required\":true,\"minimality_rationale\":\"...\","
        "\"members\":[{\"role_key\":\"...\","
        "\"required_capabilities\":[\"...\"],\"responsibility_focus\":\"...\","
        "\"reason\":\"...\",\"recommended_runtime_id\":\"...\","
        "\"recommended_model_id\":\"...\"}]},"
        "\"separation_constraints\":[{\"capabilities\":[\"...\"],"
        "\"independent_from\":\"...\",\"evidence_quote\":\"...\"}]}\n"
        f"Formation 上下文：{context_json}"
    )
    if len(prompt) > SUGGEST_PROMPT_CHAR_CAP:
        prompt = prompt[:SUGGEST_PROMPT_CHAR_CAP]
    return prompt, context_json


async def _run_formation_ai(
    *,
    provider: LLMProvider,
    payload: dict[str, Any],
    agents: list[dict[str, Any]],
    fallback: dict[str, Any],
    runtimes: list[dict[str, Any]],
    requested_mode: str,
    started_at: float,
    fast_ms: int,
) -> dict[str, Any]:
    """Run one Formation AI audit and compile it into the stable public shape."""

    context = formation_ai_context(payload, agents, fallback, runtimes)
    prompt, context_json = _formation_ai_prompt(context)
    ai_started_at = time.perf_counter()
    provider_fallback_reason = ""
    usage: dict[str, int] = {}
    response_chars = 0
    reasoning_chars = 0
    try:
        resp = await provider.chat([
            Message.system("只输出可解析 JSON；不要输出分析过程。"),
            Message.user(prompt),
        ])
        usage = dict(resp.usage or {})
        response_chars = len(resp.text or "")
        reasoning_chars = len(resp.reasoning_content or "")
        raw = extract_json_object(resp.text or "")
    except (TimeoutError, ProviderError, AttributeError, TypeError, ValueError) as exc:
        log.warning("Formation AI 生成失败，使用 Fast 方案: %s", exc)
        raw = None
        provider_fallback_reason = "provider_error"
    ai_ms = int((time.perf_counter() - ai_started_at) * 1000)

    compile_started_at = time.perf_counter()
    ai_result = None
    rejected_reason = ""
    if raw is not None:
        ai_result, rejected_reason = apply_formation_ai_audit(
            payload,
            agents,
            fallback,
            raw,
            runtimes,
        )
    compile_ms = int((time.perf_counter() - compile_started_at) * 1000)
    selected_mode = "ai" if ai_result is not None else "fast"
    final_fallback_reason = (
        ""
        if ai_result is not None
        else provider_fallback_reason or rejected_reason or "invalid_ai_output"
    )
    log.info(
        "Formation AI metrics model=%s requested=%s selected=%s fallback_reason=%s "
        "fast_ms=%d ai_ms=%d compile_ms=%d total_ms=%d context_chars=%d "
        "prompt_chars=%d prompt_tokens=%d completion_tokens=%d "
        "response_chars=%d reasoning_chars=%d agent_candidates=%d runtime_options=%d",
        str(getattr(provider, "model", "") or "unknown"),
        requested_mode,
        selected_mode,
        final_fallback_reason or "none",
        fast_ms,
        ai_ms,
        compile_ms,
        int((time.perf_counter() - started_at) * 1000),
        len(context_json),
        len(prompt),
        int(usage.get("prompt_tokens") or 0),
        int(usage.get("completion_tokens") or 0),
        response_chars,
        reasoning_chars,
        len(context.get("available_agents") or []),
        len(context.get("ready_runtime_options") or []),
    )
    return _formation_response_payload(
        ai_result or fallback,
        requested_mode=requested_mode,
        selected_mode=selected_mode,
        started_at=started_at,
        fast_ms=fast_ms,
        ai_ms=ai_ms,
        fallback_reason=final_fallback_reason,
    )


def _draft_agent_catalog(agents: list[dict]) -> list[dict[str, str]]:
    return [
        {
            "id": str(agent.get("id") or ""),
            "name": str(agent.get("name") or ""),
            "provider": str(agent.get("provider") or ""),
            "runtime_id": str(agent.get("runtime_id") or agent.get("runtime") or ""),
            "model": str(agent.get("model") or ""),
        }
        for agent in [crew_builtin_agent_public(), *agents]
    ]


def _draft_role_catalog() -> list[dict[str, Any]]:
    roles: list[dict[str, Any]] = []
    for role in all_role_public_payloads():
        lane = str(role.get("workflow_lane") or "")
        if lane == "lead":
            continue
        roles.append({
            "key": str(role.get("key") or ""),
            "label": str(role.get("label") or ""),
            "description": str(role.get("description") or ""),
            "capabilities": role.get("capabilities") or [],
            "workflow_lane": lane,
        })
    return roles


def _normalize_ai_draft_slots(raw_slots: Any, valid_agent_ids: set[str], valid_role_keys: set[str], leader_id: str) -> list[dict[str, Any]]:
    if not isinstance(raw_slots, list):
        return []
    slots: list[dict[str, Any]] = []
    seen_roles: set[str] = set()
    for raw in raw_slots[:6]:
        if not isinstance(raw, dict):
            continue
        role_key = str(raw.get("role_key") or "").strip()
        if role_key not in valid_role_keys or role_key in seen_roles:
            continue
        agent_id = str(raw.get("agent_id") or "").strip()
        if agent_id and (agent_id not in valid_agent_ids or agent_id == leader_id):
            agent_id = ""
        slots.append({
            "role_key": role_key,
            "agent_id": agent_id,
            "required": bool(raw.get("required", True)),
        })
        seen_roles.add(role_key)
    return slots


def _partial_json_string(raw: str, field: str) -> str:
    """Return the currently available value of a JSON string field.

    The provider emits incomplete JSON while streaming. Trimming an incomplete
    escape sequence lets the UI display the description without waiting for the
    closing quote, while the completed payload is still parsed normally later.
    """
    marker = json.dumps(field)
    marker_index = raw.find(marker)
    if marker_index < 0:
        return ""
    colon_index = raw.find(":", marker_index + len(marker))
    if colon_index < 0:
        return ""
    quote_index = raw.find('"', colon_index + 1)
    if quote_index < 0:
        return ""
    if raw[colon_index + 1:quote_index].strip():
        return ""
    encoded: list[str] = []
    escaped = False
    for char in raw[quote_index + 1:]:
        if char == '"' and not escaped:
            break
        encoded.append(char)
        if char == "\\" and not escaped:
            escaped = True
        else:
            escaped = False
    candidate = "".join(encoded)
    for _ in range(min(6, len(candidate)) + 1):
        try:
            return str(json.loads(f'"{candidate}"'))
        except json.JSONDecodeError:
            candidate = candidate[:-1]
    return ""


def create_runtimes_router(crew) -> APIRouter:
    router = APIRouter()
    description_draft_cache = _TeamDraftCache()
    formation_draft_cache = _TeamDraftCache()

    def _admin_or_403(request: Request) -> JSONResponse | None:
        try:
            require_admin(account_from_request(request), crew.config)
        except AuthenticationError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=403)
        return None

    def _external_store():
        require_external_agents_enabled(crew)
        if crew.external_agents is None:
            raise RuntimeError("外部智能体存储未初始化")
        return crew.external_agents

    @router.post("/api/runtimes/scan")
    async def scan_external_runtimes() -> JSONResponse:
        store = _external_store()
        detected = await discover_local_runtimes()
        synced = store.sync_runtimes(detected)
        return JSONResponse([_runtime_availability(runtime) for runtime in synced])

    @router.get("/api/runtimes")
    async def external_runtimes() -> JSONResponse:
        return JSONResponse([
            _runtime_availability(runtime)
            for runtime in _external_store().list_runtimes()
        ])

    @router.post("/api/runtimes/register")
    async def register_external_runtime(request: Request, payload: dict) -> JSONResponse:
        denied = _admin_or_403(request)
        if denied is not None:
            return denied
        # 校验必填字段与类型，防止把任意 dict 直接写库（对照 sessions.py 的 allowlist）。
        required: dict[str, type] = {
            "id": str,
            "type": str,
            "provider": str,
        }
        for field, typ in required.items():
            if field not in payload:
                return JSONResponse(
                    {"ok": False, "error": f"缺少必填字段: {field}"}, status_code=400,
                )
            if not isinstance(payload[field], typ) or not str(payload[field]).strip():
                return JSONResponse(
                    {"ok": False, "error": f"字段 {field} 必须是非空 {typ.__name__}"}, status_code=400,
                )
        return JSONResponse(_runtime_availability(
            _external_store().upsert_runtime(payload),
        ))

    @router.get("/api/external-agents")
    async def external_agents(request: Request) -> JSONResponse:
        owner = account_from_request(request).owner_account_id
        store = _external_store()
        hidden_ids = _managed_temporary_agent_ids(store, owner_account_id=owner)
        return JSONResponse(_external_agent_payloads(
            store,
            [
                agent
                for agent in store.list_agents(owner_account_id=owner)
                if str(agent.get("id") or "") not in hidden_ids
            ],
        ))

    @router.post("/api/external-agents")
    async def create_external_agent(request: Request, payload: dict) -> JSONResponse:
        runtime_id = str(payload.get("runtime_id") or "").strip()
        model = str(payload.get("model") or "").strip()
        try:
            runtime = _external_store().get_runtime(runtime_id)
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": f"运行时不存在: {exc}"}, status_code=404)
        runtime_state = _runtime_availability(runtime)
        if runtime_state.get("availability_status") != "ready":
            return JSONResponse({"ok": False, "error": "运行时尚未就绪，请重新探测"}, status_code=409)
        models = normalize_runtime_models((runtime.get("metadata") or {}).get("models"))
        if not model:
            return JSONResponse({"ok": False, "error": "请选择模型"}, status_code=400)
        if model not in {item.id for item in models}:
            return JSONResponse({"ok": False, "error": "所选模型不属于当前运行时"}, status_code=400)
        try:
            agent = _external_store().create_agent(
                owner_account_id=account_from_request(request).owner_account_id,
                name=str(payload.get("name") or "").strip() or "未命名智能体",
                runtime_id=runtime_id,
                model=model,
                system_prompt=str(payload.get("system_prompt") or payload.get("instructions") or "").strip(),
                custom_args=payload.get("custom_args") or [],
                custom_env=payload.get("custom_env") or {},
            )
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": f"运行时不存在: {exc}"}, status_code=404)
        return JSONResponse(_external_agent_payloads(
            _external_store(),
            [agent],
        )[0])

    @router.delete("/api/external-agents/{agent_id}")
    async def delete_external_agent(request: Request, agent_id: str) -> JSONResponse:
        try:
            _external_store().delete_agent(
                agent_id,
                owner_account_id=account_from_request(request).owner_account_id,
            )
        except KeyError:
            return JSONResponse({"ok": False, "error": "智能体不存在"}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True})

    @router.get("/api/external-teams")
    async def external_teams(request: Request) -> JSONResponse:
        owner = account_from_request(request).owner_account_id
        store = _external_store()
        return JSONResponse(_external_team_payloads(
            store,
            store.list_teams(owner_account_id=owner),
            owner_account_id=owner,
        ))

    @router.get("/api/external-teams/roles")
    async def external_team_roles() -> JSONResponse:
        _external_store()
        return JSONResponse(all_role_public_payloads())

    @router.post("/api/external-teams")
    async def create_external_team(request: Request, payload: dict) -> JSONResponse:
        owner = account_from_request(request).owner_account_id
        store = _external_store()
        members = [
            dict(member)
            for member in (payload.get("members") or [])
            if isinstance(member, dict)
        ]
        leader_agent_id = str(payload.get("leader_agent_id") or "").strip()
        team_goal = str(payload.get("description") or payload.get("name") or "").strip()
        existing_plan = (
            copy.deepcopy(payload.get("formation_plan"))
            if isinstance(payload.get("formation_plan"), dict)
            else {}
        )
        existing_plan_members = (
            existing_plan.get("members")
            if isinstance(existing_plan.get("members"), list)
            else []
        )
        temporary_specs = [
            item
            for item in (payload.get("temporary_members") or [])
            if isinstance(item, dict)
        ][:5]
        created_temporary_ids: list[str] = []

        def rollback_temporary_agents() -> None:
            for agent_id in reversed(created_temporary_ids):
                try:
                    store.delete_agent(agent_id, owner_account_id=owner)
                except (KeyError, ValueError):
                    log.warning("回滚临时成员失败: agent_id=%s", agent_id)

        try:
            valid_role_keys = {
                str(role.get("key") or "")
                for role in all_role_public_payloads()
            }
            for index, temporary in enumerate(temporary_specs):
                runtime_id = str(temporary.get("runtime_id") or "").strip()
                model_id = str(temporary.get("model_id") or "").strip()
                role_key = str(temporary.get("role_key") or "").strip()
                if role_key not in valid_role_keys:
                    raise ValueError("临时成员角色不属于标准角色目录")
                runtime = store.get_runtime(runtime_id)
                if _runtime_availability(runtime).get("availability_status") != "ready":
                    raise ValueError("临时成员运行时尚未就绪")
                models = normalize_runtime_models((runtime.get("metadata") or {}).get("models"))
                if not model_id or model_id not in {model.id for model in models}:
                    raise ValueError("临时成员模型不属于所选运行时")
                preset = role_preset(role_key)
                assigned_capabilities = [
                    str(item)
                    for item in (temporary.get("required_capabilities") or preset.get("capabilities") or [])
                    if str(item)
                ]
                name = str(temporary.get("name") or "").strip()
                if not name:
                    name = f"{str(payload.get('name') or '团队').strip()} · 临时{preset['label']}"
                system_prompt = intelligent_role_markdown(
                    role_key=role_key,
                    agent_name=name,
                    team_goal=team_goal,
                    assigned_capabilities=assigned_capabilities,
                    responsibility={
                        "mission": str(temporary.get("responsibility_focus") or preset["description"]),
                        "deliverables": list(preset.get("deliverables") or []),
                        "collaboration": str(preset.get("collaboration") or ""),
                    },
                )
                agent = store.create_agent(
                    owner_account_id=owner,
                    name=name[:120],
                    runtime_id=runtime_id,
                    model=model_id,
                    system_prompt=system_prompt,
                )
                agent_id = str(agent.get("id") or "")
                created_temporary_ids.append(agent_id)
                member = {
                    "agent_id": agent_id,
                    "role": system_prompt,
                    "role_key": role_key,
                    "role_label": str(preset["label"]),
                    "capabilities": assigned_capabilities,
                    "assigned_capabilities": assigned_capabilities,
                    "workflow_lane": str(preset.get("workflow_lane") or "build"),
                    "sort_order": len(members) + index,
                }
                members.append(member)
                existing_plan_members.append({
                    "agent_id": agent_id,
                    "role_key": role_key,
                    "role_label": str(preset["label"]),
                    "assigned_capabilities": assigned_capabilities,
                    "responsibility_markdown": system_prompt,
                    "selection_source": "ai_temporary",
                    "locked": True,
                    "selection_reason": str(
                        temporary.get("reason")
                        or "用户确认由 Formation AI 创建临时成员补足能力缺口。"
                    ),
                })
            if created_temporary_ids:
                existing_plan["members"] = existing_plan_members
                existing_plan["staffing_mode"] = "ai_confirmed_with_temporary"
            formation_plan = confirmed_formation_plan(
                leader_agent_id=leader_agent_id,
                members=members,
                existing=existing_plan,
                team_goal=team_goal,
            )
            team = store.create_team(
                owner_account_id=owner,
                name=str(payload.get("name") or "").strip() or "未命名团队",
                leader_agent_id=leader_agent_id,
                members=members,
                description=str(payload.get("description") or "").strip(),
                instructions=str(payload.get("instructions") or payload.get("workflow") or "").strip(),
                team_spec=payload.get("team_spec") if isinstance(payload.get("team_spec"), dict) else None,
                formation_plan=formation_plan,
            )
        except KeyError as exc:
            rollback_temporary_agents()
            return JSONResponse({"ok": False, "error": f"智能体不存在: {exc}"}, status_code=404)
        except ValueError as exc:
            rollback_temporary_agents()
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except Exception:
            rollback_temporary_agents()
            raise
        return JSONResponse(_external_team_payloads(
            store,
            [team],
            owner_account_id=owner,
        )[0])

    @router.post("/api/external-teams/suggest")
    async def suggest_external_team(request: Request, payload: dict):
        requested_mode = str(payload.get("formation_mode") or "").strip().lower()
        if requested_mode not in FORMATION_MODES:
            return JSONResponse(
                {"ok": False, "error": "formation_mode 必须是 fast、ai 或 auto"},
                status_code=400,
            )
        owner = account_from_request(request).owner_account_id
        store = _external_store()
        agents = store.list_agents(owner_account_id=owner)

        if requested_mode == "auto":
            async def stream():
                started_at = time.perf_counter()
                fast_started_at = time.perf_counter()
                fallback = fast_team_suggestion(payload, agents)
                fast_ms = int((time.perf_counter() - fast_started_at) * 1000)
                first_event_ms = int((time.perf_counter() - started_at) * 1000)
                yield _draft_stream_line({
                    "type": "suggestion",
                    "phase": "fast",
                    "first_event_ms": first_event_ms,
                    "suggestion": _formation_response_payload(
                        fallback,
                        requested_mode="auto",
                        selected_mode="fast",
                        started_at=started_at,
                        fast_ms=fast_ms,
                    ),
                })
                if await request.is_disconnected():
                    return
                if fallback.get("decision_required"):
                    auto_reasons = ["user_decision_required"]
                    final = _formation_response_payload(
                        fallback,
                        requested_mode="auto",
                        selected_mode="fast",
                        started_at=started_at,
                        fast_ms=fast_ms,
                        fallback_reason="user_decision_required",
                    )
                    log.info(
                        "Formation Auto metrics decision=fast reasons=%s first_event_ms=%d "
                        "fast_ms=%d ai_ms=0 total_ms=%d",
                        ",".join(auto_reasons),
                        first_event_ms,
                        fast_ms,
                        int((time.perf_counter() - started_at) * 1000),
                    )
                    yield _draft_stream_line({
                        "type": "suggestion",
                        "phase": "final",
                        "auto_decision": "fast",
                        "auto_reasons": auto_reasons,
                        "suggestion": final,
                    })
                    return

                requires_ai, auto_reasons = formation_auto_decision(payload, fallback)
                if not requires_ai:
                    final = _formation_response_payload(
                        fallback,
                        requested_mode="auto",
                        selected_mode="fast",
                        started_at=started_at,
                        fast_ms=fast_ms,
                    )
                    log.info(
                        "Formation Auto metrics decision=fast reasons=none first_event_ms=%d "
                        "fast_ms=%d ai_ms=0 total_ms=%d",
                        first_event_ms,
                        fast_ms,
                        int((time.perf_counter() - started_at) * 1000),
                    )
                    yield _draft_stream_line({
                        "type": "suggestion",
                        "phase": "final",
                        "auto_decision": "fast",
                        "auto_reasons": [],
                        "suggestion": final,
                    })
                    return

                yield _draft_stream_line({
                    "type": "status",
                    "phase": "ai_reviewing",
                    "auto_decision": "ai",
                    "auto_reasons": auto_reasons,
                })
                if await request.is_disconnected():
                    return
                runtimes = [
                    _runtime_availability(runtime)
                    for runtime in store.list_runtimes()
                ]
                async with crew.owner_provider(owner) as provider:
                    final = await _run_formation_ai(
                        provider=provider,
                        payload=payload,
                        agents=agents,
                        fallback=fallback,
                        runtimes=runtimes,
                        requested_mode="auto",
                        started_at=started_at,
                        fast_ms=fast_ms,
                    )
                log.info(
                    "Formation Auto metrics decision=ai reasons=%s first_event_ms=%d "
                    "fast_ms=%d ai_ms=%d total_ms=%d",
                    ",".join(auto_reasons),
                    first_event_ms,
                    fast_ms,
                    int((final.get("timing") or {}).get("ai_ms") or 0),
                    int((final.get("timing") or {}).get("total_ms") or 0),
                )
                yield _draft_stream_line({
                    "type": "suggestion",
                    "phase": "final",
                    "auto_decision": "ai",
                    "auto_reasons": auto_reasons,
                    "suggestion": final,
                })

            return StreamingResponse(stream(), media_type="application/x-ndjson")

        started_at = time.perf_counter()
        fast_started_at = time.perf_counter()
        fallback = fast_team_suggestion(payload, agents)
        fast_ms = int((time.perf_counter() - fast_started_at) * 1000)
        if requested_mode == "fast":
            return JSONResponse(_formation_response_payload(
                fallback,
                requested_mode=requested_mode,
                selected_mode="fast",
                started_at=started_at,
                fast_ms=fast_ms,
            ))
        if fallback.get("decision_required"):
            return JSONResponse(_formation_response_payload(
                fallback,
                requested_mode=requested_mode,
                selected_mode="fast",
                started_at=started_at,
                fast_ms=fast_ms,
                fallback_reason="user_decision_required",
            ))
        runtimes = [
            _runtime_availability(runtime)
            for runtime in store.list_runtimes()
        ]
        async with crew.owner_provider(owner) as provider:
            result = await _run_formation_ai(
                provider=provider,
                payload=payload,
                agents=agents,
                fallback=fallback,
                runtimes=runtimes,
                requested_mode=requested_mode,
                started_at=started_at,
                fast_ms=fast_ms,
            )
        return JSONResponse(result)

    @router.post("/api/external-teams/draft/description")
    async def draft_external_team_description(request: Request, payload: dict) -> StreamingResponse:
        owner = account_from_request(request).owner_account_id
        agents = _external_store().list_agents(owner_account_id=owner)
        description_payload = {"name": str(payload.get("name") or "").strip()}
        initial = build_team_draft(description_payload, agents)
        cache_key = _draft_cache_key(
            "description",
            description_payload,
            owner_account_id=owner,
        )

        async def stream():
            yield _draft_stream_line({"type": "draft", "phase": "initial", "draft": initial})
            if not description_payload["name"] or await request.is_disconnected():
                return
            cached = description_draft_cache.get(cache_key)
            if cached is not None:
                optimized = build_team_draft(
                    {
                        "name": description_payload["name"],
                        "description": cached["description"],
                    },
                    agents,
                )
                optimized["description"] = cached["description"]
                yield _draft_stream_line({
                    "type": "draft",
                    "phase": "optimized",
                    "draft": optimized,
                    "llm_elapsed_ms": cached["llm_elapsed_ms"],
                    "cache_hit": True,
                })
                return

            safe_payload = _truncate_user_payload(description_payload)
            prompt = (
                "你是轻量团队目标助手。根据团队名称生成一份简短、可编辑的团队目标。"
                "description 必须正好四行，依次以‘1. 负责范围：’、‘2. 所需能力：’、"
                "‘3. 交付结果：’、‘4. 验收标准：’开头，每行只写一句，不举例。只返回 JSON："
                "{\"description\":\"...\"}。\n"
                f"输入：{json.dumps(safe_payload, ensure_ascii=False)}"
            )
            started_at = time.perf_counter()
            try:
                raw_text = ""
                last_description = ""
                finish_reason = ""
                async with crew.owner_provider(owner) as provider:
                    async for chunk in provider.stream_chat([
                        Message.system("你只输出可解析 JSON。"),
                        Message.user(prompt),
                    ], max_tokens=640):
                        if await request.is_disconnected():
                            return
                        if chunk.finish_reason:
                            finish_reason = chunk.finish_reason
                        if not chunk.delta_text:
                            continue
                        raw_text += chunk.delta_text
                        partial_description = _partial_json_string(raw_text, "description")
                        if partial_description and partial_description != last_description:
                            last_description = partial_description
                            yield _draft_stream_line({"type": "description_delta", "text": partial_description})
                raw = extract_json_object(raw_text)
                if raw is None:
                    raise ValueError(
                        "团队描述 LLM 返回不可解析 JSON "
                        f"finish_reason={finish_reason or 'unknown'} output_chars={len(raw_text)}"
                    )
                description = str(raw.get("description") or "").strip()
                if len(description) < 30:
                    raise ValueError("团队描述 LLM 返回内容过短")
                goal_prefixes = (
                    "1. 负责范围：",
                    "2. 所需能力：",
                    "3. 交付结果：",
                    "4. 验收标准：",
                )
                goal_lines = [line.strip() for line in description.splitlines() if line.strip()]
                if len(goal_lines) != len(goal_prefixes) or any(
                    not line.startswith(prefix)
                    for line, prefix in zip(goal_lines, goal_prefixes, strict=True)
                ):
                    raise ValueError("团队描述 LLM 未按四点目标格式返回")
                description = "\n".join(goal_lines)
                optimized = build_team_draft(
                    {"name": description_payload["name"], "description": description[:500]},
                    agents,
                )
                optimized["description"] = description[:500]
                elapsed_ms = int((time.perf_counter() - started_at) * 1000)
                description_draft_cache.put(cache_key, {
                    "description": description[:500],
                    "llm_elapsed_ms": elapsed_ms,
                })
                if not await request.is_disconnected():
                    yield _draft_stream_line({
                        "type": "draft",
                        "phase": "optimized",
                        "draft": optimized,
                        "llm_elapsed_ms": elapsed_ms,
                        "cache_hit": False,
                    })
            except (ProviderError, AttributeError, TypeError, ValueError) as exc:
                elapsed_ms = int((time.perf_counter() - started_at) * 1000)
                log.info(
                    "团队描述草案生成失败 fallback_used=true llm_elapsed_ms=%d error=%s",
                    elapsed_ms,
                    exc,
                )
                if not await request.is_disconnected():
                    yield _draft_stream_line({"type": "draft", "phase": "fallback", "draft": initial})

        return StreamingResponse(
            stream(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.post("/api/external-teams/draft/formation")
    async def draft_external_team_formation(request: Request, payload: dict) -> StreamingResponse:
        owner = account_from_request(request).owner_account_id
        agents = _external_store().list_agents(owner_account_id=owner)
        formation_payload = {
            "name": str(payload.get("name") or "").strip(),
            "description": str(payload.get("description") or "").strip(),
            "leader_agent_id": str(payload.get("leader_agent_id") or "").strip(),
        }
        initial = build_team_draft(formation_payload, agents)
        agent_catalog = _draft_agent_catalog(agents)
        role_catalog = _draft_role_catalog()
        cache_key = _draft_cache_key(
            "formation",
            formation_payload,
            owner_account_id=owner,
            catalogs=[
                sorted(agent_catalog, key=lambda item: item["id"]),
                sorted(role_catalog, key=lambda item: item["key"]),
            ],
        )

        async def stream():
            yield _draft_stream_line({"type": "draft", "phase": "initial", "draft": initial})
            leader_id = formation_payload["leader_agent_id"]
            if not formation_payload["name"] or not leader_id or await request.is_disconnected():
                return
            cached = formation_draft_cache.get(cache_key)
            if cached is not None:
                optimized_payload = {**formation_payload}
                if cached["draft_slots"]:
                    optimized_payload["draft_slots"] = cached["draft_slots"]
                optimized = build_team_draft(optimized_payload, agents)
                yield _draft_stream_line({
                    "type": "draft",
                    "phase": "optimized",
                    "draft": optimized,
                    "llm_elapsed_ms": cached["llm_elapsed_ms"],
                    "cache_hit": True,
                })
                return

            safe_payload = _truncate_user_payload(formation_payload)
            valid_agent_ids = {str(agent.get("id") or "") for agent in agent_catalog}
            valid_role_keys = {str(role.get("key") or "") for role in role_catalog}
            prompt = (
                "你是轻量团队组队草案助手。根据团队名称、团队描述、已选 Leader、可用智能体和标准角色目录，"
                "生成最合适的非 Leader 协作槽位建议。\n"
                "要求：\n"
                "1. slots 只包含非 Leader 协作角色，0 到 5 个；role_key 必须来自 role_catalog；agent_id 必须来自 available_agents，可不填。\n"
                "2. 不要为了凑人数选满角色，普通任务用最小充分角色；不要把 Leader 放入 slots。\n"
                "3. 只返回 JSON：{\"slots\":[{\"role_key\":\"...\",\"agent_id\":\"...\",\"required\":true}]}。\n"
                f"输入：{json.dumps(safe_payload, ensure_ascii=False)}\n"
                f"available_agents：{json.dumps(agent_catalog, ensure_ascii=False)}\n"
                f"role_catalog：{json.dumps(role_catalog, ensure_ascii=False)}"
            )
            started_at = time.perf_counter()
            try:
                raw_text = ""
                async with crew.owner_provider(owner) as provider:
                    async for chunk in provider.stream_chat([
                        Message.system("你只输出可解析 JSON。"),
                        Message.user(prompt),
                    ], max_tokens=320):
                        if await request.is_disconnected():
                            return
                        raw_text += chunk.delta_text or ""
                raw = extract_json_object(raw_text)
                if raw is None:
                    raise ValueError("团队协作方案 LLM 返回不可解析 JSON")
                draft_slots = _normalize_ai_draft_slots(
                    raw.get("slots"),
                    valid_agent_ids,
                    valid_role_keys,
                    leader_id,
                )
                optimized_payload = {**formation_payload}
                if draft_slots:
                    optimized_payload["draft_slots"] = draft_slots
                optimized = build_team_draft(optimized_payload, agents)
                elapsed_ms = int((time.perf_counter() - started_at) * 1000)
                formation_draft_cache.put(cache_key, {
                    "draft_slots": draft_slots,
                    "llm_elapsed_ms": elapsed_ms,
                })
                if not await request.is_disconnected():
                    yield _draft_stream_line({
                        "type": "draft",
                        "phase": "optimized",
                        "draft": optimized,
                        "llm_elapsed_ms": elapsed_ms,
                        "cache_hit": False,
                    })
            except (ProviderError, AttributeError, TypeError, ValueError) as exc:
                elapsed_ms = int((time.perf_counter() - started_at) * 1000)
                log.info(
                    "团队协作方案生成失败 fallback_used=true llm_elapsed_ms=%d error=%s",
                    elapsed_ms,
                    exc,
                )
                if not await request.is_disconnected():
                    yield _draft_stream_line({"type": "draft", "phase": "fallback", "draft": initial})

        return StreamingResponse(
            stream(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.post("/api/external-teams/roles/suggest")
    async def suggest_external_team_role(request: Request, payload: dict) -> JSONResponse:
        agent = None
        agent_id = str(payload.get("agent_id") or "").strip()
        if agent_id:
            try:
                agent = _external_store().get_agent(
                    agent_id,
                    owner_account_id=account_from_request(request).owner_account_id,
                )
            except KeyError:
                return JSONResponse({"ok": False, "error": "智能体不存在"}, status_code=404)
        return JSONResponse(suggest_role_description(payload, agent))

    @router.delete("/api/external-teams/{team_id}")
    async def delete_external_team(request: Request, team_id: str) -> JSONResponse:
        owner = account_from_request(request).owner_account_id
        store = _external_store()
        try:
            team = store.get_team(team_id, owner_account_id=owner)
            plan = team.get("formation_plan") if isinstance(team.get("formation_plan"), dict) else {}
            temporary_ids = [
                str(member.get("agent_id") or "")
                for member in (plan.get("members") or [])
                if isinstance(member, dict)
                and member.get("selection_source") == "ai_temporary"
                and str(member.get("agent_id") or "")
            ]
            store.delete_team(team_id, owner_account_id=owner)
        except KeyError:
            return JSONResponse({"ok": False, "error": "团队不存在"}, status_code=404)
        for agent_id in temporary_ids:
            try:
                store.delete_agent(agent_id, owner_account_id=owner)
            except ValueError:
                log.info("临时成员仍被其他团队使用，保留: agent_id=%s", agent_id)
            except KeyError:
                pass
        return JSONResponse({"ok": True})

    return router
