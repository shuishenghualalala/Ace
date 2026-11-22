"""Provider HTTP client construction boundaries."""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest

from crew.providers.http_client import create_async_client


def test_proxy_parse_failure_names_the_environment_variables(monkeypatch):
    def fail(**_kwargs):
        raise httpx.InvalidURL("Invalid port: ':1]'")

    monkeypatch.setattr(httpx, "AsyncClient", fail)
    with pytest.raises(RuntimeError, match="NO_PROXY"):
        create_async_client(timeout=1)


def test_bracketed_ipv6_loopback_is_normalized(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,[::1]")
    client = create_async_client(timeout=1)
    try:
        assert os.environ["NO_PROXY"] == "localhost,127.0.0.1,::1"
    finally:
        asyncio.run(client.aclose())


def test_bracketed_ipv6_entry_with_port_is_normalized_to_loopback(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "[::1]:8080")
    client = create_async_client(timeout=1)
    try:
        assert os.environ["NO_PROXY"] == "::1"
    finally:
        asyncio.run(client.aclose())
