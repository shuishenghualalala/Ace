"""Context Contributor Registry ordering, lifecycle, and request integration."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from crew.app import build_app
from crew.core.envelope import Envelope, ResponseChunk
from crew.core.types import Message
from crew.features import (
    ContextContribution,
    ContextContributionFailedError,
    ContextContributor,
    ContextContributorRegistry,
    ContextFailurePolicy,
    ContextPhase,
    ExecutionDriver,
    FeatureDefinition,
    FeatureGeneration,
    FeatureScope,
    FeatureState,
    FeatureStopPolicy,
)
from crew.gateway.session_context import SessionContext, SessionSource
from crew.state.config import Config


def _active_scope(feature_id: str, sequence: int = 1) -> FeatureScope:
    scope = FeatureScope(FeatureGeneration(feature_id, sequence))
    scope.activate()
    return scope


# 三条核心通知通路（后台子任务/后台进程/任务恢复回合）迁为 PROMPT contributor 后常驻注册
CORE_PROMPT_CONTRIBUTOR_IDS = {
    "subagent.background.reminder",
    "process.background.reminder",
    "task.resume.reminder",
}


async def test_contributors_run_by_priority_and_merge_deterministically() -> None:
    registry = ContextContributorRegistry()
    calls: list[str] = []

    async def later(_envelope: Envelope) -> ContextContribution:
        calls.append("later")
        return ContextContribution(
            params={"shared": "later", "later": True},
            prompt_parts=("later prompt",),
        )

    async def earlier(_envelope: Envelope) -> ContextContribution:
        calls.append("earlier")
        return ContextContribution(
            params={"shared": "earlier"},
            prompt_parts=("earlier prompt",),
        )

    registry.register(
        _active_scope("later"),
        ContextContributor("feature.later", later, priority=200),
    )
    registry.register(
        _active_scope("earlier"),
        ContextContributor("feature.earlier", earlier, priority=10),
    )

    report = await registry.contribute(
        Envelope.of("hello", session_id="s1"),
        phase=ContextPhase.REQUEST,
    )

    assert calls == ["earlier", "later"]
    assert report.params == {"shared": "later", "later": True}
    assert report.prompt_parts == ("earlier prompt", "later prompt")
    assert report.failures == ()


async def test_degraded_timeout_is_reported_and_fail_closed_aborts() -> None:
    registry = ContextContributorRegistry()

    async def slow(_envelope: Envelope) -> ContextContribution:
        await asyncio.sleep(10)
        return ContextContribution()

    async def broken(_envelope: Envelope) -> ContextContribution:
        raise RuntimeError("broken context")

    registry.register(
        _active_scope("slow"),
        ContextContributor(
            "feature.slow",
            slow,
            timeout_seconds=0.01,
        ),
    )
    broken_scope = _active_scope("broken")
    registry.register(
        broken_scope,
        ContextContributor(
            "feature.broken",
            broken,
            priority=200,
            failure_policy=ContextFailurePolicy.FAIL,
        ),
    )

    with pytest.raises(ContextContributionFailedError) as captured:
        await registry.contribute(Envelope.of("hello", session_id="s1"))

    assert captured.value.contributor_id == "feature.broken"
    assert captured.value.code == "context_contribution_failed"

    await broken_scope.dispose()
    report = await registry.contribute(Envelope.of("hello", session_id="s2"))
    assert len(report.failures) == 1
    assert report.failures[0].contributor_id == "feature.slow"
    assert report.failures[0].timed_out is True


async def test_new_generation_is_hidden_until_activation() -> None:
    registry = ContextContributorRegistry()

    async def old(_envelope: Envelope) -> ContextContribution:
        return ContextContribution(params={"version": "old"})

    async def new(_envelope: Envelope) -> ContextContribution:
        return ContextContribution(params={"version": "new"})

    old_scope = _active_scope("context-feature", 1)
    registry.register(old_scope, ContextContributor("feature.version", old))
    new_scope = FeatureScope(FeatureGeneration("context-feature", 2))
    registry.register(new_scope, ContextContributor("feature.version", new))

    envelope = Envelope.of("hello", session_id="s1")
    assert (await registry.contribute(envelope)).params["version"] == "old"
    new_scope.activate()
    assert (await registry.contribute(envelope)).params["version"] == "new"

    await old_scope.dispose()
    assert (await registry.contribute(envelope)).params["version"] == "new"


async def test_contributor_lease_blocks_generation_drain() -> None:
    registry = ContextContributorRegistry()
    scope = _active_scope("blocking-context")
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking(_envelope: Envelope) -> ContextContribution:
        started.set()
        await release.wait()
        return ContextContribution(params={"done": True})

    registry.register(
        scope,
        ContextContributor("feature.blocking", blocking),
    )
    running = asyncio.create_task(
        registry.contribute(Envelope.of("hello", session_id="s1"))
    )
    await started.wait()

    stopping = asyncio.create_task(
        scope.stop(FeatureStopPolicy.DRAIN, timeout_seconds=1)
    )
    while scope.state is not FeatureState.DRAINING:
        await asyncio.sleep(0)

    assert registry.bindings() == ()
    assert not stopping.done()
    release.set()
    assert (await running).params["done"] is True
    await stopping


async def test_request_cancellation_propagates_and_releases_contributor_lease() -> None:
    registry = ContextContributorRegistry()
    scope = _active_scope("cancelled-context")
    started = asyncio.Event()

    async def blocking(_envelope: Envelope) -> ContextContribution:
        started.set()
        await asyncio.Event().wait()
        return ContextContribution()

    registry.register(
        scope,
        ContextContributor("feature.cancelled", blocking),
    )
    running = asyncio.create_task(
        registry.contribute(Envelope.of("hello", session_id="s1"))
    )
    await started.wait()

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert scope.active_leases == ()


async def test_persistent_messages_require_explicit_contributor_contract() -> None:
    async def attach(_envelope: Envelope) -> ContextContribution:
        message = Message.system_reminder("persistent context")
        message.attachment_type = "feature_context"
        return ContextContribution(messages=(message,))

    invalid_registry = ContextContributorRegistry()
    invalid_registry.register(
        _active_scope("invalid-message-context"),
        ContextContributor("feature.invalid-message", attach),
    )
    invalid = await invalid_registry.contribute(
        Envelope.of("hello", session_id="s-invalid")
    )
    assert invalid.persistent_messages == ()
    assert len(invalid.failures) == 1
    assert "persistent contributor" in invalid.failures[0].message

    registry = ContextContributorRegistry()
    registry.register(
        _active_scope("message-context"),
        ContextContributor(
            "feature.message",
            attach,
            persistent=True,
        ),
    )
    report = await registry.contribute(Envelope.of("hello", session_id="s-valid"))
    assert len(report.persistent_messages) == 1
    assert report.persistent_messages[0].attachment_type == "feature_context"


@pytest.mark.asyncio
async def test_app_uses_host_and_browser_owned_context_contributors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    app = build_app(
        Config(
            db_path=str(tmp_path / "crew.db"),
            memory_db_path=str(tmp_path / "memory.db"),
            cron_enabled=False,
            api_key="",
        ),
        enable_team=False,
    )
    # browser 插件 activation_phase=startup（c147420 迁入宿主事件循环生命周期），
    # 其 ContextContributor 与 browser_manager 都要走 startup 才会注册。
    await app.startup(start_cron=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    referenced = workspace / "notes.md"
    referenced.write_text("notes", encoding="utf-8")
    app.workspace_store.get("default", owner_account_id="owner-a")
    app.workspace_store.update(
        "default",
        owner_account_id="owner-a",
        root_path=str(workspace),
    )

    async def read_tab_content(
        owner_account_id: str,
        session_id: str,
        tab_id: str,
        *,
        max_chars: int,
    ) -> dict[str, str]:
        assert owner_account_id == "owner-a"
        assert session_id == "s1"
        assert tab_id == "tab-a"
        assert max_chars == 4000
        return {
            "title": "Docs",
            "url": "https://example.test/docs",
            "text": "browser body",
        }

    monkeypatch.setattr(app.browser_manager, "read_tab_content", read_tab_content)
    seen: list[Envelope] = []

    async def inspect_context(envelope: Envelope):
        seen.append(envelope)
        yield ResponseChunk.final(envelope.request_id, "ok")

    feature_id = "context-inspector"
    await app.plugins.feature_runtime.activate(
        FeatureDefinition(
            feature_id,
            lambda context: context.register_execution_driver(
                ExecutionDriver("test.context", inspect_context)
            ),
        )
    )
    try:
        contributor_ids = {
            binding.contributor.contributor_id
            for binding in app.context_contributors.bindings(ContextPhase.REQUEST)
        }
        assert "host.reference.structured-path" in contributor_ids
        assert "browser.reference.tab" in contributor_ids
        assert "gateway.session.source" in contributor_ids
        assert {
            binding.contributor.contributor_id
            for binding in app.context_contributors.bindings(ContextPhase.PROMPT)
        } == {"wiki.agent.context"} | CORE_PROMPT_CONTRIBUTOR_IDS

        envelope = Envelope.of(
            "inspect @file:notes.md @browser_tab:tab-a",
            session_id="s1",
            user_id="owner-a",
            mode="test.context",
            params={
                "workspace_root_path": str(workspace),
                "session_context": SessionContext(
                    source=SessionSource(
                        platform="web",
                        chat_id="chat-a",
                        user_id="owner-a",
                        user_name="AHUAMAO",
                    ),
                    connected_platforms=["local", "web"],
                    session_id="s1",
                ),
            },
        )
        chunks = [chunk async for chunk in app.handle(envelope)]

        assert chunks[-1].body["text"] == "ok"
        assert seen == [envelope]
        assert envelope.params["referenced_paths"] == [
            {"path": str(referenced.resolve()), "resource_type": "file"}
        ]
        assert envelope.params["browser_tab_references"][0]["tab_id"] == "tab-a"
        assert "browser body" in envelope.params["_context_prompt_parts"][0]
        assert "当前会话上下文" in envelope.params["_context_prompt_parts"][1]
        assert envelope.params["session_source"] == {
            "platform": "web",
            "chat_id": "chat-a",
            "chat_name": None,
            "chat_type": "dm",
            "user_id": "owner-a",
            "user_name": "AHUAMAO",
            "thread_id": None,
            "guild_id": None,
            "message_id": None,
        }

        assert await app.plugins.unload_plugin_async("browser")
        remaining_ids = {
            binding.contributor.contributor_id
            for binding in app.context_contributors.bindings(ContextPhase.REQUEST)
        }
        assert "browser.reference.tab" not in remaining_ids
        assert "host.reference.structured-path" in remaining_ids
        assert "gateway.session.source" in remaining_ids
        assert {
            binding.contributor.contributor_id
            for binding in app.context_contributors.bindings(ContextPhase.PROMPT)
        } == {"wiki.agent.context"} | CORE_PROMPT_CONTRIBUTOR_IDS
    finally:
        await app.plugins.feature_runtime.deactivate(feature_id)
        await app.shutdown()


@pytest.mark.asyncio
async def test_cron_context_contributor_follows_feature_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    app = build_app(
        Config(
            db_path=str(tmp_path / "crew.db"),
            memory_db_path=str(tmp_path / "memory.db"),
            cron_enabled=True,
            api_key="",
        ),
        enable_team=False,
    )
    try:
        await app.startup()
        prompt_ids = {
            binding.contributor.contributor_id
            for binding in app.context_contributors.bindings(ContextPhase.PROMPT)
        }
        assert prompt_ids == {
            "cron.trigger.reminder",
            "wiki.agent.context",
        } | CORE_PROMPT_CONTRIBUTOR_IDS

        cron_report = await app.context_contributors.contribute(
            Envelope.of(
                "生成日报",
                session_id="cron-session",
                channel="cron",
                params={"cron_job_name": "每日简报"},
            ),
            phase=ContextPhase.PROMPT,
        )
        assert len(cron_report.prompt_parts) == 1
        assert "每日简报" in cron_report.prompt_parts[0]

        web_report = await app.context_contributors.contribute(
            Envelope.of("生成日报", session_id="web-session", channel="web"),
            phase=ContextPhase.PROMPT,
        )
        assert web_report.prompt_parts == ()
    finally:
        await app.shutdown()

    remaining_ids = {
        binding.contributor.contributor_id
        for binding in app.context_contributors.bindings(ContextPhase.PROMPT)
    }
    assert "cron.trigger.reminder" not in remaining_ids


@pytest.mark.asyncio
async def test_notification_contributors_registered_as_core(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """三条通知通路是核心能力：常驻注册（不挂 feature flag）、瞬时、DEGRADE。"""
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    app = build_app(
        Config(
            db_path=str(tmp_path / "crew.db"),
            memory_db_path=str(tmp_path / "memory.db"),
            cron_enabled=False,
            api_key="",
        ),
        enable_team=False,
    )
    try:
        bindings = {
            binding.contributor.contributor_id: binding.contributor
            for binding in app.context_contributors.bindings(ContextPhase.PROMPT)
        }
        for contributor_id in CORE_PROMPT_CONTRIBUTOR_IDS:
            contributor = bindings[contributor_id]
            assert contributor.phase is ContextPhase.PROMPT
            assert contributor.persistent is False
            assert contributor.model_visible is True
            assert contributor.failure_policy is ContextFailurePolicy.DEGRADE
    finally:
        await app.shutdown()


async def test_subagent_notification_contributor_drains_once_and_respects_collect() -> None:
    """子任务通知 contributor：collect 摘除不重复注入，drain 每会话恰好一次。"""
    from crew.agent.subagent.context import (
        SubagentNotificationQueue,
        build_subagent_notification_handler,
    )

    queue = SubagentNotificationQueue()
    handler = build_subagent_notification_handler(queue)
    queue.enqueue("s1", {
        "task_id": "t1", "agent": "Alpha", "status": "completed",
        "summary": "a", "owner_account_id": "local",
    })
    queue.enqueue("s1", {
        "task_id": "t2", "agent": "Beta", "status": "completed",
        "summary": "b", "owner_account_id": "local",
    })

    # 模型主动 collect 取走 t1 → 从待注入队列摘除
    queue.remove("s1", "t1", "local")

    env = Envelope.of("继续", session_id="s1", user_id="local")
    result = await handler(env)
    assert result is not None
    assert len(result.prompt_parts) == 1
    text = result.prompt_parts[0]
    assert text.startswith("# 后台子任务完成通知")
    assert "[Beta]" in text and "[Alpha]" not in text

    # 排空后不重复注入
    assert await handler(Envelope.of("继续", session_id="s1", user_id="local")) is None


async def test_subagent_notification_contributor_member_session_key() -> None:
    """team member 回合：drain 键取 member_session_id，与入队键（member 子会话）对齐。"""
    from crew.agent.subagent.context import (
        SubagentNotificationQueue,
        build_subagent_notification_handler,
    )

    queue = SubagentNotificationQueue()
    handler = build_subagent_notification_handler(queue)
    queue.enqueue("team-s1::coder", {
        "task_id": "t1", "agent": "Explore", "status": "completed",
        "summary": "done", "owner_account_id": "local",
    })

    # member 回合 envelope 的 session_id 可能含 turn 段，drain 键必须是 member_session_id
    env = Envelope.of(
        "继续",
        session_id="team-s1::turn::req-1::coder",
        user_id="local",
        params={"member_session_id": "team-s1::coder"},
    )
    result = await handler(env)
    assert result is not None and "后台子任务完成通知" in result.prompt_parts[0]
    assert queue.drain("team-s1::coder", "local") == []


async def test_subagent_notification_contributor_skips_preview() -> None:
    """上下文预览只读：不消费待注入队列。"""
    from crew.agent.subagent.context import (
        SubagentNotificationQueue,
        build_subagent_notification_handler,
    )

    queue = SubagentNotificationQueue()
    handler = build_subagent_notification_handler(queue)
    queue.enqueue("s1", {
        "task_id": "t1", "agent": "Explore", "status": "completed",
        "summary": "done", "owner_account_id": "local",
    })

    preview = Envelope.of("", session_id="s1", user_id="local", params={"_context_preview": True})
    assert await handler(preview) is None
    # 队列未被预览消费，正式回合仍能注入
    assert await handler(Envelope.of("继续", session_id="s1", user_id="local")) is not None


async def test_process_notification_contributor_drains_once() -> None:
    """进程通知 contributor：复用 format_process_notification，drain 每会话恰好一次。"""
    from crew.tools.process_registry import (
        ProcessRegistry,
        build_process_notification_handler,
    )

    registry = ProcessRegistry()
    handler = build_process_notification_handler(registry)
    registry._pending[("local", "s1")] = [{
        "type": "completion", "session_id": "proc_1", "command": "echo hi",
        "exit_code": 0, "output": "hi",
    }]

    result = await handler(Envelope.of("继续", session_id="s1", user_id="local"))
    assert result is not None
    text = result.prompt_parts[0]
    assert text.startswith("# 后台进程通知")
    assert "proc_1" in text and "退出码 0" in text

    assert await handler(Envelope.of("继续", session_id="s1", user_id="local")) is None


async def test_process_notification_contributor_team_session_key() -> None:
    """team 回合：drain 键取 team_session_id（与 spawn 时 current_session_id 一致）。"""
    from crew.tools.process_registry import (
        ProcessRegistry,
        build_process_notification_handler,
    )

    registry = ProcessRegistry()
    handler = build_process_notification_handler(registry)
    registry._pending[("local", "team-s1")] = [{
        "type": "completion", "session_id": "proc_2", "command": "echo hi",
        "exit_code": 0, "output": "hi",
    }]

    env = Envelope.of(
        "继续",
        session_id="team-s1::leader",
        user_id="local",
        params={"team_session_id": "team-s1"},
    )
    result = await handler(env)
    assert result is not None and "proc_2" in result.prompt_parts[0]


async def test_task_notification_contributor_formats_resume_turn() -> None:
    """任务恢复回合 contributor：读 envelope.params 的 task_notifications 产出 reminder。"""
    from crew.tasks.context import contribute_task_notifications

    task = {
        "task_id": "t-shell-1", "kind": "shell", "status": "completed",
        "result": "构建成功",
    }
    env = Envelope.of(
        "后台任务已完成，请根据结果继续原任务。",
        session_id="s1",
        channel="task",
        params={"internal_task_resume": True, "task_notifications": [task]},
    )
    result = await contribute_task_notifications(env)
    assert result is not None
    text = result.prompt_parts[0]
    assert text.startswith("# 后台任务完成通知")
    assert "## t-shell-1 kind=shell status=completed" in text
    assert "构建成功" in text

    # 无通知的回合不产生任何输出
    assert await contribute_task_notifications(Envelope.of("你好", session_id="s1")) is None
