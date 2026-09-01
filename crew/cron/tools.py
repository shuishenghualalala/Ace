"""cron 定时任务工具实现。

核心逻辑收敛到 ``crew.cron`` 模块，外部 ``crew.tools.cron_tools`` 仅保留兼容薄层。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from crew.core.runctx import (
    current_owner_account_id,
    current_session_id,
    current_session_source,
    current_workspace_id,
)
from crew.cron.jobs import CronJobStore, format_bj_timestamp
from crew.tools.registry import Registry, tool_error, tool_result

if TYPE_CHECKING:
    from crew.cron.scheduler import CronService


# origin 投递只对「已注册外部 sender 的渠道」有效（见 gateway 装配的 DeliveryRouter）。
# 创建期与运行期共用这一份白名单：新增外部渠道（如 weixin）时在此加平台名，
# 并同步在 DeliveryRouter 注册对应 sender。
EXTERNAL_ORIGIN_PLATFORMS = frozenset({"feishu"})


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
                "mon": "周一",
                "tue": "周二",
                "wed": "周三",
                "thu": "周四",
                "fri": "周五",
                "sat": "周六",
                "sun": "周日",
            }
            return f"每{weekday_map.get(day.lower(), day)} {hour:02d}:{minute:02d}"
        return f"每天 {hour:02d}:{minute:02d}"
    return str(job.get("schedule") or "未识别调度")


def _serialize_job_detail(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": job["id"],
        "name": job["name"],
        "kind": job["kind"],
        "query": job.get("query", ""),
        "trigger_type": job.get("trigger_type", ""),
        "trigger_payload": job.get("trigger_payload") or {},
        "schedule_summary": _describe_schedule(job),
        "session_id": job.get("session_id", ""),
        "workspace_id": job.get("workspace_id", ""),
        "deliver": job.get("deliver", ""),
        "enabled": bool(job.get("enabled")),
        "last_status": job.get("last_status", ""),
        "next_run_at": job.get("next_run_at"),
        "next_run_at_bj": format_bj_timestamp(job.get("next_run_at")),
        "last_run_at": job.get("last_run_at"),
        "last_run_at_bj": format_bj_timestamp(job.get("last_run_at")),
        "created_at": job.get("created_at"),
        "created_at_bj": format_bj_timestamp(job.get("created_at")),
        "timezone": "Asia/Shanghai",
    }


CRON_CREATE_SCHEMA = {
    "name": "cron_create",
    "description": (
        "创建一个定时任务：到点把 query 作为一轮对话自动执行。"
        "你需要先用 LLM 理解用户输入，从自然语言中精确提取触发时间和执行内容，"
        "再把提取结果转成后端可解析的 schedule 字符串。"
        "schedule 支持中文自然语言，如 '10分钟后'、'每天早上9点'、'每周一8点'、"
        "'明天9点'、'后天下午3点'、'下周一8点'，也兼容 'every 30m' / 'in 1h' / '30m'。"
        "提取时间时必须精确到小时和分钟：'9点' 表示 09:00，'9点30分' 表示 09:30，"
        "'下午3点' 表示 15:00。不要混入当前时刻的分钟数。"
        "如果用户只说了 '明天提醒我开会' 而没给时间，应追问具体几点；"
        "不要擅自补成当前时间。"
        "deliver 控制结果投到哪里：不传或 'new_session' 表示投递到该任务的专属会话"
        "（首次触发时创建，后续触发追加其中，会有未读通知）；"
        "'local' 表示投递到 session_id 指定的当前会话；"
        "'origin' 表示回到原始渠道，仅飞书等外部渠道有效；来源是本地会话（web/桌面）时"
        "会在创建时自动改写为 new_session，不会真的投回当前会话；"
        "'feishu:chat_id' 投递到飞书。"
        "从飞书渠道创建且不传 deliver 时默认回到 origin。"
        "向用户说明投递行为时，以工具返回的 deliver 字段为准；除非 deliver 是 'local'，"
        "不要承诺「投递到当前会话」。"
        "注意：query 必须是未来触发时要直接执行的内容，不是当前这轮的确认回复。"
        "例如用户说“5分钟后提醒我开会”，应提取 schedule='5分钟后'，"
        "name='开会提醒'，query='提醒我开会'。"
        "用户说“明天九点提醒我开会”，应提取 schedule='明天9点'，name='开会提醒'，query='提醒我开会'。"
        "用户说“后天下午三点发日报”，应提取 schedule='后天下午3点'，name='日报'，query='发日报'。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "任务名称（便于识别）"},
            "schedule": {
                "type": "string",
                "description": (
                    "由 LLM 从用户自然语言中精确提取的调度描述。"
                    "示例：'10分钟后' / '每天9点' / '每周一8点' / '明天9点' / '后天下午3点' / '下周一8点' / 'every 30m'"
                ),
            },
            "query": {
                "type": "string",
                "description": "到点时要直接执行的指令文本；由 LLM 从用户输入中提取，不要写“已为您创建任务”这类创建确认文案",
            },
            "session_id": {"type": "string", "description": "任务归属会话；省略则用当前会话，仅用于权限与归属"},
            "deliver": {
                "type": "string",
                "description": "可选投递目标：new_session（默认，任务的专属会话）/ local（当前会话）/ origin（仅外部渠道）/ feishu:chat_id",
            },
        },
        "required": ["name", "schedule", "query"],
    },
}

CRON_LIST_SCHEMA = {
    "name": "cron_list",
    "description": "列出定时任务。默认返回当前用户的全部任务；仅当需要限定某个会话时才传 session_id。",
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "只看该会话的任务；省略则返回当前用户的全部任务"},
            "all": {"type": "boolean", "description": "为 true 时列出所有会话的任务（兼容旧参数，与不传 session_id 效果相同）"},
        },
    },
}

CRON_GET_SCHEMA = {
    "name": "cron_get",
    "description": "查看指定定时任务的详情和最近执行记录。",
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "任务 id"},
            "limit": {"type": "integer", "description": "返回最近多少条执行记录，默认 20"},
        },
        "required": ["id"],
    },
}

CRON_DELETE_SCHEMA = {
    "name": "cron_delete",
    "description": "删除一个定时任务。",
    "parameters": {
        "type": "object",
        "properties": {"id": {"type": "string", "description": "任务 id"}},
        "required": ["id"],
    },
}

CRON_PAUSE_SCHEMA = {
    "name": "cron_pause",
    "description": "暂停一个定时任务（不删除，可恢复）。",
    "parameters": {
        "type": "object",
        "properties": {"id": {"type": "string", "description": "任务 id"}},
        "required": ["id"],
    },
}

CRON_RESUME_SCHEMA = {
    "name": "cron_resume",
    "description": "恢复一个被暂停的定时任务。",
    "parameters": {
        "type": "object",
        "properties": {"id": {"type": "string", "description": "任务 id"}},
        "required": ["id"],
    },
}


def register_cron_tools(
    registry: Registry,
    store: CronJobStore,
    service: "CronService | None" = None,
) -> None:
    """把 cron 工具注册进 Registry（toolset=cron）。"""

    async def handle_create(args: dict[str, Any]) -> str:
        name = str(args.get("name", "")).strip()
        schedule = str(args.get("schedule", "")).strip()
        query = str(args.get("query", "")).strip()
        if not (name and schedule and query):
            return tool_error("name / schedule / query 均不能为空")
        session_id = str(args.get("session_id") or current_session_id.get() or "cron")
        owner = current_owner_account_id.get()
        origin_source = current_session_source.get() or {}
        deliver = str(args.get("deliver") or "").strip()
        deliver_note = ""
        origin_platform = str(origin_source.get("platform") or "")
        if not deliver:
            if origin_platform in EXTERNAL_ORIGIN_PLATFORMS:
                deliver = "origin"
            else:
                deliver = "new_session"
        elif deliver == "origin" and origin_platform not in EXTERNAL_ORIGIN_PLATFORMS:
            # origin 兜底（_cron_runner 在运行期也会做）提前到创建期显式化：
            # 本地会话没有外部 sender，存成 new_session 并在返回里说明，
            # 让模型读到真实投递目标，避免向用户误承诺「投递到当前会话」。
            deliver = "new_session"
            deliver_note = "来源是本地会话，origin 不可用，投递目标已改为 new_session（任务专属会话）。"
        try:
            job = store.create(
                name=name,
                schedule=schedule,
                query=query,
                session_id=session_id,
                workspace_id=current_workspace_id.get(),
                deliver=deliver,
                origin_source=origin_source,
                owner_account_id=owner,
            )
        except ValueError as exc:
            return tool_error(str(exc))

        if service is not None:
            service.sync_job(str(job["id"]), owner_account_id=owner)
            refreshed = store.get(str(job["id"]), owner_account_id=owner)
            if refreshed is not None:
                job = refreshed

        return tool_result(
            id=job["id"],
            name=job["name"],
            kind=job["kind"],
            trigger_type=job.get("trigger_type", ""),
            session_id=job["session_id"],
            deliver=job.get("deliver", ""),
            next_run_at=job["next_run_at"],
            next_run_at_bj=format_bj_timestamp(job.get("next_run_at")),
            timezone="Asia/Shanghai",
            **({"note": deliver_note} if deliver_note else {}),
        )

    async def handle_list(args: dict[str, Any]) -> str:
        owner = current_owner_account_id.get()
        if args.get("all"):
            jobs = store.list(owner_account_id=owner)
        elif "session_id" in args:
            session_id = str(args["session_id"] or "").strip()
            if session_id:
                jobs = store.list(session_id=session_id, owner_account_id=owner)
            else:
                jobs = store.list(owner_account_id=owner)
        else:
            jobs = store.list(owner_account_id=owner)
        brief = [
            {
                "id": j["id"],
                "name": j["name"],
                "kind": j["kind"],
                "trigger_type": j.get("trigger_type", ""),
                "enabled": bool(j["enabled"]),
                "next_run_at": j["next_run_at"],
                "next_run_at_bj": format_bj_timestamp(j.get("next_run_at")),
                "last_status": j["last_status"],
                "last_run_at_bj": format_bj_timestamp(j.get("last_run_at")),
            }
            for j in jobs
        ]
        return tool_result(jobs=brief, count=len(brief), timezone="Asia/Shanghai")

    async def handle_get(args: dict[str, Any]) -> str:
        job_id = str(args.get("id", "")).strip()
        if not job_id:
            return tool_error("id 不能为空")
        owner = current_owner_account_id.get()
        job = store.get(job_id, owner_account_id=owner)
        if job is None:
            return tool_error(f"任务不存在: {job_id}")
        limit = int(args.get("limit") or 20)
        if limit < 1:
            limit = 20
        runs = store.get_job_runs(job_id, limit=limit)
        summary = store.get_job_run_summary(job_id)
        return tool_result(
            job=_serialize_job_detail(job),
            runs=runs,
            run_summary=summary,
            timezone="Asia/Shanghai",
        )

    async def handle_delete(args: dict[str, Any]) -> str:
        job_id = str(args.get("id", "")).strip()
        owner = current_owner_account_id.get()
        ok = store.delete(job_id, owner_account_id=owner)
        if ok and service is not None:
            service.sync_job(job_id, owner_account_id=owner)
        return tool_result(deleted=ok, id=job_id) if ok else tool_error(f"任务不存在: {job_id}")

    async def handle_pause(args: dict[str, Any]) -> str:
        job_id = str(args.get("id", "")).strip()
        ok = store.set_enabled(job_id, False, owner_account_id=current_owner_account_id.get())
        if ok and service is not None:
            service.sync_job(job_id, owner_account_id=current_owner_account_id.get())
        return tool_result(paused=ok, id=job_id) if ok else tool_error(f"任务不存在: {job_id}")

    async def handle_resume(args: dict[str, Any]) -> str:
        job_id = str(args.get("id", "")).strip()
        owner = current_owner_account_id.get()
        ok = store.set_enabled(job_id, True, owner_account_id=owner)
        if ok and service is not None:
            service.sync_job(job_id, owner_account_id=current_owner_account_id.get())
        refreshed = store.get(job_id, owner_account_id=owner) if ok else None
        if ok:
            next_run_at = refreshed["next_run_at"] if refreshed else 0
            return tool_result(
                resumed=True,
                id=job_id,
                next_run_at=next_run_at,
                next_run_at_bj=format_bj_timestamp(next_run_at),
                timezone="Asia/Shanghai",
            )
        return tool_error(f"任务不存在: {job_id}")

    specs = [
        (CRON_CREATE_SCHEMA, handle_create, "创建定时任务", "创建定时任务 {name}"),
        (CRON_LIST_SCHEMA, handle_list, "列出定时任务", "列出定时任务"),
        (CRON_GET_SCHEMA, handle_get, "查看定时任务", "查看定时任务 {job_id}"),
        (CRON_DELETE_SCHEMA, handle_delete, "删除定时任务", "删除定时任务 {job_id}"),
        (CRON_PAUSE_SCHEMA, handle_pause, "暂停定时任务", "暂停定时任务 {job_id}"),
        (CRON_RESUME_SCHEMA, handle_resume, "恢复定时任务", "恢复定时任务 {job_id}"),
    ]
    for schema, handler, display_name, ui_label_template in specs:
        registry.register(
            name=schema["name"],
            toolset="cron",
            schema=schema,
            handler=handler,
            is_async=True,
            override=True,
            display_name=display_name,
            ui_label_template=ui_label_template,
            should_defer=True,
            search_hint="cron schedule recurring automation reminder job list pause resume delete",
        )
