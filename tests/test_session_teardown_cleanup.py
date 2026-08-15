"""删会话清账：teardown 回收摘要/记忆/ACP/uploads，以及 cron 守卫。"""

from __future__ import annotations

import os

import pytest
from starlette.testclient import TestClient

from crew.agent.compact.store import SummaryState
from crew.agent.external.store import ExternalAgentStore
from crew.agent.plan import write_plan
from crew.app import build_app
from crew.core.runctx import current_owner_account_id
from crew.core.types import Message
from crew.gateway.context import delete_session_uploads, save_upload
from crew.gateway.server import create_app
from crew.memory.simple import SQLiteMemory
from crew.state.config import Config

OWNER = "A:uid-a"


def _client(tmp_path):
    os.environ["CREW_HOME"] = str(tmp_path / ".crew")
    crew_home = tmp_path / ".crew"
    app = build_app(
        config=Config(
            api_key="",
            db_path=str(crew_home / "crew_data" / "crew.db"),
            memory_db_path=str(crew_home / "crew_data" / "memory.db"),
            log_level="INFO",
            cron_enabled=True,
        ),
        enable_team=False,
    )
    return TestClient(create_app(crew=app)), app


async def test_memory_delete_clears_session_rows(tmp_path):
    mem = SQLiteMemory(str(tmp_path / "m.db"))
    token = current_owner_account_id.set(OWNER)
    try:
        await mem.write("s-del", [Message.user("我喜欢用 Python")])
        assert "Python" in await mem.prefetch("s-del", "Python")
        await mem.delete("s-del", owner_account_id=OWNER)
        assert await mem.prefetch("s-del", "Python") == ""
    finally:
        current_owner_account_id.reset(token)


def test_acp_delete_bindings_for_session(tmp_path):
    store = ExternalAgentStore(str(tmp_path / "crew.db"))
    store.save_acp_session_binding(
        crew_session_id="s-acp",
        external_agent_id="agent-1",
        runtime_id="rt-1",
        provider="hermes",
        acp_session_id="ext-1",
        cwd="/tmp",
    )
    store.save_acp_session_binding(
        crew_session_id="s-other",
        external_agent_id="agent-1",
        runtime_id="rt-1",
        provider="hermes",
        acp_session_id="ext-2",
        cwd="/tmp",
    )
    n = store.delete_acp_bindings_for_session("s-acp")
    assert n == 1
    assert store.get_acp_session_binding(
        crew_session_id="s-acp",
        external_agent_id="agent-1",
        runtime_id="rt-1",
        provider="hermes",
        cwd="/tmp",
    ) is None
    assert store.get_acp_session_binding(
        crew_session_id="s-other",
        external_agent_id="agent-1",
        runtime_id="rt-1",
        provider="hermes",
        cwd="/tmp",
    ) is not None


def test_delete_session_uploads_only_under_uploads(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    meta = save_upload("a.png", b"png-bytes", owner_account_id=OWNER)
    path = meta["path"]
    assert os.path.isfile(path)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    msgs = [
        Message.user(f"附件「a.png」位于: {path}"),
        Message.user(f"附件「evil」位于: {outside}"),
    ]
    n = delete_session_uploads(msgs, owner_account_id=OWNER)
    assert n == 1
    assert not os.path.isfile(path)
    assert outside.is_file()


@pytest.mark.asyncio
async def test_delete_session_clears_summary_memory_acp_uploads(tmp_path, auth_headers):
    client, app = _client(tmp_path)
    sid = "teardown_full"
    upload = save_upload("pic.png", b"img", owner_account_id=OWNER)
    app.session_store.save(
        sid,
        [Message.user(f"附件「pic.png」位于: {upload['path']}")],
        owner_account_id=OWNER,
    )
    app.summary_store.put(
        sid,
        SummaryState(text="旧摘要", covered_count=3),
        owner_account_id=OWNER,
    )
    token = current_owner_account_id.set(OWNER)
    try:
        await app.memory.write(sid, [Message.user("记住我喜欢 Rust")])
    finally:
        current_owner_account_id.reset(token)

    if app.external_agents is not None:
        app.external_agents.save_acp_session_binding(
            crew_session_id=sid,
            external_agent_id="a1",
            runtime_id="r1",
            provider="hermes",
            acp_session_id="e1",
            cwd="",
        )

    write_plan(sid, "# plan", owner_account_id=OWNER)
    res = client.delete(f"/api/session/{sid}", headers=auth_headers)
    assert res.status_code == 200
    assert res.json().get("ok") is True
    assert app.summary_store.get(sid, owner_account_id=OWNER) is None
    assert not os.path.isfile(upload["path"])
    hit = await app.memory.prefetch(sid, "Rust")
    assert hit == ""
    if app.external_agents is not None:
        assert (
            app.external_agents.get_acp_session_binding(
                crew_session_id=sid,
                external_agent_id="a1",
                runtime_id="r1",
                provider="hermes",
                cwd="",
            )
            is None
        )


def test_delete_session_blocked_by_enabled_cron(tmp_path, auth_headers):
    client, app = _client(tmp_path)
    sid = "cron_block"
    app.session_store.save(sid, [Message.user("有定时任务")], owner_account_id=OWNER)
    assert app.cron_store is not None
    app.cron_store.create(
        name="每日汇报",
        schedule="every 30m",
        query="写日报",
        session_id=sid,
        owner_account_id=OWNER,
    )
    res = client.delete(f"/api/session/{sid}", headers=auth_headers)
    assert res.status_code == 409
    body = res.json()
    assert body.get("code") == "cron_active"
    assert "定时任务" in body.get("error", "")
    assert app.session_store.session_belongs_to(sid, OWNER)

    # 停用后可删，且级联清掉 cron 元数据（含已停用行）
    jobs = app.cron_store.list(session_id=sid, owner_account_id=OWNER)
    assert len(jobs) == 1
    app.cron_store.set_enabled(jobs[0]["id"], False, owner_account_id=OWNER)
    res2 = client.delete(f"/api/session/{sid}", headers=auth_headers)
    assert res2.status_code == 200
    assert not app.session_store.session_belongs_to(sid, OWNER)
    assert app.cron_store.list(session_id=sid, owner_account_id=OWNER) == []


def test_task_runtime_unlink_and_prune_removes_disk(tmp_path, monkeypatch):
    from crew.tasks.runtime import TaskRuntime
    from crew.state.home import get_owner_runtime_home

    db = tmp_path / "t.db"
    monkeypatch.setenv("CREW_HOME", str(tmp_path / "home"))
    tasks_dir = get_owner_runtime_home(OWNER) / "tasks"
    tasks_dir.mkdir(parents=True)
    runtime = TaskRuntime(str(db), finished_retention_days=0)  # prune 关闭时测 unlink
    log_path = tasks_dir / "task_abc.log"
    log_path.write_text("log", encoding="utf-8")
    json_path = tasks_dir / "task_abc.json"
    json_path.write_text("{}", encoding="utf-8")
    task = runtime.create_runtime(
        kind="shell",
        session_id="s-task",
        owner_account_id=OWNER,
        title="echo",
        output_ref=str(log_path),
    )
    # 覆盖 task_id 文件名不便；unlink 会尝试 output_ref 旁 {task_id}.json
    tid = task["task_id"]
    side = tasks_dir / f"{tid}.json"
    side.write_text("{}", encoding="utf-8")
    runtime.update(tid, owner_account_id=OWNER, output_ref=str(log_path))
    n = runtime.unlink_session_output_files("s-task", owner_account_id=OWNER)
    assert n >= 1
    assert not log_path.exists()

    # prune_finished：设 retention 并造过期行
    runtime.finished_retention_days = 1
    log2 = tasks_dir / "old.log"
    log2.write_text("x", encoding="utf-8")
    t2 = runtime.create_runtime(
        kind="shell",
        session_id="s-old",
        owner_account_id=OWNER,
        title="old",
        output_ref=str(log2),
    )
    import time

    old_ts = time.time() - 10 * 86400
    runtime.finish(
        t2["task_id"],
        owner_account_id=OWNER,
        status="completed",
        result="ok",
    )
    # 把 finished_at 拨到很久以前
    with runtime._lock:
        runtime._conn.execute(
            "UPDATE runtime_tasks SET finished_at = ? WHERE task_id = ?",
            (old_ts, t2["task_id"]),
        )
        runtime._conn.commit()
    deleted = runtime.prune_finished()
    assert deleted >= 1
    assert not log2.exists()
