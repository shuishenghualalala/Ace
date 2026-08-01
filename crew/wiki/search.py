"""Wiki 搜索索引抽象与 SQLite FTS5 实现。"""

from __future__ import annotations

import sqlite3
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from crew.state.logging import get_logger
from crew.state.sqlite import connect_sqlite
from crew.wiki.schemas import WikiPage

log = get_logger("wiki.search")


class WikiSearchIndex(ABC):
    """Wiki 页面搜索索引抽象。

    实现负责维护单个知识库（KB）内的页面索引，并返回匹配页面的 page_id 列表。
    具体的 WikiStore 负责把 page_id 解析为 WikiPage 对象。
    """

    @abstractmethod
    def sync_page(self, page: WikiPage) -> None:
        """把单个页面同步到索引（新增或更新）。"""

    @abstractmethod
    def delete_pages(self, page_ids: list[str]) -> None:
        """从索引中删除一组页面。"""

    @abstractmethod
    def search(self, query: str, top_k: int) -> list[str]:
        """搜索页面，返回按相关性排序的 page_id 列表。"""


class SQLiteFTS5SearchIndex(WikiSearchIndex):
    """基于 SQLite FTS5 的 Wiki 搜索索引，按 KB 隔离。"""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._lock = threading.Lock()

    @staticmethod
    def _fts_tokenize(text: str) -> str:
        """把中文拆成单字 token，英文/数字保留单词，提升 FTS5 中文匹配能力。

        FTS5 默认 tokenizer 对连续 CJK 字符只生成一个 token，导致搜"负责人"
        无法匹配"租户负责人"。单字分词后，"租户负责人"会生成"租 户 负 责 人"
        五个 token，搜"负责人"（分词后"负 责 人"）即可命中。
        """
        result: list[str] = []
        prev_cjk = False
        for ch in text:
            is_cjk = "一" <= ch <= "鿿"
            is_alnum = ch.isalnum()
            if is_cjk:
                if result and not prev_cjk:
                    result.append(" ")
                result.append(ch)
                result.append(" ")
                prev_cjk = True
            elif is_alnum:
                if result and prev_cjk:
                    result.append(" ")
                result.append(ch)
                prev_cjk = False
            else:
                result.append(" ")
                prev_cjk = False
        return " ".join("".join(result).split())

    def _ensure_table(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
                page_id UNINDEXED,
                title,
                content,
                tags,
                aliases,
                tokenize='porter unicode61'
            )
            """
        )

    def sync_page(self, page: WikiPage) -> None:
        with self._lock:
            try:
                conn = connect_sqlite(self._db_path, wal_enabled=True)
                self._ensure_table(conn)
                title = self._fts_tokenize(page.title)
                claim_parts: list[str] = []
                for claim in page.claims:
                    claim_parts.append(claim.statement)
                    claim_parts.extend(
                        evidence.excerpt
                        for evidence in claim.evidence
                        if evidence.excerpt
                    )
                claim_text = " ".join(claim_parts)
                content = self._fts_tokenize(f"{page.content} {claim_text}")
                tags = self._fts_tokenize(" ".join(page.tags))
                aliases = self._fts_tokenize(" ".join(page.aliases))
                conn.execute("DELETE FROM pages_fts WHERE page_id = ?", (page.id,))
                conn.execute(
                    "INSERT INTO pages_fts (page_id, title, content, tags, aliases) VALUES (?, ?, ?, ?, ?)",
                    (page.id, title, content, tags, aliases),
                )
                conn.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("同步 Wiki FTS 索引失败 %s: %s", page.id, exc)

    def delete_pages(self, page_ids: list[str]) -> None:
        if not page_ids:
            return
        with self._lock:
            try:
                conn = connect_sqlite(self._db_path, wal_enabled=True)
                self._ensure_table(conn)
                placeholders = ",".join("?" for _ in page_ids)
                conn.execute(f"DELETE FROM pages_fts WHERE page_id IN ({placeholders})", page_ids)
                conn.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("删除 Wiki FTS 索引失败 %s: %s", page_ids, exc)

    def search(self, query: str, top_k: int) -> list[str]:
        with self._lock:
            try:
                conn = connect_sqlite(self._db_path, wal_enabled=True)
                self._ensure_table(conn)
                fts_query = self._fts_tokenize(query)
                if not fts_query:
                    conn.close()
                    return []
                rows = conn.execute(
                    "SELECT page_id, bm25(pages_fts, 1, 10, 1, 1, 1) AS score "
                    "FROM pages_fts WHERE pages_fts MATCH ? ORDER BY score LIMIT ?",
                    (fts_query, top_k),
                ).fetchall()
                conn.close()
                return [str(row[0]) for row in rows]
            except Exception as exc:  # noqa: BLE001
                log.warning("Wiki FTS 检索失败，回退关键词搜索: %s", exc)
                return []
