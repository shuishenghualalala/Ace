"""Wiki 数据模型 schema 测试。"""

from __future__ import annotations

from crew.wiki.schemas import RawSource, WikiClaim, WikiEvidence, WikiPage


def test_raw_source_from_dict_defaults_parse_status_to_parsed_when_parsed_path_exists():
    """旧数据没有 parse_status 字段时，有 parsed_path 应视为已解析。"""
    raw = RawSource.from_dict(
        {
            "id": "s1",
            "title": "a.txt",
            "source_type": "upload",
            "parsed_path": "/tmp/s1.parsed.md",
            "original_path": "/tmp/s1.txt",
        }
    )
    assert raw.parse_status == "parsed"
    assert raw.parse_error is None


def test_raw_source_from_dict_defaults_parse_status_to_pending_when_no_parsed_path():
    """旧数据没有 parse_status 字段且 parsed_path 为空时，应视为待解析。"""
    raw = RawSource.from_dict(
        {
            "id": "s2",
            "title": "a.xlsx",
            "source_type": "upload",
            "parsed_path": "",
            "original_path": "/tmp/s2.xlsx",
        }
    )
    assert raw.parse_status == "pending"


def test_raw_source_to_dict_includes_parse_fields():
    """to_dict 应包含 parse_status 和 parse_error。"""
    raw = RawSource(
        id="s3",
        title="a.xlsx",
        source_type="upload",
        parsed_path="",
        original_path="/tmp/s3.xlsx",
        parse_status="failed",
        parse_error="解析失败: expected Fill",
    )
    data = raw.to_dict()
    assert data["parse_status"] == "failed"
    assert data["parse_error"] == "解析失败: expected Fill"


def test_raw_source_from_dict_ignores_invalid_parse_status():
    """非法 parse_status 应回退为 pending。"""
    raw = RawSource.from_dict(
        {
            "id": "s4",
            "title": "a.txt",
            "source_type": "upload",
            "parsed_path": "",
            "parse_status": "unknown",
        }
    )
    assert raw.parse_status == "pending"


def test_raw_source_hash_and_drift_fields_roundtrip():
    """hash、drift、duplicate、source_url 字段应能正常 to_dict / from_dict。"""
    raw = RawSource(
        id="s5",
        title="a.txt",
        source_type="url",
        parsed_path="/tmp/s5.parsed.md",
        original_path="/tmp/s5.txt",
        original_sha256="abc123",
        content_sha256="def456",
        drift_from="s4",
        is_duplicate=True,
        source_url="https://example.com/doc",
    )
    data = raw.to_dict()
    assert data["original_sha256"] == "abc123"
    assert data["content_sha256"] == "def456"
    assert data["drift_from"] == "s4"
    assert data["is_duplicate"] is True
    assert data["source_url"] == "https://example.com/doc"

    restored = RawSource.from_dict(data)
    assert restored.original_sha256 == "abc123"
    assert restored.content_sha256 == "def456"
    assert restored.drift_from == "s4"
    assert restored.is_duplicate is True
    assert restored.source_url == "https://example.com/doc"


# ---- PlannedPage / PlanResult brief 序列化 ----

def test_planned_page_to_dict_full_by_default():
    from crew.wiki.schemas import PlannedPage

    content = "A" * 2000
    page = PlannedPage(title="t", page_type="entity", action="create", content=content)
    data = page.to_dict()
    assert data["content"] == content


def test_planned_page_to_dict_brief_truncates_long_content():
    from crew.wiki.schemas import PlannedPage

    content = "A" * 2000
    page = PlannedPage(title="t", page_type="entity", action="create", content=content)
    data = page.to_dict(brief=True)
    assert len(data["content"]) < len(content)
    assert "...(内容已省略" in data["content"]
    assert data["content"].startswith("A" * 500)


def test_planned_page_to_dict_brief_keeps_short_content():
    from crew.wiki.schemas import PlannedPage

    content = "short content"
    page = PlannedPage(title="t", page_type="entity", action="create", content=content)
    data = page.to_dict(brief=True)
    assert data["content"] == content


def test_plan_result_to_dict_brief_propagates_to_pages():
    from crew.wiki.schemas import PlannedPage, PlanResult

    long_content = "B" * 2000
    plan = PlanResult(
        source_id="s1",
        source_title="doc",
        planned_pages=[
            PlannedPage(title="source", page_type="source", action="create", content=long_content),
            PlannedPage(title="concept", page_type="entity", action="create", content="short"),
        ],
        total_new=2,
    )
    brief = plan.to_dict(brief=True)
    assert "...(内容已省略" in brief["planned_pages"][0]["content"]
    assert brief["planned_pages"][1]["content"] == "short"

    full = plan.to_dict()
    assert full["planned_pages"][0]["content"] == long_content
    assert full["planned_pages"][1]["content"] == "short"


def test_wiki_page_quality_model_roundtrip():
    page = WikiPage(
        id="p1",
        page_type="entity",
        title="证据模型",
        content="# 证据模型",
        file_path="entities/evidence.md",
        claims=[
            WikiClaim(
                statement="Crew Wiki 使用规范页面沉淀知识",
                evidence=[
                    WikiEvidence(
                        source_id="source-1",
                        locator="第 2 节",
                        excerpt="规范页面是长期知识的归宿。",
                    )
                ],
                confidence="high",
                contested=True,
                contradictions=["另一来源认为只需要来源摘要"],
            )
        ],
        confidence="high",
        contested=True,
        contradictions=["知识组织方式存在分歧"],
    )

    restored = WikiPage.from_dict(page.to_dict())

    assert restored.confidence == "high"
    assert restored.contested is True
    assert restored.claims[0].evidence[0].source_id == "source-1"
    assert restored.claims[0].evidence[0].locator == "第 2 节"
    assert restored.contradictions == ["知识组织方式存在分歧"]
