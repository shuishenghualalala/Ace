"""Wiki 工具 handler 单元测试：验证当前知识库被正确透传。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from crew.core.runctx import current_attachment_paths, current_owner_account_id, current_session_id
from crew.tools.registry import Registry
from crew.wiki.compiler import WikiCompiler
from crew.wiki.manager import WikiSessionManager
from crew.wiki.query import WikiQuerier
from crew.wiki.store import WikiStore
from crew.wiki.store import FileSystemWikiStore
from crew.wiki.config import WikiConfig
from crew.wiki.tools import (
    WIKI_MANAGE_TOOLS,
    WIKI_MANAGE_TOOLSET,
    WIKI_READ_TOOLS,
    WIKI_READ_TOOLSET,
    register_wiki_tools,
)


@pytest.fixture
def wiki_mocks():
    store = MagicMock(spec=WikiStore)
    # 默认不在任何知识库中找到 source：退回会话活跃 KB（与各用例的 kb_active 断言一致）。
    store.find_source_kb.return_value = None
    store.check_source_duplicate.return_value = None
    store.count_pages.return_value = 0
    store.list_pages_by_source.return_value = []
    compiler = MagicMock(spec=WikiCompiler)
    source_page = MagicMock()
    source_page.id = "source-page"
    source_page.to_dict.return_value = {
        "id": "source-page",
        "title": "Source",
        "page_type": "source",
    }
    compiler.publish_source_page.return_value = source_page
    querier = MagicMock(spec=WikiQuerier)
    manager = MagicMock(spec=WikiSessionManager)
    # 默认会话活跃 KB：各用例的 kb_active 断言都依赖它，个别用例可自行覆盖。
    manager.get_kb_id.return_value = "kb_active"
    registry = Registry()
    config = WikiConfig.from_raw({"ingest": {"auto_apply": False}})
    register_wiki_tools(registry, store, compiler, querier, manager, config=config)
    return {
        "store": store,
        "compiler": compiler,
        "querier": querier,
        "manager": manager,
        "registry": registry,
        "config": config,
    }


def _set_context(session_id: str = "sid", owner: str = "owner"):
    current_session_id.set(session_id)
    current_owner_account_id.set(owner)


@pytest.fixture
def fs_wiki(tmp_path):
    """真实 FileSystemWikiStore + mock compiler/manager 的工具注册环境（KB 固定为 default）。"""
    store = FileSystemWikiStore(base_dir=tmp_path / "home")
    compiler = MagicMock(spec=WikiCompiler)
    manager = MagicMock(spec=WikiSessionManager)
    manager.get_kb_id.return_value = "default"
    registry = Registry()
    register_wiki_tools(registry, store, compiler, MagicMock(spec=WikiQuerier), manager)
    _set_context()
    return {
        "store": store,
        "compiler": compiler,
        "manager": manager,
        "registry": registry,
    }


def test_wiki_tools_are_split_into_read_and_manage_toolsets(wiki_mocks):
    registry = wiki_mocks["registry"]
    assert set(registry.names_for_toolset(WIKI_READ_TOOLSET)) == set(WIKI_READ_TOOLS)
    assert set(registry.names_for_toolset(WIKI_MANAGE_TOOLSET)) == set(WIKI_MANAGE_TOOLS)


def test_wiki_tool_ui_labels_render_placeholders(wiki_mocks):
    """ui_label_template 用 Python format 单大括号占位；双大括号会被 format_map 当字面量原样输出。"""
    registry = wiki_mocks["registry"]
    args = {
        "kb_id": "kb1",
        "source_id": "s1",
        "page_id": "p1",
        "url": "https://example.com",
        "topic": "主题",
        "path": "a.md",
        "title": "标题",
        "session_id": "sid1",
    }
    for name in list(WIKI_READ_TOOLS) + list(WIKI_MANAGE_TOOLS):
        template = registry.ui_meta(name).get("ui_label_template", "")
        assert "{{" not in template and "}}" not in template, name
        rendered = registry.render_ui_label(name, args)
        assert "{" not in rendered and "}" not in rendered, (name, rendered)
    assert {
        "wiki_check_duplicate",
        "wiki_check_drift",
        "wiki_ingest",
        "wiki_compile",
        "wiki_save_parsed_markdown",
        "wiki_update_index",
        "wiki_append_log",
        "wiki_query",
        "wiki_explore",
        "wiki_init",
        "wiki_source_status",
        "wiki_describe_image",
        "wiki_describe_video",
        "wiki_migrate_layout",
        "wiki_archive_page",
    }.isdisjoint(registry.names())
    assert registry.toolset_for("wiki_search") == WIKI_READ_TOOLSET
    assert registry.toolset_for("wiki_read") == WIKI_READ_TOOLSET
    assert registry.toolset_for("wiki_apply_ingest") == WIKI_MANAGE_TOOLSET
    assert registry.toolset_for("wiki_delete_kb") == WIKI_MANAGE_TOOLSET
    assert registry.toolset_for("wiki_refresh_source") == WIKI_MANAGE_TOOLSET
    assert registry.toolset_for("wiki_digest") == WIKI_MANAGE_TOOLSET
    assert all(
        not registry.get(name).always_load
        for name in (
            registry.names_for_toolset(WIKI_READ_TOOLSET)
            + registry.names_for_toolset(WIKI_MANAGE_TOOLSET)
        )
    )


async def test_wiki_digest_uses_active_kb(wiki_mocks):
    from crew.wiki.schemas import WikiPage

    registry = wiki_mocks["registry"]
    compiler = wiki_mocks["compiler"]
    compiler.digest = AsyncMock(
        return_value=WikiPage(
            id="syn_1",
            page_type="synthesis",
            title="Crew-深度综合",
            content="# Crew-深度综合",
            file_path="wiki/synthesis/Crew-深度综合.md",
        )
    )
    _set_context()

    await registry.get("wiki_digest").run({"topic": "Crew", "mode": "synthesis"})

    compiler.digest.assert_awaited_once_with(
        "Crew",
        mode="synthesis",
        owner_account_id="owner",
        kb_id="kb_active",
    )


async def test_wiki_search_uses_active_kb(wiki_mocks):
    registry = wiki_mocks["registry"]
    querier = wiki_mocks["querier"]
    querier.search.return_value = {"pages": [], "retrieval": {}}

    _set_context()
    tool = registry.get("wiki_search")
    await tool.run({"query": "Crew"})

    querier.search.assert_called_once_with(
        "Crew",
        top_k=5,
        owner_account_id="owner",
        kb_id="kb_active",
        expand_neighbors=True,
        include_context=True,
    )


async def test_wiki_read_uses_active_kb(wiki_mocks):
    registry = wiki_mocks["registry"]
    store = wiki_mocks["store"]
    store.get.return_value = None

    _set_context()
    tool = registry.get("wiki_read")
    await tool.run({"page_id": "p1"})

    store.get.assert_called_once_with(
        "p1",
        owner_account_id="owner",
        kb_id="kb_active",
    )


async def test_wiki_lint_uses_active_kb(wiki_mocks):
    registry = wiki_mocks["registry"]
    compiler = wiki_mocks["compiler"]
    compiler.lint = AsyncMock(return_value=[])

    _set_context()
    tool = registry.get("wiki_lint")
    await tool.run({})

    compiler.lint.assert_called_once_with(
        owner_account_id="owner",
        kb_id="kb_active",
        deep=False,
    )


@pytest.mark.asyncio
async def test_wiki_lint_deep_passes_to_compiler(wiki_mocks):
    registry = wiki_mocks["registry"]
    compiler = wiki_mocks["compiler"]
    compiler.lint = AsyncMock(return_value=[])

    _set_context()
    tool = registry.get("wiki_lint")
    await tool.run({"deep": True})

    compiler.lint.assert_called_once_with(
        owner_account_id="owner",
        kb_id="kb_active",
        deep=True,
    )


@pytest.mark.asyncio
async def test_wiki_orient_uses_active_kb(wiki_mocks):
    from crew.wiki.schemas import WikiOrientation

    registry = wiki_mocks["registry"]
    compiler = wiki_mocks["compiler"]
    compiler.orient = AsyncMock(return_value=WikiOrientation(kb_id="kb_active", kb_name="KB Active"))

    _set_context()
    tool = registry.get("wiki_orient")
    result = await tool.run({})

    compiler.orient.assert_called_once_with(owner_account_id="owner", kb_id="kb_active")
    assert "kb_active" in result
    assert "source_adapters" in result
    assert "youtube" in result


@pytest.mark.asyncio
async def test_wiki_batch_ingest_uses_active_kb_and_five_item_cap(wiki_mocks):
    registry = wiki_mocks["registry"]
    compiler = wiki_mocks["compiler"]
    config = wiki_mocks["config"]
    config.ingest.auto_apply = True
    compiler.batch_ingest = AsyncMock(return_value={
        "source_ids": ["s1"],
        "succeeded": ["s1"],
        "skipped": [],
        "failed": [],
        "plans": [],
        "page_ids": ["p1"],
        "cursor": 0,
        "next_cursor": None,
        "remaining": 0,
        "batch_size": 5,
        "applied": True,
    })
    _set_context()

    result = await registry.get("wiki_batch_ingest").run({
        "source_ids": ["s1"],
        "batch_size": 99,
    })

    assert '"auto_applied": true' in result
    compiler.batch_ingest.assert_awaited_once_with(
        source_ids=["s1"],
        cursor=0,
        batch_size=5,
        apply=True,
        owner_account_id="owner",
        kb_id="kb_active",
    )


async def test_wiki_describe_image_disabled_when_multimodal_off():
    from crew.wiki.schemas import RawSource

    store = MagicMock(spec=WikiStore)
    store.find_source_kb.return_value = None
    store.load_raw.return_value = RawSource(
        id="s1",
        title="a.png",
        source_type="image",
        parsed_path="",
        original_path="/tmp/a.png",
    )
    compiler = MagicMock(spec=WikiCompiler)
    querier = MagicMock(spec=WikiQuerier)
    manager = MagicMock(spec=WikiSessionManager)
    registry = Registry()
    config = WikiConfig()
    config.multimodal.enabled = False
    register_wiki_tools(registry, store, compiler, querier, manager, config=config)

    _set_context()
    tool = registry.get("wiki_parse_source")
    result = await tool.run({"source_id": "s1"})

    assert "未启用" in result


async def test_wiki_describe_image_calls_skill_and_returns_description(wiki_mocks):
    from crew.wiki.schemas import RawSource

    registry = wiki_mocks["registry"]
    store = wiki_mocks["store"]
    raw = RawSource(
        id="img1",
        title="a.png",
        source_type="image",
        parsed_path="",
        original_path="/tmp/a.png",
        file_type="image/png",
    )
    store.load_raw.return_value = raw

    _set_context()
    with patch("crew.wiki.multimodal.describe_image", return_value="a cat") as mock_describe:
        tool = registry.get("wiki_parse_source")
        result = await tool.run({"source_id": "img1"})

    mock_describe.assert_called_once_with("/tmp/a.png", wiki_mocks["config"].multimodal.prompt_image)
    assert "a cat" in result
    wiki_mocks["compiler"].publish_source_page.assert_called_once_with(
        "img1",
        owner_account_id="owner",
        kb_id="kb_active",
    )
    wiki_mocks["compiler"].finalize_write.assert_called_once()
    assert '"source_page"' in result


async def test_wiki_describe_video_requires_confirmation(wiki_mocks):
    from crew.wiki.schemas import RawSource

    registry = wiki_mocks["registry"]
    store = wiki_mocks["store"]
    raw = RawSource(
        id="vid1",
        title="a.mp4",
        source_type="video",
        parsed_path="",
        original_path="/tmp/a.mp4",
        file_type="video/mp4",
    )
    store.load_raw.return_value = raw

    _set_context()
    tool = registry.get("wiki_parse_source")
    result = await tool.run({"source_id": "vid1"})

    assert '"requires_confirmation": true' in result
    assert '"action": "describe_video"' in result


async def test_wiki_describe_video_with_confirmation_calls_skill(wiki_mocks):
    from crew.wiki.schemas import RawSource

    registry = wiki_mocks["registry"]
    store = wiki_mocks["store"]
    raw = RawSource(
        id="vid1",
        title="a.mp4",
        source_type="video",
        parsed_path="",
        original_path="/tmp/a.mp4",
        file_type="video/mp4",
    )
    store.load_raw.return_value = raw
    store.save_parsed_markdown.return_value = "/tmp/vid1.parsed.md"
    wiki_mocks["manager"].consume_confirmation.return_value = {"source_id": "vid1"}

    _set_context()
    with patch("crew.wiki.multimodal.describe_video", return_value="a dog") as mock_describe:
        tool = registry.get("wiki_parse_source")
        result = await tool.run({"source_id": "vid1", "confirmation_id": "wcf_test"})

    mock_describe.assert_called_once_with("/tmp/a.mp4", wiki_mocks["config"].multimodal.prompt_video, confirm_upload=True)
    assert "a dog" in result
    wiki_mocks["compiler"].publish_source_page.assert_called_once_with(
        "vid1",
        owner_account_id="owner",
        kb_id="kb_active",
    )
    wiki_mocks["compiler"].finalize_write.assert_called_once()
    assert '"source_page"' in result


async def test_wiki_parse_source_success(wiki_mocks, tmp_path, monkeypatch):
    import crew.wiki.tools as wiki_tools_module
    from crew.wiki.schemas import RawSource

    registry = wiki_mocks["registry"]
    store = wiki_mocks["store"]

    original = tmp_path / "note.txt"
    original.write_text("hello world", encoding="utf-8")
    raw = RawSource(
        id="s1",
        title="note.txt",
        source_type="upload",
        parsed_path="",
        original_path=str(original),
    )
    store.load_raw.return_value = raw
    parsed_path = str(tmp_path / "s1.parsed.md")
    store.save_parsed_markdown.return_value = parsed_path
    source_page = MagicMock()
    source_page.id = "source-note"
    source_page.to_dict.return_value = {
        "id": "source-note",
        "title": "note.txt",
        "page_type": "source",
    }
    compiler = wiki_mocks["compiler"]
    compiler.publish_source_page.return_value = source_page

    parse_document = MagicMock(return_value="hello world")
    monkeypatch.setattr(wiki_tools_module, "parse_document_from_bytes", parse_document)

    _set_context()
    tool = registry.get("wiki_parse_source")
    result = await tool.run({"source_id": "s1"})

    assert raw.parse_status == "parsed"
    assert raw.parse_error is None
    assert raw.parsed_path == parsed_path
    parse_document.assert_called_once_with(b"hello world", original.name)
    store.save_parsed_markdown.assert_called_once_with("s1", "hello world", owner_account_id="owner", kb_id="kb_active")
    compiler.publish_source_page.assert_called_once_with(
        "s1",
        owner_account_id="owner",
        kb_id="kb_active",
    )
    source_page.to_dict.assert_called_once_with(brief=True)
    assert '"content"' not in result
    assert "全文 Source 页面已发布" in result


async def test_wiki_parse_source_failure_updates_status(wiki_mocks, tmp_path, monkeypatch):
    import crew.wiki.tools as wiki_tools_module
    from crew.wiki.schemas import RawSource

    registry = wiki_mocks["registry"]
    store = wiki_mocks["store"]

    original = tmp_path / "bad.xlsx"
    original.write_bytes(b"fake bytes")
    raw = RawSource(
        id="s2",
        title="bad.xlsx",
        source_type="upload",
        parsed_path="",
        original_path=str(original),
    )
    store.load_raw.return_value = raw

    def _bad_parse(content, filename):
        raise Exception("expected <class 'openpyxl.styles.fills.Fill'>")

    monkeypatch.setattr(wiki_tools_module, "parse_document_from_bytes", _bad_parse)

    _set_context()
    tool = registry.get("wiki_parse_source")
    result = await tool.run({"source_id": "s2"})

    assert raw.parse_status == "failed"
    assert raw.parse_error is not None
    assert "解析失败" in result
    store.save_raw.assert_called_once()


async def test_wiki_read_returns_page_and_limited_neighbors(wiki_mocks):
    from crew.wiki.schemas import WikiPage

    registry = wiki_mocks["registry"]
    store = wiki_mocks["store"]

    page = WikiPage(
        id="p1",
        page_type="topic",
        title="中心页",
        content="内容",
        file_path="",
    )
    neighbor = WikiPage(
        id="p2",
        page_type="entity",
        title="关联页",
        content="关联内容",
        file_path="",
    )
    store.get.return_value = page
    store.get_neighbors.return_value = [neighbor] * 10

    _set_context()
    tool = registry.get("wiki_read")
    result = await tool.run({
        "page_id": "p1",
        "include_neighbors": True,
        "neighbor_limit": 1,
    })

    store.get.assert_called_once_with("p1", owner_account_id="owner", kb_id="kb_active")
    store.get_neighbors.assert_called_once_with("p1", owner_account_id="owner", kb_id="kb_active")
    assert "中心页" in result
    assert "关联页" in result


async def test_wiki_read_missing_page_id(wiki_mocks):
    registry = wiki_mocks["registry"]

    _set_context()
    tool = registry.get("wiki_read")
    result = await tool.run({})

    assert "缺少" in result


async def test_wiki_list_sources_uses_active_kb_and_status_filter(wiki_mocks):
    from crew.wiki.schemas import RawSource

    registry = wiki_mocks["registry"]
    store = wiki_mocks["store"]

    raws = [
        RawSource(id="s1", title="a.pdf", source_type="upload", parsed_path="", parse_status="parsed"),
        RawSource(id="s2", title="b.docx", source_type="upload", parsed_path="", parse_status="failed"),
        RawSource(id="s3", title="c.txt", source_type="paste", parsed_path="", parse_status="pending"),
    ]
    store.list_raws.return_value = raws

    _set_context()
    tool = registry.get("wiki_list_sources")

    # 默认 all
    result = await tool.run({})
    assert "s1" in result
    assert "s2" in result
    assert "s3" in result
    store.list_raws.assert_called_with(owner_account_id="owner", kb_id="kb_active")

    # 过滤 failed
    result = await tool.run({"status": "failed"})
    assert "s1" not in result
    assert "s2" in result
    assert "s3" not in result

    # 限制数量
    result = await tool.run({"limit": 2})
    assert result.count('"source_id"') == 2


# ---- wiki_delete_source ----

async def test_wiki_delete_source_uses_active_kb(wiki_mocks):
    from crew.wiki.schemas import RawSource

    registry = wiki_mocks["registry"]
    store = wiki_mocks["store"]
    manager = wiki_mocks["manager"]
    manager.consume_confirmation.return_value = {"source_id": "s1"}
    store.load_raw.return_value = RawSource(id="s1", title="a.xlsx", source_type="upload", parsed_path="")
    store.delete_raw.return_value = True

    _set_context()
    tool = registry.get("wiki_delete_source")
    result = await tool.run({"source_id": "s1", "confirmation_id": "wcf_test"})

    store.load_raw.assert_called_once_with("s1", owner_account_id="owner", kb_id="kb_active")
    store.delete_raw.assert_called_once_with("s1", owner_account_id="owner", kb_id="kb_active")
    assert "已删除" in result


async def test_wiki_delete_source_requires_confirmation(wiki_mocks):
    from crew.wiki.schemas import RawSource

    registry = wiki_mocks["registry"]
    store = wiki_mocks["store"]
    store.load_raw.return_value = RawSource(id="s1", title="a.xlsx", source_type="upload", parsed_path="")
    store.list_all.return_value = []

    _set_context()
    tool = registry.get("wiki_delete_source")
    result = await tool.run({"source_id": "s1"})

    store.delete_raw.assert_not_called()
    assert "requires_confirmation" in result
    assert "s1" in result


async def test_wiki_delete_source_returns_error_when_not_found(wiki_mocks):
    registry = wiki_mocks["registry"]
    store = wiki_mocks["store"]
    store.load_raw.return_value = None

    _set_context()
    tool = registry.get("wiki_delete_source")
    result = await tool.run({"source_id": "s1"})

    assert "不存在" in result


# ---- wiki_update_page ----

async def test_wiki_update_page_uses_active_kb(wiki_mocks):
    from crew.wiki.schemas import WikiPage

    registry = wiki_mocks["registry"]
    store = wiki_mocks["store"]

    page = WikiPage(id="p1", page_type="topic", title="原题", content="原内容", file_path="topics/p1.md")
    store.get.return_value = page
    store.update.return_value = page

    _set_context()
    tool = registry.get("wiki_update_page")
    result = await tool.run({
        "page_id": "p1",
        "content": "新内容",
        "relations": [{"target_page_id": "p2", "relation": "related"}],
    })

    store.get.assert_called_once_with("p1", owner_account_id="owner", kb_id="kb_active")
    store.update.assert_called_once()
    call_page = store.update.call_args.args[0]
    assert call_page.content == "新内容"
    assert call_page.relations[0].target_page_id == "p2"
    assert "page" in result


async def test_wiki_update_page_returns_error_when_not_found(wiki_mocks):
    registry = wiki_mocks["registry"]
    store = wiki_mocks["store"]
    store.get.return_value = None

    _set_context()
    tool = registry.get("wiki_update_page")
    result = await tool.run({"page_id": "p1", "content": "新内容"})

    assert "不存在" in result


# ---- chunk_size / use_chunking 参数透传 ----

async def test_wiki_plan_ingest_passes_chunk_options(wiki_mocks):
    from crew.wiki.schemas import PlanResult

    registry = wiki_mocks["registry"]
    compiler = wiki_mocks["compiler"]
    compiler.plan_ingest = AsyncMock(
        return_value=PlanResult(source_id="s1", planned_pages=[], total_new=0, total_update=0)
    )

    _set_context()
    tool = registry.get("wiki_plan_ingest")
    await tool.run({"source_id": "s1", "chunk_size": 8000, "use_chunking": False})

    compiler.plan_ingest.assert_called_once()
    call_kwargs = compiler.plan_ingest.call_args.kwargs
    assert call_kwargs["kb_id"] == "kb_active"
    assert call_kwargs["chunk_size"] == 8000
    assert call_kwargs["use_chunking"] is False


async def test_wiki_apply_ingest_passes_chunk_options(wiki_mocks):
    from crew.wiki.schemas import PlanResult

    registry = wiki_mocks["registry"]
    compiler = wiki_mocks["compiler"]
    manager = wiki_mocks["manager"]
    manager.consume_confirmation.return_value = {
        "source_id": "s1",
        "plan_fingerprint": "fp",
        "source_content_sha256": "sh",
        "planned_titles": [],
    }
    compiler.load_plan.return_value = PlanResult(
        source_id="s1",
        planned_pages=[],
        plan_fingerprint="fp",
        source_content_sha256="sh",
    )
    ingest_result = MagicMock()
    ingest_result.source_id = "s1"
    ingest_result.pages = []
    ingest_result.issues = []
    compiler.apply_ingest = AsyncMock(return_value=ingest_result)

    _set_context()
    tool = registry.get("wiki_apply_ingest")
    await tool.run({
        "source_id": "s1",
        "chunk_size": 12000,
        "use_chunking": True,
        "confirmation_id": "wcf_test",
    })

    compiler.apply_ingest.assert_called_once()
    call_kwargs = compiler.apply_ingest.call_args.kwargs
    assert call_kwargs["kb_id"] == "kb_active"
    assert call_kwargs["chunk_size"] == 12000
    assert call_kwargs["use_chunking"] is True


async def test_wiki_apply_ingest_rejects_stale_confirmation_when_plan_regenerated(wiki_mocks):
    """确认的计划指纹与磁盘 plan 不符（计划已被重新生成）时拒绝应用。"""
    from crew.wiki.schemas import PlanResult

    registry = wiki_mocks["registry"]
    compiler = wiki_mocks["compiler"]
    manager = wiki_mocks["manager"]
    manager.consume_confirmation.return_value = {
        "source_id": "s1",
        "plan_fingerprint": "old_fp",
        "source_content_sha256": "sh",
        "planned_titles": [],
    }
    compiler.load_plan.return_value = PlanResult(
        source_id="s1",
        planned_pages=[],
        plan_fingerprint="new_fp",  # 计划已被重新生成
        source_content_sha256="sh",
    )
    compiler.apply_ingest = AsyncMock()

    _set_context()
    result = await registry.get("wiki_apply_ingest").run({
        "source_id": "s1",
        "confirmation_id": "wcf_test",
    })

    compiler.apply_ingest.assert_not_awaited()
    assert "计划已过期" in result or "过期" in result


async def test_wiki_apply_ingest_rejects_approved_titles_outside_plan(wiki_mocks):
    """approved_titles 含计划外页面时拒绝应用。"""
    from crew.wiki.schemas import PlanResult

    registry = wiki_mocks["registry"]
    compiler = wiki_mocks["compiler"]
    manager = wiki_mocks["manager"]
    manager.consume_confirmation.return_value = {
        "source_id": "s1",
        "plan_fingerprint": "fp",
        "source_content_sha256": "sh",
        "planned_titles": ["A"],
    }
    compiler.load_plan.return_value = PlanResult(
        source_id="s1",
        planned_pages=[],
        plan_fingerprint="fp",
        source_content_sha256="sh",
    )
    compiler.apply_ingest = AsyncMock()

    _set_context()
    result = await registry.get("wiki_apply_ingest").run({
        "source_id": "s1",
        "confirmation_id": "wcf_test",
        "approved_titles": ["A", "计划外页面"],
    })

    compiler.apply_ingest.assert_not_awaited()
    assert "计划外" in result


async def test_wiki_plan_ingest_returns_brief_content(wiki_mocks):
    from crew.wiki.schemas import PlannedPage, PlanResult

    registry = wiki_mocks["registry"]
    compiler = wiki_mocks["compiler"]
    long_content = "C" * 2000
    compiler.plan_ingest = AsyncMock(
        return_value=PlanResult(
            source_id="s1",
            planned_pages=[
                PlannedPage(title="source", page_type="source", action="create", content=long_content),
            ],
            total_new=1,
            total_update=0,
        )
    )

    _set_context()
    tool = registry.get("wiki_plan_ingest")
    result = await tool.run({"source_id": "s1"})

    assert "...(内容已省略" in result
    assert result.count("C") < 1000  # 不应包含完整长内容


async def test_wiki_plan_ingest_capacity_failure_does_not_issue_confirmation(wiki_mocks):
    from crew.wiki.schemas import PlanResult

    registry = wiki_mocks["registry"]
    compiler = wiki_mocks["compiler"]
    manager = wiki_mocks["manager"]
    compiler.plan_ingest = AsyncMock(
        return_value=PlanResult(
            source_id="s1",
            issues=["LLM 分析失败: 2/2 个分块全部失败；已解析内容仍保留，可稍后重试"],
        )
    )

    _set_context()
    result = await registry.get("wiki_plan_ingest").run({"source_id": "s1"})

    assert '"analysis_status": "failed"' in result
    assert '"retryable": true' in result
    assert "已解析 Markdown 与 Source 页面仍保留" in result
    manager.issue_confirmation.assert_not_called()


async def test_wiki_plan_ingest_auto_applies_when_enabled(wiki_mocks):
    from crew.wiki.schemas import IngestResult, PlanResult, WikiPage

    registry = wiki_mocks["registry"]
    compiler = wiki_mocks["compiler"]
    manager = wiki_mocks["manager"]
    config = wiki_mocks["config"]
    config.ingest.auto_apply = True
    compiler.plan_ingest = AsyncMock(
        return_value=PlanResult(source_id="s1", total_new=1, total_update=0)
    )
    page = WikiPage(
        id="ent_ticket",
        page_type="entity",
        title="机票",
        content="内容",
        file_path="entities/机票.md",
    )
    compiler.apply_ingest = AsyncMock(
        return_value=IngestResult(source_id="s1", pages=[page], issues=[])
    )

    _set_context()
    result = await registry.get("wiki_plan_ingest").run({"source_id": "s1"})

    assert '"auto_applied": true' in result
    assert "wiki.ingest.auto_apply=true" in result
    compiler.apply_ingest.assert_awaited_once()
    compiler.finalize_write.assert_not_called()
    manager.issue_confirmation.assert_not_called()


async def test_wiki_plan_ingest_returns_confirmation_when_auto_apply_disabled(wiki_mocks):
    from crew.wiki.schemas import PlanResult

    registry = wiki_mocks["registry"]
    compiler = wiki_mocks["compiler"]
    manager = wiki_mocks["manager"]
    manager.issue_confirmation.return_value = {
        "requires_confirmation": True,
        "confirmation_id": "wcf_manual",
    }
    compiler.plan_ingest = AsyncMock(
        return_value=PlanResult(source_id="s1", total_new=1, total_update=0)
    )
    compiler.apply_ingest = AsyncMock()

    _set_context()
    result = await registry.get("wiki_plan_ingest").run({"source_id": "s1"})

    assert '"requires_confirmation": true' in result
    assert '"confirmation_id": "wcf_manual"' in result
    compiler.apply_ingest.assert_not_awaited()
    manager.issue_confirmation.assert_called_once()


async def test_capture_attachment_only_accepts_current_turn_allowlist(tmp_path, fs_wiki):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    allowed = uploads / "current.md"
    allowed.write_text("# current", encoding="utf-8")
    old = uploads / "old.md"
    old.write_text("# old", encoding="utf-8")

    store = fs_wiki["store"]
    registry = fs_wiki["registry"]
    current_attachment_paths.set((str(allowed),))

    with patch("crew.gateway.context._get_upload_dir", return_value=uploads):
        rejected = await registry.get("wiki_capture_attachment").run({"path": str(old)})
        accepted = await registry.get("wiki_capture_attachment").run({"path": str(allowed)})

    assert "当前用户回合" in rejected
    assert '"source"' in accepted
    raws = store.list_raws(owner_account_id="owner", kb_id="default")
    assert len(raws) == 1
    assert raws[0].title == "current.md"
    current_attachment_paths.set(())


async def test_capture_attachment_uses_original_name_and_content_type_not_display_title(tmp_path, fs_wiki):
    from crew.core.runctx import current_attachment_files

    uploads = tmp_path / "uploads"
    uploads.mkdir()
    stored = uploads / "ticket_1234.pdf"
    stored.write_bytes(b"%PDF-1.7\nfake")

    store = fs_wiki["store"]
    registry = fs_wiki["registry"]
    current_attachment_paths.set((str(stored),))
    current_attachment_files.set(((str(stored), "机票预订单.pdf"),))

    with patch("crew.gateway.context._get_upload_dir", return_value=uploads):
        result = await registry.get("wiki_capture_attachment").run({"path": str(stored)})
        titled_result = await registry.get("wiki_capture_attachment").run({
            "path": str(stored),
            "title": "机票预订单",
        })

    assert '"source"' in result
    assert '"source"' in titled_result
    raws = store.list_raws(owner_account_id="owner", kb_id="default")
    assert {raw.title for raw in raws} == {"机票预订单.pdf", "机票预订单"}
    assert {raw.file_type for raw in raws} == {"application/pdf"}
    current_attachment_paths.set(())
    current_attachment_files.set(())


async def test_capture_text_marks_duplicate_before_publishing_second_source(fs_wiki):
    store = fs_wiki["store"]
    compiler = fs_wiki["compiler"]
    registry = fs_wiki["registry"]
    source_page = MagicMock()
    source_page.id = "source-page"
    source_page.to_dict.return_value = {"id": "source-page", "page_type": "source"}
    compiler.publish_source_page.return_value = source_page
    content = "这是一段足够长且完全相同的测试正文，用于验证解析完成后立即去重。"

    first = await registry.get("wiki_capture_text").run({"title": "第一份", "content": content})
    second = await registry.get("wiki_capture_text").run({"title": "第二份", "content": content})

    assert '"source_page"' in first
    assert '"duplicate": true' in second
    assert '"duplicate_of"' in second
    assert compiler.publish_source_page.call_count == 1
    raws = store.list_raws(owner_account_id="owner", kb_id="default")
    assert sum(raw.is_duplicate for raw in raws) == 1


async def test_refresh_source_keeps_same_version_and_creates_drift_version_on_change(
    fs_wiki,
):
    from crew.wiki.schemas import RawSource

    store = fs_wiki["store"]
    registry = fs_wiki["registry"]
    original = RawSource(
        id="url_original",
        title="示例页面",
        source_type="url",
        parsed_path="",
        source_url="https://example.com/article",
        source_kind="article",
        source_platform="web",
    )
    store.save_raw(original, owner_account_id="owner", kb_id="default")
    old_content = "这是旧版本网页正文，长度足够用于刷新内容哈希比较。"
    store.save_parsed_markdown(
        original.id,
        old_content,
        owner_account_id="owner",
        kb_id="default",
    )
    compiler = fs_wiki["compiler"]
    page = MagicMock()
    page.id = "source-new"
    page.to_dict.return_value = {"id": "source-new", "page_type": "source"}
    compiler.publish_source_page.return_value = page

    with patch("crew.wiki.tools.fetch_url_to_markdown", return_value=(old_content, original.source_url)):
        unchanged = await registry.get("wiki_refresh_source").run({"source_id": original.id})
    assert '"changed": false' in unchanged
    assert len(store.list_raws(owner_account_id="owner", kb_id="default")) == 1

    new_content = "这是已经变化的新版本网页正文，长度同样足够通过质量检查。"
    with patch("crew.wiki.tools.fetch_url_to_markdown", return_value=(new_content, original.source_url)):
        changed = await registry.get("wiki_refresh_source").run({"source_id": original.id})
    assert '"changed": true' in changed
    raws = store.list_raws(owner_account_id="owner", kb_id="default")
    assert len(raws) == 2
    new_raw = next(raw for raw in raws if raw.id != original.id)
    assert new_raw.drift_from == original.id
    assert new_raw.content_sha256 != store.load_raw(
        original.id,
        owner_account_id="owner",
        kb_id="default",
    ).content_sha256
    old = store.load_raw(original.id, owner_account_id="owner", kb_id="default")
    assert old.superseded_by == new_raw.id
    assert old.is_current is False
    assert new_raw.is_current is True


async def test_refresh_failure_preserves_old_immutable_version(fs_wiki):
    """刷新失败不改写旧版本的 parse_status/extraction_state，旧版本仍可用。"""
    from crew.wiki.schemas import RawSource

    store = fs_wiki["store"]
    registry = fs_wiki["registry"]
    original = RawSource(
        id="url_original",
        title="示例页面",
        source_type="url",
        parsed_path="",
        source_url="https://example.com/article",
        source_kind="article",
        source_platform="web",
    )
    store.save_raw(original, owner_account_id="owner", kb_id="default")
    store.save_parsed_markdown(
        original.id,
        "这是旧版本网页正文，长度足够用于刷新内容哈希比较。",
        owner_account_id="owner",
        kb_id="default",
    )

    with patch("crew.wiki.tools.fetch_url_to_markdown", side_effect=RuntimeError("blocked")):
        result = await registry.get("wiki_refresh_source").run({"source_id": original.id})

    assert '"extracted": false' in result
    assert '"changed": false' in result
    raws = store.list_raws(owner_account_id="owner", kb_id="default")
    assert len(raws) == 1  # 未创建失败的新来源
    old = store.load_raw(original.id, owner_account_id="owner", kb_id="default")
    # 旧版本不可变：状态保持可用，只记录刷新错误
    assert old.parse_status == "parsed"
    assert old.extraction_state == "available"
    assert old.last_refresh_error
    assert old.last_refresh_at > 0
    assert old.superseded_by is None


async def test_fetch_url_failure_persists_retryable_source_state(fs_wiki):
    store = fs_wiki["store"]
    registry = fs_wiki["registry"]

    with patch("crew.wiki.tools.fetch_url_to_markdown", side_effect=RuntimeError("blocked")):
        result = await registry.get("wiki_fetch_url").run(
            {"url": "https://example.com/private", "title": "受限页面"}
        )

    assert '"extracted": false' in result
    assert '"source_id"' in result
    raws = store.list_raws(owner_account_id="owner", kb_id="default")
    assert len(raws) == 1
    assert raws[0].parse_status == "failed"
    assert raws[0].extraction_state == "runtime_failed"
    assert "blocked" in str(raws[0].parse_error)


async def test_fetch_url_authorizes_initial_and_redirect_targets(fs_wiki, monkeypatch):
    from crew.security.outbound import PublicRedirectApprovalRequired
    from crew.wiki import tools as wiki_tools

    registry = fs_wiki["registry"]
    compiler = fs_wiki["compiler"]
    page = MagicMock()
    page.id = "source-redirect"
    page.to_dict.return_value = {"id": "source-redirect", "page_type": "source"}
    compiler.publish_source_page.return_value = page
    authorized = []
    redirected = "https://cdn.example.org/article"

    async def authorize(url, **_kwargs):
        authorized.append(url)

    calls = 0

    def fetch(url, _timeout, allowed):
        nonlocal calls
        calls += 1
        if ("cdn.example.org", 443, "https") not in allowed:
            raise PublicRedirectApprovalRequired(redirected)
        return "网页正文足够长，用于通过 Wiki 文本质量检查。", redirected

    monkeypatch.setattr(wiki_tools, "authorize_network_tool", authorize)
    monkeypatch.setattr(wiki_tools, "fetch_url_to_markdown", fetch)
    result = await registry.get("wiki_fetch_url").run(
        {"url": "https://example.com/a", "title": "示例"}
    )

    assert '"extracted": true' in result
    assert authorized == ["https://example.com/a", redirected]
    assert calls == 2


async def test_rename_and_delete_pages_repair_all_inbound_reference_forms(fs_wiki):
    from crew.wiki.schemas import WikiPage, WikiRelation

    store = fs_wiki["store"]
    manager = fs_wiki["manager"]
    registry = fs_wiki["registry"]
    target = store.save_page(
        WikiPage(
            id="",
            page_type="entity",
            title="旧标题",
            content="正文",
            file_path="",
        ),
        owner_account_id="owner",
    )
    referrer = store.save_page(
        WikiPage(
            id="",
            page_type="topic",
            title="引用页",
            content="参见 [[旧标题]]",
            file_path="",
            relations=[WikiRelation(target_page_id=target.id, relation="mentions")],
        ),
        owner_account_id="owner",
    )

    await registry.get("wiki_rename_page").run(
        {"page_id": target.id, "new_title": "新标题"}
    )
    renamed_referrer = store.get(
        referrer.id,
        owner_account_id="owner",
        kb_id="default",
    )
    assert renamed_referrer.related == []
    assert renamed_referrer.relations[0].target_page_id == target.id
    assert "[[新标题]]" in renamed_referrer.content

    manager.consume_confirmation.return_value = {"page_ids": [target.id]}
    deleted = await registry.get("wiki_delete_pages").run(
        {"page_ids": [target.id], "confirmation_id": "wcf_delete"}
    )
    assert '"updated_references": 1' in deleted
    cleaned = store.get(referrer.id, owner_account_id="owner", kb_id="default")
    assert cleaned.related == []
    assert cleaned.relations == []
    assert "[[新标题]]" not in cleaned.content


# ---- source 级操作的 KB 归属：跟随 source 所在知识库 ----


async def test_wiki_plan_ingest_follows_source_kb(wiki_mocks):
    """source 在 kb_work、会话活跃 KB 为 kb_active 时，plan 与确认卡都必须落到 kb_work。"""
    from crew.wiki.schemas import PlanResult

    registry = wiki_mocks["registry"]
    store = wiki_mocks["store"]
    compiler = wiki_mocks["compiler"]
    manager = wiki_mocks["manager"]
    store.find_source_kb.return_value = "kb_work"
    manager.issue_confirmation.return_value = {
        "requires_confirmation": True,
        "confirmation_id": "wcf_manual",
    }
    compiler.plan_ingest = AsyncMock(
        return_value=PlanResult(source_id="s1", total_new=1, total_update=0)
    )

    _set_context()
    await registry.get("wiki_plan_ingest").run({"source_id": "s1"})

    assert compiler.plan_ingest.call_args.kwargs["kb_id"] == "kb_work"
    assert manager.issue_confirmation.call_args.kwargs["kb_id"] == "kb_work"


async def test_wiki_apply_ingest_follows_source_kb(wiki_mocks):
    """apply 与 plan 不割裂：会话 KB 变化后仍按 source 所在 KB 校验确认卡并写入。"""
    from crew.wiki.schemas import PlanResult

    registry = wiki_mocks["registry"]
    store = wiki_mocks["store"]
    compiler = wiki_mocks["compiler"]
    manager = wiki_mocks["manager"]
    store.find_source_kb.return_value = "kb_work"
    manager.consume_confirmation.return_value = {
        "source_id": "s1",
        "plan_fingerprint": "fp",
        "source_content_sha256": "sh",
        "planned_titles": [],
    }
    compiler.load_plan.return_value = PlanResult(
        source_id="s1",
        planned_pages=[],
        plan_fingerprint="fp",
        source_content_sha256="sh",
    )
    ingest_result = MagicMock()
    ingest_result.source_id = "s1"
    ingest_result.pages = []
    ingest_result.issues = []
    compiler.apply_ingest = AsyncMock(return_value=ingest_result)

    _set_context()
    await registry.get("wiki_apply_ingest").run({
        "source_id": "s1",
        "confirmation_id": "wcf_test",
    })

    assert manager.consume_confirmation.call_args.kwargs["kb_id"] == "kb_work"
    assert compiler.apply_ingest.call_args.kwargs["kb_id"] == "kb_work"


async def test_wiki_plan_ingest_explicit_kb_overrides_source_location(wiki_mocks):
    """显式传入 kb_id 时优先级最高，不做 source 定位。"""
    from crew.wiki.schemas import PlanResult

    registry = wiki_mocks["registry"]
    store = wiki_mocks["store"]
    compiler = wiki_mocks["compiler"]
    compiler.plan_ingest = AsyncMock(
        return_value=PlanResult(source_id="s1", total_new=0, total_update=0)
    )

    _set_context()
    await registry.get("wiki_plan_ingest").run({"source_id": "s1", "kb_id": "kb_explicit"})

    assert compiler.plan_ingest.call_args.kwargs["kb_id"] == "kb_explicit"
    store.find_source_kb.assert_not_called()


async def test_find_source_kb_locates_raw_across_kbs(tmp_path):
    """FileSystemWikiStore.find_source_kb：只在真正存有 raw 的知识库中命中。"""
    from crew.wiki.schemas import RawSource

    store = FileSystemWikiStore(base_dir=tmp_path / "home")
    store.init_kb("owner", "default")
    store.init_kb("owner", "kb_work")
    store.save_raw(
        RawSource(id="s1", title="doc", source_type="upload", parsed_path=""),
        owner_account_id="owner",
        kb_id="kb_work",
    )

    assert store.find_source_kb("s1", owner_account_id="owner") == "kb_work"
    assert store.find_source_kb("missing", owner_account_id="owner") is None
