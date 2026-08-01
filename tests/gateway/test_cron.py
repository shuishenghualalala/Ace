"""Gateway cron 路由测试。"""

from __future__ import annotations


from crew.gateway.routers.cron import _compute_cron_stats, create_cron_router
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
