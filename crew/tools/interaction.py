"""交互式工具：ask_followup_question。

当 LLM 高度不确定、需要用户做选择时，向前端弹出单选/多选框，等待用户回答后
把结构化答案回灌给模型。
"""

from __future__ import annotations

from typing import Any

from crew.core.errors import ToolError
from crew.core.followup import CANCELLED_MARKER, send_followup_question, validate_questions, wait_for_answer
from crew.core.runctx import current_session_id
from crew.tools.registry import Registry, tool_result


ASK_FOLLOWUP_QUESTION_SCHEMA = {
    "name": "ask_followup_question",
    "description": (
        "当对用户需求高度不确定、必须澄清时，向前端弹出单选/多选或文本输入交互框。"
        "提供一个问题数组；选择型问题包含选项，文本型问题设置 inputMode=text；支持 multiSelect 多选。"
        "只在确实无法通过推理和其他工具解决时才使用。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "选择框标题，可选",
            },
            "questions": {
                "type": "array",
                "description": "问题数组",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "问题唯一标识"},
                        "question": {"type": "string", "description": "问题文本"},
                        "options": {
                            "type": "array",
                            "items": {
                                "oneOf": [
                                    {"type": "string"},
                                    {
                                        "type": "object",
                                        "properties": {
                                            "label": {"type": "string", "description": "展示给用户看的选项文本"},
                                            "value": {"type": "string", "description": "提交给 Agent 的选项值"},
                                        },
                                        "required": ["label"],
                                    },
                                ],
                            },
                            "description": "选择型问题的选项数组；可传字符串，或 {label,value} 对象。inputMode=text 时可省略或为空。",
                        },
                        "inputMode": {
                            "type": "string",
                            "enum": ["choice", "text"],
                            "description": "回答方式。默认 choice；text 表示让用户自由输入文本。",
                        },
                        "multiSelect": {
                            "type": "boolean",
                            "description": "是否允许多选，默认 false",
                        },
                    },
                    "required": ["question"],
                },
            },
        },
        "required": ["questions"],
    },
}


async def handle_ask_followup_question(args: dict[str, Any]) -> str:
    questions = args.get("questions")
    title = str(args.get("title") or "").strip()

    if not isinstance(questions, list) or not questions:
        raise ToolError("questions 必须是非空数组")

    # 先校验参数（此时可能还没有 session_id，先不依赖 push_fn）
    validated = validate_questions(questions)

    session_id = current_session_id.get()
    if not session_id:
        raise ToolError("当前无会话，无法发送追问")

    session_id, question_id = await send_followup_question(validated, title=title)
    answers = await wait_for_answer(session_id, question_id)

    # 用户点「取消」：后端回灌的取消标记答案，识别后告知模型用户已放弃选择。
    cancelled = bool(answers) and answers[0].get("id") == CANCELLED_MARKER
    if cancelled:
        return tool_result({
            "success": False,
            "question_id": question_id,
            "answers": [],
            "note": "用户已取消选择，无需继续基于该追问推进，请改为依据已有信息决策或结束。",
        })

    # 把答案整理成模型易读的格式
    result = {
        "success": True,
        "question_id": question_id,
        "answers": answers,
    }
    if not answers:
        result["note"] = "用户未在超时时间内回答"
    return tool_result(result)


def register_interaction_tools(registry: Registry) -> None:
    registry.register(
        name="ask_followup_question",
        toolset="interaction",
        schema=ASK_FOLLOWUP_QUESTION_SCHEMA,
        handler=handle_ask_followup_question,
        is_async=True,
        display_name="询问用户",
        ui_label_template="询问用户",
        always_load=True,
        search_hint="ask user followup question clarify choices",
        result_retention="important",
    )
