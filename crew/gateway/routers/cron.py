"""定时任务 HTTP 入口：列表 / 创建 / 启停 / 触发 / 人工重试 / 删除 / 统计。

数据落 SQLite（crew_data/crew.db），重启保留。run-now 创建独立 manual Fire，
不改写周期任务的 next_run_at。启停 / 创建 / 删除
均通过 cron_service.sync_job() 与 APScheduler 同步。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from crew.gateway.auth import account_from_request
from crew.gateway.helpers import safe_public_error
from crew.gateway.platform_registry import platform_registry
from crew.cron.jobs import format_bj_timestamp
from crew.state.logging import get_logger

log = get_logger("gateway.cron")

# Keep background Fire tasks strongly referenced until their done callback runs.
_background_tasks: set[asyncio.Task] = set()


# ---------------------------------------------------------------------------
# 序列化与展示
# ---------------------------------------------------------------------------

def _describe_interval(seconds: int) -> str:
    if seconds % 86400 == 0:
        days = seconds // 86400
        return f"每{days}天执行一次" if days > 1 else "每天执行一次"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"每{hours}小时执行一次" if hours > 1 else "每小时执行一次"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"每{minutes}分钟执行一次" if minutes > 1 else "每分钟执行一次"
    return f"每{seconds}秒执行一次"


def _describe_schedule(job: dict[str, Any]) -> str:
    trigger_type = str(job.get("trigger_type") or "")
    payload = job.get("trigger_payload") or {}
    if trigger_type == "date":
        return f"单次执行 · {format_bj_timestamp(job.get('next_run_at')) or '待调度'}"
    if trigger_type == "interval":
        seconds = int(payload.get("seconds") or 0)
        return _describe_interval(seconds) if seconds > 0 else "固定间隔"
    if trigger_type == "cron":
        hour = int(payload.get("hour", 0))
        minute = int(payload.get("minute", 0))
        day = str(payload.get("day_of_week") or "").strip()
        if day:
            weekday_map = {
                "mon": "周一", "tue": "周二", "wed": "周三", "thu": "周四",
                "fri": "周五", "sat": "周六", "sun": "周日",
            }
            return f"每{weekday_map.get(day.lower(), day)} {hour:02d}:{minute:02d}"
        return f"每天 {hour:02d}:{minute:02d}"
    return str(job.get("schedule") or "未识别调度")


def _serialize_job(job: dict[str, Any]) -> dict[str, Any]:
    """cron 任务行 → 前端友好 payload（含 BJ 时间戳、调度说明）。"""
    next_run_at = job.get("next_run_at")
    last_run_at = job.get("last_run_at")
    created_at = job.get("created_at")
    return {
        "id": job.get("id", ""),
        "name": job.get("name", ""),
        "kind": job.get("kind", "once"),
        "interval_seconds": float(job.get("interval_seconds") or 0),
        "trigger_type": job.get("trigger_type", ""),
        "trigger_payload": job.get("trigger_payload") or {},
        "schedule": job.get("schedule", ""),
        "schedule_summary": _describe_schedule(job),
        "query": job.get("query", ""),
        "session_id": job.get("session_id", ""),
        "workspace_id": job.get("workspace_id", "default"),
        "deliver": job.get("deliver", ""),
        "enabled": bool(job.get("enabled")),
        "last_status": job.get("last_status", ""),
        "next_run_at": float(next_run_at or 0),
        "next_run_at_bj": format_bj_timestamp(next_run_at),
        "last_run_at": float(last_run_at or 0),
        "last_run_at_bj": format_bj_timestamp(last_run_at),
        "created_at": float(created_at or 0),
        "created_at_bj": format_bj_timestamp(created_at),
        "timezone": "Asia/Shanghai",
    }


# 接受前端仍可能发送的字段别名。
def _serialize(job: dict[str, Any]) -> dict[str, Any]:
    return _serialize_job(job)


def _compute_cron_stats(jobs: list[dict[str, Any]], now: float) -> dict[str, int]:
    """从前端任务列表计算 cron KPI 统计（interval/cron 统一视为周期任务）。"""
    total = len(jobs)
    enabled = sum(1 for j in jobs if j.get("enabled"))
    disabled = total - enabled
    interval = sum(1 for j in jobs if j.get("kind") in ("interval", "cron"))
    once = sum(1 for j in jobs if j.get("kind") == "once")
    failed_recent = sum(
        1 for j in jobs
        if str(j.get("last_status", "")).startswith("failed")
        and j.get("last_run_at", 0) > 0
        and (now - float(j["last_run_at"])) < 86400
    )
    upcoming = sum(
        1 for j in jobs
        if j.get("enabled") and 0 < float(j.get("next_run_at") or 0) - now < 60
    )
    return {
        "total": total,
        "enabled": enabled,
        "disabled": disabled,
        "interval": interval,
        "once": once,
        "failed_recent": failed_recent,
        "upcoming_60s": upcoming,
    }


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

def _workspace_for_session(crew, session_id: str, owner_account_id: str) -> str:
    if not session_id:
        return "default"
    for row in crew.session_store.list_sessions(owner_account_id=owner_account_id):
        if row.get("session_id") == session_id:
            return str(row.get("workspace_id") or "default")
    return "default"


def create_cron_router(crew) -> APIRouter:
    router = APIRouter()

    def _store():
        store = getattr(crew, "cron_store", None)
        if store is None:
            raise RuntimeError("cron store 未初始化")
        return store

    def _service():
        service = getattr(crew, "cron_service", None)
        if service is None or not service.is_running:
            return None
        return service

    def _sync_service(job_id: str) -> None:
        """把单个 job 的最新 trigger 同步到 APScheduler。"""
        svc = _service()
        if svc is not None:
            try:
                svc.sync_job(job_id)
            except Exception:  # noqa: BLE001 — sync_job 内部混用 sqlite/trigger 解析/APScheduler，失败面未知；写路由仅记录不阻断
                log.exception("cron service.sync_job 失败 id=%s", job_id)

    def _owner(request: Request) -> str:
        return account_from_request(request).owner_account_id

    def _session_owned(session_id: str, owner: str) -> bool:
        belongs = getattr(crew.session_store, "session_belongs_to", None)
        return bool(callable(belongs) and belongs(session_id, owner))

    @router.get("/api/cron/jobs")
    async def cron_list_jobs(
        request: Request,
        session_id: str | None = None,
        kind: str | None = None,
        enabled: str | None = None,
    ) -> JSONResponse:
        """列出定时任务。可按 session / kind(interval|once) / enabled(1|0) 过滤。"""
        try:
            store = _store()
        except RuntimeError:
            return JSONResponse({"ok": True, "jobs": [], "count": 0, "timezone": "Asia/Shanghai"})
        owner = _owner(request)
        if session_id and not _session_owned(session_id, owner):
            return JSONResponse({"ok": True, "jobs": [], "count": 0, "timezone": "Asia/Shanghai"})
        jobs = store.list(session_id=session_id, owner_account_id=owner)
        if kind in ("interval", "once", "cron"):
            jobs = [j for j in jobs if j.get("kind") == kind]
        if enabled in ("1", "0", "true", "false"):
            want = enabled in ("1", "true")
            jobs = [j for j in jobs if bool(j.get("enabled")) == want]
        return JSONResponse({
            "ok": True,
            "jobs": [_serialize_job(j) for j in jobs],
            "count": len(jobs),
            "timezone": "Asia/Shanghai",
        })

    @router.get("/api/cron/delivery-targets")
    async def cron_delivery_targets(request: Request) -> JSONResponse:
        """返回当前用户可用的 cron 投递目标列表（新会话 / 当前会话 / 已连接渠道）。"""
        targets: list[dict[str, Any]] = [
            {"id": "new_session", "label": "新会话", "platform": "local"},
            {"id": "local", "label": "当前会话", "platform": "local"},
        ]
        for entry in platform_registry.all_entries():
            try:
                raw_config = crew.config.channel_config(entry.name)
            except Exception:  # noqa: BLE001
                raw_config = {}
            if not isinstance(raw_config, dict):
                raw_config = {}
            cfg = entry.build_config(raw_config)
            if not cfg.enabled or not entry.connected(cfg):
                continue
            chat_id = None
            if cfg.home_channel is not None:
                chat_id = cfg.home_channel.chat_id
            if chat_id:
                targets.append({
                    "id": f"{entry.name}:{chat_id}",
                    "label": entry.label or entry.name,
                    "platform": entry.name,
                })
            else:
                targets.append({
                    "id": entry.name,
                    "label": entry.label or entry.name,
                    "platform": entry.name,
                })
        return JSONResponse({"ok": True, "targets": targets})

    @router.get("/api/cron/jobs/{job_id}")
    async def cron_get_job(request: Request, job_id: str, limit: int = 20) -> JSONResponse:
        """查看单个定时任务详情及最近执行记录。"""
        try:
            store = _store()
        except RuntimeError as exc:
            return JSONResponse({"ok": False, "error": safe_public_error(exc, "定时任务服务不可用")}, status_code=503)
        job = store.get(job_id, owner_account_id=_owner(request))
        if job is None:
            return JSONResponse({"ok": False, "error": f"任务不存在: {job_id}"}, status_code=404)
        runs = store.get_job_runs(job_id, limit=max(1, limit))
        summary = store.get_job_run_summary(job_id)
        return JSONResponse({
            "ok": True,
            "job": _serialize_job(job),
            "runs": runs,
            "run_summary": summary,
            "timezone": "Asia/Shanghai",
        })

    @router.get("/api/cron/stats")
    async def cron_stats(request: Request) -> JSONResponse:
        """KPI 数据源：前端用做首页 4 张 KPI 卡。"""
        try:
            store = _store()
            jobs = store.list(owner_account_id=_owner(request))
        except RuntimeError:
            return JSONResponse({
                "ok": True, "total": 0, "enabled": 0, "disabled": 0,
                "interval": 0, "once": 0, "failed_recent": 0, "upcoming_60s": 0,
            })
        stats = _compute_cron_stats(jobs, time.time())
        return JSONResponse({"ok": True, **stats})

    @router.post("/api/cron/jobs")
    async def cron_create_job(request: Request, payload: dict) -> JSONResponse:
        try:
            store = _store()
        except RuntimeError as exc:
            return JSONResponse({"ok": False, "error": safe_public_error(exc, "定时任务服务不可用")}, status_code=503)
        name = str(payload.get("name") or "").strip()
        schedule = str(payload.get("schedule") or "").strip()
        query = str(payload.get("query") or "").strip()
        session_id = str(payload.get("session_id") or "").strip()
        if not (name and schedule and query):
            return JSONResponse(
                {"ok": False, "error": "name / schedule / query 均不能为空"},
                status_code=400,
            )
        if not session_id:
            return JSONResponse({"ok": False, "error": "session_id 不能为空"}, status_code=400)
        owner = _owner(request)
        if not _session_owned(session_id, owner):
            # 前端「新会话」是草稿态：发首条消息前后端 sessions 表无记录，直接 404
            # 会把「在空会话里建定时任务」误杀。为这类会话补占位行（INSERT OR IGNORE，
            # 已存在不覆盖），补完仍不属于本账号才判定会话不存在。
            ensure = getattr(crew.session_store, "ensure_session", None)
            if callable(ensure):
                try:
                    ensure(
                        session_id,
                        workspace_id=str(payload.get("workspace_id") or "").strip() or "default",
                        owner_account_id=owner,
                    )
                except Exception:  # noqa: BLE001 — 占位失败走下面复查,仍 404
                    log.exception("cron 创建前补会话占位失败 sid=%s", session_id)
        if not _session_owned(session_id, owner):
            return JSONResponse({"ok": False, "error": f"会话不存在: {session_id}"}, status_code=404)
        try:
            job = store.create(
                name=name,
                schedule=schedule,
                query=query,
                session_id=session_id,
                workspace_id=str(payload.get("workspace_id") or _workspace_for_session(crew, session_id, owner)),
                deliver=str(payload.get("deliver") or "").strip(),
                owner_account_id=owner,
            )
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": safe_public_error(exc, "定时任务请求无效")}, status_code=400)
        _sync_service(str(job["id"]))
        return JSONResponse(_serialize_job(job), status_code=201)

    @router.post("/api/cron/jobs/{job_id}/pause")
    async def cron_pause_job(request: Request, job_id: str) -> JSONResponse:
        """暂停一个 cron 任务，并把对应 trigger 从 APScheduler 移除。"""
        try:
            store = _store()
        except RuntimeError as exc:
            return JSONResponse({"ok": False, "error": safe_public_error(exc, "定时任务服务不可用")}, status_code=503)
        owner = _owner(request)
        job = store.get(job_id, owner_account_id=owner)
        if job is None:
            return JSONResponse({"ok": False, "error": f"任务不存在: {job_id}"}, status_code=404)
        store.set_enabled(job_id, False, owner_account_id=owner)
        _sync_service(job_id)
        refreshed = store.get(job_id, owner_account_id=owner) or job
        return JSONResponse(_serialize_job(refreshed))

    @router.post("/api/cron/jobs/{job_id}/resume")
    async def cron_resume_job(request: Request, job_id: str) -> JSONResponse:
        """恢复一个被暂停的 cron 任务，并重新挂到 APScheduler。"""
        try:
            store = _store()
        except RuntimeError as exc:
            return JSONResponse({"ok": False, "error": safe_public_error(exc, "定时任务服务不可用")}, status_code=503)
        owner = _owner(request)
        job = store.get(job_id, owner_account_id=owner)
        if job is None:
            return JSONResponse({"ok": False, "error": f"任务不存在: {job_id}"}, status_code=404)
        store.set_enabled(job_id, True, owner_account_id=owner)
        _sync_service(job_id)
        refreshed = store.get(job_id, owner_account_id=owner) or job
        return JSONResponse(_serialize_job(refreshed))

    @router.delete("/api/cron/jobs/{job_id}")
    async def cron_delete_job(request: Request, job_id: str) -> JSONResponse:
        try:
            store = _store()
        except RuntimeError as exc:
            return JSONResponse({"ok": False, "error": safe_public_error(exc, "定时任务服务不可用")}, status_code=503)
        owner = _owner(request)
        job = store.get(job_id, owner_account_id=owner)
        if job is None:
            return JSONResponse({"ok": False, "error": f"任务不存在: {job_id}"}, status_code=404)
        store.delete(job_id, owner_account_id=owner)
        _sync_service(job_id)
        return JSONResponse({"ok": True, "id": job_id})

    @router.post("/api/cron/jobs/{job_id}/run")
    async def cron_run_job(request: Request, job_id: str) -> JSONResponse:
        """Create one independent manual Fire and execute it in the background."""
        try:
            store = _store()
        except RuntimeError as exc:
            return JSONResponse({"ok": False, "error": safe_public_error(exc, "定时任务服务不可用")}, status_code=503)
        owner = _owner(request)
        job = store.get(job_id, owner_account_id=owner)
        if job is None:
            return JSONResponse({"ok": False, "error": f"任务不存在: {job_id}"}, status_code=404)
        cron_service = _service()
        if cron_service is None:
            return JSONResponse({"ok": False, "error": "CronService 未启用"}, status_code=503)

        async def _kick() -> None:
            try:
                await cron_service.run_now(job_id, owner_account_id=owner)
            except Exception:  # noqa: BLE001 — 后台任务顶层统一记录，Fire 内部保留执行状态
                log.exception("cron manual Fire 执行失败 id=%s", job_id)

        task = asyncio.create_task(_kick())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        return JSONResponse({"ok": True, "job": _serialize_job(job)})

    @router.post("/api/cron/fires/{fire_id}/retry")
    async def cron_retry_fire(request: Request, fire_id: int) -> JSONResponse:
        """Create a linked Fire; never mutate or replay the source Fire."""

        try:
            store = _store()
        except RuntimeError as exc:
            return JSONResponse({"ok": False, "error": safe_public_error(exc, "定时任务服务不可用")}, status_code=503)
        owner = _owner(request)
        source = store.get_fire(fire_id, owner_account_id=owner)
        if source is None:
            return JSONResponse(
                {"ok": False, "error": f"Fire 不存在: {fire_id}"},
                status_code=404,
            )
        if str(source.get("status") or "") not in {
            "failed",
            "abandoned",
            "cancelled_by_logout",
        }:
            return JSONResponse(
                {"ok": False, "error": "只有失败、遗弃或因退出取消的 Fire 可人工重试"},
                status_code=409,
            )
        cron_service = _service()
        if cron_service is None:
            return JSONResponse({"ok": False, "error": "CronService 未启用"}, status_code=503)
        if cron_service.mounted_owner != owner:
            return JSONResponse(
                {"ok": False, "error": "当前账号未挂载到 CronService"},
                status_code=409,
            )

        async def _retry() -> None:
            try:
                await cron_service.retry_fire(fire_id, owner_account_id=owner)
            except Exception:  # noqa: BLE001 - Fire 内部记录执行终态
                log.exception("cron retry Fire 执行失败 source_fire_id=%s", fire_id)

        task = asyncio.create_task(_retry())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        return JSONResponse(
            {"ok": True, "source_fire_id": fire_id},
            status_code=202,
        )

    return router
