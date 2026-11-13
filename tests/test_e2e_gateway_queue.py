# ruff: noqa: E402 -- e2e test configures environment/path before application imports
"""端到端：真实 LLM 下验证 gateway 排队语义与状态口径（「假排队」修复回归）。

契约：
1. 全局并发槽=1 时，等待槽位的会话对外 live=running（已受理）、queue_depth=0，
   不再误报 queued；等槽状态经 waiting_for_global_slot 单独暴露。
2. 同会话执行中再发消息：真实排队（queue_depth=1，收到"排队中"status 帧），
   当前回合结束后串行执行，两条消息都拿到 final。

运行：pytest -m e2e tests/test_e2e_gateway_queue.py（需 CREW_MODEL_API_KEY）
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crew.app import build_app
from crew.core.envelope import Envelope, ResponseChunk
from crew.state.config import Config, load_config

PROMPT = "请直接回复「收到」两个字，不要调用任何工具。"


def _build_config(tmp_path: Path, max_active_runs: int) -> Config:
    """基于 config.yaml 创建临时配置，避免污染真实数据库与 crew_home。"""
    cfg = load_config()
    cfg.db_path = str(tmp_path / "crew.db")
    cfg.memory_db_path = str(tmp_path / "memory.db")
    cfg.crew_home = str(tmp_path / ".crew")
    cfg.log_file = str(tmp_path / ".crew" / "logs" / "crew.log")
    cfg.gateway_max_active_runs = max_active_runs
    # 控制成本：本用例只需要一次模型回复
    cfg.max_iterations = min(cfg.max_iterations, 10)
    return cfg


def _env(session_id: str) -> Envelope:
    return Envelope.of(PROMPT, session_id=session_id, channel="web", user_id="local")


async def _drain(dispatcher, envelope: Envelope) -> list[ResponseChunk]:
    return [chunk async for chunk in dispatcher.run(envelope)]


@pytest.fixture
async def app(tmp_path_factory):
    """每个测试函数使用独立的临时目录与配置。"""
    tmp = tmp_path_factory.mktemp("gateway_queue_e2e")
    cfg = _build_config(tmp, max_active_runs=1)
    _app = build_app(config=cfg, enable_team=False)
    if not _app.config.has_llm_key:
        pytest.skip(f"模型 {_app.config.active_model_id} 未配置 API Key，跳过真实 LLM E2E")
    yield _app


async def test_global_slot_wait_reports_running_not_queued(app):
    """等全局并发槽的当前消息：live=running 且不计入排队深度。"""
    disp = app.dispatcher
    t1 = asyncio.create_task(_drain(disp, _env("e2e-slot-a")))
    # 等 a 真正占住全局槽
    for _ in range(100):
        await asyncio.sleep(0.05)
        if disp.status("e2e-slot-a", owner_account_id="local")["global_active"] >= 1:
            break
    else:
        pytest.fail("session a 未在预期时间内开始运行")

    t2 = asyncio.create_task(_drain(disp, _env("e2e-slot-b")))
    await asyncio.sleep(0.2)
    st = disp.status("e2e-slot-b", owner_account_id="local")
    assert st["live"] == "running", f"等全局槽的会话被误报为 {st['live']}"
    assert st["queue_depth"] == 0
    assert st["waiting_for_global_slot"] == 1

    out_a, out_b = await asyncio.gather(t1, t2)
    assert out_a[-1].kind == "final"
    assert out_b[-1].kind == "final"


async def test_same_session_message_queues_and_runs_after(app):
    """同会话执行中再发消息：排队 → 当前回合结束后串行执行。"""
    disp = app.dispatcher
    t1 = asyncio.create_task(_drain(disp, _env("e2e-queue")))
    for _ in range(100):
        await asyncio.sleep(0.05)
        if disp.status("e2e-queue", owner_account_id="local")["live"] == "running":
            break
    else:
        pytest.fail("首条消息未在预期时间内开始运行")

    t2 = asyncio.create_task(_drain(disp, _env("e2e-queue")))
    await asyncio.sleep(0.2)
    st = disp.status("e2e-queue", owner_account_id="local")
    assert st["live"] == "running"  # 会话仍在跑第一条
    assert st["queue_depth"] == 1  # 第二条真实排队

    out1, out2 = await asyncio.gather(t1, t2)
    assert out1[-1].kind == "final"
    assert out2[-1].kind == "final"
    assert any(
        c.kind == "status" and "排队" in str(c.body.get("message", "")) for c in out2
    )
