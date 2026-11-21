"""Goal controls, persisted scope, and model context through the actual Gateway."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event
from time import monotonic, sleep

import pytest
from starlette.testclient import TestClient

from crew.agent.plan import write_plan
from crew.app import build_app
from crew.core.mocks import FakeProvider
from crew.core.types import ChatResponse, ToolCall
from crew.features.hooks import hook_registry
from crew.gateway.app import create_app
from crew.gateway.send_inbox import DesktopSendInbox
from crew.state.config import Config


@pytest.fixture
def goal_gateway(tmp_path, monkeypatch, unused_tcp_port_factory):
    monkeypatch.setenv("CREW_OFFLINE", "1")

    @contextmanager
    def open_gateway(provider, **overrides):
        monkeypatch.setattr("crew.app.build_provider", lambda _cfg: provider)
        ready = Event()

        def on_ready(_event, _context):
            ready.set()

        crew = build_app(config=Config(
            db_path=str(tmp_path / "goal-sessions.db"), cron_enabled=False, title_auto=False,
            auth_mode="local", health_port=unused_tcp_port_factory(), gateway_push_min_interval=0,
            **overrides,
        ), enable_team=False)
        hook_registry.register("gateway:startup", on_ready)
        try:
            with TestClient(create_app(crew), client=("127.0.0.1", 17000)) as client:
                assert ready.wait(15), "Gateway did not become ready"
                yield client, crew
        finally:
            hook_registry.unregister("gateway:startup", on_ready)

    return open_gateway


def _send(client, sid, text, **options):
    with client.websocket_connect("/ws") as socket:
        socket.send_json({"session_id": sid, "query": text, **options})
        for _ in range(30):
            frame = socket.receive_json()
            if frame.get("is_final"):
                assert frame["body"]["outcome"]["status"] == "completed", frame
                return
    pytest.fail("No terminal chat frame")


def _model():
    return FakeProvider([ChatResponse(text="goal fixture reply", finish_reason="stop")] * 4)


def _set(client, path, version, objective):
    return client.post(path, json={"action": "set", "version": version, "objective": objective})


def test_goal_round_trip_controls_prompt_and_survives_restart(goal_gateway):
    provider = _model()
    objective = "Audit the synthetic invoice reference 896A"
    path = "/api/session/goal-main/goal"
    with goal_gateway(provider) as (client, _crew):
        assert client.post("/api/session/goal-main/ensure", json={}).status_code == 200
        assert client.post("/api/session/unrelated/ensure", json={}).status_code == 200
        initial = client.get(path)
        assert initial.status_code == 200
        assert initial.json()["goal"] is None
        assert initial.json()["supports_advance"] is True
        applied = _set(client, path, initial.json()["version"], objective)
        assert applied.status_code == 200
        assert applied.json()["goal"] == {"objective": objective, "status": "active"}
        assert provider.calls == []

        _send(client, "goal-main", "Report the next step")
        assert any(objective in message.content for message in provider.calls[0])
        assert client.get(path).json() == applied.json()
        _send(client, "unrelated", "Another conversation")
        assert all(objective not in message.content for message in provider.calls[1])

        paused = client.post(path, json={"action": "pause", "version": applied.json()["version"]})
        assert paused.status_code == 200
        assert paused.json()["goal"]["status"] == "paused"
        stale = client.post(path, json={"action": "complete", "version": applied.json()["version"]})
        assert stale.status_code == 409
        assert client.get(path).json() == paused.json()
        _send(client, "goal-main", "Answer without goal context")
        assert all(objective not in message.content for message in provider.calls[2])

    after_restart = _model()
    with goal_gateway(after_restart) as (client, _crew):
        assert client.get(path).json() == paused.json()
        resumed = client.post(path, json={"action": "resume", "version": paused.json()["version"]})
        assert resumed.status_code == 200
        assert resumed.json()["goal"]["status"] == "active"
        _send(client, "goal-main", "Continue here")
        assert any(objective in message.content for message in after_restart.calls[0])
        assert any("Answer without goal context" == message.content for message in after_restart.calls[0])


def _wait_run(client, path, *states):
    deadline = monotonic() + 5
    snapshot = {}
    while monotonic() < deadline:
        snapshot = client.get(path).json()
        if (snapshot.get("run") or {}).get("state") in states:
            return snapshot
        sleep(0.01)
    pytest.fail(f"Goal step did not reach {states}: {snapshot}")


def test_advance_goal_runs_one_foreground_step_without_marking_the_goal_complete(goal_gateway):
    provider = _model()
    sid = "one-goal-step"
    path = f"/api/session/{sid}/goal"
    objective = "Reconcile synthetic invoice identity X018"
    with goal_gateway(provider) as (client, _crew):
        assert client.post(f"/api/session/{sid}/ensure", json={}).status_code == 200
        saved = _set(client, path, client.get(path).json()["version"], objective).json()
        submit = {"action": "advance", "version": saved["version"]}
        accepted = client.post(path, json=submit)
        assert accepted.status_code == 202, accepted.text
        rid = accepted.json()["run"]["request_id"]
        assert accepted.json()["run"]["state"] == "accepted"
        assert client.post(path, json=submit).status_code == 409
        settled = _wait_run(client, path, "finished")
        assert settled["run"]["request_id"] == rid
        assert settled["goal"] == {"objective": objective, "status": "active"}
        assert len(provider.calls) == 1
        assert any(objective in message.content for message in provider.calls[0])
        history = client.get(f"/api/session/{sid}/history").json()["items"]
        assert [(item["role"], item["content"]) for item in history
                if item["role"] in {"user", "assistant"} and item["content"]] == [
            ("user", "推进已保存的会话目标。"), ("assistant", "goal fixture reply"),
        ]


class VerificationModel(FakeProvider):
    def __init__(self, transform=lambda result: result):
        super().__init__()
        self.checks = []
        self.transform = transform

    async def chat(self, messages, tools=None, **kwargs):
        if kwargs.get("purpose") != "goal-completion-verification":
            self.calls.append(messages)
            return ChatResponse(text="Synthetic invoice subtotal: 72 credits", finish_reason="stop")
        self.checks.append((messages, tools))
        data = json.loads(messages[-1].content)
        entry = next(row for row in data["evidence"] if row["text"] == "Synthetic invoice subtotal: 72 credits")
        verdict = {"passed": True, "citations": [{"seq": entry["seq"], "quote": entry["text"]}]}
        result = self.transform(verdict)
        if isinstance(result, ChatResponse):
            return result
        return ChatResponse(text=json.dumps(result), finish_reason="stop")


def test_goal_verification_uses_separate_no_tool_model_call_and_saved_evidence(goal_gateway):
    provider = VerificationModel()
    objective = "Return the exact synthetic invoice subtotal"
    sid = "verified-goal"
    path = f"/api/session/{sid}/goal"
    with goal_gateway(provider) as (client, _crew):
        assert client.post(f"/api/session/{sid}/ensure", json={}).status_code == 200
        saved = _set(client, path, client.get(path).json()["version"], objective).json()
        assert saved["supports_verify"] is True
        unopened = client.post(path, json={"action": "verify", "version": saved["version"]})
        assert unopened.status_code == 409 and unopened.json()["code"] == "goal_verification_no_step"
        assert client.post(path, json={"action": "advance", "version": saved["version"]}).status_code == 202
        step = _wait_run(client, path, "finished")
        assert step["goal"]["status"] == "active" and provider.checks == []
        before_history = client.get(f"/api/session/{sid}/history").json()
        reviewed = client.post(path, json={"action": "verify", "version": step["version"]})
        assert reviewed.status_code == 200, reviewed.text
        final = reviewed.json()
        assert final["goal"] == {"objective": objective, "status": "completed"}
        assert final["version"] != step["version"]
        assert final["run"] == step["run"]
        assert final["verification"]["state"] == "passed"
        assert final["verification"]["step_request_id"] == step["run"]["request_id"]
        assert len(final["verification"]["evidence_sequences"]) == 1
        assert len(provider.calls) == 1 and len(provider.checks) == 1
        assert provider.checks[0][1] == []
        assert client.get(f"/api/session/{sid}/history").json() == before_history
        assert client.post(path, json={"action": "verify", "version": step["version"]}).status_code == 409
        assert len(provider.checks) == 1
    with goal_gateway(VerificationModel()) as (client, _crew):
        assert client.get(path).json() == final


def _finished_fixture_goal(client, sid):
    path, saved = _active_goal(client, sid)
    assert client.post(path, json={"action": "advance", "version": saved["version"]}).status_code == 202
    return path, _wait_run(client, path, "finished")


@pytest.mark.parametrize(("transform", "state", "code"), [
    pytest.param(lambda _: {"passed": False, "citations": []}, "incomplete", "goal_evidence_insufficient", id="incomplete"),
    pytest.param(lambda _: ChatResponse(text="Done!", finish_reason="stop"), "unconfirmed", "goal_verification_bad_response",
                 id="invalid-json"),
    pytest.param(lambda verdict: {**verdict, "citations": [{**verdict["citations"][0], "quote": "Unseen evidence"}]},
                 "unconfirmed", "goal_verification_bad_citations", id="invented-quote"),
    pytest.param(lambda verdict: ChatResponse(text=json.dumps(verdict), finish_reason="stop",
                 tool_calls=[ToolCall("fixture-call", "fixture-no-tool")]), "unconfirmed", "goal_verification_tool_call",
                 id="attempted-tool-call"),
])
def test_goal_verification_does_not_complete_without_positive_valid_evidence(goal_gateway, transform, state, code):
    provider = VerificationModel(transform)
    with goal_gateway(provider) as (client, _crew):
        path, step = _finished_fixture_goal(client, "goal-verify-fail-closed")
        result = client.post(path, json={"action": "verify", "version": step["version"]})
        assert result.status_code == 200, result.text
        assert result.json()["goal"]["status"] == "active"
        assert result.json()["verification"]["state"] == state
        assert result.json()["verification"]["code"] == code
        assert result.json()["verification"]["evidence_sequences"] == []
        assert len(provider.checks) == 1


def test_goal_verification_refuses_a_reopened_plan_without_calling_the_model(goal_gateway):
    provider = VerificationModel()
    sid = "goal-verify-reopened-plan"
    with goal_gateway(provider) as (client, crew):
        path, step = _finished_fixture_goal(client, sid)
        crew.plan_manager.enter(sid, owner_account_id="local")
        plan = write_plan(sid, "# Previously approved fixture", owner_account_id="local")
        crew.plan_manager.request_approval(sid, owner_account_id="local")
        crew.plan_manager.approve(sid, owner_account_id="local")
        assert client.get(f"/api/session/{sid}/plan").json()["phase"] == "approved"
        # Simulate a real edit of an approved Plan. The next manager read
        # restores it to active, even though SQLite still shows approved.
        plan.write_text("# Edited plan requires new approval", encoding="utf-8")
        response = client.post(path, json={"action": "verify", "version": step["version"]})
        assert response.status_code == 200, response.text
        assert response.json()["goal"]["status"] == "active"
        assert response.json()["verification"]["state"] == "interrupted"
        assert response.json()["verification"]["code"] == "goal_plan_active"
        assert provider.checks == []


@pytest.mark.parametrize("prefix_length", [24, 1776])
def test_goal_verifier_redacts_before_truncation_and_cannot_cite_a_generated_placeholder(goal_gateway, prefix_length):
    secret = "fake_fixture_token_not_a_credential"

    class RedactionProbe(FakeProvider):
        async def chat(self, messages, tools=None, **kwargs):
            if kwargs.get("purpose") != "goal-completion-verification":
                return ChatResponse(text="Test line " + "a" * prefix_length + f" token={secret}", finish_reason="stop")
            self.calls.append(messages)
            evidence = json.loads(messages[-1].content)["evidence"]
            row = next(record for record in evidence if record["step_reply"])
            return ChatResponse(text=json.dumps({
                "passed": True, "citations": [{"seq": row["seq"], "quote": "token=[REDACTED]"}],
            }), finish_reason="stop")

    provider = RedactionProbe()
    with goal_gateway(provider) as (client, _crew):
        path, step = _finished_fixture_goal(client, "goal-verify-redacted-evidence")
        result = client.post(path, json={"action": "verify", "version": step["version"]})
        assert result.status_code == 200, result.text
        assert result.json()["goal"]["status"] == "active"
        assert result.json()["verification"]["code"] == "goal_verification_bad_citations"
        assert len(provider.calls) == 1
        evidence = json.loads(provider.calls[0][-1].content)["evidence"]
        row = next(record for record in evidence if record["step_reply"])
        assert secret[:6] not in row["text"] and "token=[" in row["text"]


class HeldVerificationModel(VerificationModel):
    def __init__(self):
        super().__init__()
        self.entered = Event()
        self.exited = Event()
        self.release = asyncio.Event()

    async def chat(self, messages, tools=None, **kwargs):
        if kwargs.get("purpose") == "goal-completion-verification":
            self.entered.set()
            try:
                await self.release.wait()
            finally:
                self.exited.set()
        return await super().chat(messages, tools, **kwargs)


def test_goal_verification_preference_acknowledges_while_the_same_check_can_still_be_paused(goal_gateway):
    provider = HeldVerificationModel()
    sid = "goal-interactive-verification"
    with goal_gateway(provider) as (client, _crew):
        path, step = _finished_fixture_goal(client, sid)
        transcript_path = f"/api/session/{sid}/history"
        transcript = client.get(transcript_path).content
        with ThreadPoolExecutor(max_workers=1) as thread:
            pending = thread.submit(client.post, path, headers={"Prefer": "respond-async"},
                                    json={"action": "verify", "version": step["version"]})
            try:
                assert provider.entered.wait(5)
                accepted = pending.result(timeout=2)
                assert accepted.status_code == 202, accepted.text
                assert accepted.headers["Preference-Applied"] == "respond-async"
                snapshot = accepted.json()
                assert snapshot["verification"]["state"] == "running"
                assert snapshot["goal"]["status"] == "active"
                assert snapshot["version"] != step["version"]
                assert client.get(path).json() == snapshot
                duplicate = client.post(path, headers={"Prefer": "respond-async"},
                                        json={"action": "verify", "version": step["version"]})
                assert duplicate.status_code == 409
                for action in ("complete", "set", "resume"):
                    denied = client.post(path, json={"action": action, "version": snapshot["version"],
                                                     "objective": "Do not change a live check"})
                    assert denied.status_code == 409 and denied.json()["code"] == "goal_verification_active"
                assert client.get(path).json() == snapshot
                paused = client.post(path, json={"action": "pause", "version": snapshot["version"]}).json()
                assert paused["goal"]["status"] == "paused"
                assert paused["verification"]["request_id"] == snapshot["verification"]["request_id"]
                assert paused["verification"]["state"] == "interrupted"
                assert provider.exited.wait(5)
                assert client.get(transcript_path).content == transcript
            finally:
                client.portal.call(provider.release.set)


def test_goal_verification_deadline_cancels_without_changing_the_target(goal_gateway, monkeypatch):
    monkeypatch.setattr("crew.gateway.goal_verification.VERIFICATION_TIMEOUT_SECONDS", 0.3)
    provider = HeldVerificationModel()
    with goal_gateway(provider) as (client, _crew):
        path, step = _finished_fixture_goal(client, "goal-verify-deadline")
        response = client.post(path, json={"action": "verify", "version": step["version"]})
        assert response.status_code == 200, response.text
        assert provider.entered.is_set() and provider.exited.is_set()
        current = response.json()
        assert current["goal"]["status"] == "active"
        assert current["verification"]["state"] == "unconfirmed"
        assert current["verification"]["code"] == "goal_verification_timeout"


@pytest.mark.parametrize("change", ["ordinary", "pause", "archive", "logout"])
def test_goal_verification_cannot_publish_success_after_history_or_target_changes(goal_gateway, change):
    provider = HeldVerificationModel()
    sid = "goal-verification-race"
    with goal_gateway(provider) as (client, _crew):
        path, step = _finished_fixture_goal(client, sid)
        with ThreadPoolExecutor(max_workers=1) as thread:
            pending = thread.submit(client.post, path, json={"action": "verify", "version": step["version"]})
            try:
                assert provider.entered.wait(5)
                checking = client.get(path).json()
                assert checking["verification"]["state"] == "running"
                conflicting = client.post(path, json={"action": "verify", "version": checking["version"]})
                assert conflicting.status_code == 409
                if change == "ordinary":
                    _send(client, sid, "Add committed history while the verdict is pending")
                elif change == "pause":
                    paused = client.post(path, json={"action": "pause", "version": checking["version"]})
                    assert paused.status_code == 200
                    assert paused.json()["goal"]["status"] == "paused"
                    assert provider.exited.wait(5), "exact verifier call was not cancelled by pause"
                elif change == "archive":
                    archive = f"/api/session/{sid}/archive"
                    assert client.put(archive, json={"archived": True}).status_code == 200
                    assert client.get(path).status_code == 404
                    assert client.put(archive, json={"archived": False}).status_code == 200
                    assert client.get(path).json()["verification"]["state"] == "interrupted"
                else:
                    released = client.post("/api/auth/logout")
                    assert released.status_code == 200 and released.json()["released"]
                    assert provider.exited.is_set(), "logout must not leave a verifier holding the old owner scope"
            finally:
                client.portal.call(provider.release.set)
            response = pending.result(timeout=8)
        assert response.status_code == 200
        expected = {
            "ordinary": ("active", "goal_evidence_changed"),
            "pause": ("paused", "goal_changed"),
            "archive": ("active", "goal_session_archived"),
            "logout": ("active", "goal_verification_scope_lost"),
        }[change]
        snapshot = response.json()
        assert snapshot["verification"]["request_id"] == checking["verification"]["request_id"]
        assert snapshot["verification"]["state"] == "interrupted"
        assert (snapshot["goal"]["status"], snapshot["verification"]["code"]) == expected
        assert client.get(path).json() == snapshot


def test_logout_ignores_a_verify_rejected_before_reservation(goal_gateway, monkeypatch):
    provider = VerificationModel()
    with goal_gateway(provider) as (client, crew):
        path, step = _finished_fixture_goal(client, "rejected-verification-logout")
        reserve_entered, release = Event(), Event()
        reserve = crew.session_store.goals.reserve_verification

        def held_reserve(*args, **kwargs):
            reserve_entered.set()
            assert release.wait(8)
            return reserve(*args, **kwargs)

        monkeypatch.setattr(crew.session_store.goals, "reserve_verification", held_reserve)
        with ThreadPoolExecutor(max_workers=2) as threads:
            pending = threads.submit(client.post, path, headers={"Prefer": "respond-async"},
                                     json={"action": "verify", "version": "stale"})
            try:
                assert reserve_entered.wait(5)
                logout = threads.submit(client.post, "/api/auth/logout")
                deadline = monotonic() + 4
                while not logout.done() and client.get(path).status_code != 423:
                    assert monotonic() < deadline
                    sleep(0.01)
                assert not logout.done(), "logout must drain the ongoing admission"
            finally:
                release.set()
            rejected = pending.result(timeout=5)
            assert rejected.status_code == 409 and rejected.json()["code"] == "goal_version_conflict"
            result = logout.result(timeout=5)
        assert result.status_code == 200 and result.json()["released"] is True, result.text
        assert provider.checks == []
        assert client.get(path).json() == step


def test_logout_fences_a_verdict_waiting_for_the_sqlite_commit(goal_gateway, monkeypatch):
    provider = VerificationModel()
    with goal_gateway(provider) as (client, crew):
        path, step = _finished_fixture_goal(client, "verify-during-logout")
        commit_entered, release = Event(), Event()
        original = crew.session_store.goals.finish_verification

        def held_finish(*args, **kwargs):
            commit_entered.set()
            assert release.wait(8)
            return original(*args, **kwargs)

        monkeypatch.setattr(crew.session_store.goals, "finish_verification", held_finish)
        with ThreadPoolExecutor(max_workers=2) as threads:
            pending = threads.submit(client.post, path, json={"action": "verify", "version": step["version"]})
            try:
                assert commit_entered.wait(5)
                logout = threads.submit(client.post, "/api/auth/logout")
                deadline = monotonic() + 4
                while not logout.done() and client.get(path).status_code != 423:
                    assert monotonic() < deadline
                    sleep(0.01)
                assert not logout.done(), "logout released the Owner while its verifier had not settled"
            finally:
                release.set()
            assert pending.result(timeout=5).status_code == 200
            assert logout.result(timeout=5).json()["released"] is True
        current = client.get(path).json()
        assert current["goal"]["status"] == "active"
        assert current["verification"]["state"] == "interrupted"
        assert current["verification"]["code"] == "goal_verification_scope_lost"


class HeldModel(FakeProvider):
    def __init__(self, hold_query="推进已保存的会话目标。"):
        super().__init__()
        self.hold_query = hold_query
        self.entered = Event()
        self.exited = Event()
        self.release = asyncio.Event()
        self.tools_seen = []

    async def chat(self, messages, tools=None, **kwargs):
        query = next(m.content for m in reversed(messages) if m.role == "user" and not m.is_meta)
        self.calls.append(list(messages))
        self.tools_seen.append((query, tools))
        if query == self.hold_query:
            self.entered.set()
            try:
                await self.release.wait()
            finally:
                self.exited.set()
        return ChatResponse(text=f"Reply to {query}", finish_reason="stop")


def _frame(socket, predicate):
    for _ in range(50):
        frame = socket.receive_json()
        if predicate(frame):
            return frame
    pytest.fail("Expected Gateway frame was not delivered")


def _active_goal(client, sid):
    assert client.post(f"/api/session/{sid}/ensure", json={}).status_code == 200
    path = f"/api/session/{sid}/goal"
    snapshot = _set(client, path, client.get(path).json()["version"], "Check the synthetic checklist").json()
    return path, snapshot


def test_goal_step_rejects_entering_plan_while_it_holds_foreground(goal_gateway):
    provider = HeldModel()
    sid = "goal-versus-plan"
    with goal_gateway(provider) as (client, _crew):
        path, saved = _active_goal(client, sid)
        started = client.post(path, json={"action": "advance", "version": saved["version"]})
        assert started.status_code == 202
        assert provider.entered.wait(5)
        with client.websocket_connect("/ws") as socket:
            socket.send_json({"action": "plan_enter", "session_id": sid})
            _frame(socket, lambda frame: frame.get("kind") == "status" and frame.get("session_id") == sid)
            try:
                assert not client.get(f"/api/session/{sid}/plan").json()["active"]
                socket.send_json({"session_id": sid, "query": "Plan question", "plan_active": True, "request_id": "plan-attempt"})
                refused = _frame(socket, lambda frame: frame.get("request_id") == "plan-attempt" and frame.get("is_final"))
                assert refused["body"]["code"] == "goal_step_active"
                assert not client.get(f"/api/session/{sid}/plan").json()["active"]
            finally:
                client.portal.call(provider.release.set)
        assert _wait_run(client, path, "finished")["goal"]["status"] == "active"


def test_pause_stops_exact_goal_request_without_steering_or_stopping_queued_chat(goal_gateway):
    provider = HeldModel()
    sid = "scoped-goal-pause"
    with goal_gateway(provider, gateway_busy_mode="steer") as (client, _crew):
        path, saved = _active_goal(client, sid)
        started = client.post(path, json={"action": "advance", "version": saved["version"]}).json()
        rid = started["run"]["request_id"]
        assert provider.entered.wait(5)
        assert not provider.tools_seen[0][1], "Goal step must not be offered tools"
        with client.websocket_connect("/ws?send_receipts=1") as socket:
            socket.send_json({"session_id": sid, "query": "Borrow Goal identity", "request_id": rid, "send_receipt": True})
            collision = _frame(socket, lambda frame: frame.get("kind") == "send_receipt" and frame.get("request_id") == rid)
            assert collision["body"] == {"status": "rejected", "code": "reserved_request_id"}
            socket.send_json({"session_id": sid, "query": "Borrow Goal identity in legacy chat", "request_id": rid})
            _frame(socket, lambda frame: frame.get("body", {}).get("code") == "reserved_request_id")
            socket.send_json({"session_id": sid, "query": "Keep this ordinary request", "request_id": "regular-after-goal"})
            queued = _frame(socket, lambda frame: frame.get("request_id") == "regular-after-goal")
            assert queued["kind"] == "status" and "排队" in (queued["body"].get("message") or "")
            paused = client.post(path, json={"action": "pause", "version": started["version"]})
            assert paused.status_code == 200, paused.text
            assert paused.json()["goal"]["status"] == "paused"
            assert paused.json()["run"]["request_id"] == rid
            assert provider.exited.wait(4)
            frames = []
            while len(frames) < 2:
                frames.append(_frame(socket, lambda frame: frame.get("is_final") and frame.get("request_id") in {
                    rid, "regular-after-goal",
                }))
        assert {frame["request_id"]: frame["body"]["outcome"]["status"] for frame in frames} == {
            rid: "stopped", "regular-after-goal": "completed",
        }
        assert _wait_run(client, path, "stopped")["goal"]["status"] == "paused"
        assert [query for query, _ in provider.tools_seen] == ["推进已保存的会话目标。", "Keep this ordinary request"]


def test_busy_chat_is_not_interrupted_by_advancing_or_pausing_a_saved_goal(goal_gateway):
    provider = HeldModel("Keep working on this ordinary turn")
    sid = "ordinary-first"
    with goal_gateway(provider, gateway_busy_mode="interrupt") as (client, _crew):
        path, saved = _active_goal(client, sid)
        with client.websocket_connect("/ws") as socket:
            socket.send_json({"session_id": sid, "query": provider.hold_query, "request_id": "ordinary-in-flight"})
            assert provider.entered.wait(5)
            busy = client.post(path, json={"action": "advance", "version": saved["version"]})
            assert busy.status_code == 409 and busy.json()["code"] == "goal_session_busy"
            assert client.get(path).json() == saved
            paused = client.post(path, json={"action": "pause", "version": saved["version"]})
            assert paused.json()["goal"]["status"] == "paused"
            assert not provider.exited.is_set()
            client.portal.call(provider.release.set)
            result = _frame(socket, lambda frame: frame.get("is_final"))
        assert result["request_id"] == "ordinary-in-flight"
        assert result["body"]["outcome"]["status"] == "completed"
        assert len(provider.calls) == 1


def test_pause_of_accepted_step_prevents_claim_and_never_runs_the_model(goal_gateway, monkeypatch):
    provider = _model()
    ready, release = Event(), Event()
    sid = "pause-before-claim"
    with goal_gateway(provider) as (client, crew):
        path, saved = _active_goal(client, sid)
        claim = crew.session_store.goals.claim_step

        def held_claim(step, default_executor):
            ready.set()
            assert release.wait(5)
            return claim(step, default_executor)

        monkeypatch.setattr(crew.session_store.goals, "claim_step", held_claim)
        advanced = client.post(path, json={"action": "advance", "version": saved["version"]}).json()
        try:
            assert ready.wait(4)
            assert client.get(path).json()["run"]["state"] == "accepted"
            pause = client.post(path, json={"action": "pause", "version": advanced["version"]})
            assert pause.status_code == 200
            assert pause.json()["run"] == {
                "request_id": advanced["run"]["request_id"], "state": "stopped", "code": "paused_before_dispatch",
            }
        finally:
            release.set()
        _send(client, sid, "Only an ordinary request after the paused reservation")
        assert len(provider.calls) == 1
        assert not any("推进已保存的会话目标。" in message.content for message in provider.calls[0])
        assert client.get(path).json() == pause.json()


def test_pause_during_task_registration_releases_the_session_and_global_slot(goal_gateway, monkeypatch):
    provider = _model()
    entered = Event()
    release = asyncio.Event()
    sid = "goal-during-task-registration"
    with goal_gateway(provider, gateway_max_active_runs=1) as (client, crew):
        path, saved = _active_goal(client, sid)
        register = crew.tasks.create_runtime_async

        async def held_register(*args, **kwargs):
            if kwargs.get("session_id") == sid and kwargs.get("request_id", "").startswith("goal_") and not entered.is_set():
                entered.set()
                await release.wait()
            return await register(*args, **kwargs)

        monkeypatch.setattr(crew.tasks, "create_runtime_async", held_register)
        advanced = client.post(path, json={"action": "advance", "version": saved["version"]}).json()
        try:
            assert entered.wait(5)
            paused = client.post(path, json={"action": "pause", "version": advanced["version"]})
            assert paused.status_code == 200
        finally:
            client.portal.call(release.set)
        _wait_run(client, path, "stopped", "failed", "interrupted")
        assert client.get(f"/api/session/{sid}/status").json()["live"] == "idle"
        assert provider.calls == []
        _send(client, sid, "Normal conversation can run after interrupted registration")
        resumed = client.post(path, json={"action": "resume", "version": paused.json()["version"]}).json()
        assert client.post(path, json={"action": "advance", "version": resumed["version"]}).status_code == 202
        assert _wait_run(client, path, "finished")["goal"]["status"] == "active"
        assert len(provider.calls) == 2


def test_goal_yields_to_a_receipted_request_already_claiming_a_busy_turn(goal_gateway, monkeypatch):
    provider = HeldModel("First ordinary turn")
    release_claim = asyncio.Event()
    claim_entered = Event()
    begin = DesktopSendInbox.begin

    async def held_begin(inbox, key):
        allowed = await begin(inbox, key)
        if allowed and key.request_id == "ordinary-between-locks":
            claim_entered.set()
            await release_claim.wait()
        return allowed

    monkeypatch.setattr(DesktopSendInbox, "begin", held_begin)
    sid = "ordinary-claim-in-progress"
    with goal_gateway(provider, gateway_busy_mode="interrupt") as (client, _crew):
        path, saved = _active_goal(client, sid)
        with client.websocket_connect("/ws?send_receipts=1") as socket:
            socket.send_json({"session_id": sid, "query": provider.hold_query, "request_id": "initial-ordinary"})
            assert provider.entered.wait(5)
            socket.send_json({
                "session_id": sid, "query": "Previously accepted ordinary query", "send_receipt": True,
                "request_id": "ordinary-between-locks",
            })
            try:
                assert claim_entered.wait(5)
                client.portal.call(provider.release.set)
                _frame(socket, lambda frame: frame.get("is_final") and frame.get("request_id") == "initial-ordinary")
                deadline = monotonic() + 3
                while client.get(f"/api/session/{sid}/status").json()["live"] != "idle":
                    assert monotonic() < deadline
                    sleep(0.01)
                rejected = client.post(path, json={"action": "advance", "version": saved["version"]})
                assert rejected.status_code == 409 and rejected.json()["code"] == "goal_session_busy", rejected.text
            finally:
                client.portal.call(release_claim.set)
            terminal = _frame(socket, lambda frame: frame.get("is_final") and frame.get("request_id") == "ordinary-between-locks")
        assert terminal["body"]["outcome"]["status"] == "completed"
        assert client.get(path).json() == saved
        assert len(provider.calls) == 2


def test_model_cannot_execute_a_registered_tool_in_a_goal_step(goal_gateway):
    name = "synthetic_goal_counter"
    effects = []
    provider = FakeProvider([
        ChatResponse(tool_calls=[ToolCall("unexpected-tool", name)], finish_reason="tool_calls"),
        ChatResponse(text="No tool was executed", finish_reason="stop"),
    ])
    with goal_gateway(provider) as (client, crew):
        def counter(arguments):
            effects.append(arguments)
            return "Incremented fixture counter"

        crew.registry.register(name=name, schema={"type": "object", "properties": {}}, handler=counter)
        path, saved = _active_goal(client, "goal-no-tool-effect")
        assert client.post(path, json={"action": "advance", "version": saved["version"]}).status_code == 202
        settled = _wait_run(client, path, "finished", "failed")
        assert settled["goal"]["status"] == "active"
        assert provider.calls and not effects


def test_goal_refuses_a_session_with_a_prior_detached_task(goal_gateway):
    provider = _model()
    sid = "goal-with-detached-work"
    with goal_gateway(provider) as (client, crew):
        path, saved = _active_goal(client, sid)

        async def register_fixture():
            task = await crew.tasks.create_runtime_async(
                kind="shell", session_id=sid, request_id="ordinary-detached", title="Fixture background task",
                owner_account_id="local", backgrounded=True,
            )
            return task["task_id"]

        task_id = client.portal.call(register_fixture)
        try:
            rejected = client.post(path, json={"action": "advance", "version": saved["version"]})
            assert rejected.status_code == 409 and rejected.json()["code"] == "goal_session_busy"
            assert client.get(path).json() == saved
            assert provider.calls == []
        finally:
            async def finish_fixture():
                await crew.tasks.finish_async(task_id, status="completed", owner_account_id="local")

            client.portal.call(finish_fixture)


def test_goal_has_positive_budget_even_when_regular_turn_deadline_is_disabled(goal_gateway, monkeypatch):
    monkeypatch.setattr("crew.gateway.goal_steps.STEP_LIMIT_SECONDS", 2.0)
    provider = HeldModel()
    with goal_gateway(provider, tasks_agent_turn_execution_timeout_seconds=0.0) as (client, _crew):
        path, saved = _active_goal(client, "goal-deadline")
        assert client.post(path, json={"action": "advance", "version": saved["version"]}).status_code == 202
        assert provider.entered.wait(5)
        stopped = _wait_run(client, path, "failed")
        assert stopped["run"]["code"] == "goal_deadline"
        assert provider.exited.is_set()
        assert stopped["goal"]["status"] == "active"
        assert len(provider.calls) == 1


def test_goal_not_owned_or_replaced_is_not_mutated(goal_gateway):
    with goal_gateway(_model()) as (client, crew):
        path = "/api/session/target/goal"
        crew.session_store.ensure_session("target", owner_account_id="another-owner")
        assert client.get(path).status_code == 404
        assert _set(client, path, "before", "Do not create a foreign goal").status_code == 404
        assert not crew.session_store.session_belongs_to("target", owner_account_id="local")

        assert client.post("/api/session/target/ensure", json={}).status_code == 200
        old_version = client.get(path).json()["version"]
        applied = _set(client, path, old_version, "Original target")
        assert applied.status_code == 200
        crew.session_store.clear("target", owner_account_id="local")
        assert client.post("/api/session/target/ensure", json={}).status_code == 200
        assert client.get(path).json()["goal"] is None
        assert _set(client, path, old_version, "Stale target").status_code == 409
        assert client.get(path).json()["goal"] is None
        assert crew.session_store.session_belongs_to("target", owner_account_id="another-owner")
