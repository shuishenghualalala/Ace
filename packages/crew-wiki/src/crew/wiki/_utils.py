"""Wiki 模块内部工具函数（无内部依赖，避免循环导入）。"""

from __future__ import annotations

import re
import unicodedata


def is_wiki_agent_session(session_id: str) -> bool:
    """判断 session_id 是否属于 Wiki Agent 独立会话（以 wiki- 前缀识别）。"""
    return bool(session_id and session_id.startswith("wiki-"))


def normalize_page_key(value: str) -> str:
    """生成用于页面标题/别名匹配的稳定键。

    保留字母、数字和中日韩文字，只消除大小写、全半角、空白与常见标点差异。
    该函数仅用于候选匹配；页面展示标题始终保留原文。
    """
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[\W_]+", "", normalized, flags=re.UNICODE)


def query_terms(value: str) -> list[str]:
    """生成稳定检索词，兼顾英文单词和中文连续文本。"""
    text = str(value or "").casefold()
    terms = re.findall(r"[a-z0-9][a-z0-9_.+-]*", text)
    for block in re.findall(r"[\u3400-\u9fff]+", text):
        if len(block) <= 2:
            terms.append(block)
            continue
        terms.extend(block[index : index + 2] for index in range(len(block) - 1))
    return list(dict.fromkeys(term for term in terms if term))
