"""Wiki 存储层：兼容入口，已拆分为 crew/wiki/store/ 子包。

新代码请从 crew.wiki.store 导入具体模块。
"""

from crew.wiki.store import (
    FileSystemWikiStore,
    WikiStore,
    deserialize_page,
    deserialize_raw,
    filename_from_title,
    normalize_kb_id,
    page_file_path,
    page_id,
    serialize_page,
    serialize_raw,
    source_id_from_filename,
    unique_file_path,
)

__all__ = [
    "WikiStore",
    "FileSystemWikiStore",
    "filename_from_title",
    "unique_file_path",
    "page_file_path",
    "page_id",
    "source_id_from_filename",
    "normalize_kb_id",
    "serialize_page",
    "deserialize_page",
    "serialize_raw",
    "deserialize_raw",
]
