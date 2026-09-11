"""Stable Knowledge Service contract and the local Wiki implementation."""

from __future__ import annotations

import asyncio
import getpass
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

from crew.features.services import ServiceKey
from crew.state.logging import get_logger
from crew.wiki.compiler import WikiCompiler
from crew.wiki.manager import WikiSessionManager
from crew.wiki.query import WikiQuerier
from crew.wiki.schemas import (
    CompileResult,
    IngestResult,
    KnowledgeBase,
    RawSource,
    WikiGraph,
    WikiPage,
)
from crew.wiki.store import WikiStore
from crew.wiki.store._ids import normalize_kb_id as _normalize_kb_id
from crew.wiki.summary import WikiSummarizer

if TYPE_CHECKING:
    from crew.wiki.capture import CaptureTextResult

log = get_logger("wiki.service")

KnowledgeProgress = Callable[[str, int, dict[str, object]], Awaitable[None]]
KnowledgeQuery = dict[str, object]


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
    ) -> KnowledgeQuery: ...

    def search(
        self,
        query: str,
        owner_account_id: str,
        top_k: int = 5,
        kb_id: str = "default",
        *,
        expand_neighbors: bool = True,
        include_context: bool = True,
    ) -> KnowledgeQuery: ...

    def search_pages(
        self,
        query: str,
        owner_account_id: str,
        top_k: int = 5,
        kb_id: str = "default",
    ) -> list[WikiPage]: ...

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

    def initialize(self, owner_account_id: str, kb_id: str = "default") -> None: ...

    def list_knowledge_bases(self, owner_account_id: str) -> list[KnowledgeBase]: ...

    def create_knowledge_base(
        self, kb_id: str, name: str, owner_account_id: str
    ) -> KnowledgeBase: ...

    def delete_knowledge_base(self, kb_id: str, owner_account_id: str) -> bool: ...

    def read_vault_document(
        self, document_name: str, owner_account_id: str, kb_id: str = "default"
    ) -> KnowledgeVaultDocument | None: ...

    def list_pages(
        self,
        owner_account_id: str,
        kb_id: str = "default",
        *,
        limit: int = 100,
        offset: int = 0,
        brief: bool = False,
    ) -> list[WikiPage]: ...

    def save_page(self, page: WikiPage, owner_account_id: str, kb_id: str = "default") -> WikiPage: ...

    def update_page(self, page: WikiPage, owner_account_id: str, kb_id: str = "default") -> WikiPage | None: ...

    def delete_page(self, page_id: str, owner_account_id: str, kb_id: str = "default") -> bool: ...

    def source_titles(self, source_ids: list[str], owner_account_id: str, kb_id: str = "default") -> dict[str, str]: ...

    def source_pages(self, source_id: str, owner_account_id: str, kb_id: str = "default") -> list[WikiPage]: ...

    def related_pages(self, page: WikiPage, owner_account_id: str, kb_id: str = "default") -> list[WikiPageRelation]: ...

    def source_files(self, page: WikiPage, owner_account_id: str, kb_id: str = "default") -> dict[str, KnowledgeSourceFile]: ...

    def list_sources(self, owner_account_id: str, kb_id: str = "default") -> list[RawSource]: ...

    def read_source(self, source_id: str, owner_account_id: str, kb_id: str = "default") -> RawSource | None: ...

    def delete_source(self, source_id: str, owner_account_id: str, kb_id: str = "default") -> bool: ...

    def source_file(self, source_id: str, owner_account_id: str, kb_id: str = "default") -> KnowledgeSourceFile | None: ...

    async def compile_all(self, owner_account_id: str, kb_id: str = "default") -> CompileResult: ...

    async def lint(self, owner_account_id: str, kb_id: str = "default", deep: bool = False) -> list[dict[str, object]]: ...

    async def graph(self, owner_account_id: str, kb_id: str = "default") -> WikiGraph: ...

    async def upload_file(self, filename: str, content: bytes, owner_account_id: str, kb_id: str = "default") -> KnowledgeUploadResult: ...

    def capture_text(
        self,
        *,
        title: str,
        content: str,
        owner_account_id: str,
        kb_id: str = "default",
        source_url: str = "",
    ) -> "CaptureTextResult": ...

    async def capture_attachment(
        self, filename: str, content: bytes, owner_account_id: str, kb_id: str = "default"
    ) -> RawSource | None: ...

    def cancel_confirmation(self, session_id: str, confirmation_id: str, owner_account_id: str) -> bool: ...

    def session_kb_id(self, session_id: str, owner_account_id: str) -> str: ...


@dataclass(frozen=True, slots=True)
class KnowledgeIndexStatus:
    """Stable count/status view for one owner's knowledge index."""

    page_count: int
    source_count: int
    parsed_source_count: int


@dataclass(frozen=True, slots=True)
class KnowledgeVaultDocument:
    name: str
    content: str
    updated_at: float


@dataclass(frozen=True, slots=True)
class KnowledgeSourceFile:
    location: str
    title: str
    file_type: str | None
    content: bytes | None = None


@dataclass(frozen=True, slots=True)
class WikiPageRelation:
    page: WikiPage
    relation: str
    direction: str


@dataclass(frozen=True, slots=True)
class KnowledgeUploadResult:
    source_id: str
    title: str
    source_type: str
    ingested: bool = False
    needs_confirmation: bool = False
    needs_agent_review: bool = False
    error: str = ""
    error_code: str = ""
    dependency: str = ""
    install_command: str = ""
    pages: tuple[WikiPage, ...] = ()
    issues: tuple[str, ...] = ()


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

    def __init__(
        self,
        components: WikiProviderComponents,
        *,
        config: object | None = None,
        security_service: object | None = None,
    ) -> None:
        self.components = components
        self._config = config
        self._security_service = security_service

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
    ) -> KnowledgeQuery:
        return cast(KnowledgeQuery, self.querier.query(question, owner_account_id, top_k, kb_id))

    def search(
        self,
        query: str,
        owner_account_id: str,
        top_k: int = 5,
        kb_id: str = "default",
        *,
        expand_neighbors: bool = True,
        include_context: bool = True,
    ) -> KnowledgeQuery:
        return cast(KnowledgeQuery, self.querier.search(
            query,
            owner_account_id,
            top_k,
            kb_id,
            expand_neighbors=expand_neighbors,
            include_context=include_context,
        ))

    def search_pages(
        self,
        query: str,
        owner_account_id: str,
        top_k: int = 5,
        kb_id: str = "default",
    ) -> list[WikiPage]:
        return self.store.search(
            query,
            top_k,
            owner_account_id=owner_account_id,
            kb_id=kb_id,
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

    def initialize(self, owner_account_id: str, kb_id: str = "default") -> None:
        self.store.init_kb(owner_account_id, kb_id)
        try:
            if self.store.layout_migration_preview(owner_account_id, kb_id).get("required"):
                self.store.migrate_layout(owner_account_id, kb_id)
        except Exception:
            # Layout migration is a best-effort compatibility step; a failed
            # migration must not make the idempotent initialization unavailable.
            return

    def list_knowledge_bases(self, owner_account_id: str) -> list[KnowledgeBase]:
        from crew.wiki.seed import ensure_tutorial_kb

        ensure_tutorial_kb(self.store, owner_account_id)
        return self.store.list_kbs(owner_account_id)

    def create_knowledge_base(
        self, kb_id: str, name: str, owner_account_id: str
    ) -> KnowledgeBase:
        return self.store.create_kb(kb_id, name=name, owner_account_id=owner_account_id)

    def delete_knowledge_base(self, kb_id: str, owner_account_id: str) -> bool:
        return self.store.delete_kb(kb_id, owner_account_id)

    def read_vault_document(
        self, document_name: str, owner_account_id: str, kb_id: str = "default"
    ) -> KnowledgeVaultDocument | None:
        if document_name not in {"Home.md", "index.md"}:
            return None
        root_value = self.store.get_vault_path(owner_account_id, kb_id)
        if not root_value:
            return None
        root = Path(root_value)
        path = root / document_name
        if not path.is_file():
            return None
        return KnowledgeVaultDocument(
            name=document_name,
            content=path.read_text(encoding="utf-8", errors="replace"),
            updated_at=path.stat().st_mtime,
        )

    def list_pages(
        self,
        owner_account_id: str,
        kb_id: str = "default",
        *,
        limit: int = 100,
        offset: int = 0,
        brief: bool = False,
    ) -> list[WikiPage]:
        return self.store.list_all(
            owner_account_id=owner_account_id,
            kb_id=kb_id,
            limit=limit,
            offset=offset,
            brief=brief,
        )

    def save_page(self, page: WikiPage, owner_account_id: str, kb_id: str = "default") -> WikiPage:
        saved = self.store.save_page(page, owner_account_id, kb_id)
        self.compiler.finalize_write("保存 Wiki 页面", owner_account_id, kb_id)
        return saved

    def update_page(self, page: WikiPage, owner_account_id: str, kb_id: str = "default") -> WikiPage | None:
        updated = self.store.update(page, owner_account_id, kb_id)
        if updated is not None:
            self.compiler.finalize_write("更新 Wiki 页面", owner_account_id, kb_id)
        return updated

    def delete_page(self, page_id: str, owner_account_id: str, kb_id: str = "default") -> bool:
        deleted = self.store.delete(page_id, owner_account_id, kb_id)
        if deleted:
            self.compiler.finalize_write("删除 Wiki 页面", owner_account_id, kb_id)
        return deleted

    def source_titles(self, source_ids: list[str], owner_account_id: str, kb_id: str = "default") -> dict[str, str]:
        return self.store.get_source_titles(source_ids, owner_account_id, kb_id)

    def source_pages(self, source_id: str, owner_account_id: str, kb_id: str = "default") -> list[WikiPage]:
        return self.store.list_pages_by_source(source_id, owner_account_id, kb_id)

    def related_pages(self, page: WikiPage, owner_account_id: str, kb_id: str = "default") -> list[WikiPageRelation]:
        result: list[WikiPageRelation] = []
        seen: set[tuple[str, str, str]] = set()

        def append(target: WikiPage, relation: str, direction: str) -> None:
            key = (target.id, relation.casefold(), direction)
            if target.id == page.id or key in seen:
                return
            seen.add(key)
            result.append(WikiPageRelation(target, relation, direction))

        for relation in page.relations:
            target = self.store.get(relation.target_page_id, owner_account_id, kb_id)
            if target is not None:
                append(target, relation.relation, "outgoing")
        for candidate in self.store.list_all(owner_account_id=owner_account_id, kb_id=kb_id, limit=10000):
            for relation in candidate.relations:
                if relation.target_page_id == page.id:
                    append(candidate, relation.relation, "incoming")
        return result

    def _resolve_original_path(self, raw: RawSource, owner_account_id: str, kb_id: str) -> Path | None:
        if not raw.original_path:
            return None
        raw_dir = self.store._raw_dir(owner_account_id, kb_id)
        recorded = Path(raw.original_path)
        candidates = [recorded] if recorded.is_absolute() else [
            raw_dir / recorded,
            raw_dir / f"{raw.id}.original{Path(str(raw.title or '')).suffix.lower() or Path(recorded.name).suffix.lower()}",
            *sorted(raw_dir.glob(f"{raw.id}.original*")),
        ]
        return next((path for path in candidates if path.exists() and path.is_file()), None)

    def source_files(self, page: WikiPage, owner_account_id: str, kb_id: str = "default") -> dict[str, KnowledgeSourceFile]:
        result: dict[str, KnowledgeSourceFile] = {}
        for source_id in page.sources:
            raw = self.store.load_raw(source_id, owner_account_id, kb_id)
            if raw is None:
                continue
            path = self._resolve_original_path(raw, owner_account_id, kb_id)
            if path is not None:
                result[source_id] = KnowledgeSourceFile(str(path), raw.title or source_id, raw.file_type)
        return result

    def list_sources(self, owner_account_id: str, kb_id: str = "default") -> list[RawSource]:
        return self.store.list_raws(owner_account_id, kb_id)

    def read_source(self, source_id: str, owner_account_id: str, kb_id: str = "default") -> RawSource | None:
        return self.store.load_raw(source_id, owner_account_id, kb_id)

    def delete_source(self, source_id: str, owner_account_id: str, kb_id: str = "default") -> bool:
        return self.store.delete_raw(source_id, owner_account_id, kb_id)

    def source_file(self, source_id: str, owner_account_id: str, kb_id: str = "default") -> KnowledgeSourceFile | None:
        raw = self.read_source(source_id, owner_account_id, kb_id)
        if raw is None:
            return None
        path = self._resolve_original_path(raw, owner_account_id, kb_id)
        if path is None:
            return KnowledgeSourceFile(
                location="",
                title=raw.title or source_id,
                file_type=raw.file_type,
            )
        return KnowledgeSourceFile(
            location=raw.title or source_id,
            title=raw.title or source_id,
            file_type=raw.file_type,
            content=path.read_bytes(),
        )

    async def compile_all(self, owner_account_id: str, kb_id: str = "default") -> CompileResult:
        return await self.compiler.compile_all(owner_account_id, kb_id)

    async def lint(self, owner_account_id: str, kb_id: str = "default", deep: bool = False) -> list[dict[str, object]]:
        return await self.compiler.lint(owner_account_id, kb_id, deep)  # type: ignore[return-value]

    async def graph(self, owner_account_id: str, kb_id: str = "default") -> WikiGraph:
        return await asyncio.to_thread(self.store.get_graph, owner_account_id=owner_account_id, kb_id=kb_id)

    def cancel_confirmation(self, session_id: str, confirmation_id: str, owner_account_id: str) -> bool:
        return self.manager.cancel_confirmation(session_id, confirmation_id, owner_account_id=owner_account_id)

    def session_kb_id(self, session_id: str, owner_account_id: str) -> str:
        return str(self.manager.get_kb_id(session_id, owner_account_id=owner_account_id) or "default")

    def capture_text(
        self,
        *,
        title: str,
        content: str,
        owner_account_id: str,
        kb_id: str = "default",
        source_url: str = "",
    ) -> "CaptureTextResult":
        from crew.wiki.capture import capture_text_source

        return capture_text_source(
            self.store,
            self.compiler,
            title=title,
            content=content,
            owner_account_id=owner_account_id,
            kb_id=kb_id,
            source_type="paste",
            source_platform="web",
            source_url=source_url,
        )

    async def capture_attachment(
        self,
        filename: str,
        content: bytes,
        owner_account_id: str,
        kb_id: str = "default",
    ) -> RawSource | None:
        from crew.wiki.capture import capture_upload_to_wiki
        from crew.wiki.config import WikiConfig

        config = self._config if self._config is not None else WikiConfig()
        return await capture_upload_to_wiki(
            self.store,
            self.compiler,
            config,
            filename,
            content,
            owner_account_id=owner_account_id,
            kb_id=kb_id,
            provider=self.compiler.provider,
        )

    async def upload_file(
        self,
        filename: str,
        content: bytes,
        owner_account_id: str,
        kb_id: str = "default",
    ) -> KnowledgeUploadResult:
        """Store and parse one uploaded file without exposing provider paths to callers."""
        import uuid

        from crew.wiki.config import WikiConfig
        from crew.wiki.multimodal import MediaUnderstandingError, describe_media, is_image_mime, is_video_mime
        from crew.wiki.parser import MissingDependencyError, guess_mime_type, parse_document_from_bytes
        from crew.wiki.schemas import RawSource
        from crew.wiki.sources import classify_file
        from crew.wiki.store._ids import filename_from_title

        config = self._config if self._config is not None else WikiConfig()
        file_type = guess_mime_type(filename)
        source_kind = classify_file(filename, file_type)
        source_id = f"upload_{uuid.uuid4().hex[:12]}"
        is_image = is_image_mime(file_type)
        is_video = not is_image and is_video_mime(file_type)
        source_type = "image" if is_image else "video" if is_video else "upload"
        if (is_image or is_video) and not config.multimodal.enabled:
            return KnowledgeUploadResult(
                source_id, filename, source_type,
                error="Wiki 多模态功能未启用", error_code="MULTIMODAL_DISABLED",
            )
        source_dir = self.store._source_dir(source_kind, owner_account_id, kb_id)
        ext = Path(filename).suffix.lower() or ".bin"
        original_path = source_dir / f"{source_id}-{filename_from_title(Path(filename).stem)}{ext}"
        original_path.write_bytes(content)

        raw = RawSource(
            id=source_id,
            title=filename,
            source_type=source_type,  # type: ignore[arg-type]
            parsed_path="",
            original_path=str(original_path),
            file_type=file_type,
            size=len(content),
            created_at=time.time(),
            source_kind=source_kind,
            source_platform="local",
            adapter_name="builtin-file",
            original_ref=filename,
        )
        self.store.save_raw(raw, owner_account_id, kb_id)

        if is_image or is_video:
            auto_process = (
                is_image and config.multimodal.auto_image
            ) or (
                is_video and config.multimodal.auto_video and config.multimodal.video_upload_confirmed
            )
            if not auto_process:
                return KnowledgeUploadResult(
                    source_id, filename, source_type,
                    needs_confirmation=is_video,
                )
            try:
                prompt = config.multimodal.prompt_image if is_image else config.multimodal.prompt_video
                description = await asyncio.to_thread(
                    describe_media,
                    str(original_path),
                    file_type,
                    prompt,
                    confirm_upload=is_video,
                )
            except MediaUnderstandingError as exc:
                return KnowledgeUploadResult(
                    source_id, filename, source_type,
                    error=str(exc), error_code="MEDIA_UNDERSTANDING",
                    needs_confirmation=exc.needs_confirmation,
                )
            raw.parsed_path = self.store.save_parsed_markdown(source_id, description, owner_account_id, kb_id)
            raw.parse_status = "parsed"
            self.store.save_raw(raw, owner_account_id, kb_id)
            result = await self.compiler.ingest(source_id, owner_account_id=owner_account_id, kb_id=kb_id)
            return KnowledgeUploadResult(
                source_id, filename, source_type, ingested=True,
                pages=tuple(result.pages), issues=tuple(result.issues),
            )

        try:
            security_context = None
            if self._security_service is not None:
                from crew.security.context import SecurityContext
                from crew.security.launch import compile_process_launch, use_process_launch

                workspace_root = source_dir.resolve()
                security_context = SecurityContext(
                    os_user=getpass.getuser(), owner_account_id=owner_account_id,
                    workspace_id="wiki", workspace_root=workspace_root,
                    session_id="wiki-upload", request_id=uuid.uuid4().hex,
                    task_id="", cwd=workspace_root,
                )
                launch = compile_process_launch(
                    security_context,
                    self._security_service.mode_for(security_context),
                    db_path=self._security_service.db_path,
                    audit=self._security_service.audit,
                )
                with use_process_launch(launch):
                    text = await asyncio.to_thread(parse_document_from_bytes, content, filename)
            else:
                text = await asyncio.to_thread(parse_document_from_bytes, content, filename)
        except asyncio.CancelledError:
            raw.parse_status = "failed"
            raw.parse_error = "解析被中断，请重新上传或让 Agent 重新解析"
            self.store.save_raw(raw, owner_account_id, kb_id)
            raise
        except MissingDependencyError as exc:
            raw.parse_status = "failed"
            raw.parse_error = f"缺少依赖: {exc}"
            self.store.save_raw(raw, owner_account_id, kb_id)
            return KnowledgeUploadResult(
                source_id, filename, source_type, error=str(exc),
                error_code="MISSING_DEPENDENCY", dependency=exc.dependency,
                install_command=exc.install_command,
            )
        except Exception as exc:  # noqa: BLE001
            error = f"解析失败: {exc}"
            log.warning("Wiki 上传解析失败 source=%s: %s", source_id, error)
            raw.parse_status = "failed"
            raw.parse_error = error
            self.store.save_raw(raw, owner_account_id, kb_id)
            return KnowledgeUploadResult(
                source_id, filename, source_type, needs_agent_review=True, error=error,
            )

        raw.parsed_path = self.store.save_parsed_markdown(source_id, text, owner_account_id, kb_id)
        raw.parse_status = "parsed"
        self.store.save_raw(raw, owner_account_id, kb_id)
        return KnowledgeUploadResult(source_id, filename, source_type)

    def close(self) -> None:
        """Close only the local store owned by this provider."""
        close = getattr(self.store, "close", None)
        if callable(close):
            close()


KNOWLEDGE_SERVICE_KEY: ServiceKey[KnowledgeService] = ServiceKey("knowledge", version=1)


def normalize_kb_id(kb_id: str | None) -> str:
    """Normalize and validate a knowledge-base ID at the service boundary."""
    return _normalize_kb_id(kb_id)

__all__ = [
    "KnowledgeIndexStatus",
    "KnowledgeQuery",
    "KnowledgeProgress",
    "normalize_kb_id",
    "KnowledgeService",
    "KnowledgeSourceFile",
    "KnowledgeUploadResult",
    "KnowledgeVaultDocument",
    "KNOWLEDGE_SERVICE_KEY",
    "LocalWikiProvider",
    "WikiPageRelation",
    "WikiProviderComponents",
]
