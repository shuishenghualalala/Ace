"""Owner/admin security alert aggregation with bounded dedup and fail-closed actions.

Alerts are derived from already-sanitized audit events and from explicit failure
reports.  The registry never persists raw URLs, arguments, or secret values: detail
text passes the shared display redactor before storage and is bounded.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable

from crew.tools.redact import redact_sensitive_display_text

log = logging.getLogger(__name__)


class SecurityAlertKind(StrEnum):
    """Alert classes used for dedup and owner/admin display."""

    ANOMALOUS_DENIALS = "anomalous_denials"
    SANDBOX_FALLBACK = "sandbox_fallback"
    MANIFEST_MISMATCH = "manifest_mismatch"
    ORPHAN_PROCESS = "orphan_process"
    UPDATE_SIGNATURE_FAILURE = "update_signature_failure"
    AUDIT_CHAIN_BREAK = "audit_chain_break"


class SecurityAlertActionDenied(RuntimeError):
    """Raised when a required human alert action cannot be completed."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(f"security alert action denied: {self.code}")


@dataclass(frozen=True)
class SecurityAlert:
    alert_id: str
    kind: str
    owner_account_id: str
    session_id: str
    task_id: str
    detail: str
    count: int
    first_seen: float
    last_seen: float
    isolated: bool
    auto_denied: bool
    resolved: bool

    def public_dict(self) -> dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "kind": self.kind,
            "owner_account_id": self.owner_account_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "detail": self.detail,
            "count": self.count,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "isolated": self.isolated,
            "auto_denied": self.auto_denied,
            "resolved": self.resolved,
        }


@dataclass
class _AlertState:
    alert_id: str
    kind: str
    owner_account_id: str
    session_id: str
    task_id: str
    fingerprint: str
    detail: str
    count: int
    first_seen: float
    last_seen: float
    isolated: bool
    auto_denied: bool
    resolved: bool

    def freeze(self, *, count: int, last_seen: float) -> SecurityAlert:
        return SecurityAlert(
            alert_id=self.alert_id,
            kind=self.kind,
            owner_account_id=self.owner_account_id,
            session_id=self.session_id,
            task_id=self.task_id,
            detail=self.detail,
            count=count,
            first_seen=self.first_seen,
            last_seen=last_seen,
            isolated=self.isolated,
            auto_denied=self.auto_denied,
            resolved=self.resolved,
        )


_MANIFEST_ERROR_CODES = frozenset(
    {
        "runtime_stale",
        "source_stale",
        "manifest_mismatch",
        "helper_integrity",
    }
)
_UPDATE_ERROR_CODES = frozenset(
    {
        "update_signature_failure",
        "update_integrity_failure",
    }
)
_ORPHAN_ERROR_CODES = frozenset(
    {
        "orphan_process",
        "process_survivor",
    }
)
_DENY_DECISIONS = frozenset({"deny", "reject"})
_READY_RUNTIME_DECISIONS = frozenset({"ready", "probe_ok"})


class SecurityAlertRegistry:
    """Bounded in-memory deduplicated alert stream plus fail-closed kill switch."""

    _MAX_ALERTS = 1024
    _MAX_DETAIL_BYTES = 512

    def __init__(
        self,
        *,
        ui_available: Callable[[], bool] | None = None,
        freeze: Callable[[str, str, str], object] | None = None,
        revoke: Callable[[str, str, str], object] | None = None,
        threshold: int = 5,
        window_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 1:
            raise ValueError("alert threshold must be a positive integer")
        if not isinstance(window_seconds, (int, float)) or window_seconds <= 0:
            raise ValueError("alert window must be positive")
        self._ui_available = ui_available or (lambda: True)
        self._freeze = freeze or (lambda *_args: None)
        self._revoke = revoke or (lambda *_args: None)
        self._threshold = threshold
        self._window_seconds = float(window_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._states: dict[tuple[str, ...], _AlertState] = {}
        self._by_id: dict[str, tuple[str, ...]] = {}

    @staticmethod
    def _key(
        kind: str,
        owner_account_id: str,
        session_id: str,
        task_id: str,
        fingerprint: str,
    ) -> tuple[str, ...]:
        return (
            str(kind),
            str(owner_account_id),
            str(session_id),
            str(task_id),
            str(fingerprint),
        )

    @staticmethod
    def _bounded_identity(value: object, field: str, maximum: int) -> str:
        text = str(value or "").strip()
        if "\x00" in text or len(text) > maximum:
            raise ValueError(f"{field} is invalid or too long")
        return text

    def _ui_ok(self) -> bool:
        try:
            return bool(self._ui_available())
        except Exception:
            log.exception("security alert UI availability check failed")
            return False

    @staticmethod
    def _classify_event(event: Any) -> tuple[SecurityAlertKind | None, str, str]:
        """Derive one bounded alert class from an already-sanitized audit event."""
        action_type = str(getattr(event, "action_type", "") or "")
        decision = str(getattr(event, "decision", "") or "")
        error_code = str(getattr(event, "stable_error_code", "") or "")
        detail = str(getattr(event, "action_detail", "") or "")
        if action_type == "runtime_diagnostic":
            if error_code in _MANIFEST_ERROR_CODES:
                return SecurityAlertKind.MANIFEST_MISMATCH, detail, error_code
            if error_code:
                return SecurityAlertKind.SANDBOX_FALLBACK, detail, error_code
            if decision not in _READY_RUNTIME_DECISIONS:
                return SecurityAlertKind.SANDBOX_FALLBACK, detail, "runtime_failed"
            return None, "", ""
        if error_code in _UPDATE_ERROR_CODES:
            return SecurityAlertKind.UPDATE_SIGNATURE_FAILURE, detail, error_code
        if error_code in _ORPHAN_ERROR_CODES:
            return SecurityAlertKind.ORPHAN_PROCESS, detail, error_code
        if error_code == "audit_chain_break":
            return SecurityAlertKind.AUDIT_CHAIN_BREAK, detail, error_code
        if decision in _DENY_DECISIONS:
            fingerprint = str(getattr(event, "normalized_action_hash", "") or "")
            return SecurityAlertKind.ANOMALOUS_DENIALS, detail, fingerprint
        return None, "", ""

    def observe_event(self, event: Any) -> SecurityAlert | None:
        """Translate one audit event into a deduplicated alert, if applicable."""
        kind, detail, fingerprint = self._classify_event(event)
        if kind is None:
            return None
        return self.record(
            kind,
            str(getattr(event, "owner_account_id", "") or ""),
            str(getattr(event, "session_id", "") or ""),
            str(getattr(event, "task_id", "") or ""),
            detail,
            fingerprint=fingerprint,
        )

    def record(
        self,
        kind: SecurityAlertKind | str,
        owner_account_id: str,
        session_id: str = "",
        task_id: str = "",
        detail: str = "",
        *,
        fingerprint: str = "",
    ) -> SecurityAlert | None:
        """Add one occurrence and return the frozen alert when it is actionable."""
        normalized_kind = SecurityAlertKind(kind)
        owner = self._bounded_identity(owner_account_id, "owner", 200)
        session = self._bounded_identity(session_id, "session", 200)
        task = self._bounded_identity(task_id, "task", 200)
        normalized_fingerprint = self._bounded_identity(
            fingerprint,
            "fingerprint",
            128,
        )
        normalized_detail = redact_sensitive_display_text(str(detail or ""))[
            : self._MAX_DETAIL_BYTES
        ]
        key = self._key(
            normalized_kind,
            owner,
            session,
            task,
            normalized_fingerprint,
        )
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            state = self._states.get(key)
            if state is None:
                if len(self._states) >= self._MAX_ALERTS:
                    raise SecurityAlertActionDenied("alert_capacity")
                state = _AlertState(
                    alert_id=secrets.token_urlsafe(16),
                    kind=str(normalized_kind),
                    owner_account_id=owner,
                    session_id=session,
                    task_id=task,
                    fingerprint=normalized_fingerprint,
                    detail=normalized_detail,
                    count=0,
                    first_seen=now,
                    last_seen=now,
                    isolated=False,
                    auto_denied=False,
                    resolved=False,
                )
                self._states[key] = state
                self._by_id[state.alert_id] = key
            state.count += 1
            state.last_seen = now
            if state.count >= self._threshold and not state.isolated:
                if not self._ui_ok():
                    state.auto_denied = True
                    state.isolated = True
                    self._call_action(
                        self._revoke,
                        owner,
                        session,
                        task,
                    )
            if state.count == self._threshold:
                return state.freeze(count=state.count, last_seen=state.last_seen)
            return None

    def report(
        self,
        kind: SecurityAlertKind | str,
        owner_account_id: str,
        *,
        session_id: str = "",
        task_id: str = "",
        detail: str = "",
        fingerprint: str = "",
    ) -> SecurityAlert | None:
        """Public explicit failure report (for example Desktop update signature)."""
        return self.record(
            kind,
            owner_account_id,
            session_id=session_id,
            task_id=task_id,
            detail=detail,
            fingerprint=fingerprint,
        )

    def snapshot(
        self,
        owner_account_id: str = "",
        *,
        include_resolved: bool = False,
    ) -> list[SecurityAlert]:
        """Return newest-first alerts, optionally scoped to one owner."""
        owner = str(owner_account_id or "").strip()
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            alerts = [
                state.freeze(count=state.count, last_seen=state.last_seen)
                for state in self._states.values()
                if (include_resolved or not state.resolved)
                and (not owner or state.owner_account_id == owner)
            ]
        return sorted(alerts, key=lambda item: item.last_seen, reverse=True)

    def get(self, alert_id: str) -> SecurityAlert | None:
        with self._lock:
            key = self._by_id.get(str(alert_id))
            state = self._states.get(key) if key is not None else None
            if state is None:
                return None
            return state.freeze(count=state.count, last_seen=state.last_seen)

    def isolate(
        self,
        alert_id: str,
        *,
        require_ui: bool = True,
    ) -> bool:
        """Freeze the alert's owner/session; user actions require an available UI."""
        if require_ui and not self._ui_ok():
            raise SecurityAlertActionDenied("alert_ui_unavailable")
        with self._lock:
            key = self._by_id.get(str(alert_id))
            state = self._states.get(key) if key is not None else None
            if state is None or state.resolved:
                return False
            if not state.isolated:
                state.isolated = True
                self._call_action(
                    self._freeze,
                    state.owner_account_id,
                    state.session_id,
                    state.task_id,
                )
            return True

    def revoke(
        self,
        alert_id: str,
        *,
        require_ui: bool = True,
    ) -> bool:
        """Revoke owner-scoped authority for the alert; UI actions require a UI."""
        if require_ui and not self._ui_ok():
            raise SecurityAlertActionDenied("alert_ui_unavailable")
        with self._lock:
            key = self._by_id.get(str(alert_id))
            state = self._states.get(key) if key is not None else None
            if state is None or state.resolved:
                return False
            state.isolated = True
            state.auto_denied = True
            self._call_action(
                self._revoke,
                state.owner_account_id,
                state.session_id,
                state.task_id,
            )
            return True

    def resolve(self, alert_id: str) -> bool:
        with self._lock:
            key = self._by_id.get(str(alert_id))
            state = self._states.get(key) if key is not None else None
            if state is None or state.resolved:
                return False
            state.resolved = True
            return True

    def should_deny(
        self,
        owner_account_id: str,
        session_id: str = "",
        task_id: str = "",
    ) -> str:
        """Return the reason new authority must be denied for this scope, or ``""``."""
        owner = str(owner_account_id or "").strip()
        session = str(session_id or "").strip()
        task = str(task_id or "").strip()
        with self._lock:
            candidates = [
                state
                for state in self._states.values()
                if not state.resolved
                and state.owner_account_id == owner
                and state.count >= self._threshold
                and (not session or not state.session_id or state.session_id == session)
                and (not task or not state.task_id or state.task_id == task)
            ]
            if any(state.auto_denied for state in candidates):
                return "security_alert_auto_denied"
            if any(not state.isolated for state in candidates) and not self._ui_ok():
                return "security_alert_ui_unavailable"
        return ""

    def _prune_locked(self, now: float) -> None:
        cutoff = now - self._window_seconds
        for key, state in list(self._states.items()):
            if state.resolved and state.last_seen < cutoff:
                self._by_id.pop(state.alert_id, None)
                self._states.pop(key, None)

    @staticmethod
    def _call_action(
        action: Callable[[str, str, str], object],
        owner_account_id: str,
        session_id: str,
        task_id: str,
    ) -> None:
        try:
            action(owner_account_id, session_id, task_id)
        except Exception:
            # The registry's auto_denied state remains the fail-closed fallback
            # even if the host's revocation callback is temporarily broken.
            log.exception("security alert revocation callback failed")
