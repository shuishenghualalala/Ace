"""cron 任务存储 + 自然语言 schedule 解析。

使用 SQLite 持久化任务定义，并把 schedule 统一解析为 APScheduler 可消费的
trigger 规格。支持：

- 一次性：``in 30s`` / ``30m`` / ``10分钟后``
- 间隔：``every 30m`` / ``每10分钟`` / ``每小时``
- 固定时点：``每天9点`` / ``每天早上9点30分`` / ``每周一8点``
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
import uuid
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from crew.state.logging import get_logger
from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite

log = get_logger("cron")
BJ_TZ = ZoneInfo("Asia/Shanghai")


# ---------------------------------------------------------------------------
# schedule 解析
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(
    r"^(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$"
)
_ZH_DURATION_RE = re.compile(r"^(\d+)\s*(秒钟|秒|分钟|分|小时|时|天)$")
_MULTIPLIERS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_ZH_MULTIPLIERS = {
    "秒钟": 1,
    "秒": 1,
    "分钟": 60,
    "分": 60,
    "小时": 3600,
    "时": 3600,
    "天": 86400,
}
_TIME_RE = re.compile(r"^(?:(早上|上午|中午|下午|晚上))?\s*(\d{1,2})\s*点(?:\s*(\d{1,2})\s*分?)?$")
_TIME_COLON_RE = re.compile(r"^(?:(早上|上午|中午|下午|晚上))?\s*(\d{1,2})\s*:\s*(\d{1,2})$")
_DAILY_RE = re.compile(r"^每(?:天|日)(?P<time>.*)$")
_WEEKLY_RE = re.compile(r"^每周(?P<day>[一二三四五六日天1234567])(?P<time>.*)$")
_ZH_INTERVAL_RE = re.compile(r"^每(?:隔)?\s*(\d+\s*(?:秒钟|秒|分钟|分|小时|时|天))$")
_TOMORROW_RE = re.compile(r"^明天(?P<time>.*)$")
_DAY_AFTER_TOMORROW_RE = re.compile(r"^后天(?P<time>.*)$")
_NEXT_WEEKDAY_RE = re.compile(r"^下周(?P<day>[一二三四五六日天1234567])(?P<time>.*)$")
_CRON_EXPR_PART_RE = re.compile(r"^[\d\*\-,/]+$")
_WEEKDAY_MAP = {
    "1": "mon",
    "一": "mon",
    "2": "tue",
    "二": "tue",
    "3": "wed",
    "三": "wed",
    "4": "thu",
    "四": "thu",
    "5": "fri",
    "五": "fri",
    "6": "sat",
    "六": "sat",
    "7": "sun",
    "日": "sun",
    "天": "sun",
}


def _local_now() -> datetime:
    return datetime.now(BJ_TZ)


def _ensure_datetime(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=BJ_TZ)
    return dt.astimezone(BJ_TZ)


def format_bj_datetime(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.astimezone(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S CST")


def format_bj_timestamp(ts: float | int | None) -> str:
    if not ts:
        return ""
    return format_bj_datetime(datetime.fromtimestamp(float(ts), tz=BJ_TZ))


def parse_duration(s: str) -> int:
    """把英文/中文时长字符串解析成秒。"""
    s = s.strip().lower()
    match = _DURATION_RE.match(s)
    if match:
        value = int(match.group(1))
        unit = match.group(2)[0]
        return value * _MULTIPLIERS[unit]

    zh_match = _ZH_DURATION_RE.match(s)
    if zh_match:
        value = int(zh_match.group(1))
        unit = zh_match.group(2)
        return value * _ZH_MULTIPLIERS[unit]

    raise ValueError(f"无法解析时长 '{s}'，用法如 '30s' / '5m' / '2h' / '10分钟'")


def _normalize_hour(hour: int, period: str | None) -> int:
    if period in {"下午", "晚上"} and hour < 12:
        return hour + 12
    if period == "中午" and hour < 11:
        return hour + 12
    if period in {"早上", "上午"} and hour == 12:
        return 0
    return hour


def _parse_time_fragment(text: str) -> tuple[int, int]:
    stripped = text.strip()
    if not stripped:
        return 0, 0
    match = _TIME_RE.match(stripped)
    colon_match = _TIME_COLON_RE.match(stripped)
    if match:
        period = match.group(1)
        hour = _normalize_hour(int(match.group(2)), period)
        minute = int(match.group(3) or 0)
    elif colon_match:
        period = colon_match.group(1)
        hour = _normalize_hour(int(colon_match.group(2)), period)
        minute = int(colon_match.group(3) or 0)
    else:
        raise ValueError(f"无法解析时间点 '{text}'，示例：'9点' / '早上9点30分' / '09:30'")
    if hour > 23 or minute > 59:
        raise ValueError(f"时间点超出范围: '{text}'")
    return hour, minute


def _make_interval_spec(seconds: int, now: datetime) -> dict[str, Any]:
    if seconds <= 0:
        raise ValueError("间隔必须大于 0")
    start_at = now + timedelta(seconds=seconds)
    return {
        "kind": "interval",
        "trigger_type": "interval",
        "trigger_payload": {"seconds": seconds, "start_at": start_at.isoformat()},
        "interval_seconds": seconds,
    }


def _make_once_spec(seconds: int, now: datetime) -> dict[str, Any]:
    if seconds < 0:
        raise ValueError("延时时长不能小于 0")
    run_at = now + timedelta(seconds=seconds)
    return {
        "kind": "once",
        "trigger_type": "date",
        "trigger_payload": {"run_at": run_at.isoformat()},
        "delay_seconds": seconds,
    }


def _make_daily_spec(time_text: str) -> dict[str, Any]:
    hour, minute = _parse_time_fragment(time_text)
    return {
        "kind": "cron",
        "trigger_type": "cron",
        "trigger_payload": {"hour": hour, "minute": minute},
    }


def _make_weekly_spec(day: str, time_text: str) -> dict[str, Any]:
    hour, minute = _parse_time_fragment(time_text)
    return {
        "kind": "cron",
        "trigger_type": "cron",
        "trigger_payload": {"day_of_week": _WEEKDAY_MAP[day], "hour": hour, "minute": minute},
    }


def _make_date_spec(run_at: datetime) -> dict[str, Any]:
    return {
        "kind": "once",
        "trigger_type": "date",
        "trigger_payload": {"run_at": run_at.isoformat()},
    }


def _next_weekday(target_weekday: int, now: datetime) -> datetime:
    """返回 now 之后下一个 target_weekday 对应的日期（0=周一, 6=周日）。"""
    # Python weekday(): 周一=0 ... 周日=6
    days_ahead = (target_weekday - now.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return now + timedelta(days=days_ahead)


_WEEKDAY_NUMBER = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}


def _zh_day_number(day: str) -> int:
    """把中文/数字星期转换为 Python weekday() 数字（0=周一）。"""
    apsched_name = _WEEKDAY_MAP[day]
    return _WEEKDAY_NUMBER[apsched_name]


def _resolve_relative_datetime(
    base_date: datetime,
    time_text: str,
    *,
    default_hour: int = 9,
    default_minute: int = 0,
) -> datetime:
    """把相对日期（已计算好日期部分）和时间片段组合成完整 datetime。

    若 time_text 为空，则使用 default_hour:default_minute。
    """
    stripped = time_text.strip()
    if stripped:
        hour, minute = _parse_time_fragment(stripped)
    else:
        hour, minute = default_hour, default_minute
    return base_date.replace(hour=hour, minute=minute, second=0, microsecond=0)


def build_trigger(trigger_type: str, payload: dict[str, Any]) -> BaseTrigger:
    """把持久化 trigger 规格恢复成 APScheduler trigger。"""
    if trigger_type == "date":
        return DateTrigger(run_date=_ensure_datetime(str(payload["run_at"])), timezone=BJ_TZ)
    if trigger_type == "interval":
        return IntervalTrigger(
            seconds=int(payload["seconds"]),
            start_date=_ensure_datetime(str(payload["start_at"])),
            timezone=BJ_TZ,
        )
    if trigger_type == "cron":
        return CronTrigger(
            day_of_week=payload.get("day_of_week"),
            hour=int(payload.get("hour", 0)),
            minute=int(payload.get("minute", 0)),
            timezone=BJ_TZ,
        )
    raise ValueError(f"不支持的 trigger_type: {trigger_type}")


def get_next_fire_at(
    trigger_type: str,
    payload: dict[str, Any],
    *,
    now: datetime | None = None,
    previous_fire_time: datetime | None = None,
) -> datetime | None:
    trigger = build_trigger(trigger_type, payload)
    now = now or _local_now()
    if trigger_type == "date" and previous_fire_time is None:
        run_at = _ensure_datetime(str(payload["run_at"]))
        return run_at if run_at >= now else None
    return trigger.get_next_fire_time(previous_fire_time, now)


def get_next_future_fire_at(
    trigger_type: str,
    payload: dict[str, Any],
    *,
    current_fire_time: datetime,
    now: datetime,
) -> datetime | None:
    """Return the first occurrence strictly after ``now`` for a claimed Fire."""

    if trigger_type == "date":
        return None
    if trigger_type == "interval":
        seconds = int(payload["seconds"])
        elapsed = max(0.0, (now - current_fire_time).total_seconds())
        steps = int(elapsed // seconds) + 1
        return current_fire_time + timedelta(seconds=steps * seconds)
    candidate = get_next_fire_at(
        trigger_type,
        payload,
        now=now,
        previous_fire_time=current_fire_time,
    )
    if candidate is not None and candidate <= now:
        raise ValueError("Cron trigger 未能计算出严格未来的下一次执行时间")
    return candidate


def get_first_future_fire_at(
    trigger_type: str,
    payload: dict[str, Any],
    *,
    now: datetime,
) -> datetime | None:
    """Return the first occurrence strictly after ``now`` without creating a Fire."""

    if trigger_type == "date":
        run_at = _ensure_datetime(str(payload["run_at"]))
        return run_at if run_at > now else None
    trigger = build_trigger(trigger_type, payload)
    candidate = trigger.get_next_fire_time(None, now)
    if candidate is not None and candidate <= now:
        candidate = trigger.get_next_fire_time(candidate, now)
    if candidate is not None and candidate <= now:
        raise ValueError("Cron trigger 未能计算出严格未来的下一次执行时间")
    return candidate


def parse_schedule(schedule: str, *, now: datetime | None = None) -> dict[str, Any]:
    """解析 schedule 字符串为标准化 trigger 规格。"""
    now = now or _local_now()
    schedule = schedule.strip()
    lower = schedule.lower()

    # 英文间隔
    if lower.startswith("every "):
        return _make_interval_spec(parse_duration(schedule[6:].strip()), now)

    # 英文一次性
    if lower.startswith("in "):
        return _make_once_spec(parse_duration(schedule[3:].strip()), now)

    # 常见中文一次性
    if schedule.endswith("之后"):
        return _make_once_spec(parse_duration(schedule[:-2].strip()), now)
    if schedule.endswith("后"):
        return _make_once_spec(parse_duration(schedule[:-1].strip()), now)

    # 常见中文间隔
    if schedule in {"每小时", "每1小时"}:
        return _make_interval_spec(3600, now)
    if schedule in {"每天", "每日"}:
        return _make_interval_spec(86400, now)
    zh_interval = _ZH_INTERVAL_RE.match(schedule)
    if zh_interval:
        return _make_interval_spec(parse_duration(zh_interval.group(1)), now)

    # 固定时点
    daily = _DAILY_RE.match(schedule)
    if daily and daily.group("time").strip():
        return _make_daily_spec(daily.group("time"))

    weekly = _WEEKLY_RE.match(schedule)
    if weekly:
        return _make_weekly_spec(weekly.group("day"), weekly.group("time"))

    # 相对日期：明天 / 后天 / 下周X
    tomorrow = _TOMORROW_RE.match(schedule)
    if tomorrow:
        run_at = _resolve_relative_datetime(now + timedelta(days=1), tomorrow.group("time"))
        return _make_date_spec(run_at)

    day_after_tomorrow = _DAY_AFTER_TOMORROW_RE.match(schedule)
    if day_after_tomorrow:
        run_at = _resolve_relative_datetime(now + timedelta(days=2), day_after_tomorrow.group("time"))
        return _make_date_spec(run_at)

    next_weekday = _NEXT_WEEKDAY_RE.match(schedule)
    if next_weekday:
        target_weekday = _zh_day_number(next_weekday.group("day"))
        run_at = _resolve_relative_datetime(_next_weekday(target_weekday, now), next_weekday.group("time"))
        return _make_date_spec(run_at)

    # cron 表达式暂不直接开放
    parts = schedule.split()
    if len(parts) >= 5 and all(_CRON_EXPR_PART_RE.match(p) for p in parts[:5]):
        raise ValueError("暂不支持直接输入 cron 表达式，请用 '每周一8点'、'每天9点'、'明天9点' 或 'every 30m'")

    # 英文/简化裸时长，视为一次性
    return _make_once_spec(parse_duration(schedule), now)


# ---------------------------------------------------------------------------
# SQLite 任务存储
# ---------------------------------------------------------------------------

class CronJobStore:
    """cron 任务的持久化存储。表：cron_jobs。"""

    def __init__(self, db_path: str = "crew_data/crew.db", *, wal_enabled: bool = True) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = connect_sqlite(self._path, wal_enabled=wal_enabled, row_factory=True)
        self._writer = SQLiteWriteHelper(self._conn, self._lock)
        self._writer.execute(self._init_schema)

    def close(self) -> None:
        """关闭底层 SQLite 连接（WAL 模式下每库持有多个 fd，必须显式释放）。"""
        with self._lock:
            self._conn.close()

    def _init_schema(self, conn) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cron_jobs (
                id               TEXT PRIMARY KEY,
                owner_account_id TEXT NOT NULL DEFAULT '',
                name             TEXT NOT NULL DEFAULT '',
                kind             TEXT NOT NULL,
                interval_seconds REAL NOT NULL DEFAULT 0,
                query            TEXT NOT NULL DEFAULT '',
                session_id       TEXT NOT NULL DEFAULT '',
                workspace_id     TEXT NOT NULL DEFAULT 'default',
                enabled          INTEGER NOT NULL DEFAULT 1,
                next_run_at      REAL NOT NULL,
                last_run_at      REAL NOT NULL DEFAULT 0,
                last_status      TEXT NOT NULL DEFAULT '',
                deliver          TEXT NOT NULL DEFAULT '',
                origin_source    TEXT NOT NULL DEFAULT '{}',
                trigger_type     TEXT NOT NULL DEFAULT '',
                trigger_payload  TEXT NOT NULL DEFAULT '{}',
                created_at       REAL NOT NULL
            )
            """
        )
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(cron_jobs)").fetchall()}
        if "owner_account_id" not in cols:
            self._conn.execute("ALTER TABLE cron_jobs ADD COLUMN owner_account_id TEXT NOT NULL DEFAULT ''")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cron_jobs_owner_created "
            "ON cron_jobs(owner_account_id, created_at DESC)"
        )
        if "deliver" not in cols:
            self._conn.execute("ALTER TABLE cron_jobs ADD COLUMN deliver TEXT NOT NULL DEFAULT ''")
        if "origin_source" not in cols:
            self._conn.execute("ALTER TABLE cron_jobs ADD COLUMN origin_source TEXT NOT NULL DEFAULT '{}'")
        if "trigger_type" not in cols:
            self._conn.execute("ALTER TABLE cron_jobs ADD COLUMN trigger_type TEXT NOT NULL DEFAULT ''")
        if "trigger_payload" not in cols:
            self._conn.execute("ALTER TABLE cron_jobs ADD COLUMN trigger_payload TEXT NOT NULL DEFAULT '{}'")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cron_job_runs (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id         TEXT NOT NULL,
                owner_account_id TEXT NOT NULL DEFAULT '',
                fire_kind      TEXT NOT NULL DEFAULT 'legacy',
                fire_key       TEXT NOT NULL DEFAULT '',
                scheduled_for  REAL NOT NULL DEFAULT 0,
                created_at     REAL NOT NULL DEFAULT 0,
                claimed_at     REAL NOT NULL DEFAULT 0,
                retry_of_fire_id INTEGER,
                started_at     REAL NOT NULL,
                finished_at    REAL,
                status         TEXT NOT NULL DEFAULT 'running',
                error_message  TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (job_id) REFERENCES cron_jobs(id) ON DELETE CASCADE
            )
            """
        )
        run_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(cron_job_runs)").fetchall()
        }
        run_additions = {
            "owner_account_id": "TEXT NOT NULL DEFAULT ''",
            "fire_kind": "TEXT NOT NULL DEFAULT 'legacy'",
            "fire_key": "TEXT NOT NULL DEFAULT ''",
            "scheduled_for": "REAL NOT NULL DEFAULT 0",
            "created_at": "REAL NOT NULL DEFAULT 0",
            "claimed_at": "REAL NOT NULL DEFAULT 0",
            "retry_of_fire_id": "INTEGER",
        }
        for name, declaration in run_additions.items():
            if name not in run_cols:
                conn.execute(
                    f"ALTER TABLE cron_job_runs ADD COLUMN {name} {declaration}"
                )
        # 旧实现把异常拼入 status，并用无法区分退出/停机原因的 cancelled。
        # 迁移后状态恢复为有限集合；未知旧取消按最保守的 abandoned 处理。
        conn.execute(
            """
            UPDATE cron_job_runs
            SET error_message = CASE
                    WHEN error_message = '' THEN LTRIM(SUBSTR(status, 8), ': ')
                    ELSE error_message
                END,
                status = 'failed'
            WHERE status LIKE 'failed:%'
            """
        )
        conn.execute(
            """
            UPDATE cron_job_runs
            SET status = 'abandoned',
                error_message = CASE
                    WHEN error_message = '' OR error_message = 'cancelled'
                        THEN 'legacy_cancel_reason_unknown'
                    ELSE error_message
                END
            WHERE status = 'cancelled'
            """
        )
        conn.execute(
            """
            UPDATE cron_job_runs
            SET owner_account_id = COALESCE(
                    (SELECT owner_account_id FROM cron_jobs WHERE id = cron_job_runs.job_id),
                    ''
                ),
                fire_kind = CASE WHEN fire_kind = '' THEN 'legacy' ELSE fire_kind END,
                fire_key = CASE WHEN fire_key = '' THEN 'legacy:' || id ELSE fire_key END,
                scheduled_for = CASE WHEN scheduled_for = 0 THEN started_at ELSE scheduled_for END,
                created_at = CASE WHEN created_at = 0 THEN started_at ELSE created_at END,
                claimed_at = CASE WHEN claimed_at = 0 THEN started_at ELSE claimed_at END
            WHERE fire_key = '' OR owner_account_id = '' OR created_at = 0 OR claimed_at = 0
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cron_job_runs_job_id ON cron_job_runs(job_id)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cron_job_runs_retry_source "
            "ON cron_job_runs(retry_of_fire_id)"
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_cron_job_runs_fire_identity
            ON cron_job_runs(job_id, fire_key)
            WHERE fire_key <> ''
            """
        )
        self._conn.commit()

    @staticmethod
    def _legacy_payload(row: sqlite3.Row) -> tuple[str, dict[str, Any]]:
        kind = str(row["kind"] or "")
        next_run_at = float(row["next_run_at"] or 0)
        if kind == "interval" and row["interval_seconds"]:
            start_at = datetime.fromtimestamp(next_run_at, tz=BJ_TZ) if next_run_at else _local_now()
            return "interval", {
                "seconds": int(row["interval_seconds"]),
                "start_at": start_at.isoformat(),
            }
        if kind == "once" and next_run_at:
            return "date", {"run_at": datetime.fromtimestamp(next_run_at, tz=BJ_TZ).isoformat()}
        raise ValueError(f"无法从旧数据恢复 trigger: kind={kind}")

    def _extract_trigger(self, row: sqlite3.Row) -> tuple[str, dict[str, Any]]:
        trigger_type = str(row["trigger_type"] or "").strip()
        if trigger_type:
            raw_payload = row["trigger_payload"] or "{}"
            if isinstance(raw_payload, bytes):
                raw_payload = raw_payload.decode("utf-8")
            return trigger_type, json.loads(str(raw_payload))
        return self._legacy_payload(row)

    # ---- 写 ----
    def create(
        self,
        *,
        name: str,
        schedule: str,
        query: str,
        session_id: str,
        workspace_id: str = "default",
        deliver: str = "",
        origin_source: dict[str, Any] | None = None,
        owner_account_id: str = "",
    ) -> dict[str, Any]:
        """按 schedule 创建任务，返回任务 dict。schedule 解析失败抛 ValueError。"""
        now_dt = _local_now()
        parsed = parse_schedule(schedule, now=now_dt)
        next_fire = get_next_fire_at(parsed["trigger_type"], parsed["trigger_payload"], now=now_dt)
        if next_fire is None:
            raise ValueError(f"schedule '{schedule}' 没有可用的下次执行时间")
        now_ts = now_dt.timestamp()
        interval = float(parsed.get("interval_seconds", 0.0))
        next_run = next_fire.timestamp()
        origin_source = dict(origin_source or {})
        deliver = str(deliver or "").strip()
        if not deliver:
            if str(origin_source.get("platform") or "") == "feishu":
                deliver = "origin"
            else:
                deliver = "new_session"

        job_id = f"cron_{uuid.uuid4().hex[:8]}"

        def _write(conn):
            conn.execute(
                "INSERT INTO cron_jobs "
                "(id, owner_account_id, name, kind, interval_seconds, query, session_id, workspace_id, "
                " enabled, next_run_at, last_run_at, last_status, deliver, origin_source, "
                " trigger_type, trigger_payload, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 0, '', ?, ?, ?, ?, ?)",
                (
                    job_id,
                    owner_account_id,
                    name,
                    parsed["kind"],
                    interval,
                    query,
                    session_id,
                    workspace_id,
                    next_run,
                    deliver,
                    json.dumps(origin_source or {}, ensure_ascii=True, sort_keys=True),
                    parsed["trigger_type"],
                    json.dumps(parsed["trigger_payload"], ensure_ascii=True, sort_keys=True),
                    now_ts,
                ),
            )
        self._writer.execute(_write)
        log.info("创建定时任务 %s (%s) name=%s", job_id, schedule, name)
        return self.get(job_id, owner_account_id=owner_account_id)  # type: ignore[return-value]

    def set_enabled(
        self,
        job_id: str,
        enabled: bool,
        owner_account_id: str = "",
        *,
        _all_owners: bool = False,
    ) -> bool:
        """启用或停用任务，返回任务是否存在。"""
        now_dt = _local_now()

        def _write(conn):
            next_run = None
            if enabled:
                sql = "SELECT * FROM cron_jobs WHERE id = ?"
                params: tuple[Any, ...] = (job_id,)
                if not _all_owners:
                    sql += " AND owner_account_id = ?"
                    params = (job_id, owner_account_id)
                row = conn.execute(sql, params).fetchone()
                if row is None:
                    return 0
                trigger_type, payload = self._extract_trigger(row)
                next_fire = get_next_fire_at(trigger_type, payload, now=now_dt)
                next_run = next_fire.timestamp() if next_fire is not None else row["next_run_at"]
            else:
                sql = "SELECT next_run_at FROM cron_jobs WHERE id = ?"
                params = (job_id,)
                if not _all_owners:
                    sql += " AND owner_account_id = ?"
                    params = (job_id, owner_account_id)
                next_run = conn.execute(sql, params).fetchone()
                next_run = next_run["next_run_at"] if next_run else 0
            sql = "UPDATE cron_jobs SET enabled = ?, next_run_at = ? WHERE id = ?"
            params2: list[Any] = [1 if enabled else 0, float(next_run or 0), job_id]
            if not _all_owners:
                sql += " AND owner_account_id = ?"
                params2.append(owner_account_id)
            cur = conn.execute(sql, params2)
            return cur.rowcount
        return self._writer.execute(_write) > 0

    def delete(self, job_id: str, owner_account_id: str = "", *, _all_owners: bool = False) -> bool:
        """删除任务，返回任务是否存在。"""
        def _write(conn):
            sql = "DELETE FROM cron_jobs WHERE id = ?"
            params: list[Any] = [job_id]
            if not _all_owners:
                sql += " AND owner_account_id = ?"
                params.append(owner_account_id)
            cur = conn.execute(sql, params)
            return cur.rowcount
        return self._writer.execute(_write) > 0

    def update_next_run(self, job_id: str, next_run_at: float | None, *, enabled: bool | None = None) -> None:
        def _write(conn):
            if enabled is None:
                conn.execute(
                    "UPDATE cron_jobs SET next_run_at = ? WHERE id = ?",
                    (float(next_run_at or 0), job_id),
                )
            else:
                conn.execute(
                    "UPDATE cron_jobs SET next_run_at = ?, enabled = ? WHERE id = ?",
                    (float(next_run_at or 0), 1 if enabled else 0, job_id),
                )
        self._writer.execute(_write)

    def prepare_owner_jobs_for_mount(
        self,
        owner_account_id: str,
        *,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        """Skip offline occurrences and return this Owner's mountable Jobs.

        This operation never creates a Fire.  Recurring Jobs advance to the
        first occurrence strictly after login; expired one-time Jobs are
        disabled so APScheduler cannot treat them as catch-up work.
        """

        owner = str(owner_account_id or "").strip()
        if not owner:
            return []
        now_dt = datetime.fromtimestamp(time.time() if now is None else float(now), tz=BJ_TZ)

        def _prepare(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                """
                SELECT * FROM cron_jobs
                WHERE owner_account_id = ? AND enabled = 1
                ORDER BY created_at
                """,
                (owner,),
            ).fetchall()
            mountable: list[dict[str, Any]] = []
            for row in rows:
                trigger_type, payload = self._extract_trigger(row)
                next_fire = get_first_future_fire_at(trigger_type, payload, now=now_dt)
                if next_fire is None:
                    conn.execute(
                        """
                        UPDATE cron_jobs
                        SET enabled = 0, next_run_at = 0
                        WHERE id = ? AND owner_account_id = ?
                        """,
                        (str(row["id"]), owner),
                    )
                    continue
                next_run_at = next_fire.timestamp()
                conn.execute(
                    """
                    UPDATE cron_jobs
                    SET next_run_at = ?
                    WHERE id = ? AND owner_account_id = ?
                    """,
                    (next_run_at, str(row["id"]), owner),
                )
                data = self._row_to_dict(row)
                data["next_run_at"] = next_run_at
                mountable.append(data)
            return mountable

        return self._writer.execute(_prepare)

    # ---- Fire / 执行记录 ----
    @staticmethod
    def _row_to_fire(row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in row.keys()}

    def claim_scheduled_fire(
        self,
        job_id: str,
        *,
        owner_account_id: str,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        """Atomically claim the currently due scheduled Fire and advance the Job."""

        claim_time = time.time() if now is None else float(now)

        def _claim(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute(
                """
                SELECT * FROM cron_jobs
                WHERE id = ? AND owner_account_id = ? AND enabled = 1
                """,
                (job_id, owner_account_id),
            ).fetchone()
            if row is None:
                return None
            scheduled_for = float(row["next_run_at"] or 0)
            if scheduled_for <= 0 or scheduled_for > claim_time:
                return None
            fire_key = f"scheduled:{scheduled_for:.6f}"
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO cron_job_runs (
                    job_id, owner_account_id, fire_kind, fire_key, scheduled_for,
                    created_at, claimed_at, started_at, status
                ) VALUES (?, ?, 'scheduled', ?, ?, ?, ?, ?, 'running')
                """,
                (
                    job_id,
                    str(row["owner_account_id"] or ""),
                    fire_key,
                    scheduled_for,
                    claim_time,
                    claim_time,
                    claim_time,
                ),
            )
            if cursor.rowcount != 1:
                return None

            trigger_type, payload = self._extract_trigger(row)
            previous = datetime.fromtimestamp(scheduled_for, tz=BJ_TZ)
            next_fire = get_next_future_fire_at(
                trigger_type,
                payload,
                current_fire_time=previous,
                now=datetime.fromtimestamp(claim_time, tz=BJ_TZ),
            )
            next_run_at = next_fire.timestamp() if next_fire is not None else 0.0
            conn.execute(
                """
                UPDATE cron_jobs
                SET next_run_at = ?, enabled = ?, last_run_at = ?, last_status = 'running'
                WHERE id = ?
                """,
                (next_run_at, 1 if next_fire is not None else 0, claim_time, job_id),
            )
            fire = conn.execute(
                "SELECT * FROM cron_job_runs WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            claimed_job = conn.execute(
                "SELECT * FROM cron_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            result = self._row_to_fire(fire)
            result["job"] = self._row_to_dict(claimed_job)
            return result

        return self._writer.execute(_claim)

    def claim_manual_fire(
        self,
        job_id: str,
        *,
        owner_account_id: str,
    ) -> dict[str, Any] | None:
        """Create and claim one manual Fire without changing the recurring schedule."""

        claim_time = time.time()
        fire_key = f"manual:{uuid.uuid4().hex}"

        def _claim(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute(
                "SELECT * FROM cron_jobs WHERE id = ? AND owner_account_id = ?",
                (job_id, owner_account_id),
            ).fetchone()
            if row is None:
                return None
            cursor = conn.execute(
                """
                INSERT INTO cron_job_runs (
                    job_id, owner_account_id, fire_kind, fire_key, scheduled_for,
                    created_at, claimed_at, started_at, status
                ) VALUES (?, ?, 'manual', ?, ?, ?, ?, ?, 'running')
                """,
                (
                    job_id,
                    owner_account_id,
                    fire_key,
                    claim_time,
                    claim_time,
                    claim_time,
                    claim_time,
                ),
            )
            conn.execute(
                """
                UPDATE cron_jobs
                SET last_run_at = ?, last_status = 'running'
                WHERE id = ? AND owner_account_id = ?
                """,
                (claim_time, job_id, owner_account_id),
            )
            fire = conn.execute(
                "SELECT * FROM cron_job_runs WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            result = self._row_to_fire(fire)
            result["job"] = self._row_to_dict(row)
            return result

        return self._writer.execute(_claim)

    def claim_retry_fire(
        self,
        source_fire_id: int,
        *,
        owner_account_id: str,
    ) -> dict[str, Any] | None:
        """Atomically create a linked retry Fire for a retryable terminal Fire."""

        claim_time = time.time()
        fire_key = f"retry:{source_fire_id}:{uuid.uuid4().hex}"

        def _claim(conn: sqlite3.Connection) -> dict[str, Any] | None:
            source = conn.execute(
                """
                SELECT run.*, job.id AS owned_job_id
                FROM cron_job_runs AS run
                JOIN cron_jobs AS job ON job.id = run.job_id
                WHERE run.id = ?
                  AND run.owner_account_id = ?
                  AND job.owner_account_id = ?
                """,
                (source_fire_id, owner_account_id, owner_account_id),
            ).fetchone()
            if source is None or str(source["status"] or "") not in {
                "failed",
                "abandoned",
                "cancelled_by_logout",
            }:
                return None
            job_id = str(source["owned_job_id"])
            cursor = conn.execute(
                """
                INSERT INTO cron_job_runs (
                    job_id, owner_account_id, fire_kind, fire_key, scheduled_for,
                    created_at, claimed_at, retry_of_fire_id, started_at, status
                ) VALUES (?, ?, 'retry', ?, ?, ?, ?, ?, ?, 'running')
                """,
                (
                    job_id,
                    owner_account_id,
                    fire_key,
                    claim_time,
                    claim_time,
                    claim_time,
                    source_fire_id,
                    claim_time,
                ),
            )
            conn.execute(
                """
                UPDATE cron_jobs
                SET last_run_at = ?, last_status = 'running'
                WHERE id = ? AND owner_account_id = ?
                """,
                (claim_time, job_id, owner_account_id),
            )
            fire = conn.execute(
                "SELECT * FROM cron_job_runs WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            job = conn.execute(
                "SELECT * FROM cron_jobs WHERE id = ? AND owner_account_id = ?",
                (job_id, owner_account_id),
            ).fetchone()
            result = self._row_to_fire(fire)
            result["job"] = self._row_to_dict(job)
            return result

        return self._writer.execute(_claim)

    def finish_job_run(
        self,
        run_id: int,
        status: str,
        error_message: str = "",
    ) -> bool:
        """Finish a running Fire; return whether this caller won the terminal update."""

        if status not in {
            "completed",
            "failed",
            "cancelled_by_logout",
            "abandoned",
        }:
            raise ValueError(f"非法 Cron Fire 终态: {status}")

        def _write(conn):
            cursor = conn.execute(
                """
                UPDATE cron_job_runs
                SET finished_at = ?, status = ?, error_message = ?
                WHERE id = ? AND status = 'running'
                """,
                (time.time(), status, error_message or "", run_id),
            )
            return cursor.rowcount

        return self._writer.execute(_write) == 1

    def recover_running_fires_as_abandoned(self) -> int:
        """将上次进程遗留的 running Fire 收敛为 abandoned，且绝不重放。"""

        recovered_at = time.time()

        def _recover(conn: sqlite3.Connection) -> int:
            rows = conn.execute(
                "SELECT id, job_id FROM cron_job_runs WHERE status = 'running'"
            ).fetchall()
            if not rows:
                return 0
            cursor = conn.execute(
                """
                UPDATE cron_job_runs
                SET finished_at = ?, status = 'abandoned',
                    error_message = CASE
                        WHEN error_message = '' THEN 'process_restarted_before_terminal_state'
                        ELSE error_message
                    END
                WHERE status = 'running'
                """,
                (recovered_at,),
            )
            job_ids = {str(row["job_id"]) for row in rows}
            for job_id in job_ids:
                conn.execute(
                    """
                    UPDATE cron_jobs SET last_status = 'abandoned'
                    WHERE id = ? AND last_status = 'running'
                    """,
                    (job_id,),
                )
            return int(cursor.rowcount)

        return self._writer.execute(_recover)

    def mark_fire_finished(self, job_id: str, run_id: int, status: str) -> None:
        """Update Job summary only when this Fire remains the latest claim."""

        def _write(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                UPDATE cron_jobs SET last_status = ?
                WHERE id = ?
                  AND ? = (
                      SELECT id FROM cron_job_runs
                      WHERE job_id = ?
                      ORDER BY claimed_at DESC, id DESC
                      LIMIT 1
                  )
                """,
                (status, job_id, run_id, job_id),
            )

        self._writer.execute(_write)

    def session_has_running_job_run(self, session_id: str, owner_account_id: str = "") -> bool:
        """该会话下是否仍有 status=running 的 cron_job_runs。"""
        sql = (
            "SELECT 1 FROM cron_job_runs r "
            "JOIN cron_jobs j ON j.id = r.job_id "
            "WHERE j.session_id = ? AND j.owner_account_id = ? AND r.status = 'running' "
            "LIMIT 1"
        )
        with self._lock:
            row = self._conn.execute(sql, (session_id, owner_account_id)).fetchone()
        return row is not None

    def delete_jobs_for_session(self, session_id: str, owner_account_id: str = "") -> int:
        """删除某会话下全部 cron_jobs（含已停用），避免删会话后留下指向死 session_id 的孤儿行。

        调用方应先用 ``session_has_running_job_run`` / enabled 守卫拦住进行中的任务。
        """
        def _write(conn):
            cur = conn.execute(
                "DELETE FROM cron_jobs WHERE session_id = ? AND owner_account_id = ?",
                (session_id, owner_account_id),
            )
            return int(cur.rowcount)

        return self._writer.execute(_write)

    def get_job_runs(self, job_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """返回指定任务的最近执行记录。"""
        sql = (
            "SELECT id, job_id, owner_account_id, fire_kind, fire_key, scheduled_for, "
            "created_at, claimed_at, retry_of_fire_id, started_at, finished_at, "
            "status, error_message "
            "FROM cron_job_runs WHERE job_id = ? ORDER BY started_at DESC LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(sql, (job_id, limit)).fetchall()
        return [
            {
                "id": r["id"],
                "job_id": r["job_id"],
                "owner_account_id": r["owner_account_id"],
                "fire_kind": r["fire_kind"],
                "fire_key": r["fire_key"],
                "scheduled_for": r["scheduled_for"],
                "created_at": r["created_at"],
                "claimed_at": r["claimed_at"],
                "retry_of_fire_id": r["retry_of_fire_id"],
                "started_at": r["started_at"],
                "started_at_bj": format_bj_timestamp(r["started_at"]),
                "finished_at": r["finished_at"],
                "finished_at_bj": format_bj_timestamp(r["finished_at"]),
                "status": r["status"],
                "error_message": r["error_message"],
                "duration_seconds": (
                    float(r["finished_at"] - r["started_at"]) if r["finished_at"] else None
                ),
            }
            for r in rows
        ]

    def get_fire(
        self,
        fire_id: int,
        *,
        owner_account_id: str,
    ) -> dict[str, Any] | None:
        """按 Owner 读取单个 Fire，供人工重试授权与诊断使用。"""

        with self._lock:
            row = self._conn.execute(
                """
                SELECT run.* FROM cron_job_runs AS run
                JOIN cron_jobs AS job ON job.id = run.job_id
                WHERE run.id = ?
                  AND run.owner_account_id = ?
                  AND job.owner_account_id = ?
                """,
                (fire_id, owner_account_id, owner_account_id),
            ).fetchone()
        return self._row_to_fire(row) if row is not None else None

    def get_job_run_summary(self, job_id: str) -> dict[str, Any]:
        """返回任务执行统计。"""
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) AS c FROM cron_job_runs WHERE job_id = ?", (job_id,)
            ).fetchone()["c"]
            success = self._conn.execute(
                "SELECT COUNT(*) AS c FROM cron_job_runs WHERE job_id = ? AND status = 'completed'",
                (job_id,),
            ).fetchone()["c"]
            failed = self._conn.execute(
                "SELECT COUNT(*) AS c FROM cron_job_runs WHERE job_id = ? AND status LIKE 'failed%'",
                (job_id,),
            ).fetchone()["c"]
        return {
            "total": total,
            "success": success,
            "failed": failed,
            "other": total - success - failed,
        }

    # ---- 读 ----
    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = {k: row[k] for k in row.keys()}
        raw_origin = data.get("origin_source") or "{}"
        if isinstance(raw_origin, bytes):
            raw_origin = raw_origin.decode("utf-8")
        if isinstance(raw_origin, str):
            try:
                parsed_origin = json.loads(raw_origin)
            except json.JSONDecodeError:
                parsed_origin = {}
            data["origin_source"] = parsed_origin if isinstance(parsed_origin, dict) else {}
        elif not isinstance(raw_origin, dict):
            data["origin_source"] = {}
        try:
            trigger_type, payload = CronJobStore._extract_row_trigger(data)
            data["trigger_type"] = trigger_type
            data["trigger_payload"] = payload
        except ValueError:
            pass
        return data

    @staticmethod
    def _extract_row_trigger(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        trigger_type = str(data.get("trigger_type") or "").strip()
        if trigger_type:
            raw_payload = data.get("trigger_payload") or "{}"
            if isinstance(raw_payload, str):
                return trigger_type, json.loads(raw_payload)
            return trigger_type, raw_payload
        kind = str(data.get("kind") or "")
        if kind == "interval" and data.get("interval_seconds"):
            next_run_at = float(data.get("next_run_at") or 0)
            start_at = datetime.fromtimestamp(next_run_at, tz=BJ_TZ) if next_run_at else _local_now()
            return "interval", {"seconds": int(data["interval_seconds"]), "start_at": start_at.isoformat()}
        if kind == "once" and data.get("next_run_at"):
            run_at = datetime.fromtimestamp(float(data["next_run_at"]), tz=BJ_TZ).isoformat()
            return "date", {"run_at": run_at}
        raise ValueError("row 缺少可恢复的 trigger 信息")

    def get(self, job_id: str, owner_account_id: str = "", *, _all_owners: bool = False) -> dict[str, Any] | None:
        sql = "SELECT * FROM cron_jobs WHERE id = ?"
        params: tuple[Any, ...] = (job_id,)
        if not _all_owners:
            sql += " AND owner_account_id = ?"
            params = (job_id, owner_account_id)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return self._row_to_dict(row) if row else None

    def list(
        self,
        session_id: str | None = None,
        owner_account_id: str = "",
        *,
        _all_owners: bool = False,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM cron_jobs"
        clauses: list[str] = []
        params_list: list[Any] = []
        if not _all_owners:
            clauses.append("owner_account_id = ?")
            params_list.append(owner_account_id)
        if session_id is not None:
            clauses.append("session_id = ?")
            params_list.append(session_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC"
        with self._lock:
            rows = self._conn.execute(sql, tuple(params_list)).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def iter_enabled_jobs(self, owner_account_id: str) -> list[dict[str, Any]]:
        """Return enabled Jobs for exactly one Owner."""

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM cron_jobs
                WHERE owner_account_id = ? AND enabled = 1
                ORDER BY created_at
                """,
                (owner_account_id,),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_due(self, now: float | None = None, *, owner_account_id: str) -> list[dict[str, Any]]:
        """Return due enabled Jobs for exactly one Owner."""

        now = time.time() if now is None else now
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM cron_jobs
                WHERE owner_account_id = ? AND enabled = 1 AND next_run_at <= ?
                ORDER BY next_run_at
                """,
                (owner_account_id, now),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def compute_next_run(self, job: dict[str, Any], *, after_current: bool = False) -> float | None:
        trigger_type, payload = self._extract_row_trigger(job)
        if after_current and job.get("next_run_at"):
            previous_fire_time = datetime.fromtimestamp(float(job["next_run_at"]), tz=BJ_TZ)
            next_fire = get_next_fire_at(
                trigger_type,
                payload,
                now=previous_fire_time,
                previous_fire_time=previous_fire_time,
            )
        else:
            next_fire = get_next_fire_at(trigger_type, payload)
        return next_fire.timestamp() if next_fire is not None else None
