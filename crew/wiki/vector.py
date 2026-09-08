"""Wiki 向量索引：SQLite BLOB + 暴力 cosine 的语义检索索引。

结构与 ``SQLiteFTS5SearchIndex`` 对齐（pending/batch/``SQLiteWriteHelper``/``threading.Lock``），
按 (owner, kb) 隔离。向量以 float32 小端 BLOB 存储，检索时用 numpy（若可用）或纯 Python
做暴力 cosine；KBs 规模（数百~数千页）下 O(N·dim) 足够，更大规模再换 sqlite-vec/faiss。
"""

from __future__ import annotations

import math
import struct
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from crew.state.logging import get_logger
from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite
from crew.wiki.schemas import WikiPage

log = get_logger("wiki.vector")


def page_embedding_text(page: WikiPage) -> str:
    """把页面折叠为一段可嵌入文本，标题在前（截断时优先保留标题）。"""
    parts: list[str] = [page.title]
    parts.extend(alias for alias in page.aliases if alias)
    parts.extend(tag for tag in page.tags if tag)
    if page.content:
        parts.append(page.content)
    for claim in page.claims:
        if claim.statement:
            parts.append(claim.statement)
        parts.extend(evidence.excerpt for evidence in claim.evidence if evidence.excerpt)
    return "\n".join(parts)


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack(blob: bytes, dim: int) -> list[float]:
    return list(struct.unpack(f"<{dim}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


class WikiVectorIndex(ABC):
    """向量索引抽象，维护单个知识库内的页面向量并返回相似页。"""

    model: str = ""
    dim: int = 0

    @abstractmethod
    def sync_page(self, page: WikiPage) -> None:
        """把单个页面嵌入并写入索引。"""

    def sync_pages(self, pages: Iterable[WikiPage]) -> None:
        """批量同步；实现可覆写为单事务提交。"""
        for page in pages:
            self.sync_page(page)

    @abstractmethod
    def delete_pages(self, page_ids: list[str]) -> None:
        """从索引中删除一组页面。"""

    @abstractmethod
    def search_by_vector(self, query: list[float], top_k: int) -> list[tuple[str, float]]:
        """按向量检索，返回按 cosine 相似度降序的 (page_id, score) 列表。"""

    @contextmanager
    def batch(self) -> Iterator[None]:
        """把一组索引变更合并提交；不支持时退化为空上下文。"""
        yield

    def rebuild(self, pages: Iterable[WikiPage]) -> None:
        """清空并重新嵌入全部页面。"""
        self.delete_pages([])
        self.sync_pages(pages)

    def close(self) -> None:
        """释放索引资源。"""


class SQLiteVectorIndex(WikiVectorIndex):
    """基于 SQLite BLOB + 暴力 cosine 的向量索引，按 KB 隔离。"""

    def __init__(
        self,
        db_path: Path | str,
        *,
        embed_fn: Callable[[list[str]], list[list[float]]],
        model: str = "",
        dim: int = 0,
        batch_size: int = 64,
    ) -> None:
        self._db_path = Path(db_path)
        self._embed_fn = embed_fn
        self._batch_size = max(1, int(batch_size))
        self.model = model
        self.dim = dim
        self._lock = threading.Lock()
        self._conn = connect_sqlite(self._db_path, wal_enabled=True)
        self._writer = SQLiteWriteHelper(self._conn, self._lock)
        self._closed = False
        self._batch_depth = 0
        self._pending_pages: dict[str, WikiPage] = {}
        self._pending_deletes: set[str] = set()
        self._writer.execute(self._ensure_table)
        self.stored_model, self.stored_dim = self._read_meta()

    def _ensure_table(self, conn) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pages_vectors (
                page_id TEXT PRIMARY KEY,
                vector BLOB NOT NULL,
                dim INTEGER NOT NULL,
                model TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vectors_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )

    def _read_meta(self) -> tuple[str | None, int]:
        try:
            rows = self._conn.execute("SELECT key, value FROM vectors_meta").fetchall()
        except Exception as exc:  # noqa: BLE001
            log.warning("读取 Wiki 向量索引元数据失败: %s", exc)
            return None, 0
        meta = dict(rows)
        return meta.get("model"), int(meta.get("dim") or 0)

    def _set_dim(self, vectors: list[list[float]]) -> None:
        if self.dim == 0 and vectors and vectors[0]:
            self.dim = len(vectors[0])

    def _embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for i in range(0, len(texts), self._batch_size):
            vectors.extend(self._embed_fn(texts[i : i + self._batch_size]))
        return vectors

    def _write_rows(
        self,
        conn,
        rows: list[tuple[str, list[float]]],
        deleted_ids: Iterable[str] = (),
    ) -> None:
        deleted = list(dict.fromkeys(deleted_ids))
        if deleted:
            placeholders = ",".join("?" for _ in deleted)
            conn.execute(
                f"DELETE FROM pages_vectors WHERE page_id IN ({placeholders})",
                deleted,
            )
        now = time.time()
        for page_id, vector in rows:
            conn.execute("DELETE FROM pages_vectors WHERE page_id = ?", (page_id,))
            conn.execute(
                "INSERT INTO pages_vectors (page_id, vector, dim, model, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (page_id, _pack(vector), len(vector), self.model, now),
            )
        if rows or deleted:
            conn.execute(
                "INSERT OR REPLACE INTO vectors_meta (key, value) VALUES ('model', ?)",
                (self.model,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO vectors_meta (key, value) VALUES ('dim', ?)",
                (str(self.dim),),
            )
            self.stored_model = self.model
            self.stored_dim = self.dim

    def _commit_pending(self) -> None:
        with self._lock:
            pages = list(self._pending_pages.values())
            deleted = list(self._pending_deletes)
            self._pending_pages.clear()
            self._pending_deletes.clear()
        if not pages and not deleted:
            return
        rows: list[tuple[str, list[float]]] = []
        if pages:
            try:
                texts = [page_embedding_text(page) for page in pages]
                vectors = self._embed_texts(texts)
            except Exception as exc:  # noqa: BLE001
                log.warning("批量嵌入 Wiki 页面失败: %s", exc)
                vectors = []
            if len(vectors) == len(pages):
                self._set_dim(vectors)
                rows = [(page.id, vector) for page, vector in zip(pages, vectors)]
        try:
            self._writer.execute(lambda conn: self._write_rows(conn, rows, deleted))
        except Exception as exc:  # noqa: BLE001
            log.warning("同步 Wiki 向量索引失败: %s", exc)

    @contextmanager
    def batch(self) -> Iterator[None]:
        """将批量页面变更合并到一次嵌入 + 一个写事务。"""
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
        pages = list(pages)
        if not pages:
            return
        with self._lock:
            if self._batch_depth:
                for page in pages:
                    self._pending_pages[page.id] = page
                    self._pending_deletes.discard(page.id)
                return
        try:
            vectors = self._embed_texts([page_embedding_text(page) for page in pages])
        except Exception as exc:  # noqa: BLE001
            log.warning("嵌入 Wiki 页面失败 %s: %s", ",".join(p.id for p in pages), exc)
            return
        if len(vectors) != len(pages):
            log.warning("嵌入返回向量数 %d 与页面数 %d 不一致，跳过", len(vectors), len(pages))
            return
        self._set_dim(vectors)
        rows = [(page.id, vector) for page, vector in zip(pages, vectors)]
        try:
            self._writer.execute(lambda conn: self._write_rows(conn, rows))
        except Exception as exc:  # noqa: BLE001
            log.warning("同步 Wiki 向量索引失败 %s: %s", ",".join(p.id for p in pages), exc)

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
            self._writer.execute(lambda conn: self._write_rows(conn, [], page_ids))
        except Exception as exc:  # noqa: BLE001
            log.warning("删除 Wiki 向量索引失败 %s: %s", page_ids, exc)

    def search_by_vector(self, query: list[float], top_k: int) -> list[tuple[str, float]]:
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT page_id, vector, dim FROM pages_vectors"
                ).fetchall()
            except Exception as exc:  # noqa: BLE001
                log.warning("读取 Wiki 向量索引失败: %s", exc)
                return []
        if not rows:
            return []
        ids = [row[0] for row in rows]
        vectors = [_unpack(row[1], row[2]) for row in rows]
        try:
            import numpy as np
        except ImportError:
            np = None
        if np is not None:
            matrix = np.asarray(vectors, dtype=np.float32)
            q = np.asarray(query, dtype=np.float32)
            q_norm = float(np.linalg.norm(q)) or 1.0
            norms = np.linalg.norm(matrix, axis=1)
            norms[norms == 0] = 1.0
            similarities = (matrix @ q) / (norms * q_norm)
            order = np.argsort(-similarities)
            return [(ids[int(i)], float(similarities[int(i)])) for i in order[:top_k]]
        scored = [(pid, _cosine(query, vector)) for pid, vector in zip(ids, vectors)]
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:top_k]

    def rebuild(self, pages: Iterable[WikiPage]) -> None:
        pages = list(pages)
        rows: list[tuple[str, list[float]]] = []
        if pages:
            try:
                texts = [page_embedding_text(page) for page in pages]
                vectors = self._embed_texts(texts)
            except Exception as exc:  # noqa: BLE001
                log.warning("重建向量索引嵌入失败: %s", exc)
                return
            if len(vectors) != len(pages):
                log.warning("重建嵌入返回数量不一致，跳过")
                return
            self._set_dim(vectors)
            rows = [(page.id, vector) for page, vector in zip(pages, vectors)]

        def _do(conn) -> None:
            conn.execute("DELETE FROM pages_vectors")
            self._write_rows(conn, rows, [])

        try:
            self._writer.execute(_do)
        except Exception as exc:  # noqa: BLE001
            log.warning("重建 Wiki 向量索引失败: %s", exc)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._pending_pages.clear()
            self._pending_deletes.clear()
            self._conn.close()
            self._closed = True
