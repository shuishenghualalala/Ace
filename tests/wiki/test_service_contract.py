"""KnowledgeService 契约一致性测试。

目的：把 ``crew.wiki.service.KnowledgeService`` 协议面的行为断言写成与实现
无关的参数化套件。每个用例通过 ``make_service`` fixture 从 Provider 工厂获得
服务实例；未来 Remote Provider 只需在 ``PROVIDER_FACTORIES`` 注册一个新工厂，
全部契约用例即自动复跑到该实现上。

组装策略：本地工厂走与生产 Bundle（``crew.wiki.feature.make_provider``）相同
的组件构造路径——真实 FileSystemWikiStore、WikiQuerier、WikiCompiler、
WikiSummarizer、WikiSessionManager，不 mock 任何存储/检索组件。唯一被替
换的边界是 LLM（脚本化 FakeProvider，与 compiler 单测同一策略），因为
ingest 的分析阶段天然依赖模型输出。

序列化边界：协议返回的全部 DTO 做 JSON round-trip 验证，证明它们传输无关
（未来本地/远程替换的前提）。当前无法无损序列化的字段在测试中锁定现状
形态，详见各用例注释。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Callable

import pytest

from crew.core.mocks import FakeProvider
from crew.core.types import ChatResponse
from crew.wiki.capture import CaptureValidationError
from crew.wiki.config import WikiConfig, WikiMultimodalConfig
from crew.wiki.compiler import WikiCompiler
from crew.wiki.manager import WikiSessionManager
from crew.wiki.query import WikiQuerier
from crew.wiki.schemas import (
    CompileResult,
    IngestResult,
    KnowledgeBase,
    RawSource,
    WikiClaim,
    WikiEvidence,
    WikiGraph,
    WikiPage,
    WikiRelation,
)
from crew.wiki.service import (
    KnowledgeIndexStatus,
    KnowledgeService,
    KnowledgeSourceFile,
    KnowledgeUploadResult,
    KnowledgeVaultDocument,
    LocalWikiProvider,
    WikiPageRelation,
    WikiProviderComponents,
    normalize_kb_id,
)
from crew.wiki.store import FileSystemWikiStore
from crew.wiki.summary import WikiSummarizer

# ---------------------------------------------------------------------------
# Provider 工厂注册表：契约套件的唯一扩展点
# ---------------------------------------------------------------------------

ProviderFactory = Callable[..., Any]


def _build_local_provider(
    storage_root: Path,
    *,
    llm_script: list[ChatResponse] | None = None,
    config: WikiConfig | None = None,
) -> LocalWikiProvider:
    """按生产 Bundle 的组件构造路径组装 LocalWikiProvider。

    与 feature.make_provider 保持同一组装顺序；LLM 用脚本化 FakeProvider，
    其余组件全部为生产实现。
    """
    llm = FakeProvider(script=list(llm_script or []))
    store = FileSystemWikiStore(storage_root=storage_root)
    summarizer = WikiSummarizer(store, llm)
    compiler = WikiCompiler(store, llm, summarizer=summarizer)
    return LocalWikiProvider(
        WikiProviderComponents(
            store=store,
            compiler=compiler,
            querier=WikiQuerier(store),
            summarizer=summarizer,
            manager=WikiSessionManager(store=store),
        ),
        config=config or WikiConfig(),
    )


PROVIDER_FACTORIES: dict[str, ProviderFactory] = {
    "local": _build_local_provider,
}


@pytest.fixture(params=sorted(PROVIDER_FACTORIES))
def make_service(request: pytest.FixtureRequest, tmp_path: Path):
    """按工厂参数化构造服务实例，用例结束后统一释放存储句柄。"""
    factory = PROVIDER_FACTORIES[request.param]
    opened: list[Any] = []

    def _make(
        *,
        llm_script: list[ChatResponse] | None = None,
        config: WikiConfig | None = None,
    ) -> Any:
        service = factory(tmp_path / request.param, llm_script=llm_script, config=config)
        opened.append(service)
        return service

    yield _make
    for service in opened:
        service.close()


# ---------------------------------------------------------------------------
# 脚本化 LLM 分析结果的构造辅助
# ---------------------------------------------------------------------------


def _analysis(
    entities: list[dict[str, Any]] | None = None,
    topics: list[dict[str, Any]] | None = None,
    relationships: list[dict[str, str]] | None = None,
) -> ChatResponse:
    payload: dict[str, Any] = {
        "source_summary": {"one_sentence": "契约测试来源。", "core_points": ["要点一"]},
        "entities": entities or [],
        "topics": topics or [],
        "relationships": relationships or [],
    }
    return ChatResponse(text=json.dumps(payload, ensure_ascii=False))


_ENTITY = {"name": "AgentRuntime", "description": "Agent 运行时组件。"}
_TOPIC = {
    "name": "Wiki 设计",
    "description": "Wiki 模块的设计思路。",
    "summary": "记录 Wiki 设计。",
}


def _page_ids(pages: list[WikiPage]) -> set[str]:
    return {page.id for page in pages}


# ---------------------------------------------------------------------------
# 协议符合性
# ---------------------------------------------------------------------------


def test_provider_satisfies_knowledge_service_protocol(make_service):
    service = make_service()
    assert isinstance(service, KnowledgeService)


def test_normalize_kb_id_boundary():
    assert normalize_kb_id(None) == "default"
    assert normalize_kb_id("") == "default"
    assert normalize_kb_id(" 项目 ") == "项目"
    with pytest.raises(ValueError):
        normalize_kb_id("../escape")
    with pytest.raises(ValueError):
        normalize_kb_id("a" * 97)


# ---------------------------------------------------------------------------
# initialize / index_status
# ---------------------------------------------------------------------------


def test_initialize_is_idempotent_and_yields_empty_index(make_service):
    service = make_service()
    service.initialize("owner-a")
    service.initialize("owner-a")
    status = service.index_status("owner-a")
    assert (status.page_count, status.source_count, status.parsed_source_count) == (0, 0, 0)


# ---------------------------------------------------------------------------
# ingest → 查询 / 检索 / 读取
# ---------------------------------------------------------------------------


def test_ingest_makes_content_queryable_searchable_and_readable(make_service):
    service = make_service(
        llm_script=[_analysis(entities=[_ENTITY], topics=[_TOPIC],
                              relationships=[{"source": "AgentRuntime", "target": "Wiki 设计", "relation": "uses"}])]
    )
    result = asyncio.run(
        service.ingest("src_contract", "owner-a", source_content="AgentRuntime 相关的正文内容。")
    )
    assert result.issues == []
    assert [page.page_type for page in result.pages] == ["source", "entity", "topic"]

    entity = result.pages[1]
    read = service.read_document(entity.id, "owner-a")
    assert read is not None and read.title == "AgentRuntime"

    assert entity.id in _page_ids(service.search_pages("AgentRuntime", "owner-a"))

    found = service.search("AgentRuntime", "owner-a")
    assert found["pages"] and entity.id in {page["id"] for page in found["pages"]}
    assert "retrieval" in found
    answered = service.query("AgentRuntime 是什么", "owner-a")
    assert answered["answer"] == ""
    assert {page["id"] for page in answered["pages"]} == {page["id"] for page in found["pages"]}

    status = service.index_status("owner-a")
    assert (status.page_count, status.source_count, status.parsed_source_count) == (3, 0, 0)
    assert entity.id in _page_ids(service.list_pages("owner-a"))


def test_ingest_progress_reports_stages_and_survives_callback_errors(make_service):
    service = make_service(llm_script=[_analysis()])
    stages: list[tuple[str, dict[str, Any]]] = []

    async def progress(stage: str, percent: int, detail: dict[str, Any]) -> None:
        stages.append((stage, dict(detail)))

    result = asyncio.run(
        service.ingest("src_prog", "owner-a", source_content="进度契约内容。", progress=progress)
    )
    assert result.issues == []
    names = [stage for stage, _ in stages]
    assert names[0] == "load" and names[-1] == "done"
    assert set(names) <= {"load", "analyze", "done"}
    assert names.index("load") < names.index("analyze") < names.index("done")
    assert stages[-1][1]["source_id"] == "src_prog"
    assert stages[-1][1]["page_count"] == 1

    async def broken_progress(stage: str, percent: int, detail: dict[str, Any]) -> None:
        raise RuntimeError("progress callback boom")

    result = asyncio.run(
        service.ingest(
            "src_bad_cb", "owner-a", source_content="回调抛错内容。", progress=broken_progress
        )
    )
    assert result.issues == []


def test_pre_cancelled_ingest_aborts_without_writes(make_service):
    service = make_service()
    cancel_event = asyncio.Event()
    cancel_event.set()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            service.ingest("src_cancel", "owner-a", source_content="取消契约内容。", cancel_event=cancel_event)
        )
    status = service.index_status("owner-a")
    assert (status.page_count, status.source_count) == (0, 0)


# ---------------------------------------------------------------------------
# 隔离性：owner 与 kb 两个维度
# ---------------------------------------------------------------------------


def test_owner_isolation(make_service):
    service = make_service(llm_script=[_analysis(entities=[_ENTITY])])
    result = asyncio.run(
        service.ingest("src_iso", "owner-a", source_content="owner 隔离内容。")
    )
    entity = result.pages[1]

    status_b = service.index_status("owner-b")
    assert (status_b.page_count, status_b.source_count) == (0, 0)
    assert service.search_pages("AgentRuntime", "owner-b") == []
    assert service.read_document(entity.id, "owner-b") is None
    assert service.list_pages("owner-b") == []
    assert service.list_sources("owner-b") == []


def test_kb_isolation_within_owner(make_service):
    service = make_service(llm_script=[_analysis(entities=[_ENTITY])])
    created = service.create_knowledge_base("project-a", "项目 A", "owner-a")
    assert created.id == "project-a" and created.name == "项目 A"

    result = asyncio.run(
        service.ingest("src_kb", "owner-a", source_content="KB 隔离内容。", kb_id="project-a")
    )
    entity = result.pages[1]

    assert service.index_status("owner-a", "default").page_count == 0
    # 1 个 source 页 + 1 个 entity 页。
    assert service.index_status("owner-a", "project-a").page_count == 2
    assert service.search_pages("AgentRuntime", "owner-a", kb_id="default") == []
    assert entity.id in _page_ids(service.search_pages("AgentRuntime", "owner-a", kb_id="project-a"))
    assert service.read_document(entity.id, "owner-a", "default") is None
    assert service.read_document(entity.id, "owner-a", "project-a") is not None


# ---------------------------------------------------------------------------
# 知识库生命周期与删除 fail 行为
# ---------------------------------------------------------------------------


def test_knowledge_base_lifecycle_and_delete_fail_behavior(make_service):
    service = make_service()

    listed_ids = {kb.id for kb in service.list_knowledge_bases("owner-a")}
    # 现状锁定：全新 owner 的列表只含惰性播种的教程库；"default" 要等
    # initialize/ingest 等写入路径首次触碰后才会出现在列表里。
    assert "tutorial" in listed_ids
    assert "default" not in listed_ids
    service.initialize("owner-a")
    assert "default" in {kb.id for kb in service.list_knowledge_bases("owner-a")}

    created = service.create_knowledge_base("notes", "笔记", "owner-a")
    assert isinstance(created, KnowledgeBase)
    assert created.id in {kb.id for kb in service.list_knowledge_bases("owner-a")}

    with pytest.raises(ValueError):
        service.create_knowledge_base("notes", "重复创建", "owner-a")
    with pytest.raises(ValueError):
        service.create_knowledge_base("default", "默认库不可创建", "owner-a")

    # fail 行为：默认/教程库受保护，不存在的库返回 False，均不抛错删除。
    with pytest.raises(ValueError):
        service.delete_knowledge_base("default", "owner-a")
    with pytest.raises(ValueError):
        service.delete_knowledge_base("tutorial", "owner-a")
    assert service.delete_knowledge_base("missing-kb", "owner-a") is False
    assert service.delete_knowledge_base("notes", "owner-a") is True
    assert "notes" not in {kb.id for kb in service.list_knowledge_bases("owner-a")}

    # 现状锁定：删除后读取不抛错，而是得到空库视图（任何读取都会惰性重建空目录）。
    status = service.index_status("owner-a", "notes")
    assert (status.page_count, status.source_count, status.parsed_source_count) == (0, 0, 0)
    assert service.read_document("any-page", "owner-a", "notes") is None
    assert service.list_pages("owner-a", "notes") == []


# ---------------------------------------------------------------------------
# 页面写入 / 更新 / 删除 / 列表
# ---------------------------------------------------------------------------


def _topic_page(title: str, content: str, **extra: Any) -> WikiPage:
    return WikiPage(id="", page_type="topic", title=title, content=content, file_path="", **extra)


def test_page_write_update_delete_lifecycle(make_service):
    service = make_service()

    saved = service.save_page(_topic_page("契约页", "# 契约页"), "owner-a")
    assert saved.id and saved.file_path

    read = service.read_document(saved.id, "owner-a")
    assert read is not None and read.title == "契约页" and read.content == "# 契约页"
    original_created_at = read.created_at

    updated = service.update_page(replace(saved, content="更新后的正文"), "owner-a")
    assert updated is not None
    read_again = service.read_document(saved.id, "owner-a")
    assert read_again is not None and read_again.content == "更新后的正文"
    assert read_again.created_at == original_created_at

    missing = service.update_page(
        WikiPage(id="ent-missing", page_type="entity", title="不存在", content="", file_path=""),
        "owner-a",
    )
    assert missing is None

    assert service.delete_page(saved.id, "owner-a") is True
    assert service.delete_page(saved.id, "owner-a") is False
    assert service.read_document(saved.id, "owner-a") is None


def test_list_pages_pagination_and_brief(make_service):
    service = make_service()
    for index in range(3):
        service.save_page(_topic_page(f"分页页{index}", f"正文 {index}"), "owner-a")

    full_ids = _page_ids(service.list_pages("owner-a"))
    assert len(full_ids) == 3
    assert len(service.list_pages("owner-a", limit=2)) == 2
    window = service.list_pages("owner-a", limit=1, offset=1)
    assert len(window) == 1 and window[0].id in full_ids

    brief = service.list_pages("owner-a", brief=True)
    assert _page_ids(brief) == full_ids
    # 现状锁定：brief 模式的 summary 恒为正文头部摘录（读取端重算），
    # 即使页面写入了显式 summary 也会被覆盖；完整模式的 summary 仍可读回。
    for page in brief:
        assert page.summary
    explicit = service.save_page(
        _topic_page("显式摘要页", "显式摘要正文", summary="显式摘要"), "owner-a"
    )
    assert service.read_document(explicit.id, "owner-a").summary == "显式摘要"


def test_related_pages_reports_outgoing_and_incoming_without_self(make_service):
    service = make_service()
    a = service.save_page(_topic_page("页面A", "A 正文"), "owner-a")
    b = service.save_page(
        _topic_page("页面B", "B 正文", relations=[WikiRelation(target_page_id=a.id, relation="uses")]),
        "owner-a",
    )
    c = service.save_page(
        _topic_page("页面C", "C 正文", relations=[WikiRelation(target_page_id=b.id, relation="related")]),
        "owner-a",
    )

    around_b = service.related_pages(b, "owner-a")
    by_id = {item.page.id: item for item in around_b}
    assert set(by_id) == {a.id, c.id}
    assert by_id[a.id].relation == "uses" and by_id[a.id].direction == "outgoing"
    assert by_id[c.id].relation == "related" and by_id[c.id].direction == "incoming"

    around_a = service.related_pages(a, "owner-a")
    assert [(item.page.id, item.direction) for item in around_a] == [(b.id, "incoming")]
    for item in service.related_pages(c, "owner-a"):
        assert item.page.id != c.id


# ---------------------------------------------------------------------------
# Vault 文档
# ---------------------------------------------------------------------------


def test_read_vault_document_after_initialize(make_service):
    service = make_service()
    assert service.read_vault_document("Home.md", "owner-a") is None

    service.initialize("owner-a")
    doc = service.read_vault_document("Home.md", "owner-a")
    assert doc is not None
    assert isinstance(doc, KnowledgeVaultDocument)
    assert doc.name == "Home.md" and doc.content
    assert isinstance(doc.updated_at, float) and doc.updated_at > 0
    assert service.read_vault_document("不存在.md", "owner-a") is None


# ---------------------------------------------------------------------------
# 上传 → 来源面（source_pages / source_files / delete_source）
# ---------------------------------------------------------------------------


def test_upload_then_source_surface_roundtrip(make_service):
    service = make_service(llm_script=[_analysis(entities=[_ENTITY])])
    payload = "meeting notes body 契约测试".encode("utf-8")

    uploaded = asyncio.run(service.upload_file("meeting-notes.txt", payload, "owner-a"))
    assert isinstance(uploaded, KnowledgeUploadResult)
    # 现状锁定：upload_file 只解析登记，不触发 ingest（ingested=False）。
    assert uploaded.error == "" and uploaded.ingested is False
    assert uploaded.needs_confirmation is False and uploaded.needs_agent_review is False
    assert uploaded.source_id.startswith("upload_")

    status = service.index_status("owner-a")
    assert (status.source_count, status.parsed_source_count) == (1, 1)
    assert status.page_count == 0

    raw = service.read_source(uploaded.source_id, "owner-a")
    assert raw is not None and raw.title == "meeting-notes.txt" and raw.parse_status == "parsed"
    assert raw.id in {item.id for item in service.list_sources("owner-a")}

    ingested = asyncio.run(service.ingest(uploaded.source_id, "owner-a"))
    assert ingested.issues == []
    source_page = ingested.pages[0]
    assert source_page.page_type == "source"

    pages = service.source_pages(uploaded.source_id, "owner-a")
    assert source_page.id in _page_ids(pages)
    titles = service.source_titles([uploaded.source_id], "owner-a")
    assert titles[uploaded.source_id] == "meeting-notes.txt"

    files = service.source_files(source_page, "owner-a")
    located = files[uploaded.source_id]
    assert isinstance(located, KnowledgeSourceFile)
    assert Path(located.location).is_file()
    assert Path(located.location).read_bytes() == payload

    direct = service.source_file(uploaded.source_id, "owner-a")
    assert direct is not None and direct.content == payload
    assert direct.title == "meeting-notes.txt"

    assert service.source_file("upload_missing", "owner-a") is None
    assert service.delete_source(uploaded.source_id, "owner-a") is True
    assert service.read_source(uploaded.source_id, "owner-a") is None
    assert service.delete_source(uploaded.source_id, "owner-a") is False


def test_upload_image_with_multimodal_disabled_is_rejected_cleanly(make_service):
    config = WikiConfig(multimodal=WikiMultimodalConfig(enabled=False))
    service = make_service(config=config)
    uploaded = asyncio.run(service.upload_file("photo.png", b"png-bytes", "owner-a"))
    assert uploaded.error_code == "MULTIMODAL_DISABLED"
    assert uploaded.ingested is False
    assert service.list_sources("owner-a") == []


# ---------------------------------------------------------------------------
# capture_text / capture_attachment
# ---------------------------------------------------------------------------


def test_capture_text_publishes_page_and_deduplicates(make_service):
    service = make_service()
    body = "周期性同步的纪要内容，用于契约去重验证。"

    first = service.capture_text(title="会议纪要", content=body, owner_account_id="owner-a")
    assert first.duplicate is None
    assert first.page is not None
    assert first.raw.parse_status == "parsed"
    assert first.raw.id in first.page.sources

    second = service.capture_text(title="会议纪要二", content=body, owner_account_id="owner-a")
    assert second.page is None
    assert second.duplicate is not None and second.duplicate.id == first.raw.id

    with pytest.raises(CaptureValidationError):
        service.capture_text(title=" ", content=body, owner_account_id="owner-a")
    with pytest.raises(CaptureValidationError):
        service.capture_text(title="标题", content=" ", owner_account_id="owner-a")


def test_capture_attachment_registers_source(make_service):
    service = make_service()
    raw = asyncio.run(
        service.capture_attachment("inline-note.txt", "附件正文".encode("utf-8"), "owner-a")
    )
    assert raw is not None
    assert raw.id.startswith("upload_") and raw.title == "inline-note.txt"
    assert service.read_source(raw.id, "owner-a") is not None


# ---------------------------------------------------------------------------
# compile_all / lint / graph
# ---------------------------------------------------------------------------


async def test_compile_all_lint_and_graph_contracts(make_service):
    service = make_service(llm_script=[_analysis(entities=[_ENTITY]), _analysis(entities=[_ENTITY])])
    uploaded = await service.upload_file("compile-src.txt", b"compile contract body", "owner-a")
    first = await service.ingest(uploaded.source_id, "owner-a")
    assert first.issues == []
    entity = first.pages[1]

    compiled = await service.compile_all("owner-a")
    assert isinstance(compiled, CompileResult)
    assert compiled.errors == []
    assert compiled.ingested == [uploaded.source_id]

    issues = await service.lint("owner-a")
    assert all(
        isinstance(item, dict) and {"kind", "page_id", "message"} <= set(item) for item in issues
    )

    graph = await service.graph("owner-a")
    assert isinstance(graph, WikiGraph)
    assert entity.id in {node["id"] for node in graph.nodes}


# ---------------------------------------------------------------------------
# 会话面（session_kb_id / cancel_confirmation）
# ---------------------------------------------------------------------------


def test_session_surface_defaults(make_service):
    service = make_service()
    assert service.session_kb_id("unknown-session", "owner-a") == "default"
    assert service.cancel_confirmation("unknown-session", "missing-confirmation", "owner-a") is False


# ---------------------------------------------------------------------------
# 序列化边界：协议 DTO 必须传输无关
# ---------------------------------------------------------------------------


def _strict_json(value: Any) -> str:
    """不带 default 的严格序列化；出现非 JSON 原生类型会抛 TypeError。"""
    return json.dumps(value, ensure_ascii=False)


def _roundtrip(value: Any) -> Any:
    """协议约定的传输方式：json.dumps(default=str) → loads。"""
    return json.loads(_strict_json_with_default(value))


def _strict_json_with_default(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


def test_knowledge_index_status_is_transport_clean():
    status = KnowledgeIndexStatus(page_count=3, source_count=1, parsed_source_count=1)
    assert json.loads(_strict_json(asdict(status))) == {
        "page_count": 3, "source_count": 1, "parsed_source_count": 1,
    }


def test_knowledge_vault_document_is_transport_clean():
    doc = KnowledgeVaultDocument(name="Home.md", content="# 首页", updated_at=1726000000.0)
    data = json.loads(_strict_json(asdict(doc)))
    assert data == {"name": "Home.md", "content": "# 首页", "updated_at": 1726000000.0}


def test_knowledge_base_roundtrip_keeps_all_fields():
    kb = KnowledgeBase(
        id="notes", name="笔记", created_at=1.0, updated_at=2.0, vault_path="/vault/notes"
    )
    assert KnowledgeBase.from_dict(json.loads(_strict_json(kb.to_dict()))) == kb


def test_wiki_page_rich_payload_is_transport_clean():
    page = WikiPage(
        id="ent-1", page_type="entity", title="AgentRuntime", content="正文",
        file_path="wiki/entities/AgentRuntime.md",
        sources=["src-1"], related=[], tags=["运行时"], aliases=["Runtime"],
        created_at=1.5, updated_at=2.5, summary="摘要",
        claims=[WikiClaim(
            statement="主张",
            evidence=[WikiEvidence(source_id="src-1", locator="第 1 段", excerpt="原文")],
            confidence="high", contested=False, contradictions=["反例"],
        )],
        confidence="high", contested=False, contradictions=["反例"],
        relations=[WikiRelation(target_page_id="top-1", relation="uses")],
        stale=True,
    )
    data = json.loads(_strict_json(asdict(page)))
    assert data["id"] == "ent-1" and data["summary"] == "摘要" and data["stale"] is True
    assert data["claims"][0]["evidence"][0]["source_id"] == "src-1"
    assert data["relations"][0]["target_page_id"] == "top-1"
    assert data["created_at"] == 1.5 and data["updated_at"] == 2.5


def test_wiki_page_to_dict_mode_asymmetry_is_locked():
    """现状锁定：to_dict 完整模式不含 summary，brief 模式才带 summary/claim_count。

    远程传输若依赖 to_dict，消费方拿不到完整模式的 summary——这是序列化边
    界的不对称点，迁移远程 Provider 前需要明确契约。
    """
    page = WikiPage(
        id="ent-1", page_type="entity", title="T", content="C", file_path="p.md",
        summary="摘要", claims=[WikiClaim(statement="主张")],
    )
    full = page.to_dict()
    assert "summary" not in full and "claim_count" not in full
    assert full["content"] == "C"
    assert len(full["claims"]) == 1

    brief = page.to_dict(brief=True)
    assert "content" not in brief
    assert brief["summary"] == "摘要" and brief["claim_count"] == 1

    # to_dict → from_dict 在完整模式下会丢失 summary 与空 claims。
    restored = WikiPage.from_dict(full)
    assert restored.id == page.id and restored.content == page.content
    assert restored.summary is None


def test_wiki_page_relation_is_transport_clean():
    relation = WikiPageRelation(
        page=WikiPage(id="ent-1", page_type="entity", title="T", content="C", file_path="p.md"),
        relation="uses", direction="outgoing",
    )
    data = json.loads(_strict_json(asdict(relation)))
    assert data["relation"] == "uses" and data["direction"] == "outgoing"
    assert data["page"]["id"] == "ent-1"


def test_ingest_result_is_transport_clean():
    result = IngestResult(
        source_id="src-1",
        pages=[WikiPage(id="ent-1", page_type="entity", title="T", content="C", file_path="p.md")],
        issues=[],
    )
    data = json.loads(_strict_json(result.to_dict()))
    assert data["source_id"] == "src-1" and data["pages"][0]["id"] == "ent-1"


def test_raw_source_roundtrip_keeps_all_fields():
    raw = RawSource(
        id="upload_abc", title="a.txt", source_type="upload", parsed_path="parsed.md",
        original_path="raw/a.txt", file_type="text/plain", size=10,
        created_at=1726000000.0, parse_status="parsed", source_kind="note",
        source_platform="local", adapter_name="builtin-file", original_ref="a.txt",
        summary="摘要", tags=["标签"], doc_type="note",
        ingest_recommend=True, ingest_reason="值得入库", ingest_status="recommended",
    )
    restored = RawSource.from_dict(json.loads(_strict_json(raw.to_dict())))
    assert restored == raw


def test_compile_result_and_graph_are_transport_clean():
    compiled = CompileResult(ingested=["src-1"], errors=[])
    assert json.loads(_strict_json(compiled.to_dict())) == {"ingested": ["src-1"], "errors": []}

    graph = WikiGraph(nodes=[{"id": "ent-1", "title": "T", "type": "entity"}], edges=[])
    assert json.loads(_strict_json(graph.to_dict()))["nodes"][0]["id"] == "ent-1"


def test_knowledge_upload_result_tuple_fields_arrive_as_json_arrays():
    """现状锁定：pages/issues 在 DTO 中是 tuple，经 JSON 传输后变成 list。"""
    result = KnowledgeUploadResult(
        source_id="upload_abc", title="a.txt", source_type="upload",
        pages=(WikiPage(id="ent-1", page_type="entity", title="T", content="C", file_path="p.md"),),
        issues=("警告",),
    )
    data = json.loads(_strict_json(asdict(result)))
    assert isinstance(data["pages"], list) and isinstance(data["issues"], list)
    assert data["pages"][0]["id"] == "ent-1" and data["issues"] == ["警告"]


def test_knowledge_source_file_bytes_shape_is_locked():
    """现状锁定：KnowledgeSourceFile.content 是 bytes，无法无损 JSON round-trip。

    严格 json.dumps 直接抛 TypeError；按协议约定 default=str 后退化为
    ``b'...'`` 的 Python repr 字符串，远端无法还原原始字节。远程化前需要
    引入 base64 等显式编码，或在协议层拆分「元数据」与「内容」两个通道。
    """
    with_bytes = KnowledgeSourceFile(
        location="vault/raw/a.txt", title="a.txt", file_type="text/plain", content=b"payload"
    )
    with pytest.raises(TypeError):
        _strict_json(asdict(with_bytes))

    data = _roundtrip(asdict(with_bytes))
    assert data["content"] == str(b"payload")
    assert isinstance(data["content"], str) and data["title"] == "a.txt"

    metadata_only = KnowledgeSourceFile(location="", title="a.txt", file_type="text/plain")
    restored = json.loads(_strict_json(asdict(metadata_only)))
    assert restored["content"] is None


def test_provider_returned_payloads_are_transport_clean(make_service):
    """端到端：真实 Provider 产出的协议负载全部可以严格 JSON 序列化。"""
    service = make_service(llm_script=[_analysis(entities=[_ENTITY])])
    created = service.create_knowledge_base("notes", "笔记", "owner-a")
    _strict_json(created.to_dict())
    _strict_json(asdict(service.index_status("owner-a", "notes")))

    result = asyncio.run(
        service.ingest("src_json", "owner-a", source_content="传输无关性验证内容。", kb_id="notes")
    )
    _strict_json(result.to_dict())
    found = service.search("AgentRuntime", "owner-a", kb_id="notes")
    _strict_json(found)
    _strict_json(asdict(service.read_document(result.pages[1].id, "owner-a", "notes")))

    service.initialize("owner-a", "notes")
    _strict_json(asdict(service.read_vault_document("Home.md", "owner-a", "notes")))

    issues = asyncio.run(service.lint("owner-a", "notes"))
    _strict_json(issues)
    graph = asyncio.run(service.graph("owner-a", "notes"))
    _strict_json(graph.to_dict())
