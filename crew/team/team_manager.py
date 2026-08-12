"""单进程 Team 管理器。

装配关系：
  Teammate = SingleAgent（共享基础工具：terminal/file...）
  Leader   = SingleAgent（专属工具集：基础工具 + delegate_to_teammate）
  Leader 通过 delegate 工具同步驱动 Teammate，任务流转登记到 TaskManager。

对照 JiuwenSwarm：这里只做 inprocess 传输；分布式(pyzmq/pg)留接口不实现。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from crew.agent.executor import create_executor
from crew.agent.file_changes import (
    FileMetadataSnapshot,
    changes_between_snapshots,
    merge_changes,
    workspace_snapshot,
)
from crew.agent.runtime import SingleAgent
from crew.core.envelope import Envelope, ResponseChunk
from crew.core.errors import ToolError
from crew.core.followup import (
    CANCELLED_MARKER,
    send_followup_question_to,
    send_followup_status_to,
    wait_for_answer,
)
from crew.core.interfaces import (
    Agent,
    LLMProvider,
    MemoryProvider,
    SessionStore,
    TaskManager,
    TeamManager,
)
from crew.core.types import Message
from crew.plugins.manager import PluginManager
from crew.state.config import Config
from crew.state.home import safe_path_segment, task_workspace_path
from crew.state.logging import get_logger
from crew.state.team_member_model import materialize_team_member_model_bindings
from crew.team import flow_builder
from crew.team import result_presenter as team_presenter
from crew.team.bus import TeamBus, register_team_bus_tools
from crew.team.capabilities import normalize_capabilities
from crew.team.delegate_tool import (
    TEAM_RESULT_STATUSES,
    register_delegate_tool,
    register_plan_change_tool,
    register_team_mention_tool,
    require_team_result_status,
    run_delegate_to_teammate,
)
from crew.team.formation import (
    rank_staffing_candidates,
    ready_runtime_model_options,
    recommend_runtime_model,
    role_key_for_capabilities,
)
from crew.team.graph_planner import TeamGraphPlanner, schedule_planning_provider_warmup
from crew.team.models import (
    RuntimeStaffingRequest,
    TeamMemberSpec,
    TeamPlan,
    TeamPlanEdge,
    TeamPlanNode,
    TeamSession,
)
from crew.team.roles import (
    CREW_BUILTIN_AGENT_ID,
    DEFAULT_MEMBERS,
    intelligent_role_markdown,
    is_crew_builtin_agent,
    is_crew_builtin_display_id,
    leader_prompt,
    role_preset,
    teammate_prompt,
)
from crew.team.turn_decision import (
    TeamStatusQuery,
    TeamTurnDecision,
    decide_team_turn,
    new_workflow_decision,
)
from crew.team.turn_router import TeamTurnRouter
from crew.tools.registry import Registry

log = get_logger("team")
TeamKey = tuple[str, str]
_RESULT_PATH_RE = re.compile(r"(?P<path>(?:/[^\s`'\"，。；、)\]]+)+\.(?:html?|md|txt|json|csv|js|ts|tsx|py|png|jpe?g|gif|svg|pdf))")
_RESULT_RELATIVE_PATH_RE = re.compile(
    r"(?<![\w/.-])(?P<path>(?:\.?/)?(?:[\w.-]+/)*[\w.-]+\.(?:html?|md|txt|json|csv|js|ts|tsx|py|png|jpe?g|gif|svg|pdf))(?![\w/.-])"
)
_RESULT_BACKTICK_PATH_RE = re.compile(
    r"`(?P<path>(?:/|\.{0,2}/)?[\w.-]+(?:/[\w.-]+)+/?)`"
)


def _is_team_chat_noise(text: str) -> bool:
    """Return whether a persisted Team chat line is runtime/tool noise."""

    return team_presenter.is_team_chat_noise(text)


def _join_stream_fragments(parts: list[str]) -> str:
    """Join raw model deltas without changing their token boundaries."""

    return "".join(str(part or "") for part in parts).strip()


def _normalize_legacy_chunked_thinking(value: str) -> str:
    """Repair Team thinking persisted by the old one-delta-per-paragraph writer."""

    text = str(value or "")
    blocks = [part.strip() for part in re.split(r"\n{2,}", text) if part.strip()]
    if len(blocks) < 6:
        return text
    token_like = sum(bool(re.fullmatch(r"\S{1,28}", part)) for part in blocks)
    if token_like / len(blocks) < 0.8:
        return text

    joined = blocks[0]
    for block in blocks[1:]:
        if re.fullmatch(r"[.,!?;:%)\]}，。！？；：、]+", block) or block.startswith(("'", "’")):
            joined += block
        elif joined.endswith(("(", "[", "{", "'", "’")):
            joined += block
        else:
            joined += f" {block}"
    return joined


def _visible_session_id(session_id: str) -> str:
    """Team 运行 session 可带 per-turn 后缀，前端交互要回到用户可见父会话。"""
    marker = "::turn::"
    return session_id.split(marker, 1)[0] if marker in session_id else session_id


@dataclass
class Team:
    leader: Agent
    direct_leader: Agent
    teammates: dict[str, Agent]
    session: TeamSession
    display_name: str
    leader_spec: TeamMemberSpec
    members: dict[str, TeamMemberSpec]
    bus: TeamBus
    external_team_id: str = ""
    runtime_members: dict[str, TeamMemberSpec] = field(default_factory=dict)


@dataclass(frozen=True)
class NodeExecutionAssessment:
    execution_status: str
    acceptance_status: str
    reason: str
    failed_tools: tuple[str, ...] = ()
    artifact_count: int = 0
    changed_file_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_status": self.execution_status,
            "acceptance_status": self.acceptance_status,
            "reason": self.reason,
            "failed_tools": list(self.failed_tools),
            "artifact_count": self.artifact_count,
            "changed_file_count": self.changed_file_count,
        }


class InProcessTeamManager(TeamManager):
    def __init__(
        self,
        provider: LLMProvider,
        registry: Registry,
        session_store: SessionStore,
        memory: MemoryProvider,
        plugins: PluginManager,
        tasks: TaskManager,
        config: Config,
        external_store: Any | None = None,
        interaction_bridge: Any | None = None,
        kanban_store: Any | None = None,
        drain_subagent_notifications: Callable[[str, str], list] | None = None,
        provider_for_owner: Callable[[str], LLMProvider] | None = None,
        provider_for_member_model: Callable[[str, str], LLMProvider] | None = None,
    ) -> None:
        self.provider = provider
        # TeamManager 是多 owner 共享实例。规划器和内置 Leader 不能固定借用
        # 进程级 provider，否则远程登录用户在“设置 → 模型”选择的默认模型不会生效。
        self.provider_for_owner = provider_for_owner
        # 内置 Team 成员可绑定不同于 owner 默认模型的 profile。Provider 由 App
        # 缓存并负责关闭；此处只按成员快照选择，绝不在运行中改写已有 Agent。
        self.provider_for_member_model = provider_for_member_model
        self.base_registry = registry
        self.session_store = session_store
        self.memory = memory
        self.plugins = plugins
        self.tasks = tasks
        self.config = config
        self.external_store = external_store
        self.interaction_bridge = interaction_bridge
        self.kanban_store = kanban_store
        # drain team member 后台子 agent 通知的回调（app.pop_subagent_notifications）。
        # team member 派活时由 SingleAgent.run 调用，把完成通知注入 member 本轮上下文。
        self.drain_subagent_notifications = drain_subagent_notifications
        self._teams: dict[TeamKey, Team] = {}
        self._plans: dict[TeamKey, TeamPlan] = {}
        self._plan_workflows: dict[TeamKey, str] = {}
        self._plan_node_tasks: dict[tuple[str, str, str], str] = {}
        self._planning_missing_info: dict[TeamKey, list[str]] = {}
        self._active_lock = threading.Lock()
        self._active_children: dict[TeamKey, dict[str, dict[str, Any]]] = {}
        # 所有成员委派协程的唯一注册表。既持有 detached task 的强引用，
        # 也覆盖 DAG 并行节点；按 (owner, session) 索引同时服务 stop 与 logout。
        self._delegate_tasks: dict[TeamKey, set[asyncio.Task[Any]]] = {}
        self._staffing_locks: dict[TeamKey, asyncio.Lock] = {}
        self.turn_router = TeamTurnRouter()
        self.graph_planner = TeamGraphPlanner()

    def _provider_for_owner(self, owner_account_id: str = "") -> LLMProvider:
        resolver = self.provider_for_owner
        if callable(resolver):
            resolved = resolver(str(owner_account_id or ""))
            if resolved is not None:
                return resolved
        return self.provider

    def _provider_for_member(
        self,
        spec: TeamMemberSpec,
        owner_account_id: str = "",
    ) -> LLMProvider:
        """Resolve the Provider captured by one newly-created built-in member."""
        model_id = str(spec.model or "").strip()
        resolver = self.provider_for_member_model
        if spec.executor == "builtin" and model_id and callable(resolver):
            resolved = resolver(str(owner_account_id or ""), model_id)
            if resolved is not None:
                return resolved
        return self._provider_for_owner(owner_account_id)

    @staticmethod
    def _key(session_id: str, owner_account_id: str = "") -> TeamKey:
        return str(owner_account_id or ""), str(session_id or "")

    @staticmethod
    def _session_from_key(key: TeamKey) -> str:
        return key[1]

    def _kanban_store_for_owner(self, owner_account_id: str = "") -> Any | None:
        store = self.kanban_store
        if store is None:
            return None
        owner = str(owner_account_id or "").strip()
        if not owner:
            return None
        if hasattr(store, "for_owner"):
            return store.for_owner(owner)
        return store

    def _existing_plan_key(self, session_id: str, owner_account_id: str = "") -> TeamKey:
        key = self._key(session_id, owner_account_id)
        if key in self._plans or owner_account_id:
            return key
        for candidate in self._plans:
            if candidate[1] == session_id:
                return candidate
        return key

    def _existing_team_key(self, session_id: str, owner_account_id: str = "") -> TeamKey:
        key = self._key(session_id, owner_account_id)
        if key in self._teams or owner_account_id:
            return key
        for candidate in self._teams:
            if candidate[1] == session_id:
                return candidate
        return key

    def _external_team_specs(
        self,
        external_team_id: str,
        *,
        owner_account_id: str = "",
        model_bindings: dict[str, Any] | None = None,
    ) -> tuple[list[TeamMemberSpec], TeamMemberSpec | None]:
        if not external_team_id or self.external_store is None:
            return [], None
        external_team = self.external_store.get_team(
            external_team_id,
            owner_account_id=owner_account_id,
        )
        leader_agent_id = str(external_team.get("leader_agent_id") or "").strip()
        formation_plan = (
            external_team.get("formation_plan")
            if isinstance(external_team.get("formation_plan"), dict)
            else {}
        )
        formation_members = {
            str(item.get("agent_id") or ""): item
            for item in (formation_plan.get("members") or [])
            if isinstance(item, dict) and str(item.get("agent_id") or "")
        }
        formation_version = max(1, int(formation_plan.get("version") or 1)) if formation_plan else 0
        members: list[TeamMemberSpec] = []
        leader_spec: TeamMemberSpec | None = None
        bindings = model_bindings if isinstance(model_bindings, dict) else {}
        for row in external_team.get("members") or []:
            agent_id = str(row.get("agent_id") or "").strip()
            binding = bindings.get(agent_id) if isinstance(bindings.get(agent_id), dict) else {}
            formation_member = formation_members.get(agent_id, {})
            responsibility = (
                formation_member.get("responsibility")
                if isinstance(formation_member.get("responsibility"), dict)
                else {}
            )
            is_builtin = is_crew_builtin_agent(agent_id)
            member_id = CREW_BUILTIN_AGENT_ID if is_builtin else str(row.get("agent_name") or agent_id)
            spec = TeamMemberSpec.from_config({
                "member_id": member_id,
                "name": str(row.get("agent_name") or ("Crew 内置智能体" if is_builtin else agent_id)),
                "role": str(row.get("role") or ""),
                "executor": "builtin" if is_builtin else "external",
                "external_agent_id": agent_id,
                "model": str(binding.get("model_id") or ""),
                "capabilities": row.get("capabilities") or [],
                "metadata": {
                    "role_key": row.get("role_key") or "",
                    "role_label": row.get("role_label") or "",
                    "workflow_lane": row.get("workflow_lane") or "",
                    **(
                        {
                            "formation_plan_version": formation_version,
                            "formation_responsibility": dict(responsibility),
                            "formation_responsibility_markdown": str(
                                formation_member.get("responsibility_markdown") or row.get("role") or ""
                            ),
                            "formation_locked": bool(formation_member.get("locked")),
                        }
                        if formation_member
                        else {}
                    ),
                },
            })
            if agent_id and agent_id == leader_agent_id:
                leader_spec = TeamMemberSpec(
                    member_id="leader",
                    name=spec.name,
                    role=spec.role or "负责拆解、派活、跟踪任务、汇总最终结果",
                    executor=spec.executor,
                    external_agent_id="" if is_builtin else spec.external_agent_id,
                    model=spec.model,
                    capabilities=list(spec.capabilities),
                    workspace_policy=spec.workspace_policy,
                    session_policy=spec.session_policy,
                    permission_policy=spec.permission_policy,
                    system_prompt=spec.system_prompt,
                    metadata=dict(spec.metadata),
                )
            else:
                members.append(spec)
        return members, leader_spec

    def _members(
        self,
        external_team_id_override: str = "",
        *,
        owner_account_id: str = "",
    ) -> list[TeamMemberSpec]:
        team_cfg = self.config.team_config or {}
        external_team_id = str(external_team_id_override or team_cfg.get("external_team_id") or "").strip()
        if external_team_id and self.external_store is not None:
            try:
                members, leader_spec = self._external_team_specs(
                    external_team_id,
                    owner_account_id=owner_account_id,
                )
                if leader_spec is not None:
                    members = [leader_spec, *members]
                if members:
                    return members
            except Exception as exc:  # noqa: BLE001
                log.warning("读取外部团队失败 external_team_id=%s err=%s", external_team_id, exc)
                raise ToolError(f"读取外部团队失败：{external_team_id}") from exc

        return [TeamMemberSpec.from_config(dict(m)) for m in (team_cfg.get("members") or DEFAULT_MEMBERS)]

    def _clone_registry(self) -> Registry:
        registry = Registry()
        for name in self.base_registry.names():
            registry.register(self.base_registry.get(name))
        return registry

    def _executor_config(
        self,
        spec: TeamMemberSpec,
        *,
        member_session_id: str,
        team_session_id: str,
    ) -> dict[str, Any]:
        if spec.executor == "builtin":
            return {}
        config: dict[str, Any] = dict(spec.metadata.get(spec.executor) or {})
        if spec.external_agent_id:
            config["external_agent_id"] = spec.external_agent_id
        if spec.model:
            config["model"] = spec.model
        if self.external_store is not None:
            config["external_store"] = self.external_store
        if self.interaction_bridge is not None:
            config["interaction_bridge"] = self.interaction_bridge
        config["crew_session_id"] = member_session_id
        config["display_session_id"] = _visible_session_id(team_session_id)
        config["control_session_id"] = team_session_id
        return config

    def _new_agent(
        self,
        registry: Registry,
        system_prompt: str,
        *,
        spec: TeamMemberSpec,
        member_session_id: str,
        team_session_id: str,
        owner_account_id: str = "",
        tool_filter: list[str] | None = None,
    ) -> SingleAgent:
        # Wiki 工具只属于专用 Wiki Agent。Team 使用独立 Registry，但不能因此
        # 绕过全局的 Wiki 能力边界。
        from crew.tools.policy import exclude_toolsets

        base_tools = registry.names() if tool_filter is None else tool_filter
        tool_filter = exclude_toolsets(
            registry,
            base_tools,
            exact={"wiki.read", "wiki.manage"},
        )
        provider = self._provider_for_member(spec, owner_account_id)
        executor_kind = "external" if spec.executor in {"acp", "cli", "external"} else spec.executor
        executor = create_executor(
            executor_kind,
            provider=provider,
            registry=registry,
            plugins=self.plugins,
            config=self._executor_config(
                spec,
                member_session_id=member_session_id,
                team_session_id=team_session_id,
            ),
            max_iterations=self.config.max_iterations,
            max_retries=self.config.retry_max,
            backoff_seconds=self.config.retry_backoff,
            parallel_tools=self.config.parallel_tools,
            empty_retry_max=self.config.empty_retry_max,
            continuation_max=self.config.continuation_max,
            max_parallel_tool_calls=self.config.max_parallel_tool_calls,
            max_delegate_tool_calls=(
                self.config.team_max_concurrent_children if spec.member_id == "leader" else 0
            ),
        )
        return SingleAgent(
            provider=provider,
            registry=registry,
            session_store=self.session_store,
            memory=self.memory,
            plugins=self.plugins,
            system_prompt=system_prompt,
            tool_filter=tool_filter,
            max_iterations=self.config.max_iterations,
            executor=executor,
            agent_id=spec.member_id,
            subagent_drain_fn=self.drain_subagent_notifications,
        )

    def _mark_child_active(self, record: dict[str, Any]) -> None:
        parent_session_id = str(record.get("parent_session_id") or "")
        owner_account_id = str(record.get("owner_account_id") or "")
        child_id = str(record.get("child_id") or "")
        if not parent_session_id or not child_id:
            return
        with self._active_lock:
            self._active_children.setdefault(self._key(parent_session_id, owner_account_id), {})[child_id] = record

    def _mark_child_done(self, parent_session_id: str, child_id: str, owner_account_id: str = "") -> None:
        with self._active_lock:
            key = self._key(parent_session_id, owner_account_id)
            children = self._active_children.get(key)
            if not children:
                return
            children.pop(child_id, None)
            if not children:
                self._active_children.pop(key, None)

    def _track_delegate_task(
        self,
        session_id: str,
        owner_account_id: str,
        task: asyncio.Task[Any],
    ) -> None:
        """Register every live member coroutine under its owner-scoped Team session."""
        key = self._key(session_id, owner_account_id)
        with self._active_lock:
            self._delegate_tasks.setdefault(key, set()).add(task)

        def _discard(completed: asyncio.Task[Any]) -> None:
            with self._active_lock:
                tasks = self._delegate_tasks.get(key)
                if tasks is None:
                    return
                tasks.discard(completed)
                if not tasks:
                    self._delegate_tasks.pop(key, None)

        task.add_done_callback(_discard)

    def _cancel_delegate_tasks(self, session_id: str, owner_account_id: str = "") -> int:
        key = self._key(session_id, owner_account_id)
        with self._active_lock:
            tasks = list(self._delegate_tasks.get(key, set()))
        cancelled = 0
        for task in tasks:
            if task.done():
                continue
            task.cancel()
            cancelled += 1
        return cancelled

    @staticmethod
    def _public_child(record: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in record.items()
            if key not in {"agent", "owner_account_id"}
        }

    def active_children(self, session_id: str | None = None, owner_account_id: str = "") -> object:
        """返回活跃 teammate 状态；供 gateway runtime/concurrency 只读展示。"""
        with self._active_lock:
            if session_id is not None:
                return [
                    self._public_child(record)
                    for record in self._active_children.get(self._key(session_id, owner_account_id), {}).values()
                ]
            return {
                sid: [self._public_child(record) for record in children.values()]
                for (owner, sid), children in self._active_children.items()
                if not owner_account_id or owner == owner_account_id
            }

    def team_member_switch_state(
        self,
        session_id: str,
        member_id: str,
        owner_account_id: str = "",
    ) -> dict[str, Any]:
        """Return one member's execution state across a visible Team session.

        A Team turn can create ``::turn::`` sidechain sessions.  Model
        switching is scoped to the selected member, so an active sibling must
        not block it; an active invocation of this member must.  The running
        coroutine already holds its Agent instance, which makes that
        invocation an immutable model snapshot while a later turn can rebuild
        the Team from the new binding.
        """
        visible_session_id = _visible_session_id(str(session_id or ""))
        target_member_id = str(member_id or "").strip()
        if not visible_session_id or not target_member_id:
            return {"status": "idle", "active_task_count": 0, "active_children": []}
        prefix = f"{visible_session_id}::turn::"
        owner = str(owner_account_id or "")
        with self._active_lock:
            active_children = [
                self._public_child(record)
                for (record_owner, parent_session_id), children in self._active_children.items()
                if record_owner == owner
                and (
                    parent_session_id == visible_session_id
                    or parent_session_id.startswith(prefix)
                )
                for record in children.values()
                if str(record.get("member") or "") == target_member_id
            ]
        return {
            "status": "running" if active_children else "idle",
            "active_task_count": len(active_children),
            "active_children": active_children,
        }

    def _member_ids_for_session(
        self,
        session_id: str,
        external_team_id: str = "",
        owner_account_id: str = "",
    ) -> list[str]:
        team = self._get_or_create(session_id, external_team_id=external_team_id, owner_account_id=owner_account_id)
        return list(dict.fromkeys(["leader", *team.teammates.keys()]))

    def create_plan(
        self,
        session_id: str,
        *,
        goal: str,
        nodes: list[dict[str, Any]],
        edges: list[Any] | None = None,
        external_team_id: str = "",
        owner_account_id: str = "",
        workflow_plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """创建或替换当前 Team session 的轻量 TeamPlan。

        第一阶段保持内存实现；结构校验复用 Dynamic Kanban 的 PlanGraph。
        """
        from crew.dynamickanban.models import PlanEdge, PlanNode, PlanResult
        from crew.dynamickanban.plan_graph import PlanGraph

        valid_members = self._member_ids_for_session(
            session_id,
            external_team_id=external_team_id,
            owner_account_id=owner_account_id,
        )
        default_member = next((m for m in valid_members if m != "leader"), valid_members[0])
        raw_node_metadata: dict[str, dict[str, Any]] = {}
        plan_nodes = []
        for raw in nodes:
            if not isinstance(raw, dict):
                continue
            raw_id = str(raw.get("id") or raw.get("node_id") or "").strip()
            plan_nodes.append(PlanNode(
                id=raw_id,
                title=str(raw.get("title") or raw.get("content") or "").strip(),
                detail=str(raw.get("detail") or raw.get("description") or "").strip(),
                assignee=str(raw.get("assignee") or "").strip() or default_member,
            ))
            raw_meta = raw.get("metadata")
            if raw_id and isinstance(raw_meta, dict):
                raw_node_metadata[raw_id] = dict(raw_meta)
        if not plan_nodes:
            raise ValueError("TeamPlan 至少需要一个节点")

        plan_edges: list[PlanEdge] = []
        for raw in edges or []:
            if isinstance(raw, dict):
                parent = str(raw.get("parent_id") or raw.get("from") or "").strip()
                child = str(raw.get("child_id") or raw.get("to") or "").strip()
            elif isinstance(raw, (list, tuple)) and len(raw) >= 2:
                parent = str(raw[0]).strip()
                child = str(raw[1]).strip()
            else:
                continue
            if parent and child:
                plan_edges.append(PlanEdge(parent_id=parent, child_id=child))

        graph = PlanGraph.from_plan_result(
            PlanResult(summary=goal, nodes=plan_nodes, edges=plan_edges),
            valid_roles=valid_members,
            default_role=default_member,
        )
        graph.validate_and_fix()

        plan = TeamPlan(team_session_id=session_id, goal=str(goal or "").strip())
        for node in graph.topological_order():
            metadata = raw_node_metadata.get(node.id) or team_presenter.node_display_progress(
                node_id=node.id,
                title=node.title or node.id,
                assignee=node.assignee or default_member,
            )
            plan.nodes[node.id] = TeamPlanNode(
                node_id=node.id,
                title=node.title or node.id,
                detail=node.detail or node.title or node.id,
                assignee=node.assignee or default_member,
                metadata=metadata,
            )
        plan.edges = [
            TeamPlanEdge(parent_id=edge.parent_id, child_id=edge.child_id)
            for edge in graph.edges
        ]
        self._plans[self._key(session_id, owner_account_id)] = plan
        self._persist_team_plan(
            plan,
            owner_account_id=owner_account_id,
            external_team_id=external_team_id,
            workflow_plan=workflow_plan,
        )
        return {"ok": True, "plan": plan.to_dict()}

    def read_plan(self, session_id: str, owner_account_id: str = "") -> dict[str, Any]:
        plan = self._plans.get(self._existing_plan_key(session_id, owner_account_id))
        if plan is None:
            return {"ok": True, "plan": None}
        return {"ok": True, "plan": plan.to_dict()}

    def plans_for_session(self, session_id: str, owner_account_id: str = "") -> list[dict[str, Any]]:
        """返回父 Team session 及其 per-turn 子 session 下的 TeamPlan。"""
        sid = str(session_id or "").strip()
        if not sid:
            return []
        prefix = f"{sid}::turn::"
        plans = [
            plan.to_dict()
            for key, plan in self._plans.items()
            if key[0] == owner_account_id and (key[1] == sid or key[1].startswith(prefix))
        ]
        plans.sort(key=lambda item: float(item.get("created_at") or 0))
        return plans

    @staticmethod
    def _kanban_status(status: str) -> str:
        return {
            "pending": "pending",
            "in_progress": "running",
            "completed": "done",
            "failed": "failed",
            "blocked": "blocked",
            "needs_info": "blocked",
            "cancelled": "cancelled",
        }.get(str(status or "").strip(), "pending")

    @staticmethod
    def _taskboard_status(status: str) -> str:
        return {
            "pending": "pending",
            "ready": "pending",
            "running": "running",
            "done": "completed",
            "failed": "failed",
            "blocked": "blocked",
            "cancelled": "cancelled",
        }.get(str(status or "").strip().lower(), "pending")

    @staticmethod
    def _team_turn_group_id(session_id: str) -> str:
        sid = str(session_id or "")
        marker = "::turn::"
        if marker not in sid:
            return sid
        head, rest = sid.split(marker, 1)
        request_id = rest.split("::", 1)[0]
        return f"{head}{marker}{request_id}"

    @staticmethod
    def _node_event_index(events: list[dict[str, Any]]) -> tuple[dict[str, str], dict[str, int], dict[str, dict[str, Any]]]:
        task_to_node: dict[str, str] = {}
        attempts: dict[str, int] = {}
        node_metadata: dict[str, dict[str, Any]] = {}
        ordered_events = sorted(events, key=lambda item: float(item.get("ts") or 0))
        for event in ordered_events:
            if str(event.get("event_type") or "") == "team_plan_created":
                payload = dict(event.get("payload") or {})
                for node_id, task_id in dict(payload.get("node_task_ids") or {}).items():
                    task_to_node[str(task_id)] = str(node_id)
                for node in payload.get("nodes") or []:
                    if not isinstance(node, dict):
                        continue
                    node_id = str(node.get("node_id") or node.get("id") or "").strip()
                    metadata = node.get("metadata")
                    if node_id and isinstance(metadata, dict):
                        node_metadata[node_id] = dict(metadata)
                continue
            if str(event.get("event_type") or "") == "workflow_plan_revised":
                payload = dict(event.get("payload") or {})
                for node_id, task_id in dict(payload.get("node_task_ids") or {}).items():
                    task_to_node[str(task_id)] = str(node_id)
                delta = payload.get("delta") if isinstance(payload.get("delta"), dict) else {}
                metadata_updates = delta.get("updated_node_metadata") if isinstance(delta, dict) else None
                if isinstance(metadata_updates, dict):
                    for node_id, metadata in metadata_updates.items():
                        if not isinstance(metadata, dict):
                            continue
                        node_key = str(node_id or "").strip()
                        if node_key:
                            node_metadata[node_key] = {
                                **dict(node_metadata.get(node_key) or {}),
                                **dict(metadata),
                            }
                continue
            if str(event.get("event_type") or "") != "team_node_updated":
                continue
            task_id = str(event.get("task_id") or "")
            payload = dict(event.get("payload") or {})
            node_id = str(payload.get("node_id") or "")
            if task_id and node_id:
                task_to_node[task_id] = node_id
            if node_id:
                attempts[node_id] = max(attempts.get(node_id, 0), int(payload.get("attempt_count") or 0))
                metadata = payload.get("metadata")
                if isinstance(metadata, dict):
                    node_metadata[node_id] = {
                        **dict(node_metadata.get(node_id) or {}),
                        **dict(metadata),
                    }
        return task_to_node, attempts, node_metadata

    def task_projection_for_session(
        self,
        session_id: str,
        *,
        owner_account_id: str = "",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """从持久化 Kanban 工作流还原 Team 看板节点。"""
        store = self._kanban_store_for_owner(owner_account_id)
        if store is None:
            return []
        sid = str(session_id or "").strip()
        if not sid:
            return []
        try:
            workflows = store.list_workflows_by_session_prefix(sid)
        except Exception as exc:  # noqa: BLE001
            log.warning("读取 Team kanban workflow 失败 session=%s err=%s", sid, exc)
            return []

        items: list[dict[str, Any]] = []
        for workflow in workflows:
            context = dict(getattr(workflow, "context", {}) or {})
            if context.get("source") != "team":
                continue
            if str(context.get("owner_account_id") or "") != str(owner_account_id or ""):
                continue
            try:
                board = store.get_board_state(workflow.id)
            except Exception as exc:  # noqa: BLE001
                log.warning("读取 Team kanban board 失败 workflow=%s err=%s", workflow.id, exc)
                continue
            events = list(board.get("events") or [])
            task_to_node, attempts, node_metadata = self._node_event_index(events)
            parent_map: dict[str, list[str]] = {}
            child_map: dict[str, list[str]] = {}
            for dep in board.get("dependencies") or []:
                parent_id = str(dep.get("parent_task_id") or "")
                child_id = str(dep.get("child_task_id") or "")
                if not parent_id or not child_id:
                    continue
                parent_map.setdefault(child_id, []).append(parent_id)
                child_map.setdefault(parent_id, []).append(child_id)
            turn_session_id = self._team_turn_group_id(str(workflow.session_id or sid))
            turn_title = str(getattr(workflow, "title", "") or "团队任务")
            for task in board.get("tasks") or []:
                task_id = str(task.get("id") or "")
                if not task_id:
                    continue
                node_id = task_to_node.get(task_id, task_id)
                status = self._taskboard_status(str(task.get("status") or "pending"))
                result = str(task.get("result_summary") or "")
                error = result if status in {"failed", "blocked"} else ""
                display_progress = team_presenter.node_display_progress(
                    node_id=node_id,
                    title=str(task.get("title") or node_id),
                    assignee=str(task.get("assignee") or "leader"),
                    error=error,
                    result=result,
                    metadata=node_metadata.get(node_id),
                )
                result_progress = team_presenter.result_projection(error or result)
                items.append({
                    "id": task_id,
                    "task_id": task_id,
                    "kind": "team",
                    "session_id": str(workflow.session_id or sid),
                    "title": str(task.get("title") or node_id),
                    "detail": str(task.get("detail") or ""),
                    "assignee": str(task.get("assignee") or "leader"),
                    "status": status,
                    "result": result,
                    "error": error,
                    "output_ref": "\n".join(list(task.get("artifact_paths") or [])),
                    "created_at": float(task.get("created_at") or getattr(workflow, "created_at", 0) or 0),
                    "updated_at": float(task.get("updated_at") or getattr(workflow, "updated_at", 0) or 0),
                    "started_at": float(task.get("claimed_at") or task.get("created_at") or 0),
                    "finished_at": task.get("done_at") if status == "completed" else None,
                    "progress": {
                        "source": "team_kanban",
                        "workflow_id": workflow.id,
                        "plan_id": str(context.get("team_plan_id") or ""),
                        "plan_node_id": node_id,
                        "turn_session_id": turn_session_id,
                        "turn_title": turn_title,
                        "parent_node_ids": parent_map.get(task_id, []),
                        "child_node_ids": child_map.get(task_id, []),
                        "delegate_task_id": task_id,
                        "attempt_count": attempts.get(node_id, int(task.get("retry_count") or 0)),
                        "artifact_paths": list(task.get("artifact_paths") or []),
                        **display_progress,
                        **result_progress,
                    },
                })
        items.sort(key=lambda item: float(item.get("created_at") or item.get("started_at") or 0))
        return items[:max(1, int(limit or 200))]

    def _latest_team_workflow_for_status(self, session_id: str, owner_account_id: str = "") -> Any | None:
        store = self._kanban_store_for_owner(owner_account_id)
        if store is None:
            return None
        sid = str(session_id or "").strip()
        if not sid:
            return None
        try:
            workflows = store.list_workflows_by_session_prefix(sid)
        except Exception as exc:  # noqa: BLE001
            log.warning("读取 Team status workflow 失败 session=%s err=%s", sid, exc)
            return None
        owner = str(owner_account_id or "")
        candidates = []
        for workflow in workflows:
            context = dict(getattr(workflow, "context", {}) or {})
            if context.get("source") != "team":
                continue
            if str(context.get("owner_account_id") or "") != owner:
                continue
            candidates.append(workflow)
        if not candidates:
            return None
        candidates.sort(key=lambda item: float(getattr(item, "created_at", 0) or 0))
        return candidates[-1]

    def _team_turn_decision_context(self, workflow: Any) -> dict[str, Any]:
        store = self._kanban_store_for_owner(getattr(workflow, "owner_account_id", ""))
        context = dict(getattr(workflow, "context", {}) or {})
        board: dict[str, Any] = {}
        if store is not None:
            try:
                board = store.get_board_state(workflow.id)
            except Exception as exc:  # noqa: BLE001
                log.warning("读取 Team turn decision board 失败 workflow=%s err=%s", getattr(workflow, "id", ""), exc)
        tasks = list(board.get("tasks") or [])
        events = list(board.get("events") or [])
        workflow_plan = dict(board.get("workflow_plan") or context.get("workflow_plan") or {})
        planning = dict(workflow_plan.get("planning") or {})
        status_counts: dict[str, int] = {}
        members: set[str] = set()
        for task in tasks:
            status = str(task.get("status") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
            assignee = str(task.get("assignee") or "").strip()
            if assignee:
                members.add(assignee)
        latest_event_types = [
            str(event.get("event_type") or "")
            for event in events[:8]
            if str(event.get("event_type") or "")
        ]
        return {
            "has_existing_workflow": True,
            "latest_workflow": {
                "workflow_id": str(getattr(workflow, "id", "") or ""),
                "session_id": str(getattr(workflow, "session_id", "") or ""),
                "title": str(getattr(workflow, "title", "") or ""),
                "status": str(getattr(workflow, "status", "") or ""),
                "created_at": float(getattr(workflow, "created_at", 0) or 0),
                "updated_at": float(getattr(workflow, "updated_at", 0) or 0),
                "plan_strategy": str(planning.get("strategy") or planning.get("selected_mode") or ""),
                "planning_status": str((planning.get("planning_decision") or {}).get("status") or ""),
                "task_status_counts": status_counts,
                "members": sorted(members),
                "latest_event_types": latest_event_types,
            },
        }

    def _team_status_snapshot(self, workflow: Any) -> dict[str, Any]:
        store = self._kanban_store_for_owner(getattr(workflow, "owner_account_id", ""))
        context = dict(getattr(workflow, "context", {}) or {})
        board = store.get_board_state(workflow.id) if store is not None else {}
        events = list(board.get("events") or [])
        task_to_node, attempts, node_metadata = self._node_event_index(events)
        workflow_plan = dict(board.get("workflow_plan") or context.get("workflow_plan") or {})
        planning = dict(workflow_plan.get("planning") or {})
        planning_decision = dict(planning.get("planning_decision") or {})
        nodes: list[dict[str, Any]] = []
        member_activity: dict[str, dict[str, int]] = {}
        started_values: list[float] = []
        finished_values: list[float] = []
        for task in list(board.get("tasks") or []):
            task_id = str(task.get("id") or "")
            node_id = task_to_node.get(task_id, task_id)
            assignee = str(task.get("assignee") or "leader")
            status = str(task.get("status") or "unknown")
            started_at = float(task.get("claimed_at") or task.get("created_at") or 0)
            finished_at = float(task.get("done_at") or task.get("updated_at") or 0) if status in {"done", "failed"} else 0.0
            if started_at:
                started_values.append(started_at)
            if finished_at:
                finished_values.append(finished_at)
            runs = []
            if store is not None and task_id:
                try:
                    runs = [run.to_dict() for run in store.list_runs(task_id)]
                except Exception:  # noqa: BLE001
                    runs = []
            activity = member_activity.setdefault(assignee, {"pending": 0, "running": 0, "completed": 0, "failed": 0, "blocked": 0})
            if status == "done":
                activity["completed"] += 1
            elif status in activity:
                activity[status] += 1
            metadata = dict(node_metadata.get(node_id) or {})
            nodes.append({
                "node_id": node_id,
                "task_id": task_id,
                "title": str((metadata.get("display_title") or task.get("title") or node_id)),
                "assignee": assignee,
                "status": status,
                "duration_ms": int((finished_at - started_at) * 1000) if started_at and finished_at else None,
                "attempt_count": attempts.get(node_id, int(task.get("retry_count") or 0)),
                "summary": str(task.get("result_summary") or "")[:1000],
                "error": str(task.get("result_summary") or "")[:1000] if status in {"failed", "blocked"} else "",
                "latest_run": runs[0] if runs else None,
            })
        workflow_started = min(started_values) if started_values else float(getattr(workflow, "created_at", 0) or 0)
        workflow_finished = max(finished_values) if finished_values else float(getattr(workflow, "updated_at", 0) or 0)
        return {
            "session_id": str(getattr(workflow, "session_id", "") or ""),
            "workflow_id": str(getattr(workflow, "id", "") or ""),
            "title": str(getattr(workflow, "title", "") or ""),
            "status": str(getattr(workflow, "status", "") or ""),
            "duration_ms": int((workflow_finished - workflow_started) * 1000) if workflow_started and workflow_finished else None,
            "planning": {
                "selected_mode": str(planning.get("selected_mode") or ""),
                "strategy": str(planning.get("strategy") or ""),
                "fallback_from": planning.get("fallback_from"),
                "planning_decision": planning_decision,
            },
            "nodes": nodes,
            "blocked_nodes": [node for node in nodes if node["status"] == "blocked"],
            "failed_nodes": [node for node in nodes if node["status"] == "failed"],
            "running_nodes": [node for node in nodes if node["status"] == "running"],
            "member_activity": member_activity,
            "latest_events": [
                {
                    "event_type": str(event.get("event_type") or ""),
                    "actor": str(event.get("actor") or ""),
                    "task_id": str(event.get("task_id") or ""),
                    "payload": dict(event.get("payload") or {}),
                    "ts": float(event.get("ts") or 0),
                }
                for event in events[:20]
            ],
        }

    async def _leader_status_summary(
        self,
        user_message: str,
        snapshot: dict[str, Any],
        decision: TeamTurnDecision,
        *,
        owner_account_id: str = "",
    ) -> str:
        system = """你是 Crew Team Leader。用户正在询问已有团队运行事实。
只能基于 TeamStatusSnapshot 回答；不要生成新任务、不要派活、不要修改 DAG。
回答要短，优先说明耗时、成员执行、节点状态、规划/fallback 和阻塞/失败原因。
如果 snapshot 缺少某类事实，直接说明缺少记录。"""
        payload = {
            "user_message": user_message,
            "turn_decision": {
                "reason": decision.reason,
                "status_query": decision.status_query.__dict__ if decision.status_query else None,
            },
            "TeamStatusSnapshot": snapshot,
        }
        try:
            provider = self._provider_for_owner(owner_account_id)
            response = await asyncio.wait_for(
                provider.chat(
                    [Message.system(system), Message.user(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))],
                    tools=None,
                    max_tokens=800,
                ),
                timeout=8.0,
            )
            text = str(response.text or "").strip()
            if text:
                return text
        except TypeError:
            try:
                response = await asyncio.wait_for(
                    provider.chat(
                        [Message.system(system), Message.user(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))],
                        tools=None,
                    ),
                    timeout=8.0,
                )
                text = str(response.text or "").strip()
                if text:
                    return text
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass
        return self._fallback_team_status_summary(snapshot)

    @staticmethod
    def _fallback_team_status_summary(snapshot: dict[str, Any]) -> str:
        duration = snapshot.get("duration_ms")
        duration_text = f"{int(duration) / 1000:.1f}s" if isinstance(duration, int) else "暂无完整耗时记录"
        nodes = list(snapshot.get("nodes") or [])
        completed = [node for node in nodes if node.get("status") == "done"]
        running = list(snapshot.get("running_nodes") or [])
        failed = list(snapshot.get("failed_nodes") or [])
        blocked = list(snapshot.get("blocked_nodes") or [])
        planning = dict(snapshot.get("planning") or {})
        planning_decision = dict(planning.get("planning_decision") or {})
        lines = [f"当前可见的团队运行耗时：{duration_text}。"]
        if planning_decision:
            lines.append(
                "规划状态："
                f"{planning_decision.get('status') or 'unknown'}"
                f"，耗时 {planning_decision.get('elapsed_ms', '未知')}ms"
                f"，transport={planning_decision.get('transport') or 'unknown'}。"
            )
        lines.append(f"节点状态：已完成 {len(completed)} 个，运行中 {len(running)} 个，失败 {len(failed)} 个，阻塞 {len(blocked)} 个。")
        if nodes:
            details = [
                f"{node.get('title') or node.get('node_id')}({node.get('assignee')}): {node.get('status')}"
                for node in nodes[:6]
            ]
            lines.append("主要节点：" + "；".join(details))
        if failed or blocked:
            problem_nodes = failed + blocked
            lines.append("需关注：" + "；".join(str(node.get("title") or node.get("node_id")) for node in problem_nodes[:4]))
        return "\n".join(lines)

    async def _try_team_status_query(
        self,
        envelope: Envelope,
        *,
        team: Team,
    ) -> list[ResponseChunk] | None:
        workflow = self._latest_team_workflow_for_status(envelope.session_id, owner_account_id=envelope.user_id)
        if workflow is None:
            return None
        context = self._team_turn_decision_context(workflow)
        user_message = str(envelope.query or "")
        decision = self._deterministic_team_status_query_decision(user_message)
        if decision is None:
            decision = await decide_team_turn(
                self._provider_for_owner(envelope.user_id),
                user_message=user_message,
                context=context,
            )
        if not decision.is_status_query:
            return None
        snapshot = self._team_status_snapshot(workflow)
        text = await self._leader_status_summary(
            user_message,
            snapshot,
            decision,
            owner_account_id=envelope.user_id,
        )
        source_session_id = f"{envelope.session_id}::turn::{envelope.request_id}::leader"
        node_id = f"team_status_query_{envelope.request_id}"
        return [
            ResponseChunk.status_event(envelope.request_id, "Leader 正在汇总团队运行状态…"),
            self._team_internal_chunk(
                envelope.request_id,
                agent_id="leader",
                role="leader",
                is_leader=True,
                source_session_id=source_session_id,
                text=text,
                node_id=node_id,
                event_type="team_summary",
                display_mode="chat",
                collapsed_title="Leader 的状态汇总过程",
            ),
            ResponseChunk.final(envelope.request_id, text),
        ]

    @staticmethod
    def _deterministic_team_status_query_decision(user_message: str) -> TeamTurnDecision | None:
        text = str(user_message or "").strip().lower()
        if not text:
            return None

        duration_terms = (
            "用了多久",
            "用多久",
            "花了多久",
            "花多久",
            "花了多长时间",
            "多长时间",
            "耗时",
            "耗费",
            "用时",
            "多久完成",
            "完成多久",
        )
        status_terms = (
            "进度",
            "状态",
            "运行情况",
            "执行情况",
            "完成情况",
            "完成了吗",
            "做完了吗",
            "还在运行",
            "还没完成",
            "是否完成",
        )
        contribution_terms = (
            "谁做了什么",
            "分别做了什么",
            "成员做了什么",
            "谁负责",
            "哪个节点",
            "节点",
            "失败",
            "阻塞",
            "报错",
            "原因",
        )
        history_terms = (
            "刚刚",
            "刚才",
            "上一轮",
            "上次",
            "之前",
            "这个项目",
            "这个任务",
            "这轮",
            "本轮",
            "历史",
            "kanban",
        )
        has_status_intent = any(term in text for term in duration_terms + status_terms + contribution_terms)
        has_history_anchor = any(term in text for term in history_terms)
        if not has_status_intent:
            return None

        # Standalone duration/status questions inside an existing Team session should read the
        # latest Kanban workflow instead of being reinterpreted as a new execution goal.
        past_duration_terms = (
            "用了多久",
            "花了多久",
            "花了多长时间",
            "耗时",
            "耗费",
            "用时",
            "多久完成",
            "完成多久",
        )
        if not has_history_anchor and not any(term in text for term in past_duration_terms):
            return None

        needs: list[str] = []
        if any(term in text for term in duration_terms):
            needs.append("duration")
        if any(term in text for term in contribution_terms):
            needs.extend(["members", "nodes"])
        if any(term in text for term in status_terms):
            needs.append("nodes")
        if any(term in text for term in ("失败", "阻塞", "报错", "原因")):
            needs.append("errors")
        if any(term in text for term in ("刚刚", "刚才", "上一轮", "上次", "之前", "历史", "kanban")):
            needs.append("latest_events")
        if not needs:
            needs = ["duration", "nodes"]
        deduped_needs = list(dict.fromkeys(needs))
        return TeamTurnDecision(
            turn_kind="status_query",
            execution_mode="direct",
            reason="deterministic_team_status_query",
            status_query=TeamStatusQuery(
                question=str(user_message or "").strip(),
                scope="latest_turn",
                needs=deduped_needs,
            ),
            diagnostics={"source": "deterministic_team_status_query"},
        )

    def _persist_team_plan(
        self,
        plan: TeamPlan,
        *,
        owner_account_id: str = "",
        external_team_id: str = "",
        workflow_plan: dict[str, Any] | None = None,
    ) -> None:
        store = self._kanban_store_for_owner(owner_account_id)
        if store is None:
            return
        key = self._key(plan.team_session_id, owner_account_id)
        if key in self._plan_workflows:
            return
        try:
            plan_snapshot = json.loads(json.dumps(workflow_plan or {}, ensure_ascii=False))
            if plan_snapshot:
                task = dict(plan_snapshot.get("task") or {})
                marker = "::turn::"
                task["turn_id"] = plan.team_session_id.split(marker, 1)[1].split("::", 1)[0] if marker in plan.team_session_id else plan.team_session_id
                plan_snapshot["task"] = task
            context = {
                "source": "team",
                "owner_account_id": owner_account_id,
                "parent_session_id": _visible_session_id(plan.team_session_id),
                "team_session_id": plan.team_session_id,
                "team_plan_id": plan.plan_id,
                "external_team_id": external_team_id,
                "workflow_plan": plan_snapshot,
                "current_revision": int(plan_snapshot.get("revision") or 1) if plan_snapshot else 0,
            }
            event_payload = {
                "team_plan_id": plan.plan_id,
                "goal": plan.goal,
                "workflow_plan": plan_snapshot,
                "nodes": [node.to_dict() for node in plan.nodes.values()],
                "edges": [edge.to_dict() for edge in plan.edges],
            }
            if hasattr(store, "create_workflow_graph"):
                workflow, tasks = store.create_workflow_graph(
                    plan.team_session_id,
                    plan.goal,
                    context=context,
                    nodes=[
                        {
                            "id": node.node_id,
                            "title": node.title,
                            "detail": node.detail,
                            "assignee": node.assignee,
                            "status": self._kanban_status(node.status),
                            "max_retries": int((plan_snapshot.get("budget_snapshot") or {}).get("max_retries") or 2),
                        }
                        for node in plan.nodes.values()
                    ],
                    edges=[(edge.parent_id, edge.child_id) for edge in plan.edges],
                    event_type="team_plan_created",
                    event_payload=event_payload,
                    actor="team_runtime",
                )
                self._plan_workflows[key] = workflow.id
                for node_id, task in tasks.items():
                    self._plan_node_tasks[(owner_account_id, plan.team_session_id, node_id)] = task.id
                return
            workflow = store.create_workflow(
                session_id=plan.team_session_id,
                title=plan.goal,
                context=context,
            )
            self._plan_workflows[key] = workflow.id
            node_to_task: dict[str, str] = {}
            parent_map: dict[str, list[str]] = {}
            for edge in plan.edges:
                parent_map.setdefault(edge.child_id, []).append(edge.parent_id)
            for node in plan.nodes.values():
                parent_task_ids = [
                    node_to_task[parent_id]
                    for parent_id in parent_map.get(node.node_id, [])
                    if parent_id in node_to_task
                ]
                task = store.add_task(
                    workflow.id,
                    title=node.title,
                    detail=node.detail,
                    assignee=node.assignee,
                    parent_task_ids=parent_task_ids or None,
                    status=self._kanban_status(node.status),
                    auto_promote=False,
                )
                node_to_task[node.node_id] = task.id
                self._plan_node_tasks[(owner_account_id, plan.team_session_id, node.node_id)] = task.id
            store.add_event(
                workflow.id,
                "team_plan_created",
                actor="team_runtime",
                payload={
                    **event_payload,
                    "node_task_ids": node_to_task,
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("TeamPlan 同步到 kanban store 失败 session=%s err=%s", plan.team_session_id, exc)

    def _sync_kanban_node(self, plan: TeamPlan, node: TeamPlanNode, owner_account_id: str = "") -> None:
        store = self._kanban_store_for_owner(owner_account_id)
        if store is None:
            return
        key = self._key(plan.team_session_id, owner_account_id)
        workflow_id = self._plan_workflows.get(key)
        task_id = self._plan_node_tasks.get((owner_account_id, plan.team_session_id, node.node_id))
        if not workflow_id or not task_id:
            return
        try:
            store.update_task_status(
                task_id,
                self._kanban_status(node.status),
                result_summary=node.result_summary or node.last_error or None,
                artifacts=node.artifact_refs or None,
            )
            store.add_event(
                workflow_id,
                "team_node_updated",
                task_id=task_id,
                actor=node.assignee or "team_runtime",
                payload={
                    "team_plan_id": plan.plan_id,
                    "node_id": node.node_id,
                    "status": node.status,
                    "result_summary": node.result_summary,
                    "last_error": node.last_error,
                    "delegate_task_id": node.delegate_task_id,
                    "attempt_count": node.attempt_count,
                    "metadata": dict(node.metadata or {}),
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("TeamPlan 节点同步到 kanban store 失败 session=%s node=%s err=%s", plan.team_session_id, node.node_id, exc)

    def _record_team_event(
        self,
        session_id: str,
        *,
        owner_account_id: str = "",
        event_type: str,
        actor: str,
        payload: dict[str, Any],
        node_id: str = "",
    ) -> None:
        store = self._kanban_store_for_owner(owner_account_id)
        if store is None:
            return
        key = self._key(session_id, owner_account_id)
        workflow_id = self._plan_workflows.get(key)
        if not workflow_id:
            return
        task_id = self._plan_node_tasks.get((owner_account_id, session_id, node_id)) if node_id else None
        try:
            store.add_event(workflow_id, event_type, task_id=task_id, actor=actor, payload=payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("Team 事件同步到 kanban store 失败 session=%s type=%s err=%s", session_id, event_type, exc)

    async def _handle_team_mention(
        self,
        session_id: str,
        owner_account_id: str,
        event: dict[str, Any],
        *,
        teammates: dict[str, Agent] | None = None,
        bus: TeamBus | None = None,
        on_task_created: Callable[[dict[str, Any]], None] | None = None,
        on_task_finished: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Record a structured @mention and bridge @user to the followup UI."""

        intent = str(event.get("intent") or "broadcast")
        result_status = require_team_result_status(intent, event.get("result_status"))
        if intent == "assign":
            return await self._handle_team_mention_assign(
                session_id,
                owner_account_id=owner_account_id,
                event=event,
                teammates=teammates or {},
                bus=bus,
                on_task_created=on_task_created,
                on_task_finished=on_task_finished,
            )
        event_type = {
            "assign": "team_assign",
            "submit": "team_submit",
            "handoff": "team_submit",
            "ack": "team_ack",
            "review": "team_review",
            "decision": "team_decision",
        }.get(intent, "team_decision")
        raw_from = str(event.get("from") or "agent")
        display_agent_id = CREW_BUILTIN_AGENT_ID if is_crew_builtin_display_id(raw_from) else raw_from
        payload = {
            "text": str(event.get("text") or event.get("content") or ""),
            "agent_id": display_agent_id,
            "agent_name": "Crew" if is_crew_builtin_display_id(raw_from) else raw_from,
            "agent_role": "",
            "source_session_id": f"{session_id}::{raw_from}",
            "is_leader": str(event.get("from") or "") == "leader",
            "display_mode": "chat",
            "event_type": event_type,
            "node_id": str(event.get("node_id") or ""),
            "mention_from": raw_from,
            "mention_to": list(event.get("to") or []),
            "mention_intent": intent,
            "result_status": result_status,
            "artifacts": list(event.get("artifacts") or []),
        }
        self._record_team_event(
            session_id,
            owner_account_id=owner_account_id,
            event_type=event_type,
            actor=payload["agent_id"],
            node_id=payload["node_id"],
            payload=payload,
        )
        if "leader" in payload["mention_to"] and payload["mention_from"] != "leader":
            ack_payload = {
                "text": f"@{payload['mention_from']} 已收到你的消息，我会结合当前计划状态处理。",
                "agent_id": "leader",
                "agent_name": "leader",
                "agent_role": "leader",
                "source_session_id": f"{session_id}::leader",
                "is_leader": True,
                "display_mode": "chat",
                "event_type": "team_ack",
                "node_id": payload["node_id"],
                "mention_from": "leader",
                "mention_to": [payload["mention_from"]],
                "mention_intent": "ack",
                "artifacts": [],
            }
            self._record_team_event(
                session_id,
                owner_account_id=owner_account_id,
                event_type="team_ack",
                actor="leader",
                node_id=payload["node_id"],
                payload=ack_payload,
            )
        if "user" in payload["mention_to"] and payload["mention_from"] == "leader":
            questions = list(event.get("questions") or [])
            if not questions:
                questions = [{
                    "id": "user_confirmation",
                    "question": str(event.get("content") or event.get("text") or "请确认后续处理方式"),
                    "options": ["确认", "需要调整"],
                }]
            visible_session_id = _visible_session_id(session_id)
            followup_session_id, question_id = await send_followup_question_to(
                visible_session_id,
                questions,
                title=str(event.get("title") or "Leader 需要确认"),
                origin={
                    "agent_id": "leader",
                    "agent_name": "Leader",
                    "team_session_id": session_id,
                    "node_id": payload["node_id"],
                    "mention_intent": payload["mention_intent"],
                },
            )
            try:
                answers = await wait_for_answer(followup_session_id, question_id)
            except TypeError:
                answers = await wait_for_answer(followup_session_id, question_id)
            return {"followup_question_id": question_id, "answers": answers}
        return {}

    async def _handle_team_mention_assign(
        self,
        session_id: str,
        *,
        owner_account_id: str,
        event: dict[str, Any],
        teammates: dict[str, Agent],
        bus: TeamBus | None = None,
        on_task_created: Callable[[dict[str, Any]], None] | None = None,
        on_task_finished: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        if str(event.get("from") or "") != "leader":
            raise ToolError("只有 Leader 可以使用 team_mention(assign) 派发 TeamPlan 节点。")
        targets = [
            str(target or "").strip()
            for target in list(event.get("to") or [])
            if str(target or "").strip() not in {"", "all", "leader", "user"}
        ]
        targets = list(dict.fromkeys(targets))
        if len(targets) != 1:
            raise ToolError("team_mention(assign) 必须且只能 @ 一个具体团队成员。")
        member = targets[0]
        node_id = str(event.get("node_id") or "").strip()
        if not node_id:
            raise ToolError("team_mention(assign) 必须绑定现有 TeamPlan node_id；如需新增工作，请先 request_plan_change(add_node)。")
        instruction = str(event.get("content") or event.get("text") or "").strip()
        result = await self._execute_team_plan_assignment(
            session_id,
            owner_account_id=owner_account_id,
            member=member,
            instruction=instruction,
            plan_node_id=node_id,
            teammates=teammates,
            bus=bus,
            on_task_created=on_task_created,
            on_task_finished=on_task_finished,
            task_payload_meta=(
                dict(event.get("task_payload_meta"))
                if isinstance(event.get("task_payload_meta"), dict)
                else None
            ),
            require_plan=True,
            source="mention",
        )
        return {
            "assigned": True,
            "member": member,
            "node_id": node_id,
            "result": result["result"],
            "message": "mention assign 已按 TeamPlan 节点执行完成。",
        }

    async def _execute_team_plan_assignment(
        self,
        session_id: str,
        *,
        owner_account_id: str,
        member: str,
        instruction: str,
        plan_node_id: str,
        teammates: dict[str, Agent],
        bus: TeamBus | None = None,
        on_task_created: Callable[[dict[str, Any]], None] | None = None,
        on_task_finished: Callable[[dict[str, Any]], None] | None = None,
        task_payload_meta: dict[str, Any] | None = None,
        require_plan: bool = False,
        source: str = "mention",
    ) -> dict[str, Any]:
        node_id = str(plan_node_id or "").strip()
        plan = self._plans.get(self._existing_plan_key(session_id, owner_account_id))
        if plan is None:
            if require_plan:
                raise ToolError("当前 Team session 尚未创建 TeamPlan，不能通过 mention assign 任意派活。")
            final_text = await run_delegate_to_teammate(
                teammates,
                self.tasks,
                session_id,
                member=member,
                instruction=instruction,
                requester_member_id="leader",
                plan_node_id=node_id,
                bus=bus,
                on_child_start=self._mark_child_active,
                on_child_done=self._mark_child_done,
                on_task_created=on_task_created,
                on_task_finished=on_task_finished,
                owner_account_id=owner_account_id,
                task_payload_meta=task_payload_meta,
            )
            return {"result": final_text, "node_id": node_id, "member": member, "legacy": True}

        self._guard_delegate_against_plan(
            session_id,
            owner_account_id=owner_account_id,
            member=member,
            plan_node_id=node_id,
        )
        node = plan.nodes[node_id]
        effective_instruction = str(instruction or node.detail or node.title).strip()
        if not effective_instruction:
            raise ToolError("assignment instruction 不能为空。")
        if member not in teammates:
            raise ToolError(f"未知或不可委派成员: {member}")

        assign_payload = {
            "text": f"@{member} {effective_instruction}".strip(),
            "agent_id": "leader",
            "agent_name": "leader",
            "agent_role": "leader",
            "source_session_id": f"{session_id}::leader",
            "is_leader": True,
            "display_mode": "chat",
            "event_type": "team_assign",
            "node_id": node_id,
            "mention_from": "leader",
            "mention_to": [member],
            "mention_intent": "assign",
            "assignment_source": source,
            "artifacts": [],
        }
        self._record_team_event(
            session_id,
            owner_account_id=owner_account_id,
            event_type="team_assign",
            actor="leader",
            node_id=node_id,
            payload=assign_payload,
        )

        final_text = await run_delegate_to_teammate(
            teammates,
            self.tasks,
            session_id,
            member=member,
            instruction=effective_instruction,
            requester_member_id="leader",
            plan_node_id=node_id,
            bus=bus,
            on_child_start=self._mark_child_active,
            on_child_done=self._mark_child_done,
            on_task_created=on_task_created,
            on_task_finished=on_task_finished,
            owner_account_id=owner_account_id,
            task_payload_meta=task_payload_meta,
        )
        submit_payload = {
            "text": f"@leader {final_text}".strip(),
            "agent_id": member,
            "agent_name": member,
            "agent_role": node.title,
            "source_session_id": f"{session_id}::{member}",
            "is_leader": False,
            "display_mode": "chat",
            "event_type": "team_submit",
            "node_id": node_id,
            "mention_from": member,
            "mention_to": ["leader"],
            "mention_intent": "submit",
            "assignment_source": source,
            "artifacts": [],
        }
        self._record_team_event(
            session_id,
            owner_account_id=owner_account_id,
            event_type="team_submit",
            actor=member,
            node_id=node_id,
            payload=submit_payload,
        )
        return {"result": final_text, "node_id": node_id, "member": member, "legacy": False}

    def _team_workflow_ids_for_session(self, session_id: str, owner_account_id: str = "") -> list[str]:
        store = self._kanban_store_for_owner(owner_account_id)
        if store is None:
            return []
        sid = str(session_id or "").strip()
        if not sid:
            return []
        prefix = f"{sid}::turn::"
        workflow_ids = [
            workflow_id
            for key, workflow_id in self._plan_workflows.items()
            if key[0] == owner_account_id and (key[1] == sid or key[1].startswith(prefix))
        ]
        if not workflow_ids:
            try:
                workflows = store.list_workflows_by_session_prefix(sid)
            except Exception:  # noqa: BLE001
                workflows = []
            for workflow in workflows:
                context = dict(getattr(workflow, "context", {}) or {})
                if context.get("source") != "team":
                    continue
                if str(context.get("owner_account_id") or "") != str(owner_account_id or ""):
                    continue
                workflow_ids.append(str(workflow.id))
        return list(dict.fromkeys(workflow_ids))

    def has_team_workflow_for_session(self, session_id: str, owner_account_id: str = "") -> bool:
        return bool(self._team_workflow_ids_for_session(session_id, owner_account_id))

    @staticmethod
    def _node_dict_is_review_submission(node: dict[str, Any]) -> bool:
        return team_presenter.node_dict_is_review_submission(node)

    @staticmethod
    def _node_dict_is_verify_execution(node: dict[str, Any]) -> bool:
        return team_presenter.node_dict_is_verify_execution(node)

    @staticmethod
    def _node_dict_assignment_text(node: dict[str, Any]) -> str:
        return team_presenter.node_dict_assignment_text(node)

    @staticmethod
    def _node_dict_should_show_assignment(
        node: dict[str, Any],
        edges: list[dict[str, Any]],
    ) -> bool:
        return team_presenter.node_dict_should_show_assignment(node, edges)

    def event_history_for_session(self, session_id: str, owner_account_id: str = "") -> list[dict[str, Any]]:
        store = self._kanban_store_for_owner(owner_account_id)
        if store is None:
            return []
        sid = str(session_id or "").strip()
        if not sid:
            return []
        workflow_ids = self._team_workflow_ids_for_session(sid, owner_account_id)
        items: list[dict[str, Any]] = []
        for workflow_id in workflow_ids:
            try:
                events = store.list_events(workflow_id, limit=500)
            except Exception:  # noqa: BLE001
                continue
            for event in events:
                payload = dict(event.payload or {})
                if event.event_type not in {
                    "team_assign",
                    "team_stream",
                    "team_submit",
                    "team_ack",
                    "team_review",
                    "team_decision",
                    "team_summary",
                }:
                    continue
                text = str(payload.get("text") or "").strip()
                if _is_team_chat_noise(text):
                    continue
                items.append({
                    "role": "team_internal",
                    "content": text[:1200],
                    "agent_id": str(payload.get("agent_id") or event.actor or "agent"),
                    "agent_name": str(payload.get("agent_name") or payload.get("agent_id") or event.actor or "Agent"),
                    "agent_role": str(payload.get("agent_role") or ""),
                    "agent_tone": payload.get("agent_tone"),
                    "is_leader": bool(payload.get("is_leader")),
                    "source_session_id": str(payload.get("source_session_id") or ""),
                    "node_id": str(payload.get("node_id") or ""),
                    "event_type": str(payload.get("event_type") or event.event_type),
                    "display_mode": str(payload.get("display_mode") or "chat"),
                    "collapsed_title": str(payload.get("collapsed_title") or ""),
                    "process_text": str(payload.get("process_text") or ""),
                    "thinking": _normalize_legacy_chunked_thinking(str(payload.get("thinking") or "")),
                    "tool_calls": list(payload.get("tool_calls") or []),
                    "artifacts": list(payload.get("artifacts") or []),
                    "turn_file_changes": list(payload.get("turn_file_changes") or []),
                    "mention_from": str(payload.get("mention_from") or ""),
                    "mention_to": list(payload.get("mention_to") or []),
                    "mention_intent": str(payload.get("mention_intent") or ""),
                    "timestamp": float(event.ts or 0),
                    **({"turn_started_at": payload.get("turn_started_at")} if payload.get("turn_started_at") is not None else {}),
                    **({"turn_duration": payload.get("turn_duration")} if payload.get("turn_duration") is not None else {}),
                })
        items.sort(key=lambda item: float(item.get("timestamp") or 0))
        return items

    def update_plan_node(
        self,
        session_id: str,
        *,
        node_id: str,
        status: str | None = None,
        result_summary: str | None = None,
        artifact_refs: list[str] | None = None,
        delegate_task_id: str | None = None,
        attempt_count: int | None = None,
        last_error: str | None = None,
        allow_reopen: bool = False,
        owner_account_id: str = "",
    ) -> dict[str, Any]:
        plan = self._plans.get(self._existing_plan_key(session_id, owner_account_id))
        if plan is None:
            raise ValueError("当前 Team session 尚未创建 TeamPlan")
        node = plan.nodes.get(str(node_id or "").strip())
        if node is None:
            raise ValueError(f"未知 TeamPlan 节点: {node_id}")
        node.update(
            status=status,
            result_summary=result_summary,
            artifact_refs=artifact_refs,
            delegate_task_id=delegate_task_id,
            attempt_count=attempt_count,
            last_error=last_error,
            allow_reopen=allow_reopen,
        )
        plan.updated_at = node.updated_at
        if plan.nodes and all(n.status == "completed" for n in plan.nodes.values()):
            plan.status = "completed"
        elif any(n.status in {"failed", "blocked", "needs_info"} for n in plan.nodes.values()):
            plan.status = "active"
        self._sync_kanban_node(plan, node, owner_account_id=owner_account_id)
        return {"ok": True, "plan": plan.to_dict(), "node": node.to_dict()}

    def _mark_plan_node(self, session_id: str, node_id: str, owner_account_id: str = "", **updates: Any) -> None:
        if not node_id:
            return
        try:
            self.update_plan_node(session_id, node_id=node_id, owner_account_id=owner_account_id, **updates)
        except Exception as exc:  # noqa: BLE001
            log.warning("更新 TeamPlan 节点失败 session=%s node=%s err=%s", session_id, node_id, exc)

    def _guard_delegate_against_plan(
        self,
        session_id: str,
        *,
        owner_account_id: str = "",
        member: str,
        plan_node_id: str,
    ) -> None:
        plan = self._plans.get(self._existing_plan_key(session_id, owner_account_id))
        if plan is None:
            return
        member_id = str(member or "").strip()
        node_id = str(plan_node_id or "").strip()
        if not node_id:
            raise ToolError(
                "当前 TeamPlan 已存在，delegate_to_teammate 必须绑定现有 plan_node_id；"
                "如需新增或重拆任务，请先变更 DAG。"
            )
        node = plan.nodes.get(node_id)
        if node is None:
            raise ToolError(f"未知 TeamPlan 节点: {node_id}；如需新增任务，请先变更 DAG。")
        if node.assignee == "leader":
            raise ToolError(f"TeamPlan 节点 {node_id} 是 Leader 控制节点，不能直接委派给成员。")
        if str(node.assignee or "").strip() != member_id:
            raise ToolError(
                f"TeamPlan 节点 {node_id} 分配给 {node.assignee}，不能委派给 {member_id}；"
                "如需调整负责人，请先变更 DAG。"
            )
        if node.status not in {"pending", "failed"}:
            raise ToolError(
                f"TeamPlan 节点 {node_id} 当前状态为 {node.status}，不能重复委派；"
                "如需重跑或追加工作，请先变更 DAG。"
            )

    @staticmethod
    def _normalize_plan_node_refs(raw: Any) -> list[str]:
        if isinstance(raw, str):
            items = [raw]
        else:
            items = list(raw or [])
        refs: list[str] = []
        for item in items:
            value = str(item or "").strip()
            if value and value not in refs:
                refs.append(value)
        return refs

    @staticmethod
    def _plan_edges_have_cycle(node_ids: set[str], edges: list[TeamPlanEdge]) -> bool:
        outgoing: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
        indegree: dict[str, int] = {node_id: 0 for node_id in node_ids}
        for edge in edges:
            if edge.parent_id not in node_ids or edge.child_id not in node_ids:
                continue
            outgoing.setdefault(edge.parent_id, []).append(edge.child_id)
            indegree[edge.child_id] = indegree.get(edge.child_id, 0) + 1
        ready = [node_id for node_id, degree in indegree.items() if degree == 0]
        visited = 0
        while ready:
            node_id = ready.pop()
            visited += 1
            for child_id in outgoing.get(node_id, []):
                indegree[child_id] -= 1
                if indegree[child_id] == 0:
                    ready.append(child_id)
        return visited != len(node_ids)

    @staticmethod
    def _safe_plan_node_id(value: str, *, fallback: str = "node") -> str:
        text = re.sub(r"[^0-9A-Za-z_]+", "_", str(value or "").strip().lower()).strip("_")
        return text[:48] or fallback

    def _unique_plan_node_id(self, plan: TeamPlan, *, raw_node_id: str, assignee: str, title: str) -> str:
        base = self._safe_plan_node_id(raw_node_id) if raw_node_id else self._safe_plan_node_id(f"{assignee}_{title}")
        candidate = base
        index = 2
        while candidate in plan.nodes:
            candidate = f"{base}_{index}"
            index += 1
        return candidate

    def _consume_plan_change_requeue(self, node: TeamPlanNode) -> bool:
        metadata = dict(node.metadata or {})
        if not metadata.pop("plan_change_requeue", False):
            return False
        node.metadata = metadata
        return True

    def _apply_leader_plan_change(
        self,
        session_id: str,
        *,
        owner_account_id: str = "",
        change: dict[str, Any],
        valid_member_ids: set[str],
        member_specs: dict[str, TeamMemberSpec],
    ) -> dict[str, Any]:
        plan = self._plans.get(self._existing_plan_key(session_id, owner_account_id))
        if plan is None:
            raise ToolError("当前 Team session 尚未创建 TeamPlan，不能变更 DAG。")
        change_type = str(change.get("change_type") or "add_node").strip()
        if change_type != "add_node":
            raise ToolError(f"当前阶段仅支持 add_node 计划变更，不支持: {change_type}")

        assignee = str(change.get("assignee") or "").strip()
        if not assignee or assignee not in valid_member_ids:
            raise ToolError(f"新增节点 assignee 必须是当前团队成员，可选: {sorted(valid_member_ids)}")
        title = str(change.get("title") or "").strip()
        detail = str(change.get("detail") or "").strip()
        if not title:
            raise ToolError("新增节点 title 不能为空")
        if not detail:
            raise ToolError("新增节点 detail 不能为空")
        required_capabilities = normalize_capabilities(change.get("required_capabilities") or [])
        if not required_capabilities:
            raise ToolError("新增节点 required_capabilities 必须包含标准能力 key")

        parent_ids = self._normalize_plan_node_refs(change.get("depends_on"))
        before_ids = self._normalize_plan_node_refs(change.get("before"))
        if not parent_ids and "leader_plan" in plan.nodes:
            parent_ids = ["leader_plan"]
        if not before_ids and "leader_summary" in plan.nodes:
            before_ids = ["leader_summary"]

        unknown_refs = [node_id for node_id in [*parent_ids, *before_ids] if node_id not in plan.nodes]
        if unknown_refs:
            raise ToolError(f"计划变更引用了未知 TeamPlan 节点: {unknown_refs}")

        node_id = self._unique_plan_node_id(
            plan,
            raw_node_id=str(change.get("node_id") or ""),
            assignee=assignee,
            title=title,
        )
        if node_id in parent_ids or node_id in before_ids:
            raise ToolError("新增节点不能依赖或阻塞自身")

        for before_id in before_ids:
            before_node = plan.nodes[before_id]
            if before_node.assignee != "leader" and before_node.status in {"in_progress", "completed"}:
                raise ToolError(
                    f"不能把新增节点插入到已执行成员节点 {before_id} 之前；"
                    "请在后续阶段使用受控 split/retry。"
                )

        new_edges = list(plan.edges)
        existing_pairs = {(edge.parent_id, edge.child_id) for edge in new_edges}
        for parent_id in parent_ids:
            pair = (parent_id, node_id)
            if pair not in existing_pairs:
                new_edges.append(TeamPlanEdge(parent_id=parent_id, child_id=node_id))
                existing_pairs.add(pair)
        for before_id in before_ids:
            pair = (node_id, before_id)
            if pair not in existing_pairs:
                new_edges.append(TeamPlanEdge(parent_id=node_id, child_id=before_id))
                existing_pairs.add(pair)
        if self._plan_edges_have_cycle({*plan.nodes.keys(), node_id}, new_edges):
            raise ToolError("计划变更会形成 DAG 环，已拒绝。")

        member = member_specs.get(assignee)
        member_meta = dict(member.metadata or {}) if member is not None else {}
        workflow_lane = str(member_meta.get("workflow_lane") or "build")
        role_label = str(member_meta.get("role_label") or member.role if member is not None else "") or assignee
        metadata = team_presenter.node_display_progress(
            node_id=node_id,
            title=title,
            assignee=assignee,
            metadata={
                "workflow_lane": workflow_lane,
                "role_label": role_label,
                "role_key": str(member_meta.get("role_key") or ""),
                "plan_strategy": "leader_plan_change",
                "replan_kind": "leader_add_node",
                "plan_change_reason": str(change.get("reason") or "").strip(),
                "required_capabilities": required_capabilities,
                "capability_source": "leader_plan_change",
            },
        )
        node = TeamPlanNode(
            node_id=node_id,
            title=title,
            detail=detail,
            assignee=assignee,
            metadata={
                **metadata,
                "execution_events": [
                    {
                        "id": f"{node_id}:created",
                        "kind": "status",
                        "event_type": "plan_change",
                        "event_icon": "route",
                        "event_title": "DAG 变更",
                        "event_text": str(change.get("reason") or "Leader 已请求新增执行节点").strip(),
                        "collapsed": False,
                    }
                ],
            },
        )

        ordered: dict[str, TeamPlanNode] = {}
        inserted = False
        before_set = set(before_ids)
        for current_id, current_node in plan.nodes.items():
            if not inserted and current_id in before_set:
                ordered[node_id] = node
                inserted = True
            ordered[current_id] = current_node
        if not inserted:
            ordered[node_id] = node
        plan.nodes = ordered
        plan.edges = new_edges
        plan.status = "active"
        plan.updated_at = time.time()

        requeued_nodes: list[str] = []
        for before_id in before_ids:
            before_node = plan.nodes[before_id]
            if before_node.assignee != "leader":
                continue
            before_node.metadata = {
                **dict(before_node.metadata or {}),
                "plan_change_requeue": True,
                "plan_change_added_node_id": node_id,
            }
            if before_node.status in {"in_progress", "completed", "failed"}:
                before_node.update(
                    status="pending",
                    result_summary="",
                    delegate_task_id="",
                    last_error="",
                )
                requeued_nodes.append(before_id)
                self._sync_kanban_node(plan, before_node, owner_account_id=owner_account_id)

        self._sync_added_plan_node(
            plan,
            node,
            owner_account_id=owner_account_id,
            parent_node_ids=parent_ids,
            reason="leader_plan_change",
        )
        return {
            "ok": True,
            "change_type": "add_node",
            "node": node.to_dict(),
            "depends_on": parent_ids,
            "before": before_ids,
            "requeued_nodes": requeued_nodes,
            "message": f"已新增 TeamPlan 节点 {node_id}，Runtime 将按 DAG 后续派发。",
        }

    def _append_plan_node_event(
        self,
        plan: TeamPlan,
        node: TeamPlanNode,
        *,
        owner_account_id: str = "",
        event: dict[str, Any],
    ) -> None:
        metadata = dict(node.metadata or {})
        events = list(metadata.get("execution_events") or [])
        event_id = str(event.get("id") or f"{node.node_id}:runtime:{len(events) + 1}")
        events.append({
            "id": event_id,
            "kind": str(event.get("kind") or "status"),
            "event_type": str(event.get("event_type") or event.get("kind") or "status"),
            "event_icon": str(event.get("event_icon") or "spark"),
            "event_title": str(event.get("event_title") or "运行事件"),
            "event_text": str(event.get("event_text") or ""),
            "collapsed": bool(event.get("collapsed", False)),
        })
        metadata["execution_events"] = events[-12:]
        node.metadata = metadata
        self._sync_kanban_node(plan, node, owner_account_id=owner_account_id)

    @staticmethod
    def _child_chunk_execution_event(node: TeamPlanNode, member: str, chunk: ResponseChunk) -> dict[str, Any] | None:
        if chunk.kind == "tool":
            name = str(chunk.body.get("ui_label") or chunk.body.get("name") or "工具调用").strip()
            phase = str(chunk.body.get("phase") or "").strip()
            tool_status = "running" if phase in {"generating", "start"} else ("error" if phase == "error" else "done")
            args = str(chunk.body.get("args") or "").strip()
            detail = str(chunk.body.get("detail") or "").strip()
            body = "\n".join(part for part in [args, detail] if part).strip()
            return {
                "id": f"{node.node_id}:tool:{chunk.body.get('tool_call_id') or chunk.sequence or len(body)}:{phase}",
                "kind": "tool",
                "event_type": "tool",
                "event_icon": "tool",
                "event_title": f"{member} 工具调用：{name}",
                "event_text": body or phase or "工具调用已记录。",
                "collapsed": True,
                "phase": phase,
                "status": tool_status,
                "tool_call": {
                    "id": str(chunk.body.get("tool_call_id") or chunk.sequence or len(body)),
                    "name": str(chunk.body.get("name") or name),
                    "ui_label": name,
                    "arguments": chunk.body.get("arguments") or chunk.body.get("args") or {},
                    "result": chunk.body.get("result") or chunk.body.get("detail") or "",
                    "status": tool_status,
                },
            }
        if chunk.kind == "thinking":
            text = str(chunk.body.get("text") or "")
            if not text.strip():
                return None
            return {
                "id": f"{node.node_id}:thinking:{chunk.sequence or len(text)}",
                "kind": "thinking",
                "event_type": "thinking",
                "event_icon": "thinking",
                "event_title": f"{member} 思考",
                "event_text": text,
                "collapsed": True,
            }
        if chunk.kind == "status":
            text = str(chunk.body.get("message") or "").strip()
            if not text:
                return None
            return {
                "id": f"{node.node_id}:status:{chunk.sequence or len(text)}",
                "kind": "status",
                "event_type": "status",
                "event_icon": "route",
                "event_title": f"{member} 状态",
                "event_text": text,
                "collapsed": True,
            }
        return None

    @staticmethod
    def _assess_node_execution(
        node: TeamPlanNode,
        *,
        runtime_events: list[dict[str, Any]],
        artifact_refs: list[str],
        changed_paths: set[str],
        result_contract: dict[str, str],
    ) -> NodeExecutionAssessment:
        """Separate transport completion from evidence-backed node completion."""
        failed_events: list[tuple[int, str, str]] = []
        successful_indexes: list[int] = []
        for index, event in enumerate(runtime_events):
            if str(event.get("event_type") or "") != "tool":
                continue
            tool_call = event.get("tool_call") if isinstance(event.get("tool_call"), dict) else {}
            status = str(tool_call.get("status") or event.get("status") or "").strip().lower()
            name = str(tool_call.get("name") or event.get("event_title") or "工具调用").strip()
            detail = str(tool_call.get("result") or event.get("event_text") or "").strip()
            if status == "error":
                failed_events.append((index, name, detail))
            elif status == "done":
                successful_indexes.append(index)

        artifact_count = len([ref for ref in artifact_refs if str(ref).strip()])
        changed_file_count = len([path for path in changed_paths if str(path).strip()])
        has_material_evidence = artifact_count > 0 or changed_file_count > 0
        lane = str((node.metadata or {}).get("workflow_lane") or "other").strip().lower()
        material_lane = lane in {"build", "verify", "docs", "release"}
        last_success = max(successful_indexes, default=-1)
        unrecovered = [item for item in failed_events if item[0] > last_success]
        if material_lane and has_material_evidence:
            # A later observable workspace change/artifact is stronger recovery
            # evidence than an earlier transport-level tool error.
            unrecovered = []
        elif material_lane and failed_events:
            unrecovered = failed_events

        signal = str(result_contract.get("status_signal") or "unknown").strip().lower()
        if unrecovered:
            failed_tools = tuple(dict.fromkeys(item[1] for item in unrecovered))
            failure_text = " ".join(item[2] for item in unrecovered).lower()
            permission_denied = any(marker in failure_text for marker in (
                "permission denied",
                "rejected by user",
                "not allowed",
                "权限被拒绝",
                "用户拒绝",
                "不允许",
            ))
            return NodeExecutionAssessment(
                execution_status="blocked" if permission_denied else "failed",
                acceptance_status="blocked" if permission_denied else "fail",
                reason=(
                    "用户拒绝了完成该节点所需的操作权限。"
                    if permission_denied
                    else f"存在未恢复的工具失败：{'、'.join(failed_tools)}。"
                ),
                failed_tools=failed_tools,
                artifact_count=artifact_count,
                changed_file_count=changed_file_count,
            )

        if (
            signal == "blocked"
            and not (
                team_presenter.is_review_submission_node(node)
                and has_material_evidence
            )
        ):
            return NodeExecutionAssessment(
                execution_status="blocked",
                acceptance_status="blocked",
                reason="成员结构化结果表明当前节点仍被阻塞。",
                artifact_count=artifact_count,
                changed_file_count=changed_file_count,
            )

        acceptance_status = signal if signal in {"pass", "fail"} else ("pass" if has_material_evidence else "unknown")
        reason = (
            "执行完成，并取得可检查的产物或工作区变更。"
            if has_material_evidence
            else "执行完成；当前节点没有要求可落盘产物，保留结果契约供 Leader 验收。"
        )
        return NodeExecutionAssessment(
            execution_status="completed",
            acceptance_status=acceptance_status,
            reason=reason,
            artifact_count=artifact_count,
            changed_file_count=changed_file_count,
        )

    @staticmethod
    def _runtime_result_status(
        node: TeamPlanNode,
        runtime_events: list[dict[str, Any]],
    ) -> str:
        """Read the latest completed structured Team submission for this attempt."""

        calls: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for event in runtime_events:
            tool_call = event.get("tool_call")
            if str(event.get("event_type") or "") != "tool" or not isinstance(tool_call, dict):
                continue
            call_id = str(tool_call.get("id") or "").strip()
            if not call_id:
                continue
            if call_id not in calls:
                order.append(call_id)
            previous = calls.get(call_id, {})
            calls[call_id] = {
                **previous,
                **tool_call,
                "arguments": tool_call.get("arguments") or previous.get("arguments") or {},
            }

        for call_id in reversed(order):
            call = calls[call_id]
            if str(call.get("status") or "").strip().lower() != "done":
                continue
            name = str(call.get("name") or call.get("ui_label") or "").strip().lower()
            if not name.endswith("team_mention"):
                continue
            raw_arguments = call.get("arguments")
            if isinstance(raw_arguments, dict):
                arguments = raw_arguments
            elif isinstance(raw_arguments, str):
                try:
                    decoded = json.loads(raw_arguments)
                except (TypeError, ValueError):
                    continue
                arguments = decoded if isinstance(decoded, dict) else {}
            else:
                continue
            if str(arguments.get("intent") or "").strip().lower() != "submit":
                continue
            submitted_node_id = str(arguments.get("node_id") or "").strip()
            if submitted_node_id and submitted_node_id != node.node_id:
                continue
            result_status = str(arguments.get("result_status") or "").strip().lower()
            if result_status in TEAM_RESULT_STATUSES:
                return result_status
        return ""

    def _record_external_agent_profile_observation(
        self,
        plan: TeamPlan,
        node: TeamPlanNode,
        *,
        owner_account_id: str,
        outcome: str,
        quality_weight: float,
        assessment_source: str,
        failure_kind: str = "",
        source_attempt_id: str = "",
    ) -> bool:
        """Persist one settled External Agent execution fact without blocking the workflow."""

        if self.external_store is None or node.assignee == "leader":
            return False
        team = self._teams.get(self._existing_team_key(plan.team_session_id, owner_account_id))
        spec = team.members.get(node.assignee) if team is not None else None
        external_agent_id = str(spec.external_agent_id or "").strip() if spec is not None else ""
        capabilities = normalize_capabilities((node.metadata or {}).get("required_capabilities") or [])
        attempt_id = str(source_attempt_id or node.delegate_task_id or "").strip()
        if not external_agent_id or is_crew_builtin_agent(external_agent_id):
            return False
        if not capabilities or not attempt_id:
            return False
        try:
            result = self.external_store.record_agent_profile_observation(
                owner_account_id=owner_account_id,
                external_agent_id=external_agent_id,
                source_run_id=plan.plan_id,
                source_node_id=node.node_id,
                source_attempt_id=attempt_id,
                capabilities=capabilities,
                assessment_source=assessment_source,
                outcome=outcome,
                quality_weight=quality_weight,
                failure_kind=failure_kind,
            )
            return bool(result.get("inserted"))
        except Exception as exc:  # noqa: BLE001 - 画像派生失败不能改变用户任务结果
            log.warning(
                "AgentProfile observation 写入失败 session=%s node=%s agent=%s err=%s",
                plan.team_session_id,
                node.node_id,
                external_agent_id,
                exc,
            )
            return False

    @staticmethod
    def _profile_outcome_from_execution(
        assessment: NodeExecutionAssessment,
    ) -> tuple[str, float, str]:
        if assessment.execution_status != "completed":
            failure_kind = "permission" if assessment.execution_status == "blocked" else "tool"
            return "neutral", 0.0, failure_kind
        if assessment.acceptance_status == "fail":
            return "failure", 0.8, "acceptance"
        if assessment.acceptance_status == "pass":
            has_material_evidence = assessment.artifact_count > 0 or assessment.changed_file_count > 0
            return "success", 0.8 if has_material_evidence else 0.4, ""
        return "neutral", 0.0, "unverified"

    @staticmethod
    def _runtime_staffing_request(node: TeamPlanNode) -> RuntimeStaffingRequest | None:
        raw = (node.metadata or {}).get("runtime_staffing")
        return RuntimeStaffingRequest.from_dict(raw) if isinstance(raw, dict) else None

    @staticmethod
    def _runtime_staffing_request_id(
        plan: TeamPlan,
        node: TeamPlanNode,
        *,
        trigger_type: str,
        required_capabilities: list[str],
    ) -> str:
        identity = "\x1f".join([
            plan.plan_id,
            node.node_id,
            trigger_type,
            ",".join(normalize_capabilities(required_capabilities)),
            str(node.attempt_count),
            str(node.delegate_task_id or ""),
        ])
        return f"staffing_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:20]}"

    def _runtime_staffing_trigger(
        self,
        team: Team,
        node: TeamPlanNode,
        *,
        owner_account_id: str,
        max_attempts: int,
    ) -> dict[str, Any] | None:
        """Return one hard Runtime staffing gap; low confidence alone is not a trigger."""

        if self.external_store is None or node.assignee == "leader":
            return None
        required = normalize_capabilities((node.metadata or {}).get("required_capabilities") or [])
        if not required:
            return None

        explicit_trigger = str((node.metadata or {}).get("runtime_staffing_trigger") or "").strip()
        if explicit_trigger:
            return {
                "trigger_type": explicit_trigger,
                "required_capabilities": required,
                "reason": str(
                    (node.metadata or {}).get("runtime_staffing_trigger_reason")
                    or "Leader 审阅修订已耗尽，需要更换执行成员。"
                ),
            }
        if node.assignee not in team.teammates:
            return {
                "trigger_type": "unknown_assignee",
                "required_capabilities": required,
                "reason": f"节点指向未知或不可委派成员 {node.assignee}。",
            }
        if node.attempt_count >= max_attempts:
            return {
                "trigger_type": "acceptance_exhausted",
                "required_capabilities": required,
                "reason": f"节点已连续失败 {node.attempt_count} 次，达到自动重试上限。",
            }

        assigned = team.members.get(node.assignee)
        if assigned is not None and assigned.executor == "external" and assigned.external_agent_id:
            try:
                assigned_agent = self.external_store.get_agent(
                    assigned.external_agent_id,
                    owner_account_id=owner_account_id,
                )
                assigned_ready = bool(rank_staffing_candidates(required, [assigned_agent], limit=1))
            except Exception:  # noqa: BLE001 - 不可读取本身就是运行时不可用事实
                assigned_ready = False
            if not assigned_ready:
                return {
                    "trigger_type": "agent_unavailable",
                    "required_capabilities": required,
                    "reason": f"当前成员 {node.assignee} 的 Runtime/model 不可用或画像已不满足节点硬能力。",
                }

        covered: set[str] = set()
        current_agents: list[dict[str, Any]] = []
        for spec in team.members.values():
            if spec.executor == "external" and spec.external_agent_id:
                try:
                    current_agents.append(self.external_store.get_agent(
                        spec.external_agent_id,
                        owner_account_id=owner_account_id,
                    ))
                except Exception as exc:  # noqa: BLE001
                    log.debug("跳过不可读取的 Runtime Team Agent agent=%s err=%s", spec.external_agent_id, exc)
                    continue
            else:
                covered.update(normalize_capabilities(spec.capabilities))
        for capability in required:
            if rank_staffing_candidates([capability], current_agents, limit=1):
                covered.add(capability)
        missing = [capability for capability in required if capability not in covered]
        if missing:
            return {
                "trigger_type": "capability_gap",
                "required_capabilities": missing,
                "reason": f"当前 Runtime Team 缺少硬能力：{'、'.join(missing)}。",
            }
        return None

    def _runtime_staffing_candidates(
        self,
        team: Team,
        *,
        owner_account_id: str,
        required_capabilities: list[str],
    ) -> list[dict[str, Any]]:
        if self.external_store is None:
            return []
        excluded_agent_ids = {
            str(spec.external_agent_id or "").strip()
            for spec in team.members.values()
            if str(spec.external_agent_id or "").strip()
        }
        candidates = rank_staffing_candidates(
            required_capabilities,
            self.external_store.list_agents(owner_account_id=owner_account_id, include_managed=True),
            excluded_agent_ids=excluded_agent_ids,
            limit=3,
        )
        if len(candidates) >= 3:
            return candidates
        options = ready_runtime_model_options(self.external_store.list_runtimes())
        recommended = recommend_runtime_model(
            options,
            required_capabilities=required_capabilities,
        )
        if recommended is None:
            return candidates
        role_key = role_key_for_capabilities(required_capabilities)
        preset = role_preset(role_key)
        candidates.append({
            "candidate_type": "runtime",
            "selection_source": "new_managed_agent",
            "runtime_id": str(recommended.get("runtime_id") or ""),
            "runtime_name": str(recommended.get("runtime_name") or recommended.get("runtime_id") or "Runtime"),
            "model_id": str(recommended.get("model_id") or ""),
            "role_key": role_key,
            "role_label": str(preset.get("label") or role_key),
            "covered_capabilities": list(required_capabilities),
            "profile_version": 0,
            "reason": "创建一个隐藏的 Runtime 托管 Agent；仅挂载到本次 WorkflowRun，后续实证继续更新其 AgentProfile。",
        })
        return candidates[:3]

    @staticmethod
    def _runtime_staffing_answer(answers: list[dict[str, Any]]) -> str:
        if not answers or any(
            str(item.get("id") or "") == CANCELLED_MARKER
            for item in answers
            if isinstance(item, dict)
        ):
            return ""
        for item in answers:
            values = item.get("answers") if isinstance(item, dict) else None
            if not isinstance(values, list):
                continue
            for value in values:
                text = str(value or "").strip()
                if text:
                    return text
        return ""

    @staticmethod
    def _runtime_staffing_user_reason(trigger_type: str) -> str:
        return {
            "agent_unavailable": "当前负责这项工作的成员暂时无法使用。",
            "capability_gap": "当前团队还缺少完成这项工作所需的能力。",
            "acceptance_exhausted": "这项工作已经尝试了几次仍未通过，换位助手接手会更稳妥。",
            "review_exhausted": "这项工作经过多次修改仍未通过，换位助手接手会更稳妥。",
            "unknown_assignee": "原定成员现在无法接手这项工作。",
        }.get(trigger_type, "这项工作暂时缺少合适的执行成员。")

    def _runtime_staffing_candidate_option(
        self,
        candidate: dict[str, Any],
        *,
        index: int,
        role_label: str,
    ) -> dict[str, str]:
        model_label = str(candidate.get("model_id") or "").strip()
        if candidate.get("candidate_type") == "runtime":
            runtime_label = str(
                candidate.get("runtime_name") or candidate.get("runtime_id") or "可用 Runtime"
            ).strip()
            description = f"负责{role_label}；使用 {runtime_label}"
            if model_label:
                description += f" · {model_label}"
            description += "，首次参与这项任务"
            label = "新建一位协作助手"
        else:
            selection_source = str(candidate.get("selection_source") or "")
            label = (
                str(candidate.get("name") or "现有协作助手")
                if selection_source == "existing_agent"
                else "现有协作助手"
            )
            description = f"适合{role_label}，有相关能力记录，可以立即开始"
            if model_label:
                description += f" · {model_label}"
        if index == 0:
            label = f"{label}（推荐）"
        return {
            "label": label,
            "value": f"candidate:{index}",
            "description": description,
        }

    def _runtime_staffing_member_spec(
        self,
        team: Team,
        plan: TeamPlan,
        request: RuntimeStaffingRequest,
        candidate: dict[str, Any],
        *,
        owner_account_id: str,
    ) -> TeamMemberSpec:
        if self.external_store is None:
            raise ToolError("External Agent Store 未启用")
        required = normalize_capabilities(request.required_capabilities)
        role_key = str(candidate.get("role_key") or role_key_for_capabilities(required))
        preset = role_preset(role_key)
        if candidate.get("candidate_type") == "runtime":
            runtime_id = str(candidate.get("runtime_id") or "").strip()
            model_id = str(candidate.get("model_id") or "").strip()
            managed_identity = f"{runtime_id}\x1f{model_id}\x1f{role_key}"
            managed_key = hashlib.sha256(managed_identity.encode("utf-8")).hexdigest()
            name = f"Runtime 外援·{preset.get('label') or role_key}"
            generic_prompt = intelligent_role_markdown(
                role_key=role_key,
                agent_name=name,
                team_goal="根据每次 WorkflowPlan 节点上下文完成受控任务",
                assigned_capabilities=required,
            )
            agent = self.external_store.get_or_create_managed_agent(
                owner_account_id=owner_account_id,
                managed_kind="runtime_staffing",
                managed_key=managed_key,
                name=name,
                runtime_id=runtime_id,
                model=model_id,
                system_prompt=generic_prompt,
            )
        else:
            agent = self.external_store.get_agent(
                str(candidate.get("external_agent_id") or ""),
                owner_account_id=owner_account_id,
            )
            name = str(agent.get("name") or agent.get("id") or "Runtime 外援")

        external_agent_id = str(agent.get("id") or "").strip()
        if not external_agent_id:
            raise ToolError("补员候选缺少 External Agent id")
        member_id = name
        if member_id in team.members and team.members[member_id].external_agent_id != external_agent_id:
            member_id = f"{name}_{external_agent_id[-6:]}"
        role_markdown = intelligent_role_markdown(
            role_key=role_key,
            agent_name=name,
            team_goal=plan.goal,
            assigned_capabilities=required,
        )
        return TeamMemberSpec(
            member_id=member_id,
            name=name,
            role=str(preset.get("description") or "Runtime 动态补员"),
            executor="external",
            external_agent_id=external_agent_id,
            model=str(agent.get("model") or candidate.get("model_id") or ""),
            capabilities=required,
            system_prompt=role_markdown,
            metadata={
                "role_key": role_key,
                "role_label": str(preset.get("label") or role_key),
                "workflow_lane": str(preset.get("workflow_lane") or "build"),
                "selection_source": str(candidate.get("selection_source") or "runtime_staffing"),
                "runtime_staffing": True,
                "staffing_request_id": request.request_id,
            },
        )

    def _persist_runtime_staffing_revision(
        self,
        plan: TeamPlan,
        node: TeamPlanNode,
        team: Team,
        request: RuntimeStaffingRequest,
        *,
        owner_account_id: str,
    ) -> None:
        store = self._kanban_store_for_owner(owner_account_id)
        key = self._key(plan.team_session_id, owner_account_id)
        workflow_id = self._plan_workflows.get(key)
        task_id = self._plan_node_tasks.get((owner_account_id, plan.team_session_id, node.node_id))
        if store is None or not workflow_id or not task_id:
            return
        workflow = store.get_workflow(workflow_id)
        current = dict(((workflow.context or {}).get("workflow_plan") or {}) if workflow is not None else {})
        nodes: list[dict[str, Any]] = []
        found = False
        for raw_node in current.get("nodes") or []:
            if not isinstance(raw_node, dict):
                continue
            current_node = dict(raw_node)
            if str(current_node.get("id") or "") == node.node_id:
                current_node["assignee_id"] = node.assignee
                current_node["runtime_staffing"] = request.to_dict()
                found = True
            nodes.append(current_node)
        if not found:
            nodes.append({
                "id": node.node_id,
                "title": node.title,
                "assignee_id": node.assignee,
                "required_capabilities": list((node.metadata or {}).get("required_capabilities") or []),
                "runtime_staffing": request.to_dict(),
            })
        revised_plan = {
            **current,
            "version": int(current.get("version") or 1),
            "revision": int(current.get("revision") or 1) + 1,
            "nodes": nodes,
            "runtime_members": [spec.to_dict() for spec in team.runtime_members.values()],
        }
        delta = {
            "reassigned_node": {
                "node_id": node.node_id,
                "previous_assignee": request.previous_assignee,
                "assignee": node.assignee,
                "staffing_request_id": request.request_id,
            },
            "updated_node_metadata": {node.node_id: dict(node.metadata or {})},
            "runtime_staffing": request.to_dict(),
        }
        if hasattr(store, "apply_task_reassignment_revision"):
            store.apply_task_reassignment_revision(
                workflow_id,
                task_id,
                revised_plan,
                assignee=node.assignee,
                reason="runtime_staffing",
                delta=delta,
                actor="team_runtime",
            )
            return
        store.save_workflow_plan_revision(
            workflow_id,
            revised_plan,
            reason="runtime_staffing",
            delta=delta,
            actor="team_runtime",
        )
        store.update_task_status(
            task_id,
            "pending",
            result_summary="",
            artifacts=[],
            reset_retry=True,
            assignee=node.assignee,
        )

    def _reopen_staffing_review_nodes(
        self,
        plan: TeamPlan,
        target: TeamPlanNode,
        *,
        owner_account_id: str,
    ) -> None:
        review_ids = {
            edge.child_id
            for edge in plan.edges
            if edge.parent_id == target.node_id and edge.child_id.startswith("leader_review")
        }
        for review_id in review_ids:
            review = plan.nodes.get(review_id)
            if review is None or review.status not in {"blocked", "needs_info"}:
                continue
            metadata = dict(review.metadata or {})
            metadata["runtime_staffing_reopened_by"] = target.node_id
            review.metadata = metadata
            self._mark_plan_node(
                plan.team_session_id,
                review.node_id,
                owner_account_id=owner_account_id,
                status="pending",
                result_summary="",
                last_error="",
                allow_reopen=True,
            )

    def _apply_runtime_staffing(
        self,
        plan: TeamPlan,
        node: TeamPlanNode,
        team: Team,
        request: RuntimeStaffingRequest,
        candidate: dict[str, Any],
        *,
        owner_account_id: str,
    ) -> Team:
        request.status = "applying"
        spec = self._runtime_staffing_member_spec(
            team,
            plan,
            request,
            candidate,
            owner_account_id=owner_account_id,
        )
        runtime_specs = {
            **dict(team.runtime_members),
            spec.member_id: spec,
        }
        rebuilt = self._build_team(
            plan.team_session_id,
            external_team_id=team.external_team_id,
            owner_account_id=owner_account_id,
            runtime_members=list(runtime_specs.values()),
            existing_session=team.session,
            existing_bus=team.bus,
        )

        previous = {
            "assignee": node.assignee,
            "status": node.status,
            "result_summary": node.result_summary,
            "artifact_refs": list(node.artifact_refs),
            "delegate_task_id": node.delegate_task_id,
            "attempt_count": node.attempt_count,
            "last_error": node.last_error,
            "metadata": dict(node.metadata or {}),
        }
        metadata = dict(node.metadata or {})
        history = list(metadata.get("runtime_assignment_history") or [])
        history.append({
            "staffing_request_id": request.request_id,
            "previous_assignee": node.assignee,
            "previous_delegate_task_id": node.delegate_task_id,
            "previous_attempt_count": node.attempt_count,
            "replacement_assignee": spec.member_id,
            "replacement_external_agent_id": spec.external_agent_id,
            "reason": request.reason,
            "changed_at": time.time(),
        })
        request.status = "applied"
        request.selected_candidate = {
            **dict(candidate),
            "external_agent_id": spec.external_agent_id,
            "member_id": spec.member_id,
        }
        request.resolved_at = time.time()
        metadata["runtime_assignment_history"] = history[-6:]
        metadata["runtime_staffing"] = request.to_dict()
        metadata.pop("runtime_staffing_trigger", None)
        metadata.pop("runtime_staffing_trigger_reason", None)
        node.assignee = spec.member_id
        node.metadata = metadata
        node.update(
            status="pending",
            result_summary="",
            artifact_refs=[],
            delegate_task_id="",
            attempt_count=0,
            last_error="",
            allow_reopen=True,
        )
        try:
            self._persist_runtime_staffing_revision(
                plan,
                node,
                rebuilt,
                request,
                owner_account_id=owner_account_id,
            )
        except Exception:
            node.assignee = str(previous["assignee"])
            node.status = str(previous["status"])  # type: ignore[assignment]
            node.result_summary = str(previous["result_summary"])
            node.artifact_refs = list(previous["artifact_refs"])
            node.delegate_task_id = str(previous["delegate_task_id"])
            node.attempt_count = int(previous["attempt_count"])
            node.last_error = str(previous["last_error"])
            node.metadata = dict(previous["metadata"])
            raise
        self._teams[self._key(plan.team_session_id, owner_account_id)] = rebuilt
        self._sync_kanban_node(plan, node, owner_account_id=owner_account_id)
        self._reopen_staffing_review_nodes(plan, node, owner_account_id=owner_account_id)
        return rebuilt

    async def _handle_runtime_staffing(
        self,
        envelope: Envelope,
        plan: TeamPlan,
        node: TeamPlanNode,
        team: Team,
        trigger: dict[str, Any],
    ) -> tuple[Team, str]:
        key = self._key(plan.team_session_id, envelope.user_id)
        lock = self._staffing_locks.setdefault(key, asyncio.Lock())
        async with lock:
            required = normalize_capabilities(trigger.get("required_capabilities") or [])
            request_id = self._runtime_staffing_request_id(
                plan,
                node,
                trigger_type=str(trigger.get("trigger_type") or "capability_gap"),
                required_capabilities=required,
            )
            existing = self._runtime_staffing_request(node)
            if (
                existing is not None
                and existing.request_id == request_id
                and existing.status in {"applied", "declined", "failed", "awaiting_confirmation"}
            ):
                return team, existing.status
            if (
                existing is not None
                and existing.request_id == request_id
                and existing.status in {"approved", "applying"}
                and existing.selected_candidate
            ):
                try:
                    rebuilt = self._apply_runtime_staffing(
                        plan,
                        node,
                        team,
                        existing,
                        existing.selected_candidate,
                        owner_account_id=envelope.user_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    existing.status = "failed"
                    existing.last_error = str(exc)
                    existing.resolved_at = time.time()
                    metadata = dict(node.metadata or {})
                    metadata["runtime_staffing"] = existing.to_dict()
                    node.metadata = metadata
                    self._mark_plan_node(
                        plan.team_session_id,
                        node.node_id,
                        owner_account_id=envelope.user_id,
                        status="blocked",
                        result_summary=f"Runtime 补员恢复应用失败：{exc}",
                        last_error=str(exc),
                        allow_reopen=True,
                    )
                    return team, "failed"
                return rebuilt, "applied"

            candidates = self._runtime_staffing_candidates(
                team,
                owner_account_id=envelope.user_id,
                required_capabilities=required,
            )
            request = RuntimeStaffingRequest(
                request_id=request_id,
                trigger_node_id=node.node_id,
                trigger_type=str(trigger.get("trigger_type") or "capability_gap"),
                required_capabilities=required,
                reason=str(trigger.get("reason") or "Runtime Team 存在硬能力缺口。"),
                status="awaiting_confirmation" if candidates else "failed",
                candidates=candidates,
                previous_assignee=node.assignee,
                previous_delegate_task_id=node.delegate_task_id,
                previous_attempt_count=node.attempt_count,
                last_error="" if candidates else "没有可用的 External Agent 或 ready Runtime/model",
            )
            metadata = dict(node.metadata or {})
            previous_request = self._runtime_staffing_request(node)
            if previous_request is not None and previous_request.request_id != request.request_id:
                history = list(metadata.get("runtime_staffing_history") or [])
                history.append(previous_request.to_dict())
                metadata["runtime_staffing_history"] = history[-5:]
            metadata["runtime_staffing"] = request.to_dict()
            node.metadata = metadata
            if not candidates:
                request.resolved_at = time.time()
                metadata["runtime_staffing"] = request.to_dict()
                node.metadata = metadata
                self._mark_plan_node(
                    plan.team_session_id,
                    node.node_id,
                    owner_account_id=envelope.user_id,
                    status="blocked",
                    result_summary="Runtime 补员失败：没有可用候选。",
                    last_error=request.last_error,
                    allow_reopen=True,
                )
                return team, "failed"

            self._mark_plan_node(
                plan.team_session_id,
                node.node_id,
                owner_account_id=envelope.user_id,
                status="needs_info",
                result_summary="已检测到 Runtime 补员需求，等待用户明确选择。",
                last_error="",
                allow_reopen=True,
            )
            role_key = role_key_for_capabilities(required)
            role_label = str(role_preset(role_key).get("label") or "协作执行")
            options = [
                self._runtime_staffing_candidate_option(
                    candidate,
                    index=index,
                    role_label=role_label,
                )
                for index, candidate in enumerate(candidates)
            ]
            options.append({
                "label": "这次先不添加",
                "value": "decline",
                "description": "任务会停在这里，之后仍可以继续。",
            })
            task_title = str(node.title or "当前任务").strip()
            if len(task_title) > 42:
                task_title = f"{task_title[:41]}…"
            trigger_type = str(trigger.get("trigger_type") or "capability_gap")
            try:
                followup_session_id, question_id = await send_followup_question_to(
                    _visible_session_id(envelope.session_id),
                    [{
                        "id": f"runtime_staffing:{request.request_id}",
                        "question": (
                            f"{self._runtime_staffing_user_reason(trigger_type)}\n"
                            f"为了继续完成「{task_title}」，我找到了以下可用选择。"
                        ),
                        "options": options,
                        "allowFreeText": False,
                    }],
                    title="给这项任务找一位帮手？",
                    note="仅用于本次任务，不会加入或修改原团队。",
                    origin={
                        "type": "team_control",
                        "agent_id": "leader",
                        "agent_name": "Leader",
                        "team_session_id": envelope.session_id,
                        "node_id": node.node_id,
                        "mention_intent": "runtime_staffing",
                        "staffing_request_id": request.request_id,
                    },
                    record_history=False,
                )
                answers = await wait_for_answer(followup_session_id, question_id)
            except Exception as exc:  # noqa: BLE001
                request.last_error = str(exc)
                metadata["runtime_staffing"] = request.to_dict()
                node.metadata = metadata
                self._mark_plan_node(
                    plan.team_session_id,
                    node.node_id,
                    owner_account_id=envelope.user_id,
                    status="needs_info",
                    result_summary="Runtime 补员确认未完成，未自动补员。",
                    last_error=str(exc),
                    allow_reopen=True,
                )
                return team, "awaiting_confirmation"

            async def update_followup_status(status: str, note: str) -> None:
                try:
                    await send_followup_status_to(
                        followup_session_id,
                        question_id,
                        status,
                        note=note,
                    )
                except Exception as exc:  # noqa: BLE001 - 展示回执不得影响补员事务
                    log.debug("Runtime 补员展示状态推送失败 status=%s error=%s", status, exc)

            answer = self._runtime_staffing_answer(answers)
            if not answer:
                self._mark_plan_node(
                    plan.team_session_id,
                    node.node_id,
                    owner_account_id=envelope.user_id,
                    status="needs_info",
                    result_summary="Runtime 补员确认已取消或超时，未自动补员。",
                    allow_reopen=True,
                )
                return team, "awaiting_confirmation"
            if answer == "decline":
                request.status = "declined"
                request.resolved_at = time.time()
                metadata["runtime_staffing"] = request.to_dict()
                node.metadata = metadata
                self._mark_plan_node(
                    plan.team_session_id,
                    node.node_id,
                    owner_account_id=envelope.user_id,
                    status="blocked",
                    result_summary="用户选择暂不补员，当前节点保持阻塞。",
                    allow_reopen=True,
                )
                await update_followup_status(
                    "declined",
                    "好，这次先不添加。任务会停在这里，之后仍可以继续。",
                )
                return team, "declined"
            try:
                candidate_index = int(answer.split(":", 1)[1]) if answer.startswith("candidate:") else -1
                candidate = candidates[candidate_index] if 0 <= candidate_index < len(candidates) else None
            except (TypeError, ValueError, IndexError):
                candidate = None
            if candidate is None:
                self._mark_plan_node(
                    plan.team_session_id,
                    node.node_id,
                    owner_account_id=envelope.user_id,
                    status="needs_info",
                    result_summary="未收到有效的 Runtime 补员选择，未自动补员。",
                    allow_reopen=True,
                )
                return team, "awaiting_confirmation"

            request.status = "approved"
            request.selected_candidate = dict(candidate)
            metadata["runtime_staffing"] = request.to_dict()
            node.metadata = metadata
            self._mark_plan_node(
                plan.team_session_id,
                node.node_id,
                owner_account_id=envelope.user_id,
                status="needs_info",
                result_summary="用户已批准 Runtime 补员，正在挂载并改派。",
                allow_reopen=True,
            )
            await update_followup_status(
                "applying",
                "正在邀请协作助手加入……",
            )
            try:
                rebuilt = self._apply_runtime_staffing(
                    plan,
                    node,
                    team,
                    request,
                    candidate,
                    owner_account_id=envelope.user_id,
                )
            except Exception as exc:  # noqa: BLE001
                request.status = "failed"
                request.last_error = str(exc)
                request.resolved_at = time.time()
                metadata = dict(node.metadata or {})
                metadata["runtime_staffing"] = request.to_dict()
                node.metadata = metadata
                self._mark_plan_node(
                    plan.team_session_id,
                    node.node_id,
                    owner_account_id=envelope.user_id,
                    status="blocked",
                    result_summary=f"Runtime 补员应用失败：{exc}",
                    last_error=str(exc),
                    allow_reopen=True,
                )
                await update_followup_status(
                    "failed",
                    "这位助手暂时没能加入，请稍后再试。",
                )
                return team, "failed"
            await update_followup_status(
                "applied",
                "协作助手已加入，继续开工。\n仅参与本次任务，原团队没有变化。",
            )
            return rebuilt, "applied"

    def _reflect_plan_node(
        self,
        plan: TeamPlan,
        node: TeamPlanNode,
        *,
        owner_account_id: str = "",
        reason: str,
        decision: str,
        suggested_action: str = "",
        retryable: bool = False,
    ) -> None:
        metadata = dict(node.metadata or {})
        reflections = list(metadata.get("runtime_reflections") or [])
        reflection = {
            "reason": reason,
            "decision": decision,
            "suggested_action": suggested_action,
            "retryable": retryable,
        }
        reflections.append(reflection)
        metadata["runtime_reflections"] = reflections[-6:]
        node.metadata = metadata
        if retryable:
            reflection_block = (
                "\n\n[Runtime reflection]\n"
                f"- 上次失败原因：{reason}\n"
                f"- 本次调整：{decision}\n"
                "- 请基于上游摘要和当前节点目标重试，优先产出可检查结果；如仍缺成员或权限，明确说明需要用户确认。"
            )
            if reflection_block not in node.detail:
                node.detail = f"{node.detail}{reflection_block}"
        self._append_plan_node_event(
            plan,
            node,
            owner_account_id=owner_account_id,
            event={
                "kind": "status",
                "event_type": "reflection",
                "event_icon": "spark",
                "event_title": "运行时反思",
                "event_text": f"{decision}{(' 建议：' + suggested_action) if suggested_action else ''}",
            },
        )

    def _sync_added_plan_node(
        self,
        plan: TeamPlan,
        node: TeamPlanNode,
        *,
        owner_account_id: str = "",
        parent_node_ids: list[str] | None = None,
        reason: str = "runtime_reflection",
    ) -> None:
        store = self._kanban_store_for_owner(owner_account_id)
        if store is None:
            return
        key = self._key(plan.team_session_id, owner_account_id)
        workflow_id = self._plan_workflows.get(key)
        if not workflow_id:
            return
        node_task_ids = {
            node_id: task_id
            for (owner_id, session_id, node_id), task_id in self._plan_node_tasks.items()
            if owner_id == owner_account_id and session_id == plan.team_session_id
        }
        parent_task_ids = [
            self._plan_node_tasks[(owner_account_id, plan.team_session_id, parent_id)]
            for parent_id in parent_node_ids or []
            if (owner_account_id, plan.team_session_id, parent_id) in self._plan_node_tasks
        ]
        try:
            if hasattr(store, "apply_workflow_graph_revision") and hasattr(store, "get_workflow"):
                workflow = store.get_workflow(workflow_id)
                current = dict(((workflow.context or {}).get("workflow_plan") or {}) if workflow is not None else {})
                current_nodes = {
                    str(item.get("id") or ""): dict(item)
                    for item in (current.get("nodes") or [])
                    if isinstance(item, dict) and str(item.get("id") or "")
                }
                for current_node in plan.nodes.values():
                    metadata = dict(current_node.metadata or {})
                    contract = dict(metadata.get("execution_contract") or {})
                    current_nodes[current_node.node_id] = {
                        **current_nodes.get(current_node.node_id, {}),
                        "id": current_node.node_id,
                        "title": current_node.title,
                        "kind": str(metadata.get("work_unit_kind") or metadata.get("workflow_lane") or "other"),
                        "assignee_id": current_node.assignee,
                        "required_capabilities": list(metadata.get("required_capabilities") or []),
                        "inputs": list(contract.get("inputs") or ["task.goal"]),
                        "expected_outputs": list(contract.get("outputs") or []),
                        "acceptance_criteria": list(contract.get("acceptance_criteria") or []),
                    }
                revised_plan = {
                    **current,
                    "version": int(current.get("version") or 1),
                    "revision": int(current.get("revision") or 1) + 1,
                    "nodes": [current_nodes[item.node_id] for item in plan.nodes.values()],
                    "edges": [{"from": edge.parent_id, "to": edge.child_id} for edge in plan.edges],
                }
                _, task = store.apply_workflow_graph_revision(
                    workflow_id,
                    revised_plan,
                    added_node={
                        "id": node.node_id,
                        "title": node.title,
                        "detail": node.detail,
                        "assignee": node.assignee,
                        "status": self._kanban_status(node.status),
                        "max_retries": int((revised_plan.get("budget_snapshot") or {}).get("max_retries") or 2),
                    },
                    node_task_ids=node_task_ids,
                    edges=[(edge.parent_id, edge.child_id) for edge in plan.edges],
                    reason=reason,
                    delta={"added_nodes": [node.node_id], "reason": reason},
                )
                self._plan_node_tasks[(owner_account_id, plan.team_session_id, node.node_id)] = task.id
                return
            task = store.add_task(
                workflow_id,
                title=node.title,
                detail=node.detail,
                assignee=node.assignee,
                parent_task_ids=parent_task_ids or None,
                status=self._kanban_status(node.status),
                auto_promote=False,
            )
            self._plan_node_tasks[(owner_account_id, plan.team_session_id, node.node_id)] = task.id
            store.add_event(
                workflow_id,
                "team_plan_replanned",
                task_id=task.id,
                actor="team_runtime",
                payload={
                    "team_plan_id": plan.plan_id,
                    "node": node.to_dict(),
                    "edges": [edge.to_dict() for edge in plan.edges],
                    "reason": reason,
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("TeamPlan replan 节点同步到 kanban store 失败 session=%s node=%s err=%s", plan.team_session_id, node.node_id, exc)

    def _insert_runtime_diagnostic_node(
        self,
        plan: TeamPlan,
        node: TeamPlanNode,
        *,
        owner_account_id: str = "",
        reason: str,
    ) -> TeamPlanNode | None:
        if node.assignee == "leader" or node.node_id.startswith("runtime_diagnosis_"):
            return None
        existing = str((node.metadata or {}).get("runtime_diagnostic_node_id") or "")
        if existing and existing in plan.nodes:
            return plan.nodes[existing]
        diagnostic_id = f"runtime_diagnosis_{node.node_id}"
        if diagnostic_id in plan.nodes:
            return plan.nodes[diagnostic_id]

        parent_ids = self._node_dependencies(plan, node.node_id)
        plan.edges = [
            edge
            for edge in plan.edges
            if not (edge.child_id == node.node_id and edge.parent_id in parent_ids)
        ]
        for parent_id in parent_ids:
            plan.edges.append(TeamPlanEdge(parent_id=parent_id, child_id=diagnostic_id))
        plan.edges.append(TeamPlanEdge(parent_id=diagnostic_id, child_id=node.node_id))

        metadata = team_presenter.node_display_progress(
            node_id=diagnostic_id,
            title=f"运行诊断：{node.title}",
            assignee="leader",
            metadata={
                "workflow_lane": "lead",
                "role_label": "运行诊断、局部重排",
                "role_key": "team_lead",
                "plan_strategy": str((node.metadata or {}).get("plan_strategy") or "runtime_replan"),
                "replan_kind": "diagnostic_before_retry",
            },
        )
        diagnostic = TeamPlanNode(
            node_id=diagnostic_id,
            title=f"运行诊断：{node.title}",
            detail=(
                f"根据节点「{node.title}」的失败原因做局部诊断，整理重试重点、风险和是否需要用户确认。\n"
                f"失败原因：{reason}"
            ),
            assignee="leader",
            metadata={
                **metadata,
                "execution_events": [
                    {
                        "id": f"{diagnostic_id}:created",
                        "kind": "status",
                        "event_type": "replan",
                        "event_icon": "route",
                        "event_title": "局部重排",
                        "event_text": f"Runtime 已插入诊断节点，先由 Leader 诊断后再重试「{node.title}」。",
                        "collapsed": False,
                    }
                ],
            },
        )
        ordered: dict[str, TeamPlanNode] = {}
        for current_id, current_node in plan.nodes.items():
            if current_id == node.node_id:
                ordered[diagnostic_id] = diagnostic
            ordered[current_id] = current_node
        plan.nodes = ordered
        node.metadata = {
            **dict(node.metadata or {}),
            "runtime_diagnostic_node_id": diagnostic_id,
        }
        plan.updated_at = diagnostic.updated_at
        self._sync_added_plan_node(plan, diagnostic, owner_account_id=owner_account_id, parent_node_ids=parent_ids)
        self._append_plan_node_event(
            plan,
            node,
            owner_account_id=owner_account_id,
            event={
                "kind": "status",
                "event_type": "replan",
                "event_icon": "route",
                "event_title": "局部重排",
                "event_text": f"已插入 Leader 诊断节点「{diagnostic.title}」，诊断完成后再重试当前节点。",
            },
        )
        return diagnostic

    @staticmethod
    def _result_needs_leader_review(result_summary: str, result_contract: dict[str, str] | None = None) -> bool:
        text = " ".join([
            str(result_summary or ""),
            *[str(value or "") for value in (result_contract or {}).values()],
        ])
        return any(word in text for word in ("需要补充", "信息不足", "缺少", "待确认", "无法确认", "需要确认", "请确认"))

    @staticmethod
    def _result_requires_user_input(result_summary: str, result_contract: dict[str, str] | None = None) -> bool:
        text = " ".join([
            str(result_summary or ""),
            *[str(value or "") for value in (result_contract or {}).values()],
        ])
        return any(marker in text for marker in (
            "需要用户补充",
            "需要用户确认",
            "请用户确认",
            "必须由用户",
            "等待用户确认",
            "用户权限",
        ))

    @staticmethod
    def _node_contract_requires_leader_review(node: TeamPlanNode) -> bool:
        contract = (node.metadata or {}).get("execution_contract")
        if isinstance(contract, dict) and contract.get("requires_leader_review") is True:
            return True
        return False

    @staticmethod
    def _reviewed_member_nodes(plan: TeamPlan, review_node: TeamPlanNode) -> list[TeamPlanNode]:
        return [
            plan.nodes[parent_id]
            for parent_id in InProcessTeamManager._node_dependencies(plan, review_node.node_id)
            if parent_id in plan.nodes and plan.nodes[parent_id].assignee != "leader"
        ]

    @staticmethod
    def _parse_leader_review_decision(text: str) -> dict[str, str]:
        body = str(text or "").strip()
        data: dict[str, Any] = {}
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", body, flags=re.DOTALL)
        candidate = fenced.group(1) if fenced else body
        if "{" in candidate and "}" in candidate:
            try:
                parsed = json.loads(candidate[candidate.find("{"):candidate.rfind("}") + 1])
                if isinstance(parsed, dict):
                    data = parsed
            except (TypeError, ValueError, json.JSONDecodeError):
                data = {}

        allowed_actions = {"approve", "revise", "ask_user", "block"}
        action = str(data.get("action") or "").strip().lower()
        if action not in allowed_actions:
            lowered = body.lower()
            if any(word in lowered for word in ("需要修改", "继续优化", "重新处理", "revise", "changes requested")):
                action = "revise"
            elif any(word in lowered for word in ("需要用户", "请用户", "补充信息", "ask_user")):
                action = "ask_user"
            elif any(word in lowered for word in ("阻塞", "无法继续", "block")):
                action = "block"
            else:
                action = "approve"

        message = str(data.get("message") or data.get("review") or "").strip()
        instructions = str(data.get("instructions") or data.get("revision_instructions") or "").strip()
        if not message:
            message = body
        if action == "revise" and not instructions:
            instructions = message
        return {
            "action": action,
            "target_node_id": str(data.get("target_node_id") or "").strip(),
            "message": message,
            "instructions": instructions,
        }

    @staticmethod
    def _json_object_from_text(text: str) -> dict[str, Any]:
        body = str(text or "").strip()
        if not body:
            return {}
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", body, flags=re.DOTALL)
        candidate = fenced.group(1) if fenced else body
        if "{" not in candidate or "}" not in candidate:
            return {}
        try:
            parsed = json.loads(candidate[candidate.find("{"):candidate.rfind("}") + 1])
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _clean_display_patch_value(value: Any, *, limit: int = 24) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip(" \t\r\n-—:：，,。；;")
        if not text:
            return ""
        if len(text) > limit:
            text = text[:limit].rstrip(" -—:：，,。；;") or text[:limit]
        return text

    async def _extract_final_display_metadata(
        self,
        plan: TeamPlan,
        *,
        final_summary: str,
        owner_account_id: str = "",
    ) -> dict[str, dict[str, str]]:
        candidates: list[dict[str, str]] = []
        for node in plan.nodes.values():
            if node.assignee == "leader" or node.status != "completed":
                continue
            metadata = dict(node.metadata or {})
            display_title = str(metadata.get("display_title") or "").strip()
            if not display_title:
                continue
            result = self._node_result_digest(
                self._dedupe_repeated_colon_prefix(node.result_summary or node.last_error or ""),
                limit=520,
            )
            if not result:
                continue
            candidates.append({
                "node_id": node.node_id,
                "current_display_title": display_title,
                "full_title": str(metadata.get("full_title") or node.title),
                "assignee": node.assignee,
                "result_summary": result,
            })
        if not candidates:
            return {}
        payload = {
            "goal": plan.goal,
            "leader_summary": self._node_result_digest(final_summary, limit=1000),
            "nodes": candidates[:20],
        }
        prompt = "\n".join([
            "你是团队看板的最终对象映射提取器。",
            "请只根据已完成节点结果和最终汇总，判断每个节点最终对应的具体对象、主题或交付物。",
            "只在结果中明确出现对象时更新；不要猜测，不要补业务规则。",
            "display_subject 写最短对象名，display_title 写适合看板卡片的短标题。",
            "如果 current_display_title 已经足够具体，或无法明确对象，就不要返回该节点。",
            "必须只输出 JSON：{\"updates\":[{\"node_id\":\"...\",\"display_subject\":\"...\",\"display_title\":\"...\"}]}",
            "",
            json.dumps(payload, ensure_ascii=False),
        ])
        try:
            response = await self._provider_for_owner(owner_account_id).chat([
                Message(role="system", content="你只输出严格 JSON，不输出解释。"),
                Message(role="user", content=prompt),
            ])
        except Exception as exc:  # noqa: BLE001
            log.debug("final display metadata extraction skipped session=%s err=%s", plan.team_session_id, exc)
            return {}
        data = self._json_object_from_text(str(getattr(response, "text", "") or ""))
        updates = data.get("updates") if isinstance(data.get("updates"), list) else []
        valid_ids = {item["node_id"] for item in candidates}
        patches: dict[str, dict[str, str]] = {}
        for item in updates:
            if not isinstance(item, dict):
                continue
            node_id = str(item.get("node_id") or "").strip()
            if node_id not in valid_ids:
                continue
            subject = self._clean_display_patch_value(item.get("display_subject"), limit=18)
            title = self._clean_display_patch_value(item.get("display_title"), limit=24)
            if not subject and not title:
                continue
            patch: dict[str, str] = {}
            if subject:
                patch["display_subject"] = subject
            if title:
                patch["display_title"] = title
            patches[node_id] = patch
        return patches

    def _save_display_metadata_revision(
        self,
        plan: TeamPlan,
        *,
        owner_account_id: str,
        patches: dict[str, dict[str, str]],
    ) -> None:
        store = self._kanban_store_for_owner(owner_account_id)
        if store is None or not patches:
            return
        key = self._key(plan.team_session_id, owner_account_id)
        workflow_id = self._plan_workflows.get(key)
        if not workflow_id or not hasattr(store, "save_workflow_plan_revision") or not hasattr(store, "get_workflow"):
            return
        try:
            workflow = store.get_workflow(workflow_id)
            current = dict(((workflow.context or {}).get("workflow_plan") or {}) if workflow is not None else {})
            current_nodes: list[dict[str, Any]] = []
            seen: set[str] = set()
            for raw_node in current.get("nodes") or []:
                if not isinstance(raw_node, dict):
                    continue
                node_id = str(raw_node.get("id") or "").strip()
                if not node_id:
                    continue
                current_nodes.append({**dict(raw_node), **dict(patches.get(node_id) or {})})
                seen.add(node_id)
            for node_id, patch in patches.items():
                if node_id in seen or node_id not in plan.nodes:
                    continue
                node = plan.nodes[node_id]
                metadata = dict(node.metadata or {})
                current_nodes.append({
                    "id": node_id,
                    "title": node.title,
                    "display_title": str(metadata.get("display_title") or ""),
                    "kind": str(metadata.get("work_unit_kind") or metadata.get("workflow_lane") or "other"),
                    "assignee_id": node.assignee,
                    **patch,
                })
            revised_plan = {
                **current,
                "version": int(current.get("version") or 1),
                "revision": int(current.get("revision") or 1) + 1,
                "nodes": current_nodes,
            }
            store.save_workflow_plan_revision(
                workflow_id,
                revised_plan,
                reason="final_display_metadata",
                delta={"updated_node_metadata": patches},
                actor="team_runtime",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "TeamPlan final display metadata revision failed session=%s err=%s",
                plan.team_session_id,
                exc,
            )

    async def _refresh_final_display_metadata(
        self,
        plan: TeamPlan,
        *,
        owner_account_id: str,
        final_summary: str,
    ) -> None:
        workflow_id = self._plan_workflows.get(self._key(plan.team_session_id, owner_account_id))
        if self.kanban_store is None or not workflow_id:
            return
        patches = await self._extract_final_display_metadata(
            plan,
            final_summary=final_summary,
            owner_account_id=owner_account_id,
        )
        if not patches:
            return
        applied: dict[str, dict[str, str]] = {}
        for node_id, patch in patches.items():
            node = plan.nodes.get(node_id)
            if node is None:
                continue
            metadata = dict(node.metadata or {})
            changed: dict[str, str] = {}
            for key, value in patch.items():
                if value and str(metadata.get(key) or "") != value:
                    metadata[key] = value
                    changed[key] = value
            if changed:
                node.metadata = metadata
                applied[node_id] = changed
        self._save_display_metadata_revision(plan, owner_account_id=owner_account_id, patches=applied)

    @staticmethod
    def _leader_review_decision_conflicts(
        plan: TeamPlan,
        review_node: TeamPlanNode,
        decision: dict[str, str],
    ) -> bool:
        if str(decision.get("action") or "") not in {"ask_user", "block"}:
            return False
        reviewed = InProcessTeamManager._reviewed_member_nodes(plan, review_node)
        if not reviewed or not all(item.result_summary or item.artifact_refs for item in reviewed):
            return False
        text = " ".join([
            str(decision.get("message") or ""),
            str(decision.get("instructions") or ""),
        ])
        missing_claim = any(marker in text for marker in (
            "仅包含成员角色",
            "缺少实现方案",
            "缺少测试方案",
            "缺少成员提交",
            "没有收到方案",
            "未提供方案",
            "无法获取方案",
        ))
        return missing_claim

    @staticmethod
    def _review_followup_answered(answers: list[dict[str, Any]]) -> bool:
        if not answers:
            return False
        return not any(
            str(answer.get("id") or "") == CANCELLED_MARKER
            for answer in answers
            if isinstance(answer, dict)
        )

    @staticmethod
    def _infer_review_revision_target(
        reviewed: list[TeamPlanNode],
        decision: dict[str, str],
    ) -> TeamPlanNode | None:
        text = " ".join([
            str(decision.get("message") or ""),
            str(decision.get("instructions") or ""),
        ]).lower()
        if not text:
            return None
        lane_keywords = {
            "design": ("实现方案", "开发方案", "技术栈", "文件结构", "算法", "伪代码", "动画", "架构"),
            "build": ("实现", "开发", "编码", "代码", "集成"),
            "plan": ("测试方案", "测试用例", "测试范围", "通过标准", "失败场景", "缺陷"),
            "verify": ("测试执行", "验证结果", "回归", "验收结论", "缺陷"),
        }
        scored: list[tuple[int, TeamPlanNode]] = []
        for item in reviewed:
            score = 0
            if item.node_id.lower() in text:
                score += 20
            if f"@{item.assignee.lower()}" in text:
                score += 16
            title_head = item.title.split("：", 1)[0].strip().lower()
            if title_head and title_head in text:
                score += 8
            metadata = dict(item.metadata or {})
            role_label = str(metadata.get("role_label") or "").strip().lower()
            if role_label and role_label in text:
                score += 4
            lane = str(metadata.get("workflow_lane") or "").strip().lower()
            score += sum(2 for keyword in lane_keywords.get(lane, ()) if keyword in text)
            if score > 0:
                scored.append((score, item))
        if not scored:
            return None
        scored.sort(key=lambda pair: pair[0], reverse=True)
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            return None
        return scored[0][1]

    def _apply_leader_review_decision(
        self,
        plan: TeamPlan,
        review_node: TeamPlanNode,
        decision: dict[str, str],
        *,
        owner_account_id: str = "",
        max_revisions: int = 2,
    ) -> dict[str, str]:
        action = str(decision.get("action") or "approve")
        reviewed = self._reviewed_member_nodes(plan, review_node)
        target_id = str(decision.get("target_node_id") or "")
        target = next((item for item in reviewed if item.node_id == target_id), None)
        if target is None and len(reviewed) == 1:
            target = reviewed[0]
            target_id = target.node_id
            decision = {**decision, "target_node_id": target_id}
        elif target is None and action == "revise":
            target = self._infer_review_revision_target(reviewed, decision)
            if target is not None:
                target_id = target.node_id
                decision = {**decision, "target_node_id": target_id}

        review_meta = dict(review_node.metadata or {})
        revision_count = int(review_meta.get("revision_count") or 0)
        revision_exhausted = bool(
            action == "revise"
            and target is not None
            and revision_count >= max_revisions
        )
        if action == "revise" and (target is None or revision_count >= max_revisions):
            action = "block"
            reason = (
                "Leader 未指定可修订的成员节点。"
                if target is None
                else f"已达到最多 {max_revisions} 次自动修订。"
            )
            decision = {
                **decision,
                "action": action,
                "message": f"{decision.get('message') or '审阅未通过'} {reason}".strip(),
            }
            if revision_exhausted and target is not None and self.external_store is not None:
                target_meta = dict(target.metadata or {})
                target_meta["runtime_staffing_trigger"] = "review_exhausted"
                target_meta["runtime_staffing_trigger_reason"] = reason
                target.metadata = target_meta
                review_meta["runtime_staffing_target_node_id"] = target.node_id
                self._mark_plan_node(
                    plan.team_session_id,
                    target.node_id,
                    owner_account_id=owner_account_id,
                    status="failed",
                    last_error=reason,
                    allow_reopen=True,
                )

        if action == "revise" and target is not None:
            self._record_external_agent_profile_observation(
                plan,
                target,
                owner_account_id=owner_account_id,
                outcome="revise",
                quality_weight=0.5,
                assessment_source="leader_review",
                failure_kind="leader_revise",
            )
            instructions = str(decision.get("instructions") or decision.get("message") or "请按 Leader 意见修订。")
            target_meta = dict(target.metadata or {})
            history = list(target_meta.get("revision_history") or [])
            history.append({
                "revision": revision_count + 1,
                "review_node_id": review_node.node_id,
                "instructions": instructions,
                "previous_result_summary": target.result_summary,
            })
            target_meta["revision_history"] = history[-max_revisions:]
            target_meta["revision_instructions"] = instructions
            target.metadata = target_meta
            review_meta["revision_count"] = revision_count + 1
            review_meta["last_decision"] = dict(decision)
            review_node.metadata = review_meta
            self._mark_plan_node(
                plan.team_session_id,
                target.node_id,
                owner_account_id=owner_account_id,
                status="pending",
                result_summary="",
                artifact_refs=[],
                delegate_task_id="",
                attempt_count=0,
                last_error="",
                allow_reopen=True,
            )
            self._mark_plan_node(
                plan.team_session_id,
                review_node.node_id,
                owner_account_id=owner_account_id,
                status="pending",
                result_summary="",
            )
        elif action == "approve":
            for reviewed_node in reviewed:
                self._record_external_agent_profile_observation(
                    plan,
                    reviewed_node,
                    owner_account_id=owner_account_id,
                    outcome="success",
                    quality_weight=1.0,
                    assessment_source="leader_review",
                )
            review_meta["last_decision"] = dict(decision)
            review_node.metadata = review_meta
            self._mark_plan_node(
                plan.team_session_id,
                review_node.node_id,
                owner_account_id=owner_account_id,
                status="completed",
                result_summary=str(decision.get("message") or "Leader 审阅通过。"),
            )
        else:
            review_meta["last_decision"] = dict(decision)
            review_node.metadata = review_meta
            status = "needs_info" if action == "ask_user" else "blocked"
            self._mark_plan_node(
                plan.team_session_id,
                review_node.node_id,
                owner_account_id=owner_account_id,
                status=status,
                result_summary=str(decision.get("message") or "Leader 已暂停后续流程。"),
            )
        return {**decision, "action": action}

    def _insert_leader_review_node(
        self,
        plan: TeamPlan,
        node: TeamPlanNode,
        *,
        owner_account_id: str = "",
        reason: str,
    ) -> TeamPlanNode | None:
        if node.assignee == "leader" or node.node_id.startswith("leader_review"):
            return None
        existing = str((node.metadata or {}).get("leader_review_node_id") or "")
        if existing and existing in plan.nodes:
            return plan.nodes[existing]
        child_ids = [edge.child_id for edge in plan.edges if edge.parent_id == node.node_id]
        if any(child_id.startswith("leader_review") for child_id in child_ids):
            return None
        review_id = f"leader_review_{node.node_id}"
        if review_id in plan.nodes:
            return plan.nodes[review_id]

        plan.edges = [edge for edge in plan.edges if edge.parent_id != node.node_id]
        plan.edges.append(TeamPlanEdge(parent_id=node.node_id, child_id=review_id))
        for child_id in child_ids:
            if child_id != review_id:
                plan.edges.append(TeamPlanEdge(parent_id=review_id, child_id=child_id))

        metadata = team_presenter.node_display_progress(
            node_id=review_id,
            title=f"Leader 审阅：{node.title}",
            assignee="leader",
            metadata={
                "workflow_lane": "lead",
                "role_label": "审阅成员提交、判断后续动作",
                "role_key": "team_lead",
                "plan_strategy": str((node.metadata or {}).get("plan_strategy") or "runtime_review"),
                "review_reason": reason,
            },
        )
        review = TeamPlanNode(
            node_id=review_id,
            title=f"Leader 审阅：{node.title}",
            detail=(
                f"审阅成员节点「{node.title}」的提交，判断是否通过、是否继续、是否阻塞，"
                f"以及是否需要向用户或成员补充信息。\n触发原因：{reason}"
            ),
            assignee="leader",
            metadata={
                **metadata,
                "execution_events": [
                    {
                        "id": f"{review_id}:created",
                        "kind": "status",
                        "event_type": "review",
                        "event_icon": "review",
                        "event_title": "Leader 审阅",
                        "event_text": f"Runtime 已插入 Leader 审阅节点，先确认「{node.title}」再继续后续流程。",
                        "collapsed": False,
                    }
                ],
            },
        )
        ordered: dict[str, TeamPlanNode] = {}
        for current_id, current_node in plan.nodes.items():
            ordered[current_id] = current_node
            if current_id == node.node_id:
                ordered[review_id] = review
        plan.nodes = ordered
        node.metadata = {
            **dict(node.metadata or {}),
            "leader_review_node_id": review_id,
        }
        plan.updated_at = review.updated_at
        self._sync_added_plan_node(plan, review, owner_account_id=owner_account_id, parent_node_ids=[node.node_id])
        self._append_plan_node_event(
            plan,
            node,
            owner_account_id=owner_account_id,
            event={
                "kind": "status",
                "event_type": "review",
                "event_icon": "review",
                "event_title": "等待 Leader 审阅",
                "event_text": f"已插入 Leader 审阅节点「{review.title}」。",
            },
        )
        return review

    def _matching_team_session_ids(self, session_id: str, owner_account_id: str = "") -> list[str]:
        sid = str(session_id or "").strip()
        if not sid:
            return []
        prefix = f"{sid}::turn::"
        keys = set(self._teams) | set(self._plans)
        with self._active_lock:
            keys.update(self._active_children)
            keys.update(self._delegate_tasks)
        matched = [key[1] for key in keys if key[0] == owner_account_id and (key[1] == sid or key[1].startswith(prefix))]
        return list(dict.fromkeys([sid, *sorted(matched)]))

    def _team_context_summary(self, session_id: str, owner_account_id: str = "") -> str:
        try:
            list_tasks = getattr(self.tasks, "list_tasks", None)
            if callable(list_tasks):
                tasks = list_tasks(limit=1000, owner_account_id=owner_account_id)
            else:
                listed = getattr(self.tasks, "list", None)
                tasks = listed(session_id) if callable(listed) else []
        except Exception:  # noqa: BLE001
            tasks = []
        prefix = f"{session_id}::turn::"
        relevant = [
            task for task in tasks
            if str(task.get("session_id") or "") == session_id
            or str(task.get("session_id") or "").startswith(prefix)
        ]
        if not relevant:
            return ""
        parent_turns = [
            task for task in relevant
            if str(task.get("kind") or "") == "agent_turn"
            and str(task.get("session_id") or "") == session_id
        ]
        team_tasks = [task for task in relevant if str(task.get("kind") or "") == "team"]
        lines = [
            "# Team 历史上下文",
            "你正在处理同一个 Team 父会话；用户说“继续”时，优先承接最近未完成、失败或被终止的团队任务，不要当作新会话。",
        ]
        if parent_turns:
            lines.append("## 用户可见请求")
            for task in sorted(parent_turns, key=lambda item: float(item.get("created_at") or 0))[-6:]:
                title = str(task.get("detail") or task.get("title") or "").strip()
                status = str(task.get("status") or "").strip()
                if title:
                    lines.append(f"- [{status or 'unknown'}] {title}")
        if team_tasks:
            lines.append("## 团队节点状态")
            for task in sorted(team_tasks, key=lambda item: float(item.get("created_at") or 0))[-8:]:
                title = str(task.get("title") or task.get("detail") or "").strip()
                assignee = str(task.get("assignee") or (task.get("progress") or {}).get("member") or "").strip()
                status = str(task.get("status") or "").strip()
                result = str(task.get("result") or task.get("error") or "").strip().replace("\n", " ")
                line = f"- [{status or 'unknown'}]"
                if assignee:
                    line += f" {assignee}:"
                if title:
                    line += f" {title}"
                if result:
                    line += f" -> {result[:220]}"
                lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _team_roster_summary(team: Team) -> str:
        rows: list[str] = []
        leader = team.leader_spec
        leader_label = leader.name or leader.member_id or "leader"
        leader_role = leader.role or "负责拆解、派活、跟踪任务、汇总最终结果"
        rows.append(f"- {leader_label}（leader）：{leader_role}")
        for member_id, member in team.members.items():
            label = member.name or member_id
            role = member.role or "未填写职责"
            lane = str((member.metadata or {}).get("workflow_lane") or "").strip()
            suffix = f"；workflow_lane={lane}" if lane else ""
            rows.append(f"- {label}（{member_id}）：{role}{suffix}")
        return "\n".join([
            "# 当前团队成员",
            "这是 Crew 控制面已配置的真实团队成员列表。用户询问团队成员、Leader 或职责时，必须基于此列表回答，不要说未提供成员信息。",
            *rows,
        ])

    def _cancel_plan(self, session_id: str, message: str | None = None, owner_account_id: str = "") -> bool:
        plan = self._plans.get(self._existing_plan_key(session_id, owner_account_id))
        if plan is None:
            return False
        reason = message or "团队运行已停止"
        changed = False
        for node in plan.nodes.values():
            if node.status not in {"completed", "cancelled", "blocked"}:
                node.update(status="cancelled", last_error=reason, result_summary=reason)
                changed = True
        if changed:
            plan.status = "cancelled"
            plan.updated_at = max((node.updated_at for node in plan.nodes.values()), default=plan.updated_at)
        return changed

    @staticmethod
    def _node_dependencies(plan: TeamPlan, node_id: str) -> list[str]:
        return [edge.parent_id for edge in plan.edges if edge.child_id == node_id]

    @staticmethod
    def _node_ready(plan: TeamPlan, node: TeamPlanNode) -> bool:
        dependencies = InProcessTeamManager._node_dependencies(plan, node.node_id)
        return all(
            plan.nodes.get(parent) is not None and plan.nodes[parent].status == "completed"
            for parent in dependencies
        )

    @staticmethod
    def _node_upstream_summary(plan: TeamPlan, node: TeamPlanNode) -> str:
        rows: list[str] = []
        for parent_id in InProcessTeamManager._node_dependencies(plan, node.node_id):
            parent = plan.nodes.get(parent_id)
            if parent is None:
                continue
            summary = str(parent.result_summary or parent.detail or "").strip()
            if len(summary) > 360:
                summary = f"{summary[:360].rstrip()}..."
            rows.append(f"- {parent.title}: {summary or parent.status}")
        return "\n".join(rows)

    @staticmethod
    def _first_nonempty_text(*parts: str) -> str:
        for part in parts:
            text = str(part or "").strip()
            if text:
                return text
        return ""

    @staticmethod
    def _dedupe_repeated_colon_prefix(text: str) -> str:
        value = str(text or "").strip()
        if not value:
            return ""
        for _ in range(6):
            parts = [part.strip() for part in value.split("：")]
            if len(parts) < 3:
                break
            changed = False
            compact: list[str] = []
            for part in parts:
                if compact and part == compact[-1]:
                    changed = True
                    continue
                compact.append(part)
            next_value = "：".join(compact)
            if not changed or next_value == value:
                break
            value = next_value
        return value

    @staticmethod
    def _leader_model_result_usable(text: str) -> bool:
        value = str(text or "").strip()
        if not value:
            return False
        compact = re.sub(r"\s+", "", value)
        generic = {
            "节点完成",
            "节点完成。",
            "已完成",
            "已完成。",
            "我只做总结，不主动派活",
        }
        if compact in generic:
            return False
        if len(compact) < 8 and not any(ch in compact for ch in "？?"):
            return False
        return True

    @staticmethod
    def _node_upstream_artifact_refs(plan: TeamPlan, node: TeamPlanNode) -> list[str]:
        refs: list[str] = []
        seen: set[str] = set()
        for parent_id in InProcessTeamManager._node_dependencies(plan, node.node_id):
            parent = plan.nodes.get(parent_id)
            if parent is None:
                continue
            for ref in parent.artifact_refs:
                value = str(ref or "").strip()
                if not value or value in seen:
                    continue
                seen.add(value)
                refs.append(value)
        return refs

    @staticmethod
    def _format_upstream_artifacts(refs: list[str]) -> str:
        if not refs:
            return ""
        return "\n".join(f"- {ref}" for ref in refs[:12])

    @staticmethod
    def _workspace_guard_config(workspace_scope: str, delegate_cwd: str, upstream_artifacts: list[str]) -> dict[str, Any] | None:
        if workspace_scope != "isolated_turn_workspace" or not delegate_cwd:
            return None
        allowed_roots: list[str] = [delegate_cwd]
        readable_files: list[str] = []
        seen_roots = {str(Path(delegate_cwd).expanduser().resolve())}
        seen_files: set[str] = set()
        for ref in upstream_artifacts:
            raw = str(ref or "").strip()
            if not raw:
                continue
            try:
                path = Path(raw).expanduser().resolve()
            except Exception:  # noqa: BLE001
                continue
            resolved = str(path)
            if path.is_dir():
                if resolved in seen_roots:
                    continue
                seen_roots.add(resolved)
                allowed_roots.append(resolved)
            elif resolved not in seen_files:
                seen_files.add(resolved)
                readable_files.append(resolved)
        return {
            "enabled": True,
            "root": str(Path(delegate_cwd).expanduser().resolve()),
            "readable_roots": allowed_roots,
            "readable_files": readable_files,
            "writable_roots": [str(Path(delegate_cwd).expanduser().resolve())],
            "allowed_roots": allowed_roots,
        }

    @staticmethod
    def _delegate_output_contract(workspace_scope: str) -> str:
        return (
            f"工作区范围：{workspace_scope}；"
            "若工作区是 isolated_turn_workspace，不要从旧文件或历史产物推断测试对象；"
            "如果 Leader 委派中提供了上游产物路径，必须优先读取这些路径作为测试/复核对象；"
            "验证节点需要先基于实际产物复核并必要时补充测试方案，再执行验证并给出结论；"
            "读/搜索/枚举工具会被限制在当前团队工作区和显式上游产物目录内；"
            "写入、修改和生成文件只能落在当前团队工作区内；"
            "如果确实缺少关键输入，请明确写出缺失项和建议动作；"
            "输出当前节点的执行结果、关键发现、风险/阻塞和可交付结论；"
            "最终回复必须包含面向业务目标的结果契约：结论、关键依据、风险、建议；"
            "提交结果时必须调用 team_mention(intent=\"submit\", result_status=\"pass|fail|blocked\")；"
            "result_status 是当前节点的结构化验收事实，不得从历史失败描述推断；"
            "结论要直接回答当前节点对用户目标的贡献，例如是否通过、是否可验收、是否需要修复，而不是只说节点已完成；"
            "如果输出完整 Markdown 产物，请用一级标题（# 文档标题）给出体面的文档名；"
            "若当前节点标题包含“方案”或 node_id 包含 plan/design，只提交方案、通过标准、风险和待确认问题；"
            "方案节点禁止执行验证、禁止跑测试命令、禁止产出验证结论；"
            "当前节点产物通过 @leader 提交给 Leader 审阅，不要写“提交给用户审阅”或要求用户直接审批；"
            "只有 Leader 判断需要用户复核时，后续才由 Leader 面向用户发起确认；"
            "不要展开完整团队计划，不要重复上游全文。"
        )

    @staticmethod
    def _role_hint(member: TeamMemberSpec) -> str:
        return flow_builder.role_hint(member)

    @staticmethod
    def _workflow_lane(member: TeamMemberSpec) -> str:
        return flow_builder.workflow_lane(member)

    @staticmethod
    def _role_key(member: TeamMemberSpec) -> str:
        return flow_builder.role_key(member)

    @staticmethod
    def _role_label(member: TeamMemberSpec) -> str:
        return flow_builder.role_label(member)

    @staticmethod
    def _role_slug(member: TeamMemberSpec, fallback: str) -> str:
        return flow_builder.role_slug(member, fallback)

    @staticmethod
    def _verify_role_template(member: TeamMemberSpec, task_title: str, goal: str) -> dict[str, str]:
        return flow_builder.verify_role_template(member, task_title, goal)

    @staticmethod
    def _goal_title(goal: str) -> str:
        return flow_builder.goal_title(goal)

    @staticmethod
    def _goal_needs_build(goal: str) -> bool:
        return flow_builder.goal_needs_build(goal)

    def _default_workflow_nodes(
        self,
        team: Team,
        goal: str,
        *,
        team_spec: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], list[Any]]:
        plan = self.graph_planner.plan(team, goal, team_spec=team_spec)
        return plan.nodes, plan.edges

    def _team_execution_profile(self, envelope: Envelope) -> dict[str, Any] | None:
        explicit = envelope.params.get("team_execution_profile")
        if isinstance(explicit, dict):
            return explicit
        config_profile = (self.config.team_config or {}).get("execution_profile")
        if isinstance(config_profile, dict):
            return config_profile
        return None

    @staticmethod
    def _turn_decision_for_execution_profile(
        turn_decision: TeamTurnDecision,
        *,
        explicit_mode: str,
    ) -> TeamTurnDecision:
        if turn_decision.diagnostics.get("turn_source") == "missing_info_empty_goal":
            return turn_decision
        if explicit_mode in {"fast", "standard", "ai"}:
            return new_workflow_decision(explicit_mode, turn_decision.reason, source="explicit_execution_profile")
        return turn_decision

    def _execution_profile_for_turn(
        self,
        envelope: Envelope,
        *,
        intent_profile: dict[str, Any],
        turn_decision: TeamTurnDecision,
    ) -> dict[str, Any] | None:
        base_profile = self._team_execution_profile(envelope)
        if isinstance(base_profile, dict):
            return base_profile
        if turn_decision.turn_kind != "new_workflow" or turn_decision.execution_mode != "fast":
            return None
        profile = dict(intent_profile)
        profile["requested_mode"] = "fast"
        profile["turn_kind"] = turn_decision.turn_kind
        profile["turn_decision_source"] = str(turn_decision.diagnostics.get("source") or "")
        return profile

    @staticmethod
    def _team_mode_from_followup_answers(answers: list[dict[str, Any]]) -> str:
        allowed = {"auto", "fast", "standard", "ai"}
        for item in answers or []:
            if not isinstance(item, dict):
                continue
            raw_answers = item.get("answers")
            if not isinstance(raw_answers, list):
                continue
            for value in raw_answers:
                mode = str(value or "").strip().lower()
                if mode in allowed:
                    return mode
        return "auto"

    @staticmethod
    def _blocking_planning_missing_info(goal: str, missing_info: list[str]) -> list[str]:
        """只保留确实会阻塞执行、必须由用户补充的缺失事实。"""
        goal_text = str(goal or "").strip()
        blockers: list[str] = []
        blocker_keywords = (
            "文件", "附件", "正文", "原文", "数据", "表格", "链接", "url", "路径",
            "仓库", "repo", "账号", "密码", "token", "密钥", "凭证", "权限",
            "合同", "订单", "工单", "记录", "名单", "编号", "标识",
            "必须确认", "需要用户确认", "无法继续", "缺少关键输入",
        )
        preference_keywords = (
            "风格", "样式", "配色", "颜色", "技术栈", "框架", "尺寸", "布局",
            "偏好", "细节", "文案口吻", "默认", "是否需要", "希望",
        )
        for item in missing_info or []:
            text = str(item or "").strip()
            if not text:
                continue
            lower = text.lower()
            if any(keyword in lower for keyword in blocker_keywords):
                blockers.append(text)
                continue
            if any(keyword in lower for keyword in preference_keywords):
                log.info(
                    "PlanningDecision missing info treated as defaultable preference goal=%r missing=%r",
                    goal_text[:80],
                    text,
                )
                continue
            blockers.append(text)
        return blockers

    async def _confirm_team_execution_mode(self, envelope: Envelope) -> dict[str, Any]:
        visible_session_id = _visible_session_id(envelope.session_id)
        schedule_planning_provider_warmup(self._provider_for_owner(envelope.user_id))
        questions = [{
            "id": "team_execution_mode",
            "question": (
                "请选择这次团队任务的执行方式。默认自动；系统会先理解任务结构，再选择合适的规划深度。"
            ),
            "options": [
                {
                    "label": "自动（默认）：根据本轮任务的工作单元、依赖和质量要求选择合适方式。",
                    "value": "auto",
                },
                {
                    "label": "标准：语义拆分工作单元，再稳定编译为顺序、并行或阶段式协作。",
                    "value": "standard",
                },
                {
                    "label": "快速：跳过规划模型，以极简路径完成单一、低风险任务。",
                    "value": "fast",
                },
                {
                    "label": "AI 深度规划：为动态发现、条件分支或需要循环收敛的复杂任务生成定制 DAG。",
                    "value": "ai",
                },
            ],
        }]
        try:
            followup_session_id, question_id = await send_followup_question_to(
                visible_session_id,
                questions,
                title="Leader 选择团队执行模式",
                origin={
                    "agent_id": "leader",
                    "agent_name": "Leader",
                    "team_session_id": envelope.session_id,
                    "mention_intent": "team_mode",
                },
            )
            answers = await wait_for_answer(followup_session_id, question_id)
        except Exception as exc:  # noqa: BLE001
            log.info(
                "Team execution mode followup fallback to auto session=%s err=%s",
                envelope.session_id,
                exc,
            )
            answers = []
        mode = self._team_mode_from_followup_answers(answers)
        profile: dict[str, Any] = {
            "requested_mode": mode,
            "profile_source": "user_followup",
        }
        return profile

    def _ensure_runtime_plan(
        self,
        session_id: str,
        team: Team,
        goal: str,
        external_team_id: str,
        owner_account_id: str = "",
        execution_profile: dict[str, Any] | None = None,
        team_spec: dict[str, Any] | None = None,
    ) -> TeamPlan | None:
        plan_key = self._key(session_id, owner_account_id)
        existing = self._plans.get(plan_key)
        if existing is not None:
            return existing
        graph_plan = self.graph_planner.plan(
            team,
            goal,
            execution_profile=execution_profile,
            team_spec=team_spec,
        )
        nodes, edges = graph_plan.nodes, graph_plan.edges
        if not nodes:
            return None
        created = self.create_plan(
            session_id,
            goal=goal,
            nodes=nodes,
            edges=edges,
            external_team_id=external_team_id,
            owner_account_id=owner_account_id,
            workflow_plan=graph_plan.workflow_plan,
        )
        plan_data = created.get("plan") if isinstance(created, dict) else None
        if isinstance(plan_data, dict):
            log.info(
                "[Team] Runtime 创建 TeamPlan session=%s nodes=%s policy_warnings=%s",
                session_id,
                [node.get("node_id") for node in plan_data.get("nodes") or []],
                [item.message for item in graph_plan.policy_report.warnings],
        )
        return self._plans.get(plan_key)

    async def _ensure_runtime_plan_async(
        self,
        session_id: str,
        team: Team,
        goal: str,
        external_team_id: str,
        owner_account_id: str = "",
        execution_profile: dict[str, Any] | None = None,
        team_spec: dict[str, Any] | None = None,
        planning_progress: Callable[[dict[str, Any]], Any] | None = None,
    ) -> TeamPlan | None:
        plan_key = self._key(session_id, owner_account_id)
        existing = self._plans.get(plan_key)
        if existing is not None:
            return existing
        graph_plan = await self.graph_planner.plan_async(
            team,
            goal,
            execution_profile=execution_profile,
            team_spec=team_spec,
            provider=self._provider_for_owner(owner_account_id),
            planning_progress=planning_progress,
        )
        blocking_missing = self._blocking_planning_missing_info(goal, list(graph_plan.critical_missing_info))
        if blocking_missing:
            self._planning_missing_info[plan_key] = blocking_missing
            return None
        self._planning_missing_info.pop(plan_key, None)
        nodes, edges = graph_plan.nodes, graph_plan.edges
        if not nodes:
            return None
        created = self.create_plan(
            session_id,
            goal=goal,
            nodes=nodes,
            edges=edges,
            external_team_id=external_team_id,
            owner_account_id=owner_account_id,
            workflow_plan=graph_plan.workflow_plan,
        )
        plan_data = created.get("plan") if isinstance(created, dict) else None
        if isinstance(plan_data, dict):
            log.info(
                "[Team] Runtime 创建 TeamPlan session=%s nodes=%s strategy=%s policy_warnings=%s",
                session_id,
                [node.get("node_id") for node in plan_data.get("nodes") or []],
                (nodes[0].get("metadata") or {}).get("plan_strategy") if nodes else "",
                [item.message for item in graph_plan.policy_report.warnings],
            )
        return self._plans.get(plan_key)

    def _has_open_member_question(self, team: Team, task_id: str) -> bool:
        if not task_id:
            return False
        question_types = {"question", "decision_request", "permission_request", "blocked"}
        for message in team.bus.list_messages(team.session.team_session_id):
            if message.get("task_id") == task_id and message.get("message_type") in question_types:
                if "leader" in list(message.get("recipient_member_ids") or []):
                    return True
        return False

    def _format_workflow_result(self, plan: TeamPlan | None) -> str:
        if plan is None:
            return "团队工作流未能创建可执行计划。"
        summary = next(
            (
                node.result_summary
                for node in plan.nodes.values()
                if node.node_id == "leader_summary" and str(node.result_summary or "").strip()
            ),
            "",
        )
        if summary:
            return summary
        lines = [f"团队工作流完成：{plan.goal}"]
        for node in plan.nodes.values():
            status = node.status
            summary = node.result_summary or node.last_error or "无结果摘要"
            lines.append(f"- [{status}] {node.title}（{node.assignee}）：{summary}")
        return "\n".join(lines)

    @staticmethod
    def _node_result_digest(text: str, limit: int = 260) -> str:
        return team_presenter.node_result_digest(text, limit=limit)

    @staticmethod
    def _clean_result_value(text: str, limit: int = 140) -> str:
        return team_presenter.clean_result_value(text, limit=limit)

    @staticmethod
    def _extract_result_contract(text: str) -> dict[str, str]:
        return team_presenter.extract_result_contract(text)

    @staticmethod
    def _business_result_summary(
        node: TeamPlanNode,
        text: str,
        *,
        is_review_submission: bool = False,
        preserve_detail: bool = False,
    ) -> str:
        return team_presenter.business_result_summary(
            node,
            text,
            is_review_submission=is_review_submission,
            preserve_detail=preserve_detail,
        )

    @staticmethod
    def _acceptance_headline(goal: str, summaries: list[str]) -> str:
        return team_presenter.acceptance_headline(goal, summaries)

    @staticmethod
    def _summary_node_label(node: TeamPlanNode) -> str:
        return team_presenter.summary_node_label(node)

    @staticmethod
    def _artifact_cards(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return team_presenter.artifact_cards(artifacts)

    @staticmethod
    def _is_review_submission_node(node: TeamPlanNode) -> bool:
        return team_presenter.is_review_submission_node(node)

    @staticmethod
    def _is_verify_execution_node(node: TeamPlanNode) -> bool:
        return team_presenter.is_verify_execution_node(node)

    @staticmethod
    def _assignment_text(node: TeamPlanNode) -> str:
        return team_presenter.assignment_text(node)

    @staticmethod
    def _should_show_assignment(plan: TeamPlan, node: TeamPlanNode) -> bool:
        return team_presenter.should_show_assignment(plan, node)

    @staticmethod
    def _team_goal_uses_shared_workspace(goal: str) -> bool:
        """Return whether a Team goal explicitly targets existing workspace files."""

        return flow_builder.team_goal_uses_shared_workspace(goal)

    def _team_delegate_cwd(
        self,
        envelope: Envelope,
        goal: str,
        *,
        node_id: str = "",
        agent_id: str = "",
    ) -> str:
        """Give abstract Team tasks a per-turn workspace to avoid stale artifact bleed."""

        if self._team_goal_uses_shared_workspace(goal):
            return ""
        try:
            session_dir = safe_path_segment(envelope.session_id, "team-turn")
            path = task_workspace_path(envelope.workspace_id or "default") / "team_turns" / session_dir
            if node_id or agent_id:
                path = (
                    path
                    / safe_path_segment(node_id, "node")
                    / safe_path_segment(agent_id, "agent")
                )
            path.mkdir(parents=True, exist_ok=True)
            return str(path)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "team turn workspace unavailable: session=%s workspace=%s error=%s",
                envelope.session_id,
                envelope.workspace_id,
                exc,
            )
            return ""

    @staticmethod
    def _team_shared_cwd(envelope: Envelope) -> str:
        """Resolve the actual project root used by shared-workspace Team nodes."""

        explicit = str(envelope.params.get("cwd") or "").strip()
        if explicit:
            path = Path(explicit).expanduser()
            path.mkdir(parents=True, exist_ok=True)
            return str(path.resolve())
        workspace_root = str(envelope.params.get("workspace_root_path") or "").strip()
        if workspace_root:
            path = Path(workspace_root).expanduser()
            if path.is_dir():
                return str(path.resolve())
        return str(task_workspace_path(envelope.workspace_id or "default").resolve())

    @staticmethod
    def _artifact_title_head(title: str) -> str:
        return team_presenter.artifact_title_head(title)

    @staticmethod
    def _markdown_document_title(content: str) -> str:
        return team_presenter.markdown_document_title(content)

    @staticmethod
    def _artifact_label(node: TeamPlanNode, content: str = "") -> str:
        return team_presenter.artifact_label(node, content)

    @staticmethod
    def _artifact_filename(node: TeamPlanNode, content: str = "") -> str:
        return team_presenter.artifact_filename(node, content)

    @staticmethod
    def _unique_artifact_path(artifact_dir: Path, filename: str) -> Path:
        return team_presenter.unique_artifact_path(artifact_dir, filename)

    def _write_node_markdown_artifact(
        self,
        envelope: Envelope,
        *,
        team: Team,
        node: TeamPlanNode,
        task_id: str,
        content: str,
    ) -> dict[str, Any] | None:
        text = str(content or "").strip()
        if not text:
            return None
        session_dir = safe_path_segment(envelope.session_id, "session")
        filename = self._artifact_filename(node, text)
        candidate_dirs: list[Path] = []
        try:
            candidate_dirs.append(task_workspace_path(envelope.workspace_id or "default") / "team_artifacts" / session_dir)
        except Exception as exc:  # noqa: BLE001
            log.warning("team artifact workspace unavailable: session=%s error=%s", envelope.session_id, exc)
        candidate_dirs.append(Path.cwd() / ".crew" / "team_artifacts" / session_dir)

        last_error: Exception | None = None
        for artifact_dir in candidate_dirs:
            try:
                artifact_dir.mkdir(parents=True, exist_ok=True)
                path = self._unique_artifact_path(artifact_dir, filename)
                path.write_text(text, encoding="utf-8")
                title = path.stem
                artifact = team.bus.add_artifact(
                    team_session_id=envelope.session_id,
                    owner_member_id=node.assignee,
                    summary=f"{title} 完整内容",
                    scope="team",
                    task_id=task_id,
                    content_type="text/markdown",
                    path=str(path),
                )
                return artifact.to_dict()
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        if last_error is not None:
            log.warning(
                "team artifact write failed: session=%s node=%s error=%s",
                envelope.session_id,
                node.node_id,
                last_error,
            )
        return None

    def _node_owned_artifacts(
        self,
        artifacts: list[dict[str, Any]],
        *,
        node: TeamPlanNode,
        task_id: str,
        workspace_root: str | Path | None = None,
    ) -> list[dict[str, Any]]:
        """Keep node-owned artifacts and normalize relative paths to the turn workspace."""

        owned: list[dict[str, Any]] = []
        seen: set[str] = set()
        resolved_root: Path | None = None
        if workspace_root:
            try:
                resolved_root = Path(workspace_root).expanduser().resolve()
            except Exception:  # noqa: BLE001
                resolved_root = None
        for artifact in artifacts:
            owner = str(artifact.get("owner_member_id") or "").strip()
            artifact_task_id = str(artifact.get("task_id") or "").strip()
            if owner and owner != node.assignee:
                continue
            if artifact_task_id and task_id and artifact_task_id != task_id:
                continue
            normalized = dict(artifact)
            raw_path = str(normalized.get("path") or "").strip()
            if raw_path:
                candidate = Path(raw_path).expanduser()
                was_relative = not candidate.is_absolute()
                if was_relative:
                    if resolved_root is None:
                        continue
                    try:
                        candidate = (resolved_root / candidate).resolve()
                        candidate.relative_to(resolved_root)
                    except (OSError, ValueError):
                        continue
                else:
                    try:
                        candidate = candidate.resolve()
                    except OSError:
                        continue
                if was_relative and not (candidate.is_file() or candidate.is_dir()):
                    continue
                normalized["path"] = str(candidate)
            key = str(normalized.get("path") or normalized.get("artifact_id") or normalized.get("summary") or "")
            if key in seen:
                continue
            seen.add(key)
            owned.append(normalized)
        return owned

    def _auto_file_artifacts_from_result(
        self,
        envelope: Envelope,
        *,
        team: Team,
        node: TeamPlanNode,
        task_id: str,
        text: str,
        existing_artifacts: list[dict[str, Any]],
        changed_paths: set[str] | None = None,
        workspace_root: str = "",
    ) -> list[dict[str, Any]]:
        """Register concrete file or directory paths mentioned by a node result."""

        existing_paths = {
            str(item.get("path") or "").strip()
            for item in [*team.bus.list_artifacts(envelope.session_id), *existing_artifacts]
            if str(item.get("path") or "").strip()
        }
        created: list[dict[str, Any]] = []
        candidate_paths = self._candidate_artifact_paths(
            envelope,
            str(text or ""),
            workspace_root=workspace_root,
        )
        for artifact_path in candidate_paths:
            path = str(artifact_path)
            if not path or path in existing_paths:
                continue
            if changed_paths is not None:
                if artifact_path.is_dir():
                    changed = any(
                        changed_path == path or changed_path.startswith(f"{path}{os.sep}")
                        for changed_path in changed_paths
                    )
                else:
                    changed = path in changed_paths
                if not changed:
                    continue
            suffix = artifact_path.suffix.lower().lstrip(".")
            content_type = "inode/directory" if artifact_path.is_dir() else {
                "html": "text/html",
                "htm": "text/html",
                "md": "text/markdown",
                "txt": "text/plain",
                "json": "application/json",
                "js": "text/javascript",
                "ts": "text/typescript",
                "tsx": "text/typescript",
                "py": "text/x-python",
                "png": "image/png",
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "gif": "image/gif",
                "svg": "image/svg+xml",
                "pdf": "application/pdf",
            }.get(suffix, "application/octet-stream")
            try:
                artifact = team.bus.add_artifact(
                    team_session_id=envelope.session_id,
                    owner_member_id=node.assignee,
                    summary=artifact_path.name,
                    scope="node-output",
                    task_id=task_id,
                    content_type=content_type,
                    path=str(artifact_path),
                ).to_dict()
            except Exception as exc:  # noqa: BLE001
                log.warning("auto team file artifact failed: session=%s node=%s path=%s err=%s", envelope.session_id, node.node_id, path, exc)
                continue
            existing_paths.add(path)
            created.append(artifact)
        return created

    @staticmethod
    def _workspace_file_snapshot(root: str | Path | None) -> FileMetadataSnapshot:
        base = Path(str(root or "")).expanduser() if root else None
        if base is None:
            return {}
        try:
            resolved_base = base.resolve()
            if not resolved_base.is_dir():
                return {}
        except Exception:  # noqa: BLE001
            return {}
        snapshot = workspace_snapshot(resolved_base)
        return snapshot or {}

    @classmethod
    def _workspace_file_changes(
        cls,
        root: str | Path | None,
        before: FileMetadataSnapshot,
    ) -> list[dict[str, Any]]:
        after = cls._workspace_file_snapshot(root)
        return changes_between_snapshots(before, after)

    @classmethod
    def _changed_workspace_files(
        cls,
        root: str | Path | None,
        before: dict[str, tuple[int, int]],
    ) -> set[str]:
        return {
            str(item.get("path") or "")
            for item in cls._workspace_file_changes(root, before)
            if item.get("status") != "deleted" and str(item.get("path") or "")
        }

    def _persist_node_full_result(
        self,
        envelope: Envelope,
        node: TeamPlanNode,
        text: str,
    ) -> tuple[str, int]:
        content = str(text or "")
        if not content:
            return "", 0
        try:
            delegate_cwd = str(self._team_delegate_cwd(envelope, str(envelope.query or "")) or "").strip()
            workspace = (
                Path(delegate_cwd)
                if delegate_cwd
                else task_workspace_path(envelope.workspace_id or "default")
                / "team_turns"
                / safe_path_segment(envelope.session_id, "team-turn")
            )
            result_dir = workspace / ".crew" / "node-results"
            result_dir.mkdir(parents=True, exist_ok=True)
            filename = f"{safe_path_segment(node.node_id, 'node')}.txt"
            path = result_dir / filename
            temp_path = result_dir / f".{filename}.tmp"
            temp_path.write_text(content, encoding="utf-8")
            temp_path.replace(path)
            return str(path.resolve()), len(content.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "persist Team node full result failed session=%s node=%s error=%s",
                envelope.session_id,
                node.node_id,
                exc,
            )
            return "", 0

    def _candidate_artifact_paths(
        self,
        envelope: Envelope,
        text: str,
        *,
        workspace_root: str = "",
    ) -> list[Path]:
        """Resolve file or directory mentions inside the current Team turn workspace."""

        raw_paths: list[str] = []
        seen_raw: set[str] = set()
        for regex in (_RESULT_PATH_RE, _RESULT_RELATIVE_PATH_RE, _RESULT_BACKTICK_PATH_RE):
            for match in regex.finditer(str(text or "")):
                raw = match.group("path").strip().strip("`'\"")
                if not raw or raw in seen_raw:
                    continue
                seen_raw.add(raw)
                raw_paths.append(raw)

        base_dirs: list[Path] = []
        if str(workspace_root or "").strip():
            try:
                base_dirs.append(Path(workspace_root).expanduser().resolve())
            except OSError:
                pass
        try:
            session_dir = safe_path_segment(envelope.session_id, "team-turn")
            turn_root = task_workspace_path(envelope.workspace_id or "default") / "team_turns" / session_dir
            if turn_root not in base_dirs:
                base_dirs.append(turn_root)
        except Exception as exc:  # noqa: BLE001
            log.debug("team artifact relative workspace unavailable: session=%s error=%s", envelope.session_id, exc)

        resolved: list[Path] = []
        seen: set[str] = set()
        for raw in raw_paths:
            raw_path = Path(raw)
            candidates = (
                [(raw_path, None)]
                if raw_path.is_absolute()
                else [(base / raw_path, base.resolve()) for base in base_dirs]
            )
            for candidate, allowed_base in candidates:
                try:
                    path = candidate.resolve()
                    if allowed_base is not None:
                        path.relative_to(allowed_base)
                    if not (path.is_file() or path.is_dir()):
                        continue
                except (OSError, ValueError):
                    continue
                key = str(path)
                if key in seen:
                    continue
                seen.add(key)
                resolved.append(path)
                break
        return resolved

    @staticmethod
    def _leader_control_text(plan: TeamPlan, node: TeamPlanNode, fallback_error: str = "") -> str:
        goal = plan.goal.strip() or "当前任务"
        if node.node_id == "leader_plan":
            member_nodes = [item for item in plan.nodes.values() if item.assignee != "leader"]
            if member_nodes:
                flow = "、".join(item.title for item in member_nodes[:4])
                return f"收到，我会按团队流程推进：{flow}，最后由我汇总给你。"
            return f"收到，我来处理：{goal}"
        if node.node_id == "leader_review" or node.node_id.startswith("leader_review_"):
            parent_ids = {
                edge.parent_id
                for edge in plan.edges
                if edge.child_id == node.node_id
            }
            reviewed = [
                item for item in plan.nodes.values()
                if item.node_id in parent_ids and item.assignee != "leader"
            ]
            if not reviewed:
                reviewed = [
                    item for item in plan.nodes.values()
                    if item.status == "completed" and item.assignee != "leader"
                ]
            mentions = " ".join(
                f"@{assignee}"
                for assignee in dict.fromkeys(item.assignee for item in reviewed)
                if assignee
            )
            target = "、".join(item.title for item in reviewed[:3]) if reviewed else "成员提交内容"
            prefix = f"{mentions} " if mentions else ""
            if reviewed and all(InProcessTeamManager._is_review_submission_node(item) for item in reviewed):
                return f"{prefix}方案已通过 Leader 审阅，开始验证。"
            return f"{prefix}{target} 已通过 Leader 审阅，继续推进后续节点。"
        if node.node_id.startswith("runtime_diagnosis_"):
            target_id = next((edge.child_id for edge in plan.edges if edge.parent_id == node.node_id), "")
            target = plan.nodes.get(target_id)
            target_title = target.title if target is not None else "后续节点"
            reason = node.detail.split("失败原因：", 1)[-1].strip() if "失败原因：" in node.detail else "上次执行失败"
            return (
                f"已完成运行诊断：{target_title} 上次失败原因是「{reason[:160]}」。"
                "下一轮将保留原团队与原成员，带着失败上下文重试；如仍失败，再提示用户确认补员、改派或缩小范围。"
            )
        if node.node_id == "leader_summary":
            completed = [
                item for item in plan.nodes.values()
                if item.status == "completed" and item.assignee != "leader"
            ]
            if not completed:
                error = InProcessTeamManager._node_result_digest(str(fallback_error or "").strip(), limit=180)
                lines = [
                    "团队最终汇总没有生成答案。",
                    f"- 用户问题：{goal}",
                    "- 已完成成员结果：暂无可汇总的成员结果。",
                ]
                if error:
                    lines.append(f"- 失败原因：{error}")
                lines.append("请在模型恢复可用后重试，或重新发送任务让团队继续执行。")
                return "\n".join(lines)
            summary_by_label: dict[str, str] = {}
            artifact_names: list[str] = []
            for item in completed:
                label = InProcessTeamManager._summary_node_label(item)
                summary_source = InProcessTeamManager._dedupe_repeated_colon_prefix(
                    str(item.result_summary or item.last_error or "").strip()
                )
                summary_source = InProcessTeamManager._node_result_digest(summary_source, limit=180)
                business = InProcessTeamManager._business_result_summary(
                    item,
                    summary_source,
                    is_review_submission=InProcessTeamManager._is_review_submission_node(item),
                )
                body = business.split("：", 1)[-1] if "：" in business else business
                if label not in summary_by_label and body:
                    summary_by_label[label] = body
                for ref in item.artifact_refs:
                    name = Path(str(ref)).name if "/" in str(ref) else str(ref)
                    if name and name not in artifact_names:
                        artifact_names.append(name)
            summaries = [f"{label}：{body}" for label, body in summary_by_label.items()]
            lines = [InProcessTeamManager._acceptance_headline(goal, summaries)]
            for line in summaries[:4]:
                lines.append(f"- {line}")
            if artifact_names:
                lines.append("产物已整理为下方文件卡片。")
            return "\n".join(lines)
        return f"{node.title} 已完成。"

    @staticmethod
    def _team_internal_chunk(
        request_id: str,
        *,
        agent_id: str,
        text: str,
        source_session_id: str = "",
        role: str = "",
        is_leader: bool = False,
        tone: int | None = None,
        append: bool = False,
        node_id: str = "",
        display_mode: str = "chat",
        event_type: str = "",
        collapsed_title: str = "",
        process_text: str = "",
        artifacts: list[dict[str, Any]] | None = None,
        turn_file_changes: list[dict[str, Any]] | None = None,
        thinking: str = "",
        tool_calls: list[dict[str, Any]] | None = None,
        turn_started_at: float | None = None,
        turn_duration: float | None = None,
        timestamp: float | None = None,
        mention_from: str = "",
        mention_to: list[str] | None = None,
        mention_intent: str = "",
    ) -> ResponseChunk:
        body: dict[str, Any] = {
            "text": text,
            "agent_id": CREW_BUILTIN_AGENT_ID if is_crew_builtin_display_id(agent_id) else agent_id,
            "agent_name": "Crew" if is_crew_builtin_display_id(agent_id) else agent_id,
            "agent_role": role,
            "source_session_id": source_session_id,
            "is_leader": is_leader,
            "display_mode": display_mode,
        }
        if node_id:
            body["node_id"] = node_id
        if event_type:
            body["event_type"] = event_type
        if collapsed_title:
            body["collapsed_title"] = collapsed_title
        if process_text:
            body["process_text"] = process_text
        if artifacts:
            body["artifacts"] = artifacts
        if turn_file_changes:
            body["turn_file_changes"] = turn_file_changes
        if thinking:
            body["thinking"] = thinking
        if tool_calls:
            body["tool_calls"] = tool_calls
        if turn_started_at is not None:
            body["turn_started_at"] = turn_started_at
        if turn_duration is not None:
            body["turn_duration"] = turn_duration
        if timestamp is not None:
            body["timestamp"] = timestamp
        if mention_from:
            body["mention_from"] = mention_from
        if mention_to:
            body["mention_to"] = list(mention_to)
        if mention_intent:
            body["mention_intent"] = mention_intent
        if append:
            body["append"] = True
        if tone is not None:
            body["agent_tone"] = tone
        return ResponseChunk(request_id=request_id, kind="team_internal", body=body)

    def _recorded_team_internal_chunk(
        self,
        envelope: Envelope,
        *,
        agent_id: str,
        text: str,
        source_session_id: str = "",
        role: str = "",
        is_leader: bool = False,
        tone: int | None = None,
        append: bool = False,
        node_id: str = "",
        event_type: str = "team_internal_message",
        display_mode: str = "chat",
        collapsed_title: str = "",
        process_text: str = "",
        artifacts: list[dict[str, Any]] | None = None,
        turn_file_changes: list[dict[str, Any]] | None = None,
        thinking: str = "",
        tool_calls: list[dict[str, Any]] | None = None,
        turn_started_at: float | None = None,
        turn_duration: float | None = None,
        timestamp: float | None = None,
        mention_from: str = "",
        mention_to: list[str] | None = None,
        mention_intent: str = "",
    ) -> ResponseChunk:
        chunk = self._team_internal_chunk(
            envelope.request_id,
            agent_id=agent_id,
            text=text,
            source_session_id=source_session_id,
            role=role,
            is_leader=is_leader,
            tone=tone,
            append=append,
            node_id=node_id,
            display_mode=display_mode,
            event_type=event_type,
            collapsed_title=collapsed_title,
            process_text=process_text,
            artifacts=artifacts,
            turn_file_changes=turn_file_changes,
            thinking=thinking,
            tool_calls=tool_calls,
            turn_started_at=turn_started_at,
            turn_duration=turn_duration,
            timestamp=timestamp,
            mention_from=mention_from,
            mention_to=mention_to,
            mention_intent=mention_intent,
        )
        if node_id:
            chunk.body["node_id"] = node_id
        payload = dict(chunk.body)
        payload["node_id"] = node_id
        payload["event_type"] = event_type
        if not append:
            self._record_team_event(
                envelope.session_id,
                owner_account_id=envelope.user_id,
                event_type=event_type,
                actor=agent_id,
                node_id=node_id,
                payload=payload,
            )
        return chunk

    async def _run_leader_node(
        self,
        envelope: Envelope,
        *,
        team: Team,
        plan: TeamPlan,
        node: TeamPlanNode,
        attempt: int,
        correction: str = "",
        on_chunk: Callable[[ResponseChunk], None] | None = None,
    ) -> str:
        completed: list[str] = []
        reviewed_ids = set(self._node_dependencies(plan, node.node_id)) if node.node_id.startswith("leader_review") else set()
        completed_nodes = [
            item
            for item in plan.nodes.values()
            if item.status == "completed"
            and item.node_id != node.node_id
            and (not reviewed_ids or item.node_id in reviewed_ids)
        ]
        upstream_artifact_refs: list[str] = []
        for item in completed_nodes:
            meta = dict(item.metadata or {})
            summary = self._node_result_digest(
                self._dedupe_repeated_colon_prefix(item.result_summary or item.last_error or "已完成"),
                limit=600,
            )
            contract = meta.get("result_contract") if isinstance(meta.get("result_contract"), dict) else {}
            contract_bits = []
            for label, key in (("结论", "answer"), ("依据", "evidence"), ("风险", "risk"), ("建议", "next_action")):
                value = self._node_result_digest(str(contract.get(key) or "").strip(), limit=300)
                if value:
                    contract_bits.append(f"{label}：{value}")
            artifact_refs = [str(ref) for ref in item.artifact_refs if str(ref).strip()]
            upstream_artifact_refs.extend(artifact_refs)
            completed.append(f"- {item.title}（{item.assignee}）：{summary}")
            if contract_bits:
                completed.append(f"  结构化结果：{'；'.join(contract_bits)}")
            if artifact_refs:
                completed.append(f"  产物引用：{'; '.join(artifact_refs[:6])}")
        member_lines = []
        for spec in (team.members or {}).values():
            metadata = dict(spec.metadata or {})
            role_label = str(metadata.get("role_label") or "").strip()
            if not role_label:
                role_label = next(
                    (line.strip().lstrip("#").strip() for line in str(spec.role or "").splitlines() if line.strip()),
                    "未声明职责",
                )
            member_lines.append(f"- {spec.member_id} / {spec.name}: {role_label[:80]}")
        if node.node_id == "leader_summary":
            leader_instruction = "\n".join([
                "请作为团队 Leader 进行最终汇总，直接回答用户原始问题。",
                "不要只说任务已完成；要对用户的问题给出可理解结论。",
                "如果关键信息不足，请明确指出需要用户补充什么，并说明已确认的团队状态。",
                "请概括成员输出，避免重复粘贴节点标题或原文。",
                "不要再派发新任务。",
            ])
        elif node.node_id == "leader_review" or node.node_id.startswith("leader_review_"):
            leader_instruction = "\n".join([
                "请作为团队 Leader 真实审阅成员提交。",
                "判断成员输出是否回答了当前目标、是否存在信息缺口、是否需要向用户追问或继续推进。",
                "必须输出 JSON 对象，不要输出 JSON 之外的文字。",
                '格式：{"action":"approve|revise|ask_user|block","target_node_id":"需要修订的节点 ID 或空字符串","message":"审阅结论","instructions":"给成员的具体修订要求或空字符串"}。',
                "revise 仅用于原成员可按明确意见继续优化，并且必须填写被修订父节点的 target_node_id；"
                "ask_user 用于必须由用户补充信息；block 用于当前团队无法继续。",
                "如果存在安全、可逆的默认处理方式，不要只说缺少信息；请在 ask_user 的 message 中先给出你的默认建议、影响和确认问题，"
                "让用户可以直接确认继续或选择调整。",
            ])
        else:
            leader_instruction = "\n".join([
                "请作为团队 Leader 直接处理当前节点。",
                "你拥有团队上下文和成员状态，请优先回答用户问题；如果信息不足，请明确需要用户补充什么。",
                f"当前 TeamPlan 节点 ID：{node.node_id}。",
                "如果你发现当前 DAG 缺少必要成员工作，必须调用 request_plan_change(add_node) 新增节点；不要绕过 DAG 派活。",
                "如果要派发现有成员节点，请调用 team_mention(intent=\"assign\", to=[成员], node_id=\"现有节点ID\", content=\"执行要求\")。",
                "新增节点应写明 assignee、title、detail、required_capabilities、depends_on 和 before；"
                "required_capabilities 只能使用工具 schema 约定的标准能力 key；"
                "通常新增执行节点应 before=leader_summary，让 Runtime 在成员完成后重新汇总。",
                "不要伪造外部实时信息。",
            ])
        instruction = "\n".join([
            node.title,
            "",
            node.detail,
            "",
            leader_instruction,
            "",
            "用户原始问题：",
            plan.goal,
            "",
            "团队成员：",
            *(member_lines or ["- 暂无普通成员"]),
            "",
            "已完成节点：",
            *(completed or ["- 暂无"]),
            *(["", "审阅纠正要求：", correction] if correction else []),
        ])
        review_meta = dict(node.metadata or {})
        user_followup_answers = review_meta.get("user_followup_answers")
        if user_followup_answers:
            answer_label = (
                "用户未在超时时间内回答，系统已采用安全默认选择"
                if review_meta.get("user_followup_timeout_default")
                else "用户针对本次审阅的回答"
            )
            instruction = "\n".join([
                instruction,
                "",
                f"{answer_label}：",
                json.dumps(user_followup_answers, ensure_ascii=False),
                "请基于该回答重新审阅；本轮必须决定 approve、revise 或 block，不要再次 ask_user。",
            ])
        self._mark_plan_node(
            envelope.session_id,
            node.node_id,
            owner_account_id=envelope.user_id,
            status="in_progress",
            attempt_count=attempt,
            last_error="",
        )
        leader_env = Envelope.of(
            instruction,
            session_id=f"{envelope.session_id}::leader",
            params={
                **{k: v for k, v in envelope.params.items() if k != "query"},
                "task_session_id": envelope.session_id,
                "team_session_id": envelope.session_id,
                "member_session_id": f"{envelope.session_id}::leader",
                "agent_id": "leader",
                "team_plan_node_id": node.node_id,
                "team_goal": plan.goal,
                "team_node_title": node.title,
                "team_node_detail": node.detail,
                "team_upstream_summary": "\n".join(completed),
                "team_upstream_artifacts": "\n".join(dict.fromkeys(upstream_artifact_refs)),
                "team_display_name": team.display_name,
                "external_team_role": "leader",
            },
            request_id=envelope.request_id,
            channel=envelope.channel,
            user_id=envelope.user_id,
            workspace_id=envelope.workspace_id,
            mode="agent",
            attachments=[
                dict(attachment)
                for attachment in (envelope.attachments or [])
                if isinstance(attachment, dict)
            ],
        )
        # Leader 可能在最终汇总阶段兜底生成可运行文件。与普通成员保持同一工作区
        # 语义：抽象任务写入本次 Team Turn；显式操作既有项目时继续使用共享工作区。
        leader_cwd = self._team_delegate_cwd(envelope, plan.goal)
        leader_workspace = (
            Path(leader_cwd)
            if leader_cwd
            else task_workspace_path(envelope.workspace_id or "default")
        )
        if leader_cwd:
            leader_env.params["cwd"] = leader_cwd
        workspace_snapshot = self._workspace_file_snapshot(leader_workspace)
        runtime_artifact_text: list[str] = []
        final_text = ""
        error_text = ""
        async for chunk in team.leader.run(leader_env):
            if on_chunk is not None:
                on_chunk(chunk)
            if chunk.kind == "final":
                final_text = str(chunk.body.get("text") or "")
            elif chunk.kind == "error":
                error_text = str(chunk.body.get("message") or "Leader 节点执行失败")
            elif chunk.kind == "tool":
                runtime_artifact_text.append(json.dumps(chunk.body, ensure_ascii=False, default=str))
        if error_text:
            raise RuntimeError(error_text)
        changed_paths = self._changed_workspace_files(leader_workspace, workspace_snapshot)
        if changed_paths:
            task_id = self._plan_node_tasks.get(
                (envelope.user_id, envelope.session_id, node.node_id),
                "",
            )
            leader_artifacts = self._auto_file_artifacts_from_result(
                envelope,
                team=team,
                node=node,
                task_id=task_id,
                text="\n".join([
                    final_text,
                    *runtime_artifact_text,
                    *sorted(changed_paths),
                ]),
                existing_artifacts=[],
                changed_paths=changed_paths,
            )
            if leader_artifacts:
                created_refs = [
                    str(item.get("path") or item.get("artifact_id") or "").strip()
                    for item in leader_artifacts
                    if str(item.get("path") or item.get("artifact_id") or "").strip()
                ]
                node.artifact_refs = list(dict.fromkeys([
                    *node.artifact_refs,
                    *created_refs,
                ]))
        return final_text or "Leader 已完成该验收节点，但未返回详细文本。"

    def _leader_runtime_stream_chunk(
        self,
        envelope: Envelope,
        node: TeamPlanNode,
        chunk: ResponseChunk,
    ) -> ResponseChunk | None:
        common = {
            "agent_id": "leader",
            "role": "leader",
            "is_leader": True,
            "source_session_id": f"{envelope.session_id}::leader",
            "node_id": node.node_id,
            "event_type": "team_stream",
            "display_mode": "stream",
            "collapsed_title": f"{node.title} 的执行过程",
            "append": True,
        }
        if chunk.kind == "delta":
            text = str(chunk.body.get("text") or "")
            return self._team_internal_chunk(envelope.request_id, text=text, **common) if text else None
        if chunk.kind == "thinking":
            text = str(chunk.body.get("text") or "")
            return self._team_internal_chunk(
                envelope.request_id,
                text="",
                thinking=text,
                **common,
            ) if text.strip() else None
        if chunk.kind == "tool":
            body = chunk.body
            phase = str(body.get("phase") or "")
            tool_call = {
                "id": str(body.get("tool_call_id") or chunk.sequence or "leader_tool"),
                "name": str(body.get("name") or "unknown"),
                "ui_label": str(body.get("ui_label") or body.get("name") or "工具调用"),
                "arguments": body.get("arguments") or body.get("args") or {},
                "result": body.get("result") or body.get("detail") or "",
                "status": "running" if phase == "start" else ("error" if phase == "error" else "done"),
            }
            return self._team_internal_chunk(
                envelope.request_id,
                text="",
                tool_calls=[tool_call],
                **common,
            )
        return None

    async def _stream_leader_node(
        self,
        envelope: Envelope,
        *,
        team: Team,
        plan: TeamPlan,
        node: TeamPlanNode,
        attempt: int,
        correction: str = "",
    ) -> AsyncIterator[tuple[ResponseChunk | None, str | None]]:
        queue: asyncio.Queue[ResponseChunk] = asyncio.Queue()
        task = asyncio.create_task(self._run_leader_node(
            envelope,
            team=team,
            plan=plan,
            node=node,
            attempt=attempt,
            correction=correction,
            on_chunk=queue.put_nowait,
        ))
        while not task.done():
            queue_task = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait([task, queue_task], return_when=asyncio.FIRST_COMPLETED)
            if queue_task in done:
                live_chunk = self._leader_runtime_stream_chunk(envelope, node, queue_task.result())
                if live_chunk is not None:
                    yield live_chunk, None
            else:
                queue_task.cancel()
        while not queue.empty():
            live_chunk = self._leader_runtime_stream_chunk(envelope, node, queue.get_nowait())
            if live_chunk is not None:
                yield live_chunk, None
        yield None, task.result()

    @staticmethod
    def _planning_progress_process_text(phase: str, status: str, detail: str = "") -> str:
        phase_rank = {
            "started": 0,
            "connected": 1,
            "reasoning": 1,
            "content": 1,
            "chat_fallback": 1,
            "parsed": 2,
            "compiled": 3,
            "fallback": 3,
        }
        rank = phase_rank.get(phase, 0)
        failed = status in {"fallback", "error"}
        completed = status == "done"

        def state(index: int) -> str:
            if completed:
                return "完成"
            if failed and index >= 2:
                return "已切换稳定流程" if index == 2 else "待执行"
            if rank > index:
                return "完成"
            if rank == index:
                return "进行中"
            return "待开始"

        lines = [
            f"- 理解任务目标：{state(0)}",
            f"- 识别工作单元：{state(1)}",
            f"- 生成团队执行图：{state(2)}",
            f"- 准备团队执行：{state(3)}",
        ]
        if detail:
            lines.append(f"- 说明：{detail}")
        return "\n".join(lines)

    @classmethod
    def _planning_progress_chunk(cls, envelope: Envelope, event: dict[str, Any]) -> ResponseChunk:
        phase = str(event.get("phase") or "started")
        status = str(event.get("status") or "running")
        label = str(event.get("label") or "正在规划团队协作")
        detail = str(event.get("detail") or "")
        try:
            elapsed_ms = int(event.get("elapsed_ms") or 0)
        except (TypeError, ValueError):
            elapsed_ms = 0
        now = time.time()
        elapsed_seconds = max(0.0, elapsed_ms / 1000)
        # Team 规划直接沿用普通 Agent Turn 的计时契约。前端以
        # turn_started_at 每秒本地刷新，不需要后端为显示秒数高频推帧。
        turn_started_at = now - elapsed_seconds
        running = status not in {"done", "fallback", "error"}
        title_prefix = "Crew 正在规划团队协作"
        if status == "done":
            title_prefix = "Crew 已生成团队执行图"
        elif status == "fallback":
            title_prefix = "Crew 已切换稳定执行图"
        return cls._team_internal_chunk(
            envelope.request_id,
            agent_id=CREW_BUILTIN_AGENT_ID,
            text=label,
            source_session_id=f"{envelope.session_id}::planning",
            role="规划团队协作",
            is_leader=True,
            node_id="workflow_planning",
            display_mode="stream" if running else "collapsible",
            event_type="team_planning_progress",
            collapsed_title=title_prefix,
            process_text=cls._planning_progress_process_text(phase, status, detail),
            turn_started_at=turn_started_at,
            turn_duration=None if running else elapsed_seconds,
            timestamp=now,
        )

    async def _stream_runtime_plan(
        self,
        envelope: Envelope,
        *,
        team: Team,
        goal: str,
        external_team_id: str,
        owner_account_id: str,
        execution_profile: dict[str, Any] | None,
        team_spec: dict[str, Any] | None = None,
    ) -> AsyncIterator[tuple[ResponseChunk | None, TeamPlan | None]]:
        queue: asyncio.Queue[ResponseChunk] = asyncio.Queue()

        def on_progress(event: dict[str, Any]) -> None:
            queue.put_nowait(self._planning_progress_chunk(envelope, event))

        task = asyncio.create_task(self._ensure_runtime_plan_async(
            envelope.session_id,
            team,
            goal,
            external_team_id,
            owner_account_id=owner_account_id,
            execution_profile=execution_profile,
            team_spec=team_spec,
            planning_progress=on_progress,
        ))
        while not task.done():
            queue_task = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait([task, queue_task], return_when=asyncio.FIRST_COMPLETED)
            if queue_task in done:
                yield queue_task.result(), None
            else:
                queue_task.cancel()
        while not queue.empty():
            yield queue.get_nowait(), None
        yield None, task.result()

    async def _run_required_workflow(
        self,
        envelope: Envelope,
        *,
        team: Team,
        external_team_id: str,
        execution_profile: dict[str, Any] | None = None,
        team_spec: dict[str, Any] | None = None,
    ) -> AsyncIterator[ResponseChunk]:
        goal = str(envelope.query or "").strip()
        explicit_profile = envelope.params.get("team_execution_profile")
        if execution_profile is not None:
            resolved_execution_profile = execution_profile
        elif envelope.params.get("team_confirm_execution_mode") and not isinstance(explicit_profile, dict):
            resolved_execution_profile = await self._confirm_team_execution_mode(envelope)
        else:
            resolved_execution_profile = self._team_execution_profile(envelope)
        plan = None
        async for planning_chunk, planned in self._stream_runtime_plan(
            envelope,
            team=team,
            goal=goal,
            external_team_id=external_team_id,
            owner_account_id=envelope.user_id,
            execution_profile=resolved_execution_profile,
            team_spec=team_spec,
        ):
            if planning_chunk is not None:
                yield planning_chunk
            if planned is not None:
                plan = planned
        planning_key = self._key(envelope.session_id, envelope.user_id)
        missing_info = self._planning_missing_info.pop(planning_key, [])
        if plan is None and missing_info:
            question = "为了正确拆分本轮任务，请补充：" + "；".join(missing_info)
            try:
                followup_session_id, question_id = await send_followup_question_to(
                    _visible_session_id(envelope.session_id),
                    [{"id": "workflow_planning_missing_info", "question": question, "inputMode": "text"}],
                    title="Leader 需要补充任务信息",
                    origin={
                        "agent_id": "leader",
                        "agent_name": "Leader",
                        "team_session_id": envelope.session_id,
                        "mention_intent": "workflow_planning",
                    },
                )
                answers = await wait_for_answer(followup_session_id, question_id)
            except Exception as exc:  # noqa: BLE001
                log.info("Workflow PlanningDecision followup failed session=%s err=%s", envelope.session_id, exc)
                yield ResponseChunk.error(envelope.request_id, "Leader 追问任务缺失信息失败，请补充目标后重试。")
                return
            answer_texts = [
                str(value or "").strip()
                for item in answers
                if isinstance(item, dict)
                for value in (item.get("answers") if isinstance(item.get("answers"), list) else [])
                if str(value or "").strip()
            ]
            if not answer_texts:
                yield ResponseChunk.final(
                    envelope.request_id,
                    "任务还缺少关键信息，已暂停本轮规划。请补充必要信息后继续。",
                    reason="planning_missing_info",
                )
                return
            goal = f"{goal}\n\n用户补充：{'；'.join(answer_texts)}"
            plan = None
            async for planning_chunk, planned in self._stream_runtime_plan(
                envelope,
                team=team,
                goal=goal,
                external_team_id=external_team_id,
                owner_account_id=envelope.user_id,
                execution_profile=execution_profile,
            ):
                if planning_chunk is not None:
                    yield planning_chunk
                if planned is not None:
                    plan = planned
        if plan is None:
            if self._planning_missing_info.pop(planning_key, []):
                yield ResponseChunk.final(
                    envelope.request_id,
                    "补充信息后任务仍不够明确，已暂停规划，请重新描述目标。",
                    reason="planning_missing_info",
                )
            else:
                yield ResponseChunk.final(
                    envelope.request_id,
                    "Team 没有可委派成员，无法创建团队执行计划。",
                    reason="team_plan_empty",
                )
            return

        yield ResponseChunk.status_event(envelope.request_id, "Team Runtime 已创建 TeamPlan，开始按节点派活…")
        max_rounds = max(1, len(plan.nodes) * 3)
        profile = execution_profile or {}
        budget = profile.get("budget") if isinstance(profile.get("budget"), dict) else {}
        try:
            max_attempts = max(1, int(budget.get("max_retries") or 2))
        except (TypeError, ValueError):
            max_attempts = 2
        try:
            max_review_revisions = max(0, int(budget.get("max_review_revisions") or 2))
        except (TypeError, ValueError):
            max_review_revisions = 2
        for _ in range(max_rounds):
            progressed = False
            pause_dispatch_this_round = False
            for node in list(plan.nodes.values()):
                if node.status not in {"pending", "failed"}:
                    continue
                if node.assignee != "leader" or not self._node_ready(plan, node):
                    continue
                attempt = node.attempt_count + 1
                should_run_leader = (
                    node.node_id in {"leader_review", "leader_summary"}
                    or (
                        node.node_id not in {"leader_plan"}
                        and not node.node_id.startswith("runtime_diagnosis_")
                    )
                )
                if should_run_leader:
                    try:
                        result = ""
                        async for live_chunk, final_result in self._stream_leader_node(
                            envelope,
                            team=team,
                            plan=plan,
                            node=node,
                            attempt=attempt,
                        ):
                            if live_chunk is not None:
                                yield live_chunk
                            if final_result is not None:
                                result = final_result
                        if not self._leader_model_result_usable(result):
                            result = self._leader_control_text(plan, node, fallback_error="Leader 模型返回内容不可用")
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "team leader node model execution failed, fallback to control text: session=%s node=%s error=%s",
                            envelope.session_id,
                            node.node_id,
                            exc,
                        )
                        result = self._leader_control_text(plan, node, fallback_error=str(exc))
                else:
                    result = self._leader_control_text(plan, node)
                is_review = node.node_id == "leader_review" or node.node_id.startswith("leader_review_")
                decision: dict[str, str] | None = None
                followup_resumed = False
                leader_node_requeued = False
                if is_review:
                    parsed_decision = self._parse_leader_review_decision(result)
                    if self._leader_review_decision_conflicts(plan, node, parsed_decision):
                        result = ""
                        async for live_chunk, final_result in self._stream_leader_node(
                            envelope,
                            team=team,
                            plan=plan,
                            node=node,
                            attempt=attempt,
                            correction=(
                                "上一次判断声称缺少成员方案，但结构化上游摘要和产物引用均已提供。"
                                "请重新阅读“已完成节点”和 Team Context Summary 后审阅，不要要求用户重复提供已有内容。"
                            ),
                        ):
                            if live_chunk is not None:
                                yield live_chunk
                            if final_result is not None:
                                result = final_result
                        parsed_decision = self._parse_leader_review_decision(result)
                        if self._leader_review_decision_conflicts(plan, node, parsed_decision):
                            parsed_decision = {
                                "action": "approve",
                                "target_node_id": "",
                                "message": "成员方案和产物引用已齐备，矛盾重审仍未正确消费上下文，按结构化提交放行后续执行。",
                                "instructions": "",
                            }
                    if (node.metadata or {}).get("user_followup_answers") and parsed_decision.get("action") == "ask_user":
                        parsed_decision = {
                            **parsed_decision,
                            "action": "block",
                            "message": (
                                f"{parsed_decision.get('message') or 'Leader 仍无法决策'} "
                                "用户已回答本次追问，Review 不再重复 ask_user，请人工检查审阅条件。"
                            ).strip(),
                        }
                    decision = self._apply_leader_review_decision(
                        plan,
                        node,
                        parsed_decision,
                        owner_account_id=envelope.user_id,
                        max_revisions=max_review_revisions,
                    )
                    result = str(decision.get("message") or result)
                    node.attempt_count = attempt
                    if decision.get("action") == "ask_user":
                        questions = [{
                            "id": "leader_review_decision",
                            "question": result or "Leader 需要你确认后续处理方式。",
                            "options": ["确认并继续", "需要调整"],
                        }]
                        try:
                            visible_session_id = _visible_session_id(envelope.session_id)
                            followup_session_id, question_id = await send_followup_question_to(
                                visible_session_id,
                                questions,
                                title="Leader 需要确认",
                                origin={
                                    "agent_id": "leader",
                                    "agent_name": "Leader",
                                    "team_session_id": envelope.session_id,
                                    "node_id": node.node_id,
                                    "mention_intent": "ask_user",
                                },
                            )
                            try:
                                answers = await wait_for_answer(followup_session_id, question_id)
                            except TypeError:
                                answers = await wait_for_answer(followup_session_id, question_id)
                        except Exception as exc:  # noqa: BLE001
                            log.warning(
                                "Leader review followup failed session=%s node=%s err=%s",
                                envelope.session_id,
                                node.node_id,
                                exc,
                            )
                            answers = []
                        if self._review_followup_answered(answers):
                            review_meta = dict(node.metadata or {})
                            review_meta["user_followup_answers"] = answers
                            review_meta["followup_count"] = int(review_meta.get("followup_count") or 0) + 1
                            node.metadata = review_meta
                            self._mark_plan_node(
                                envelope.session_id,
                                node.node_id,
                                owner_account_id=envelope.user_id,
                                status="pending",
                                result_summary="",
                                last_error="",
                            )
                            followup_resumed = True
                            pause_dispatch_this_round = True
                else:
                    leader_node_requeued = self._consume_plan_change_requeue(node)
                    if leader_node_requeued:
                        result = result or "TeamPlan 已更新，等待新增节点完成后重新汇总。"
                        self._mark_plan_node(
                            envelope.session_id,
                            node.node_id,
                            owner_account_id=envelope.user_id,
                            status="pending",
                            result_summary="TeamPlan 已更新，等待新增节点完成后重新执行。",
                            attempt_count=attempt,
                            last_error="",
                        )
                    else:
                        self._mark_plan_node(
                            envelope.session_id,
                            node.node_id,
                            owner_account_id=envelope.user_id,
                            status="completed",
                            result_summary=result,
                            attempt_count=attempt,
                        )
                        if node.node_id == "leader_summary":
                            await self._refresh_final_display_metadata(
                                plan,
                                owner_account_id=envelope.user_id,
                                final_summary=result,
                            )
                leader_event_type = (
                    "team_summary" if node.node_id == "leader_summary"
                    and not leader_node_requeued
                    else "team_review" if is_review
                    else "team_decision"
                )
                leader_artifacts = (
                    self._artifact_cards(team.bus.list_artifacts(envelope.session_id))
                    if node.node_id == "leader_summary"
                    else None
                )
                yield self._recorded_team_internal_chunk(
                    envelope,
                    agent_id="leader",
                    role="leader",
                    is_leader=True,
                    source_session_id=f"{envelope.session_id}::leader",
                    text=result,
                    node_id=node.node_id,
                    event_type=leader_event_type,
                    artifacts=leader_artifacts,
                )
                if decision is not None:
                    action = str(decision.get("action") or "approve")
                    target = str(decision.get("target_node_id") or "")
                    review_meta = dict(node.metadata or {})
                    timeout_default_note = str(review_meta.get("user_followup_timeout_note") or "")
                    decision_text = {
                        "approve": "审阅通过，继续后续流程。",
                        "revise": f"审阅未通过，@{plan.nodes[target].assignee} 请继续修订。" if target in plan.nodes else "审阅未通过，请继续修订。",
                        "ask_user": (
                            f"{timeout_default_note} Leader 将带着默认选择重新审阅。".strip()
                            if timeout_default_note
                            else "已收到用户回答，Leader 将带着回答重新审阅。"
                            if followup_resumed
                            else "需要用户补充信息，团队流程已暂停。"
                        ),
                        "block": "当前条件下无法继续，团队流程已阻塞。",
                    }[action]
                    yield self._recorded_team_internal_chunk(
                        envelope,
                        agent_id="leader",
                        role="leader",
                        is_leader=True,
                        source_session_id=f"{envelope.session_id}::leader",
                        text=decision_text,
                        node_id=node.node_id,
                        event_type="team_decision",
                        mention_from="leader",
                        mention_to=[plan.nodes[target].assignee] if target in plan.nodes else [],
                        mention_intent=action,
                    )
                yield ResponseChunk.status_event(envelope.request_id, f"完成节点「{node.title}」")
                progressed = True
                if pause_dispatch_this_round:
                    break

            if pause_dispatch_this_round:
                progressed = True
                continue

            dispatch_nodes: list[TeamPlanNode] = []
            occupied_assignees: set[str] = set()
            for node in list(plan.nodes.values()):
                if node.status not in {"pending", "failed"}:
                    continue
                if not self._node_ready(plan, node):
                    continue
                if node.assignee == "leader":
                    continue
                staffing_trigger = self._runtime_staffing_trigger(
                    team,
                    node,
                    owner_account_id=envelope.user_id,
                    max_attempts=max_attempts,
                )
                if staffing_trigger is not None:
                    yield ResponseChunk.status_event(
                        envelope.request_id,
                        f"「{node.title}」需要一位协作助手，等待你的选择…",
                    )
                    team, staffing_status = await self._handle_runtime_staffing(
                        envelope,
                        plan,
                        node,
                        team,
                        staffing_trigger,
                    )
                    if staffing_status == "applied":
                        yield ResponseChunk.status_event(
                            envelope.request_id,
                            f"协作助手已加入本次任务，正在继续「{node.title}」。",
                        )
                    elif staffing_status == "declined":
                        yield ResponseChunk.status_event(
                            envelope.request_id,
                            f"这次先不添加协作助手，「{node.title}」暂时停在这里。",
                        )
                    elif staffing_status == "failed":
                        yield ResponseChunk.status_event(
                            envelope.request_id,
                            f"暂时没能找到可加入的协作助手，「{node.title}」先停在这里。",
                        )
                    progressed = True
                    continue
                if node.assignee not in team.teammates:
                    self._reflect_plan_node(
                        plan,
                        node,
                        owner_account_id=envelope.user_id,
                        reason=f"未知或不可委派成员 {node.assignee}",
                        decision="保持用户团队不变，停止自动改派。",
                        suggested_action="请确认是否补充成员、改派节点或由 Leader 临时承接。",
                    )
                    self._mark_plan_node(
                        envelope.session_id,
                        node.node_id,
                        owner_account_id=envelope.user_id,
                        status="blocked",
                        result_summary=f"无法派活：未知或不可委派成员 {node.assignee}",
                        last_error=f"unknown assignee: {node.assignee}",
                    )
                    progressed = True
                    continue
                if node.attempt_count >= max_attempts:
                    self._reflect_plan_node(
                        plan,
                        node,
                        owner_account_id=envelope.user_id,
                        reason=f"节点连续失败 {node.attempt_count} 次",
                        decision="停止自动重试，保留当前团队并等待用户确认下一步。",
                        suggested_action="可选择补员、改派、缩小任务范围或手动重试。",
                    )
                    self._mark_plan_node(
                        envelope.session_id,
                        node.node_id,
                        owner_account_id=envelope.user_id,
                        status="blocked",
                        result_summary=f"节点连续失败 {node.attempt_count} 次，已停止重试，等待 Leader/用户介入。",
                    )
                    progressed = True
                    continue
                if node.assignee in occupied_assignees:
                    continue
                occupied_assignees.add(node.assignee)
                dispatch_nodes.append(node)

            dispatch_team = team
            live_queue: asyncio.Queue[ResponseChunk] = asyncio.Queue()
            member_stream_text: dict[str, list[str]] = {}
            member_runtime_events: dict[str, list[dict[str, Any]]] = {}
            member_file_changes: dict[str, list[dict[str, Any]]] = {}

            def _relay_child_chunk(node: TeamPlanNode, member: str, chunk: ResponseChunk) -> None:
                text = ""
                append = False
                started_at = float((node.metadata or {}).get("execution_started_at") or node.updated_at or time.time())
                now = time.time()
                if chunk.kind == "file_changes":
                    files = chunk.body.get("files") if isinstance(chunk.body, dict) else None
                    if isinstance(files, list):
                        member_file_changes[node.node_id] = merge_changes(
                            member_file_changes.get(node.node_id, []),
                            [item for item in files if isinstance(item, dict)],
                        )
                    return
                runtime_event = self._child_chunk_execution_event(node, member, chunk)
                if runtime_event is not None:
                    events = member_runtime_events.setdefault(node.node_id, [])
                    events.append(runtime_event)
                    # Thinking commonly arrives as many small chunks. Keeping only
                    # the last ten events evicted early tools/thoughts before the
                    # final team_submit was built, so the timeline disappeared on
                    # completion or refresh. Bound generously and compact below.
                    member_runtime_events[node.node_id] = events[-200:]
                    live_queue.put_nowait(self._recorded_team_internal_chunk(
                        envelope,
                        agent_id=member,
                        role=node.title,
                        source_session_id=f"{envelope.session_id}::{member}",
                        text="",
                        append=True,
                        node_id=node.node_id,
                        event_type="team_stream",
                        display_mode="stream",
                        collapsed_title=f"{node.title} 的执行过程",
                        thinking=str(runtime_event.get("event_text") or "") if runtime_event.get("event_type") == "thinking" else "",
                        tool_calls=[dict(runtime_event.get("tool_call") or {})]
                        if runtime_event.get("event_type") == "tool" and isinstance(runtime_event.get("tool_call"), dict)
                        else None,
                        turn_started_at=started_at,
                        turn_duration=max(0.0, now - started_at),
                        timestamp=now,
                    ))
                if chunk.kind == "delta":
                    text = str(chunk.body.get("text") or "")
                    append = True
                    if text:
                        member_stream_text.setdefault(node.node_id, []).append(text)
                elif chunk.kind == "final":
                    final_text = str(chunk.body.get("text") or "")
                    if final_text:
                        member_stream_text.setdefault(node.node_id, []).append(final_text)
                    return
                elif chunk.kind == "tool":
                    return
                elif chunk.kind == "thinking":
                    return
                elif chunk.kind == "status":
                    return
                elif chunk.kind == "error":
                    text = "我这边执行遇到问题，需要看板详情继续排查。"
                if not text:
                    return
                live_queue.put_nowait(self._recorded_team_internal_chunk(
                    envelope,
                    agent_id=member,
                    role=node.title,
                    source_session_id=f"{envelope.session_id}::{member}",
                    text=text,
                    append=append,
                    node_id=node.node_id,
                    event_type="team_stream",
                    display_mode="stream",
                    collapsed_title=f"{node.title} 的执行过程",
                    turn_started_at=started_at,
                    turn_duration=max(0.0, time.time() - started_at),
                    timestamp=time.time(),
                ))

            async def _dispatch_node(node: TeamPlanNode) -> tuple[TeamPlanNode, dict[str, Any] | None, Exception | None]:
                attempt = node.attempt_count + 1
                node.metadata = {
                    **dict(node.metadata or {}),
                    "execution_started_at": time.time(),
                    "execution_attempt": attempt,
                }
                self._mark_plan_node(
                    envelope.session_id,
                    node.node_id,
                    owner_account_id=envelope.user_id,
                    status="in_progress",
                    attempt_count=attempt,
                    last_error="",
                )
                try:
                    before_artifact_ids = {
                        str(item.get("artifact_id") or "")
                        for item in dispatch_team.bus.list_artifacts(envelope.session_id)
                    }
                    delegate_cwd = self._team_delegate_cwd(
                        envelope,
                        goal,
                        node_id=node.node_id,
                        agent_id=node.assignee,
                    )
                    workspace_scope = "isolated_turn_workspace" if delegate_cwd else "shared_workspace"
                    member_cwd = delegate_cwd or self._team_shared_cwd(envelope)
                    workspace_snapshot = self._workspace_file_snapshot(delegate_cwd) if delegate_cwd else {}
                    upstream_artifact_refs = self._node_upstream_artifact_refs(plan, node)
                    upstream_artifact_refs.extend(
                        str(item.get("path") or "")
                        for item in (envelope.params.get("referenced_paths") or [])
                        if isinstance(item, dict) and str(item.get("path") or "").strip()
                    )
                    upstream_artifact_text = self._format_upstream_artifacts(upstream_artifact_refs)
                    upstream_summary = self._node_upstream_summary(plan, node)
                    workspace_guard = self._workspace_guard_config(
                        workspace_scope,
                        delegate_cwd,
                        upstream_artifact_refs,
                    )
                    instruction_detail = node.detail
                    revision_instructions = str((node.metadata or {}).get("revision_instructions") or "").strip()
                    if revision_instructions:
                        instruction_detail = (
                            f"{instruction_detail}\n\n"
                            "Leader 审阅未通过，请针对以下意见修订后重新提交：\n"
                            f"{revision_instructions}"
                        )
                    if upstream_artifact_text:
                        instruction_detail = (
                            f"{instruction_detail}\n\n"
                            "上游产物路径（优先读取这些文件作为当前节点输入）：\n"
                            f"{upstream_artifact_text}"
                        )
                    if self._is_verify_execution_node(node):
                        instruction_detail = (
                            f"{instruction_detail}\n\n"
                            "验证执行要求：先根据上游产物路径复核并必要时补充测试方案，再执行功能验证、回归检查和缺陷记录；"
                            "如果上游产物路径缺失或不可读，请明确报告阻塞。"
                        )
                    task_payload_meta = {
                        "team_goal": goal,
                        "team_member_id": node.assignee,
                        "team_plan_node_id": node.node_id,
                        "team_node_title": node.title,
                        "team_node_detail": instruction_detail,
                        "team_upstream_summary": upstream_summary,
                        "team_upstream_artifacts": upstream_artifact_refs,
                        "team_display_name": dispatch_team.display_name,
                        "external_team_role": "member",
                        "external_task_budget": "focused",
                        "team_workspace_scope": workspace_scope,
                        "external_output_contract": self._delegate_output_contract(workspace_scope),
                        "workspace_instructions": self._team_roster_summary(dispatch_team),
                    }
                    if envelope.params.get("active_skills"):
                        task_payload_meta["active_skills"] = list(
                            envelope.params.get("active_skills") or []
                        )
                    if member_cwd:
                        task_payload_meta["cwd"] = member_cwd
                    if workspace_guard:
                        task_payload_meta["workspace_guard"] = workspace_guard
                    result = await self.request_delegate(
                        envelope.session_id,
                        member=node.assignee,
                        instruction=f"{node.title}\n\n{instruction_detail}",
                        requester_member_id="leader",
                        external_team_id=external_team_id,
                        plan_node_id=node.node_id,
                        wait_for_result=True,
                        owner_account_id=envelope.user_id,
                        on_child_chunk=lambda member, chunk, current=node: _relay_child_chunk(current, member, chunk),
                        task_payload_meta=task_payload_meta,
                        finalize_plan_node=False,
                        attachments=envelope.attachments,
                    )
                    snapshot_changes = (
                        self._workspace_file_changes(delegate_cwd, workspace_snapshot)
                        if delegate_cwd
                        else []
                    )
                    result["_workspace_file_changes"] = merge_changes(
                        snapshot_changes,
                        member_file_changes.get(node.node_id, []),
                    )
                    result["_workspace_root"] = member_cwd
                    result["_workspace_changed_paths"] = [
                        str(item.get("path") or "")
                        for item in result["_workspace_file_changes"]
                        if item.get("status") != "deleted" and str(item.get("path") or "")
                    ]
                    artifacts = self._node_owned_artifacts([
                        item for item in dispatch_team.bus.list_artifacts(envelope.session_id)
                        if str(item.get("artifact_id") or "") not in before_artifact_ids
                    ], node=node, task_id=str((result or {}).get("task_id") or ""), workspace_root=member_cwd)
                    result["artifacts"] = artifacts
                    return node, result, None
                except asyncio.CancelledError as exc:
                    return node, None, exc
                except Exception as exc:  # noqa: BLE001
                    return node, None, exc

            if dispatch_nodes:
                progressed = True
                if len(dispatch_nodes) > 1:
                    names = "、".join(f"{node.title}→{node.assignee}" for node in dispatch_nodes)
                    yield ResponseChunk.status_event(envelope.request_id, f"并发派发节点：{names}")
                else:
                    node = dispatch_nodes[0]
                    yield ResponseChunk.status_event(
                        envelope.request_id,
                        f"派发节点「{node.title}」给 {node.assignee}（第 {node.attempt_count + 1} 次）…",
                    )
                for node in dispatch_nodes:
                    if not self._should_show_assignment(plan, node):
                        continue
                    yield self._recorded_team_internal_chunk(
                        envelope,
                        agent_id="leader",
                        role="leader",
                        is_leader=True,
                        source_session_id=f"{envelope.session_id}::leader",
                        text=self._assignment_text(node),
                        node_id=node.node_id,
                        event_type="team_assign",
                        mention_from="leader",
                        mention_to=[node.assignee],
                        mention_intent="assign",
                    )
                def _finish_dispatch_result(
                    node: TeamPlanNode,
                    result: dict[str, Any] | None,
                    error: Exception | None,
                ) -> list[ResponseChunk]:
                    chunks: list[ResponseChunk] = []
                    attempt = node.attempt_count
                    if error is not None:
                        started_at = float((node.metadata or {}).get("execution_started_at") or node.updated_at or time.time())
                        finished_at = time.time()
                        self._record_external_agent_profile_observation(
                            plan,
                            node,
                            owner_account_id=envelope.user_id,
                            outcome="neutral",
                            quality_weight=0.0,
                            assessment_source="execution_assessment",
                            failure_kind="cancelled" if isinstance(error, asyncio.CancelledError) else "runtime",
                        )
                        chunks.append(self._recorded_team_internal_chunk(
                            envelope,
                            agent_id=node.assignee,
                            role=node.title,
                            source_session_id=f"{envelope.session_id}::{node.assignee}",
                            text=f"@leader {node.title} 执行失败：{error}",
                            node_id=node.node_id,
                            event_type="team_submit",
                            turn_started_at=started_at,
                            turn_duration=max(0.0, finished_at - started_at),
                            timestamp=finished_at,
                            mention_from=node.assignee,
                            mention_to=["leader"],
                            mention_intent="submit",
                        ))
                        if isinstance(error, asyncio.CancelledError):
                            self._mark_plan_node(
                                envelope.session_id,
                                node.node_id,
                                owner_account_id=envelope.user_id,
                                status="cancelled",
                                result_summary="已停止当前回复",
                                attempt_count=attempt,
                                last_error="cancelled",
                            )
                            raise error
                        retryable = attempt < max_attempts
                        self._reflect_plan_node(
                            plan,
                            node,
                            owner_account_id=envelope.user_id,
                            reason=str(error),
                            decision="补充失败上下文后按原成员重试。" if retryable else "达到自动重试上限，停止重试并等待用户确认。",
                            suggested_action="" if retryable else "请确认是否补员、改派或调整任务目标。",
                            retryable=retryable,
                        )
                        if retryable:
                            self._insert_runtime_diagnostic_node(
                                plan,
                                node,
                                owner_account_id=envelope.user_id,
                                reason=str(error),
                            )
                        self._mark_plan_node(
                            envelope.session_id,
                            node.node_id,
                            owner_account_id=envelope.user_id,
                            status="failed" if attempt < max_attempts else "blocked",
                            result_summary=f"节点执行失败，Runtime 将在下一轮尝试重排或阻塞：{error}",
                            attempt_count=attempt,
                            last_error=str(error),
                        )
                        return chunks

                    task_id = str((result or {}).get("task_id") or "")
                    started_at = float((node.metadata or {}).get("execution_started_at") or node.updated_at or time.time())
                    finished_at = time.time()
                    output = str((result or {}).get("output") or "").strip()
                    artifacts = self._artifact_cards(list((result or {}).get("artifacts") or []))
                    turn_file_changes = [
                        dict(item)
                        for item in (result or {}).get("_workspace_file_changes") or []
                        if isinstance(item, dict) and str(item.get("path") or "").strip()
                    ]
                    changed_paths = {
                        str(path)
                        for path in (result or {}).get("_workspace_changed_paths") or []
                        if str(path).strip()
                    }
                    artifact_refs = [
                        str(item.get("path") or item.get("artifact_id") or "")
                        for item in artifacts
                        if str(item.get("path") or item.get("artifact_id") or "").strip()
                    ]
                    node_result = (
                        self._first_nonempty_text(
                            output,
                            str((result or {}).get("result") or "").strip(),
                            "".join(member_stream_text.get(node.node_id, [])).strip(),
                            str(node.result_summary or "").strip(),
                        )
                        or "当前节点已完成，详细过程可在看板中查看。"
                    )
                    is_review_submission = self._is_review_submission_node(node)
                    for runtime_event in member_runtime_events.get(node.node_id, [])[-8:]:
                        self._append_plan_node_event(
                            plan,
                            node,
                            owner_account_id=envelope.user_id,
                            event=runtime_event,
                        )
                    if is_review_submission and node_result:
                        auto_artifact = self._write_node_markdown_artifact(
                            envelope,
                            team=dispatch_team,
                            node=node,
                            task_id=task_id,
                            content=node_result,
                        )
                        if auto_artifact:
                            artifacts.extend(self._artifact_cards([auto_artifact]))
                            artifact_ref = str(auto_artifact.get("path") or auto_artifact.get("artifact_id") or "")
                            if artifact_ref and artifact_ref not in artifact_refs:
                                artifact_refs.append(artifact_ref)
                    elif node_result:
                        runtime_artifact_text = "\n".join(
                            str(item.get("event_text") or "")
                            for item in member_runtime_events.get(node.node_id, [])
                            if str(item.get("event_type") or "") == "tool"
                        )
                        auto_file_artifacts = self._auto_file_artifacts_from_result(
                            envelope,
                            team=dispatch_team,
                            node=node,
                            task_id=task_id,
                            text="\n".join(part for part in [node_result, runtime_artifact_text] if part),
                            existing_artifacts=artifacts,
                            changed_paths=changed_paths,
                            workspace_root=str((result or {}).get("_workspace_root") or ""),
                        )
                        if auto_file_artifacts:
                            artifacts.extend(self._artifact_cards(auto_file_artifacts))
                            for artifact in auto_file_artifacts:
                                artifact_ref = str(artifact.get("path") or artifact.get("artifact_id") or "")
                                if artifact_ref and artifact_ref not in artifact_refs:
                                    artifact_refs.append(artifact_ref)
                    result_summary = self._business_result_summary(
                        node,
                        node_result,
                        is_review_submission=is_review_submission,
                    )
                    result_contract = self._extract_result_contract(node_result)
                    result_contract["status_signal"] = (
                        self._runtime_result_status(
                            node,
                            member_runtime_events.get(node.node_id, []),
                        )
                        or "unknown"
                    )
                    assessment = self._assess_node_execution(
                        node,
                        runtime_events=member_runtime_events.get(node.node_id, []),
                        artifact_refs=artifact_refs,
                        changed_paths=changed_paths,
                        result_contract=result_contract,
                    )
                    summary_text = (
                        f"@leader {result_summary}。"
                        if assessment.execution_status == "completed"
                        else f"@leader 「{node.title}」未通过执行验收：{assessment.reason}"
                    )
                    process_text = "".join(member_stream_text.get(node.node_id, [])).strip()
                    runtime_events = member_runtime_events.get(node.node_id, [])
                    runtime_thinking = _join_stream_fragments([
                        str(item.get("event_text") or "")
                        for item in runtime_events
                        if str(item.get("event_type") or "") == "thinking"
                        and str(item.get("event_text") or "").strip()
                    ])
                    runtime_tool_calls_by_id: dict[str, dict[str, Any]] = {}
                    for item in runtime_events:
                        tool_call = item.get("tool_call")
                        if str(item.get("event_type") or "") != "tool" or not isinstance(tool_call, dict):
                            continue
                        tool_id = str(tool_call.get("id") or f"tool_{len(runtime_tool_calls_by_id)}")
                        previous = runtime_tool_calls_by_id.get(tool_id, {})
                        runtime_tool_calls_by_id[tool_id] = {
                            **previous,
                            **tool_call,
                            "arguments": tool_call.get("arguments") or previous.get("arguments") or {},
                            "result": tool_call.get("result") or previous.get("result") or "",
                        }
                    runtime_tool_calls = list(runtime_tool_calls_by_id.values())
                    chunks.append(self._recorded_team_internal_chunk(
                        envelope,
                        agent_id=node.assignee,
                        role=node.title,
                        source_session_id=f"{envelope.session_id}::{node.assignee}",
                        text=summary_text,
                        node_id=node.node_id,
                        event_type="team_submit",
                        process_text=process_text,
                        artifacts=artifacts,
                        turn_file_changes=turn_file_changes,
                        thinking=runtime_thinking,
                        tool_calls=runtime_tool_calls,
                        turn_started_at=started_at,
                        turn_duration=max(0.0, finished_at - started_at),
                        timestamp=finished_at,
                        mention_from=node.assignee,
                        mention_to=["leader"],
                        mention_intent="submit" if is_review_submission else "handoff",
                    ))
                    if assessment.execution_status != "completed":
                        ack_text = f"@{node.assignee} 「{node.title}」未通过执行验收，已标记为{assessment.execution_status}。"
                    elif is_review_submission:
                        ack_text = f"@{node.assignee} 已收到「{node.title}」方案，将进入 Leader 审阅。"
                    elif self._result_requires_user_input(result_summary, result_contract):
                        ack_text = f"@{node.assignee} 已收到，我会结合团队状态判断是否需要向用户补充信息。"
                    else:
                        ack_text = f"@{node.assignee} 已收到「{node.title}」提交，我会结合计划状态推进下一步。"
                    chunks.append(self._recorded_team_internal_chunk(
                        envelope,
                        agent_id="leader",
                        role="leader",
                        is_leader=True,
                        source_session_id=f"{envelope.session_id}::leader",
                        text=ack_text,
                        node_id=node.node_id,
                        event_type="team_ack",
                        mention_from="leader",
                        mention_to=[node.assignee],
                        mention_intent="ack",
                    ))
                    node_meta = dict(node.metadata or {})
                    full_result_ref, full_result_bytes = self._persist_node_full_result(
                        envelope,
                        node,
                        node_result,
                    )
                    if full_result_ref:
                        node_meta["full_result_ref"] = full_result_ref
                        node_meta["full_result_bytes"] = full_result_bytes
                    node_meta["result_contract"] = result_contract
                    node_meta["execution_assessment"] = assessment.to_dict()
                    node.metadata = node_meta
                    if assessment.execution_status != "completed":
                        outcome, quality_weight, failure_kind = self._profile_outcome_from_execution(assessment)
                        self._record_external_agent_profile_observation(
                            plan,
                            node,
                            owner_account_id=envelope.user_id,
                            outcome=outcome,
                            quality_weight=quality_weight,
                            assessment_source="execution_assessment",
                            failure_kind=failure_kind,
                            source_attempt_id=task_id,
                        )
                        retryable = assessment.execution_status == "failed" and attempt < max_attempts
                        self._reflect_plan_node(
                            plan,
                            node,
                            owner_account_id=envelope.user_id,
                            reason=assessment.reason,
                            decision="保留结构化失败证据并按原成员重试。" if retryable else "阻止下游节点继续执行，等待 Leader/用户处理。",
                            suggested_action="" if retryable else "请检查权限、输入产物或调整任务后再继续。",
                            retryable=retryable,
                        )
                        if retryable:
                            self._insert_runtime_diagnostic_node(
                                plan,
                                node,
                                owner_account_id=envelope.user_id,
                                reason=assessment.reason,
                            )
                        failure_summary = f"节点未通过执行验收：{assessment.reason}"
                        self._mark_plan_node(
                            envelope.session_id,
                            node.node_id,
                            owner_account_id=envelope.user_id,
                            status=assessment.execution_status,
                            result_summary=failure_summary,
                            artifact_refs=artifact_refs,
                            delegate_task_id=task_id,
                            attempt_count=attempt,
                            last_error=assessment.reason,
                        )
                        return chunks
                    review_reason = ""
                    if self._node_contract_requires_leader_review(node):
                        review_reason = "节点契约要求 Leader review"
                    elif self._result_needs_leader_review(result_summary, result_contract):
                        review_reason = "成员提交需要 Leader 确认或补充信息"
                    elif self._has_open_member_question(dispatch_team, task_id):
                        review_reason = "成员通过 Team Bus 向 Leader 提出待确认问题"
                    if review_reason:
                        self._insert_leader_review_node(
                            plan,
                            node,
                            owner_account_id=envelope.user_id,
                            reason=review_reason,
                        )
                    else:
                        outcome, quality_weight, failure_kind = self._profile_outcome_from_execution(assessment)
                        self._record_external_agent_profile_observation(
                            plan,
                            node,
                            owner_account_id=envelope.user_id,
                            outcome=outcome,
                            quality_weight=quality_weight,
                            assessment_source="execution_assessment",
                            failure_kind=failure_kind,
                            source_attempt_id=task_id,
                        )
                    self._mark_plan_node(
                        envelope.session_id,
                        node.node_id,
                        owner_account_id=envelope.user_id,
                        status="completed",
                        result_summary=result_summary,
                        artifact_refs=artifact_refs,
                        delegate_task_id=task_id,
                        attempt_count=attempt,
                    )
                    return chunks

                pending_tasks = [asyncio.create_task(_dispatch_node(node)) for node in dispatch_nodes]
                for task in pending_tasks:
                    self._track_delegate_task(envelope.session_id, envelope.user_id, task)
                try:
                    while pending_tasks:
                        queue_task = asyncio.create_task(live_queue.get())
                        done, _ = await asyncio.wait(
                            [*pending_tasks, queue_task],
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if queue_task in done:
                            yield queue_task.result()
                        else:
                            queue_task.cancel()
                            await asyncio.gather(queue_task, return_exceptions=True)
                        for task in done:
                            if task is queue_task:
                                continue
                            while not live_queue.empty():
                                yield live_queue.get_nowait()
                            for chunk in _finish_dispatch_result(*task.result()):
                                yield chunk
                        pending_tasks = [task for task in pending_tasks if not task.done()]
                finally:
                    for task in pending_tasks:
                        if not task.done():
                            task.cancel()
                    if pending_tasks:
                        await asyncio.gather(*pending_tasks, return_exceptions=True)
                while not live_queue.empty():
                    yield live_queue.get_nowait()

            for node in list(plan.nodes.values()):
                if node.status == "failed" and node.attempt_count >= max_attempts:
                    if self._runtime_staffing_trigger(
                        team,
                        node,
                        owner_account_id=envelope.user_id,
                        max_attempts=max_attempts,
                    ) is not None:
                        continue
                    self._reflect_plan_node(
                        plan,
                        node,
                        owner_account_id=envelope.user_id,
                        reason=f"节点连续失败 {node.attempt_count} 次",
                        decision="停止自动重试，保留当前团队并等待用户确认下一步。",
                        suggested_action="可选择补员、改派、缩小任务范围或手动重试。",
                    )
                    self._mark_plan_node(
                        envelope.session_id,
                        node.node_id,
                        owner_account_id=envelope.user_id,
                        status="blocked",
                        result_summary=f"节点连续失败 {node.attempt_count} 次，已停止重试，等待 Leader/用户介入。",
                    )
                    progressed = True

            if plan.nodes and all(node.status == "completed" for node in plan.nodes.values()):
                plan.status = "completed"
                break
            if any(node.status == "needs_info" for node in plan.nodes.values()):
                break
            if not progressed:
                for node in plan.nodes.values():
                    if node.status == "pending":
                        self._reflect_plan_node(
                            plan,
                            node,
                            owner_account_id=envelope.user_id,
                            reason="依赖未满足或无可执行进展",
                            decision="防止工作流空转，暂停节点并等待用户确认。",
                            suggested_action="请确认是否补充信息、调整依赖或改派成员。",
                        )
                        self._mark_plan_node(
                            envelope.session_id,
                            node.node_id,
                            owner_account_id=envelope.user_id,
                            status="blocked",
                            result_summary="依赖未满足或无可执行进展，防止工作流空转。",
                        )
                break

        yield ResponseChunk.final(envelope.request_id, self._format_workflow_result(plan))

    def _build_team(
        self,
        session_id: str,
        *,
        external_team_id: str = "",
        owner_account_id: str = "",
        runtime_members: list[TeamMemberSpec] | None = None,
        existing_session: TeamSession | None = None,
        existing_bus: TeamBus | None = None,
    ) -> Team:
        team_cfg = self.config.team_config or {}
        external_team_id = str(external_team_id or team_cfg.get("external_team_id") or "").strip()
        display_name = str(team_cfg.get("name") or "团队").strip() or "团队"
        leader_spec: TeamMemberSpec | None = None
        model_bindings: dict[str, Any] = {}
        if external_team_id and self.external_store is not None:
            try:
                getter = getattr(self.session_store, "get_agent_config", None)
                stored_config = (
                    getter(_visible_session_id(session_id), owner_account_id=owner_account_id)
                    if callable(getter)
                    else None
                )
                stored_team = (
                    stored_config.get("team")
                    if isinstance(stored_config, dict) and isinstance(stored_config.get("team"), dict)
                    else {}
                )
                if str(stored_team.get("external_team_id") or "").strip() == external_team_id:
                    materialized, _ = materialize_team_member_model_bindings(
                        self.session_store,
                        self.external_store,
                        session_id,
                        owner_account_id=owner_account_id,
                        builtin_model_id=self.config.owner_default_model_id(owner_account_id),
                    )
                    materialized_team = (
                        materialized.get("team")
                        if isinstance(materialized.get("team"), dict)
                        else {}
                    )
                    model_bindings = (
                        materialized_team.get("member_model_bindings")
                        if isinstance(materialized_team.get("member_model_bindings"), dict)
                        else {}
                    )
                external_team = self.external_store.get_team(
                    external_team_id,
                    owner_account_id=owner_account_id,
                )
                display_name = str(external_team.get("name") or display_name).strip() or display_name
                leader_agent_id = str(external_team.get("leader_agent_id") or "").strip()
                if leader_agent_id and not is_crew_builtin_agent(leader_agent_id):
                    members, leader_spec = self._external_team_specs(
                        external_team_id,
                        owner_account_id=owner_account_id,
                        model_bindings=model_bindings,
                    )
                else:
                    members, leader_spec = self._external_team_specs(
                        external_team_id,
                        owner_account_id=owner_account_id,
                        model_bindings=model_bindings,
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning("读取外部团队失败 external_team_id=%s err=%s", external_team_id, exc)
                raise ToolError(f"读取外部团队失败：{external_team_id}") from exc
        else:
            members = self._members("", owner_account_id=owner_account_id)

        runtime_member_map = {
            member.member_id: member
            for member in (runtime_members or [])
            if member.member_id and member.member_id != "leader"
        }
        if runtime_member_map:
            members = [
                *[member for member in members if member.member_id not in runtime_member_map],
                *runtime_member_map.values(),
            ]

        member_map = {m.member_id: m for m in members}
        leader_member_id = str(team_cfg.get("leader") or team_cfg.get("leader_member_id") or "leader").strip()
        if leader_spec is not None:
            leader_member_id = "leader"
        elif leader_member_id == "leader" and "leader" not in member_map:
            leader_spec = TeamMemberSpec(
                member_id="leader",
                name="leader",
                role="负责拆解、派活、跟踪任务、汇总最终结果",
                executor="builtin",
            )
        else:
            leader_spec = member_map.get(leader_member_id) or next(iter(member_map.values()))
            leader_member_id = leader_spec.member_id
            members = [m for m in members if m.member_id != leader_member_id]

        team_session = existing_session or TeamSession(
            team_session_id=session_id,
            leader_member_id=leader_member_id,
        )
        team_session.leader_member_id = leader_member_id
        bus = existing_bus or TeamBus()
        member_map = {m.member_id: m for m in members}
        all_member_ids = list(dict.fromkeys(["leader", *member_map.keys()]))

        async def _on_team_mention(event: dict[str, Any]) -> dict[str, Any]:
            return await self._handle_team_mention(
                session_id,
                owner_account_id,
                event,
                teammates=teammates,
                bus=bus,
                on_task_created=_mark_plan_from_delegate_task,
                on_task_finished=_finish_plan_from_delegate_task,
            )

        # 1) 队友：共享基础工具
        teammates: dict[str, Agent] = {}
        for m in members:
            member_session = team_session.ensure_member(m)
            registry = self._clone_registry()
            registry.unregister("ask_followup_question")
            register_team_bus_tools(
                registry,
                bus,
                team_session_id=session_id,
                member_id=m.member_id,
                member_ids=all_member_ids,
            )
            register_team_mention_tool(
                registry,
                bus=bus,
                session_id=session_id,
                member_id=m.member_id,
                member_names=list(member_map.keys()),
                allow_user=False,
                on_mention=_on_team_mention,
            )
            teammates[m.member_id] = self._new_agent(
                registry,
                m.system_prompt or teammate_prompt(m.name, m.role),
                spec=m,
                member_session_id=member_session.member_session_id,
                team_session_id=session_id,
                owner_account_id=owner_account_id,
            )

        # 2) Leader 专属注册表 = 基础工具 + delegate
        leader_registry = self._clone_registry()
        leader_session = team_session.ensure_member(leader_spec)
        register_team_bus_tools(
            leader_registry,
            bus,
            team_session_id=session_id,
            member_id="leader",
            member_ids=all_member_ids,
        )
        register_team_mention_tool(
            leader_registry,
            bus=bus,
            session_id=session_id,
            member_id="leader",
            member_names=list(member_map.keys()),
            on_mention=_on_team_mention,
        )
        direct_leader_registry = self._clone_registry()
        register_team_bus_tools(
            direct_leader_registry,
            bus,
            team_session_id=session_id,
            member_id="leader",
            member_ids=all_member_ids,
        )
        register_team_mention_tool(
            direct_leader_registry,
            bus=bus,
            session_id=session_id,
            member_id="leader",
            member_names=list(member_map.keys()),
            on_mention=_on_team_mention,
        )

        def _mark_plan_from_delegate_task(task: dict[str, Any]) -> None:
            plan_node_id = str(task.get("plan_node_id") or "").strip()
            if plan_node_id:
                self._mark_plan_node(
                    session_id,
                    plan_node_id,
                    owner_account_id=owner_account_id,
                    status="in_progress",
                    delegate_task_id=str(task.get("id") or ""),
                )

        def _finish_plan_from_delegate_task(task: dict[str, Any]) -> None:
            plan_node_id = str(task.get("plan_node_id") or "").strip()
            if plan_node_id:
                self._mark_plan_node(
                    session_id,
                    plan_node_id,
                    owner_account_id=owner_account_id,
                    status=str(task.get("status") or "completed"),
                    result_summary=str(task.get("result") or ""),
                    delegate_task_id=str(task.get("id") or ""),
                )

        def _guard_delegate_from_plan(task: dict[str, Any]) -> None:
            self._guard_delegate_against_plan(
                session_id,
                owner_account_id=owner_account_id,
                member=str(task.get("member") or ""),
                plan_node_id=str(task.get("plan_node_id") or ""),
            )

        def _apply_plan_change_from_leader(change: dict[str, Any]) -> dict[str, Any]:
            return self._apply_leader_plan_change(
                session_id,
                owner_account_id=owner_account_id,
                change=change,
                valid_member_ids=set(teammates),
                member_specs=member_map,
            )

        async def _execute_legacy_delegate_from_plan(task: dict[str, Any]) -> str:
            result = await self._execute_team_plan_assignment(
                session_id,
                owner_account_id=owner_account_id,
                member=str(task.get("member") or ""),
                instruction=str(task.get("instruction") or ""),
                plan_node_id=str(task.get("plan_node_id") or ""),
                teammates=teammates,
                bus=bus,
                on_task_created=_mark_plan_from_delegate_task,
                on_task_finished=_finish_plan_from_delegate_task,
                require_plan=False,
                source="legacy_delegate",
            )
            return str(result.get("result") or "")

        register_plan_change_tool(
            leader_registry,
            member_names=list(teammates.keys()),
            on_plan_change=_apply_plan_change_from_leader,
        )

        register_delegate_tool(
            leader_registry,
            teammates,
            self.tasks,
            session_id,
            bus=bus,
            before_delegate=_guard_delegate_from_plan,
            execute_delegate=_execute_legacy_delegate_from_plan,
            on_child_start=self._mark_child_active,
            on_child_done=self._mark_child_done,
            on_task_created=_mark_plan_from_delegate_task,
            on_task_finished=_finish_plan_from_delegate_task,
        )
        leader_visible_tools = [
            name for name in leader_registry.names()
            if name != "delegate_to_teammate"
        ]

        leader = self._new_agent(
            leader_registry,
            leader_spec.system_prompt or leader_prompt([m.to_dict() for m in members]),
            spec=leader_spec,
            member_session_id=leader_session.member_session_id,
            team_session_id=session_id,
            owner_account_id=owner_account_id,
            tool_filter=leader_visible_tools,
        )
        direct_leader = self._new_agent(
            direct_leader_registry,
            leader_spec.system_prompt or leader_prompt([m.to_dict() for m in members]),
            spec=leader_spec,
            member_session_id=leader_session.member_session_id,
            team_session_id=session_id,
            owner_account_id=owner_account_id,
        )
        log.info(
            "[Team] 已组建团队 session=%s leader=%s 成员=%s",
            session_id,
            leader_member_id,
            list(teammates),
        )
        return Team(
            leader=leader,
            direct_leader=direct_leader,
            teammates=teammates,
            session=team_session,
            display_name=display_name,
            leader_spec=leader_spec,
            members=member_map,
            bus=bus,
            external_team_id=external_team_id,
            runtime_members=runtime_member_map,
        )

    def _get_or_create(self, session_id: str, *, external_team_id: str = "", owner_account_id: str = "") -> Team:
        key = self._key(session_id, owner_account_id)
        if key not in self._teams:
            self._teams[key] = self._build_team(
                session_id,
                external_team_id=external_team_id,
                owner_account_id=owner_account_id,
            )
        return self._teams[key]

    async def external_team_mention(
        self,
        session_id: str,
        *,
        member_id: str,
        to: list[str],
        intent: str,
        content: str,
        node_id: str = "",
        result_status: str = "",
        artifacts: list[str] | None = None,
        questions: list[dict[str, Any]] | None = None,
        title: str = "",
        task_payload_meta: dict[str, Any] | None = None,
        owner_account_id: str = "",
    ) -> dict[str, Any]:
        """受信任 Binding 调用的 Team mention 入口。

        member_id 由 Gateway 的 ExternalInteractionBinding 提供，不接收外部
        Runtime 自报身份。TeamManager 继续作为 mention 语义的唯一实现。
        """

        team = self._get_or_create(session_id, owner_account_id=owner_account_id)
        event = {
            "from": member_id,
            "to": list(to or []),
            "intent": str(intent or "broadcast"),
            "content": str(content or ""),
            "node_id": str(node_id or ""),
            "result_status": str(result_status or ""),
            "artifacts": list(artifacts or []),
            "questions": list(questions or []),
            "title": str(title or ""),
            "task_payload_meta": dict(task_payload_meta or {}),
        }
        return await self._handle_team_mention(
            session_id,
            owner_account_id,
            event,
            teammates=team.teammates,
            bus=team.bus,
        )

    async def request_delegate(
        self,
        session_id: str,
        *,
        member: str,
        instruction: str,
        requester_member_id: str = "mcp",
        external_team_id: str = "",
        plan_node_id: str = "",
        wait_for_result: bool = False,
        owner_account_id: str = "",
        on_child_chunk: Callable[[str, ResponseChunk], None] | None = None,
        task_payload_meta: dict[str, Any] | None = None,
        finalize_plan_node: bool = True,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """受控派活入口：供 MCP/Gateway 等外部控制面调用。

        外部 agent 不直接调用 delegate_to_teammate；它们只能通过这个方法请求
        Crew Team Runtime 派活。真正执行仍在 Crew 内部完成，并写入 TaskManager
        与 Team Bus。
        """
        team = self._get_or_create(session_id, external_team_id=external_team_id, owner_account_id=owner_account_id)
        if plan_node_id:
            self.update_plan_node(
                session_id,
                node_id=plan_node_id,
                status="in_progress",
                owner_account_id=owner_account_id,
            )
        created_fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()

        def _on_task_created(task: dict[str, Any]) -> None:
            if not created_fut.done():
                created_fut.set_result(task)
            if plan_node_id:
                self._mark_plan_node(
                    session_id,
                    plan_node_id,
                    owner_account_id=owner_account_id,
                    status="in_progress",
                    delegate_task_id=str(task.get("id") or ""),
                )

        def _on_task_finished(task: dict[str, Any]) -> None:
            node_id = str(task.get("plan_node_id") or plan_node_id or "").strip()
            if node_id:
                updates: dict[str, Any] = {
                    "delegate_task_id": str(task.get("id") or ""),
                }
                if finalize_plan_node:
                    updates.update({
                        "status": str(task.get("status") or "completed"),
                        "result_summary": str(task.get("result") or ""),
                    })
                self._mark_plan_node(
                    session_id,
                    node_id,
                    owner_account_id=owner_account_id,
                    **updates,
                )

        async def _run_background() -> None:
            try:
                await run_delegate_to_teammate(
                    team.teammates,
                    self.tasks,
                    session_id,
                    member=member,
                    instruction=instruction,
                    requester_member_id=requester_member_id or "mcp",
                    plan_node_id=plan_node_id,
                    bus=team.bus,
                    on_child_start=self._mark_child_active,
                    on_child_done=self._mark_child_done,
                    on_child_chunk=on_child_chunk,
                    on_task_created=_on_task_created,
                    on_task_finished=_on_task_finished,
                    owner_account_id=owner_account_id,
                    task_payload_meta=task_payload_meta,
                    attachments=attachments,
                )
            except asyncio.CancelledError:
                if plan_node_id:
                    self._mark_plan_node(
                        session_id,
                        plan_node_id,
                        owner_account_id=owner_account_id,
                        status="cancelled",
                        result_summary="已停止当前回复",
                    )
                raise
            except Exception as exc:  # noqa: BLE001
                if not created_fut.done():
                    created_fut.set_exception(exc)
                if plan_node_id:
                    self._mark_plan_node(
                        session_id,
                        plan_node_id,
                        owner_account_id=owner_account_id,
                        status="failed",
                        result_summary=str(exc),
                    )
                log.warning(
                    "[Team] 后台派活失败 session=%s member=%s node=%s err=%s",
                    session_id,
                    member,
                    plan_node_id,
                    exc,
                )

        if wait_for_result:
            try:
                output = await run_delegate_to_teammate(
                    team.teammates,
                    self.tasks,
                    session_id,
                    member=member,
                    instruction=instruction,
                    requester_member_id=requester_member_id or "mcp",
                    plan_node_id=plan_node_id,
                    bus=team.bus,
                    on_child_start=self._mark_child_active,
                    on_child_done=self._mark_child_done,
                    on_child_chunk=on_child_chunk,
                    on_task_created=_on_task_created,
                    on_task_finished=_on_task_finished,
                    owner_account_id=owner_account_id,
                    task_payload_meta=task_payload_meta,
                    attachments=attachments,
                )
            except asyncio.CancelledError:
                if plan_node_id:
                    self._mark_plan_node(
                        session_id,
                        plan_node_id,
                        owner_account_id=owner_account_id,
                        status="cancelled",
                        result_summary="已停止当前回复",
                    )
                raise
            except Exception as exc:
                if plan_node_id:
                    self._mark_plan_node(
                        session_id,
                        plan_node_id,
                        owner_account_id=owner_account_id,
                        status="failed",
                        result_summary=str(exc),
                    )
                raise
            return {
                "ok": True,
                "session_id": session_id,
                "member": member,
                "plan_node_id": plan_node_id,
                "status": "completed",
                "task_id": str((created_fut.result() if created_fut.done() else {}).get("id") or ""),
                "output": output,
            }

        bg_task = asyncio.create_task(_run_background())
        self._track_delegate_task(session_id, owner_account_id, bg_task)
        try:
            task = await asyncio.wait_for(created_fut, timeout=2.0)
        except Exception:
            if not bg_task.done():
                bg_task.cancel()
            raise
        return {
            "ok": True,
            "session_id": session_id,
            "member": member,
            "plan_node_id": plan_node_id,
            "status": "in_progress",
            "task_id": str(task.get("id") or ""),
            "message": "delegate task accepted; poll team_plan_read or task board for completion",
        }

    async def interact(self, envelope: Envelope) -> AsyncIterator[ResponseChunk]:
        external_team_id = str(envelope.params.get("external_team_id") or "").strip()
        try:
            team = self._get_or_create(
                envelope.session_id,
                external_team_id=external_team_id,
                owner_account_id=envelope.user_id,
            )
        except ToolError as exc:
            yield ResponseChunk.error(envelope.request_id, str(exc))
            return
        status_chunks = await self._try_team_status_query(envelope, team=team)
        if status_chunks is not None:
            for chunk in status_chunks:
                yield chunk
            return
        team_cfg = self.config.team_config or {}
        required_workflow = bool(team_cfg.get("required_workflow", True))
        raw_team_spec = envelope.params.get("team_spec")
        team_spec = raw_team_spec if isinstance(raw_team_spec, dict) else None
        explicit_profile = envelope.params.get("team_execution_profile")
        route_team_spec = team_spec
        if route_team_spec is None and isinstance(explicit_profile, dict):
            route_team_spec = {"goal": str(envelope.query or ""), "execution_profile": explicit_profile}
        base_turn_decision = self.turn_router.route(
            str(envelope.query or ""),
            team_spec=route_team_spec,
        )
        intent_spec = (
            base_turn_decision.diagnostics.get("team_spec")
            if isinstance(base_turn_decision.diagnostics.get("team_spec"), dict)
            else {}
        )
        if team_spec is None and intent_spec:
            team_spec = intent_spec
        intent_profile = intent_spec.get("task_profile") if isinstance(intent_spec.get("task_profile"), dict) else {}
        explicit_mode = (
            str(explicit_profile.get("requested_mode") or "").strip().lower()
            if isinstance(explicit_profile, dict)
            else ""
        )
        turn_decision = self._turn_decision_for_execution_profile(
            base_turn_decision,
            explicit_mode=explicit_mode,
        )
        direct_leader = turn_decision.is_direct_chat
        execution_profile = self._execution_profile_for_turn(
            envelope,
            intent_profile=intent_profile,
            turn_decision=turn_decision,
        )
        log.info(
            "[Team] turn_decision session=%s turn_kind=%s execution_mode=%s source=%s reason=%s task_kind=%s complexity=%s required_workflow=%s query=%r",
            envelope.session_id,
            turn_decision.turn_kind,
            turn_decision.execution_mode,
            turn_decision.diagnostics.get("source"),
            turn_decision.reason,
            intent_profile.get("intent"),
            intent_profile.get("complexity"),
            required_workflow,
            str(envelope.query or "")[:80],
        )
        if required_workflow and team.teammates and turn_decision.is_new_workflow:
            async for chunk in self._run_required_workflow(
                envelope,
                team=team,
                external_team_id=external_team_id,
                execution_profile=execution_profile,
                team_spec=team_spec,
            ):
                yield chunk
            return
        # Leader 使用独立的会话历史（区别于单 Agent 模式）
        leader_env = Envelope(
            session_id=f"{envelope.session_id}::leader",
            params={
                **envelope.params,
                "task_session_id": envelope.session_id,
                "team_session_id": envelope.session_id,
                "member_session_id": f"{envelope.session_id}::leader",
                "agent_id": "leader",
                "team_display_name": team.display_name,
            },
            request_id=envelope.request_id,
            channel=envelope.channel,
            user_id=envelope.user_id,
            workspace_id=envelope.workspace_id,
            mode="agent",
            attachments=[
                dict(attachment)
                for attachment in (envelope.attachments or [])
                if isinstance(attachment, dict)
            ],
        )
        if direct_leader:
            roster_summary = self._team_roster_summary(team)
            context_summary = self._team_context_summary(envelope.session_id, owner_account_id=envelope.user_id)
            direct_context = "\n\n".join(part for part in [roster_summary, context_summary] if part).strip()
            if direct_context:
                existing_instructions = str(leader_env.params.get("workspace_instructions") or "").strip()
                leader_env.params["workspace_instructions"] = (
                    f"{existing_instructions}\n\n{direct_context}".strip()
                    if existing_instructions else direct_context
                )
            yield ResponseChunk.status_event(envelope.request_id, "简单消息由 Leader 直接回复…")
        else:
            yield ResponseChunk.status_event(envelope.request_id, "Leader 正在分析并拆解任务…")
        target_leader = team.direct_leader if direct_leader else team.leader
        if direct_leader:
            direct_text_parts: list[str] = []
            direct_thinking_parts: list[str] = []
            direct_tool_calls: dict[str, dict[str, Any]] = {}
            direct_source_session_id = f"{envelope.session_id}::turn::{envelope.request_id}::leader"
            direct_node_id = f"direct_leader_{envelope.request_id}"
            async for chunk in target_leader.run(leader_env):
                if chunk.kind == "delta":
                    text = str(chunk.body.get("text") or "")
                    if text:
                        direct_text_parts.append(text)
                        yield self._team_internal_chunk(
                            envelope.request_id,
                            agent_id="leader",
                            role="leader",
                            is_leader=True,
                            source_session_id=direct_source_session_id,
                            text=text,
                            node_id=direct_node_id,
                            event_type="team_stream",
                            display_mode="stream",
                            collapsed_title="Leader 的回复过程",
                            append=True,
                        )
                    continue
                if chunk.kind == "thinking":
                    text = str(chunk.body.get("text") or "")
                    if text.strip():
                        direct_thinking_parts.append(text)
                        yield self._team_internal_chunk(
                            envelope.request_id,
                            agent_id="leader",
                            role="leader",
                            is_leader=True,
                            source_session_id=direct_source_session_id,
                            text="",
                            node_id=direct_node_id,
                            event_type="team_stream",
                            display_mode="stream",
                            collapsed_title="Leader 的回复过程",
                            thinking=text,
                            append=True,
                        )
                    continue
                if chunk.kind == "tool":
                    body = chunk.body
                    tool_call_id = str(body.get("tool_call_id") or chunk.sequence or len(direct_tool_calls))
                    existing = direct_tool_calls.get(tool_call_id, {})
                    phase = str(body.get("phase") or "")
                    direct_tool_calls[tool_call_id] = {
                        **existing,
                        "id": tool_call_id,
                        "name": str(body.get("name") or existing.get("name") or "unknown"),
                        "ui_label": str(body.get("ui_label") or existing.get("ui_label") or body.get("name") or "工具调用"),
                        "arguments": body.get("arguments") or body.get("args") or existing.get("arguments") or {},
                        "result": body.get("result") or body.get("detail") or existing.get("result") or "",
                        "status": "running" if phase == "start" else ("error" if phase == "error" else "done"),
                    }
                    yield self._team_internal_chunk(
                        envelope.request_id,
                        agent_id="leader",
                        role="leader",
                        is_leader=True,
                        source_session_id=direct_source_session_id,
                        text="",
                        node_id=direct_node_id,
                        event_type="team_stream",
                        display_mode="stream",
                        collapsed_title="Leader 的回复过程",
                        tool_calls=[direct_tool_calls[tool_call_id]],
                        append=True,
                    )
                    continue
                if chunk.kind == "final":
                    final_text = str(chunk.body.get("text") or "")
                    process_text = "".join(direct_text_parts).strip()
                    text = final_text or process_text
                    folded_process_text = process_text if process_text and process_text != text else ""
                    if text:
                        yield self._recorded_team_internal_chunk(
                            envelope,
                            agent_id="leader",
                            role="leader",
                            is_leader=True,
                            source_session_id=direct_source_session_id,
                            text=text,
                            node_id=direct_node_id,
                            event_type="team_summary",
                            display_mode="chat",
                            collapsed_title="Leader 的回复过程",
                            process_text=folded_process_text,
                            thinking=_join_stream_fragments(direct_thinking_parts),
                            tool_calls=list(direct_tool_calls.values()),
                        )
                    yield chunk
                    continue
                yield chunk
            return
        async for chunk in target_leader.run(leader_env):
            yield chunk

    async def destroy(self, session_id: str, owner_account_id: str = "") -> None:
        self.interrupt(session_id, "团队已销毁", owner_account_id=owner_account_id)
        key = self._existing_team_key(session_id, owner_account_id)
        self._teams.pop(key, None)
        self._plans.pop(self._existing_plan_key(session_id, owner_account_id), None)
        with self._active_lock:
            self._active_children.pop(self._key(session_id, owner_account_id), None)
        log.info("[Team] 已销毁团队 session=%s", session_id)

    def drop_session_team(self, session_id: str, owner_account_id: str = "") -> bool:
        """Evict cached Team runtimes while preserving persisted plan/history.

        In-flight turns retain their local Team/Agent references, so eviction
        only changes the model snapshot used by later turns.
        """

        visible_session_id = _visible_session_id(session_id)
        prefix = f"{visible_session_id}::turn::"
        keys = [
            key
            for key in self._teams
            if key[0] == str(owner_account_id or "")
            and (key[1] == visible_session_id or key[1].startswith(prefix))
        ]
        for key in keys:
            self._teams.pop(key, None)
        return bool(keys)

    def clear(self) -> None:
        self._teams.clear()
        self._plans.clear()
        with self._active_lock:
            delegate_tasks = {
                task
                for tasks in self._delegate_tasks.values()
                for task in tasks
            }
            self._delegate_tasks.clear()
            self._active_children.clear()
        for task in delegate_tasks:
            if not task.done():
                task.cancel()

    def drop_owner_teams(self, owner_account_id: str) -> int:
        """只淘汰一个 owner 的 Team Agent 缓存，不中断正在执行的任务。

        owner 修改默认模型后，下一轮 Team 会用新 Provider 重建 Leader；当前轮仍持有
        旧 Team 引用并可自然完成。计划与看板事实保留，不会因切模型而丢失。
        """
        owner = str(owner_account_id or "")
        keys = [key for key in self._teams if key[0] == owner]
        for key in keys:
            self._teams.pop(key, None)
        return len(keys)

    def active_tasks_snapshot(self) -> set[asyncio.Task[Any]]:
        """返回可能仍持有 Team Provider 的后台委派任务快照。"""
        with self._active_lock:
            return {
                task
                for tasks in self._delegate_tasks.values()
                for task in tasks
                if not task.done()
            }

    async def _cancel_and_wait(
        self,
        tasks: set[asyncio.Task[Any]],
        reason: str,
        *,
        timeout: float = 5.0,
    ) -> int:
        """Cancel 一组 task 并有界等待收敛。

        成员委派被 cancel 后，CancelledError 不被执行协程的 ``except Exception``
        捕获（CancelledError 自 3.8 起隶属 BaseException），task 直接转 cancelled
        态。这里有界 wait 是为了等它真正停下、回收资源，同时防止异常 task 把
        logout/shutdown 卡死。
        """
        live = {t for t in tasks if not t.done()}
        if not live:
            return 0
        for task in live:
            task.cancel()
        try:
            await asyncio.wait(live, timeout=timeout)
        except Exception:  # noqa: BLE001 - 等待被打断也不能阻塞调用方
            log.warning("[Team] cancel_and_wait 等待被中断 reason=%s", reason)
        return len(live)

    async def cancel_owner(
        self,
        owner_account_id: str,
        reason: str = "已停止：账号退出登录",
    ) -> int:
        """取消一个 Owner 的全部 Team 交互与成员委派，并清空其缓存。

        logout 经 LogoutCoordinator 调用本方法（鸭子类型调用，不进 core ABC——当前
        TeamManager 仅有 InProcessTeamManager 一个实现）。步骤：
          1) 枚举该 owner 名下所有 session 和委派 task，并先拍 task 快照；
          2) 对每个 session 复用 interrupt 中断 leader/direct leader、plan 和成员；
          3) destroy 语义清掉 _teams/_plans（interrupt 不清这两个），并兜底清 _active_children
             里没有对应 team 的孤儿条目；
          4) cancel+wait 第一步拍下的全部成员委派，避免 done callback 提前移除后漏等。

        返回被中断的 session 数与被取消的后台委派数之和；其他 owner 的 Team 不受影响。
        """
        owner = str(owner_account_id or "").strip()
        if not owner:
            return 0
        # 1) 枚举该 owner 的 session，并在 interrupt 前拍下 task 强引用。
        sessions: set[str] = set()
        for key in list(self._teams):
            if key[0] == owner:
                sessions.add(key[1])
        for key in list(self._plans):
            if key[0] == owner:
                sessions.add(key[1])
        with self._active_lock:
            for key in list(self._active_children):
                if key[0] == owner:
                    sessions.add(key[1])
            owner_tasks = {
                task
                for key, tasks in self._delegate_tasks.items()
                if key[0] == owner
                for task in tasks
            }
            sessions.update(
                key[1]
                for key in self._delegate_tasks
                if key[0] == owner
            )
        # 2) 复用完整 interrupt 中断 leader/direct leader、plan、协程和活跃子 agent。
        interrupted = 0
        for sid in sessions:
            if self.interrupt(sid, reason, owner_account_id=owner):
                interrupted += 1
        # interrupt 是同步快速取消；登出边界还要有界等待第一步拍下的 task 真正收敛。
        cancelled_tasks = await self._cancel_and_wait(owner_tasks, reason)
        # 3) destroy 语义清缓存 + 兜底清 _active_children 残留
        for key in [k for k in list(self._teams) if k[0] == owner]:
            self._teams.pop(key, None)
        for key in [k for k in list(self._plans) if k[0] == owner]:
            self._plans.pop(key, None)
        with self._active_lock:
            for key in [k for k in list(self._active_children) if k[0] == owner]:
                self._active_children.pop(key, None)
        return interrupted + cancelled_tasks

    def steer(self, session_id: str, text: str, owner_account_id: str = "") -> bool:
        team = self._teams.get(self._existing_team_key(session_id, owner_account_id))
        if team is None:
            return False
        fn = getattr(team.leader, "steer", None)
        if not callable(fn):
            return False
        return bool(fn(text))

    def interrupt(self, session_id: str, message: str | None = None, owner_account_id: str = "") -> bool:
        did_interrupt = False
        for target_session_id in self._matching_team_session_ids(session_id, owner_account_id=owner_account_id):
            team = self._teams.get(self._existing_team_key(target_session_id, owner_account_id))
            if team is not None:
                for leader_agent in (getattr(team, "leader", None), getattr(team, "direct_leader", None)):
                    fn = getattr(leader_agent, "interrupt", None)
                    if callable(fn):
                        fn(message)
                        did_interrupt = True
            did_interrupt = self._cancel_plan(target_session_id, message, owner_account_id=owner_account_id) or did_interrupt
            did_interrupt = bool(self._cancel_delegate_tasks(target_session_id, owner_account_id)) or did_interrupt

            with self._active_lock:
                active_key = self._key(target_session_id, owner_account_id)
                active = list(self._active_children.get(active_key, {}).values())
            for record in active:
                agent = record.get("agent")
                fn = getattr(agent, "interrupt", None)
                if callable(fn):
                    fn(message)
                    did_interrupt = True
            if active:
                with self._active_lock:
                    self._active_children.pop(active_key, None)
        return did_interrupt
