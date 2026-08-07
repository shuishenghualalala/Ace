from pathlib import Path


ROOT = Path(__file__).parents[2]


def test_cron_and_team_reenter_app_security_context() -> None:
    app = (ROOT / "crew/app.py").read_text(encoding="utf-8")
    runtime = (ROOT / "crew/agent/runtime.py").read_text(encoding="utf-8")
    assert "build_gateway_security_context" in app
    assert 'envelope.params["_security_process_launch"]' in app
    assert 'current_process_launch.set(envelope.params.get("_security_process_launch"))' in runtime


def test_managed_mcp_and_acp_fail_closed_until_native_stdio_exists() -> None:
    mcp = (ROOT / "crew/tools/mcp_client.py").read_text(encoding="utf-8")
    acp = (ROOT / "crew/agent/external/acp_adapter.py").read_text(encoding="utf-8")
    assert "ACE_ALLOW_HOST_MCP_STDIO" in mcp
    assert "managed 模式拒绝宿主启动" in acp
