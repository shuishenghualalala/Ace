import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from crew.agent.subagent.definition import build_preset_spec
from crew.app import build_app
from crew.gateway.server import create_app
from crew.state.config import Config

OWNER = "A:uid-a"


def _client(tmp_path):
    os.environ["CREW_HOME"] = str(tmp_path / ".crew")
    crew_home = tmp_path / ".crew"
    app = build_app(
        config=Config(
            api_key="",
            db_path=str(crew_home / "crew_data" / "crew.db"),
            memory_db_path=str(crew_home / "crew_data" / "memory.db"),
            log_level="INFO",
        ),
        enable_team=False,
    )
    return TestClient(create_app(crew=app)), app


def test_wiki_init_and_pages_crud(tmp_path, auth_headers):
    client, _app = _client(tmp_path)

    # init
    res = client.post("/api/wiki/init", headers=auth_headers)
    assert res.status_code == 200
    assert res.json()["ok"] is True

    # create
    res = client.post(
        "/api/wiki/pages",
        headers={**auth_headers, "Content-Type": "application/json"},
        json={
            "page_type": "topic",
            "title": "测试主题",
            "content": "测试内容",
            "tags": ["tag1"],
        },
    )
    assert res.status_code == 200
    page = res.json()["page"]
    assert page["title"] == "测试主题"
    page_id = page["id"]

    # list
    res = client.get("/api/wiki/pages", headers=auth_headers)
    assert res.status_code == 200
    assert len(res.json()["pages"]) == 1

    # get
    res = client.get(f"/api/wiki/pages/{page_id}", headers=auth_headers)
    assert res.status_code == 200
    assert res.json()["page"]["content"] == "测试内容"

    # update
    res = client.put(
        f"/api/wiki/pages/{page_id}",
        headers={**auth_headers, "Content-Type": "application/json"},
        json={"content": "更新后内容"},
    )
    assert res.status_code == 200
    assert res.json()["page"]["content"] == "更新后内容"

    # search
    res = client.get("/api/wiki/search?q=测试", headers=auth_headers)
    assert res.status_code == 200
    assert len(res.json()["pages"]) == 1

    # delete
    res = client.delete(f"/api/wiki/pages/{page_id}", headers=auth_headers)
    assert res.status_code == 200
    res = client.get(f"/api/wiki/pages/{page_id}", headers=auth_headers)
    assert res.status_code == 404


def test_wiki_page_detail_returns_outgoing_and_incoming_relations(tmp_path, auth_headers):
    client, _app = _client(tmp_path)
    client.post("/api/wiki/init", headers=auth_headers)

    def create(page_type: str, title: str) -> str:
        response = client.post(
            "/api/wiki/pages",
            headers={**auth_headers, "Content-Type": "application/json"},
            json={"page_type": page_type, "title": title, "content": title},
        )
        assert response.status_code == 200
        return response.json()["page"]["id"]

    keyword_id = create("entity", "罗马")
    topic_id = create("topic", "意大利城市旅行")
    source_id = create("source", "意大利旅行行程单")

    response = client.put(
        f"/api/wiki/pages/{keyword_id}",
        headers={**auth_headers, "Content-Type": "application/json"},
        json={
            "relations": [
                {"target_page_id": topic_id, "relation": "part_of"},
            ],
        },
    )
    assert response.status_code == 200
    response = client.put(
        f"/api/wiki/pages/{source_id}",
        headers={**auth_headers, "Content-Type": "application/json"},
        json={
            "relations": [
                {"target_page_id": keyword_id, "relation": "describes"},
            ],
        },
    )
    assert response.status_code == 200

    detail = client.get(f"/api/wiki/pages/{keyword_id}", headers=auth_headers)
    assert detail.status_code == 200
    relations = detail.json()["relation_pages"]
    assert {
        (item["id"], item["relation"], item["direction"])
        for item in relations
    } == {
        (topic_id, "part_of", "outgoing"),
        (source_id, "describes", "incoming"),
    }


def test_wiki_summary_read_does_not_implicitly_call_llm(tmp_path, auth_headers):
    from crew.wiki.schemas import KBSummary

    client, app = _client(tmp_path)
    summarizer = app._wiki_summarizer
    summarizer.get_summary = MagicMock(
        return_value=KBSummary(summary="缓存摘要", status="stale")
    )
    summarizer.generate_kb_summary = AsyncMock(
        side_effect=AssertionError("普通 summary GET 不得调用 LLM")
    )

    res = client.get("/api/wiki/summary", headers=auth_headers)

    assert res.status_code == 200
    assert res.json()["summary"] == "缓存摘要"
    assert res.json()["status"] == "stale"
    summarizer.get_summary.assert_called_once_with(OWNER, "default")
    summarizer.generate_kb_summary.assert_not_awaited()


def test_wiki_agent_session_is_preset_and_isolated_per_kb(tmp_path, auth_headers):
    client, app = _client(tmp_path)

    first = client.post("/api/wiki/agent-session?kb_id=project_a", headers=auth_headers)
    repeated = client.post("/api/wiki/agent-session?kb_id=project_a", headers=auth_headers)
    other = client.post("/api/wiki/agent-session?kb_id=project_b", headers=auth_headers)

    assert first.status_code == 200
    assert first.json()["session_id"] == repeated.json()["session_id"]
    assert first.json()["session_id"] != other.json()["session_id"]
    assert first.json()["kb_id"] == "project_a"

    cfg = app.session_store.get_agent_config(
        first.json()["session_id"], owner_account_id=OWNER
    )
    assert cfg["preset_agent_type"] == "Wiki"
    assert cfg["wiki_agent_session"] is True
    assert cfg["wiki_kb_id"] == "project_a"


def test_wiki_agent_session_force_new_and_history(tmp_path, auth_headers):
    client, _app = _client(tmp_path)

    first = client.post("/api/wiki/agent-session?kb_id=project_a", headers=auth_headers)
    second = client.post(
        "/api/wiki/agent-session?kb_id=project_a&force_new=true",
        headers=auth_headers,
    )
    client.post("/api/wiki/agent-session?kb_id=project_b", headers=auth_headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["session_id"] != second.json()["session_id"]

    history = client.get("/api/wiki/agent-sessions?kb_id=project_a", headers=auth_headers)
    assert history.status_code == 200
    data = history.json()
    assert data["kb_id"] == "project_a"
    assert {item["session_id"] for item in data["sessions"]} == {
        first.json()["session_id"],
        second.json()["session_id"],
    }
    assert all(item["workspace_id"] == "wiki" for item in data["sessions"])


def test_wiki_agent_session_rejects_unsafe_kb_id(tmp_path, auth_headers):
    client, _app = _client(tmp_path)
    res = client.post("/api/wiki/agent-session?kb_id=../escape", headers=auth_headers)
    assert res.status_code == 400
    assert "kb_id" in res.json()["error"]


def test_wiki_tools_are_exclusive_to_wiki_preset(tmp_path):
    _client_instance, app = _client(tmp_path)
    with patch("crew.app.build_provider_for_profile", return_value=app.provider):
        main_agent = app._make_agent({}, owner_account_id=OWNER)
        wiki_agent = app._make_agent(
            {"wiki_agent_session": True, "preset_agent_type": "Wiki"},
            owner_account_id=OWNER,
        )

    definition = app.subagent_registry.get("Wiki")
    run_agent_wiki = app._make_subagent(build_preset_spec(definition))
    main_expected = app._single_agent_tool_filter(
        "builtin",
        app.config.access_control.resolve_for("internal"),
    )
    expected = app._wiki_agent_tool_filter(main_expected)

    assert not any(name.startswith("wiki_") for name in main_agent.tool_filter)
    assert main_agent.wiki_manager is None
    assert wiki_agent.tool_filter == expected
    assert run_agent_wiki.tool_filter == wiki_agent.tool_filter
    assert run_agent_wiki.system_prompt == wiki_agent.system_prompt
    assert run_agent_wiki.enabled_skills == wiki_agent.enabled_skills
    assert run_agent_wiki.wiki_manager is wiki_agent.wiki_manager
    assert run_agent_wiki.tool_disclosure_mode == wiki_agent.tool_disclosure_mode
    assert "wiki_search" in wiki_agent.tool_filter
    assert "wiki_apply_ingest" in wiki_agent.tool_filter
    assert ("web_search" in wiki_agent.tool_filter) == ("web_search" in main_expected)
    assert ("web_extract" in wiki_agent.tool_filter) == ("web_extract" in main_expected)
    assert "terminal" in wiki_agent.tool_filter
    assert "file_read" in wiki_agent.tool_filter
    assert "wiki_compile" not in wiki_agent.tool_filter
    assert wiki_agent.wiki_manager is app.wiki_manager
    assert wiki_agent.agent_id == "subagent_Wiki"
    assert wiki_agent.tool_disclosure_mode == "direct"
    assert "crew-wiki-curator" in (main_agent.disabled_skills or [])
    assert wiki_agent.enabled_skills == ["crew-wiki-curator"]
    assert "crew-wiki-curator" not in (wiki_agent.disabled_skills or [])


def test_legacy_wiki_session_flag_does_not_grant_wiki_tools(tmp_path):
    _client_instance, app = _client(tmp_path)
    with patch("crew.app.build_provider_for_profile", return_value=app.provider):
        agent = app._make_agent(
            {"wiki_agent_session": True},
            owner_account_id=OWNER,
        )

    assert not any(name.startswith("wiki_") for name in agent.tool_filter)
    assert agent.wiki_manager is None
    assert agent.tool_disclosure_mode == "progressive"


def test_cancel_confirmation_is_owner_and_session_scoped(tmp_path, auth_headers):
    client, app = _client(tmp_path)
    issued = app.wiki_manager.issue_confirmation(
        "wiki-session",
        action="archive_page",
        kb_id="default",
        payload={"page_id": "p1"},
        summary="归档",
        impact={},
        owner_account_id=OWNER,
    )
    cid = issued["confirmation_id"]
    wrong = client.post(
        f"/api/wiki/confirmations/{cid}/cancel",
        json={"session_id": "other-session"},
        headers=auth_headers,
    )
    assert wrong.status_code == 404
    ok = client.post(
        f"/api/wiki/confirmations/{cid}/cancel",
        json={"session_id": "wiki-session"},
        headers=auth_headers,
    )
    assert ok.status_code == 200
    assert ok.json()["cancelled"] is True


def test_wiki_page_response_includes_source_titles(tmp_path, auth_headers):
    """页面详情/列表/搜索/创建/更新接口应返回 source_id -> title 映射。"""
    client, app = _client(tmp_path)
    from crew.wiki.schemas import RawSource

    # 准备一个 RawSource（与请求使用相同的 owner 与 kb_id）
    app._wiki_store.save_raw(
        RawSource(id="src_doc", title="产品文档.docx", source_type="upload", parsed_path=""),
        owner_account_id=OWNER,
    )
    source_res = client.post(
        "/api/wiki/pages",
        headers={**auth_headers, "Content-Type": "application/json"},
        json={
            "page_type": "source",
            "title": "产品文档摘要",
            "content": "产品文档来源摘要。",
            "sources": ["src_doc"],
        },
    )
    source_page_id = source_res.json()["page"]["id"]

    # 创建引用该 source 的页面
    res = client.post(
        "/api/wiki/pages",
        headers={**auth_headers, "Content-Type": "application/json"},
        json={
            "page_type": "topic",
            "title": "产品分析",
            "content": "基于产品文档整理。",
            "sources": ["src_doc", "src_doc"],
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["source_titles"]["src_doc"] == "产品文档.docx"
    page_id = data["page"]["id"]

    # 详情
    res = client.get(f"/api/wiki/pages/{page_id}", headers=auth_headers)
    assert res.json()["source_titles"]["src_doc"] == "产品文档.docx"
    assert res.json()["source_pages"] == [{
        "id": source_page_id,
        "title": "产品文档摘要",
        "page_type": "source",
    }]

    # 列表
    res = client.get("/api/wiki/pages", headers=auth_headers)
    assert res.json()["source_titles"]["src_doc"] == "产品文档.docx"

    # 搜索
    res = client.get("/api/wiki/search?q=产品文档", headers=auth_headers)
    assert res.json()["source_titles"]["src_doc"] == "产品文档.docx"

    # 更新
    res = client.put(
        f"/api/wiki/pages/{page_id}",
        headers={**auth_headers, "Content-Type": "application/json"},
        json={"content": "更新后内容"},
    )
    assert res.json()["source_titles"]["src_doc"] == "产品文档.docx"
    assert res.json()["source_pages"][0]["id"] == source_page_id


def test_wiki_page_response_includes_source_files(tmp_path, auth_headers):
    """页面详情/列表/搜索/创建/更新接口应返回 source_id -> 原始文件元信息映射。"""
    client, app = _client(tmp_path)
    from crew.wiki.schemas import RawSource

    original = tmp_path / "product.docx"
    original.write_bytes(b"original bytes")

    app._wiki_store.save_raw(
        RawSource(
            id="src_file",
            title="产品文档.docx",
            source_type="upload",
            parsed_path="",
            original_path=str(original),
            file_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size=len(b"original bytes"),
        ),
        owner_account_id=OWNER,
    )

    res = client.post(
        "/api/wiki/pages",
        headers={**auth_headers, "Content-Type": "application/json"},
        json={
            "page_type": "topic",
            "title": "产品分析",
            "content": "基于产品文档整理。",
            "sources": ["src_file"],
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert "source_files" in data
    assert data["source_files"]["src_file"]["title"] == "产品文档.docx"
    assert data["source_files"]["src_file"]["original_path"] == str(original)
    page_id = data["page"]["id"]

    # 详情
    res = client.get(f"/api/wiki/pages/{page_id}", headers=auth_headers)
    assert res.json()["source_files"]["src_file"]["title"] == "产品文档.docx"

    # 列表
    res = client.get("/api/wiki/pages", headers=auth_headers)
    assert res.json()["source_files"]["src_file"]["title"] == "产品文档.docx"

    # 搜索
    res = client.get("/api/wiki/search?q=产品文档", headers=auth_headers)
    assert res.json()["source_files"]["src_file"]["title"] == "产品文档.docx"


def test_wiki_source_file_download(tmp_path, auth_headers):
    """GET /api/wiki/sources/{source_id}/file 应返回原始文件。"""
    client, app = _client(tmp_path)
    from crew.wiki.schemas import RawSource

    original = tmp_path / "report.pptx"
    original.write_bytes(b"pptx binary content")

    app._wiki_store.save_raw(
        RawSource(
            id="src_pptx",
            title="汇报.pptx",
            source_type="upload",
            parsed_path="",
            original_path=str(original),
            file_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            size=len(b"pptx binary content"),
        ),
        owner_account_id=OWNER,
    )

    res = client.get("/api/wiki/sources/src_pptx/file", headers=auth_headers)
    from urllib.parse import unquote

    assert res.status_code == 200
    assert res.content == b"pptx binary content"
    assert res.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )
    disposition = unquote(res.headers.get("content-disposition", ""))
    assert "汇报.pptx" in disposition

    # 不存在的 source
    res = client.get("/api/wiki/sources/not_exist/file", headers=auth_headers)
    assert res.status_code == 404


def test_wiki_upload_and_ingest_txt(tmp_path, auth_headers):
    client, app = _client(tmp_path)
    app._wiki_summarizer.maybe_refresh = AsyncMock(
        side_effect=AssertionError("上传接口不得隐式调用 LLM 摘要")
    )

    res = client.post(
        "/api/wiki/upload",
        headers=auth_headers,
        data={},
        files={"file": ("note.txt", b"hello world", "text/plain")},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    source_id = data["source_id"]
    app._wiki_summarizer.maybe_refresh.assert_not_awaited()

    res = client.post(
        "/api/wiki/ingest",
        headers={**auth_headers, "Content-Type": "application/json"},
        json={"source_id": source_id},
    )
    assert res.status_code == 200
    assert res.json()["ok"] is True


def test_wiki_upload_with_kb_id(tmp_path, auth_headers):
    client, _app = _client(tmp_path)

    res = client.post(
        "/api/wiki/upload?kb_id=interview",
        headers=auth_headers,
        data={},
        files={"file": ("interview.md", "# 面试问题\n\nQ1".encode("utf-8"), "text/markdown")},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True

    # 上传会自动创建 interview 知识库目录
    res = client.get("/api/wiki/kbs", headers=auth_headers)
    assert res.status_code == 200
    assert any(kb["id"] == "interview" for kb in res.json()["kbs"])


def test_wiki_upload_rejects_missing_file(tmp_path, auth_headers):
    client, _app = _client(tmp_path)
    res = client.post(
        "/api/wiki/upload",
        headers=auth_headers,
        data={},
    )
    assert res.status_code == 400
    data = res.json()
    assert "file" in data["error"]


def test_wiki_upload_rejects_empty_file(tmp_path, auth_headers):
    client, _app = _client(tmp_path)
    res = client.post(
        "/api/wiki/upload",
        headers=auth_headers,
        data={},
        files={"file": ("empty.txt", b"", "text/plain")},
    )
    assert res.status_code == 400
    assert "空" in res.json()["error"]


def test_wiki_upload_unknown_binary_returns_clear_error(tmp_path, auth_headers):
    client, _app = _client(tmp_path)
    res = client.post(
        "/api/wiki/upload",
        headers=auth_headers,
        data={},
        files={"file": ("data.bin", b"\x00\x01\x02\x03", "application/octet-stream")},
    )
    # 原文件保留为 RawSource，但质量门阻止乱码进入后续 LLM 分析。
    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert res.json()["needs_agent_review"] is True
    assert "二进制" in res.json()["error"]


def test_wiki_upload_parse_failure_returns_needs_agent_review(tmp_path, auth_headers, monkeypatch):
    """文档解析失败时应保存原文件为 raw source，并返回 needs_agent_review 让 Agent 接管。"""
    import crew.gateway.routers.wiki as wiki_router

    def _bad_parse(content, filename):
        raise Exception("expected <class 'openpyxl.styles.fills.Fill'>")

    monkeypatch.setattr(wiki_router, "parse_document_from_bytes", _bad_parse)

    client, app = _client(tmp_path)
    res = client.post(
        "/api/wiki/upload?kb_id=review",
        headers=auth_headers,
        data={},
        files={
            "file": (
                "weird.xlsx",
                b"fake xlsx bytes",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["needs_agent_review"] is True
    assert "expected" in data["error"]
    assert "已交给 Wiki Agent" in data["message"]
    source_id = data["source_id"]

    raw = app._wiki_store.load_raw(source_id, owner_account_id=OWNER, kb_id="review")
    assert raw is not None
    assert raw.parse_status == "failed"
    assert raw.parse_error is not None
    assert raw.original_path
    assert Path(raw.original_path).exists()


def test_wiki_upload_pdf_without_dependency_returns_clear_error(tmp_path, auth_headers, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "fitz", None)
    client, _app = _client(tmp_path)
    res = client.post(
        "/api/wiki/upload",
        headers=auth_headers,
        data={},
        files={"file": ("paper.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )
    assert res.status_code == 400
    data = res.json()
    assert data["error_code"] == "MISSING_DEPENDENCY"
    assert data["dependency"] == "pymupdf"
    assert "wiki" in data["install_command"]
    assert "pymupdf" in data["error"]


def test_wiki_upload_docx_without_dependency_returns_structured_error(tmp_path, auth_headers, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "docx", None)
    client, _app = _client(tmp_path)
    res = client.post(
        "/api/wiki/upload",
        headers=auth_headers,
        data={},
        files={"file": ("report.docx", b"fake docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert res.status_code == 400
    data = res.json()
    assert data["error_code"] == "MISSING_DEPENDENCY"
    assert data["dependency"] == "python-docx"
    assert "wiki" in data["install_command"]


def test_wiki_upload_docx_parses_content(tmp_path, auth_headers):
    client, _app = _client(tmp_path)

    docx = pytest.importorskip("docx")
    from io import BytesIO

    buffer = BytesIO()
    document = docx.Document()
    document.add_heading("面试问题", level=1)
    document.add_paragraph("请介绍你自己。")
    document.save(buffer)
    buffer.seek(0)

    res = client.post(
        "/api/wiki/upload?kb_id=interview",
        headers=auth_headers,
        data={},
        files={"file": ("interview.docx", buffer.read(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["title"] == "interview.docx"

    # 解析后的文本应包含中文内容
    source_id = data["source_id"]
    res = client.post(
        "/api/wiki/ingest?kb_id=interview",
        headers={**auth_headers, "Content-Type": "application/json"},
        json={"source_id": source_id},
    )
    assert res.status_code == 200
    result = res.json()
    assert result["ok"] is True


def test_wiki_ingest_with_session_id_accepts_progress_target(tmp_path, auth_headers):
    """POST /api/wiki/ingest 带 session_id 时，应正常完成且不触发 _push_payload_fn 异常。

    注：router 内部通过 asyncio.create_task 推送进度帧，同步 TestClient 无法等待 task
    完成，因此本测试只验证接口契约（返回 200、body 正确），不断言具体帧内容。
    """
    import json
    from crew.core.mocks import FakeProvider
    from crew.core.types import ChatResponse

    client, app = _client(tmp_path)

    # 让 ingest 的 LLM 分析成功返回空结果
    app._wiki_compiler.provider = FakeProvider(
        script=[
            ChatResponse(
                text=json.dumps(
                    {"entities": [], "topics": [], "relationships": []},
                    ensure_ascii=False,
                )
            )
        ]
    )

    # 上传文件
    res = client.post(
        "/api/wiki/upload",
        headers=auth_headers,
        data={},
        files={"file": ("note.txt", b"hello world", "text/plain")},
    )
    assert res.status_code == 200
    source_id = res.json()["source_id"]

    calls: list[dict] = []

    async def _push_payload(session_id: str, payload: dict, owner_account_id: str = "") -> None:
        calls.append({"session_id": session_id, "payload": payload, "owner_account_id": owner_account_id})

    app._push_payload_fn = _push_payload

    res = client.post(
        "/api/wiki/ingest",
        headers={**auth_headers, "Content-Type": "application/json"},
        json={"source_id": source_id, "session_id": "sess_progress_1"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["source_id"] == source_id
    assert "pages" in data
    # router 在返回前 gather 等待 progress task，因此可以断言推送内容
    assert len(calls) > 0
    assert calls[0]["session_id"] == "sess_progress_1"
    assert calls[0]["payload"]["kind"] == "wiki_ingest_progress"
    assert calls[0]["payload"]["body"]["stage"] == "load"


def test_wiki_ingest_without_session_id_does_not_push_progress(tmp_path, auth_headers):
    """POST /api/wiki/ingest 不带 session_id 时，不应触发 _push_payload_fn。"""
    client, app = _client(tmp_path)

    res = client.post(
        "/api/wiki/upload",
        headers=auth_headers,
        data={},
        files={"file": ("note.txt", b"hello world", "text/plain")},
    )
    assert res.status_code == 200
    source_id = res.json()["source_id"]

    pushed: list[dict] = []

    async def _push_payload(session_id: str, payload: dict, owner_account_id: str = "") -> None:
        pushed.append(payload)

    app._push_payload_fn = _push_payload

    res = client.post(
        "/api/wiki/ingest",
        headers={**auth_headers, "Content-Type": "application/json"},
        json={"source_id": source_id},
    )
    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert len(pushed) == 0


def test_wiki_lint(tmp_path, auth_headers):
    client, _app = _client(tmp_path)
    res = client.post("/api/wiki/lint", headers=auth_headers)
    assert res.status_code == 200
    assert "issues" in res.json()


def test_wiki_kb_crud(tmp_path, auth_headers):
    client, _app = _client(tmp_path)

    # list initially only default after init
    res = client.post("/api/wiki/init", headers=auth_headers)
    assert res.status_code == 200

    res = client.get("/api/wiki/kbs", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert any(kb["id"] == "default" for kb in data["kbs"])

    # create
    res = client.post(
        "/api/wiki/kbs",
        headers={**auth_headers, "Content-Type": "application/json"},
        json={"kb_id": "project_a", "name": "Project A"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["kb"]["id"] == "project_a"
    assert data["kb"]["name"] == "Project A"

    duplicate = client.post(
        "/api/wiki/kbs",
        headers={**auth_headers, "Content-Type": "application/json"},
        json={"kb_id": "project_a", "name": "Duplicate"},
    )
    assert duplicate.status_code == 400

    # cannot create default
    res = client.post(
        "/api/wiki/kbs",
        headers={**auth_headers, "Content-Type": "application/json"},
        json={"kb_id": "default"},
    )
    assert res.status_code == 400

    # delete
    res = client.delete("/api/wiki/kbs/project_a", headers=auth_headers)
    assert res.status_code == 200
    res = client.delete("/api/wiki/kbs/project_a", headers=auth_headers)
    assert res.status_code == 404

    # cannot delete default
    res = client.delete("/api/wiki/kbs/default", headers=auth_headers)
    assert res.status_code == 400
    # built-in tutorial is also protected
    res = client.delete("/api/wiki/kbs/tutorial", headers=auth_headers)
    assert res.status_code == 400


def test_delete_and_recreate_same_kb_uses_fresh_wiki_session(tmp_path, auth_headers):
    client, app = _client(tmp_path)
    payload = {"kb_id": "project_a", "name": "Project A"}
    assert client.post(
        "/api/wiki/kbs",
        headers={**auth_headers, "Content-Type": "application/json"},
        json=payload,
    ).status_code == 200
    first = client.post(
        "/api/wiki/agent-session?kb_id=project_a",
        headers=auth_headers,
    ).json()["session_id"]

    deleted = client.delete("/api/wiki/kbs/project_a", headers=auth_headers)

    assert deleted.status_code == 200
    assert deleted.json()["deleted_session_ids"] == [first]
    assert not app.session_store.session_belongs_to(first, OWNER)
    assert client.post(
        "/api/wiki/kbs",
        headers={**auth_headers, "Content-Type": "application/json"},
        json=payload,
    ).status_code == 200
    second = client.post(
        "/api/wiki/agent-session?kb_id=project_a",
        headers=auth_headers,
    ).json()["session_id"]
    assert second != first


def test_vault_root_documents_are_readable_but_path_is_whitelisted(
    tmp_path,
    auth_headers,
):
    client, _app = _client(tmp_path)
    assert client.post("/api/wiki/init", headers=auth_headers).status_code == 200

    home = client.get(
        "/api/wiki/vault-documents/Home.md?kb_id=default",
        headers=auth_headers,
    )
    index = client.get(
        "/api/wiki/vault-documents/index.md?kb_id=default",
        headers=auth_headers,
    )
    forbidden = client.get(
        "/api/wiki/vault-documents/log.md?kb_id=default",
        headers=auth_headers,
    )

    assert home.status_code == 200
    assert home.json()["document"]["name"] == "Home.md"
    assert "# " in home.json()["document"]["content"]
    assert index.status_code == 200
    assert index.json()["document"]["name"] == "index.md"
    assert forbidden.status_code == 400


def test_init_repairs_existing_kb_without_vault_root_documents(
    tmp_path,
    auth_headers,
):
    client, app = _client(tmp_path)
    store = app._wiki_store
    base = store._kb_root(OWNER) / "legacy_kb"
    base.mkdir(parents=True)

    missing = client.get(
        "/api/wiki/vault-documents/Home.md?kb_id=legacy_kb",
        headers=auth_headers,
    )
    assert missing.status_code == 404

    initialized = client.post(
        "/api/wiki/init?kb_id=legacy_kb",
        headers=auth_headers,
    )
    assert initialized.status_code == 200

    home = client.get(
        "/api/wiki/vault-documents/Home.md?kb_id=legacy_kb",
        headers=auth_headers,
    )
    index = client.get(
        "/api/wiki/vault-documents/index.md?kb_id=legacy_kb",
        headers=auth_headers,
    )
    assert home.status_code == 200
    assert index.status_code == 200


def test_wiki_kb_crud_supports_chinese_id(tmp_path, auth_headers):
    client, _app = _client(tmp_path)

    res = client.post(
        "/api/wiki/kbs",
        headers={**auth_headers, "Content-Type": "application/json"},
        json={"kb_id": "产品知识库", "name": "产品知识库"},
    )

    assert res.status_code == 200
    data = res.json()
    assert data["kb"]["id"] == "产品知识库"
    assert data["kb"]["name"] == "产品知识库"
    assert data["kb"]["vault_path"].endswith("/wiki_lib/产品知识库")

    res = client.get("/api/wiki/kbs", headers=auth_headers)
    assert any(kb["id"] == "产品知识库" for kb in res.json()["kbs"])

    res = client.delete("/api/wiki/kbs/产品知识库", headers=auth_headers)
    assert res.status_code == 200


def test_wiki_pages_isolated_by_kb_id(tmp_path, auth_headers):
    client, _app = _client(tmp_path)

    res = client.post("/api/wiki/init?kb_id=default", headers=auth_headers)
    assert res.status_code == 200
    res = client.post("/api/wiki/init?kb_id=project_a", headers=auth_headers)
    assert res.status_code == 200

    # create distinct pages in each KB
    res = client.post(
        "/api/wiki/pages?kb_id=default",
        headers={**auth_headers, "Content-Type": "application/json"},
        json={"page_type": "topic", "title": "DefaultOnly", "content": "default content"},
    )
    assert res.status_code == 200
    default_page_id = res.json()["page"]["id"]

    res = client.post(
        "/api/wiki/pages?kb_id=project_a",
        headers={**auth_headers, "Content-Type": "application/json"},
        json={"page_type": "topic", "title": "ProjectOnly", "content": "project content"},
    )
    assert res.status_code == 200
    project_page_id = res.json()["page"]["id"]

    # list isolation
    res = client.get("/api/wiki/pages?kb_id=default", headers=auth_headers)
    assert len(res.json()["pages"]) == 1
    assert res.json()["pages"][0]["title"] == "DefaultOnly"
    res = client.get("/api/wiki/pages?kb_id=project_a", headers=auth_headers)
    assert len(res.json()["pages"]) == 1
    assert res.json()["pages"][0]["title"] == "ProjectOnly"

    # get isolation
    res = client.get(f"/api/wiki/pages/{default_page_id}?kb_id=default", headers=auth_headers)
    assert res.status_code == 200
    assert res.json()["page"]["content"] == "default content"
    res = client.get(f"/api/wiki/pages/{default_page_id}?kb_id=project_a", headers=auth_headers)
    assert res.status_code == 404

    res = client.get(f"/api/wiki/pages/{project_page_id}?kb_id=project_a", headers=auth_headers)
    assert res.status_code == 200
    assert res.json()["page"]["content"] == "project content"
    res = client.get(f"/api/wiki/pages/{project_page_id}?kb_id=default", headers=auth_headers)
    assert res.status_code == 404

    # search isolation: results must stay within the requested kb; FTS5 search
    # may surface other pages in the same kb, but must never leak pages from
    # another kb.
    res = client.get("/api/wiki/search?q=DefaultOnly&kb_id=default", headers=auth_headers)
    assert any(p["id"] == default_page_id for p in res.json()["pages"])
    res = client.get("/api/wiki/search?q=DefaultOnly&kb_id=project_a", headers=auth_headers)
    assert not any(p["id"] == default_page_id for p in res.json()["pages"])
    res = client.get("/api/wiki/search?q=ProjectOnly&kb_id=project_a", headers=auth_headers)
    assert any(p["id"] == project_page_id for p in res.json()["pages"])
    res = client.get("/api/wiki/search?q=ProjectOnly&kb_id=default", headers=auth_headers)
    assert not any(p["id"] == project_page_id for p in res.json()["pages"])

    # delete isolation
    res = client.delete(f"/api/wiki/pages/{default_page_id}?kb_id=default", headers=auth_headers)
    assert res.status_code == 200
    res = client.get(f"/api/wiki/pages/{default_page_id}?kb_id=default", headers=auth_headers)
    assert res.status_code == 404
    res = client.get(f"/api/wiki/pages/{project_page_id}?kb_id=project_a", headers=auth_headers)
    assert res.status_code == 200


def test_wiki_graph(tmp_path, auth_headers):
    """GET /api/wiki/graph 返回页面节点与相关关系边。"""
    client, _app = _client(tmp_path)
    client.post("/api/wiki/init", headers=auth_headers)

    # 创建两个互相关联的页面
    res = client.post(
        "/api/wiki/pages",
        headers={**auth_headers, "Content-Type": "application/json"},
        json={
            "page_type": "topic",
            "title": "部署规范",
            "content": "参考 [[CI/CD 流水线]]。",
            "sources": ["upload_abc"],
        },
    )
    assert res.status_code == 200
    deploy_id = res.json()["page"]["id"]

    res = client.post(
        "/api/wiki/pages",
        headers={**auth_headers, "Content-Type": "application/json"},
        json={
            "page_type": "entity",
            "title": "CI/CD 流水线",
            "content": "持续集成与交付。",
        },
    )
    assert res.status_code == 200
    cicd_id = res.json()["page"]["id"]

    res = client.get("/api/wiki/graph", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    graph = data["graph"]
    node_ids = {n["id"] for n in graph["nodes"]}
    assert deploy_id in node_ids
    assert cicd_id in node_ids
    assert any(n["id"].startswith("source:") and "upload_abc" in n["id"] for n in graph["nodes"])

    relations = {(e["source"], e["target"], e["relation"]) for e in graph["edges"]}
    assert (deploy_id, cicd_id, "mentions") in relations
    assert any(e["source"] == deploy_id and e["relation"] == "source_of" for e in graph["edges"])


def test_wiki_bulk_delete(tmp_path, auth_headers):
    client, _app = _client(tmp_path)
    client.post("/api/wiki/init", headers=auth_headers)

    ids = []
    for title in ("页面 A", "页面 B"):
        res = client.post(
            "/api/wiki/pages",
            headers={**auth_headers, "Content-Type": "application/json"},
            json={"page_type": "topic", "title": title, "content": "x"},
        )
        assert res.status_code == 200
        ids.append(res.json()["page"]["id"])

    res = client.request(
        "DELETE",
        "/api/wiki/pages",
        headers={**auth_headers, "Content-Type": "application/json"},
        json={"page_ids": ids},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert sorted(data["deleted"]) == sorted(ids)
    assert len(data["failed"]) == 0

    res = client.get("/api/wiki/pages", headers=auth_headers)
    assert len(res.json()["pages"]) == 0




def test_wiki_upload_image_auto_ingests_when_enabled(tmp_path, auth_headers):
    """前端上传图片，默认自动理解并 ingest。"""
    client, app = _client(tmp_path)
    # 默认 config.multimodal.enabled=True, auto_image=True

    fake_result = MagicMock()
    fake_result.pages = [MagicMock(to_dict=lambda: {"title": "猫", "page_type": "entity"})]
    fake_result.issues = []
    app._wiki_compiler.ingest = AsyncMock(return_value=fake_result)

    with patch("crew.wiki.multimodal.describe_media", return_value="图片里有一只猫") as mock_describe:
        res = client.post(
            "/api/wiki/upload",
            headers=auth_headers,
            data={},
            files={"file": ("cat.png", b"fake png", "image/png")},
        )

    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["source_type"] == "image"
    assert data["ingested"] is True
    assert len(data["pages"]) > 0
    mock_describe.assert_called_once()
    app._wiki_compiler.ingest.assert_awaited_once()


def test_wiki_upload_video_returns_needs_confirmation(tmp_path, auth_headers):
    """前端上传视频默认不自动 ingest，返回需要确认。"""
    client, app = _client(tmp_path)

    res = client.post(
        "/api/wiki/upload",
        headers=auth_headers,
        data={},
        files={"file": ("dog.mp4", b"fake mp4", "video/mp4")},
    )

    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["source_type"] == "video"
    assert data["ingested"] is False
    assert data["needs_confirmation"] is True


def test_wiki_upload_video_auto_ingests_when_configured(tmp_path, auth_headers):
    """配置 auto_video + video_upload_confirmed 后，前端上传视频自动 ingest。"""
    client, app = _client(tmp_path)
    app.config.wiki.multimodal.auto_video = True
    app.config.wiki.multimodal.video_upload_confirmed = True

    fake_result = MagicMock()
    fake_result.pages = [MagicMock(to_dict=lambda: {"title": "狗", "page_type": "entity"})]
    fake_result.issues = []
    app._wiki_compiler.ingest = AsyncMock(return_value=fake_result)

    with patch("crew.wiki.multimodal.describe_media", return_value="视频里有一只狗") as mock_describe:
        res = client.post(
            "/api/wiki/upload",
            headers=auth_headers,
            data={},
            files={"file": ("dog.mp4", b"fake mp4", "video/mp4")},
        )

    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["source_type"] == "video"
    assert data["ingested"] is True
    assert len(data["pages"]) > 0
    mock_describe.assert_called_once()
    app._wiki_compiler.ingest.assert_awaited_once()


def test_wiki_upload_multimodal_disabled_rejects_media(tmp_path, auth_headers):
    """多模态总开关关闭时，上传图片/视频被拒绝。"""
    client, app = _client(tmp_path)
    app.config.wiki.multimodal.enabled = False

    res = client.post(
        "/api/wiki/upload",
        headers=auth_headers,
        data={},
        files={"file": ("cat.png", b"fake png", "image/png")},
    )

    assert res.status_code == 400
    assert "未启用" in res.json()["error"]


def test_wiki_sources_list_and_status_filter(tmp_path, auth_headers):
    """GET /api/wiki/sources 应列出 raw sources，并支持按 parse_status 过滤。"""
    client, app = _client(tmp_path)
    from crew.wiki.schemas import RawSource

    app._wiki_store.save_raw(
        RawSource(id="src_pending", title="待解析.txt", source_type="upload", parsed_path=""),
        owner_account_id=OWNER,
    )
    app._wiki_store.save_raw(
        RawSource(
            id="src_parsed",
            title="已解析.txt",
            source_type="upload",
            parsed_path="/tmp/x.parsed.md",
            parse_status="parsed",
        ),
        owner_account_id=OWNER,
    )
    app._wiki_store.save_raw(
        RawSource(
            id="src_failed",
            title="失败.txt",
            source_type="upload",
            parsed_path="",
            parse_status="failed",
            parse_error="missing dep",
        ),
        owner_account_id=OWNER,
    )

    res = client.get("/api/wiki/sources", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["total"] == 3
    ids = {s["id"] for s in data["sources"]}
    assert ids == {"src_pending", "src_parsed", "src_failed"}

    res = client.get("/api/wiki/sources?status=parsed", headers=auth_headers)
    assert res.json()["total"] == 1
    assert res.json()["sources"][0]["id"] == "src_parsed"

    res = client.get("/api/wiki/sources?status=failed", headers=auth_headers)
    assert res.json()["total"] == 1
    assert res.json()["sources"][0]["parse_error"] == "missing dep"


def test_wiki_delete_source_cleans_related_pages(tmp_path, auth_headers):
    """DELETE /api/wiki/sources/{source_id} 应删除 raw source 并清理关联页面。"""
    client, app = _client(tmp_path)
    from crew.wiki.schemas import RawSource

    app._wiki_store.save_raw(
        RawSource(id="src_del", title="待删.txt", source_type="upload", parsed_path=""),
        owner_account_id=OWNER,
    )
    res = client.post(
        "/api/wiki/pages",
        headers={**auth_headers, "Content-Type": "application/json"},
        json={"page_type": "topic", "title": "引用页面", "content": "x", "sources": ["src_del"]},
    )
    assert res.status_code == 200
    page_id = res.json()["page"]["id"]

    res = client.delete("/api/wiki/sources/src_del", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["deleted_source_id"] == "src_del"
    assert any(p["id"] == page_id for p in data["related_pages"])

    res = client.get("/api/wiki/sources", headers=auth_headers)
    assert not any(s["id"] == "src_del" for s in res.json()["sources"])
    res = client.get(f"/api/wiki/pages/{page_id}", headers=auth_headers)
    assert res.status_code == 404


def test_wiki_delete_source_not_found(tmp_path, auth_headers):
    """删除不存在的 source 应返回 404。"""
    client, _app = _client(tmp_path)
    res = client.delete("/api/wiki/sources/not_exist", headers=auth_headers)
    assert res.status_code == 404
    assert res.json()["ok"] is False
