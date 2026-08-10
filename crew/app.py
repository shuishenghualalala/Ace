"""装配层（依赖注入）。

把各模块的具体实现拼装成一个可运行的 CrewApp，并提供统一入口 handle(envelope)。
所有入口（CLI/Gateway/Web）都调 app.handle()，按 mode 路由到单 Agent 或 Team。

新增一个实现（如新 Provider/新工具）通常只需在这里改一行注册。
"""

from __future__ import annotations

import asyncio
import json
import hashlib
import inspect
import re
import sys
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Coroutine

from crew.agent.compact import ContextCompactor, SummaryStore
from crew.agent.executor import create_executor
from crew.agent.external.store import ExternalAgentStore
from crew.agent.external.tools import register_external_agent_tools
from crew.agent.runtime import SingleAgent
from crew.agent.subagent.definition import build_preset_spec
from crew.core.envelope import Envelope, ResponseChunk
from crew.core.interfaces import Agent, LLMProvider, MemoryProvider, SessionStore, WorkspaceStore
from crew.evolution import EvolutionManager, EvolutionQueue
from crew.gateway.dispatcher import BusyMode, SessionDispatcher
from crew.memory.simple import SQLiteMemory
from crew.plugins.builtin import LoggingPlugin
from crew.plugins.manager import PluginManager
from crew.providers.anthropic_provider import AnthropicProvider
from crew.providers.openai_provider import OpenAIProvider
from crew.security.approvals import ApprovalManager
from crew.security.audit import SQLiteSecurityAudit
from crew.security.grants import GrantRegistry
from crew.security.rule_store import SQLiteRuleStore
from crew.security.service import SecurityApprovalService
from crew.state.config import (
    Config,
    ModelProfile,
    _build_profile_from_payload,
    is_placeholder_model_profile,
    load_config,
    remove_env_key,
    resolve_writable_env_path,
    write_env_key,
)
from crew.state.home import ensure_crew_home
from crew.agent.skills import configure_skill_filter
from crew.state.logging import get_logger, setup_logging
from crew.state._migration import OWNER_TABLE_LABELS, inspect_and_backfill_legacy_owners
from crew.state.session_store import SQLiteSessionStore
from crew.state.active_owner import ActiveOwnerLeaseStore
from crew.state.workspace_store import SQLiteWorkspaceStore
from crew.tools.registry import Registry, register_builtin_tools
from crew.tools.policy import (
    ToolDisclosureMode,
    exclude_toolsets,
    extend_with_toolsets,
    ordered_intersection,
    select_requested_tools,
)
from crew.tasks import TaskRuntime

log = get_logger("app")
OwnerSessionKey = tuple[str, str]

_MODEL_API_KEY_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MODEL_API_KEY_ENV_EXAMPLES = "CREW_API_KEY、OPENAI_API_KEY、ANTHROPIC_API_KEY 或 *_API_KEY*"

# 子 agent 工具黑名单（按 toolset 维度），用于 DELEGATE_BLOCKED_TOOLS。
# 一行可调：想放开飞书/定时任务，删掉对应项即可。
#   subagent/external_agent —— 禁嵌套 + 不转外部 agent
#   memory                  —— 临时子 agent 不应修改跨会话共享记忆
#   cron                    —— 不应注册后台定时任务
#   wiki.*                  —— Wiki 能力只属于专用 Wiki Agent
#   feishu*（前缀）          —— 不应产生发言/评论等外部副作用
SUBAGENT_BLOCKED_TOOLSETS = {
    "subagent",
    "subagent.preset",
    "external_agent",
    "memory",
    "cron",
    "tasks",
    "wiki.read",
    "wiki.manage",
}
SUBAGENT_BLOCKED_TOOLSET_PREFIXES = ("feishu",)


def _validate_model_api_key_env(api_key_env: str) -> str:
    """校验模型 API Key 的 env 名，避免模型配置变成任意环境变量写入口。"""
    name = str(api_key_env or "").strip() or "CREW_API_KEY"
    if not _MODEL_API_KEY_ENV_RE.fullmatch(name):
        raise ValueError(f"非法 api_key_env: {name!r}")
    if "API_KEY" not in name.upper():
        raise ValueError(f"api_key_env 只能使用模型密钥变量名，例如 {_MODEL_API_KEY_ENV_EXAMPLES}")
    return name


def _payload_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _parent_allowed_skill_slugs(
    parent_enabled: list[str] | None,
    parent_disabled: list[str] | None,
) -> set[str]:
    """父 agent 允许的 skill slug 集合（用于裁剪子 agent 指定的 skills，防越权）。

    委托 crew.agent.skills.list_skills 按父范围过滤，取 slug。父范围为 (None, None) 时
    等于全局过滤后的全量——即主 agent 默认能看到的全部技能。
    """
    from crew.agent.skills import list_skills

    try:
        return {item["slug"] for item in list_skills(enabled=parent_enabled, disabled=parent_disabled)}
    except Exception:  # noqa: BLE001
        return set()


class AgentManager:
    """按 session 缓存单 Agent 实例（LRU + 空闲 TTL 淘汰 + 指纹区分配置）。

    用于 gateway/run.py 的 _agent_cache（OrderedDict + _enforce_agent_cache_cap +
    _session_expiry_watcher）。支持 fingerprint 区分同一 session 不同配置的 Agent。

    工厂签名约定（按能力探测，兼容旧无参 / 仅 config 工厂）：
    - ``factory()``
    - ``factory(agent_config)``
    - ``factory(agent_config, owner_account_id=...)``  ← 主路径；owner 必须显式传入，
      不能依赖 ``current_owner_account_id``（该 ContextVar 要到 ``agent.run`` 才设置）。
    """

    def __init__(
        self,
        factory,
        *,
        max_size: int = 128,
        idle_ttl: float = 3600.0,  # 1 小时
    ) -> None:
        self._factory = factory
        self._factory_accepts_config = False
        self._factory_accepts_owner = False
        try:
            params = list(inspect.signature(factory).parameters.values())
            # 位置参数（含仅位置）→ 可接收 agent_config
            self._factory_accepts_config = any(
                p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL)
                for p in params
            )
            # 显式 owner_account_id 形参或 **kwargs → 可把 owner 传下去
            self._factory_accepts_owner = any(
                p.name == "owner_account_id" or p.kind == p.VAR_KEYWORD
                for p in params
            )
        except (TypeError, ValueError):
            self._factory_accepts_config = True
            self._factory_accepts_owner = True
        self._cache: OrderedDict[tuple[str, str, str], Agent] = OrderedDict()
        self._access_ts: dict[tuple[str, str, str], float] = {}
        self._max_size = max_size
        self._idle_ttl = idle_ttl
        # Lease is bound to the concrete Agent object, not only its cache key: an evicted
        # key may immediately create a replacement while the old turn is still finishing.
        self._lease_counts: dict[int, int] = {}
        self._retired: dict[int, Agent] = {}
        self._pending_close: dict[int, Agent] = {}
        self._closing_ids: set[int] = set()
        self._close_tasks: set[asyncio.Task] = set()
        self._leases_drained = asyncio.Event()
        self._leases_drained.set()
        self._accepting = True

    @staticmethod
    def _fingerprint(agent_config: dict) -> str:
        raw = json.dumps(agent_config, ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def get(self, session_id: str, agent_config: dict | None = None, owner_account_id: str = "") -> Agent:
        if not self._accepting:
            raise RuntimeError("AgentManager 已关闭")
        config = agent_config or {}
        key = (owner_account_id, session_id, self._fingerprint(config))
        if key in self._cache:
            self._cache.move_to_end(key)  # LRU 标记最近使用
        else:
            self._cache[key] = self._call_factory(config, owner_account_id)
        self._access_ts[key] = time.monotonic()
        self._enforce_cap()
        return self._cache[key]

    @asynccontextmanager
    async def lease(
        self,
        session_id: str,
        agent_config: dict | None = None,
        owner_account_id: str = "",
    ) -> AsyncIterator[Agent]:
        """Lease one concrete cached Agent for the complete duration of a turn."""
        agent = self.get(session_id, agent_config, owner_account_id)
        identity = id(agent)
        self._lease_counts[identity] = self._lease_counts.get(identity, 0) + 1
        self._leases_drained.clear()
        try:
            yield agent
        finally:
            self._release(agent)

    def _call_factory(self, config: dict, owner_account_id: str) -> Agent:
        """按工厂签名能力调用；owner 有则显式传入，避免依赖尚未设置的 ContextVar。"""
        if self._factory_accepts_config and self._factory_accepts_owner:
            return self._factory(config, owner_account_id=owner_account_id)
        if self._factory_accepts_config:
            return self._factory(config)
        return self._factory()

    def peek(self, session_id: str, owner_account_id: str = "") -> Agent | None:
        """取已缓存的 Agent（不创建）。用于 steer/interrupt 只作用于运行中的会话。"""
        for key, agent in self._cache.items():
            if key[0] == owner_account_id and key[1] == session_id:
                return agent
        return None

    def drop(self, session_id: str, owner_account_id: str = "") -> None:
        for key in list(self._cache):
            if key[0] == owner_account_id and key[1] == session_id:
                self._evict_key(key)

    def drop_owner(self, owner_account_id: str) -> None:
        """清理指定账号的 Agent 缓存，使模型配置与能力在下一轮立即生效。"""
        owner = str(owner_account_id or "")
        for key in list(self._cache):
            if key[0] == owner:
                self._cache.pop(key, None)
                self._access_ts.pop(key, None)

    def clear(self) -> None:
        for key in list(self._cache):
            self._evict_key(key)

    def evict_idle(self) -> int:
        """淘汰超过 idle_ttl 未访问的缓存条目。

        Returns:
            淘汰的条目数量。
        """
        now = time.monotonic()
        expired = [
            key for key, ts in self._access_ts.items()
            if now - ts > self._idle_ttl
        ]
        for key in expired:
            self._evict_key(key)
        return len(expired)

    def _enforce_cap(self) -> None:
        """超限时淘汰最久未用的条目。"""
        while len(self._cache) > self._max_size:
            key = next(iter(self._cache))
            self._evict_key(key)

    def _evict_key(self, key: tuple[str, str, str]) -> None:
        """Remove a cache key and retire its concrete Agent without racing active leases."""
        agent = self._cache.pop(key, None)
        self._access_ts.pop(key, None)
        if agent is None:
            return
        # A custom factory may deliberately share an Agent across keys. Do not retire it
        # until its final cache reference disappears.
        if any(cached is agent for cached in self._cache.values()):
            return
        identity = id(agent)
        if self._lease_counts.get(identity, 0) > 0:
            self._retired[identity] = agent
            return
        self._schedule_close(agent)

    def _release(self, agent: Agent) -> None:
        identity = id(agent)
        remaining = max(0, self._lease_counts.get(identity, 1) - 1)
        if remaining:
            self._lease_counts[identity] = remaining
        else:
            self._lease_counts.pop(identity, None)
            retired = self._retired.pop(identity, None)
            if retired is not None and not any(
                cached is retired for cached in self._cache.values()
            ):
                self._schedule_close(retired)
        if not self._lease_counts:
            self._leases_drained.set()

    def _schedule_close(self, agent: Agent) -> None:
        identity = id(agent)
        if identity in self._closing_ids:
            return
        self._closing_ids.add(identity)
        self._pending_close[identity] = agent
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Synchronous callers (notably a few unit tests/CLI setup paths) have no loop;
            # the next wait_closed()/aclose() drains these pending resources.
            return
        self._start_close_task(loop, identity, agent)

    def _start_close_task(
        self,
        loop: asyncio.AbstractEventLoop,
        identity: int,
        agent: Agent,
    ) -> None:
        self._pending_close.pop(identity, None)
        task = loop.create_task(self._close_one(identity, agent))
        self._close_tasks.add(task)
        task.add_done_callback(self._close_tasks.discard)

    async def _close_one(self, identity: int, agent: Agent) -> None:
        close = getattr(agent, "aclose", None)
        try:
            if callable(close):
                await close()
        except Exception:  # noqa: BLE001 - one Agent must not block sibling cleanup
            log.exception("关闭淘汰 Agent 失败: %s", type(agent).__name__)
        finally:
            self._closing_ids.discard(identity)

    async def wait_closed(self) -> None:
        """Drain all currently scheduled inactive-Agent close operations."""
        loop = asyncio.get_running_loop()
        for identity, agent in list(self._pending_close.items()):
            self._start_close_task(loop, identity, agent)
        while self._close_tasks:
            await asyncio.gather(*list(self._close_tasks), return_exceptions=True)

    async def aclose(self, *, timeout: float = 10.0) -> None:
        """Stop admissions, retire the cache, and close Agents after leases drain."""
        self._accepting = False
        self.clear()
        try:
            await asyncio.wait_for(self._leases_drained.wait(), timeout=max(0.1, timeout))
        except asyncio.TimeoutError:
            log.warning("等待 Agent lease 释放超时，活跃 Agent 将在实际释放后关闭")
        for identity, agent in list(self._retired.items()):
            if self._lease_counts.get(identity, 0) == 0:
                self._retired.pop(identity, None)
                self._schedule_close(agent)
        await self.wait_closed()


class CrewApp:
    def __init__(
        self,
        config: Config,
        provider: LLMProvider,
        registry: Registry,
        session_store: SessionStore,
        workspace_store: WorkspaceStore,
        memory: MemoryProvider,
        plugins: PluginManager,
    ) -> None:
        self.config = config
        self.provider = provider
        self.registry = registry
        self.session_store = session_store
        self.workspace_store = workspace_store
        self.memory = memory
        self.plugins = plugins
        self.security_grants = GrantRegistry()
        self.security_approvals = ApprovalManager(self.security_grants)
        self.security_rules = SQLiteRuleStore(config.db_path, wal_enabled=config.sqlite_wal)
        self.security_audit = SQLiteSecurityAudit(config.db_path, wal_enabled=config.sqlite_wal)
        self.security_service = SecurityApprovalService(
            self.security_approvals,
            self.security_grants,
            self.security_rules,
            self.security_audit,
            db_path=config.db_path,
        )
        # Gateway 级单活登录事实源；HTTP/WS、Cron 与渠道只消费这一份租约。
        self.active_owner = ActiveOwnerLeaseStore(
            config.db_path,
            wal_enabled=config.sqlite_wal,
        )
        self.tasks = TaskRuntime(
            config.db_path,
            wal_enabled=config.sqlite_wal,
            monitor_interval=config.tasks_monitor_interval_seconds,
            heartbeat_interval=config.tasks_heartbeat_interval_seconds,
            wait_timeout=config.tasks_wait_timeout_seconds,
            finished_retention_days=config.tasks_finished_retention_days,
        )
        self.tasks.auto_background_after = config.tasks_auto_background_after_seconds
        self.tasks.defaults = {
            "shell_inactivity": config.tasks_shell_inactivity_timeout_seconds,
            "shell_execution": config.tasks_shell_execution_timeout_seconds,
            "subagent_inactivity": config.tasks_subagent_inactivity_timeout_seconds,
            "subagent_execution": config.tasks_subagent_execution_timeout_seconds,
            "agent_turn_inactivity": config.tasks_agent_turn_inactivity_timeout_seconds,
            "agent_turn_execution": config.tasks_agent_turn_execution_timeout_seconds,
        }
        self.tasks.set_callbacks(
            on_event=self._on_task_event,
            on_completion=self._on_task_completion,
        )
        from crew.tools.process_registry import process_registry

        process_registry.configure_task_runtime(self.tasks)
        self.external_agents: ExternalAgentStore | None = None
        # L2 摘要缓存：单实例共享给所有 agent 的 compactor（避免多开 SQLite 连接）
        self.summary_store = SummaryStore(config.db_path, wal_enabled=config.sqlite_wal)

        self.agents = AgentManager(self._make_agent)
        # 对话级 Plan 模式管理器（由 build_app 装配后赋值）
        self.plan_manager = None
        # 专用 Wiki Agent 会话管理器（由 build_app 装配后赋值）
        self.wiki_manager = None
        # Team 管理器延迟装配（见 set_team_manager），避免 core 之外的循环依赖
        self.team = None
        # Dynamic Kanban 管理器延迟装配（见 build_app）
        self.dynamic_kanban = None
        # Work 业务域组合服务（由 build_app 装配；不进入 core）。
        self.work_service = None
        self.channel_bindings = None
        # 用户级插件开关偏好（由 build_app 装配后赋值）
        self.plugin_prefs = None
        # subagent：预设注册表 + 活跃子 agent 跟踪 + 后台任务（由 build_app 装配后赋值）
        self.subagent_registry = None
        self.subagent_active = None
        self.subagent_tasks = None
        # 后台子 agent 的 asyncio 任务强引用（防 GC），完成后自动移除
        self._subagent_bg_tasks: set[asyncio.Task] = set()
        # 后台子 agent 完成结果的待通知队列（按父 session），下一轮注入主 agent 上下文
        self._subagent_pending: dict[OwnerSessionKey, list[dict]] = {}
        # 异步进化队列：按 session 串行处理 evolution 任务，结果在下一轮交互中体现
        self._evolution_queue = EvolutionQueue()
        # App-owned global Providers retired by model switching wait for every task that
        # entered before the switch. This covers agent/team/kanban and detached children.
        self._provider_retirement_tasks: set[asyncio.Task] = set()
        self._pending_provider_retirements: dict[int, tuple[LLMProvider, set[asyncio.Task]]] = {}
        self._retiring_provider_ids: set[int] = set()
        # 外部 Team 的内置 Leader/规划器会跨多个请求复用 Agent，因此 owner 默认
        # Provider 也需由 App 持有并统一关闭；普通一次性生成接口仍走 owner_provider。
        self._owner_team_providers: dict[str, LLMProvider] = {}
        # Team 内置成员的显式模型绑定。key 含 owner 与 profile id，避免不同
        # 账号或不同模型错误复用同一个持久连接。
        self._owner_team_member_model_providers: dict[tuple[str, str], LLMProvider] = {}
        # 默认模型切换时，已有 Team/Dynamic Kanban 后台任务可能仍持有旧客户端。
        # 保留强引用到 shutdown，避免后台化后已脱离 Dispatcher 的任务被提前断流。
        self._stale_owner_team_providers: dict[int, LLMProvider] = {}
        # 显式功能级模型（当前为 wiki.model）创建的独立 Provider。
        self._auxiliary_providers: list[LLMProvider] = []
        self._shutdown_lock = asyncio.Lock()
        self._shutdown_complete = False

        # cron / mcp 由 build_app 装配后赋值；startup/shutdown 统一拉起与关闭
        self.cron_store = None
        self.cron_service = None
        self.delivery_router = None
        self.mcp_manager = None
        # First-party Browser Use runtime. build_app injects BrowserManager and
        # registers its tools after the generic built-ins are assembled.
        self.browser_manager = None
        # Gateway 装配后注入；供 ACP executor 创建按会话绑定的受限 MCP 交互工具。
        self.interaction_bridge = None
        # 后台任务推送回调：push(session_id, chunk) → 发给前端活跃 WS
        self._push_fn: Callable[..., Any] | None = None
        self._push_payload_fn: Callable[..., Coroutine[Any, Any, None]] | None = None
        self._notify_owner_fn: Callable[..., Coroutine[Any, Any, None]] | None = None
        # 会话过期定时器
        self._expiry_task: asyncio.Task | None = None
        self.dispatcher = SessionDispatcher(
            self.handle,
            self.session_store,
            busy_mode=BusyMode(self.config.gateway_busy_mode),
            controller=self,
            max_active_runs=self.config.gateway_max_active_runs,
            max_queue_depth_per_session=self.config.gateway_max_queue_depth_per_session,
            active_children_fn=self._active_children_snapshot,
            task_runtime=self.tasks,
        )

    def _on_task_event(self, task: dict[str, Any]) -> None:
        """Push normalized task events to connected clients."""
        if self._push_fn is None:
            return
        summary = str(task.get("result") or task.get("error") or "")[:500]
        chunk = ResponseChunk.task_event(
            str(task.get("request_id") or ""),
            str(task.get("task_id") or ""),
            str(task.get("kind") or ""),
            str(task.get("phase") or "progress"),
            status=str(task.get("status") or ""),
            progress=task.get("progress") if isinstance(task.get("progress"), dict) else {},
            output_ref=str(task.get("output_ref") or ""),
            summary=summary,
        )
        try:
            value = self._push_fn(
                str(task.get("session_id") or ""),
                chunk,
                owner_account_id=str(task.get("owner_account_id") or ""),
            )
            if asyncio.iscoroutine(value):
                try:
                    asyncio.get_running_loop().create_task(value)
                except RuntimeError:
                    value.close()
        except RuntimeError:
            return

    def _on_task_completion(self, task: dict[str, Any]) -> None:
        """Deduplicate completion delivery and resume background tool tasks."""
        task_id = str(task.get("task_id") or "")
        owner = str(task.get("owner_account_id") or "")
        if not task_id or not self.tasks.mark_notified(
            task_id,
            owner_account_id=owner,
        ):
            return
        self._on_task_event({**task, "phase": task.get("status", "completed")})
        if (
            task.get("kind") in {"shell", "subagent"}
            and bool(task.get("backgrounded"))
            and self._should_resume_completed_task(task)
            and self.tasks.mark_resume_enqueued(
                task_id,
                owner_account_id=owner,
            )
            and self.tasks._loop is not None
            and self.tasks._loop.is_running()
        ):
            asyncio.create_task(self._resume_completed_task(task))

    def _should_resume_completed_task(self, task: dict[str, Any]) -> bool:
        """Return whether a background task completion should trigger a resume turn."""
        if bool(task.get("cancel_requested")):
            return False
        if str(task.get("status") or "") in {"cancelled"}:
            return False
        sid = str(task.get("session_id") or "")
        owner = str(task.get("owner_account_id") or "")
        visible_sid = sid.split("::turn::", 1)[0] if "::turn::" in sid else sid
        for candidate in dict.fromkeys([sid, visible_sid]):
            if not candidate:
                continue
            try:
                status, _error = self.session_store.get_status(candidate, owner_account_id=owner)
            except Exception:  # noqa: BLE001 - 状态查询失败不阻断正常完成通知
                continue
            if status in {"stopped", "cancelled"}:
                return False
        return True

    async def _resume_completed_task(self, task: dict[str, Any]) -> None:
        """Queue one internal turn carrying a completed task result."""
        sid = str(task.get("session_id") or "")
        if not sid or not self._should_resume_completed_task(task):
            return
        envelope = Envelope.of(
            "后台任务已完成，请根据结果继续原任务。",
            session_id=sid,
            channel="task",
            workspace_id=self.session_store.get_workspace_id(
                sid,
                owner_account_id=str(task.get("owner_account_id") or ""),
            ) or "default",
            user_id=str(task.get("owner_account_id") or ""),
            mode="agent",
            params={
                "internal_task_resume": True,
                "task_notifications": [task],
            },
        )
        async for chunk in self.dispatch(envelope):
            if self._push_fn is not None:
                value = self._push_fn(sid, chunk, owner_account_id=envelope.user_id)
                if asyncio.iscoroutine(value):
                    await value

    def _build_fallback_providers(self, owner_account_id: str = "") -> list[LLMProvider]:
        """按 config.fallback_models 预建备用 provider（跳过当前激活模型与无 key 的）。

        解析 profile 走 owner 视图，避免 fallback 列表里的私有模型 id 被当成「不存在」。
        """
        cfg = self.config
        owner = str(owner_account_id or "").strip()
        profiles = self.owner_model_profiles(owner) if owner else cfg.model_profiles
        active_id = cfg.owner_default_model_id(owner) if owner else (cfg.default_model_id or cfg.active_model_id)
        providers: list[LLMProvider] = []
        for mid in cfg.fallback_models:
            if mid == active_id:
                continue
            profile = profiles.get(mid)
            if profile is None or not getattr(profile, "has_key", False):
                log.warning("fallback 模型 %s 不存在或未配置 key，跳过", mid)
                continue
            providers.append(build_provider_for_profile(profile, cfg.stream_read_timeout))
        return providers

    def _default_agent_config(self) -> dict:
        cfg = self.config
        return {
            "executor": cfg.agent_executor,
            "client": cfg.agent_client_config,
            "acp": cfg.agent_acp_config,
            # 未显式绑定的 Session 在装配时解析 owner 默认模型。不能把进程级 active
            # 写成固定绑定，否则 API/Cron 创建的会话会压过远程 owner 的默认配置。
            "model_profile_id": "inherit",
        }

    def _session_agent_config(self, session_id: str, owner_account_id: str = "") -> dict:
        getter = getattr(self.session_store, "get_agent_config", None)
        if callable(getter):
            stored = getter(session_id, owner_account_id=owner_account_id)
            if isinstance(stored, dict) and stored:
                return {k: v for k, v in stored.items() if not k.startswith("_")}
        return self._default_agent_config()

    @staticmethod
    def _uses_external_agents_feature(agent_config: dict | None) -> bool:
        """是否为“智能体”页创建的外部 Agent/Team 绑定。"""
        config = agent_config if isinstance(agent_config, dict) else {}
        executor = str(config.get("executor") or "").strip().lower()
        external = config.get("external") if isinstance(config.get("external"), dict) else {}
        acp = config.get("acp") if isinstance(config.get("acp"), dict) else {}
        team = config.get("team") if isinstance(config.get("team"), dict) else {}
        if executor in {"external", "acp"}:
            return bool(
                str(
                    config.get("external_agent_id")
                    or external.get("external_agent_id")
                    or acp.get("external_agent_id")
                    or ""
                ).strip()
            )
        if executor == "team":
            return bool(str(team.get("external_team_id") or "").strip())
        return False

    def _single_agent_tool_filter(
        self,
        executor_kind: str,
        ac: dict[str, Any] | None = None,
    ) -> list[str] | None:
        if executor_kind != "builtin":
            return []

        base_enabled = ac.get("enabled_toolsets") if ac is not None else None
        base_disabled = ac.get("disabled_toolsets") if ac is not None else None
        base_enabled_tools = ac.get("enabled_tools") if ac is not None else None
        base_disabled_tools = ac.get("disabled_tools") if ac is not None else None
        base_names = {
            s["function"]["name"]
            for s in self.registry.list_schemas(
                enabled_toolsets=base_enabled,
                disabled_toolsets=base_disabled,
                enabled_tools=base_enabled_tools,
                disabled_tools=base_disabled_tools,
            )
        }
        registry_order = self.registry.names()
        allowed = ordered_intersection(registry_order, base_names)

        # builtin 执行器不直接调用 external_agent 工具。
        allowed = exclude_toolsets(self.registry, allowed, exact={"external_agent"})

        return allowed

    def _wiki_agent_tool_filter(self, main_tools: list[str]) -> list[str]:
        """Wiki Agent = 同身份主 Agent 最终静态范围 + Wiki 专属工具。"""
        from crew.wiki.tools import WIKI_MANAGE_TOOLSET, WIKI_READ_TOOLSET

        return extend_with_toolsets(
            self.registry,
            main_tools,
            (WIKI_READ_TOOLSET, WIKI_MANAGE_TOOLSET),
        )

    def _browser_plugin_effective(self, owner: str, user_type: str) -> bool:
        """当前 (owner, user_type) 下 Browser 插件的有效状态。

        system_enabled 由「插件已加载、browser_use 已注册」表达；role/user 两层见
        crew.state.plugin_preferences。偏好读取失败按 fail-closed（视为关）。
        """
        from crew.state.plugin_preferences import (
            plugin_effective_enabled,
            plugin_role_allowed,
        )

        system_enabled = self.registry is not None and "browser_use" in set(
            self.registry.names()
        )
        ac = self.config.access_control.resolve_for(user_type)
        user_enabled: bool | None = None
        if self.plugin_prefs is not None and owner:
            try:
                user_enabled = self.plugin_prefs.get_enabled(owner, "browser")
            except Exception:  # noqa: BLE001 - 读取失败 fail-closed
                log.exception("读取插件偏好失败，按关闭处理 owner=%s", owner)
                user_enabled = False
        return plugin_effective_enabled(
            system_enabled=system_enabled,
            role_allowed=plugin_role_allowed(ac, "browser"),
            user_enabled=user_enabled,
            user_type=user_type,
        )

    def _make_agent(
        self,
        agent_config: dict | None = None,
        *,
        owner_account_id: str = "",
    ) -> SingleAgent:
        """按会话配置装配主 Agent。

        ``owner_account_id`` 必须由 ``AgentManager.get`` / 调用方显式传入。
        不能只读 ``current_owner_account_id``：该 ContextVar 在 ``agent.run`` 才设置，
        而 ``handle → agents.get → _make_agent`` 发生在更早，空 ContextVar 会导致
        owner 私有模型（会话绑定）被误判为不存在并回退全局 provider。
        """
        cfg = self.config
        resolved = agent_config or self._default_agent_config()
        executor_kind = str(
            resolved.get("executor")
            or cfg.agent_executor
            or "builtin"
        ).strip().lower()
        executor_config = resolved.get(executor_kind, {})
        if executor_kind == "external" and not isinstance(executor_config, dict):
            executor_config = {}
        if executor_kind == "external" and not executor_config:
            legacy_acp = resolved.get("acp")
            executor_config = legacy_acp if isinstance(legacy_acp, dict) else {}
        if not isinstance(executor_config, dict):
            executor_config = {}
        if executor_kind == "client":
            executor_config = {
                **cfg.agent_client_config,
                **executor_config,
            }
        elif executor_kind in {"external", "acp"}:
            executor_config = {
                **cfg.agent_acp_config,
                **executor_config,
            }
        else:
            executor_config = {}
        if executor_kind in {"external", "acp", "client"}:
            external_agent_id = str(resolved.get("external_agent_id") or "").strip()
            if external_agent_id and not str(executor_config.get("external_agent_id") or "").strip():
                executor_config["external_agent_id"] = external_agent_id

        # 显式参数优先；仅在调用方未传时回退 ContextVar（兼容直接测 _make_agent 的旧路径）
        from crew.core.runctx import current_owner_account_id

        owner = str(owner_account_id or current_owner_account_id.get() or "").strip()
        requested_user_type = str(resolved.get("user_type") or "").strip().lower()
        resolver = getattr(cfg.access_control, "user_type_for_owner", None)
        user_type = (
            resolver(owner, requested=requested_user_type)
            if callable(resolver)
            else requested_user_type or cfg.access_control.user_type
        )
        if user_type not in ("external", "internal"):
            user_type = cfg.access_control.user_type
        ac = cfg.access_control.resolve_for(user_type)
        owner_profiles = self.owner_model_profiles(owner) if owner else cfg.model_profiles
        # 先选出最终 profile 再创建客户端，避免 Owner 默认模型随后被 session
        # 覆盖时遗留一个无人持有的连接池。没有动态 profile 才借用 App 全局 Provider。
        provider_profile: ModelProfile | None = None
        build_dynamic_provider = False
        # 装配期回退说明：写入 agent，供 run 开头以 status 帧推到 UI（避免只打日志用户无感）
        model_fallback_notice: str | None = None
        # owner 默认激活模型；仅 owner 非空时赋值，末尾用于静默降级判定（见缺口 1a）
        active: ModelProfile | None = None
        if owner:
            active = self.config.owner_default_model_profile(owner)
            if active is not None and active.has_key:
                provider_profile = active
                build_dynamic_provider = True
            else:
                # The owner overlay can retain a deleted/unloaded model id, or
                # point at a profile whose owner-local key disappeared.  In
                # both cases self.provider is the global active provider, so
                # capability gating must use that same profile too.
                provider_profile = cfg.active_model
        else:
            provider_profile = cfg.active_model
        session_model = str(resolved.get("model_profile_id") or "").strip()
        if session_model and session_model != "inherit":
            profile = owner_profiles.get(session_model)
            if profile and profile.has_key:
                provider_profile = profile
                build_dynamic_provider = True
            else:
                fallback_label = (
                    provider_profile.label
                    if provider_profile is not None and provider_profile.has_key
                    else "FakeProvider 演示模式"
                )
                model_fallback_notice = (
                    f"会话绑定模型「{session_model}」不可用（不存在或无 API Key），"
                    f"已回退到「{fallback_label}」。"
                    "请前往“设置 → 模型”配置 API Key 后切换到真实模型。"
                )
                log.warning("会话绑定模型 %s 不存在或无 API Key，回退全局 provider", session_model)
        # 静默降级提示（缺口 1a）：仅当最终 provider 真的落回全局 active 模型、且 owner
        # 默认激活的内置模型本身缺 key（典型 = 登录未下发 modelkey）时才提示。session
        # 若已用自己的有 key 模型救回（provider_profile 不再是全局 active），或已产生更具体的
        # 会话绑定提示（model_fallback_notice 已设），都不重复打扰——避免「用户自带可用模型却
        # 误报降级」的假阳性。
        if (
            active is not None
            and not active.has_key
            and provider_profile is cfg.active_model
            and not model_fallback_notice
        ):
            if cfg.active_model.has_key:
                model_fallback_notice = (
                    f"内置模型「{active.label}」未配置 API Key（登录可能未下发），"
                    f"已临时回退到「{cfg.active_model.label}」。"
                )
            else:
                model_fallback_notice = (
                    f"模型「{active.label}」未配置 API Key，"
                    "当前使用 FakeProvider 演示模式，不会调用真实模型。"
                    "请前往“设置 → 模型”完成配置。"
                )
            log.warning(
                "owner active 模型 %s 无 API Key，回退全局 provider %s",
                active.id,
                cfg.active_model.id,
            )
        owns_provider = (
            build_dynamic_provider
            and provider_profile is not None
            and provider_profile.has_key
        )
        provider = (
            build_provider_for_profile(provider_profile, cfg.stream_read_timeout)
            if owns_provider
            else self.provider
        )
        enabled_skills = ac.get("enabled_skills") if ac is not None else None
        disabled_skills = ac.get("disabled_skills") if ac is not None else None
        browser_effective = self._browser_plugin_effective(owner, user_type)
        if not browser_effective:
            # 插件有效状态折算进 per-session skill 范围：关闭后下一轮不再出现
            disabled_skills = [*(disabled_skills or []), "browser-use"]

        if executor_kind in {"external", "acp"} and isinstance(executor_config, dict):
            executor_config = {
                **executor_config,
                "external_store": self.external_agents,
                "interaction_bridge": self.interaction_bridge,
            }
        if executor_kind == "client" and isinstance(executor_config, dict):
            executor_config = {
                **executor_config,
                "external_store": self.external_agents,
            }

        # ---- 持久化预设 Agent 会话 ----
        agent_id = "default"
        system_prompt_override = None
        preset_agent_type = str(resolved.get("preset_agent_type") or "").strip()
        preset_definition = None
        preset_spec = None
        if preset_agent_type and self.subagent_registry is not None:
            preset_definition = self.subagent_registry.get(preset_agent_type)
        is_wiki_agent_session = preset_agent_type == "Wiki"
        if is_wiki_agent_session:
            if preset_definition is None:
                raise RuntimeError("Wiki 预设不存在，无法创建 Wiki Agent")
            preset_spec = build_preset_spec(preset_definition)
            system_prompt_override = str(preset_spec["system_prompt"] or "")
            agent_id = "subagent:Wiki"
            enabled_skills = list(preset_spec["preset_skills"] or [])
            disabled_skills = ["*"] if not enabled_skills else None
        else:
            # Wiki 管理 Skill 只属于固定 Wiki 预设；普通主 Agent 即使默认启用全部
            # Skills 也看不到它，避免通过说明间接获得管理工作流。
            disabled_skills = [*(disabled_skills or []), "crew-wiki-curator"]

        main_tool_filter = self._single_agent_tool_filter(executor_kind, ac)
        if is_wiki_agent_session:
            tool_filter = self._wiki_agent_tool_filter(main_tool_filter)
        else:
            tool_filter = main_tool_filter
            # 普通对话不能直接发现或调用任何 Wiki 工具；Wiki 页面会把消息直接
            # 发送到 preset_agent_type=Wiki 的持久化预设会话。
            tool_filter = exclude_toolsets(
                self.registry,
                tool_filter,
                exact={"wiki.read", "wiki.manage"},
            )
        if not browser_effective and tool_filter is not None:
            # 插件关闭时从允许工具中剔除 browser_use（skill 过滤见上）
            tool_filter = [name for name in tool_filter if name != "browser_use"]
        tool_filter = self._apply_model_capability_filter(
            tool_filter,
            provider_profile.capabilities if provider_profile is not None else None,
        )

        return self._build_single_agent(
            provider=provider,
            executor_kind=executor_kind,
            executor_config=executor_config,
            max_iterations=cfg.max_iterations,
            tool_filter=tool_filter,
            user_type=user_type,
            profile_path=ac.get("prompt_profile_path"),
            system_prompt=system_prompt_override,
            enable_title=cfg.title_auto,
            plan_manager=self.plan_manager,
            wiki_manager=self.wiki_manager if is_wiki_agent_session else None,
            tool_disclosure_mode=(
                ToolDisclosureMode.DIRECT
                if is_wiki_agent_session
                else ToolDisclosureMode.PROGRESSIVE
            ),
            agent_id=agent_id,
            enabled_skills=enabled_skills,
            disabled_skills=disabled_skills,
            include_optional_skills=False,
            context_window_override=provider_profile.context_window if provider_profile else None,
            owner_account_id=owner,
            model_fallback_notice=model_fallback_notice,
            owned_providers=[provider] if owns_provider else None,
            model_capabilities=(
                list(provider_profile.capabilities) if provider_profile is not None else None
            ),
        )

    def _build_single_agent(
        self,
        *,
        provider: LLMProvider,
        executor_kind: str,
        executor_config: dict | None,
        max_iterations: int,
        tool_filter: list[str] | None,
        user_type: str = "internal",
        profile_path: str | None = None,
        system_prompt: str | None = None,
        enable_title: bool = False,
        lightweight: bool = False,
        plan_manager: Any = None,
        wiki_manager: Any = None,
        tool_disclosure_mode: ToolDisclosureMode = ToolDisclosureMode.PROGRESSIVE,
        agent_id: str = "default",
        enabled_skills: list[str] | None = None,
        disabled_skills: list[str] | None = None,
        inject_skills: bool = False,
        include_optional_skills: bool = False,
        context_window_override: int | None = None,
        owner_account_id: str = "",
        model_fallback_notice: str | None = None,
        owned_providers: list[LLMProvider] | None = None,
        model_capabilities: list[str] | None = None,
    ) -> SingleAgent:
        """构造一个 SingleAgent（executor + compactor + guardrail）。

        _make_agent（主 agent）与 _make_subagent（子 agent）共用，避免重复装配。
        lightweight=True 时（子 agent）跳过全局 SOUL/MEMORY/USER/上下文文件/skills 注入；
        inject_skills=True 时为 lightweight 子 agent 单独放开 skills 索引注入
        （供 delegate_task 继承主 agent 技能）。
        """
        cfg = self.config
        # 触发阈值：compaction_token_budget>0 绝对值优先；否则按 ratio × context_window
        # 动态计算并取保守的 0.75，避免硬编码小值导致过早压缩。
        if cfg.compaction_token_budget > 0:
            token_budget = cfg.compaction_token_budget
        else:
            cw = context_window_override or cfg.context_window or 128000
            token_budget = int(cfg.compaction_token_budget_ratio * cw)
        compactor = ContextCompactor(
            provider,
            enabled=cfg.compaction_enabled,
            token_budget=token_budget,
            keep_recent=cfg.compaction_keep_recent,
            keep_recent_tools=cfg.compaction_keep_recent_tools,
            l2_incremental=cfg.compaction_l2_incremental,
            l2_delta_threshold=cfg.compaction_l2_delta_threshold,
            post_compact_files=cfg.compaction_post_compact_files,
            post_compact_max_chars_per_file=cfg.compaction_post_compact_max_chars_per_file,
            max_tool_result_chars=cfg.compaction_max_tool_result_chars,
            store=self.summary_store,
        )
        from crew.agent.loop import ToolCallGuardrailConfig

        guardrail_config = ToolCallGuardrailConfig(
            warnings_enabled=True,
            hard_stop_enabled=cfg.guardrail_enabled and cfg.guardrail_hard_stop,
            exact_failure_block_after=cfg.guardrail_exact_failure_block_after,
            no_progress_block_after=cfg.guardrail_no_progress_block_after,
        )

        fallback_providers = self._build_fallback_providers(owner_account_id)
        executor = create_executor(
            executor_kind,
            provider=provider,
            registry=self.registry,
            plugins=self.plugins,
            config=executor_config or {},
            max_iterations=max_iterations,
            max_retries=cfg.retry_max,
            backoff_seconds=cfg.retry_backoff,
            guardrail_config=guardrail_config,
            parallel_tools=cfg.parallel_tools,
            fallback_providers=fallback_providers,
            compactor=compactor,
            empty_retry_max=cfg.empty_retry_max,
            continuation_max=cfg.continuation_max,
            max_parallel_tool_calls=cfg.max_parallel_tool_calls,
            max_delegate_tool_calls=cfg.team_max_concurrent_children,
            plan_manager=plan_manager,
        )
        kwargs: dict = dict(
            provider=provider,
            registry=self.registry,
            session_store=self.session_store,
            memory=self.memory,
            plugins=self.plugins,
            max_iterations=max_iterations,
            executor=executor,
            compactor=compactor,
            enable_title=enable_title,
            tool_filter=tool_filter,
            user_type=user_type,
            profile_path=profile_path,
            lightweight=lightweight,
            plan_manager=plan_manager,
            wiki_manager=wiki_manager,
            tool_disclosure_mode=tool_disclosure_mode,
            agent_id=agent_id,
            enabled_skills=enabled_skills,
            disabled_skills=disabled_skills,
            inject_skills=inject_skills,
            include_optional_skills=include_optional_skills,
            model_fallback_notice=model_fallback_notice,
            owned_providers=[*(owned_providers or []), *fallback_providers],
            model_capabilities=model_capabilities,
        )
        if system_prompt:
            kwargs["system_prompt"] = system_prompt
        # Evolution 自动触发：仅主 agent（非 lightweight）注入。
        if cfg.evolution_auto_trigger and not lightweight:
            kwargs["evolution_manager"] = EvolutionManager(
                session_store=self.session_store,
                llm_provider=provider,
            )
            kwargs["evolution_full_cycle"] = cfg.evolution_auto_full_cycle
            kwargs["evolution_visible"] = cfg.evolution_visible
            kwargs["evolution_queue"] = self._evolution_queue
        return SingleAgent(**kwargs)

    # ---- subagent（主 agent 通过 delegate_task / run_agent 调用的子 agent）----
    def _subagent_tool_filter(
        self,
        toolsets: list[str] | None,
        tools: list[str] | None,
        *,
        user_type: str = "internal",
    ) -> list[str]:
        """按 toolsets（不填=继承主 agent 全量）算工具名，tools 白名单取交集，
        并强制剔除子 agent 不该碰的工具集。

        同时按父 user_type 的 access_control 封顶——子 agent 不能拿到父本身
        拿不到的工具（用于 子/父 toolset 求交集，防外部用户越权）。
        """
        # 防御性归一化：模型可能把 toolsets/tools 传成字符串（schema 虽是 array）。
        # 不归一化会让 list_schemas(enabled_toolsets="file") 把字符串当字符集合，
        # 导致子 agent 静默拿到空工具集。
        if isinstance(toolsets, str):
            toolsets = [toolsets]
        if isinstance(tools, str):
            tools = [tools]

        ac = self.config.access_control.resolve_for(user_type)
        access_allowed = {
            s["function"]["name"]
            for s in self.registry.list_schemas(
                enabled_toolsets=ac.get("enabled_toolsets"),
                disabled_toolsets=ac.get("disabled_toolsets"),
                enabled_tools=ac.get("enabled_tools"),
                disabled_tools=ac.get("disabled_tools"),
            )
        }
        from crew.core.runctx import current_authorized_tool_names

        parent_snapshot = current_authorized_tool_names.get()
        parent_allowed = (
            set(parent_snapshot) & access_allowed
            if parent_snapshot is not None
            else access_allowed
        )
        names = select_requested_tools(
            self.registry,
            ordered_intersection(self.registry.names(), parent_allowed),
            requested_toolsets=toolsets,
            requested_tools=tools,
        )
        names = exclude_toolsets(
            self.registry,
            names,
            exact=SUBAGENT_BLOCKED_TOOLSETS,
            prefixes=SUBAGENT_BLOCKED_TOOLSET_PREFIXES,
        )
        # 子 agent 同样受 per-owner 插件有效状态约束（schema 层；执行层还有
        # browser_use 的 permission_resolver 逐次重查兜底）。
        from crew.core.runctx import current_owner_account_id

        owner = str(current_owner_account_id.get() or "").strip()
        if not self._browser_plugin_effective(owner, user_type):
            names = [n for n in names if n != "browser_use"]
        return names

    def _apply_model_capability_filter(
        self,
        tool_filter: list[str] | None,
        capabilities: list[str] | tuple[str, ...] | None,
    ) -> list[str] | None:
        """把最终生效模型能力收敛到实际可执行工具集。"""
        if capabilities is None:
            return tool_filter
        enabled = {str(item).strip().lower() for item in capabilities}
        if "tools" not in enabled:
            return []
        if "vision" not in enabled:
            base = self.registry.names() if tool_filter is None else tool_filter
            return [name for name in base if name != "browser_vision"]
        return tool_filter

    def _make_subagent(self, spec: dict) -> SingleAgent:
        """根据 spec（system_prompt/toolsets/tools/model/max_iterations）构造子 agent。

        作为 build_child 回调注入 register_subagent_tools。子 agent 继承父会话的
        user_type（经 contextvar），权限不超过父——防外部受限用户越权。
        """
        from crew.core.runctx import (
            current_model_capabilities,
            current_owner_account_id,
            current_skill_scope,
            current_user_type,
        )

        cfg = self.config
        parent_user_type = (current_user_type.get() or cfg.access_control.user_type).strip().lower()
        if parent_user_type not in ("external", "internal"):
            parent_user_type = cfg.access_control.user_type

        provider = self.provider
        sub_profile: ModelProfile | None = None
        inherited_capabilities = current_model_capabilities.get()
        effective_capabilities: list[str] | None = (
            list(inherited_capabilities) if inherited_capabilities is not None else None
        )
        model = str(spec.get("model") or "inherit").strip()
        if model and model != "inherit":
            # 子 agent 创建时通常已在 agent.run 内，ContextVar 有值；仍走 owner 视图，
            # 才能解析会话/账号私有模型，与 _make_agent 口径一致。
            owner = str(current_owner_account_id.get() or "").strip()
            profiles = self.owner_model_profiles(owner) if owner else cfg.model_profiles
            profile = profiles.get(model)
            if profile and profile.has_key:
                provider = build_provider_for_profile(profile, cfg.stream_read_timeout)
                sub_profile = profile
                effective_capabilities = list(profile.capabilities)
            else:
                log.warning("subagent 指定模型 %s 不存在或无 API Key，回退继承主 agent", model)
        elif effective_capabilities is None:
            # 兼容直接构造子 Agent 的调用路径；正常运行中优先使用父 Agent 写入的
            # ContextVar，因为父会话可能绑定的并不是 owner/global active 模型。
            owner = str(current_owner_account_id.get() or "").strip()
            inherited_profile = cfg.owner_default_model_profile(owner) if owner else cfg.active_model
            if inherited_profile is not None:
                effective_capabilities = list(inherited_profile.capabilities)

        # 子 agent 必须有有限迭代上限：主 agent 无限（0）时，
        # spec 未指定则回落 subagent_max_iterations；spec 显式 0 视为无限（尊重调用方意图）。
        spec_mi = spec.get("max_iterations")
        if spec_mi is not None:
            max_iter = int(spec_mi)
        elif cfg.max_iterations > 0:
            max_iter = cfg.max_iterations
        else:
            max_iter = cfg.subagent_max_iterations

        # —— skills 继承（仅 delegate_task 标记 inherit_skills 时启用）——
        # 父范围取主 agent 运行时写入的 current_skill_scope，保证子 agent
        # 继承的是父真实生效范围，而非仅 access_control 基线。run_agent 不标记 → 不注入。
        enabled_skills: list[str] | None = None
        disabled_skills: list[str] | None = None
        inject_skills = False
        preset_skills = spec.get("preset_skills")
        if isinstance(preset_skills, list):
            inject_skills = True
            enabled_skills = [str(slug) for slug in preset_skills if str(slug).strip()]
            disabled_skills = ["*"] if not enabled_skills else None
        elif spec.get("inherit_skills"):
            inject_skills = True
            parent_enabled, parent_disabled = current_skill_scope.get()
            requested = spec.get("skills")
            if isinstance(requested, list) and len(requested) == 0:
                # 显式传 []：不继承任何技能
                disabled_skills = ["*"]
            elif isinstance(requested, list) and requested:
                # 指定列表：与父允许集取交集，防越权拿到父都拿不到的技能
                allowed = _parent_allowed_skill_slugs(parent_enabled, parent_disabled)
                enabled_skills = [s for s in requested if s in allowed]
                if not enabled_skills:
                    log.warning(
                        "delegate_task 指定的 skills %s 均不在父允许范围内，子 agent 将无可用技能",
                        requested,
                    )
                    disabled_skills = ["*"]
            else:
                # 未指定：继承父全部生效范围
                enabled_skills = parent_enabled
                disabled_skills = parent_disabled

        is_wiki_preset = spec.get("preset_name") == "Wiki"
        if is_wiki_preset:
            from crew.core.runctx import current_authorized_tool_names

            parent_snapshot = current_authorized_tool_names.get()
            if parent_snapshot is None:
                ac = cfg.access_control.resolve_for(parent_user_type)
                main_tools = self._single_agent_tool_filter("builtin", ac)
            else:
                main_tools = ordered_intersection(self.registry.names(), parent_snapshot)
            tool_filter = self._wiki_agent_tool_filter(main_tools)
        else:
            tool_filter = self._subagent_tool_filter(
                spec.get("toolsets"), spec.get("tools"), user_type=parent_user_type
            )
        tool_filter = self._apply_model_capability_filter(tool_filter, effective_capabilities)

        return self._build_single_agent(
            provider=provider,
            executor_kind="builtin",
            executor_config={},
            max_iterations=int(max_iter),
            tool_filter=tool_filter,
            user_type=parent_user_type,
            profile_path=None,
            system_prompt=spec.get("system_prompt"),
            enable_title=False,
            lightweight=True,
            wiki_manager=self.wiki_manager if is_wiki_preset else None,
            tool_disclosure_mode=(
                ToolDisclosureMode.DIRECT
                if is_wiki_preset
                else ToolDisclosureMode.PROGRESSIVE
            ),
            agent_id=f"subagent:{spec['preset_name']}" if spec.get("preset_name") else "subagent",
            enabled_skills=enabled_skills,
            disabled_skills=disabled_skills,
            inject_skills=inject_skills,
            context_window_override=sub_profile.context_window if sub_profile else None,
            owned_providers=[provider] if sub_profile is not None else None,
            model_capabilities=effective_capabilities,
        )

    def set_team_manager(self, team) -> None:
        self.team = team

    def set_push(
        self,
        push_fn: Callable[..., Any] | None = None,
        *,
        push_payload_fn: Callable[..., Coroutine[Any, Any, None]] | None = None,
        notify_owner_fn: Callable[..., Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        """注入 WS 推送回调（由 gateway 在构建时调用）。

        push_fn(session_id, chunk): 接收 ResponseChunk 的推送（已有代码使用）。
        push_payload_fn(session_id, payload): 接收已格式化 dict payload 的推送；
        用于工具层直接向前端发事件（如 ask_followup_question）。
        notify_owner_fn(owner_account_id, payload): 向某账号下所有 WS 连接广播，
        用于 cron 新建会话等场景（新会话尚未被订阅，无法按 session 推送）。
        """
        if push_fn is not None:
            self._push_fn = push_fn
        if push_payload_fn is not None:
            self._push_payload_fn = push_payload_fn
        if notify_owner_fn is not None:
            self._notify_owner_fn = notify_owner_fn

    def _active_children_snapshot(self, session_id: str | None = None, owner_account_id: str = "") -> object:
        team = self.team
        fn = getattr(team, "active_children", None)
        team_snap = fn(session_id, owner_account_id=owner_account_id) if callable(fn) else ([] if session_id else {})

        if self.subagent_active is None:
            return team_snap
        sub_snap = self.subagent_active.snapshot(session_id)

        # 合并 team 与 subagent 的活跃记录（供 gateway/UI 展示）
        if session_id is not None:
            return list(team_snap or []) + list(sub_snap or [])
        merged: dict[str, list] = {k: list(v) for k, v in (team_snap or {}).items()}
        for sid, recs in (sub_snap or {}).items():
            merged.setdefault(sid, []).extend(recs)
        return merged

    async def dispatch(self, envelope: Envelope) -> AsyncIterator[ResponseChunk]:
        """共享调度入口：gateway/cron/平台入口都走同一 SessionDispatcher。"""
        from crew.core.runctx import current_push_fn
        from crew.gateway.channel_sessions import prepare_inbound_channel_envelope

        prepare_inbound_channel_envelope(self, envelope)

        command = str(envelope.params.get("channel_session_command") or "")
        if command:
            yield ResponseChunk.final(
                envelope.request_id,
                "已新建对话，接下来的消息将从空白上下文开始。",
            )
            return

        token = None
        if self._push_payload_fn is not None:
            async def _push_for_owner(session_id: str, payload: dict) -> None:
                await self._push_payload_fn(session_id, payload, owner_account_id=envelope.user_id)

            token = current_push_fn.set(_push_for_owner)
        try:
            async for chunk in self.dispatcher.run(envelope):
                yield chunk
        finally:
            if token is not None:
                current_push_fn.reset(token)

    # ---- 可控性：gateway dispatcher 经此路由到运行中 Agent 的 TurnControl ----
    def steer(self, session_id: str, text: str, owner_account_id: str = "") -> bool:
        """向运行中的会话注入补充指令。无运行中 Agent 则返回 False（dispatcher 降级缓存）。"""
        agent = self.agents.peek(session_id, owner_account_id=owner_account_id)
        fn = getattr(agent, "steer", None)
        if agent is not None and callable(fn):
            try:
                if fn(text):
                    return True
            except Exception:  # noqa: BLE001
                log.exception("steer 失败 session=%s", session_id)
        team_fn = getattr(self.team, "steer", None)
        if callable(team_fn):
            try:
                return bool(team_fn(session_id, text, owner_account_id=owner_account_id))
            except Exception:  # noqa: BLE001
                log.exception("team steer 失败 session=%s", session_id)
        dk_fn = getattr(self.dynamic_kanban, "steer", None)
        if callable(dk_fn):
            try:
                return bool(dk_fn(session_id, text, owner_account_id=owner_account_id))
            except Exception:  # noqa: BLE001
                log.exception("dynamic_kanban steer 失败 session=%s", session_id)
        return False

    def interrupt(self, session_id: str, message: str | None = None, owner_account_id: str = "") -> bool:
        """请求运行中的会话在安全点优雅中断。无运行中 Agent 则返回 False。"""
        agent = self.agents.peek(session_id, owner_account_id=owner_account_id)
        fn = getattr(agent, "interrupt", None)
        interrupted = False
        if agent is not None and callable(fn):
            try:
                fn(message)
                interrupted = True
            except Exception:  # noqa: BLE001
                log.exception("interrupt 失败 session=%s", session_id)
        team_fn = getattr(self.team, "interrupt", None)
        if callable(team_fn):
            try:
                interrupted = bool(team_fn(session_id, message, owner_account_id=owner_account_id)) or interrupted
            except Exception:  # noqa: BLE001
                log.exception("team interrupt 失败 session=%s", session_id)
        dk_fn = getattr(self.dynamic_kanban, "interrupt", None)
        if callable(dk_fn):
            try:
                # DK store 的查询要求 owner scope；不传 owner 会在 _require_owner 抛
                # ValueError，把每一次「停止」都变成一条 ERROR 堆栈。
                interrupted = (
                    bool(dk_fn(session_id, message, owner_account_id=owner_account_id))
                    or interrupted
                )
            except Exception:  # noqa: BLE001
                log.exception("dynamic_kanban interrupt 失败 session=%s", session_id)
        # 级联到该 session 下正在运行的子 agent（父被软中断时阻塞在 await child.run，
        # 看不到自己的标志，故需主动下发；配合子任务超时兜底）。
        if self.subagent_active is not None:
            try:
                interrupted = self.subagent_active.interrupt(session_id, message) or interrupted
            except Exception:  # noqa: BLE001
                log.exception("subagent interrupt 失败 session=%s", session_id)
        return interrupted

    def _current_active_owner_id(self) -> str | None:
        """当前 Active Owner 账号；未登录时返回 None（供后台快照刷新跳过本轮）。"""
        lease = self.active_owner.current()
        return lease.owner_account_id if lease is not None else None

    async def start_cron(self) -> None:
        """Start the cron engine after delivery channels are ready."""
        if self.cron_service is not None:
            try:
                await self.cron_service.start()
            except Exception:  # noqa: BLE001
                log.exception("CronService 启动失败")

    async def startup(self, *, start_cron: bool = True) -> None:
        """拉起后台能力：连接外部 MCP server、启动 cron 引擎、会话过期定时器。失败静默降级。"""
        try:
            counts, backfilled = inspect_and_backfill_legacy_owners(
                self.config.db_path,
                wal_enabled=self.config.sqlite_wal,
            )
            if backfilled:
                log.info("已自动回填 %d 条 legacy cron owner", backfilled)
            for table, count in counts.items():
                if count:
                    log.warning(
                        "存在 %d 条未归属%s，请运行 python -m crew.cli migrate claim-legacy --account <owner-id>",
                        count,
                        OWNER_TABLE_LABELS.get(table, table),
                    )
        except Exception:  # noqa: BLE001
            log.exception("legacy owner 检查失败")
        # 崩溃恢复：按 host PID 重新认领上次未结束的后台进程
        try:
            from crew.tools.process_registry import process_registry

            recovered = process_registry.recover_from_checkpoint()
            if recovered:
                log.info("崩溃恢复：认领 %d 个后台进程", recovered)
        except Exception:  # noqa: BLE001
            log.exception("后台进程崩溃恢复失败")
        await self.tasks.start()
        if self.work_service is not None:
            try:
                await self.work_service.start()
            except Exception:  # noqa: BLE001
                log.exception("WorkService 启动失败")
        if self.browser_manager is not None:
            try:
                await self.browser_manager.startup()
            except Exception:  # noqa: BLE001
                log.exception("BrowserManager 启动失败")
        if self.mcp_manager is not None:
            try:
                # MCP 连接移出 lifespan 关键路径：后台 task 内完成子进程 spawn + 工具注册，
                # 不阻塞 /api/health 就绪。连接完成前调用对应工具返回“连接已断开”错误（守门不崩）。
                await self.mcp_manager.start(self.registry)
            except Exception:  # noqa: BLE001
                log.exception("MCP Client 启动失败")
        if start_cron:
            await self.start_cron()
        if getattr(self, "sites", None) is not None:
            try:
                await self.sites.start()
            except Exception:  # noqa: BLE001
                log.exception("Sites Blueprint 调度器启动失败")
        # 启动会话过期定时器
        if self.config.session_idle_timeout > 0:
            self._expiry_task = asyncio.create_task(self._session_expiry_loop())
            log.info("会话过期定时器已启动: idle_timeout=%d 分钟", self.config.session_idle_timeout)

    def _consumer_tasks_snapshot(self) -> set[asyncio.Task]:
        """Snapshot tasks that may still hold the current App-owned Provider."""
        tasks = self.dispatcher.active_tasks_snapshot()
        tasks.update(task for task in self._subagent_bg_tasks if not task.done())
        team_snapshot = getattr(self.team, "active_tasks_snapshot", None)
        if callable(team_snapshot):
            tasks.update(task for task in team_snapshot() if not task.done())
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if current is not None:
            tasks.discard(current)
        return tasks

    def _schedule_provider_retirement(self, provider: LLMProvider) -> None:
        """Close a replaced App-owned Provider after pre-switch consumers finish."""
        close = getattr(provider, "aclose", None)
        identity = id(provider)
        if not callable(close) or identity in self._retiring_provider_ids:
            return
        self._retiring_provider_ids.add(identity)
        blockers = self._consumer_tasks_snapshot()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._pending_provider_retirements[identity] = (provider, blockers)
            return
        self._start_provider_retirement(loop, provider, blockers)

    def _start_provider_retirement(
        self,
        loop: asyncio.AbstractEventLoop,
        provider: LLMProvider,
        blockers: set[asyncio.Task],
    ) -> None:
        identity = id(provider)
        self._pending_provider_retirements.pop(identity, None)
        task = loop.create_task(self._retire_provider(provider, blockers))
        self._provider_retirement_tasks.add(task)
        task.add_done_callback(self._provider_retirement_tasks.discard)

    async def _retire_provider(
        self,
        provider: LLMProvider,
        blockers: set[asyncio.Task],
    ) -> None:
        identity = id(provider)
        try:
            if blockers:
                await asyncio.gather(*blockers, return_exceptions=True)
            # Agent clear/drop may have scheduled owned-resource close tasks in the same
            # generation. Drain those before the App-owned shared client.
            await self.agents.wait_closed()
            close = getattr(provider, "aclose", None)
            if callable(close):
                await close()
        except Exception:  # noqa: BLE001 - retirement failure must not stop later shutdown
            log.exception("关闭已替换 App Provider 失败: %s", type(provider).__name__)
        finally:
            self._retiring_provider_ids.discard(identity)

    async def _drain_provider_retirements(self) -> None:
        loop = asyncio.get_running_loop()
        for provider, blockers in list(self._pending_provider_retirements.values()):
            self._start_provider_retirement(loop, provider, blockers)
        while self._provider_retirement_tasks:
            await asyncio.gather(
                *list(self._provider_retirement_tasks),
                return_exceptions=True,
            )

    async def _close_current_provider(self) -> None:
        close = getattr(self.provider, "aclose", None)
        if not callable(close):
            return
        try:
            await close()
        except Exception:  # noqa: BLE001 - continue closing remaining App resources
            log.exception("关闭 App-owned Provider 失败: %s", type(self.provider).__name__)

    async def _close_owner_team_providers(self) -> None:
        providers = list({
            **self._stale_owner_team_providers,
            **{id(provider): provider for provider in self._owner_team_providers.values()},
            **{id(provider): provider for provider in self._owner_team_member_model_providers.values()},
            **{id(provider): provider for provider in self._auxiliary_providers},
        }.values())
        self._owner_team_providers.clear()
        self._owner_team_member_model_providers.clear()
        self._stale_owner_team_providers.clear()
        self._auxiliary_providers.clear()
        for provider in providers:
            if provider is self.provider:
                continue
            close = getattr(provider, "aclose", None)
            if not callable(close):
                continue
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001 - continue closing remaining owner resources
                log.exception("关闭辅助 Provider 失败: %s", type(provider).__name__)

    async def _shutdown_provider_resources(self) -> None:
        """Close Agent and Provider resources in ownership order."""
        await self.agents.aclose()
        await self._drain_provider_retirements()
        await self._close_owner_team_providers()
        await self._close_current_provider()

    @staticmethod
    def _consume_late_provider_shutdown(task: asyncio.Task[None]) -> None:
        """Consume a cancellation-resistant Provider shutdown after the budget expires."""
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:  # noqa: BLE001 - the main shutdown has already continued
            log.exception("Provider 资源在 App shutdown 超时后关闭失败")

    async def _shutdown_provider_resources_with_deadline(self, timeout: float) -> None:
        """Bound Provider cleanup without waiting for cancellation-resistant SDKs."""
        effective_timeout = max(0.001, float(timeout))
        cleanup = asyncio.create_task(
            self._shutdown_provider_resources(),
            name="app-provider-shutdown",
        )
        try:
            done, _pending = await asyncio.wait({cleanup}, timeout=effective_timeout)
        except asyncio.CancelledError:
            cleanup.cancel()
            cleanup.add_done_callback(self._consume_late_provider_shutdown)
            raise
        if cleanup in done:
            cleanup.result()
            return

        cleanup.cancel()
        cleanup.add_done_callback(self._consume_late_provider_shutdown)
        log.error(
            "App shutdown 的 Agent/Provider 资源关闭超过 %.3fs，继续关闭其余资源",
            effective_timeout,
        )

    async def shutdown(self, *, timeout: float = 10.0) -> None:
        """Stop consumers and close App resources within a Provider cleanup budget."""
        async with self._shutdown_lock:
            if self._shutdown_complete:
                return
            await self._shutdown_resources(provider_timeout=timeout)
            self._shutdown_complete = True

    async def _shutdown_resources(self, *, provider_timeout: float) -> None:
        """Execute the ordered App shutdown sequence."""
        if self._expiry_task is not None:
            self._expiry_task.cancel()
            try:
                await self._expiry_task
            except asyncio.CancelledError:
                pass
            self._expiry_task = None
        await self.dispatcher.shutdown()
        if self.cron_service is not None:
            await self.cron_service.stop()
        if self.work_service is not None:
            try:
                await self.work_service.stop()
            except Exception:  # noqa: BLE001
                log.exception("WorkService 停止失败")
        if getattr(self, "sites", None) is not None:
            await self.sites.stop()
        # One-shot Subagents own dynamic providers and must finish their finally blocks
        # before AgentManager/global Provider shutdown.
        subagent_tasks = {task for task in self._subagent_bg_tasks if not task.done()}
        for task in subagent_tasks:
            task.cancel()
        if subagent_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*subagent_tasks, return_exceptions=True),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                log.warning("App shutdown 等待 Subagent 退出超时 count=%d", len(subagent_tasks))
        self._subagent_bg_tasks.clear()
        # 关闭异步进化队列，等待后台 evolution 任务完成
        await self._evolution_queue.shutdown()
        team_shutdown = getattr(self.team, "shutdown", None)
        if callable(team_shutdown):
            try:
                await team_shutdown()
            except Exception:  # noqa: BLE001 - 继续释放其他 App-owned 资源
                log.exception("Team shutdown 失败")
        await self._shutdown_provider_resources_with_deadline(provider_timeout)
        if self.mcp_manager is not None:
            await self.mcp_manager.aclose()
        if self.browser_manager is not None:
            await self.browser_manager.aclose()
        await self.tasks.stop()
        bindings = getattr(self, "channel_bindings", None)
        if bindings is not None and hasattr(bindings, "close"):
            bindings.close()
        self.active_owner.close()
        if self.work_service is not None:
            self.work_service.close()
        self.security_rules.close()
        self.security_audit.close()

    async def reload_mcp_manager(self) -> None:
        """热重载 MCPClientManager：关闭现有连接并用当前 config.mcp_servers 重新启动。

        用于一键安装 CUA Driver 等场景：安装完成后把新 MCP server 注册进 Registry。
        注意：这会短暂断开所有已连接的 MCP server。
        """
        if self.mcp_manager is not None:
            try:
                await self.mcp_manager.aclose()
            except Exception:  # noqa: BLE001
                log.exception("关闭旧 MCPClientManager 异常")
            self.mcp_manager = None

        from crew.tools.mcp_client import MCPClientManager

        self.mcp_manager = MCPClientManager(self.config.mcp_servers)
        try:
            await self.mcp_manager.start(self.registry)
        except Exception:  # noqa: BLE001
            log.exception("重新启动 MCPClientManager 失败")
            raise

        # MCPClientManager.start 内部已使用 override=True 注册，通常无需清理；
        # 这里仅做日志记录。
        cua_tools = [name for name in self.registry.names() if name.startswith("cua-driver__")]
        log.info("MCP 热重载完成，当前 cua-driver 工具数: %d", len(cua_tools))

    async def _session_expiry_loop(self) -> None:
        """定时清理空闲过期会话 + 淘汰空闲 Agent 缓存。

        用于 _session_expiry_watcher（每隔 60s 扫描一次）。
        """
        while True:
            try:
                await asyncio.sleep(60.0)
                # 淘汰空闲 Agent 缓存
                evicted_agents = self.agents.evict_idle()
                if evicted_agents:
                    log.info("淘汰空闲 Agent 缓存: %d 个", evicted_agents)
                # 清理过期会话（排除 dispatcher 中正在 running/queued 的会话，
                # 避免 Dynamic Kanban 长任务期间被误删）
                idle_seconds = self.config.session_idle_timeout * 60.0
                runtime = self.dispatcher.runtime_status()
                exclude_sids = {
                    sid
                    for sid, st in runtime.get("sessions", {}).items()
                    if st.get("live") in ("running", "queued")
                }
                expired = self.session_store.expire_idle_sessions(idle_seconds, exclude_sids)
                if expired:
                    log.info("清理过期会话: %d 个（idle_timeout=%d 分钟）",
                             expired, self.config.session_idle_timeout)
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                log.exception("会话过期定时器异常")

    def owner_model_profiles(self, owner_account_id: str = "") -> dict[str, ModelProfile]:
        return self.config.owner_model_profiles(owner_account_id)

    def resolve_session_context_window(
        self,
        session_id: str,
        owner_account_id: str = "",
    ) -> int:
        """会话绑定模型的上下文窗口。

        组合 read_binding + owner_model_profiles 解析出当前会话生效模型的
        context_window；无绑定 / profile 缺失时回退全局 cfg.context_window，再回退 128000。
        供 /api/session/{id}/context 用量显示与 compactor 阈值共用，避免两者读全局窗口。
        """
        from crew.state.session_model import read_binding

        getter = getattr(self.session_store, "get_agent_config", None)
        stored = getter(session_id, owner_account_id=owner_account_id) if callable(getter) else None
        profiles = self.owner_model_profiles(owner_account_id)
        binding = read_binding(
            stored,
            self.config,
            profiles,
            fallback_model_id=self.config.owner_default_model_id(owner_account_id),
        )
        mid = str(binding.get("model_profile_id") or "").strip()
        profile = profiles.get(mid) if mid else None
        cw = profile.context_window if profile is not None else None
        return int(cw or self.config.context_window or 128000)

    def owner_active_model_profile(self, owner_account_id: str = "") -> ModelProfile:
        profile = self.config.owner_active_model_profile(owner_account_id)
        if profile is not None:
            return profile
        return self.config.active_model

    def owner_default_model_profile(self, owner_account_id: str = "") -> ModelProfile:
        """返回 owner 默认兜底模型；active 命名仅作为旧 API 兼容保留。"""
        return self.owner_active_model_profile(owner_account_id)

    def owner_team_provider(self, owner_account_id: str = "") -> LLMProvider:
        """Return the owner-default Provider used by Team planning and built-in members.

        Team Agents survive across HTTP/WS requests, so their Provider cannot use the
        request-scoped ``owner_provider`` context manager.  The App owns one cached
        client per owner and retires it when that owner changes the default model.
        Session-level model bindings are intentionally ignored here.
        """
        owner = str(owner_account_id or "").strip()
        if not owner:
            return self.provider
        cached = self._owner_team_providers.get(owner)
        if cached is not None:
            return cached
        profile = self.config.owner_default_model_profile(owner)
        if profile is None or not profile.has_key:
            return self.provider
        provider = build_provider_for_profile(profile, self.config.stream_read_timeout)
        self._owner_team_providers[owner] = provider
        return provider

    def owner_team_member_model_provider(
        self,
        owner_account_id: str = "",
        model_profile_id: str = "",
    ) -> LLMProvider:
        """Return the App-owned Provider for one explicitly bound Team member."""
        owner = str(owner_account_id or "").strip()
        model_id = str(model_profile_id or "").strip()
        if not model_id or model_id == self.config.owner_default_model_id(owner):
            return self.owner_team_provider(owner)
        profiles = self.owner_model_profiles(owner) if owner else self.config.model_profiles
        profile = profiles.get(model_id)
        if profile is None or not profile.loaded or not profile.has_key:
            return self.owner_team_provider(owner)
        key = (owner, model_id)
        cached = self._owner_team_member_model_providers.get(key)
        if cached is not None:
            return cached
        provider = build_provider_for_profile(profile, self.config.stream_read_timeout)
        self._owner_team_member_model_providers[key] = provider
        return provider

    def _invalidate_owner_team_provider(self, owner_account_id: str = "") -> None:
        owner = str(owner_account_id or "").strip()
        if owner:
            provider = self._owner_team_providers.pop(owner, None)
            if provider is not None and provider is not self.provider:
                self._stale_owner_team_providers[id(provider)] = provider
            for key in [key for key in self._owner_team_member_model_providers if key[0] == owner]:
                provider = self._owner_team_member_model_providers.pop(key)
                if provider is not self.provider:
                    self._stale_owner_team_providers[id(provider)] = provider
            drop = getattr(self.team, "drop_owner_teams", None)
            if callable(drop):
                drop(owner)
            drop_kanban = getattr(self.dynamic_kanban, "drop_owner_provider_state", None)
            if callable(drop_kanban):
                drop_kanban(owner)
            return

        providers = list({
            id(provider): provider
            for provider in [
                *self._owner_team_providers.values(),
                *self._owner_team_member_model_providers.values(),
            ]
        }.values())
        self._owner_team_providers.clear()
        self._owner_team_member_model_providers.clear()
        for provider in providers:
            if provider is not self.provider:
                self._stale_owner_team_providers[id(provider)] = provider
        clear = getattr(self.team, "clear", None)
        if callable(clear):
            clear()
        clear_kanban = getattr(self.dynamic_kanban, "clear_provider_state", None)
        if callable(clear_kanban):
            clear_kanban()

    @asynccontextmanager
    async def owner_provider(
        self,
        owner_account_id: str = "",
    ) -> AsyncIterator[LLMProvider]:
        """Yield the owner-default Provider for a request-scoped auxiliary task.

        Owner-local model profiles need their own client because their API Key is
        intentionally isolated from the process-wide environment.  A profile
        without a Key follows the same fallback rule as normal conversations and
        borrows the App Provider.  Dynamically created clients are closed when the
        request or response stream finishes.
        """

        owner = str(owner_account_id or "").strip()
        profile = self.config.owner_default_model_profile(owner) if owner else None
        owns_provider = bool(owner and profile is not None and profile.has_key)
        provider = (
            build_provider_for_profile(profile, self.config.stream_read_timeout)
            if owns_provider and profile is not None
            else self.provider
        )
        try:
            yield provider
        finally:
            if owns_provider:
                close = getattr(provider, "aclose", None)
                if callable(close):
                    try:
                        result = close()
                        if inspect.isawaitable(result):
                            await result
                    except Exception:  # noqa: BLE001 - request cleanup must not mask its result
                        log.exception(
                            "关闭 owner Provider 失败 owner=%s provider=%s",
                            owner,
                            type(provider).__name__,
                        )

    def owner_public_model_options(self, owner_account_id: str = "") -> list[dict[str, Any]]:
        return self.config.owner_public_model_options(owner_account_id)

    def owner_visible_model_profiles(
        self,
        owner_account_id: str = "",
        *,
        include_builtin_profiles: bool = True,
    ) -> list[ModelProfile]:
        return self.config.owner_visible_model_profiles(
            owner_account_id,
            include_builtin_profiles=include_builtin_profiles,
        )

    def use_model(self, model_id: str, *, owner_account_id: str = "") -> ModelProfile:
        """设置默认兜底模型；Session 的显式模型绑定保持不变。"""
        owner = str(owner_account_id or "").strip()
        if owner:
            profiles = self.owner_model_profiles(owner)
            profile = profiles.get(model_id)
            if profile is None:
                raise KeyError(model_id)
            if not profile.loaded:
                raise ValueError(f"模型未加载，不能用于对话: {model_id}")
            self.config.persist_owner_model_profiles(owner, profiles, active_model_id=model_id)
            self.agents.drop_owner(owner)
            self._invalidate_owner_team_provider(owner)
        else:
            profile = self.config.activate_model(model_id)
            self.config.default_model_id = model_id
            if self.config.config_path:
                self.config.persist_model_profiles()
            old_provider = self.provider
            self.provider = build_provider(self.config)
            self.agents.clear()
            self._invalidate_owner_team_provider()
            if self.team is not None:
                self.team.provider = self.provider
            if self.dynamic_kanban is not None:
                self.dynamic_kanban.provider = self.provider
                if hasattr(self.dynamic_kanban, "clear"):
                    self.dynamic_kanban.clear()
            if old_provider is not self.provider:
                self._schedule_provider_retirement(old_provider)
        log.info("设置默认模型: %s model=%s base_url=%s", profile.id, profile.model, profile.base_url or "默认")
        return profile

    # ---- 模型 profile CRUD（运行时增删改 + 持久化 + Provider 同步）----
    #
    # 与 use_model 共享副作用路径：写 yaml → 写 env（仅当传 api_key）→ 改 cfg →
    # 激活模型变动时重建 Provider + 清缓存。CRUD 是配置层动作，不切换激活模型，
    # 但删除激活模型时自动切到剩余的第一个（按用户决策）。
    def _apply_api_key_to_env(
        self,
        api_key_env: str,
        api_key: str,
        *,
        owner_account_id: str = "",
    ) -> str:
        """把用户填写的 api_key 写入 .env，返回实际 env 文件路径。

        - api_key_env 必须是合法模型 API Key 环境变量名；非法时抛 ValueError。
        - owner 私有 key 只写 owner .env，不再同步全局进程环境，避免跨账号串线。
        """
        api_key_env = _validate_model_api_key_env(api_key_env)
        env_path = resolve_writable_env_path(owner_account_id)
        write_env_key(env_path, api_key_env, api_key, sync_process_env=not bool(owner_account_id))
        log.info("已写入 API Key 到 .env (var=%s, file=%s)", api_key_env, env_path)
        return str(env_path)

    def add_model(self, payload: dict, *, owner_account_id: str = "") -> ModelProfile:
        """新增模型 profile 并持久化。

        Args:
            payload: {
                id: 必填,
                name, base_url, model, api_key_env, temperature,
                max_tokens, context_window, timeout: 选填,
                api_key: 选填明文 key，提供则写入 .env
            }

        Raises:
            ValueError: id 缺失/重复；env 写入失败；yaml 写回失败。
        """
        cfg = self.config
        model_id = str(payload.get("id") or "").strip()
        if not model_id:
            raise ValueError("model id 不能为空")
        owner = str(owner_account_id or "").strip()
        existing = self.owner_model_profiles(owner) if owner else cfg.model_profiles
        if model_id in existing:
            raise ValueError(f"模型 id 已存在: {model_id}")
        current_default_id = cfg.owner_active_model_id(owner) if owner else cfg.active_model_id
        current_default_is_placeholder = is_placeholder_model_profile(existing.get(current_default_id))

        api_key_env = _validate_model_api_key_env(payload.get("api_key_env") or "CREW_API_KEY")
        payload = {**payload, "api_key_env": api_key_env, "builtin": False}
        api_key = str(payload.get("api_key") or "")
        # 先写 env（让 _build_profile_from_payload 能从 os.environ 取到），再构建 profile
        if api_key:
            self._apply_api_key_to_env(
                api_key_env,
                api_key,
                owner_account_id=owner_account_id,
            )
        if owner:
            profile = _build_profile_from_payload(model_id, payload)
            profile.api_key = api_key or cfg.owner_env_map(owner).get(api_key_env, "")
            profiles = cfg.owner_model_profiles(owner)
            profiles[model_id] = profile
            activate_new_model = bool(
                current_default_is_placeholder
                and profile.loaded
                and profile.has_key
                and not is_placeholder_model_profile(profile)
            )
            next_default_id = model_id if activate_new_model else cfg.owner_active_model_id(owner)
            cfg.persist_owner_model_profiles(owner, profiles, active_model_id=next_default_id)
            self.agents.drop_owner(owner)
            if activate_new_model:
                self._invalidate_owner_team_provider(owner)
                log.info("首个可用模型已自动设为 owner 默认模型: %s", model_id)
        else:
            profile = cfg.add_model(payload)
            cfg.persist_model_profiles()
        log.info("新增模型 profile: %s (model=%s)", profile.id, profile.model)
        return profile

    def update_model(self, model_id: str, payload: dict, *, owner_account_id: str = "") -> ModelProfile:
        """更新已存在的模型 profile 并持久化。

        若更新目标是当前激活模型，会重建 Provider 并清空 Agent 缓存（与 use_model 对齐）。

        Raises:
            KeyError: model_id 不存在。
            ValueError: env 写入失败；yaml 写回失败。
        """
        cfg = self.config
        owner = str(owner_account_id or "").strip()
        profiles = self.owner_model_profiles(owner) if owner else cfg.model_profiles
        if model_id not in profiles:
            raise KeyError(model_id)
        if owner and profiles[model_id].builtin:
            owner = ""
            profiles = cfg.model_profiles
        loaded_val = payload.get("loaded")
        active_model_id = cfg.owner_active_model_id(owner) if owner else cfg.active_model_id
        if active_model_id == model_id and loaded_val is not None and not _payload_bool(loaded_val):
            raise ValueError("当前激活模型不能设为未加载，请先切换到其它已加载模型")

        api_key_env = _validate_model_api_key_env(
            payload.get("api_key_env") or profiles[model_id].api_key_env
        )
        if "api_key_env" in payload:
            payload = {**payload, "api_key_env": api_key_env}
        payload = {**payload, "builtin": profiles[model_id].builtin}
        api_key = str(payload.get("api_key") or "")
        if api_key:
            self._apply_api_key_to_env(
                api_key_env,
                api_key,
                owner_account_id=owner_account_id,
            )
        if owner:
            current = profiles[model_id]
            merged = {
                "id": model_id,
                "name": payload.get("name", current.name),
                "api_key_env": payload.get("api_key_env", current.api_key_env),
                "provider": payload.get("provider", current.provider),
                "base_url": payload.get("base_url", current.base_url),
                "model": payload.get("model", current.model),
                "temperature": payload.get("temperature", current.temperature),
                "max_tokens": payload.get("max_tokens", current.max_tokens),
                "context_window": payload.get("context_window", current.context_window),
                "timeout": payload.get("timeout", current.timeout),
                "loaded": payload.get("loaded", current.loaded),
                "builtin": current.builtin,
                "capabilities": payload.get("capabilities", list(current.capabilities)),
            }
            profile = _build_profile_from_payload(model_id, merged)
            if api_key:
                profile.api_key = api_key
            else:
                profile.api_key = cfg.owner_env_map(owner).get(profile.api_key_env, "")
            profiles[model_id] = profile
            cfg.persist_owner_model_profiles(owner, profiles, active_model_id=cfg.owner_active_model_id(owner))
            self.agents.drop_owner(owner)
            # Team 内置成员可以显式绑定非默认 profile；更新任意 profile
            # 都要淘汰对应 Team/Provider 缓存，下一轮才会使用新配置。
            self._invalidate_owner_team_provider(owner)
        else:
            profile = cfg.update_model(model_id, payload)
            cfg.persist_model_profiles()
            # Session cache keys contain the selected profile id, not the
            # mutable profile contents. Clear even for a non-active shared
            # model because existing sessions may be explicitly bound to it.
            self.agents.clear()
            self._invalidate_owner_team_provider()

        # 激活模型变更 → 重建 Provider + 清缓存，让下一轮对话立即生效
        if active_model_id == model_id and not owner:
            cfg.activate_model(model_id)
            old_provider = self.provider
            self.provider = build_provider(cfg)
            if self.team is not None:
                self.team.provider = self.provider
            if self.dynamic_kanban is not None:
                self.dynamic_kanban.provider = self.provider
                if hasattr(self.dynamic_kanban, "clear"):
                    self.dynamic_kanban.clear()
            if old_provider is not self.provider:
                self._schedule_provider_retirement(old_provider)
        log.info("更新模型 profile: %s (model=%s)", profile.id, profile.model)
        return profile

    def remove_model(self, model_id: str, *, owner_account_id: str = "") -> dict:
        """删除模型 profile 并持久化。

        - 删除激活模型时：自动切到剩余的第一个 profile，重建 Provider + 清缓存。
        - 删除最后一个模型时：抛 ValueError（由 gateway 层返回 409）。

        Returns:
            {"removed": <ModelProfile>, "switched_to": <new_active_id or None>}
        """
        cfg = self.config
        owner = str(owner_account_id or "").strip()
        profiles = self.owner_model_profiles(owner) if owner else cfg.model_profiles
        if model_id not in profiles:
            raise KeyError(model_id)
        if owner and profiles[model_id].builtin:
            owner = ""
            profiles = cfg.model_profiles
        if len(profiles) <= 1:
            raise ValueError("至少保留一个模型配置，禁止删除最后一个")
        active_model_id = cfg.owner_active_model_id(owner) if owner else cfg.active_model_id
        if active_model_id == model_id:
            loaded_replacements = [
                mid
                for mid, profile in profiles.items()
                if mid != model_id and profile.loaded
            ]
            if not loaded_replacements:
                raise ValueError("删除当前激活模型前，至少需要另一个已加载模型")

        if owner:
            removed = profiles.pop(model_id)
            if not any(profile.api_key_env == removed.api_key_env for profile in profiles.values()):
                remove_env_key(resolve_writable_env_path(owner_account_id), removed.api_key_env, sync_process_env=False)
        else:
            removed = cfg.remove_model(model_id)  # 内部校验"最后一个"
            if not any(profile.api_key_env == removed.api_key_env for profile in cfg.model_profiles.values()):
                remove_env_key(resolve_writable_env_path(owner_account_id), removed.api_key_env)
        switched_to: str | None = None
        if active_model_id == model_id:
            # 按 id 字典序切到剩余的第一个，行为可预测
            new_id = sorted(mid for mid, profile in profiles.items() if profile.loaded)[0]
            switched_to = new_id
            if owner:
                cfg.persist_owner_model_profiles(owner, profiles, active_model_id=new_id)
                self._invalidate_owner_team_provider(owner)
            else:
                cfg.activate_model(new_id)
                cfg.default_model_id = new_id
                old_provider = self.provider
                self.provider = build_provider(cfg)
                self.agents.clear()
                self._invalidate_owner_team_provider()
                if self.team is not None:
                    self.team.provider = self.provider
                if self.dynamic_kanban is not None:
                    self.dynamic_kanban.provider = self.provider
                    if hasattr(self.dynamic_kanban, "clear"):
                        self.dynamic_kanban.clear()
                if old_provider is not self.provider:
                    self._schedule_provider_retirement(old_provider)
        elif owner:
            cfg.persist_owner_model_profiles(owner, profiles, active_model_id=cfg.owner_active_model_id(owner))
            self._invalidate_owner_team_provider(owner)
        else:
            cfg.persist_model_profiles()
            self._invalidate_owner_team_provider()
        # Cache keys only contain the selected profile id, not mutable profile contents.
        # A deleted non-active profile can still be pinned by an existing session.
        if owner:
            self.agents.drop_owner(owner)
        else:
            self.agents.clear()
        log.info(
            "删除模型 profile: %s%s",
            removed.id,
            f"，自动切换激活到 {switched_to}" if switched_to else "",
        )
        return {"removed": removed, "switched_to": switched_to}

    def promote_pending_model_if_idle(
        self,
        session_id: str,
        owner_account_id: str = "",
        *,
        queue_depth: int = 0,
        running_depth: int = 0,
    ) -> bool:
        """session 完全 idle 后将 pending 模型提升为正式绑定。"""
        if queue_depth > 0 or running_depth > 0:
            return False
        from crew.state.session_model import promote_pending_session_model

        if promote_pending_session_model(
            self.session_store,
            self.config,
            self.owner_model_profiles(owner_account_id),
            session_id,
            owner_account_id=owner_account_id,
            fallback_model_id=self.config.owner_default_model_id(owner_account_id),
        ):
            self.agents.drop(session_id, owner_account_id=owner_account_id)
            return True
        return False

    def set_session_model_binding(
        self,
        session_id: str,
        model_profile_id: str,
        owner_account_id: str = "",
        *,
        busy: bool,
    ) -> dict:
        """Composer / 桌面端：为单会话设置模型（busy 时仅 pending）。"""
        from crew.state.session_model import read_binding, set_session_model

        stored = set_session_model(
            self.session_store,
            self.config,
            self.owner_model_profiles(owner_account_id),
            session_id,
            model_profile_id,
            owner_account_id=owner_account_id,
            busy=busy,
            fallback_model_id=self.config.owner_default_model_id(owner_account_id),
        )
        if not busy:
            self.agents.drop(session_id, owner_account_id=owner_account_id)
        binding = read_binding(
            stored,
            self.config,
            self.owner_model_profiles(owner_account_id),
            fallback_model_id=self.config.owner_default_model_id(owner_account_id),
        )
        return {
            "ok": True,
            "pending": busy or binding.get("has_pending"),
            **binding,
        }

    def read_session_model_binding(
        self,
        session_id: str,
        owner_account_id: str = "",
    ) -> dict:
        from crew.state.session_model import read_binding

        getter = getattr(self.session_store, "get_agent_config", None)
        stored = getter(session_id, owner_account_id=owner_account_id) if callable(getter) else None
        binding = read_binding(
            stored,
            self.config,
            self.owner_model_profiles(owner_account_id),
            fallback_model_id=self.config.owner_default_model_id(owner_account_id),
        )
        return {"ok": True, **binding}

    def _enrich_workspace(self, envelope: Envelope) -> None:
        """把工作空间的 instructions 注入到 envelope.params，供内核构建 system prompt。"""
        try:
            if "workspace_instructions" not in envelope.params:
                ws = self.workspace_store.get(envelope.workspace_id, owner_account_id=envelope.user_id)
                envelope.params["workspace_instructions"] = ws.get("instructions", "")
                envelope.params["workspace_root_path"] = ws.get("root_path", "") or ""
            workspace_root = str(envelope.params.get("workspace_root_path") or "").strip()
            if not workspace_root:
                from crew.state.home import task_workspace_path

                workspace_root = str(
                    task_workspace_path(
                        envelope.workspace_id,
                        owner_account_id=envelope.user_id,
                    )
                )
            from crew.gateway.context import resolve_structured_path_references

            referenced_paths = resolve_structured_path_references(
                envelope.query,
                workspace_root=workspace_root,
            )
            if referenced_paths:
                envelope.params["referenced_paths"] = referenced_paths
        except Exception:  # noqa: BLE001 - 空间不存在则忽略
            envelope.params["workspace_instructions"] = ""

    def _on_subagent_background_done(self, session_id: str, result: dict) -> None:
        """后台子 agent 完成回调（在事件循环内同步调用）：入队 + 实时推送。"""
        # 1) 入队，供下一轮 handle() 注入主 agent 上下文（限长，防无限堆积）
        key = self._owner_session_key(session_id, str(result.get("owner_account_id") or ""))
        queue = self._subagent_pending.setdefault(key, [])
        queue.append(result)
        if len(queue) > 20:
            del queue[:-20]
        # 2) 实时推送（best-effort，有活跃 WS 时；push_fn 为协程，fire-and-forget）
        if self._push_fn is not None:
            label = result.get("agent", "子智能体")
            status = result.get("status", "")
            msg = f"后台子智能体 {label} 已完成（{status}），task_id={result.get('task_id', '')}"
            try:
                fut = self._push_fn(
                    session_id,
                    ResponseChunk.status_event("", msg),
                    owner_account_id=str(result.get("owner_account_id") or ""),
                )
                if asyncio.iscoroutine(fut):
                    asyncio.ensure_future(fut)
            except RuntimeError:
                pass  # 无运行中事件循环则跳过

    @staticmethod
    def _owner_session_key(session_id: str, owner_account_id: str = "") -> OwnerSessionKey:
        return owner_account_id or "", session_id

    def _on_subagent_collected(self, session_id: str, task_id: str, owner_account_id: str = "") -> None:
        """模型已主动 collect 取走某后台结果 → 从待注入队列移除，避免下一轮重复注入。"""
        key = self._owner_session_key(session_id, owner_account_id)
        queue = self._subagent_pending.get(key)
        if not queue:
            return
        remaining = [r for r in queue if r.get("task_id") != task_id]
        if remaining:
            self._subagent_pending[key] = remaining
        else:
            self._subagent_pending.pop(key, None)

    def _drain_subagent_notifications(self, envelope: Envelope) -> None:
        """把该 session 待通知的后台子任务结果取出，注入 envelope.params 供内核注入上下文。"""
        pending = self.pop_subagent_notifications(envelope.session_id, envelope.user_id)
        if pending:
            envelope.params["subagent_notifications"] = pending

    def pop_subagent_notifications(self, session_id: str, owner_account_id: str = "") -> list:
        """取出并清空该 session 待通知的后台子任务结果。

        供 team member 派活前 drain：让发起 delegate_task/run_agent 后台的 member
        在下一轮执行时看到完成通知（team 模式下 member.run 不经 app.handle 的 drain）。
        """
        return self._subagent_pending.pop(
            self._owner_session_key(session_id, owner_account_id),
            None,
        ) or []

    def _drain_process_notifications(self, envelope: Envelope) -> None:
        """把该 session 后台进程的 watch/完成通知取出，注入 envelope.params。"""
        from crew.tools.process_registry import process_registry

        pending = process_registry.drain_for_session(envelope.session_id, owner_account_id=envelope.user_id)
        if pending:
            envelope.params["process_notifications"] = pending

    async def handle(self, envelope: Envelope) -> AsyncIterator[ResponseChunk]:
        """统一入口：按 mode 路由。"""
        from crew.core.runctx import current_push_fn

        token = None
        if self._push_payload_fn is not None:
            async def _push_for_owner(session_id: str, payload: dict) -> None:
                await self._push_payload_fn(session_id, payload, owner_account_id=envelope.user_id)

            token = current_push_fn.set(_push_for_owner)
        try:
            self._enrich_workspace(envelope)
            from crew.security.context import build_gateway_security_context
            from crew.security.launch import compile_process_launch

            security_context = build_gateway_security_context(
                self.workspace_store,
                owner_account_id=envelope.user_id,
                workspace_id=envelope.workspace_id,
                session_id=envelope.session_id,
                task_id=str(envelope.params.get("task_id") or ""),
                request_id=envelope.request_id,
                cwd=envelope.params.get("workspace_root_path") or None,
            )
            envelope.params["_security_process_launch"] = compile_process_launch(
                security_context,
                self.security_service.mode_for(security_context),
                db_path=self.security_service.db_path,
                audit=self.security_service.audit,
            )
            config_session_id = str(envelope.params.get("task_session_id") or envelope.session_id)
            if not getattr(self.config, "external_agents_enabled", True):
                agent_config = self._session_agent_config(
                    config_session_id,
                    owner_account_id=envelope.user_id,
                )
                if (
                    self._uses_external_agents_feature(agent_config)
                    or bool(str(envelope.params.get("external_team_id") or "").strip())
                ):
                    yield ResponseChunk.error(
                        envelope.request_id,
                        "外部智能体功能已在配置中关闭",
                    )
                    return
            self._drain_subagent_notifications(envelope)
            self._drain_process_notifications(envelope)
            if envelope.mode == "team":
                if self.team is None:
                    yield ResponseChunk.error(envelope.request_id, "Team 模式未启用")
                    return
                if not envelope.params.get("external_team_id"):
                    # 必须带 owner：session_agent_config 按账号隔离，漏传会读到空/错配置
                    config = self._session_agent_config(
                        envelope.session_id,
                        owner_account_id=envelope.user_id,
                    )
                    team_cfg = config.get("team") if isinstance(config.get("team"), dict) else {}
                    external_team_id = str(team_cfg.get("external_team_id") or "").strip()
                    if external_team_id:
                        envelope.params["external_team_id"] = external_team_id
                async for chunk in self.team.interact(envelope):
                    yield chunk
                return

            if envelope.mode == "dynamic_kanban":
                if self.dynamic_kanban is None:
                    yield ResponseChunk.error(envelope.request_id, "Dynamic Kanban 模式未启用")
                    return
                async for chunk in self.dynamic_kanban.interact(envelope):
                    yield chunk
                return

            async with self.agents.lease(
                envelope.session_id,
                self._session_agent_config(config_session_id, owner_account_id=envelope.user_id),
                owner_account_id=envelope.user_id,
            ) as agent:
                async for chunk in agent.run(envelope):
                    yield chunk
        finally:
            if token is not None:
                current_push_fn.reset(token)


def build_provider_for_profile(profile: ModelProfile, stream_read_timeout: float | None = None) -> LLMProvider:
    """按指定 ModelProfile 直接创建 Provider（fallback 用，不改全局激活模型）。"""
    provider_cls = _provider_class(profile.provider)
    return provider_cls(
        api_key=profile.api_key,
        base_url=profile.base_url or None,
        model=profile.model,
        temperature=profile.temperature,
        max_tokens=profile.max_tokens,
        timeout=stream_read_timeout if stream_read_timeout is not None else profile.timeout,
        vision=profile.vision,
    )


def build_provider(cfg: Config) -> LLMProvider:
    """按当前激活模型配置创建 Provider。"""
    if cfg.has_llm_key:
        provider_cls = _provider_class(cfg.provider)
        provider: LLMProvider = provider_cls(
            api_key=cfg.api_key,
            base_url=cfg.base_url or None,
            model=cfg.model,
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            timeout=cfg.stream_read_timeout,
            vision=cfg.vision,
        )
        log.info("使用 %s Provider，profile=%s model=%s base_url=%s", cfg.provider, cfg.active_model_id, cfg.model, cfg.base_url or "默认")
        return provider

    from crew.core.mocks import FakeProvider

    log.warning("模型配置 %s 未配置 API Key，回退 FakeProvider（仅演示链路，不调真实模型）", cfg.active_model_id)
    return FakeProvider()


def _provider_class(provider: str):
    name = str(provider or "openai").strip().lower()
    if name == "openai":
        return OpenAIProvider
    if name == "anthropic":
        return AnthropicProvider
    raise ValueError(f"未知模型 provider: {provider}")


def _browser_manager_from_plugins(plugins: PluginManager):
    """从已加载的 browser 插件取回 BrowserManager；未加载/加载失败返回 None。"""
    loaded = plugins.get_plugin("browser")
    if loaded is None or not loaded.enabled or loaded.manifest.path is None:
        return None
    module_key = (loaded.manifest.key or loaded.manifest.name).replace("/", "_").replace("-", "_")
    module = sys.modules.get(f"crew_runtime_plugins.{module_key}")
    manager = getattr(module, "manager", None) if module is not None else None
    if manager is None:
        log.warning("browser 插件已加载但未暴露 BrowserManager")
    return manager


def build_app(config: Config | None = None, *, enable_team: bool = True) -> CrewApp:
    """工厂：从配置构建一个 CrewApp。"""
    cfg = config or load_config()
    setup_logging(cfg.log_level, cfg.log_file, llm_trace=cfg.llm_trace)
    if cfg.gateway_dev_mode:
        log.warning(
            "gateway.dev_mode 开启：开发账号 %s 放行 loopback 并自动 admin，切勿用于生产",
            cfg.gateway_dev_account,
        )

    # 初始化 Crew Home 目录（.crew/），确保 SOUL.md / MEMORY.md / USER.md 存在
    ensure_crew_home()

    provider = build_provider(cfg)

    # 解析访问控制配置（全局）
    ac = cfg.access_control.resolve_for()

    registry = Registry()

    session_store = SQLiteSessionStore(cfg.db_path)
    workspace_store = SQLiteWorkspaceStore(cfg.db_path)
    from crew.state.channel_bindings import ChannelBindingsStore

    channel_bindings = ChannelBindingsStore(cfg.db_path, wal_enabled=cfg.sqlite_wal)
    external_agents = ExternalAgentStore(cfg.db_path)
    from crew.state.plugin_preferences import PluginPreferencesStore

    plugin_prefs = PluginPreferencesStore(cfg.db_path, wal_enabled=cfg.sqlite_wal)
    memory = SQLiteMemory(
        db_path=cfg.memory_db_path,
        wal_enabled=cfg.sqlite_wal,
    )
    log.info("memory.db 路径: %s", cfg.memory_db_path)
    plugins = PluginManager(
        [LoggingPlugin()],
        registry=registry,
        services={"config": cfg, "plugin_prefs": plugin_prefs},
    )

    # 配置全局 skill 过滤器
    configure_skill_filter(
        enabled=ac.get("enabled_skills"),
        disabled=ac.get("disabled_skills"),
    )
    plugins.discover_and_load(enabled=cfg.plugins_enabled, disabled=cfg.plugins_disabled)
    from crew.agent.skills import configure_plugin_skill_roots

    # Skill 扫描纳入已加载插件的 skills/ 根（动态取值：插件卸载后下轮不再出现）
    configure_plugin_skill_roots(plugins.plugin_skill_roots)
    from crew.gateway.platform_registry import platform_registry

    cfg.apply_platform_config_bridges(platform_registry.all_entries())

    app = CrewApp(cfg, provider, registry, session_store, workspace_store, memory, plugins)
    register_builtin_tools(
        registry,
        workspace_store=workspace_store,
        security_service=app.security_service,
    )
    from crew.sites import SQLiteSiteStore, SiteManager
    from crew.tools.blueprint_tools import register_blueprint_tools
    from crew.tools.site_tools import register_site_tools

    app.sites = SiteManager(SQLiteSiteStore(cfg.db_path, wal_enabled=cfg.sqlite_wal))
    register_site_tools(registry, app.sites)
    register_blueprint_tools(registry, app.sites)
    # Browser 能力由 plugins/browser 插件装配（创建 BrowserManager、注册 browser_use）。
    # 系统级禁用/未加载时保持 None，面板路由与 startup/aclose 已有 None 兜底。
    app.browser_manager = _browser_manager_from_plugins(plugins)
    app.channel_bindings = channel_bindings
    app.plugin_prefs = plugin_prefs
    app.external_agents = external_agents
    from crew.gateway.channel_sessions import register_channel_session_tools

    register_channel_session_tools(registry, session_store)
    register_external_agent_tools(
        registry,
        external_agents,
        interaction_bridge_getter=lambda: app.interaction_bridge,
    )
    from crew.tasks.tools import register_task_tools

    register_task_tools(registry, app.tasks)

    # 对话级 Plan 模式：状态机 + enter_plan_mode / exit_plan_mode / todo 工具
    from crew.agent.plan import PlanModeManager, register_plan_tools

    app.plan_manager = PlanModeManager(session_store=session_store)
    register_plan_tools(registry, app.plan_manager)

    # Wiki：存储 + 编译器 + 查询器 + 摘要器 + 专用会话状态 + 工具
    from crew.wiki import (
        FileSystemWikiStore,
        WikiCompiler,
        WikiSessionManager,
        WikiQuerier,
        WikiSummarizer,
    )
    from crew.wiki.tools import register_wiki_tools
    wiki_storage_root = cfg.wiki.storage.resolved_root() if cfg.wiki else None
    app._wiki_store = FileSystemWikiStore(storage_root=wiki_storage_root)
    if wiki_storage_root is not None:
        log.info("Wiki 独立存储根目录: %s", wiki_storage_root)
    app.wiki_manager = WikiSessionManager(store=app._wiki_store)
    # wiki 编译/摘要可用 wiki.model 指定独立模型档案（如更快的 flash 模型），
    # 默认跟随当前 owner 的默认模型。推理型主模型对长 JSON 输出会产生数分钟推理，
    # 导致编译在"LLM 分析"阶段卡死（实测 minimax-latest 3 次全部超时）。
    wiki_provider = provider
    wiki_provider_is_explicit = False
    wiki_model_id = (cfg.wiki.model or "").strip() if cfg.wiki else ""
    if wiki_model_id:
        wiki_profile = cfg.model_profiles.get(wiki_model_id)
        if wiki_profile is not None and wiki_profile.api_key:
            wiki_provider = build_provider_for_profile(wiki_profile, cfg.stream_read_timeout)
            wiki_provider_is_explicit = True
            app._auxiliary_providers.append(wiki_provider)
            log.info("Wiki 使用独立模型 profile=%s model=%s", wiki_model_id, wiki_profile.model)
        else:
            log.warning("wiki.model=%s 未找到可用模型档案，回退 owner 默认模型", wiki_model_id)
    wiki_owner_provider = None if wiki_provider_is_explicit else app.owner_team_provider
    app._wiki_summarizer = WikiSummarizer(
        app._wiki_store,
        wiki_provider,
        provider_for_owner=wiki_owner_provider,
    )
    app._wiki_compiler = WikiCompiler(
        app._wiki_store,
        wiki_provider,
        summarizer=app._wiki_summarizer,
        provider_for_owner=wiki_owner_provider,
    )
    app._wiki_querier = WikiQuerier(app._wiki_store)
    register_wiki_tools(
        registry,
        app._wiki_store,
        app._wiki_compiler,
        app._wiki_querier,
        app.wiki_manager,
        config=cfg.wiki,
        session_store=session_store,
    )

    from crew.work.briefs import WorkBriefStore
    from crew.work.items import WorkItemStore
    from crew.work.knowledge import WorkKnowledgeStore
    from crew.work.preferences import WorkPreferenceStore
    from crew.work.references import WorkReferenceStore
    from crew.work.service import LLMPreferenceExtractor, WorkService
    from crew.work.settings import WorkSettingsStore
    from crew.work.sources import WorkSourceStore
    from crew.work.templates import WorkTemplateStore

    async def _notify_work_owner(owner_account_id: str, payload: dict[str, Any]) -> None:
        if app._notify_owner_fn is not None:
            await app._notify_owner_fn(owner_account_id, payload)

    app.work_service = WorkService(
        references=WorkReferenceStore(
            cfg.db_path,
            session_store=session_store,
            wal_enabled=cfg.sqlite_wal,
        ),
        preferences=WorkPreferenceStore(cfg.db_path, wal_enabled=cfg.sqlite_wal),
        items=WorkItemStore(cfg.db_path, wal_enabled=cfg.sqlite_wal),
        sources=WorkSourceStore(
            cfg.db_path,
            approved_source_keys=set(),
            adapters={},
            wal_enabled=cfg.sqlite_wal,
        ),
        briefs=WorkBriefStore(cfg.db_path, wal_enabled=cfg.sqlite_wal),
        settings=WorkSettingsStore(
            cfg.db_path,
            workspace_store=workspace_store,
            wal_enabled=cfg.sqlite_wal,
        ),
        templates=WorkTemplateStore(cfg.db_path, wal_enabled=cfg.sqlite_wal),
        knowledge=WorkKnowledgeStore(
            cfg.db_path,
            wiki_store=app._wiki_store,
            wal_enabled=cfg.sqlite_wal,
        ),
        session_store=session_store,
        workspace_store=workspace_store,
        preference_extractor=LLMPreferenceExtractor(provider),
        preference_notifier=_notify_work_owner,
    )

    # subagent：预设注册表 + delegate_task / run_agent / collect_subagent（toolset='subagent'）
    from crew.agent.subagent import ActiveSubagents, SubagentRegistry, register_subagent_tools

    sub_registry = SubagentRegistry()
    app.subagent_registry = sub_registry
    app.subagent_active = ActiveSubagents()
    from crew.tasks.task_manager import LegacyTaskManagerAdapter

    app.subagent_tasks = LegacyTaskManagerAdapter(app.tasks)

    def _launch_background(coro) -> None:
        """把后台子 agent 协程起飞成 asyncio 任务并持有强引用，完成后移除。"""
        task = asyncio.ensure_future(coro)
        app._subagent_bg_tasks.add(task)
        task.add_done_callback(app._subagent_bg_tasks.discard)

    register_subagent_tools(
        registry,
        sub_registry,
        build_child=app._make_subagent,
        max_concurrent=cfg.subagent_max_concurrent,
        max_tasks=cfg.subagent_max_tasks,
        idle_timeout=cfg.tasks_subagent_inactivity_timeout_seconds,
        max_runtime=cfg.tasks_subagent_execution_timeout_seconds,
        active=app.subagent_active,
        tasks=app.subagent_tasks,
        launch_background=_launch_background,
        on_background_done=app._on_subagent_background_done,
        background_capacity=lambda: len(app._subagent_bg_tasks) < cfg.subagent_max_concurrent,
        on_collected=app._on_subagent_collected,
    )

    # cron：任务存储 + 引擎 + 暴露给 agent 的工具
    from crew.cron import CronJobStore, CronService
    from crew.tools.cron_tools import register_cron_tools

    cron_store = CronJobStore(cfg.db_path, wal_enabled=cfg.sqlite_wal)
    app.cron_store = cron_store
    app.cron_service = None
    if cfg.cron_enabled:
        def _cron_origin_source(env: Envelope):
            raw = env.params.get("cron_origin_source")
            if not isinstance(raw, dict) or not raw:
                return None
            try:
                from crew.gateway.session_context import SessionSource

                return SessionSource.from_dict(raw)
            except Exception:  # noqa: BLE001
                log.warning("cron origin_source 无法解析 session=%s raw=%s", env.session_id, raw)
                return None

        async def _notify_cron_session(kind: str, env: Envelope, session_id: str) -> None:
            """Notify the active Owner that a Cron Fire created or updated a session."""
            if app._notify_owner_fn is None:
                return
            try:
                await app._notify_owner_fn(
                    env.user_id,
                    {
                        "kind": kind,
                        "body": {
                            "job_id": str(env.params.get("cron_job_id") or ""),
                            "job_name": str(env.params.get("cron_job_name") or "").strip(),
                            "source_session_id": str(
                                env.params.get("cron_source_session_id") or ""
                            ),
                        },
                        "session_id": session_id,
                        "is_final": True,
                        "sequence": 0,
                    },
                )
            except Exception:  # noqa: BLE001
                log.debug("cron 广播会话事件失败 kind=%s session=%s", kind, session_id)

        async def _cron_runner(env: Envelope) -> None:
            # 走 SessionDispatcher：尊重忙时策略 / 全局并发上限，与 WS/平台入口同一调度
            final_text, error = "", ""
            deliver_target = str(env.params.get("cron_deliver") or "").strip()
            origin = _cron_origin_source(env)

            # origin 只对当前 DeliveryRouter 支持的外部渠道有效。Web/local/missing
            # 必须回退新会话，否则会把本地来源误当作外部 sender 并在执行后失败。
            external_origin_platforms = {"feishu"}
            if deliver_target.lower() == "origin" and (
                origin is None or origin.platform not in external_origin_platforms
            ):
                log.debug("cron deliver origin 无外部 sender，fallback 为新建会话")
                deliver_target = "new_session"

            # 默认/新会话投递：每次触发都新建一个本地会话，便于前端通过未读绿点感知。
            # "local" 表示显式投递回原绑定会话，保持原行为不新建。
            if deliver_target in {"", "new_session"}:
                job_name = str(env.params.get("cron_job_name") or "").strip()
                new_sid = f"cron_{uuid.uuid4().hex[:12]}"
                app.session_store.ensure_session(
                    new_sid,
                    workspace_id=env.workspace_id,
                    title=f"[定时] {job_name}" if job_name else "[定时] 任务",
                    owner_account_id=env.user_id,
                )
                env.session_id = new_sid
                await _notify_cron_session("cron_session_created", env, new_sid)
                deliver_target = "new_session"

            if origin is not None and "session_context" not in env.params:
                from crew.gateway.session_context import SessionContext

                env.params["session_context"] = SessionContext(
                    source=origin,
                    connected_platforms=["local", origin.platform],
                    shared_multi_user=origin.chat_type in {"group", "channel"},
                    session_id=env.session_id,
                    workspace_id=env.workspace_id,
                )
            async for chunk in app.dispatch(env):
                # 有活跃 WS 时实时推送，无则只落库（用户下次打开可见）
                if app._push_fn is not None:
                    try:
                        await app._push_fn(env.session_id, chunk, owner_account_id=env.user_id)
                    except Exception:  # noqa: BLE001
                        log.debug("cron 推送 chunk 失败，session=%s", env.session_id)
                # 捕获最终文本/错误，供投递到外部渠道（如 feishu:chat_id）
                if chunk.kind == "final":
                    final_text = chunk.body.get("text", "")
                elif chunk.kind == "error":
                    error = chunk.body.get("message", "")
            if deliver_target.lower() == "local":
                await _notify_cron_session("cron_session_updated", env, env.session_id)
            # 投递：把 cron 结果发到 deliver 指定的渠道（delivery_router 由 gateway 装配）
            if deliver_target and deliver_target.lower() not in {"local", "new_session"}:
                reply = (final_text or error).strip()
                if reply:
                    if app.delivery_router is None:
                        raise RuntimeError("cron deliver 需要 gateway delivery router")
                    result = await app.delivery_router.deliver(deliver_target, reply, origin=origin)
                    if not result.get("ok"):
                        log.warning("cron deliver 失败 target=%s err=%s",
                                    deliver_target, result.get("error"))
                        raise RuntimeError(str(result.get("error") or f"deliver failed: {deliver_target}"))

        app.cron_service = CronService(
            cron_store,
            _cron_runner,
            max_parallel_jobs=cfg.cron_max_parallel_jobs,
        )
    register_cron_tools(registry, cron_store, app.cron_service)

    # MCP Client：连接外部 MCP server（在 startup 时实际连接）
    from crew.tools.mcp_client import MCPClientManager
    app.mcp_manager = MCPClientManager(cfg.mcp_servers)

    from crew.dynamickanban.store import SQLiteKanbanStore

    dk_store = SQLiteKanbanStore(cfg.db_path, wal_enabled=cfg.sqlite_wal)

    if enable_team:
        from crew.team.team_manager import InProcessTeamManager
        app.set_team_manager(
            InProcessTeamManager(
                provider=provider,
                registry=registry,
                session_store=session_store,
                memory=memory,
                plugins=plugins,
                tasks=LegacyTaskManagerAdapter(app.tasks),
                config=cfg,
                external_store=app.external_agents,
                interaction_bridge=app.interaction_bridge,
                kanban_store=dk_store,
                drain_subagent_notifications=app.pop_subagent_notifications,
                provider_for_owner=app.owner_team_provider,
                provider_for_member_model=app.owner_team_member_model_provider,
            )
        )

    # Dynamic Kanban：独立的多智能体协同后端
    from crew.dynamickanban.manager import DynamicKanbanManager

    app.dynamic_kanban = DynamicKanbanManager(
        store=dk_store,
        provider=provider,
        base_registry=registry,
        session_store=session_store,
        memory=memory,
        plugins=plugins,
        config=cfg,
        provider_for_owner=app.owner_team_provider,
    )

    from crew.gateway.hooks import hook_registry

    async def _promote_session_model_on_agent_end(_event: str, ctx: dict) -> None:
        sid = str(ctx.get("session_id") or "")
        if not sid:
            return
        app.promote_pending_model_if_idle(
            sid,
            owner_account_id=str(ctx.get("owner_account_id") or ""),
            queue_depth=int(ctx.get("queue_depth") or 0),
            running_depth=int(ctx.get("running_depth") or 0),
        )

    hook_registry.register("agent:end", _promote_session_model_on_agent_end)

    return app
