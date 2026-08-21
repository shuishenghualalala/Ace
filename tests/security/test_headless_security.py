from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


def test_cron_and_team_reenter_app_security_context() -> None:
    app = (ROOT / "crew/app.py").read_text(encoding="utf-8")
    runtime = (ROOT / "crew/agent/runtime.py").read_text(encoding="utf-8")
    assert "build_gateway_security_context" in app
    assert 'envelope.params["_security_process_launch"]' in app
    assert "current_process_launch.set(" in runtime
    assert "current_process_launch.reset(" in runtime



def test_mcp_stdio_requires_authenticated_bounded_native_transport() -> None:
    mcp = (ROOT / "crew/tools/mcp_client.py").read_text(encoding="utf-8")
    assert "MCP stdio requires an authenticated managed launch context" in mcp
    assert "open_authorized_stdio" in mcp
    assert "max_lifetime_seconds=MCP_STDIO_MAX_LIFETIME_SECONDS" in mcp


def test_mcp_stdio_env_does_not_inherit_ambient_credentials(monkeypatch) -> None:
    from crew.tools.mcp_client import _interpolate, _stdio_env

    monkeypatch.setenv("PATH", "C:/safe/bin")
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy-with-credentials")

    env = _stdio_env({"SAFE_VALUE": "explicit-value"})

    assert env is not None
    assert "PATH" not in env
    assert "HOME" not in env
    assert env["SAFE_VALUE"] == "explicit-value"
    assert "OPENAI_API_KEY" not in env
    assert "HTTPS_PROXY" not in env
    assert _interpolate("${OPENAI_API_KEY}") == "${OPENAI_API_KEY}"
    assert _interpolate("${PATH}") == "${PATH}"
    with pytest.raises(ValueError, match="not allowed"):
        _stdio_env({"PATH": "C:/configured/bin"})
    with pytest.raises(ValueError, match="not allowed"):
        _stdio_env({"PYTHONPATH": "C:/untrusted"})


def test_host_security_helpers_use_minimal_environment(monkeypatch) -> None:
    from crew.security.launch import minimal_inherited_environment

    monkeypatch.setenv("OPENAI_API_KEY", "ambient-secret")
    monkeypatch.setenv("PATH", "C:/safe/bin")
    monkeypatch.setenv("ACE_BUNDLED_BWRAP", "C:/attacker/bwrap")
    monkeypatch.setenv("ACE_BUNDLED_BWRAP_SHA256", "0" * 64)

    env = minimal_inherited_environment()

    assert env["PATH"] == "C:/safe/bin"
    assert "OPENAI_API_KEY" not in env
    assert "ACE_BUNDLED_BWRAP" not in env
    assert "ACE_BUNDLED_BWRAP_SHA256" not in env


def test_browser_control_payload_and_owner_scope_are_strict() -> None:
    from crew.gateway.routers.browser import (
        _browser_owner_access_allowed,
        _parse_browser_control_payload,
    )

    with pytest.raises(ValueError):
        _parse_browser_control_payload({"action": "open", "unexpected": True})
    with pytest.raises(ValueError):
        _parse_browser_control_payload({"action": "open", "value": 1})

    assert _parse_browser_control_payload({"action": "open"}) == ("open", "")

    class AccessControl:
        user_type = "default"

        @staticmethod
        def resolve_for(_user_type: str) -> dict:
            return {}

    class Registry:
        @staticmethod
        def list_schemas(**_kwargs):
            return [{"_crew_toolset": "browser"}]

    assert not _browser_owner_access_allowed(
        Registry(), AccessControl(), lambda _owner, _user_type: False, "owner"
    )
    assert _browser_owner_access_allowed(
        Registry(), AccessControl(), lambda _owner, _user_type: True, "owner"
    )
