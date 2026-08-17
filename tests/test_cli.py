"""crew CLI 命令树自动化测试。"""

from __future__ import annotations

import json

import pytest

from crew.cli.main import main


@pytest.fixture
def cli_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("CREW_HOME", str(home))
    monkeypatch.setenv("CREW_CLI_ACCOUNT", "cli-test")
    return home


def run_cli(capsys, *argv: str) -> tuple[int, str]:
    try:
        code = main(list(argv))
    except SystemExit as exc:
        code = int(exc.code or 0)
    return code, capsys.readouterr().out


def json_out(out: str):
    """从 stdout 中提取 emit() 打印的 JSON（前置日志行可能混入 stdout）。"""
    start = None
    for line in out.splitlines():
        stripped = line.strip()
        if stripped == "[" or stripped.startswith("{"):
            start = out.index(stripped)
            break
    if start is None:
        return None
    try:
        return json.loads(out[start:])
    except json.JSONDecodeError:
        return None


def test_help_lists_all_command_groups(capsys, cli_home):
    code, out = run_cli(capsys, "--help")
    assert code == 0
    for name in (
        "chat",
        "run",
        "config",
        "session",
        "workspace",
        "task",
        "cron",
        "system",
        "wiki",
        "skill",
        "mcp",
        "plugin",
        "channel",
        "runtime",
        "browser",
        "security",
        "site",
        "work",
    ):
        assert name in out


def test_version(capsys, cli_home):
    code, out = run_cli(capsys, "--version")
    assert code == 0
    assert out.strip().startswith("crew ")


def test_global_flags_work_before_subcommand(capsys, cli_home):
    code, out = run_cli(capsys, "--json", "config", "models", "list")
    assert code == 0
    data = json_out(out)
    assert data is not None
    assert any(item["id"] == "default" for item in data)


def test_config_models_list_json(capsys, cli_home):
    code, out = run_cli(capsys, "config", "models", "list", "--json")
    assert code == 0
    data = json_out(out)
    assert data is not None
    assert any(item["id"] == "default" for item in data)


def test_workspace_crud(capsys, cli_home):
    code, out = run_cli(capsys, "workspace", "create", "--name", "w1", "--json")
    assert code == 0
    created = json_out(out)
    assert created is not None and created["name"] == "w1"

    code, out = run_cli(capsys, "workspace", "list", "--json")
    assert code == 0
    ids = [row["id"] for row in json_out(out)]
    assert created["id"] in ids

    code, _ = run_cli(capsys, "workspace", "update", "--id", created["id"], "--name", "w2")
    assert code == 0
    code, _ = run_cli(capsys, "workspace", "delete", "--id", created["id"])
    assert code == 0


def test_session_flow(capsys, cli_home):
    code, _ = run_cli(capsys, "session", "ensure", "--id", "sess-1", "--title", "测试")
    assert code == 0

    code, out = run_cli(capsys, "session", "list", "--json")
    assert code == 0
    sessions = json_out(out)
    assert any(row["session_id"] == "sess-1" for row in sessions)

    code, _ = run_cli(capsys, "session", "title", "--id", "sess-1", "--title", "改名")
    assert code == 0
    code, out = run_cli(capsys, "session", "status", "--id", "sess-1", "--json")
    assert code == 0
    assert json_out(out) is not None

    code, _ = run_cli(capsys, "session", "delete", "--id", "sess-1")
    assert code == 0


def test_session_show_missing_returns_404(capsys, cli_home):
    code, _ = run_cli(capsys, "session", "show", "--id", "missing")
    assert code == 404


def test_cron_flow(capsys, cli_home):
    code, _ = run_cli(capsys, "session", "ensure", "--id", "cron-sess")
    assert code == 0
    code, out = run_cli(
        capsys,
        "cron",
        "create",
        "--name",
        "job1",
        "--schedule",
        "in 10m",
        "--query",
        "测试",
        "--session-id",
        "cron-sess",
        "--json",
    )
    assert code == 0
    job = json_out(out)
    assert job is not None and job["id"]

    code, out = run_cli(capsys, "cron", "list", "--json")
    assert code == 0
    assert any(item["id"] == job["id"] for item in json_out(out))

    assert run_cli(capsys, "cron", "pause", "--id", job["id"])[0] == 0
    assert run_cli(capsys, "cron", "resume", "--id", job["id"])[0] == 0
    assert run_cli(capsys, "cron", "delete", "--id", job["id"])[0] == 0


def test_channel_config_show_loads_builtin_platforms(capsys, cli_home):
    code, out = run_cli(
        capsys,
        "channel",
        "config",
        "show",
        "--platform",
        "feishu",
        "--json",
    )
    assert code == 0
    data = json_out(out)
    assert data is not None and data["ok"] is True and data["name"] == "feishu"


def test_wiki_kbs_list(capsys, cli_home):
    code, out = run_cli(capsys, "wiki", "kbs", "list", "--json")
    assert code == 0
    data = json_out(out)
    assert data is not None
    assert any(kb["id"] == "tutorial" for kb in data["kbs"])


def test_security_mode_and_fake_decision_flow(capsys, cli_home):
    code, _ = run_cli(capsys, "session", "ensure", "--id", "sec-sess")
    assert code == 0

    code, out = run_cli(
        capsys,
        "security",
        "mode",
        "set",
        "--session-id",
        "sec-sess",
        "--mode",
        "full_access",
        "--json",
    )
    assert code == 0
    assert json_out(out)["mode"] == "full_access"

    code, out = run_cli(
        capsys,
        "security",
        "fake-executions",
        "--session-id",
        "sec-sess",
        "--argv",
        "echo",
        "hi",
        "--decision",
        "reject",
        "--json",
    )
    assert code == 0
    fake = json_out(out)
    assert fake is not None and fake["request"]["request_id"]
    assert fake["decision"]["status"] == "rejected"


def test_security_check_terminal(capsys, cli_home):
    code, out = run_cli(capsys, "security", "check-terminal", "--command", "echo hi", "--json")
    assert code == 0
    data = json_out(out)
    assert data is not None and data["verdict"] == "allow"

    code, out = run_cli(capsys, "security", "check-terminal", "--command", "rm -rf /", "--json")
    assert code == 0
    data = json_out(out)
    assert data is not None and data["verdict"] == "blocked"


def test_security_check_file_and_network(capsys, cli_home):
    code, out = run_cli(
        capsys,
        "security",
        "check-file",
        "--path",
        "/tmp/ace-cli-check-target",
        "--operation",
        "write",
        "--json",
    )
    assert code == 0
    data = json_out(out)
    assert data is not None and data["result"] in {"allow", "require_approval"}

    code, out = run_cli(
        capsys,
        "security",
        "check-network",
        "--url",
        "https://example.com",
        "--json",
    )
    assert code == 0
    data = json_out(out)
    assert data is not None and data["allowed"] is True


def test_security_fake_file_and_network_actions(capsys, cli_home):
    code, _ = run_cli(capsys, "session", "ensure", "--id", "sec-fake")
    assert code == 0

    code, out = run_cli(
        capsys,
        "security",
        "fake-file-actions",
        "--session-id",
        "sec-fake",
        "--path",
        "/tmp/ace-cli-fake-file",
        "--operation",
        "write",
        "--decision",
        "reject",
        "--json",
    )
    assert code == 0
    data = json_out(out)
    assert data is not None and data["request"]["request_id"]
    assert data["decision"]["status"] == "rejected"

    code, out = run_cli(
        capsys,
        "security",
        "fake-network-actions",
        "--session-id",
        "sec-fake",
        "--host",
        "example.com",
        "--port",
        "443",
        "--protocol",
        "https",
        "--decision",
        "reject",
        "--json",
    )
    assert code == 0
    data = json_out(out)
    assert data is not None and data["request"]["request_id"]
    assert data["decision"]["status"] == "rejected"


def test_security_sandbox_run(capsys, cli_home):
    from pathlib import Path

    from crew.security.launch import packaged_runtime_argv

    if not Path(packaged_runtime_argv()[0]).is_file():
        pytest.skip("native security runtime 未随包安装")
    code, out = run_cli(capsys, "security", "sandbox-run", "--argv", "/bin/echo", "sandbox-ok", "--json")
    assert code == 0
    data = json_out(out)
    assert data is not None and data["exit_code"] == 0
    assert "sandbox-ok" in data["stdout"]


def test_run_non_interactive_json(capsys, cli_home):
    code, out = run_cli(capsys, "run", "你好", "--output-format", "json")
    assert code == 0
    data = json_out(out)
    assert data is not None
    assert data["session_id"]
    assert "收到" in data["text"]


def test_readonly_content_commands(capsys, cli_home):
    for argv in (("site", "list"), ("work", "items", "list"), ("system", "health")):
        code, out = run_cli(capsys, *argv, "--json")
        assert code == 0, argv
        assert json_out(out) is not None, argv
