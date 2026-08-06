"""用户交互式追问（ask_followup_question）的等待与解析机制。

工具 handler 在运行时需要向前端弹出选择框并暂停等待用户选择。
本模块提供：
  - send_followup_question：向前端推事件并创建等待 Future
  - wait_for_answer：工具 handler 调用，挂起等待用户回答
  - resolve_answer：gateway WebSocket 收到用户选择后调用，唤醒 Future
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from crew.core.errors import ToolError
from crew.core.runctx import current_push_fn, current_request_id, current_session_id
from crew.state.logging import get_logger

log = get_logger("followup")

_DEFAULT_TIMEOUT = 300.0

# 取消标记：用户点「取消」时以此作为答案回灌，工具 handler 据此识别取消。
# 用回灌而非 future.cancel()，避免 CancelledError 冒泡到 agent 主任务。
CANCELLED_MARKER = "__cancelled__"
_CANCELLED_ANSWER = [{"id": CANCELLED_MARKER, "answers": []}]


class FollowupWaiter:
    """按 (session_id, question_id) 管理等待中的追问。"""

    def __init__(self) -> None:
        # {(session_id, question_id): asyncio.Future}
        self._futures: dict[tuple[str, str], asyncio.Future[list[dict[str, Any]]]] = {}
        # {(session_id, question_id): normalized questions}
        self._questions: dict[tuple[str, str], list[dict[str, Any]]] = {}
        # {(session_id, question_id): 是否把答案写进 canonical history}
        self._record_history: dict[tuple[str, str], bool] = {}
        # session_id -> user-visible messages waiting to be merged into canonical history
        self._answer_messages: dict[str, list[str]] = {}

    def _key(self, session_id: str, question_id: str) -> tuple[str, str]:
        return (session_id, question_id)

    def create(
        self,
        session_id: str,
        questions: list[dict[str, Any]] | None = None,
        *,
        record_history: bool = True,
    ) -> str:
        """创建一个新的 question_id 和 Future，返回 question_id。

        record_history=False 时，用户的选择不会被格式化成 user 消息灌进 canonical
        history——供权限确认等 side-channel 交互使用（不该作为对话内容留下）。
        """
        question_id = uuid.uuid4().hex[:16]
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        future: asyncio.Future[list[dict[str, Any]]] = loop.create_future()
        key = self._key(session_id, question_id)
        self._futures[key] = future
        self._questions[key] = list(questions or [])
        self._record_history[key] = bool(record_history)
        return question_id

    async def wait(
        self,
        session_id: str,
        question_id: str,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> list[dict[str, Any]]:
        """等待用户回答；超时返回空答案列表（让 LLM 自己处理）。"""
        future = self._futures.get(self._key(session_id, question_id))
        if future is None:
            raise ToolError(f"追问不存在: {question_id}")
        try:
            answers = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            log.warning("追问超时 session=%s question=%s", session_id, question_id)
            answers = []
        finally:
            key = self._key(session_id, question_id)
            self._futures.pop(key, None)
            self._questions.pop(key, None)
            self._record_history.pop(key, None)
        return answers

    def resolve(
        self,
        session_id: str,
        question_id: str,
        answers: list[dict[str, Any]],
    ) -> bool:
        """用户回答后调用；成功唤醒返回 True，Future 不存在返回 False。"""
        future = self._futures.get(self._key(session_id, question_id))
        if future is None or future.done():
            return False
        key = self._key(session_id, question_id)
        if self._record_history.get(key, True):
            display_message = self._format_answer_message(session_id, question_id, answers)
            if display_message:
                self._answer_messages.setdefault(session_id, []).append(display_message)
        future.set_result(answers)
        return True

    def cancel(self, session_id: str, question_id: str) -> bool:
        """取消等待：回灌取消标记答案，让 wait() 正常返回而非抛 CancelledError。"""
        future = self._futures.pop(self._key(session_id, question_id), None)
        key = self._key(session_id, question_id)
        self._questions.pop(key, None)
        self._record_history.pop(key, None)
        if future is not None and not future.done():
            future.set_result(_CANCELLED_ANSWER)
            return True
        return False

    def is_waiting(self, session_id: str, question_id: str) -> bool:
        """判断该追问是否仍在等待用户回答（未回答、未取消、未超时）。"""
        future = self._futures.get(self._key(session_id, question_id))
        return future is not None and not future.done()

    def drain_answer_messages(self, session_id: str) -> list[str]:
        """取出并清空等待写入 history 的用户选择展示文本。"""
        return self._answer_messages.pop(session_id, [])

    def _format_answer_message(
        self,
        session_id: str,
        question_id: str,
        answers: list[dict[str, Any]],
    ) -> str:
        """把结构化答案格式化成历史中展示的 user 消息。

        如果选项使用 {label,value}，历史展示 label；模型工具结果仍保留 value。
        """
        if not answers:
            return ""
        if answers[0].get("id") == CANCELLED_MARKER:
            return ""

        questions = self._questions.get(self._key(session_id, question_id), [])
        question_map = {str(q.get("id") or ""): q for q in questions if isinstance(q, dict)}
        multi = len(answers) > 1
        parts: list[str] = []
        for item in answers:
            if not isinstance(item, dict):
                continue
            qid = str(item.get("question_id") or item.get("id") or "")
            raw_values = item.get("answers")
            if not isinstance(raw_values, list):
                continue
            q = question_map.get(qid, {})
            value_to_label = {
                str(opt.get("value")): str(opt.get("label"))
                for opt in q.get("options", [])
                if isinstance(opt, dict) and opt.get("label") is not None
            }
            labels = [value_to_label.get(str(value), str(value)) for value in raw_values]
            labels = [label for label in labels if label]
            if not labels:
                continue
            text = ", ".join(labels)
            if q.get("inputMode") == "text" and q.get("question"):
                text = f"{q.get('question')}：{text}"
            if multi and q.get("question"):
                text = f"{q.get('question')}：{text}"
            parts.append(text)
        prefix = "已补充" if any(q.get("inputMode") == "text" for q in question_map.values()) else "已选择"
        return f"{prefix}：{'；'.join(parts)}" if parts else ""


# 全局单例
_followup_waiter = FollowupWaiter()


def get_followup_waiter() -> FollowupWaiter:
    return _followup_waiter


def _first_text_field(payload: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _normalize_option(option: Any, question_index: int, option_index: int) -> dict[str, str]:
    """把前端选项统一成 {label,value,description?}。

    兼容两类输入：
      - "风险分析"                                      → label=value="风险分析"
      - {"label": "风险分析", "value": "risk"}          → 展示 label，提交 value
      - {"label": "A", "description": "True, False"}    → 展示短码 + 说明，提交 A
    """
    if isinstance(option, dict):
        label = _first_text_field(option, ("label", "text", "name", "title", "key"))
        value_raw = option.get("value", label)
        value = str(value_raw if value_raw is not None else label).strip()
        if not label:
            raise ToolError(f"questions[{question_index}].options[{option_index}].label 不能为空")
        if not value:
            value = label
        description = _first_text_field(
            option,
            ("description", "desc", "detail", "details", "content", "body", "explanation", "summary"),
        )
        normalized = {"label": label, "value": value}
        if description:
            normalized["description"] = description
        return normalized
    text = str(option).strip()
    if not text:
        raise ToolError(f"questions[{question_index}].options[{option_index}] 不能为空")
    return {"label": text, "value": text}


def validate_questions(questions: list[Any]) -> list[dict[str, Any]]:
    """校验并规范化 questions 数组。"""
    if not isinstance(questions, list):
        raise ToolError("questions 必须是数组")
    normalized: list[dict[str, Any]] = []
    for idx, q in enumerate(questions):
        if not isinstance(q, dict):
            raise ToolError(f"questions[{idx}] 必须是对象")
        qid = str(q.get("id") or f"q{idx}")
        text = str(q.get("question") or "").strip()
        if not text:
            raise ToolError(f"questions[{idx}].question 不能为空")
        input_mode = str(q.get("inputMode") or q.get("input_mode") or q.get("responseType") or "").strip().lower()
        if input_mode in {"free_text", "textarea", "text_input"}:
            input_mode = "text"
        if input_mode not in {"", "choice", "text"}:
            raise ToolError(f"questions[{idx}].inputMode 只支持 choice/text")
        options = q.get("options")
        if input_mode == "text":
            if options is None:
                options = []
            if not isinstance(options, list):
                raise ToolError(f"questions[{idx}].options 必须是数组")
        elif not isinstance(options, list) or len(options) == 0:
            raise ToolError(f"questions[{idx}].options 必须是非空数组")
        multi = bool(q.get("multiSelect", False))
        item = {
            "id": qid,
            "question": text,
            "options": [_normalize_option(o, idx, opt_idx) for opt_idx, o in enumerate(options)],
            "multiSelect": multi,
            # Ordinary follow-ups keep the escape hatch by default. Permission
            # prompts opt out because an arbitrary string is not an approval.
            "allowFreeText": q.get("allowFreeText", q.get("allow_free_text", True)) is not False,
        }
        if input_mode == "text":
            item["inputMode"] = "text"
        normalized.append(item)
    return normalized


async def send_followup_question(
    questions: list[dict[str, Any]],
    title: str = "",
    *,
    record_history: bool = True,
) -> tuple[str, str]:
    """向前端发送追问事件，并返回 (session_id, question_id)。

    由 ask_followup_question 工具 handler 调用。record_history=False 时答案
    不写进 canonical history（权限确认等 side-channel 用）。
    """
    session_id = current_session_id.get()
    if not session_id:
        raise ToolError("当前无会话，无法发送追问")

    return await send_followup_question_to(
        session_id, questions, title=title, record_history=record_history
    )


async def send_followup_question_to(
    session_id: str,
    questions: list[dict[str, Any]],
    title: str = "",
    *,
    note: str = "",
    origin: dict[str, Any] | None = None,
    push_fn=None,
    record_history: bool = True,
) -> tuple[str, str]:
    """向指定主会话发送追问，供跨进程 ACP/MCP 桥显式绑定 session。"""
    session_id = str(session_id or "").strip()
    if not session_id:
        raise ToolError("session_id 不能为空")

    push_fn = push_fn or current_push_fn.get()
    if push_fn is None:
        raise ToolError("当前运行环境不支持追问交互（无 push 函数）")

    normalized = validate_questions(questions)
    question_id = _followup_waiter.create(
        session_id, normalized, record_history=record_history
    )

    payload = {
        "kind": "followup_question",
        "body": {
            "question_id": question_id,
            "title": str(title or "").strip(),
            "questions": normalized,
            # The renderer needs the same history boundary as FollowupWaiter.
            # Permission prompts are side-channel UI and must not split the
            # assistant turn or appear as a synthetic user message.
            "record_history": bool(record_history),
        },
        "is_final": False,
        "sequence": 0,
        "request_id": current_request_id.get(),
        "session_id": session_id,
    }
    normalized_note = str(note or "").strip()
    if normalized_note:
        payload["body"]["note"] = normalized_note
    if origin:
        payload["body"]["origin"] = dict(origin)
    await push_fn(session_id, payload)
    log.info("已发送追问 session=%s question=%s", session_id, question_id)
    return session_id, question_id


async def send_followup_status_to(
    session_id: str,
    question_id: str,
    status: str,
    *,
    note: str = "",
    push_fn=None,
) -> bool:
    """Push a presentation-only lifecycle update for an existing follow-up."""

    session_id = str(session_id or "").strip()
    question_id = str(question_id or "").strip()
    status = str(status or "").strip()
    if not session_id or not question_id or not status:
        return False
    push_fn = push_fn or current_push_fn.get()
    if push_fn is None:
        return False
    await push_fn(session_id, {
        "kind": "followup_question",
        "body": {
            "question_id": question_id,
            "status": status,
            "note": str(note or "").strip(),
        },
        "is_final": False,
        "sequence": 0,
        "request_id": current_request_id.get(),
        "session_id": session_id,
    })
    return True


async def wait_for_answer(
    session_id: str,
    question_id: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    return await _followup_waiter.wait(session_id, question_id, timeout=timeout)


def resolve_answer(
    session_id: str,
    question_id: str,
    answers: list[dict[str, Any]],
) -> bool:
    return _followup_waiter.resolve(session_id, question_id, answers)


def cancel_followup(session_id: str, question_id: str) -> bool:
    return _followup_waiter.cancel(session_id, question_id)


def drain_followup_answer_messages(session_id: str) -> list[str]:
    """取出当前 session 已提交的追问选择，用于写入 canonical history。"""
    return _followup_waiter.drain_answer_messages(session_id)
