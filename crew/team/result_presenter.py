"""Presentation helpers for Team chat, summaries, mentions, and artifacts."""

from __future__ import annotations

from pathlib import Path
import re
import time
from typing import Any

from crew.state.home import safe_path_segment
from crew.team.models import TeamPlan, TeamPlanNode

WORKFLOW_LANE_ORDER = {
    "lead": 10,
    "plan": 20,
    "design": 30,
    "build": 40,
    "verify": 50,
    "release": 60,
    "docs": 70,
    "summary": 80,
    "other": 90,
}


def workflow_lane_order(lane: str) -> int:
    return WORKFLOW_LANE_ORDER.get(str(lane or "").strip().lower(), WORKFLOW_LANE_ORDER["other"])


def infer_node_workflow_lane(node_id: str, assignee: str = "") -> str:
    lowered_id = str(node_id or "").strip().lower()
    lowered_assignee = str(assignee or "").strip().lower()
    if "summary" in lowered_id:
        return "summary"
    if lowered_id.startswith("leader_") or lowered_assignee == "leader":
        return "lead"
    for lane in ("lead", "plan", "design", "build", "verify", "release", "docs"):
        if lowered_id == lane or lowered_id.startswith(f"{lane}_") or lowered_id.endswith(f"_{lane}"):
            return lane
    return "other"


def node_role_label(assignee: str, lane: str) -> str:
    if str(assignee or "").strip().lower() == "leader" or lane == "lead":
        return "拆解任务、派活跟踪、汇总反馈"
    return {
        "plan": "需求规划、范围拆解",
        "design": "方案设计、体验约束",
        "build": "编码实现、产物落地",
        "verify": "测试验证、质量把关",
        "release": "交付整理、发布确认",
        "docs": "文档整理、结论输出",
        "summary": "汇总结论、验收反馈",
    }.get(lane, "按节点协议处理子任务")


def task_error_kind(error: str, result: str = "") -> str:
    text = f"{error} {result}"
    if not text.strip():
        return ""
    if "429" in text or "RPM" in text or "限流" in text:
        return "rate_limit"
    if "delegate_to_teammate" in text:
        return "delegate_tool_unavailable"
    return "runtime_error"


def node_display_progress(
    *,
    node_id: str,
    title: str = "",
    assignee: str = "",
    error: str = "",
    result: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = dict(metadata or {})
    lane = str(meta.get("workflow_lane") or infer_node_workflow_lane(node_id, assignee)).strip().lower() or "other"
    if lane not in WORKFLOW_LANE_ORDER:
        lane = "other"
    display_order = meta.get("display_order")
    try:
        order = int(display_order) if display_order is not None and str(display_order).strip() else workflow_lane_order(lane)
    except (TypeError, ValueError):
        order = workflow_lane_order(lane)
    role_label = str(meta.get("role_label") or "").strip() or node_role_label(assignee, lane)
    error_kind = str(meta.get("error_kind") or "").strip() or task_error_kind(error, result)
    progress = {
        "workflow_lane": lane,
        "display_order": order,
        "role_label": role_label,
        "error_kind": error_kind,
    }
    for key in (
        "agent_log_style",
        "execution_events",
        "execution_contract",
        "required_capabilities",
        "capability_source",
        "runtime_reflections",
        "policy_report",
        "planner",
        "plan_strategy",
        "full_result_ref",
        "full_result_bytes",
        "display_title",
        "display_subject",
        "display_action",
        "full_title",
        "runtime_staffing",
        "runtime_blocking",
        "runtime_reassignment",
        "runtime_recovery",
        "previous_assignee",
        "unassigned_reason",
        "blocked_by_nodes",
    ):
        if key in meta:
            progress[key] = meta[key]
    return progress


def is_team_chat_noise(text: str) -> bool:
    stripped = str(text or "").strip()
    if not stripped:
        return True
    noise_prefixes = (
        "正在使用工具：",
        "正在使用工具:",
        "调用工具：",
        "调用工具:",
        "工具 ",
    )
    return stripped.startswith(noise_prefixes)


def _node_dict_metadata(node: dict[str, Any]) -> dict[str, Any]:
    metadata = node.get("metadata")
    if isinstance(metadata, dict):
        return metadata
    progress = node.get("progress")
    return progress if isinstance(progress, dict) else {}


def _workflow_lane(metadata: dict[str, Any]) -> str:
    return str(metadata.get("workflow_lane") or "other").strip().lower() or "other"


def _assignment_text_for(*, node_id: str, assignee: str, title: str, metadata: dict[str, Any]) -> str:
    target = str(assignee or "").strip()
    label = str(title or node_id or "团队节点").strip()
    if _workflow_lane(metadata) in {"plan", "design"}:
        return f"@{target} {label}：请只写方案，先不要执行验证。"
    return f"@{target} {label}"


def node_dict_is_review_submission(node: dict[str, Any]) -> bool:
    return _workflow_lane(_node_dict_metadata(node)) in {"plan", "design"}


def node_dict_is_verify_execution(node: dict[str, Any]) -> bool:
    return _workflow_lane(_node_dict_metadata(node)) == "verify"


def node_dict_assignment_text(node: dict[str, Any]) -> str:
    return _assignment_text_for(
        node_id=str(node.get("node_id") or ""),
        assignee=str(node.get("assignee") or ""),
        title=str(node.get("title") or node.get("node_id") or "团队节点"),
        metadata=_node_dict_metadata(node),
    )


def node_dict_should_show_assignment(node: dict[str, Any], edges: list[dict[str, Any]]) -> bool:
    del edges
    assignee = str(node.get("assignee") or "").strip().lower()
    return bool(assignee and assignee != "leader")


def node_result_digest(text: str, limit: int = 260) -> str:
    normalized = " ".join(str(text or "").strip().split())
    if not normalized:
        return ""
    if normalized.startswith("[") and "的执行结果]" in normalized[:40]:
        normalized = normalized.split("]", 1)[-1].strip()
    return normalized if len(normalized) <= limit else f"{normalized[:limit].rstrip()}..."


def _compact_preserving_boundaries(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    window = value[:limit].rstrip()
    if window.count("`") % 2 == 1:
        opener = window.rfind("`")
        closer = value.find("`", limit)
        if closer != -1 and closer - limit <= 80:
            window = value[:closer + 1].rstrip()
        elif opener > max(24, int(limit * 0.55)):
            window = window[:opener].rstrip()
    boundary_floor = max(24, int(min(len(window), limit) * 0.62))
    boundary = -1
    for pattern in ("。", "；", ";", "，", ",", "、", " "):
        index = window.rfind(pattern)
        if index >= boundary_floor:
            boundary = max(boundary, index + (0 if pattern == " " else 1))
    if boundary > 0:
        window = window[:boundary].rstrip()
    return f"{window}..."


def clean_result_value(text: str, limit: int = 140) -> str:
    value = re.sub(r"^[#>\-\*\d\.\s]+", "", str(text or "").strip())
    value = re.sub(r"\*\*([^*]+)\*\*", r"\1", value).strip(" ：:;-，,。")
    if not value:
        return ""
    return _compact_preserving_boundaries(value, limit)


def result_status_signal(text: str) -> str:
    source = str(text or "")
    if any(word in source for word in ("不通过", "不可验收", "暂不建议", "失败", "需要修复")):
        return "fail"
    if any(word in source for word in ("可验收", "建议验收", "通过", "正常", "未发现", "无阻断", "无明显风险")):
        return "pass"
    if any(word in source for word in ("阻断", "高风险", "阻塞")):
        return "blocked"
    return "unknown"


def extract_result_contract(text: str) -> dict[str, str]:
    contract = {"answer": "", "risk": "", "next_action": "", "evidence": "", "status_signal": "unknown"}
    source = str(text or "").strip()
    if not source:
        return contract
    label_map = {
        "answer": {"answer", "结论", "验收结论", "测试结论", "安全结论", "业务结论", "执行结果", "结果"},
        "risk": {"risk", "风险", "风险等级", "阻塞", "问题", "缺陷"},
        "next_action": {"next_action", "next action", "建议", "下一步", "处理建议", "验收建议", "行动建议"},
        "evidence": {"evidence", "依据", "关键依据", "证据", "验证依据", "测试依据"},
    }
    for raw_line in source.splitlines():
        line = clean_result_value(raw_line, limit=260)
        if not line:
            continue
        match = re.match(r"^([A-Za-z_ ]+|[\u4e00-\u9fa5]{2,8})\s*[:：]\s*(.+)$", line)
        if not match:
            continue
        label = match.group(1).strip().lower()
        value = clean_result_value(match.group(2), limit=360)
        if not value:
            continue
        for key, labels in label_map.items():
            if label in labels and not contract[key]:
                contract[key] = value
                break
    contract["status_signal"] = result_status_signal(source)
    if contract["answer"]:
        return contract

    sentences = [
        clean_result_value(part, limit=360)
        for part in re.split(r"[。！？!?]\s*", source)
    ]
    priority_words = (
        "可验收", "通过", "正常", "未发现", "无阻断", "无明显风险", "建议",
        "不通过", "失败", "阻断", "高风险", "不可验收", "需要修复",
    )
    for sentence in sentences:
        if sentence and any(word in sentence for word in priority_words):
            contract["answer"] = sentence
            break
    if not contract["answer"]:
        contract["answer"] = next((sentence for sentence in sentences if sentence), "")
    return contract


def result_summary_items(text: str, *, max_items: int = 4) -> list[str]:
    contract = extract_result_contract(text)
    items: list[str] = []
    labels = {
        "answer": "结论",
        "evidence": "依据",
        "risk": "风险",
        "next_action": "建议",
    }
    for key in ("answer", "evidence", "risk", "next_action"):
        value = clean_result_value(contract.get(key, ""), limit=320)
        if value:
            label = labels[key]
            if value.startswith(f"{label}：") or value.startswith(f"{label}:"):
                items.append(value)
            else:
                items.append(f"{label}：{value}")
    if not items:
        fallback = clean_result_value(text, limit=320)
        if fallback:
            items.append(fallback)
    return items[:max_items]


def result_projection(text: str) -> dict[str, Any]:
    return {
        "result_contract": extract_result_contract(text),
        "summary_items": result_summary_items(text),
    }


def is_review_submission_node(node: TeamPlanNode) -> bool:
    return _workflow_lane(node.metadata or {}) in {"plan", "design"}


def is_verify_execution_node(node: TeamPlanNode) -> bool:
    return _workflow_lane(node.metadata or {}) == "verify"


def summary_node_label(node: TeamPlanNode) -> str:
    metadata = node.metadata or {}
    lane = _workflow_lane(metadata)
    role_key = str(metadata.get("role_key") or "").strip().lower()
    role_label = str(metadata.get("role_label") or "").strip()
    if role_key == "security_engineer":
        return "安全验证" if lane == "verify" else "安全方案"
    if role_key == "qa_engineer":
        return "测试验证" if lane == "verify" else "测试方案"
    return {
        "plan": role_label or "方案节点",
        "design": role_label or "设计节点",
        "build": role_label or "实现节点",
        "verify": role_label or "验证节点",
        "docs": role_label or "文档整理",
        "release": role_label or "交付整理",
        "summary": role_label or "汇总结论",
        "lead": role_label or "Leader 节点",
    }.get(lane, role_label or "成员节点")


def business_result_summary(
    node: TeamPlanNode,
    text: str,
    *,
    is_review_submission: bool = False,
    preserve_detail: bool = False,
) -> str:
    label = summary_node_label(node)
    source = str(text or "").strip()
    if preserve_detail:
        answer = re.sub(r"\*\*([^*]+)\*\*", r"\1", source).strip(" ：:;-，,。")
        risk = ""
        next_action = ""
    else:
        contract = extract_result_contract(source)
        answer = contract.get("answer", "")
        risk = contract.get("risk", "")
        next_action = contract.get("next_action", "")
    if answer.startswith(f"{label}：") or answer.startswith(f"{label}:"):
        answer = answer.split("：", 1)[-1] if "：" in answer else answer.split(":", 1)[-1]
    if preserve_detail:
        answer = re.sub(r"\*\*([^*]+)\*\*", r"\1", str(answer or "").strip(" ：:;-，,。"))
    else:
        answer = clean_result_value(answer, limit=420)
    parts = [answer]
    if risk and risk not in answer:
        parts.append(f"风险：{risk}")
    if next_action and next_action not in answer:
        parts.append(next_action)
    body = "；".join(part for part in parts if part)
    if body:
        suffix = "请审阅" if is_review_submission and "审阅" not in body else ""
        return "；".join(part for part in [f"{label}：{body}", suffix] if part)
    if is_review_submission:
        return f"{label}已提交，请审阅"
    return f"{label}已完成，详细结果见看板或产物"


def acceptance_headline(goal: str, summaries: list[str]) -> str:
    text = " ".join([goal, *summaries])
    if any(word in text for word in ("需要补充", "信息不足", "缺少", "请补充", "待确认", "无法确认")):
        return "还需要补充关键信息。"
    if any(word in text for word in ("不通过", "不可验收", "暂不建议", "阻断", "高风险", "失败", "需要修复")):
        return "暂不建议验收。"
    if any(word in text for word in ("可验收", "建议验收", "通过", "正常", "未发现", "无阻断", "无明显风险")):
        return "可以验收。"
    if any(word in goal for word in ("验收", "测试", "验证", "是否可以", "能否")):
        return "验收结论待复核。"
    return "本次团队任务已完成。"


def artifact_cards(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for artifact in artifacts:
        artifact_id = str(artifact.get("artifact_id") or "").strip()
        path = str(artifact.get("path") or "").strip()
        title = Path(path).name if path else str(artifact.get("summary") or artifact_id or "产物").strip()
        if not title:
            continue
        cards.append({
            "id": artifact_id,
            "artifact_id": artifact_id,
            "title": title,
            "summary": str(artifact.get("summary") or "").strip(),
            "path": path,
            "content_type": str(artifact.get("content_type") or "").strip(),
        })
    return cards


def assignment_text(node: TeamPlanNode) -> str:
    return _assignment_text_for(
        node_id=node.node_id,
        assignee=node.assignee,
        title=node.title,
        metadata=node.metadata or {},
    )


def should_show_assignment(plan: TeamPlan, node: TeamPlanNode) -> bool:
    del plan
    assignee = str(node.assignee or "").strip().lower()
    return bool(assignee and assignee != "leader")


def artifact_title_head(title: str) -> str:
    head = re.split(r"[:：]", str(title or "").strip(), maxsplit=1)[0].strip()
    head = re.sub(r"\s+", " ", head).strip(" ._-—")
    if not head or len(head) > 32:
        return ""
    vague = {
        "处理一下这个需求",
        "处理需求",
        "当前任务",
        "团队任务",
        "成员节点",
        "执行节点",
        "任务处理",
    }
    return "" if head in vague else head


def markdown_document_title(content: str) -> str:
    for line in str(content or "").splitlines()[:20]:
        match = re.match(r"^\s{0,3}#\s+(.+?)\s*#*\s*$", line)
        if not match:
            continue
        title = re.sub(r"\s+", " ", match.group(1)).strip(" ._-—")
        return title[:48]
    return ""


def artifact_label(node: TeamPlanNode, content: str = "") -> str:
    return markdown_document_title(content) or artifact_title_head(str(node.title or "")) or "团队产物"


def artifact_filename(node: TeamPlanNode, content: str = "") -> str:
    raw = artifact_label(node, content)
    name = re.sub(r"[\\/:*\?\"<>\|\0]+", "_", raw).strip(" ._")
    if not name:
        name = safe_path_segment(node.node_id, "team-artifact")
    stem = name[:-3] if name.lower().endswith(".md") else name
    return f"{stem[:117]}.md"


def unique_artifact_path(artifact_dir: Path, filename: str) -> Path:
    path = artifact_dir / filename
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix or ".md"
    for index in range(2, 100):
        candidate = artifact_dir / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    return artifact_dir / f"{stem}-{safe_path_segment(str(time.time()), 'artifact')}{suffix}"
