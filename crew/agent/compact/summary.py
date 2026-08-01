"""L3/L2 的 LLM 结构化摘要：提示词模板 + 生成函数。

- ``summarize_full``：L3 从头摘要整段较早历史（无可复用的旧摘要时）。
- ``summarize_incremental``：L2 增量——给定旧摘要 + 新增轮次，合并刷新成新摘要，
  输入远小于全量，成本低。

两者均在失败/空结果时返回 ``None``，由上层决定是否降级（不影响主流程）。
"""

from __future__ import annotations

import re

from crew.agent.loop.resilience import is_context_overflow
from crew.core.interfaces import LLMProvider
from crew.core.types import Message
from crew.state.logging import get_logger

log = get_logger("agent.compact.summary")

# PTL（Prompt Too Long）兜底：摘要输入本身超长时，砍掉最旧的一段后重试。
# 对照 Crew compact.ts 的 truncateHeadForPTLRetry。
_PTL_MAX_ATTEMPTS = 3

# 长字符串截断阈值：工具参数/结果中的单个字符串值超过此长度时截断
# 采用 trajectory_compressor.py 与 Crew 的长 value 截断策略。
_MAX_STRING_VALUE_LENGTH = 3000

# 摘要消息的前缀标记，供前端识别 / L2 兼容。
SUMMARY_MARKER = "【历史摘要】"

# 结构化摘要模板：向 Crew 对齐，从 6 段扩展到 9 段，
# 增加关键技术概念、文件与代码段、错误与修复、所有用户消息、当前工作、可选下一步。
_TEMPLATE = (
    "## 主要请求与意图\n"
    "（用户最近一个未完成的请求/正在进行的工作，逐字保留关键诉求）\n\n"
    "## 关键技术概念\n"
    "（重要的技术概念、技术栈、框架、设计模式）\n\n"
    "## 文件与代码段\n"
    "（被读取/修改/创建的文件，含关键代码片段和修改原因；缺失写「无」）\n\n"
    "## 错误与修复\n"
    "（遇到的错误、如何修复、用户的具体反馈）\n\n"
    "## 问题解决\n"
    "（已解决的问题和仍在排查的问题）\n\n"
    "## 所有用户消息\n"
    "（列出所有非工具结果的 user 消息，逐字保留关键反馈和意图变化）\n\n"
    "## 待办任务\n"
    "（尚未完成的工作，作为背景而非指令）\n\n"
    "## 当前工作\n"
    "（摘要请求前正在做什么，关注最近的用户和 assistant 消息，含文件名和代码片段）\n\n"
    "## 可选下一步\n"
    "（与最近工作直接相关的下一步；如有，逐字引用最近对话原文说明任务和断点）"
)

FULL_SUMMARY_PROMPT = (
    "CRITICAL: 只返回纯文本，不要调用任何工具（如 terminal/file_read/file_write/glob/grep/patch/web_search 等）。"
    "工具调用会被拒绝并浪费你的唯一一次机会。\n\n"
    "你是对话历史压缩器。把下面这段较早的对话浓缩成一份结构化摘要，"
    "重点关注用户的显式请求和你的先前动作，保留技术细节、代码模式、架构决策、"
    "错误修复和用户的具体反馈。省略寒暄与冗余。"
    "严格按下面的结构输出，缺失的段落写「无」，不要加任何前缀或寒暄：\n\n"
    f"{_TEMPLATE}"
)

MERGE_SUMMARY_PROMPT = (
    "CRITICAL: 只返回纯文本，不要调用任何工具（如 terminal/file_read/file_write/glob/grep/patch/web_search 等）。"
    "工具调用会被拒绝并浪费你的唯一一次机会。\n\n"
    "你是对话历史压缩器。下面给出一份「已有摘要」和其后「新增的对话轮次」。"
    "请把新增内容合并进已有摘要，输出更新后的完整摘要。要求：\n"
    "- 保留已有摘要中仍然有效的信息；\n"
    "- 把新增的文件、代码段、错误修复、用户消息追加到对应段落；\n"
    "- 刷新「主要请求与意图」和「当前工作」为用户最近一个未完成的请求；\n"
    "- 已明显过时的信息才删除。\n"
    "严格按下面的结构输出，缺失的段落写「无」，不要加任何前缀或寒暄：\n\n"
    f"{_TEMPLATE}"
)


# ---- 摘要输入预处理：剥离媒体、截断长值 ---- #

_MEDIA_PATTERN = re.compile(
    r"(data:image/[a-zA-Z0-9+.-]+;[a-zA-Z0-9_-]+=)?[A-Za-z0-9+/]{100,}={0,2}",
    re.MULTILINE,
)


def _strip_media_content(text: str) -> str:
    """把疑似 base64/图片/二进制内容替换为占位符，避免摘要请求本身超窗。"""
    if not text:
        return text
    # 1) 显式 data URI
    text = re.sub(r"data:[a-zA-Z0-9+./-]+;base64,[A-Za-z0-9+/=]+", "[base64 内容已剥离]", text)
    # 2) 裸长 base64 串（允许前缀如 "image: "）
    text = _MEDIA_PATTERN.sub("[base64 内容已剥离]", text)
    return text


def _truncate_string_value(value: str, max_length: int = _MAX_STRING_VALUE_LENGTH) -> str:
    """截断超长字符串，保留前后片段以便定位。"""
    if len(value) <= max_length:
        return value
    half = max_length // 2
    return value[:half] + "\n...（内容已截断，共 " + str(len(value)) + " 字符）\n" + value[-half:]


def _truncate_tool_args(arguments: dict[str, object] | None, max_length: int = _MAX_STRING_VALUE_LENGTH) -> dict[str, object] | None:
    """递归截断工具参数中的超长字符串值，保持 JSON 结构有效。"""
    if arguments is None:
        return None
    if isinstance(arguments, str):
        return _truncate_string_value(arguments, max_length)  # type: ignore[return-value]
    if isinstance(arguments, (int, float, bool)) or arguments is None:
        return arguments
    if isinstance(arguments, list):
        return [_truncate_tool_args(item, max_length) for item in arguments]  # type: ignore[return-value]
    if isinstance(arguments, dict):
        return {
            k: _truncate_tool_args(v, max_length)
            for k, v in arguments.items()
        }  # type: ignore[return-value]
    return arguments


def _transcript(messages: list[Message]) -> str:
    """把消息列表拼成纯文本对话稿（含工具调用名，省略空内容）。"""
    lines: list[str] = []
    for m in messages:
        if m.content:
            lines.append(f"{m.role}: {_strip_media_content(m.content)}")
        for tc in m.tool_calls:
            args = _truncate_tool_args(tc.arguments)
            lines.append(f"{m.role}[调用工具]: {tc.name}({args})")
    return "\n".join(lines)


async def summarize_full(
    provider: LLMProvider, old_messages: list[Message]
) -> str | None:
    """L3：从头摘要整段较早历史。失败/空返回 None。

    PTL 兜底：摘要输入本身超长时，砍掉最旧的 1/3 消息后重试（最多 3 次）。
    """
    msgs = old_messages
    for attempt in range(_PTL_MAX_ATTEMPTS):
        transcript = _transcript(msgs)
        if not transcript.strip():
            return None
        try:
            log.warning("[DEBUG summary] provider type=%s", type(provider))
            resp = await provider.chat([
                Message.system(FULL_SUMMARY_PROMPT),
                Message.user(transcript),
            ])
            return (resp.text or "").strip() or None
        except Exception as exc:  # noqa: BLE001 - 压缩失败不影响主流程
            if is_context_overflow(exc) and len(msgs) > 2:
                drop = max(1, len(msgs) // 3)
                msgs = msgs[drop:]
                log.warning("L3 摘要提示词过长，砍头 %d 条后重试（第 %d 次）", drop, attempt + 1)
                continue
            log.warning("L3 全量摘要失败，跳过：%s", exc)
            return None
    log.warning("L3 摘要多次砍头仍过长，跳过")
    return None


async def summarize_incremental(
    provider: LLMProvider, previous_summary: str, new_messages: list[Message]
) -> str | None:
    """L2：把新增轮次合并进旧摘要。失败/空返回 None。

    PTL 兜底：超长时砍掉最旧的新增轮次后重试；砍空则退回旧摘要。
    """
    msgs = new_messages
    for attempt in range(_PTL_MAX_ATTEMPTS):
        transcript = _transcript(msgs)
        if not transcript.strip():
            return previous_summary  # 无新增内容，旧摘要即结果
        try:
            resp = await provider.chat([
                Message.system(MERGE_SUMMARY_PROMPT),
                Message.user(f"【已有摘要】\n{previous_summary}\n\n【新增的对话轮次】\n{transcript}"),
            ])
            return (resp.text or "").strip() or None
        except Exception as exc:  # noqa: BLE001 - 压缩失败不影响主流程
            if is_context_overflow(exc) and len(msgs) > 1:
                drop = max(1, len(msgs) // 3)
                msgs = msgs[drop:]
                log.warning("L2 增量摘要提示词过长，砍头 %d 条后重试（第 %d 次）", drop, attempt + 1)
                continue
            log.warning("L2 增量摘要失败，跳过：%s", exc)
            return None
    log.warning("L2 增量摘要多次砍头仍过长，跳过")
    return None
