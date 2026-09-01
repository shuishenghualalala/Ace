"""通知中心：store CRUD / 裁剪、service 推送容错、cron/approval 来源接入、REST 路由。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from crew.core.followup import (
    cancel_followup,
    resolve_answer,
    send_followup_question_to,
    set_followup_notification_hooks,
    wait_for_answer,
)
from crew.core.interfaces import Notification
from crew.cron import CronJobStore, CronService
from crew.gateway.auth import AccountContext
from crew.gateway.routers.notifications import create_notifications_router
from crew.notifications import NotificationCenterService, NotificationStore
from crew.security.actions import normalize_exec_action
from crew.security.approvals import ApprovalDecision, ApprovalManager
from crew.security.audit import SQLiteSecurityAudit
from crew.security.context import SecurityContext
from crew.security.grants import GrantRegistry
from crew.security.rule_store import SQLiteRuleStore
from crew.security.service import SecurityApprovalService

OWNER = "A:uid-a"


def _notification(source: str = "cron", kind: str = "cron_run_completed", **kwargs) -> Notification:
    defaults = {
        "owner_account_id": OWNER,
        "source": source,
        "kind": kind,
        "title": "标题",
        "body": "正文",
    }
    defaults.update(kwargs)
    return Notification(**defaults)


def _store(tmp_path: Path) -> NotificationStore:
    return NotificationStore(str(tmp_path / "crew.db"))


# ---- store ----

def test_store_publish_list_and_unread_count(tmp_path):
    store = _store(tmp_path)
    first = store.insert(_notification(title="一"))
    second = store.insert(_notification(title="二"))

    items = store.list(OWNER)
    assert [item.title for item in items] == ["二", "一"]  # created_at 倒序
    assert items[0].id and items[0].created_at > 0
    assert store.unread_count(OWNER) == 2

    # owner 隔离
    assert store.list("B:uid-b") == []
    assert store.unread_count("B:uid-b") == 0

    assert first.id != second.id


def test_store_list_pagination_and_unread_only(tmp_path):
    store = _store(tmp_path)
    ids = [store.insert(_notification(title=f"n{i}")).id for i in range(5)]
    store.mark_read(OWNER, ids[3])

    page = store.list(OWNER, limit=2, offset=1)
    assert [item.title for item in page] == ["n3", "n2"]

    unread = store.list(OWNER, unread_only=True)
    assert len(unread) == 4
    assert all(item.read_at is None for item in unread)


def test_store_mark_read_and_mark_all_read(tmp_path):
    store = _store(tmp_path)
    a = store.insert(_notification())
    b = store.insert(_notification())

    assert store.mark_read(OWNER, a.id) is True
    assert store.mark_read(OWNER, a.id) is False  # 已读后再标为 no-op
    assert store.mark_read(OWNER, "不存在") is False
    assert store.mark_read("B:uid-b", b.id) is False  # 不能跨 owner 已读
    assert store.unread_count(OWNER) == 1

    assert store.mark_all_read(OWNER) == 1
    assert store.unread_count(OWNER) == 0


def test_store_mark_read_by_payload(tmp_path):
    store = _store(tmp_path)
    hit = store.insert(
        _notification(source="approval", kind="approval_pending", payload={"request_id": "req-1"})
    )
    store.insert(_notification(source="approval", payload={"request_id": "req-2"}))
    store.insert(_notification(source="cron", payload={"request_id": "req-1"}))  # source 不同不受影响

    assert store.mark_read_by_payload("approval", "req-1", owner_account_id=OWNER) == 1
    by_id = {item.id: item for item in store.list(OWNER)}
    assert by_id[hit.id].read_at is not None
    assert store.unread_count(OWNER) == 2


def test_store_clear(tmp_path):
    store = _store(tmp_path)
    store.insert(_notification())
    store.insert(_notification())
    store.insert(_notification(owner_account_id="B:uid-b"))

    assert store.clear(OWNER) == 2
    assert store.list(OWNER) == []
    assert len(store.list("B:uid-b")) == 1


def test_store_trims_to_max_per_source(tmp_path):
    store = _store(tmp_path)
    for i in range(205):
        store.insert(_notification(title=f"n{i}"), max_per_source=200)

    items = store.list(OWNER, limit=500)
    assert len(items) == 200
    assert items[0].title == "n204"  # 最旧的被裁掉
    assert items[-1].title == "n5"

    # 裁剪按 (owner, source) 独立：其它来源不受影响
    store.insert(_notification(source="tasks", kind="task_completed"))
    assert len(store.list(OWNER, limit=500)) == 201


def test_store_payload_roundtrip(tmp_path):
    store = _store(tmp_path)
    saved = store.insert(_notification(payload={"session_id": "s-1", "n": 1}))
    loaded = store.list(OWNER)[0]
    assert loaded.id == saved.id
    assert loaded.payload == {"session_id": "s-1", "n": 1}
    assert store.insert(_notification(payload=None)).payload is None


# ---- service ----

def test_service_publish_pushes_notification_frame(tmp_path):
    pushed: list[tuple[str, dict]] = []
    service = NotificationCenterService(_store(tmp_path), push_fn=lambda owner, payload: pushed.append((owner, payload)))

    saved = service.publish(_notification(title="hello"))

    assert len(pushed) == 1
    owner, frame = pushed[0]
    assert owner == OWNER
    assert frame["kind"] == "notification"
    assert frame["notification"]["id"] == saved.id
    assert frame["notification"]["title"] == "hello"


async def test_service_publish_awaits_async_push_fn(tmp_path):
    pushed: list[dict] = []

    async def push(owner: str, payload: dict) -> None:
        pushed.append(payload)

    service = NotificationCenterService(_store(tmp_path), push_fn=push)
    service.publish(_notification())
    await asyncio.sleep(0)  # fire-and-forget 任务落地
    await asyncio.sleep(0)
    assert len(pushed) == 1


def test_service_push_failure_does_not_break_publish(tmp_path):
    def boom(owner: str, payload: dict) -> None:
        raise RuntimeError("ws down")

    service = NotificationCenterService(_store(tmp_path), push_fn=boom)
    saved = service.publish(_notification())

    assert saved.id
    assert service.unread_count(OWNER) == 1  # 写库不受推送失败影响


def test_service_without_push_fn_works(tmp_path):
    service = NotificationCenterService(_store(tmp_path))
    saved = service.publish(_notification())
    assert service.unread_count(OWNER) == 1
    assert service.mark_read(OWNER, saved.id) is True


# ---- cron 来源接入 ----

async def _run_one_cron_job(
    tmp_path: Path,
    runner,
    *,
    on_run_finished=None,
) -> tuple[CronJobStore, CronService]:
    store = CronJobStore(str(tmp_path / "cron.db"))
    service = CronService(store, runner)
    if on_run_finished is not None:
        service.set_on_run_finished(on_run_finished)
    await service.start()
    service.mount_owner(OWNER)
    store.create(
        name="t",
        schedule="in 0s",
        query="x",
        session_id="s1",
        owner_account_id=OWNER,
    )
    await service.tick()
    await service.stop()
    return store, service


async def test_cron_run_finished_callback_on_completed_and_failed(tmp_path):
    events: list[dict] = []

    async def ok_runner(env):
        return None

    store, _ = await _run_one_cron_job(tmp_path / "ok", ok_runner, on_run_finished=events.append)
    assert len(events) == 1
    info = events[0]
    assert info["status"] == "completed"
    assert info["error_message"] == ""
    assert info["owner_account_id"] == OWNER
    assert info["job_name"] == "t"
    assert info["session_id"] == "s1"
    assert info["job_id"]

    async def fail_runner(env):
        raise RuntimeError("boom")

    _, _ = await _run_one_cron_job(tmp_path / "fail", fail_runner, on_run_finished=events.append)
    assert events[1]["status"] == "failed"
    assert "boom" in events[1]["error_message"]


async def test_cron_without_callback_still_runs(tmp_path):
    async def fail_runner(env):
        raise RuntimeError("boom")

    store, _ = await _run_one_cron_job(tmp_path / "bare", fail_runner)
    job = store.list(owner_account_id=OWNER)[0]
    assert job["last_status"].startswith("failed")


async def test_cron_callback_exception_does_not_break_engine(tmp_path):
    def boom(info: dict) -> None:
        raise RuntimeError("notify down")

    async def ok_runner(env):
        return None

    store, _ = await _run_one_cron_job(tmp_path / "cb-boom", ok_runner, on_run_finished=boom)
    assert store.list(owner_account_id=OWNER)[0]["last_status"] == "completed"


# ---- approval 来源接入 ----

def _security_context(tmp_path: Path) -> SecurityContext:
    return SecurityContext(
        os_user="os-a",
        owner_account_id=OWNER,
        workspace_id="project-a",
        workspace_root=tmp_path,
        session_id="session-a",
        request_id="req-a",
        task_id="task-a",
        cwd=tmp_path,
    )


def _security_service(tmp_path: Path) -> SecurityApprovalService:
    grants = GrantRegistry()
    approvals = ApprovalManager(grants)
    return SecurityApprovalService(
        approvals,
        grants,
        SQLiteRuleStore(tmp_path / "rules.db"),
        SQLiteSecurityAudit(tmp_path / "audit.db"),
        db_path=tmp_path / "crew.db",
    )


def test_approval_created_publishes_and_decide_marks_read(tmp_path):
    center = NotificationCenterService(_store(tmp_path))
    service = _security_service(tmp_path)

    def on_created(context, request: dict) -> None:
        center.publish(
            Notification(
                owner_account_id=context.owner_account_id,
                source="approval",
                kind="approval_pending",
                title="有一个操作等待审批",
                payload={"request_id": request["request_id"]},
            )
        )

    service.set_notification_hooks(
        on_created=on_created,
        on_decided=lambda owner, request_id: center.mark_read_by_payload(
            "approval", request_id, owner_account_id=owner
        ),
    )

    context = _security_context(tmp_path)
    action = normalize_exec_action(["git", "status"], tmp_path)
    public = service.request_action(context, action, tool_name="terminal", risk_class="exec")

    assert center.unread_count(OWNER) == 1
    assert center.list(OWNER)[0].payload["request_id"] == public["request_id"]

    service.decide(
        context,
        request_id=public["request_id"],
        nonce=public["nonce"],
        decision=ApprovalDecision.ONCE,
    )
    assert center.unread_count(OWNER) == 0  # 决策后自动已读


def test_approval_reused_request_does_not_duplicate_notification(tmp_path):
    created: list[str] = []
    service = _security_service(tmp_path)
    service.set_notification_hooks(on_created=lambda ctx, req: created.append(req["request_id"]))

    context = _security_context(tmp_path)
    action = normalize_exec_action(["git", "status"], tmp_path)
    first = service.request_action(context, action, tool_name="terminal", risk_class="exec")
    second = service.request_action(context, action, tool_name="terminal", risk_class="exec")

    assert first["request_id"] == second["request_id"]
    assert created == [first["request_id"]]  # 复用已有请求不重复通知


def test_approval_without_hooks_still_works(tmp_path):
    service = _security_service(tmp_path)
    context = _security_context(tmp_path)
    action = normalize_exec_action(["git", "status"], tmp_path)
    public = service.request_action(context, action, tool_name="terminal", risk_class="exec")
    result = service.decide(
        context,
        request_id=public["request_id"],
        nonce=public["nonce"],
        decision=ApprovalDecision.REJECT,
    )
    assert result["status"] == "rejected"


# ---- followup 来源接入 ----

def _wire_followup_hooks(center: NotificationCenterService) -> None:
    """按 app.py 装配语义接线：pending 发通知，resolved 按 payload 自动已读。"""

    def on_pending(session_id: str, question_id: str, text: str) -> None:
        center.publish(
            Notification(
                owner_account_id=OWNER,
                source="followup",
                kind="followup_pending",
                title="有一个问题等待你回答",
                body=text[:200],
                payload={"question_id": question_id, "session_id": session_id},
            )
        )

    def on_resolved(session_id: str, question_id: str) -> None:
        center.mark_read_by_payload("followup", question_id, owner_account_id=OWNER)

    set_followup_notification_hooks(on_pending=on_pending, on_resolved=on_resolved)


async def _send_one_followup(session_id: str = "s-f1", **kwargs):
    pushed: list[dict] = []

    async def push_fn(sid: str, payload: dict) -> None:
        pushed.append(payload)

    sid, question_id = await send_followup_question_to(
        session_id,
        [{"question": "选哪个方案？", "options": ["方案A", "方案B"]}],
        push_fn=push_fn,
        **kwargs,
    )
    assert pushed and pushed[0]["kind"] == "followup_question"
    return sid, question_id


async def test_followup_send_publishes_and_resolve_marks_read(tmp_path):
    center = NotificationCenterService(_store(tmp_path))
    _wire_followup_hooks(center)
    try:
        sid, question_id = await _send_one_followup()
        assert center.unread_count(OWNER) == 1
        item = center.list(OWNER)[0]
        assert item.source == "followup"
        assert item.kind == "followup_pending"
        assert item.title == "有一个问题等待你回答"
        assert item.body == "选哪个方案？"
        assert item.payload == {"question_id": question_id, "session_id": sid}

        waiter = asyncio.create_task(wait_for_answer(sid, question_id, timeout=5))
        await asyncio.sleep(0)
        assert resolve_answer(sid, question_id, [{"id": "q0", "answers": ["方案A"]}]) is True
        await waiter
        assert center.unread_count(OWNER) == 0  # 回答后自动已读
    finally:
        set_followup_notification_hooks()


async def test_followup_side_channel_does_not_publish(tmp_path):
    """record_history=False 是权限确认 side-channel UI，由 approval 来源覆盖，不重复通知。"""
    center = NotificationCenterService(_store(tmp_path))
    _wire_followup_hooks(center)
    try:
        sid, question_id = await _send_one_followup("s-f2", record_history=False)
        assert center.unread_count(OWNER) == 0
        # side-channel 结束时的 resolved 触发不得误伤其它通知（此处本就无通知，仅验证不报错）
        cancel_followup(sid, question_id)
        assert center.unread_count(OWNER) == 0
    finally:
        set_followup_notification_hooks()


async def test_followup_cancel_marks_read(tmp_path):
    center = NotificationCenterService(_store(tmp_path))
    _wire_followup_hooks(center)
    try:
        sid, question_id = await _send_one_followup("s-f3")
        assert center.unread_count(OWNER) == 1
        assert cancel_followup(sid, question_id) is True
        assert center.unread_count(OWNER) == 0
    finally:
        set_followup_notification_hooks()


async def test_followup_timeout_marks_read(tmp_path):
    center = NotificationCenterService(_store(tmp_path))
    _wire_followup_hooks(center)
    try:
        sid, question_id = await _send_one_followup("s-f4")
        assert center.unread_count(OWNER) == 1
        answers = await wait_for_answer(sid, question_id, timeout=0.05)
        assert answers == []  # 超时回空答案
        assert center.unread_count(OWNER) == 0
    finally:
        set_followup_notification_hooks()


async def test_followup_hook_failure_does_not_break_flow(tmp_path):
    """通知钩子异常不得打断追问主流程（fire-and-forget）。"""
    def boom(*args) -> None:
        raise RuntimeError("notify down")

    set_followup_notification_hooks(on_pending=boom, on_resolved=boom)
    try:
        sid, question_id = await _send_one_followup("s-f5")  # pending 钩子爆炸不影响发送
        waiter = asyncio.create_task(wait_for_answer(sid, question_id, timeout=5))
        await asyncio.sleep(0)
        assert resolve_answer(sid, question_id, [{"id": "q0", "answers": ["方案A"]}]) is True
        answers = await waiter  # resolved 钩子爆炸不影响回答回灌
        assert answers == [{"id": "q0", "answers": ["方案A"]}]
    finally:
        set_followup_notification_hooks()


# ---- plan 来源接入 ----

def _app_with_notifications(center: NotificationCenterService):
    """绕过 CrewApp 重型 __init__，仅挂 notifications 供来源方法使用。"""
    from crew.app import CrewApp

    app = CrewApp.__new__(CrewApp)
    app.notifications = center
    return app


def test_plan_review_publishes_and_decision_marks_read(tmp_path):
    center = NotificationCenterService(_store(tmp_path))
    app = _app_with_notifications(center)
    long_plan = "# 实施计划\n" + "步骤详情。" * 100  # 超过 200 字需截断
    review = {"plan": long_plan, "empty": False, "phase": "review", "status": "pending"}

    app.publish_plan_review_notification("s-p1", OWNER, review)
    assert center.unread_count(OWNER) == 1
    item = center.list(OWNER)[0]
    assert item.source == "plan"
    assert item.kind == "plan_review_pending"
    assert item.title == "有一个计划等待你批准"
    assert len(item.body) == 200
    assert item.payload == {"session_id": "s-p1"}

    app.mark_plan_review_notification_read("s-p1", OWNER)
    assert center.unread_count(OWNER) == 0  # 批准/拒绝后自动已读


def test_plan_empty_review_does_not_publish(tmp_path):
    """empty=True 是「计划为空」提示卡（无审批按钮），纯信息展示不进待办。"""
    center = NotificationCenterService(_store(tmp_path))
    app = _app_with_notifications(center)

    app.publish_plan_review_notification(
        "s-p2", OWNER, {"plan": None, "empty": True, "phase": "active", "status": "empty"}
    )
    app.publish_plan_review_notification("s-p2", OWNER, None)
    assert center.unread_count(OWNER) == 0


# ---- task 完成回调：会恢复 turn 的中间任务不发通知 ----

class _TaskRuntimeStub:
    """仅实现 _on_task_completion 用到的 TaskRuntime 接口。"""

    def __init__(self) -> None:
        self._loop = None
        self.resume_enqueued: list[str] = []

    def mark_notified(self, task_id: str, owner_account_id: str = "") -> bool:
        return True

    def mark_resume_enqueued(self, task_id: str, owner_account_id: str = "") -> bool:
        self.resume_enqueued.append(task_id)
        return True


class _SessionStoreStub:
    """按固定状态回答 get_status，模拟 turn 进行中 / 已结束。"""

    def __init__(self, status: str) -> None:
        self._status = status

    def get_status(self, session_id: str, owner_account_id: str = ""):
        return self._status, None


def _app_for_task_completion(center: NotificationCenterService, session_status: str):
    app = _app_with_notifications(center)
    app._push_fn = None
    app.tasks = _TaskRuntimeStub()
    app.session_store = _SessionStoreStub(session_status)
    return app


def _completed_shell_task(**kwargs) -> dict:
    task = {
        "task_id": "t-shell-1",
        "owner_account_id": OWNER,
        "kind": "shell",
        "backgrounded": True,
        "status": "completed",
        "session_id": "s-task-1",
        "result": "done",
    }
    task.update(kwargs)
    return task


def test_task_completion_resume_eligible_skips_notification(tmp_path):
    """后台 shell 任务会恢复 turn（会话仍在运行）→ 中间步骤不发通知。"""
    center = NotificationCenterService(_store(tmp_path))
    app = _app_for_task_completion(center, session_status="running")

    app._on_task_completion(_completed_shell_task())

    assert center.unread_count(OWNER) == 0
    assert app.tasks.resume_enqueued == ["t-shell-1"]  # resume 逻辑不受影响


def test_task_completion_turn_over_publishes_notification(tmp_path):
    """后台 shell 任务完成时会话已停止（turn 结束）→ 正常发通知。"""
    center = NotificationCenterService(_store(tmp_path))
    app = _app_for_task_completion(center, session_status="stopped")

    app._on_task_completion(_completed_shell_task())

    assert center.unread_count(OWNER) == 1
    assert app.tasks.resume_enqueued == []
    item = center.list(OWNER)[0]
    assert item.source == "tasks"
    assert item.kind == "task_completed"
    assert item.payload["task_id"] == "t-shell-1"


def test_task_completion_non_resume_kind_publishes_notification(tmp_path):
    """非 shell/subagent 类型的任务完成 → 保持原有行为，正常发通知。"""
    center = NotificationCenterService(_store(tmp_path))
    app = _app_for_task_completion(center, session_status="running")

    app._on_task_completion(_completed_shell_task(kind="browser"))

    assert center.unread_count(OWNER) == 1
    assert app.tasks.resume_enqueued == []
    assert center.list(OWNER)[0].payload["task_kind"] == "browser"


def test_task_completion_foreground_never_notifies(tmp_path):
    """前台执行（未转后台）完成一律不通知：回合与工具调用在会话里已内联展示。"""
    center = NotificationCenterService(_store(tmp_path))
    app = _app_for_task_completion(center, session_status="running")

    app._on_task_completion(_completed_shell_task(kind="agent_turn", backgrounded=False))
    app._on_task_completion(_completed_shell_task(task_id="t-shell-2", backgrounded=False))

    assert center.unread_count(OWNER) == 0


def test_task_completion_backgrounded_resume_turn_notifies(tmp_path):
    """后台工作唤醒的恢复回合（agent_turn 且 backgrounded）结束 → 发最终通知。"""
    center = NotificationCenterService(_store(tmp_path))
    app = _app_for_task_completion(center, session_status="running")

    app._on_task_completion(_completed_shell_task(kind="agent_turn", result="最终答复"))

    assert center.unread_count(OWNER) == 1
    item = center.list(OWNER)[0]
    assert item.payload["task_kind"] == "agent_turn"
    assert item.body == "最终答复"


# ---- REST 路由 ----

def _router_client(tmp_path: Path) -> tuple[TestClient, NotificationCenterService]:
    center = NotificationCenterService(_store(tmp_path))

    class _Crew:
        notifications = center

    app = FastAPI()

    @app.middleware("http")
    async def _fake_auth(request, call_next):
        request.state.account = AccountContext(owner_account_id=OWNER, is_local=True)
        return await call_next(request)

    app.include_router(create_notifications_router(_Crew()))
    return TestClient(app), center


def test_notifications_router_contract(tmp_path):
    client, center = _router_client(tmp_path)
    saved = center.publish(_notification(title="t1", payload={"session_id": "s-1"}))
    center.publish(_notification(title="t2"))

    response = client.get("/api/notifications")
    assert response.status_code == 200
    data = response.json()
    assert data["unread_count"] == 2
    assert [item["title"] for item in data["notifications"]] == ["t2", "t1"]
    first = data["notifications"][0]
    assert set(first) == {"id", "source", "kind", "title", "body", "payload", "created_at", "read_at"}
    assert first["read_at"] is None

    response = client.get("/api/notifications/unread-count")
    assert response.json() == {"unread_count": 2}

    response = client.post(f"/api/notifications/{saved.id}/read")
    assert response.json() == {"ok": True}
    assert client.get("/api/notifications/unread-count").json() == {"unread_count": 1}

    response = client.post("/api/notifications/read-all")
    assert response.json() == {"ok": True}
    assert client.get("/api/notifications/unread-count").json() == {"unread_count": 0}

    response = client.delete("/api/notifications")
    assert response.json() == {"ok": True}
    assert client.get("/api/notifications").json()["notifications"] == []


def test_notifications_router_unread_only_and_pagination(tmp_path):
    client, center = _router_client(tmp_path)
    saved = [center.publish(_notification(title=f"n{i}")) for i in range(4)]
    center.mark_read(OWNER, saved[2].id)

    data = client.get("/api/notifications", params={"unread_only": "true"}).json()
    assert [item["title"] for item in data["notifications"]] == ["n3", "n1", "n0"]

    data = client.get("/api/notifications", params={"limit": 2, "offset": 2}).json()
    assert [item["title"] for item in data["notifications"]] == ["n1", "n0"]
