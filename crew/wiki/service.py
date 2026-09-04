"""Stable Knowledge Service contract and the local Wiki implementation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from crew.features.services import ServiceKey
from crew.wiki.compiler import WikiCompiler
from crew.wiki.manager import WikiSessionManager
from crew.wiki.query import WikiQuerier
from crew.wiki.schemas import IngestResult, WikiPage
from crew.wiki.store import WikiStore
from crew.wiki.summary import WikiSummarizer

KnowledgeProgress = Callable[[str, int, dict[str, Any]], Awaitable[None]]


@runtime_checkable
class KnowledgeService(Protocol):
    """Business capability exposed by a knowledge provider.

    Consumers depend on these operations rather than a concrete filesystem
    store.  The return values intentionally remain the existing Wiki payloads
    so CLI and Gateway migrations can happen independently of this seam.
    """

    def query(
        self,
        question: str,
        owner_account_id: str,
        top_k: int = 5,
        kb_id: str = "default",
    ) -> dict[str, Any]: ...

    def search(
        self,
        query: str,
        owner_account_id: str,
        top_k: int = 5,
        kb_id: str = "default",
        *,
        expand_neighbors: bool = True,
        include_context: bool = True,
    ) -> dict[str, Any]: ...

    async def ingest(
        self,
        source_id: str,
        owner_account_id: str,
        source_content: str | None = None,
        kb_id: str = "default",
        progress: KnowledgeProgress | None = None,
        cancel_event: asyncio.Event | None = None,
        *,
        chunk_size: int | None = None,
        use_chunking: bool | None = None,
        skip_index: bool = False,
    ) -> IngestResult: ...

    def read_document(
        self,
        page_id: str,
        owner_account_id: str,
        kb_id: str = "default",
    ) -> WikiPage | None: ...

    def index_status(
        self,
        owner_account_id: str,
        kb_id: str = "default",
    ) -> KnowledgeIndexStatus: ...


@dataclass(frozen=True, slots=True)
class KnowledgeIndexStatus:
    """Stable count/status view for one owner's knowledge index."""

    page_count: int
    source_count: int
    parsed_source_count: int


@dataclass(frozen=True, slots=True)
class WikiProviderComponents:
    """Temporary component view for CLI/Gateway migration adapters."""

    store: WikiStore
    compiler: WikiCompiler
    querier: WikiQuerier
    summarizer: WikiSummarizer
    manager: WikiSessionManager


class LocalWikiProvider:
    """Filesystem-backed KnowledgeService with an explicit compatibility seam."""

    def __init__(self, components: WikiProviderComponents) -> None:
        self.components = components

    @property
    def store(self) -> WikiStore:
        return self.components.store

    @property
    def compiler(self) -> WikiCompiler:
        return self.components.compiler

    @property
    def querier(self) -> WikiQuerier:
        return self.components.querier

    @property
    def summarizer(self) -> WikiSummarizer:
        return self.components.summarizer

    @property
    def manager(self) -> WikiSessionManager:
        return self.components.manager

    def query(
        self,
        question: str,
        owner_account_id: str,
        top_k: int = 5,
        kb_id: str = "default",
    ) -> dict[str, Any]:
        return self.querier.query(question, owner_account_id, top_k, kb_id)

    def search(
        self,
        query: str,
        owner_account_id: str,
        top_k: int = 5,
        kb_id: str = "default",
        *,
        expand_neighbors: bool = True,
        include_context: bool = True,
    ) -> dict[str, Any]:
        return self.querier.search(
            query,
            owner_account_id,
            top_k,
            kb_id,
            expand_neighbors=expand_neighbors,
            include_context=include_context,
        )

    async def ingest(
        self,
        source_id: str,
        owner_account_id: str,
        source_content: str | None = None,
        kb_id: str = "default",
        progress: KnowledgeProgress | None = None,
        cancel_event: asyncio.Event | None = None,
        *,
        chunk_size: int | None = None,
        use_chunking: bool | None = None,
        skip_index: bool = False,
    ) -> IngestResult:
        return await self.compiler.ingest(
            source_id,
            owner_account_id,
            source_content,
            kb_id,
            progress,
            cancel_event,
            chunk_size=chunk_size,
            use_chunking=use_chunking,
            skip_index=skip_index,
        )

    def read_document(
        self,
        page_id: str,
        owner_account_id: str,
        kb_id: str = "default",
    ) -> WikiPage | None:
        return self.store.get(page_id, owner_account_id=owner_account_id, kb_id=kb_id)

    def index_status(
        self,
        owner_account_id: str,
        kb_id: str = "default",
    ) -> KnowledgeIndexStatus:
        raws = self.store.list_raws(owner_account_id, kb_id)
        return KnowledgeIndexStatus(
            page_count=self.store.count_pages(owner_account_id, kb_id),
            source_count=len(raws),
            parsed_source_count=sum(
                1 for raw in raws if (raw.parse_status or "pending") == "parsed"
            ),
        )

    def close(self) -> None:
        """Close only the local store owned by this provider."""
        close = getattr(self.store, "close", None)
        if callable(close):
            close()


KNOWLEDGE_SERVICE_KEY = ServiceKey[KnowledgeService]("knowledge")

__all__ = [
    "KnowledgeIndexStatus",
    "KnowledgeProgress",
    "KnowledgeService",
    "KNOWLEDGE_SERVICE_KEY",
    "LocalWikiProvider",
    "WikiProviderComponents",
]
