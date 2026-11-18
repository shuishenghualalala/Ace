"""crew CLI 命令树自动化测试。"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

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


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["once", "reject", None], ids=["approve-once", "reject", "eof"])
async def test_chat_plan_file_approval_preserves_selected_workspace(
    cli_home, tmp_path, external_write_dir, monkeypatch, decision,
):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.output import DummyOutput

    from crew.agent.plan import manager as plan_manager
    from crew.agent.plan import plan_path, read_plan
    from crew.app import build_app
    from crew.cli import chat
    from crew.cli.app import CliContext
    from crew.core.mocks import FakeProvider
    from crew.core.types import ChatResponse, ToolCall
    from crew.state.config import Config

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(plan_manager, "plans_dir", lambda: external_write_dir / "plans")
    owner = "cli-test"
    session_id = "cli_12345678"
    selected = tmp_path / "selected"
    selected.mkdir()
    monkeypatch.setattr(chat, "uuid", SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="12345678")))
    app = build_app(Config(api_key="", cron_enabled=False), enable_team=False)
    workspace = app.workspace_store.create("Selected", root_path=str(selected), owner_account_id=owner)
    plan = "# Write a marker in the selected workspace."
    assert isinstance(app.provider, FakeProvider)

    def model_steps():
        yield ToolCall("plan-file", "file_write", {"path": str(plan_path(session_id, owner)), "content": plan})
        yield ToolCall("review", "exit_plan_mode", {})
        if decision == "once":
            yield ToolCall("apply", "file_write", {"path": "marker.txt", "content": "applied"})
        yield "Finished."

    steps = model_steps()

    async def model_chat(*args, **kwargs):
        step = next(steps)
        return ChatResponse(tool_calls=[step]) if isinstance(step, ToolCall) else ChatResponse(text=step)

    monkeypatch.setattr(app.provider, "chat", model_chat)
    decisions = []
    decide = app.security_service.decide

    def record_decision(context, **kwargs):
        result = decide(context, **kwargs)
        decisions.append((kwargs["decision"].value, kwargs["nonce"]))
        return result

    monkeypatch.setattr(app.security_service, "decide", record_decision)
    answers = ["/plan", "Write a plan.", decision]
    if decision == "once":
        answers.append("y")
    answers.append("/quit")
    prompts = []

    async def answer(prompt):
        prompts.append(prompt)
        value = answers.pop(0)
        if value is None:
            raise EOFError
        return value

    monkeypatch.setattr(chat, "PromptSession", lambda: SimpleNamespace(prompt_async=answer))
    try:
        with create_app_session(output=DummyOutput()):
            await asyncio.wait_for(chat._repl(app, CliContext(owner=owner, workspace_id=workspace["id"])), timeout=10)
        assert not answers
        approval_prompt, = [prompt for prompt in prompts if "[once/reject]" in prompt]
        scope = f"文件权限: read_write {plan_path(session_id, owner)}"
        assert repr(scope)[1:-1] in approval_prompt
        assert [result for result, _ in decisions] == [decision or "reject"]
        assert decisions[0][1] and decisions[0][1] not in approval_prompt
        assert sum("[y/N]" in prompt for prompt in prompts) == (1 if decision == "once" else 0)
        markers = [*tmp_path.rglob("marker.txt"), *external_write_dir.rglob("marker.txt")]
        if decision == "once":
            assert read_plan(session_id, owner) == plan
            assert markers == [selected / "marker.txt"]
            assert markers[0].read_text(encoding="utf-8") == "applied"
        else:
            assert not read_plan(session_id, owner)
            assert not markers
    finally:
        await app.shutdown()


@pytest.mark.asyncio
async def test_cancelled_cli_permission_prompt_rejects_only_its_bound_request(
    cli_home, tmp_path, external_write_dir, monkeypatch,
):
    from crew.app import build_app
    from crew.cli.security import consume_conversation_turn
    from crew.core.envelope import Envelope
    from crew.core.runctx import current_task_runtime_id
    from crew.core.types import ChatResponse, ToolCall
    from crew.security.context import build_security_context
    from crew.state.config import Config

    monkeypatch.chdir(tmp_path)
    app = build_app(Config(api_key="", cron_enabled=False), enable_team=False)
    service = app.security_service
    target = external_write_dir / "outside-workspaces.txt"
    both_pending = asyncio.Event()
    release_other = asyncio.Event()
    requests = {}
    decisions = []

    async def model_chat(messages, *args, **kwargs):
        if any(message.role == "tool" for message in messages):
            return ChatResponse(text="Finished.")
        return ChatResponse(tool_calls=[ToolCall("write", "file_write", {"path": str(target), "content": "fixture"})])

    async def prompt(_text):
        context = build_security_context(app.workspace_store)
        request, = service.pending(context, include_nonce=False)
        requests[context.owner_account_id] = (context, request["request_id"])
        if len(requests) == 2:
            both_pending.set()
        await release_other.wait()
        return False

    decide = service.decide

    def record_decision(context, **kwargs):
        result = decide(context, **kwargs)
        decisions.append((context, kwargs["request_id"], kwargs["decision"].value))
        return result

    monkeypatch.setattr(app.provider, "chat", model_chat)
    monkeypatch.setattr(service, "decide", record_decision)

    async def run_turn(owner):
        current_task_runtime_id.set(f"task-{owner}")
        envelope = Envelope.of("Write a file.", session_id=f"cli-{owner}", user_id=owner)
        await consume_conversation_turn(app, envelope, lambda _chunk: None, prompt=prompt)

    turns = [asyncio.create_task(run_turn(owner)) for owner in ("owner-a", "owner-b")]
    try:
        async with asyncio.timeout(15):
            await both_pending.wait()
            first, other = requests["owner-a"], requests["owner-b"]
            assert (first[0].task_id, other[0].task_id) == ("task-owner-a", "task-owner-b")
            assert first[0].session_id != other[0].session_id
            turns[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await turns[0]
            assert decisions == [(*first, "reject")]
            assert not service.pending(first[0], include_nonce=False)
            assert [item["request_id"] for item in service.pending(other[0], include_nonce=False)] == [other[1]]
            assert not turns[1].done()
            release_other.set()
            await turns[1]
            assert decisions == [(*first, "reject"), (*other, "reject")]
            assert not target.exists()
    finally:
        for turn in turns:
            turn.cancel()
        await asyncio.gather(*turns, return_exceptions=True)
        await app.shutdown()


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


def test_dump_features_outputs_audit_and_returns_nonzero_for_blocker(
    capsys,
    cli_home,
    monkeypatch,
):
    class FakeReport:
        healthy = False

        @staticmethod
        def as_dict():
            return {
                "healthy": False,
                "issues": ["waiting-plugin"],
                "features": [
                    {
                        "id": "waiting-plugin",
                        "state": "waiting",
                        "generation": None,
                        "config": {
                            "desired_revision": 1,
                            "effective_revision": None,
                        },
                        "services": {
                            "requires": ["storage"],
                            "optional": [],
                            "provides": [],
                            "missing_required": ["storage"],
                            "missing_optional": [],
                        },
                        "registrations": [],
                        "error": None,
                    }
                ],
            }

    class FakePlugins:
        @staticmethod
        def startup_audit():
            return FakeReport()

    class FakeApp:
        plugins = FakePlugins()
        closed = False

        async def shutdown(self):
            self.closed = True

    app = FakeApp()
    monkeypatch.setattr("crew.cli.app.build_app", lambda _config: app)
    monkeypatch.setattr("crew.cli.app.load_config", lambda _path: object())

    code, out = run_cli(capsys, "--dump-features", "--json")

    assert code == 1
    assert json_out(out)["issues"] == ["waiting-plugin"]
    assert app.closed


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


def test_session_show_missing_exit_code(capsys, cli_home):
    code, _ = run_cli(capsys, "session", "show", "--id", "missing")
    # 业务错误的语义码（404）在进程出口统一收敛为 1（见 _shell_exit_code）
    assert code == 1


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


@pytest.mark.parametrize("available", [True, False])
def test_security_auto_review_uses_live_runtime_probe(capsys, cli_home, monkeypatch, available):
    async def probe(*, network_access):
        assert network_access is False
        return (object() if available else None), available, False, "test"

    monkeypatch.setattr("crew.gateway.routers.security._live_filesystem_runtime", probe)

    code, out = run_cli(
        capsys,
        "security",
        "mode",
        "set",
        "--session-id",
        "sec-sess",
        "--mode",
        "auto_review",
        "--json",
    )

    if available:
        assert code == 0
        assert json_out(out)["mode"] == "auto_review"
    else:
        assert code == 1


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
    # The default auto-review mode admits this public HTTPS target.
    assert data is not None and data["allowed"] is True
    assert data["request"] is None


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
    if os.name == "nt":
        if not os.environ.get("ACE_SECURITY_STATE_DIR"):
            pytest.skip("Windows native sandbox state is not configured for the CLI")
        argv = ("cmd.exe", "/d", "/c", "echo sandbox-ok")
    else:
        argv = ("/bin/echo", "sandbox-ok")
    workspace = cli_home.parent / "workspace"
    workspace.mkdir()
    code, out = run_cli(
        capsys, "security", "sandbox-run", "--cwd", str(workspace), "--argv", *argv, "--json"
    )
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


@pytest.mark.parametrize("output", ["json", "stream-json"])
def test_run_returns_structured_rejection_for_required_approval(
    capsys, cli_home, external_write_dir, monkeypatch, output,
):
    from crew.core.mocks import FakeProvider
    from crew.core.types import ChatResponse, ToolCall

    target = external_write_dir / "protected.txt"
    calls = 0

    async def model_chat(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        return ChatResponse(tool_calls=[ToolCall("write", "file_write", {"path": str(target), "content": "fixture"})])

    monkeypatch.setattr(FakeProvider, "chat", model_chat)
    code, out = run_cli(capsys, "run", "Write a file.", "--yes", "--output-format", output)

    assert code == 2
    assert calls == 1
    assert not target.exists()
    if output == "json":
        summary = json.loads(out)
        assert (summary["status"], summary["error_code"]) == ("stopped", "approval_required")
    else:
        final = [json.loads(line) for line in out.splitlines()][-1]
        assert final["is_final"]
        assert final["body"]["outcome"]["reason"] == "approval_rejected"


def test_business_error_exit_code_normalized(capsys, cli_home):
    """CliError 的 HTTP 风格语义码（404/409）在进程出口收敛为 1，
    不再出现 404 被 shell 截断成 148（看起来像被信号杀死）的情况。"""
    code, _ = run_cli(capsys, "task", "show", "--id", "badtask")
    assert code == 1


def test_cli_log_level_env_does_not_leak(capsys, cli_home):
    """main() 运行期间把 CREW_LOG_LEVEL 压为 WARNING，退出后恢复原状，
    不污染同进程的其他调用方（测试套件也在同进程跑 main()）。"""
    os.environ.pop("CREW_LOG_LEVEL", None)
    try:
        code, _ = run_cli(capsys, "--help")
        assert code == 0
        assert os.environ.get("CREW_LOG_LEVEL") is None
    finally:
        os.environ.pop("CREW_LOG_LEVEL", None)


def test_readonly_content_commands(capsys, cli_home):
    for argv in (("site", "list"), ("work", "items", "list"), ("system", "health")):
        code, out = run_cli(capsys, *argv, "--json")
        assert code == 0, argv
        assert json_out(out) is not None, argv
