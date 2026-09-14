"""会话历史配平：冷读后为孤儿 tool_call 合成 error tool 结果。

崩溃/硬停可能在历史里留下「assistant 带 tool_calls 但无配对 tool 结果」的
悬空调用，下一轮请求会被 provider 以 tool_call 无响应拒绝。本模块只做纯
扫描：每个孤儿合成一条确定性 error 结果消息（内容由 tool_call 自身字段
派生，重复扫描/重放结果一致，幂等可安全持久化）。

两段语义（与执行痕迹一一对应）：
  TOOL_OUTCOME_UNKNOWN —— 调用已开始执行（status="running" 或 duration 已
    写回）但结果没落盘，副作用可能已发生：先验证外部状态，只对只读/幂等
    操作重试。
  TOOL_NOT_STARTED    —— 没有任何开始执行的痕迹，可安全重试。
"""

from __future__ import annotations

from crew.core.types import Message

TOOL_NOT_STARTED = "TOOL_NOT_STARTED"
TOOL_OUTCOME_UNKNOWN = "TOOL_OUTCOME_UNKNOWN"

_OUTCOME_UNKNOWN_TEXT = (
    "工具调用在会话中断前已开始执行，但结果未落盘，结局未知。"
    "请根据工具语义决定是否重试：仅当操作只读或幂等时才可直接重试；"
    "若可能产生副作用，先验证外部实际状态或询问用户，不要盲目重试。"
)

_NOT_STARTED_TEXT = "工具调用在会话中断前未开始执行。如仍需要该操作，可以直接重试。"


def repair_orphan_tool_calls(messages: list[Message]) -> list[Message]:
    """返回闭合孤儿 tool_call 的合成 error 结果消息（按历史顺序，输入不被修改）。

    已有配对 tool 结果、或结果内联在 ToolCall.result（ACP 外部智能体记录方式）
    的调用不算孤儿。平衡的历史返回空列表。
    """
    answered = {
        m.tool_call_id for m in messages if m.role == "tool" and m.tool_call_id
    }
    repaired: list[Message] = []
    for m in messages:
        if m.role != "assistant" or not m.tool_calls:
            continue
        for tc in m.tool_calls:
            if tc.id in answered or tc.result:
                continue
            if tc.status == "running" or tc.duration is not None:
                code, text = TOOL_OUTCOME_UNKNOWN, _OUTCOME_UNKNOWN_TEXT
            else:
                code, text = TOOL_NOT_STARTED, _NOT_STARTED_TEXT
            repaired.append(Message.tool(tc.id, f"{code}: {text}", name=tc.name))
    return repaired
