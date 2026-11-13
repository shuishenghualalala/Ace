from __future__ import annotations

import asyncio
import logging
import threading
from types import SimpleNamespace

import pytest

from crew.core.observability import (
    ObservationContext,
    ObservationRecord,
    attach_context,
    bind_observation,
    bind_sink,
    capture_context,
    capture_payload,
    event,
    install_sink,
    span,
)
from crew.state.observability import ObservationLogHandler, ObservationPolicy, ObservationRecorder, sanitize_value
from crew.state.trace_store import TraceStore


@pytest.fixture(autouse=True)
def clear_observation_sink():
    yield
    install_sink(None)


def _recorder(tmp_path, **policy):
    store = TraceStore(tmp_path / "traces.sqlite3")
    recorder = ObservationRecorder(store, policy=ObservationPolicy(**policy))
    install_sink(recorder)
    return store, recorder


def test_nested_spans_have_root_parent_and_terminal_status(tmp_path):
    store, recorder = _recorder(tmp_path)
    with bind_observation(owner_account_id="owner-a", request_id="request-a", session_id="session-a"):
        with span("interaction", module="gateway"):
            with span("provider", kind="client", module="providers"):
                event("first-token", attributes={"level": "INFO"})
    assert recorder.flush(2)
    trace = store.list_traces(owner_account_id="owner-a")["items"][0]
    assert trace["status"] == "succeeded"
    spans = store.list_spans(owner_account_id="owner-a", trace_id=trace["trace_id"])
    assert len(spans) == 2
    assert spans[0]["parent_span_id"] == ""
    assert spans[1]["parent_span_id"] == spans[0]["span_id"]
    recorder.close()
    store.close()


def test_context_is_restored_after_failure_and_async_tasks(tmp_path):
    store, recorder = _recorder(tmp_path)
    with bind_observation(owner_account_id="owner-a", request_id="request-a"):
        caller = capture_context()
        try:
            with span("failing"):
                raise RuntimeError("expected")
        except RuntimeError:
            pass
        assert capture_context() == caller

        async def child():
            await asyncio.sleep(0)
            return capture_context()

        child_context = asyncio.run(child())
        assert child_context == caller
    recorder.flush(2)
    assert store.list_traces(owner_account_id="owner-a")["items"][0]["has_error_span"] == 1
    recorder.close()
    store.close()


def test_sink_override_does_not_leak_and_global_sink_reaches_thread(tmp_path):
    store, recorder = _recorder(tmp_path)
    class MemorySink:
        def __init__(self):
            self.records = []
        def accept(self, record):
            self.records.append(record)
            return True
        def capture_payload(self, *args, **kwargs):
            return capture_payload(*args, **kwargs)
        def flush(self, timeout=None):
            return True
        def close(self, timeout=None):
            return True

    override = MemorySink()
    with bind_sink(override):
        event("only-override")
    seen = []
    with bind_observation(owner_account_id="owner-a", request_id="request-a"):
        context = capture_context()
        thread = threading.Thread(target=lambda: _thread_event(context, seen))
        thread.start()
        thread.join()
    assert seen == [True]
    assert len(override.records) == 1
    recorder.flush(2)
    assert store.list_events(owner_account_id="owner-a")[0]["name"] == "thread-event"
    recorder.close()
    store.close()


def test_retiring_app_cannot_remove_a_newer_global_sink():
    class Sink:
        def __init__(self):
            self.records = []

        def accept(self, record):
            self.records.append(record)
            return True

        def capture_payload(self, *args, **kwargs):
            return capture_payload(*args, **kwargs)

        def flush(self, timeout=None):
            return True

        def close(self, timeout=None):
            return True

    first = Sink()
    second = Sink()
    install_sink(first)
    install_sink(second)
    from crew.core.observability import remove_sink

    remove_sink(first)
    event("newer-sink-event")
    assert len(first.records) == 0
    assert [record.name for record in second.records] == ["newer-sink-event"]
    remove_sink(second)


def _thread_event(context, seen):
    with attach_context(context):
        seen.append(event("thread-event"))


def test_redaction_is_bounded_and_does_not_call_custom_str(tmp_path):
    class Explosive:
        def __str__(self):
            raise AssertionError("str must not be called")
        def __repr__(self):
            raise AssertionError("repr must not be called")

    cleaned = sanitize_value(
        {"api_key": "secret", "authorization": "Bearer abcdefghijk", "object": Explosive(), "deep": {"x": "ok"}},
        policy=ObservationPolicy(max_depth=1),
    )
    assert cleaned.value["api_key"] == "<secret_redacted>"
    assert cleaned.value["object"]["redacted"] is True
    assert cleaned.truncated_reason == "depth_limit"


def test_redaction_covers_provider_tokens_signed_urls_and_mapping_keys():
    class ExplosiveKey:
        def __str__(self):
            raise AssertionError("mapping key __str__ must not run")

    cleaned = sanitize_value(
        {
            ExplosiveKey(): "safe",
            "text": (
                "sk-1234567890abcdef xoxb-1234567890abcdef ghp_1234567890abcdef "
                "AKIA1234567890ABCDEF https://s3.example.test/a?X-Amz-Signature=secret-value"
            ),
        },
        policy=ObservationPolicy(),
    )
    assert cleaned.value["<ExplosiveKey>"] == "safe"
    text = cleaned.value["text"]
    assert "1234567890abcdef" not in text
    assert "secret-value" not in text
    assert text.count("<secret_redacted>") >= 5


def test_metadata_logs_store_facts_without_body(tmp_path):
    store, recorder = _recorder(tmp_path, capture_profile="metadata")
    with bind_observation(owner_account_id="owner-a", request_id="request-a"):
        event("diagnostic", attributes={"message": "private正文", "module": "agent"})
    assert recorder.flush(2)
    row = store.list_logs(owner_account_id="owner-a")["items"][0]
    assert row["message"] == ""
    assert row["attributes"]["message_length"] == len("private正文")
    assert row["attributes"]["message_sha256"]
    recorder.close()
    store.close()


def test_payload_capture_metadata_and_owner_isolation(tmp_path):
    store, recorder = _recorder(tmp_path, capture_profile="content_redacted", max_payload_bytes=32)
    with bind_observation(owner_account_id="owner-a", request_id="request-a"):
        with span("root"):
            capture = capture_payload(
                "llm.request",
                {"text": "x" * 100, "token": "secret"},
                attributes={"capture_layer": "provider_arguments", "policy_version": 7},
            )
            assert capture.capture_state == "truncated"
            assert capture.payload_id
    recorder.flush(2)
    payload = store.get_payload(owner_account_id="owner-a", payload_id=capture.payload_id)
    assert payload is not None
    assert payload["attributes"]["capture_layer"] == "provider_arguments"
    assert payload["attributes"]["policy_version"] == 7
    assert payload["attributes"]["capture_policy_version"] == 1
    assert store.get_payload(owner_account_id="owner-b", payload_id=capture.payload_id) is None
    recorder.close()
    store.close()


def test_writer_degrades_when_store_is_closed(tmp_path):
    store = TraceStore(tmp_path / "traces.sqlite3")
    recorder = ObservationRecorder(store)
    install_sink(recorder)
    store.close()
    with bind_observation(owner_account_id="owner-a", request_id="request-a"):
        event("after-store-close")
    assert recorder.flush(1)
    assert recorder.writer_errors >= 1
    recorder.close()


def test_unbound_third_party_logs_use_system_scope(tmp_path):
    store, recorder = _recorder(tmp_path)
    logger = logging.getLogger("third_party.observability.test")
    handler = ObservationLogHandler(recorder)
    logger.addHandler(handler)
    previous_level = logger.level
    previous_propagate = logger.propagate
    try:
        logger.setLevel(logging.WARNING)
        logger.propagate = False
        logger.warning("startup diagnostic")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate
    assert recorder.flush(2)
    system_logs = store.list_logs(owner_account_id="system")
    assert any(item["message"] == "startup diagnostic" for item in system_logs["items"])
    assert store.list_logs(owner_account_id="local")["items"] == []
    recorder.close()
    store.close()


def test_close_accounts_for_stop_sentinel(tmp_path):
    store, recorder = _recorder(tmp_path)
    assert recorder.close(2)
    assert recorder._queue.unfinished_tasks == 0  # noqa: SLF001 - lifecycle invariant
    store.close()


def test_keyset_pagination_is_stable(tmp_path):
    store, recorder = _recorder(tmp_path)
    for index in range(3):
        with bind_observation(owner_account_id="owner-a", request_id=f"request-{index}"):
            with span(f"root-{index}"):
                pass
    recorder.flush(2)
    first = store.list_traces(owner_account_id="owner-a", limit=2)
    second = store.list_traces(owner_account_id="owner-a", limit=2, cursor=first["next_cursor"])
    assert len(first["items"]) == 2
    assert len(second["items"]) == 1
    assert {item["trace_id"] for item in first["items"]}.isdisjoint(item["trace_id"] for item in second["items"])
    recorder.close()
    store.close()


def test_span_keyset_keeps_same_timestamp_rows_and_links_are_owner_scoped(tmp_path):
    store = TraceStore(tmp_path / "traces.sqlite3")
    owner = "owner-a"
    trace_a = "a" * 32
    trace_b = "b" * 32
    context_a = ObservationContext(trace_id=trace_a, owner_account_id=owner)
    context_b = ObservationContext(trace_id=trace_b, owner_account_id=owner)
    records = [
        ObservationRecord(record_type="span.start", name=f"span-{index}", context=context_a.child(span_id=f"{index:016x}"), started_at_us=100)
        for index in range(4)
    ]
    records.append(ObservationRecord(record_type="span.start", name="root-b", context=context_b, started_at_us=1))
    store.write_batch(records)
    first = store.page_spans(owner_account_id=owner, trace_id=trace_a, limit=2)
    second = store.page_spans(
        owner_account_id=owner,
        trace_id=trace_a,
        limit=2,
        after_started_at_us=first["next_started_at_us"],
        after_span_id=first["next_span_id"],
    )
    assert [item["span_id"] for item in first["items"] + second["items"]] == [
        f"{index:016x}" for index in range(4)
    ]
    assert store.write_link(owner_account_id=owner, trace_id=trace_a, linked_trace_id=trace_b)
    assert store.write_link(owner_account_id="owner-b", trace_id=trace_a, linked_trace_id=trace_b) is False
    assert store.list_links(owner_account_id=owner, trace_id=trace_a) == [
        {"linked_trace_id": trace_b, "relation": "related"}
    ]
    store.close()


def test_provider_stream_restores_caller_context_while_suspended(tmp_path):
    from crew.agent.executor.builtin import _observed_provider_stream

    store, recorder = _recorder(tmp_path)

    async def run():
        with bind_observation(owner_account_id="owner-a", request_id="request-a"):
            caller = capture_context()

            async def stream():
                yield SimpleNamespace(delta_text="first")
                yield SimpleNamespace(delta_text="second")

            seen = []
            async for chunk in _observed_provider_stream(
                stream(),
                request_payload={"messages": []},
                provider="fake",
                model="fake-model",
                attempt=1,
                provider_index=0,
            ):
                seen.append((chunk.delta_text, capture_context()))
            assert [item[0] for item in seen] == ["first", "second"]
            assert all(item[1] == caller for item in seen)
            assert capture_context() == caller

    asyncio.run(run())
    assert recorder.flush(2)
    recorder.close()
    store.close()
