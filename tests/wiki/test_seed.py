"""内置教程知识库初始化（seed）测试。"""

from __future__ import annotations

import re

from crew.wiki.schemas import WikiPage
from crew.wiki.seed import TUTORIAL_KB_ID, TUTORIAL_KB_NAME, ensure_tutorial_kb
from crew.wiki.store import FileSystemWikiStore


def _make_store(tmp_path) -> FileSystemWikiStore:
    return FileSystemWikiStore(base_dir=tmp_path)


def _page_count(store: FileSystemWikiStore) -> int:
    return len(list(store._iter_pages("", TUTORIAL_KB_ID)))


def test_app_build_does_not_seed_tutorial_kb(monkeypatch, tmp_path):
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

    assert TUTORIAL_KB_ID not in {kb.id for kb in app._wiki_store.list_kbs()}
    assert not (crew_home / ".tutorial_kb_seeded").exists()


def test_seed_creates_tutorial_kb(tmp_path):
    store = _make_store(tmp_path)
    assert ensure_tutorial_kb(store) is True

    kbs = {kb.id: kb for kb in store.list_kbs()}
    assert TUTORIAL_KB_ID in kbs
    assert kbs[TUTORIAL_KB_ID].name == TUTORIAL_KB_NAME
    # 种子内容只使用当前支持的 entity/topic 页面类型。
    assert _page_count(store) == 13
    assert {page.page_type for page in store._iter_pages("", TUTORIAL_KB_ID)} <= {
        "entity",
        "topic",
    }
    # 教程专属 SCHEMA.md 已覆写默认模板
    schema = (tmp_path / "wiki_lib" / TUTORIAL_KB_ID / "SCHEMA.md").read_text(encoding="utf-8")
    assert "教程" in schema
    home = (tmp_path / "wiki_lib" / TUTORIAL_KB_ID / "Home.md").read_text(encoding="utf-8")
    assert "共 13 个页面" in home
    assert "这个知识库还没有内容" not in home


def test_seed_writes_prebuilt_summary(tmp_path):
    """教程库初始化即带手写概览（status=ready），无需 LLM 调用。"""
    store = _make_store(tmp_path)
    assert ensure_tutorial_kb(store) is True

    summary = store.get_kb_summary("", TUTORIAL_KB_ID)
    assert summary.status == "ready"
    assert summary.summary
    assert "LLM Wiki" in summary.summary
    assert summary.page_count == 13
    assert summary.source_count == 0
    # content_hash 与 WikiSummarizer 的刷新判定同源，内容未变时不会触发重生成。
    from crew.wiki.summary import WikiSummarizer

    pages = store.list_all(owner_account_id="", kb_id=TUTORIAL_KB_ID, limit=10000)
    assert summary.content_hash == WikiSummarizer._compute_content_hash(pages, [])

    # Home.md 导读同样预置（status=ready），且渲染进 Home.md 而非占位文案。
    intro = store.get_home_intro("", TUTORIAL_KB_ID)
    assert intro.status == "ready"
    assert intro.text
    assert intro.content_hash == summary.content_hash
    assert len(intro.questions) == 3
    home = (tmp_path / "wiki_lib" / TUTORIAL_KB_ID / "Home.md").read_text(encoding="utf-8")
    assert intro.text in home
    assert "导读整理中" not in home
    # 推荐问题小节渲染进 Home.md（桌面端会转成可点击的提问按钮）。
    assert "## 推荐问题" in home
    for question in intro.questions:
        assert question in home


def test_seed_pages_searchable(tmp_path):
    store = _make_store(tmp_path)
    ensure_tutorial_kb(store)
    results = store.search("知识图谱", top_k=5, kb_id=TUTORIAL_KB_ID)
    assert results, "教程页面应能被 FTS 检索命中"


def test_seed_wiki_links_have_existing_targets(tmp_path):
    store = _make_store(tmp_path)
    ensure_tutorial_kb(store)
    pages = list(store._iter_pages("", TUTORIAL_KB_ID))
    titles = {page.title for page in pages}
    broken = {
        (page.title, target)
        for page in pages
        for target in re.findall(r"\[\[([^\]]+)\]\]", page.content)
        if target not in titles
    }
    assert broken == set()


def test_seed_is_idempotent(tmp_path):
    store = _make_store(tmp_path)
    assert ensure_tutorial_kb(store) is True
    assert ensure_tutorial_kb(store) is False
    assert _page_count(store) == 13


def test_tutorial_kb_cannot_be_deleted_but_pages_remain_editable(tmp_path):
    store = _make_store(tmp_path)
    ensure_tutorial_kb(store)
    page = next(store._iter_pages("", TUTORIAL_KB_ID))
    page.content = "用户修改后的教程内容"
    assert store.update(page, kb_id=TUTORIAL_KB_ID) is not None
    assert store.get(page.id, kb_id=TUTORIAL_KB_ID).content == "用户修改后的教程内容"

    try:
        store.delete_kb(TUTORIAL_KB_ID)
    except ValueError as exc:
        assert "禁止删除 tutorial" in str(exc)
    else:
        raise AssertionError("教程知识库必须禁止删除")


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
