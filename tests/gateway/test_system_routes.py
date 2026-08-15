"""系统监控路由测试：/api/system/metrics 与 /api/system/logs。"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from crew.app import build_app
from crew.core.runctx import current_owner_account_id
from crew.gateway.auth import AccountContext
from crew.gateway.routers.system import create_system_router
from crew.gateway.server import create_app
from crew.state.config import Config
from crew.state.logging import get_logger, setup_logging


@pytest.fixture
def api(tmp_path):
    crew = build_app(
        config=Config(
            db_path=str(tmp_path / "crew.db"),
            cron_enabled=False,
            gateway_admin_accounts=["A:uid-a"],
        ),
        enable_team=False,
    )
    return create_app(crew)


@pytest.mark.asyncio
async def test_system_metrics_allow_local_and_reject_remote(api):
    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        local = await client.get("/api/system/metrics")

    remote_transport = ASGITransport(app=api, client=("10.0.0.1", 12345))
    async with AsyncClient(transport=remote_transport, base_url="http://test") as client:
        remote = await client.get("/api/system/metrics")

    assert local.status_code == 200
    assert remote.status_code == 401
    data = local.json()
    assert isinstance(data["uptime_s"], (int, float)) and data["uptime_s"] >= 0
    assert "disk" in data


@pytest.mark.asyncio
async def test_system_metrics_returns_real_indicators(api, auth_headers):
    """/api/system/metrics 必须返回真实指标（uptime/磁盘/CPU/内存）。"""
    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.get("/api/system/metrics")
    assert resp.status_code == 200
    data = resp.json()
    # 运行时长必须是正数（进程已启动）
    assert isinstance(data["uptime_s"], (int, float)) and data["uptime_s"] >= 0
    # 磁盘信息来自 stdlib，一定存在
    assert "disk" in data
    assert data["disk"]["total_gb"] > 0
    assert 0 <= data["disk"]["percent"] <= 100
    # psutil 已加入依赖，cpu_percent 与 memory 应存在
    assert "cpu_percent" in data
    assert "memory" in data
    assert data["memory"]["total_gb"] > 0


@pytest.mark.asyncio
async def test_system_logs_returns_recent_entries(api, auth_headers):
    """注入一条日志后，/api/system/logs 能查到，且结构正确。"""
    # setup_logging 可能已被其他测试调用过；这里再发一条确保缓冲非空
    setup_logging(level="DEBUG")
    log = get_logger("test.system")
    log.warning("gateway-probe-marker-%d", 42)

    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.get("/api/system/logs", params={"limit": 50})
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data and "total" in data
    assert isinstance(data["items"], list)
    # 刚写的 marker 一定在最新这批里
    messages = [it["message"] for it in data["items"]]
    assert any("gateway-probe-marker-42" in m for m in messages)
    # 每条日志必须有级别字段
    item = data["items"][0]
    assert item["level"] in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


@pytest.mark.asyncio
async def test_system_logs_level_filter(api, auth_headers):
    """level=WARNING 时只返回 WARNING 及以上的条目。"""
    setup_logging(level="DEBUG")
    log = get_logger("test.system.filter")
    log.info("should-be-filtered-out")
    log.error("should-remain-error")

    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.get("/api/system/logs", params={"level": "ERROR"})
    data = resp.json()
    levels = {it["level"] for it in data["items"]}
    assert levels == {"ERROR"}
    assert any("should-remain-error" in it["message"] for it in data["items"])


@pytest.mark.asyncio
async def test_system_logs_keyword_search(api, auth_headers):
    """q 关键词对 message 做子串匹配。"""
    setup_logging(level="DEBUG")
    log = get_logger("test.system.search")
    log.info("unique-needle-token-xyz")

    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.get("/api/system/logs", params={"q": "unique-needle-token-xyz"})
    data = resp.json()
    assert data["total"] >= 1
    assert all("unique-needle-token-xyz" in it["message"] for it in data["items"])


@pytest.mark.asyncio
async def test_system_logs_local_owner_sees_all_and_remote_is_rejected(api, auth_headers):
    setup_logging(level="DEBUG")
    log = get_logger("test.system.owner")
    marker = "causal-owner-route-20260714"

    token_a = current_owner_account_id.set("A:uid-a")
    try:
        log.warning("%s-a", marker)
    finally:
        current_owner_account_id.reset(token_a)
    token_b = current_owner_account_id.set("B:uid-b")
    try:
        log.warning("%s-b", marker)
    finally:
        current_owner_account_id.reset(token_b)
    log.warning("%s-system", marker)

    local_transport = ASGITransport(app=api)
    async with AsyncClient(transport=local_transport, base_url="http://test") as client:
        local_response = await client.get(
            "/api/system/logs",
            params={"q": marker},
            headers=auth_headers,
        )
    remote_transport = ASGITransport(app=api, client=("10.0.0.1", 12345))
    async with AsyncClient(transport=remote_transport, base_url="http://test") as client:
        remote_response = await client.get(
            "/api/system/logs",
            params={"q": marker},
        )

    assert remote_response.status_code == 401
    assert local_response.status_code == 200
    assert {item["message"] for item in local_response.json()["items"]} == {
        f"{marker}-a",
        f"{marker}-b",
        f"{marker}-system",
    }


@pytest.mark.asyncio
async def test_system_logs_admin_can_clear_ring_buffer(api, auth_headers):
    """Admin DELETE /api/system/logs empties the process-local ring buffer."""
    setup_logging(level="DEBUG")
    log = get_logger("test.system.clear")
    marker = "clear-ring-marker-823"
    log.warning(marker)

    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        before = await client.get("/api/system/logs", params={"q": marker})
        cleared = await client.delete("/api/system/logs")
        after = await client.get("/api/system/logs", params={"q": marker})

    assert before.status_code == 200
    assert before.json()["total"] >= 1
    assert cleared.status_code == 200
    assert cleared.json()["ok"] is True
    assert cleared.json()["cleared"] >= 1
    assert after.status_code == 200
    assert after.json()["total"] == 0


@pytest.fixture
def system_auth_app():
    """System router with header-injected non-local account identity."""
    config = SimpleNamespace(gateway_admin_accounts=["A:uid-a"])
    crew = SimpleNamespace(config=config)
    app = FastAPI()

    @app.middleware("http")
    async def authenticated_account(request: Request, call_next):
        request.state.account = AccountContext(
            owner_account_id=request.headers.get("X-Test-Owner", "tenant:user"),
            provider_id="tenant",
            user_id="user",
        )
        return await call_next(request)

    app.include_router(create_system_router(crew))
    return app


@pytest.mark.asyncio
async def test_system_logs_non_admin_cannot_clear(system_auth_app):
    """Non-admin DELETE /api/system/logs is rejected with 403 and no clear."""
    transport = ASGITransport(app=system_auth_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete(
            "/api/system/logs",
            headers={"X-Test-Owner": "tenant:user"},
        )

    assert resp.status_code == 403
    assert resp.json() == {"ok": False, "error": "需要管理员权限"}
