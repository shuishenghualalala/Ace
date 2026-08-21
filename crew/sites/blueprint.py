"""Sites 看板资产、数据刷新执行与 Widget 投递运行时。"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from jsonschema import Draft202012Validator

from crew.core.errors import ToolError
from crew.cron.jobs import BJ_TZ, parse_duration
from crew.security.outbound import (
    PublicRedirectApprovalRequired,
    parse_public_http_target,
    request_public_http,
)
from crew.state.home import get_owner_runtime_home, safe_path_segment
from crew.state.sqlite import SQLiteWriteHelper, connect_sqlite
from crew.tools.redact import safe_public_error
from crew.tools.security_guard import (
    authorize_network_url,
    fetch_authorized_url,
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class BlueprintStore:
    """按 owner 隔离保存 Blueprint 六类资产与运行记录。"""

    def __init__(self, db_path: str, *, wal_enabled: bool = True) -> None:
        self._lock = threading.RLock()
        self._conn = connect_sqlite(Path(db_path), wal_enabled=wal_enabled)
        self._writer = SQLiteWriteHelper(self._conn, self._lock)
        self._writer.execute(self._init_schema)

    @staticmethod
    def _init_schema(conn) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS site_canvases (
                owner_account_id TEXT NOT NULL, id TEXT NOT NULL,
                workspace_id TEXT NOT NULL DEFAULT '', session_id TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL, purpose TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY(owner_account_id,id)
            );
            CREATE INDEX IF NOT EXISTS idx_site_canvases_owner
                ON site_canvases(owner_account_id,updated_at DESC);
            CREATE TABLE IF NOT EXISTS site_widgets (
                owner_account_id TEXT NOT NULL, id TEXT NOT NULL,
                workspace_id TEXT NOT NULL DEFAULT '', title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '', workspace_path TEXT NOT NULL,
                slots TEXT NOT NULL DEFAULT '{}', events TEXT NOT NULL DEFAULT '{}',
                latest_data TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'idle',
                current_input TEXT NOT NULL DEFAULT '{}', last_run_id TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '', resource_revision INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY(owner_account_id,id)
            );
            CREATE INDEX IF NOT EXISTS idx_site_widgets_owner
                ON site_widgets(owner_account_id,updated_at DESC);
            CREATE TABLE IF NOT EXISTS site_canvas_placements (
                owner_account_id TEXT NOT NULL, mount_id TEXT NOT NULL,
                canvas_id TEXT NOT NULL, widget_id TEXT NOT NULL,
                layout TEXT NOT NULL, z_order INTEGER NOT NULL DEFAULT 0,
                view_state TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL,
                updated_at REAL NOT NULL, PRIMARY KEY(owner_account_id,mount_id)
            );
            CREATE INDEX IF NOT EXISTS idx_site_placements_canvas
                ON site_canvas_placements(owner_account_id,canvas_id,z_order,created_at);
            CREATE TABLE IF NOT EXISTS site_automations (
                owner_account_id TEXT NOT NULL, id TEXT NOT NULL,
                workspace_id TEXT NOT NULL DEFAULT '', title TEXT NOT NULL,
                description TEXT NOT NULL, trigger_json TEXT NOT NULL,
                input_json TEXT NOT NULL, execution_json TEXT NOT NULL,
                result_json TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
                latest_artifact_run_id TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY(owner_account_id,id)
            );
            CREATE INDEX IF NOT EXISTS idx_site_automations_owner
                ON site_automations(owner_account_id,updated_at DESC);
            CREATE TABLE IF NOT EXISTS site_automation_runs (
                owner_account_id TEXT NOT NULL, id TEXT NOT NULL,
                automation_id TEXT NOT NULL, trigger_kind TEXT NOT NULL,
                status TEXT NOT NULL, input_json TEXT NOT NULL DEFAULT '{}',
                artifact TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
                logs TEXT NOT NULL DEFAULT '', started_at REAL NOT NULL,
                finished_at REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(owner_account_id,id)
            );
            CREATE INDEX IF NOT EXISTS idx_site_runs_automation
                ON site_automation_runs(owner_account_id,automation_id,started_at DESC);
            CREATE TABLE IF NOT EXISTS site_bindings (
                owner_account_id TEXT NOT NULL, id TEXT NOT NULL,
                automation_id TEXT NOT NULL, widget_id TEXT NOT NULL,
                status TEXT NOT NULL, validation_issues TEXT NOT NULL DEFAULT '[]',
                active INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL,
                updated_at REAL NOT NULL, PRIMARY KEY(owner_account_id,id)
            );
            CREATE INDEX IF NOT EXISTS idx_site_bindings_automation
                ON site_bindings(owner_account_id,automation_id,active);
            CREATE INDEX IF NOT EXISTS idx_site_bindings_widget
                ON site_bindings(owner_account_id,widget_id,active);
            """
        )

    @staticmethod
    def _canvas(row) -> dict[str, Any]:
        return {"id": row[0], "workspaceId": row[1], "sessionId": row[2], "title": row[3],
                "purpose": row[4], "createdAt": row[5], "updatedAt": row[6]}

    def create_canvas(self, owner: str, workspace_id: str, session_id: str,
                      title: str, purpose: str) -> dict[str, Any]:
        canvas_id, now = _id("canvas"), time.time()
        self._writer.execute(lambda conn: conn.execute(
            "INSERT INTO site_canvases VALUES(?,?,?,?,?,?,?,?)",
            (owner, canvas_id, workspace_id, session_id, title, purpose, now, now),
        ))
        return self.get_canvas(owner, canvas_id)

    def get_canvas(self, owner: str, canvas_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id,workspace_id,session_id,title,purpose,created_at,updated_at "
                "FROM site_canvases WHERE owner_account_id=? AND id=?", (owner, canvas_id),
            ).fetchone()
        if not row:
            raise KeyError("看板不存在")
        canvas = self._canvas(row)
        canvas["placements"] = self.list_placements(owner, canvas_id)
        return canvas

    def list_canvases(self, owner: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id,workspace_id,session_id,title,purpose,created_at,updated_at "
                "FROM site_canvases WHERE owner_account_id=? ORDER BY updated_at DESC", (owner,),
            ).fetchall()
        result = []
        for row in rows:
            item = self._canvas(row)
            item["widgetCount"] = len(self.list_placements(owner, item["id"]))
            result.append(item)
        return result

    def update_canvas(self, owner: str, canvas_id: str, *, title: str | None,
                      purpose: str | None) -> dict[str, Any]:
        current = self.get_canvas(owner, canvas_id)
        self._writer.execute(lambda conn: conn.execute(
            "UPDATE site_canvases SET title=?,purpose=?,updated_at=? "
            "WHERE owner_account_id=? AND id=?",
            (title if title is not None else current["title"],
             purpose if purpose is not None else current["purpose"], time.time(), owner, canvas_id),
        ))
        return self.get_canvas(owner, canvas_id)

    def delete_canvas(self, owner: str, canvas_id: str) -> None:
        self.get_canvas(owner, canvas_id)
        def _write(conn):
            conn.execute("DELETE FROM site_canvas_placements WHERE owner_account_id=? AND canvas_id=?", (owner, canvas_id))
            conn.execute("DELETE FROM site_canvases WHERE owner_account_id=? AND id=?", (owner, canvas_id))
        self._writer.execute(_write)

    def place_widget(self, owner: str, canvas_id: str, widget_id: str,
                     layout: dict[str, Any]) -> dict[str, Any]:
        self.get_canvas(owner, canvas_id)
        self.get_widget(owner, widget_id)
        mount_id, now = _id("mount"), time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(z_order),-1)+1 FROM site_canvas_placements "
                "WHERE owner_account_id=? AND canvas_id=?", (owner, canvas_id),
            ).fetchone()
        z_order = int(row[0] if row else 0)
        self._writer.execute(lambda conn: conn.execute(
            "INSERT INTO site_canvas_placements VALUES(?,?,?,?,?,?,?,?,?)",
            (owner, mount_id, canvas_id, widget_id, _json(layout), z_order, "{}", now, now),
        ))
        return self.get_placement(owner, mount_id)

    def get_placement(self, owner: str, mount_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT mount_id,canvas_id,widget_id,layout,z_order,view_state,created_at,updated_at "
                "FROM site_canvas_placements WHERE owner_account_id=? AND mount_id=?", (owner, mount_id),
            ).fetchone()
        if not row:
            raise KeyError("看板组件位置不存在")
        return {"mountId": row[0], "canvasId": row[1], "widgetId": row[2],
                "layout": _load(row[3], {}), "zOrder": row[4],
                "viewState": _load(row[5], {}), "createdAt": row[6], "updatedAt": row[7]}

    def list_placements(self, owner: str, canvas_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT mount_id FROM site_canvas_placements WHERE owner_account_id=? AND canvas_id=? "
                "ORDER BY z_order,created_at", (owner, canvas_id),
            ).fetchall()
        return [self.get_placement(owner, row[0]) for row in rows]

    def update_placement(self, owner: str, mount_id: str, *, layout: dict[str, Any] | None,
                         z_order: int | None, view_state: dict[str, Any] | None) -> dict[str, Any]:
        current = self.get_placement(owner, mount_id)
        self._writer.execute(lambda conn: conn.execute(
            "UPDATE site_canvas_placements SET layout=?,z_order=?,view_state=?,updated_at=? "
            "WHERE owner_account_id=? AND mount_id=?",
            (_json(layout if layout is not None else current["layout"]),
             z_order if z_order is not None else current["zOrder"],
             _json(view_state if view_state is not None else current["viewState"]),
             time.time(), owner, mount_id),
        ))
        return self.get_placement(owner, mount_id)

    def remove_placement(self, owner: str, mount_id: str) -> None:
        self.get_placement(owner, mount_id)
        self._writer.execute(lambda conn: conn.execute(
            "DELETE FROM site_canvas_placements WHERE owner_account_id=? AND mount_id=?", (owner, mount_id)
        ))

    @staticmethod
    def _widget(row) -> dict[str, Any]:
        return {"id": row[0], "workspaceId": row[1], "title": row[2], "description": row[3],
                "workspacePath": row[4], "slots": _load(row[5], {}), "events": _load(row[6], {}),
                "latestData": _load(row[7], {}), "status": row[8],
                "inputState": {"currentInput": _load(row[9], {})}, "lastRun": row[10],
                "error": row[11], "resourceRevision": row[12], "createdAt": row[13], "updatedAt": row[14]}

    def create_widget(self, owner: str, workspace_id: str, workspace_path: str,
                      title: str, description: str, *, widget_id: str = "") -> dict[str, Any]:
        widget_id, now = widget_id or _id("widget"), time.time()
        self._writer.execute(lambda conn: conn.execute(
            "INSERT INTO site_widgets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (owner, widget_id, workspace_id, title, description, workspace_path, "{}", "{}", "{}",
             "idle", "{}", "", "", 0, now, now),
        ))
        return self.get_widget(owner, widget_id)

    def get_widget(self, owner: str, widget_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id,workspace_id,title,description,workspace_path,slots,events,latest_data,status,"
                "current_input,last_run_id,error,resource_revision,created_at,updated_at "
                "FROM site_widgets WHERE owner_account_id=? AND id=?", (owner, widget_id),
            ).fetchone()
        if not row:
            raise KeyError("Widget 不存在")
        widget = self._widget(row)
        active = self.active_binding_for_widget(owner, widget_id)
        widget["bindings"] = {"main": active["id"] if active else ""}
        widget["metadata"] = {"roots": {"workspaceRoot": widget["workspacePath"]}}
        return widget

    def list_widgets(self, owner: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM site_widgets WHERE owner_account_id=? ORDER BY updated_at DESC", (owner,),
            ).fetchall()
        return [self.get_widget(owner, row[0]) for row in rows]

    def update_widget(self, owner: str, widget_id: str, **changes: Any) -> dict[str, Any]:
        current = self.get_widget(owner, widget_id)
        self._writer.execute(lambda conn: conn.execute(
            "UPDATE site_widgets SET title=?,description=?,slots=?,events=?,resource_revision=resource_revision+1,updated_at=? "
            "WHERE owner_account_id=? AND id=?",
            (changes.get("title", current["title"]), changes.get("description", current["description"]),
             _json(changes.get("slots", current["slots"])), _json(changes.get("events", current["events"])),
             time.time(), owner, widget_id),
        ))
        return self.get_widget(owner, widget_id)

    def bump_widget_revision(self, owner: str, widget_id: str) -> dict[str, Any]:
        self.get_widget(owner, widget_id)
        self._writer.execute(lambda conn: conn.execute(
            "UPDATE site_widgets SET resource_revision=resource_revision+1,updated_at=? "
            "WHERE owner_account_id=? AND id=?",
            (time.time(), owner, widget_id),
        ))
        return self.get_widget(owner, widget_id)

    def set_widget_delivery(self, owner: str, widget_id: str, *, data: dict[str, Any],
                            status: str, run_id: str, error: str = "") -> None:
        self._writer.execute(lambda conn: conn.execute(
            "UPDATE site_widgets SET latest_data=?,status=?,last_run_id=?,error=?,updated_at=? "
            "WHERE owner_account_id=? AND id=?",
            (_json(data), status, run_id, error, time.time(), owner, widget_id),
        ))

    def set_widget_status(self, owner: str, widget_id: str, status: str, error: str = "") -> None:
        self._writer.execute(lambda conn: conn.execute(
            "UPDATE site_widgets SET status=?,error=?,updated_at=? WHERE owner_account_id=? AND id=?",
            (status, error, time.time(), owner, widget_id),
        ))

    def delete_widget(self, owner: str, widget_id: str) -> None:
        self.get_widget(owner, widget_id)
        def _write(conn):
            conn.execute("DELETE FROM site_canvas_placements WHERE owner_account_id=? AND widget_id=?", (owner, widget_id))
            conn.execute("DELETE FROM site_bindings WHERE owner_account_id=? AND widget_id=?", (owner, widget_id))
            conn.execute("DELETE FROM site_widgets WHERE owner_account_id=? AND id=?", (owner, widget_id))
        self._writer.execute(_write)

    @staticmethod
    def _automation(row) -> dict[str, Any]:
        return {"id": row[0], "workspaceId": row[1], "title": row[2], "description": row[3],
                "trigger": _load(row[4], {}), "input": _load(row[5], {}),
                "execution": _load(row[6], {}), "result": _load(row[7], {}),
                "enabled": bool(row[8]), "latestArtifactRunId": row[9],
                "createdAt": row[10], "updatedAt": row[11]}

    def create_automation(self, owner: str, workspace_id: str, title: str, description: str,
                          trigger: dict[str, Any], input_spec: dict[str, Any],
                          execution: dict[str, Any], result: dict[str, Any], enabled: bool) -> dict[str, Any]:
        automation_id, now = _id("automation"), time.time()
        self._writer.execute(lambda conn: conn.execute(
            "INSERT INTO site_automations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (owner, automation_id, workspace_id, title, description, _json(trigger), _json(input_spec),
             _json(execution), _json(result), int(enabled), "", now, now),
        ))
        return self.get_automation(owner, automation_id)

    def get_automation(self, owner: str, automation_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id,workspace_id,title,description,trigger_json,input_json,execution_json,result_json,"
                "enabled,latest_artifact_run_id,created_at,updated_at FROM site_automations "
                "WHERE owner_account_id=? AND id=?", (owner, automation_id),
            ).fetchone()
        if not row:
            raise KeyError("Automation 不存在")
        automation = self._automation(row)
        automation["runs"] = self.list_runs(owner, automation_id, limit=20)
        return automation

    def list_automations(self, owner: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM site_automations WHERE owner_account_id=? ORDER BY updated_at DESC", (owner,),
            ).fetchall()
        return [self.get_automation(owner, row[0]) for row in rows]

    def list_enabled_automations(self) -> list[tuple[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT owner_account_id,id FROM site_automations WHERE enabled=1"
            ).fetchall()
        return [(str(row[0]), str(row[1])) for row in rows]

    def update_automation(self, owner: str, automation_id: str, **changes: Any) -> dict[str, Any]:
        current = self.get_automation(owner, automation_id)
        values = {key: changes.get(key, current[key]) for key in
                  ("title", "description", "trigger", "input", "execution", "result", "enabled")}
        self._writer.execute(lambda conn: conn.execute(
            "UPDATE site_automations SET title=?,description=?,trigger_json=?,input_json=?,execution_json=?,"
            "result_json=?,enabled=?,updated_at=? WHERE owner_account_id=? AND id=?",
            (values["title"], values["description"], _json(values["trigger"]), _json(values["input"]),
             _json(values["execution"]), _json(values["result"]), int(bool(values["enabled"])),
             time.time(), owner, automation_id),
        ))
        return self.get_automation(owner, automation_id)

    def delete_automation(self, owner: str, automation_id: str) -> None:
        self.get_automation(owner, automation_id)
        def _write(conn):
            conn.execute("DELETE FROM site_bindings WHERE owner_account_id=? AND automation_id=?", (owner, automation_id))
            conn.execute("DELETE FROM site_automation_runs WHERE owner_account_id=? AND automation_id=?", (owner, automation_id))
            conn.execute("DELETE FROM site_automations WHERE owner_account_id=? AND id=?", (owner, automation_id))
        self._writer.execute(_write)

    def start_run(self, owner: str, automation_id: str, trigger_kind: str, run_input: Any) -> dict[str, Any]:
        with self._lock:
            running = self._conn.execute(
                "SELECT id FROM site_automation_runs WHERE owner_account_id=? AND automation_id=? AND status='running'",
                (owner, automation_id),
            ).fetchone()
        if running:
            raise RuntimeError("Automation 已在运行")
        run_id, now = _id("run"), time.time()
        self._writer.execute(lambda conn: conn.execute(
            "INSERT INTO site_automation_runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (owner, run_id, automation_id, trigger_kind, "running", _json(run_input), "{}", "", "", now, 0),
        ))
        return self.get_run(owner, run_id)

    def finish_run(self, owner: str, run_id: str, *, status: str,
                   artifact: dict[str, Any] | None = None, error: str = "", logs: str = "") -> dict[str, Any]:
        def _write(conn):
            conn.execute(
                "UPDATE site_automation_runs SET status=?,artifact=?,error=?,logs=?,finished_at=? "
                "WHERE owner_account_id=? AND id=?",
                (status, _json(artifact or {}), error, logs[-12000:], time.time(), owner, run_id),
            )
            if status == "succeeded":
                conn.execute(
                    "UPDATE site_automations SET latest_artifact_run_id=?,updated_at=? "
                    "WHERE owner_account_id=? AND id=(SELECT automation_id FROM site_automation_runs "
                    "WHERE owner_account_id=? AND id=?)",
                    (run_id, time.time(), owner, owner, run_id),
                )
        self._writer.execute(_write)
        return self.get_run(owner, run_id)

    def get_run(self, owner: str, run_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id,automation_id,trigger_kind,status,input_json,artifact,error,logs,started_at,finished_at "
                "FROM site_automation_runs WHERE owner_account_id=? AND id=?", (owner, run_id),
            ).fetchone()
        if not row:
            raise KeyError("Automation 运行记录不存在")
        return {"id": row[0], "automationId": row[1], "triggerKind": row[2], "status": row[3],
                "input": _load(row[4], {}), "artifact": _load(row[5], {}), "error": row[6],
                "logs": row[7], "startedAt": row[8], "finishedAt": row[9]}

    def list_runs(self, owner: str, automation_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM site_automation_runs WHERE owner_account_id=? AND automation_id=? "
                "ORDER BY started_at DESC LIMIT ?", (owner, automation_id, max(1, min(limit, 100))),
            ).fetchall()
        return [self.get_run(owner, row[0]) for row in rows]

    @staticmethod
    def _binding(row) -> dict[str, Any]:
        return {"id": row[0], "automationId": row[1], "widgetId": row[2], "status": row[3],
                "validationIssues": _load(row[4], []), "active": bool(row[5]),
                "createdAt": row[6], "updatedAt": row[7]}

    def create_binding(self, owner: str, automation_id: str, widget_id: str,
                       status: str, issues: list[dict[str, Any]]) -> dict[str, Any]:
        self.get_automation(owner, automation_id)
        self.get_widget(owner, widget_id)
        binding_id, now = _id("binding"), time.time()
        def _write(conn):
            conn.execute(
                "UPDATE site_bindings SET active=0,status='invalid',validation_issues=?,updated_at=? "
                "WHERE owner_account_id=? AND widget_id=? AND active=1",
                (_json([{"code": "superseded", "message": "已被新的主 Binding 替代"}]), now, owner, widget_id),
            )
            conn.execute("INSERT INTO site_bindings VALUES(?,?,?,?,?,?,?,?,?)",
                         (owner, binding_id, automation_id, widget_id, status, _json(issues), 1, now, now))
        self._writer.execute(_write)
        return self.get_binding(owner, binding_id)

    def get_binding(self, owner: str, binding_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id,automation_id,widget_id,status,validation_issues,active,created_at,updated_at "
                "FROM site_bindings WHERE owner_account_id=? AND id=?", (owner, binding_id),
            ).fetchone()
        if not row:
            raise KeyError("Binding 不存在")
        return self._binding(row)

    def list_bindings(self, owner: str, *, automation_id: str = "", widget_id: str = "") -> list[dict[str, Any]]:
        sql = ("SELECT id,automation_id,widget_id,status,validation_issues,active,created_at,updated_at "
               "FROM site_bindings WHERE owner_account_id=?")
        params: list[Any] = [owner]
        if automation_id:
            sql += " AND automation_id=?"
            params.append(automation_id)
        if widget_id:
            sql += " AND widget_id=?"
            params.append(widget_id)
        sql += " ORDER BY created_at DESC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._binding(row) for row in rows]

    def active_binding_for_widget(self, owner: str, widget_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id,automation_id,widget_id,status,validation_issues,active,created_at,updated_at "
                "FROM site_bindings WHERE owner_account_id=? AND widget_id=? AND active=1",
                (owner, widget_id),
            ).fetchone()
        return self._binding(row) if row else None

    def set_binding_validation(self, owner: str, binding_id: str, status: str,
                               issues: list[dict[str, Any]]) -> dict[str, Any]:
        self._writer.execute(lambda conn: conn.execute(
            "UPDATE site_bindings SET status=?,validation_issues=?,updated_at=? "
            "WHERE owner_account_id=? AND id=?", (status, _json(issues), time.time(), owner, binding_id),
        ))
        return self.get_binding(owner, binding_id)

    def delete_binding(self, owner: str, binding_id: str) -> None:
        self.get_binding(owner, binding_id)
        self._writer.execute(lambda conn: conn.execute(
            "DELETE FROM site_bindings WHERE owner_account_id=? AND id=?", (owner, binding_id)
        ))


class BlueprintManager:
    """实现资产契约、受控 HTTP 执行、Schema 验证与调度投递。"""

    MAX_RESPONSE_BYTES = 5 * 1024 * 1024
    MAX_REDIRECTS = 4

    def __init__(
        self,
        store: BlueprintStore,
        *,
        workspace_store: Any | None = None,
        security_service: Any | None = None,
    ) -> None:
        self.store = store
        self.workspace_store = workspace_store
        self.security_service = security_service
        self._scheduler: AsyncIOScheduler | None = None

    def widget_root(self, owner: str, widget_id: str) -> Path:
        root = get_owner_runtime_home(owner, create=True) / "sites" / "blueprint" / "widgets" / safe_path_segment(widget_id, "widget")
        root.mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    def normalize_layout(value: Any) -> dict[str, Any]:
        layout = value if isinstance(value, dict) else {}
        mode = str(layout.get("mode") or "grid")
        if mode not in {"grid", "free"}:
            raise ValueError("layout.mode 只能是 grid 或 free")
        result = {"mode": mode}
        limits = {"x": (0, 10000), "y": (0, 10000), "w": (2, 12), "h": (2, 33)} if mode == "grid" else {
            "x": (0, 10000), "y": (0, 10000), "w": (240, 960), "h": (160, 1440)}
        defaults = {"x": 0, "y": 0, "w": 5 if mode == "grid" else 420,
                    "h": 8 if mode == "grid" else 320}
        for key, (minimum, maximum) in limits.items():
            number = int(layout.get(key, defaults[key]))
            if number < minimum or number > maximum:
                raise ValueError(f"layout.{key} 超出范围 {minimum}-{maximum}")
            result[key] = number
        return result

    @staticmethod
    def validate_widget_file(widget: dict[str, Any]) -> dict[str, Any]:
        entry = Path(widget["workspacePath"]) / "index.html"
        issues: list[dict[str, str]] = []
        if not entry.is_file():
            issues.append({"code": "index_missing", "message": "Widget workspace 缺少 index.html"})
        else:
            text = entry.read_text(encoding="utf-8")
            if "<html" not in text.lower() or "</html>" not in text.lower():
                issues.append({"code": "document_incomplete", "message": "index.html 必须是完整 HTML 文档"})
        return {"status": "valid" if not issues else "invalid", "issues": issues, "entry": str(entry)}

    def validate_binding(self, owner: str, binding_id: str) -> dict[str, Any]:
        binding = self.store.get_binding(owner, binding_id)
        automation = self.store.get_automation(owner, binding["automationId"])
        widget = self.store.get_widget(owner, binding["widgetId"])
        issues: list[dict[str, str]] = []
        result = automation.get("result") or {}
        slot = (widget.get("slots") or {}).get("main") or {}
        result_schema = result.get("schema") if result.get("kind") == "artifact" else None
        slot_schema = slot.get("schema") if slot.get("kind") == "json" else None
        if not isinstance(result_schema, dict):
            issues.append({"code": "result_not_bindable", "message": "Automation 必须返回 artifact object"})
        if not isinstance(slot_schema, dict):
            issues.append({"code": "slot_missing", "message": "Widget 必须声明 slots.main JSON schema"})
        if isinstance(result_schema, dict) and isinstance(slot_schema, dict):
            required = set(slot_schema.get("required") or [])
            produced = set((result_schema.get("properties") or {}).keys())
            if not required.issubset(produced):
                issues.append({"code": "schema_mismatch", "message": "Automation 结果未声明 Widget 必填字段"})
        issues.extend(self.validate_widget_file(widget)["issues"])
        status = "invalid" if issues else ("valid" if automation.get("latestArtifactRunId") else "pending_run")
        return self.store.set_binding_validation(owner, binding_id, status, issues)

    def validate_automation_contract(self, trigger: dict[str, Any], execution: dict[str, Any],
                                     result: dict[str, Any]) -> None:
        if execution.get("kind") != "http_json":
            raise ValueError("当前仅支持 http_json Automation")
        raw_url = str(execution.get("url") or "")
        parse_public_http_target(raw_url)
        parsed = urlparse(raw_url)
        self._assert_no_url_secrets(parsed.query)
        headers = execution.get("headers") if isinstance(execution.get("headers"), dict) else {}
        sensitive = {str(name).lower() for name in headers} & {
            "authorization", "cookie", "proxy-authorization", "x-api-key", "api-key",
        }
        if sensitive:
            raise ValueError("一期公开接口模式不保存鉴权请求头")
        if result.get("kind") != "artifact" or not isinstance(result.get("schema"), dict):
            raise ValueError("Automation result 必须声明 artifact schema")
        if (result["schema"].get("type") or "object") != "object":
            raise ValueError("Automation artifact schema 必须描述 JSON object")
        kind = str(trigger.get("kind") or "manual")
        if kind not in {"manual", "interval", "schedule", "once"}:
            raise ValueError("不支持的 Automation trigger")
        if kind != "manual":
            self._trigger_for(trigger)

    @staticmethod
    def _assert_no_url_secrets(query: str) -> None:
        secret_names = {"key", "token", "api_key", "apikey", "access_token", "signature", "secret"}
        names = {name.lower() for name, _ in parse_qsl(query, keep_blank_values=True)}
        if names & secret_names:
            raise ValueError("一期公开接口模式不允许在 URL 查询参数中保存密钥")

    @staticmethod
    def _normalized_http_request(
        execution: dict[str, Any],
    ) -> tuple[str, str, dict[str, str], float]:
        """归一化 http_json 请求参数：方法、鉴权头过滤、超时钳制。"""
        raw_url = str(execution.get("url") or "").strip()
        method = str(execution.get("method") or "GET").upper()
        if method not in {"GET", "POST"}:
            raise ValueError("http_json 一期只允许 GET 或 POST")
        headers = execution.get("headers") if isinstance(execution.get("headers"), dict) else {}
        sensitive = {str(name).lower() for name in headers} & {
            "authorization", "cookie", "proxy-authorization", "x-api-key", "api-key",
        }
        if sensitive:
            raise ValueError("一期公开接口模式不允许鉴权请求头")
        safe_headers = {str(k): str(v) for k, v in headers.items() if str(k).lower() not in {"host", "content-length"}}
        timeout = max(1.0, min(float(execution.get("timeoutSeconds") or 15), 60.0))
        return raw_url, method, safe_headers, timeout

    async def _fetch_json(self, execution: dict[str, Any], run_input: Any) -> tuple[dict[str, Any], str]:
        if self.workspace_store is None or self.security_service is None:
            raise ToolError(
                '{"code":"SECURITY_OUTBOUND_DENIED",'
                '"reason":"authorization_unavailable"}'
            )
        raw_url, method, safe_headers, timeout = self._normalized_http_request(execution)
        current = raw_url
        for redirect in range(self.MAX_REDIRECTS + 1):
            authorized_plan = await authorize_network_url(
                current,
                method=method,
                tool_name="blueprint_automation",
                workspace_store=self.workspace_store,
                security_service=self.security_service,
            )
            request_body = (
                json.dumps(run_input, ensure_ascii=False).encode("utf-8")
                if method == "POST"
                else None
            )
            request_headers = dict(safe_headers)
            if request_body is not None:
                request_headers.setdefault("Content-Type", "application/json")
            response = await asyncio.to_thread(
                fetch_authorized_url,
                authorized_plan,
                body=request_body,
                headers=request_headers,
                timeout=timeout,
                max_bytes=self.MAX_RESPONSE_BYTES,
                reject_redirects=False,
            )
            if 300 <= response.status < 400:
                if redirect == self.MAX_REDIRECTS:
                    raise ValueError("接口重定向次数过多")
                location = response.headers.get("location")
                if not location:
                    raise ValueError("接口返回了无目标的重定向")
                current = urljoin(current, location)
                continue
            if response.status < 200 or response.status >= 300:
                raise ValueError(f"接口返回 HTTP {response.status}")
            try:
                value = json.loads(response.body)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("接口响应不是有效 JSON") from exc
            if not isinstance(value, dict):
                value = {"items": value}
            parsed_current = urlparse(current)
            return value, f"{method} {parsed_current.scheme}://{parsed_current.netloc} -> {response.status}"
        raise RuntimeError("接口请求未产生响应")

    async def _request_json(
        self,
        execution: dict[str, Any],
        run_input: Any,
        allowed_targets: set[tuple[str, int, str]] | None,
    ) -> tuple[dict[str, Any], str]:
        raw_url, method, safe_headers, timeout = self._normalized_http_request(execution)
        self._assert_no_url_secrets(urlparse(raw_url).query)
        response = await asyncio.to_thread(
            request_public_http,
            raw_url,
            method=method,
            timeout=timeout,
            max_bytes=self.MAX_RESPONSE_BYTES,
            headers=safe_headers,
            json_body=run_input if method == "POST" else None,
            allowed_targets=allowed_targets,
        )
        try:
            value = json.loads(response.body.decode(response.charset, errors="strict"))
        except (LookupError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("接口响应不是有效 JSON") from exc
        if not isinstance(value, dict):
            value = {"items": value}
        parsed = urlparse(response.url)
        return value, f"{method} {parsed.scheme}://{parsed.netloc} -> {response.status}"

    async def _fetch_json_authorized(
        self,
        execution: dict[str, Any],
        run_input: Any,
        authorize_network: Callable[[str], Awaitable[None]],
    ) -> tuple[dict[str, Any], str]:
        raw_url = str(execution.get("url") or "").strip()
        next_target = raw_url
        allowed: set[tuple[str, int, str]] = set()
        for _attempt in range(self.MAX_REDIRECTS + 2):
            await authorize_network(next_target)
            allowed.add(parse_public_http_target(next_target).authority)
            try:
                return await self._request_json(execution, run_input, allowed)
            except PublicRedirectApprovalRequired as exc:
                next_target = exc.url
        raise ValueError("接口重定向次数过多")

    async def run_automation(self, owner: str, automation_id: str, *, run_input: Any = None,
                             trigger_kind: str = "manual",
                             authorize_network: Callable[[str], Awaitable[None]] | None = None,
                             ) -> dict[str, Any]:
        automation = self.store.get_automation(owner, automation_id)
        input_spec = automation.get("input") or {"kind": "none"}
        resolved_input = run_input
        if resolved_input is None:
            resolved_input = input_spec.get("defaultInput", {} if input_spec.get("kind") == "json" else None)
        run = self.store.start_run(owner, automation_id, trigger_kind, resolved_input)
        bindings = [item for item in self.store.list_bindings(owner, automation_id=automation_id)
                    if item["active"]]
        for binding in bindings:
            self.store.set_widget_status(owner, binding["widgetId"], "running")
        try:
            execution = automation.get("execution") or {}
            if execution.get("kind") != "http_json":
                raise ValueError("当前仅支持 http_json Automation")
            if authorize_network is None:
                artifact, logs = await self._fetch_json(execution, resolved_input)
            else:
                artifact, logs = await self._fetch_json_authorized(
                    execution, resolved_input, authorize_network,
                )
            schema = (automation.get("result") or {}).get("schema") or {"type": "object"}
            errors = sorted(Draft202012Validator(schema).iter_errors(artifact), key=lambda item: list(item.path))
            if errors:
                raise ValueError("Artifact 不符合结果 Schema: " + "; ".join(error.message for error in errors[:5]))
            finished = self.store.finish_run(owner, run["id"], status="succeeded", artifact=artifact, logs=logs)
            delivery_results = []
            for binding in bindings:
                validated = self.validate_binding(owner, binding["id"])
                if validated["status"] == "invalid":
                    message = "; ".join(
                        str(issue.get("message") or "Binding 校验失败")
                        for issue in validated["validationIssues"][:5]
                    )
                    self.store.set_widget_status(owner, binding["widgetId"], "error", message)
                    delivery_results.append({"bindingId": binding["id"], "status": "failed",
                                             "issues": validated["validationIssues"]})
                    continue
                widget = self.store.get_widget(owner, binding["widgetId"])
                slot_schema = ((widget.get("slots") or {}).get("main") or {}).get("schema") or {"type": "object"}
                slot_errors = list(Draft202012Validator(slot_schema).iter_errors(artifact))
                if slot_errors:
                    self.store.set_widget_status(
                        owner, binding["widgetId"], "error", slot_errors[0].message
                    )
                    delivery_results.append({"bindingId": binding["id"], "status": "failed",
                                             "issues": [{"code": "schema_mismatch", "message": slot_errors[0].message}]})
                    continue
                self.store.set_widget_delivery(owner, binding["widgetId"], data={"main": artifact},
                                               status="idle", run_id=run["id"])
                self.store.set_binding_validation(owner, binding["id"], "valid", [])
                delivery_results.append({"bindingId": binding["id"], "widgetId": binding["widgetId"],
                                         "slot": "main", "status": "succeeded"})
            finished["deliveryResults"] = delivery_results
            return finished
        except Exception as exc:  # noqa: BLE001 - 运行失败必须统一固化为可检查的终态记录
            error = safe_public_error(exc, "站点自动化运行失败")
            failed = self.store.finish_run(owner, run["id"], status="failed", error=error)
            for binding in bindings:
                self.store.set_widget_status(owner, binding["widgetId"], "error", error)
            return failed

    def _trigger_for(self, trigger: dict[str, Any]):
        kind = str(trigger.get("kind") or "manual")
        if kind == "interval":
            return IntervalTrigger(seconds=parse_duration(str(trigger.get("every") or "")), timezone=BJ_TZ)
        if kind == "schedule":
            return CronTrigger.from_crontab(str(trigger.get("cron") or ""), timezone=str(trigger.get("timezone") or BJ_TZ))
        if kind == "once":
            return DateTrigger(run_date=datetime.fromisoformat(str(trigger.get("at") or "")), timezone=BJ_TZ)
        return None

    async def start(self) -> None:
        if self._scheduler is not None:
            return
        self._scheduler = AsyncIOScheduler(timezone=BJ_TZ)
        self._scheduler.start()
        for owner, automation_id in self.store.list_enabled_automations():
            self.sync_schedule(owner, automation_id)

    async def stop(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def sync_schedule(self, owner: str, automation_id: str) -> None:
        if self._scheduler is None:
            return
        job_id = f"blueprint:{owner}:{automation_id}"
        if self._scheduler.get_job(job_id):
            self._scheduler.remove_job(job_id)
        automation = self.store.get_automation(owner, automation_id)
        trigger = self._trigger_for(automation["trigger"])
        if automation["enabled"] and trigger is not None:
            self._scheduler.add_job(self.run_automation, trigger=trigger, id=job_id,
                                    kwargs={"owner": owner, "automation_id": automation_id,
                                            "trigger_kind": "scheduled"}, replace_existing=True,
                                    max_instances=1, coalesce=True)

    def remove_schedule(self, owner: str, automation_id: str) -> None:
        if self._scheduler is None:
            return
        job_id = f"blueprint:{owner}:{automation_id}"
        if self._scheduler.get_job(job_id):
            self._scheduler.remove_job(job_id)

    @staticmethod
    def runtime_html(widget: dict[str, Any], placement: dict[str, Any] | None = None) -> str:
        entry = Path(widget["workspacePath"]) / "index.html"
        if not entry.is_file():
            raise KeyError("Widget 尚未写入 index.html")
        html = entry.read_text(encoding="utf-8")
        state = placement.get("viewState", {}) if placement else {}
        payload = json.dumps({
            "widgetId": widget["id"], "title": widget["title"], "data": widget["latestData"],
            "status": widget["status"], "error": widget["error"], "lastRun": widget["lastRun"],
            "inputState": widget["inputState"], "viewState": state,
            "canvasId": placement.get("canvasId", "") if placement else "",
            "mountId": placement.get("mountId", "") if placement else "",
        }, ensure_ascii=False).replace("<", "\\u003c")
        bridge = f"""<script data-ace-widget-runtime>
(() => {{
  const initial = {payload};
  const dataListeners = new Set(), statusListeners = new Set(), viewListeners = new Set();
  const host = {{ widgetId: initial.widgetId, title: initial.title, data: initial.data,
    status: initial.status, error: initial.error, lastRun: initial.lastRun,
    inputState: initial.inputState, theme: 'light', tokens: {{}}, getToken: () => '',
    onDataChange(fn) {{ dataListeners.add(fn); fn(host.data); return () => dataListeners.delete(fn); }},
    onStatusChange(fn) {{ statusListeners.add(fn); fn(); return () => statusListeners.delete(fn); }},
    onThemeChange() {{ return () => {{}}; }},
    saveInput(value) {{ parent.postMessage({{type:'ace-widget-save-input',widgetId:initial.widgetId,value}},'*'); }},
    emit(name, value) {{ parent.postMessage({{type:'ace-widget-emit',widgetId:initial.widgetId,name,value}},'*'); }} }};
  const canvas = {{ canvasId: initial.canvasId, mountId: initial.mountId, widgetId: initial.widgetId,
    viewState: initial.viewState,
    setViewState(value) {{ canvas.viewState=value; parent.postMessage({{type:'ace-widget-view-state',mountId:initial.mountId,value}},'*'); }},
    onViewStateChange(fn) {{ viewListeners.add(fn); return () => viewListeners.delete(fn); }} }};
  window.DaimonWidget = host; window.DaimonCanvas = canvas;
  addEventListener('message', event => {{
    if (event.data?.type === 'ace-blueprint-annotation-mode') {{
      document.documentElement.dataset.aceAnnotationMode = event.data.enabled ? 'true' : 'false';
    }}
    if (event.data?.type === 'ace-widget-data' && event.data.widgetId === initial.widgetId) {{
      host.data=event.data.data; host.status=event.data.status; host.error=event.data.error || '';
      host.lastRun=event.data.lastRun || ''; dataListeners.forEach(fn => fn(host.data)); statusListeners.forEach(fn => fn());
    }}
    if (event.data?.type === 'ace-widget-view-state' && event.data.mountId === initial.mountId) {{
      canvas.viewState=event.data.value || {{}}; viewListeners.forEach(fn => fn());
    }}
  }});
  let annotated = null;
  document.addEventListener('mouseover', event => {{
    if (document.documentElement.dataset.aceAnnotationMode !== 'true' || !(event.target instanceof Element)) return;
    if (annotated && annotated !== event.target) annotated.style.outline = '';
    annotated = event.target; annotated.style.outline = '2px solid #5b7cff';
  }}, true);
  document.addEventListener('click', event => {{
    if (document.documentElement.dataset.aceAnnotationMode !== 'true' || !(event.target instanceof Element)) return;
    event.preventDefault(); event.stopPropagation();
    const el = event.target, parts = [];
    for (let node = el; node && node.nodeType === 1 && parts.length < 6; node = node.parentElement) {{
      let part = node.tagName.toLowerCase();
      if (node.id) {{ part += '#' + CSS.escape(node.id); parts.unshift(part); break; }}
      const siblings = node.parentElement ? [...node.parentElement.children].filter(x => x.tagName === node.tagName) : [];
      if (siblings.length > 1) part += `:nth-of-type(${{siblings.indexOf(node) + 1}})`;
      parts.unshift(part);
    }}
    parent.postMessage({{type:'ace-blueprint-element-selected',widgetId:initial.widgetId,
      canvasId:initial.canvasId,mountId:initial.mountId,payload:{{route:location.pathname + location.search,
      selector:parts.join(' > '),element_tag:el.tagName.toLowerCase(),
      element_text:(el.textContent || '').trim().slice(0,2000)}}}},'*');
  }}, true);
  parent.postMessage({{type:'ace-widget-ready',widgetId:initial.widgetId,mountId:initial.mountId}},'*');
}})();
</script>"""
        lower = html.lower()
        position = lower.find("<head>")
        if position >= 0:
            position += len("<head>")
            return html[:position] + bridge + html[position:]
        return bridge + html
