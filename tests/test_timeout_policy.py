from __future__ import annotations

import time

from crew.core.timeout_policy import TimeoutPolicy


def test_default_policy_preserves_legacy_external_budgets() -> None:
    policy = TimeoutPolicy.from_mapping({})

    team = policy.resolve_external(
        120,
        mode="team_execute",
        has_interaction_binding=True,
        protocol="acp-stdio",
    )
    assert team.idle_seconds == 330
    assert team.hard_seconds == 1320
    assert team.binding_ttl_seconds == 1350

    cli = policy.resolve_external(120, protocol="cli")
    assert cli.idle_seconds == 120
    assert cli.hard_seconds == 120


def test_explicit_zero_disables_only_the_hard_deadline() -> None:
    policy = TimeoutPolicy.from_mapping({"hard_timeout_seconds": 0})
    budget = policy.resolve_external(
        120,
        mode="team_execute",
        has_interaction_binding=True,
        protocol="acp-stdio",
    )

    assert budget.hard_seconds is None
    assert budget.hard_deadline(time.monotonic()) is None
    assert budget.idle_seconds == 330
    assert budget.binding_ttl_seconds == 360


def test_explicit_hard_timeout_overrides_protocol_compatibility_formula() -> None:
    policy = TimeoutPolicy.from_mapping({"hard_timeout_seconds": 2700})
    budget = policy.resolve_external(120, protocol="acp-stdio")

    assert budget.hard_seconds == 2700
    started = time.monotonic()
    assert budget.hard_deadline(started) == started + 2700


def test_configured_idle_timeout_is_the_shared_source_of_truth() -> None:
    policy = TimeoutPolicy.from_mapping({"external_idle_seconds": 600})
    budget = policy.resolve_external(120, protocol="codex-app-server")

    assert budget.idle_seconds == 600
    assert budget.hard_seconds is None
