"""Developer-gated trace, log, payload and export APIs."""

from __future__ import annotations

import csv
import asyncio
import io
import json
import os
import secrets
import shutil
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from crew.core.observability import (
    SYSTEM_OWNER_ACCOUNT_ID,
    ObservationContext,
    ObservationRecord,
    bind_observation,
    capture_payload,
)
from crew.gateway.auth import AuthenticationError, account_from_request, require_admin
from crew.state.trace_store import MAX_PAGE_SIZE, TraceStore


_TRACE_ID_CHARS = frozenset("0123456789abcdef")
_CLIENT_EVENTS = frozenset({"user.submitted", "user.presented", "desktop.error", "desktop.event"})
_EXPORT_FORMATS = frozenset({"json", "jsonl", "csv"})


class _ClientEventRateLimiter:
    """Bounded per-owner and process-wide client-event limiter."""

    def __init__(self, *, per_owner: int = 100, total: int = 1_000, window_seconds: float = 10.0) -> None:
        self.per_owner = max(1, int(per_owner))
        self.total = max(self.per_owner, int(total))
        self.window_seconds = max(1.0, float(window_seconds))
        self._owners: dict[str, deque[float]] = {}
        self._all: deque[float] = deque()

    def allow(self, owner: str, count: int, *, now: float | None = None) -> bool:
        stamp = time.monotonic() if now is None else float(now)
        cutoff = stamp - self.window_seconds
        while self._all and self._all[0] <= cutoff:
            self._all.popleft()
        owner_queue = self._owners.setdefault(owner, deque())
        while owner_queue and owner_queue[0] <= cutoff:
            owner_queue.popleft()
        if len(owner_queue) + count > self.per_owner or len(self._all) + count > self.total:
            # Keep state bounded even when a caller rotates arbitrary owner
            # identifiers; rejected requests do not consume another token.
            self._trim_owner_buckets()
            return False
        owner_queue.extend([stamp] * count)
        self._all.extend([stamp] * count)
        self._trim_owner_buckets()
        return True

    def _trim_owner_buckets(self) -> None:
        if len(self._owners) <= 4_096:
            return
        # Owner identifiers are authenticated input but can still rotate over
        # a long-lived process.  Evict oldest buckets as a hard memory bound;
        # the process-wide deque remains the authoritative total capacity.
        while len(self._owners) > 4_096:
            oldest = next(iter(self._owners), None)
            if oldest is None:
                break
            self._owners.pop(oldest, None)


class _ExportCancelled(RuntimeError):
    """Internal signal used to stop a worker before it commits a file."""


def _config_observability(config: Any) -> dict[str, Any]:
    value = getattr(config, "observability", None)
    return value if isinstance(value, dict) else {}


def _developer_access(config: Any) -> bool:
    raw = _config_observability(config)
    return bool(raw.get("developer_access", getattr(config, "observability_developer_access", False)))


def _trace_access(request: Request, crew: Any) -> tuple[Any | None, JSONResponse | None]:
    try:
        account = account_from_request(request)
    except AuthenticationError as exc:
        return None, JSONResponse({"ok": False, "error": str(exc)}, status_code=401)
    if not bool(getattr(crew.config, "observability_enabled", True)):
        return account, JSONResponse(
            {"ok": False, "available": False, "error": "追踪采集未启用", "code": "TRACE_DISABLED"},
            status_code=403,
        )
    if not _developer_access(crew.config):
        return account, JSONResponse(
            {"ok": False, "available": False, "error": "未开放开发诊断能力", "code": "TRACE_ACCESS_DISABLED"},
            status_code=403,
        )
    try:
        require_admin(account, crew.config)
    except AuthenticationError as exc:
        return account, JSONResponse({"ok": False, "error": str(exc), "code": "TRACE_ADMIN_REQUIRED"}, status_code=403)
    return account, None


def _store(crew: Any) -> TraceStore | None:
    value = getattr(crew, "observability_store", None)
    return value if isinstance(value, TraceStore) else None


def _recorder(crew: Any) -> Any | None:
    value = getattr(crew, "observability", None)
    return value if value is not None and hasattr(value, "accept") else None


def _values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _valid_trace_id(value: str) -> bool:
    text = str(value or "")
    return len(text) == 32 and set(text.lower()) <= _TRACE_ID_CHARS


def _json_response(value: Any, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(value, status_code=status_code)


def _trace_bundle(
    store: TraceStore,
    owner: str,
    trace_id: str,
    *,
    include_payloads: bool = False,
    snapshot_ingest_seq: int | None = None,
    snapshot_started_at_us: int | None = None,
) -> dict[str, Any] | None:
    trace = store.get_trace(owner_account_id=owner, trace_id=trace_id)
    if trace is None:
        return None
    spans = store.list_spans(owner_account_id=owner, trace_id=trace_id, limit=2_000)
    events = store.list_events(
        owner_account_id=owner,
        trace_id=trace_id,
        limit=2_000,
        max_ingest_seq=snapshot_ingest_seq,
    )
    result: dict[str, Any] = {"trace": trace, "spans": spans, "events": events}
    if include_payloads:
        payload_ids: set[str] = set()
        for span in spans:
            attrs = span.get("attributes") if isinstance(span.get("attributes"), dict) else {}
            if isinstance(attrs, dict):
                _collect_payload_refs(attrs, payload_ids)
        for event_item in events:
            attrs = event_item.get("attributes") if isinstance(event_item.get("attributes"), dict) else {}
            if isinstance(attrs, dict):
                _collect_payload_refs(attrs, payload_ids)
        payloads: list[dict[str, Any]] = []
        for payload_id in sorted(payload_ids):
            payload = store.get_payload(
                owner_account_id=owner,
                payload_id=payload_id,
                max_created_at_us=snapshot_started_at_us,
            )
            if payload is not None:
                payloads.append(payload)
        result["payloads"] = payloads
    return result


_PAYLOAD_REF_KEYS = frozenset({
    "payload_id",
    "request_payload_id",
    "response_payload_id",
    "input_payload_id",
    "output_payload_id",
})


def _collect_payload_refs(value: Any, output: set[str], *, key: str = "") -> None:
    """Collect only explicit payload references from bounded metadata."""
    if isinstance(value, dict):
        for item_key, item in value.items():
            name = str(item_key)
            if name in _PAYLOAD_REF_KEYS and isinstance(item, str) and item:
                output.add(item)
            elif name == "payload_ids" and isinstance(item, (list, tuple, set)):
                output.update(str(payload_id) for payload_id in item if str(payload_id))
            elif isinstance(item, (dict, list, tuple, set)):
                _collect_payload_refs(item, output, key=name)
    elif key in _PAYLOAD_REF_KEYS and isinstance(value, str) and value:
        output.add(value)


def _csv_bytes(bundle: dict[str, Any]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["record_type", "trace_id", "span_id", "name", "module", "component", "operation", "status", "started_at_us", "ended_at_us", "duration_ms", "message"])
    trace = bundle.get("trace") or {}
    trace_id = str(trace.get("trace_id") or "")
    writer.writerow(["trace", trace_id, "", _safe_cell(trace.get("summary")), "", "", "", trace.get("status"), trace.get("started_at_us"), trace.get("ended_at_us"), trace.get("server_total_ms"), ""])
    for span in bundle.get("spans", ()):
        writer.writerow([
            "span", trace_id, span.get("span_id"), _safe_cell(span.get("name")),
            _safe_cell(span.get("module")), _safe_cell(span.get("component")),
            _safe_cell(span.get("operation")), span.get("status"), span.get("started_at_us"),
            span.get("ended_at_us"), span.get("duration_ms"), _safe_cell(span.get("error_message")),
        ])
    for event in bundle.get("events", ()):
        writer.writerow([
            "event", trace_id, event.get("span_id"), _safe_cell(event.get("name")),
            _safe_cell(event.get("module")), _safe_cell(event.get("component")),
            _safe_cell(event.get("operation")), event.get("status"), event.get("occurred_at_us"),
            "", "", _safe_cell(event.get("message")),
        ])
    return output.getvalue().encode("utf-8")


def _safe_cell(value: Any) -> Any:
    text = "" if value is None else str(value)
    if text[:1] in {"=", "+", "-", "@"}:
        return "'" + text
    return text


def _write_csv_bundle(writer: csv.writer, bundle: dict[str, Any]) -> int:
    trace = bundle.get("trace") or {}
    trace_id = str(trace.get("trace_id") or "")
    writer.writerow(["trace", trace_id, "", _safe_cell(trace.get("summary")), "", "", "", trace.get("status"), trace.get("started_at_us"), trace.get("ended_at_us"), trace.get("server_total_ms"), ""])
    count = 1
    for item in bundle.get("spans", ()):
        writer.writerow(["span", trace_id, item.get("span_id"), _safe_cell(item.get("name")), _safe_cell(item.get("module")), _safe_cell(item.get("component")), _safe_cell(item.get("operation")), item.get("status"), item.get("started_at_us"), item.get("ended_at_us"), item.get("duration_ms"), _safe_cell(item.get("error_message"))])
        count += 1
    for item in bundle.get("events", ()):
        writer.writerow(["event", trace_id, item.get("span_id"), _safe_cell(item.get("name")), _safe_cell(item.get("module")), _safe_cell(item.get("component")), _safe_cell(item.get("operation")), item.get("status"), item.get("occurred_at_us"), "", "", _safe_cell(item.get("message"))])
        count += 1
    return count


_CSV_COLUMNS = [
    "record_type", "trace_id", "span_id", "name", "module", "component",
    "operation", "status", "started_at_us", "ended_at_us", "duration_ms", "message",
]


def _csv_header_bytes() -> bytes:
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerow(_CSV_COLUMNS)
    return output.getvalue().encode("utf-8")


def _csv_manifest_bytes(manifest: dict[str, Any]) -> bytes:
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerow(
        ["manifest", "", "", "", "", "", "", "", "", "", "", json.dumps(manifest, ensure_ascii=False)]
    )
    return output.getvalue().encode("utf-8")


def _bundle_csv_bytes(bundle: dict[str, Any]) -> bytes:
    output = io.StringIO(newline="")
    _write_csv_bundle(csv.writer(output, lineterminator="\n"), bundle)
    return output.getvalue().encode("utf-8")


def _bundle_jsonl_bytes(bundle: dict[str, Any]) -> bytes:
    rows: list[dict[str, Any]] = [
        {"record_type": "trace", **dict(bundle.get("trace") or {})},
        *({"record_type": "span", **item} for item in bundle.get("spans", ())),
        *({"record_type": "event", **item} for item in bundle.get("events", ())),
        *({"record_type": "payload", **item} for item in bundle.get("payloads", ())),
    ]
    return b"".join(
        (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        for row in rows
    )


def _iter_trace_jsonl(bundle: dict[str, Any], *, trace_id: str, owner: str):
    """Yield one bounded JSONL record at a time for a single-trace download."""
    manifest = {"record_type": "manifest", "trace_id": trace_id, "owner_scope": owner}
    yield (json.dumps(manifest, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    for record_type, key in (("trace", "trace"), ("span", "spans"), ("event", "events"), ("payload", "payloads")):
        values = [bundle[key]] if key == "trace" else bundle.get(key, ())
        for item in values:
            yield (
                json.dumps({"record_type": record_type, **item}, ensure_ascii=False, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")


def _public_export_job(job: dict[str, Any]) -> dict[str, Any]:
    """Expose durable progress without leaking local artifact paths."""
    hidden = {"artifact_path", "path", "process_instance_id"}
    return {key: value for key, value in job.items() if key not in hidden}


def _export_artifact_path(store: TraceStore, job: dict[str, Any]) -> Path | None:
    """Resolve a durable export artifact only when it stays in the export dir."""
    raw_path = str(job.get("path") or job.get("artifact_path") or "")
    if not raw_path:
        return None
    candidate = Path(raw_path)
    export_dir = store.path.parent / "exports"
    if not TraceStore._safe_export_path(candidate, export_dir):
        return None
    return candidate


def _write_export_file(
    store: TraceStore,
    *,
    owner: str,
    path: Path,
    fmt: str,
    filter_payload: dict[str, Any],
    include_payloads: bool,
    max_traces: int = 10_000,
    max_bytes: int = 64 * 1024 * 1024,
    timeout_seconds: float = 60.0,
    stop_event: threading.Event | None = None,
    snapshot_ingest_seq: int | None = None,
    snapshot_started_at_us: int | None = None,
) -> dict[str, Any]:
    """Stream a frozen, bounded export in a worker thread."""
    started = time.monotonic()
    snapshot_started_at_us = int(snapshot_started_at_us or time.time_ns() // 1_000)
    snapshot_ingest_seq = int(
        snapshot_ingest_seq
        if snapshot_ingest_seq is not None
        else store.status(owner_account_id=owner).get("latest_ingest_seq") or 0
    )
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    records_path = path.with_suffix(path.suffix + ".records.tmp")
    tmp_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp_path.unlink(missing_ok=True)
    records_path.unlink(missing_ok=True)
    count = 0
    partial = False
    partial_reason = ""
    params = {
        "trace_id": filter_payload.get("trace_id"),
        "start_after_us": filter_payload.get("start_after_us"),
        "start_before_us": filter_payload.get("start_before_us"),
        "status": _values(filter_payload.get("status")),
        "source": _values(filter_payload.get("source")),
        "module": _values(filter_payload.get("module")),
        "component": _values(filter_payload.get("component")),
        "operation": _values(filter_payload.get("operation")),
        "feature_id": _values(filter_payload.get("feature_id")),
        "provider": _values(filter_payload.get("provider")),
        "model": _values(filter_payload.get("model")),
        "tool": _values(filter_payload.get("tool")),
        "service": _values(filter_payload.get("service")),
        "keyword": str(filter_payload.get("q") or ""),
        "min_duration_ms": filter_payload.get("min_duration_ms"),
        "min_server_total_ms": filter_payload.get("min_server_total_ms"),
        "has_error_span": filter_payload.get("has_error_span"),
    }
    cursor: str | None = None
    generated_at_us = time.time_ns() // 1_000

    def _manifest() -> dict[str, Any]:
        return {
            "record_type": "manifest",
            "owner_scope": owner,
            "format": fmt,
            "snapshot_ingest_seq": snapshot_ingest_seq,
            "generated_at_us": generated_at_us,
            "count": count,
            "partial": partial,
            "partial_reason": partial_reason,
        }

    def _reserve_tail() -> int:
        # Reserve a deliberately conservative tail before accepting a record;
        # this makes the committed artifact stay within the byte budget even
        # when the final manifest changes from complete to partial.
        reserve_manifest = {
            "record_type": "manifest",
            "owner_scope": owner,
            "format": fmt,
            "snapshot_ingest_seq": snapshot_ingest_seq,
            "generated_at_us": generated_at_us,
            "count": max(0, int(max_traces)),
            "partial": True,
            "partial_reason": "size_limit",
        }
        encoded = json.dumps(reserve_manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if fmt == "json":
            return len(b'],"manifest":') + len(encoded) + 1
        if fmt == "jsonl":
            return len(b'{"record_type":"manifest"}\n') + len(encoded) + 256
        return len(_csv_header_bytes()) + len(_csv_manifest_bytes(reserve_manifest)) + 256

    prefix = b'{"traces":[' if fmt == "json" else b""
    written_bytes = len(prefix)
    first_json_record = True
    try:
        with records_path.open("wb") as records_handle:
            if prefix:
                records_handle.write(prefix)
            while True:
                if stop_event is not None and stop_event.is_set():
                    raise _ExportCancelled
                if count >= max_traces:
                    partial, partial_reason = True, "max_traces"
                    break
                if time.monotonic() - started > timeout_seconds:
                    partial, partial_reason = True, "timeout"
                    break
                page = store.list_traces(
                    owner_account_id=owner,
                    limit=MAX_PAGE_SIZE,
                    cursor=cursor,
                    snapshot_ingest_seq=snapshot_ingest_seq,
                    snapshot_started_at_us=snapshot_started_at_us,
                    **params,
                )
                items = page.get("items") or []
                if not items:
                    break
                for item in items:
                    if stop_event is not None and stop_event.is_set():
                        raise _ExportCancelled
                    if count >= max_traces:
                        partial, partial_reason = True, "max_traces"
                        break
                    if time.monotonic() - started > timeout_seconds:
                        partial, partial_reason = True, "timeout"
                        break
                    bundle = _trace_bundle(
                        store,
                        owner,
                        str(item["trace_id"]),
                        include_payloads=include_payloads,
                        snapshot_ingest_seq=snapshot_ingest_seq,
                        snapshot_started_at_us=snapshot_started_at_us,
                    )
                    if bundle is None:
                        continue
                    if fmt == "json":
                        encoded = json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                        separator = b"" if first_json_record else b","
                    elif fmt == "jsonl":
                        encoded = _bundle_jsonl_bytes(bundle)
                        separator = b""
                    else:
                        encoded = _bundle_csv_bytes(bundle)
                        separator = b""
                    candidate_size = written_bytes + len(separator) + len(encoded) + _reserve_tail()
                    if candidate_size > max_bytes:
                        partial, partial_reason = True, "size_limit"
                        break
                    if separator:
                        records_handle.write(separator)
                        written_bytes += len(separator)
                    records_handle.write(encoded)
                    written_bytes += len(encoded)
                    first_json_record = False
                    count += 1
                if partial or not page.get("has_more"):
                    break
                cursor = str(page.get("next_cursor") or "") or None

        manifest = _manifest()
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with tmp_path.open("wb") as output:
            if fmt == "jsonl":
                output.write(manifest_bytes)
                output.write(b"\n")
                with records_path.open("rb") as records_handle:
                    shutil.copyfileobj(records_handle, output, length=1024 * 1024)
            elif fmt == "csv":
                output.write(_csv_header_bytes())
                output.write(_csv_manifest_bytes(manifest))
                with records_path.open("rb") as records_handle:
                    shutil.copyfileobj(records_handle, output, length=1024 * 1024)
            else:
                with records_path.open("rb") as records_handle:
                    shutil.copyfileobj(records_handle, output, length=1024 * 1024)
                output.write(b'],"manifest":')
                output.write(manifest_bytes)
                output.write(b"}")
            output.flush()
            try:
                os.fsync(output.fileno())
            except OSError:
                pass
        final_size = tmp_path.stat().st_size
        if final_size > max_bytes:
            raise RuntimeError("export byte budget exceeded")
        if stop_event is not None and stop_event.is_set():
            raise _ExportCancelled
        os.replace(tmp_path, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return {
            "count": count,
            "partial": partial,
            "partial_reason": partial_reason,
            "snapshot_ingest_seq": snapshot_ingest_seq,
            "bytes": final_size,
        }
    finally:
        tmp_path.unlink(missing_ok=True)
        records_path.unlink(missing_ok=True)


def create_tracing_router(crew: Any) -> APIRouter:
    router = APIRouter()
    # Runtime handles are intentionally ephemeral; durable job state lives in
    # TraceStore so status/download survive a gateway restart.
    export_runtime: dict[str, tuple[threading.Event, asyncio.Task[Any]]] = {}
    rate_limiter = _ClientEventRateLimiter()
    store_at_creation = _store(crew)
    recorder_at_creation = _recorder(crew)
    if store_at_creation is not None:
        try:
            store_at_creation.recover_export_jobs(
                process_instance_id=str(getattr(recorder_at_creation, "process_instance_id", "")),
            )
            store_at_creation.cleanup_export_artifacts(
                export_dir=store_at_creation.path.parent / "exports",
            )
        except Exception:
            pass

    @router.get("/api/tracing/capabilities")
    async def capabilities(request: Request) -> JSONResponse:
        account, denied = _trace_access(request, crew)
        if denied is not None:
            return _json_response({"available": False, "capture_profile": _config_observability(crew.config).get("capture_profile", "metadata")})
        return _json_response({
            "available": True,
            "owner_scope": account.owner_account_id,
            "capture_profile": _config_observability(crew.config).get("capture_profile", "content_redacted"),
            "formats": sorted(_EXPORT_FORMATS),
        })

    @router.get("/api/tracing/status")
    async def status(request: Request) -> JSONResponse:
        account, denied = _trace_access(request, crew)
        if denied is not None:
            return denied
        store = _store(crew)
        if store is None:
            return _json_response({"available": False, "writer": {"enabled": False}}, status_code=503)
        recorder = _recorder(crew)
        result = store.status(owner_account_id=account.owner_account_id)
        result.update({"available": True, "writer": recorder.status() if recorder is not None and hasattr(recorder, "status") else {}})
        return _json_response(result)

    @router.get("/api/tracing/traces")
    async def traces(
        request: Request,
        limit: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
        cursor: str | None = Query(None),
        session_id: str | None = Query(None),
        request_id: str | None = Query(None),
        trace_id: str | None = Query(None),
        status: str | None = Query(None),
        source: str | None = Query(None),
        module: str | None = Query(None),
        component: str | None = Query(None),
        operation: str | None = Query(None),
        feature_id: str | None = Query(None),
        provider: str | None = Query(None),
        model: str | None = Query(None),
        tool: str | None = Query(None),
        service: str | None = Query(None),
        q: str = Query(""),
        start_after_us: int | None = Query(None, ge=0),
        start_before_us: int | None = Query(None, ge=0),
        min_duration_ms: float | None = Query(None, ge=0),
        min_server_total_ms: float | None = Query(None, ge=0),
        has_error_span: bool | None = Query(None),
    ) -> JSONResponse:
        account, denied = _trace_access(request, crew)
        if denied is not None:
            return denied
        store = _store(crew)
        if store is None:
            return _json_response({"items": [], "next_cursor": None, "available": False}, status_code=503)
        try:
            result = store.list_traces(
                owner_account_id=account.owner_account_id, limit=limit, cursor=cursor,
                trace_id=trace_id, session_id=session_id, request_id=request_id, status=_values(status), source=_values(source),
                module=_values(module), component=_values(component), operation=_values(operation),
                feature_id=_values(feature_id), provider=_values(provider), model=_values(model),
                tool=_values(tool), service=_values(service), keyword=q,
                start_after_us=start_after_us, start_before_us=start_before_us,
                min_duration_ms=min_duration_ms, min_server_total_ms=min_server_total_ms,
                has_error_span=has_error_span,
            )
        except ValueError:
            return _json_response({"ok": False, "error": "非法分页游标"}, status_code=400)
        return _json_response(result)

    @router.get("/api/tracing/traces/{trace_id}")
    async def trace(request: Request, trace_id: str) -> JSONResponse:
        account, denied = _trace_access(request, crew)
        if denied is not None:
            return denied
        if not _valid_trace_id(trace_id):
            return _json_response({"ok": False, "error": "trace_id 无效"}, status_code=400)
        store = _store(crew)
        bundle = _trace_bundle(store, account.owner_account_id, trace_id) if store is not None else None
        return _json_response(bundle or {"ok": False, "error": "trace 不存在"}, status_code=200 if bundle else 404)

    @router.get("/api/tracing/traces/{trace_id}/spans")
    async def spans(
        request: Request,
        trace_id: str,
        limit: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
        parent_span_id: str | None = Query(None),
        kind: str | None = Query(None),
        after_started_at_us: int | None = Query(None, ge=0),
        after_span_id: str | None = Query(None),
    ) -> JSONResponse:
        account, denied = _trace_access(request, crew)
        if denied is not None:
            return denied
        if not _valid_trace_id(trace_id):
            return _json_response({"ok": False, "error": "trace_id 无效"}, status_code=400)
        store = _store(crew)
        return _json_response(
            store.page_spans(
                owner_account_id=account.owner_account_id,
                trace_id=trace_id,
                limit=limit,
                parent_span_id=parent_span_id,
                kind=_values(kind),
                after_started_at_us=after_started_at_us,
                after_span_id=after_span_id,
            ) if store else {"items": [], "has_more": False, "next_started_at_us": None}
        )

    @router.get("/api/tracing/traces/{trace_id}/events")
    async def events(
        request: Request,
        trace_id: str,
        limit: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
        after_seq: int | None = Query(None, ge=0),
        level: str | None = Query(None),
        kind: str | None = Query(None),
        span_id: str | None = Query(None),
    ) -> JSONResponse:
        account, denied = _trace_access(request, crew)
        if denied is not None:
            return denied
        if not _valid_trace_id(trace_id):
            return _json_response({"ok": False, "error": "trace_id 无效"}, status_code=400)
        store = _store(crew)
        return _json_response(
            store.page_events(
                owner_account_id=account.owner_account_id,
                trace_id=trace_id,
                span_id=span_id,
                kind=_values(kind),
                level=_values(level),
                limit=limit,
                after_seq=after_seq,
            ) if store else {"items": [], "has_more": False, "next_after_seq": None}
        )

    @router.get("/api/tracing/logs")
    async def logs(
        request: Request,
        limit: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
        level: str | None = Query(None),
        q: str = Query(""),
        trace_id: str | None = Query(None),
        module: str | None = Query(None),
        component: str | None = Query(None),
        operation: str | None = Query(None),
        source: str | None = Query(None),
        start_after_us: int | None = Query(None, ge=0),
        start_before_us: int | None = Query(None, ge=0),
        after_seq: int | None = Query(None, ge=0),
    ) -> JSONResponse:
        account, denied = _trace_access(request, crew)
        if denied is not None:
            return denied
        store = _store(crew)
        if store is None:
            return _json_response({"items": [], "total": 0})
        # System-scope records have no verified business owner.  This route is
        # already behind the diagnostic-admin gate, so administrators may see
        # their owner-scoped logs together with process logs; trace/span and
        # payload routes remain strictly owner-scoped.
        owner_logs = store.list_logs(
            owner_account_id=account.owner_account_id,
            limit=limit,
            level=_values(level),
            keyword=q,
            trace_id=trace_id,
            module=_values(module),
            component=_values(component),
            operation=_values(operation),
            source=_values(source),
            start_after_us=start_after_us,
            start_before_us=start_before_us,
            after_seq=after_seq,
        )
        system_logs = store.list_logs(
            owner_account_id=SYSTEM_OWNER_ACCOUNT_ID,
            limit=limit,
            level=_values(level),
            keyword=q,
            trace_id=trace_id,
            module=_values(module),
            component=_values(component),
            operation=_values(operation),
            source=_values(source),
            start_after_us=start_after_us,
            start_before_us=start_before_us,
            after_seq=after_seq,
        )
        merged = sorted(
            [*owner_logs["items"], *system_logs["items"]],
            key=lambda item: int(item.get("ingest_seq") or 0),
            reverse=True,
        )
        items = merged[:limit]
        has_more = bool(owner_logs.get("has_more") or system_logs.get("has_more") or len(merged) > limit)
        return _json_response(
            {
                "items": items,
                "total": len(items),
                "has_more": has_more,
                "next_after_seq": items[-1].get("ingest_seq") if has_more and items else None,
            }
        )

    @router.get("/api/tracing/payloads/{payload_id}")
    async def payload(request: Request, payload_id: str) -> JSONResponse:
        account, denied = _trace_access(request, crew)
        if denied is not None:
            return denied
        store = _store(crew)
        value = store.get_payload(owner_account_id=account.owner_account_id, payload_id=payload_id) if store else None
        return _json_response(value or {"ok": False, "error": "payload 不存在"}, status_code=200 if value else 404)

    @router.post("/api/tracing/client-events")
    async def client_events(request: Request) -> JSONResponse:
        account, denied = _trace_access(request, crew)
        if denied is not None:
            return denied
        store = _store(crew)
        if store is None:
            return _json_response({"ok": False, "error": "追踪存储不可用"}, status_code=503)
        recorder = _recorder(crew)
        if recorder is None:
            return _json_response({"ok": False, "error": "追踪写入器不可用"}, status_code=503)
        raw = await request.body()
        if len(raw) > 256 * 1024:
            return _json_response({"ok": False, "error": "客户端事件请求过大"}, status_code=413)
        try:
            decoded = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _json_response({"ok": False, "error": "客户端事件请求体无效"}, status_code=400)
        payloads = decoded if isinstance(decoded, list) else [decoded]
        if not payloads or len(payloads) > 100 or any(not isinstance(item, dict) for item in payloads):
            return _json_response({"ok": False, "error": "客户端事件批量大小无效"}, status_code=400)
        # Validate the complete batch before asking the recorder to capture a
        # presentation payload or enqueue an event.  A malformed item can no
        # longer leave an earlier item from the same HTTP request persisted.
        prepared: list[tuple[dict[str, Any], str, str, str, str, str, dict[str, Any], str | None]] = []
        seen_event_ids: set[str] = set()
        accepted_count = 0
        duplicate_count = 0
        for payload in payloads:
            name_value = payload.get("name") or payload.get("event")
            if not isinstance(name_value, str) or name_value not in _CLIENT_EVENTS:
                return _json_response({"ok": False, "error": "客户端事件类型不允许"}, status_code=400)
            name = name_value
            trace_value = payload.get("trace_id") or ""
            if not isinstance(trace_value, str):
                return _json_response({"ok": False, "error": "trace_id 无效"}, status_code=400)
            trace_id = trace_value
            if trace_id and not _valid_trace_id(trace_id):
                return _json_response({"ok": False, "error": "trace_id 无效"}, status_code=400)
            if any(key in payload and payload.get(key) is not None and not isinstance(payload.get(key), str) for key in ("request_id", "session_id", "workspace_id", "message_id")):
                return _json_response({"ok": False, "error": "客户端关联字段无效"}, status_code=400)
            request_id = payload.get("request_id") or ""
            session_id = payload.get("session_id") or ""
            verified_trace = store.get_trace(owner_account_id=account.owner_account_id, trace_id=trace_id) if trace_id else None
            if trace_id and verified_trace is None:
                return _json_response({"ok": False, "error": "trace 不存在"}, status_code=404)
            if verified_trace is not None and (
                (request_id and request_id != verified_trace.get("request_id"))
                or (session_id and session_id != verified_trace.get("session_id"))
            ):
                return _json_response({"ok": False, "error": "trace 与请求归属不匹配"}, status_code=404)
            if not trace_id:
                resolved = store.find_trace(owner_account_id=account.owner_account_id, request_id=request_id, session_id=session_id)
                if resolved is None:
                    return _json_response({"ok": False, "error": "无法验证客户端事件归属"}, status_code=404)
                trace_id = str(resolved["trace_id"])
            event_id = payload.get("event_id")
            if event_id is not None and (not isinstance(event_id, str) or not event_id or len(event_id) > 128):
                return _json_response({"ok": False, "error": "event_id 无效"}, status_code=400)
            attributes = payload.get("attributes")
            if attributes is not None and not isinstance(attributes, dict):
                return _json_response({"ok": False, "error": "attributes 无效"}, status_code=400)
            if isinstance(event_id, str) and (event_id in seen_event_ids or store.event_exists(owner_account_id=account.owner_account_id, event_id=event_id)):
                duplicate_count += 1
                continue
            if isinstance(event_id, str):
                seen_event_ids.add(event_id)
            prepared.append(
                (
                    payload,
                    name,
                    trace_id,
                    request_id,
                    session_id,
                    str(payload.get("workspace_id") or "default"),
                    attributes or {},
                    event_id,
                )
            )

        if not rate_limiter.allow(account.owner_account_id, len(payloads)):
            return _json_response({"ok": False, "error": "客户端事件请求过于频繁"}, status_code=429)
        for item in prepared:
            payload, name, trace_id, request_id, session_id, workspace_id, attributes, event_id = item
            if event_id is not None and store.event_exists(owner_account_id=account.owner_account_id, event_id=event_id):
                continue
            context = ObservationContext(
                trace_id=trace_id,
                span_id="",
                owner_account_id=account.owner_account_id,
                workspace_id=workspace_id,
                session_id=session_id,
                request_id=request_id,
                message_id=payload.get("message_id") or "",
                source="desktop.renderer",
                module="desktop",
                component="renderer",
                operation=name,
            )
            with bind_observation(context):
                if name == "user.presented" and "presentation" in payload:
                    captured = capture_payload("user.presented", payload.get("presentation"), attributes=attributes)
                    attributes = {**attributes, "payload_id": captured.payload_id, "capture_state": captured.capture_state}
                record = ObservationRecord(
                    record_type="event", name=name, context=context, event_id=event_id or "",
                    attributes=attributes, status="recorded", kind="client",
                )
                if recorder.accept(record):
                    accepted_count += 1
        return _json_response({"ok": True, "accepted": accepted_count, "duplicates": duplicate_count}, status_code=202)

    @router.get("/api/tracing/traces/{trace_id}/export")
    async def export_trace(request: Request, trace_id: str, include_payloads: bool = False, format: str = "json") -> Any:
        account, denied = _trace_access(request, crew)
        if denied is not None:
            return denied
        if not _valid_trace_id(trace_id) or format not in _EXPORT_FORMATS:
            return _json_response({"ok": False, "error": "导出参数无效"}, status_code=400)
        store = _store(crew)
        bundle = _trace_bundle(store, account.owner_account_id, trace_id, include_payloads=include_payloads) if store else None
        if bundle is None:
            return _json_response({"ok": False, "error": "trace 不存在"}, status_code=404)
        if format == "json":
            return _json_response(bundle)
        if format == "csv":
            return Response(_csv_bytes(bundle), media_type="text/csv; charset=utf-8")
        return StreamingResponse(
            _iter_trace_jsonl(bundle, trace_id=trace_id, owner=account.owner_account_id),
            media_type="application/x-ndjson",
        )

    @router.post("/api/tracing/exports")
    async def create_export(request: Request, payload: dict[str, Any]) -> JSONResponse:
        account, denied = _trace_access(request, crew)
        if denied is not None:
            return denied
        fmt = str(payload.get("format") or "jsonl").lower()
        if fmt not in _EXPORT_FORMATS:
            return _json_response({"ok": False, "error": "不支持的导出格式"}, status_code=400)
        store = _store(crew)
        if store is None:
            return _json_response({"ok": False, "error": "追踪存储不可用"}, status_code=503)
        filter_payload = payload.get("filter") if isinstance(payload.get("filter"), dict) else {}
        job_id = secrets.token_hex(16)
        export_dir = store.path.parent / "exports"
        export_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = export_dir / f"{job_id}.{fmt}"
        now_us = time.time_ns() // 1_000
        snapshot_ingest_seq = int(store.status(owner_account_id=account.owner_account_id).get("latest_ingest_seq") or 0)
        expires_at_us = now_us + 3_600_000_000
        job = {
            "export_id": job_id,
            "owner_scope": account.owner_account_id,
            "status": "running",
            "format": fmt,
            "artifact_path": str(path),
            "count": 0,
            "partial": False,
            "created_at_us": now_us,
            "updated_at_us": now_us,
            "expires_at_us": expires_at_us,
            "snapshot_ingest_seq": snapshot_ingest_seq,
            "snapshot_started_at_us": now_us,
            "filter": filter_payload,
            "include_payloads": bool(payload.get("include_payloads")),
            "process_instance_id": str(getattr(_recorder(crew), "process_instance_id", "")),
        }
        try:
            persisted = store.create_export_job(job)
        except Exception as exc:  # noqa: BLE001 - a failed diagnostic job is explicit
            return _json_response({"ok": False, "error": f"导出任务创建失败: {exc}"}, status_code=503)
        stop_event = threading.Event()

        async def _finish() -> None:
            try:
                result = await asyncio.to_thread(
                    _write_export_file,
                    store,
                    owner=account.owner_account_id,
                    path=path,
                    fmt=fmt,
                    filter_payload=filter_payload,
                    include_payloads=bool(payload.get("include_payloads")),
                    stop_event=stop_event,
                    snapshot_ingest_seq=snapshot_ingest_seq,
                    snapshot_started_at_us=now_us,
                )
                store.update_export_job(
                    owner_account_id=account.owner_account_id,
                    export_id=job_id,
                    **result,
                    status="partial" if result.get("partial") else "completed",
                    artifact_path=str(path),
                )
            except _ExportCancelled:
                store.update_export_job(
                    owner_account_id=account.owner_account_id,
                    export_id=job_id,
                    status="cancelled", partial=True, partial_reason="cancelled",
                )
                path.unlink(missing_ok=True)
            except asyncio.CancelledError:
                store.update_export_job(
                    owner_account_id=account.owner_account_id,
                    export_id=job_id,
                    status="cancelled", partial=True, partial_reason="cancelled",
                )
                path.unlink(missing_ok=True)
            except Exception as exc:  # noqa: BLE001 - expose bounded job status
                store.update_export_job(
                    owner_account_id=account.owner_account_id,
                    export_id=job_id,
                    status="failed", error=f"{type(exc).__name__}: {exc}"[:500],
                )
                path.unlink(missing_ok=True)
            finally:
                export_runtime.pop(job_id, None)

        task = asyncio.create_task(_finish())
        export_runtime[job_id] = (stop_event, task)
        return _json_response({"ok": True, **_public_export_job(persisted)}, status_code=202)

    @router.get("/api/tracing/exports/{export_id}")
    async def get_export(request: Request, export_id: str) -> JSONResponse:
        account, denied = _trace_access(request, crew)
        if denied is not None:
            return denied
        store = _store(crew)
        job = store.get_export_job(owner_account_id=account.owner_account_id, export_id=export_id) if store else None
        if job is None:
            return _json_response({"ok": False, "error": "导出不存在"}, status_code=404)
        if int(job.get("expires_at_us") or 0) <= time.time_ns() // 1_000 and job.get("status") not in {"expired", "cancelled"}:
            artifact = _export_artifact_path(store, job)
            job = store.update_export_job(
                owner_account_id=account.owner_account_id,
                export_id=export_id,
                status="expired",
                artifact_path="",
            ) or job
            if artifact is not None:
                artifact.unlink(missing_ok=True)
        return _json_response(_public_export_job(job))

    @router.get("/api/tracing/exports/{export_id}/download")
    async def download_export(request: Request, export_id: str) -> Any:
        account, denied = _trace_access(request, crew)
        if denied is not None:
            return denied
        store = _store(crew)
        job = store.get_export_job(owner_account_id=account.owner_account_id, export_id=export_id) if store else None
        if job is None:
            return _json_response({"ok": False, "error": "导出不存在"}, status_code=404)
        if int(job.get("expires_at_us") or 0) <= time.time_ns() // 1_000:
            artifact = _export_artifact_path(store, job)
            store.update_export_job(
                owner_account_id=account.owner_account_id,
                export_id=export_id,
                status="expired",
                artifact_path="",
            )
            if artifact is not None:
                artifact.unlink(missing_ok=True)
            return _json_response({"ok": False, "status": "expired", "error": "导出已过期"}, status_code=410)
        if job.get("status") not in {"completed", "partial"}:
            return _json_response(_public_export_job(job), status_code=409)
        path = _export_artifact_path(store, job)
        if path is None or not path.is_file():
            return _json_response({"ok": False, "error": "导出已过期"}, status_code=410)
        return FileResponse(path, filename=path.name, media_type="application/octet-stream")

    @router.post("/api/tracing/exports/{export_id}/cancel")
    async def cancel_export(request: Request, export_id: str) -> JSONResponse:
        account, denied = _trace_access(request, crew)
        if denied is not None:
            return denied
        store = _store(crew)
        job = store.get_export_job(owner_account_id=account.owner_account_id, export_id=export_id) if store else None
        if job is None:
            return _json_response({"ok": False, "error": "导出不存在"}, status_code=404)
        path = _export_artifact_path(store, job)
        runtime = export_runtime.get(export_id)
        if runtime is not None:
            stop_event, _task = runtime
            stop_event.set()
        store.update_export_job(
            owner_account_id=account.owner_account_id,
            export_id=export_id,
            status="cancelled", partial=True, partial_reason="cancelled",
        )
        if path is not None:
            path.unlink(missing_ok=True)
        return _json_response({"ok": True, "status": "cancelled"})

    @router.delete("/api/tracing/exports/{export_id}")
    async def delete_export(request: Request, export_id: str) -> JSONResponse:
        # Compatibility alias; explicit cancellation keeps the durable status.
        return await cancel_export(request, export_id)

    return router


__all__ = ["create_tracing_router"]
