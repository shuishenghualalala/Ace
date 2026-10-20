"""Per-Feature schema 版本表助手与 product.work 试点接入契约。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from crew.cli.app import CliContext, CliError
from crew.cli.main import build_parser
from crew.state.schema_version import (
    SchemaVersionError,
    apply_version,
    current_version,
    ensure_version_table,
    stamp_baseline,
    version_table_name,
)


def _conn(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "scratch.db"))
    conn.isolation_level = None
    return conn


def _rows(conn: sqlite3.Connection, table: str) -> list[tuple[int, float]]:
    return conn.execute(f"SELECT version, updated_at FROM {table}").fetchall()


# ── 助手三函数语义 ────────────────────────────────────────────────────────


def test_version_table_name_rejects_non_identifier_feature():
    assert version_table_name("work") == "work_schema_version"
    for bad in ("", "Work", "work-x", "work.x", "work; DROP TABLE x", "1work"):
        with pytest.raises(SchemaVersionError):
            version_table_name(bad)


def test_ensure_version_table_is_idempotent_and_empty(tmp_path):
    conn = _conn(tmp_path)
    try:
        ensure_version_table(conn, "work")
        ensure_version_table(conn, "work")
        assert current_version(conn, "work") == 0
        assert _rows(conn, "work_schema_version") == []
    finally:
        conn.close()


def test_apply_version_is_monotonic_and_single_row(tmp_path):
    conn = _conn(tmp_path)
    try:
        apply_version(conn, "work", 1)
        assert current_version(conn, "work") == 1
        conn.execute("UPDATE work_schema_version SET updated_at = 100.0")
        apply_version(conn, "work", 2)
        assert current_version(conn, "work") == 2
        # 单行记录，且写入时刷新 updated_at
        rows = _rows(conn, "work_schema_version")
        assert len(rows) == 1
        assert rows[0][0] == 2
        assert rows[0][1] > 100.0
        # 回退与平写都拒绝
        for stale in (1, 2):
            with pytest.raises(SchemaVersionError, match="只能递增"):
                apply_version(conn, "work", stale)
        assert current_version(conn, "work") == 2
        # 非正整数一律拒绝
        for bad in (0, -1, True, 1.5, "3"):
            with pytest.raises(SchemaVersionError):
                apply_version(conn, "work", bad)  # type: ignore[arg-type]
    finally:
        conn.close()


def test_multiple_features_coexist_in_one_database(tmp_path):
    conn = _conn(tmp_path)
    try:
        stamp_baseline(conn, "work", version=1)
        stamp_baseline(conn, "team", version=3)
        assert current_version(conn, "work") == 1
        assert current_version(conn, "team") == 3
        apply_version(conn, "work", 2)
        assert current_version(conn, "work") == 2
        assert current_version(conn, "team") == 3
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE '%_schema_version'"
            ).fetchall()
        }
        assert names == {"work_schema_version", "team_schema_version"}
    finally:
        conn.close()


def test_stamp_baseline_writes_once_and_never_downgrades(tmp_path):
    conn = _conn(tmp_path)
    try:
        assert stamp_baseline(conn, "work", version=1) == 1
        conn.execute("UPDATE work_schema_version SET updated_at = 100.0")
        # 重复运行：不重复写、不改 updated_at
        assert stamp_baseline(conn, "work", version=1) == 1
        assert _rows(conn, "work_schema_version") == [(1, 100.0)]
        # 库里已登记到更高版本时，旧代码的基线补写不能降级
        assert stamp_baseline(conn, "work", version=1) == 1
        conn.execute("DELETE FROM work_schema_version")
        conn.execute(
            "INSERT INTO work_schema_version (singleton, version, updated_at) VALUES (1, 5, 0)"
        )
        assert stamp_baseline(conn, "work", version=1) == 5
        assert _rows(conn, "work_schema_version") == [(5, 0.0)]
    finally:
        conn.close()


# ── product.work 试点接入 ────────────────────────────────────────────────


def _work_host() -> SimpleNamespace:
    return SimpleNamespace(
        session_store=object(),
        workspace_store=object(),
        work_service=None,
        plugins=None,
        _notify_owner_fn=None,
    )


def _build_work(tmp_path: Path):
    from crew.work.feature import build_work_feature

    return build_work_feature(
        _work_host(),
        db_path=tmp_path / "work.db",
        wal_enabled=False,
        hook_registry=object(),
    )


def test_work_feature_stamps_schema_baseline_v1(tmp_path):
    from crew.work.feature import WORK_SCHEMA_FEATURE, WORK_SCHEMA_VERSION

    assert WORK_SCHEMA_FEATURE == "work"
    assert WORK_SCHEMA_VERSION == 1
    bundle = _build_work(tmp_path)
    try:
        conn = sqlite3.connect(str(tmp_path / "work.db"))
        try:
            assert current_version(conn, WORK_SCHEMA_FEATURE) == 1
            rows = _rows(conn, "work_schema_version")
            assert len(rows) == 1 and rows[0][0] == 1
        finally:
            conn.close()
    finally:
        bundle.service.close()


def test_work_rebuild_does_not_rewrite_version_record(tmp_path):
    from crew.work.feature import WORK_SCHEMA_FEATURE

    bundle = _build_work(tmp_path)
    bundle.service.close()
    conn = sqlite3.connect(str(tmp_path / "work.db"))
    try:
        conn.execute("UPDATE work_schema_version SET updated_at = 100.0")
        conn.commit()
    finally:
        conn.close()
    # 同一库重建 generation（等价于重复运行）：版本记录保持原样
    second = _build_work(tmp_path)
    try:
        conn = sqlite3.connect(str(tmp_path / "work.db"))
        try:
            assert current_version(conn, WORK_SCHEMA_FEATURE) == 1
            assert _rows(conn, "work_schema_version") == [(1, 100.0)]
        finally:
            conn.close()
    finally:
        second.service.close()


# ── CLI：migrate feature <name> ──────────────────────────────────────────


def _cli_app(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(
            db_path=str(tmp_path / "crew.db"),
            work_db_path=str(tmp_path / "work.db"),
            sqlite_wal=False,
        )
    )


def _invoke_migrate_feature(name: str, app: SimpleNamespace):
    args = build_parser().parse_args(["migrate", "feature", name])
    result = args.handler(args, CliContext(_app=app))
    assert not hasattr(result, "__await__")
    return result


def test_migrate_feature_work_reports_and_prepares_version_table(tmp_path):
    app = _cli_app(tmp_path)
    result = _invoke_migrate_feature("work", app)
    assert result.data == {
        "feature": "work",
        "current_version": 1,
        "target_version": 1,
    }
    assert "v1" in result.text
    # work 已拆独立库（ADR-0038）：版本表在 work 库就绪，不触碰主库
    assert (tmp_path / "work.db").exists()
    conn = sqlite3.connect(str(tmp_path / "work.db"))
    try:
        assert current_version(conn, "work") == 1
    finally:
        conn.close()
    # 再次运行幂等：版本不变、仍单行
    again = _invoke_migrate_feature("work", app)
    assert again.data == result.data
    conn = sqlite3.connect(str(tmp_path / "work.db"))
    try:
        assert len(conn.execute("SELECT 1 FROM work_schema_version").fetchall()) == 1
    finally:
        conn.close()


def test_migrate_feature_unknown_name_reports_no_versioned_migration():
    with pytest.raises(CliError, match="尚无版本化迁移"):
        _invoke_migrate_feature("nope", None)
