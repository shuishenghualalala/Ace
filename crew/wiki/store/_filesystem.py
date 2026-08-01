"""基于本地文件系统的 Wiki 存储实现。"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from crew.state.home import get_owner_runtime_home, owner_path_segment
from crew.state.logging import get_logger
from crew.wiki.schemas import HomeIntro, KBSummary, KnowledgeBase, LintIssue, RawSource, WikiGraph, WikiOrientation, WikiPage
from crew.wiki.sources import SOURCE_DIRS
from crew.wiki._utils import normalize_page_key, query_terms
from crew.wiki.search import SQLiteFTS5SearchIndex, WikiSearchIndex
from crew.wiki.store._base import WikiStore
from crew.wiki.store._ids import (
    _DEFAULT_KB_ID,
    normalize_kb_id,
    page_file_path,
    page_id,
    source_id_from_filename,
)
from crew.wiki.store._serde import (
    deserialize_page,
    deserialize_page_brief,
    deserialize_raw,
    serialize_page,
)

log = get_logger("wiki.store.fs")

_ACTIVE_PAGE_DIRS = ("entities", "topics", "sources", "comparisons", "synthesis")
_PAGE_DIRS = _ACTIVE_PAGE_DIRS
_PAGE_TYPES = ("entity", "topic", "source", "comparison", "synthesis")
_PAGE_TYPE_LABELS = {
    "entity": "关键词",
    "topic": "话题",
    "source": "来源摘要",
    "comparison": "对比分析",
    "synthesis": "综合报告",
}


class FileSystemWikiStore(WikiStore):
    """基于本地文件系统的 Wiki 存储实现，支持多知识库与 SQLite FTS5 全文索引。"""

    def __init__(
        self,
        base_dir: Path | str | None = None,
        *,
        storage_root: Path | str | None = None,
    ) -> None:
        if base_dir and storage_root:
            raise ValueError("base_dir 与 storage_root 不能同时设置")
        self._base_dir = Path(base_dir) if base_dir else None
        self._storage_root = Path(storage_root) if storage_root else None
        self._locks: dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()
        self._search_indexes: dict[str, WikiSearchIndex] = {}

    def _owner_home(self, owner_account_id: str = "") -> Path:
        """返回 owner 级运行时 home（不依赖 kb_id）。"""
        if self._base_dir:
            return self._base_dir
        if self._storage_root:
            owner = str(owner_account_id or "").strip()
            if owner:
                path = self._storage_root / "accounts" / owner_path_segment(owner)
            else:
                path = self._storage_root
            path.mkdir(parents=True, exist_ok=True)
            return path
        return get_owner_runtime_home(owner_account_id or "")

    def _legacy_dir(self, owner_account_id: str = "") -> Path:
        """旧版 wiki 目录（兼容回退）。"""
        return self._owner_home(owner_account_id) / "wiki"

    def _legacy_raw_dir(self, owner_account_id: str = "") -> Path:
        """旧版 raw source 目录（兼容回退）。"""
        return self._owner_home(owner_account_id) / "wiki_raw"

    def _kb_root(self, owner_account_id: str = "") -> Path:
        """多知识库根目录 wiki_lib/。"""
        return self._owner_home(owner_account_id) / "wiki_lib"

    def _dir(self, owner_account_id: str = "", kb_id: str = "default") -> Path:
        kb_id = normalize_kb_id(kb_id)
        path = self._kb_root(owner_account_id) / kb_id
        # 兼容旧版：default KB 且新版目录不存在但旧版 wiki/ 存在时，使用旧版路径
        if kb_id == _DEFAULT_KB_ID:
            legacy = self._legacy_dir(owner_account_id)
            if not path.exists() and legacy.exists():
                path = legacy
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _raw_dir(self, owner_account_id: str = "", kb_id: str = "default") -> Path:
        kb_id = normalize_kb_id(kb_id)
        # 默认优先使用新版路径 wiki_lib/{kb_id}/raw
        new_raw = self._kb_root(owner_account_id) / kb_id / "raw"
        # 兼容旧版：default KB 且新版 raw 目录不存在但旧版 wiki_raw/ 存在时，使用旧版路径
        if kb_id == _DEFAULT_KB_ID:
            legacy_raw = self._legacy_raw_dir(owner_account_id)
            if not new_raw.exists() and legacy_raw.exists():
                return legacy_raw
        new_raw.mkdir(parents=True, exist_ok=True)
        return new_raw

    def _lock(self, owner_account_id: str = "", kb_id: str = "default") -> threading.Lock:
        with self._global_lock:
            key = f"{owner_account_id or '__default__'}:{normalize_kb_id(kb_id)}"
            if key not in self._locks:
                self._locks[key] = threading.Lock()
            return self._locks[key]

    def _search_index_key(self, owner_account_id: str, kb_id: str) -> str:
        return f"{owner_account_id or '__default__'}:{normalize_kb_id(kb_id)}"

    def _search_index(self, owner_account_id: str, kb_id: str) -> WikiSearchIndex:
        key = self._search_index_key(owner_account_id, kb_id)
        if key not in self._search_indexes:
            db_path = self._dir(owner_account_id, kb_id) / ".crew" / "index" / "fts.db"
            self._search_indexes[key] = SQLiteFTS5SearchIndex(db_path)
        return self._search_indexes[key]

    def _source_dir(
        self,
        source_kind: str,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> Path:
        raw_dir = self._raw_dir(owner_account_id, kb_id)
        path = raw_dir / SOURCE_DIRS.get(str(source_kind), "assets")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _source_meta_dir(self, owner_account_id: str = "", kb_id: str = "default") -> Path:
        path = self._dir(owner_account_id, kb_id) / ".crew" / "sources"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def init_kb(self, owner_account_id: str = "", kb_id: str = "default") -> None:
        base = self._dir(owner_account_id, kb_id)
        # raw 目录与页面目录一起初始化
        self._raw_dir(owner_account_id, kb_id)
        for sub in SOURCE_DIRS.values():
            (base / "raw" / sub).mkdir(parents=True, exist_ok=True)
        for sub in _ACTIVE_PAGE_DIRS:
            (base / "wiki" / sub).mkdir(parents=True, exist_ok=True)
        for sub in SOURCE_DIRS.values():
            (base / "wiki" / "sources" / sub).mkdir(parents=True, exist_ok=True)
        for sub in ("sources", "plans", "cache", "index"):
            (base / ".crew" / sub).mkdir(parents=True, exist_ok=True)
        # 新版 schema；读取端继续兼容 SCHEMA.md。
        schema_path = base / ".wiki-schema.md"
        if not schema_path.exists():
            schema_path.write_text(_DEFAULT_SCHEMA_MD, encoding="utf-8")
        else:
            schema_text = schema_path.read_text(encoding="utf-8", errors="replace")
            migrated_schema = schema_text.replace(
                "# Crew Wiki Schema",
                "# 知识库维护规则",
                1,
            ).replace(
                "- Crew 内部状态保存到 `.crew/`",
                "- 系统内部状态保存到 `.crew/`",
            )
            if migrated_schema != schema_text:
                schema_path.write_text(migrated_schema, encoding="utf-8")
        # index.md
        index_path = base / "index.md"
        if not index_path.exists():
            index_path.write_text("# 知识导航\n\n暂无页面。\n", encoding="utf-8")
        else:
            index_text = index_path.read_text(encoding="utf-8", errors="replace")
            migrated_index = (
                index_text.replace("# Crew Wiki", "# 知识导航", 1)
                .replace("## 实体\n", "## 关键词\n")
                .replace("## 主题\n", "## 话题\n")
                .replace("## 来源\n", "## 来源摘要\n")
            )
            if migrated_index != index_text:
                index_path.write_text(migrated_index, encoding="utf-8")
        # log.md
        log_path = base / "log.md"
        if not log_path.exists():
            log_path.write_text("# Wiki 更新日志\n\n", encoding="utf-8")
        home_path = base / "Home.md"
        if not home_path.exists():
            home_path.write_text(_build_home_markdown(kb_id, [], []), encoding="utf-8")
        else:
            home_text = home_path.read_text(encoding="utf-8", errors="replace")
            if _is_legacy_generated_empty_home(home_text):
                home_path.write_text(_build_home_markdown(kb_id, [], []), encoding="utf-8")

    def _quick_count(self, base: Path, raw_dir: Path) -> tuple[int, int]:
        """快速统计 KB 的页面数和 source 数（只数文件，不解序列化）。"""
        page_count = 0
        for sub in _PAGE_DIRS:
            current_dir = base / "wiki" / sub
            legacy_dir = base / sub
            if current_dir.exists():
                page_count += len(list(current_dir.rglob("*.md")))
            if legacy_dir.exists():
                page_count += len(list(legacy_dir.glob("*.md")))
        meta_dir = base / ".crew" / "sources"
        if meta_dir.exists():
            raw_count = len(list(meta_dir.glob("*.json")))
        else:
            raw_count = len([
                path for path in raw_dir.glob("*.md")
                if not path.name.endswith(".parsed.md")
            ]) if raw_dir.exists() else 0
        return page_count, raw_count

    def list_kbs(self, owner_account_id: str = "") -> list[KnowledgeBase]:
        kbs: dict[str, KnowledgeBase] = {}
        root = self._kb_root(owner_account_id)
        if root.exists():
            for sub in sorted(root.iterdir()):
                if sub.is_dir():
                    kb_id = sub.name
                    meta = self._read_kb_meta(sub)
                    summary = KBSummary()
                    summary_data = meta.get("summary")
                    if isinstance(summary_data, dict):
                        summary = KBSummary.from_dict(summary_data)
                    # 兜底：summary 未生成时，直接从文件系统统计
                    if summary.status == "empty" or summary.page_count == 0:
                        raw_dir = self._raw_dir(owner_account_id, kb_id)
                        page_count, raw_count = self._quick_count(sub, raw_dir)
                        if page_count > 0:
                            summary.page_count = page_count
                            summary.source_count = raw_count
                            summary.status = "ready"
                    kbs[kb_id] = KnowledgeBase(
                        id=kb_id,
                        name=meta.get("name") or kb_id,
                        created_at=float(meta.get("created_at", 0.0) or sub.stat().st_ctime),
                        updated_at=float(meta.get("updated_at", 0.0) or sub.stat().st_mtime),
                        summary=summary,
                        vault_path=str(sub.resolve()),
                    )
        # 旧版 wiki/ 作为 default KB 回退
        if _DEFAULT_KB_ID not in kbs:
            legacy = self._legacy_dir(owner_account_id)
            if legacy.exists():
                legacy_raw = self._legacy_raw_dir(owner_account_id)
                page_count, raw_count = self._quick_count(legacy, legacy_raw)
                kbs[_DEFAULT_KB_ID] = KnowledgeBase(
                    id=_DEFAULT_KB_ID,
                    name="默认知识库",
                    created_at=legacy.stat().st_ctime,
                    updated_at=legacy.stat().st_mtime,
                    summary=KBSummary(
                        page_count=page_count,
                        source_count=raw_count,
                        status="ready" if page_count > 0 else "empty",
                    ),
                    vault_path=str(legacy.resolve()),
                )
        return list(kbs.values())

    def create_kb(
        self,
        kb_id: str,
        name: str = "",
        owner_account_id: str = "",
    ) -> KnowledgeBase:
        kb_id = normalize_kb_id(kb_id)
        if kb_id == _DEFAULT_KB_ID:
            raise ValueError("default 知识库已存在，无需创建")
        base = self._kb_root(owner_account_id) / kb_id
        if base.exists():
            raise ValueError(f"知识库已存在: {kb_id}")
        base.mkdir(parents=True, exist_ok=False)
        now = time.time()
        kb = KnowledgeBase(
            id=kb_id,
            name=name or kb_id,
            created_at=now,
            updated_at=now,
            vault_path=str(base.resolve()),
        )
        self._write_kb_meta(base, kb)
        self.init_kb(owner_account_id, kb_id)
        return kb

    def delete_kb(self, kb_id: str, owner_account_id: str = "") -> bool:
        kb_id = normalize_kb_id(kb_id)
        if kb_id == _DEFAULT_KB_ID:
            raise ValueError("禁止删除 default 知识库")
        base = self._kb_root(owner_account_id) / kb_id
        if not base.exists():
            return False
        try:
            shutil.rmtree(base)
            self._search_indexes.pop(
                self._search_index_key(owner_account_id, kb_id),
                None,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("删除知识库失败 %s: %s", base, exc)
            return False

    def get_vault_path(
        self,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> str:
        return str(self._dir(owner_account_id, kb_id).resolve())

    def _kb_meta_path(self, base: Path) -> Path:
        return base / ".kb.json"

    def get_kb_summary(
        self,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> KBSummary:
        """直接读取 .kb.json 中的 summary 字段，避免遍历全部知识库。"""
        base = self._kb_root(owner_account_id) / normalize_kb_id(kb_id)
        meta = self._read_kb_meta(base)
        summary_data = meta.get("summary")
        if isinstance(summary_data, dict):
            return KBSummary.from_dict(summary_data)
        return KBSummary()

    def set_kb_summary(
        self,
        summary: KBSummary,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> None:
        """将 summary 写回 .kb.json，同时保留其它元数据字段。"""
        base = self._kb_root(owner_account_id) / normalize_kb_id(kb_id)
        meta = self._read_kb_meta(base)
        meta["summary"] = summary.to_dict()
        try:
            self._kb_meta_path(base).write_text(
                json.dumps(meta, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("写入知识库摘要元数据失败 %s: %s", base, exc)

    def get_home_intro(
        self,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> HomeIntro:
        """直接读取 .kb.json 中的 home_intro 字段。"""
        base = self._kb_root(owner_account_id) / normalize_kb_id(kb_id)
        meta = self._read_kb_meta(base)
        intro_data = meta.get("home_intro")
        if isinstance(intro_data, dict):
            return HomeIntro.from_dict(intro_data)
        return HomeIntro()

    def set_home_intro(
        self,
        intro: HomeIntro,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> None:
        """将 Home.md 导读写回 .kb.json，同时保留其它元数据字段。"""
        base = self._kb_root(owner_account_id) / normalize_kb_id(kb_id)
        meta = self._read_kb_meta(base)
        meta["home_intro"] = intro.to_dict()
        try:
            self._kb_meta_path(base).write_text(
                json.dumps(meta, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("写入知识库导读元数据失败 %s: %s", base, exc)

    def _read_kb_meta(self, base: Path) -> dict[str, Any]:
        path = self._kb_meta_path(base)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _write_kb_meta(self, base: Path, kb: KnowledgeBase) -> None:
        try:
            path = self._kb_meta_path(base)
            path.write_text(json.dumps(kb.to_dict(), ensure_ascii=False), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.warning("写入知识库元数据失败 %s: %s", base, exc)

    # ---- raw sources ----

    @staticmethod
    def _raw_source_path(raw_dir: Path, source_id: str, *, parsed: bool = False) -> Path:
        """把 source_id 收敛为 raw_dir 的直接子文件，阻止目录逃逸。"""
        value = str(source_id or "").strip()
        if (
            not value
            or value in {".", ".."}
            or "\x00" in value
            or Path(value).name != value
            or "/" in value
            or "\\" in value
        ):
            raise ValueError("source_id 必须是非空文件名片段，不能包含路径分隔符")
        suffix = ".parsed.md" if parsed else ".md"
        return raw_dir / f"{value}{suffix}"

    def save_raw(
        self,
        source: RawSource,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> RawSource:
        with self._lock(owner_account_id, kb_id):
            # 调用方常在 save_parsed_markdown 后继续保存手中的旧 RawSource 对象；
            # 不允许这个旧对象把刚计算出的内容 hash 覆盖为空。
            meta_path = self._source_meta_dir(owner_account_id, kb_id) / f"{source.id}.json"
            if not source.content_sha256 and meta_path.exists():
                try:
                    existing = RawSource.from_dict(
                        json.loads(meta_path.read_text(encoding="utf-8"))
                    )
                    source.content_sha256 = existing.content_sha256
                except Exception:  # noqa: BLE001
                    pass
            # 自动计算原始文件 hash
            if source.original_path and not source.original_sha256:
                original_path = Path(source.original_path)
                if original_path.exists():
                    try:
                        source.original_sha256 = compute_sha256(original_path.read_bytes())
                    except Exception as exc:  # noqa: BLE001
                        log.warning("计算 original_sha256 失败 %s: %s", original_path, exc)
            meta_path.write_text(
                json.dumps(source.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return source

    def load_raw(
        self,
        source_id: str,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> RawSource | None:
        meta_path = self._source_meta_dir(owner_account_id, kb_id) / f"{source_id}.json"
        if meta_path.exists():
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
                data["id"] = source_id
                return RawSource.from_dict(data)
            except Exception as exc:  # noqa: BLE001
                log.warning("RawSource JSON 解析失败 %s: %s", meta_path, exc)
        # 兼容旧版 raw/{source_id}.md frontmatter。
        raw_dir = self._raw_dir(owner_account_id, kb_id)
        legacy_path = self._raw_source_path(raw_dir, source_id)
        if not legacy_path.exists():
            return None
        return deserialize_raw(
            legacy_path.read_text(encoding="utf-8", errors="replace"),
            source_id,
        )

    def list_raws(
        self,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> list[RawSource]:
        results: list[RawSource] = []
        seen: set[str] = set()
        meta_dir = self._source_meta_dir(owner_account_id, kb_id)
        for path in sorted(meta_dir.glob("*.json")):
            source = self.load_raw(path.stem, owner_account_id, kb_id)
            if source:
                results.append(source)
                seen.add(source.id)
        # 兼容旧版平铺元数据。
        raw_dir = self._raw_dir(owner_account_id, kb_id)
        for path in sorted(raw_dir.glob("*.md")):
            if path.name.endswith(".parsed.md"):
                continue
            source = self.load_raw(source_id_from_filename(path), owner_account_id, kb_id)
            if source and source.id not in seen:
                results.append(source)
        return results

    def delete_raw(
        self,
        source_id: str,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> bool:
        with self._lock(owner_account_id, kb_id):
            source = self.load_raw(source_id, owner_account_id, kb_id)
            raw_dir = self._raw_dir(owner_account_id, kb_id)
            legacy_raw_path = self._raw_source_path(raw_dir, source_id)
            legacy_parsed_path = self._raw_source_path(raw_dir, source_id, parsed=True)
            meta_path = self._source_meta_dir(owner_account_id, kb_id) / f"{source_id}.json"
            source_files: list[Path] = []
            if source:
                for recorded in (source.original_path, source.parsed_path):
                    if recorded:
                        source_files.append(Path(recorded))
            if not meta_path.exists() and not legacy_raw_path.exists() and not legacy_parsed_path.exists():
                return False

            deleted_page_ids: list[str] = []
            for page in self._iter_pages(owner_account_id, kb_id):
                if source_id not in page.sources:
                    continue

                page_path = self._dir(owner_account_id, kb_id) / page.file_path

                # Source 摘要页：唯一身份基于 source_id。仅当页面唯一关联该来源
                # 才整页删除；legacy 多源合并页（旧版按标题合并的产物）只移除
                # 关联并置 stale，保护其他来源的内容入口，避免误删他人 source 页。
                if page.page_type == "source":
                    if page.sources == [source_id]:
                        try:
                            page_path.unlink(missing_ok=True)
                            deleted_page_ids.append(page.id)
                        except Exception as exc:  # noqa: BLE001
                            log.warning("删除 source %s 关联页面失败 %s: %s", source_id, page_path, exc)
                        continue
                    page.sources = [value for value in page.sources if value != source_id]
                    page.stale = True
                    page.updated_at = time.time()
                    try:
                        page_path.write_text(serialize_page(page), encoding="utf-8")
                        self._search_index(owner_account_id, kb_id).sync_page(page)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("删除 source %s 后更新合并来源页失败 %s: %s", source_id, page.id, exc)
                    continue

                # 聚合知识页（entity/topic/comparison/synthesis）。
                # 仅由该来源支撑 -> 整页删除。
                if len(page.sources) == 1:
                    try:
                        page_path.unlink(missing_ok=True)
                        deleted_page_ids.append(page.id)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("删除 source %s 关联页面失败 %s: %s", source_id, page_path, exc)
                    continue

                # 多来源聚合页：移除来源与证据，丢弃失去全部证据的 claim。
                # 若有 claim 被丢弃，依据剩余 claims 重编正文并置 stale，防止
                # 正文残留已无证据支撑的结论（"幽灵知识"）。无 claim 丢失时
                # 保留原 LLM 叙述，仅更新 frontmatter。
                page.sources = [value for value in page.sources if value != source_id]
                retained_claims = []
                for claim in page.claims:
                    had_evidence = bool(claim.evidence)
                    claim.evidence = [
                        evidence
                        for evidence in claim.evidence
                        if evidence.source_id != source_id
                    ]
                    if not had_evidence or claim.evidence:
                        retained_claims.append(claim)
                claims_dropped = len(page.claims) - len(retained_claims)
                page.claims = retained_claims
                if claims_dropped:
                    page.content = page.content_from_claims()
                    page.stale = True
                if len(page.sources) == 1 and page.confidence == "high":
                    page.confidence = "medium"
                try:
                    page.updated_at = time.time()
                    page_path.write_text(serialize_page(page), encoding="utf-8")
                    self._search_index(owner_account_id, kb_id).sync_page(page)
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "删除 source %s 后更新聚合页面失败 %s: %s",
                        source_id,
                        page.id,
                        exc,
                    )

            if deleted_page_ids:
                self._search_index(owner_account_id, kb_id).delete_pages(deleted_page_ids)

            try:
                meta_path.unlink(missing_ok=True)
                legacy_raw_path.unlink(missing_ok=True)
                legacy_parsed_path.unlink(missing_ok=True)
                for source_file in source_files:
                    if source_file.is_file() and raw_dir.resolve() in source_file.resolve().parents:
                        source_file.unlink(missing_ok=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("删除 raw source 文件失败 %s: %s", source_id, exc)
                return False
            return True

    def get_source_titles(
        self,
        source_ids: list[str],
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> dict[str, str]:
        """批量获取 source_id 对应的人类可读标题，缺失时回退到 source_id。"""
        result: dict[str, str] = {}
        for sid in source_ids:
            raw = self.load_raw(sid, owner_account_id, kb_id)
            result[sid] = raw.title if raw and raw.title else sid
        return result

    def save_parsed_markdown(
        self,
        source_id: str,
        content: str,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> str:
        """把内容保存为 raw source 的 parsed markdown，返回文件路径。"""
        raw = self.load_raw(source_id, owner_account_id, kb_id)
        source_kind = raw.source_kind if raw is not None else "note"
        source_dir = self._source_dir(source_kind, owner_account_id, kb_id)
        parsed_path = source_dir / f"{source_id}.md"
        parsed_path.write_text(content, encoding="utf-8")

        # 同步更新 raw source 的 content_sha256
        if raw is not None:
            raw.content_sha256 = compute_sha256(content)
            raw.parsed_path = str(parsed_path)
            if raw.parse_status != "failed":
                raw.parse_status = "parsed"
            self.save_raw(raw, owner_account_id, kb_id)

        return str(parsed_path)

    # ---- pages ----

    def save_page(
        self,
        page: WikiPage,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> WikiPage:
        with self._lock(owner_account_id, kb_id):
            if page.page_type not in _PAGE_TYPES:
                raise ValueError(f"不支持的 Wiki 页面类型: {page.page_type}")
            base = self._dir(owner_account_id, kb_id)
            if not page.file_path:
                file_path = page_file_path(base, page.page_type, page.title)
                page.file_path = str(file_path.relative_to(base))
            if not page.id:
                page.id = page_id(page.page_type, page.title)
            now = time.time()
            if not page.created_at:
                page.created_at = now
            page.updated_at = now
            path = base / page.file_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(serialize_page(page), encoding="utf-8")
            self._search_index(owner_account_id, kb_id).sync_page(page)
            return page

    def _read_page_head(self, path: Path, max_bytes: int = 8192) -> str:
        """读取页面文件头部，用于 brief 模式快速提取元信息。"""
        with path.open("rb") as f:
            raw = f.read(max_bytes)
        return raw.decode("utf-8", errors="replace")

    def _deserialize_page_brief_from_file(
        self,
        path: Path,
        rel: str,
    ) -> WikiPage | None:
        """从文件头部解析页面简要信息；frontmatter 不完整时回退完整读取。"""
        head = self._read_page_head(path)
        # 没有 frontmatter 或 frontmatter 被截断：回退完整读取
        if not head.startswith("---") or "---" not in head[3:]:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                return deserialize_page(text, rel)
            except Exception as exc:  # noqa: BLE001
                log.warning("读取 Wiki 页面失败 %s: %s", path, exc)
                return None
        return deserialize_page_brief(head, rel)

    def _iter_pages(
        self,
        owner_account_id: str = "",
        kb_id: str = "default",
        brief: bool = False,
    ):
        base = self._dir(owner_account_id, kb_id)
        seen_paths: set[str] = set()
        for sub in _PAGE_DIRS:
            # v2 页面位于 wiki/；根目录旧路径只作为兼容读取。
            current_dir = base / "wiki" / sub
            legacy_dir = base / sub
            for directory, recursive in (
                (current_dir, True),
                (legacy_dir, False),
            ):
                paths = directory.rglob("*.md") if recursive else directory.glob("*.md")
                for path in sorted(paths):
                    resolved = str(path.resolve())
                    if resolved in seen_paths:
                        continue
                    seen_paths.add(resolved)
                    try:
                        rel = str(path.relative_to(base))
                        if brief:
                            page = self._deserialize_page_brief_from_file(path, rel)
                        else:
                            text = path.read_text(encoding="utf-8", errors="replace")
                            page = deserialize_page(text, rel)
                        if page is not None:
                            yield page
                    except Exception as exc:  # noqa: BLE001
                        log.warning("读取 Wiki 页面失败 %s: %s", path, exc)

    def get(
        self,
        page_id_str: str,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> WikiPage | None:
        for page in self._iter_pages(owner_account_id, kb_id):
            if page.id == page_id_str:
                return page
        return None

    def get_by_title(
        self,
        title: str,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> WikiPage | None:
        for page in self._iter_pages(owner_account_id, kb_id):
            if page.title == title:
                return page
        return None

    def update(
        self,
        page: WikiPage,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> WikiPage | None:
        with self._lock(owner_account_id, kb_id):
            if page.page_type not in _PAGE_TYPES:
                raise ValueError(f"不支持的 Wiki 页面类型: {page.page_type}")
            existing = self.get(page.id, owner_account_id, kb_id)
            if existing is None:
                return None
            old_path = self._dir(owner_account_id, kb_id) / existing.file_path
            page.created_at = existing.created_at
            page.updated_at = time.time()
            if not page.file_path:
                page.file_path = existing.file_path
            path = self._dir(owner_account_id, kb_id) / page.file_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(serialize_page(page), encoding="utf-8")
            if old_path != path and old_path.is_file():
                old_path.unlink()
            self._search_index(owner_account_id, kb_id).sync_page(page)
            return page

    def delete(
        self,
        page_id_str: str,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> bool:
        with self._lock(owner_account_id, kb_id):
            page = self.get(page_id_str, owner_account_id, kb_id)
            if page is None:
                return False
            path = self._dir(owner_account_id, kb_id) / page.file_path
            try:
                path.unlink(missing_ok=True)
                self._search_index(owner_account_id, kb_id).delete_pages([page_id_str])
                return True
            except Exception as exc:  # noqa: BLE001
                log.warning("删除 Wiki 页面失败 %s: %s", path, exc)
                return False

    def list_all(
        self,
        owner_account_id: str = "",
        kb_id: str = "default",
        limit: int = 100,
        offset: int = 0,
        brief: bool = False,
    ) -> list[WikiPage]:
        pages = list(self._iter_pages(owner_account_id, kb_id, brief=brief))
        pages.sort(key=lambda p: p.updated_at, reverse=True)
        return pages[offset : offset + limit]

    def search(
        self,
        query: str,
        top_k: int = 5,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> list[WikiPage]:
        """搜索 Wiki 页面。

        搜索优先级：
        1. SQLite FTS5 全文检索（内置，零额外依赖，自动中文单字分词）
        2. 简单关键词打分回退
        """
        query = str(query or "").strip()
        if not query:
            return []

        pages = list(self._iter_pages(owner_account_id, kb_id))
        query_key = normalize_page_key(query)
        fts_ids = self._search_index(owner_account_id, kb_id).search(
            query,
            max(top_k * 3, top_k),
        )
        fts_rank = {page_id: index for index, page_id in enumerate(fts_ids)}

        # 默认只暴露当前版本：被取代的旧版本 Source 页不参与检索，
        # 避免旧结论借旧版本重新浮现。entity/topic 页的 claim 级陈旧
        # 不在此处细粒度剔除（留作后续）。
        superseded = self.superseded_source_ids(owner_account_id, kb_id)
        if superseded:
            pages = [
                page for page in pages
                if not (
                    page.page_type == "source"
                    and any(sid in superseded for sid in page.sources)
                )
            ]

        # 精确标题/alias、FTS 与关键词信号统一重排；Source 页面作为证据层，
        # 在相关度相当时排在规范知识页之后。
        terms = [t.lower() for t in re.split(r"\s+", query) if t]
        if not terms and not query_key:
            return []
        scored: list[tuple[float, WikiPage]] = []
        for page in pages:
            title_key = normalize_page_key(page.title)
            alias_keys = {
                normalize_page_key(alias)
                for alias in page.aliases
                if normalize_page_key(alias)
            }
            claim_parts: list[str] = []
            for claim in page.claims:
                claim_parts.append(claim.statement)
                claim_parts.extend(
                    evidence.excerpt
                    for evidence in claim.evidence
                    if evidence.excerpt
                )
            claim_text = " ".join(claim_parts)
            haystack = (
                f"{page.title} {' '.join(page.aliases)} "
                f"{' '.join(page.tags)} {page.content} {claim_text}"
            ).lower()
            relevance = 0.0
            if query_key and query_key == title_key:
                relevance += 1000
            elif query_key and query_key in alias_keys:
                relevance += 900
            elif query_key and query_key and query_key in title_key:
                relevance += 300
            if page.id in fts_rank:
                relevance += max(100, 500 - fts_rank[page.id] * 10)
            relevance += sum(
                30 if term in page.title.lower() else 10
                for term in terms
                if term in haystack
            )
            if relevance <= 0:
                continue
            score = relevance
            if page.page_type != "source":
                score += 20
            if page.confidence == "high":
                score += 8
            elif page.confidence == "low":
                score -= 5
            if page.contested:
                score -= 10
            scored.append((score, page))
        scored.sort(key=lambda x: (-x[0], -x[1].updated_at))
        return [p for _, p in scored[:top_k]]

    def search_index(
        self,
        query: str,
        top_k: int = 5,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> list[WikiPage]:
        """把 index.md 作为独立导航通道搜索，不向 Agent 注入完整索引正文。"""
        query = str(query or "").strip()
        if not query or top_k <= 0:
            return []
        index_path = self._dir(owner_account_id, kb_id) / "index.md"
        if not index_path.is_file():
            return []

        entries = _parse_index_entries(index_path.read_text(encoding="utf-8"))
        if not entries:
            return []
        pages = list(self._iter_pages(owner_account_id, kb_id))
        page_by_title = {
            normalize_page_key(page.title): page
            for page in pages
            if normalize_page_key(page.title)
        }
        query_key = normalize_page_key(query)
        terms = query_terms(query)
        scored: list[tuple[float, int, WikiPage]] = []
        seen: set[str] = set()
        for position, entry in enumerate(entries):
            page = page_by_title.get(normalize_page_key(entry["title"]))
            if page is None or page.id in seen:
                continue
            title_text = entry["title"].casefold()
            summary_text = entry["summary"].casefold()
            entry_key = normalize_page_key(f"{entry['title']} {entry['summary']}")
            score = 0.0
            title_key = normalize_page_key(entry["title"])
            if query_key and query_key == title_key:
                score += 1000
            elif query_key and query_key in title_key:
                score += 300
            elif query_key and query_key in entry_key:
                score += 180
            for term in terms:
                if term in title_text:
                    score += 45
                elif term in summary_text:
                    score += 15
            if score <= 0:
                continue
            if page.page_type != "source":
                score += 10
            if page.confidence == "high":
                score += 3
            elif page.confidence == "low":
                score -= 2
            if page.contested:
                score -= 4
            scored.append((score, position, page))
            seen.add(page.id)

        scored.sort(key=lambda item: (-item[0], item[1], -item[2].updated_at))
        return [page for _, _, page in scored[:top_k]]

    def get_graph(
        self,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> WikiGraph:
        pages = list(self._iter_pages(owner_account_id, kb_id))
        nodes = [
            {
                "id": p.id,
                "title": p.title,
                "type": p.page_type,
            }
            for p in pages
        ]
        title_to_id: dict[str, str] = {}
        for page in pages:
            for value in [page.title, *page.aliases]:
                title_to_id[normalize_page_key(value)] = page.id
        # 预加载所有 RawSource，用于 source 节点显示标题
        raw_by_id = {raw.id: raw for raw in self.list_raws(owner_account_id, kb_id)}
        # source 页面本身就是该 raw source 的节点：建立 raw_id -> source 页面 id 的映射，
        # 让 source_of 边直接指向 source 页面，避免同一来源在图中出现两个节点。
        raw_to_page_id: dict[str, str] = {}
        for p in pages:
            if p.page_type == "source":
                for raw_id in p.sources:
                    raw_to_page_id.setdefault(raw_id, p.id)
        edges: list[dict[str, Any]] = []
        seen_edges: set[tuple[str, str, str]] = set()

        def _add_edge(source: str, target: str, relation: str) -> None:
            key = (source, target, relation)
            if key not in seen_edges:
                seen_edges.add(key)
                edges.append({"source": source, "target": target, "relation": relation})

        for page in pages:
            for typed_relation in page.relations:
                target_id = title_to_id.get(normalize_page_key(typed_relation.target))
                if target_id and target_id != page.id:
                    _add_edge(page.id, target_id, typed_relation.relation)
            for related in page.related:
                target_id = title_to_id.get(normalize_page_key(related))
                if target_id:
                    _add_edge(page.id, target_id, "related")
            for src in page.sources:
                target_id = raw_to_page_id.get(src, f"source:{src}")
                if target_id == page.id:
                    continue  # source 页面指向自身的边无意义，跳过
                _add_edge(page.id, target_id, "source_of")
            # 扫描正文 [[...]] 双向链接
            for link in re.findall(r"\[\[([^\]]+)\]\]", page.content):
                target_id = title_to_id.get(normalize_page_key(link))
                if target_id and target_id != page.id:
                    _add_edge(page.id, target_id, "mentions")
        # 添加 source 节点（按 source:{id} 去重），标题使用 RawSource.title
        existing_ids = {n["id"] for n in nodes}
        for edge in edges:
            if edge["target"].startswith("source:"):
                if edge["target"] not in existing_ids:
                    existing_ids.add(edge["target"])
                    src_id = edge["target"].split(":", 1)[1]
                    raw = raw_by_id.get(src_id)
                    src_title = raw.title if raw and raw.title else src_id
                    nodes.append({"id": edge["target"], "title": src_title, "type": "source"})
        return WikiGraph(nodes=nodes, edges=edges)

    def get_neighbors(
        self,
        page_id_str: str,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> list[WikiPage]:
        """返回与指定页面关联的邻居，按关系远近排序。

        使用页面已有的 related 列表、content 中的 [[wikilinks]] 以及 sources
        来构建邻居集合，零额外基础设施。
        """
        page = self.get(page_id_str, owner_account_id, kb_id)
        if page is None:
            return []

        pages = list(self._iter_pages(owner_account_id, kb_id))
        title_to_page: dict[str, WikiPage] = {}
        for candidate in pages:
            for value in [candidate.title, *candidate.aliases]:
                title_to_page[normalize_page_key(value)] = candidate
        neighbor_titles: set[str] = set()

        # 显式 related 与有类型关系
        for t in page.related:
            neighbor_titles.add(t.strip())
        for relation in page.relations:
            neighbor_titles.add(relation.target.strip())

        # 正文 [[wikilinks]]
        for link in re.findall(r"\[\[([^\]]+)\]\]", page.content):
            stripped = link.strip()
            if stripped:
                neighbor_titles.add(stripped)

        # 收集邻居页面（排除自身）
        neighbors: list[WikiPage] = []
        seen: set[str] = {page.id}
        for title in neighbor_titles:
            neighbor = title_to_page.get(normalize_page_key(title))
            if neighbor and neighbor.id not in seen:
                neighbors.append(neighbor)
                seen.add(neighbor.id)

        # sources → source 页面
        for src_id in page.sources:
            src_page = title_to_page.get(normalize_page_key(src_id))
            if src_page and src_page.id not in seen:
                neighbors.append(src_page)
                seen.add(src_page.id)

        # 反向 related / wikilink 与共同来源。这样从 Source Page 也能回到由它
        # 编译出的规范知识页，查询阶段无需维护第二套反向索引。
        page_sources = set(page.sources)
        for candidate in pages:
            if candidate.id in seen:
                continue
            links = {
                link.strip()
                for link in re.findall(r"\[\[([^\]]+)\]\]", candidate.content)
            }
            has_inbound = (
                page.title in candidate.related
                or any(
                    normalize_page_key(relation.target) == normalize_page_key(page.title)
                    for relation in candidate.relations
                )
                or page.title in links
                or bool(page_sources & set(candidate.sources))
            )
            if has_inbound:
                neighbors.append(candidate)
                seen.add(candidate.id)

        # 按关系亲密度排序：related > mentions > source_of
        def _rank(p: WikiPage) -> int:
            score = 0
            if p.title in page.related:
                score += 100
            if any(
                normalize_page_key(relation.target) == normalize_page_key(p.title)
                for relation in page.relations
            ):
                score += 120
            if re.findall(r"\[\[([^\]]+)\]\]", page.content):
                for link in re.findall(r"\[\[([^\]]+)\]\]", page.content):
                    if link.strip() == p.title:
                        score += 50
                        break
            if p.id in page.sources or p.title in page.sources:
                score += 10
            if set(page.sources) & set(p.sources):
                score += 10
            return -score

        neighbors.sort(key=_rank)
        return neighbors

    def lint(
        self,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> list[LintIssue]:
        """程序化 Lint：断链、孤立页面、格式违规、时效性标记。"""
        pages = list(self._iter_pages(owner_account_id, kb_id))
        title_to_id = {p.title: p.id for p in pages}
        normalized_title_keys = {
            normalize_page_key(value)
            for page in pages
            for value in [page.title, *page.aliases]
            if normalize_page_key(value)
        }
        raw_ids = {
            source.id for source in self.list_raws(owner_account_id, kb_id)
        }
        issues: list[LintIssue] = []

        # 时效性用语正则（中/英）
        _OUTDATED_PATTERNS = re.compile(
            r"最新版|目前最新|currently v|latest v|just released|剛出|剛推出|截至 \d{4}",
            re.IGNORECASE,
        )

        # 预先收集所有被引用的标题（用于断链和孤立检测，O(n) 替代 O(n²)）
        all_linked: set[str] = set()
        for p in pages:
            for link in re.findall(r"\[\[([^\]]+)\]\]", p.content):
                all_linked.add(link.strip())
            for related_title in p.related:
                all_linked.add(related_title)
            for relation in p.relations:
                all_linked.add(relation.target)

        for page in pages:
            # 1. 断链
            for link in re.findall(r"\[\[([^\]]+)\]\]", page.content):
                target = link.strip()
                if target not in title_to_id:
                    issues.append(
                        LintIssue(
                            kind="broken_link",
                            page_id=page.id,
                            message=f"断链: [[{target}]]",
                            details={"target": target},
                        )
                    )

            # 2. 格式违规：首行必须是 # title
            lines = page.content.splitlines()
            first_line = lines[0].strip() if lines else ""
            if not first_line.startswith("# "):
                issues.append(
                    LintIssue(
                        kind="format_violation",
                        page_id=page.id,
                        message=f"格式违规: 首行应为 '# 标题'，当前为 '{first_line[:40]}'",
                        details={"first_line": first_line},
                    )
                )

            # 3. 时效性标记
            for match in _OUTDATED_PATTERNS.finditer(page.content):
                issues.append(
                    LintIssue(
                        kind="outdated_marker",
                        page_id=page.id,
                        message=f"时效性标记: '{match.group()}'",
                        details={"marker": match.group(), "position": match.start()},
                    )
                )

            # 4. 孤立页面：无出链且无入链（source 页面除外）
            has_out = (
                bool(page.related)
                or bool(page.relations)
                or bool(re.findall(r"\[\[([^\]]+)\]\]", page.content))
            )
            has_in = page.title in all_linked
            if not has_out and not has_in and page.page_type != "source":
                issues.append(
                    LintIssue(
                        kind="orphan",
                        page_id=page.id,
                        message=f"孤立页面: {page.title}",
                    )
                )

            # 5. 已声明的来源必须真实存在
            for source_id in page.sources:
                if source_id not in raw_ids:
                    issues.append(
                        LintIssue(
                            kind="missing_source",
                            page_id=page.id,
                            message=f"来源缺失: {source_id}",
                            details={"source_id": source_id},
                        )
                    )

            for relation in page.relations:
                if normalize_page_key(relation.target) not in normalized_title_keys:
                    issues.append(
                        LintIssue(
                            kind="broken_link",
                            page_id=page.id,
                            message=f"关系目标不存在: {relation.target}",
                            details={
                                "target": relation.target,
                                "relation": relation.relation,
                            },
                        )
                    )

            # 6. 质量信号进入明确复核队列
            if page.confidence == "low":
                issues.append(
                    LintIssue(
                        kind="low_confidence",
                        page_id=page.id,
                        message=f"低置信度页面: {page.title}",
                    )
                )
            if page.contested or page.contradictions:
                issues.append(
                    LintIssue(
                        kind="contested",
                        page_id=page.id,
                        message=f"存在未解决争议: {page.title}",
                        details={"contradictions": list(page.contradictions)},
                    )
                )
            if page.stale:
                issues.append(
                    LintIssue(
                        kind="stale",
                        page_id=page.id,
                        message=f"页面证据已变化待整理: {page.title}",
                    )
                )

        # 7. 标题和 aliases 不能把多个页面映射到同一个规范键
        owners_by_key: dict[str, list[WikiPage]] = {}
        for page in pages:
            for value in [page.title, *page.aliases]:
                key = normalize_page_key(value)
                if key:
                    owners_by_key.setdefault(key, []).append(page)
        for key, owners in owners_by_key.items():
            unique = {page.id: page for page in owners}
            if len(unique) <= 1:
                continue
            ordered = sorted(unique.values(), key=lambda page: page.title)
            issues.append(
                LintIssue(
                    kind="alias_conflict",
                    page_id=ordered[0].id,
                    message=f"标题/别名冲突: {', '.join(page.title for page in ordered)}",
                    details={
                        "normalized_key": key,
                        "page_ids": [page.id for page in ordered],
                    },
                )
            )

        # 8. index 必须覆盖全部页面且页面总数一致
        index_path = self._dir(owner_account_id, kb_id) / "index.md"
        index_text = (
            index_path.read_text(encoding="utf-8")
            if index_path.exists()
            else ""
        )
        missing_titles = [
            page.title for page in pages if f"[[{page.title}]]" not in index_text
        ]
        declared_total = _parse_index(index_text).get("total_from_index", 0)
        if missing_titles or declared_total != len(pages):
            issues.append(
                LintIssue(
                    kind="index_drift",
                    page_id="",
                    message=(
                        f"index 与页面状态不一致: 缺少 {len(missing_titles)} 页，"
                        f"声明 {declared_total} 页，实际 {len(pages)} 页"
                    ),
                    details={
                        "missing_titles": missing_titles,
                        "declared_total": declared_total,
                        "actual_total": len(pages),
                    },
                )
            )

        return issues

    def append_log(
        self,
        messages: list[str],
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> None:
        """追加 Wiki 操作日志到 log.md。"""
        if not messages:
            return
        base = self._dir(owner_account_id, kb_id)
        append_wiki_log(base, messages)

    def update_home(
        self,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> None:
        base = self._dir(owner_account_id, kb_id)
        pages = list(self._iter_pages(owner_account_id, kb_id))
        raws = self.list_raws(owner_account_id, kb_id)
        kb_name = kb_id
        meta = self._read_kb_meta(base)
        if meta.get("name"):
            kb_name = str(meta["name"])
        intro_data = meta.get("home_intro")
        intro = (
            HomeIntro.from_dict(intro_data)
            if isinstance(intro_data, dict)
            else HomeIntro()
        )
        (base / "Home.md").write_text(
            _build_home_markdown(kb_name, pages, raws, intro),
            encoding="utf-8",
        )

    def layout_migration_preview(
        self,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> dict[str, Any]:
        base = self._dir(owner_account_id, kb_id)
        legacy_pages = [
            path
            for sub in _PAGE_DIRS
            for path in (base / sub).glob("*.md")
        ]
        raw_dir = self._raw_dir(owner_account_id, kb_id)
        legacy_sources = [
            path for path in raw_dir.glob("*.md")
            if not path.name.endswith(".parsed.md")
        ]
        legacy_internal = [
            path for path in (
                base / "SCHEMA.md",
                base / ".index",
            )
            if path.exists()
        ]
        flat_source_pages = list((base / "wiki" / "sources").glob("*.md"))
        return {
            "required": bool(
                legacy_pages
                or legacy_sources
                or legacy_internal
                or flat_source_pages
            ),
            "pages": len(legacy_pages),
            "source_pages_to_classify": len(flat_source_pages),
            "sources": len(legacy_sources),
            "internal_paths": [str(path.relative_to(base)) for path in legacy_internal],
            "vault_path": str(base.resolve()),
        }

    def migrate_layout(
        self,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> dict[str, Any]:
        """幂等迁移旧平铺布局；只移动明确识别的 Wiki 文件。"""
        base = self._dir(owner_account_id, kb_id)
        preview = self.layout_migration_preview(owner_account_id, kb_id)
        collisions = [
            str((base / "wiki" / sub / path.name).relative_to(base))
            for sub in _PAGE_DIRS
            for path in (base / sub).glob("*.md")
            if (base / "wiki" / sub / path.name).exists()
        ]
        flat_source_pages = list((base / "wiki" / "sources").glob("*.md"))
        source_page_targets: list[tuple[Path, Path, WikiPage]] = []
        for path in flat_source_pages:
            page = deserialize_page(
                path.read_text(encoding="utf-8", errors="replace"),
                str(path.relative_to(base)),
            )
            raw = (
                self.load_raw(page.sources[0], owner_account_id, kb_id)
                if page.sources
                else None
            )
            source_dir = SOURCE_DIRS.get(
                str(raw.source_kind if raw else "asset"),
                "assets",
            )
            target = base / "wiki" / "sources" / source_dir / path.name
            if target.exists():
                collisions.append(str(target.relative_to(base)))
            source_page_targets.append((path, target, page))
        if collisions:
            raise FileExistsError(
                "迁移目标存在同名页面，尚未移动任何旧页面: " + ", ".join(collisions)
            )
        self.init_kb(owner_account_id, kb_id)
        moved_pages = 0
        moved_sources = 0
        classified_source_pages = 0

        for sub in _PAGE_DIRS:
            legacy_dir = base / sub
            target_dir = base / "wiki" / sub
            if not legacy_dir.exists():
                continue
            for path in sorted(legacy_dir.glob("*.md")):
                target = target_dir / path.name
                path.replace(target)
                moved_pages += 1
            try:
                legacy_dir.rmdir()
            except OSError:
                pass

        for path, target, page in source_page_targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            page.file_path = str(target.relative_to(base))
            target.write_text(serialize_page(page), encoding="utf-8")
            path.unlink()
            self._search_index(owner_account_id, kb_id).sync_page(page)
            classified_source_pages += 1

        raws = self.list_raws(owner_account_id, kb_id)
        raw_dir = self._raw_dir(owner_account_id, kb_id)
        for raw in raws:
            old_meta = self._raw_source_path(raw_dir, raw.id)
            old_parsed = self._raw_source_path(raw_dir, raw.id, parsed=True)
            target_dir = self._source_dir(raw.source_kind, owner_account_id, kb_id)
            if old_parsed.exists():
                target_parsed = target_dir / f"{raw.id}.md"
                if not target_parsed.exists():
                    old_parsed.replace(target_parsed)
                raw.parsed_path = str(target_parsed)
            if raw.original_path:
                original = Path(raw.original_path)
                if original.is_file() and original.parent == raw_dir:
                    target_original = target_dir / original.name
                    if not target_original.exists():
                        original.replace(target_original)
                    raw.original_path = str(target_original)
            self.save_raw(raw, owner_account_id, kb_id)
            if old_meta.exists():
                old_meta.unlink()
                moved_sources += 1

        legacy_schema = base / "SCHEMA.md"
        schema = base / ".wiki-schema.md"
        if legacy_schema.exists():
            # 旧 Schema 可能包含用户自定义规则，优先保留其内容。
            schema.write_text(legacy_schema.read_text(encoding="utf-8"), encoding="utf-8")
            legacy_schema.unlink()
        legacy_index = base / ".index"
        target_index = base / ".crew" / "index"
        if legacy_index.exists():
            for path in legacy_index.iterdir():
                target = target_index / path.name
                if not target.exists():
                    path.replace(target)
            try:
                legacy_index.rmdir()
            except OSError:
                pass
        self.update_home(owner_account_id, kb_id)
        return {
            "migrated": bool(
                moved_pages
                or moved_sources
                or classified_source_pages
                or preview["internal_paths"]
            ),
            "pages": moved_pages,
            "source_pages_classified": classified_source_pages,
            "sources": moved_sources,
            "vault_path": str(base.resolve()),
        }

    def check_source_duplicate(
        self,
        source: RawSource,
        owner_account_id: str = "",
        kb_id: str = "default",
        *,
        _raws: list[RawSource] | None = None,
    ) -> RawSource | None:
        """检查同 KB 下是否已有相同 content_sha256 的 source。"""
        if not source.content_sha256:
            return None
        raws = _raws if _raws is not None else self.list_raws(owner_account_id, kb_id)
        for raw in raws:
            if raw.id != source.id and raw.content_sha256 == source.content_sha256:
                return raw
        return None

    def check_source_drift(
        self,
        source: RawSource,
        owner_account_id: str = "",
        kb_id: str = "default",
        *,
        _raws: list[RawSource] | None = None,
    ) -> list[RawSource]:
        """检查同 URL 的 source 是否发生过内容漂移。

        返回同 URL 但 content_sha256 不同的历史 source 列表（按创建时间倒序）。
        """
        if not source.source_url:
            return []
        raws = _raws if _raws is not None else self.list_raws(owner_account_id, kb_id)
        drifted: list[RawSource] = []
        for raw in raws:
            if raw.id != source.id and raw.source_url == source.source_url:
                if raw.content_sha256 and source.content_sha256 and raw.content_sha256 != source.content_sha256:
                    drifted.append(raw)
        drifted.sort(key=lambda r: r.created_at, reverse=True)
        return drifted

    def superseded_source_ids(
        self,
        owner_account_id: str = "",
        kb_id: str = "default",
        *,
        _raws: list[RawSource] | None = None,
    ) -> set[str]:
        """返回已被新版本取代的 source_id 集合。

        默认检索与入库只认当前版本，被取代的旧版本不参与搜索/综合/批处理，
        防止旧结论借旧版本 Source 页重新浮现。去重/漂移检查仍看全量历史。
        """
        raws = _raws if _raws is not None else self.list_raws(owner_account_id, kb_id)
        return {raw.id for raw in raws if raw.superseded_by}

    def orient(
        self,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> WikiOrientation:
        """返回当前 KB 的全景信息，供 Agent 在操作前 orientation。"""
        self.init_kb(owner_account_id, kb_id)
        base = self._dir(owner_account_id, kb_id)
        pages = list(self._iter_pages(owner_account_id, kb_id))
        raws = self.list_raws(owner_account_id, kb_id)

        # v2 .wiki-schema.md；兼容旧 SCHEMA.md。
        schema_path = base / ".wiki-schema.md"
        if not schema_path.exists():
            schema_path = base / "SCHEMA.md"
        schema = _parse_schema(schema_path.read_text(encoding="utf-8")) if schema_path.exists() else {}

        # index.md
        index_path = base / "index.md"
        index_summary = _parse_index(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}

        # log.md
        log_path = base / "log.md"
        recent_log = _parse_recent_log(log_path.read_text(encoding="utf-8"), limit=20) if log_path.exists() else []

        # stats
        stats = _compute_page_stats(pages, raws)

        # candidate index for page matching
        candidate_index = _build_candidate_index(pages)

        # kb name
        kb_name = kb_id
        for kb in self.list_kbs(owner_account_id):
            if kb.id == kb_id:
                kb_name = kb.name or kb_id
                break

        return WikiOrientation(
            kb_id=kb_id,
            kb_name=kb_name,
            schema=schema,
            index={
                "page_count": len(pages),
                "raw_source_count": len(raws),
                "last_updated": max((p.updated_at for p in pages), default=0.0),
                **index_summary,
                "pages": [
                    {
                        "page_id": p.id,
                        "title": p.title,
                        "page_type": p.page_type,
                        "aliases": p.aliases,
                        "tags": p.tags,
                        "updated_at": p.updated_at,
                    }
                    for p in sorted(pages, key=lambda p: p.updated_at, reverse=True)[:50]
                ],
            },
            recent_log=recent_log,
            stats=stats,
            candidate_index=candidate_index,
            vault_path=str(base.resolve()),
            generated_at=time.time(),
        )


# Home.md 知识地图最多展示的页面数
_HOME_MAP_MAX_PAGES = 6
# Home.md 最近更新最多展示的页面数
_HOME_RECENT_MAX_PAGES = 8
# 知识地图单个页面介绍的最大长度
_HOME_EXCERPT_MAX_CHARS = 300

_EMPTY_HOME_MD = """# 知识库概览

> {kb_name}

这个知识库还没有内容。你可以：

- 上传文件（PDF、Markdown、文本等），AI 会自动解析并整理
- 粘贴文本或提供链接，直接收入知识库

素材整理成互相链接的知识页面后，这里会自动生成知识库导读和知识地图。
"""

_HOME_INTRO_PLACEHOLDER = "导读整理中：知识库内容更新后，这里会自动生成整体介绍。"


def _build_home_markdown(
    kb_name: str,
    pages: list[WikiPage],
    raws: list[RawSource],
    intro: HomeIntro | None = None,
) -> str:
    if not pages and not raws:
        return _EMPTY_HOME_MD.format(kb_name=kb_name)
    by_type: dict[str, int] = {}
    for page in pages:
        by_type[page.page_type] = by_type.get(page.page_type, 0) + 1
    intro_text = intro.text.strip() if intro is not None else ""
    lines = [
        "# 知识库概览",
        "",
        f"> {kb_name} · 共 {len(pages)} 个页面 · {len(raws)} 份素材",
        "",
        "## 关于这个知识库",
        "",
        intro_text or _HOME_INTRO_PLACEHOLDER,
        "",
        "## 知识地图",
        "",
    ]
    map_pages = _home_map_pages(pages)
    if map_pages:
        for page in map_pages:
            lines.extend(_home_map_entry(page))
    else:
        lines.extend(["暂无关键词或话题页面。", ""])
    lines.extend(
        [
            "## 快速导航",
            "",
            "| 类型 | 数量 |",
            "| --- | --- |",
            f"| 原始素材 | {len(raws)} |",
            f"| 关键词 | {by_type.get('entity', 0)} |",
            f"| 话题 | {by_type.get('topic', 0)} |",
            f"| 来源摘要 | {by_type.get('source', 0)} |",
            f"| 对比分析 | {by_type.get('comparison', 0)} |",
            f"| 综合报告 | {by_type.get('synthesis', 0)} |",
            "",
            "> 完整目录见 `index.md`（知识导航）。",
            "",
            "## 最近更新",
            "",
        ]
    )
    recent = sorted(pages, key=lambda page: page.updated_at, reverse=True)[
        :_HOME_RECENT_MAX_PAGES
    ]
    if recent:
        lines.extend(
            f"- [[{page.title}]] · {_PAGE_TYPE_LABELS.get(page.page_type, page.page_type)} · "
            f"{time.strftime('%Y-%m-%d', time.localtime(page.updated_at))}"
            for page in recent
        )
    else:
        lines.append("- 暂无页面")
    lines.append("")
    return "\n".join(lines)


def _home_map_pages(pages: list[WikiPage]) -> list[WikiPage]:
    """知识地图候选：话题页优先、关键词页补足，按来源数/关联数/更新时间排序。"""

    def rank(page: WikiPage) -> tuple[int, int, float]:
        return (len(page.sources), len(page.related), page.updated_at or 0.0)

    topics = sorted(
        (page for page in pages if page.page_type == "topic"),
        key=rank,
        reverse=True,
    )
    entities = sorted(
        (page for page in pages if page.page_type == "entity"),
        key=rank,
        reverse=True,
    )
    return (topics + entities)[:_HOME_MAP_MAX_PAGES]


def _home_map_entry(page: WikiPage) -> list[str]:
    date = (
        time.strftime("%Y-%m-%d", time.localtime(page.updated_at))
        if page.updated_at
        else "-"
    )
    return [
        f"### [[{page.title}]]",
        "",
        f"> {len(page.sources)} 个来源 · 更新于 {date}",
        "",
        _page_home_excerpt(page.content),
        "",
    ]


def _page_home_excerpt(content: str, max_len: int = _HOME_EXCERPT_MAX_CHARS) -> str:
    """取正文开头 1-2 个自然段作为知识地图介绍，按句子边界截断。"""
    parts: list[str] = []
    for line in str(content or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            if parts:
                break
            continue
        if not stripped or stripped == "---":
            continue
        plain = re.sub(r"\[\[([^\]]+)\]\]", r"\1", stripped)
        plain = re.sub(r"\s+", " ", plain).strip(" >-*").strip()
        if not plain:
            continue
        parts.append(plain)
        if sum(len(part) for part in parts) >= max_len:
            break
    text = "".join(parts)
    if not text:
        return "暂无介绍。"
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    boundary = max(cut.rfind(mark) for mark in ("。", "！", "？", "；"))
    if boundary >= max_len // 2:
        return cut[: boundary + 1]
    return cut.rstrip() + "…"


def _is_legacy_generated_empty_home(text: str) -> bool:
    """只识别旧版系统生成的空库首页，避免覆盖用户自定义 Home.md。"""
    if text.strip() == "# 知识库概览\n\n暂无页面。":
        return True
    required = (
        "Crew 持续把原始素材编译为",
        "- 原始素材：0",
        "- 实体：0",
        "- 主题：0",
        "- 来源摘要：0",
        "- 对比分析：0",
        "- 综合报告：0",
        "- 暂无页面",
    )
    return all(marker in text for marker in required)


_DEFAULT_SCHEMA_MD = """# 知识库维护规则

## 页面类型
- `entity`：关键词页面，包括人、组织、工具、项目、系统、概念、方法、原则和机制
- `topic`：话题综合页面
- `source`：源文件摘要
- `comparison`：多对象、多方案或多观点对比
- `synthesis`：至少两个独立来源形成的跨来源综合

## Vault 目录
- 原始素材按类型保存到 `raw/articles|pdfs|words|excels|ppts|notes|sessions|images|videos|assets`
- 编译知识保存到 `wiki/entities|topics|comparisons|synthesis`
- 来源摘要按素材类型保存到 `wiki/sources/articles|pdfs|words|excels|ppts|notes|sessions|images|videos|assets`
- 系统内部状态保存到 `.crew/`

## 编译规则
- 每个 source 必须生成一个 source 摘要
- 长 source 整篇最多生成 5 个关键词和 3 个话题；短 source 最多 3 个关键词且不生成话题
- 创建页面前必须按规范标题、aliases 和页面类型匹配已有知识
- 关键结论写入 claims，并以 evidence.source_id 回溯 Raw Source
- 页面级 confidence 取所有主张中的保守值
- 不兼容结论必须标记 contested 和 contradictions，禁止静默覆盖
- 正文中首次出现的关键词应使用 `[[名称]]` 链接
- 检测到矛盾时，在 log.md 中记录并标记待审核

## 质量字段
- `claims`：页面中的可追溯知识主张
- `evidence`：主张对应的 source、原文位置和短摘录
- `confidence`：high / medium / low
- `contested`：是否存在未解决争议
- `contradictions`：冲突结论或页面列表

## 命名规则
- 关键词/话题页面文件名保留中文
- 重名时追加序号
"""


# --------------------------------------------------------------------------- #
# Orientation helpers
# --------------------------------------------------------------------------- #


def _parse_schema(text: str) -> dict[str, Any]:
    """简单解析 SCHEMA.md，返回结构化描述。"""
    lines = text.splitlines()
    rules: list[str] = []
    in_rules = False
    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith("## 编译规则") or stripped.lower().startswith("## 规则"):
            in_rules = True
            continue
        if in_rules:
            if stripped.startswith("## "):
                break
            if stripped.startswith("-"):
                rules.append(stripped.lstrip("-").strip())
    return {
        "raw": text,
        "page_types": ["entity", "topic", "source", "comparison", "synthesis"],
        "rules": rules,
    }


def _parse_index(text: str) -> dict[str, Any]:
    """从 index.md 提取页面统计和分支信息。"""
    lines = text.splitlines()
    total = 0
    for line in lines:
        if line.strip().startswith("页面总数:"):
            try:
                total = int(line.split(":", 1)[1].strip())
            except (ValueError, IndexError):
                pass
            break
    return {"total_from_index": total}


_INDEX_ENTRY_RE = re.compile(
    r"^\s*-\s+\[\[([^\]|]+)(?:\|[^\]]+)?\]\]\s*(?:[—-]\s*)?(.*)$"
)


def _parse_index_entries(text: str) -> list[dict[str, str]]:
    """解析 index 导航条目；只提取页面标题、摘要和所属分类。"""
    entries: list[dict[str, str]] = []
    section = ""
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            section = stripped[3:].strip()
            continue
        match = _INDEX_ENTRY_RE.match(line)
        if match is None:
            continue
        title = match.group(1).strip()
        summary = match.group(2).strip()
        if title:
            entries.append({
                "title": title,
                "summary": summary,
                "section": section,
            })
    return entries


def _parse_recent_log(text: str, limit: int = 20) -> list[dict[str, Any]]:
    """解析条目化 log.md，返回最近 N 条日志。"""
    entries: list[dict[str, Any]] = []
    current_time = ""
    current_messages: list[str] = []

    def _flush() -> None:
        nonlocal current_time, current_messages
        if current_time and current_messages:
            entries.append({
                "time": current_time,
                "messages": list(current_messages),
            })
        current_time = ""
        current_messages = []

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped == "# Wiki 更新日志":
            continue
        if stripped.startswith("## "):
            _flush()
            current_time = stripped[3:].strip()
            continue
        if stripped.startswith("-"):
            current_messages.append(stripped.lstrip("-").strip())

    _flush()
    # append_wiki_log 把新条目 prepend 到标题后，所以文件顺序即时间倒序
    return entries[:limit]


def _compute_page_stats(pages: list[WikiPage], raws: list[RawSource]) -> dict[str, Any]:
    """统计页面类型分布、标签分布、最近更新。"""
    by_type: dict[str, int] = {}
    by_tag: dict[str, int] = {}
    for p in pages:
        by_type[p.page_type] = by_type.get(p.page_type, 0) + 1
        for tag in p.tags:
            by_tag[tag] = by_tag.get(tag, 0) + 1

    sorted_pages = sorted(pages, key=lambda p: p.updated_at, reverse=True)
    return {
        "by_type": by_type,
        "by_tag": dict(sorted(by_tag.items(), key=lambda x: -x[1])[:20]),
        "recent_pages": [
            {"page_id": p.id, "title": p.title, "page_type": p.page_type, "updated_at": p.updated_at}
            for p in sorted_pages[:10]
        ],
        "raw_source_count": len(raws),
    }


def _build_candidate_index(pages: list[WikiPage]) -> dict[str, Any]:
    """构建用于页面匹配的候选索引。"""
    title_to_id: dict[str, str] = {}
    alias_to_id: dict[str, str] = {}
    tag_to_pages: dict[str, list[str]] = {}
    for p in pages:
        title_to_id[p.title] = p.id
        for alias in p.aliases:
            alias_to_id[alias] = p.id
        for tag in p.tags:
            tag_to_pages.setdefault(tag, []).append(p.id)
    return {
        "title_to_id": title_to_id,
        "alias_to_id": alias_to_id,
        "tag_to_pages": tag_to_pages,
    }


def append_wiki_log(base: Path, messages: list[str]) -> None:
    """向 log.md 追加一条新的日志条目。"""
    log_path = base / "log.md"
    if not log_path.exists():
        log_path.write_text("# Wiki 更新日志\n\n", encoding="utf-8")

    timestamp = time.strftime("%Y-%m-%d %H:%M")
    entry_lines = [f"## {timestamp}\n", ""]
    for msg in messages:
        entry_lines.append(f"- {msg}\n")
    entry_lines.append("")

    text = log_path.read_text(encoding="utf-8")
    # 插入到标题后的第一个空行位置
    lines = text.splitlines(keepends=True)
    insert_at = 0
    for i, line in enumerate(lines):
        if line.strip() == "# Wiki 更新日志":
            insert_at = i + 1
            break
    # 跳过标题后的空行
    while insert_at < len(lines) and lines[insert_at].strip() == "":
        insert_at += 1

    new_lines = lines[:insert_at] + entry_lines + lines[insert_at:]
    log_path.write_text("".join(new_lines), encoding="utf-8")


def compute_sha256(data: bytes | str | None) -> str | None:
    """计算 bytes 或 str 的 sha256；None 或空值返回 None。"""
    if data is None:
        return None
    if isinstance(data, str):
        data = data.encode("utf-8")
    if not data:
        return None
    return hashlib.sha256(data).hexdigest()
