"""Gateway 工具 / 工具集目录路由测试（工具选择器的数据源）。"""

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from crew.gateway.routers import misc


def _client(crew) -> TestClient:  # noqa: ANN001
    app = FastAPI()
    app.include_router(misc.create_misc_router(crew))
    return TestClient(app)


def test_tools_catalog_routes_return_empty_without_registry():
    client = _client(SimpleNamespace())
    assert client.get("/api/toolsets").json() == []
    assert client.get("/api/tools").json() == []


def test_tools_catalog_routes_expose_registry_data():
    class FakeRegistry:
        def toolsets(self):
            return ["builtin", "web"]

        def names(self):
            return ["web_search", "file_read"]

        def toolset_for(self, name):
            return {"web_search": "web", "file_read": "builtin"}.get(name)

        def ui_meta(self, name):
            return {"display_name": "网页搜索"} if name == "web_search" else {}

    client = _client(SimpleNamespace(registry=FakeRegistry()))
    assert client.get("/api/toolsets").json() == ["builtin", "web"]
    assert client.get("/api/tools").json() == [
        {"name": "file_read", "toolset": "builtin", "display_name": ""},
        {"name": "web_search", "toolset": "web", "display_name": "网页搜索"},
    ]
