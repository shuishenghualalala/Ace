"""4C-3 CrewApp Team/Kanban 中心接线收口：compat 属性与 Service Registry 分派。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from crew.app import build_app
from crew.core.mocks import FakeProvider
from crew.dynamickanban.feature import DYNAMIC_KANBAN_FEATURE_ID, DYNAMIC_KANBAN_SERVICE_KEY
from crew.features import FeatureState
from crew.state.config import Config
from crew.team.feature import TEAM_FEATURE_ID, TEAM_SERVICE_KEY


def _config(tmp_path: Any, api_key: str = "sk-test") -> Config:
    return Config(
        db_path=str(tmp_path / "app.db"),
        memory_db_path=str(tmp_path / "memory.db"),
        api_key=api_key,
        cron_enabled=False,
    )


@pytest.mark.asyncio
async def test_build_app_team_and_kanban_resolve_via_service_registry(tmp_path):
    """真实 build_app 后，team/dynamic_kanban 经 Service Registry 解析同一 Generation。"""
    app = build_app(_config(tmp_path), enable_team=True)
    try:
        runtime = app.plugins.feature_runtime

        team_record = runtime.get(TEAM_FEATURE_ID)
        assert team_record is not None
        assert team_record.state is FeatureState.ACTIVE
        team_service = runtime.services.get(TEAM_SERVICE_KEY)
        assert team_service is app.team

        dk_record = runtime.get(DYNAMIC_KANBAN_FEATURE_ID)
        assert dk_record is not None
        assert dk_record.state is FeatureState.ACTIVE
        dk_service = runtime.services.get(DYNAMIC_KANBAN_SERVICE_KEY)
        assert dk_service.manager is app.dynamic_kanban
        assert dk_service.consumer is app.dynamic_kanban_consumer
    finally:
        await app.shutdown()


@pytest.mark.asyncio
async def test_team_and_kanban_fail_closed_after_deactivation(tmp_path):
    """Feature 停用后，app.team / app.dynamic_kanban 返回 None，不再持有旧 Manager。"""
    app = build_app(_config(tmp_path), enable_team=True)
    runtime = app.plugins.feature_runtime
    try:
        assert app.team is not None
        assert app.dynamic_kanban is not None

        assert await runtime.deactivate(TEAM_FEATURE_ID) is True
        assert app.team is None
        # Team 停用不影响 DK。
        assert app.dynamic_kanban is not None

        assert await runtime.deactivate(DYNAMIC_KANBAN_FEATURE_ID) is True
        assert app.dynamic_kanban is None
        assert app.dynamic_kanban_consumer is None
    finally:
        await app.shutdown()


@pytest.mark.asyncio
async def test_app_steer_dispatches_to_active_team_and_kanban_generations(tmp_path):
    """steer 按 agent -> team -> kanban 顺序经 active Generation 分派。"""
    app = build_app(_config(tmp_path), enable_team=True)
    runtime = app.plugins.feature_runtime
    try:
        team_manager = runtime.services.get(TEAM_SERVICE_KEY)
        dk_service = runtime.services.get(DYNAMIC_KANBAN_SERVICE_KEY)
        dk_manager = dk_service.manager

        team_calls = []
        dk_calls = []

        team_manager.steer = lambda session_id, text, owner_account_id="": (
            team_calls.append((session_id, text, owner_account_id)) or True
        )
        dk_manager.steer = lambda session_id, text, owner_account_id="": (
            dk_calls.append((session_id, text, owner_account_id)) or True
        )

        # 无运行中 Agent，Team 返回 True；不再继续到 DK。
        assert app.steer("s1", "继续", owner_account_id="acct-1") is True
        assert team_calls == [("s1", "继续", "acct-1")]
        assert dk_calls == []

        # Team 抛异常后落到 DK。
        team_calls.clear()
        dk_calls.clear()
        team_manager.steer = lambda session_id, text, owner_account_id="": (
            team_calls.append((session_id, text, owner_account_id)) or (_ for _ in ()).throw(RuntimeError("boom"))
        )
        assert app.steer("s2", "下一步", owner_account_id="acct-2") is True
        assert team_calls == [("s2", "下一步", "acct-2")]
        assert dk_calls == [("s2", "下一步", "acct-2")]
    finally:
        await app.shutdown()


@pytest.mark.asyncio
async def test_app_interrupt_aggregates_team_and_kanban_generations(tmp_path):
    """interrupt 聚合 agent/team/kanban/subagent 结果；DK 的 owner scope 必须透传。"""
    app = build_app(_config(tmp_path), enable_team=True)
    runtime = app.plugins.feature_runtime
    try:
        team_manager = runtime.services.get(TEAM_SERVICE_KEY)
        dk_service = runtime.services.get(DYNAMIC_KANBAN_SERVICE_KEY)
        dk_manager = dk_service.manager

        team_manager.interrupt = lambda session_id, message=None, owner_account_id="": True
        dk_calls = []
        dk_manager.interrupt = lambda session_id, message=None, owner_account_id="": (
            dk_calls.append((session_id, message, owner_account_id)) or True
        )

        assert app.interrupt("s1", "停止", owner_account_id="acct-1") is True
        assert dk_calls == [("s1", "停止", "acct-1")]
    finally:
        await app.shutdown()


@pytest.mark.asyncio
async def test_app_interrupt_fail_closed_when_kanban_deactivated(tmp_path):
    """DK 停用后 interrupt 安静降级，不抛异常、不混入 True。"""
    app = build_app(_config(tmp_path), enable_team=False)
    runtime = app.plugins.feature_runtime
    try:
        assert app.dynamic_kanban is not None
        await runtime.deactivate(DYNAMIC_KANBAN_FEATURE_ID)
        assert app.dynamic_kanban is None
        # 没有 subagent_active，也没有 agent/team/dk，应返回 False。
        assert app.interrupt("s1", "停止", owner_account_id="acct-1") is False
    finally:
        await app.shutdown()


@pytest.mark.asyncio
async def test_app_consumer_tasks_snapshot_uses_active_team_generation(tmp_path):
    """_consumer_tasks_snapshot 只抓取当前 active Team Generation 的任务。"""
    app = build_app(_config(tmp_path), enable_team=True)
    runtime = app.plugins.feature_runtime
    try:
        team_manager = runtime.services.get(TEAM_SERVICE_KEY)

        fake_task = asyncio.create_task(asyncio.sleep(10))
        team_manager.active_tasks_snapshot = lambda: {fake_task}

        snapshot = app._consumer_tasks_snapshot()
        assert fake_task in snapshot

        # 停用 Team 后不应再抓取到旧 Generation 的任务。
        await runtime.deactivate(TEAM_FEATURE_ID)
        team_manager.active_tasks_snapshot = lambda: {fake_task}
        snapshot_after = app._consumer_tasks_snapshot()
        assert fake_task not in snapshot_after

        fake_task.cancel()
        try:
            await fake_task
        except asyncio.CancelledError:
            pass
    finally:
        await app.shutdown()


@pytest.mark.asyncio
async def test_app_provider_sync_reaches_active_team_and_kanban_generations(tmp_path):
    """切换默认模型时，新 Provider 同步到 active Team/DK Generation。"""
    app = build_app(_config(tmp_path), enable_team=True)
    runtime = app.plugins.feature_runtime
    try:
        team_manager = runtime.services.get(TEAM_SERVICE_KEY)
        dk_service = runtime.services.get(DYNAMIC_KANBAN_SERVICE_KEY)
        dk_manager = dk_service.manager

        team_set = []
        dk_set = []

        original_team_set = team_manager.set_provider
        original_dk_set = dk_manager.set_provider

        def _team_set_provider(provider):
            team_set.append(provider)
            original_team_set(provider)

        def _dk_set_provider(provider):
            dk_set.append(provider)
            original_dk_set(provider)

        team_manager.set_provider = _team_set_provider
        dk_manager.set_provider = _dk_set_provider

        new_provider = FakeProvider()
        app.provider = new_provider
        app._sync_default_provider_to_features()

        assert team_set
        assert team_set[-1] is new_provider
        assert dk_set
        assert dk_set[-1] is new_provider
    finally:
        await app.shutdown()
