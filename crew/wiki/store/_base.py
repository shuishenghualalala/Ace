"""Wiki 存储抽象接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from crew.wiki.schemas import (
    HomeIntro,
    KBSummary,
    KnowledgeBase,
    LintIssue,
    RawSource,
    WikiGraph,
    WikiOrientation,
    WikiPage,
)
from crew.wiki._utils import normalize_page_key


class WikiStore(ABC):
    """Wiki 存储抽象接口。"""

    @abstractmethod
    def init_kb(self, owner_account_id: str = "", kb_id: str = "default") -> None: ...

    @abstractmethod
    def list_kbs(self, owner_account_id: str = "") -> list[KnowledgeBase]: ...

    @abstractmethod
    def create_kb(
        self,
        kb_id: str,
        name: str = "",
        owner_account_id: str = "",
    ) -> KnowledgeBase: ...

    @abstractmethod
    def delete_kb(self, kb_id: str, owner_account_id: str = "") -> bool: ...

    def get_vault_path(
        self,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> str:
        """返回可由 Obsidian 直接打开的知识库根目录。"""
        for kb in self.list_kbs(owner_account_id):
            if kb.id == kb_id:
                return kb.vault_path
        return ""

    # ---- kb summary ----
    def get_kb_summary(
        self,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> KBSummary:
        """读取知识库摘要元数据；默认实现从 list_kbs 中查找。"""
        for kb in self.list_kbs(owner_account_id):
            if kb.id == kb_id:
                return kb.summary
        return KBSummary()

    def set_kb_summary(
        self,
        summary: KBSummary,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> None:
        """写入知识库摘要元数据；子类可覆盖以优化写入路径。"""
        for kb in self.list_kbs(owner_account_id):
            if kb.id == kb_id:
                kb.summary = summary
                return

    # ---- home intro ----
    def get_home_intro(
        self,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> HomeIntro:
        """读取 Home.md 导读缓存；不支持的存储实现返回空导读。"""
        return HomeIntro()

    def set_home_intro(
        self,
        intro: HomeIntro,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> None:
        """写入 Home.md 导读缓存；不支持的存储实现可保持空操作。"""

    # ---- raw sources ----
    @abstractmethod
    def save_raw(
        self,
        source: RawSource,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> RawSource: ...

    @abstractmethod
    def load_raw(
        self,
        source_id: str,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> RawSource | None: ...

    @abstractmethod
    def list_raws(
        self,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> list[RawSource]: ...

    @abstractmethod
    def delete_raw(
        self,
        source_id: str,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> bool: ...

    def find_source_kb(
        self,
        source_id: str,
        owner_account_id: str = "",
    ) -> str | None:
        """定位 source_id 实际所在的知识库 ID；找不到返回 None。

        供 source 级操作在调用方未显式指定 kb_id 时跟随 source 归属，
        避免 capture 与后续 parse/plan/apply 因会话活跃 KB 变化而落到不同知识库。
        """
        kb_ids = [kb.id for kb in self.list_kbs(owner_account_id)]
        if "default" not in kb_ids:
            kb_ids.append("default")
        for kb_id in kb_ids:
            if self.load_raw(source_id, owner_account_id, kb_id) is not None:
                return kb_id
        return None

    def get_source_titles(
        self,
        source_ids: list[str],
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> dict[str, str]:
        """批量获取 source_id 对应的人类可读标题，缺失时回退到 source_id。"""
        return {sid: sid for sid in source_ids}

    @abstractmethod
    def save_parsed_markdown(
        self,
        source_id: str,
        content: str,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> str: ...

    # ---- pages ----
    @abstractmethod
    def save_page(
        self,
        page: WikiPage,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> WikiPage: ...

    @abstractmethod
    def get(
        self,
        page_id: str,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> WikiPage | None: ...

    @abstractmethod
    def get_by_title(
        self,
        title: str,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> WikiPage | None: ...

    def resolve_page(
        self,
        title: str,
        page_type: str | None = None,
        aliases: list[str] | None = None,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> WikiPage | None:
        """按规范标题和 aliases 解析已有页面。

        精确标题优先；随后使用规范化标题/别名匹配。若出现多个候选，只有页面
        类型能唯一消歧时才返回，避免把同名但不同语义的页面静默合并。
        """
        exact = self.get_by_title(title, owner_account_id, kb_id)
        if exact is not None and (page_type is None or exact.page_type == page_type):
            return exact

        keys = {
            normalize_page_key(value)
            for value in [title, *(aliases or [])]
            if normalize_page_key(value)
        }
        if not keys:
            return None

        candidates: list[WikiPage] = []
        for page in self.list_all(
            owner_account_id=owner_account_id,
            kb_id=kb_id,
            limit=10000,
        ):
            page_keys = {
                normalize_page_key(value)
                for value in [page.title, *page.aliases]
                if normalize_page_key(value)
            }
            if keys & page_keys:
                candidates.append(page)

        typed = [page for page in candidates if page_type is None or page.page_type == page_type]
        if len(typed) == 1:
            return typed[0]
        return None

    def get_source_page(
        self,
        source_id: str,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> WikiPage | None:
        """按 source_id 定位其 Source 摘要页；身份基于 source_id 而非标题。

        先用 brief 列表定位命中页面 id，再按 id 读取完整页面，避免全量加载正文。
        """
        brief_page = None
        for page in self.list_all(
            owner_account_id=owner_account_id,
            kb_id=kb_id,
            limit=10000,
            brief=True,
        ):
            if page.page_type == "source" and source_id in page.sources:
                brief_page = page
                break
        if brief_page is None:
            return None
        return self.get(brief_page.id, owner_account_id=owner_account_id, kb_id=kb_id)

    @abstractmethod
    def update(
        self,
        page: WikiPage,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> WikiPage | None: ...

    @abstractmethod
    def delete(
        self,
        page_id: str,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> bool: ...

    @abstractmethod
    def list_all(
        self,
        owner_account_id: str = "",
        kb_id: str = "default",
        limit: int = 100,
        offset: int = 0,
        brief: bool = False,
    ) -> list[WikiPage]: ...

    def count_pages(
        self,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> int:
        """返回知识库中的页面总数。默认通过 list_all(brief=True) 计数，子类应覆盖为高效实现。"""
        return len(self.list_all(owner_account_id=owner_account_id, kb_id=kb_id, limit=100000, brief=True))

    def list_pages_by_source(
        self,
        source_id: str,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> list[WikiPage]:
        """返回引用了指定 source 的页面列表（brief 模式，不含正文）。默认通过 list_all 过滤。"""
        return [
            page
            for page in self.list_all(owner_account_id=owner_account_id, kb_id=kb_id, limit=100000, brief=True)
            if source_id in page.sources
        ]

    @abstractmethod
    def search(
        self,
        query: str,
        top_k: int = 5,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> list[WikiPage]: ...

    def search_index(
        self,
        query: str,
        top_k: int = 5,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> list[WikiPage]:
        """从结构化 index 导航召回页面；不支持的存储实现可返回空结果。"""
        return []

    @abstractmethod
    def get_graph(
        self,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> WikiGraph: ...

    @abstractmethod
    def get_neighbors(
        self,
        page_id: str,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> list[WikiPage]:
        """返回与指定页面关联的邻居页面列表（通过 related / [[wikilink]] / sources 关系）。

        用于 Agent 沿知识图谱多跳探索，无需额外依赖。
        """
        ...

    @abstractmethod
    def lint(
        self,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> list[LintIssue]: ...

    @abstractmethod
    def orient(
        self,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> WikiOrientation: ...

    def append_log(
        self,
        messages: list[str],
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> None:
        """追加 Wiki 操作日志；默认空实现，子类可覆盖。"""
        return

    def update_home(
        self,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> None:
        """重建 Obsidian Home.md；不支持的存储实现可保持空操作。"""
        return

    def layout_migration_preview(
        self,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> dict[str, Any]:
        return {"required": False, "pages": 0, "sources": 0}

    def migrate_layout(
        self,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> dict[str, Any]:
        return {"migrated": False, "pages": 0, "sources": 0}

    def check_source_duplicate(
        self,
        source: RawSource,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> RawSource | None:
        """检查同 KB 下是否已有相同 content_sha256 的 source；默认空实现。"""
        return None

    def check_source_drift(
        self,
        source: RawSource,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> list[RawSource]:
        """检查同 URL 的 source 是否发生过内容漂移；默认空实现。"""
        return []

    def superseded_source_ids(
        self,
        owner_account_id: str = "",
        kb_id: str = "default",
    ) -> set[str]:
        """返回已被新版本取代的 source_id 集合；默认空实现。"""
        return set()
