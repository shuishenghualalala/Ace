"""Wiki 页面/源文件 ID、slug、文件名生成。"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

from crew.wiki.schemas import PageType

_PAGE_TYPE_PREFIX = {
    "entity": "ent",
    "topic": "top",
    "source": "src",
    "comparison": "cmp",
    "synthesis": "syn",
}

_PAGE_TYPE_DIR = {
    "entity": "entities",
    "topic": "topics",
    "source": "sources",
    "comparison": "comparisons",
    "synthesis": "synthesis",
}

_DEFAULT_KB_ID = "default"


def filename_from_title(title: str) -> str:
    """把页面标题转换为可用作文件名的字符串（保留中文，清理非法字符）。"""
    text = str(title or "").strip()
    # 替换 Windows/Unix 非法字符为空格
    text = re.sub(r'[\\/:*?"<>|]+', " ", text)
    # 控制字符也替换
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = text.strip()
    if not text:
        return "untitled"
    return text


def unique_file_path(dir_path: Path, filename: str, ext: str = ".md") -> Path:
    """处理重名：追加 " (2)", " (3)" ..."""
    base = dir_path / f"{filename}{ext}"
    if not base.exists():
        return base
    stem = filename
    counter = 2
    while True:
        candidate = dir_path / f"{stem} ({counter}){ext}"
        if not candidate.exists():
            return candidate
        counter += 1


def page_file_path(base_dir: Path, page_type: PageType, title: str) -> Path:
    """生成页面文件路径（含重名处理）。"""
    dir_name = _PAGE_TYPE_DIR.get(page_type, "topics")
    dir_path = base_dir / "wiki" / dir_name
    dir_path.mkdir(parents=True, exist_ok=True)
    filename = filename_from_title(title)
    return unique_file_path(dir_path, filename)


def page_id(page_type: PageType, title: str) -> str:
    """生成稳定的页面 ID。"""
    prefix = _PAGE_TYPE_PREFIX.get(page_type, "top")
    slug = filename_from_title(title).replace(" ", "_").lower()
    if not slug or slug == "untitled":
        slug = "untitled"
    h = hashlib.md5(f"{page_type}:{title}".encode("utf-8")).hexdigest()[:6]
    return f"{prefix}_{slug}_{h}"


def source_page_id(source_id: str) -> str:
    """按 source_id 派生稳定的 Source Page ID，与标题无关。

    Source Page 的唯一身份必须基于 source_id：两份同名但内容不同的来源
    不应合并到同一 Source Page。标题仅用于展示。
    """
    value = str(source_id or "").strip()
    raw = value or "untitled"
    slug = re.sub(r"[\W_]+", "_", raw, flags=re.UNICODE).strip("_").lower() or "untitled"
    digest = hashlib.md5(f"source:{value}".encode("utf-8")).hexdigest()[:8]
    return f"src_{slug}_{digest}"


def source_id_from_filename(path: Path) -> str:
    """从 raw source 文件名提取 source_id。"""
    return path.stem


def normalize_kb_id(kb_id: str | None) -> str:
    """归一化并校验知识库 ID，阻止目录逃逸和不可移植路径。"""
    value = unicodedata.normalize("NFC", str(kb_id or "").strip())
    if not value:
        return _DEFAULT_KB_ID
    if len(value) > 96 or not re.fullmatch(r"[\w-]+", value, flags=re.UNICODE):
        raise ValueError(
            "kb_id 只能包含中文或其他语言的字母、数字、下划线和连字符，"
            "且长度不能超过 96"
        )
    return value
