"""Wiki 搜索索引抽象与 SQLite FTS5 实现。"""

from __future__ import annotations

import sqlite3
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from crew.state.logging import get_logger
from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite
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

    def sync_pages(self, pages: Iterable[WikiPage]) -> None:
        """批量同步页面；实现可覆写为单事务提交。"""
        for page in pages:
            self.sync_page(page)

    @abstractmethod
    def delete_pages(self, page_ids: list[str]) -> None:
        """从索引中删除一组页面。"""

    @abstractmethod
    def search(self, query: str, top_k: int) -> list[str]:
        """搜索页面，返回按相关性排序的 page_id 列表。"""

    @contextmanager
    def batch(self) -> Iterator[None]:
        """把一组索引变更合并提交；不支持时退化为空上下文。"""
        yield

    def close(self) -> None:
        """释放索引资源。"""


class SQLiteFTS5SearchIndex(WikiSearchIndex):
    """基于 SQLite FTS5 的 Wiki 搜索索引，按 KB 隔离。"""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._lock = threading.Lock()
        self._conn = connect_sqlite(self._db_path, wal_enabled=True)
        self._writer = SQLiteWriteHelper(self._conn, self._lock)
        self._closed = False
        self._batch_depth = 0
        self._pending_pages: dict[str, tuple[str, str, str, str, str]] = {}
        self._pending_deletes: set[str] = set()
        self._writer.execute(self._ensure_table)

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

    @staticmethod
    def _page_row(page: WikiPage) -> tuple[str, str, str, str, str]:
        title = SQLiteFTS5SearchIndex._fts_tokenize(page.title)
        claim_parts: list[str] = []
        for claim in page.claims:
            claim_parts.append(claim.statement)
            claim_parts.extend(
                evidence.excerpt
                for evidence in claim.evidence
                if evidence.excerpt
            )
        claim_text = " ".join(claim_parts)
        content = SQLiteFTS5SearchIndex._fts_tokenize(f"{page.content} {claim_text}")
        tags = SQLiteFTS5SearchIndex._fts_tokenize(" ".join(page.tags))
        aliases = SQLiteFTS5SearchIndex._fts_tokenize(" ".join(page.aliases))
        return page.id, title, content, tags, aliases

    @staticmethod
    def _write_pages(
        conn: sqlite3.Connection,
        rows: Iterable[tuple[str, str, str, str, str]],
        deleted_ids: Iterable[str] = (),
    ) -> None:
        deleted = list(dict.fromkeys(deleted_ids))
        if deleted:
            placeholders = ",".join("?" for _ in deleted)
            conn.execute(
                f"DELETE FROM pages_fts WHERE page_id IN ({placeholders})",
                deleted,
            )
        for page_id, title, content, tags, aliases in rows:
            conn.execute("DELETE FROM pages_fts WHERE page_id = ?", (page_id,))
            conn.execute(
                "INSERT INTO pages_fts (page_id, title, content, tags, aliases) "
                "VALUES (?, ?, ?, ?, ?)",
                (page_id, title, content, tags, aliases),
            )

    def _commit_pending(self) -> None:
        with self._lock:
            rows = list(self._pending_pages.values())
            deleted = list(self._pending_deletes)
            self._pending_pages.clear()
            self._pending_deletes.clear()
        if not rows and not deleted:
            return
        try:
            self._writer.execute(lambda conn: self._write_pages(conn, rows, deleted))
        except Exception as exc:  # noqa: BLE001
            log.warning("同步 Wiki FTS 索引失败: %s", exc)

    @contextmanager
    def batch(self) -> Iterator[None]:
        """将批量页面变更合并到一个 SQLite 写事务。"""
        with self._lock:
            self._batch_depth += 1
        try:
            yield
        finally:
            flush = False
            with self._lock:
                self._batch_depth -= 1
                flush = self._batch_depth == 0
            if flush:
                self._commit_pending()

    def sync_page(self, page: WikiPage) -> None:
        self.sync_pages([page])

    def sync_pages(self, pages: Iterable[WikiPage]) -> None:
        rows = [self._page_row(page) for page in pages]
        if not rows:
            return
        with self._lock:
            if self._batch_depth:
                for row in rows:
                    self._pending_pages[row[0]] = row
                    self._pending_deletes.discard(row[0])
                return
        try:
            self._writer.execute(lambda conn: self._write_pages(conn, rows))
        except Exception as exc:  # noqa: BLE001
            log.warning("同步 Wiki FTS 索引失败 %s: %s", ",".join(row[0] for row in rows), exc)

    def delete_pages(self, page_ids: list[str]) -> None:
        if not page_ids:
            return
        page_ids = list(dict.fromkeys(page_ids))
        with self._lock:
            if self._batch_depth:
                self._pending_deletes.update(page_ids)
                for page_id in page_ids:
                    self._pending_pages.pop(page_id, None)
                return
        try:
            self._writer.execute(
                lambda conn: self._write_pages(conn, (), page_ids)
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("删除 Wiki FTS 索引失败 %s: %s", page_ids, exc)

    def search(self, query: str, top_k: int) -> list[str]:
        with self._lock:
            try:
                fts_query = self._fts_tokenize(query)
                if not fts_query:
                    return []
                rows = self._conn.execute(
                    "SELECT page_id, bm25(pages_fts, 1, 10, 1, 1, 1) AS score "
                    "FROM pages_fts WHERE pages_fts MATCH ? ORDER BY score LIMIT ?",
                    (fts_query, top_k),
                ).fetchall()
                return [str(row[0]) for row in rows]
            except Exception as exc:  # noqa: BLE001
                log.warning("Wiki FTS 检索失败，回退关键词搜索: %s", exc)
                return []

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._pending_pages.clear()
            self._pending_deletes.clear()
            self._conn.close()
            self._closed = True
