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
    def _boom(content, filename):
        raise RuntimeError("boom")

    monkeypatch.setattr("crew.wiki.capture.parse_document_from_bytes", _boom)
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


async def test_capture_image_auto_describes_and_ingests(store, monkeypatch):
    """auto_image 开启：图片自动多模态理解并 ingest（与 /api/wiki/upload 一致）。"""
    describe = Mock(return_value="一张截图的描述")
    monkeypatch.setattr("crew.wiki.capture.describe_media", describe)
    compiler = SimpleNamespace(ingest=AsyncMock())

    raw = await capture_upload_to_wiki(
        store, compiler, WikiConfig(), "shot.png", b"\x89PNG-fake", owner_account_id="A:uid-a"
    )
    assert raw is not None
    describe.assert_called_once()
    compiler.ingest.assert_awaited_once_with(
        raw.id, owner_account_id="A:uid-a", kb_id="default"
    )
    saved = store.load_raw(raw.id, "A:uid-a", "default")
    assert saved is not None
    assert saved.parse_status == "parsed"
    assert "一张截图的描述" in Path(saved.parsed_path).read_text(encoding="utf-8")


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


async def test_capture_never_raises(store, monkeypatch):
    """底层异常（如磁盘错误）被吞掉并返回 None，不影响上传主链路。"""
    monkeypatch.setattr(store, "_source_dir", Mock(side_effect=RuntimeError("disk full")))
    assert (
        await capture_upload_to_wiki(store, None, WikiConfig(), "a.txt", b"x")
        is None
    )
