"""Create isolated planning previews without invoking any team executor."""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from typing import Any

from crew.collaboration.planning.plan import PlanningError, make_plan, validate_work_units
from crew.collaboration.planning.store import PlanningStore
from crew.core.text_parsing import extract_json_object
from crew.core.types import Message


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
                        "responsibility_source": str(saved.get("responsibility_source") or "unknown")})
    if not members:
        raise PlanningError("团队没有可规划的成员")
    return {"team_id": str(team.get("id") or ""), "name": str(team.get("name") or ""),
            "goal": str(team.get("description") or ""), "formation_version": formation.get("version"),
            "team_updated_at": team.get("updated_at"), "leader_id": str(team.get("leader_agent_id") or ""),
            "policies": {key: formation.get(key) for key in ("runtime_allow_suggestions", "runtime_allow_auto_reassign")},
            "members": members}


SYSTEM = """你是任务规划器。只输出 JSON，不执行任务，也不要分配人员。按本次任务拆分，简单任务可只有一个工作单元；
不要为了让所有成员参与而制造重复工作。依赖只表示必须等待上游交付。
输出格式：{"coverage_note":"覆盖说明", "work_units":[{"key":"英文标识", "objective":"具体目标",
"inputs":[],"expected_outputs":["交付物"],"completion_criteria":["完成条件"],
"depends_on":["前置单元标识"],"required_tools":[],"required_capabilities":[]}]}。
若任务缺少关键输入且无法规划，返回 {"critical_missing_info":"具体缺失内容"}。"""

ASSIGN_SYSTEM = """你是团队任务分配器，只输出 JSON。对每个工作单元比较所有成员的职责及本次任务要求。
优先级：用户本次明确指定负责人 > 用户填写的长期职责 > 已确认的角色和职责建议 > 其他合理推断。
理解同义表达，避免只靠关键词；能力未知不是能力缺失，不因此覆盖用户分工。不要添加或修改工作单元。
本次明确指定时 assignment_source=task_instruction，responsibility_ref 必须逐字引用用户 goal 的相应原文。
长期职责匹配时 responsibility_ref 必须逐字引用该成员 responsibility 的相关原文。
只有确实有多个合理人选且会影响结果或无法决定时，ambiguity 填具体问题且 candidate_member_id 留空。
输出 {"assignments":[{"key":"原工作单元 key","candidate_member_id":"成员标识", "candidate_reason":"具体依据",
"assignment_source":"task_instruction|responsibility|inferred", "responsibility_ref":"原文或空", "ambiguity":""}]}。
每个工作单元恰好一项，member_id 必须来自团队。"""


def merge_assignments(raw: dict[str, Any], proposals: Any) -> dict[str, Any]:
    """Match decisions by key; never trust the allocator to rewrite work."""
    units = validate_work_units(raw.get("work_units"))
    items = proposals.get("assignments") if isinstance(proposals, dict) else None
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise PlanningError("负责人建议格式无效")
    by_key = {str(item.get("key") or ""): item for item in items}
    if len(by_key) != len(items) or set(by_key) != {unit["key"] for unit in units}:
        raise PlanningError("负责人建议未覆盖所有工作单元或包含重复单元")
    fields = ("candidate_member_id", "candidate_reason", "assignment_source", "responsibility_ref", "ambiguity")
    for unit in units:
        unit.update({field: by_key[unit["key"]].get(field, "") for field in fields})
    return {"work_units": units, "coverage_note": raw.get("coverage_note", "")}


class PlanningService:
    def __init__(self, store: PlanningStore) -> None:
        self.store = store

    async def create(self, *, owner: str, request_id: str, session_id: str, goal: str,
                     team: dict[str, Any], provider: Any, inputs: list[str] | None = None) -> dict[str, Any]:
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
        error = ""
        plan = None
        for attempt in range(2):
            try:
                messages = [Message.system(SYSTEM), Message.user(json.dumps(payload, ensure_ascii=False))]
                if attempt:
                    messages.append(Message.user("上次结果无效：" + error + "。请修正并只输出完整 JSON。"))
                response = await asyncio.wait_for(provider.chat(messages), timeout=60)
                raw = extract_json_object(response.text or "")
                if not isinstance(raw, dict):
                    raise PlanningError("模型没有返回有效 JSON")
                if raw.get("critical_missing_info"):
                    raise PlanningError("缺少规划所需信息：" + str(raw["critical_missing_info"]))
                units = validate_work_units(raw.get("work_units"))
                assignment_input = {"goal": payload["goal"], "members": snapshot["members"], "work_units": units}
                assignment_response = await asyncio.wait_for(provider.chat([
                    Message.system(ASSIGN_SYSTEM), Message.user(json.dumps(assignment_input, ensure_ascii=False)),
                ]), timeout=60)
                proposals = extract_json_object(assignment_response.text or "")
                plan = make_plan(merge_assignments(raw, proposals), snapshot["members"], goal=payload["goal"])
                break
            except (PlanningError, ValueError, TypeError, AttributeError, asyncio.TimeoutError) as exc:
                error = str(exc) or "规划模型响应超时"
            except asyncio.CancelledError:
                self.store.finish(owner, task["task_id"], error="规划已中断，请使用新的请求重试")
                raise
            except Exception:
                error = "规划服务不可用，请稍后重试"
        # Storage errors must propagate; never report a plan that was not committed.
        self.store.finish(owner, task["task_id"], plan=plan, error="" if plan is not None else error)
        result = self.store.get(owner, task["task_id"])
        assert result is not None
        return result
