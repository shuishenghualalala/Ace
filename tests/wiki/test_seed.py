"""内置教程知识库初始化（seed）测试。"""

from __future__ import annotations

import shutil

from crew.wiki.schemas import WikiPage
from crew.wiki.seed import TUTORIAL_KB_ID, TUTORIAL_KB_NAME, ensure_tutorial_kb
from crew.wiki.store import FileSystemWikiStore


def _make_store(tmp_path) -> FileSystemWikiStore:
    return FileSystemWikiStore(base_dir=tmp_path)


def _page_count(store: FileSystemWikiStore) -> int:
    return len(list(store._iter_pages("", TUTORIAL_KB_ID)))


def test_seed_creates_tutorial_kb(tmp_path):
    store = _make_store(tmp_path)
    assert ensure_tutorial_kb(store) is True

    kbs = {kb.id: kb for kb in store.list_kbs()}
    assert TUTORIAL_KB_ID in kbs
    assert kbs[TUTORIAL_KB_ID].name == TUTORIAL_KB_NAME
    # 种子内容：14 个教程页面
    assert _page_count(store) == 14
    # 教程专属 SCHEMA.md 已覆写默认模板
    schema = (tmp_path / "wiki_lib" / TUTORIAL_KB_ID / "SCHEMA.md").read_text(encoding="utf-8")
    assert "教程" in schema


def test_seed_pages_searchable(tmp_path):
    store = _make_store(tmp_path)
    ensure_tutorial_kb(store)
    results = store.search("知识图谱", top_k=5, kb_id=TUTORIAL_KB_ID)
    assert results, "教程页面应能被 FTS 检索命中"


def test_seed_is_idempotent(tmp_path):
    store = _make_store(tmp_path)
    assert ensure_tutorial_kb(store) is True
    assert ensure_tutorial_kb(store) is False
    assert _page_count(store) == 14


def test_seed_does_not_resurrect_after_user_delete(tmp_path):
    store = _make_store(tmp_path)
    ensure_tutorial_kb(store)
    # 用户删除教程库（标记文件仍在）→ 不再重建
    shutil.rmtree(tmp_path / "wiki_lib" / TUTORIAL_KB_ID)
    assert ensure_tutorial_kb(store) is False
    kbs = {kb.id for kb in store.list_kbs()}
    assert TUTORIAL_KB_ID not in kbs


def test_seed_does_not_overwrite_existing_kb(tmp_path):
    store = _make_store(tmp_path)
    # 用户已手动建了同名 KB 并写入一个页面
    store.create_kb(TUTORIAL_KB_ID, "我自己的库")
    store.save_page(WikiPage(id="", page_type="topic", title="私有页面", content="x", file_path=""),
                    kb_id=TUTORIAL_KB_ID)
    assert ensure_tutorial_kb(store) is False
    # 内容未被种子覆盖
    assert _page_count(store) == 1
    kb = {k.id: k for k in store.list_kbs()}[TUTORIAL_KB_ID]
    assert kb.name == "我自己的库"
