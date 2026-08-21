from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from crew.agent.loop.tool_result_display import tool_result_detail_for_ui
from crew.core.errors import ToolError
from crew.core.runctx import (
    current_agent_workdir,
    current_owner_account_id,
    current_session_id,
    current_workspace_id,
)
from crew.core.types import ToolCall
from crew.gateway.auth import AccountContext
from crew.gateway.helpers import session_agent_label
from crew.gateway.routers.sites import create_sites_router
from crew.sites.manager import SiteBuildError, SiteManager
from crew.sites.store import SQLiteSiteStore
from crew.tools.blueprint_tools import register_blueprint_tools
from crew.tools.registry import Registry
from crew.tools.site_tools import register_site_tools


@pytest.fixture()
def site_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SiteManager:
    manager = SiteManager(SQLiteSiteStore(str(tmp_path / "crew.db")))
    root = tmp_path / "runtime-sites"
    monkeypatch.setattr(manager, "_root", lambda owner: root)
    return manager


@pytest.mark.asyncio
async def test_static_site_publish_preview_annotation_and_export(site_manager: SiteManager, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "landing"
    (source / "assets").mkdir(parents=True)
    (source / "index.html").write_text(
        '<!doctype html><link href="/assets/app.css"><h1>Hello</h1>', encoding="utf-8"
    )
    (source / "assets" / "app.css").write_text("body{background:url('/assets/bg.png')}", encoding="utf-8")
    (source / "assets" / "bg.png").write_bytes(b"png")

    result = await site_manager.publish(
        owner="owner-1", workspace_id="ws-1", session_id="session-1",
        workspace_root=str(workspace), source_path="landing", name="Landing",
    )

    site = result["site"]
    release = result["release"]
    assert site["active_release_id"] == release["id"]
    index = site_manager.release_file("owner-1", site["id"], "index.html")
    assert 'href="./assets/app.css"' in index.read_text(encoding="utf-8")
    assert "url('../assets/bg.png')" in (index.parent / "assets" / "app.css").read_text(encoding="utf-8")

    annotation = site_manager.store.create_annotation(
        "owner-1", site["id"], release["id"],
        {"selector": "h1", "element_tag": "h1", "element_text": "Hello", "comment": "改成中文"},
    )
    assert annotation["status"] == "open"
    assert site_manager.store.update_annotation_status("owner-1", annotation["id"], "resolved")["status"] == "resolved"

    archive = site_manager.export("owner-1", site["id"])
    with zipfile.ZipFile(archive) as zf:
        assert set(zf.namelist()) == {"index.html", "assets/app.css", "assets/bg.png"}
        assert "source_path" not in json.dumps(release["manifest"])


def test_copy_release_rejects_symlinked_files(site_manager: SiteManager, tmp_path: Path) -> None:
    source = tmp_path / "symlink-site"
    source.mkdir()
    (source / "index.html").write_text("<h1>site</h1>", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("not for publishing", encoding="utf-8")
    try:
        (source / "leak.txt").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不支持创建符号链接")

    with pytest.raises(SiteBuildError, match="符号链接"):
        SiteManager._copy_release(source, tmp_path / "release")


def test_copy_release_rewrites_root_assets_relative_to_nested_files(
    site_manager: SiteManager, tmp_path: Path,
) -> None:
    source = tmp_path / "nested-site"
    (source / "pages").mkdir(parents=True)
    (source / "assets" / "css").mkdir(parents=True)
    (source / "index.html").write_text("<link href=\"/assets/app.css\">", encoding="utf-8")
    (source / "pages" / "index.html").write_text(
        '<script src="/assets/app.js"></script>', encoding="utf-8",
    )
    (source / "assets" / "css" / "app.css").write_text(
        "body{background:url('/assets/bg.png')}", encoding="utf-8",
    )

    SiteManager._copy_release(source, tmp_path / "release")

    assert 'src="../assets/app.js"' in (tmp_path / "release" / "pages" / "index.html").read_text(encoding="utf-8")
    assert "url('../../assets/bg.png')" in (tmp_path / "release" / "assets" / "css" / "app.css").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_preview_directory_serves_entry_assets_and_spa_routes(site_manager: SiteManager, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "vite-site"
    (source / "assets").mkdir(parents=True)
    (source / "index.html").write_text(
        '<!doctype html><link href="/assets/app.css"><script src="/assets/app.js"></script>',
        encoding="utf-8",
    )
    (source / "assets" / "app.css").write_text("body{color:#123}", encoding="utf-8")
    (source / "assets" / "app.js").write_text("window.siteReady=true", encoding="utf-8")
    published = await site_manager.publish(
        owner="owner-1", workspace_id="ws-1", session_id="session-1",
        workspace_root=str(workspace), source_path="vite-site", name="Vite Site",
    )

    app = FastAPI()

    @app.middleware("http")
    async def identify(request: Request, call_next):
        request.state.account = AccountContext(owner_account_id="owner-1", is_local=True)
        return await call_next(request)

    app.include_router(create_sites_router(SimpleNamespace(sites=site_manager)))
    client = TestClient(app)
    site_id = published["site"]["id"]

    entry = client.get(f"/api/sites/{site_id}/preview/")
    assert entry.status_code == 200
    assert 'src="./assets/app.js"' in entry.text
    assert "data-ace-site-annotation-bridge" in entry.text
    assert client.get(f"/api/sites/{site_id}/preview/assets/app.js").text == "window.siteReady=true"
    assert client.get(f"/api/sites/{site_id}/preview/assets/app.css").text == "body{color:#123}"
    assert client.get(f"/api/sites/{site_id}/preview/settings/profile").status_code == 200


@pytest.mark.asyncio
async def test_publish_rejects_source_outside_workspace(site_manager: SiteManager, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "index.html").write_text("ok", encoding="utf-8")
    with pytest.raises(ValueError, match="Workspace"):
        await site_manager.publish(
            owner="owner", workspace_id="ws", session_id="s",
            workspace_root=str(workspace), source_path=str(outside), name="bad",
        )


@pytest.mark.asyncio
async def test_publish_requires_index_or_build_script(site_manager: SiteManager, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "empty"
    source.mkdir(parents=True)
    with pytest.raises(SiteBuildError, match="index.html"):
        await site_manager.publish(
            owner="owner", workspace_id="ws", session_id="s",
            workspace_root=str(workspace), source_path=str(source), name="empty",
        )
    assert site_manager.store.list_sites("owner") == []


def test_site_build_plan_uses_explicit_node_and_package_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    node_root = tmp_path / "node-runtime"
    node = node_root / "bin" / "node"
    npm_root = tmp_path / "packages" / "node_modules" / "npm"
    npm = npm_root / "bin" / "npm-cli.js"
    node.parent.mkdir(parents=True)
    npm.parent.mkdir(parents=True)
    node.write_bytes(b"\x7fELF")
    npm.write_text("#!/usr/bin/env node\n", encoding="utf-8")

    monkeypatch.setattr(
        "crew.sites.manager.shutil.which",
        lambda name: str(npm) if name == "npm" else str(node) if name == "node" else None,
    )

    plan = SiteManager._build_plan(["npm", "run", "build"])

    assert plan.stored_argv == ("npm", "run", "build")
    assert plan.runtime_argv == (str(node.resolve()), str(npm.resolve()), "run", "build")
    assert plan.trusted_readable_roots == (node_root.resolve(), npm_root.resolve())
    assert plan.runtime_path.split(os.pathsep)[0] == str(node.parent)


def test_site_build_plan_resolves_windows_corepack_shim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    install = tmp_path / "nodejs"
    shim = install / "pnpm.cmd"
    node = install / "node.exe"
    script = install / "node_modules" / "corepack" / "dist" / "pnpm.js"
    script.parent.mkdir(parents=True)
    shim.write_text("@echo off\n", encoding="utf-8")
    node.write_bytes(b"MZ")
    script.write_text("require('./lib/corepack.cjs')\n", encoding="utf-8")
    monkeypatch.setattr(
        "crew.sites.manager.shutil.which",
        lambda name: str(shim) if name == "pnpm" else str(node) if name == "node" else None,
    )

    plan = SiteManager._build_plan(["pnpm", "run", "build"])

    assert plan.runtime_argv == (str(node.resolve()), str(script.resolve()), "run", "build")
    assert install.resolve() in plan.trusted_readable_roots
    assert (install / "node_modules" / "corepack").resolve() in plan.trusted_readable_roots


@pytest.mark.asyncio
async def test_site_publish_authorizes_build_before_creating_records(
    site_manager: SiteManager, tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "app"
    source.mkdir(parents=True)
    (source / "package.json").write_text(
        json.dumps({"scripts": {"build": "vite build"}}), encoding="utf-8",
    )
    calls: list[tuple[tuple[str, ...], Path, str]] = []

    async def reject(argv: tuple[str, ...], cwd: Path, preview: str) -> None:
        calls.append((argv, cwd, preview))
        raise RuntimeError("not approved")

    with pytest.raises(RuntimeError, match="not approved"):
        await site_manager.publish(
            owner="owner", workspace_id="ws", session_id="s",
            workspace_root=str(workspace), source_path="app", name="App",
            build_authorizer=reject,
        )

    assert calls and calls[0][1:] == (source.resolve(), "npm run build")
    assert Path(calls[0][0][0]).stem == "node"  # Windows 上解析为 node.exe
    assert site_manager.store.list_sites("owner") == []


@pytest.mark.asyncio
async def test_publish_site_emits_standard_inspiration_surface(
    site_manager: SiteManager, tmp_path: Path,
) -> None:
    source = tmp_path / "published-app"
    source.mkdir()
    (source / "index.html").write_text("<!doctype html><h1>App</h1>", encoding="utf-8")
    registry = Registry()
    register_site_tools(registry, site_manager)
    contexts = [
        (current_owner_account_id, current_owner_account_id.set("owner-1")),
        (current_workspace_id, current_workspace_id.set("ws-1")),
        (current_session_id, current_session_id.set("session-1")),
        (current_agent_workdir, current_agent_workdir.set(str(tmp_path))),
    ]
    try:
        result = await registry.execute(ToolCall("publish-app", "publish_site", {
            "source_path": "published-app", "name": "我的 App", "description": "日常使用的小工具",
        }))
        payload = json.loads(result.content)
        assert payload["surface"] == {
            "kind": "inspiration", "mode": "site",
            "inspirationId": payload["site_id"], "siteId": payload["site_id"],
            "sessionId": "session-1", "title": "我的 App", "status": "ready",
            "revisionId": payload["release_id"],
        }
        assert site_manager.list_inspirations("owner-1")[0]["description"] == "日常使用的小工具"
    finally:
        for context, token in reversed(contexts):
            context.reset(token)


@pytest.mark.parametrize("config", [
    {"executor": "builtin", "inspiration_creation": True},
    {"executor": "builtin", "site_creation": True},
])
def test_inspiration_creation_session_has_persistent_label(config: dict) -> None:
    class Store:
        @staticmethod
        def get_agent_config(session_id: str, owner_account_id: str = "") -> dict:
            return config

    class Crew:
        session_store = Store()
        external_agents = None

    assert session_agent_label(Crew(), "session-1", owner_account_id="owner") == {
        "name": "灵感", "provider": "sites", "display_badge": "◇",
    }


@pytest.mark.asyncio
async def test_blueprint_http_automation_delivers_and_preserves_last_success(
    site_manager: SiteManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = site_manager.blueprint
    widget_root = tmp_path / "ticker-widget"
    widget_root.mkdir()
    (widget_root / "index.html").write_text(
        "<!doctype html><html><head></head><body><strong data-price></strong>"
        "<script>window.DaimonWidget.onDataChange(d=>document.querySelector('[data-price]').textContent=d.main.price)</script>"
        "</body></html>", encoding="utf-8",
    )
    widget = blueprint.store.create_widget(
        "owner-1", "ws-1", str(widget_root), "自选股", "显示最新价格",
    )
    widget = blueprint.store.update_widget(
        "owner-1", widget["id"],
        slots={"main": {"kind": "json", "schema": {
            "type": "object", "properties": {"price": {"type": "number"}},
            "required": ["price"],
        }}},
    )
    automation = blueprint.store.create_automation(
        "owner-1", "ws-1", "刷新行情", "从公开接口读取股票价格",
        {"kind": "manual"}, {"kind": "none"},
        {"kind": "http_json", "method": "GET", "url": "https://market.example/prices"},
        {"kind": "artifact", "schema": {
            "type": "object", "properties": {"price": {"type": "number"}},
            "required": ["price"],
        }}, False,
    )
    binding = blueprint.store.create_binding(
        "owner-1", automation["id"], widget["id"], "pending_run", [],
    )
    assert blueprint.validate_binding("owner-1", binding["id"])["status"] == "pending_run"

    async def succeed(execution, run_input):
        return {"price": 1309.05}, "GET https://market.example -> 200"

    monkeypatch.setattr(blueprint, "_fetch_json", succeed)
    run = await blueprint.run_automation("owner-1", automation["id"])
    assert run["status"] == "succeeded"
    assert run["deliveryResults"][0]["status"] == "succeeded"
    delivered = blueprint.store.get_widget("owner-1", widget["id"])
    assert delivered["latestData"] == {"main": {"price": 1309.05}}
    assert delivered["status"] == "idle"

    async def fail(execution, run_input):
        raise ValueError("upstream timeout")

    monkeypatch.setattr(blueprint, "_fetch_json", fail)
    failed = await blueprint.run_automation("owner-1", automation["id"])
    assert failed["status"] == "failed"
    retained = blueprint.store.get_widget("owner-1", widget["id"])
    assert retained["latestData"] == {"main": {"price": 1309.05}}
    assert retained["status"] == "error"
    assert "timeout" in retained["error"]

    blueprint.store.update_widget(
        "owner-1", widget["id"],
        slots={"main": {"kind": "json", "schema": {
            "type": "object", "properties": {"volume": {"type": "number"}},
            "required": ["volume"],
        }}},
    )
    monkeypatch.setattr(blueprint, "_fetch_json", succeed)
    incompatible = await blueprint.run_automation("owner-1", automation["id"])
    assert incompatible["status"] == "succeeded"
    assert incompatible["deliveryResults"][0]["status"] == "failed"
    rejected = blueprint.store.get_widget("owner-1", widget["id"])
    assert rejected["latestData"] == {"main": {"price": 1309.05}}
    assert rejected["status"] == "error"
    assert "必填字段" in rejected["error"]


@pytest.mark.asyncio
async def test_blueprint_automation_authorizes_redirect_targets(
    site_manager: SiteManager, monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = site_manager.blueprint
    execution = {"kind": "http_json", "method": "GET", "url": "https://api.example/data"}
    authorized: list[str] = []
    redirected = "https://cdn.example/data"

    async def authorize(url: str) -> None:
        authorized.append(url)

    async def request(_execution, _run_input, allowed):
        if ("cdn.example", 443, "https") not in allowed:
            from crew.security.outbound import PublicRedirectApprovalRequired

            raise PublicRedirectApprovalRequired(redirected)
        return {"ok": True}, "GET https://cdn.example -> 200"

    monkeypatch.setattr(blueprint, "_request_json", request)

    result, _logs = await blueprint._fetch_json_authorized(execution, None, authorize)

    assert result == {"ok": True}
    assert authorized == [execution["url"], redirected]


@pytest.mark.asyncio
async def test_blueprint_post_uses_shared_dns_pinned_transport(
    site_manager: SiteManager, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from crew.security.outbound import PublicHttpResponse

    captured: dict = {}

    def request(url: str, **kwargs):
        captured.update({"url": url, **kwargs})
        return PublicHttpResponse(
            url=url,
            body=b'{"price":1309.05}',
            content_type="application/json",
            charset="utf-8",
            status=200,
        )

    monkeypatch.setattr("crew.sites.blueprint.request_public_http", request)

    # 传输原语直测（DNS 固定的共享出口）；生产路径是 _fetch_json（带逐跳授权，
    # 走 fetch_authorized_url，需要 security_service 装配）。
    artifact, logs = await site_manager.blueprint._request_json(
        {
            "kind": "http_json",
            "method": "POST",
            "url": "https://market.example/query",
            "headers": {"X-View": "summary"},
        },
        {"symbol": "ACE"},
        None,
    )

    assert artifact == {"price": 1309.05}
    assert captured["method"] == "POST"
    assert captured["json_body"] == {"symbol": "ACE"}
    assert captured["allowed_targets"] is None
    assert logs == "POST https://market.example -> 200"


def test_blueprint_canvas_layout_and_widget_runtime(site_manager: SiteManager, tmp_path: Path) -> None:
    blueprint = site_manager.blueprint
    root = tmp_path / "widget"
    root.mkdir()
    (root / "index.html").write_text(
        "<!doctype html><html><head></head><body>ready</body></html>", encoding="utf-8",
    )
    canvas = blueprint.store.create_canvas("owner-1", "ws-1", "session-1", "每日财经", "行情总览")
    widget = blueprint.store.create_widget("owner-1", "ws-1", str(root), "市场脉搏", "市场状态")
    placement = blueprint.store.place_widget(
        "owner-1", canvas["id"], widget["id"], blueprint.normalize_layout({"mode": "grid"}),
    )
    assert placement["layout"] == {"mode": "grid", "x": 0, "y": 0, "w": 5, "h": 8}
    updated = blueprint.store.update_placement(
        "owner-1", placement["mountId"],
        layout=blueprint.normalize_layout({"mode": "grid", "x": 5, "y": 0, "w": 7, "h": 10}),
        z_order=None, view_state={"main": {"tab": "overview"}},
    )
    assert updated["viewState"] == {"main": {"tab": "overview"}}
    runtime = blueprint.runtime_html(widget, updated)
    assert "window.DaimonWidget = host" in runtime
    assert placement["mountId"] in runtime


def test_blueprint_gateway_lists_canvas_and_serves_widget(
    site_manager: SiteManager, tmp_path: Path,
) -> None:
    root = tmp_path / "gateway-widget"
    root.mkdir()
    (root / "index.html").write_text(
        "<!doctype html><html><head></head><body>market</body></html>", encoding="utf-8",
    )
    canvas = site_manager.blueprint.store.create_canvas(
        "owner-1", "ws-1", "session-1", "行情", "公开接口行情",
    )
    widget = site_manager.blueprint.store.create_widget(
        "owner-1", "ws-1", str(root), "价格", "最新价格",
    )
    placement = site_manager.blueprint.store.place_widget(
        "owner-1", canvas["id"], widget["id"],
        {"mode": "grid", "x": 0, "y": 0, "w": 5, "h": 8},
    )
    app = FastAPI()

    @app.middleware("http")
    async def identify(request: Request, call_next):
        request.state.account = AccountContext(owner_account_id="owner-1", is_local=True)
        return await call_next(request)

    app.include_router(create_sites_router(SimpleNamespace(sites=site_manager)))
    client = TestClient(app)
    listed = client.get("/api/sites/canvases")
    assert listed.status_code == 200
    assert listed.json()["canvases"][0]["widgetCount"] == 1
    detail = client.get(f"/api/sites/canvases/{canvas['id']}").json()
    assert detail["canvas"]["placements"][0]["mountId"] == placement["mountId"]
    rendered = client.get(
        f"/api/sites/widgets/{widget['id']}/render?mount_id={placement['mountId']}"
    )
    assert rendered.status_code == 200
    assert "data-ace-widget-runtime" in rendered.text
    unbound = client.post(f"/api/sites/widgets/{widget['id']}/emit", json={"value": None})
    assert unbound.status_code == 409
    assert unbound.json()["code"] == "widget_binding_missing"
    assert widget["id"] in unbound.json()["error"]
    widget_note = client.post(
        f"/api/sites/inspirations/{widget['id']}/annotations",
        json={
            "targetKind": "widget_dom", "widgetId": widget["id"],
            "mountId": placement["mountId"], "selector": "body", "comment": "调整组件",
        },
    )
    assert widget_note.status_code == 200
    assert widget_note.json()["annotation"]["inspirationKind"] == "widget"
    assert widget_note.json()["annotation"]["targetKind"] == "widget_dom"


@pytest.mark.asyncio
async def test_inspiration_gateway_merges_sorts_and_isolates_owners(
    site_manager: SiteManager, tmp_path: Path,
) -> None:
    workspace = tmp_path / "inspiration-workspace"
    source = workspace / "site"
    source.mkdir(parents=True)
    (source / "index.html").write_text("<!doctype html><h1>site</h1>", encoding="utf-8")
    published = await site_manager.publish(
        owner="owner-1", workspace_id="ws-1", session_id="site-session",
        workspace_root=str(workspace), source_path="site", name="网站产物",
    )
    canvas = site_manager.blueprint.store.create_canvas(
        "owner-1", "ws-2", "canvas-session", "工作台", "每日使用的工具",
    )
    site_manager.blueprint.store.create_canvas(
        "owner-2", "ws-private", "private-session", "其他账号", "不可见",
    )

    app = FastAPI()

    @app.middleware("http")
    async def identify(request: Request, call_next):
        request.state.account = AccountContext(owner_account_id="owner-1", is_local=True)
        return await call_next(request)

    app.include_router(create_sites_router(SimpleNamespace(sites=site_manager)))
    client = TestClient(app)
    response = client.get("/api/sites/inspirations")
    assert response.status_code == 200
    items = response.json()["inspirations"]
    assert [item["id"] for item in items] == [canvas["id"], published["site"]["id"]]
    assert [item["kind"] for item in items] == ["canvas", "site"]
    assert all(item["title"] != "其他账号" for item in items)
    assert client.get(f"/api/sites/inspirations/{canvas['id']}").json()["inspiration"]["sessionId"] == "canvas-session"

    annotation = client.post(
        f"/api/sites/inspirations/{canvas['id']}/annotations",
        json={
            "targetKind": "widget_dom", "widgetId": "widget_example",
            "mountId": "mount_example", "route": "/", "selector": "main > h1",
            "elementTag": "h1", "elementText": "旧标题", "comment": "换成新的标题",
        },
    )
    assert annotation.status_code == 200
    saved = annotation.json()["annotation"]
    assert saved["inspirationId"] == canvas["id"]
    assert saved["targetKind"] == "widget_dom"
    assert saved["selector"] == "main > h1"
    canvas_note = client.post(
        f"/api/sites/inspirations/{canvas['id']}/annotations",
        json={"targetKind": "canvas", "selector": ":canvas", "comment": "调整整体布局"},
    )
    assert canvas_note.json()["annotation"]["targetKind"] == "canvas"


@pytest.mark.asyncio
async def test_legacy_site_annotations_migrate_to_unified_store(
    site_manager: SiteManager, tmp_path: Path,
) -> None:
    workspace = tmp_path / "migration-workspace"
    source = workspace / "site"
    source.mkdir(parents=True)
    (source / "index.html").write_text("<!doctype html><h1>legacy</h1>", encoding="utf-8")
    published = await site_manager.publish(
        owner="owner-1", workspace_id="ws-1", session_id="session-1",
        workspace_root=str(workspace), source_path="site", name="Legacy",
    )
    annotation = site_manager.store.create_annotation(
        "owner-1", published["site"]["id"], published["release"]["id"],
        {"selector": "h1", "element_tag": "h1", "element_text": "legacy", "comment": "保留草稿"},
    )
    site_manager.store._writer.execute(lambda conn: conn.execute(
        "DELETE FROM inspiration_annotations WHERE owner_account_id=? AND id=?",
        ("owner-1", annotation["id"]),
    ))

    reopened = SQLiteSiteStore(str(site_manager.store.db_path))
    migrated = reopened.list_inspiration_annotations("owner-1", published["site"]["id"])
    assert len(migrated) == 1
    assert migrated[0]["comment"] == "保留草稿"
    assert migrated[0]["targetKind"] == "site_dom"


def test_canvas_offline_export_and_shared_asset_cleanup(
    site_manager: SiteManager, tmp_path: Path,
) -> None:
    blueprint = site_manager.blueprint
    widget_root = tmp_path / "shared-widget"
    widget_root.mkdir()
    (widget_root / "index.html").write_text(
        "<!doctype html><html><head></head><body><strong id='price'></strong></body></html>",
        encoding="utf-8",
    )
    widget = blueprint.store.create_widget(
        "owner-1", "ws-1", str(widget_root), "行情组件", "最近价格",
    )
    blueprint.store.set_widget_delivery(
        "owner-1", widget["id"], data={"main": {"price": 1309.05}},
        status="idle", run_id="run_snapshot",
    )
    automation = blueprint.store.create_automation(
        "owner-1", "ws-1", "刷新行情", "含敏感运行信息",
        {"kind": "interval", "every": "1h"}, {"kind": "none"},
        {"kind": "http_json", "url": "https://market.example/private"},
        {"kind": "artifact", "schema": {"type": "object"}}, True,
    )
    blueprint.store.create_binding("owner-1", automation["id"], widget["id"], "valid", [])
    first = blueprint.store.create_canvas("owner-1", "ws-1", "session-1", "行情一", "共享组件")
    second = blueprint.store.create_canvas("owner-1", "ws-1", "session-2", "行情二", "共享组件")
    for canvas in (first, second):
        blueprint.store.place_widget(
            "owner-1", canvas["id"], widget["id"],
            {"mode": "grid", "x": 0, "y": 0, "w": 12, "h": 10},
        )

    archive = site_manager.export_inspiration("owner-1", first["id"])
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        assert {"index.html", "manifest.json", f"widgets/{widget['id']}/index.html"}.issubset(names)
        canvas_html = zf.read("index.html").decode("utf-8")
        assert 'class="canvas canvas--single"' in canvas_html
        assert ".canvas--single .widget{width:100%;height:100%" in canvas_html
        joined = "\n".join(zf.read(name).decode("utf-8", errors="ignore") for name in names)
        assert "1309.05" in joined
        assert "market.example" not in joined
        assert str(widget_root) not in joined
        assert "Authorization" not in joined

    site_manager.delete_inspiration("owner-1", first["id"])
    assert blueprint.store.get_widget("owner-1", widget["id"])["id"] == widget["id"]
    assert blueprint.store.get_automation("owner-1", automation["id"])["id"] == automation["id"]
    assert widget_root.is_dir()

    site_manager.delete_inspiration("owner-1", second["id"])
    with pytest.raises(KeyError):
        blueprint.store.get_widget("owner-1", widget["id"])
    with pytest.raises(KeyError):
        blueprint.store.get_automation("owner-1", automation["id"])
    assert widget_root.is_dir()


@pytest.mark.asyncio
async def test_blueprint_rejects_private_network_and_secret_headers(
    site_manager: SiteManager,
) -> None:
    blueprint = site_manager.blueprint
    with pytest.raises(ToolError, match="authorization_unavailable"):
        await blueprint._fetch_json(
            {"url": "https://127.0.0.1/private", "method": "GET"},
            None,
        )
    with pytest.raises(ValueError, match="不保存鉴权"):
        blueprint.validate_automation_contract(
            {"kind": "manual"},
            {"kind": "http_json", "url": "https://market.example", "headers": {"Authorization": "secret"}},
            {"kind": "artifact", "schema": {"type": "object"}},
        )
    with pytest.raises(ValueError, match="查询参数"):
        blueprint.validate_automation_contract(
            {"kind": "manual"},
            {"kind": "http_json", "url": "https://market.example?api_key=secret"},
            {"kind": "artifact", "schema": {"type": "object"}},
        )


def test_blueprint_agent_tools_are_registered_as_one_contract_per_asset(
    site_manager: SiteManager,
) -> None:
    registry = Registry()
    register_blueprint_tools(registry, site_manager)
    assert {"Canvas", "Widget", "Automation", "Binding"}.issubset(registry.names())
    assert registry.toolset_for("Canvas") == "blueprint"
    assert registry.get("Automation").to_schema()["function"]["parameters"]["required"] == ["action"]


@pytest.mark.asyncio
async def test_blueprint_show_emits_surface_and_validate_advances_revision(
    site_manager: SiteManager, tmp_path: Path,
) -> None:
    root = tmp_path / "surface-widget"
    root.mkdir()
    (root / "index.html").write_text(
        "<!doctype html><html><head></head><body>surface</body></html>", encoding="utf-8",
    )
    canvas = site_manager.blueprint.store.create_canvas(
        "owner-1", "ws-1", "session-1", "生成态看板", "在对话右侧预览",
    )
    widget = site_manager.blueprint.store.create_widget(
        "owner-1", "ws-1", str(root), "生成态组件", "生成过程中预览",
    )
    registry = Registry()
    register_blueprint_tools(registry, site_manager)
    contexts = [
        (current_owner_account_id, current_owner_account_id.set("owner-1")),
        (current_workspace_id, current_workspace_id.set("ws-1")),
        (current_session_id, current_session_id.set("session-1")),
        (current_agent_workdir, current_agent_workdir.set(str(tmp_path))),
    ]
    try:
        shown = await registry.execute(ToolCall("show-widget", "Widget", {
            "action": "show", "widgetId": widget["id"],
        }))
        payload = json.loads(shown.content)
        assert payload["surface"] == {
            "kind": "inspiration", "mode": "widget", "sessionId": "session-1",
            "widgetId": widget["id"], "title": "生成态组件",
            "resourceRevision": 0, "status": "ready",
        }
        ui_detail = json.loads(tool_result_detail_for_ui("Widget", shown.content))
        assert ui_detail == {"ok": True, "surface": payload["surface"]}

        validated = await registry.execute(ToolCall("validate-widget", "Widget", {
            "action": "validate", "widgetId": widget["id"],
        }))
        assert json.loads(validated.content)["data"]["widget"]["resourceRevision"] == 1

        canvas_shown = await registry.execute(ToolCall("show-canvas", "Canvas", {
            "action": "show", "canvasId": canvas["id"],
        }))
        assert json.loads(canvas_shown.content)["surface"]["mode"] == "canvas"
    finally:
        for context, token in reversed(contexts):
            context.reset(token)


@pytest.mark.asyncio
async def test_blueprint_scheduler_restores_enabled_interval_automation(
    site_manager: SiteManager,
) -> None:
    blueprint = site_manager.blueprint
    automation = blueprint.store.create_automation(
        "owner-1", "ws-1", "每小时行情", "每小时刷新公开行情接口",
        {"kind": "interval", "every": "1h"}, {"kind": "none"},
        {"kind": "http_json", "method": "GET", "url": "https://market.example/prices"},
        {"kind": "artifact", "schema": {"type": "object"}}, True,
    )
    await blueprint.start()
    try:
        assert blueprint._scheduler is not None
        job_id = f"blueprint:owner-1:{automation['id']}"
        assert blueprint._scheduler.get_job(job_id) is not None
        blueprint.remove_schedule("owner-1", automation["id"])
        assert blueprint._scheduler.get_job(job_id) is None
    finally:
        await blueprint.stop()
