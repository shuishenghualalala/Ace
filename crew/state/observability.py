"""Bounded, best-effort observation recorder and redaction policy."""

from __future__ import annotations

import json
import hashlib
import logging
import queue
import re
import secrets
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Mapping

from crew.core.observability import (
    SYSTEM_OWNER_ACCOUNT_ID,
    ObservationContext,
    ObservationRecord,
    ObservationSink,
    PayloadCapture,
    install_sink,
)
from crew.state.trace_store import TraceStore


_SECRET_KEY_RE = re.compile(
    r"(?:api[_-]?key|authorization|cookie|password|passwd|secret|token|credential|private[_-]?key|signed[_-]?url)",
    re.IGNORECASE,
)
_SECRET_TEXT_RE = re.compile(
    r"(?i)\b(?:bearer\s+|basic\s+)[A-Za-z0-9._~+/=-]{8,}"
    r"|\b(?:sk|key|token|secret)[_-][A-Za-z0-9_-]{8,}"
    r"|\b(?:sk|xox[baprs]|gh[pousr]|github_pat)[-_][A-Za-z0-9_-]{8,}"
    r"|\bAKIA[0-9A-Z]{12,}\b"
    r"|(?:x-amz-signature|signature|sig|token|access_token)=([^&\s]+)"
)
_BINARY_TYPES = (bytes, bytearray, memoryview)


@dataclass(frozen=True, slots=True)
class ObservationPolicy:
    """Collection and storage budgets; all limits are enforced before enqueue."""

    enabled: bool = True
    capture_profile: str = "content_redacted"
    max_payload_bytes: int = 1 * 1024 * 1024
    max_trace_bytes: int = 20 * 1024 * 1024
    max_queue_items: int = 10_000
    max_queue_bytes: int = 32 * 1024 * 1024
    max_depth: int = 12
    max_nodes: int = 20_000
    max_string_chars: int = 100_000
    max_spans_per_trace: int = 10_000
    max_events_per_trace: int = 50_000
    retention_days: int = 7
    max_disk_bytes: int = 1024 * 1024 * 1024
    capture_profile_source: str = "default"

    @classmethod
    def from_config(cls, config: Any) -> "ObservationPolicy":
        raw = getattr(config, "observability", None)
        if isinstance(raw, ObservationPolicy):
            return raw
        if not isinstance(raw, Mapping):
            return cls(
                enabled=bool(getattr(config, "observability_enabled", True)),
                capture_profile_source="legacy_llm_trace" if bool(getattr(config, "llm_trace", False)) else "default",
            )
        values: dict[str, Any] = {}
        for field_info in cls.__dataclass_fields__.values():
            if field_info.name in raw:
                values[field_info.name] = raw[field_info.name]
        if "enabled" not in values:
            values["enabled"] = bool(getattr(config, "observability_enabled", True))
        if "capture_profile_source" not in values:
            values["capture_profile_source"] = (
                "observability.capture_profile"
                if "capture_profile" in raw
                else "legacy_llm_trace"
                if bool(getattr(config, "llm_trace", False))
                else "default"
            )
        return cls(**values)


@dataclass(frozen=True, slots=True)
class _Sanitized:
    value: Any
    redacted_paths: tuple[str, ...] = ()
    truncated_reason: str = ""
    observed_size: int | None = None


def sanitize_value(
    value: Any,
    *,
    policy: ObservationPolicy,
    path: str = "$",
    depth: int = 0,
    _nodes: list[int] | None = None,
    _seen: set[int] | None = None,
) -> _Sanitized:
    """Bounded, deterministic redaction that never walks an object indefinitely."""
    nodes = _nodes if _nodes is not None else [0]
    seen = _seen if _seen is not None else set()
    nodes[0] += 1
    if nodes[0] > max(1, policy.max_nodes):
        return _Sanitized("<truncated>", truncated_reason="node_limit")
    if depth > max(0, policy.max_depth):
        return _Sanitized("<truncated>", truncated_reason="depth_limit")
    if isinstance(value, _BINARY_TYPES):
        return _Sanitized(
            {"type": "binary", "redacted": True, "bytes": len(value)},
            redacted_paths=(path,),
            observed_size=len(value),
        )
    if value is None or isinstance(value, (bool, int, float)):
        return _Sanitized(value)
    if isinstance(value, str):
        text = _SECRET_TEXT_RE.sub("<secret_redacted>", value)
        if len(text) > policy.max_string_chars:
            return _Sanitized(
                text[: policy.max_string_chars],
                truncated_reason="string_limit",
                observed_size=len(value.encode("utf-8", errors="replace")),
            )
        redacted = (path,) if text != value else ()
        return _Sanitized(text, redacted_paths=redacted, observed_size=len(value.encode("utf-8", errors="replace")))
    identity = id(value)
    if identity in seen:
        return _Sanitized("<cycle>", truncated_reason="cycle")
    seen.add(identity)
    try:
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            redacted: list[str] = []
            reason = ""
            observed = 0
            for key, item in value.items():
                key_text = _safe_mapping_key(key)
                child_path = f"{path}.{key_text}"
                if _SECRET_KEY_RE.search(key_text):
                    result[key_text] = "<secret_redacted>"
                    redacted.append(child_path)
                    continue
                child = sanitize_value(
                    item,
                    policy=policy,
                    path=child_path,
                    depth=depth + 1,
                    _nodes=nodes,
                    _seen=seen,
                )
                result[key_text] = child.value
                redacted.extend(child.redacted_paths)
                reason = reason or child.truncated_reason
                if child.observed_size is not None:
                    observed += child.observed_size
            return _Sanitized(result, tuple(redacted), reason, observed)
        if isinstance(value, (list, tuple, set, frozenset)):
            result_list: list[Any] = []
            redacted = []
            reason = ""
            observed = 0
            for index, item in enumerate(value):
                child = sanitize_value(
                    item,
                    policy=policy,
                    path=f"{path}[{index}]",
                    depth=depth + 1,
                    _nodes=nodes,
                    _seen=seen,
                )
                result_list.append(child.value)
                redacted.extend(child.redacted_paths)
                reason = reason or child.truncated_reason
                if child.observed_size is not None:
                    observed += child.observed_size
            return _Sanitized(result_list, tuple(redacted), reason, observed)
        # Avoid invoking arbitrary object methods or getters in a diagnostic path.
        # The type name is safe metadata; the object's repr/str may execute user
        # code or accidentally expose secrets.
        return _Sanitized({"type": type(value).__name__, "redacted": True}, redacted_paths=(path,))
    finally:
        seen.discard(identity)


def _encoded_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError, OverflowError):
        return 0


def _safe_mapping_key(key: Any) -> str:
    """Render mapping keys without invoking arbitrary user ``__str__``."""
    if type(key) is str:  # noqa: E721 - exact type avoids str subclasses
        return key[:200]
    if type(key) is bool:  # noqa: E721
        return "true" if key else "false"
    if type(key) is int:  # noqa: E721
        return f"<int:{key}>"
    if type(key) is float:  # noqa: E721
        return f"<float:{key!r}>"
    return f"<{type(key).__name__}>"


@dataclass(frozen=True, slots=True)
class _PendingPayload:
    owner_account_id: str
    payload_id: str
    trace_id: str
    span_id: str
    stage: str
    capture_state: str
    content: Any
    redacted_paths: tuple[str, ...]
    truncated_reason: str
    observed_size: int | None
    stored_size: int | None
    attributes: dict[str, Any]


class ObservationRecorder(ObservationSink):
    """A bounded producer queue plus a single SQLite writer thread."""

    def __init__(
        self,
        store: TraceStore,
        *,
        policy: ObservationPolicy | None = None,
        auto_start: bool = True,
        flush_interval: float = 0.1,
        batch_size: int = 200,
    ) -> None:
        self.store = store
        self.policy = policy or ObservationPolicy()
        self.flush_interval = max(0.01, float(flush_interval))
        self.batch_size = max(1, min(int(batch_size), 1_000))
        self.process_instance_id = secrets.token_hex(16)
        self._queue: queue.Queue[ObservationRecord | _PendingPayload | object] = queue.Queue(
            maxsize=max(1, self.policy.max_queue_items)
        )
        self._queue_bytes = 0
        self._queue_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = object()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._started = False
        self._flush_waiters: list[threading.Event] = []
        self.dropped_events = 0
        self.dropped_payload_bytes = 0
        self.writer_errors = 0
        self.last_error = ""
        self.accepted_records = 0
        self._trace_bytes: dict[str, int] = {}
        self._trace_counts: dict[str, list[int]] = {}
        self._trace_budget_cap = 4_096
        if auto_start and self.policy.enabled:
            self.start()

    def start(self) -> None:
        with self._state_lock:
            if self._started or self._closed:
                return
            self._started = True
            self._thread = threading.Thread(
                target=self._writer_loop,
                name="crew-observation-writer",
                daemon=True,
            )
            self._thread.start()

    def install(self) -> None:
        """Install this recorder as the process-wide sink."""
        install_sink(self)

    def accept(self, record: ObservationRecord) -> bool:
        if not self.policy.enabled or self._closed:
            return False
        self.start()
        raw_attributes = dict(record.attributes)
        if self.policy.capture_profile == "metadata":
            raw_attributes = self._metadata_attributes(raw_attributes)
        sanitized = sanitize_value(raw_attributes, policy=self.policy)
        attrs = sanitized.value if isinstance(sanitized.value, dict) else {"value": sanitized.value}
        if sanitized.redacted_paths:
            attrs = dict(attrs)
            attrs["redacted_paths"] = list(sanitized.redacted_paths[:1_000])
        normalized = replace(
            record,
            attributes=attrs,
            context=replace(
                record.context,
                owner_account_id=(
                    record.context.owner_account_id
                    if isinstance(record.context.owner_account_id, str) and record.context.owner_account_id.strip()
                    else SYSTEM_OWNER_ACCOUNT_ID
                ),
                process_instance_id=record.context.process_instance_id or self.process_instance_id,
            ),
        )
        trace_id = normalized.context.trace_id
        size = _encoded_size(normalized.as_dict())
        with self._queue_lock:
            self._remember_trace(trace_id)
            counts = self._trace_counts.setdefault(trace_id, [0, 0])
            if normalized.record_type.startswith("span"):
                if counts[0] >= self.policy.max_spans_per_trace:
                    self.dropped_events += 1
                    return False
                counts[0] += 1
            elif normalized.record_type == "event":
                if counts[1] >= self.policy.max_events_per_trace:
                    self.dropped_events += 1
                    return False
                counts[1] += 1
            trace_total = self._trace_bytes.get(trace_id, 0) + size
            if trace_total > self.policy.max_trace_bytes:
                self.dropped_events += 1
                return False
            if self._queue.qsize() >= self.policy.max_queue_items or self._queue_bytes + size > self.policy.max_queue_bytes:
                self.dropped_events += 1
                return False
            self._trace_bytes[trace_id] = trace_total
            self._queue_bytes += size
            try:
                self._queue.put_nowait(normalized)
                self.accepted_records += 1
            except queue.Full:
                self._queue_bytes -= size
                self.dropped_events += 1
                return False
        self._wake.set()
        return True

    def _remember_trace(self, trace_id: str) -> None:
        """Keep accounting bounded even when a long-lived process sees many traces."""
        if trace_id in self._trace_bytes:
            return
        if len(self._trace_bytes) >= self._trace_budget_cap:
            oldest = next(iter(self._trace_bytes), None)
            if oldest is not None:
                self._trace_bytes.pop(oldest, None)
                self._trace_counts.pop(oldest, None)
        self._trace_bytes[trace_id] = 0

    def capture_payload(
        self,
        stage: str,
        value: Any,
        *,
        policy: str | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> PayloadCapture:
        current = self._current_context()
        if not self.policy.enabled or self._closed or not current.trace_id:
            return PayloadCapture(capture_state="disabled", truncated_reason="no_trace")
        if hasattr(self.store, "payload_capture_allowed") and not self.store.payload_capture_allowed():
            self.dropped_payload_bytes += max(0, _encoded_size(value))
            return PayloadCapture(capture_state="disabled", truncated_reason="disk_budget")
        if policy == "metadata" or self.policy.capture_profile == "metadata":
            return PayloadCapture(capture_state="disabled", truncated_reason="metadata_profile")
        raw_attributes = dict(attributes or {})
        raw_attributes.setdefault("capture_policy", str(policy or self.policy.capture_profile))
        raw_attributes.setdefault("capture_policy_version", 1)
        cleaned_attributes = sanitize_value(raw_attributes, policy=self.policy)
        payload_attributes = (
            dict(cleaned_attributes.value)
            if isinstance(cleaned_attributes.value, dict)
            else {"value": cleaned_attributes.value}
        )
        if cleaned_attributes.redacted_paths:
            payload_attributes["redacted_paths"] = list(cleaned_attributes.redacted_paths[:1_000])
        cleaned = sanitize_value(value, policy=self.policy)
        observed = cleaned.observed_size
        stored = _encoded_size(cleaned.value)
        state = "redacted" if cleaned.redacted_paths else "complete"
        reason = cleaned.truncated_reason
        if stored > self.policy.max_payload_bytes:
            preview = sanitize_value(
                {"preview": cleaned.value},
                policy=replace(self.policy, max_string_chars=min(self.policy.max_string_chars, 8_192), max_nodes=2_000),
            ).value
            cleaned = _Sanitized(preview, cleaned.redacted_paths, "payload_limit", observed)
            stored = _encoded_size(cleaned.value)
            state = "truncated"
            reason = "payload_limit"
        payload_id = secrets.token_hex(16)
        pending = _PendingPayload(
            owner_account_id=current.owner_account_id,
            payload_id=payload_id,
            trace_id=current.trace_id,
            span_id=current.span_id,
            stage=str(stage),
            capture_state=state,
            content=cleaned.value,
            redacted_paths=tuple(cleaned.redacted_paths),
            truncated_reason=reason,
            observed_size=observed,
            stored_size=stored,
            attributes=payload_attributes,
        )
        size = max(stored, 128)
        with self._queue_lock:
            if self._queue.qsize() >= self.policy.max_queue_items or self._queue_bytes + size > self.policy.max_queue_bytes:
                self.dropped_payload_bytes += size
                return PayloadCapture(
                    payload_id=payload_id,
                    capture_state="dropped",
                    redacted_paths=tuple(cleaned.redacted_paths),
                    truncated_reason="queue_full",
                    observed_size=observed,
                    stored_size=0,
                )
            self._queue_bytes += size
            try:
                self._queue.put_nowait(pending)
            except queue.Full:
                self._queue_bytes -= size
                self.dropped_payload_bytes += size
                return PayloadCapture(payload_id=payload_id, capture_state="dropped", truncated_reason="queue_full")
        self._wake.set()
        return PayloadCapture(
            payload_id=payload_id,
            capture_state=state,
            redacted_paths=tuple(cleaned.redacted_paths),
            truncated_reason=reason,
            observed_size=observed,
            stored_size=stored,
        )

    @staticmethod
    def _metadata_attributes(attributes: Mapping[str, Any]) -> dict[str, Any]:
        """Keep only bounded facts when the metadata profile is selected."""
        result: dict[str, Any] = {}
        for key in ("level", "logger", "event_type", "capture_layer", "capture_policy_version"):
            value = attributes.get(key)
            if isinstance(value, (str, int, float, bool)):
                result[key] = value
        for key in ("message", "content", "text", "prompt", "response"):
            value = attributes.get(key)
            if value is not None:
                text = value if type(value) is str else "<non_text>"
                result[f"{key}_length"] = len(text)
                result[f"{key}_sha256"] = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
        return result

    def _current_context(self) -> ObservationContext:
        from crew.core.observability import get_observation_context

        return get_observation_context()

    def _writer_loop(self) -> None:
        batch: list[ObservationRecord] = []
        payloads: list[_PendingPayload] = []
        while True:
            try:
                item = self._queue.get(timeout=self.flush_interval)
            except queue.Empty:
                if batch or payloads:
                    self._write(batch, payloads)
                    batch.clear()
                    payloads.clear()
                if self._closed and self._queue.empty():
                    return
                continue
            if item is self._stop:
                if batch or payloads:
                    self._write(batch, payloads)
                    batch.clear()
                    payloads.clear()
                self._queue.task_done()
                return
            if isinstance(item, _PendingPayload):
                payloads.append(item)
            else:
                batch.append(item)
            if len(batch) + len(payloads) >= self.batch_size:
                self._write(batch, payloads)
                batch.clear()
                payloads.clear()

    def _write(self, records: list[ObservationRecord], payloads: list[_PendingPayload]) -> None:
        total_bytes = sum(_encoded_size(item.as_dict()) for item in records)
        total_bytes += sum(max(item.stored_size or 0, 128) for item in payloads)
        try:
            if records:
                self.store.write_batch(records)
            if payloads:
                self.store.write_payload_batch([
                    {
                        "owner_account_id": item.owner_account_id,
                        "payload_id": item.payload_id,
                        "trace_id": item.trace_id,
                        "span_id": item.span_id,
                        "stage": item.stage,
                        "capture_state": item.capture_state,
                        "content": item.content,
                        "redacted_paths": item.redacted_paths,
                        "truncated_reason": item.truncated_reason,
                        "observed_size": item.observed_size,
                        "stored_size": item.stored_size,
                        "attributes": item.attributes,
                    }
                    for item in payloads
                ])
        except Exception as exc:  # noqa: BLE001 - diagnostics degrade, business continues
            self.writer_errors += 1
            self.last_error = f"{type(exc).__name__}: {exc}"[:1_000]
        finally:
            with self._queue_lock:
                self._queue_bytes = max(0, self._queue_bytes - total_bytes)
            for _ in range(len(records) + len(payloads)):
                self._queue.task_done()
            self._wake.set()

    def flush(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
        while True:
            if self._queue.unfinished_tasks == 0:
                return True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            self._wake.wait(timeout=0.02)
            self._wake.clear()

    def close(self, timeout: float | None = None) -> bool:
        with self._state_lock:
            if self._closed:
                thread = self._thread
            else:
                self._closed = True
                thread = self._thread
                if thread is not None:
                    try:
                        self._queue.put_nowait(self._stop)
                    except queue.Full:
                        # The writer will observe _closed after draining the
                        # bounded queue and exit at its next timeout.
                        pass
        if thread is None:
            return True
        thread.join(timeout=max(0.0, float(timeout if timeout is not None else 3.0)))
        return not thread.is_alive()

    def status(self) -> dict[str, Any]:
        with self._queue_lock:
            queue_bytes = self._queue_bytes
        return {
            "enabled": bool(self.policy.enabled),
            "started": self._started,
            "closed": self._closed,
            "process_instance_id": self.process_instance_id,
            "queue_items": self._queue.qsize(),
            "queue_bytes": queue_bytes,
            "queue_capacity": self.policy.max_queue_items,
            "queue_byte_capacity": self.policy.max_queue_bytes,
            "accepted_records": self.accepted_records,
            "dropped_events": self.dropped_events,
            "dropped_payload_bytes": self.dropped_payload_bytes,
            "writer_errors": self.writer_errors,
            "last_error": self.last_error,
            "capture_profile": self.policy.capture_profile,
            "capture_profile_source": self.policy.capture_profile_source,
        }


class ObservationLogHandler(logging.Handler):
    """Bridge structured Python logging into the recorder without recursion."""

    def __init__(self, recorder: ObservationRecorder) -> None:
        super().__init__()
        self.recorder = recorder
        self._local = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._local, "active", False):
            return
        self._local.active = True
        try:
            from crew.core.observability import event, get_observation_context

            context = get_observation_context()
            # A logger emitted before a trusted request binds its owner must be
            # queryable as a process/system record, never as the ordinary local
            # account merely because runctx's compatibility default is "local".
            trusted_request = bool(context.trace_id and (context.request_id or context.session_id))
            owner = context.owner_account_id if trusted_request else SYSTEM_OWNER_ACCOUNT_ID
            event(
                "system.log",
                context=replace(
                    context,
                    owner_account_id=owner,
                    source=context.source if trusted_request else "system",
                    module=str(record.name).split(".", 1)[0],
                    component=str(record.name),
                    process_instance_id=context.process_instance_id or self.recorder.process_instance_id,
                ),
                attributes={
                    "level": record.levelname,
                    "message": self.format(record)[:4_000],
                    "logger": record.name,
                },
            )
        except Exception:
            return
        finally:
            self._local.active = False


__all__ = [
    "ObservationLogHandler",
    "ObservationPolicy",
    "ObservationRecorder",
    "sanitize_value",
]
