"""辅助型 LLM 任务：会话标题生成。

「用一次轻量 LLM 调用服务主对话」的旁路能力。
上下文压缩已拆分到 ``crew.agent.compact`` 子包（三层渐进式压缩）。
用于 的 title_generator。
"""

from __future__ import annotations

import asyncio
import re

from crew.core.interfaces import LLMProvider
from crew.core.types import Message
from crew.state.logging import get_logger

log = get_logger("agent.auxiliary")


_TITLE_PROMPT = (
    "根据下面的对话开头，生成一个不超过 12 个字的简短中文标题，"
    "概括用户的核心意图。只输出标题本身，不要标点、引号或前缀。"
)

# 标题生成超时：超过即放弃，用首条 user query 截断兜底，避免挂起请求长占连接。
# 标题是辅助任务，绝不能拖住主流程（generate_session_title 已在后台 task 调用）。
_TITLE_TIMEOUT = 10.0


def _sanitize_title_text(text: str) -> str:
    """去掉标题生成里不应持久化的演示/调试前缀和思考标签。"""
    cleaned = str(text or "").strip()
    # 思考型模型可能把 <think>...</think> 内联在正文里返回（未走 reasoning_content
    # 通道），成对块和孤立标签都要剥掉，避免标签残片混进标题。
    cleaned = re.sub(r"<think>.*?</think>", " ", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"</?think>", " ", cleaned, flags=re.IGNORECASE).strip()
    for prefix in ("[fake] 收到:", "[fake] 收到：", "[fake]"):
        if cleaned.lower().startswith(prefix.lower()):
            cleaned = cleaned[len(prefix):].strip()
            break
    cleaned = cleaned.strip("。.\"'《》：: ")
    cleaned = re.sub(r"\s+(助手|用户)\s*$", "", cleaned)
    return cleaned.strip("。.\"'《》：: ")


async def generate_session_title(
    provider: LLMProvider,
    messages: list[Message],
    *,
    user_only: bool = False,
) -> str:
    """生成简短标题；失败/超时使用首条用户消息兜底。

    ``user_only`` 用于只允许首条用户文本参与标题生成的旁路调用，避免把尚未
    完成或不应进入摘要的 assistant 内容带入辅助请求。
    """
    first_user = _sanitize_title_text(
        next(
            (
                m.content
                for m in messages
                if m.role == "user" and m.content and not m.is_meta
            ),
            "",
        )
    )
    if not first_user:
        first_user = _sanitize_title_text(
            next((m.content for m in messages if m.role == "user" and m.content), "")
        )
    if not first_user:
        return ""
    snippet = first_user[:500]
    if not user_only:
        first_assistant = _sanitize_title_text(
            next((m.content for m in messages if m.role == "assistant" and m.content), "")
        )
        if first_assistant:
            snippet += f"\n助手：{first_assistant[:200]}"
    title = ""
    try:
        resp = await asyncio.wait_for(
            provider.chat([
                Message.system(_TITLE_PROMPT),
                Message.user(snippet),
            ], max_tokens=32),
            timeout=_TITLE_TIMEOUT,
        )
        title = _sanitize_title_text((resp.text or "").replace("\n", " "))
    except Exception as exc:  # noqa: BLE001 - 标题生成失败/超时不影响主流程，下方兜底
        log.warning("会话标题生成失败，使用首条 query 兜底：%s", exc)
    if not title:
        # 超时/失败：用首条 user query 截断作为标题，避免会话一直顶着占位标题
        title = first_user.strip().replace("\n", " ")
    return title[:20]
