"""来源素材轻量元数据生成（第二层）。

只生成一句话摘要、标签、文档类型和是否建议深度整理，
不生成 entity/topic/relationship 等结构化知识。
"""

from __future__ import annotations

import json
from typing import Any

from crew.core.interfaces import LLMProvider
from crew.core.types import Message

from ._llm import chat_text

# 内容截断长度，控制 prompt 大小与成本
_LIGHT_SUMMARY_MAX_CHARS = 8_000

_LIGHT_SUMMARY_PROMPT = """你负责为刚上传到 Wiki 的素材生成轻量元数据，帮助用户快速判断是否需要深度整理。

请阅读下面素材的前 {max_chars} 个字符（已截断），输出一个紧凑 JSON：

{{
  "summary": "一句话概括这份素材的核心内容，40字以内",
  "tags": ["标签1", "标签2", "标签3"],
  "doc_type": "paper|meeting|article|code_doc|spec|note|log|image_desc|other",
  "ingest_recommend": true|false,
  "ingest_reason": "如果建议整理，说明原因；不建议则为空"
}}

判断规则：
- ingest_recommend=true：素材包含可长期复用的知识、概念、结论或结构化信息（如论文、技术文档、正式会议纪要、产品规格）。
- ingest_recommend=false：素材是临时性的、用于单次排查的、内容杂乱或过时的（如日志、草稿、临时截图、重复文件、只含寒暄的聊天记录）。

素材内容：
---
{content}
---

只输出 JSON，不要解释文字。如果内容为空或无法判断，输出：
{{"summary":"", "tags":[], "doc_type":"other", "ingest_recommend":false, "ingest_reason":"内容不足"}}
"""


def _strip_code_fence(text: str) -> str:
    """去掉 LLM 输出可能携带的 markdown 代码围栏。"""
    lines = text.strip().splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _limit_tags(tags: object) -> list[str]:
    """清洗并限制标签数量与长度。"""
    if not isinstance(tags, list):
        return []
    return [str(t).strip()[:20] for t in tags if str(t).strip()][:5]


def _default_metadata() -> dict[str, Any]:
    return {
        "summary": "",
        "tags": [],
        "doc_type": "other",
        "ingest_recommend": False,
        "ingest_reason": "",
    }


async def generate_source_metadata(
    provider: LLMProvider,
    content: str,
) -> dict[str, Any]:
    """生成 source 的轻量元数据，失败时返回安全默认值。"""
    prompt = _LIGHT_SUMMARY_PROMPT.format(
        content=content[:_LIGHT_SUMMARY_MAX_CHARS],
        max_chars=_LIGHT_SUMMARY_MAX_CHARS,
    )
    try:
        text = _strip_code_fence(await chat_text(provider, [Message.user(prompt)]))
        data = json.loads(text)
        if not isinstance(data, dict):
            return _default_metadata()
        return {
            "summary": str(data.get("summary", "")).strip()[:80],
            "tags": _limit_tags(data.get("tags")),
            "doc_type": str(data.get("doc_type", "other")).strip()[:30],
            "ingest_recommend": bool(data.get("ingest_recommend", False)),
            "ingest_reason": str(data.get("ingest_reason", "")).strip()[:120],
        }
    except Exception:
        return _default_metadata()
