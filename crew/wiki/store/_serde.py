"""Wiki 页面与 RawSource 的序列化/反序列化。"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import yaml

from crew.state.logging import get_logger
from crew.wiki.schemas import PageType, RawSource, WikiPage
from crew.wiki.store._ids import page_id

log = get_logger("wiki.store.serde")


def serialize_page(page: WikiPage) -> str:
    """把 WikiPage 序列化为带 YAML frontmatter 的 Markdown。"""
    frontmatter = {
        "id": page.id,
        "page_type": page.page_type,
        "title": page.title,
        "file_path": page.file_path,
        "sources": page.sources,
        "related": page.related,
        "tags": page.tags,
        "created_at": page.created_at,
        "updated_at": page.updated_at,
        "aliases": page.aliases,
        "summary": page.summary,
        "claims": [claim.to_dict() for claim in page.claims],
        "confidence": page.confidence,
        "contested": page.contested,
        "contradictions": page.contradictions,
        "relations": [relation.to_dict() for relation in page.relations],
        "stale": page.stale,
    }
    yaml_text = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)
    return f"---\n{yaml_text}---\n\n{page.content}"


def deserialize_page(text: str, file_path: str) -> WikiPage:
    """从 Markdown 文本解析 WikiPage，frontmatter 损坏时容错。"""
    text = str(text or "")
    if text.startswith("---"):
        try:
            _, fm, content = text.split("---", 2)
            data: dict[str, Any] = yaml.safe_load(fm) or {}
            data["content"] = content.strip()
            data["file_path"] = file_path
            return WikiPage.from_dict(data)
        except Exception:  # noqa: BLE001
            log.warning("Wiki 页面 frontmatter 解析失败: %s", file_path)
    # 容错：把整段文本当内容
    title = Path(file_path).stem
    page_type: PageType = "topic"
    return WikiPage(
        id=page_id(page_type, title),
        page_type=page_type,
        title=title,
        content=text,
        file_path=file_path,
        created_at=time.time(),
        updated_at=time.time(),
    )


# brief 模式下从正文头部提取摘要的字符数
_BRIEF_SUMMARY_LEN = 200


def _extract_summary(content: str) -> str:
    """取正文前 N 个字符作为列表摘要，去除 Markdown 标题标记。"""
    text = content.strip()
    # 去掉首行的 # 标题标记
    text = re.sub(r"^#+\s*", "", text)
    return re.sub(r"\s+", " ", text).strip()[:_BRIEF_SUMMARY_LEN]


def deserialize_page_brief(text: str, file_path: str) -> WikiPage:
    """从 Markdown 文本解析 WikiPage 的简要信息（用于列表）。

    只解析 frontmatter，正文仅提取前 200 字符作为 summary，
    不加载完整 content，避免大文件 IO 拖慢列表接口。
    """
    text = str(text or "")
    data: dict[str, Any] = {"file_path": file_path, "content": ""}
    if text.startswith("---"):
        try:
            parts = text.split("---", 2)
            if len(parts) >= 3:
                _, fm, content_head = parts
                data.update(yaml.safe_load(fm) or {})
                data["summary"] = _extract_summary(content_head)
            else:
                # frontmatter 未闭合，按容错处理
                data["summary"] = _extract_summary(text)
        except Exception:  # noqa: BLE001
            log.warning("Wiki 页面 frontmatter 解析失败: %s", file_path)
            data["summary"] = _extract_summary(text)
    else:
        data["summary"] = _extract_summary(text)
    return WikiPage.from_dict(data)


def serialize_raw(source: RawSource) -> str:
    """把 RawSource 序列化为带 frontmatter 的 Markdown。"""
    frontmatter = {
        "id": source.id,
        "title": source.title,
        "source_type": source.source_type,
        "original_path": source.original_path,
        "parsed_path": source.parsed_path,
        "file_type": source.file_type,
        "size": source.size,
        "created_at": source.created_at,
        "session_id": source.session_id,
        "parse_status": source.parse_status,
        "parse_error": source.parse_error,
        "original_sha256": source.original_sha256,
        "content_sha256": source.content_sha256,
        "drift_from": source.drift_from,
        "is_duplicate": source.is_duplicate,
        "source_url": source.source_url,
        "source_kind": source.source_kind,
        "source_platform": source.source_platform,
        "adapter_name": source.adapter_name,
        "original_ref": source.original_ref,
        "extraction_state": source.extraction_state,
        "superseded_by": source.superseded_by,
        "last_refresh_at": source.last_refresh_at,
        "last_refresh_error": source.last_refresh_error,
    }
    yaml_text = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)
    return f"---\n{yaml_text}---\n"


def deserialize_raw(text: str, source_id: str) -> RawSource | None:
    """从 Markdown frontmatter 解析 RawSource。"""
    text = str(text or "")
    if not text.startswith("---"):
        return None
    try:
        _, fm, _ = text.split("---", 2)
        data: dict[str, Any] = yaml.safe_load(fm) or {}
        data["id"] = source_id
        return RawSource.from_dict(data)
    except Exception:  # noqa: BLE001
        log.warning("RawSource frontmatter 解析失败: %s", source_id)
        return None
