"""Provider HTTP client construction boundaries."""

from __future__ import annotations

import httpx
import pytest

from crew.providers.http_client import create_async_client


def test_proxy_parse_failure_names_the_environment_variables(monkeypatch):
    def fail(**_kwargs):
        raise httpx.InvalidURL("Invalid port: ':1]'")

    monkeypatch.setattr(httpx, "AsyncClient", fail)
    with pytest.raises(RuntimeError, match="NO_PROXY"):
        create_async_client(timeout=1)
