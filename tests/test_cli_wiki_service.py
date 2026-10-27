"""Wiki CLI 的 KnowledgeService / generation lease 契约测试。"""

from __future__ import annotations

import ast
import asyncio
import inspect
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from crew.cli.app import CliContext, CliError
from crew.cli.main import build_parser, main
from crew.features import (
    FeatureGeneration,
    FeatureScope,
    FeatureState,
    FeatureStopPolicy,
    ServiceRegistry,
)
from crew.wiki.schemas import CompileResult, IngestResult, KnowledgeBase, RawSource, WikiGraph, WikiPage
from crew.wiki.service import KNOWLEDGE_SERVICE_KEY, KnowledgeUploadResult


OWNER = "cli-contract-owner"
KB = "contract-kb"


def _page(page_id: str = "page-1") -> WikiPage:
    return WikiPage(
        id=page_id,
        page_type="topic",
        title="Contract page",
        content="contract content",
        file_path="",
    )


def _source(source_id: str = "source-1") -> RawSource:
    return RawSource(
        id=source_id,
        title="contract.txt",
        source_type="upload",
        parsed_path="",
        created_at=1.0,
    )


class StrictKnowledgeService:
    """仅暴露 CLI 需要的 Service API，任何旧组件访问都会立即失败。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.upload_result = KnowledgeUploadResult("source-1", "contract.txt", "upload")
        self.ingest_started: asyncio.Event | None = None
        self.ingest_release: asyncio.Event | None = None
        self.ingest_result = IngestResult("source-1", [_page()], [])

    def _call(self, name: str, *args: object, **kwargs: object) -> None:
        self.calls.append((name, args, kwargs))

    def initialize(self, owner_account_id: str, kb_id: str = "default") -> None:
        self._call("initialize", owner_account_id, kb_id)

    def list_knowledge_bases(self, owner_account_id: str) -> list[KnowledgeBase]:
        self._call("list_knowledge_bases", owner_account_id)
        return [KnowledgeBase(KB, "Contract KB")]

    def create_knowledge_base(self, kb_id: str, name: str, owner_account_id: str) -> KnowledgeBase:
        self._call("create_knowledge_base", kb_id, name, owner_account_id)
        return KnowledgeBase(kb_id, name)

    def delete_knowledge_base(self, kb_id: str, owner_account_id: str) -> bool:
        self._call("delete_knowledge_base", kb_id, owner_account_id)
        return True

    def list_pages(self, owner_account_id: str, kb_id: str = "default", *, limit: int = 100, offset: int = 0, brief: bool = False) -> list[WikiPage]:
        self._call("list_pages", owner_account_id, kb_id, limit=limit, offset=offset, brief=brief)
        return [_page()]

    def read_document(self, page_id: str, owner_account_id: str, kb_id: str = "default") -> WikiPage | None:
        self._call("read_document", page_id, owner_account_id, kb_id)
        return _page(page_id)

    def save_page(self, page: WikiPage, owner_account_id: str, kb_id: str = "default") -> WikiPage:
        self._call("save_page", page, owner_account_id, kb_id)
        return page

    def update_page(self, page: WikiPage, owner_account_id: str, kb_id: str = "default") -> WikiPage:
        self._call("update_page", page, owner_account_id, kb_id)
        return page

    def delete_page(self, page_id: str, owner_account_id: str, kb_id: str = "default") -> bool:
        self._call("delete_page", page_id, owner_account_id, kb_id)
        return True

    def search_pages(self, query: str, owner_account_id: str, top_k: int = 5, kb_id: str = "default") -> list[WikiPage]:
        self._call("search_pages", query, owner_account_id, top_k, kb_id)
        return [_page()]

    def list_sources(self, owner_account_id: str, kb_id: str = "default") -> list[RawSource]:
        self._call("list_sources", owner_account_id, kb_id)
        return [_source()]

    def delete_source(self, source_id: str, owner_account_id: str, kb_id: str = "default") -> bool:
        self._call("delete_source", source_id, owner_account_id, kb_id)
        return True

    async def upload_file(self, filename: str, content: bytes, owner_account_id: str, kb_id: str = "default") -> KnowledgeUploadResult:
        self._call("upload_file", filename, content, owner_account_id, kb_id)
        return self.upload_result

    async def ingest(self, source_id: str, owner_account_id: str, source_content: str | None = None, kb_id: str = "default", progress=None, cancel_event=None, *, chunk_size=None, use_chunking=None, skip_index=False) -> IngestResult:
        self._call("ingest", source_id, owner_account_id, source_content, kb_id, progress, cancel_event, chunk_size=chunk_size, use_chunking=use_chunking, skip_index=skip_index)
        if self.ingest_started is not None:
            self.ingest_started.set()
        if self.ingest_release is not None:
            await self.ingest_release.wait()
        return self.ingest_result

    async def compile_all(self, owner_account_id: str, kb_id: str = "default") -> CompileResult:
        self._call("compile_all", owner_account_id, kb_id)
        return CompileResult(["source-1"], [])

    async def graph(self, owner_account_id: str, kb_id: str = "default") -> WikiGraph:
        self._call("graph", owner_account_id, kb_id)
        return WikiGraph([{"id": "page-1"}], [])

    def query(self, question: str, owner_account_id: str, top_k: int = 5, kb_id: str = "default") -> dict[str, object]:
        self._call("query", question, owner_account_id, top_k, kb_id)
        return {"answer": "strict answer", "pages": []}

    async def lint(self, owner_account_id: str, kb_id: str = "default", deep: bool = False) -> list[dict[str, object]]:
        self._call("lint", owner_account_id, kb_id, deep=deep)
        return []

    def cancel_confirmation(self, session_id: str, confirmation_id: str, owner_account_id: str) -> bool:
        self._call("cancel_confirmation", session_id, confirmation_id, owner_account_id)
        return True


class _HostSessionStore:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []
        self.calls: list[str] = []

    def list_sessions(self, **kwargs):
        self.calls.append("list_sessions")
        return self.rows

    def get_agent_config(self, session_id, **kwargs):
        self.calls.append("get_agent_config")
        return {}

    def ensure_session(self, session_id, **kwargs):
        self.calls.append("ensure_session")
        return None

    def set_agent_config(self, session_id, config, **kwargs):
        self.calls.append("set_agent_config")

    def clear(self, session_id, **kwargs):
        self.calls.append("clear")


class _RuntimeView:
    def __init__(self, record=None):
        self.record = record

    def get(self, feature_id):
        assert feature_id == "product.wiki"
        return self.record


class _Plugins:
    def __init__(self, service=None, *, record=None, lease=None):
        self.service = service
        self.lease = lease
        self.feature_runtime = _RuntimeView(record)
        self.acquire_calls: list[str] = []

    def acquire_service_lease(self, key, *, label="service-request"):
        assert key is KNOWLEDGE_SERVICE_KEY
        self.acquire_calls.append(label)
        if self.service is None:
            return None
        if self.lease is None:
            return None
        return self.service, self.lease


def _ctx(service=None, *, plugins=None, external=None, session_store=None) -> CliContext:
    app = SimpleNamespace(
        plugins=plugins or _Plugins(service),
        knowledge_service=external,
        session_store=session_store or _HostSessionStore(),
        config=SimpleNamespace(
            evolution_auto_trigger=False,
            evolution_auto_full_cycle=False,
            evolution_visible=True,
        ),
    )
    return CliContext(owner=OWNER, _app=app)


def _invoke(argv: list[str], ctx: CliContext):
    args = build_parser().parse_args(argv)
    result = args.handler(args, ctx)
    return asyncio.run(result) if inspect.isawaitable(result) else result


WIKI_LEAVES = (
    ["wiki", "init"],
    ["wiki", "kbs", "list"],
    ["wiki", "kbs", "create", "--kb-id", KB],
    ["wiki", "kbs", "delete", "--kb-id", KB],
    ["wiki", "pages", "list"],
    ["wiki", "pages", "show", "--id", "page-1"],
    ["wiki", "pages", "create", "--title", "new"],
    ["wiki", "pages", "update", "--id", "page-1"],
    ["wiki", "pages", "delete", "--id", "page-1"],
    ["wiki", "search", "--q", "term"],
    ["wiki", "sources", "list"],
    ["wiki", "sources", "delete", "--id", "source-1"],
    ["wiki", "upload", "--file", "missing.txt"],
    ["wiki", "ingest", "--source-id", "source-1"],
    ["wiki", "compile"],
    ["wiki", "graph"],
    ["wiki", "query", "--q", "question"],
    ["wiki", "lint"],
    ["wiki", "agent-sessions"],
    ["wiki", "agent-session"],
    ["wiki", "confirmation-cancel", "--confirmation-id", "c", "--session-id", "s"],
)


@pytest.mark.parametrize("argv", WIKI_LEAVES, ids=lambda argv: "-".join(argv[1:]))
def test_every_registered_wiki_leaf_is_gated_when_missing(argv):
    with pytest.raises(CliError, match="^Wiki 未启用$"):
        _invoke(argv, _ctx())


@pytest.mark.parametrize("argv", WIKI_LEAVES, ids=lambda argv: "-".join(argv[1:]))
def test_every_registered_wiki_leaf_is_gated_when_generation_draining(argv):
    service = StrictKnowledgeService()
    scope = FeatureScope(FeatureGeneration("product.wiki", 1))
    registry = ServiceRegistry()
    registry.register(scope, KNOWLEDGE_SERVICE_KEY, service)
    scope.activate()
    scope.begin_draining()
    plugins = _Plugins(record=SimpleNamespace(state=FeatureState.DRAINING))
    plugins.acquire_service_lease = lambda key, *, label="service-request": registry.acquire_lease(key, label=label)
    try:
        with pytest.raises(CliError, match="^Wiki 未启用$"):
            _invoke(argv, _ctx(plugins=plugins))
    finally:
        asyncio.run(scope.dispose())


def test_skill_leaf_is_not_wrapped_by_wiki_gate():
    args = build_parser().parse_args(["skill", "evolution"])
    assert args.handler.__name__ == "_skill_evolution"
    plugins = _Plugins()
    result = args.handler(args, _ctx(plugins=plugins))
    assert result.data["visible"] is True
    assert plugins.acquire_calls == []


def test_cli_real_main_path_uses_service_and_emits_result(capsys, monkeypatch):
    service = StrictKnowledgeService()
    app = _ctx(external=service).app
    monkeypatch.setattr("crew.cli.app.build_app", lambda _config: app)
    code = main(["wiki", "query", "--q", "main-path", "--json"])
    assert code == 0
    assert '"answer": "strict answer"' in capsys.readouterr().out
    assert service.calls[-1][0] == "query"


def test_each_command_resolves_current_service_generation_without_stale_cache():
    registry = ServiceRegistry()
    old_scope = FeatureScope(FeatureGeneration("product.wiki", 1))
    old = StrictKnowledgeService()
    registry.register(old_scope, KNOWLEDGE_SERVICE_KEY, old)
    old_scope.activate()
    plugins = _Plugins()
    plugins.acquire_service_lease = lambda key, *, label="service-request": registry.acquire_lease(key, label=label)
    ctx = _ctx(plugins=plugins)
    assert _invoke(["wiki", "query", "--q", "old"], ctx).data["answer"] == "strict answer"

    new_scope = FeatureScope(FeatureGeneration("product.wiki", 2))
    new = StrictKnowledgeService()
    new.query = lambda question, owner_account_id, top_k=5, kb_id="default": {"answer": "new", "pages": []}
    registry.register(new_scope, KNOWLEDGE_SERVICE_KEY, new)
    new_scope.activate()
    try:
        result = _invoke(["wiki", "query", "--q", "new"], ctx)
        assert result.data["answer"] == "new"
        assert not new_scope.active_leases
        assert not old_scope.active_leases
    finally:
        asyncio.run(old_scope.dispose())
        asyncio.run(new_scope.dispose())


@dataclass
class _CountingLease:
    release_calls: int = 0
    entered: int = 0
    exited: int = 0

    def release(self):
        self.release_calls += 1

    async def __aenter__(self):
        self.entered += 1
        return self

    async def __aexit__(self, *_args):
        self.exited += 1
        self.release()
        return False


def test_sync_handler_releases_lease_once_on_success_and_exception():
    service = StrictKnowledgeService()
    lease = _CountingLease()
    plugins = _Plugins(service, lease=lease)
    _invoke(["wiki", "query", "--q", "ok"], _ctx(plugins=plugins))
    assert lease.release_calls == 1

    class Broken(StrictKnowledgeService):
        def query(self, *args, **kwargs):
            raise RuntimeError("service failure")

    broken_lease = _CountingLease()
    with pytest.raises(RuntimeError, match="service failure"):
        _invoke(["wiki", "query", "--q", "broken"], _ctx(Broken(), plugins=_Plugins(Broken(), lease=broken_lease)))
    assert broken_lease.release_calls == 1


@pytest.mark.asyncio
async def test_async_handler_holds_lease_for_entire_await_and_releases_once():
    service = StrictKnowledgeService()
    started = asyncio.Event()
    release = asyncio.Event()
    service.ingest_started = started
    service.ingest_release = release
    lease = _CountingLease()
    task = asyncio.create_task(_invoke_async(["wiki", "ingest", "--source-id", "source-1"], _ctx(service, plugins=_Plugins(service, lease=lease))))
    await started.wait()
    assert lease.entered == 1 and lease.release_calls == 0
    release.set()
    await task
    assert lease.exited == 1 and lease.release_calls == 1


async def _invoke_async(argv, ctx):
    args = build_parser().parse_args(argv)
    return await args.handler(args, ctx)


@pytest.mark.asyncio
async def test_drain_waits_for_async_cli_command_then_lease_is_released():
    service = StrictKnowledgeService()
    service.ingest_started = asyncio.Event()
    service.ingest_release = asyncio.Event()
    scope = FeatureScope(FeatureGeneration("product.wiki", 1))
    registry = ServiceRegistry()
    registry.register(scope, KNOWLEDGE_SERVICE_KEY, service)
    scope.activate()
    plugins = _Plugins()
    plugins.acquire_service_lease = lambda key, *, label="service-request": registry.acquire_lease(key, label=label)
    command = asyncio.create_task(_invoke_async(["wiki", "ingest", "--source-id", "source-1"], _ctx(plugins=plugins)))
    await service.ingest_started.wait()
    stopping = asyncio.create_task(scope.stop(FeatureStopPolicy.DRAIN, timeout_seconds=None))
    for _ in range(10):
        if scope.state is FeatureState.DRAINING:
            break
        await asyncio.sleep(0)
    assert not stopping.done()
    assert scope.state is FeatureState.DRAINING
    service.ingest_release.set()
    await command
    await stopping
    assert scope.state is FeatureState.DISPOSED
    assert not scope.active_leases


def test_external_override_only_applies_without_product_wiki_record():
    external = StrictKnowledgeService()
    result = _invoke(["wiki", "query", "--q", "external"], _ctx(external=external))
    assert result.data["answer"] == "strict answer"

    draining_record = SimpleNamespace(state=FeatureState.DRAINING)
    with pytest.raises(CliError, match="^Wiki 未启用$"):
        _invoke(
            ["wiki", "query", "--q", "stale"],
            _ctx(plugins=_Plugins(record=draining_record), external=external),
        )
    assert len(external.calls) == 1


def _last(service: StrictKnowledgeService, name: str):
    return next(call for call in reversed(service.calls) if call[0] == name)


def test_kb_page_source_search_and_query_forward_owner_kb_and_parameters():
    service = StrictKnowledgeService()
    ctx = _ctx(external=service)
    _invoke(["wiki", "init", "--kb-id", KB], ctx)
    _invoke(["wiki", "kbs", "list"], ctx)
    _invoke(["wiki", "kbs", "create", "--kb-id", KB, "--name", "Name"], ctx)
    _invoke(["wiki", "pages", "list", "--kb-id", KB, "--limit", "7", "--offset", "2", "--brief"], ctx)
    _invoke(["wiki", "pages", "show", "--id", "page-1", "--kb-id", KB], ctx)
    _invoke(["wiki", "pages", "create", "--kb-id", KB, "--title", "New", "--content", "Body", "--sources", "s1,s2", "--tags", "t1"], ctx)
    _invoke(["wiki", "pages", "update", "--id", "page-1", "--kb-id", KB, "--title", "Updated"], ctx)
    _invoke(["wiki", "pages", "delete", "--id", "page-1", "--kb-id", KB], ctx)
    _invoke(["wiki", "search", "--q", "needle", "--kb-id", KB, "--top-k", "9"], ctx)
    _invoke(["wiki", "sources", "list", "--kb-id", KB], ctx)
    _invoke(["wiki", "sources", "delete", "--id", "source-1", "--kb-id", KB], ctx)
    _invoke(["wiki", "query", "--q", "question", "--kb-id", KB], ctx)

    assert _last(service, "initialize")[1] == (OWNER, KB)
    listed = [call for call in service.calls if call[0] == "list_pages" and call[2]["limit"] == 7]
    assert listed and listed[-1][2] == {"limit": 7, "offset": 2, "brief": True}
    saved = _last(service, "save_page")[1][0]
    assert saved.relations == []
    assert _last(service, "search_pages")[1] == ("needle", OWNER, 9, KB)
    assert _last(service, "query")[1] == ("question", OWNER, 5, KB)
    assert _last(service, "delete_source")[1] == ("source-1", OWNER, KB)


def test_pages_create_explicit_invalid_relations_json_still_fails():
    service = StrictKnowledgeService()
    with pytest.raises(CliError, match="^relations 不是合法 JSON:"):
        _invoke(
            [
                "wiki",
                "pages",
                "create",
                "--title",
                "Bad",
                "--relations",
                "not-json",
            ],
            _ctx(external=service),
        )
    assert service.calls == []


@pytest.mark.parametrize(
    ("name", "argv"),
    [
        ("ingest", ["wiki", "ingest", "--source-id", "source-1", "--kb-id", KB]),
        ("compile_all", ["wiki", "compile", "--kb-id", KB]),
        ("graph", ["wiki", "graph", "--kb-id", KB]),
        ("lint", ["wiki", "lint", "--kb-id", KB, "--deep"]),
        ("cancel_confirmation", ["wiki", "confirmation-cancel", "--confirmation-id", "c", "--session-id", "s"]),
    ],
)
def test_ingest_compile_graph_lint_confirmation_call_only_service(name, argv):
    service = StrictKnowledgeService()
    _invoke(argv, _ctx(external=service))
    assert _last(service, name)
    assert all(call[0] not in {"_wiki_store", "_wiki_compiler", "_wiki_querier"} for call in service.calls)


@pytest.mark.parametrize(
    ("result", "expected_data", "expected_text"),
    [
        (KnowledgeUploadResult("s", "a.txt", "upload"), {"ok": True, "source_id": "s", "title": "a.txt"}, "已保存数据源 s"),
        (KnowledgeUploadResult("s", "a.png", "image"), {"ok": True, "source_id": "s", "title": "a.png", "source_type": "image", "ingested": False}, "已保存媒体数据源 s"),
        (KnowledgeUploadResult("s", "a.png", "image", ingested=True, pages=(_page(),), issues=("issue",)), {"ok": True, "source_id": "s", "title": "a.png", "source_type": "image", "ingested": True, "pages": [_page().to_dict()], "issues": ["issue"]}, "已入库媒体数据源 s"),
        (KnowledgeUploadResult("s", "a.png", "image", error="confirm", error_code="MEDIA_UNDERSTANDING", needs_confirmation=True), {"ok": False, "error": "confirm", "source_id": "s", "needs_confirmation": True}, "confirm"),
        (KnowledgeUploadResult("s", "a.png", "image", error="no-confirm", error_code="MEDIA_UNDERSTANDING", needs_confirmation=False), {"ok": False, "error": "no-confirm", "source_id": "s", "needs_confirmation": False}, "no-confirm"),
        (KnowledgeUploadResult("s", "a.bin", "upload", error="review", needs_agent_review=True), {"ok": True, "source_id": "s", "title": "a.bin", "needs_agent_review": True, "error": "review"}, "文件已保存但解析失败: review"),
    ],
)
def test_upload_data_and_text_semantics(tmp_path, result, expected_data, expected_text):
    path = tmp_path / result.title
    path.write_bytes(b"payload")
    service = StrictKnowledgeService()
    service.upload_result = result
    output = _invoke(["wiki", "upload", "--file", str(path), "--kb-id", KB], _ctx(external=service))
    assert output.data == expected_data
    assert output.text == expected_text
    assert _last(service, "upload_file")[1] == (result.title, b"payload", OWNER, KB)


@pytest.mark.parametrize(
    "result",
    [
        KnowledgeUploadResult("s", "a.png", "image", error="disabled", error_code="MULTIMODAL_DISABLED"),
        KnowledgeUploadResult("s", "a.pdf", "upload", error="install", error_code="MISSING_DEPENDENCY"),
    ],
)
def test_upload_structured_errors_are_cli_failures(tmp_path, result):
    path = tmp_path / result.title
    path.write_bytes(b"payload")
    service = StrictKnowledgeService()
    service.upload_result = result
    with pytest.raises(CliError, match=result.error):
        _invoke(["wiki", "upload", "--file", str(path)], _ctx(external=service))


def test_upload_missing_and_empty_files_are_cli_failures(tmp_path):
    service = StrictKnowledgeService()
    with pytest.raises(CliError, match="文件不存在"):
        _invoke(["wiki", "upload", "--file", str(tmp_path / "missing")], _ctx(external=service))
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")
    with pytest.raises(CliError, match="上传文件为空"):
        _invoke(["wiki", "upload", "--file", str(empty)], _ctx(external=service))
    assert service.calls == []


def test_session_store_host_operations_still_require_service_gate():
    sessions = _HostSessionStore()
    with pytest.raises(CliError, match="^Wiki 未启用$"):
        _invoke(["wiki", "agent-sessions"], _ctx(session_store=sessions))
    assert sessions.calls == []

    _invoke(["wiki", "agent-session", "--force-new"], _ctx(external=StrictKnowledgeService(), session_store=sessions))
    assert {"ensure_session", "set_agent_config"} <= set(sessions.calls)


def test_cli_knowledge_source_has_no_legacy_component_or_local_layout_boundary():
    path = Path(__file__).parents[1] / "crew" / "cli" / "knowledge.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_names = {"Store", "Parser", "Compiler", "Querier", "Manager"}
    imported = []
    imported_modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_modules.append(node.module or "")
            imported.extend(alias.name for alias in node.names)
    assert not any(name.split(".")[-1] in forbidden_names for name in imported)
    assert not any(
        module.startswith((
            "crew.wiki.store",
            "crew.wiki.parser",
            "crew.wiki.compiler",
            "crew.wiki.query",
            "crew.wiki.manager",
        ))
        for module in imported_modules
    )
    for forbidden in ("_wiki_store", "_wiki_compiler", "_wiki_querier", "wiki_manager"):
        assert forbidden not in source
