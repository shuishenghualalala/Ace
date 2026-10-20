"""Cron Product Feature 的声明式装配与 Generation 所有权。"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Protocol

from crew.core.envelope import Envelope
from crew.cron.context import contribute_cron_trigger_reminder
from crew.cron.jobs import CronJobStore
from crew.cron.scheduler import CronService
from crew.cron.tools import EXTERNAL_ORIGIN_PLATFORMS, build_cron_tools
from crew.features import (
    ContextContributor,
    ContextPhase,
    FeatureDefinition,
    FeatureInstallContext,
    FeatureStopPolicy,
    FeatureUpdateStrategy,
    RegistrationPhase,
)
from crew.features.session_context import SessionContext, SessionSource
from crew.state.logging import get_logger
from crew.tools.registry import FunctionTool, Registry

log = get_logger("cron.feature")

CRON_FEATURE_ID = "product.cron"


class CronFeatureHost(Protocol):
    """Cron Bundle 使用的最小宿主能力，避免依赖 CrewApp 具体类型。"""

    cron_service: CronService | None
    session_store: Any
    active_owner: Any
    delivery_router: Any
    _notify_owner_fn: Any
    _push_fn: Any

    def dispatch(self, envelope: Envelope) -> Any: ...

    def _on_cron_run_finished(self, info: dict[str, Any]) -> Any: ...


@dataclass(frozen=True, slots=True)
class CronFeatureBundle:
    """一次不可变 Cron 配置对应的定义和候选 Service。"""

    definition: FeatureDefinition
    service: CronService | None


def _origin_source(envelope: Envelope) -> SessionSource | None:
    raw = envelope.params.get("cron_origin_source")
    if not isinstance(raw, dict) or not raw:
        return None
    try:
        return SessionSource.from_dict(raw)
    except Exception:  # noqa: BLE001 - 持久化旧数据不应阻断 Cron Fire
        log.warning(
            "cron origin_source 无法解析 session=%s raw=%s",
            envelope.session_id,
            raw,
        )
        return None


async def _notify_session(
    host: CronFeatureHost,
    kind: str,
    envelope: Envelope,
    session_id: str,
) -> None:
    """通知当前 Owner：Cron Fire 创建或更新了投递会话。"""

    if host._notify_owner_fn is None:
        return
    try:
        await host._notify_owner_fn(
            envelope.user_id,
            {
                "kind": kind,
                "body": {
                    "job_id": str(envelope.params.get("cron_job_id") or ""),
                    "job_name": str(envelope.params.get("cron_job_name") or "").strip(),
                    "source_session_id": str(
                        envelope.params.get("cron_source_session_id") or ""
                    ),
                },
                "session_id": session_id,
                "is_final": True,
                "sequence": 0,
            },
        )
    except Exception:  # noqa: BLE001 - 通知失败不改变 Fire 业务终态
        log.debug("cron 广播会话事件失败 kind=%s session=%s", kind, session_id)


def _build_runner(host: CronFeatureHost):
    async def run(envelope: Envelope) -> None:
        final_text, error = "", ""
        deliver_target = str(envelope.params.get("cron_deliver") or "").strip()
        origin = _origin_source(envelope)

        if deliver_target.lower() == "origin" and (
            origin is None or origin.platform not in EXTERNAL_ORIGIN_PLATFORMS
        ):
            log.debug("cron deliver origin 无外部 sender，fallback 为新建会话")
            deliver_target = "new_session"

        if deliver_target in {"", "new_session"}:
            job_name = str(envelope.params.get("cron_job_name") or "").strip()
            new_session_id = f"{str(envelope.params.get('cron_job_id') or 'job')}_feed"
            already_exists = host.session_store.session_belongs_to(
                new_session_id,
                owner_account_id=envelope.user_id,
            )
            host.session_store.ensure_session(
                new_session_id,
                workspace_id=envelope.workspace_id,
                title=f"[定时] {job_name}" if job_name else "[定时] 任务",
                owner_account_id=envelope.user_id,
            )
            envelope.session_id = new_session_id
            await _notify_session(
                host,
                "cron_session_updated" if already_exists else "cron_session_created",
                envelope,
                new_session_id,
            )
            deliver_target = "new_session"

        if origin is not None and "session_context" not in envelope.params:
            envelope.params["session_context"] = SessionContext(
                source=origin,
                connected_platforms=["local", origin.platform],
                shared_multi_user=origin.chat_type in {"group", "channel"},
                session_id=envelope.session_id,
                workspace_id=envelope.workspace_id,
            )

        async for chunk in host.dispatch(envelope):
            if host._push_fn is not None:
                try:
                    await host._push_fn(
                        envelope.session_id,
                        chunk,
                        owner_account_id=envelope.user_id,
                    )
                except Exception:  # noqa: BLE001 - WS 推送不影响持久化结果
                    log.debug("cron 推送 chunk 失败，session=%s", envelope.session_id)
            if chunk.kind == "final":
                final_text = chunk.body.get("text", "")
            elif chunk.kind == "error":
                error = chunk.body.get("message", "")

        if deliver_target.lower() == "local":
            await _notify_session(host, "cron_session_updated", envelope, envelope.session_id)
        if deliver_target and deliver_target.lower() not in {"local", "new_session"}:
            reply = (final_text or error).strip()
            if reply:
                if host.delivery_router is None:
                    raise RuntimeError("cron deliver 需要 gateway delivery router")
                result = await host.delivery_router.deliver(
                    deliver_target,
                    reply,
                    origin=origin,
                    owner_account_id=envelope.user_id,
                )
                if not result.get("ok"):
                    failure = str(result.get("error") or f"deliver failed: {deliver_target}")
                    log.warning("cron deliver 失败 target=%s err=%s", deliver_target, failure)
                    raise RuntimeError(failure)

    return run


def _bind_tool_lease(tool: FunctionTool, context: FeatureInstallContext) -> None:
    handler = tool.handler

    async def run(args: dict[str, Any]) -> Any:
        async with context.acquire_lease(f"tool:{tool.name}"):
            result = handler(args)
            return await result if inspect.isawaitable(result) else result

    tool.handler = run
    tool.is_async = True


def _resolve_service_binding(
    host: CronFeatureHost,
    candidate: CronService | None,
) -> CronService | None:
    """选择这一代 Feature 要绑定的 Cron Service。

    ``build_cron_feature`` 会预先创建候选 Service，但宿主也可能在激活前显式
    注入一个实现（例如嵌入式宿主或测试替身）。只有当前绑定为空，或仍然是
    本次定义创建的候选，Feature 才接管候选；不同对象表示宿主已经提供了
    明确依赖，不能在激活时静默覆盖它。

    重启 Generation 停止旧代时会先撤销旧绑定，因此新代仍会自然接管候选
    Service，保持 restart/rollback 的所有权边界。
    """

    if candidate is None:
        return None
    current = getattr(host, "cron_service", None)
    return candidate if current is None or current is candidate else current


def build_cron_feature(
    host: CronFeatureHost,
    registry: Registry,
    store: CronJobStore,
    *,
    enabled: bool,
    max_parallel_jobs: int,
    desired_config_revision: int = 1,
) -> CronFeatureBundle:
    """构造一代 Cron Feature；真正启动由 FeatureRuntime 激活完成。"""

    base_runner = _build_runner(host)
    lease_factory: list[Any] = []

    async def run_with_lease(envelope: Envelope) -> None:
        if not lease_factory:
            await base_runner(envelope)
            return
        label = f"fire:{str(envelope.params.get('cron_fire_id') or envelope.request_id)}"
        async with lease_factory[0](label):
            await base_runner(envelope)

    service = (
        CronService(store, run_with_lease, max_parallel_jobs=max_parallel_jobs)
        if enabled
        else None
    )
    if service is not None:
        service.set_on_run_finished(host._on_cron_run_finished)

    async def install(context: FeatureInstallContext) -> None:
        current_service = getattr(host, "cron_service", None)
        explicit_binding = service is not None and (
            current_service is not None and current_service is not service
        )
        active_service = _resolve_service_binding(host, service)
        if active_service is not service:
            log.info(
                "Cron Feature 使用宿主显式注入的 Service: %s",
                type(active_service).__name__ if active_service is not None else "None",
            )
        if active_service is not None:
            callback = getattr(active_service, "set_on_run_finished", None)
            if callable(callback):
                callback(host._on_cron_run_finished)
        host.cron_service = active_service
        start_failed = False

        def clear_host_binding() -> None:
            # 外部注入的 Service 不属于候选 Generation。启动失败时保留它，
            # 让 Gateway 健康检查继续暴露其 start_error；成功停用则照常清除
            # 运行期绑定，避免已停用 Feature 被误报为 ready。
            if host.cron_service is active_service and not (explicit_binding and start_failed):
                host.cron_service = None

        context.register_disposer(
            clear_host_binding,
            label="binding:cron.service",
            phase=RegistrationPhase.CONTRIBUTION,
        )

        if enabled:
            context.register_context_contributor(
                ContextContributor(
                    contributor_id="cron.trigger.reminder",
                    handler=contribute_cron_trigger_reminder,
                    phase=ContextPhase.PROMPT,
                    priority=50,
                    predicate=lambda envelope: envelope.channel == "cron",
                    model_visible=True,
                    persistent=False,
                    description="Active Cron fire framing for the executing agent",
                )
            )

        for tool in build_cron_tools(store, active_service):
            _bind_tool_lease(tool, context)
            registry.register(tool)
            context.register_disposer(
                lambda tool=tool: registry.unregister(tool.name, expected=tool),
                label=f"tool:{tool.name}",
                phase=RegistrationPhase.CONTRIBUTION,
            )

        if active_service is None:
            return

        lease_factory.append(context.acquire_lease)
        context.register_disposer(
            lease_factory.clear,
            label="resource:cron.lease-gate",
        )
        # 先登记资源 disposer，再执行可能失败的启动，确保事务回滚可完整收尾。
        context.register_disposer(active_service.stop, label="resource:cron.scheduler")
        try:
            await active_service.start()
        except Exception:
            start_failed = True
            raise
        for owner_lease in host.active_owner.list():
            active_service.mount_owner(owner_lease.owner_account_id)

    definition = FeatureDefinition(
        CRON_FEATURE_ID,
        install,
        desired_config_revision=desired_config_revision,
        stop_policy=FeatureStopPolicy.CANCEL,
        update_strategy=FeatureUpdateStrategy.RESTART,
        required_by_product=False,
    )
    return CronFeatureBundle(definition=definition, service=service)


__all__ = [
    "CRON_FEATURE_ID",
    "CronFeatureBundle",
    "CronFeatureHost",
    "build_cron_feature",
]
