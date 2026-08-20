"""会话、工作空间、任务、用量、运行时并发等会话中心路由。"""

from __future__ import annotations

import hashlib
import json
import logging
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from crew.agent.external.runtime_profile import canonical_runtime_model_id, normalize_runtime_models
from crew.core.errors import ToolError
from crew.agent.loop.tool_result_display import (
    SUBAGENT_FULL_RESULT_TOOLS,
    tool_result_detail_for_ui,
)
from crew.core.types import Message, ToolCall, tool_arguments_for_ui
from crew.gateway.auth import account_from_request
from crew.gateway.helpers import require_external_agents_enabled, with_session_agent_labels
from crew.gateway.hooks import hook_registry
from crew.security.settings import strict_security_enabled
from crew.state.session_store import SessionOwnershipError, is_placeholder_title
from crew.state.team_member_model import (
    TeamMemberModelBindingError,
    materialize_team_member_model_bindings,
    set_team_member_model_binding,
)
from crew.team.history_projection import (
    direct_mention_request_ids,
    is_duplicate_team_parent_final,
    team_internal_history_items,
    team_tasks_with_plan_projection,
    team_visible_history_items,
)
from crew.team.roles import CREW_BUILTIN_AGENT_ID, is_crew_builtin_agent

log = logging.getLogger(__name__)


def _tool_result_for_history(tool_call: ToolCall, paired_results: dict[str, str]) -> str:
    """Recover only durable UI artifacts missing from older built-in calls.

    Other built-in tool results intentionally stay out of history payloads;
    replaying all of them would inflate the response and expose detail that the
    previous history UI never rendered.
    """
    if tool_call.result:
        return tool_call.result
    if (
        tool_call.name == "browser_use"
        and tool_call.arguments.get("action") == "screenshot"
    ):
        return tool_result_detail_for_ui(
            tool_call.name, paired_results.get(tool_call.id, "")
        )
    # subagent 委派卡（delegate_task/run_agent）：历史回放同样需要完整
    # {"results":[...]} JSON 渲染任务描述/最终回复/执行摘要，否则重开会话后
    # 卡片只剩任务描述。走 tool_result_detail_for_ui 与 live 路径同一出口。
    if tool_call.name in SUBAGENT_FULL_RESULT_TOOLS:
        return tool_result_detail_for_ui(
            tool_call.name, paired_results.get(tool_call.id, "")
        )
    if tool_call.name in {"Widget", "Canvas"} and tool_call.arguments.get("action") == "show":
        return tool_result_detail_for_ui(
            tool_call.name, paired_results.get(tool_call.id, "")
        )
    if tool_call.name == "publish_site":
        return tool_result_detail_for_ui(
            tool_call.name, paired_results.get(tool_call.id, "")
        )
    return ""


def _session_messages_to_history_items(
    msgs: list[Message],
    *,
    source_session_id: str = "",
) -> list[dict[str, Any]]:
    """把存储消息转换成前端历史回放格式。"""
    items: list[dict[str, Any]] = []
    # Built-in executor stores canonical tool results as separate `tool`
    # messages. Older assistant ToolCall rows therefore have an empty result,
    # even though the live tool_event carried one. Pair them at read time so
    # history can reconstruct UI artifacts such as exported browser images.
    tool_results = {
        message.tool_call_id: message.content
        for message in msgs
        if message.role == "tool" and message.tool_call_id and message.content
    }
    for message in msgs:
        if message.is_meta:
            continue  # 跳过元信息消息
        if message.role == "user" and message.content:
            item = {"role": message.role, "content": message.content}
            if source_session_id:
                item["source_session_id"] = source_session_id
            if message.timestamp is not None:
                item["timestamp"] = message.timestamp
            items.append(item)
        elif message.role == "assistant":
            item: dict[str, Any] = {"role": message.role, "content": message.content}
            if source_session_id:
                item["source_session_id"] = source_session_id
            if message.timestamp is not None:
                item["timestamp"] = message.timestamp
            if message.turn_started_at is not None:
                item["turn_started_at"] = message.turn_started_at
            if message.turn_duration is not None:
                item["turn_duration"] = message.turn_duration
            if message.turn_file_changes:
                item["turn_file_changes"] = message.turn_file_changes
            if message.thinking is not None:
                item["thinking"] = message.thinking
            if message.model:
                item["model"] = message.model
            if message.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": tc.id,
                        "name": tc.name,
                        "arguments": tool_arguments_for_ui(tc.name, tc.arguments),
                        "result": _tool_result_for_history(tc, tool_results),
                        "status": tc.status,
                        **({"ui_label": tc.ui_label} if tc.ui_label else {}),
                        **({"started_at": tc.started_at} if tc.started_at is not None else {}),
                        **({"duration": tc.duration} if tc.duration is not None else {}),
                    }
                    for tc in message.tool_calls
                ]
            items.append(item)
    return items


def _history_sort_key(item: dict[str, Any], index: int) -> tuple[float, int]:
    timestamp = item.get("timestamp")
    if isinstance(timestamp, int | float):
        return (float(timestamp), index)
    # 缺 timestamp 的项（如中断/失败的 team 子回合消息）按 0 处理，与
    # history_projection 内部排序（float(ts or 0)）保持一致；若映射为 inf
    # 会把该条内部消息沉到最终答案之后，同回合语义顺序被打乱。
    return (0.0, index)


async def _teardown_session_resources(
    crew,
    session_id: str,
    owner: str,
    *,
    messages_snapshot: list | None = None,
) -> None:
    """删除会话前回收运行中任务、plan 目录、摘要/记忆/ACP/uploads/task 磁盘产物。

    后台终端进程都挂在 shell task 上（terminal 工具 spawn 时带 task_id，task 的
    cancel 回调即 ``kill_process_group``，现在会整树杀），故 cancel 这些 task 会
    连带回收关联进程。会话删除时若不取消，进程会一直跑到自然结束或超时，与
    "杀不干净"叠加造成内存累积。

    同时 ``plan_manager.reset`` 清掉该会话的 ``.crew/plans/<owner>/<sid>/``，
    并清理 compaction / memory / ACP 绑定 / 会话引用的 uploads / task 落盘文件，
    避免删会话后长期占盘。各清理步骤独立 try/except，失败只打日志不阻断删会话。

    ``messages_snapshot``：删库后再清盘时传入删前历史，供 uploads 反查路径。
    """
    try:
        running = crew.tasks.list_tasks(
            session_id=session_id, status="running", owner_account_id=owner, limit=1000
        )
    except Exception:  # noqa: BLE001
        running = []
    for task in running:
        try:
            await crew.tasks.cancel(
                str(task.get("task_id") or ""), reason="会话已删除", owner_account_id=owner
            )
        except Exception:  # noqa: BLE001
            pass

    browser_manager = getattr(crew, "browser_manager", None)
    if browser_manager is not None:
        try:
            await browser_manager.close_session(owner, session_id)
        except Exception:  # noqa: BLE001
            session_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]
            log.warning("清理会话浏览器标签页失败 session=%s", session_hash, exc_info=True)

    # uploads：优先用调用方快照（workspace 先删库场景）；否则从 session_store 反查
    try:
        from crew.gateway.context import delete_session_uploads

        msgs = messages_snapshot
        if msgs is None:
            msgs = crew.session_store.load(session_id, owner_account_id=owner)
        delete_session_uploads(msgs or [], owner_account_id=owner)
    except Exception as exc:  # noqa: BLE001
        log.warning("删除会话 %s 时清理 uploads 失败: %s", session_id, exc)

    pm = getattr(crew, "plan_manager", None)
    if pm is not None:
        try:
            pm.reset(session_id, owner_account_id=owner)
        except Exception as exc:  # noqa: BLE001
            log.warning("删除会话 %s 时清理 plan 目录失败: %s", session_id, exc)

    summary_store = getattr(crew, "summary_store", None)
    if summary_store is not None:
        try:
            summary_store.delete(session_id, owner_account_id=owner)
        except Exception as exc:  # noqa: BLE001
            log.warning("删除会话 %s 时清理 compaction 摘要失败: %s", session_id, exc)

    memory = getattr(crew, "memory", None)
    delete_mem = getattr(memory, "delete", None) if memory is not None else None
    if callable(delete_mem):
        try:
            result = delete_mem(session_id, owner_account_id=owner)
            if hasattr(result, "__await__"):
                await result
        except Exception as exc:  # noqa: BLE001
            log.warning("删除会话 %s 时清理 memory 失败: %s", session_id, exc)

    external = getattr(crew, "external_agents", None)
    delete_acp = getattr(external, "delete_acp_bindings_for_session", None) if external else None
    if callable(delete_acp):
        try:
            delete_acp(session_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("删除会话 %s 时清理 ACP 绑定失败: %s", session_id, exc)

    # 已结束任务的磁盘 log/json（运行中已在上方 cancel）
    unlink_fn = getattr(getattr(crew, "tasks", None), "unlink_session_output_files", None)
    if callable(unlink_fn):
        try:
            unlink_fn(session_id, owner_account_id=owner)
        except Exception as exc:  # noqa: BLE001
            log.warning("删除会话 %s 时清理 task 磁盘文件失败: %s", session_id, exc)

    # 已停用 / 非阻塞的 cron 元数据：删会话后级联清掉，避免指向死 session_id 的孤儿行。
    # 调用方须先用 _session_has_blocking_cron 拦住 enabled / running。
    store = getattr(crew, "cron_store", None)
    delete_jobs = getattr(store, "delete_jobs_for_session", None) if store is not None else None
    if callable(delete_jobs):
        try:
            delete_jobs(session_id, owner_account_id=owner)
        except Exception as exc:  # noqa: BLE001
            log.warning("删除会话 %s 时清理 cron 元数据失败: %s", session_id, exc)


def _session_has_blocking_cron(crew, session_id: str, owner: str) -> str | None:
    """若该会话仍有未停止的定时任务，返回给用户的提示文案；否则 None。"""
    store = getattr(crew, "cron_store", None)
    if store is None:
        return None
    try:
        jobs = store.list(session_id=session_id, owner_account_id=owner)
    except Exception as exc:  # noqa: BLE001
        log.warning("检查会话 %s 定时任务失败: %s", session_id, exc)
        return None
    enabled = [j for j in jobs if j.get("enabled")]
    if enabled:
        names = "、".join(str(j.get("name") or j.get("id") or "?") for j in enabled[:3])
        more = f" 等 {len(enabled)} 个" if len(enabled) > 3 else ""
        return f"该会话仍有进行中的定时任务（{names}{more}），请先停止后再删除会话。"
    # 已停用但仍有 running run：避免打断正在执行的一轮
    has_running_run = getattr(store, "session_has_running_job_run", None)
    if callable(has_running_run):
        try:
            if has_running_run(session_id, owner_account_id=owner):
                return "该会话仍有正在执行的定时任务，请等待完成或停止后再删除会话。"
        except Exception as exc:  # noqa: BLE001
            log.warning("检查会话 %s 定时任务执行状态失败: %s", session_id, exc)
    else:
        for job in jobs:
            jid = str(job.get("id") or "")
            if not jid:
                continue
            try:
                runs = store.get_job_runs(jid, limit=5)
            except Exception:  # noqa: BLE001
                continue
            if any(str(r.get("status") or "") == "running" for r in runs):
                return "该会话仍有正在执行的定时任务，请等待完成或停止后再删除会话。"
    return None


def create_sessions_router(crew, dispatcher) -> APIRouter:
    router = APIRouter()

    @router.get("/api/channel-sessions")
    async def channel_sessions(request: Request) -> JSONResponse:
        """绑定者可见的渠道会话（按平台分组，仅绑定后有消息的会话）。"""
        from crew.gateway.channel_sessions import list_channel_session_groups
        from crew.gateway.platform_registry import platform_registry

        owner = _owner(request)
        bindings = getattr(crew, "channel_bindings", None)
        if bindings is None:
            return JSONResponse({"platforms": []})
        labels = {e.name: e.label for e in platform_registry.all_entries()}
        groups = list_channel_session_groups(
            crew.session_store,
            bindings,
            owner,
            labels,
        )
        return JSONResponse({"platforms": groups})

    @router.get("/api/sessions")
    async def sessions(
        request: Request,
        workspace_id: str | None = None,
        include_archived: bool = False,
    ) -> JSONResponse:
        owner = account_from_request(request).owner_account_id
        return JSONResponse(
            with_session_agent_labels(
                crew,
                crew.session_store.list_sessions(
                    workspace_id,
                    owner_account_id=owner,
                    include_archived=include_archived,
                ),
                owner_account_id=owner,
            )
        )

    @router.post("/api/session/{session_id}/ensure")
    async def ensure_session(request: Request, session_id: str, payload: dict) -> JSONResponse:
        """Persist a renderer draft before a session-scoped feature uses it.

        A new chat exists only in renderer memory until its first message.  Features such as
        the built-in Browser need an owned backend session immediately, without manufacturing
        a chat message just to make the session real.
        """
        owner = _owner(request)
        ensure = getattr(crew.session_store, "ensure_session", None)
        if not callable(ensure):
            return JSONResponse(
                {"ok": False, "error": "会话存储不支持创建会话"}, status_code=503
            )
        workspace_id = str(payload.get("workspace_id") or "default").strip() or "default"
        title = str(payload.get("title") or "新对话").strip() or "新对话"
        try:
            ensure(
                session_id,
                workspace_id=workspace_id,
                title=title,
                owner_account_id=owner,
            )
        except SessionOwnershipError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        if not _session_owned(session_id, owner):
            return _not_found(session_id)
        return JSONResponse({"ok": True, "session_id": session_id})

    # ---- 工作空间 ----
    @router.get("/api/workspaces")
    async def workspaces(request: Request) -> JSONResponse:
        owner = account_from_request(request).owner_account_id
        return JSONResponse(crew.workspace_store.list(owner_account_id=owner))

    @router.post("/api/workspaces")
    async def create_workspace(request: Request, payload: dict) -> JSONResponse:
        owner = account_from_request(request).owner_account_id
        raw_root = str(payload.get("root_path") or "").strip()
        if raw_root:
            from crew.state.workspace_store import _normalize_root_path

            if not _normalize_root_path(raw_root):
                return JSONResponse(
                    {"ok": False, "error": "root_path 不是有效目录"},
                    status_code=400,
                )
        ws = crew.workspace_store.create(
            name=payload.get("name", "新工作空间"),
            description=payload.get("description", ""),
            instructions=payload.get("instructions", ""),
            root_path=str(payload.get("root_path") or ""),
            owner_account_id=owner,
        )
        return JSONResponse(ws)

    @router.put("/api/workspace/{workspace_id}")
    async def update_workspace(request: Request, workspace_id: str, payload: dict) -> JSONResponse:
        owner = account_from_request(request).owner_account_id
        try:
            return JSONResponse(crew.workspace_store.update(workspace_id, owner_account_id=owner, **payload))
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)

    @router.delete("/api/workspace/{workspace_id}")
    async def delete_workspace(request: Request, workspace_id: str) -> JSONResponse:
        owner = account_from_request(request).owner_account_id
        # 先列出会话，任一有未停止 cron 则整次拒绝（避免半删）
        try:
            preview = crew.session_store.list_sessions(workspace_id, owner_account_id=owner)
        except Exception:  # noqa: BLE001
            preview = []
        session_ids = [str(row.get("session_id") or "") for row in preview if row.get("session_id")]
        for sid in session_ids:
            block = _session_has_blocking_cron(crew, sid, owner)
            if block:
                return JSONResponse(
                    {"ok": False, "error": block, "code": "cron_active", "session_id": sid},
                    status_code=409,
                )
        # 先事务删库，再尽力清盘：避免「盘已清、库行仍在」的半失败。
        # uploads 反查依赖历史：在删库前把各会话消息快照下来，供 teardown 使用。
        message_snapshots: dict[str, list] = {}
        for sid in session_ids:
            try:
                message_snapshots[sid] = list(
                    crew.session_store.load(sid, owner_account_id=owner) or []
                )
            except Exception:  # noqa: BLE001
                message_snapshots[sid] = []
        try:
            def _delete_workspace(conn):
                deleted = crew.session_store.delete_sessions_for_workspace(
                    workspace_id, owner_account_id=owner, writer=conn
                )
                crew.workspace_store.delete(workspace_id, owner_account_id=owner, writer=conn)
                return deleted

            tx = getattr(crew.session_store, "transaction", None)
            deleted_sessions = tx(_delete_workspace) if callable(tx) else crew.session_store.delete_sessions_for_workspace(
                workspace_id, owner_account_id=owner
            )
            for sid in deleted_sessions:
                await hook_registry.emit("session:end", {"session_id": sid, "owner_account_id": owner})
                if crew.dynamic_kanban is not None:
                    try:
                        crew.dynamic_kanban.interrupt(sid, owner_account_id=owner)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("删除工作空间 %s 时会话 %s 中断 Kanban 失败: %s", workspace_id, sid, exc)
                    try:
                        crew.dynamic_kanban.clear_session_workspaces(
                            sid,
                            owner_account_id=owner,
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.warning("删除工作空间 %s 时会话 %s 清理 Kanban 目录失败: %s", workspace_id, sid, exc)
            if not callable(tx):
                crew.workspace_store.delete(workspace_id, owner_account_id=owner)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        for sid in session_ids:
            await hook_registry.emit("session:end", {"session_id": sid, "owner_account_id": owner})
            await _teardown_session_resources(
                crew,
                sid,
                owner,
                messages_snapshot=message_snapshots.get(sid),
            )
            if crew.dynamic_kanban is not None:
                try:
                    crew.dynamic_kanban.interrupt(sid, owner_account_id=owner)
                except Exception as exc:  # noqa: BLE001
                    log.warning("删除工作空间 %s 时会话 %s 中断 Kanban 失败: %s", workspace_id, sid, exc)
                try:
                    crew.dynamic_kanban.clear_session_workspaces(
                        sid,
                        owner_account_id=owner,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("删除工作空间 %s 时会话 %s 清理 Kanban 目录失败: %s", workspace_id, sid, exc)
        return JSONResponse({"ok": True, "deleted_sessions": deleted_sessions})

    def _owner(request: Request) -> str:
        return account_from_request(request).owner_account_id

    def _session_owned(session_id: str, owner: str) -> bool:
        belongs = getattr(crew.session_store, "session_belongs_to", None)
        return bool(callable(belongs) and belongs(session_id, owner))

    def _not_found(session_id: str) -> JSONResponse:
        return JSONResponse({"ok": False, "error": f"会话不存在: {session_id}"}, status_code=404)

    @router.get("/api/session/{session_id}/todos")
    async def session_todos(request: Request, session_id: str) -> JSONResponse:
        """返回当前 todo 快照（访问即触发 plan_manager 的惰性 hydrate）。"""
        owner = _owner(request)
        if not _session_owned(session_id, owner):
            return _not_found(session_id)
        pm = getattr(crew, "plan_manager", None)
        if pm is None:
            return JSONResponse({"todos": []})
        return JSONResponse({"todos": pm.todo_store(session_id, owner_account_id=owner).read()})

    @router.get("/api/session/{session_id}")
    async def session_history(request: Request, session_id: str) -> JSONResponse:
        owner = _owner(request)
        if not _session_owned(session_id, owner):
            return _not_found(session_id)
        msgs = crew.session_store.load(session_id, owner_account_id=owner)
        items = _session_messages_to_history_items(msgs, source_session_id=session_id)

        # Team 父 session 本身通常只保存 meta 唤醒消息，真实对话分布在
        # {team_session}::turn::*::{leader/member} 子 session。点击左侧 Team 会话时，
        # 聚合这些子历史，避免右侧显示为空欢迎态。
        getter = getattr(crew.session_store, "get_agent_config", None)
        config = getter(session_id, owner_account_id=owner) if callable(getter) else None
        should_aggregate = str((config or {}).get("executor") or "").lower() == "team"
        loader = getattr(crew.session_store, "load_child_sessions", None)
        child_sessions = loader(session_id, owner_account_id=owner) if callable(loader) else []
        if child_sessions:
            should_aggregate = should_aggregate or any("::turn::" in child_id for child_id, _ in child_sessions)
        if should_aggregate:
            internal = team_internal_history_items(
                crew,
                session_id,
                child_sessions,
                owner_account_id=owner,
                config=config,
            )
            visible = team_visible_history_items(
                crew,
                session_id,
                owner_account_id=owner,
                config=config,
                has_child_team_sessions=any("::turn::" in child_id for child_id, _ in child_sessions),
                suppressed_request_ids=direct_mention_request_ids(internal),
            )
            if internal:
                visible = [
                    item for item in visible
                    if not is_duplicate_team_parent_final(item, internal)
                ]
            if visible or internal:
                items = [
                    item
                    for _, item in sorted(
                        enumerate([*visible, *internal]),
                        key=lambda pair: _history_sort_key(pair[1], pair[0]),
                    )
                ]
            else:
                items = [
                    item
                    for _, item in sorted(
                        enumerate(items),
                        key=lambda pair: _history_sort_key(pair[1], pair[0]),
                    )
                ]
        return JSONResponse(items)

    @router.get("/api/session/{session_id}/plan")
    async def session_plan(request: Request, session_id: str) -> JSONResponse:
        """Return persisted plan content for history replay and current plan state."""
        owner = _owner(request)
        if not _session_owned(session_id, owner):
            return _not_found(session_id)
        from crew.agent.plan import plan_display_path, read_plan

        pm = getattr(crew, "plan_manager", None)
        active = bool(pm is not None and pm.is_active(session_id, owner_account_id=owner))
        awaiting = bool(pm is not None and pm.is_awaiting_approval(session_id, owner_account_id=owner))
        phase = pm.phase(session_id, owner_account_id=owner) if pm is not None else "inactive"
        plan = read_plan(session_id, owner_account_id=owner)
        status = {
            "review": "pending",
            "revising": "revising",
            "approved": "approved",
            "rejected": "rejected",
            "cancelled": "cancelled",
            "active": "editing",
        }.get(phase, "readonly")
        # 注意：不要把 active+已有正文映射成 pending。
        # 批准落地后若误再 enter，phase 会回到 active 且仍读到旧 plan 文件；
        # 若映射成 pending，看板会永久卡在「等待审批」并露出批准按钮。
        # 真正待批只由 phase=review（awaiting_approval）表达。
        return JSONResponse({
            "session_id": session_id,
            "active": active,
            "awaiting_approval": awaiting,
            "phase": phase,
            "status": status,
            "plan": plan or "",
            "plan_file": plan_display_path(session_id, owner_account_id=owner),
            "has_plan": bool(plan),
        })

    @router.put("/api/session/{session_id}/title")
    async def rename_session(request: Request, session_id: str, payload: dict) -> JSONResponse:
        owner = _owner(request)
        if not _session_owned(session_id, owner):
            return _not_found(session_id)
        crew.session_store.set_title(session_id, payload.get("title", ""), owner_account_id=owner)
        return JSONResponse({"ok": True})

    @router.put("/api/session/{session_id}/archive")
    async def archive_session(request: Request, session_id: str, payload: dict) -> JSONResponse:
        """归档 / 取消归档会话。body: {archived: bool}。归档会话从主列表隐藏。"""
        owner = _owner(request)
        if not _session_owned(session_id, owner):
            return _not_found(session_id)
        archived = bool(payload.get("archived", True))
        crew.session_store.set_archived(session_id, archived, owner_account_id=owner)
        return JSONResponse({"ok": True, "archived": archived})

    @router.put("/api/session/{session_id}/pin")
    async def pin_session(request: Request, session_id: str, payload: dict) -> JSONResponse:
        """置顶 / 取消置顶会话。body: {pinned: bool}。置顶会话在主列表排序靠前。"""
        owner = _owner(request)
        if not _session_owned(session_id, owner):
            return _not_found(session_id)
        pinned = bool(payload.get("pinned", True))
        crew.session_store.set_pinned(session_id, pinned, owner_account_id=owner)
        return JSONResponse({"ok": True, "pinned": pinned})

    def is_external_session_config(config: dict[str, Any] | None) -> bool:
        if not isinstance(config, dict):
            return False
        executor = str(config.get("executor") or "").strip().lower()
        team = config.get("team") if isinstance(config.get("team"), dict) else {}
        return executor in {"external", "acp"} or (
            executor == "team" and bool(str(team.get("external_team_id") or "").strip())
        )

    def runtime_model_switchable(runtime: dict[str, Any], models: list[Any]) -> bool:
        metadata = runtime.get("metadata") if isinstance(runtime.get("metadata"), dict) else {}
        capabilities = (
            metadata.get("runtime_capabilities")
            if isinstance(metadata.get("runtime_capabilities"), dict)
            else {}
        )
        return bool(
            metadata.get("availability_status") == "ready"
            and models
            and capabilities.get("model_switch") is True
        )

    def external_session_model_binding(session_id: str, owner: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        getter = getattr(crew.session_store, "get_agent_config", None)
        if not callable(getter):
            return None
        raw = getter(session_id, owner_account_id=owner)
        if is_external_session_config(raw):
            require_external_agents_enabled(crew)
        if (
            not isinstance(raw, dict)
            or str(raw.get("executor") or "").strip().lower() not in {"external", "acp"}
        ):
            return None
        config = {key: value for key, value in raw.items() if not key.startswith("_")}
        external = config.get("external") if isinstance(config.get("external"), dict) else {}
        acp = config.get("acp") if isinstance(config.get("acp"), dict) else {}
        external_agent_id = str(
            config.get("external_agent_id")
            or external.get("external_agent_id")
            or acp.get("external_agent_id")
            or ""
        ).strip()
        if not external_agent_id:
            return None
        agent, runtime = crew.external_agents.agent_with_runtime(
            external_agent_id,
            owner_account_id=owner,
        )
        metadata = runtime.get("metadata") if isinstance(runtime.get("metadata"), dict) else {}
        models = normalize_runtime_models(metadata.get("models"))
        selected = canonical_runtime_model_id(
            runtime,
            str(
                external.get("model")
                or acp.get("model")
                or agent.get("model")
                or metadata.get("default_model_id")
                or ""
            ).strip(),
        )
        selected_profile = next((item for item in models if item.id == selected), None)
        body = {
            "ok": True,
            "source": "external",
            "model_profile_id": selected,
            "model_label": selected_profile.label if selected_profile else selected,
            "pending_model_profile_id": None,
            "pending_label": None,
            "has_pending": False,
            "pending": False,
            "models": [item.to_dict() for item in models],
            "model_switchable": runtime_model_switchable(runtime, models),
            "runtime_id": str(runtime.get("id") or ""),
            "external_agent_id": external_agent_id,
        }
        return body, config

    def team_session_model_binding(
        session_id: str,
        owner: str,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        getter = getattr(crew.session_store, "get_agent_config", None)
        if not callable(getter):
            return None
        raw = getter(session_id, owner_account_id=owner)
        if not isinstance(raw, dict):
            return None
        team_config = raw.get("team") if isinstance(raw.get("team"), dict) else {}
        if (
            str(raw.get("executor") or "").strip().lower() != "team"
            or not str(team_config.get("external_team_id") or "").strip()
        ):
            return None
        require_external_agents_enabled(crew)
        stored, team = materialize_team_member_model_bindings(
            crew.session_store,
            crew.external_agents,
            session_id,
            owner_account_id=owner,
            builtin_model_id=crew.config.owner_default_model_id(owner),
        )
        stored_team = stored.get("team") if isinstance(stored.get("team"), dict) else {}
        bindings = (
            stored_team.get("member_model_bindings")
            if isinstance(stored_team.get("member_model_bindings"), dict)
            else {}
        )
        leader_agent_id = str(team.get("leader_agent_id") or "")
        team_member_state = getattr(crew.team, "team_member_switch_state", None)
        dispatcher_state = dispatcher.status(session_id, owner_account_id=owner)
        session_is_running = dispatcher_state.get("live") != "idle"
        owner_profiles = crew.owner_model_profiles(owner)
        owner_default_model_id = crew.config.owner_default_model_id(owner)
        members: list[dict[str, Any]] = []
        for team_member in team.get("members") or []:
            if not isinstance(team_member, dict):
                continue
            agent_id = str(team_member.get("agent_id") or "").strip()
            binding = bindings.get(agent_id) if isinstance(bindings.get(agent_id), dict) else {}
            selected_model_id = str(binding.get("model_id") or "").strip()
            if is_crew_builtin_agent(agent_id):
                models = [
                    {
                        "id": profile.id,
                        "label": profile.label,
                        "provider": profile.provider,
                        "default": profile.id == owner_default_model_id,
                        "capabilities": list(profile.capabilities),
                        "context_window": profile.context_window,
                        "loaded": profile.loaded,
                        "has_key": profile.has_key,
                    }
                    for profile in owner_profiles.values()
                ]
                selected = owner_profiles.get(selected_model_id)
                switchable = any(
                    profile.loaded and profile.has_key
                    for profile in owner_profiles.values()
                )
                unavailable_reason = (
                    None
                    if switchable
                    else "没有可用的内置模型配置"
                )
                runtime_id = "builtin"
                model_label = selected.label if selected is not None else selected_model_id
            else:
                agent, runtime = crew.external_agents.agent_with_runtime(
                    agent_id,
                    owner_account_id=owner,
                )
                metadata = runtime.get("metadata") if isinstance(runtime.get("metadata"), dict) else {}
                normalized_models = normalize_runtime_models(metadata.get("models"))
                models = [item.to_dict() for item in normalized_models]
                selected = next((item for item in normalized_models if item.id == selected_model_id), None)
                switchable = runtime_model_switchable(runtime, normalized_models)
                if metadata.get("availability_status") != "ready":
                    unavailable_reason = "成员运行时当前不可用"
                elif not normalized_models:
                    unavailable_reason = "成员运行时未提供模型目录"
                elif not switchable:
                    unavailable_reason = "成员运行时不支持保留会话的模型切换"
                else:
                    unavailable_reason = None
                runtime_id = str(runtime.get("id") or agent.get("runtime_id") or "")
                model_label = selected.label if selected is not None else selected_model_id
            is_leader = agent_id == leader_agent_id
            # API/持久化用外部 agent id；Team 运行时则以 member_id 路由，
            # 对外部普通成员该 id 优先是 agent_name。忙闲判断必须映射到后者。
            runtime_member_id = (
                "leader"
                if is_leader
                else CREW_BUILTIN_AGENT_ID
                if is_crew_builtin_agent(agent_id)
                else str(team_member.get("agent_name") or agent_id)
            )
            state = (
                team_member_state(
                    session_id,
                    runtime_member_id,
                    owner_account_id=owner,
                )
                if callable(team_member_state)
                else {}
            )
            active_task_count = int(state.get("active_task_count") or 0)
            # Leader 的执行由 dispatcher 管理，而 teammate 的执行由
            # TeamManager 的 child registry 管理；两者合起来才是成员级状态。
            if is_leader and session_is_running:
                status = "running"
                active_task_count = max(1, active_task_count)
            else:
                status = str(state.get("status") or "idle")
            members.append({
                "member_id": agent_id,
                "member_name": str(team_member.get("agent_name") or agent_id),
                "is_leader": is_leader,
                "runtime_id": runtime_id,
                "model_profile_id": selected_model_id,
                "model_label": model_label,
                "binding_source": str(binding.get("binding_source") or ""),
                "binding_revision": int(binding.get("revision") or 0),
                "status": status,
                "active_task_count": active_task_count,
                "model_switchable": switchable,
                "unavailable_reason": unavailable_reason,
                "models": models,
            })
        return {
            "ok": True,
            "source": "team",
            "scope": "team",
            "session_id": session_id,
            "external_team_id": str(team.get("id") or ""),
            "model_binding_revision": int(stored_team.get("model_binding_revision") or 0),
            "members": members,
        }, stored

    @router.get("/api/session/{session_id}/model")
    async def get_session_model(request: Request, session_id: str) -> JSONResponse:
        owner = _owner(request)
        if not _session_owned(session_id, owner):
            return _not_found(session_id)
        try:
            team_binding = team_session_model_binding(session_id, owner)
            external = external_session_model_binding(session_id, owner)
        except TeamMemberModelBindingError as exc:
            return JSONResponse({"ok": False, "code": exc.code, "error": exc.message}, status_code=409)
        except KeyError:
            return JSONResponse({"ok": False, "error": "外部智能体或运行时不存在"}, status_code=409)
        if team_binding is not None:
            body = team_binding[0]
            member_id = str(request.query_params.get("member_id") or "").strip()
            if not member_id:
                return JSONResponse(body)
            member = next((item for item in body["members"] if item["member_id"] == member_id), None)
            if member is None:
                return JSONResponse(
                    {"ok": False, "code": "session_or_member_not_found", "error": "Team 成员不存在"},
                    status_code=404,
                )
            return JSONResponse({
                "ok": True,
                "source": "team",
                "scope": "team_member",
                "session_id": session_id,
                "external_team_id": body["external_team_id"],
                "model_binding_revision": body["model_binding_revision"],
                **member,
            })
        if external is not None:
            return JSONResponse(external[0])
        return JSONResponse(crew.read_session_model_binding(session_id, owner_account_id=owner))

    @router.put("/api/session/{session_id}/model")
    async def put_session_model(request: Request, session_id: str, payload: dict) -> JSONResponse:
        owner = _owner(request)
        model_id = str(payload.get("model_profile_id") or "").strip()
        if not model_id:
            return JSONResponse({"ok": False, "error": "model_profile_id 必填"}, status_code=400)

        workspace_id = str(payload.get("workspace_id") or "default")
        title = str(payload.get("title") or "")
        ensure = getattr(crew.session_store, "ensure_session", None)
        if callable(ensure):
            try:
                ensure(
                    session_id,
                    workspace_id=workspace_id,
                    title=title,
                    owner_account_id=owner,
                )
            except SessionOwnershipError as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)

        if not _session_owned(session_id, owner):
            return _not_found(session_id)

        st = dispatcher.status(session_id, owner_account_id=owner)
        try:
            team_binding = team_session_model_binding(session_id, owner)
            external = external_session_model_binding(session_id, owner)
        except TeamMemberModelBindingError as exc:
            return JSONResponse({"ok": False, "code": exc.code, "error": exc.message}, status_code=409)
        except KeyError:
            return JSONResponse({"ok": False, "error": "外部智能体或运行时不存在"}, status_code=409)
        if team_binding is not None:
            binding_body = team_binding[0]
            member_id = str(payload.get("member_id") or "").strip()
            if not member_id:
                return JSONResponse(
                    {"ok": False, "code": "member_required", "error": "Team 模型切换必须指定 member_id"},
                    status_code=400,
                )
            member = next(
                (item for item in binding_body["members"] if item["member_id"] == member_id),
                None,
            )
            if member is None:
                return JSONResponse(
                    {"ok": False, "code": "session_or_member_not_found", "error": "Team 成员不存在"},
                    status_code=404,
                )
            runtime_member_id = (
                "leader"
                if member.get("is_leader")
                else str(member.get("member_name") or member_id)
            )
            if model_id == str(member.get("model_profile_id") or ""):
                return JSONResponse({
                    "ok": True,
                    "source": "team",
                    "scope": "team_member",
                    "session_id": session_id,
                    "external_team_id": binding_body["external_team_id"],
                    "model_binding_revision": binding_body["model_binding_revision"],
                    **member,
                })
            if member.get("status") != "idle":
                return JSONResponse(
                    {"ok": False, "code": "member_busy", "error": "目标成员运行中，请在任务结束后切换模型"},
                    status_code=409,
                )
            if is_crew_builtin_agent(member_id):
                profile = crew.owner_model_profiles(owner).get(model_id)
                if profile is None or not profile.loaded:
                    return JSONResponse(
                        {"ok": False, "code": "model_not_available", "error": "所选模型不可用"},
                        status_code=400,
                    )
                if not profile.has_key:
                    return JSONResponse(
                        {"ok": False, "code": "model_not_available", "error": "所选模型未配置 API Key"},
                        status_code=409,
                    )
                runtime_id = "builtin"
                canonical_model_id = profile.id
            else:
                try:
                    agent, runtime = crew.external_agents.agent_with_runtime(
                        member_id,
                        owner_account_id=owner,
                    )
                except KeyError:
                    return JSONResponse(
                        {"ok": False, "code": "session_or_member_not_found", "error": "Team 成员不存在"},
                        status_code=404,
                    )
                metadata = runtime.get("metadata") if isinstance(runtime.get("metadata"), dict) else {}
                models = normalize_runtime_models(metadata.get("models"))
                canonical_model_id = canonical_runtime_model_id(runtime, model_id)
                selected = next((item for item in models if item.id == canonical_model_id), None)
                if selected is None:
                    return JSONResponse(
                        {"ok": False, "code": "model_not_in_runtime", "error": "所选模型不属于目标成员运行时"},
                        status_code=400,
                    )
                if not runtime_model_switchable(runtime, models):
                    return JSONResponse(
                        {
                            "ok": False,
                            "code": "runtime_model_switch_unsupported",
                            "error": "目标成员运行时不支持保留会话的模型切换",
                        },
                        status_code=409,
                    )
                crew.external_agents.resolve_agent_profile(
                    agent["id"],
                    canonical_model_id,
                    owner_account_id=owner,
                )
                runtime_id = str(runtime.get("id") or agent.get("runtime_id") or "")
            expected_revision_raw = payload.get("expected_revision")
            try:
                expected_revision = (
                    int(expected_revision_raw)
                    if expected_revision_raw is not None
                    else None
                )
            except (TypeError, ValueError):
                return JSONResponse(
                    {"ok": False, "code": "invalid_revision", "error": "expected_revision 必须是整数"},
                    status_code=400,
                )
            team_manager = getattr(crew, "team", None)
            member_lock_factory = getattr(team_manager, "member_model_lock", None)
            lock_context = (
                member_lock_factory(session_id, runtime_member_id, owner)
                if callable(member_lock_factory)
                else nullcontext()
            )
            with lock_context:
                if callable(getattr(team_manager, "team_is_planning", None)) and team_manager.team_is_planning(
                    session_id,
                    owner,
                ):
                    return JSONResponse(
                        {"ok": False, "code": "team_planning", "error": "Team 正在规划中，请稍后重试"},
                        status_code=409,
                    )
                latest_state = (
                    team_manager.team_member_switch_state(
                        session_id,
                        runtime_member_id,
                        owner_account_id=owner,
                    )
                    if callable(getattr(team_manager, "team_member_switch_state", None))
                    else {}
                )
                latest_dispatcher_state = dispatcher.status(session_id, owner_account_id=owner)
                if (
                    latest_state.get("status") != "idle"
                    or (
                        member.get("is_leader")
                        and latest_dispatcher_state.get("live") != "idle"
                    )
                ):
                    return JSONResponse(
                        {"ok": False, "code": "member_busy", "error": "目标成员运行中，请在任务结束后切换模型"},
                        status_code=409,
                    )
                incompatibility_checker = getattr(team_manager, "pending_model_switch_incompatibilities", None)
                if callable(incompatibility_checker) and not is_crew_builtin_agent(member_id):
                    incompatible_nodes = incompatibility_checker(
                        session_id,
                        member_id,
                        runtime,
                        canonical_model_id,
                        owner_account_id=owner,
                    )
                    if incompatible_nodes:
                        return JSONResponse(
                            {
                                "ok": False,
                                "code": "pending_work_incompatible",
                                "error": "所选模型不满足当前待执行节点的硬能力要求",
                                "incompatible_nodes": incompatible_nodes,
                            },
                            status_code=409,
                        )
                try:
                    set_team_member_model_binding(
                        crew.session_store,
                        session_id,
                        owner_account_id=owner,
                        agent_id=member_id,
                        runtime_id=runtime_id,
                        model_id=canonical_model_id,
                        binding_source=(
                            "restored_from_agent_default"
                            if bool(payload.get("restore_default"))
                            else "session_override"
                        ),
                        expected_revision=expected_revision,
                    )
                except TeamMemberModelBindingError as exc:
                    status_code = 404 if exc.code == "session_or_member_not_found" else 409
                    return JSONResponse(
                        {"ok": False, "code": exc.code, "error": exc.message},
                        status_code=status_code,
                    )
                drop_team = getattr(crew.team, "drop_session_team", None)
                if callable(drop_team):
                    drop_team(session_id, owner_account_id=owner)
            refreshed = team_session_model_binding(session_id, owner)
            if refreshed is None:  # pragma: no cover - binding cannot change executor here
                return JSONResponse({"ok": False, "error": "Team 模型绑定读取失败"}, status_code=500)
            refreshed_body = refreshed[0]
            refreshed_member = next(
                item for item in refreshed_body["members"] if item["member_id"] == member_id
            )
            return JSONResponse({
                "ok": True,
                "source": "team",
                "scope": "team_member",
                "session_id": session_id,
                "external_team_id": refreshed_body["external_team_id"],
                "model_binding_revision": refreshed_body["model_binding_revision"],
                **refreshed_member,
            })
        if external is not None:
            binding, config = external
            if st.get("live") != "idle":
                return JSONResponse(
                    {"ok": False, "error": "外部智能体运行中，请在任务结束后切换模型"},
                    status_code=409,
                )
            if not binding["model_switchable"]:
                return JSONResponse({"ok": False, "error": "当前运行时暂不支持模型切换"}, status_code=409)
            models = binding.get("models") or []
            selected = next((item for item in models if item.get("id") == model_id), None)
            if selected is None:
                return JSONResponse({"ok": False, "error": "所选模型不属于当前运行时"}, status_code=400)
            external_config = (
                config.get("external")
                if isinstance(config.get("external"), dict)
                else config.get("acp")
                if isinstance(config.get("acp"), dict)
                else {}
            )
            config["external"] = {
                **external_config,
                "external_agent_id": binding["external_agent_id"],
                "model": model_id,
            }
            config["executor"] = "external"
            config.pop("acp", None)
            setter = getattr(crew.session_store, "set_agent_config", None)
            if not callable(setter):
                return JSONResponse({"ok": False, "error": "session agent config store 不可用"}, status_code=500)
            setter(session_id, config, owner_account_id=owner)
            crew.agents.drop(session_id, owner_account_id=owner)
            binding.update({"model_profile_id": model_id, "model_label": selected.get("label") or model_id})
            return JSONResponse(binding)

        busy = st.get("live") in ("running", "queued")
        try:
            body = crew.set_session_model_binding(
                session_id,
                model_id,
                owner_account_id=owner,
                busy=busy,
            )
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        return JSONResponse(body)

    @router.get("/api/session/{session_id}/agent-config")
    async def get_session_agent_config(request: Request, session_id: str) -> JSONResponse:
        owner = _owner(request)
        if not _session_owned(session_id, owner):
            return _not_found(session_id)
        getter = getattr(crew.session_store, "get_agent_config", None)
        if not callable(getter):
            return JSONResponse({})
        config = getter(session_id, owner_account_id=owner) or {}
        if strict_security_enabled():
            config = {key: value for key, value in config.items() if key != "user_type"}
        if is_external_session_config(config):
            require_external_agents_enabled(crew)
        return JSONResponse(config)

    @router.put("/api/session/{session_id}/agent-config")
    async def set_session_agent_config(request: Request, session_id: str, payload: dict) -> JSONResponse:
        owner = _owner(request)
        raw_config = payload.get("config") if isinstance(payload.get("config"), dict) else payload
        config = {k: v for k, v in raw_config.items() if k not in {"workspace_id", "title"}}
        if strict_security_enabled():
            config.pop("user_type", None)
        executor = str(config.get("executor") or "").strip().lower()
        if executor not in {"builtin", "client", "external", "acp", "team"}:
            return JSONResponse(
                {"ok": False, "error": "executor 必须是 builtin | client | external | team"},
                status_code=400,
            )
        external_config = config.get("external") if isinstance(config.get("external"), dict) else {}
        acp_config = config.get("acp") if isinstance(config.get("acp"), dict) else {}
        team_config = config.get("team") if isinstance(config.get("team"), dict) else {}
        external_agent_id = str(
            config.get("external_agent_id")
            or external_config.get("external_agent_id")
            or acp_config.get("external_agent_id")
            or ""
        ).strip()
        external_team_id = str(team_config.get("external_team_id") or "").strip()
        if (
            executor in {"external", "acp"}
            or (executor == "team" and external_team_id)
        ):
            require_external_agents_enabled(crew)

        if executor in {"external", "acp"} and external_agent_id:
            try:
                _, runtime = crew.external_agents.agent_with_runtime(
                    external_agent_id,
                    owner_account_id=owner,
                )
            except KeyError:
                return JSONResponse(
                    {"ok": False, "error": "外援智能体不存在或无权访问"},
                    status_code=404,
                )
            metadata = runtime.get("metadata") if isinstance(runtime.get("metadata"), dict) else {}
            availability = str(metadata.get("availability_status") or "").strip().lower()
            if availability in {"degraded", "unavailable"}:
                return JSONResponse(
                    {"ok": False, "error": "外援智能体的运行时当前不可用，请重新探测"},
                    status_code=409,
                )
        if executor == "team" and external_team_id:
            try:
                crew.external_agents.get_team(
                    external_team_id,
                    owner_account_id=owner,
                )
            except KeyError:
                return JSONResponse(
                    {"ok": False, "error": "外援团队不存在或无权访问"},
                    status_code=404,
                )

        if executor == "team":
            # Member model bindings are server-owned Session state.  A client
            # may update Team selection/configuration but cannot forge or reset
            # the materialized model/revision map.
            team_config = dict(team_config)
            team_config.pop("member_model_bindings", None)
            team_config.pop("model_binding_revision", None)
            getter = getattr(crew.session_store, "get_agent_config", None)
            existing_config = (
                getter(session_id, owner_account_id=owner)
                if callable(getter)
                else None
            )
            existing_team = (
                existing_config.get("team")
                if isinstance(existing_config, dict) and isinstance(existing_config.get("team"), dict)
                else {}
            )
            if str(existing_team.get("external_team_id") or "").strip() == external_team_id:
                for key in ("member_model_bindings", "model_binding_revision"):
                    if key in existing_team:
                        team_config[key] = existing_team[key]
            config["team"] = team_config

        workspace_id = str(payload.get("workspace_id") or raw_config.get("workspace_id") or "default")
        title = str(payload.get("title") or raw_config.get("title") or "新会话")
        ensure = getattr(crew.session_store, "ensure_session", None)
        if callable(ensure):
            try:
                ensure(session_id, workspace_id=workspace_id, title=title, owner_account_id=owner)
            except SessionOwnershipError as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)

        setter = getattr(crew.session_store, "set_agent_config", None)
        if not callable(setter):
            return JSONResponse({"ok": False, "error": "session agent config store 不可用"}, status_code=500)
        stored = setter(session_id, config, owner_account_id=owner)
        crew.agents.drop(session_id, owner_account_id=owner)
        if title and is_placeholder_title(title):
            crew.session_store.set_title(session_id, title, owner_account_id=owner)
        return JSONResponse(stored)

    @router.get("/api/session/{session_id}/status")
    async def session_status(request: Request, session_id: str) -> JSONResponse:
        owner = _owner(request)
        if not _session_owned(session_id, owner):
            return _not_found(session_id)
        return JSONResponse(dispatcher.status(session_id, owner_account_id=owner))

    @router.get("/api/session/{session_id}/debug-log")
    async def session_debug_log(request: Request, session_id: str, limit: int = 200) -> JSONResponse:
        owner = _owner(request)
        if not _session_owned(session_id, owner):
            return _not_found(session_id)
        limit = max(1, min(int(limit or 200), 1000))
        enabled = bool(crew.config.llm_trace)
        if crew.config.log_file:
            trace_path = Path(crew.config.log_file).expanduser().parent / "llm.jsonl"
        else:
            from crew.state.home import get_crew_home
            trace_path = get_crew_home() / "logs" / "llm.jsonl"
        if not enabled or not trace_path.exists():
            return JSONResponse({"enabled": enabled, "events": []})

        import collections

        # 逐行流式读取 + 有界双端队列：只保留最后 limit 条匹配，避免大文件全量入内存。
        # 同时修正会话匹配：必须 event_session == session_id 精确相等，去掉旧的
        # startswith(f"{session_id}::") 前缀匹配（它会让 /api/session/a 命中所有以 a
        # 开头的会话，造成跨会话泄露）。
        matched = collections.deque(maxlen=limit)
        try:
            with trace_path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        event = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    if (
                        str(event.get("session_id") or "") == session_id
                        and str(event.get("owner_account_id") or "") == owner
                    ):
                        matched.append(event)
        except OSError as exc:
            return JSONResponse({"enabled": enabled, "events": [], "error": str(exc)}, status_code=500)
        events = sorted(matched, key=lambda ev: ev.get("ts", 0))
        return JSONResponse({"enabled": enabled, "events": events})

    @router.get("/api/session/{session_id}/context")
    async def session_context_usage(request: Request, session_id: str) -> JSONResponse:
        owner = _owner(request)
        if not _session_owned(session_id, owner):
            return _not_found(session_id)
        return JSONResponse(
            crew.session_store.context_usage(
                session_id,
                crew.resolve_session_context_window(session_id, owner),
                owner_account_id=owner,
            )
        )

    @router.delete("/api/session/{session_id}")
    async def delete_session(request: Request, session_id: str) -> JSONResponse:
        owner = _owner(request)
        if not _session_owned(session_id, owner):
            return _not_found(session_id)
        block = _session_has_blocking_cron(crew, session_id, owner)
        if block:
            return JSONResponse(
                {"ok": False, "error": block, "code": "cron_active"},
                status_code=409,
            )
        await hook_registry.emit("session:end", {"session_id": session_id, "owner_account_id": owner})
        # 回收运行中任务、plan、摘要、memory、ACP、uploads、task 文件和停用 cron，
        # 避免会话数据库记录删除后遗留无法再定位的孤儿资源。
        await _teardown_session_resources(crew, session_id, owner)
        # 先中断可能正在运行的 Dynamic Kanban workflow
        if crew.dynamic_kanban is not None:
            try:
                crew.dynamic_kanban.interrupt(session_id, owner_account_id=owner)
            except Exception as exc:  # noqa: BLE001
                log.warning("删除会话 %s 时中断 Dynamic Kanban 失败: %s", session_id, exc)
            try:
                crew.dynamic_kanban.clear_session_workspaces(
                    session_id,
                    owner_account_id=owner,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("删除会话 %s 时清理 Dynamic Kanban 目录失败: %s", session_id, exc)
        crew.session_store.clear(session_id, owner_account_id=owner)
        return JSONResponse({"ok": True})

    @router.get("/api/tasks")
    async def tasks_query(
        request: Request,
        session_id: str | None = None,
        status: str | None = None,
        limit: int = 200,
    ) -> JSONResponse:
        try:
            owner = _owner(request)
            if session_id and not _session_owned(session_id, owner):
                return _not_found(session_id)
            getter = getattr(crew.session_store, "get_agent_config", None)
            config = getter(session_id, owner_account_id=owner) if session_id and callable(getter) else None
            if session_id and str((config or {}).get("executor") or "").lower() == "team":
                return JSONResponse(team_tasks_with_plan_projection(
                    crew,
                    session_id,
                    status,
                    limit,
                    owner_account_id=owner,
                ))
            return JSONResponse(
                crew.tasks.list_tasks(
                    session_id=session_id,
                    status=status,
                    limit=limit,
                    owner_account_id=owner,
                )
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @router.post("/api/session/{session_id}/team/recover")
    async def recover_team_node(
        request: Request,
        session_id: str,
        payload: dict | None = None,
    ) -> JSONResponse:
        """恢复 Team 阻塞节点，并由 Team Runtime 继续可执行分支。"""
        owner = _owner(request)
        if not _session_owned(session_id, owner):
            return _not_found(session_id)
        team_manager = getattr(crew, "team", None)
        recover = getattr(team_manager, "recover_plan_node", None)
        if not callable(recover):
            return JSONResponse({"ok": False, "error": "Team Runtime 不可用"}, status_code=409)
        body = payload or {}
        try:
            result = recover(
                session_id,
                node_id=str(body.get("node_id") or ""),
                action=str(body.get("action") or ""),
                replacement_assignee=str(body.get("replacement_assignee") or ""),
                owner_account_id=owner,
            )
            return JSONResponse(result)
        except (ValueError, ToolError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)

    @router.get("/api/tasks/{task_or_session_id}")
    async def task_or_legacy_session(request: Request, task_or_session_id: str) -> JSONResponse:
        """Task detail; legacy session IDs still return a task list."""
        owner = _owner(request)
        try:
            return JSONResponse(crew.tasks.get(task_or_session_id, owner_account_id=owner))
        except KeyError:
            if not _session_owned(task_or_session_id, owner):
                return _not_found(task_or_session_id)
            return JSONResponse(crew.tasks.list(task_or_session_id, owner_account_id=owner))

    @router.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(request: Request, task_id: str, payload: dict | None = None) -> JSONResponse:
        try:
            task = await crew.tasks.cancel(
                task_id,
                reason=str((payload or {}).get("reason") or "用户取消"),
                owner_account_id=_owner(request),
            )
            return JSONResponse(task)
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)

    @router.post("/api/tasks/{task_id}/wait")
    async def wait_task(request: Request, task_id: str, payload: dict | None = None) -> JSONResponse:
        try:
            task = await crew.tasks.wait(
                task_id,
                timeout=(payload or {}).get("timeout"),
                owner_account_id=_owner(request),
            )
            return JSONResponse(task)
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)

    @router.get("/api/runtime/concurrency")
    async def runtime_concurrency() -> JSONResponse:
        return JSONResponse(dispatcher.runtime_status())

    @router.get("/api/sessions/status")
    async def sessions_status(request: Request) -> JSONResponse:
        """批量返回各会话状态词（前端刷新后据此恢复左侧栏状态点）。

        live=running/queued 直接映射；live=idle 时若上轮失败则 error，否则 idle。
        """
        owner = _owner(request)
        result: dict[str, str] = {}
        for s in crew.session_store.list_sessions(owner_account_id=owner):
            sid = s["session_id"]
            st = dispatcher.status(sid, owner_account_id=owner)
            if st["live"] in ("running", "queued"):
                result[sid] = st["live"]
            elif st["last_status"] == "failed":
                result[sid] = "error"
            else:
                result[sid] = "idle"
        return JSONResponse(result)

    @router.get("/api/usage")
    async def usage(request: Request) -> JSONResponse:
        return JSONResponse(crew.session_store.total_usage(owner_account_id=_owner(request)))

    return router
