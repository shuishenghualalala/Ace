from __future__ import annotations

from types import SimpleNamespace

import pytest

from crew.security.alerts import (
    SecurityAlertActionDenied,
    SecurityAlertKind,
    SecurityAlertRegistry,
)


def _registry(
    *,
    ui_available: bool = True,
    now: list[float] | None = None,
) -> tuple[SecurityAlertRegistry, list[str], list[str]]:
    freezes: list[str] = []
    revokes: list[str] = []
    clock_value = [100.0] if now is None else now
    registry = SecurityAlertRegistry(
        ui_available=lambda: ui_available,
        freeze=lambda owner, session, task: freezes.append(
            f"{owner}:{session}:{task}"
        ),
        revoke=lambda owner, session, task: revokes.append(
            f"{owner}:{session}:{task}"
        ),
        threshold=3,
        window_seconds=30.0,
        clock=lambda: clock_value[0],
    )
    return registry, freezes, revokes


def test_alerts_deduplicate_in_window_and_emit_once_at_threshold() -> None:
    registry, _freezes, _revokes = _registry()

    assert registry.record(SecurityAlertKind.ANOMALOUS_DENIALS, "owner") is None
    assert registry.record(SecurityAlertKind.ANOMALOUS_DENIALS, "owner") is None
    alert = registry.record(SecurityAlertKind.ANOMALOUS_DENIALS, "owner")
    assert alert is not None
    assert alert.count == 3
    assert len(registry.snapshot("owner")) == 1


def test_alerts_do_not_merge_across_task_or_fingerprint() -> None:
    registry, _freezes, _revokes = _registry()

    for _ in range(2):
        registry.record(
            SecurityAlertKind.ANOMALOUS_DENIALS,
            "owner",
            "session",
            "task-a",
            fingerprint="action-a",
        )
    assert registry.record(
        SecurityAlertKind.ANOMALOUS_DENIALS,
        "owner",
        "session",
        "task-a",
        fingerprint="action-b",
    ) is None
    registry.record(
        SecurityAlertKind.ANOMALOUS_DENIALS,
        "owner",
        "session",
        "task-a",
        fingerprint="action-b",
    )
    alert = registry.record(
        SecurityAlertKind.ANOMALOUS_DENIALS,
        "owner",
        "session",
        "task-a",
        fingerprint="action-b",
    )
    assert alert is not None
    assert {item.count for item in registry.snapshot("owner")} == {2, 3}


def test_threshold_auto_denies_when_ui_is_unavailable() -> None:
    registry, freezes, revokes = _registry(ui_available=False)

    for _ in range(2):
        assert registry.record(SecurityAlertKind.SANDBOX_FALLBACK, "owner") is None
    alert = registry.record(SecurityAlertKind.SANDBOX_FALLBACK, "owner")

    assert alert is not None
    assert alert.auto_denied is True
    assert alert.isolated is True
    assert revokes == ["owner::"]
    assert freezes == []
    assert registry.should_deny("owner") == "security_alert_auto_denied"


def test_unhandled_threshold_denies_new_authority_until_isolated() -> None:
    registry, _freezes, _revokes = _registry(ui_available=True)
    for _ in range(3):
        registry.record(SecurityAlertKind.MANIFEST_MISMATCH, "owner", "session")

    assert registry.should_deny("owner", "session") == ""
    registry._ui_available = lambda: False
    assert registry.should_deny("owner", "session") == "security_alert_ui_unavailable"


def test_isolate_and_revoke_are_one_click_and_require_ui() -> None:
    registry, freezes, revokes = _registry(ui_available=True)
    registry.record(SecurityAlertKind.MANIFEST_MISMATCH, "owner", "session")
    registry.record(SecurityAlertKind.MANIFEST_MISMATCH, "owner", "session")
    alert = registry.record(
        SecurityAlertKind.MANIFEST_MISMATCH,
        "owner",
        "session",
    )
    assert alert is not None

    assert registry.isolate(alert.alert_id) is True
    assert freezes == ["owner:session:"]
    assert revokes == []

    other_alert = None
    for _index in range(3):
        other_alert = registry.record(
            SecurityAlertKind.ORPHAN_PROCESS,
            "owner",
            "session",
            fingerprint="pid-1",
        )
    assert other_alert is not None
    assert registry.revoke(other_alert.alert_id) is True
    assert revokes == ["owner:session:"]

    registry._ui_available = lambda: False
    with pytest.raises(SecurityAlertActionDenied) as denied:
        registry.isolate(other_alert.alert_id)
    assert denied.value.code == "alert_ui_unavailable"


def test_observe_event_classifies_runtime_and_denial_events() -> None:
    registry, _freezes, _revokes = _registry()
    runtime = SimpleNamespace(
        action_type="runtime_diagnostic",
        decision="failed",
        stable_error_code="runtime_stale",
        owner_account_id="owner",
        session_id="session",
        task_id="task",
        action_detail="SECRET=must-not-leak",
    )
    registry.observe_event(runtime)
    denial = SimpleNamespace(
        action_type="approval_decision",
        decision="deny",
        stable_error_code="",
        owner_account_id="owner",
        session_id="session",
        task_id="task",
        action_detail="denied action",
        normalized_action_hash="action-hash",
    )
    for _ in range(3):
        registry.observe_event(denial)

    alerts = registry.snapshot("owner")
    kinds = {item.kind for item in alerts}
    assert SecurityAlertKind.MANIFEST_MISMATCH in kinds
    assert SecurityAlertKind.ANOMALOUS_DENIALS in kinds
    assert all("must-not-leak" not in item.detail for item in alerts)


def test_snapshot_is_owner_scoped_and_resolve_hides_alert() -> None:
    registry, _freezes, _revokes = _registry()
    registry.record(SecurityAlertKind.UPDATE_SIGNATURE_FAILURE, "owner-a")
    alert = None
    for _index in range(3):
        alert = registry.record(
            SecurityAlertKind.UPDATE_SIGNATURE_FAILURE,
            "owner-b",
        )
    assert {item.owner_account_id for item in registry.snapshot("owner-a")} == {
        "owner-a"
    }
    assert alert is not None
    assert registry.resolve(alert.alert_id) is True
    assert registry.snapshot("owner-b") == []
