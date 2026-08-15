"""Gateway 全局 Skill 变更路由的操作者归属测试。"""

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from crew.gateway.auth import AccountContext
from crew.gateway.routers import misc


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

    app.include_router(misc.create_misc_router(SimpleNamespace(
        config=SimpleNamespace(gateway_admin_accounts=["A:uid-a"]),
    )))
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
