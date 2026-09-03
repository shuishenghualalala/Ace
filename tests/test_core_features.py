"""Core Feature 声明化与 required_by_product 产品必需性守卫。"""

from __future__ import annotations

import asyncio

import pytest

from crew.features import (
    FeatureDefinition,
    FeatureRequiredByProductError,
    FeatureRuntime,
    FeatureState,
    run_async_compat,
)


def _recording_definition(feature_id: str, *, required: bool = False) -> tuple[FeatureDefinition, list[str]]:
    calls: list[str] = []

    def install(ctx) -> None:
        calls.append(ctx.definition.feature_id)

    return (
        FeatureDefinition(feature_id, install, required_by_product=required),
        calls,
    )


class TestRunAsyncCompat:
    def test_runs_without_event_loop(self) -> None:
        async def job() -> int:
            return 42

        assert run_async_compat(job()) == 42

    @pytest.mark.asyncio
    async def test_runs_inside_running_loop_via_worker(self) -> None:
        async def job() -> int:
            # worker 线程拥有独立循环，不重入调用方循环
            return asyncio.get_running_loop() is not None

        assert run_async_compat(job()) is True


class TestRequiredByProduct:
    async def test_deactivate_rejects_required_feature(self) -> None:
        runtime = FeatureRuntime()
        definition, calls = _recording_definition("core.echo", required=True)
        await runtime.activate(definition)
        assert calls == ["core.echo"]

        with pytest.raises(FeatureRequiredByProductError, match="core.echo"):
            await runtime.deactivate("core.echo")
        # 拒绝停用后状态不受损
        assert runtime.get("core.echo").state is FeatureState.ACTIVE

    async def test_deactivate_allows_required_feature_with_explicit_override(self) -> None:
        runtime = FeatureRuntime()
        definition, _ = _recording_definition("core.echo", required=True)
        await runtime.activate(definition)

        assert (
            await runtime.deactivate("core.echo", allow_required_by_product=True)
            is True
        )
        assert runtime.get("core.echo").state is FeatureState.DISCOVERED

    async def test_optional_feature_deactivates_without_override(self) -> None:
        runtime = FeatureRuntime()
        definition, _ = _recording_definition("product.echo")
        await runtime.activate(definition)

        assert await runtime.deactivate("product.echo") is True

    async def test_required_feature_visible_in_diagnostic(self) -> None:
        runtime = FeatureRuntime()
        definition, _ = _recording_definition("core.echo", required=True)
        await runtime.activate(definition)

        diagnostics = {d.feature_id: d for d in runtime.startup_audit().features}
        assert diagnostics["core.echo"].required_by_product is True
        assert diagnostics["core.echo"].as_dict()["required_by_product"] is True


class TestBuiltinCoreFeatures:
    def test_build_app_declares_core_features_in_runtime(self) -> None:
        from crew.app import build_app

        app = build_app()
        records = {
            record.definition.feature_id: record
            for record in app.plugins.feature_runtime.records
        }
        # 组合根内置能力全部进入 Feature Runtime 并被审计
        for feature_id in (
            "core.agent-driver",
            "core.subagent-context",
            "core.process-context",
            "core.task-context",
            "host.structured-path-context",
            "gateway.session-context",
        ):
            record = records.get(feature_id)
            assert record is not None, feature_id
            assert record.state is FeatureState.ACTIVE, feature_id
            assert record.definition.required_by_product is True, feature_id
        # product.* 适配器保持可选
        assert records["product.team-driver-adapter"].definition.required_by_product is False
        # 默认 Agent Driver 行为不变
        assert app.execution_drivers.get("agent") is not None
        assert app.execution_drivers.get("agent.default") is not None
