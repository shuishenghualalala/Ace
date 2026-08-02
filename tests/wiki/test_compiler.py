"""WikiCompiler 单元测试。

使用 FakeProvider 模拟 LLM 分析结果，避免依赖真实模型 key。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from crew.core.mocks import FakeProvider
from crew.core.types import ChatResponse
from crew.wiki.compiler import WikiCompiler
from crew.wiki.schemas import RawSource, WikiPage
from crew.wiki.store import FileSystemWikiStore


def _analysis_response(payload: dict[str, Any]) -> ChatResponse:
    """构造 FakeProvider 返回的 LLM 分析结果。"""
    return ChatResponse(text=json.dumps(payload, ensure_ascii=False))


@pytest.fixture
def store(tmp_path: Path) -> FileSystemWikiStore:
    return FileSystemWikiStore(base_dir=tmp_path)


@pytest.fixture
def compiler(store: FileSystemWikiStore) -> WikiCompiler:
    return WikiCompiler(store=store, provider=FakeProvider())


def test_publish_source_page_is_fast_and_searchable_without_llm(store, compiler):
    raw = RawSource(
        id="src_fast",
        title="快速来源",
        source_type="upload",
        parsed_path="",
        source_kind="pdf",
    )
    store.save_raw(raw)
    raw.parsed_path = store.save_parsed_markdown("src_fast", "这是无需 LLM 的全文来源内容")
    raw.parse_status = "parsed"
    store.save_raw(raw)

    page = compiler.publish_source_page("src_fast")

    assert page.page_type == "source"
    assert page.file_path == "wiki/sources/pdfs/快速来源.md"
    assert page.summary == "这是无需 LLM 的全文来源内容"
    assert "## 来源信息" in page.content
    assert "这是无需 LLM 的全文来源内容" in page.content
    assert store.get_by_title("快速来源") is not None
    assert any(item.id == page.id for item in store.search("全文来源"))


def test_publish_source_page_moves_existing_summary_into_source_kind_directory(
    store,
    compiler,
):
    raw = RawSource(
        id="src_move",
        title="移动来源",
        source_type="paste",
        parsed_path="",
        source_kind="note",
    )
    store.save_raw(raw)
    raw.parsed_path = store.save_parsed_markdown(raw.id, "来源正文")
    store.save_raw(raw)
    first = compiler.publish_source_page(raw.id)
    old_path = store._dir() / first.file_path
    assert first.file_path == "wiki/sources/notes/移动来源.md"

    raw.source_kind = "article"
    store.save_raw(raw)
    moved = compiler.publish_source_page(raw.id)

    assert moved.file_path == "wiki/sources/articles/移动来源.md"
    assert not old_path.exists()
    assert (store._dir() / moved.file_path).is_file()
    assert store.get(moved.id) is not None


@pytest.mark.asyncio
async def test_short_ingest_creates_source_summary_and_entities_only(store, compiler):
    source_content = "这是关于 Crew 多智能体调用平台的原始文档内容。"
    analysis = {
        "source_summary": {
            "one_sentence": "Crew 是一个多智能体调用平台。",
            "core_points": ["提供 Agent 运行时", "支持 Wiki 知识整理"],
        },
        "entities": [
            {"name": "AgentRuntime", "description": "Agent 运行时。"},
            {
                "name": "ModeManager",
                "description": "模式管理器抽象。",
                "entity_kind": "concept",
            },
        ],
        "topics": [
            {
                "name": "Wiki 设计",
                "description": "Wiki 模块设计思路。",
                "summary": "记录 Wiki 相关设计。",
            },
        ],
        "relationships": [
            {"source": "AgentRuntime", "target": "ModeManager", "relation": "uses"},
        ],
    }
    compiler.provider = FakeProvider(script=[_analysis_response(analysis)])
    compiler.summarizer = MagicMock()
    compiler.summarizer.maybe_refresh = AsyncMock(
        side_effect=AssertionError("ingest 不得隐式触发第二次 LLM 摘要")
    )

    store.save_raw(
        RawSource(
            id="src_1",
            title="原始文档",
            source_type="paste",
            parsed_path="",
        )
    )

    result = await compiler.ingest("src_1", source_content=source_content)

    assert not result.issues
    compiler.summarizer.maybe_refresh.assert_not_awaited()
    assert store.get_kb_summary().status == "stale"
    assert len(result.pages) == 3
    titles = {p.title for p in result.pages}
    assert "原始文档" in titles
    assert "AgentRuntime" in titles
    assert "ModeManager" in titles
    assert "Wiki 设计" not in titles

    # source 页面保存原始内容
    source_page = store.get_by_title("原始文档")
    assert source_page is not None
    assert source_page.page_type == "source"
    assert "## 核心观点" in source_page.content
    assert "## 关键词" in source_page.content
    assert "1. 提供 Agent 运行时" in source_page.content
    assert "[[AgentRuntime]]" in source_page.content
    assert source_page.summary == "Crew 是一个多智能体调用平台。"
    assert source_content in source_page.content

    # entity 页面保存到 store
    entity_page = store.get_by_title("AgentRuntime")
    assert entity_page is not None
    assert entity_page.page_type == "entity"
    # relationship 被应用
    mode_page = store.get_by_title("ModeManager")
    assert mode_page is not None
    assert entity_page.related == []
    assert any(
        relation.target_page_id == mode_page.id and relation.relation == "uses"
        for relation in entity_page.relations
    )

    # index.md 已更新
    index_text = (store._dir() / "index.md").read_text(encoding="utf-8")
    assert "AgentRuntime" in index_text


@pytest.mark.asyncio
async def test_ingest_with_source_content_no_raw_file(store, compiler):
    analysis = {
        "entities": [],
        "topics": [],
        "relationships": [],
    }
    compiler.provider = FakeProvider(script=[_analysis_response(analysis)])

    result = await compiler.ingest("missing_src", source_content="一些内容")

    assert not result.issues
    assert len(result.pages) == 1
    assert result.pages[0].title == "missing_src"
    assert "一些内容" in result.pages[0].content


@pytest.mark.asyncio
async def test_ingest_returns_issue_when_source_missing_and_no_content(store, compiler):
    result = await compiler.ingest("missing_src")
    assert result.issues
    assert "missing_src 不存在" in result.issues[0]
    assert not result.pages


@pytest.mark.asyncio
async def test_ingest_handles_invalid_json_gracefully(store, compiler):
    compiler.provider = FakeProvider(script=[ChatResponse(text="not valid json")])
    store.save_raw(
        RawSource(
            id="src_bad",
            title="Bad",
            source_type="paste",
            parsed_path="",
        )
    )

    result = await compiler.ingest("src_bad", source_content="bad content")

    assert result.issues
    assert "LLM 分析失败" in result.issues[0]


@pytest.mark.asyncio
async def test_plan_capacity_failure_preserves_parsed_source_for_retry(store, compiler):
    raw = RawSource(
        id="src_capacity",
        title="已解析长文",
        source_type="upload",
        parsed_path="",
        parse_status="parsed",
    )
    store.save_raw(raw)
    raw.parsed_path = store.save_parsed_markdown("src_capacity", "已成功保存的 Markdown 正文")
    store.save_raw(raw)
    compiler._analyze = AsyncMock(
        return_value={
            "_chunk_failed": True,
            "entities": [],
            "topics": [],
            "relationships": [],
        }
    )

    plan = await compiler.plan_ingest("src_capacity")

    assert any("已解析内容仍保留" in issue for issue in plan.issues)
    saved = store.load_raw("src_capacity")
    assert saved is not None
    assert saved.parse_status == "parsed"
    assert Path(saved.parsed_path).read_text(encoding="utf-8") == "已成功保存的 Markdown 正文"


@pytest.mark.asyncio
async def test_ingest_strips_markdown_code_fences(store, compiler):
    analysis = {
        "entities": [],
        "topics": [],
        "relationships": [],
    }
    compiler.provider = FakeProvider(
        script=[ChatResponse(text=f"```json\n{json.dumps(analysis)}\n```")]
    )
    store.save_raw(
        RawSource(id="src_fenced", title="Fenced", source_type="paste", parsed_path="")
    )

    result = await compiler.ingest("src_fenced", source_content="原始内容")

    assert not result.issues
    assert result.pages[0].title == "Fenced"
    assert "原始内容" in result.pages[0].content


@pytest.mark.asyncio
async def test_compile_all_recompiles_all_raw_sources(store, compiler):
    analysis = {
        "entities": [],
        "topics": [],
        "relationships": [],
    }
    compiler.provider = FakeProvider(
        script=[_analysis_response(analysis), _analysis_response(analysis)]
    )

    for i in range(2):
        raw = RawSource(
            id=f"src_{i}", title=f"Raw {i}", source_type="paste", parsed_path=""
        )
        store.save_raw(raw)
        raw.parsed_path = store.save_parsed_markdown(raw.id, f"Raw {i} content")
        raw.parse_status = "parsed"
        store.save_raw(raw)

    result = await compiler.compile_all()

    assert len(result.ingested) == 2
    assert not result.errors
    pages = store.list_all()
    assert len(pages) == 2


@pytest.mark.asyncio
async def test_repeated_ingest_updates_source_page(store, compiler):
    analysis = {
        "entities": [],
        "topics": [],
        "relationships": [],
    }
    compiler.provider = FakeProvider(
        script=[_analysis_response(analysis), _analysis_response(analysis)]
    )
    store.save_raw(
        RawSource(id="src_repeat", title="重复源", source_type="paste", parsed_path="")
    )

    await compiler.ingest("src_repeat", source_content="第一段内容。")
    await compiler.ingest("src_repeat", source_content="第二段内容。")

    pages = store.list_all()
    assert len(pages) == 1
    # 当前 source 页面会按最新内容覆盖
    assert "第二段内容" in pages[0].content
    assert "第一段内容" not in pages[0].content


@pytest.mark.asyncio
async def test_lint_returns_issue_dicts(store, compiler):
    store.save_page(
        WikiPage(
            id="p1",
            page_type="topic",
            title="孤立页",
            content="# 孤立页\n\n正文无链接。",
            file_path="topics/孤立页.md",
        )
    )
    issues = await compiler.lint()
    orphan = next(issue for issue in issues if issue["kind"] == "orphan")
    assert orphan["page_id"] == "p1"
    assert any(issue["kind"] == "index_drift" for issue in issues)


@pytest.mark.asyncio
async def test_lint_deep_calls_llm_for_contradiction_and_gap(store, compiler):
    store.save_page(
        WikiPage(
            id="p_a",
            page_type="entity",
            title="概念A",
            content="# 概念A\n\n属性值是 1。",
            file_path="entities/概念A.md",
        )
    )
    store.save_page(
        WikiPage(
            id="p_b",
            page_type="entity",
            title="概念B",
            content="# 概念B\n\n概念A 的属性值是 2。",
            file_path="entities/概念B.md",
        )
    )
    llm_response = [
        {
            "kind": "contradiction",
            "page_id": "p_a",
            "message": "概念A 属性值陈述矛盾",
            "details": {"target": "概念A", "other_page_id": "p_b"},
        },
        {
            "kind": "entity_gap",
            "page_id": "p_b",
            "message": "反复提到 概念X 但无独立页面",
            "details": {"target": "概念X"},
        },
    ]
    compiler.provider = FakeProvider(script=[ChatResponse(text=json.dumps(llm_response, ensure_ascii=False))])

    issues = await compiler.lint(deep=True)
    kinds = {i["kind"] for i in issues}
    assert "contradiction" in kinds
    assert "entity_gap" in kinds


@pytest.mark.asyncio
async def test_lint_deep_skips_llm_when_single_page(store, compiler):
    store.save_page(
        WikiPage(
            id="p_only",
            page_type="topic",
            title="唯一页",
            content="# 唯一页\n\n无矛盾对象。",
            file_path="topics/唯一页.md",
        )
    )
    issues = await compiler.lint(deep=True)
    assert all(i["kind"] != "contradiction" for i in issues)
    assert all(i["kind"] != "entity_gap" for i in issues)


@pytest.mark.asyncio
async def test_orient_returns_kb_snapshot(store, compiler):
    store.save_page(
        WikiPage(id="p_a", page_type="topic", title="页面A", content="# 页面A\n\n正文", file_path="topics/页面A.md")
    )
    store.save_page(
        WikiPage(id="p_b", page_type="entity", title="概念B", content="# 概念B\n\n正文", file_path="entities/概念B.md", aliases=["B"])
    )

    orientation = await compiler.orient()
    assert orientation.kb_id == "default"
    assert orientation.index["page_count"] == 2
    assert orientation.stats["by_type"]["topic"] == 1
    assert orientation.stats["by_type"]["entity"] == 1
    assert "页面A" in orientation.candidate_index["title_to_id"]
    assert "B" in orientation.candidate_index["alias_to_id"]


@pytest.mark.asyncio
async def test_orient_appends_log_after_ingest(store, compiler):
    analysis = {
        "entities": [],
        "topics": [],
        "relationships": [],
    }
    compiler.provider = FakeProvider(script=[_analysis_response(analysis)])
    store.save_raw(RawSource(id="src_log", title="日志测试源", source_type="paste", parsed_path=""))

    await compiler.ingest("src_log", source_content="测试内容")

    orientation = await compiler.orient()
    assert any("src_log" in " ".join(entry["messages"]) for entry in orientation.recent_log)


@pytest.mark.asyncio
async def test_ingest_reports_duplicate_source(store, compiler):
    analysis = {
        "entities": [],
        "topics": [],
        "relationships": [],
    }
    compiler.provider = FakeProvider(script=[_analysis_response(analysis)])
    store.save_raw(
        RawSource(id="src_dup_1", title="源A", source_type="paste", parsed_path="", content_sha256="samehash")
    )
    store.save_raw(
        RawSource(id="src_dup_2", title="源B", source_type="paste", parsed_path="", content_sha256="samehash")
    )

    result = await compiler.ingest("src_dup_2", source_content="测试内容")
    assert any("重复" in issue for issue in result.issues)


@pytest.mark.asyncio
async def test_ingest_progress_callback_reports_stages(store, compiler):
    """progress callback 应按顺序收到各阶段事件，且包含预期百分比。"""
    analysis = {
        "entities": [
            {"name": "E1", "description": "实体 1"},
            {"name": "C1", "description": "概念 1", "entity_kind": "concept"},
        ],
        "topics": [{"name": "T1", "description": "主题 1", "summary": "摘要"}],
        "relationships": [],
    }
    compiler.provider = FakeProvider(script=[_analysis_response(analysis)])
    store.save_raw(RawSource(id="src_prog", title="Progress", source_type="paste", parsed_path=""))

    stages: list[tuple[str, int, dict[str, Any]]] = []

    async def progress(stage: str, percent: int, detail: dict[str, Any]) -> None:
        stages.append((stage, percent, detail))

    result = await compiler.ingest("src_prog", source_content="内容", progress=progress)

    assert not result.issues
    # 进度协议已简化为 load → analyze（平滑推进） → done 三个阶段。
    seen_stages = [s[0] for s in stages]
    for expected in ("load", "analyze", "done"):
        assert expected in seen_stages, f"缺少阶段 {expected}"

    # 百分比应单调非降
    for prev, cur in zip(stages[:-1], stages[1:]):
        assert cur[1] >= prev[1], f"百分比不应下降: {prev[0]}({prev[1]}) -> {cur[0]}({cur[1]})"

    # 关键阶段百分比符合映射；analyze 在 LLM 分析期间会平滑推进到 99%，
    # 因此只检查起点与上限，不强制等于 10。
    percent_by_stage = {s[0]: s[1] for s in stages}
    assert percent_by_stage["load"] == 5
    assert 10 <= percent_by_stage["analyze"] <= 99
    assert percent_by_stage["done"] == 100


@pytest.mark.asyncio
async def test_ingest_progress_callback_silent_on_exception(store, compiler):
    """progress callback 抛异常时不应中断 ingest。"""
    analysis = {
        "entities": [],
        "topics": [],
        "relationships": [],
    }
    compiler.provider = FakeProvider(script=[_analysis_response(analysis)])
    store.save_raw(RawSource(id="src_broken_cb", title="BrokenCB", source_type="paste", parsed_path=""))

    async def bad_progress(_stage: str, _percent: int, _detail: dict[str, Any]) -> None:
        raise RuntimeError("callback error")

    result = await compiler.ingest("src_broken_cb", source_content="内容", progress=bad_progress)

    assert not result.issues
    assert len(result.pages) == 1


@pytest.mark.asyncio
async def test_apply_ingest_uses_saved_plan_and_respects_approved_titles(store, compiler):
    """apply_ingest 应读取 plan_ingest 保存的计划，并按 approved_titles 过滤。"""
    source_content = "文档涉及 EntityA 和 EntityB。"
    analysis = {
        "entities": [
            {"name": "EntityA", "description": "实体 A 描述"},
            {"name": "EntityB", "description": "实体 B 描述"},
        ],
        "topics": [],
        "relationships": [],
    }
    compiler.provider = FakeProvider(script=[_analysis_response(analysis)])

    store.save_raw(
        RawSource(
            id="src_plan",
            title="计划文档",
            source_type="paste",
            parsed_path="",
        )
    )
    store.save_parsed_markdown("src_plan", source_content)

    plan = await compiler.plan_ingest("src_plan")
    assert plan.total_new == 3  # source + 两个 entity

    # 只批准 EntityA，source 页面也会自动写入
    result = await compiler.apply_ingest("src_plan", approved_titles=["EntityA"])

    assert not result.issues
    titles = {p.title for p in result.pages}
    assert "EntityA" in titles
    assert "EntityB" not in titles
    assert "计划文档" in titles  # source 页面始终写入

    assert store.get_by_title("EntityB") is None


@pytest.mark.asyncio
async def test_apply_ingest_rejects_when_plan_missing(store, compiler):
    """未找到 plan 文件时不得绕过 plan/apply 契约直接写入。"""
    source_content = "文档涉及 EntityA。"
    analysis = {
        "entities": [{"name": "EntityA", "description": "实体 A 描述"}],
        "topics": [],
        "relationships": [],
    }
    compiler.provider = FakeProvider(script=[_analysis_response(analysis)])

    store.save_raw(
        RawSource(
            id="src_fallback",
            title="回退文档",
            source_type="paste",
            parsed_path="",
        )
    )
    store.save_parsed_markdown("src_fallback", source_content)

    result = await compiler.apply_ingest("src_fallback")

    assert result.pages == []
    assert result.issues == ["未找到 ingest 计划；请先重新调用 wiki_plan_ingest"]


@pytest.mark.asyncio
async def test_apply_ingest_rejects_stale_source_content(store, compiler):
    raw = RawSource(
        id="src_stale",
        title="版本化文档",
        source_type="paste",
        parsed_path="",
    )
    store.save_raw(raw)
    store.save_parsed_markdown(raw.id, "第一版内容")
    compiler.provider = FakeProvider(
        script=[_analysis_response({"entities": [], "topics": [], "relationships": []})]
    )
    plan = await compiler.plan_ingest(raw.id)
    assert plan.source_content_sha256

    store.save_parsed_markdown(raw.id, "第二版内容")
    result = await compiler.apply_ingest(raw.id)

    assert result.pages == []
    assert any("计划已过期" in issue for issue in result.issues)


@pytest.mark.asyncio
async def test_merge_content_dedup_paragraphs():
    """_merge_content 应跳过已存在的段落，避免机械追加导致页面膨胀。"""
    from crew.wiki.compiler import _merge_content

    existing = "# EntityA\n\n这是描述。\n\n---\n\n补充信息。"
    new = "这是描述。\n\n全新信息。"
    merged = _merge_content(existing, new)

    assert "全新信息" in merged
    assert merged.count("这是描述") == 1


def test_split_into_semantic_chunks():
    """_split_into_semantic_chunks 应按 Markdown 标题切分，并保持顺序。"""
    from crew.wiki.compiler import _split_into_semantic_chunks

    content = "# 前言\n\n前言内容。\n\n# 第一章\n\n第一章内容。\n\n# 第二章\n\n第二章内容。\n\n# 结语\n\n结语内容。"
    chunks = _split_into_semantic_chunks(content, max_size=15)

    # 每个章节应独立成块
    assert len(chunks) == 4
    # 标题切分应保持相对顺序
    assert chunks[0].startswith("# 前言")
    assert chunks[-1].startswith("# 结语")
    # 每个块不应超过 max_size
    assert all(len(c) <= 15 for c in chunks)


def test_split_into_semantic_chunks_no_split_when_short():
    """短文档不应被无意义切分。"""
    from crew.wiki.compiler import _split_into_semantic_chunks

    content = "# 短文档\n\n内容。"
    chunks = _split_into_semantic_chunks(content, max_size=10_000)
    assert chunks == [content]


def test_merge_analysis_results():
    """_merge_analysis_results 应按名称去重并合并描述。"""
    from crew.wiki.compiler import _merge_analysis_results

    results = [
        {
            "entities": [
                {"name": "AgentRuntime", "description": "Agent 运行时。", "aliases": ["Runtime"]},
                {"name": "ModeManager", "description": "模式管理器。", "entity_kind": "concept"},
            ],
            "topics": [
                {"name": "Wiki 设计", "description": "设计思路。", "summary": "记录设计。"},
            ],
            "relationships": [
                {"source": "AgentRuntime", "target": "ModeManager", "relation": "uses"},
            ],
        },
        {
            "entities": [
                {"name": "agentruntime", "description": "负责执行 Agent 回合。", "aliases": ["AR"]},
                {"name": "ModeManager", "description": "管理 Plan/Wiki 模式切换。", "entity_kind": "concept"},
            ],
            "topics": [
                {"name": "Wiki 设计", "description": "模块设计。", "summary": "", "decisions": ["使用 Markdown"]},
            ],
            "relationships": [
                {"source": "AgentRuntime", "target": "ModeManager", "relation": "uses"},
                {"source": "ModeManager", "target": "Wiki 设计", "relation": "mentions"},
            ],
        },
    ]

    merged = _merge_analysis_results(results)

    assert len(merged["entities"]) == 2
    entity = next(item for item in merged["entities"] if item["name"].lower() == "agentruntime")
    assert "Agent 运行时" in entity["description"]
    assert "负责执行 Agent 回合" in entity["description"]
    assert set(entity["aliases"]) == {"Runtime", "AR"}

    mode_entity = next(item for item in merged["entities"] if item["name"] == "ModeManager")
    assert "模式管理器" in mode_entity["description"]
    assert "管理 Plan/Wiki 模式切换" in mode_entity["description"]

    assert len(merged["topics"]) == 1
    topic = merged["topics"][0]
    assert "设计思路" in topic["description"]
    assert "模块设计" in topic["description"]
    assert "记录设计" in topic["summary"]
    assert "使用 Markdown" in topic.get("decisions", [])

    # 重复关系只保留一条
    assert len(merged["relationships"]) == 2


def test_load_analysis_json_salvages_complete_typed_units_from_truncated_output():
    from crew.wiki.compiler import _load_analysis_json

    truncated = (
        '{"format":"knowledge-units-v5",'
        '"entities":[{"subject":"A","statement":"完整主张 A"},'
        '{"subject":"B","statement":"完整主张 B"}],'
        '"topics":[{"subject":"C","statement":"未完成'
    )

    parsed = _load_analysis_json(truncated)

    assert parsed is not None
    assert [unit["subject"] for unit in parsed["entities"]] == ["A", "B"]
    assert parsed["topics"] == []
    assert parsed["_truncated"] is True
    assert "保留 2 个完整知识单元" in parsed["_analysis_warnings"][0]


def test_merge_analysis_resolves_cross_type_subject_collision():
    from crew.wiki.compiler import _merge_analysis_results

    merged = _merge_analysis_results(
        [
            {
                "format": "knowledge-units-v5",
                "entities": [
                    {
                        "subject": "RAG",
                        "statement": "RAG 是一个具体实现名称",
                    },
                    {
                        "subject": "RAG",
                        "statement": "RAG 通过检索外部知识增强生成",
                        "entity_kind": "concept",
                    }
                ],
                "topics": [
                    {
                        "subject": "RAG",
                        "statement": "RAG 是材料讨论主题",
                    }
                ],
            }
        ]
    )

    assert [item["name"] for item in merged["entities"]] == ["RAG"]
    assert len(merged["entities"][0]["claims"]) == 2
    assert merged["topics"] == []


@pytest.mark.asyncio
async def test_long_document_ingest_uses_chunked_analysis(store, compiler):
    """超过阈值的文档应触发分块分析，并合并各块提取结果。"""
    # 构造两个长章节，并显式启用分块以锁定合并行为。
    para1 = "这是第一章的补充段落，用于触发分块分析并包含实体 EntityA 和概念 ConceptA 的上下文信息。"
    section1 = "# 第一章\n\n" + "\n\n".join([para1] * 300)
    para2 = "这是第二章的补充段落，用于触发分块分析并包含实体 EntityB 和概念 ConceptB 的上下文信息。"
    section2 = "# 第二章\n\n" + "\n\n".join([para2] * 300)
    source_content = f"{section1}\n\n{section2}"
    # 期望被切成 2 个语义块，每个 chunk 返回对应章节的实体。
    compiler.provider = FakeProvider(
        script=[
            _analysis_response(
                {
                    "entities": [
                        {"name": "EntityA", "description": "实体 A 描述。"},
                        {"name": "MechanismA", "description": "机制 A 描述。", "entity_kind": "concept"},
                    ],
                    "topics": [],
                    "relationships": [],
                }
            ),
            _analysis_response(
                {
                    "entities": [
                        {"name": "EntityB", "description": "实体 B 描述。"},
                        {"name": "MechanismB", "description": "机制 B 描述。", "entity_kind": "concept"},
                    ],
                    "topics": [],
                    "relationships": [],
                }
            ),
        ]
    )

    store.save_raw(
        RawSource(
            id="long_src",
            title="长文档",
            source_type="paste",
            parsed_path="",
        )
    )

    result = await compiler.ingest(
        "long_src",
        source_content=source_content,
        chunk_size=20_000,
        use_chunking=True,
    )

    assert not result.issues
    titles = {p.title for p in result.pages}
    # source 页面 + 4 个 entity
    assert len(result.pages) == 5
    assert "长文档" in titles
    assert "EntityA" in titles
    assert "MechanismA" in titles
    assert "EntityB" in titles
    assert "MechanismB" in titles

    # 验证 LLM 被调用了 2 次（分块分析）
    assert len(compiler.provider.calls) == 2


@pytest.mark.asyncio
async def test_plan_ingest_uses_compact_units_and_page_threshold(store, compiler):
    raw = RawSource(
        id="src_units",
        title="丰富材料",
        source_type="paste",
        parsed_path="",
    )
    store.save_raw(raw)
    raw.parsed_path = store.save_parsed_markdown(
        raw.id,
        "核心机制、辅助实践和路过名称的材料。",
    )
    store.save_raw(raw)
    compiler.provider = FakeProvider(
        script=[
            _analysis_response(
                {
                    "format": "knowledge-units-v5",
                    "entities": [
                        {
                            "subject": "路过名称",
                            "importance": "supporting",
                            "statement": "路过名称只在材料中出现一次",
                            "confidence": "low",
                        },
                        {
                            "subject": "核心机制",
                            "importance": "core",
                            "statement": "核心机制负责长期知识沉淀",
                            "summary": "长期知识沉淀机制",
                            "locator": "第一节",
                            "excerpt": "核心机制负责长期知识沉淀。",
                            "confidence": "medium",
                            "relations": [
                                {"target": "辅助实践", "relation": "uses"}
                            ],
                        },
                        {
                            "subject": "辅助实践",
                            "importance": "supporting",
                            "statement": "辅助实践提供增量更新",
                            "summary": "增量更新实践",
                            "confidence": "medium",
                        },
                        {
                            "subject": "辅助实践",
                            "importance": "supporting",
                            "statement": "辅助实践减少重复页面",
                            "summary": "增量更新实践",
                            "confidence": "medium",
                        },
                    ],
                    "topics": [],
                }
            )
        ]
    )

    plan = await compiler.plan_ingest(raw.id)
    titles = {page.title for page in plan.planned_pages}

    assert "核心机制" in titles
    assert "辅助实践" in titles
    assert "路过名称" not in titles
    core = next(page for page in plan.planned_pages if page.title == "核心机制")
    assert core.claims[0].evidence[0].locator == "第一节"
    assert plan.relationships == [
        {"source": "核心机制", "target": "辅助实践", "relation": "uses"}
    ]
    prompt = compiler.provider.calls[0][-1].content
    assert '"entities"' in prompt
    assert '"concepts"' not in prompt
    assert '"topics"' in prompt
    assert "entities 最多 3 个 unit" in prompt
    assert "topics 最多 2 个 unit" in prompt
    assert "最多 5 个 Entity 和 3 个 Topic" in prompt


@pytest.mark.asyncio
async def test_analyze_chunk_uses_compact_output_budget(monkeypatch, compiler):
    chat = AsyncMock(
        return_value=(
            '{"format":"knowledge-units-v7",'
            '"source_summary":{},"entities":[],"topics":[]}'
        )
    )
    monkeypatch.setattr("crew.wiki.compiler.chat_text", chat)

    result = await compiler._analyze_chunk("测试材料")

    assert result["format"] == "knowledge-units-v7"
    assert result["entities"] == []
    assert result["topics"] == []
    assert chat.await_args.kwargs["max_tokens"] == 2_500


@pytest.mark.asyncio
async def test_plan_ingest_reuses_successful_chunk_cache(store, compiler):
    para = "这是需要分块处理的丰富资料，其中包含可复用的知识内容。"
    content = "# 第一部分\n\n" + para * 1_200 + "\n\n# 第二部分\n\n" + para * 1_200
    raw = RawSource(
        id="src_cached",
        title="缓存材料",
        source_type="paste",
        parsed_path="",
    )
    store.save_raw(raw)
    raw.parsed_path = store.save_parsed_markdown(raw.id, content)
    store.save_raw(raw)

    from crew.wiki.compiler import _split_into_semantic_chunks

    chunk_count = len(_split_into_semantic_chunks(content))
    compiler.provider = FakeProvider(
        script=[
            _analysis_response(
                {
                    "format": "knowledge-units-v5",
                    "entities": [],
                    "topics": [
                        {
                            "subject": f"知识主题 {index}",
                            "importance": "core",
                            "statement": f"第 {index} 块包含可复用知识",
                            "confidence": "medium",
                        }
                    ]
                }
            )
            for index in range(chunk_count)
        ]
    )

    first = await compiler.plan_ingest(raw.id)
    call_count = len(compiler.provider.calls)
    second = await compiler.plan_ingest(raw.id)

    assert call_count == chunk_count
    assert len(compiler.provider.calls) == call_count
    assert first.analysis_stats["analyzed_chunks"] == chunk_count
    assert first.analysis_stats["cache_hits"] == 0
    assert second.analysis_stats["analyzed_chunks"] == 0
    assert second.analysis_stats["cache_hits"] == chunk_count
    assert (
        store._dir() / ".crew" / "cache" / f"{raw.id}.analysis-cache.json"
    ).exists()


@pytest.mark.asyncio
async def test_chunk_cache_resumes_only_failed_chunks(tmp_path, compiler):
    section_a = "# A\n\n" + ("A 内容。" * 4_000)
    section_b = "# B\n\n" + ("B 内容。" * 4_000)
    content = f"{section_a}\n\n{section_b}"
    cache_path = tmp_path / "resume.analysis-cache.json"
    attempts: dict[str, int] = {}

    async def fake_analyze_chunk(
        chunk: str,
        *,
        chunk_index: int = 0,
        total_chunks: int = 1,
    ) -> dict[str, Any]:
        marker = "B" if chunk.startswith("# B") else f"A-{chunk_index}"
        attempts[marker] = attempts.get(marker, 0) + 1
        if marker == "B" and attempts[marker] == 1:
            return {
                "_chunk_failed": True,
                "format": "knowledge-units-v5",
                "entities": [],
                "topics": [],
            }
        return {
            "format": "knowledge-units-v5",
            "entities": [],
            "topics": [
                {
                    "subject": marker,
                    "importance": "core",
                    "statement": f"{marker} 的知识",
                    "confidence": "medium",
                }
            ]
        }

    compiler._analyze_chunk = AsyncMock(side_effect=fake_analyze_chunk)

    first = await compiler._analyze(
        content,
        chunk_size=30_000,
        use_chunking=True,
        cache_path=cache_path,
    )
    second = await compiler._analyze(
        content,
        chunk_size=30_000,
        use_chunking=True,
        cache_path=cache_path,
    )

    assert first["_analysis_meta"]["failed_chunks"] == 1
    assert second["_analysis_meta"]["failed_chunks"] == 0
    assert second["_analysis_meta"]["analyzed_chunks"] == 1
    assert second["_analysis_meta"]["cache_hits"] == (
        second["_analysis_meta"]["total_chunks"] - 1
    )
    assert attempts["B"] == 2


@pytest.mark.asyncio
async def test_plan_matches_alias_and_applies_claim_evidence(store, compiler):
    existing = store.save_page(
        WikiPage(
            id="",
            page_type="entity",
            title="Kubernetes",
            content="容器编排平台。",
            file_path="",
            aliases=["K8s"],
        )
    )
    raw = RawSource(
        id="src_alias",
        title="平台说明",
        source_type="paste",
        parsed_path="",
    )
    store.save_raw(raw)
    raw.parsed_path = store.save_parsed_markdown("src_alias", "K8s 支持声明式部署。")
    store.save_raw(raw)
    compiler.provider = FakeProvider(
        script=[
            _analysis_response(
                {
                    "entities": [
                        {
                            "name": "k8s",
                            "description": "支持声明式部署。",
                            "aliases": ["K8s"],
                            "claims": [
                                {
                                    "statement": "Kubernetes 支持声明式部署",
                                    "locator": "第一段",
                                    "excerpt": "K8s 支持声明式部署。",
                                    "confidence": "high",
                                }
                            ],
                        }
                    ],
                    "topics": [],
                    "relationships": [],
                }
            )
        ]
    )

    plan = await compiler.plan_ingest("src_alias")
    entity_plan = next(page for page in plan.planned_pages if page.page_type == "entity")

    assert entity_plan.action == "update"
    assert entity_plan.existing_title == "Kubernetes"
    assert entity_plan.title == "Kubernetes"
    assert entity_plan.claims[0].evidence[0].source_id == "src_alias"

    await compiler.apply_ingest("src_alias")
    updated = store.get(existing.id)
    assert updated is not None
    assert updated.claims[0].confidence == "high"
    assert updated.claims[0].evidence[0].locator == "第一段"
    assert len([page for page in store.list_all() if page.page_type == "entity"]) == 1


@pytest.mark.asyncio
async def test_contested_ingest_is_preserved_in_plan_and_page(store, compiler):
    existing = store.save_page(
        WikiPage(
            id="",
            page_type="entity",
            title="发布策略",
            content="默认采用蓝绿发布。",
            file_path="",
        )
    )
    raw = RawSource(
        id="src_contested",
        title="发布复盘",
        source_type="paste",
        parsed_path="",
    )
    store.save_raw(raw)
    raw.parsed_path = store.save_parsed_markdown(
        "src_contested",
        "部分团队反对默认蓝绿发布。",
    )
    store.save_raw(raw)
    compiler.provider = FakeProvider(
        script=[
            _analysis_response(
                {
                    "entities": [
                        {
                            "name": "发布策略",
                            "description": "部分团队反对默认蓝绿发布。",
                            "claims": [
                                {
                                    "statement": "蓝绿发布不应作为所有团队的默认方案",
                                    "confidence": "medium",
                                    "contested": True,
                                    "contradictions": ["既有页面建议默认采用蓝绿发布"],
                                }
                            ],
                        }
                    ],
                    "topics": [],
                    "relationships": [],
                }
            )
        ]
    )

    plan = await compiler.plan_ingest("src_contested")
    entity_plan = next(page for page in plan.planned_pages if page.page_type == "entity")
    assert entity_plan.action == "contest"
    assert plan.total_contested == 1

    await compiler.apply_ingest("src_contested")
    updated = store.get(existing.id)
    assert updated is not None
    assert updated.contested is True
    assert "既有页面建议默认采用蓝绿发布" in updated.contradictions


def test_update_index_contains_navigation_quality_metadata(store, compiler):
    store.save_page(
        WikiPage(
            id="",
            page_type="entity",
            title="知识编译",
            content="# 知识编译\n\n把资料增量整理为规范知识。",
            file_path="",
            sources=["s1", "s2"],
            confidence="high",
            contested=True,
        )
    )

    compiler.update_index()
    index_text = (store._dir() / "index.md").read_text(encoding="utf-8")

    assert "# 知识导航" in index_text
    assert "## 关键词" in index_text
    assert "## 话题" in index_text
    assert "Crew" not in index_text
    assert "## 概念" not in index_text
    assert "[[知识编译]]" in index_text
    assert "来源 2" in index_text
    assert "关系 0" in index_text


def test_document_limits_apply_after_all_chunks_are_merged():
    from crew.wiki.compiler import _apply_document_limits

    analysis = {
        "entities": [
            {
                "name": f"Entity {index}",
                "importance": "core" if index < 2 else "supporting",
                "claims": [{"statement": f"claim {index}"}],
            }
            for index in range(8)
        ],
        "topics": [
            {
                "name": f"Topic {index}",
                "importance": "core",
                "claims": [{"statement": f"topic claim {index}"}],
            }
            for index in range(5)
        ],
        "relationships": [],
    }

    _apply_document_limits(analysis, content_length=50_000)

    assert len(analysis["entities"]) == 5
    assert len(analysis["topics"]) == 3


def test_short_document_limits_skip_topics():
    from crew.wiki.compiler import _apply_document_limits

    analysis = {
        "entities": [{"name": f"E{index}", "claims": []} for index in range(5)],
        "topics": [{"name": "Short Topic", "claims": []}],
        "relationships": [],
    }

    _apply_document_limits(analysis, content_length=1_000)

    assert len(analysis["entities"]) == 3
    assert analysis["topics"] == []


@pytest.mark.asyncio
async def test_batch_ingest_is_bounded_and_returns_cursor(store, compiler):
    compiler.provider = FakeProvider(
        script=[
            _analysis_response({"entities": [], "topics": [], "relationships": []}),
            _analysis_response({"entities": [], "topics": [], "relationships": []}),
        ]
    )
    source_ids = []
    for index in range(2):
        raw = RawSource(
            id=f"batch_{index}",
            title=f"Batch {index}",
            source_type="paste",
            parsed_path="",
        )
        store.save_raw(raw)
        raw.parsed_path = store.save_parsed_markdown(raw.id, f"content {index}")
        raw.parse_status = "parsed"
        store.save_raw(raw)
        source_ids.append(raw.id)

    first = await compiler.batch_ingest(
        source_ids=source_ids,
        batch_size=1,
        apply=True,
    )
    second = await compiler.batch_ingest(
        source_ids=source_ids,
        cursor=first["next_cursor"],
        batch_size=1,
        apply=True,
    )

    assert first["succeeded"] == ["batch_0"]
    assert first["next_cursor"] == 1
    assert first["remaining"] == 1
    assert second["succeeded"] == ["batch_1"]
    assert second["next_cursor"] is None


# ---------------------------------------------------------------------------
# #4 计划指纹；#5 apply 目标页版本校验；#8 apply 拒绝被取代来源
# ---------------------------------------------------------------------------


def test_compute_plan_fingerprint_is_stable_and_distinct():
    """计划指纹确定性可复现，且规划变化时指纹变化。"""
    from crew.wiki.compiler import compute_plan_fingerprint
    from crew.wiki.schemas import PlannedPage, PlanResult

    base = PlanResult(
        source_id="s1",
        source_content_sha256="hash-a",
        planned_pages=[
            PlannedPage(title="A", page_type="entity", action="create", content="x"),
            PlannedPage(title="B", page_type="topic", action="update", content="y"),
        ],
        relationships=[{"source": "A", "target": "B", "relation": "related"}],
    )
    fp1 = compute_plan_fingerprint(base)
    fp2 = compute_plan_fingerprint(base)
    assert fp1 and fp1 == fp2  # 稳定

    # 规划内容变化 -> 指纹变化
    changed = PlanResult(
        source_id="s1",
        source_content_sha256="hash-a",
        planned_pages=[
            PlannedPage(title="A", page_type="entity", action="create", content="z"),
        ],
        relationships=[{"source": "A", "target": "B", "relation": "related"}],
    )
    assert compute_plan_fingerprint(changed) != fp1

    # source 内容版本变化 -> 指纹变化（即使规划相同）
    version_changed = PlanResult(
        source_id="s1",
        source_content_sha256="hash-b",
        planned_pages=base.planned_pages,
        relationships=base.relationships,
    )
    assert compute_plan_fingerprint(version_changed) != fp1


@pytest.mark.asyncio
async def test_plan_ingest_records_plan_fingerprint(store, compiler):
    """plan_ingest 保存的计划携带非空指纹。"""
    analysis = {
        "source_summary": {"one_sentence": "摘要。", "core_points": ["要点"]},
        "entities": [{"name": "AgentRuntime", "description": "运行时。"}],
        "topics": [],
        "relationships": [],
    }
    compiler.provider = FakeProvider(script=[_analysis_response(analysis)])
    store.save_raw(RawSource(id="s1", title="文档", source_type="paste", parsed_path=""))
    store.save_parsed_markdown("s1", "文档正文内容")
    plan = await compiler.plan_ingest("s1")
    assert plan.plan_fingerprint
    disk = compiler.load_plan("s1")
    assert disk is not None and disk.plan_fingerprint == plan.plan_fingerprint


@pytest.mark.asyncio
async def test_apply_skips_page_when_target_modified_externally(store, compiler):
    """计划生成后目标页被外部修改，apply 跳过该页而不覆盖新内容。"""
    analysis = {
        "source_summary": {"one_sentence": "摘要。", "core_points": ["要点"]},
        "entities": [{"name": "AgentRuntime", "description": "运行时描述。"}],
        "topics": [],
        "relationships": [],
    }
    compiler.provider = FakeProvider(script=[_analysis_response(analysis)])
    store.save_raw(RawSource(id="s1", title="文档", source_type="paste", parsed_path=""))
    store.save_parsed_markdown("s1", "文档正文内容")
    # 预置目标 entity 页，使计划走 update 并快照其正文版本
    existing = store.save_page(
        WikiPage(
            id="",
            page_type="entity",
            title="AgentRuntime",
            content="# AgentRuntime\n\n原始正文",
            file_path="",
            sources=["s1"],
        )
    )
    plan = await compiler.plan_ingest("s1")
    update_plan = next(
        p for p in plan.planned_pages if p.title == "AgentRuntime" and p.action == "update"
    )
    assert update_plan.target_content_sha256  # 已快照目标版本

    # 计划生成后，目标页被外部修改
    existing.content = "# AgentRuntime\n\n已被外部修改的新内容"
    store.update(existing)

    result = await compiler.apply_ingest("s1")
    page_after = store.get(existing.id)
    assert "已被外部修改的新内容" in page_after.content
    assert "运行时描述" not in page_after.content  # 计划内容未覆盖


@pytest.mark.asyncio
async def test_apply_rejects_superseded_source(store, compiler):
    """被取代的来源不再应用其历史计划。"""
    analysis = {
        "source_summary": {"one_sentence": "摘要。", "core_points": ["要点"]},
        "entities": [],
        "topics": [],
        "relationships": [],
    }
    compiler.provider = FakeProvider(script=[_analysis_response(analysis)])
    store.save_raw(RawSource(id="s1", title="文档", source_type="paste", parsed_path=""))
    store.save_parsed_markdown("s1", "文档正文内容")
    await compiler.plan_ingest("s1")

    raw = store.load_raw("s1")
    raw.superseded_by = "s2"
    store.save_raw(raw)

    result = await compiler.apply_ingest("s1")
    assert any("已被新版本" in issue for issue in result.issues)
    assert result.pages == []
