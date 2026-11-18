"""Interactive CLI adapter for ordinary Agent questions."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from crew.core.envelope import Envelope
from crew.core.errors import ToolError
from crew.core.followup import get_followup_waiter, normalize_followup_answers
from crew.core.runctx import (
    current_display_session_id,
    current_followup_push,
    current_owner_account_id,
    current_request_id,
    current_session_id,
)
from crew.state.logging import get_logger
from crew.tools.redact import redact_sensitive_display_text

log = get_logger("cli.followup")
Ask = Callable[[str], Awaitable[str]]


def _display(text: str) -> str:
    return repr(redact_sensitive_display_text(text))


async def _text_answer(ask: Ask, prompt: str) -> str | None:
    hint = ""
    while True:
        value = (await ask(hint + prompt)).strip()
        if value.casefold() == "/cancel":
            return None
        if value:
            return value
        hint = "请输入非空回答。\n"


async def _answer_question(ask: Ask, heading: str, question: dict[str, Any]) -> list[str] | None:
    if question.get("inputMode") == "text":
        value = await _text_answer(ask, f"{heading}\n请输入回答 [text /cancel] ")
        return [value] if value is not None else None

    options = question["options"]
    labels = []
    for index, option in enumerate(options, 1):
        label = f"  {index}. {_display(option['label'])}"
        if option.get("description"):
            label += f"  {_display(option['description'])}"
        labels.append(label)
    choices = {str(index): option["value"] for index, option in enumerate(options, 1)}
    if question["allowFreeText"]:
        labels.append(f"  {len(labels) + 1}. 其他（自行输入）")
        choices[str(len(labels))] = None
    multi = question["multiSelect"]
    instruction = (
        "请输入选项编号，多项用逗号分隔 [1,2,… /cancel] "
        if multi else f"请输入一个选项编号 [1…{len(labels)} /cancel] "
    )
    prompt = "\n".join((heading, *labels, instruction))
    hint = ""
    while True:
        raw = (await ask(hint + prompt)).strip()
        if raw.casefold() == "/cancel":
            return None
        indexes = [part.strip() for part in raw.replace("，", ",").split(",")]
        if (
            (not multi and len(indexes) != 1)
            or len(set(indexes)) != len(indexes)
            or any(index not in choices for index in indexes)
        ):
            hint = "请输入菜单中的编号，每个编号只选一次。\n"
            continue
        values = []
        for index in indexes:
            value = choices[index]
            if value is None:
                value = await _text_answer(ask, "请输入自定义回答 [other /cancel] ")
                if value is None:
                    return None
            if value not in values:
                values.append(value)
        return values


@asynccontextmanager
async def conversation_followups(envelope: Envelope, ask: Ask, input_lock: asyncio.Lock) -> AsyncIterator[None]:
    """Collect an entire questionnaire before resolving its originating tool call."""
    waiter = get_followup_waiter()
    scope = {"owner_account_id": envelope.user_id, "request_id": envelope.request_id}
    session_id = envelope.session_id
    tasks: dict[str, asyncio.Task[None]] = {}
    closed = False

    async def collect(question_id: str, body: dict[str, Any]) -> None:
        try:
            async with input_lock:
                answers = []
                questions = body["questions"]
                for index, question in enumerate(questions, 1):
                    if not waiter.is_waiting(session_id, question_id, **scope):
                        return
                    heading = f"追问 {index}/{len(questions)}  {_display(question['question'])}"
                    if index == 1 and body.get("title"):
                        heading = f"{_display(body['title'])}\n{heading}"
                    values = await _answer_question(ask, heading, question)
                    if values is None:
                        return
                    answers.append({"question_id": question["id"], "answers": values})
                normalized = normalize_followup_answers(answers)
                if normalized is not None and waiter.validated_answer(session_id, question_id, normalized, **scope)[0]:
                    waiter.resolve(session_id, question_id, normalized, **scope)
        except (EOFError, KeyboardInterrupt):
            pass
        except Exception as exc:
            log.warning("CLI 追问输入失败 question=%s error=%s", question_id, type(exc).__name__)
        finally:
            waiter.cancel(session_id, question_id, **scope)
            tasks.pop(question_id, None)

    async def push(display_session: str, payload: dict[str, Any]) -> None:
        body = payload.get("body", {})
        if closed or payload.get("kind") != "followup_question":
            return
        if (
            current_owner_account_id.get() != envelope.user_id
            or current_request_id.get() != envelope.request_id
            or (current_display_session_id.get() or current_session_id.get()) != session_id
            or display_session != session_id or payload.get("session_id") != session_id
            or payload.get("request_id") != envelope.request_id
        ):
            if body.get("questions"):
                raise ToolError("追问与当前 CLI 回合不匹配")
            return
        question_id = body["question_id"]
        if body.get("status") == "closed":
            task = tasks.get(question_id)
            if task is not None:
                task.cancel()
            return
        if body.get("record_history") is not True:
            raise ToolError("追问与当前 CLI 回合不匹配")
        if question_id not in tasks and waiter.is_waiting(session_id, question_id, **scope):
            tasks[question_id] = asyncio.create_task(collect(question_id, body))

    token = current_followup_push.set(push)
    try:
        yield
    finally:
        closed = True
        current_followup_push.reset(token)
        active = list(tasks.items())
        for question_id, task in active:
            waiter.cancel(session_id, question_id, **scope)
            task.cancel()
        await asyncio.gather(*(task for _, task in active), return_exceptions=True)
