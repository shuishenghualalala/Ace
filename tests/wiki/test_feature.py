"""Knowledge Feature Bundle lifecycle, provider, and tool ownership contracts."""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

from crew.core.mocks import FakeProvider
from crew.features import FeatureGeneration, FeatureRuntime, FeatureScope, FeatureState, FeatureStopPolicy
from crew.wiki import (
    KNOWLEDGE_SERVICE_KEY,
    FileSystemWikiStore,
    LocalWikiProvider,
    WikiCompiler,
    WikiQuerier,
    WikiSessionManager,
    WikiSummarizer,
    build_wiki_feature,
)
from crew.tools.registry import FunctionTool, Registry
from crew.wiki.service import WikiProviderComponents
from crew.wiki.schemas import IngestResult, RawSource, WikiPage
from crew.wiki.tools import WIKI_MANAGE_TOOLS, WIKI_READ_TOOLS


class _WikiHost:
    session_store = object()
    workspace_store = object()
    security_service = object()
    _auxiliary_providers: list[object] = []
    _UNSET_KNOWLEDGE_SERVICE = object()

    def __init__(self) -> None:
        self._knowledge_service_override = self._UNSET_KNOWLEDGE_SERVICE


def _bundle_host() -> _WikiHost:
    host = _WikiHost()
    host._auxiliary_providers = []
    return host


def test_local_provider_contract_uses_real_filesystem_store(tmp_path):
    store = FileSystemWikiStore(base_dir=tmp_path / "wiki")
    store.init_kb("owner", "default")
    page = WikiPage(
        id="topic-1",
        page_type="topic",
        title="Python",
        content="# Python\n\nA language.",
        file_path="",
    )
    store.save_page(page, "owner", "default")
    store.save_raw(
        RawSource(
            id="source-1",
            title="notes",
            source_type="paste",
            parsed_path="source-1.parsed.md",
            parse_status="parsed",
        ),
        "owner",
        "default",
    )

    querier = MagicMock(spec=WikiQuerier)
    querier.query.return_value = {"answer": "query"}
    querier.search.return_value = {"pages": [], "retrieval": {}}
    compiler = MagicMock(spec=WikiCompiler)
    compiler.ingest = AsyncMock(return_value=IngestResult(source_id="source-2"))
    components = WikiProviderComponents(
        store=store,
        compiler=compiler,
        querier=querier,
        summarizer=MagicMock(spec=WikiSummarizer),
        manager=MagicMock(spec=WikiSessionManager),
    )
    service = LocalWikiProvider(components)

    assert service.query("what", "owner", 3, "default") == {"answer": "query"}
    assert service.search("python", "owner", 4, "default", expand_neighbors=False) == {
        "pages": [],
        "retrieval": {},
    }
    result = asyncio.run(service.ingest("source-2", "owner", "body", "default"))
    assert result.source_id == "source-2"
    assert service.read_document("topic-1", "owner", "default").title == "Python"
    assert service.index_status("owner", "default").page_count == 1
    assert service.index_status("owner", "default").source_count == 1
    assert service.index_status("owner", "default").parsed_source_count == 1

    service.close()
    assert (tmp_path / "wiki").exists()
    assert store.get("topic-1", "owner", "default").title == "Python"


def test_wiki_feature_activates_service_all_tools_and_context_then_revokes(tmp_path):
    runtime = FeatureRuntime()
    registry = Registry()
    host = _bundle_host()
    bundle = build_wiki_feature(
        host,
        registry,
        provider=FakeProvider(),
        storage_root=tmp_path / "wiki",
    )
    bundle.provider.store.init_kb("owner", "default")

    async def exercise():
        record = await runtime.activate(bundle.definition)
        assert record.state is FeatureState.ACTIVE
        assert runtime.services.get(KNOWLEDGE_SERVICE_KEY) is not None
        assert set(registry.names()) >= set(WIKI_READ_TOOLS + WIKI_MANAGE_TOOLS)
        assert len(set(WIKI_READ_TOOLS + WIKI_MANAGE_TOOLS)) == 24
        assert [b.contributor.contributor_id for b in runtime.context_contributors.bindings()] == [
            "wiki.agent.context"
        ]
        await runtime.deactivate("product.wiki")
        assert runtime.services.get(KNOWLEDGE_SERVICE_KEY) is None
        assert runtime.context_contributors.bindings() == ()
        assert not set(WIKI_READ_TOOLS + WIKI_MANAGE_TOOLS).intersection(registry.names())
        assert (tmp_path / "wiki").exists()
        assert await runtime.deactivate("product.wiki") is False

    asyncio.run(exercise())


def test_external_knowledge_service_is_not_closed_and_candidate_is_reclaimed(tmp_path):
    class TrackingStore(FileSystemWikiStore):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.close_calls = 0

        def close(self):
            self.close_calls += 1
            super().close()

    external_store = TrackingStore(base_dir=tmp_path / "external")
    external_store.init_kb("owner", "default")
    external = LocalWikiProvider(
        WikiProviderComponents(
            store=external_store,
            compiler=WikiCompiler(external_store, FakeProvider()),
            querier=WikiQuerier(external_store),
            summarizer=WikiSummarizer(external_store, FakeProvider()),
            manager=WikiSessionManager(store=external_store),
        )
    )
    host = _bundle_host()
    host._knowledge_service_override = external
    runtime = FeatureRuntime()
    registry = Registry()
    bundle = build_wiki_feature(
        host,
        registry,
        provider=FakeProvider(),
        storage_root=tmp_path / "candidate",
    )
    candidate_store = bundle.provider.store
    candidate_close = MagicMock(wraps=candidate_store.close)
    candidate_store.close = candidate_close

    async def exercise():
        record = await runtime.activate(bundle.definition)
        assert record.state is FeatureState.ACTIVE
        assert runtime.services.get(KNOWLEDGE_SERVICE_KEY) is external
        await runtime.deactivate("product.wiki")
        assert external_store.close_calls == 0
        assert candidate_store is not external_store
        assert candidate_close.call_count == 1
        assert (tmp_path / "external").exists()

    asyncio.run(exercise())


def test_wiki_restart_replaces_generation_store_and_keeps_single_tool_context_set(
    tmp_path, monkeypatch
):
    import importlib

    feature_module = importlib.import_module("crew.wiki.feature")
    stores = []

    class TrackingStore(FileSystemWikiStore):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.close_calls = 0
            stores.append(self)

        def close(self):
            self.close_calls += 1
            super().close()

    monkeypatch.setattr(feature_module, "FileSystemWikiStore", TrackingStore)
    host = _bundle_host()
    runtime = FeatureRuntime()
    registry = Registry()
    first = build_wiki_feature(
        host,
        registry,
        provider=FakeProvider(),
        storage_root=tmp_path / "wiki",
        desired_config_revision=1,
    )
    second = build_wiki_feature(
        host,
        registry,
        provider=FakeProvider(),
        storage_root=tmp_path / "wiki",
        desired_config_revision=2,
    )

    async def exercise():
        record = await runtime.activate(first.definition)
        old_service = runtime.services.get(KNOWLEDGE_SERVICE_KEY)
        old_store = first.provider.store
        result = await runtime.update(second.definition)
        assert result.updated
        assert result.previous_generation == "product.wiki@g1"
        assert result.current_generation == "product.wiki@g2"
        assert record.generation.key == "product.wiki@g2"
        assert runtime.services.get(KNOWLEDGE_SERVICE_KEY) is not old_service
        assert old_store.close_calls == 1
        assert len(runtime.context_contributors.bindings()) == 1
        assert set(registry.names()) >= set(WIKI_READ_TOOLS + WIKI_MANAGE_TOOLS)
        assert len(registry.names()) == 24
        await runtime.deactivate("product.wiki")
        assert stores[-1].close_calls == 1

    asyncio.run(exercise())


def test_wiki_restart_failure_restores_old_definition_and_closes_failed_candidate(
    tmp_path, monkeypatch
):
    import importlib

    feature_module = importlib.import_module("crew.wiki.feature")
    stores = []

    class TrackingStore(FileSystemWikiStore):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.close_calls = 0
            stores.append(self)

        def close(self):
            self.close_calls += 1
            super().close()

    monkeypatch.setattr(feature_module, "FileSystemWikiStore", TrackingStore)
    real_build = feature_module.build_wiki_tools
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("candidate tools rejected")
        return real_build(*args, **kwargs)

    host = _bundle_host()
    runtime = FeatureRuntime()
    registry = Registry()
    first = build_wiki_feature(
        host,
        registry,
        provider=FakeProvider(),
        storage_root=tmp_path / "wiki",
        desired_config_revision=1,
    )
    second = build_wiki_feature(
        host,
        registry,
        provider=FakeProvider(),
        storage_root=tmp_path / "wiki",
        desired_config_revision=2,
    )

    async def exercise():
        record = await runtime.activate(first.definition)
        old_definition = record.definition
        monkeypatch.setattr(feature_module, "build_wiki_tools", fail_once)
        result = await runtime.update(second.definition)
        assert not result.updated
        assert result.restored
        assert record.state is FeatureState.ACTIVE
        assert record.definition is old_definition
        assert record.generation is not None
        assert record.generation.sequence == 3
        service = runtime.services.get(KNOWLEDGE_SERVICE_KEY)
        assert service is not None and hasattr(service, "components")
        assert len(runtime.context_contributors.bindings()) == 1
        assert len(registry.names()) == 24
        assert stores[1].close_calls == 1
        await runtime.deactivate("product.wiki")
        assert stores[2].close_calls == 1

    asyncio.run(exercise())


async def test_build_app_shutdown_stops_work_before_closing_wiki_store_once(tmp_path, monkeypatch):
    from crew.app import build_app
    from crew.state.config import Config

    crew_home = tmp_path / ".crew"
    monkeypatch.setenv("CREW_HOME", str(crew_home))
    app = build_app(
        config=Config(
            api_key="",
            db_path=str(crew_home / "crew_data" / "crew.db"),
            memory_db_path=str(crew_home / "crew_data" / "memory.db"),
        ),
        enable_team=False,
    )
    wiki_store = app._wiki_store
    assert wiki_store is not None
    events: list[str] = []
    original_store_close = wiki_store.close
    wiki_store.close = MagicMock(
        side_effect=lambda: (events.append("wiki-close"), original_store_close())[1]
    )
    original_work_stop = app.work_service.stop

    async def work_stop():
        events.append("work-stop")
        return await original_work_stop()

    app.work_service.stop = work_stop
    await app.shutdown(timeout=3)
    await app.shutdown(timeout=3)

    assert events.count("work-stop") == 1
    assert events.count("wiki-close") == 1
    assert events.index("work-stop") < events.index("wiki-close")


def test_wiki_tool_lease_keeps_sync_handler_off_event_loop_thread():
    from crew.wiki.feature import _bind_tool_lease

    event_loop_thread = threading.get_ident()
    handler_thread: list[int] = []

    def handler(_args):
        handler_thread.append(threading.get_ident())
        return "ok"

    tool = FunctionTool(
        name="wiki_sync_test",
        toolset="wiki.read",
        schema={"name": "wiki_sync_test", "parameters": {"type": "object"}},
        handler=handler,
        is_async=False,
    )

    class LeaseContext:
        @asynccontextmanager
        async def acquire_lease(self, _label):
            yield object()

    _bind_tool_lease(tool, LeaseContext())
    assert asyncio.run(tool.run({})) == "ok"
    assert handler_thread and handler_thread[0] != event_loop_thread


async def test_home_intro_refresh_is_scope_owned_and_drain_rejection_is_foreground_safe(tmp_path):
    store = FileSystemWikiStore(base_dir=tmp_path / "wiki")
    store.init_kb("owner", "default")
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class Summary:
        async def generate_home_intro(self, _owner, _kb):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            return None, False

    compiler = WikiCompiler(store, FakeProvider(), summarizer=Summary())
    scope = FeatureScope(FeatureGeneration("wiki", 1))
    scope.activate()
    compiler.set_task_factory(
        lambda awaitable: scope.create_task(awaitable, name="wiki.home-intro")
    )
    compiler.finalize_write("update", "owner", "default")
    await started.wait()
    assert any(token.label == "tasks:feature-scope" for token in scope.registrations)

    await scope.stop(FeatureStopPolicy.IMMEDIATE)
    assert cancelled.is_set()

    second_scope = FeatureScope(FeatureGeneration("wiki", 2))
    second_scope.activate()
    compiler.set_task_factory(
        lambda awaitable: second_scope.create_task(awaitable, name="wiki.home-intro")
    )
    second_scope.begin_draining()
    # Admission rejection is optional background work; the foreground write remains valid.
    compiler.finalize_write("after-drain", "owner", "default")
    await second_scope.dispose()


def test_wiki_tool_conflict_mid_batch_rolls_back_only_generation_tools(tmp_path):
    runtime = FeatureRuntime()
    registry = Registry()
    host = _bundle_host()
    conflict_name = WIKI_MANAGE_TOOLS[5]
    external = FunctionTool(
        name=conflict_name,
        toolset="external",
        schema={"name": conflict_name, "parameters": {"type": "object"}},
        handler=lambda _args: "external",
    )
    registry.register(external)
    bundle = build_wiki_feature(
        host,
        registry,
        provider=FakeProvider(),
        storage_root=tmp_path / "wiki",
    )
    bundle.provider.store.init_kb("owner", "default")

    async def exercise():
        record = await runtime.activate(bundle.definition)
        assert record.state is FeatureState.FAILED
        assert registry.get(conflict_name) is external
        assert set(registry.names()) == {conflict_name}
        assert runtime.services.get(KNOWLEDGE_SERVICE_KEY) is None
        assert runtime.context_contributors.bindings() == ()
        assert (tmp_path / "wiki").exists()

    asyncio.run(exercise())
