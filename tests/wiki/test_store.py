import tempfile
import time as time_mod
from pathlib import Path

import pytest

from crew.wiki.schemas import HomeIntro, RawSource, WikiPage, WikiRelation
from crew.wiki.store import FileSystemWikiStore


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as tmp:
        yield FileSystemWikiStore(base_dir=tmp)


def test_init_kb_creates_directories_and_defaults(store: FileSystemWikiStore):
    store.init_kb()
    base = store._dir()
    assert (base / "wiki" / "entities").is_dir()
    assert not (base / "wiki" / "concepts").exists()
    assert (base / "wiki" / "topics").is_dir()
    assert (base / "wiki" / "sources").is_dir()
    for source_dir in (
        "articles",
        "pdfs",
        "words",
        "excels",
        "ppts",
        "notes",
        "sessions",
        "images",
        "videos",
        "assets",
    ):
        assert (base / "wiki" / "sources" / source_dir).is_dir()
    assert (base / "wiki" / "comparisons").is_dir()
    assert (base / "wiki" / "synthesis").is_dir()
    assert (base / ".wiki-schema.md").exists()
    assert (base / "Home.md").exists()
    assert (base / "index.md").exists()
    assert (base / "log.md").exists()
    home_text = (base / "Home.md").read_text(encoding="utf-8")
    index_text = (base / "index.md").read_text(encoding="utf-8")
    assert home_text.startswith("# 知识库概览")
    assert "> default" in home_text
    assert "这个知识库还没有内容" in home_text
    assert "上传文件" in home_text
    assert index_text == "# 知识导航\n\n暂无页面。\n"
    assert "Crew" not in home_text
    assert "Crew" not in index_text
    schema_text = (base / ".wiki-schema.md").read_text(encoding="utf-8")
    assert "不按素材长度设置固定的关键词或话题数量上限" in schema_text
    assert "最多生成 5 个关键词和 3 个话题" not in schema_text


def test_init_kb_migrates_generated_fixed_knowledge_limits(store: FileSystemWikiStore):
    store.init_kb(kb_id="legacy-quota")
    schema_path = store._dir(kb_id="legacy-quota") / ".wiki-schema.md"
    schema_path.write_text(
        "# 知识库维护规则\n\n## 编译规则\n"
        "- 长 source 整篇最多生成 5 个关键词和 3 个话题；短 source 最多 3 个关键词且不生成话题\n"
        "- 用户自定义规则\n",
        encoding="utf-8",
    )

    store.init_kb(kb_id="legacy-quota")

    schema_text = schema_path.read_text(encoding="utf-8")
    assert "不按素材长度设置固定的关键词或话题数量上限" in schema_text
    assert "用户自定义规则" in schema_text
    assert "最多生成 5 个关键词和 3 个话题" not in schema_text


def test_init_kb_migrates_legacy_generated_empty_home_without_overwriting_custom_home(
    store: FileSystemWikiStore,
):
    store.init_kb(kb_id="legacy")
    base = store._dir(kb_id="legacy")
    legacy_home = """# legacy

> Crew 持续把原始素材编译为可追溯、互相关联的长期知识。

- 原始素材：0
- 实体：0
- 主题：0
- 来源摘要：0
- 对比分析：0
- 综合报告：0
- 暂无页面
"""
    (base / "Home.md").write_text(legacy_home, encoding="utf-8")
    (base / "index.md").write_text("# Crew Wiki\n\n## 实体\n\n## 主题\n\n## 来源\n", encoding="utf-8")

    store.init_kb(kb_id="legacy")

    migrated_home = (base / "Home.md").read_text(encoding="utf-8")
    assert migrated_home.startswith("# 知识库概览")
    assert "这个知识库还没有内容" in migrated_home
    migrated_index = (base / "index.md").read_text(encoding="utf-8")
    assert "# 知识导航" in migrated_index
    assert "## 关键词" in migrated_index
    assert "## 话题" in migrated_index
    assert "## 来源摘要" in migrated_index

    (base / "Home.md").write_text("# 我的首页\n\n自定义内容\n", encoding="utf-8")
    store.init_kb(kb_id="legacy")
    assert (base / "Home.md").read_text(encoding="utf-8") == "# 我的首页\n\n自定义内容\n"


def test_update_home_writes_overview_sections(store: FileSystemWikiStore):
    store.init_kb()
    topic = store.save_page(
        WikiPage(
            id="topic_1",
            page_type="topic",
            title="大语言模型",
            content=(
                "# 大语言模型\n\n"
                "> 大语言模型是基于 Transformer 的生成式模型。\n\n"
                "大语言模型通过海量语料预训练获得通用语言能力，"
                "再通过指令微调对齐人类偏好。\n\n"
                "## 正文\n\n更多细节。"
            ),
            file_path="",
            sources=["s1", "s2", "s3"],
        ),
        "",
        "default",
    )
    store.save_page(
        WikiPage(
            id="entity_1",
            page_type="entity",
            title="Transformer",
            content=(
                "# Transformer\n\n"
                "> Transformer 是自注意力架构。\n\n"
                "Transformer 通过多头注意力并行捕获长距离依赖。"
            ),
            file_path="",
            sources=["s1"],
        ),
        "",
        "default",
    )
    store.save_raw(
        RawSource(
            id="s1",
            title="论文.pdf",
            source_type="upload",
            parsed_path="x",
            parse_status="parsed",
        ),
        "",
        "default",
    )
    store.set_home_intro(
        HomeIntro(
            text="这个知识库聚焦大语言模型，涵盖架构原理与训练方法。",
            questions=["Transformer 的自注意力机制是怎么工作的？", "指令微调解决了什么问题？"],
            content_hash="h1",
            generated_at=1.0,
            status="ready",
        ),
        "",
        "default",
    )

    store.update_home()
    home = (store._dir() / "Home.md").read_text(encoding="utf-8")

    # 导读：紧跟元信息行，不再有「内容导读」小标题
    assert "## 内容导读" not in home
    assert "这个知识库聚焦大语言模型" in home
    # 推荐问题：位于导读之后、知识地图之前
    assert "## 推荐问题" in home
    assert "- Transformer 的自注意力机制是怎么工作的？" in home
    assert home.index("## 推荐问题") < home.index("## 知识地图")
    assert home.index("这个知识库聚焦大语言模型") < home.index("## 推荐问题")
    # 知识地图：话题页排在关键词页之前，带来源数与较长介绍
    assert "## 知识地图" in home
    assert home.index("### [[大语言模型]]") < home.index("### [[Transformer]]")
    topic_date = time_mod.strftime("%Y-%m-%d", time_mod.localtime(topic.updated_at))
    assert f"> 3 个来源 · 更新于 {topic_date}" in home
    assert "大语言模型通过海量语料预训练获得通用语言能力" in home
    # 快速导航：计数表 + index.md 指引
    assert "| 原始素材 | 1 |" in home
    assert "| 关键词 | 1 |" in home
    assert "| 话题 | 1 |" in home
    assert "index.md" in home
    # 最近更新
    assert "## 最近更新" in home
    assert "- [[Transformer]] · 关键词 ·" in home


def test_update_home_uses_placeholder_when_intro_missing(store: FileSystemWikiStore):
    store.init_kb()
    store.save_page(
        WikiPage(
            id="topic_1",
            page_type="topic",
            title="主题",
            content="# 主题\n\n内容。",
            file_path="",
        ),
        "",
        "default",
    )

    store.update_home()
    home = (store._dir() / "Home.md").read_text(encoding="utf-8")

    assert "导读整理中" in home
    # 没有推荐问题时不生成该小节
    assert "## 推荐问题" not in home


def test_init_kb_under_wiki_lib_default(store: FileSystemWikiStore):
    store.init_kb()
    assert store._dir().relative_to(store._owner_home()) == Path("wiki_lib/default")


def test_configured_storage_root_preserves_owner_isolation(tmp_path):
    store = FileSystemWikiStore(storage_root=tmp_path)
    store.init_kb(owner_account_id="A:alice", kb_id="default")
    store.init_kb(owner_account_id="A:bob", kb_id="default")

    alice = store._dir("A:alice", "default")
    bob = store._dir("A:bob", "default")
    assert alice != bob
    assert alice.parents[3] == tmp_path
    assert bob.parents[3] == tmp_path
    assert alice.name == "default"


@pytest.mark.parametrize("kb_id", ["../escape", "a/b", "a\\b", ".", "知识 库"])
def test_kb_id_rejects_unsafe_path_segments(store: FileSystemWikiStore, kb_id: str):
    with pytest.raises(ValueError, match="kb_id"):
        store.init_kb(kb_id=kb_id)


def test_list_kbs_includes_default_after_init(store: FileSystemWikiStore):
    store.init_kb()
    kbs = store.list_kbs()
    assert any(kb.id == "default" for kb in kbs)


def test_list_kbs_default_kb_display_name(store: FileSystemWikiStore):
    """默认知识库展示名为「我的工作」，kb_id 标识符仍为 default。"""
    store.init_kb()
    kbs = store.list_kbs()
    default_kb = next(kb for kb in kbs if kb.id == "default")
    assert default_kb.name == "我的工作"


def test_update_home_uses_default_kb_display_name(store: FileSystemWikiStore):
    """空默认知识库的 Home.md 头部应展示「我的工作」。"""
    store.init_kb()
    store.update_home()
    home = (store._dir() / "Home.md").read_text(encoding="utf-8")
    assert "我的工作" in home


def test_create_and_delete_kb(store: FileSystemWikiStore):
    store.init_kb()
    kb = store.create_kb("project_a", name="Project A")
    assert kb.id == "project_a"
    assert kb.name == "Project A"
    assert (store._kb_root() / "project_a").is_dir()

    kbs = store.list_kbs()
    assert any(k.id == "project_a" for k in kbs)
    with pytest.raises(ValueError, match="已存在"):
        store.create_kb("project_a", name="Duplicate")

    assert store.delete_kb("project_a") is True
    assert store.delete_kb("project_a") is False
    assert not (store._kb_root() / "project_a").exists()
    recreated = store.create_kb("project_a", name="Fresh")
    assert recreated.name == "Fresh"
    assert store.list_all(kb_id="project_a") == []


def test_create_and_delete_kb_with_chinese_id(store: FileSystemWikiStore):
    store.init_kb()

    kb = store.create_kb("产品知识库", name="产品知识库")

    assert kb.id == "产品知识库"
    assert kb.name == "产品知识库"
    assert kb.vault_path == str((store._kb_root() / "产品知识库").resolve())
    assert (store._kb_root() / "产品知识库" / "Home.md").is_file()
    assert any(item.id == "产品知识库" for item in store.list_kbs())
    assert store.delete_kb("产品知识库") is True


def test_kb_id_normalizes_unicode_and_rejects_unsafe_paths(store: FileSystemWikiStore):
    kb = store.create_kb("Cafe\u0301", name="法语知识库")

    assert kb.id == "Café"
    assert (store._kb_root() / "Café").is_dir()
    for unsafe in ("产品 知识库", "../产品", "产品/知识库", "产品.知识库"):
        with pytest.raises(ValueError):
            store.create_kb(unsafe)


def test_cannot_create_default_kb(store: FileSystemWikiStore):
    with pytest.raises(ValueError):
        store.create_kb("default")


def test_cannot_delete_default_kb(store: FileSystemWikiStore):
    with pytest.raises(ValueError):
        store.delete_kb("default")


def test_kb_page_isolation(store: FileSystemWikiStore):
    store.init_kb(kb_id="default")
    store.init_kb(kb_id="project_a")

    store.save_page(WikiPage(id="", page_type="topic", title="Shared", content="default content", file_path=""), kb_id="default")
    store.save_page(WikiPage(id="", page_type="topic", title="Shared", content="project content", file_path=""), kb_id="project_a")

    default_pages = store.list_all(kb_id="default")
    project_pages = store.list_all(kb_id="project_a")
    assert len(default_pages) == 1
    assert len(project_pages) == 1
    assert default_pages[0].content == "default content"
    assert project_pages[0].content == "project content"


def test_save_page_assigns_id_and_file_path(store: FileSystemWikiStore):
    page = WikiPage(
        id="",
        page_type="topic",
        title="测试页面",
        content="这是内容。",
        file_path="",
    )
    saved = store.save_page(page)
    assert saved.id
    assert saved.file_path
    assert saved.created_at
    assert saved.updated_at
    loaded = store.get(saved.id)
    assert loaded is not None
    assert loaded.title == "测试页面"
    assert loaded.content == "这是内容。"


def test_from_dict_ignores_legacy_status_key():
    """旧磁盘数据（status 体系已删除）中的多余键必须被忽略。"""
    legacy = {
        "id": "p1",
        "page_type": "topic",
        "title": "旧页面",
        "content": "正文",
        "file_path": "topics/旧页面.md",
        "status": "deprecated",
        "tags": [],
    }
    page = WikiPage.from_dict(legacy)
    assert page.id == "p1"
    assert page.title == "旧页面"
    assert not hasattr(page, "status")
    # draft 是更早期数据里的取值，同样忽略
    assert WikiPage.from_dict({**legacy, "status": "draft"}).title == "旧页面"


def test_load_page_file_with_legacy_status_frontmatter(store: FileSystemWikiStore):
    """旧 KB 目录里带 status frontmatter 的页面 .md 能正常加载。"""
    saved = store.save_page(
        WikiPage(id="", page_type="topic", title="旧页面", content="旧正文", file_path="")
    )
    page_file = store._dir() / saved.file_path
    text = page_file.read_text(encoding="utf-8")
    assert "status" not in text
    page_file.write_text(text.replace("---\n", "---\nstatus: deprecated\n", 1), encoding="utf-8")

    loaded = store.get(saved.id)
    assert loaded is not None
    assert loaded.title == "旧页面"
    assert loaded.content == "旧正文"
    assert not hasattr(loaded, "status")


def test_save_page_with_chinese_title(store: FileSystemWikiStore):
    page = WikiPage(
        id="",
        page_type="entity",
        title="用户/实体：测试？",
        content="内容",
        file_path="",
    )
    saved = store.save_page(page)
    assert saved.file_path.endswith(".md")
    assert "/" not in Path(saved.file_path).stem


def test_duplicate_title_gets_unique_path(store: FileSystemWikiStore):
    for _ in range(3):
        store.save_page(
            WikiPage(id="", page_type="topic", title="同名页面", content="x", file_path=""),
        )
    pages = store.list_all()
    assert len(pages) == 3
    paths = {p.file_path for p in pages}
    assert len(paths) == 3


def test_update_page(store: FileSystemWikiStore):
    saved = store.save_page(
        WikiPage(id="", page_type="topic", title="原题", content="原内容", file_path=""),
    )
    saved.title = "新题"
    saved.content = "新内容"
    updated = store.update(saved)
    assert updated is not None
    assert updated.title == "新题"
    loaded = store.get(saved.id)
    assert loaded is not None
    assert loaded.content == "新内容"


def test_delete_page(store: FileSystemWikiStore):
    saved = store.save_page(
        WikiPage(id="", page_type="topic", title="待删除", content="x", file_path=""),
    )
    assert store.delete(saved.id) is True
    assert store.get(saved.id) is None
    assert store.delete(saved.id) is False


def test_delete_raw_and_related_pages(store: FileSystemWikiStore):
    store.save_raw(RawSource(id="s1", title="a.xlsx", source_type="upload", parsed_path=""))
    page = store.save_page(
        WikiPage(
            id="",
            page_type="topic",
            title="来自 s1",
            content="内容",
            file_path="",
            sources=["s1"],
        ),
    )
    other = store.save_page(
        WikiPage(id="", page_type="topic", title="其他", content="x", file_path="", sources=["s2"]),
    )

    assert store.delete_raw("s1") is True
    assert store.load_raw("s1") is None
    assert store.get(page.id) is None
    assert store.get(other.id) is not None
    assert store.delete_raw("s1") is False


def test_delete_raw_preserves_multi_source_page_and_removes_only_its_evidence(
    store: FileSystemWikiStore,
):
    from crew.wiki.schemas import WikiClaim, WikiEvidence

    store.save_raw(RawSource(id="s1", title="一", source_type="paste", parsed_path=""))
    store.save_raw(RawSource(id="s2", title="二", source_type="paste", parsed_path=""))
    page = store.save_page(
        WikiPage(
            id="",
            page_type="entity",
            title="聚合页",
            content="内容",
            file_path="",
            sources=["s1", "s2"],
            confidence="high",
            claims=[
                WikiClaim(
                    statement="共同主张",
                    evidence=[WikiEvidence(source_id="s1"), WikiEvidence(source_id="s2")],
                ),
                WikiClaim(
                    statement="仅由 s1 支撑",
                    evidence=[WikiEvidence(source_id="s1")],
                ),
            ],
        )
    )

    assert store.delete_raw("s1") is True
    retained = store.get(page.id)
    assert retained is not None
    assert retained.sources == ["s2"]
    assert [claim.statement for claim in retained.claims] == ["共同主张"]
    assert [e.source_id for e in retained.claims[0].evidence] == ["s2"]
    assert retained.confidence == "medium"


@pytest.mark.parametrize("source_id", ["../escape", "a/b", "a\\b", ".", "..", ""])
def test_raw_source_id_rejects_path_escape(store: FileSystemWikiStore, source_id: str):
    with pytest.raises(ValueError, match="source_id"):
        store.load_raw(source_id)
    with pytest.raises(ValueError, match="source_id"):
        store.save_parsed_markdown(source_id, "content")


def test_list_raws_does_not_treat_parsed_markdown_as_source(store: FileSystemWikiStore):
    store.save_raw(RawSource(id="s1", title="a.txt", source_type="upload", parsed_path=""))
    store.save_parsed_markdown("s1", "parsed content")
    assert [raw.id for raw in store.list_raws()] == ["s1"]


def test_list_all(store: FileSystemWikiStore):
    store.save_page(WikiPage(id="", page_type="topic", title="页面一", content="x", file_path=""))
    store.save_page(WikiPage(id="", page_type="topic", title="页面二", content="x", file_path=""))
    assert len(store.list_all()) == 2


# ---------------------------------------------------------------------------
# #2 Source Page 身份基于 source_id；#6 删除后重编正文；#8 版本链与搜索
# ---------------------------------------------------------------------------


def test_source_page_identity_by_source_id_not_title(store: FileSystemWikiStore):
    """两份同名但内容不同的来源各自拥有独立 Source Page，互不覆盖。"""
    from crew.wiki.schemas import RawSource

    store.save_raw(RawSource(id="s1", title="项目报告", source_type="paste", parsed_path=""))
    store.save_raw(RawSource(id="s2", title="项目报告", source_type="paste", parsed_path=""))
    # Source Page 身份基于 source_id（compiler 用 source_page_id 派生），
    # 标题相同但 id 与 sources 不同，二者共存不覆盖。
    store.save_page(
        WikiPage(
            id="src_s1",
            page_type="source",
            title="项目报告",
            content="# 项目报告\ns1 内容",
            file_path="",
            sources=["s1"],
        )
    )
    store.save_page(
        WikiPage(
            id="src_s2",
            page_type="source",
            title="项目报告",
            content="# 项目报告\ns2 内容",
            file_path="",
            sources=["s2"],
        )
    )

    p1 = store.get_source_page("s1")
    p2 = store.get_source_page("s2")
    assert p1 is not None and p2 is not None
    assert p1.id != p2.id
    assert p1.sources == ["s1"]
    assert p2.sources == ["s2"]
    # 标题相同但页面不同，删除其一不影响另一个
    store.delete_raw("s1")
    assert store.get_source_page("s1") is None
    assert store.get_source_page("s2") is not None


def test_delete_raw_recompiles_aggregation_when_claims_dropped_and_marks_stale(
    store: FileSystemWikiStore,
):
    """删除支撑来源导致 claim 丢失时，依据剩余 claims 重编正文并置 stale。"""
    from crew.wiki.schemas import WikiClaim, WikiEvidence

    store.save_raw(RawSource(id="s1", title="一", source_type="paste", parsed_path=""))
    store.save_raw(RawSource(id="s2", title="二", source_type="paste", parsed_path=""))
    page = store.save_page(
        WikiPage(
            id="",
            page_type="entity",
            title="聚合页",
            content="# 聚合页\n\n由 LLM 撰写的富文本叙述，引用了仅 s1 支撑的结论。",
            file_path="",
            sources=["s1", "s2"],
            claims=[
                WikiClaim(
                    statement="共同主张",
                    evidence=[WikiEvidence(source_id="s1"), WikiEvidence(source_id="s2")],
                ),
                WikiClaim(
                    statement="仅由 s1 支撑",
                    evidence=[WikiEvidence(source_id="s1")],
                ),
            ],
        )
    )

    assert store.delete_raw("s1") is True
    retained = store.get(page.id)
    assert retained is not None
    assert retained.sources == ["s2"]
    # 失去全部证据的 claim 被丢弃
    assert [c.statement for c in retained.claims] == ["共同主张"]
    # 正文被重编为剩余主张骨架，并标记待整理
    assert retained.stale is True
    assert "仅由 s1 支撑" not in retained.content
    assert "共同主张" in retained.content
    # stale 进入 lint 复核队列
    assert any(issue.kind == "stale" for issue in store.lint())


def test_delete_raw_keeps_narrative_when_no_claim_dropped(store: FileSystemWikiStore):
    """删除来源未丢弃任何 claim 时保留原 LLM 叙述，只更新 frontmatter。"""
    from crew.wiki.schemas import WikiClaim, WikiEvidence

    store.save_raw(RawSource(id="s1", title="一", source_type="paste", parsed_path=""))
    store.save_raw(RawSource(id="s2", title="二", source_type="paste", parsed_path=""))
    original_narrative = "# 聚合页\n\n由 LLM 撰写的富文本叙述。"
    page = store.save_page(
        WikiPage(
            id="",
            page_type="entity",
            title="聚合页",
            content=original_narrative,
            file_path="",
            sources=["s1", "s2"],
            claims=[
                WikiClaim(
                    statement="共同主张",
                    evidence=[WikiEvidence(source_id="s1"), WikiEvidence(source_id="s2")],
                ),
            ],
        )
    )

    assert store.delete_raw("s1") is True
    retained = store.get(page.id)
    assert retained is not None
    assert retained.content == original_narrative
    assert retained.stale is False
    assert [c.statement for c in retained.claims] == ["共同主张"]


def test_delete_raw_deletes_sole_source_page_and_preserves_legacy_merged_page(
    store: FileSystemWikiStore,
):
    """唯一关联的 Source Page 整页删除；legacy 多源合并页只移除关联并置 stale。"""
    from crew.wiki.schemas import RawSource

    store.save_raw(RawSource(id="s1", title="独占来源", source_type="paste", parsed_path=""))
    store.save_raw(RawSource(id="s2", title="合并A", source_type="paste", parsed_path=""))
    store.save_raw(RawSource(id="s3", title="合并B", source_type="paste", parsed_path=""))

    # 独占 source 页
    sole = store.save_page(
        WikiPage(
            id="src_sole",
            page_type="source",
            title="独占来源",
            content="# 独占来源",
            file_path="",
            sources=["s1"],
        )
    )
    # legacy 按标题合并的多源 Source 页
    merged = store.save_page(
        WikiPage(
            id="src_merged",
            page_type="source",
            title="合并标题",
            content="# 合并标题",
            file_path="",
            sources=["s2", "s3"],
        )
    )

    assert store.delete_raw("s1") is True
    assert store.get(sole.id) is None  # 独占页被删除

    assert store.delete_raw("s2") is True
    retained = store.get(merged.id)
    assert retained is not None  # 合并页保留
    assert retained.sources == ["s3"]
    assert retained.stale is True


def test_search_excludes_superseded_source_pages(store: FileSystemWikiStore):
    """被取代的旧版本 Source 页不参与默认检索。"""
    from crew.wiki.schemas import RawSource

    old = RawSource(
        id="url_old",
        title="版本变迁",
        source_type="url",
        parsed_path="",
        source_url="https://example.com/a",
        superseded_by="url_new",
    )
    new = RawSource(
        id="url_new",
        title="版本变迁",
        source_type="url",
        parsed_path="",
        source_url="https://example.com/a",
    )
    store.save_raw(old)
    store.save_raw(new)
    store.save_page(
        WikiPage(
            id="src_old",
            page_type="source",
            title="版本变迁-旧",
            content="版本变迁 旧版本内容",
            file_path="",
            sources=["url_old"],
        )
    )
    store.save_page(
        WikiPage(
            id="src_new",
            page_type="source",
            title="版本变迁-新",
            content="版本变迁 新版本内容",
            file_path="",
            sources=["url_new"],
        )
    )

    assert store.superseded_source_ids() == {"url_old"}
    hits = store.search("版本变迁", top_k=10)
    hit_ids = [p.id for p in hits]
    assert "src_new" in hit_ids
    assert "src_old" not in hit_ids


def test_raw_source_is_current_property(store: FileSystemWikiStore):
    from crew.wiki.schemas import RawSource

    current = RawSource(id="a", title="a", source_type="url", parsed_path="")
    superseded = RawSource(
        id="b", title="b", source_type="url", parsed_path="", superseded_by="a"
    )
    assert current.is_current is True
    assert superseded.is_current is False



def test_search_returns_all_pages(store: FileSystemWikiStore):
    store.save_page(WikiPage(id="", page_type="topic", title="页面一", content="关键字", file_path=""))
    store.save_page(WikiPage(id="", page_type="topic", title="页面二", content="关键字", file_path=""))
    results = store.search("关键字")
    assert len(results) == 2
    assert {r.title for r in results} == {"页面一", "页面二"}


def test_delete_page_removes_fts_index(store: FileSystemWikiStore):
    """删除页面后，FTS5 索引中不应再搜到。"""
    saved = store.save_page(
        WikiPage(id="", page_type="topic", title="待删除", content="这个页面即将被删除。", file_path=""),
    )
    assert len(store.search("删除")) == 1

    store.delete(saved.id)
    assert len(store.search("删除")) == 0


def test_search_fts_and_keyword_fallback(store: FileSystemWikiStore):
    """FTS5 失败时仍能回退到关键词搜索（此测试直接覆盖正常路径）。"""
    store.save_page(
        WikiPage(id="", page_type="topic", title="关键词测试", content="包含一个特殊的关键字。", file_path=""),
    )
    results = store.search("关键字")
    assert len(results) == 1


def test_search_index_matches_navigation_summary_not_page_body(store: FileSystemWikiStore):
    page = store.save_page(
        WikiPage(
            id="",
            page_type="topic",
            title="知识地图",
            content="# 知识地图\n\n这是页面正文。",
            file_path="",
        )
    )
    store.init_kb()
    (store._dir() / "index.md").write_text(
        "# Crew Wiki\n\n"
        "## 主题\n\n"
        "- [[知识地图]] — 负责跨来源知识编排与关系导航（来源 2）\n",
        encoding="utf-8",
    )

    assert store.search("跨来源") == []
    assert [item.id for item in store.search_index("跨来源")] == [page.id]


def test_search_index_respects_kb_isolation(store: FileSystemWikiStore):
    for kb_id in ("default", "project_a"):
        store.init_kb(kb_id=kb_id)
        store.save_page(
            WikiPage(
                id="",
                page_type="topic",
                title=f"{kb_id} 导航",
                content="# 导航\n\n无关正文。",
                file_path="",
            ),
            kb_id=kb_id,
        )
        (store._dir(kb_id=kb_id) / "index.md").write_text(
            "# Crew Wiki\n\n"
            f"## 主题\n\n- [[{kb_id} 导航]] — {kb_id} 专属检索线索\n",
            encoding="utf-8",
        )

    assert [page.title for page in store.search_index("default", kb_id="default")] == [
        "default 导航"
    ]
    assert store.search_index("default", kb_id="project_a") == []


def test_search_fts_respects_kb_id(store: FileSystemWikiStore):
    """FTS5 索引按 kb_id 隔离。"""
    store.init_kb(kb_id="default")
    store.init_kb(kb_id="project_a")
    store.save_page(
        WikiPage(id="", page_type="topic", title="目标", content="default 内容", file_path=""),
        kb_id="default",
    )
    store.save_page(
        WikiPage(id="", page_type="topic", title="目标", content="project_a 内容", file_path=""),
        kb_id="project_a",
    )

    assert len(store.search("default", kb_id="default")) == 1
    assert len(store.search("project_a", kb_id="project_a")) == 1
    assert len(store.search("default", kb_id="project_a")) == 0


def test_get_by_title(store: FileSystemWikiStore):
    store.save_page(WikiPage(id="", page_type="topic", title="按标题找", content="x", file_path=""))
    found = store.get_by_title("按标题找")
    assert found is not None
    assert found.title == "按标题找"


def test_get_graph_extracts_links(store: FileSystemWikiStore):
    a = store.save_page(WikiPage(id="", page_type="topic", title="页面A", content="链接到 [[页面B]]", file_path=""))
    b = store.save_page(WikiPage(id="", page_type="topic", title="页面B", content="内容", file_path="", related=["页面A"]))
    graph = store.get_graph()
    ids = {n["id"] for n in graph.nodes}
    assert a.id in ids
    assert b.id in ids
    assert any(e["source"] == a.id and e["target"] == b.id for e in graph.edges)


def test_get_graph_deduplicates_source_nodes(store: FileSystemWikiStore):
    """多个页面引用同一个 source 时，source 节点应只出现一次，且标题使用 RawSource.title。"""
    from crew.wiki.schemas import RawSource

    src_id = "upload_abc123"
    store.save_raw(RawSource(id=src_id, title="面试记录.docx", source_type="upload", parsed_path=""))
    a = store.save_page(WikiPage(id="", page_type="topic", title="页面A", content="内容", file_path="", sources=[src_id]))
    b = store.save_page(WikiPage(id="", page_type="topic", title="页面B", content="内容", file_path="", sources=[src_id]))
    c = store.save_page(WikiPage(id="", page_type="topic", title="页面C", content="内容", file_path="", sources=[src_id]))
    graph = store.get_graph()
    source_nodes = [n for n in graph.nodes if n["type"] == "source"]
    assert len(source_nodes) == 1
    assert source_nodes[0]["id"] == f"source:{src_id}"
    assert source_nodes[0]["title"] == "面试记录.docx"
    source_edges = [e for e in graph.edges if e["target"] == f"source:{src_id}"]
    assert len(source_edges) == 3
    assert {e["source"] for e in source_edges} == {a.id, b.id, c.id}


def test_lint_detects_broken_link_and_orphan(store: FileSystemWikiStore):
    store.save_page(WikiPage(id="", page_type="topic", title="断链页", content="# 断链页\n\n[[不存在的页面]]", file_path=""))
    store.save_page(WikiPage(id="", page_type="topic", title="孤立页", content="# 孤立页\n\n无链接", file_path=""))
    issues = store.lint()
    kinds = {i.kind for i in issues}
    assert "broken_link" in kinds
    assert "orphan" in kinds


def test_lint_detects_format_violation(store: FileSystemWikiStore):
    store.save_page(WikiPage(id="", page_type="topic", title="格式错误", content="没有标题行", file_path=""))
    issues = store.lint()
    assert any(i.kind == "format_violation" for i in issues)


def test_lint_detects_outdated_marker(store: FileSystemWikiStore):
    store.save_page(
        WikiPage(
            id="",
            page_type="topic",
            title="时效页",
            content="# 时效页\n\n截至 2024 年底，这是最新版。",
            file_path="",
        )
    )
    issues = store.lint()
    assert any(i.kind == "outdated_marker" for i in issues)


def test_lint_no_format_violation_for_valid_title(store: FileSystemWikiStore):
    store.save_page(WikiPage(id="", page_type="topic", title="合法页", content="# 合法页\n\n正文", file_path=""))
    issues = store.lint()
    assert not any(i.kind == "format_violation" for i in issues)


def test_lint_no_orphan_for_source_pages(store: FileSystemWikiStore):
    store.save_page(WikiPage(id="", page_type="source", title="来源页", content="# 来源页\n\n无链接", file_path=""))
    issues = store.lint()
    assert not any(i.kind == "orphan" for i in issues)


def test_get_source_titles(store: FileSystemWikiStore):
    from crew.wiki.schemas import RawSource

    store.save_raw(RawSource(id="s1", title="文档一", source_type="upload", parsed_path=""))
    store.save_raw(RawSource(id="s2", title="文档二", source_type="paste", parsed_path=""))
    titles = store.get_source_titles(["s1", "s2", "missing"])
    assert titles["s1"] == "文档一"
    assert titles["s2"] == "文档二"
    assert titles["missing"] == "missing"


def test_raw_source_isolation(store: FileSystemWikiStore):
    from crew.wiki.schemas import RawSource

    store.init_kb(kb_id="default")
    store.init_kb(kb_id="project_a")

    store.save_raw(RawSource(id="s1", title="Source", source_type="paste", parsed_path=""), kb_id="default")
    store.save_raw(RawSource(id="s1", title="Source", source_type="paste", parsed_path=""), kb_id="project_a")

    assert len(store.list_raws(kb_id="default")) == 1
    assert len(store.list_raws(kb_id="project_a")) == 1
    assert store.load_raw("s1", kb_id="default") is not None
    assert store.load_raw("s1", kb_id="project_a") is not None


def test_get_update_delete_respect_kb_id(store: FileSystemWikiStore):
    store.init_kb(kb_id="default")
    store.init_kb(kb_id="project_a")

    page_default = store.save_page(
        WikiPage(id="", page_type="topic", title="Target", content="default", file_path=""),
        kb_id="default",
    )
    page_project = store.save_page(
        WikiPage(id="", page_type="topic", title="Target", content="project", file_path=""),
        kb_id="project_a",
    )

    assert store.get_by_title("Target", kb_id="default").content == "default"
    assert store.get_by_title("Target", kb_id="project_a").content == "project"

    page_default.content = "default updated"
    store.update(page_default, kb_id="default")
    assert store.get(page_default.id, kb_id="default").content == "default updated"
    assert store.get(page_project.id, kb_id="project_a").content == "project"

    assert store.delete(page_default.id, kb_id="default") is True
    assert store.get(page_default.id, kb_id="default") is None
    assert store.get(page_project.id, kb_id="project_a") is not None


# ---- get_neighbors ----

def test_get_neighbors_empty_for_unknown_page(store: FileSystemWikiStore):
    assert store.get_neighbors("nonexistent") == []


def test_get_neighbors_returns_related_pages(store: FileSystemWikiStore):
    """related 字段中的页面应出现在邻居列表中。"""
    a = store.save_page(WikiPage(id="", page_type="topic", title="页面A", content="内容", file_path=""))
    b = store.save_page(WikiPage(id="", page_type="topic", title="页面B", content="内容", file_path=""))
    # 页面A 显式关联 页面B
    a.relations = [WikiRelation(target_page_id=b.id)]
    store.update(a)

    neighbors = store.get_neighbors(a.id)
    assert len(neighbors) == 1
    assert neighbors[0].title == "页面B"


def test_get_neighbors_from_wikilinks(store: FileSystemWikiStore):
    """正文 [[...]] 链接中的页面应出现在邻居列表中。"""
    a = store.save_page(WikiPage(id="", page_type="topic", title="页面A", content="参考 [[页面B]] 了解更多", file_path=""))
    store.save_page(WikiPage(id="", page_type="topic", title="页面B", content="被引用的页面", file_path=""))

    neighbors = store.get_neighbors(a.id)
    assert len(neighbors) == 1
    assert neighbors[0].title == "页面B"


def test_get_neighbors_combines_related_and_wikilinks(store: FileSystemWikiStore):
    """related 和 [[wikilinks]] 应合并去重。"""
    b = store.save_page(WikiPage(id="", page_type="topic", title="页面B", content="", file_path=""))
    store.save_page(WikiPage(id="", page_type="topic", title="页面C", content="", file_path=""))
    a = store.save_page(WikiPage(id="", page_type="topic", title="中心页", content="参见 [[页面B]] 和 [[页面C]]", file_path="", relations=[WikiRelation(target_page_id=b.id)]))

    neighbors = store.get_neighbors(a.id)
    assert len(neighbors) == 2
    titles = {n.title for n in neighbors}
    assert titles == {"页面B", "页面C"}


def test_get_neighbors_related_ranked_before_mentions(store: FileSystemWikiStore):
    """related 关系的邻居应排在 mentions（wikilinks）前面。"""
    explicit = store.save_page(WikiPage(id="", page_type="topic", title="显式关联", content="", file_path=""))
    store.save_page(WikiPage(id="", page_type="topic", title="仅提及", content="", file_path=""))
    a = store.save_page(WikiPage(id="", page_type="topic", title="中心页", content="[[仅提及]]", file_path="", relations=[WikiRelation(target_page_id=explicit.id)]))

    neighbors = store.get_neighbors(a.id)
    assert len(neighbors) == 2
    assert neighbors[0].title == "显式关联"
    assert neighbors[1].title == "仅提及"


def test_get_neighbors_excludes_self(store: FileSystemWikiStore):
    """不应把自己当作邻居。"""
    a = store.save_page(WikiPage(id="", page_type="topic", title="页面A", content="[[页面A]]", file_path="", related=["页面A"]))

    neighbors = store.get_neighbors(a.id)
    assert len(neighbors) == 0


def test_get_neighbors_respects_kb_id(store: FileSystemWikiStore):
    """邻居查询应按知识库隔离。"""
    store.init_kb(kb_id="default")
    store.init_kb(kb_id="project_a")

    a_default = store.save_page(WikiPage(id="", page_type="topic", title="页面A", content="[[页面B]]", file_path=""), kb_id="default")
    store.save_page(WikiPage(id="", page_type="topic", title="页面B", content="", file_path=""), kb_id="default")
    store.save_page(WikiPage(id="", page_type="topic", title="页面B", content="", file_path=""), kb_id="project_a")

    neighbors = store.get_neighbors(a_default.id, kb_id="default")
    assert len(neighbors) == 1


# ---- orientation & log ----

def test_orient_returns_stats_and_candidate_index(store: FileSystemWikiStore):
    store.save_page(WikiPage(id="", page_type="topic", title="页面A", content="# 页面A\n\n正文", file_path="", tags=["项目A"]))
    store.save_page(WikiPage(id="", page_type="entity", title="概念B", content="# 概念B\n\n正文", file_path="", aliases=["B"]))
    store.save_raw(RawSource(id="s1", title="来源一", source_type="upload", parsed_path=""))

    orient = store.orient()
    assert orient.kb_id == "default"
    assert orient.index["page_count"] == 2
    assert orient.index["raw_source_count"] == 1
    assert orient.stats["by_type"]["topic"] == 1
    assert orient.stats["by_type"]["entity"] == 1
    assert orient.candidate_index["title_to_id"]["页面A"]
    assert orient.candidate_index["alias_to_id"]["B"]
    assert orient.schema["rules"]  # SCHEMA.md 默认有规则


def test_orient_parses_recent_log(store: FileSystemWikiStore):
    from crew.wiki.store._filesystem import append_wiki_log

    base = store._dir()
    append_wiki_log(base, ["创建页面 A", "创建页面 B"])
    append_wiki_log(base, ["更新页面 C"])

    orient = store.orient()
    assert len(orient.recent_log) == 2
    assert orient.recent_log[0]["messages"] == ["更新页面 C"]
    assert orient.recent_log[1]["messages"] == ["创建页面 A", "创建页面 B"]


def test_orient_empty_kb_returns_defaults(store: FileSystemWikiStore):
    orient = store.orient()
    assert orient.index["page_count"] == 0
    assert orient.index["raw_source_count"] == 0
    assert orient.recent_log == []
    assert orient.candidate_index["title_to_id"] == {}


def test_append_log_creates_file_and_inserts_at_top(store: FileSystemWikiStore):
    from crew.wiki.store._filesystem import append_wiki_log

    base = store._dir()
    append_wiki_log(base, ["第一条日志"])
    append_wiki_log(base, ["第二条日志"])

    text = (base / "log.md").read_text(encoding="utf-8")
    lines = text.splitlines()
    # 标题后第一条日志应该是最新的
    header_idx = lines.index("# Wiki 更新日志")
    assert lines[header_idx + 1].strip() == ""
    assert lines[header_idx + 2].startswith("## ")
    assert "第二条日志" in lines[header_idx + 3]


# ---- source hash / duplicate / drift ----

def test_save_raw_computes_original_sha256(store: FileSystemWikiStore, tmp_path):
    from crew.wiki.schemas import RawSource

    original = tmp_path / "note.txt"
    original.write_text("hello world", encoding="utf-8")
    raw = RawSource(
        id="s_hash",
        title="note.txt",
        source_type="upload",
        parsed_path="",
        original_path=str(original),
    )
    saved = store.save_raw(raw)
    assert saved.original_sha256 is not None
    assert len(saved.original_sha256) == 64


def test_save_parsed_markdown_updates_content_sha256(store: FileSystemWikiStore):
    from crew.wiki.schemas import RawSource

    raw = RawSource(id="s_parsed", title="a.md", source_type="upload", parsed_path="")
    store.save_raw(raw)
    store.save_parsed_markdown("s_parsed", "parsed content")

    loaded = store.load_raw("s_parsed")
    assert loaded is not None
    assert loaded.content_sha256 is not None
    assert loaded.parse_status == "parsed"


def test_check_source_duplicate_finds_same_content(store: FileSystemWikiStore):
    from crew.wiki.schemas import RawSource

    raw1 = RawSource(id="s1", title="a.txt", source_type="upload", parsed_path="", content_sha256="abc123")
    raw2 = RawSource(id="s2", title="b.txt", source_type="upload", parsed_path="", content_sha256="abc123")
    store.save_raw(raw1)
    store.save_raw(raw2)

    dup = store.check_source_duplicate(raw2)
    assert dup is not None
    assert dup.id == "s1"


def test_check_source_drift_finds_url_with_different_hash(store: FileSystemWikiStore):
    from crew.wiki.schemas import RawSource

    raw1 = RawSource(
        id="url_v1",
        title="spec.md",
        source_type="url",
        parsed_path="",
        source_url="https://example.com/spec",
        content_sha256="abc123",
    )
    raw2 = RawSource(
        id="url_v2",
        title="spec.md",
        source_type="url",
        parsed_path="",
        source_url="https://example.com/spec",
        content_sha256="def456",
    )
    store.save_raw(raw1)
    store.save_raw(raw2)

    drifted = store.check_source_drift(raw2)
    assert len(drifted) == 1
    assert drifted[0].id == "url_v1"


def test_page_quality_frontmatter_roundtrip(store: FileSystemWikiStore):
    from crew.wiki.schemas import WikiClaim, WikiEvidence

    store.save_raw(
        RawSource(
            id="s1",
            title="来源",
            source_type="paste",
            parsed_path="",
        )
    )
    saved = store.save_page(
        WikiPage(
            id="",
            page_type="entity",
            title="可靠知识",
            content="# 可靠知识",
            file_path="",
            sources=["s1"],
            claims=[
                WikiClaim(
                    statement="知识需要可追溯",
                    evidence=[WikiEvidence(source_id="s1", locator="第1节")],
                    confidence="high",
                )
            ],
            confidence="high",
        )
    )

    loaded = store.get(saved.id)
    assert loaded is not None
    assert loaded.confidence == "high"
    assert loaded.claims[0].evidence[0].locator == "第1节"


def test_typed_relations_roundtrip_graph_and_neighbors(store: FileSystemWikiStore):
    target = store.save_page(
        WikiPage(
            id="",
            page_type="entity",
            title="检索增强生成",
            content="# 检索增强生成",
            file_path="",
            aliases=["RAG"],
        )
    )
    source = store.save_page(
        WikiPage(
            id="",
            page_type="topic",
            title="知识问答",
            content="# 知识问答",
            file_path="",
            relations=[WikiRelation(target_page_id=target.id, relation="uses")],
        )
    )

    loaded = store.get(source.id)
    assert loaded is not None
    assert loaded.relations[0].relation == "uses"
    assert target.id in {page.id for page in store.get_neighbors(source.id)}
    assert {
        (edge["source"], edge["target"], edge["relation"])
        for edge in store.get_graph().edges
    } >= {(source.id, target.id, "uses")}


def test_lint_reports_quality_source_alias_and_index_issues(store: FileSystemWikiStore):
    store.save_raw(
        RawSource(
            id="known",
            title="已知来源",
            source_type="paste",
            parsed_path="",
        )
    )
    store.save_page(
        WikiPage(
            id="",
            page_type="entity",
            title="知识管理",
            content="# 知识管理\n\n正文",
            file_path="",
            sources=["missing"],
            aliases=["KM"],
            confidence="low",
            contested=True,
            contradictions=["另一来源给出不同结论"],
        )
    )
    store.save_page(
        WikiPage(
            id="",
            page_type="topic",
            title="KM",
            content="# KM\n\n正文",
            file_path="",
            aliases=["知识管理"],
        )
    )

    kinds = {issue.kind for issue in store.lint()}

    assert "missing_source" in kinds
    assert "low_confidence" in kinds
    assert "contested" in kinds
    assert "alias_conflict" in kinds
    assert "index_drift" in kinds


def test_legacy_frontmatter_without_id_gets_stable_derived_id(store: FileSystemWikiStore):
    """旧版 frontmatter（type 键、无 id 字段）读路径派生稳定 id，brief 列表与详情一致。"""
    legacy = store._dir() / "wiki" / "entities" / "R2S2R.md"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        "---\n"
        "type: entity\n"
        "title: R2S2R\n"
        "aliases: [Real-to-Sim-to-Real]\n"
        "confidence: high\n"
        "---\n\n# R2S2R\n\n正文内容\n",
        encoding="utf-8",
    )

    brief = [p for p in store.list_all(brief=True) if p.title == "R2S2R"]
    assert len(brief) == 1
    derived_id = brief[0].id
    assert derived_id  # 不再是空 id（空 id 会导致前端无法选中、详情 404）
    assert brief[0].page_type == "entity"  # 旧版 type 键映射为 page_type

    # get 内部重新全量读取文件，能找到即证明 brief/full 两条反序列化路径派生的 id 一致
    full = store.get(derived_id)
    assert full is not None
    assert "正文内容" in full.content


def test_legacy_frontmatter_without_type_defaults_topic_and_stem_title(store: FileSystemWikiStore):
    """旧版 frontmatter 连 type 也缺省时回退 topic，标题缺省时用文件名派生 id。"""
    legacy = store._dir() / "wiki" / "topics" / "旧页面.md"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("---\naliases: [old]\n---\n\n正文\n", encoding="utf-8")

    brief = [p for p in store.list_all(brief=True) if p.file_path.endswith("旧页面.md")]
    assert len(brief) == 1
    assert brief[0].id
    assert brief[0].page_type == "topic"
    assert store.get(brief[0].id) is not None
