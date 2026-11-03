"""Create isolated planning previews without invoking any team executor."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import uuid
from typing import Any

from crew.collaboration.planning.plan import PlanningError, make_plan, reference_names_member, validate_work_units
from crew.collaboration.planning.store import PlanningStore
from crew.core.text_parsing import extract_json_object
from crew.core.types import Message
from crew.state.logging import get_logger

log = get_logger("collaboration.planning")


async def _structured_chat(provider: Any, messages: list[Message]) -> Any:
    """Use supported structured-output options without assuming provider support."""
    params = inspect.signature(provider.chat).parameters
    accepts_kwargs = any(item.kind == inspect.Parameter.VAR_KEYWORD for item in params.values())
    options = {"max_tokens": 4096, "response_format": {"type": "json_object"}, "reasoning_mode": "disabled"}
    return await provider.chat(messages, **{key: value for key, value in options.items() if accepts_kwargs or key in params})


def team_snapshot(team: dict[str, Any]) -> dict[str, Any]:
    formation = team.get("formation_plan") if isinstance(team.get("formation_plan"), dict) else {}
    responsibilities = {str(m.get("agent_id") or ""): m for m in formation.get("members", []) if isinstance(m, dict)}
    members = []
    for member in team.get("members", []):
        if not isinstance(member, dict):
            continue
        member_id = str(member.get("agent_id") or "").strip()
        if not member_id:
            continue
        saved = responsibilities.get(member_id, {})
        structured = saved.get("responsibility") or {}
        original = str(saved.get("original_user_responsibility") or "").strip()
        text = original or str(saved.get("responsibility_markdown") or "").strip()
        if not text and isinstance(structured, dict):
            text = "\n".join([str(structured.get("mission") or ""), *[str(v) for v in structured.get("deliverables", [])]])
        members.append({"member_id": member_id, "name": str(member.get("agent_name") or member_id),
                        "role": str(saved.get("role_label") or member.get("role_label") or ""),
                        "responsibility": text.strip(),
                        "original_user_responsibility": original,
                        "responsibility_structured": structured if isinstance(structured, dict) else {},
                        "configured_capabilities": list(member.get("assigned_capabilities") or []),
                        "required": bool(saved.get("required")),
                        "excluded": bool(saved.get("excluded")),
                        "responsibility_source": str(saved.get("responsibility_source") or "unknown")})
    if not members:
        raise PlanningError("团队没有可规划的成员")
    return {"team_id": str(team.get("id") or ""), "name": str(team.get("name") or ""),
            "goal": str(team.get("description") or ""), "formation_version": formation.get("version"),
            "team_updated_at": team.get("updated_at"), "leader_id": str(team.get("leader_agent_id") or ""),
            "policies": {key: formation.get(key) for key in ("runtime_allow_suggestions", "runtime_allow_auto_reassign")},
            "members": members}


def runtime_team_record(team: Any) -> dict[str, Any]:
    """Adapt an in-process team's actual roster into the same snapshot input."""
    specs = [team.leader_spec, *list((getattr(team, "members", {}) or {}).values())]
    members = []
    formation_members = []
    for spec in specs:
        member_id = str(spec.member_id or "").strip()
        metadata = spec.metadata if isinstance(spec.metadata, dict) else {}
        responsibility = metadata.get("formation_responsibility")
        members.append({"agent_id": member_id, "agent_name": str(spec.name or member_id),
                        "role_label": str(metadata.get("role_label") or spec.role or ""),
                        "assigned_capabilities": list(spec.capabilities or [])})
        formation_members.append({"agent_id": member_id, "role_label": str(metadata.get("role_label") or ""),
                                  "responsibility": dict(responsibility) if isinstance(responsibility, dict) else {},
                                  "responsibility_markdown": str(metadata.get("formation_responsibility_markdown") or spec.role or ""),
                                  "responsibility_source": str(metadata.get("responsibility_source") or "unknown")})
    spec = getattr(team, "team_spec", {}) or {}
    spec = spec if isinstance(spec, dict) else {}
    policy = getattr(team, "staffing_policy", {}) or {}
    policy = policy if isinstance(policy, dict) else {}
    content = {"description": str(spec.get("goal") or ""), "members": members,
               "formation_plan": {"members": formation_members,
                                  **{key: policy.get(key) for key in
                                     ("runtime_allow_suggestions", "runtime_allow_auto_reassign")}}}
    version = hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {"id": "in_process_team", "name": str(getattr(team, "display_name", "") or ""),
            "leader_agent_id": str(team.leader_spec.member_id or ""), "updated_at": version, **content}


SYSTEM = """你是任务规划器。只输出 JSON，不执行任务，也不要分配人员。按本次任务拆分，简单任务可只有一个工作单元；
不要为了让所有成员参与而制造重复工作。依赖只表示必须等待上游交付。
如用户明确指定或排除某成员，记录 member_constraints：每项包含 member_id、action=assign|exclude、
unit_keys（对应工作标识列表）和 reference（来自用户任务或补充回答、包含成员名称的原文）。
不要将模型推测当成用户约束。输出格式：{"coverage_note":"覆盖说明", "member_constraints":[], "work_units":[{"key":"英文标识", "objective":"具体目标",
"inputs":[],"expected_outputs":["交付物"],"completion_criteria":["完成条件"],
"depends_on":["前置单元标识"],"required_tools":[],"required_capabilities":[]}]}。
若任务缺少关键输入且无法规划，返回 {"critical_missing_info":"具体缺失内容"}。"""

ASSIGN_SYSTEM = """你是团队职责语义分析器，只输出 JSON。对每个工作单元逐一比较所有成员的职责及本次任务要求，列出所有合理匹配。
优先级：用户本次明确指定负责人 > 用户填写的长期职责 > 已确认的角色和职责建议 > 其他合理推断。
理解同义表达，避免只靠关键词；能力未知不是能力缺失，不因此覆盖用户分工。不要添加或修改工作单元。
本次明确指定时 kind=task_instruction，reference 必须逐字引用用户 goal 或 clarifications 中包含成员名字或 ID 的原文。
长期职责匹配时 kind=responsibility，reference 必须逐字引用该成员 responsibility 的相关原文。
推断使用 kind=inferred，reference 留空。不要只提供你认为最合适的一人，系统会按来源优先级决定。
只有存在实际歧义时，ambiguity 填具体问题。输出 {"assignments":[{"key":"原工作单元 key",
"matches":[{"member_id":"成员标识","kind":"task_instruction|responsibility|inferred",
"reference":"依据原文或空","reason":"具体依据"}],"ambiguity":""}]}。
每个工作单元恰好一项，member_id 必须来自团队。"""


def merge_assignments(raw: dict[str, Any], proposals: Any, *, members: list[dict[str, Any]],
                      goal: str, clarifications: list[dict[str, str]]) -> dict[str, Any]:
    """Match decisions by key; never trust the allocator to rewrite work."""
    units = validate_work_units(raw.get("work_units"))
    items = proposals.get("assignments") if isinstance(proposals, dict) else None
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise PlanningError("负责人建议格式无效")
    by_key = {str(item.get("key") or ""): item for item in items}
    if len(by_key) != len(items) or set(by_key) != {unit["key"] for unit in units}:
        raise PlanningError("负责人建议未覆盖所有工作单元或包含重复单元")
    fields = ("matches", "ambiguity")
    for unit in units:
        item = by_key[unit["key"]]
        if not isinstance(item.get("matches"), list):
            raise PlanningError(f"{unit['key']} 缺少成员匹配列表")
        unit.update({field: item.get(field, [] if field == "matches" else "") for field in fields})
    constraints = raw.get("member_constraints", [])
    if not isinstance(constraints, list):
        raise PlanningError("用户成员约束格式无效")
    member_map = {str(member.get("member_id") or ""): member for member in members}
    by_unit = {unit["key"]: unit for unit in units}
    seen_constraints: dict[tuple[str, str], str] = {}
    for constraint in constraints:
        if not isinstance(constraint, dict):
            raise PlanningError("用户成员约束格式无效")
        member_id = str(constraint.get("member_id") or "").strip()
        member = member_map.get(member_id)
        ref = str(constraint.get("reference") or "").strip()
        action = str(constraint.get("action") or "").strip()
        keys = constraint.get("unit_keys")
        answer_texts = [str(item.get("answer") or "") for item in clarifications]
        if (not member or action not in {"assign", "exclude"} or not isinstance(keys, list) or not keys
                or any(not isinstance(key, str) or key not in by_unit for key in keys)
                or not ref or not (ref in goal or any(ref in answer for answer in answer_texts))):
            raise PlanningError("用户成员约束缺少有效的任务或原文依据")
        name = str(member.get("name") or "").strip()
        if not reference_names_member(ref, member):
            raise PlanningError("用户成员约束的原文未指出对应成员")
        if name and name in ref and member_id not in ref and sum(m.get("name") == name for m in members) > 1:
            raise PlanningError("用户指定的成员名称不唯一，请明确成员")
        for key in keys:
            previous_action = seen_constraints.setdefault((key, member_id), action)
            if previous_action != action:
                raise PlanningError("同一工作对成员同时指定和排除，请明确本次分工")
            if action == "exclude":
                by_unit[key]["excluded_members"].append(member_id)
            else:
                by_unit[key]["matches"].append({"member_id": member_id, "kind": "task_instruction",
                                                "reference": ref, "reason": "用户本次任务明确指定"})
    return {"work_units": units, "coverage_note": raw.get("coverage_note", ""), "member_constraints": constraints}


class PlanningService:
    def __init__(self, store: PlanningStore) -> None:
        self.store = store

    async def create(self, *, owner: str, request_id: str, session_id: str, goal: str,
                     team: dict[str, Any], provider: Any, inputs: list[Any] | None = None) -> dict[str, Any]:
        if not owner or not request_id or not goal.strip():
            raise PlanningError("账户、请求标识和任务目标不能为空")
        snapshot = team_snapshot(team)
        payload = {"session_id": session_id, "goal": goal.strip(), "inputs": inputs or [], "team": snapshot}
        # Idempotency identifies the request, not a team's later mutable state.
        request = {"session_id": session_id, "goal": goal.strip(), "inputs": inputs or [], "team_id": snapshot["team_id"]}
        digest = hashlib.sha256(json.dumps(request, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        task = {"task_id": uuid.uuid4().hex, "request_id": request_id, **payload}
        existing = self.store.create(owner, task, digest)
        if existing["task"]["task_id"] != task["task_id"]:
            return existing
        return await self._plan_task(owner=owner, task=task, provider=provider)

    async def answer_clarification(self, *, owner: str, task_id: str, decision_id: str,
                                   answer: str, provider: Any) -> dict[str, Any]:
        result, claimed = self.store.answer_clarification(owner, task_id, decision_id, answer)
        if not claimed:
            return result
        return await self._plan_task(owner=owner, task=result["task"], provider=provider)

    async def _plan_task(self, *, owner: str, task: dict[str, Any], provider: Any) -> dict[str, Any]:
        payload = {key: task[key] for key in ("session_id", "goal", "inputs", "team")}
        payload["inputs"] = [
            {key: value for key, value in item.items() if key != "path"} if isinstance(item, dict) else item
            for item in payload["inputs"]
        ]
        payload["clarifications"] = task.get("clarifications", [])
        snapshot = task["team"]
        error = ""
        plan = None
        for attempt in range(2):
            try:
                messages = [Message.system(SYSTEM), Message.user(json.dumps(payload, ensure_ascii=False))]
                if attempt:
                    messages.append(Message.user("上次结果无效：" + error + "。请修正并只输出完整 JSON。"))
                response = await asyncio.wait_for(_structured_chat(provider, messages), timeout=60)
                raw = extract_json_object(response.text or "")
                if not isinstance(raw, dict):
                    log.warning("规划响应无有效 JSON task=%s finish=%s content_chars=%s reasoning_chars=%s",
                                task["task_id"], getattr(response, "finish_reason", None),
                                len(response.text or ""), len(getattr(response, "reasoning_content", "") or ""))
                    raise PlanningError("模型没有返回有效 JSON")
                if raw.get("critical_missing_info"):
                    question = str(raw["critical_missing_info"]).strip()
                    previous = task.get("clarifications", [])
                    if any(item.get("question") == question for item in previous):
                        raise PlanningError("已收到补充信息，但规划仍无法消除同一项缺失，请重新描述任务")
                    plan = {"plan_id": uuid.uuid4().hex, "version": len(previous) + 1,
                            "status": "waiting_user", "work_units": [], "assignments": [],
                            "decisions": [{"decision_id": uuid.uuid4().hex, "kind": "clarification",
                                           "question": question, "state": "open", "options": []}],
                            "coverage_note": ""}
                    break
                units = validate_work_units(raw.get("work_units"))
                assignment_input = {"goal": payload["goal"], "clarifications": payload["clarifications"],
                                    "members": snapshot["members"], "work_units": units}
                assignment_response = await asyncio.wait_for(_structured_chat(provider, [
                    Message.system(ASSIGN_SYSTEM), Message.user(json.dumps(assignment_input, ensure_ascii=False)),
                ]), timeout=60)
                proposals = extract_json_object(assignment_response.text or "")
                plan = make_plan(merge_assignments(raw, proposals, members=snapshot["members"], goal=payload["goal"],
                                                   clarifications=payload["clarifications"]), snapshot["members"], goal=payload["goal"],
                                 clarifications=payload["clarifications"])
                plan["version"] = len(payload["clarifications"]) + 1
                break
            except (PlanningError, ValueError, TypeError, AttributeError, asyncio.TimeoutError) as exc:
                error = str(exc) or "规划模型响应超时"
                log.warning("规划尝试失败 task=%s attempt=%s type=%s", task["task_id"], attempt + 1, type(exc).__name__)
            except asyncio.CancelledError:
                self.store.finish(owner, task["task_id"], error="规划已中断，请使用新的请求重试")
                raise
            except Exception as exc:
                error = "规划服务不可用，请稍后重试"
                log.warning("规划模型请求失败 task=%s attempt=%s type=%s", task["task_id"], attempt + 1, type(exc).__name__)
        # Storage errors must propagate; never report a plan that was not committed.
        self.store.finish(owner, task["task_id"], plan=plan, error="" if plan is not None else error)
        result = self.store.get(owner, task["task_id"])
        assert result is not None
        return result
