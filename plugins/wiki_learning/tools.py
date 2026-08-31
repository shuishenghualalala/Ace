"""Three generic Wiki learning tools: state, activity, and assessment."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from crew.core.errors import ToolError
from crew.core.runctx import current_owner_account_id, current_request_id, current_session_id
from crew.wiki._utils import is_wiki_agent_session
from crew.wiki.store._ids import normalize_kb_id
from crew.wiki.store._filesystem import FileSystemWikiStore

from . import context
from .store import WikiLearningStore


STATE_SCHEMA: dict[str, Any] = {
    "name": "wiki_learning_state",
    "description": "创建、恢复、查看或结束一个基于当前 Wiki 的学习会话，并读取掌握度。",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["open", "resume", "update", "inspect", "finish"]},
            "episode_id": {
                "type": "string",
                "description": "resume/inspect/finish 时使用的学习会话 ID。",
            },
            "kb_id": {"type": "string", "description": "可省略；默认使用当前 Wiki 知识库。"},
            "goal": {
                "type": "string",
                "description": "开放式学习目标，例如复习重点或模拟后端面试。",
            },
            "constraints": {
                "type": "object",
                "description": "难度、时长、偏好等可扩展约束。",
                "additionalProperties": True,
            },
            "page_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选的学习范围。",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}

ACTIVITY_SCHEMA: dict[str, Any] = {
    "name": "wiki_learning_activity",
    "description": "登记、查看或关闭一项基于 Wiki 证据的学习活动；活动类型和流程不写死。",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "inspect", "close"]},
            "episode_id": {"type": "string"},
            "activity_id": {"type": "string"},
            "kb_id": {"type": "string", "description": "可省略；默认使用当前 Wiki 知识库。"},
            "activity_type": {
                "type": "string",
                "description": "开放类型，如 recall、quiz、interview、teach_back。",
            },
            "prompt": {
                "type": "string",
                "description": "将向用户展示的题目或练习。不得包含隐藏答案。",
            },
            "evidence_page_ids": {"type": "array", "items": {"type": "string"}},
            "knowledge_keys": {
                "type": "array",
                "items": {"type": "string"},
                "description": "该活动检查的稳定知识点键。",
            },
            "reveal_policy": {
                "type": "string",
                "enum": ["on_assess", "on_request", "never"],
                "default": "on_assess",
            },
            "public_payload": {
                "type": "object",
                "description": (
                    "可公开的通用交互卡片数据。选择题使用 schema=crew.interaction.v1，"
                    "interaction.kind=single_choice，interaction.options=[{id,label,description?}]；"
                    "可附 title 和 progress={current,total}。不得放答案或评分标准。"
                ),
                "additionalProperties": True,
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}

ASSESS_SCHEMA: dict[str, Any] = {
    "name": "wiki_learning_assess",
    "description": "评估用户对已登记活动的当前回合回答并更新掌握度。回答会由插件私下捕获，不得作为工具参数传入。",
    "parameters": {
        "type": "object",
        "properties": {
            "activity_id": {"type": "string"},
            "summary": {"type": "string", "description": "简短、可展示的总体评价。"},
            "score": {"type": "number", "minimum": 0, "maximum": 1},
            "strengths": {"type": "array", "items": {"type": "string"}},
            "gaps": {"type": "array", "items": {"type": "string"}},
            "knowledge_signals": {
                "type": "object",
                "description": "knowledge_key 到 0..1 分数的映射，只能使用活动预先登记的键。",
                "additionalProperties": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "evidence_page_ids": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["activity_id", "summary", "score", "knowledge_signals"],
        "additionalProperties": False,
    },
}


def _result(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _required_text(args: dict[str, Any], key: str) -> str:
    value = str(args.get(key) or "").strip()
    if not value:
        raise ToolError(f"缺少参数: {key}")
    return value


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ToolError("列表参数格式不正确")
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _contains_private_key(value: Any) -> bool:
    forbidden = {"answer", "answers", "correct_answer", "rubric", "reference_answer"}
    if isinstance(value, dict):
        if any(str(key).lower() in forbidden for key in value):
            return True
        return any(_contains_private_key(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_private_key(item) for item in value)
    return False


class WikiLearningTools:
    def __init__(self, store: WikiLearningStore, wiki_store: FileSystemWikiStore) -> None:
        self.store = store
        self.wiki_store = wiki_store

    @staticmethod
    def _scope(args: dict[str, Any]) -> tuple[str, str, str, str]:
        owner = current_owner_account_id.get().strip()
        session_id = current_session_id.get().strip()
        request_id = current_request_id.get().strip()
        if not owner:
            raise ToolError("当前账号上下文缺失")
        if not session_id or not is_wiki_agent_session(session_id):
            raise ToolError("学习工具只能在 Wiki Agent 会话中使用")
        raw_kb = str(args.get("kb_id") or context.active_kb_id() or "").strip()
        if not raw_kb:
            raise ToolError("当前知识库上下文缺失")
        try:
            kb_id = normalize_kb_id(raw_kb)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return owner, session_id, kb_id, request_id

    def _page_fingerprints(self, page_ids: Iterable[str], owner: str, kb_id: str) -> dict[str, str]:
        fingerprints: dict[str, str] = {}
        for page_id in page_ids:
            page = self.wiki_store.get(page_id, owner_account_id=owner, kb_id=kb_id)
            if page is None:
                raise ToolError(f"Wiki 证据页面不存在: {page_id}")
            encoded = json.dumps(
                page.to_dict(brief=False), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            fingerprints[page_id] = hashlib.sha256(encoded).hexdigest()
        return fingerprints

    def state(self, args: dict[str, Any]) -> str:
        action = _required_text(args, "action")
        owner, session_id, kb_id, _ = self._scope(args)
        if action == "open":
            pages = _strings(args.get("page_ids"))
            if pages:
                self._page_fingerprints(pages, owner, kb_id)
            constraints = args.get("constraints") or {}
            if not isinstance(constraints, dict):
                raise ToolError("constraints 必须是对象")
            episode = self.store.open_episode(
                owner,
                session_id,
                kb_id,
                goal=str(args.get("goal") or ""),
                constraints=constraints,
                page_ids=pages,
            )
        elif action == "resume":
            episode = self.store.resume_episode(
                _required_text(args, "episode_id"), owner, session_id
            )
        elif action == "update":
            pages = _strings(args.get("page_ids")) if "page_ids" in args else None
            if pages:
                self._page_fingerprints(pages, owner, kb_id)
            constraints = args.get("constraints") if "constraints" in args else None
            if constraints is not None and not isinstance(constraints, dict):
                raise ToolError("constraints 必须是对象")
            episode = self.store.update_episode(
                _required_text(args, "episode_id"),
                owner,
                session_id,
                goal=str(args.get("goal") or "") if "goal" in args else None,
                constraints=constraints,
                page_ids=pages,
            )
        elif action == "inspect":
            episode_id = str(args.get("episode_id") or "").strip()
            episode = (
                self.store.get_episode(episode_id, owner, session_id)
                if episode_id
                else self.store.active_episode(owner, session_id, kb_id)
            )
            if episode is None:
                raise ToolError("没有找到学习会话")
        elif action == "finish":
            episode = self.store.finish_episode(
                _required_text(args, "episode_id"), owner, session_id
            )
        else:
            raise ToolError(f"不支持的 action: {action}")
        return _result(
            {"episode": episode, "mastery": self.store.mastery_snapshot(owner, episode["kb_id"])}
        )

    def activity(self, args: dict[str, Any]) -> str:
        action = _required_text(args, "action")
        owner, session_id, kb_id, request_id = self._scope(args)
        if action == "create":
            page_ids = _strings(args.get("evidence_page_ids"))
            keys = _strings(args.get("knowledge_keys"))
            if not page_ids:
                raise ToolError("学习活动必须至少引用一个 Wiki 证据页面")
            if not keys:
                raise ToolError("学习活动必须至少登记一个知识点")
            payload = args.get("public_payload") or {}
            if not isinstance(payload, dict):
                raise ToolError("public_payload 必须是对象")
            payload = dict(payload)
            payload.setdefault("schema", "crew.interaction.v1")
            if not isinstance(payload.get("interaction"), dict):
                root_options = payload.pop("options", None)
                payload["interaction"] = (
                    {"kind": "single_choice", "options": root_options}
                    if isinstance(root_options, list) and root_options
                    else {"kind": "text"}
                )
            if _contains_private_key(payload):
                raise ToolError("public_payload 不能包含答案或隐藏评分标准")
            reveal_policy = str(args.get("reveal_policy") or "on_assess")
            if reveal_policy not in {"on_assess", "on_request", "never"}:
                raise ToolError("reveal_policy 无效")
            activity = self.store.create_activity(
                _required_text(args, "episode_id"),
                owner,
                session_id,
                kb_id,
                activity_type=_required_text(args, "activity_type"),
                prompt=_required_text(args, "prompt"),
                evidence_page_ids=page_ids,
                evidence_fingerprints=self._page_fingerprints(page_ids, owner, kb_id),
                knowledge_keys=keys,
                reveal_policy=reveal_policy,
                public_payload=payload,
                request_id=request_id,
            )
        elif action == "inspect":
            activity = self.store.get_activity(
                _required_text(args, "activity_id"), owner, session_id
            )
            if activity is None:
                raise ToolError("学习活动不存在或无权访问")
            missing: list[str] = []
            stale: list[str] = []
            for page_id in activity["evidence_page_ids"]:
                page = self.wiki_store.get(page_id, owner_account_id=owner, kb_id=activity["kb_id"])
                if page is None:
                    missing.append(page_id)
                    continue
                encoded = json.dumps(
                    page.to_dict(brief=False),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                if (
                    activity["evidence_fingerprints"].get(page_id)
                    != hashlib.sha256(encoded).hexdigest()
                ):
                    stale.append(page_id)
            activity = {
                **activity,
                "missing_evidence_page_ids": missing,
                "stale_evidence_page_ids": stale,
            }
        elif action == "close":
            activity = self.store.close_activity(
                _required_text(args, "activity_id"), owner, session_id
            )
        else:
            raise ToolError(f"不支持的 action: {action}")
        return _result({"activity": activity})

    def assess(self, args: dict[str, Any]) -> str:
        owner, session_id, _, request_id = self._scope(args)
        score = float(args.get("score", -1))
        if score < 0 or score > 1:
            raise ToolError("score 必须在 0 到 1 之间")
        raw_signals = args.get("knowledge_signals")
        if not isinstance(raw_signals, dict) or not raw_signals:
            raise ToolError("knowledge_signals 必须是非空对象")
        signals: dict[str, float] = {}
        for key, value in raw_signals.items():
            clean_key = str(key or "").strip()
            try:
                clean_score = float(value)
            except (TypeError, ValueError) as exc:
                raise ToolError(f"知识点 {clean_key} 的分数无效") from exc
            if not clean_key or clean_score < 0 or clean_score > 1:
                raise ToolError("知识点名称不能为空，分数必须在 0 到 1 之间")
            signals[clean_key] = clean_score
        response = context.latest_user_text()
        assessment = self.store.record_assessment(
            _required_text(args, "activity_id"),
            owner,
            session_id,
            request_id=request_id,
            response_text=response,
            response_hash=hashlib.sha256(response.encode("utf-8")).hexdigest(),
            summary=_required_text(args, "summary"),
            score=score,
            strengths=_strings(args.get("strengths")),
            gaps=_strings(args.get("gaps")),
            signals=signals,
            evidence_page_ids=_strings(args.get("evidence_page_ids")),
        )
        activity = self.store.get_activity(assessment["activity_id"], owner, session_id)
        if activity is None:
            raise ToolError("评估完成后无法读取学习活动")
        return _result(
            {
                "assessment": assessment,
                "mastery": self.store.mastery_snapshot(owner, activity["kb_id"]),
            }
        )
