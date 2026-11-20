"""Gateway cron 路由测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from crew.app import build_app
from crew.cron import CronJobStore, CronService
from crew.gateway.auth import AccountContext
from crew.gateway.routers.cron import _compute_cron_stats, create_cron_router
from crew.gateway.server import create_app
from crew.state.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _DummyCrew:
    def __init__(self):
        self.config = _DummyConfig()


class _DummyConfig:
    def channel_config(self, name: str) -> dict:
        return {}


def test_cron_stats_counts_cron_kind_as_interval():
    """kind=cron 的固定时点任务应被计入周期任务，而不是一次性任务。"""
    jobs = [
        {"kind": "interval", "enabled": True, "last_status": "", "last_run_at": 0, "next_run_at": 0},
        {"kind": "cron", "enabled": True, "last_status": "", "last_run_at": 0, "next_run_at": 0},
        {"kind": "once", "enabled": True, "last_status": "", "last_run_at": 0, "next_run_at": 0},
    ]
    stats = _compute_cron_stats(jobs, 100.0)
    assert stats["interval"] == 2
    assert stats["once"] == 1
    assert stats["total"] == 3


def test_cron_delivery_targets_returns_local_defaults():
    crew = _DummyCrew()
    router = create_cron_router(crew)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.get("/api/cron/delivery-targets")
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    ids = [t["id"] for t in data["targets"]]
    assert "new_session" in ids
    assert "local" in ids


@pytest.mark.asyncio
async def test_create_job_with_draft_session_creates_placeholder(tmp_path):
    """前端「新会话」是草稿态(未发消息、后端无记录),创建 cron 不应报「会话不存在」。"""
    crew = build_app(
        config=Config(
            db_path=str(tmp_path / "crew.db"),
            cron_db_path=str(tmp_path / "cron.db"),
            cron_enabled=False,
        ),
        enable_team=False,
    )
    app = create_app(crew)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/cron/jobs", json={
            "name": "draft cron",
            "schedule": "every 1h",
            "query": "ping",
            "session_id": "web_draft_abc123",
        })
        assert resp.status_code == 201
        assert crew.session_store.session_belongs_to("web_draft_abc123", "local")

        jobs = (await client.get("/api/cron/jobs")).json()["jobs"]
        assert [j["name"] for j in jobs] == ["draft cron"]


@pytest.mark.asyncio
async def test_manual_run_waits_for_mounted_owner_before_reporting_acceptance(tmp_path, monkeypatch):
    store = CronJobStore(str(tmp_path / "cron.db"))
    prepared = asyncio.Event()
    release = asyncio.Event()
    ran = asyncio.Event()
    prepare = store.prepare_owner_jobs_for_mount_async

    async def suspend_before_projection(owner, **kwargs):
        rows = await prepare(owner, **kwargs)
        prepared.set()
        await release.wait()
        return rows

    async def runner(_envelope):
        ran.set()

    monkeypatch.setattr(store, "prepare_owner_jobs_for_mount_async", suspend_before_projection)
    service = CronService(store, runner)
    app = FastAPI()

    @app.middleware("http")
    async def _trusted_local(request, call_next):
        request.state.account = AccountContext(owner_account_id="local", is_local=True)
        return await call_next(request)

    app.include_router(create_cron_router(SimpleNamespace(cron_store=store, cron_service=service)))
    job = store.create(name="manual", schedule="every 1h", query="go", session_id="s", owner_account_id="local")
    path_fragment = f"/api/cron/jobs/{job['id']}/run"

    try:
        await service.start()
        service.request_owner_mount("local")
        await asyncio.wait_for(prepared.wait(), timeout=5)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(path_fragment)
            assert response.status_code == 409
            assert store.get_job_runs(job["id"]) == []
            release.set()
            await service.mount_owner_async("local")
            response = await client.post(path_fragment)
            assert response.status_code == 200 and response.json()["ok"]
            await asyncio.wait_for(ran.wait(), timeout=5)
    finally:
        release.set()
        await service.stop()
        store.close()
