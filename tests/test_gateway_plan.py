"""网关 WS 层的 Plan 模式控制测试（plan_enter / plan_reject）。

用 FakeProvider（无 key），经 TestClient 的 websocket 验证 action 路由：
进入/拒绝会更新 PlanModeManager 状态并回一帧 status。plan_approve 会起一轮执行
（依赖模型行为），不在此单元测试覆盖——见 tests/e2e_plan_mode.py。
"""

import os

os.environ.setdefault("CREW_API_KEY", "")  # 确保走 FakeProvider

from starlette.testclient import TestClient

from crew.agent.plan import write_plan
from crew.app import build_app
from crew.core.types import Message
from crew.gateway.server import create_app
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
        ),
        enable_team=False,
    )
    return TestClient(create_app(crew=app)), app


def test_ws_plan_enter_and_reject(tmp_path, auth_headers, send_ws_json):
    client, app = _client(tmp_path)
    sid = "ws_plan"
    app.session_store.save(
        sid,
        [Message(role="user", content="进入计划")],
        owner_account_id=OWNER,
    )
    with client.websocket_connect("/ws", headers=auth_headers) as ws:
        send_ws_json(ws, {"action": "plan_enter", "session_id": sid})
        msg = ws.receive_json()
        assert msg["kind"] == "status"
        assert "Plan 模式" in msg["body"]["message"]
        assert app.plan_manager.is_active(sid, owner_account_id=OWNER)

        # 模型已写计划、进入待审批后，用户在前端点「继续修改」→ plan_reject
        app.plan_manager.request_approval(sid, owner_account_id=OWNER)
        send_ws_json(ws, {"action": "plan_reject", "session_id": sid})
        msg = ws.receive_json()
        assert msg["kind"] == "status"
        assert app.plan_manager.is_active(sid, owner_account_id=OWNER)  # 仍在 plan 模式
        assert not app.plan_manager.is_awaiting_approval(sid, owner_account_id=OWNER)
        assert app.plan_manager.phase(sid, owner_account_id=OWNER) == "revising"

        send_ws_json(ws, {"action": "plan_exit", "session_id": sid})
        msg = ws.receive_json()
        assert msg["kind"] == "status"
        assert not app.plan_manager.is_active(sid, owner_account_id=OWNER)
        assert app.plan_manager.phase(sid, owner_account_id=OWNER) == "cancelled"


def test_ws_plan_reject_and_exit_sets_rejected_phase(
    tmp_path,
    auth_headers,
    send_ws_json,
):
    client, app = _client(tmp_path)
    sid = "ws_plan_reject_exit"
    app.session_store.save(
        sid,
        [Message(role="user", content="进入计划")],
        owner_account_id=OWNER,
    )
    app.plan_manager.enter(sid, owner_account_id=OWNER)
    app.plan_manager.request_approval(sid, owner_account_id=OWNER)
    with client.websocket_connect("/ws", headers=auth_headers) as ws:
        send_ws_json(ws, {"action": "plan_reject_and_exit", "session_id": sid})
        msg = ws.receive_json()
        assert msg["kind"] == "status"
        assert "拒绝计划" in msg["body"]["message"]
        assert not app.plan_manager.is_active(sid, owner_account_id=OWNER)
        assert app.plan_manager.phase(sid, owner_account_id=OWNER) == "rejected"


def test_ws_first_message_plan_active_enters_plan(
    tmp_path,
    auth_headers,
    send_ws_json,
):
    client, app = _client(tmp_path)
    sid = "ws_plan_first_message"
    with client.websocket_connect("/ws", headers=auth_headers) as ws:
        send_ws_json(ws, {
            "session_id": sid,
            "query": "你好",
            "plan_active": True,
            "request_id": "r-plan-first",
        })
        final = None
        model_notice = None
        for _ in range(20):
            msg = ws.receive_json()
            if msg.get("kind") == "status" and "FakeProvider" in str(msg.get("body")):
                model_notice = msg
            if msg.get("kind") == "final":
                final = msg
                break

        assert final is not None
        assert model_notice is not None
        assert "设置 → 模型" in model_notice["body"]["message"]
        assert app.plan_manager.is_active(sid, owner_account_id=OWNER)


def test_session_plan_rest_restores_persisted_plan(tmp_path, auth_headers):
    client, app = _client(tmp_path)
    sid = "history_plan"
    app.session_store.save(
        sid,
        [Message(role="user", content="需要计划")],
        owner_account_id=OWNER,
    )
    write_plan(sid, "# 历史计划\n\n- 第一步", owner_account_id=OWNER)

    res = client.get(f"/api/session/{sid}/plan", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["has_plan"] is True
    assert data["plan"] == "# 历史计划\n\n- 第一步"
    assert data["active"] is False
    assert data["awaiting_approval"] is False
    assert data["phase"] == "inactive"
    assert data["status"] == "readonly"
    pf = data["plan_file"].replace("\\", "/")
    assert "/plans/" in pf  # 落在 .crew/plans/<owner>/<sid>/ 子目录下
    assert pf.endswith(".md")
    assert "/plan_" in pf

    app.plan_manager.enter(sid, owner_account_id=OWNER)
    write_plan(sid, "# 待审计划\n\n- 第一步", owner_account_id=OWNER)
    app.plan_manager.submit_review(
        sid,
        owner_account_id=OWNER,
    )
    res2 = client.get(f"/api/session/{sid}/plan", headers=auth_headers)
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["active"] is True
    assert data2["awaiting_approval"] is True
    assert data2["phase"] == "review"
    assert data2["status"] == "pending"


def test_session_plan_rest_legacy_active_plan_restores_editing_not_pending(tmp_path, auth_headers):
    """active + 已有正文 → editing，不得伪装成 pending（否则已落地计划会卡在等待审批）。"""
    client, app = _client(tmp_path)
    sid = "history_plan_legacy_active"
    app.session_store.save(
        sid,
        [Message(role="user", content="需要计划")],
        owner_account_id=OWNER,
    )
    app.plan_manager.enter(sid, owner_account_id=OWNER)
    write_plan(sid, "# 未处理计划\n\n- 执行", owner_account_id=OWNER)

    res = client.get(f"/api/session/{sid}/plan", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["active"] is True
    assert data["awaiting_approval"] is False
    assert data["phase"] == "active"
    assert data["status"] == "editing"

    app.plan_manager.reject(sid, owner_account_id=OWNER)
    res2 = client.get(f"/api/session/{sid}/plan", headers=auth_headers)
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["phase"] == "revising"
    assert data2["status"] == "revising"


def test_session_plan_rest_approved_is_readonly_approved(tmp_path, auth_headers):
    """批准后 GET /plan 必须返回 approved，刷新不应再出现等待审批。"""
    client, app = _client(tmp_path)
    sid = "history_plan_approved"
    app.session_store.save(
        sid,
        [Message(role="user", content="需要计划")],
        owner_account_id=OWNER,
    )
    app.plan_manager.enter(sid, owner_account_id=OWNER)
    write_plan(sid, "# 已批计划\n\n- 做完", owner_account_id=OWNER)
    app.plan_manager.request_approval(sid, owner_account_id=OWNER)
    app.plan_manager.approve(sid, owner_account_id=OWNER)

    res = client.get(f"/api/session/{sid}/plan", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["phase"] == "approved"
    assert data["status"] == "approved"
    assert data["awaiting_approval"] is False
    assert data["active"] is False
    assert data["has_plan"] is True


def test_ws_plan_update_writes_plan_while_awaiting(
    tmp_path,
    auth_headers,
    send_ws_json,
):
    """看板手改：plan_update 在待审批时写回计划文件，并回推更新后的 plan_review。"""
    from crew.agent.plan import read_plan

    client, app = _client(tmp_path)
    sid = "ws_plan_update"
    app.session_store.save(
        sid,
        [Message(role="user", content="进入计划")],
        owner_account_id=OWNER,
    )
    app.plan_manager.enter(sid, owner_account_id=OWNER)
    write_plan(sid, "# 原计划\n\n- A", owner_account_id=OWNER)
    app.plan_manager.request_approval(sid, owner_account_id=OWNER)

    edited = "# 手改计划\n\n- A\n- B 用户补充"
    with client.websocket_connect("/ws", headers=auth_headers) as ws:
        send_ws_json(ws, {"action": "plan_update", "session_id": sid, "plan": edited})
        # 先收到 status 确认，再收到更新后的 plan_review
        kinds = []
        review = None
        for _ in range(6):
            msg = ws.receive_json()
            kinds.append(msg.get("kind"))
            if msg.get("kind") == "plan_review":
                review = msg
                break

        assert "status" in kinds
        assert review is not None
        assert review["body"]["plan"] == edited
        assert review["body"].get("empty") is not True
        assert review["body"].get("status") == "pending"

    assert read_plan(sid, owner_account_id=OWNER) == edited
    assert app.plan_manager.is_awaiting_approval(sid, owner_account_id=OWNER)


def test_ws_plan_update_rejected_when_inactive(
    tmp_path,
    auth_headers,
    send_ws_json,
):
    """未进入 Plan 模式时不允许 plan_update。"""
    client, app = _client(tmp_path)
    sid = "ws_plan_update_inactive"
    app.session_store.save(
        sid,
        [Message(role="user", content="普通对话")],
        owner_account_id=OWNER,
    )
    with client.websocket_connect("/ws", headers=auth_headers) as ws:
        send_ws_json(ws, {"action": "plan_update", "session_id": sid, "plan": "# x"})
        msg = ws.receive_json()
        assert msg["kind"] == "status"
        assert "不在 Plan 模式" in msg["body"]["message"]


def test_ws_plan_approve_with_edited_plan_persists_before_execute(
    tmp_path,
    auth_headers,
    send_ws_json,
):
    """看板手改后批准：plan_approve 附带 plan 正文须先落盘，再进入执行。"""
    from crew.agent.plan import read_plan

    client, app = _client(tmp_path)
    sid = "ws_plan_approve_edit"
    app.session_store.save(
        sid,
        [Message(role="user", content="进入计划")],
        owner_account_id=OWNER,
    )
    app.plan_manager.enter(sid, owner_account_id=OWNER)
    write_plan(sid, "# 原计划\n\n- A", owner_account_id=OWNER)
    app.plan_manager.request_approval(sid, owner_account_id=OWNER)

    edited = "# 手改后批准\n\n- A\n- B 用户补充"
    with client.websocket_connect("/ws", headers=auth_headers) as ws:
        send_ws_json(ws, {
            "action": "plan_approve",
            "session_id": sid,
            "mode": "agent",
            "workspace_id": "default",
            "plan": edited,
            "request_id": "r-approve-edit",
        })
        # 批准会起一轮执行；至少应收到后续帧，且文件已是手改稿。
        for _ in range(30):
            msg = ws.receive_json()
            if msg.get("kind") in ("delta", "final", "status", "error"):
                break

    assert read_plan(sid, owner_account_id=OWNER) == edited
    assert not app.plan_manager.is_active(sid, owner_account_id=OWNER)
    assert app.plan_manager.phase(sid, owner_account_id=OWNER) == "approved"


def test_ws_rejects_empty_query_without_attachments(
    tmp_path,
    auth_headers,
    send_ws_json,
):
    """空 query 且无附件不得起一轮，避免历史写入空 user 污染后续 LLM 调用。"""
    client, app = _client(tmp_path)
    sid = "ws_empty_query"
    app.session_store.save(
        sid,
        [Message(role="user", content="已有历史")],
        owner_account_id=OWNER,
    )
    with client.websocket_connect("/ws", headers=auth_headers) as ws:
        send_ws_json(
            ws,
            {
                "query": "",
                "session_id": sid,
                "mode": "agent",
                "workspace_id": "default",
            },
        )
        msg = ws.receive_json()
        assert msg["kind"] == "status"
        assert "为空" in msg["body"]["message"]
        # 历史不得被追加空 user
        history = app.session_store.load(sid, owner_account_id=OWNER)
        assert len(history) == 1
        assert history[0].content == "已有历史"


def test_delete_session_removes_plan_directory(tmp_path, auth_headers):
    """DELETE /api/session/{id} 须清掉该会话 plans/<owner>/<sid>/，避免删会话后 plan 文件残留占盘。"""
    from crew.agent.plan.manager import _plan_dir

    client, app = _client(tmp_path)
    sid = "ws_delete_plan_dir"
    app.session_store.save(
        sid,
        [Message(role="user", content="有计划的会话")],
        owner_account_id=OWNER,
    )
    path = write_plan(sid, "# delete me\n\nstep", owner_account_id=OWNER)
    plan_dir = _plan_dir(sid, owner_account_id=OWNER)
    (plan_dir / "plan_old_history.md").write_text("# old", encoding="utf-8")
    app.plan_manager.enter(sid, owner_account_id=OWNER)
    assert path.is_file()
    assert plan_dir.is_dir()

    res = client.delete(f"/api/session/{sid}", headers=auth_headers)
    assert res.status_code == 200
    assert res.json().get("ok") is True
    assert not plan_dir.exists()
    assert not path.exists()
    assert not app.plan_manager.is_active(sid, owner_account_id=OWNER)
