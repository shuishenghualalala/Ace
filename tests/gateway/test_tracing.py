from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from crew.core.observability import (
    ObservationContext,
    ObservationRecord,
    bind_observation,
    capture_payload,
    install_sink,
    span,
)
from crew.gateway.auth import AccountContext
from crew.gateway.routers.tracing import _ClientEventRateLimiter, _write_export_file, create_tracing_router
from crew.state.config import Config
from crew.state.observability import ObservationRecorder
from crew.state.trace_store import TraceStore


@pytest.fixture(autouse=True)
def clear_observation_sink():
    yield
    install_sink(None)


def _router_client(tmp_path):
    store = TraceStore(tmp_path / "traces.sqlite3")
    recorder = ObservationRecorder(store)
    install_sink(recorder)
    config = Config(
        observability_enabled=True,
        observability_developer_access=True,
        observability={"developer_access": True, "capture_profile": "content_redacted"},
        gateway_admin_accounts=["alice", "bob"],
    )
    crew = SimpleNamespace(config=config, observability_store=store, observability=recorder)
    app = FastAPI()

    @app.middleware("http")
    async def identity(request: Request, call_next):
        owner = request.headers.get("x-owner", "alice")
        request.state.account = AccountContext(owner_account_id=owner, is_local=False)
        return await call_next(request)

    app.include_router(create_tracing_router(crew))
    return TestClient(app), store, recorder


def _trace(recorder, owner: str, request_id: str, session_id: str = "session") -> str:
    with bind_observation(owner_account_id=owner, request_id=request_id, session_id=session_id):
        with span("interaction", module="gateway", component="dispatcher"):
            pass
    assert recorder.flush(2)
    # Trace IDs are random, so use the store through the recorder's caller in tests.
    return ""


def test_trace_api_is_owner_scoped_and_client_event_requires_verified_trace(tmp_path):
    client, store, recorder = _router_client(tmp_path)
    with bind_observation(owner_account_id="alice", request_id="request-a", session_id="same"):
        with span("interaction"):
            payload = capture_payload(
                "llm.request",
                {"messages": [{"role": "user", "content": "hello"}]},
                attributes={"capture_layer": "provider_arguments"},
            )
    with bind_observation(owner_account_id="bob", request_id="request-b", session_id="same"):
        with span("interaction"):
            pass
    recorder.flush(2)
    alice_trace = store.list_traces(owner_account_id="alice")["items"][0]
    bob_trace = store.list_traces(owner_account_id="bob")["items"][0]

    response = client.get("/api/tracing/traces", headers={"x-owner": "alice"})
    assert response.status_code == 200
    assert [item["trace_id"] for item in response.json()["items"]] == [alice_trace["trace_id"]]
    assert client.get(f"/api/tracing/traces/{bob_trace['trace_id']}", headers={"x-owner": "alice"}).status_code == 404
    assert client.post(
        "/api/tracing/client-events",
        headers={"x-owner": "alice"},
        json={"name": "user.presented", "trace_id": bob_trace["trace_id"], "request_id": "request-b"},
    ).status_code == 404
    assert client.post(
        "/api/tracing/client-events",
        headers={"x-owner": "alice"},
        json={"name": "user.presented", "trace_id": alice_trace["trace_id"], "request_id": "request-a"},
    ).status_code == 202
    payload_response = client.get(
        f"/api/tracing/payloads/{payload.payload_id}",
        headers={"x-owner": "alice"},
    )
    assert payload_response.status_code == 200
    assert payload_response.json()["attributes"]["capture_layer"] == "provider_arguments"
    assert client.get(
        f"/api/tracing/payloads/{payload.payload_id}",
        headers={"x-owner": "bob"},
    ).status_code == 404
    recorder.close()
    store.close()


def test_trace_access_is_explicitly_gated(tmp_path):
    client, store, recorder = _router_client(tmp_path)
    # Local diagnostics can still be authenticated, but not enabled by a hidden
    # renderer flag or an arbitrary URL parameter.
    crew = SimpleNamespace(
        config=Config(observability_enabled=True, observability_developer_access=False),
        observability_store=store,
        observability=recorder,
    )
    app = FastAPI()

    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.account = AccountContext(owner_account_id="alice", is_local=False)
        return await call_next(request)

    app.include_router(create_tracing_router(crew))
    response = TestClient(app).get("/api/tracing/traces?developer_access=true")
    assert response.status_code == 403
    recorder.close()
    store.close()


def test_client_event_limiter_has_bounded_total_capacity():
    limiter = _ClientEventRateLimiter(per_owner=5, total=6, window_seconds=10)
    assert limiter.allow("owner-a", 5, now=0)
    assert not limiter.allow("owner-b", 2, now=0)
    assert limiter.allow("owner-b", 1, now=0)
    assert limiter.allow("owner-b", 5, now=11)
    assert len(limiter._all) == 5  # noqa: SLF001 - bounded limiter invariant
    bounded = _ClientEventRateLimiter(per_owner=2, total=10_000)
    for index in range(5_000):
        bounded.allow(f"rotating-owner-{index}", 1, now=0)
    assert len(bounded._owners) <= 4_096  # noqa: SLF001 - bounded limiter invariant


def test_client_event_batch_is_fully_validated_before_recording(tmp_path):
    client, store, recorder = _router_client(tmp_path)
    with bind_observation(owner_account_id="alice", request_id="request-a", session_id="session-a"):
        with span("interaction"):
            pass
    assert recorder.flush(2)
    trace_id = store.list_traces(owner_account_id="alice")["items"][0]["trace_id"]
    response = client.post(
        "/api/tracing/client-events",
        headers={"x-owner": "alice"},
        json=[
            {"name": "desktop.event", "trace_id": trace_id},
            {"name": "not-allowed", "trace_id": trace_id},
        ],
    )
    assert response.status_code == 400
    assert recorder.flush(2)
    assert not any(
        row["name"] == "desktop.event"
        for row in store.list_events(owner_account_id="alice", trace_id=trace_id)
    )
    recorder.close()
    store.close()


def test_log_pagination_returns_cursor_metadata(tmp_path):
    client, store, recorder = _router_client(tmp_path)
    from crew.core.observability import event

    for index in range(3):
        event(f"system-log-{index}", attributes={"message": f"message-{index}"})
    assert recorder.flush(2)
    first = client.get("/api/tracing/logs?limit=1", headers={"x-owner": "alice"})
    assert first.status_code == 200
    first_data = first.json()
    assert first_data["has_more"] is True
    assert first_data["next_after_seq"]
    second = client.get(
        f"/api/tracing/logs?limit=1&after_seq={first_data['next_after_seq']}",
        headers={"x-owner": "alice"},
    )
    assert second.status_code == 200
    assert second.json()["items"][0]["ingest_seq"] < first_data["items"][0]["ingest_seq"]
    recorder.close()
    store.close()


def test_admin_log_query_includes_system_scope_without_relaxing_owner_routes(tmp_path):
    client, store, recorder = _router_client(tmp_path)
    with bind_observation(owner_account_id="alice", request_id="request-a", session_id="session-a"):
        with span("interaction"):
            from crew.core.observability import event

            event("owner-log", attributes={"message": "owner diagnostic"})
    from crew.core.observability import event

    event("system-log", attributes={"message": "startup diagnostic"})
    assert recorder.flush(2)

    response = client.get("/api/tracing/logs", headers={"x-owner": "alice"})
    assert response.status_code == 200
    messages = {item["message"] for item in response.json()["items"]}
    assert {"owner diagnostic", "startup diagnostic"} <= messages

    denied = client.get("/api/tracing/logs", headers={"x-owner": "mallory"})
    assert denied.status_code == 403
    # Trace details stay owner scoped even though the admin log view includes
    # process records.
    assert store.list_traces(owner_account_id="system")["items"] == []
    recorder.close()
    store.close()


def _write_trace_records(store: TraceStore, owner: str, trace_id: str, *, event_name: str = "base-event") -> None:
    context = ObservationContext(trace_id=trace_id, owner_account_id=owner, request_id=trace_id)
    store.write_batch(
        [
            ObservationRecord(
                record_type="span.start",
                name="interaction",
                context=context,
                status="running",
                started_at_us=1,
            ),
            ObservationRecord(
                record_type="span.end",
                name="interaction",
                context=context,
                status="succeeded",
                started_at_us=1,
                ended_at_us=2,
                duration_ms=0.001,
            ),
            ObservationRecord(
                record_type="event",
                name=event_name,
                context=context,
                status="recorded",
                occurred_at_us=2,
            ),
        ]
    )


def test_export_is_frozen_across_pages_and_payload_refs(tmp_path):
    store = TraceStore(tmp_path / "traces.sqlite3")
    _write_trace_records(store, "alice", "0" * 31 + "1")
    payload_id = "payload-from-event"
    store.write_payload(
        owner_account_id="alice",
        payload_id=payload_id,
        trace_id="0" * 31 + "1",
        span_id="",
        stage="llm.request",
        capture_state="complete",
        content={"safe": True},
    )
    store.write_batch(
        [
            ObservationRecord(
                record_type="event",
                name="payload-reference",
                context=ObservationContext(trace_id="0" * 31 + "1", owner_account_id="alice"),
                attributes={"payload_id": payload_id},
                status="recorded",
            )
        ]
    )
    for index in range(1, 202):
        _write_trace_records(store, "alice", f"{index + 1:032x}", event_name=f"event-{index}")

    class LateStore(TraceStore):
        injected = False

        def list_traces(self, **kwargs):  # type: ignore[no-untyped-def]
            if not self.injected:
                self.injected = True
                _write_trace_records(self, "alice", "f" * 32, event_name="late-event")
                self.write_batch(
                    [
                        ObservationRecord(
                            record_type="event",
                            name="late-on-existing",
                            context=ObservationContext(trace_id="0" * 31 + "1", owner_account_id="alice"),
                            status="recorded",
                        )
                    ]
                )
            return super().list_traces(**kwargs)

    late_store = LateStore(tmp_path / "late.sqlite3")
    _write_trace_records(late_store, "alice", "0" * 31 + "1")
    source_payload = store.get_payload(owner_account_id="alice", payload_id=payload_id)
    assert source_payload is not None
    late_store.write_payload(
        owner_account_id="alice",
        payload_id=payload_id,
        trace_id="0" * 31 + "1",
        span_id="",
        stage=source_payload["stage"],
        capture_state=source_payload["capture_state"],
        content=source_payload["content"],
        created_at_us=source_payload["created_at_us"],
    )
    late_store.write_batch(
        [
            ObservationRecord(
                record_type="event",
                name="payload-reference",
                context=ObservationContext(trace_id="0" * 31 + "1", owner_account_id="alice"),
                attributes={"payload_id": payload_id},
                status="recorded",
                event_id="payload-reference-copy",
            )
        ]
    )
    # Copying through the public records keeps this test independent from the
    # writer's private SQLite connection.
    for trace in store.list_traces(owner_account_id="alice", limit=200)["items"]:
        trace_id = trace["trace_id"]
        spans = store.list_spans(owner_account_id="alice", trace_id=trace_id)
        events = store.list_events(owner_account_id="alice", trace_id=trace_id)
        records = []
        for item in spans:
            context = ObservationContext(
                trace_id=trace_id,
                span_id=item["span_id"],
                owner_account_id="alice",
                parent_span_id=item["parent_span_id"],
            )
            records.append(ObservationRecord(record_type="span.end", name=item["name"], context=context, status=item["status"], started_at_us=item["started_at_us"], ended_at_us=item["ended_at_us"], duration_ms=item["duration_ms"]))
        for item in events:
            records.append(ObservationRecord(record_type="event", name=item["name"], context=ObservationContext(trace_id=trace_id, owner_account_id="alice"), attributes=item["attributes"], status=item["status"], occurred_at_us=item["occurred_at_us"], event_id=item["event_id"]))
        late_store.write_batch(records)
    # The first page above intentionally has 200 rows; copy the remaining row.
    cursor = store.list_traces(owner_account_id="alice", limit=200)["next_cursor"]
    for trace in store.list_traces(owner_account_id="alice", limit=200, cursor=cursor)["items"]:
        _write_trace_records(late_store, "alice", trace["trace_id"])

    path = tmp_path / "frozen.jsonl"
    result = _write_export_file(
        late_store,
        owner="alice",
        path=path,
        fmt="jsonl",
        filter_payload={},
        include_payloads=True,
        max_traces=500,
    )
    assert result["partial"] is False
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["record_type"] == "manifest"
    names = {line.get("name") for line in lines}
    assert "late-event" not in names
    assert "late-on-existing" not in names
    payload_rows = [line for line in lines if line.get("record_type") == "payload"]
    assert {row["payload_id"] for row in payload_rows} == {payload_id}
    assert result["count"] == 202
    assert path.stat().st_size == result["bytes"]
    late_store.close()
    store.close()


def test_export_partial_manifest_and_byte_budget(tmp_path):
    store = TraceStore(tmp_path / "traces.sqlite3")
    _write_trace_records(store, "alice", "1" * 32)
    _write_trace_records(store, "alice", "2" * 32)
    path = tmp_path / "partial.json"
    result = _write_export_file(
        store,
        owner="alice",
        path=path,
        fmt="json",
        filter_payload={},
        include_payloads=False,
        max_traces=1,
        max_bytes=64 * 1024,
    )
    document = json.loads(path.read_text(encoding="utf-8"))
    assert result["partial"] is True
    assert result["partial_reason"] == "max_traces"
    assert document["manifest"]["partial"] is True
    assert document["manifest"]["partial_reason"] == "max_traces"
    assert len(document["traces"]) == 1

    tiny_path = tmp_path / "tiny.jsonl"
    tiny_result = _write_export_file(
        store,
        owner="alice",
        path=tiny_path,
        fmt="jsonl",
        filter_payload={},
        include_payloads=False,
        max_bytes=1_000,
    )
    tiny_manifest = json.loads(tiny_path.read_text(encoding="utf-8").splitlines()[0])
    assert tiny_result["partial"] is True
    assert tiny_result["partial_reason"] == "size_limit"
    assert tiny_manifest["partial"] is True
    assert tiny_manifest["partial_reason"] == "size_limit"
    assert tiny_path.stat().st_size <= 1_000
    store.close()


def test_export_jobs_recover_expire_and_reject_paths_outside_budget(tmp_path):
    store = TraceStore(tmp_path / "traces.sqlite3")
    export_dir = store.path.parent / "exports"
    safe_path = export_dir / "job.jsonl"
    now_us = 1_000_000
    job = store.create_export_job({
        "export_id": "job-1",
        "owner_scope": "alice",
        "artifact_path": str(safe_path),
        "status": "running",
        "process_instance_id": "old-process",
        "created_at_us": now_us,
        "updated_at_us": now_us,
        "expires_at_us": now_us + 100,
    })
    assert job["path"] == str(safe_path)
    safe_path.parent.mkdir(parents=True, exist_ok=True)
    safe_path.write_text("partial", encoding="utf-8")
    assert store.recover_export_jobs(process_instance_id="new-process", now_us=now_us + 1) == 1
    assert store.get_export_job(owner_account_id="alice", export_id="job-1")["status"] == "interrupted"
    cleanup = store.cleanup_export_artifacts(now_us=now_us + 101)
    assert cleanup["expired_jobs"] == 1
    assert not safe_path.exists()
    with pytest.raises(ValueError):
        store.create_export_job({
            "export_id": "job-2",
            "owner_scope": "alice",
            "artifact_path": str(tmp_path / "outside.jsonl"),
        })
    store.close()
