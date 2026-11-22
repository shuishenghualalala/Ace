"""Gateway 全局 Skill 变更路由的操作者归属测试。"""

import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from crew.gateway.auth import AccountContext
from crew.gateway.routers import misc
from crew.agent.skills import SkillSourceError


def test_skill_mutation_routes_forward_authenticated_owner(monkeypatch):
    """安装与卸载入口都必须把认证 Owner 和明确来源传给全局审计边界。"""
    calls: list[tuple[str, str | None, str]] = []

    def fake_install(slug, *, operator_account_id=None, source=""):  # noqa: ANN001
        calls.append((f"install:{slug}", operator_account_id, source))
        return True

    def fake_uninstall(slug, *, operator_account_id=None, source=""):  # noqa: ANN001
        calls.append((f"uninstall:{slug}", operator_account_id, source))
        return True

    monkeypatch.setattr(misc, "install_skill", fake_install)
    monkeypatch.setattr(misc, "uninstall_skill", fake_uninstall)

    app = FastAPI()

    @app.middleware("http")
    async def inject_account(request, call_next):  # noqa: ANN001
        request.state.account = AccountContext(owner_account_id="A:uid-a")
        return await call_next(request)

    app.include_router(misc.create_misc_router(SimpleNamespace()))
    client = TestClient(app)

    assert client.post("/api/skills/demo/install").status_code == 200
    assert client.delete("/api/skills/demo").status_code == 200

    assert calls == [
        ("install:demo", "A:uid-a", "desktop-api"),
        ("uninstall:demo", "A:uid-a", "desktop-api"),
    ]


def test_store_route_includes_local(monkeypatch):
    """store 路由响应含 local 字段（本地 skill 可安装源）。"""
    monkeypatch.setattr(misc, "list_skills", lambda **k: [])
    monkeypatch.setattr(misc, "list_optional_skills", lambda: [])
    monkeypatch.setattr(
        misc,
        "list_local_skills",
        lambda: [{"slug": "lark-doc", "source": "local"}],
    )

    crew = SimpleNamespace(config=SimpleNamespace(
        evolution_auto_trigger=False,
        evolution_auto_full_cycle=False,
        evolution_visible=False,
    ))
    app = FastAPI()
    app.include_router(misc.create_misc_router(crew))
    client = TestClient(app)

    resp = client.get("/api/skills/store")
    assert resp.status_code == 200
    data = resp.json()
    assert data["local"] == [{"slug": "lark-doc", "source": "local"}]

@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/skills", "/api/skills/store"])
async def test_catalog_requests_leave_gateway_loop_responsive(monkeypatch, path):
    loop = asyncio.get_running_loop()
    scanning = asyncio.Event()
    release = threading.Event()
    completed_before_release = []

    def list_catalog():
        loop.call_soon_threadsafe(scanning.set)
        completed_before_release.append(release.wait(timeout=2))
        return [{"slug": "scan-fixture"}]

    monkeypatch.setattr(misc, "list_skills", list_catalog)
    monkeypatch.setattr(misc, "list_optional_skills", lambda: [])
    monkeypatch.setattr(misc, "list_local_skills", lambda: [])
    crew = SimpleNamespace(config=SimpleNamespace(
        evolution_auto_trigger=False, evolution_auto_full_cycle=False, evolution_visible=False,
    ))
    app = FastAPI()

    @app.get("/ready")
    async def ready():
        return {"ready": True}

    app.include_router(misc.create_misc_router(crew))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        request = asyncio.create_task(client.get(path))
        try:
            await asyncio.wait_for(scanning.wait(), 5)
            assert (await client.get("/ready")).json() == {"ready": True}
        finally:
            release.set()
        response = await request

    assert completed_before_release == [True]
    data = response.json()
    assert (data["installed"] if path.endswith("/store") else data) == [{"slug": "scan-fixture"}]


def _skill_api_app(monkeypatch, *, is_local: bool = True, admin_accounts: list[str] | None = None):
    added: list[tuple[str, str]] = []
    removed: list[str] = []
    imported: list[tuple[str, str, str, bool]] = []

    monkeypatch.setattr(misc, "list_skill_sources", lambda: [{"path": "C:/market", "label": "Team"}])
    monkeypatch.setattr(
        misc, "add_skill_source",
        lambda path, *, label="": added.append((path, label)) or {"path": path, "label": label},
    )
    monkeypatch.setattr(
        misc, "remove_skill_source",
        lambda path: removed.append(path) or path != "missing",
    )

    def fake_import(path, *, slug="", operator_account_id=None, source="", validate=True):  # noqa: ANN001
        imported.append((str(path), slug, source, validate))
        return True

    monkeypatch.setattr(misc, "install_skill_from_dir", fake_import)
    crew = SimpleNamespace(config=SimpleNamespace(gateway_admin_accounts=admin_accounts or []))
    app = FastAPI()

    @app.middleware("http")
    async def inject_account(request, call_next):  # noqa: ANN001
        request.state.account = AccountContext(owner_account_id="A:uid-a", is_local=is_local)
        return await call_next(request)

    app.include_router(misc.create_misc_router(crew))
    return TestClient(app), added, removed, imported


def test_skill_source_and_import_routes_forward_admin_operations(monkeypatch, tmp_path):
    client, added, removed, imported = _skill_api_app(monkeypatch)
    skill_dir = tmp_path / "prepared"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: prepared\n---\nbody\n", encoding="utf-8")

    created = client.post("/api/skills/sources", json={"path": "C:/market", "label": "Team"})
    assert created.status_code == 201
    assert created.json()["source"] == {"path": "C:/market", "label": "Team"}
    listed = client.get("/api/skills/sources")
    assert listed.status_code == 200 and listed.json()["sources"][0]["label"] == "Team"
    deleted = client.delete("/api/skills/sources", params={"path": "C:/market"})
    assert deleted.status_code == 200
    missing = client.delete("/api/skills/sources", params={"path": "missing"})
    assert missing.status_code == 404

    imported_response = client.post(
        "/api/skills/import", json={"path": str(skill_dir), "slug": "prepared-copy"},
    )
    assert imported_response.status_code == 200
    assert imported_response.json() == {"ok": True, "slug": "prepared-copy"}

    monkeypatch.setattr(
        misc, "refresh_skill_source",
        lambda target: {"kind": "url", "url": target, "label": "Remote"},
    )
    refreshed = client.post(
        "/api/skills/sources/refresh", json={"url": "https://skills.example.com/manifest.json"},
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["source"]["label"] == "Remote"
    assert added == [("C:/market", "Team")]
    assert removed == ["C:/market", "missing"]
    assert imported == [(str(skill_dir), "prepared-copy", "desktop-api-import", False)]


def test_skill_source_error_is_a_validation_response(monkeypatch):
    client, _added, _removed, _imported = _skill_api_app(monkeypatch)

    def reject(path, *, label=""):  # noqa: ANN001
        raise SkillSourceError("source_exists", "该市场源已登记")

    monkeypatch.setattr(misc, "add_skill_source", reject)
    response = client.post("/api/skills/sources", json={"path": "C:/market"})
    assert response.status_code == 400
    assert response.json() == {"ok": False, "code": "source_exists", "error": "该市场源已登记"}


def test_skill_source_mutations_reject_non_admin(monkeypatch):
    client, _added, _removed, _imported = _skill_api_app(monkeypatch, is_local=False)
    for response in (
        client.post("/api/skills/sources", json={"path": "C:/market"}),
        client.delete("/api/skills/sources", params={"path": "C:/market"}),
        client.post("/api/skills/import", json={"path": "C:/market"}),
        client.post("/api/skills/sources/refresh", json={"url": "https://skills.example.com/m.json"}),
    ):
        assert response.status_code == 403
