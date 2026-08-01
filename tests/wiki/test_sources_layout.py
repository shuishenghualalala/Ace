from __future__ import annotations

from pathlib import Path

import pytest

from crew.core.mocks import FakeProvider
from crew.core.types import ChatResponse
from crew.wiki.compiler import WikiCompiler
from crew.wiki.schemas import RawSource, WikiPage
from crew.wiki.sources import classify_file, classify_url, extract_youtube_video_id
from crew.wiki.store import FileSystemWikiStore
from crew.wiki.store._serde import serialize_page


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("paper.pdf", "pdf"),
        ("draft.docx", "word"),
        ("data.xlsx", "excel"),
        ("table.csv", "excel"),
        ("slides.pptx", "ppt"),
        ("page.html", "article"),
        ("note.md", "note"),
        ("image.png", "image"),
        ("movie.mp4", "video"),
        ("archive.zip", "asset"),
    ],
)
def test_classify_file_uses_material_type(filename: str, expected: str):
    assert classify_file(filename) == expected


@pytest.mark.parametrize(
    ("url", "kind", "platform"),
    [
        ("https://example.com/a", "article", "web"),
        ("https://mp.weixin.qq.com/s/a", "article", "wechat"),
        ("https://www.zhihu.com/question/1", "article", "zhihu"),
        ("https://x.com/user/status/1", "article", "x"),
        ("https://www.xiaohongshu.com/explore/1", "article", "xiaohongshu"),
        ("https://youtu.be/dQw4w9WgXcQ", "video", "youtube"),
    ],
)
def test_url_platform_does_not_create_platform_directory(
    url: str,
    kind: str,
    platform: str,
):
    assert classify_url(url) == (kind, platform)


def test_youtube_video_id_formats():
    assert extract_youtube_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert extract_youtube_video_id(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    ) == "dQw4w9WgXcQ"


def test_raw_source_metadata_and_content_use_v2_layout(tmp_path: Path):
    store = FileSystemWikiStore(base_dir=tmp_path)
    store.init_kb()
    raw = RawSource(
        id="src_pdf",
        title="paper.pdf",
        source_type="upload",
        parsed_path="",
        source_kind="pdf",
        source_platform="local",
        adapter_name="builtin-file",
        original_ref="paper.pdf",
    )
    store.save_raw(raw)
    parsed = Path(store.save_parsed_markdown(raw.id, "# Paper"))
    base = store._dir()

    assert parsed == base / "raw" / "pdfs" / "src_pdf.md"
    assert (base / ".crew" / "sources" / "src_pdf.json").exists()
    loaded = store.load_raw(raw.id)
    assert loaded is not None
    assert loaded.source_kind == "pdf"
    assert loaded.source_platform == "local"


def test_layout_migration_moves_legacy_pages_without_changing_id(tmp_path: Path):
    store = FileSystemWikiStore(base_dir=tmp_path)
    store.init_kb()
    base = store._dir()
    legacy_dir = base / "entities"
    legacy_dir.mkdir()
    page = WikiPage(
        id="ent_legacy",
        page_type="entity",
        title="Legacy",
        content="# Legacy",
        file_path="entities/Legacy.md",
    )
    (legacy_dir / "Legacy.md").write_text(serialize_page(page), encoding="utf-8")

    preview = store.layout_migration_preview()
    result = store.migrate_layout()

    assert preview["required"] is True
    assert result["pages"] == 1
    migrated = store.get("ent_legacy")
    assert migrated is not None
    assert migrated.id == "ent_legacy"
    assert migrated.file_path == "wiki/entities/Legacy.md"


def test_layout_migration_collision_leaves_legacy_page_in_place(tmp_path: Path):
    store = FileSystemWikiStore(base_dir=tmp_path)
    store.init_kb()
    base = store._dir()
    legacy_dir = base / "entities"
    legacy_dir.mkdir()
    legacy = legacy_dir / "Same.md"
    legacy.write_text("# legacy", encoding="utf-8")
    (base / "wiki" / "entities" / "Same.md").write_text("# current", encoding="utf-8")

    with pytest.raises(FileExistsError):
        store.migrate_layout()

    assert legacy.exists()
    assert legacy.read_text(encoding="utf-8") == "# legacy"


def test_layout_migration_ignores_unsupported_concept_directory(tmp_path: Path):
    store = FileSystemWikiStore(base_dir=tmp_path)
    store.init_kb()
    base = store._dir()
    concept_dir = base / "wiki" / "concepts"
    concept_dir.mkdir()
    (concept_dir / "知识编译.md").write_text(
        "---\nid: con_legacy\npage_type: concept\ntitle: 知识编译\n---\n# 知识编译\n",
        encoding="utf-8",
    )

    preview = store.layout_migration_preview()
    result = store.migrate_layout()

    assert preview["required"] is False
    assert result["migrated"] is False
    assert store.get("con_legacy") is None
    assert concept_dir.exists()


def test_layout_migration_classifies_flat_source_summary(tmp_path: Path):
    store = FileSystemWikiStore(base_dir=tmp_path)
    store.init_kb()
    base = store._dir()
    raw = RawSource(
        id="src_pdf",
        title="论文",
        source_type="upload",
        parsed_path="",
        source_kind="pdf",
    )
    store.save_raw(raw)
    page = WikiPage(
        id="src_paper",
        page_type="source",
        title="论文",
        content="# 论文",
        file_path="wiki/sources/论文.md",
        sources=[raw.id],
    )
    flat_path = base / page.file_path
    flat_path.write_text(serialize_page(page), encoding="utf-8")

    preview = store.layout_migration_preview()
    result = store.migrate_layout()

    assert preview["source_pages_to_classify"] == 1
    assert result["source_pages_classified"] == 1
    moved = store.get(page.id)
    assert moved is not None
    assert moved.file_path == "wiki/sources/pdfs/论文.md"
    assert not flat_path.exists()


@pytest.mark.asyncio
async def test_digest_requires_multiple_sources_and_writes_synthesis(tmp_path: Path):
    store = FileSystemWikiStore(base_dir=tmp_path)
    store.init_kb()
    for source_id in ("s1", "s2"):
        store.save_raw(
            RawSource(
                id=source_id,
                title=source_id,
                source_type="paste",
                parsed_path="",
            )
        )
    for index, source_id in enumerate(("s1", "s2"), 1):
        store.save_page(
            WikiPage(
                id="",
                page_type="topic",
                title=f"Crew 观点 {index}",
                content=f"# Crew 观点 {index}\n\nCrew 架构观点",
                file_path="",
                sources=[source_id],
            )
        )
    compiler = WikiCompiler(
        store=store,
        provider=FakeProvider(
            script=[ChatResponse(text="# Crew-深度综合\n\n综合结论 [[Crew 观点 1]] [[Crew 观点 2]]")]
        ),
    )

    page = await compiler.digest("Crew", mode="synthesis")

    assert page.page_type == "synthesis"
    assert page.file_path.startswith("wiki/synthesis/")
    assert page.sources == ["s1", "s2"]
    assert "[[Crew-深度综合]]" in (store._dir() / "index.md").read_text(encoding="utf-8")
    assert "| 综合报告 | 1 |" in (store._dir() / "Home.md").read_text(encoding="utf-8")
