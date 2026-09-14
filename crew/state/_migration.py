"""Small SQLite migration helpers."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path

from crew.core.runctx import LOCAL_OWNER_ACCOUNT_ID
from crew.state.logging import get_logger
from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite

log = get_logger("migration")

OWNER_TABLE_LABELS = {
    "sessions": "会话",
    "session_events": "会话事件",
    "writer_leases": "会话写者租约",
    "session_agent_config": "会话 Agent 配置",
    "channel_session_routes": "渠道会话路由",
    "workspaces": "工作空间",
    "cron_jobs": "定时任务",
    "runtime_tasks": "任务",
    "notifications": "通知",
    "compaction_summaries": "压缩摘要",
}

# Feature 拆库归属（ADR-0038）：cron 两表已迁至独立库（crew_data/cron.db），
# work 域 15 表已迁至独立库（crew_data/work.db），dynamic-kanban 域 6 表已迁至
# 独立库（crew_data/kanban.db），external-agents 域 4 表与 team 域 2 表已分别
# 迁至独立库（crew_data/external.db、crew_data/team.db），收尾批三小域——
# sites 域 10 表、tasks 域 runtime_tasks、notifications 域 notifications——已分别
# 迁至独立库（crew_data/sites.db、crew_data/tasks.db、crew_data/notifications.db），
# channels 域 2 表已随 P2-7 归属迁移迁至独立库（crew_data/channels.db，最后一批）。
# ADR-0038 批次清单拆完后另有一笔清单外补充批：wiki_learning 目录插件自有 6 表
# 迁插件独立库（crew_data/wiki_learning.db）。至此主库仅保留 core 状态表
# （见各批表清单之外的 OWNER_TABLE_LABELS 成员）。
CRON_DB_TABLES: tuple[str, ...] = ("cron_jobs", "cron_job_runs")
# work 域表按外键依赖排序（connect_sqlite 开启 foreign_keys=ON，
# copy_legacy_feature_rows 整表复制必须父表先于子表写入）：
# work_items ← work_item_events / work_session_links ← work_references；
# work_sources ← work_source_records。其余表域内无外键。
WORK_DB_TABLES: tuple[str, ...] = (
    "work_items",
    "work_item_events",
    "work_session_links",
    "work_references",
    "work_sources",
    "work_source_records",
    "work_preferences",
    "work_preference_settings",
    "work_preference_evidence",
    "work_daily_briefs",
    "work_period_reports",
    "work_publish_requests",
    "work_workspace_index_status",
    "work_settings",
    "work_templates",
)
# kanban 域 6 表按外键依赖排序：kanban_tasks / kanban_events / kanban_runtime_states
# 指向 kanban_workflows，kanban_dependencies / kanban_task_runs 指向 kanban_tasks。
# 注意：kanban 域只登记拆库归属，不接入本模块的通用 owner 巡检/认领扫描——
# kanban_workflows 的无主行语义由 store 自带的 isolation_state 迁移负责
# （legacy_ambiguous 歧义行必须保持 owner='' 隔离留给人工认领，通用
# backfill 会把它的 owner 静默改写为 local）。表清单供 copy_legacy_feature_rows
# 与 legacy_owner_scan_targets（结构登记/回退合并）使用。
KANBAN_DB_TABLES: tuple[str, ...] = (
    "kanban_workflows",
    "kanban_tasks",
    "kanban_dependencies",
    "kanban_task_runs",
    "kanban_events",
    "kanban_runtime_states",
)
# external-agents 域 4 表按外键依赖排序：external_agent / observations / bindings
# 指向 external_runtime，observations 与 bindings 还指向 external_agent。
# 与 team 域（下）同用 kanban 的"显式传参才登记"语义：两域表均不在
# OWNER_TABLE_LABELS，owner 归一由各自 store 构造时自带（external_agent /
# observations 在子包 store，external_team 在 team 门面），通用 owner 巡检/
# 认领无需感知；表清单供 copy_legacy_feature_rows 与 legacy_owner_scan_targets
# （结构登记/回退合并）使用，显式传入仅供未来的域专用认领工具消费。
EXTERNAL_DB_TABLES: tuple[str, ...] = (
    "external_runtime",
    "external_agent",
    "external_agent_profile_observation",
    "external_runtime_session_binding",
)
# team 域 2 表按外键依赖排序：external_team_member 指向 external_team。
TEAM_DB_TABLES: tuple[str, ...] = ("external_team", "external_team_member")
# sites 域 10 表（收尾批）：store 4 表（sites/site_releases/site_annotations/
# inspiration_annotations）与 blueprint 6 表（site_canvases/site_widgets/
# site_canvas_placements/site_automations/site_automation_runs/site_bindings）
# 同住 sites.db，各由自己的 store 在构造时复制。域内全部是普通列关联、无任何
# 外键，整表复制顺序无关。sites 表不在 OWNER_TABLE_LABELS，与 external/team
# 同用"显式传参才登记"语义：owner 归一由 store 构造时自带（backfill 只覆盖
# store 半域——blueprint 6 表 owner 列 NOT NULL 无缺省，不存在空 owner 行），
# 通用巡检/认领无需感知；表清单供 copy_legacy_feature_rows 与
# legacy_owner_scan_targets（结构登记/回退合并）使用。
SITES_STORE_DB_TABLES: tuple[str, ...] = (
    "sites",
    "site_releases",
    "site_annotations",
    "inspiration_annotations",
)
SITES_BLUEPRINT_DB_TABLES: tuple[str, ...] = (
    "site_canvases",
    "site_widgets",
    "site_canvas_placements",
    "site_automations",
    "site_automation_runs",
    "site_bindings",
)
SITES_DB_TABLES: tuple[str, ...] = SITES_STORE_DB_TABLES + SITES_BLUEPRINT_DB_TABLES
# tasks 域 1 表（收尾批）：runtime_tasks 是统一长任务运行时的存储（非 product
# feature）。表在 OWNER_TABLE_LABELS，与 cron/work 同语义接入通用巡检/认领
# （无主行由 store 构造时的 backfill 与启动巡检归一）。
TASKS_DB_TABLES: tuple[str, ...] = ("runtime_tasks",)
# notifications 域 1 表（收尾批）：同在 OWNER_TABLE_LABELS，照 cron/work 语义接入。
NOTIFICATIONS_DB_TABLES: tuple[str, ...] = ("notifications",)
# channels 域 2 表（6W 最后一批，随 P2-7 归属迁移）：channel_bindings 与
# channel_session_routes 迁独立库（crew_data/channels.db），表代码同批从 core
# state 归位 crew/channels/。域内零外键，两表各由自己的 store 构造时复制
# （同 sites 两半语义）。巡检语义分化：channel_session_routes 在
# OWNER_TABLE_LABELS，照 cron/work 语义接入通用巡检/认领；
# channel_bindings 不在清单内，与 external/team/sites 同显式豁免语义
# （owner 归一由 ChannelBindingsStore 构造时的 backfill 自带）。
CHANNELS_BINDINGS_DB_TABLES: tuple[str, ...] = ("channel_bindings",)
CHANNELS_ROUTES_DB_TABLES: tuple[str, ...] = ("channel_session_routes",)
CHANNELS_DB_TABLES: tuple[str, ...] = CHANNELS_BINDINGS_DB_TABLES + CHANNELS_ROUTES_DB_TABLES
# wiki_learning 插件域 5 张数据表（ADR-0038 清单外补充批）：目录插件
# plugins/wiki_learning 自有 schema（5 张数据表 + 组件版本表 wiki_learning_schema）
# 迁插件独立库 crew_data/wiki_learning.db，装配点在插件 register。wiki_learning_schema
# 不参与复制——它是插件自管的组件版本表，store 每次 ensure-schema 幂等 upsert
# 单行 stamp（目标库自行重建），恒非空的它若进清单会误触 copy_legacy_feature_rows
# 的域级 gate。5 张数据表域内零外键，整表复制顺序无关；owner_account_id 全部
# NOT NULL 且写入路径必带 owner，不存在空 owner 行来源。6 表均不在
# OWNER_TABLE_LABELS，与 external/team/sites 同显式豁免语义：不进通用巡检/认领，
# 表清单供 copy_legacy_feature_rows 与 legacy_owner_scan_targets（结构登记/
# 回退合并）使用，显式传入仅供未来的域专用认领工具消费。
WIKI_LEARNING_DB_TABLES: tuple[str, ...] = (
    "wiki_learning_episodes",
    "wiki_learning_activities",
    "wiki_learning_assessments",
    "wiki_learning_mastery_events",
    "wiki_learning_mastery_state",
)


def primary_key_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Return primary key column names ordered by SQLite PK position."""

    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [
        row[1]
        for row in sorted((r for r in info if int(r[5] or 0) > 0), key=lambda r: int(r[5]))
    ]


def _has_owner_column(conn: sqlite3.Connection, table: str) -> bool:
    """Return whether a table exists and carries owner_account_id."""

    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if not row:
        return False
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    return "owner_account_id" in cols


def legacy_owner_counts(
    conn: sqlite3.Connection,
    tables: Sequence[str] | None = None,
) -> dict[str, int]:
    """Count rows that still use the legacy empty owner.

    tables 缺省扫描 OWNER_TABLE_LABELS 全集；不存在的表自动跳过。
    """

    counts: dict[str, int] = {}
    for table in tables if tables is not None else OWNER_TABLE_LABELS:
        if not _has_owner_column(conn, table):
            continue
        row = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE owner_account_id = ''").fetchone()
        counts[table] = int(row[0] or 0)
    return counts


def claim_legacy_owner_rows(
    conn: sqlite3.Connection,
    owner_account_id: str,
    tables: Sequence[str] | None = None,
) -> dict[str, int]:
    """Move legacy empty-owner rows to one explicit account."""

    changed: dict[str, int] = {}
    for table in tables if tables is not None else OWNER_TABLE_LABELS:
        if not _has_owner_column(conn, table):
            continue
        conn.execute(
            f"UPDATE OR IGNORE {table} SET owner_account_id = ? WHERE owner_account_id = ''",
            (owner_account_id,),
        )
        changed[table] = int(conn.execute("SELECT changes()").fetchone()[0] or 0)
    return changed


def backfill_cron_owner_from_sessions(
    session_conn: sqlite3.Connection,
    cron_conn: sqlite3.Connection,
) -> int:
    """Backfill cron owner only when session_id maps to exactly one owner.

    sessions 与 cron_jobs 可能同库也可能分库（ADR-0038 cron 拆库）：分库时
    经参数化行传递匹配结果，不使用跨库 SQL；同库时两个连接是同一连接。
    """

    if not (
        _has_owner_column(session_conn, "sessions")
        and _has_owner_column(cron_conn, "cron_jobs")
    ):
        return 0
    single_owner = {
        str(row[0]): str(row[1])
        for row in session_conn.execute(
            """
            SELECT session_id, MIN(owner_account_id) AS owner_account_id
            FROM sessions
            WHERE owner_account_id != ''
            GROUP BY session_id
            HAVING COUNT(DISTINCT owner_account_id) = 1
            """
        ).fetchall()
    }
    orphan_session_ids = {
        str(row[0])
        for row in cron_conn.execute(
            "SELECT session_id FROM cron_jobs WHERE owner_account_id = ''"
        ).fetchall()
    }
    pairs = [
        (owner, session_id)
        for session_id, owner in single_owner.items()
        if session_id in orphan_session_ids
    ]
    if not pairs:
        return 0
    before = cron_conn.total_changes
    cron_conn.executemany(
        "UPDATE cron_jobs SET owner_account_id = ? "
        "WHERE session_id = ? AND owner_account_id = ''",
        pairs,
    )
    return cron_conn.total_changes - before


def backfill_empty_owner_rows(
    conn: sqlite3.Connection,
    tables: list[str] | None = None,
    *,
    owner_account_id: str = LOCAL_OWNER_ACCOUNT_ID,
) -> dict[str, int]:
    """把空 owner 行自动归一到指定账号（默认本机 ``local``）。

    owner 统一后系统不存在"无主"数据：历史 ``owner=''`` 行属于本机场景。
    回填策略（两步）：
    1. ``UPDATE OR IGNORE`` 逐行归一——与既有目标 owner 行主键冲突的空行跳过；
    2. 结束后仍残留的空行即冲突重复行，按"保归属行、删无主行"清除。
    tables 缺省扫描 OWNER_TABLE_LABELS 全集；不存在的表自动跳过。
    """
    changed: dict[str, int] = {}
    for table in tables or list(OWNER_TABLE_LABELS):
        if not _has_owner_column(conn, table):
            continue
        conn.execute(
            f"UPDATE OR IGNORE {table} SET owner_account_id = ? WHERE owner_account_id = ''",
            (owner_account_id,),
        )
        changed[table] = int(conn.execute("SELECT changes()").fetchone()[0] or 0)
        conn.execute(f"DELETE FROM {table} WHERE owner_account_id = ''")
    return changed


def legacy_owner_scan_targets(
    main_db_path: str | Path,
    cron_db_path: str | Path | None = None,
    work_db_path: str | Path | None = None,
    kanban_db_path: str | Path | None = None,
    external_db_path: str | Path | None = None,
    team_db_path: str | Path | None = None,
    sites_db_path: str | Path | None = None,
    tasks_db_path: str | Path | None = None,
    notifications_db_path: str | Path | None = None,
    channels_db_path: str | Path | None = None,
    wiki_learning_db_path: str | Path | None = None,
) -> dict[Path, list[str]]:
    """把 owner 表清单按归属库解析为 ``路径→表清单`` 扫描映射。

    cron 两表归 cron 库、work 域表归 work 库、tasks/notifications/channels
    （routes 表）归各自独立库（ADR-0038），其余表归主库；任一 Feature 路径
    与主库指向同一文件时（回退配置把 Feature 库指回 crew.db）自动合并到
    同一条目。

    kanban / external / team / sites / wiki_learning 五域与 cron/work 的缺省
    语义不同：只在**显式传入路径时**登记为独立条目（指向与主库同一文件时按
    回退语义并入该条目）。缺省（None）时不并入任何条目——kanban 的无主行是
    隔离语义（legacy_ambiguous 留给人工认领）；external/team/sites/wiki_learning
    四域表均不在 OWNER_TABLE_LABELS，owner 归一由各自 store/插件写入语义自带。
    生产巡检/认领调用点不传即天然豁免（含 crew.db 里的拆库备份行），显式传入
    仅供未来的域专用认领工具消费（见 EXTERNAL_DB_TABLES / SITES_DB_TABLES /
    WIKI_LEARNING_DB_TABLES 注释）。
    channels 域两表分属两种语义：routes 表照 cron/work 缺省并入，bindings
    表不在 OWNER_TABLE_LABELS 不参与扫描（见 CHANNELS_DB_TABLES 注释）。
    """

    targets: dict[Path, list[str]] = {Path(main_db_path): []}
    targets[Path(main_db_path)].extend(
        table
        for table in OWNER_TABLE_LABELS
        if table not in CRON_DB_TABLES
        and table not in WORK_DB_TABLES
        and table not in KANBAN_DB_TABLES
        and table not in EXTERNAL_DB_TABLES
        and table not in TEAM_DB_TABLES
        and table not in TASKS_DB_TABLES
        and table not in NOTIFICATIONS_DB_TABLES
        and table not in CHANNELS_DB_TABLES
        and table not in WIKI_LEARNING_DB_TABLES
    )
    cron_path = Path(cron_db_path) if cron_db_path else Path(main_db_path)
    targets.setdefault(cron_path, []).extend(CRON_DB_TABLES)
    work_path = Path(work_db_path) if work_db_path else Path(main_db_path)
    targets.setdefault(work_path, []).extend(WORK_DB_TABLES)
    if kanban_db_path:
        targets.setdefault(Path(kanban_db_path), []).extend(KANBAN_DB_TABLES)
    if external_db_path:
        targets.setdefault(Path(external_db_path), []).extend(EXTERNAL_DB_TABLES)
    if team_db_path:
        targets.setdefault(Path(team_db_path), []).extend(TEAM_DB_TABLES)
    if sites_db_path:
        targets.setdefault(Path(sites_db_path), []).extend(SITES_DB_TABLES)
    tasks_path = Path(tasks_db_path) if tasks_db_path else Path(main_db_path)
    targets.setdefault(tasks_path, []).extend(TASKS_DB_TABLES)
    notifications_path = (
        Path(notifications_db_path) if notifications_db_path else Path(main_db_path)
    )
    targets.setdefault(notifications_path, []).extend(NOTIFICATIONS_DB_TABLES)
    # channels 只登记 OWNER_TABLE_LABELS 内的 routes 表；bindings 表豁免扫描。
    channels_path = Path(channels_db_path) if channels_db_path else Path(main_db_path)
    targets.setdefault(channels_path, []).extend(CHANNELS_ROUTES_DB_TABLES)
    # wiki_learning 同 kanban/external/team/sites：显式传参才登记（豁免语义）。
    if wiki_learning_db_path:
        targets.setdefault(Path(wiki_learning_db_path), []).extend(WIKI_LEARNING_DB_TABLES)
    return targets


def _open_targets(
    targets: Mapping[str | Path, Sequence[str]],
    *,
    wal_enabled: bool,
) -> dict[Path, sqlite3.Connection]:
    return {
        Path(path): connect_sqlite(path, wal_enabled=wal_enabled)
        for path in targets
    }


def inspect_and_backfill_legacy_owners(
    targets: Mapping[str | Path, Sequence[str]],
    *,
    wal_enabled: bool = True,
) -> tuple[dict[str, int], int]:
    """Run startup legacy-owner maintenance across mapped databases.

    targets 是 ``库路径→表清单`` 映射（见 :func:`legacy_owner_scan_targets`）。
    智能回填（cron 按 sessions 唯一归属）优先于 local 兜底，且能跨库工作：
    sessions 与 cron_jobs 各自从归属库读写。返回剩余空 owner 行计数与回填条数。
    """

    conns = _open_targets(targets, wal_enabled=wal_enabled)
    try:
        writers = {
            path: SQLiteWriteHelper(conn, threading.Lock())
            for path, conn in conns.items()
        }

        def route(table: str) -> Path | None:
            for path, tables in targets.items():
                if table in tables and _has_owner_column(conns[Path(path)], table):
                    return Path(path)
            return None

        backfilled = 0
        session_path = route("sessions")
        cron_path = route("cron_jobs")
        if session_path is not None and cron_path is not None:
            backfilled = writers[cron_path].execute(
                lambda c: backfill_cron_owner_from_sessions(conns[session_path], c)
            )
        claimed: dict[str, int] = {}
        for path, tables in targets.items():
            claimed.update(
                writers[Path(path)].execute(
                    lambda c, own_tables=list(tables): backfill_empty_owner_rows(c, own_tables)
                )
            )
        total = sum(claimed.values())
        if total:
            log.info("已将 %d 条无主数据归属本机账号 %s", total, LOCAL_OWNER_ACCOUNT_ID)
        counts: dict[str, int] = {}
        for path, tables in targets.items():
            counts.update(legacy_owner_counts(conns[Path(path)], list(tables)))
        return counts, backfilled
    finally:
        for conn in conns.values():
            conn.close()


def claim_legacy_owner_databases(
    targets: Mapping[str | Path, Sequence[str]],
    owner_account_id: str,
    *,
    dry_run: bool = False,
    wal_enabled: bool = True,
) -> tuple[dict[str, int], dict[str, int]]:
    """Claim empty-owner rows across the mapped feature databases."""

    conns = _open_targets(targets, wal_enabled=wal_enabled)
    try:
        changed: dict[str, int] = {}
        remaining: dict[str, int] = {}
        for path, tables in targets.items():
            conn = conns[Path(path)]
            if dry_run:
                counts = legacy_owner_counts(conn, list(tables))
                changed.update(counts)
                remaining.update(counts)
                continue
            writer = SQLiteWriteHelper(conn, threading.Lock())
            changed.update(
                writer.execute(
                    lambda c, own_tables=list(tables): claim_legacy_owner_rows(
                        c, owner_account_id, own_tables
                    )
                )
            )
            remaining.update(legacy_owner_counts(conn, list(tables)))
        return changed, remaining
    finally:
        for conn in conns.values():
            conn.close()


def rebuild_table_pk(
    conn: sqlite3.Connection,
    *,
    table: str,
    expected_pk: list[str],
    new_ddl: str,
    copy_sql: str,
) -> bool:
    """Rebuild a table when its primary key does not match the expected shape."""

    if primary_key_columns(conn, table) == expected_pk:
        return False
    conn.execute(new_ddl)
    conn.execute(copy_sql)
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
    return True
