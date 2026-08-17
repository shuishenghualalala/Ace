from __future__ import annotations

import logging
import socket

import pytest

from crew.security.provider_proxy import ProviderProxyConfig, ProviderProxyUnavailable
from crew.security.outbound import (
    ConnectionPlan,
    OutboundContext,
    OutboundDenied,
    OutboundGrantRegistry,
    OutboundHttpClient,
    OutboundPolicy,
    OutboundTarget,
    ResolvedEndpoint,
)


def _public_answer(*_args, **_kwargs):
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("93.184.216.34", 443),
        )
    ]


@pytest.mark.parametrize(
    "address",
    [
        "192.0.0.9",
        "192.0.0.10",
        "198.18.0.1",
        "203.0.113.1",
        "64:ff9b::c000:0009",
    ],
)
def test_reserved_and_special_use_addresses_are_not_public(address: str) -> None:
    policy = OutboundPolicy(
        resolver=lambda *_args, **_kwargs: [
            (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443, 0, 0) if ":" in address else (address, 443))
        ]
    )

    with pytest.raises(OutboundDenied) as denied:
        policy.plan_url("https://special.example.test/")

    assert denied.value.code == "non_public_target"


def test_connect_rechecks_frozen_endpoint_before_creating_socket() -> None:
    socket_calls = 0

    def socket_factory(_family: int, _kind: int):
        nonlocal socket_calls
        socket_calls += 1
        raise AssertionError("invalid frozen endpoint must not create a socket")

    policy = OutboundPolicy(socket_factory=socket_factory)
    plan = ConnectionPlan(
        target=OutboundTarget("https", "example.test", 443, "GET"),
        endpoints=(
            ResolvedEndpoint(
                socket.AF_INET,
                "10.0.0.7",
                ("10.0.0.7", 443),
            ),
        ),
        expires_monotonic=policy._clock() + 10,
    )

    with pytest.raises(OutboundDenied, match="non_public_target"):
        policy.connect_socket(plan)

    assert socket_calls == 0


def test_mapped_metadata_endpoint_is_denied_even_with_private_grant() -> None:
    context = OutboundContext("owner", "session", "task", "request", "test")
    grants = OutboundGrantRegistry()
    token = grants.issue_private(
        context,
        host="::ffff:169.254.169.254",
        port=443,
        scheme="https",
    )

    with pytest.raises(OutboundDenied, match="metadata_target"):
        OutboundPolicy(grants=grants).plan_url(
            "https://[::ffff:169.254.169.254]/",
            context=context,
            private_grant=token,
        )


def test_cross_origin_redirect_requires_an_explicit_reauthorization() -> None:
    policy = OutboundPolicy(resolver=_public_answer)
    client = OutboundHttpClient(policy)

    def fake_fetch(plan, **_kwargs):
        if plan.target.host == "example.test":
            return type(
                "Response",
                (),
                {
                    "status": 302,
                    "headers": {"location": "https://other.example.test/next"},
                },
            )()
        raise AssertionError("cross-origin redirect must not be fetched")

    client.fetch_plan = fake_fetch

    with pytest.raises(OutboundDenied, match="redirect_reauthorization_required"):
        client.fetch("https://example.test/start", max_redirects=1)


def test_redirect_reauthorization_callback_receives_normalized_targets() -> None:
    policy = OutboundPolicy(resolver=_public_answer)
    client = OutboundHttpClient(policy)
    seen: list[tuple[str, str]] = []

    def fake_fetch(plan, **_kwargs):
        if plan.target.host == "example.test":
            return type(
                "Response",
                (),
                {
                    "status": 302,
                    "headers": {"location": "https://other.example.test/next"},
                },
            )()
        return type("Response", (), {"status": 200, "headers": {}})()

    def authorize(previous, next_target) -> bool:
        seen.append((previous.audit_summary, next_target.audit_summary))
        return True

    client.fetch_plan = fake_fetch
    response = client.fetch(
        "https://example.test/start",
        max_redirects=1,
        redirect_authorizer=authorize,
    )

    assert response.status == 200
    assert seen == [("https://example.test", "https://other.example.test")]


def test_outbound_decision_audit_has_safe_network_fields(caplog) -> None:
    caplog.set_level(logging.INFO, logger="crew.security.outbound")
    context = OutboundContext("owner", "session", "task", "request", "test")

    with pytest.raises(OutboundDenied):
        OutboundPolicy().plan_url(
            "https://192.0.2.1:8443/private?token=must-not-leak",
            method="POST",
            context=context,
        )

    record = next(
        item
        for item in caplog.records
        if getattr(item, "outbound_event", "") == "network_decision"
    )
    assert record.outbound_decision == "deny"
    assert record.outbound_host == "192.0.2.1"
    assert record.outbound_port == 8443
    assert record.outbound_protocol == "https"
    assert record.outbound_method == "POST"
    assert record.outbound_source == "test"
    assert record.outbound_reason == "private_grant_required"
    assert "token=must-not-leak" not in record.getMessage()


def test_provider_proxy_config_is_loopback_and_credential_free() -> None:
    with pytest.raises(ProviderProxyUnavailable):
        ProviderProxyConfig(
            "http://user:password@127.0.0.1:43119/",
            "crew",
            "password",
            origin=("https", "api.example.test", 443),
        )

    with pytest.raises(ProviderProxyUnavailable):
        ProviderProxyConfig(
            "http://proxy.example.test:43119/",
            "crew",
            "password",
            origin=("https", "api.example.test", 443),
        )


def test_provider_request_is_bound_to_exact_origin() -> None:
    config = ProviderProxyConfig(
        "http://127.0.0.1:43119/",
        "crew",
        "password",
        origin=("https", "api.example.test", 443),
    )

    with pytest.raises(ProviderProxyUnavailable, match="origin mismatch"):
        config.validate_request("https://other.example.test/v1", method="POST")
