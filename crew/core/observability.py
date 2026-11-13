"""Small, dependency-free observation SDK used by Crew boundaries.

The core package intentionally knows nothing about SQLite, FastAPI or the
desktop client.  Producers create immutable records and hand them to the
currently installed sink.  A sink may persist, export or discard records;
failure in a sink is contained so diagnostics never become a business
dependency.
"""

from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import secrets
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


def _now_us() -> int:
    return time.time_ns() // 1_000


def _trace_id() -> str:
    return secrets.token_hex(16)


def _span_id() -> str:
    # token_hex(8) is a non-zero 64-bit value with negligible retry cost.
    value = secrets.token_hex(8)
    return value if int(value, 16) else "0000000000000001"


def _event_id() -> str:
    return uuid.uuid4().hex


SYSTEM_OWNER_ACCOUNT_ID = "system"


@dataclass(frozen=True, slots=True)
class ObservationContext:
    """The minimal context propagated across an execution boundary."""

    trace_id: str = ""
    span_id: str = ""
    parent_span_id: str = ""
    owner_account_id: str = SYSTEM_OWNER_ACCOUNT_ID
    workspace_id: str = "default"
    session_id: str = ""
    execution_session_id: str = ""
    request_id: str = ""
    message_id: str = ""
    session_event_seq: int | None = None
    task_id: str = ""
    parent_task_id: str = ""
    agent_id: str = ""
    tool_call_id: str = ""
    source: str = "backend"
    process_instance_id: str = ""
    origin: str = ""
    module: str = ""
    component: str = ""
    operation: str = ""
    feature_id: str = ""
    links: tuple[dict[str, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def child(self, *, span_id: str, operation: str = "", **updates: Any) -> "ObservationContext":
        """Return a child context without mutating the parent context."""
        values = self.as_dict()
        values.update(updates)
        values["parent_span_id"] = self.span_id
        values["span_id"] = span_id
        if operation:
            values["operation"] = operation
        values["links"] = tuple(values.get("links") or ())
        return ObservationContext(**values)


@dataclass(frozen=True, slots=True)
class ObservationRecord:
    """Immutable record accepted by a sink."""

    record_type: str
    name: str
    occurred_at_us: int = field(default_factory=_now_us)
    context: ObservationContext = field(default_factory=ObservationContext)
    event_id: str = field(default_factory=_event_id)
    attributes: Mapping[str, Any] = field(default_factory=dict)
    status: str = ""
    kind: str = "internal"
    started_at_us: int | None = None
    ended_at_us: int | None = None
    duration_ms: float | None = None
    error_type: str = ""
    error_message: str = ""

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "record_type": self.record_type,
            "name": self.name,
            "occurred_at_us": self.occurred_at_us,
            "event_id": self.event_id,
            "status": self.status,
            "kind": self.kind,
            "attributes": dict(self.attributes),
            "context": self.context.as_dict(),
        }
        if self.started_at_us is not None:
            result["started_at_us"] = self.started_at_us
        if self.ended_at_us is not None:
            result["ended_at_us"] = self.ended_at_us
        if self.duration_ms is not None:
            result["duration_ms"] = self.duration_ms
        if self.error_type:
            result["error_type"] = self.error_type
        if self.error_message:
            result["error_message"] = self.error_message
        return result


@dataclass(frozen=True, slots=True)
class PayloadCapture:
    """Result returned by :func:`capture_payload`."""

    payload_id: str = ""
    capture_state: str = "disabled"
    redacted_paths: tuple[str, ...] = ()
    truncated_reason: str = ""
    observed_size: int | None = None
    stored_size: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@runtime_checkable
class ObservationSink(Protocol):
    """Sink protocol implemented by state-level recorders."""

    def accept(self, record: ObservationRecord) -> bool | None:
        ...

    def capture_payload(
        self,
        stage: str,
        value: Any,
        *,
        policy: str | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> PayloadCapture:
        ...

    def flush(self, timeout: float | None = None) -> bool:
        ...

    def close(self, timeout: float | None = None) -> bool:
        ...


class NoopSink:
    """A safe default sink used before the application is assembled."""

    def accept(self, _record: ObservationRecord) -> bool:
        return False

    def capture_payload(self, *_args: Any, **_kwargs: Any) -> PayloadCapture:
        return PayloadCapture()

    def flush(self, _timeout: float | None = None) -> bool:
        return True

    def close(self, _timeout: float | None = None) -> bool:
        return True


_NOOP = NoopSink()
_GLOBAL_SINK: ObservationSink = _NOOP
_SINK: contextvars.ContextVar[ObservationSink | None] = contextvars.ContextVar(
    "crew_observation_sink", default=None
)
_CONTEXT: contextvars.ContextVar[ObservationContext] = contextvars.ContextVar(
    "crew_observation_context", default=ObservationContext()
)


def _default_context() -> ObservationContext:
    """Build a root context from existing run context without a hard import."""
    try:
        from crew.core import runctx

        request_id = str(runctx.current_request_id.get() or "")
        session_id = str(runctx.current_session_id.get() or "")
        # runctx's compatibility owner defaults to ``local``.  Without a
        # request/session correlation that value is not proof of an
        # authenticated business owner, so unbound startup/worker records use
        # the dedicated system scope.
        owner = (
            str(runctx.current_owner_account_id.get() or SYSTEM_OWNER_ACCOUNT_ID)
            if request_id or session_id
            else SYSTEM_OWNER_ACCOUNT_ID
        )
        values = {
            "owner_account_id": owner,
            "workspace_id": str(runctx.current_workspace_id.get() or "default"),
            "session_id": session_id,
            "request_id": request_id,
            "tool_call_id": str(runctx.current_tool_call_id.get() or ""),
            "parent_task_id": str(runctx.current_parent_task_id.get() or ""),
            "agent_id": str(runctx.current_agent_id.get() or ""),
            "source": "backend",
        }
    except Exception:  # pragma: no cover - defensive for early interpreter startup
        values = {}
    return ObservationContext(**values)


def get_observation_context() -> ObservationContext:
    """Return the current context, lazily hydrating business IDs for a root."""
    current = _CONTEXT.get()
    if current.trace_id:
        return current
    return _default_context()


def install_sink(sink: ObservationSink | None) -> None:
    """Install the process-wide sink used by all execution contexts.

    The process sink is deliberately not copied into the current
    :class:`~contextvars.Context`.  A task may be created before application
    startup or before a later sink replacement; keeping the process default
    out of a context variable ensures that those tasks still reach the active
    sink.  Temporary, request-scoped overrides belong to :func:`bind_sink`.
    """
    global _GLOBAL_SINK
    _GLOBAL_SINK = sink or _NOOP


def remove_sink(sink: ObservationSink | None) -> None:
    """Remove a process sink only when it is still the active sink.

    App instances can overlap during tests, reloads, and gateway handoff.  A
    retiring instance must not clear the recorder installed by its successor.
    """
    global _GLOBAL_SINK
    if sink is None or _GLOBAL_SINK is sink:
        _GLOBAL_SINK = _NOOP


def reset_sink(token: contextvars.Token[ObservationSink | None] | None) -> None:
    """Reset a legacy temporary sink token, if one was supplied.

    ``install_sink`` no longer returns a token, but accepting ``None`` keeps
    lifecycle cleanup call sites harmless while preserving the old helper for
    callers that still pass a token created by ``ContextVar.set``.
    """
    if token is not None:
        _SINK.reset(token)


@contextlib.contextmanager
def bind_sink(sink: ObservationSink | None) -> Iterator[None]:
    token = _SINK.set(sink or _GLOBAL_SINK)
    try:
        yield
    finally:
        _SINK.reset(token)


def _new_root_context(**updates: Any) -> ObservationContext:
    base = get_observation_context()
    values = base.as_dict()
    values.update(updates)
    values["trace_id"] = values.get("trace_id") or _trace_id()
    # A bound trace is a propagation scope, not an implicit running span.  The
    # first explicit ``span()`` therefore becomes the root span and has no
    # parent, which lets stores derive the trace's terminal status reliably.
    values["span_id"] = values.get("span_id") or ""
    values["parent_span_id"] = ""
    values["links"] = tuple(values.get("links") or ())
    return ObservationContext(**values)


@contextlib.contextmanager
def bind_observation(
    context: ObservationContext | None = None,
    *,
    trace_id: str = "",
    source: str = "",
    **updates: Any,
) -> Iterator[ObservationContext]:
    """Bind a root or supplied context and always reset it on exit."""
    base = context or _new_root_context(trace_id=trace_id, source=source, **updates)
    token = _CONTEXT.set(base)
    try:
        yield base
    finally:
        _CONTEXT.reset(token)


def capture_context() -> ObservationContext:
    """Capture an immutable context for queue/thread propagation."""
    return get_observation_context()


@contextlib.contextmanager
def attach_context(context: ObservationContext | Mapping[str, Any] | None) -> Iterator[ObservationContext]:
    """Attach a previously captured context for one worker scope."""
    if context is None:
        value = get_observation_context()
    elif isinstance(context, ObservationContext):
        value = context
    else:
        value = ObservationContext(**dict(context))
    token = _CONTEXT.set(value)
    try:
        yield value
    finally:
        _CONTEXT.reset(token)


def _emit(record: ObservationRecord) -> bool:
    try:
        sink = _SINK.get() or _GLOBAL_SINK
        return bool(sink.accept(record))
    except Exception:
        # Diagnostics must never alter the producer's outcome.
        return False


def ensure_trace_context(**updates: Any) -> ObservationContext:
    """Create and bind a trace when the current operation has no trace."""
    current = get_observation_context()
    if current.trace_id:
        return current
    value = _new_root_context(**updates)
    _CONTEXT.set(value)
    return value


@contextlib.contextmanager
def span(
    name: str,
    *,
    kind: str = "internal",
    attributes: Mapping[str, Any] | None = None,
    context: ObservationContext | None = None,
    **context_updates: Any,
) -> Iterator[ObservationContext]:
    """Record a span while preserving the caller's context and exceptions."""
    parent = context or get_observation_context()
    if not parent.trace_id:
        parent = _new_root_context(**context_updates)
    elif context_updates:
        values = parent.as_dict()
        values.update(context_updates)
        values["links"] = tuple(values.get("links") or ())
        parent = ObservationContext(**values)
    child = parent.child(span_id=_span_id(), operation=str(context_updates.get("operation") or parent.operation))
    started_wall = _now_us()
    started_mono = time.perf_counter_ns()
    token = _CONTEXT.set(child)
    _emit(
        ObservationRecord(
            record_type="span.start",
            name=str(name),
            occurred_at_us=started_wall,
            context=child,
            attributes=dict(attributes or {}),
            kind=str(kind),
            status="running",
            started_at_us=started_wall,
        )
    )
    try:
        yield child
    except BaseException as exc:
        ended_wall = _now_us()
        _emit(
            ObservationRecord(
                record_type="span.end",
                name=str(name),
                occurred_at_us=ended_wall,
                context=child,
                attributes=dict(attributes or {}),
                kind=str(kind),
                status="cancelled" if isinstance(exc, BaseException) and exc.__class__.__name__ == "CancelledError" else "failed",
                started_at_us=started_wall,
                ended_at_us=ended_wall,
                duration_ms=(time.perf_counter_ns() - started_mono) / 1_000_000,
                error_type=type(exc).__name__,
                error_message=str(exc)[:2_000],
            )
        )
        raise
    else:
        ended_wall = _now_us()
        _emit(
            ObservationRecord(
                record_type="span.end",
                name=str(name),
                occurred_at_us=ended_wall,
                context=child,
                attributes=dict(attributes or {}),
                kind=str(kind),
                status="succeeded",
                started_at_us=started_wall,
                ended_at_us=ended_wall,
                duration_ms=(time.perf_counter_ns() - started_mono) / 1_000_000,
            )
        )
    finally:
        _CONTEXT.reset(token)


def event(
    name: str,
    *,
    attributes: Mapping[str, Any] | None = None,
    level: str = "INFO",
    context: ObservationContext | None = None,
    **context_updates: Any,
) -> bool:
    """Record a point-in-time event; it never creates a fake duration."""
    current = context or get_observation_context()
    if context_updates:
        values = current.as_dict()
        values.update(context_updates)
        values["links"] = tuple(values.get("links") or ())
        current = ObservationContext(**values)
    attrs = dict(attributes or {})
    attrs.setdefault("level", str(level).upper())
    return _emit(
        ObservationRecord(
            record_type="event",
            name=str(name),
            context=current,
            attributes=attrs,
            status="recorded",
        )
    )


def capture_payload(
    stage: str,
    value: Any,
    *,
    policy: str | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> PayloadCapture:
    """Capture a payload through the installed state-level policy."""
    sink = _SINK.get() or _GLOBAL_SINK
    try:
        return sink.capture_payload(stage, value, policy=policy, attributes=attributes)
    except Exception:
        return PayloadCapture(capture_state="dropped", truncated_reason="sink_error")


def flush(timeout: float | None = None) -> bool:
    try:
        return bool((_SINK.get() or _GLOBAL_SINK).flush(timeout))
    except Exception:
        return False


def close(timeout: float | None = None) -> bool:
    try:
        return bool((_SINK.get() or _GLOBAL_SINK).close(timeout))
    except Exception:
        return False


__all__ = [
    "SYSTEM_OWNER_ACCOUNT_ID",
    "ObservationContext",
    "ObservationRecord",
    "ObservationSink",
    "PayloadCapture",
    "NoopSink",
    "attach_context",
    "bind_observation",
    "bind_sink",
    "capture_context",
    "capture_payload",
    "close",
    "ensure_trace_context",
    "event",
    "flush",
    "get_observation_context",
    "install_sink",
    "remove_sink",
    "reset_sink",
    "span",
]
