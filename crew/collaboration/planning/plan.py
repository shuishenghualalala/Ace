"""Validate proposed work and make assignment decisions in one place.

This module has no executor side effects. A plan is not an execution record.
"""
from __future__ import annotations

import copy
import re
import uuid
from typing import Any


class PlanningError(ValueError):
    """The proposed plan cannot safely be used."""


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanningError(f"{field}不能为空")
    return value.strip()


def _strings(value: Any, field: str, *, required: bool = False) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise PlanningError(f"{field}必须是非空字符串列表")
    result = [v.strip() for v in value]
    if required and not result:
        raise PlanningError(f"{field}不能为空")
    return result


def validate_work_units(raw: Any) -> list[dict[str, Any]]:
    """Reject malformed references rather than silently deleting edges or work."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= 12:
        raise PlanningError("工作单元数量必须在 1 到 12 之间")
    units: list[dict[str, Any]] = []
    ids: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise PlanningError("工作单元格式无效")
        key = _text(item.get("key"), "工作单元标识")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key) or key in ids:
            raise PlanningError(f"工作单元标识无效或重复：{key}")
        ids.add(key)
        units.append({
            "key": key,
            "objective": _text(item.get("objective"), f"{key} 的目标"),
            "inputs": _strings(item.get("inputs", []), f"{key} 的输入"),
            "expected_outputs": _strings(item.get("expected_outputs"), f"{key} 的交付物", required=True),
            "completion_criteria": _strings(item.get("completion_criteria"), f"{key} 的完成条件", required=True),
            "depends_on": _strings(item.get("depends_on", []), f"{key} 的依赖"),
            "required_tools": _strings(item.get("required_tools", []), f"{key} 的工具要求"),
            "required_capabilities": _strings(item.get("required_capabilities", []), f"{key} 的能力要求"),
            "requirements_source": "model_suggestion",
            "ambiguity": str(item.get("ambiguity") or "").strip(),
            "matches": item.get("matches") if isinstance(item.get("matches"), list) else [],
            "excluded_members": item.get("excluded_members") if isinstance(item.get("excluded_members"), list) else [],
        })
    for unit in units:
        deps = unit["depends_on"]
        if len(set(deps)) != len(deps) or unit["key"] in deps or any(d not in ids for d in deps):
            raise PlanningError(f"{unit['key']} 的依赖无效")
    remaining = {u["key"]: set(u["depends_on"]) for u in units}
    while remaining:
        ready = {key for key, deps in remaining.items() if not deps}
        if not ready:
            raise PlanningError("工作单元依赖存在循环")
        remaining = {key: deps - ready for key, deps in remaining.items() if key not in ready}
    return units


def assign_units(units: list[dict[str, Any]], members: list[dict[str, Any]], goal: str = "",
                 clarifications: list[dict[str, str]] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Accept evidence-backed suggestions; unresolved choices remain decisions.

    A model suggestion is not evidence of a member's tool availability. The
    executor must check hard requirements before dispatch.
    """
    member_map = {str(m.get("member_id") or ""): m for m in members if m.get("member_id")}
    if not member_map or len(member_map) != len(members):
        raise PlanningError("团队成员为空或成员标识重复")
    assignments, decisions = [], []
    for unit in units:
        ranked: dict[int, dict[str, dict[str, Any]]] = {}
        for match in unit["matches"]:
            if not isinstance(match, dict):
                raise PlanningError(f"{unit['key']} 的分配依据格式无效")
            member_id = str(match.get("member_id") or "").strip()
            member = member_map.get(member_id)
            ref = str(match.get("reference") or "").strip()
            reason = str(match.get("reason") or "").strip()
            kind = str(match.get("kind") or "").strip()
            if not member or not reason or kind not in {"task_instruction", "responsibility", "inferred"}:
                raise PlanningError(f"{unit['key']} 的成员或分配依据无效")
            if member.get("excluded") is True or member_id in unit["excluded_members"]:
                continue
            if kind == "task_instruction":
                names = [member_id, str(member.get("name") or "").strip()]
                in_goal = bool(ref and ref in goal)
                in_answer = any(ref and ref in str(item.get("answer") or "") for item in (clarifications or []))
                if not (in_goal or in_answer) or not any(name and name in ref for name in names):
                    raise PlanningError(f"{unit['key']} 的任务指定依据无效")
                rank, source = 3, "task_instruction" if in_goal else "user_clarification"
            elif kind == "responsibility":
                if not ref or ref not in str(member.get("responsibility") or ""):
                    raise PlanningError(f"{unit['key']} 的职责依据与团队快照不一致")
                if member.get("responsibility_source") == "user":
                    rank, source = 2, "user_responsibility"
                elif member.get("responsibility_source") == "confirmed_suggestion":
                    rank, source = 1, "confirmed_suggestion"
                else:
                    rank, source = 0, "model_suggestion"
            else:
                if ref:
                    raise PlanningError(f"{unit['key']} 的推断不能冒充用户原文")
                rank, source = 0, "model_suggestion"
            ranked.setdefault(rank, {})[member_id] = {
                "member_id": member_id, "source": source, "reason": reason,
                "responsibility_ref": ref, "capability_status": "unverified",
            }
        candidates = ranked[max(ranked)] if ranked else {}
        if candidates and (not unit["ambiguity"] or (max(ranked) == 3 and len(candidates) == 1)) and (len(candidates) == 1 or max(ranked) == 0):
            assignments.append({"assignment_id": uuid.uuid4().hex, "unit_key": unit["key"],
                                **next(iter(candidates.values()))})
            continue
        options = list(candidates) if candidates else [key for key, value in member_map.items()
                                                        if not value.get("excluded") and key not in unit["excluded_members"]]
        if not options:
            raise PlanningError(f"{unit['key']} 没有可选的团队成员")
        decisions.append({"decision_id": uuid.uuid4().hex, "unit_key": unit["key"],
                          "kind": "assignment", "question": unit["ambiguity"] or "请选择此工作单元的负责人",
                          "options": [{"member_id": key, "name": member_map[key].get("name", key)} for key in options],
                          "state": "open"})
    return assignments, decisions


def make_plan(raw: dict[str, Any], members: list[dict[str, Any]], goal: str = "",
              clarifications: list[dict[str, str]] | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PlanningError("规划结果不是对象")
    units = validate_work_units(raw.get("work_units"))
    assignments, decisions = assign_units(units, members, goal, clarifications)
    ids = {u["key"]: uuid.uuid4().hex for u in units}
    for unit in units:
        unit["unit_id"] = ids[unit["key"]]
        unit["depends_on"] = [ids[k] for k in unit["depends_on"]]
        unit.pop("matches", None)
        unit.pop("ambiguity", None)
        unit.pop("excluded_members", None)
    for entry in [*assignments, *decisions]:
        entry["unit_id"] = ids[entry.pop("unit_key")]
    return {"plan_id": uuid.uuid4().hex, "version": 1,
            "status": "waiting_user" if decisions else "planned", "work_units": copy.deepcopy(units),
            "assignments": assignments, "decisions": decisions,
            "coverage_note": str(raw.get("coverage_note") or "").strip(),
            "member_constraints": copy.deepcopy(raw.get("member_constraints") or [])}
