"""聊天附件自动收入 Wiki 知识库（crew.wiki.capture）测试。"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from crew.wiki.capture import capture_upload_to_wiki
from crew.wiki.config import WikiConfig, WikiMultimodalConfig
from crew.wiki.store import FileSystemWikiStore


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = FileSystemWikiStore(base_dir=tmp)
        s.init_kb()
        yield s


async def test_capture_document_parsed_into_default_kb(store):
    """文档类附件：保存原文件并解析成 markdown，登记到 default 知识库。"""
    raw = await capture_upload_to_wiki(
        store, None, WikiConfig(), "note.txt", b"hello wiki", owner_account_id="A:uid-a"
    )
    assert raw is not None
    assert raw.parse_status == "parsed"

    saved = store.load_raw(raw.id, "A:uid-a", "default")
    assert saved is not None
    assert saved.title == "note.txt"
    assert saved.source_type == "upload"
    assert saved.parse_status == "parsed"
    assert saved.original_path and Path(saved.original_path).exists()
    assert saved.parsed_path
    assert "hello wiki" in Path(saved.parsed_path).read_text(encoding="utf-8")


async def test_capture_document_parse_failure_marks_failed(store, monkeypatch):
    """解析失败：不抛异常，raw 标记 failed 供 Agent / 用户挽救。"""
    async def _boom(content, filename):
        raise RuntimeError("boom")

    monkeypatch.setattr("crew.wiki.capture.parse_document_from_bytes_async", _boom)
    raw = await capture_upload_to_wiki(
        store, None, WikiConfig(), "note.txt", b"hello", owner_account_id="A:uid-a"
    )
    assert raw is not None
    saved = store.load_raw(raw.id, "A:uid-a", "default")
    assert saved is not None
    assert saved.parse_status == "failed"
    assert "boom" in (saved.parse_error or "")
    # 原文件仍保留
    assert saved.original_path and Path(saved.original_path).exists()


async def test_capture_image_multimodal_disabled_only_saves_original(store, monkeypatch):
    """多模态未启用：图片只保存原文件，不调用 describe_media。"""
    describe = Mock(side_effect=AssertionError("不应调用 describe_media"))
    monkeypatch.setattr("crew.wiki.capture.describe_media", describe)
    cfg = WikiConfig(multimodal=WikiMultimodalConfig(enabled=False))

    raw = await capture_upload_to_wiki(
        store, None, cfg, "shot.png", b"\x89PNG-fake", owner_account_id="A:uid-a"
    )
    assert raw is not None
    assert raw.source_type == "image"
    describe.assert_not_called()
    saved = store.load_raw(raw.id, "A:uid-a", "default")
    assert saved is not None
    assert saved.parse_status == "pending"
    assert saved.original_path and Path(saved.original_path).exists()


async def test_capture_image_auto_describes_but_not_auto_ingests(store, monkeypatch):
    """auto_image 开启：图片自动多模态理解并生成轻量元数据，但默认不自动深度 ingest。"""
    describe = Mock(return_value="一张截图的描述")
    monkeypatch.setattr("crew.wiki.capture.describe_media", describe)
    compiler = SimpleNamespace(ingest=AsyncMock())

    raw = await capture_upload_to_wiki(
        store, compiler, WikiConfig(), "shot.png", b"\x89PNG-fake", owner_account_id="A:uid-a"
    )
    assert raw is not None
    describe.assert_called_once()
    compiler.ingest.assert_not_awaited()
    saved = store.load_raw(raw.id, "A:uid-a", "default")
    assert saved is not None
    assert saved.parse_status == "parsed"
    assert "一张截图的描述" in Path(saved.parsed_path).read_text(encoding="utf-8")


async def test_capture_image_auto_ingests_when_explicitly_enabled(store, monkeypatch):
    """显式开启 auto_ingest：图片多模态理解后自动深度 ingest。"""
    describe = Mock(return_value="一张截图的描述")
    monkeypatch.setattr("crew.wiki.capture.describe_media", describe)
    compiler = SimpleNamespace(ingest=AsyncMock())
    cfg = WikiConfig()
    cfg.ingest.auto_ingest = True

    raw = await capture_upload_to_wiki(
        store, compiler, cfg, "shot.png", b"\x89PNG-fake", owner_account_id="A:uid-a"
    )
    assert raw is not None
    describe.assert_called_once()
    compiler.ingest.assert_awaited_once_with(
        raw.id, owner_account_id="A:uid-a", kb_id="default"
    )


async def test_capture_image_describe_failure_marks_failed(store, monkeypatch):
    """多模态理解失败：不抛异常，raw 标记 failed。"""
    describe = Mock(side_effect=RuntimeError("llm down"))
    monkeypatch.setattr("crew.wiki.capture.describe_media", describe)
    compiler = SimpleNamespace(ingest=AsyncMock())

    raw = await capture_upload_to_wiki(
        store, compiler, WikiConfig(), "shot.png", b"\x89PNG-fake", owner_account_id="A:uid-a"
    )
    assert raw is not None
    compiler.ingest.assert_not_awaited()
    saved = store.load_raw(raw.id, "A:uid-a", "default")
    assert saved is not None
    assert saved.parse_status == "failed"
    assert "llm down" in (saved.parse_error or "")


async def test_capture_skips_empty_content(store):
    assert await capture_upload_to_wiki(store, None, WikiConfig(), "a.txt", b"") is None
    assert store.list_raws("A:uid-a", "default") == []


async def test_capture_document_publishes_source_page(store):
    """文档解析成功后发布全文来源页：Wiki 树只渲染 page,仅落 raw source 不可见。"""
    from crew.core.mocks import FakeProvider
    from crew.wiki.compiler import WikiCompiler

    compiler = WikiCompiler(store=store, provider=FakeProvider())
    raw = await capture_upload_to_wiki(
        store, compiler, WikiConfig(), "note.txt", b"hello wiki", owner_account_id="A:uid-a"
    )
    assert raw is not None
    assert raw.parse_status == "parsed"

    page = store.get_source_page(raw.id, "A:uid-a", "default")
    assert page is not None
    assert page.page_type == "source"
    assert "hello wiki" in page.content


async def test_capture_document_publish_failure_does_not_break_capture(store):
    """来源页发布失败不影响 capture 主链路：raw 仍为 parsed。"""
    compiler = SimpleNamespace(
        publish_source_page=Mock(side_effect=RuntimeError("index broken"))
    )
    raw = await capture_upload_to_wiki(
        store, compiler, WikiConfig(), "note.txt", b"hello wiki", owner_account_id="A:uid-a"
    )
    assert raw is not None
    assert raw.parse_status == "parsed"


async def test_capture_never_raises(store, monkeypatch):
    """底层异常（如磁盘错误）被吞掉并返回 None，不影响上传主链路。"""
    monkeypatch.setattr(store, "_source_dir", Mock(side_effect=RuntimeError("disk full")))
    assert (
        await capture_upload_to_wiki(store, None, WikiConfig(), "a.txt", b"x")
        is None
    )


async def test_capture_uses_atomic_bounded_write(store, monkeypatch):
    import crew.wiki.capture as capture_module

    monkeypatch.setattr(capture_module, "_MAX_CAPTURE_BYTES", 4, raising=False)
    original_write_bytes = Path.write_bytes

    def forbidden_write_bytes(self, data):
        raise AssertionError(f"Wiki capture used Path.write_bytes: {self}")

    monkeypatch.setattr(Path, "write_bytes", forbidden_write_bytes)
    try:
        assert await capture_upload_to_wiki(
            store,
            None,
            WikiConfig(),
            "note.txt",
            b"safe",
            owner_account_id="A:uid-a",
        ) is not None
    finally:
        monkeypatch.setattr(Path, "write_bytes", original_write_bytes)

    assert await capture_upload_to_wiki(
        store,
        None,
        WikiConfig(),
        "too-large.txt",
        b"12345",
        owner_account_id="A:uid-a",
    ) is None
# ---------------------------------------------------------------------------
# 文本捕获流水线（capture_text_source / save_parsed_source）
# ---------------------------------------------------------------------------

from crew.wiki.capture import (  # noqa: E402
    CaptureError,
    CaptureValidationError,
    capture_text_source,
)
from crew.wiki.compiler import WikiCompiler  # noqa: E402


@pytest.fixture
def compiler(store):
    from crew.core.mocks import FakeProvider

    return WikiCompiler(store=store, provider=FakeProvider())


def test_capture_text_source_classifies_note_by_default(store, compiler):
    """无来源平台的粘贴文本归为 note。"""
    outcome = capture_text_source(
        store, compiler, title="随手记", content="一段普通笔记正文。",
        owner_account_id="A:uid-a", kb_id="default",
    )
    assert outcome.duplicate is None
    assert outcome.page is not None
    assert outcome.raw.source_kind == "note"
    assert outcome.raw.adapter_name == "builtin-text"


def test_capture_text_source_classifies_article_for_known_platform(store, compiler):
    """已知内容平台（web/微信/知乎等）归为 article，与 wiki_capture_text 工具一致。"""
    outcome = capture_text_source(
        store, compiler, title="标签页", content="浏览器标签页正文。",
        owner_account_id="A:uid-a", kb_id="default",
        source_platform="web", source_url="https://example.com/a",
    )
    assert outcome.raw.source_kind == "article"
    assert outcome.raw.source_platform == "web"
    assert outcome.raw.source_url == "https://example.com/a"


def test_capture_text_source_session_kind(store, compiler):
    """会话沉淀归为 session，平台与适配器随会话来源。"""
    outcome = capture_text_source(
        store, compiler, title="会话 s1", content="## user\n\n你好",
        owner_account_id="A:uid-a", kb_id="default",
        source_type="session", session_id="s1",
    )
    assert outcome.raw.source_kind == "session"
    assert outcome.raw.source_platform == "crew"
    assert outcome.raw.adapter_name == "builtin-session"
    assert outcome.raw.session_id == "s1"


def test_capture_text_source_empty_rejected_before_save(store, compiler):
    """空标题/内容在落库前拒绝：不带 source_id，store 无残留。"""
    with pytest.raises(CaptureValidationError) as excinfo:
        capture_text_source(
            store, compiler, title="  ", content="",
            owner_account_id="A:uid-a", kb_id="default",
        )
    assert excinfo.value.source_id == ""
    assert store.list_raws("A:uid-a", "default") == []


def test_capture_text_source_quality_failure_is_validation_error(store, compiler):
    """乱码内容校验失败归为 400 语义（CaptureValidationError 是 ValueError），raw 已落库可挽救。"""
    with pytest.raises(CaptureValidationError) as excinfo:
        capture_text_source(
            store, compiler, title="坏文件", content="abc\ufffd\ufffd\ufffd",
            owner_account_id="A:uid-a", kb_id="default",
        )
    assert isinstance(excinfo.value, ValueError)
    source_id = excinfo.value.source_id
    assert source_id.startswith("paste_")
    assert store.load_raw(source_id, "A:uid-a", "default") is not None


def test_capture_text_source_pipeline_failure_carries_source_id(store):
    """发布等下游失败归为 CaptureError，带出已落库的 source_id 供挽救。"""
    broken = SimpleNamespace(
        publish_source_page=Mock(side_effect=RuntimeError("index broken")),
        finalize_write=Mock(),
    )
    with pytest.raises(CaptureError) as excinfo:
        capture_text_source(
            store, broken, title="标签页", content="正常正文内容。",
            owner_account_id="A:uid-a", kb_id="default",
        )
    assert "index broken" in str(excinfo.value)
    source_id = excinfo.value.source_id
    assert source_id.startswith("paste_")
    saved = store.load_raw(source_id, "A:uid-a", "default")
    assert saved is not None
    assert saved.parse_status == "parsed"


def test_capture_text_source_duplicate_skips_publish(store, compiler):
    """相同内容重复捕获：第二次只落库不发页，duplicate 指向首次来源。"""
    first = capture_text_source(
        store, compiler, title="第一份", content="完全相同的正文内容。",
        owner_account_id="A:uid-a", kb_id="default",
    )
    second = capture_text_source(
        store, compiler, title="第二份", content="完全相同的正文内容。",
        owner_account_id="A:uid-a", kb_id="default",
    )
    assert first.duplicate is None
    assert second.duplicate is not None
    assert second.duplicate.id == first.raw.id
    assert second.page is None
    assert store.get_source_page(second.raw.id, "A:uid-a", "default") is None
