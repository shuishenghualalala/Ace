"""文件上传安全测试（G3）：体积上限、TOCTOU 去重、非法文件名。"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
import pytest
from httpx import ASGITransport, AsyncClient

from crew.app import build_app
from crew.features import FeatureGeneration, FeatureScope, ServiceRegistry
from crew.gateway.auth import AccountContext
from crew.gateway.context import save_upload
from crew.gateway.routers.misc import create_misc_router
from crew.gateway.server import create_app
from crew.state.home import owner_path_segment
from crew.wiki.service import KNOWLEDGE_SERVICE_KEY


@pytest.fixture
def crew_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    return tmp_path / ".crew"


# ---------------- save_upload 直接测试 ----------------

def test_save_upload_concurrent_same_name_distinct_ids(crew_home):
    """两次同名上传落不同文件（uuid），互不覆盖；返回 name 仍是原始名。"""
    a = save_upload("note.txt", b"aaa")
    b = save_upload("note.txt", b"bbb")
    assert a["name"] == "note.txt" and b["name"] == "note.txt"
    # 落地文件不同（路径不同）
    assert a["path"] != b["path"]
    # 各自读回自己的内容（未被对方覆盖）
    assert Path(a["path"]).read_bytes() == b"aaa"
    assert Path(b["path"]).read_bytes() == b"bbb"
    # id 不同
    assert a["id"] != b["id"]


def test_save_upload_owner_scoped_paths(crew_home):
    a = save_upload("note.txt", b"aaa", owner_account_id="A:uid-a")
    b = save_upload("note.txt", b"bbb", owner_account_id="B:uid-b")
    seg_a = owner_path_segment("A:uid-a")
    seg_b = owner_path_segment("B:uid-b")
    assert "accounts" in a["path"] and seg_a in a["path"]
    assert "accounts" in b["path"] and seg_b in b["path"]
    assert Path(a["path"]).parent != Path(b["path"]).parent


def test_save_upload_rejects_path_separator(crew_home):
    with pytest.raises(ValueError):
        save_upload("../evil.txt", b"x")
    with pytest.raises(ValueError):
        save_upload("a/b.txt", b"x")
    with pytest.raises(ValueError):
        save_upload("win\\evil.txt", b"x")


def test_save_upload_rejects_nul(crew_home):
    with pytest.raises(ValueError):
        save_upload("evil\x00.txt", b"x")


def _misc_router_app(crew):
    app = FastAPI()

    @app.middleware("http")
    async def attach_test_account(request, call_next):
        request.state.account = AccountContext("A:uid-a", is_local=True)
        return await call_next(request)

    app.include_router(create_misc_router(crew))
    return app


class _CaptureService:
    def __init__(self):
        self.calls = []
        self.done = asyncio.Event()

    def list_knowledge_bases(self, owner_account_id):
        return [SimpleNamespace(id="project")]

    def session_kb_id(self, session_id, owner_account_id):
        assert session_id == "wiki-session"
        return "session-kb"

    async def capture_attachment(self, filename, content, owner_account_id, kb_id="default"):
        self.calls.append((filename, content, owner_account_id, kb_id))
        self.done.set()


class _RegistryPlugins:
    def __init__(self, registry):
        self.registry = registry

    def acquire_service_lease(self, key, *, label="service-request"):
        return self.registry.acquire_lease(key, label=label)

    def resolve_service(self, key, default=None):
        return self.registry.get(key, default=default)


class _DrainAfterAcquirePlugins(_RegistryPlugins):
    def __init__(self, registry, scope):
        super().__init__(registry)
        self.scope = scope

    def acquire_service_lease(self, key, *, label="service-request"):
        acquired = super().acquire_service_lease(key, label=label)
        self.scope.begin_draining()
        return acquired


def _capture_crew(registry):
    return SimpleNamespace(
        config=SimpleNamespace(
            wiki=SimpleNamespace(enabled=True, capture_attachments=True),
        ),
        plugins=_RegistryPlugins(registry),
        knowledge_service=None,
    )


@pytest.mark.asyncio
async def test_upload_capture_uses_external_override_without_product_record(crew_home):
    registry = ServiceRegistry()
    service = _CaptureService()
    crew = _capture_crew(registry)
    crew.knowledge_service = service
    transport = ASGITransport(app=_misc_router_app(crew))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/upload",
            json={
                "filename": "external.txt",
                "content": base64.b64encode(b"external").decode(),
            },
        )
        assert response.status_code == 200
        await service.done.wait()

    assert service.calls == [("external.txt", b"external", "A:uid-a", "default")]


@pytest.mark.asyncio
async def test_upload_capture_keeps_acquired_service_when_drain_starts_before_kb_resolution(crew_home):
    registry = ServiceRegistry()
    scope = FeatureScope(FeatureGeneration("product.wiki", 1))
    service = _CaptureService()
    registry.register(scope, KNOWLEDGE_SERVICE_KEY, service)
    scope.activate()
    crew = _capture_crew(registry)
    crew.plugins = _DrainAfterAcquirePlugins(registry, scope)
    transport = ASGITransport(app=_misc_router_app(crew))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/upload",
            json={
                "filename": "race.txt",
                "content": base64.b64encode(b"race").decode(),
                "kb_id": "project",
            },
        )
        assert response.status_code == 200
        await service.done.wait()

    assert service.calls == [("race.txt", b"race", "A:uid-a", "project")]
    await scope.dispose()


@pytest.mark.asyncio
async def test_upload_capture_resolves_explicit_and_session_kb_with_lease(crew_home):
    registry = ServiceRegistry()
    scope = FeatureScope(FeatureGeneration("product.wiki", 1))
    service = _CaptureService()
    registry.register(scope, KNOWLEDGE_SERVICE_KEY, service)
    scope.activate()
    crew = _capture_crew(registry)
    transport = ASGITransport(app=_misc_router_app(crew))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/upload",
            json={
                "filename": "explicit.txt",
                "content": base64.b64encode(b"explicit").decode(),
                "kb_id": "project",
            },
        )
        assert response.status_code == 200
        await service.done.wait()
        service.done.clear()
        response = await client.post(
            "/api/upload",
            json={
                "filename": "session.txt",
                "content": base64.b64encode(b"session").decode(),
                "session_id": "wiki-session",
            },
        )
        assert response.status_code == 200
        await service.done.wait()

    assert [(item[0], item[3]) for item in service.calls] == [
        ("explicit.txt", "project"),
        ("session.txt", "session-kb"),
    ]
    await scope.dispose()


@pytest.mark.asyncio
async def test_upload_capture_does_not_fallback_to_compat_service_during_drain(crew_home):
    registry = ServiceRegistry()
    scope = FeatureScope(FeatureGeneration("product.wiki", 1))
    service = _CaptureService()
    registry.register(scope, KNOWLEDGE_SERVICE_KEY, service)
    scope.activate()
    scope.begin_draining()
    crew = _capture_crew(registry)
    transport = ASGITransport(app=_misc_router_app(crew))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/upload",
            json={
                "filename": "draining.txt",
                "content": base64.b64encode(b"draining").decode(),
            },
        )
        assert response.status_code == 200
        await asyncio.sleep(0)

    assert service.calls == []
    await scope.dispose()


# ---------------- 路由层 413 体积上限 ----------------

@pytest.mark.asyncio
async def test_upload_oversized_returns_413(crew_home, auth_headers):
    """超过体积上限的 base64 在 b64decode 前即被拒，返回 413。"""
    crew = build_app(enable_team=False)
    app = create_app(crew)
    # 构造 > 28 MiB 的 base64 串（约 21+ MiB 解码后）
    big = base64.b64encode(b"x" * (22 * 1024 * 1024)).decode()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.post("/api/upload", json={"filename": "big.bin", "content": big})
    assert resp.status_code == 413
    assert resp.json()["ok"] is False


@pytest.mark.asyncio
async def test_upload_normal_works(crew_home, auth_headers):
    """正常体积上传成功（回归）。"""
    crew = build_app(enable_team=False)
    app = create_app(crew)
    content = base64.b64encode(b"hello world").decode()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.post("/api/upload", json={"filename": "hi.txt", "content": content})
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "hi.txt"
    assert data["size"] == len(b"hello world")
    assert "accounts" in data["path"]


# ---------------- 附件自动收入 default wiki 知识库 ----------------

@pytest.mark.asyncio
async def test_upload_captures_attachment_into_default_wiki(crew_home, auth_headers):
    """上传成功后附件被后台收入 default 知识库（保存原文 + 解析 markdown）。"""
    crew = build_app(enable_team=False)
    app = create_app(crew)
    content = base64.b64encode(b"hello wiki").decode()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.post("/api/upload", json={"filename": "wiki-note.txt", "content": content})
    assert resp.status_code == 200
    assert resp.json()["name"] == "wiki-note.txt"

    # 入库是后台任务：轮询直到 default KB 中该来源解析完成
    saved = None
    for _ in range(50):
        raws = [
            r for r in crew._wiki_store.list_raws("A:uid-a", "default")
            if r.title == "wiki-note.txt"
        ]
        if raws and raws[0].parse_status == "parsed":
            saved = raws[0]
            break
        await asyncio.sleep(0.1)
    assert saved is not None
    assert saved.parsed_path
    assert "hello wiki" in Path(saved.parsed_path).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_upload_skips_wiki_capture_when_disabled(crew_home, auth_headers):
    """wiki.capture_attachments=false 时上传行为与现状一致，不写入知识库。"""
    crew = build_app(enable_team=False)
    crew.config.wiki.capture_attachments = False
    app = create_app(crew)
    content = base64.b64encode(b"no capture").decode()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.post("/api/upload", json={"filename": "no-capture.txt", "content": content})
    assert resp.status_code == 200

    await asyncio.sleep(0.5)
    raws = [
        r for r in crew._wiki_store.list_raws("A:uid-a", "default")
        if r.title == "no-capture.txt"
    ]
    assert raws == []


# ---------------- 附件按会话/显式 kb_id 收入对应知识库 ----------------

async def _wait_raw_parsed(crew, owner: str, kb_id: str, title: str):
    """轮询后台 capture 任务，直到指定 KB 中该来源解析完成。"""
    saved = None
    for _ in range(50):
        raws = [
            r for r in crew._wiki_store.list_raws(owner, kb_id)
            if r.title == title
        ]
        if raws and raws[0].parse_status == "parsed":
            saved = raws[0]
            break
        await asyncio.sleep(0.1)
    return saved


@pytest.mark.asyncio
async def test_upload_captures_into_explicit_kb(crew_home, auth_headers):
    """上传带 kb_id 时附件收入该知识库，而不是 default。"""
    crew = build_app(enable_team=False)
    crew._wiki_store.create_kb("proj-x", name="Proj X", owner_account_id="A:uid-a")
    app = create_app(crew)
    content = base64.b64encode(b"explicit kb").decode()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.post(
            "/api/upload",
            json={"filename": "explicit-kb.txt", "content": content, "kb_id": "proj-x"},
        )
    assert resp.status_code == 200

    saved = await _wait_raw_parsed(crew, "A:uid-a", "proj-x", "explicit-kb.txt")
    assert saved is not None
    assert "explicit kb" in Path(saved.parsed_path).read_text(encoding="utf-8")
    # 不应落入 default
    default_raws = [
        r for r in crew._wiki_store.list_raws("A:uid-a", "default")
        if r.title == "explicit-kb.txt"
    ]
    assert default_raws == []


@pytest.mark.asyncio
async def test_upload_captures_into_session_bound_kb(crew_home, auth_headers):
    """上传带 session_id 且会话已绑定知识库时，附件收入绑定的知识库。"""
    crew = build_app(enable_team=False)
    crew._wiki_store.create_kb("kb-y", name="KB Y", owner_account_id="A:uid-a")
    crew.wiki_manager.set_kb_id("sess-1", "kb-y", owner_account_id="A:uid-a")
    app = create_app(crew)
    content = base64.b64encode(b"session kb").decode()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.post(
            "/api/upload",
            json={"filename": "session-kb.txt", "content": content, "session_id": "sess-1"},
        )
    assert resp.status_code == 200

    saved = await _wait_raw_parsed(crew, "A:uid-a", "kb-y", "session-kb.txt")
    assert saved is not None
    assert "session kb" in Path(saved.parsed_path).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_upload_unknown_kb_falls_back_to_default(crew_home, auth_headers):
    """上传带不存在的 kb_id 时回落 default 知识库。"""
    crew = build_app(enable_team=False)
    app = create_app(crew)
    content = base64.b64encode(b"fallback kb").decode()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.post(
            "/api/upload",
            json={"filename": "fallback-kb.txt", "content": content, "kb_id": "no-such-kb"},
        )
    assert resp.status_code == 200

    saved = await _wait_raw_parsed(crew, "A:uid-a", "default", "fallback-kb.txt")
    assert saved is not None
    assert "fallback kb" in Path(saved.parsed_path).read_text(encoding="utf-8")
