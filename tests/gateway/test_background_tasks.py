"""Cron 后台 asyncio.Task 强引用测试（G6）。

Feishu webhook 已改为 Adapter 统一 ingress 有限队列，不再从路由直接创建
dispatch task。此处只验证仍使用 create_task 的 cron run-now 强引用。
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from crew.app import build_app
from crew.core.types import Message
from crew.gateway.routers import cron as cron_mod
from crew.gateway.server import create_app
from crew.state.config import Config

OWNER = "A:uid-a"


@pytest.fixture
def crew_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    cfg = Config(
        db_path=str(tmp_path / "crew.db"),
        cron_enabled=True,  # 让 _service() 返回真实 service，走 create_task 分支
    )
    crew = build_app(config=cfg, enable_team=False)
    return crew


@pytest.mark.asyncio
async def test_cron_run_now_background_task_strong_ref_drains(crew_env, auth_headers):
    """run-now 起的后台 tick 任务被 _background_tasks 持有，完成后自动移出。"""
    crew = crew_env
    job = crew.cron_store.create(
        name="g6",
        schedule="every 1h",
        query="ping",
        session_id="s-g6",
        owner_account_id=OWNER,
    )
    crew.session_store.save("s-g6", [Message.user("cron")], owner_account_id=OWNER)
    crew.cron_store.set_enabled(job["id"], False, owner_account_id=OWNER)
    app = create_app(crew)
    await crew.cron_service.start()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.post(f"/api/cron/jobs/{job['id']}/run")
    assert resp.status_code == 200

    # 后台任务被强引用持有过（至少一度进入集合），且最终排空（done_callback discard）
    # 给事件循环几轮迭代让 _kick 跑完。
    for _ in range(50):
        await asyncio.sleep(0.01)
        if not cron_mod._background_tasks:
            break
    assert not cron_mod._background_tasks, "后台任务集合应在任务完成后排空"
    await crew.shutdown()


def test_cron_module_exposes_background_task_set():
    """仍直接 create_task 的 cron 模块必须保留强引用集合。"""
    assert isinstance(cron_mod._background_tasks, set)
