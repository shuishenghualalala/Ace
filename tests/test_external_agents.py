import asyncio
import json
import os
import sqlite3
import stat
import sys
import textwrap
from types import SimpleNamespace

import pytest

from crew.agent.external import acp_adapter, detector, process_lifecycle, runtime_registry
from crew.agent.executor.base import ExecutionContext
from crew.agent.skills import SkillActivation, SkillEntrypoint
from crew.agent.executor.external import (
    AcpExecutor,
    ClientExecutor,
    _build_compact_acp_system_prompt,
    _external_system_prompt,
    _followup_cli_diagnostic,
    _followup_mcp_diagnostic,
    _looks_like_missing_followup_tool,
    _permission_guard,
    _permission_question,
    _stream_runtime_with_safe_resume,
)
from crew.core.types import Message, ToolCall
from crew.agent.external.detector import (
    discover_local_runtimes,
    scan_claude_runtime,
    scan_codex_runtime,
    scan_hermes_runtime,
    scan_kimi_runtime,
    scan_runtimes,
)
from crew.agent.external.acp_adapter import (
    AcpAdapterConfig,
    AcpAdapterError,
    _JsonRpcClient,
    _build_session_new_params,
    _build_session_resume_params,
    _permission_result,
    run_acp_prompt,
    stream_acp_events,
)
from crew.agent.external.cli_adapter import (
    ClaudeStreamJsonAdapter,
    ExternalCliConfig,
    _compact_cli_error,
    run_external_cli,
    stream_claude_events,
)
from crew.agent.external.codex_adapter import stream_codex_events
from crew.agent.external.runtime_adapter import (
    ExternalStreamEvent,
    RuntimeExecutionRequest,
    RuntimeResumeRejected,
    build_external_runtime_env,
    runtime_adapter_ids,
)
from crew.agent.external.store import ExternalAgentStore
from crew.agent.external.tools import register_external_agent_tools
from crew.gateway.helpers import role_markdown, suggest_role_description, with_session_agent_labels
from crew.state.config import Config
from crew.team.formation import build_agent_profile, fast_team_suggestion
from crew.team.roles import CREW_BUILTIN_AGENT_ID, all_role_public_payloads
from crew.team.workspace_guard import classify_external_permission
from crew.tools.registry import Registry


def test_external_runtime_env_inherits_owner_settings_and_blocks_crew_credentials(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    monkeypatch.setenv("HOME", "/Users/test")
    monkeypatch.setenv("JWT", "owner-jwt")
    monkeypatch.setenv("CREW_INTERNAL_SECRET", "secret-value")
    monkeypatch.setenv("SEARCH_PROVIDER_API_KEY", "owner-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://models.example.test/v1")

    env = build_external_runtime_env({
        "RUNTIME_FLAG": "1",
        "OPENAI_API_KEY": "explicit-runtime-key",
        "JWT": "override-must-not-pass",
        "CREW_ENV_FILE": "/private/.env",
        "CREW_RUNTIME_SECRET": "must-not-pass",
    })

    assert env["PATH"] == "/usr/local/bin:/usr/bin"
    assert env["HOME"] == "/Users/test"
    assert env["RUNTIME_FLAG"] == "1"
    assert env["OPENAI_API_KEY"] == "explicit-runtime-key"
    assert env["SEARCH_PROVIDER_API_KEY"] == "owner-secret"
    assert env["ANTHROPIC_API_KEY"] == "anthropic-key"
    assert env["OPENAI_BASE_URL"] == "https://models.example.test/v1"
    assert "JWT" not in env
    assert "CREW_INTERNAL_SECRET" not in env
    assert "CREW_ENV_FILE" not in env
    assert "CREW_RUNTIME_SECRET" not in env


@pytest.mark.asyncio
async def test_external_cli_process_receives_sanitized_runtime_env(monkeypatch, tmp_path):
    script = tmp_path / "capture-runtime-env"
    capture_file = tmp_path / "runtime-env.json"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import os

            with open(os.environ["CAPTURE_FILE"], "w", encoding="utf-8") as handle:
                json.dump({
                    "runtime_flag": os.environ.get("RUNTIME_FLAG"),
                    "provider_key": os.environ.get("ANTHROPIC_API_KEY"),
                    "jwt": os.environ.get("JWT"),
                    "crew_env": os.environ.get("CREW_ENV_FILE"),
                    "crew_secret": os.environ.get("CREW_INTERNAL_SECRET"),
                }, handle)
            print(json.dumps({"text": "done"}))
            """
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("JWT", "owner-jwt")
    monkeypatch.setenv("CREW_ENV_FILE", "/private/.env")
    monkeypatch.setenv("CREW_INTERNAL_SECRET", "secret-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")

    await run_external_cli(
        ExternalCliConfig(
            provider="test",
            executable_path=str(script),
            prompt="unused",
            cwd=str(tmp_path),
            custom_args=["--capture"],
            custom_env={
                "RUNTIME_FLAG": "enabled",
                "CAPTURE_FILE": str(capture_file),
            },
        )
    )

    assert json.loads(capture_file.read_text(encoding="utf-8")) == {
        "runtime_flag": "enabled",
        "provider_key": "anthropic-key",
        "jwt": None,
        "crew_env": None,
        "crew_secret": None,
    }


def test_acp_permission_result_uses_runtime_advertised_option_ids():
    options = (
        {"optionId": "allow_once", "kind": "allow_once", "name": "Allow edit"},
        {"optionId": "deny", "kind": "reject_once", "name": "Deny"},
    )

    assert _permission_result(options, "allow") == {
        "outcome": {"outcome": "selected", "optionId": "allow_once"}
    }
    assert _permission_result(options, "deny") == {
        "outcome": {"outcome": "selected", "optionId": "deny"}
    }


def test_acp_permission_result_never_manufactures_or_broadens_allow_option():
    options = (
        {"optionId": "allow_session", "kind": "allow_always"},
        {"optionId": "deny", "kind": "reject_once"},
    )

    assert _permission_result(options, "allow") == {"outcome": {"outcome": "cancelled"}}
    assert "approve_for_session" not in json.dumps(_permission_result(options, "allow"))


def test_external_permission_guard_adds_exact_attachment_without_write_access(tmp_path):
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    attachment = tmp_path / "uploads" / "template.png"
    attachment.parent.mkdir()
    attachment.write_bytes(b"image")
    forged = tmp_path / "outside.txt"
    forged.write_text("secret", encoding="utf-8")

    guard = _permission_guard(
        {},
        cwd=str(cwd),
        attachments=[
            {"name": "template.png", "path": str(attachment), "type": "image"},
            {"name": "outside.txt", "path": str(forged), "type": "file"},
        ],
        attachment_root=str(attachment.parent),
    )

    assert guard["readable_roots"] == [str(cwd)]
    assert guard["readable_files"] == [str(attachment.resolve())]
    assert guard["writable_roots"] == [str(cwd)]
    assert str(attachment.resolve()) not in guard["writable_roots"]


def test_external_permission_guard_adds_active_skill_as_read_only_root(tmp_path):
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    skill_root = tmp_path / "skills" / "search"
    skill_root.mkdir(parents=True)
    active = SkillActivation(
        skill_id="search",
        name="Search",
        instruction="instructions",
        skill_root=str(skill_root),
    )

    guard = _permission_guard(
        {},
        cwd=str(cwd),
        active_skills=(active,),
    )

    assert str(skill_root) in guard["readable_roots"]
    assert str(skill_root) in guard["allowed_roots"]
    assert guard["writable_roots"] == [str(cwd)]


def test_external_permission_guard_marks_bound_project_deletes_for_confirmation(tmp_path):
    cwd = tmp_path / "project"
    cwd.mkdir()

    guard = _permission_guard(
        {"workspace_root_path": str(cwd)},
        cwd=str(cwd),
    )

    assert guard["readable_roots"] == [str(cwd)]
    assert guard["writable_roots"] == [str(cwd)]
    assert guard["confirm_delete_roots"] == [str(cwd)]


def test_external_permission_guard_allows_reference_read_but_asks_before_overwrite(tmp_path):
    cwd = tmp_path / "project"
    cwd.mkdir()
    source = cwd / "input.xlsx"
    source.write_text("data", encoding="utf-8")
    guard = _permission_guard(
        {
            "workspace_root_path": str(cwd),
            "referenced_paths": [
                {"path": str(source), "resource_type": "file"},
            ],
        },
        cwd=str(cwd),
    )

    read = classify_external_permission({
        "rawInput": {
            "name": "read_file",
            "arguments": {"path": str(source)},
        },
    }, guard, cwd=str(cwd))
    write = classify_external_permission({
        "kind": "edit",
        "rawInput": {
            "name": "write_file",
            "arguments": {"path": str(source), "content": "changed"},
        },
    }, guard, cwd=str(cwd))
    local_command = classify_external_permission({
        "kind": "execute",
        "rawInput": {
            "name": "shell",
            "arguments": {
                "command": f"python analyze.py {source}",
            },
        },
    }, guard, cwd=str(cwd))

    assert read.action == "allow"
    assert write.action == "ask"
    assert write.operation == "write"
    assert local_command.action == "allow"


def test_active_skill_outside_workspace_is_readable_without_prompt(tmp_path):
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    skill_root = tmp_path / "installed-skills" / "sample-search"
    script = skill_root / "scripts" / "search_tool.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('ok')", encoding="utf-8")
    active = SkillActivation(
        skill_id="sample-search",
        name="网页搜索",
        instruction="instructions",
        skill_root=str(skill_root),
    )
    guard = _permission_guard({}, cwd=str(cwd), active_skills=(active,))

    decision = classify_external_permission({
        "kind": "execute",
        "rawInput": {
            "name": "shell",
            "arguments": {
                "command": f"{script} --query 数据合规",
            },
        },
    }, guard, cwd=str(cwd))

    assert decision.action == "allow"
    assert decision.tool_name == "terminal"

    relative = classify_external_permission({
        "kind": "execute",
        "rawInput": {
            "name": "shell",
            "arguments": {
                "command": "./scripts/search_tool.py --query 数据合规",
            },
        },
    }, guard, cwd=str(cwd))
    assert relative.action == "allow"
    assert relative.target.startswith("./scripts/search_tool.py")


def test_permission_question_names_read_and_write_operations(tmp_path):
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    outside = tmp_path / "outside.txt"
    guard = {
        "enabled": True,
        "root": str(cwd),
        "readable_roots": [str(cwd)],
        "writable_roots": [str(cwd)],
    }
    read = classify_external_permission({
        "kind": "execute",
        "rawInput": {
            "name": "shell",
            "arguments": {"command": f"cat {outside}"},
        },
    }, guard, cwd=str(cwd))
    write = classify_external_permission({
        "kind": "execute",
        "rawInput": {
            "name": "shell",
            "arguments": {"command": f"tee {outside}"},
        },
    }, guard, cwd=str(cwd))

    assert read.action == "ask"
    assert read.operation == "read"
    assert write.action == "ask"
    assert write.operation == "write"
    assert "即将执行：读取文件" in _permission_question(
        reason=read.reason,
        tool_name=read.tool_name,
        target=read.target,
        operation=read.operation,
        agent_name="Kimi",
        member_id="",
        node_id="",
    )
    assert "即将执行：写入或修改文件" in _permission_question(
        reason=write.reason,
        tool_name=write.tool_name,
        target=write.target,
        operation=write.operation,
        agent_name="Kimi",
        member_id="",
        node_id="",
    )


@pytest.mark.asyncio
async def test_external_cli_cancellation_terminates_runtime_process_tree(tmp_path):
    script = tmp_path / "slow_external_runtime"
    pid_file = tmp_path / "runtime-pids.json"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import os
            import subprocess
            import sys
            import time

            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            with open(os.environ["PID_FILE"], "w", encoding="utf-8") as handle:
                json.dump({"parent": os.getpid(), "child": child.pid}, handle)
            time.sleep(60)
            """
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    task = asyncio.create_task(run_external_cli(ExternalCliConfig(
        provider="test",
        executable_path=str(script),
        prompt="wait",
        custom_args=["run"],
        custom_env={"PID_FILE": str(pid_file)},
        cwd=str(tmp_path),
        timeout=120,
    )))
    for _ in range(100):
        if pid_file.exists():
            break
        await asyncio.sleep(0.01)
    assert pid_file.exists()
    pids = json.loads(pid_file.read_text(encoding="utf-8"))

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    def alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    for _ in range(100):
        if not any(alive(int(pid)) for pid in pids.values()):
            break
        await asyncio.sleep(0.01)
    assert not any(alive(int(pid)) for pid in pids.values())


@pytest.mark.asyncio
async def test_acp_cancellation_terminates_runtime_process(tmp_path):
    script = tmp_path / "slow_acp_runtime"
    pid_file = tmp_path / "acp-pid.txt"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import os
            import sys
            import time

            with open(os.environ["PID_FILE"], "w", encoding="utf-8") as handle:
                handle.write(str(os.getpid()))
            for line in sys.stdin:
                msg = json.loads(line)
                mid = msg.get("id")
                method = msg.get("method")
                if method == "initialize":
                    result = {"ok": True}
                elif method == "session/new":
                    result = {"sessionId": "slow-session"}
                elif method == "session/prompt":
                    time.sleep(60)
                    result = {"stopReason": "end_turn"}
                else:
                    result = {}
                print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}), flush=True)
            """
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    task = asyncio.create_task(run_acp_prompt("wait", AcpAdapterConfig(
        executable_path=str(script),
        provider="test",
        cwd=str(tmp_path),
        custom_env={"PID_FILE": str(pid_file)},
        timeout=120,
    )))
    for _ in range(100):
        if pid_file.exists():
            break
        await asyncio.sleep(0.01)
    assert pid_file.exists()
    pid = int(pid_file.read_text(encoding="utf-8"))
    await asyncio.sleep(0.05)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def _fake_kimi(tmp_path):
    script = tmp_path / "kimi"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import sys

            if "--version" in sys.argv or "-v" in sys.argv:
                print("kimi 1.2.3")
                raise SystemExit(0)

            for line in sys.stdin:
                msg = json.loads(line)
                mid = msg.get("id")
                method = msg.get("method")
                if method == "initialize":
                    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"ok": True}}), flush=True)
                elif method == "session/new":
                    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {
                        "sessionId": "s1",
                        "models": {
                            "currentModelId": "kimi-code/kimi-for-coding",
                            "availableModels": [
                                {"modelId": "kimi-code/kimi-for-coding", "name": "Kimi for Coding"},
                                {"modelId": "kimi-code/k3", "name": "Kimi K3"}
                            ]
                        }
                    }}), flush=True)
                elif method == "session/set_model":
                    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"ok": True}}), flush=True)
                elif method == "session/prompt":
                    prompt = msg["params"]["prompt"][0]["text"]
                    print(json.dumps({
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": "s1",
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": "Kimi收到: " + prompt[-12:]}
                            }
                        }
                    }), flush=True)
                    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}}), flush=True)
                else:
                    print(json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "nope"}}), flush=True)
            """
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _fake_acp_permission_runtime(tmp_path):
    script = tmp_path / "acp_permission"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import sys

            for line in sys.stdin:
                msg = json.loads(line)
                mid = msg.get("id")
                method = msg.get("method")
                if method == "initialize":
                    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"ok": True}}), flush=True)
                elif method == "session/new":
                    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "perm-s1"}}), flush=True)
                elif method == "session/prompt":
                    print(json.dumps({
                        "jsonrpc": "2.0",
                        "id": 900,
                        "method": "session/request_permission",
                        "params": {
                            "sessionId": "perm-s1",
                            "toolCall": {
                                "id": "edit-1",
                                "kind": "edit",
                                "rawInput": {
                                    "tool": "write_file",
                                    "arguments": {"path": "index.html", "content": "ok"}
                                }
                            },
                            "options": [
                                {"optionId": "allow_once", "kind": "allow_once"},
                                {"optionId": "deny", "kind": "reject_once"}
                            ]
                        }
                    }), flush=True)
                    permission = json.loads(next(sys.stdin))
                    selected = permission.get("result", {}).get("outcome", {}).get("optionId", "")
                    print(json.dumps({
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {"update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": selected}
                        }}
                    }), flush=True)
                    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}}), flush=True)
            """
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


@pytest.mark.asyncio
async def test_acp_permission_handler_returns_runtime_allow_once(tmp_path):
    script = _fake_acp_permission_runtime(tmp_path)
    seen = []

    async def allow(request):
        seen.append(request)
        return "allow"

    output = await run_acp_prompt(
        "create game",
        AcpAdapterConfig(
            executable_path=str(script),
            cwd=str(tmp_path),
            permission_handler=allow,
        ),
    )

    assert output == "allow_once"
    assert seen[0].tool_call["rawInput"]["arguments"]["path"] == "index.html"


def _fake_kimi_streamed_permission_runtime(tmp_path):
    script = tmp_path / "acp_kimi_streamed_permission"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import sys

            for line in sys.stdin:
                msg = json.loads(line)
                mid = msg.get("id")
                method = msg.get("method")
                if method == "initialize":
                    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"ok": True}}), flush=True)
                elif method == "session/new":
                    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "kimi-perm-s1"}}), flush=True)
                elif method == "session/prompt":
                    selected = []
                    for index, path in enumerate(("index.html", "style.css", "game.js"), start=1):
                        call_id = f"{index}:tool_write"
                        print(json.dumps({
                            "jsonrpc": "2.0",
                            "method": "session/update",
                            "params": {"update": {
                                "sessionUpdate": "tool_call",
                                "toolCallId": call_id,
                                "title": "Write",
                                "content": [{"type": "content", "content": {"type": "text", "text": ""}}],
                            }},
                        }), flush=True)
                        print(json.dumps({
                            "jsonrpc": "2.0",
                            "method": "session/update",
                            "params": {"update": {
                                "sessionUpdate": "tool_call_update",
                                "toolCallId": call_id,
                                "content": [{
                                    "type": "content",
                                    "content": {
                                        "type": "text",
                                        "text": json.dumps({"path": path, "content": "test"}),
                                    },
                                }],
                            }},
                        }), flush=True)
                        print(json.dumps({
                            "jsonrpc": "2.0",
                            "id": 900 + index,
                            "method": "session/request_permission",
                            "params": {
                                "sessionId": "kimi-perm-s1",
                                "toolCall": {
                                    "toolCallId": call_id,
                                    "title": "Write",
                                    "content": [{
                                        "type": "content",
                                        "content": {
                                            "type": "text",
                                            "text": f"Requesting approval to Writing {path}",
                                        },
                                    }],
                                },
                                "options": [
                                    {"optionId": "approve_once", "name": "Approve once", "kind": "allow_once"},
                                    {"optionId": "approve_always", "name": "Approve for this session", "kind": "allow_always"},
                                    {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
                                ],
                            },
                        }), flush=True)
                        permission = json.loads(next(sys.stdin))
                        selected.append(permission.get("result", {}).get("outcome", {}).get("optionId", ""))
                    print(json.dumps({
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {"update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": ",".join(selected)},
                        }},
                    }), flush=True)
                    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}}), flush=True)
            """
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


@pytest.mark.asyncio
async def test_kimi_streamed_write_inputs_are_correlated_across_three_permissions(tmp_path):
    script = _fake_kimi_streamed_permission_runtime(tmp_path)
    seen_paths = []

    async def allow(request):
        seen_paths.append(request.tool_call["rawInput"]["arguments"]["path"])
        return "allow"

    output = await run_acp_prompt(
        "create three files",
        AcpAdapterConfig(
            executable_path=str(script),
            cwd=str(tmp_path),
            permission_handler=allow,
        ),
    )

    assert seen_paths == ["index.html", "style.css", "game.js"]
    assert output == "approve_once,approve_once,approve_once"


def _fake_acp_config_options(tmp_path):
    script = tmp_path / "acp_config_options"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import sys

            selected = "model-a"
            for line in sys.stdin:
                msg = json.loads(line)
                mid = msg.get("id")
                method = msg.get("method")
                if method == "initialize":
                    result = {"ok": True}
                elif method == "session/new":
                    result = {
                        "sessionId": "s-config",
                        "configOptions": [{"id": "active-model", "category": "model"}],
                    }
                elif method == "session/set_config_option":
                    params = msg.get("params") or {}
                    if params.get("configId") != "active-model":
                        print(json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": -1, "message": "bad config"}}), flush=True)
                        continue
                    selected = params.get("value") or selected
                    result = {"ok": True}
                elif method == "session/prompt":
                    print(json.dumps({
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": "s-config",
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": "selected:" + selected}
                            }
                        }
                    }), flush=True)
                    result = {"stopReason": "end_turn"}
                else:
                    print(json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "no legacy"}}), flush=True)
                    continue
                print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}), flush=True)
            """
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _fake_kimi_with_cli_catalog(tmp_path):
    script = tmp_path / "kimi_catalog"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import sys

            if "--version" in sys.argv or "-v" in sys.argv:
                print("kimi 0.26.0")
                raise SystemExit(0)
            if sys.argv[1:] == ["provider", "list", "--json"]:
                print(json.dumps({
                    "providers": {"managed:kimi-code": {"type": "kimi"}},
                    "models": {
                        "kimi-code/kimi-for-coding": {
                            "provider": "managed:kimi-code",
                            "displayName": "K2.7 Coding",
                            "capabilities": ["thinking", "tool_use"]
                        },
                        "kimi-code/k3": {
                            "provider": "managed:kimi-code",
                            "displayName": "K3",
                            "capabilities": ["thinking", "tool_use"],
                            "supportEfforts": ["max"]
                        }
                    }
                }))
                raise SystemExit(0)
            if sys.argv[1:] == ["provider", "list"]:
                print("managed:kimi-code  type=kimi  models=2  source=oauth")
                print("Default model: kimi-code/k3")
                raise SystemExit(0)

            for line in sys.stdin:
                msg = json.loads(line)
                mid = msg.get("id")
                method = msg.get("method")
                if method == "initialize":
                    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"ok": True}}), flush=True)
                elif method == "session/new":
                    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "s1"}}), flush=True)
                else:
                    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"ok": True}}), flush=True)
            """
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _fake_acp_with_tool_events(tmp_path):
    script = tmp_path / "kimi_tools"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import sys

            for line in sys.stdin:
                msg = json.loads(line)
                mid = msg.get("id")
                method = msg.get("method")
                if method == "initialize":
                    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"ok": True}}), flush=True)
                elif method == "session/new":
                    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "s1"}}), flush=True)
                elif method == "session/prompt":
                    for update in [
                        {
                            "sessionUpdate": "tool_call",
                            "id": "tool_1",
                            "name": "file_read",
                            "arguments": {"path": "README.md"}
                        },
                        {
                            "sessionUpdate": "tool_result",
                            "id": "tool_1",
                            "name": "file_read",
                            "result": "read ok"
                        },
                        {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "工具完成"}
                        },
                    ]:
                        print(json.dumps({
                            "jsonrpc": "2.0",
                            "method": "session/update",
                            "params": {"sessionId": "s1", "update": update}
                        }), flush=True)
                    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}}), flush=True)
                else:
                    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"ok": True}}), flush=True)
            """
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _fake_cli(tmp_path, name: str, version: str, prefix: str = ""):
    script = tmp_path / name
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import json
            import sys

            if "--version" in sys.argv or "-v" in sys.argv:
                if {prefix!r}:
                    print({prefix!r})
                print("{version}")
                raise SystemExit(0)
            if sys.argv[1:] == ["debug", "models", "--bundled"]:
                print(json.dumps({{"models": [{{
                    "slug": "gpt-test-codex",
                    "display_name": "GPT Test Codex",
                    "is_default": True,
                    "supported_reasoning_levels": [{{"effort": "medium"}}, {{"effort": "high"}}]
                }}]}}))
                raise SystemExit(0)
            print("{name} fake cli")
            """
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_acp_adapter_pairs_anonymous_tool_events():
    client = _JsonRpcClient(SimpleNamespace())
    for update in (
        {"sessionUpdate": "tool_call", "name": "file_read", "arguments": {"path": "README.md"}},
        {"sessionUpdate": "tool_result", "name": "file_read", "result": "read ok"},
    ):
        client._handle_notification({
            "method": "session/update",
            "params": {"update": update},
        })

    started = client.event_queue.get_nowait()
    finished = client.event_queue.get_nowait()
    assert started.tool is not None
    assert finished.tool is not None
    assert started.tool.tool_call_id.startswith("acp_tool_")
    assert finished.tool.tool_call_id == started.tool.tool_call_id


def test_acp_adapter_preserves_external_thinking_events():
    client = _JsonRpcClient(SimpleNamespace())
    client._handle_notification({
        "method": "session/update",
        "params": {
            "update": {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"type": "text", "text": "先检查上游产物。"},
            },
        },
    })

    event = client.event_queue.get_nowait()
    assert event.kind == "thinking"
    assert event.text == "先检查上游产物。"


def test_acp_adapter_accepts_direct_params_and_plain_thinking_text():
    client = _JsonRpcClient(SimpleNamespace())
    client._handle_notification({
        "method": "session/update",
        "params": {
            "sessionUpdate": "agent_thought_chunk",
            "text": "先检查运行环境。",
        },
    })

    event = client.event_queue.get_nowait()
    assert event.kind == "thinking"
    assert event.text == "先检查运行环境。"


def _fake_hermes(tmp_path):
    script = tmp_path / "hermes"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import sys

            if "--version" in sys.argv or "-v" in sys.argv:
                print("Hermes Agent v0.16.0")
                raise SystemExit(0)
            if len(sys.argv) >= 3 and sys.argv[1:3] == ["acp", "--check"]:
                print("ACP OK")
                raise SystemExit(0)
            print("hermes fake cli")
            """
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_scan_kimi_runtime_uses_env_path(tmp_path, monkeypatch):
    kimi = _fake_kimi(tmp_path)
    monkeypatch.setenv("CREW_KIMI_PATH", str(kimi))

    runtime = scan_kimi_runtime()

    assert runtime is not None
    assert runtime.provider == "kimi"
    assert runtime.executable_path == str(kimi)
    assert runtime.version == "kimi 1.2.3"


def test_windows_runtime_search_dirs_and_executable_rules(tmp_path, monkeypatch):
    appdata = tmp_path / "AppData" / "Roaming"
    localappdata = tmp_path / "AppData" / "Local"
    pnpm_home = tmp_path / "pnpm"
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))
    monkeypatch.setenv("PNPM_HOME", str(pnpm_home))

    search_dirs = detector._platform_search_dirs(platform_name="nt")
    assert str(appdata) in search_dirs
    assert str(localappdata / "Programs") in search_dirs
    assert str(localappdata / "Microsoft" / "WindowsApps") in search_dirs
    assert str(pnpm_home) in search_dirs

    command = tmp_path / "agent.cmd"
    command.write_text("@echo off\r\n", encoding="utf-8")
    command.chmod(0o600)
    assert detector._usable_executable(
        str(command),
        platform_name="nt",
    ) == str(command.resolve())
    assert detector._usable_executable(
        str(command),
        platform_name="posix",
    ) is None


def test_process_launcher_uses_platform_specific_isolation(monkeypatch):
    monkeypatch.setattr(process_lifecycle.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    monkeypatch.setattr(process_lifecycle.subprocess, "CREATE_NO_WINDOW", 0x800, raising=False)

    assert process_lifecycle.isolated_process_kwargs(platform_name="nt") == {
        "creationflags": 0xA00,
    }
    assert process_lifecycle.isolated_process_kwargs(platform_name="posix") == {
        "start_new_session": True,
    }


def test_scan_codex_runtime_uses_env_path(tmp_path, monkeypatch):
    codex = _fake_cli(tmp_path, "codex", "codex 0.42.0")
    monkeypatch.setenv("CREW_CODEX_PATH", str(codex))

    runtime = scan_codex_runtime()

    assert runtime is not None
    assert runtime.provider == "codex"
    assert runtime.name == "Codex"
    assert runtime.executable_path == str(codex)
    assert runtime.version == "codex 0.42.0"
    assert runtime.protocol == "cli"
    assert runtime.metadata["descriptor_id"] == "builtin:codex"
    assert runtime.metadata["adapter_id"] == "codex-app-server"


def test_scan_runtimes_returns_kimi_and_codex(tmp_path, monkeypatch):
    kimi = _fake_kimi(tmp_path)
    codex = _fake_cli(tmp_path, "codex", "codex 0.42.0")
    claude = _fake_cli(tmp_path, "claude", "claude 2.0.0")
    hermes = _fake_hermes(tmp_path)
    monkeypatch.setenv("CREW_KIMI_PATH", str(kimi))
    monkeypatch.setenv("CREW_CODEX_PATH", str(codex))
    monkeypatch.setenv("CREW_CLAUDE_ACP_PATH", str(claude))
    monkeypatch.setenv("CREW_HERMES_PATH", str(hermes))

    providers = {row["provider"]: row for row in scan_runtimes()}

    assert providers["kimi"]["protocol"] == "acp"
    assert providers["codex"]["protocol"] == "cli"
    assert providers["claude"]["protocol"] == "acp"
    assert providers["hermes"]["protocol"] == "acp"
    followup = providers["hermes"]["metadata"]["capability_probe"]["followup"]
    assert "mcp__crew-interaction__ask_followup_question" in followup["tool_name_candidates"]
    assert "detail" in followup["option_description_fields"]


def test_builtin_runtime_descriptors_preserve_existing_contracts_and_add_common_acp_agents():
    descriptors = {
        descriptor.provider: descriptor
        for descriptor in runtime_registry.BUILTIN_RUNTIME_DESCRIPTORS
    }

    assert list(descriptors) == [
        "kimi",
        "codex",
        "claude",
        "hermes",
        "kiro",
        "qoder",
        "trae",
        "grok",
        "gemini",
        "qwen",
        "auggie",
        "kilo",
        "mistral-vibe",
        "codex-acp",
        "copilot-acp",
        "claude-code",
    ]
    assert descriptors["kimi"].commands == ("kimi",)
    assert descriptors["kimi"].launch_args == ("acp",)
    assert descriptors["codex"].protocol == "cli"
    assert descriptors["codex"].launch_args == ()
    assert descriptors["claude"].commands == ("claude-agent-acp",)
    assert descriptors["claude"].launch_args == ()
    assert descriptors["hermes"].commands == ("hermes",)
    assert descriptors["hermes"].launch_args == ("acp",)
    assert dict(descriptors["hermes"].probe_env) == {"HERMES_YOLO_MODE": "1"}
    assert {
        provider: descriptor.launch_args
        for provider, descriptor in descriptors.items()
        if provider not in {"kimi", "codex", "claude", "hermes", "claude-code"}
    } == {
        "kiro": ("acp",),
        "qoder": ("--yolo", "--acp"),
        "trae": ("acp", "serve"),
        "grok": ("agent", "stdio"),
        "gemini": ("--experimental-acp",),
        "qwen": ("--acp", "--experimental-skills"),
        "auggie": ("--acp",),
        "kilo": ("acp",),
        "mistral-vibe": (),
        "codex-acp": (),
        "copilot-acp": ("--acp",),
    }
    assert descriptors["claude-code"].commands == ("claude",)
    assert descriptors["claude-code"].adapter_id == "claude-stream-json"
    assert descriptors["codex"].adapter_id == "codex-app-server"
    assert {
        provider: descriptors[provider].display_badge
        for provider in ("kimi", "codex", "hermes", "claude-code")
    } == {
        "kimi": "K",
        "codex": "X",
        "hermes": "H",
        "claude-code": "C",
    }
    assert len({
        descriptor.display_badge
        for descriptor in descriptors.values()
    }) == len(descriptors)
    assert all(
        descriptor.adapter_id == "acp-stdio"
        for descriptor in descriptors.values()
        if descriptor.protocol == "acp"
    )
    assert all(
        descriptor.protocol == "acp"
        for descriptor in descriptors.values()
        if descriptor.provider not in {"codex", "claude-code"}
    )


def test_common_acp_descriptor_uses_generic_scanner(tmp_path, monkeypatch):
    qoder = _fake_cli(tmp_path, "qodercli", "qodercli 1.0.0")
    monkeypatch.setenv("CREW_QODER_PATH", str(qoder))

    descriptor = runtime_registry.builtin_descriptor("qoder")
    candidate = detector.scan_provider_runtime(descriptor)

    assert candidate is not None
    assert candidate.provider == "qoder"
    assert candidate.protocol == "acp"
    assert candidate.launch_args == ("--yolo", "--acp")
    assert candidate.metadata["runtime_descriptor_source"] == "builtin"
    assert candidate.metadata["descriptor_id"] == "builtin:qoder"
    assert candidate.metadata["display_badge"] == "QD"
    assert candidate.metadata["resolution_source"] == "environment"
    assert candidate.metadata["adapter_id"] == "acp-stdio"


def test_runtime_descriptor_catalog_is_static_and_immutable():
    assert runtime_registry.runtime_descriptors() is runtime_registry.BUILTIN_RUNTIME_DESCRIPTORS
    assert runtime_adapter_ids() == (
        "acp-stdio",
        "claude-stream-json",
        "codex-app-server",
    )


@pytest.mark.asyncio
async def test_claude_adapter_exposes_concrete_versioned_model_catalog():
    probe = await ClaudeStreamJsonAdapter().probe(
        "/usr/local/bin/claude",
        provider="claude-code",
    )

    assert probe.default_model_id == "sonnet"
    assert probe.models[0].id == "sonnet"
    assert probe.models[0].label == "Claude Sonnet（当前）"
    assert probe.models[0].default is True
    assert "default" not in {model.id for model in probe.models}
    assert {
        "claude-sonnet-5",
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001",
        "claude-sonnet-4-5",
    }.issubset({model.id for model in probe.models})
    assert probe.model_migrations == {"default": "sonnet"}
    assert probe.source == "claude_static_catalog"


def test_claude_runtime_probe_persists_model_migration_metadata(tmp_path, monkeypatch):
    claude = _fake_cli(tmp_path, "claude", "claude 2.0.0")
    monkeypatch.setenv("CREW_CLAUDE_PATH", str(claude))
    candidate = detector.scan_provider_runtime(
        runtime_registry.builtin_descriptor("claude-code"),
    )

    assert candidate is not None
    profile = asyncio.run(detector.probe_runtime(candidate))

    assert profile.default_model_id == "sonnet"
    assert profile.metadata["model_migrations"] == {"default": "sonnet"}
    assert profile.probe is not None
    assert profile.probe.source == "claude_static_catalog"


def test_runtime_refresh_migrates_only_declared_legacy_agent_model(tmp_path):
    store = ExternalAgentStore(str(tmp_path / "crew.db"))
    old_runtime = store.upsert_runtime({
        "id": "claude-model-migration",
        "provider": "claude-code",
        "name": "Claude Code",
        "executable_path": "/bin/claude",
        "version": "old",
        "protocol": "cli",
        "metadata": {
            "availability_status": "ready",
            "default_model_id": "default",
            "models": [{"id": "default", "label": "CLI 默认模型", "default": True}],
        },
    })
    legacy = store.create_agent(
        name="Legacy Claude",
        runtime_id=old_runtime["id"],
        model="default",
    )

    store.upsert_runtime({
        **old_runtime,
        "version": "new",
        "metadata": {
            "availability_status": "ready",
            "default_model_id": "sonnet",
            "models": [
                {"id": "sonnet", "label": "Claude Sonnet（当前）", "default": True},
                {"id": "claude-sonnet-4-5", "label": "Claude Sonnet 4.5"},
            ],
            "model_migrations": {"default": "sonnet"},
        },
    })

    assert store.get_agent(legacy["id"])["model"] == "sonnet"
def test_runtime_display_badge_reconciles_old_rows_and_unknown_runtimes():
    assert runtime_registry.resolve_runtime_display_badge(
        provider="codex",
        metadata={"display_badge": "OLD"},
    ) == "X"
    assert runtime_registry.resolve_runtime_display_badge(
        provider="legacy-provider",
        metadata={"descriptor_id": "builtin:claude-code"},
    ) == "C"
    assert runtime_registry.resolve_runtime_display_badge(
        provider="custom-runtime",
        metadata={"display_badge": " cr "},
    ) == "CR"
    assert runtime_registry.resolve_runtime_display_badge(
        provider="unknown-runtime",
        metadata={},
    ) == "U"


def test_login_shell_resolution_is_lazy_safe_and_canonical(tmp_path, monkeypatch):
    executable = _fake_cli(tmp_path, "shell-agent", "shell-agent 1.0.0")
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout=f"shell-agent\t{executable}\n")

    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setattr(detector.subprocess, "run", fake_run)

    resolved = detector._login_shell_executables({"shell-agent", "bad command"})

    assert resolved == {"shell-agent": str(executable)}
    assert len(calls) == 1
    assert calls[0][1] == "-ilc"
    assert calls[0][-1] == "shell-agent"
    assert "unalias" in calls[0][2]
    assert "unset -f" in calls[0][2]
    assert "pwd -P" in calls[0][2]


def test_login_shell_resolution_skips_unsupported_shell(monkeypatch):
    monkeypatch.setenv("SHELL", "/usr/local/bin/fish")
    monkeypatch.setattr(
        detector.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("unsupported shell must not be started"),
    )

    assert detector._login_shell_executables({"claude"}) == {}


def test_scan_claude_runtime_uses_env_path(tmp_path, monkeypatch):
    claude = _fake_cli(tmp_path, "claude", "claude 2.0.0")
    monkeypatch.setenv("CREW_CLAUDE_ACP_PATH", str(claude))

    runtime = scan_claude_runtime()

    assert runtime is not None
    assert runtime.provider == "claude"
    assert runtime.executable_path == str(claude)
    assert runtime.version == "claude 2.0.0"
    assert runtime.protocol == "acp"


def test_discover_local_runtimes_probes_acp_and_cli_models(tmp_path, monkeypatch):
    kimi = _fake_kimi(tmp_path)
    codex = _fake_cli(tmp_path, "codex", "codex 0.42.0")
    monkeypatch.setenv("CREW_KIMI_PATH", str(kimi))
    monkeypatch.setenv("CREW_CODEX_PATH", str(codex))
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")

    discovered = {item["provider"]: item for item in asyncio.run(discover_local_runtimes())}

    assert discovered["kimi"]["metadata"]["availability_status"] == "ready"
    assert discovered["kimi"]["metadata"]["default_model_id"] == "kimi-code/kimi-for-coding"
    assert [item["id"] for item in discovered["kimi"]["metadata"]["models"]] == [
        "kimi-code/kimi-for-coding",
        "kimi-code/k3",
    ]
    assert discovered["codex"]["metadata"]["availability_status"] == "ready"
    assert discovered["codex"]["metadata"]["models"][0]["thinking_levels"] == ["medium", "high"]


def test_kimi_acp_falls_back_to_local_cli_model_catalog(tmp_path, monkeypatch):
    kimi = _fake_kimi_with_cli_catalog(tmp_path)
    monkeypatch.setenv("CREW_KIMI_PATH", str(kimi))
    candidate = scan_kimi_runtime()

    assert candidate is not None
    profile = asyncio.run(detector.probe_runtime(candidate))

    assert profile.availability_status == "ready"
    assert profile.default_model_id == "kimi-code/k3"
    assert [model.id for model in profile.models] == [
        "kimi-code/kimi-for-coding",
        "kimi-code/k3",
    ]
    assert profile.models[1].default is True
    assert profile.models[1].thinking_levels == ("max",)
    assert profile.probe is not None
    assert profile.probe.source == "acp_session_new+kimi_provider_list"


def test_scan_hermes_runtime_uses_env_path(tmp_path, monkeypatch):
    hermes = _fake_hermes(tmp_path)
    monkeypatch.setenv("CREW_HERMES_PATH", str(hermes))

    runtime = scan_hermes_runtime()

    assert runtime is not None
    assert runtime.provider == "hermes"
    assert runtime.executable_path == str(hermes)
    assert runtime.protocol == "acp"


def test_scan_codex_runtime_skips_warning_version_lines(tmp_path, monkeypatch):
    codex = _fake_cli(tmp_path, "codex", "codex 0.42.0", prefix="WARNING: path update failed")
    monkeypatch.setenv("CREW_CODEX_PATH", str(codex))

    runtime = scan_codex_runtime()

    assert runtime is not None
    assert runtime.version == "codex 0.42.0"


def test_scan_codex_runtime_uses_desktop_bundle_when_not_on_path(tmp_path, monkeypatch):
    codex = _fake_cli(tmp_path, "codex-bundle", "codex 0.42.0")
    monkeypatch.delenv("CREW_CODEX_PATH", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(detector, "codex_desktop_app_bundle_paths", lambda: [str(codex)])

    runtime = scan_codex_runtime()

    assert runtime is not None
    assert runtime.provider == "codex"
    assert runtime.executable_path == str(codex)


def test_external_agent_store_is_additive(tmp_path):
    db = tmp_path / "crew.db"
    store = ExternalAgentStore(str(db))
    runtime = store.upsert_runtime({
        "id": "kimi_test",
        "provider": "kimi",
        "name": "Kimi",
        "executable_path": "/bin/kimi",
        "version": "1.2.3",
    })
    agent = store.create_agent(name="Kimi Coder", runtime_id=runtime["id"], model="moonshot")

    assert store.list_runtimes()[0]["id"] == "kimi_test"
    assert store.agent_with_runtime(agent["id"])[1]["executable_path"] == "/bin/kimi"
    assert agent["profile"]["agent_id"] == agent["id"]
    assert agent["profile"]["version"] == 3
    assert "backend" in agent["profile"]["capabilities"]
    assert "research" in agent["profile"]["capabilities"]
    backend = agent["profile"]["capabilities"]["backend"]
    assert {"score", "confidence", "evidence"} <= set(backend)


def test_external_runtime_missing_identity_uses_neutral_fallback(tmp_path):
    store = ExternalAgentStore(str(tmp_path / "crew.db"))

    runtime = store.upsert_runtime({
        "id": "unknown-runtime",
        "provider": " ",
        "name": "",
        "executable_path": "/bin/external-agent",
    })

    assert runtime["provider"] == "external"
    assert runtime["name"] == "外援"


def test_external_agents_and_teams_are_owner_private_while_runtime_is_global(tmp_path):
    store = ExternalAgentStore(str(tmp_path / "crew.db"))
    runtime = store.upsert_runtime({
        "id": "shared-runtime",
        "provider": "hermes",
        "name": "Hermes",
        "executable_path": "/bin/hermes",
        "protocol": "acp",
    })
    agent_a = store.create_agent(
        owner_account_id="owner-a",
        name="Agent A",
        runtime_id=runtime["id"],
    )
    agent_b = store.create_agent(
        owner_account_id="owner-b",
        name="Agent B",
        runtime_id=runtime["id"],
    )
    team_a = store.create_team(
        owner_account_id="owner-a",
        name="Team A",
        leader_agent_id=agent_a["id"],
        members=[{"agent_id": agent_a["id"], "role": "Leader"}],
    )

    assert [item["id"] for item in store.list_runtimes()] == [runtime["id"]]
    assert [item["id"] for item in store.list_agents(owner_account_id="owner-a")] == [agent_a["id"]]
    assert [item["id"] for item in store.list_agents(owner_account_id="owner-b")] == [agent_b["id"]]
    assert [item["id"] for item in store.list_teams(owner_account_id="owner-a")] == [team_a["id"]]
    assert store.list_teams(owner_account_id="owner-b") == []
    with pytest.raises(KeyError):
        store.get_agent(agent_a["id"], owner_account_id="owner-b")
    with pytest.raises(KeyError):
        store.get_team(team_a["id"], owner_account_id="owner-b")
    with pytest.raises(KeyError):
        store.create_team(
            owner_account_id="owner-b",
            name="Cross Owner Team",
            leader_agent_id=agent_a["id"],
            members=[{"agent_id": agent_a["id"], "role": "Leader"}],
        )


def test_external_agent_profile_v2_uses_generic_capability_evidence(tmp_path):
    store = ExternalAgentStore(str(tmp_path / "crew.db"))
    runtime = store.upsert_runtime({
        "id": "research_runtime",
        "provider": "kimi",
        "name": "Research Runtime",
        "executable_path": "/bin/kimi",
        "version": "1.2.3",
        "metadata": {"skills": ["文献检索", "研究分析"], "tools": ["web search"]},
    })
    agent = store.create_agent(
        name="研究分析助手",
        runtime_id=runtime["id"],
        system_prompt="检索资料、分析不同观点并汇总结论。",
    )

    profile = agent["profile"]
    assert profile["version"] == 3
    assert profile["capabilities"]["information_retrieval"]["score"] >= 0.7
    assert profile["capabilities"]["research"]["score"] >= 0.7
    assert profile["capabilities"]["analysis"]["score"] >= 0.7
    sources = {
        evidence["source"]
        for evidence in profile["capabilities"]["research"]["evidence"]
    }
    assert "runtime_skill" in sources


def test_agent_profile_tracks_selected_runtime_model_binding(tmp_path):
    store = ExternalAgentStore(str(tmp_path / "crew.db"))
    runtime = store.upsert_runtime({
        "id": "model_runtime",
        "provider": "kimi",
        "name": "Kimi",
        "executable_path": "/bin/kimi",
        "protocol": "acp",
        "metadata": {
            "availability_status": "ready",
            "default_model_id": "kimi/default",
            "models": [{
                "id": "kimi/default",
                "label": "Kimi Default",
                "default": True,
                "capabilities": ["text", "tools"],
            }],
        },
    })
    agent = store.create_agent(
        name="Kimi Model Agent",
        runtime_id=runtime["id"],
        model="kimi/default",
    )

    assert agent["profile"]["version"] == 3
    assert agent["profile"]["model"] == {
        "id": "kimi/default",
        "label": "Kimi Default",
        "binding_status": "valid",
        "capabilities": ["text", "tools"],
        "thinking_levels": [],
    }

    store.upsert_runtime({
        **runtime,
        "metadata": {
            "availability_status": "ready",
            "default_model_id": "kimi/new",
            "models": [{"id": "kimi/new", "label": "Kimi New", "default": True}],
        },
    })
    assert store.get_agent(agent["id"])["profile"]["model"]["binding_status"] == "missing"

    store.upsert_runtime({
        **runtime,
        "metadata": {
            "availability_status": "degraded",
            "models": [],
            "probe": {"error_code": "probe_failed"},
        },
    })
    assert store.get_agent(agent["id"])["profile"]["model"]["binding_status"] == "unverified"


def test_runtime_sync_only_marks_discovery_managed_runtime_unavailable(tmp_path):
    store = ExternalAgentStore(str(tmp_path / "crew.db"))
    store.upsert_runtime({
        "id": "manual-runtime",
        "provider": "custom",
        "name": "Manual",
        "executable_path": "/bin/sh",
        "protocol": "cli",
        "metadata": {"availability_status": "ready"},
    })
    store.upsert_runtime({
        "id": "discovered-runtime",
        "provider": "codex",
        "name": "Codex",
        "executable_path": "/bin/sh",
        "protocol": "cli",
        "metadata": {
            "runtime_profile_version": 1,
            "availability_status": "ready",
            "models": [{"id": "model-a", "label": "Model A"}],
        },
    })

    runtimes = {runtime["id"]: runtime for runtime in store.sync_runtimes([])}

    assert runtimes["manual-runtime"]["metadata"]["availability_status"] == "ready"
    assert runtimes["discovered-runtime"]["metadata"]["availability_status"] == "unavailable"


def test_runtime_sync_rebinds_agents_and_retires_replaced_installation(tmp_path):
    store = ExternalAgentStore(str(tmp_path / "crew.db"))
    old_runtime = store.upsert_runtime({
        "id": "hermes-old-path",
        "provider": "hermes",
        "name": "Hermes",
        "executable_path": "/old/bin/hermes",
        "protocol": "acp",
        "metadata": {
            "runtime_profile_version": 1,
            "runtime_descriptor_source": "builtin",
            "adapter_id": "acp-stdio",
            "availability_status": "ready",
            "default_model_id": "hermes/default",
            "models": [{"id": "hermes/default", "label": "Hermes Default", "default": True}],
        },
    })
    agent = store.create_agent(
        owner_account_id="owner-a",
        name="Hermes Agent",
        runtime_id=old_runtime["id"],
        model="hermes/default",
    )
    team = store.create_team(
        owner_account_id="owner-a",
        name="Hermes Team",
        leader_agent_id=agent["id"],
        members=[{"agent_id": agent["id"], "role": "Leader"}],
    )
    store.save_runtime_session_binding(
        owner_account_id="owner-a",
        crew_session_id="crew-hermes",
        external_agent_id=agent["id"],
        runtime_id=old_runtime["id"],
        adapter_id="acp-stdio",
        native_session_id="native-old",
    )
    new_runtime = {
        "id": "hermes-new-path",
        "provider": "hermes",
        "name": "Hermes",
        "executable_path": "/new/venv/bin/hermes",
        "protocol": "acp",
        "metadata": {
            "runtime_profile_version": 1,
            "runtime_descriptor_source": "builtin",
            "descriptor_id": "builtin:hermes",
            "adapter_id": "acp-stdio",
            "availability_status": "ready",
            "default_model_id": "hermes/default",
            "models": [{"id": "hermes/default", "label": "Hermes Default", "default": True}],
        },
    }

    runtimes = {runtime["id"]: runtime for runtime in store.sync_runtimes([new_runtime])}

    migrated_agent = store.get_agent(agent["id"], owner_account_id="owner-a")
    assert migrated_agent["runtime_id"] == "hermes-new-path"
    assert migrated_agent["profile"]["availability"] == "ready"
    migrated_team = store.get_team(team["id"], owner_account_id="owner-a")
    assert migrated_team["leader_agent_id"] == agent["id"]
    assert migrated_team["members"][0]["agent_id"] == agent["id"]
    assert runtimes["hermes-old-path"]["metadata"]["lifecycle_status"] == "replaced"
    assert runtimes["hermes-old-path"]["metadata"]["replaced_by_runtime_id"] == "hermes-new-path"
    assert runtimes["hermes-new-path"]["metadata"]["replaces_runtime_ids"] == ["hermes-old-path"]
    assert store.get_runtime_session_binding(
        owner_account_id="owner-a",
        crew_session_id="crew-hermes",
        external_agent_id=agent["id"],
        runtime_id=old_runtime["id"],
        adapter_id="acp-stdio",
    ) is None


def test_runtime_sync_does_not_guess_between_multiple_replacements(tmp_path):
    store = ExternalAgentStore(str(tmp_path / "crew.db"))
    old_runtime = store.upsert_runtime({
        "id": "hermes-old-path",
        "provider": "hermes",
        "name": "Hermes",
        "executable_path": "/old/bin/hermes",
        "protocol": "acp",
        "metadata": {
            "runtime_profile_version": 1,
            "runtime_descriptor_source": "builtin",
            "availability_status": "ready",
        },
    })
    agent = store.create_agent(
        name="Hermes Agent",
        runtime_id=old_runtime["id"],
    )

    def replacement(runtime_id: str, path: str) -> dict:
        return {
            "id": runtime_id,
            "provider": "hermes",
            "name": "Hermes",
            "executable_path": path,
            "protocol": "acp",
            "metadata": {
                "runtime_profile_version": 1,
                "runtime_descriptor_source": "builtin",
                "descriptor_id": "builtin:hermes",
                "availability_status": "ready",
            },
        }

    runtimes = {
        runtime["id"]: runtime
        for runtime in store.sync_runtimes([
            replacement("hermes-new-a", "/new/a/hermes"),
            replacement("hermes-new-b", "/new/b/hermes"),
        ])
    }

    assert store.get_agent(agent["id"])["runtime_id"] == "hermes-old-path"
    assert runtimes["hermes-old-path"]["metadata"]["lifecycle_status"] == "missing"
    assert "replaced_by_runtime_id" not in runtimes["hermes-old-path"]["metadata"]


def test_runtime_sync_retires_legacy_path_when_unique_replacement_is_degraded(tmp_path):
    store = ExternalAgentStore(str(tmp_path / "crew.db"))
    old_runtime = store.upsert_runtime({
        "id": "hermes-old-path",
        "provider": "hermes",
        "name": "Hermes",
        "executable_path": "/old/bin/hermes",
        "protocol": "acp",
        "metadata": {
            "runtime_profile_version": 1,
            "availability_status": "ready",
            "default_model_id": "hermes/default",
            "models": [{"id": "hermes/default", "label": "Hermes Default", "default": True}],
        },
    })
    agent = store.create_agent(
        owner_account_id="owner-a",
        name="Hermes Agent",
        runtime_id=old_runtime["id"],
        model="hermes/default",
    )
    degraded_runtime = {
        "id": "hermes-new-path",
        "provider": "hermes",
        "name": "Hermes",
        "executable_path": "/new/venv/bin/hermes",
        "protocol": "acp",
        "metadata": {
            "runtime_profile_version": 1,
            "runtime_descriptor_source": "builtin",
            "descriptor_id": "builtin:hermes",
            "adapter_id": "acp-stdio",
            "availability_status": "degraded",
            "models": [],
            "probe": {
                "error_code": "probe_failed",
                "message": "hermes 运行时探测超时",
            },
        },
    }

    runtimes = {runtime["id"]: runtime for runtime in store.sync_runtimes([degraded_runtime])}

    migrated_agent = store.get_agent(agent["id"], owner_account_id="owner-a")
    assert migrated_agent["runtime_id"] == "hermes-new-path"
    assert migrated_agent["profile"]["availability"] == "degraded"
    assert runtimes["hermes-old-path"]["metadata"]["lifecycle_status"] == "replaced"
    assert runtimes["hermes-old-path"]["metadata"]["replaced_by_runtime_id"] == "hermes-new-path"
    assert runtimes["hermes-new-path"]["metadata"]["replaces_runtime_ids"] == ["hermes-old-path"]


def test_team_auto_selection_skips_agent_with_missing_runtime_model():
    ready_runtime = {
        "metadata": {
            "availability_status": "ready",
            "models": [{"id": "model-ready", "label": "Ready"}],
        },
    }
    missing_agent = {
        "id": "agent-missing",
        "name": "Missing Model",
        "runtime_id": "rt",
        "model": "model-removed",
        "capabilities": {"backend": 1.0},
    }
    ready_agent = {
        "id": "agent-ready",
        "name": "Ready Model",
        "runtime_id": "rt",
        "model": "model-ready",
        "capabilities": {"backend": 0.8},
    }
    missing_agent["profile"] = build_agent_profile(missing_agent, runtime=ready_runtime).to_dict()
    ready_agent["profile"] = build_agent_profile(ready_agent, runtime=ready_runtime).to_dict()

    suggestion = fast_team_suggestion(
        {
            "name": "后端开发团队",
            "description": "实现后端接口",
            "leader_agent_id": CREW_BUILTIN_AGENT_ID,
            "required_capabilities": ["backend"],
        },
        [missing_agent, ready_agent],
    )

    member_ids = {member["agent_id"] for member in suggestion["members"]}
    assert "agent-ready" in member_ids
    assert "agent-missing" not in member_ids


def test_external_agent_store_refreshes_v1_profiles_without_schema_change(tmp_path):
    db = tmp_path / "crew.db"
    store = ExternalAgentStore(str(db))
    runtime = store.upsert_runtime({
        "id": "legacy_runtime",
        "provider": "kimi",
        "name": "Kimi",
        "executable_path": "/bin/kimi",
        "version": "1.2.3",
    })
    agent = store.create_agent(name="Legacy Agent", runtime_id=runtime["id"])
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE external_agent SET profile_json = ?, profile_version = 1 WHERE id = ?",
            (json.dumps({
                "version": 1,
                "agent_id": agent["id"],
                "capabilities": {"backend": {"score": 0.8, "confidence": 0.8, "evidence": []}},
            }), agent["id"]),
        )

    refreshed = ExternalAgentStore(str(db)).get_agent(agent["id"])

    assert refreshed["profile_version"] == 3
    assert refreshed["profile"]["version"] == 3
    assert "information_retrieval" in refreshed["profile"]["capabilities"]


def test_external_team_persists_independent_formation_plan(tmp_path):
    store = ExternalAgentStore(str(tmp_path / "crew.db"))
    runtime = store.upsert_runtime({
        "id": "kimi_test",
        "provider": "kimi",
        "name": "Kimi",
        "executable_path": "/bin/kimi",
        "version": "1.2.3",
    })
    agent = store.create_agent(name="Kimi Coder", runtime_id=runtime["id"])
    team_spec = {"version": 3, "goal": "开发接口", "required_capabilities": ["backend"]}
    formation_plan = {
        "version": 1,
        "leader_agent_id": agent["id"],
        "members": [{
            "agent_id": agent["id"],
            "role_key": "backend_developer",
            "assigned_capabilities": ["backend"],
            "responsibility_markdown": "负责后端实现",
        }],
        "coverage": {"required": ["backend"], "covered": ["backend"], "uncovered": []},
    }

    team = store.create_team(
        name="后端团队",
        leader_agent_id=agent["id"],
        team_spec=team_spec,
        formation_plan=formation_plan,
        members=[{
            "agent_id": agent["id"],
            "role": "负责后端实现",
            "role_key": "backend_developer",
            "assigned_capabilities": ["backend"],
        }],
    )

    assert team["team_spec"] == team_spec
    assert "formation" not in team["team_spec"]
    assert team["formation_plan"] == formation_plan
    assert team["members"][0]["assigned_capabilities"] == ["backend"]


def test_store_migrates_legacy_embedded_formation_plan(tmp_path):
    db = tmp_path / "crew.db"
    store = ExternalAgentStore(str(db))
    runtime = store.upsert_runtime({
        "id": "kimi_test",
        "provider": "kimi",
        "name": "Kimi",
        "executable_path": "/bin/kimi",
        "version": "1.2.3",
    })
    agent = store.create_agent(name="Kimi Coder", runtime_id=runtime["id"])
    team = store.create_team(
        name="旧团队",
        leader_agent_id=agent["id"],
        members=[{
            "agent_id": agent["id"],
            "role": "负责后端",
            "role_key": "backend_developer",
            "assigned_capabilities": ["backend"],
        }],
    )
    legacy_spec = {
        "version": 2,
        "goal": "开发接口",
        "formation": {
            "leader_agent_id": agent["id"],
            "assignments": [{"agent_id": agent["id"], "source": "user", "locked": True}],
            "required_capabilities": ["backend"],
            "confidence": 1.0,
        },
    }
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE external_team SET team_spec_json = ?, formation_plan_json = '{}' WHERE id = ?",
            (json.dumps(legacy_spec), team["id"]),
        )

    migrated = ExternalAgentStore(str(db)).get_team(team["id"])

    assert "formation" not in migrated["team_spec"]
    assert migrated["formation_plan"]["leader_agent_id"] == agent["id"]
    assert migrated["formation_plan"]["coverage"]["covered"] == ["backend"]


def test_role_markdown_is_complete():
    role = role_markdown(
        "Leader 职责",
        {"name": "Leader", "provider": "kimi"},
        "Leader 拆解任务并协调成员",
        "开发一个异构智能体团队",
        True,
    )

    assert "开发一个异构智能体团队" in role
    assert "工作原则" in role
    assert "团队协作关系" in role
    assert "风险/阻塞" in role


def test_delete_team_archives_team_and_unblocks_agent_delete(tmp_path):
    store = ExternalAgentStore(str(tmp_path / "crew.db"))
    runtime = store.upsert_runtime({
        "id": "kimi_test",
        "provider": "kimi",
        "name": "Kimi",
        "executable_path": "/bin/kimi",
        "version": "1.2.3",
    })
    agent = store.create_agent(name="Kimi Leader", runtime_id=runtime["id"])
    team = store.create_team(
        name="研发团队",
        leader_agent_id=agent["id"],
        members=[{"agent_id": agent["id"], "role": "Leader"}],
    )

    store.delete_team(team["id"])

    assert store.list_teams() == []
    store.delete_agent(agent["id"])
    assert store.list_agents() == []


def test_team_role_presets_and_suggestion_are_goal_aware():
    roles = all_role_public_payloads()
    assert any(role["key"] == "qa_engineer" for role in roles)

    suggestion = suggest_role_description({
        "name": "像素风小游戏团队",
        "description": "写一个贪吃蛇小游戏，像素风",
        "workflow": "开发完成后测试核心玩法",
        "agent_name": "QA",
        "role_key": "qa_engineer",
    })

    assert suggestion["key"] == "qa_engineer"
    assert suggestion["workflow_lane"] == "verify"
    assert "贪吃蛇小游戏" in suggestion["role"]
    assert "测试路径" in suggestion["role"]


def test_external_team_member_role_metadata_persists(tmp_path):
    store = ExternalAgentStore(str(tmp_path / "crew.db"))
    runtime = store.upsert_runtime({
        "id": "kimi_test",
        "provider": "kimi",
        "name": "Kimi",
        "executable_path": "/bin/kimi",
        "version": "1.2.3",
    })
    developer = store.create_agent(name="Dev Agent", runtime_id=runtime["id"])
    tester = store.create_agent(name="QA Agent", runtime_id=runtime["id"])
    team = store.create_team(
        name="研发团队",
        leader_agent_id=developer["id"],
        members=[
            {
                "agent_id": developer["id"],
                "role": "负责开发小游戏",
                "role_key": "frontend_developer",
                "role_label": "前端开发",
                "capabilities": ["frontend", "browser"],
                "workflow_lane": "build",
            },
            {
                "agent_id": tester["id"],
                "role": "负责测试小游戏",
                "role_key": "qa_engineer",
            },
        ],
    )

    dev_member = next(member for member in team["members"] if member["agent_id"] == developer["id"])
    qa_member = next(member for member in team["members"] if member["agent_id"] == tester["id"])

    assert dev_member["role_key"] == "frontend_developer"
    assert dev_member["role_label"] == "前端开发"
    assert dev_member["capabilities"] == ["frontend"]
    assert dev_member["workflow_lane"] == "build"
    assert qa_member["role_key"] == "qa_engineer"
    assert "testing" in qa_member["capabilities"]
    assert "verification" in qa_member["capabilities"]
    assert qa_member["workflow_lane"] == "verify"


def test_external_team_accepts_crew_builtin_as_leader_and_member(tmp_path):
    store = ExternalAgentStore(str(tmp_path / "crew.db"))
    runtime = store.upsert_runtime({
        "id": "kimi_test",
        "provider": "kimi",
        "name": "Kimi",
        "executable_path": "/bin/kimi",
        "version": "1.2.3",
    })
    writer = store.create_agent(name="Kimi Writer", runtime_id=runtime["id"])

    team = store.create_team(
        name="内置 Crew 协作团队",
        leader_agent_id=CREW_BUILTIN_AGENT_ID,
        members=[
            {"agent_id": CREW_BUILTIN_AGENT_ID, "role": "负责拆解、派活和汇总", "role_key": "tech_lead"},
            {"agent_id": writer["id"], "role": "负责文字整理", "role_key": "technical_writer"},
        ],
    )

    builtin = next(member for member in team["members"] if member["agent_id"] == CREW_BUILTIN_AGENT_ID)
    assert team["leader_agent_id"] == CREW_BUILTIN_AGENT_ID
    assert builtin["agent_name"] == "Crew 内置智能体"
    assert builtin["agent_provider"] == "crew"
    assert builtin["role_key"] == "tech_lead"

    reloaded = store.get_team(team["id"])
    assert any(member["agent_id"] == CREW_BUILTIN_AGENT_ID for member in reloaded["members"])


async def test_delegate_to_external_agent_runs_kimi_via_acp(tmp_path):
    kimi = _fake_kimi(tmp_path)
    store = ExternalAgentStore(str(tmp_path / "crew.db"))
    runtime = store.upsert_runtime({
        "id": "kimi_test",
        "provider": "kimi",
        "name": "Kimi",
        "executable_path": str(kimi),
        "version": "1.2.3",
    })
    agent = store.create_agent(
        name="Kimi Coder",
        runtime_id=runtime["id"],
        model="moonshot",
        system_prompt="你是 Kimi 子智能体",
    )
    registry = Registry()
    register_external_agent_tools(registry, store)

    result = await registry.execute(ToolCall("c1", "delegate_to_external_agent", {
        "agent_id": agent["id"],
        "prompt": "请回答 hello",
        "cwd": str(tmp_path),
    }))

    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["provider"] == "kimi"
    assert "Kimi收到" in payload["output"]


async def test_acp_prompt_reports_early_process_stderr(tmp_path):
    script = tmp_path / "broken_acp"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import sys
            print("ACP dependencies not installed.", file=sys.stderr)
            print("Install them with: pip install -e '.[acp]'", file=sys.stderr)
            raise SystemExit(1)
            """
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    try:
        await run_acp_prompt(
            "hello",
            AcpAdapterConfig(
                executable_path=str(script),
                cwd=str(tmp_path),
                timeout=2,
            ),
        )
    except AcpAdapterError as exc:
        text = str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected AcpAdapterError")

    assert "ACP process exited before responding" in text
    assert "ACP dependencies not installed" in text


async def test_acp_session_new_sends_mcp_servers_in_camel_and_snake_case(tmp_path):
    script = tmp_path / "strict_acp"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import sys

            for line in sys.stdin:
                msg = json.loads(line)
                mid = msg.get("id")
                method = msg.get("method")
                if method == "initialize":
                    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"ok": True}}), flush=True)
                elif method == "session/new":
                    params = msg.get("params") or {}
                    servers = [{"name": "crew-interaction", "command": "python", "args": []}]
                    if params.get("mcpServers") != servers or params.get("mcp_servers") != servers:
                        print(json.dumps({
                            "jsonrpc": "2.0",
                            "id": mid,
                            "error": {"code": -32602, "message": "missing compatible MCP fields"}
                        }), flush=True)
                        continue
                    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "s1"}}), flush=True)
                elif method == "session/prompt":
                    print(json.dumps({
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": "s1",
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": "ok"}
                            }
                        }
                    }), flush=True)
                    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}}), flush=True)
                else:
                    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"ok": True}}), flush=True)
            """
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    result = await run_acp_prompt(
        "hello",
        AcpAdapterConfig(
            executable_path=str(script),
            cwd=str(tmp_path),
            timeout=2,
            mcp_servers=[{"name": "crew-interaction", "command": "python", "args": []}],
        ),
    )

    assert result == "ok"


def test_build_session_new_params_documents_acp_mcp_compatibility():
    servers = [{"name": "crew-interaction", "command": "python", "args": []}]

    params = _build_session_new_params("/tmp/project", servers)

    assert params == {
        "cwd": "/tmp/project",
        "mcpServers": servers,
        "mcp_servers": servers,
    }


def test_build_session_resume_params_documents_acp_session_binding():
    servers = [{"name": "crew-interaction", "command": "python", "args": []}]

    params = _build_session_resume_params("/tmp/project", "s1", servers)

    assert params == {
        "cwd": "/tmp/project",
        "sessionId": "s1",
        "session_id": "s1",
        "mcpServers": servers,
        "mcp_servers": servers,
    }


def test_external_store_saves_acp_session_binding(tmp_path):
    store = ExternalAgentStore(str(tmp_path / "crew.db"))
    runtime = store.upsert_runtime({
        "id": "hermes_runtime",
        "provider": "hermes",
        "name": "Hermes",
        "executable_path": "/bin/hermes",
        "version": "1.0.0",
        "protocol": "acp",
    })
    agent = store.create_agent(name="Hermes", runtime_id=runtime["id"])

    store.save_acp_session_binding(
        crew_session_id="crew_s1",
        external_agent_id=agent["id"],
        runtime_id=runtime["id"],
        provider="hermes",
        cwd=str(tmp_path),
        acp_session_id="h1",
    )

    binding = store.get_acp_session_binding(
        crew_session_id="crew_s1",
        external_agent_id=agent["id"],
        runtime_id=runtime["id"],
        provider="hermes",
        cwd=str(tmp_path),
    )

    assert binding is not None
    assert binding["acp_session_id"] == "h1"


def test_external_store_scopes_acp_session_binding_by_owner(tmp_path):
    store = ExternalAgentStore(str(tmp_path / "crew.db"))
    runtime = store.upsert_runtime({
        "id": "hermes_runtime",
        "provider": "hermes",
        "name": "Hermes",
        "executable_path": "/bin/hermes",
        "version": "1.0.0",
        "protocol": "acp",
    })
    agent = store.create_agent(name="Hermes", runtime_id=runtime["id"])
    key = {
        "crew_session_id": "same_session",
        "external_agent_id": agent["id"],
        "runtime_id": runtime["id"],
        "provider": "hermes",
        "cwd": str(tmp_path),
    }

    store.save_acp_session_binding(owner_account_id="A:uid-a", acp_session_id="a1", **key)
    store.save_acp_session_binding(owner_account_id="B:uid-b", acp_session_id="b1", **key)

    binding_a = store.get_acp_session_binding(owner_account_id="A:uid-a", **key)
    binding_b = store.get_acp_session_binding(owner_account_id="B:uid-b", **key)
    assert binding_a is not None
    assert binding_b is not None
    assert binding_a["acp_session_id"] == "a1"
    assert binding_b["acp_session_id"] == "b1"

    assert store.delete_acp_bindings_for_session(
        "same_session",
        owner_account_id="A:uid-a",
    ) == 1
    assert store.get_acp_session_binding(owner_account_id="A:uid-a", **key) is None
    assert store.get_acp_session_binding(owner_account_id="B:uid-b", **key) is not None


async def test_acp_resume_drops_replayed_history_before_current_prompt(tmp_path):
    script = tmp_path / "resume_acp"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import sys

            session_id = "old_s1"
            for line in sys.stdin:
                msg = json.loads(line)
                mid = msg.get("id")
                method = msg.get("method")
                if method == "initialize":
                    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"ok": True}}), flush=True)
                elif method == "session/resume":
                    print(json.dumps({
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": session_id,
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": "历史回放不应展示"}
                            }
                        }
                    }), flush=True)
                    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": session_id}}), flush=True)
                elif method == "session/prompt":
                    print(json.dumps({
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": session_id,
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": "当前轮回答"}
                            }
                        }
                    }), flush=True)
                    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}}), flush=True)
                else:
                    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"ok": True}}), flush=True)
            """
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    events = [
        event async for event in stream_acp_events(
            "继续",
            AcpAdapterConfig(
                executable_path=str(script),
                cwd=str(tmp_path),
                resume_session_id="old_s1",
                timeout=2,
            ),
        )
    ]

    assert [event.text for event in events if event.kind == "text"] == ["当前轮回答"]
    session_events = [event for event in events if event.kind == "session"]
    assert session_events[-1].session_id == "old_s1"
    assert session_events[-1].session_resumed is True


async def test_acp_adapter_uses_idle_timeout_for_active_stream(tmp_path):
    script = tmp_path / "kimi_active"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import sys
            import time

            for line in sys.stdin:
                msg = json.loads(line)
                mid = msg.get("id")
                method = msg.get("method")
                if method == "initialize":
                    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"ok": True}}), flush=True)
                elif method == "session/new":
                    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "s1"}}), flush=True)
                elif method == "session/prompt":
                    for text in ["step 1", "step 2", "done"]:
                        time.sleep(0.15)
                        print(json.dumps({
                            "jsonrpc": "2.0",
                            "method": "session/update",
                            "params": {
                                "sessionId": "s1",
                                "update": {
                                    "sessionUpdate": "agent_message_chunk",
                                    "content": {"type": "text", "text": text}
                                }
                            }
                        }), flush=True)
                    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}}), flush=True)
                else:
                    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"ok": True}}), flush=True)
            """
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    events = [
        event async for event in stream_acp_events(
            "继续",
            AcpAdapterConfig(
                executable_path=str(script),
                cwd=str(tmp_path),
                timeout=0.4,
            ),
        )
    ]

    assert [event.text for event in events if event.kind == "text"] == ["step 1", "step 2", "done"]


async def test_acp_adapter_idle_timeout_names_provider_and_last_activity(tmp_path):
    script = tmp_path / "kimi_hangs_after_tool"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import sys
            import time

            for line in sys.stdin:
                msg = json.loads(line)
                mid = msg.get("id")
                method = msg.get("method")
                if method == "initialize":
                    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"ok": True}}), flush=True)
                elif method == "session/new":
                    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "s1"}}), flush=True)
                elif method == "session/prompt":
                    print(json.dumps({
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": "s1",
                            "update": {
                                "sessionUpdate": "toolCallResult",
                                "toolCallId": "t1",
                                "name": "Edit",
                                "content": {"type": "text", "text": "Replaced 1 occurrence in game.js"}
                            }
                        }
                    }), flush=True)
                    time.sleep(2)
                else:
                    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"ok": True}}), flush=True)
            """
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    try:
        events = [
            event async for event in stream_acp_events(
                "继续",
                AcpAdapterConfig(
                    executable_path=str(script),
                    provider="kimi",
                    cwd=str(tmp_path),
                    timeout=0.8,
                ),
            )
        ]
    except AcpAdapterError as exc:
        text = str(exc)
    else:  # pragma: no cover
        raise AssertionError(f"expected AcpAdapterError, got {events}")

    assert "kimi 模型响应空闲超时" in text
    assert "last_activity=工具 Edit result" in text
    assert "game.js" in text


def test_external_system_prompt_explains_cli_followup_limit():
    prompt = _external_system_prompt(
        {"name": "Hermes", "provider": "hermes"},
        {"name": "Hermes", "provider": "hermes", "protocol": "cli"},
        "kimi-code/k3",
    )

    assert "Crew 当前连接的单个外部智能体" in prompt
    assert "model=kimi-code/k3" in prompt
    assert "团队模式" not in prompt
    assert "不要假装已经获得用户答案" in prompt
    assert "CLI runtime" in prompt
    assert "不会注入 Crew Interaction MCP" in prompt
    assert "不要声称原因是会话处于 Plan 模式" in prompt
    assert "team_plan_create" not in prompt


async def test_acp_executor_runs_codex_cli(tmp_path):
    codex = _fake_cli(tmp_path, "codex", "codex 0.42.0")
    store = ExternalAgentStore(str(tmp_path / "crew.db"))
    runtime = store.upsert_runtime({
        "id": "codex_test",
        "provider": "codex",
        "name": "Codex",
        "executable_path": str(codex),
        "version": "0.42.0",
        "protocol": "cli",
    })
    agent = store.create_agent(name="Codex Coder", runtime_id=runtime["id"])
    ctx = ExecutionContext(
        session_id="s",
        request_id="r",
        system_prompt="",
        messages=[],
        query="hello codex",
        cwd=str(tmp_path),
    )

    chunks = [
        ch async for ch in AcpExecutor({
            "external_agent_id": agent["id"],
            "external_store": store,
        }).execute(ctx)
    ]

    assert chunks[-1].kind == "final"
    assert "codex fake cli" in chunks[-1].body["text"]


async def test_acp_executor_prefers_session_model_override(tmp_path, monkeypatch):
    from crew.agent.executor import external as external_executor

    captured = {}

    async def fake_run(config):
        captured["model"] = config.model
        return "ok"

    monkeypatch.setattr(external_executor, "run_external_cli", fake_run)
    store = ExternalAgentStore(str(tmp_path / "crew.db"))
    runtime = store.upsert_runtime({
        "id": "codex-session-model",
        "provider": "codex",
        "name": "Codex",
        "executable_path": "codex",
        "version": "test",
        "protocol": "cli",
    })
    agent = store.create_agent(
        name="Codex Session Model",
        runtime_id=runtime["id"],
        model="agent-default",
    )
    ctx = ExecutionContext(
        session_id="s",
        request_id="r",
        system_prompt="",
        messages=[],
        query="hello",
        cwd=str(tmp_path),
    )

    chunks = [
        chunk async for chunk in AcpExecutor({
            "external_agent_id": agent["id"],
            "external_store": store,
            "model": "session-override",
        }).execute(ctx)
    ]

    assert chunks[-1].kind == "final"
    assert captured["model"] == "session-override"
    assert ctx.messages[-1].model == "session-override"


async def test_cli_executor_rewrites_missing_followup_tool_as_cli_limit(tmp_path, monkeypatch):
    from crew.agent.executor import external as external_executor

    async def fake_run(_config):
        return "我当前没有 ask_followup_question 工具，可能需要切换 Plan 模式。"

    monkeypatch.setattr(external_executor, "run_external_cli", fake_run)
    store = ExternalAgentStore(str(tmp_path / "crew.db"))
    runtime = store.upsert_runtime({
        "id": "codex-cli-followup",
        "provider": "codex",
        "name": "Codex",
        "executable_path": "codex",
        "version": "test",
        "protocol": "cli",
    })
    agent = store.create_agent(name="Codex CLI", runtime_id=runtime["id"])
    ctx = ExecutionContext(
        session_id="s",
        request_id="r",
        system_prompt="",
        messages=[],
        query="请使用 ask_followup_question 弹出选择框",
        cwd=str(tmp_path),
    )

    chunks = [
        chunk async for chunk in AcpExecutor({
            "external_agent_id": agent["id"],
            "external_store": store,
        }).execute(ctx)
    ]

    output = chunks[-1].body["text"]
    assert "当前 Crew 通过 CLI runtime" in output
    assert "不是因为会话处于 Plan 模式" in output
    assert "可能需要切换 Plan 模式" not in output


async def test_codex_cli_detaches_inherited_stdin(tmp_path, monkeypatch):
    captured = {}

    class FakeReader:
        def __init__(self, data):
            self.data = data

        async def read(self, _size):
            data, self.data = self.data, b""
            return data

    class FakeProcess:
        returncode = 0
        pid = 123
        stdin = None
        stdout = FakeReader(b"codex ok")
        stderr = FakeReader(b"")

        async def wait(self):
            return self.returncode

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    output = await run_external_cli(ExternalCliConfig(
        provider="codex",
        executable_path="codex",
        prompt="hello",
        model="gpt-test-codex",
        cwd=str(tmp_path),
    ))

    assert output == "codex ok"
    assert captured["stdin"] == asyncio.subprocess.DEVNULL


def test_codex_cli_error_hides_prompt_and_returns_actionable_tail():
    prompt = "这是单智能体会话。用户问题：你用的什么模型"
    stderr = "\n".join([
        "Reading additional input from stdin...",
        "OpenAI Codex v0.145.0-alpha.27",
        "--------",
        "user",
        prompt,
        "2026-07-21T08:00:00Z ERROR codex_models_manager: failed to refresh available models: timeout waiting for child process to exit",
    ])

    detail = _compact_cli_error(stderr, "", prompt=prompt, returncode=1)

    assert "failed to refresh available models" in detail
    assert "你用的什么模型" not in detail
    assert "Reading additional input" not in detail


async def test_acp_executor_streams_acp_chunks(tmp_path):
    kimi = _fake_kimi(tmp_path)
    store = ExternalAgentStore(str(tmp_path / "crew.db"))
    runtime = store.upsert_runtime({
        "id": "kimi_stream",
        "provider": "kimi",
        "name": "Kimi",
        "executable_path": str(kimi),
        "version": "1.2.3",
        "protocol": "acp",
    })
    agent = store.create_agent(name="Kimi Streamer", runtime_id=runtime["id"])
    ctx = ExecutionContext(
        session_id="s",
        request_id="r",
        system_prompt="",
        messages=[],
        query="hello streaming",
        cwd=str(tmp_path),
    )

    chunks = [
        ch async for ch in AcpExecutor({
            "external_agent_id": agent["id"],
            "external_store": store,
        }).execute(ctx)
    ]

    assert chunks[0].kind == "delta"
    assert "Kimi收到" in chunks[0].body["text"]
    assert chunks[-1].kind == "final"
    assert chunks[-1].body["text"] == chunks[0].body["text"]


async def test_acp_prefers_standard_session_config_option_for_model(tmp_path):
    executable = _fake_acp_config_options(tmp_path)

    output = await run_acp_prompt(
        "hello",
        AcpAdapterConfig(
            executable_path=str(executable),
            provider="standard-acp",
            model="model-b",
            cwd=str(tmp_path),
        ),
    )

    assert output == "selected:model-b"


@pytest.mark.parametrize("resume_session_id", ["", "s-current"])
async def test_acp_skips_redundant_model_update_for_any_runtime(tmp_path, resume_session_id):
    executable = tmp_path / "acp_current_model"
    executable.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import sys

            for line in sys.stdin:
                msg = json.loads(line)
                mid = msg.get("id")
                method = msg.get("method")
                if method == "initialize":
                    result = {"ok": True}
                elif method in {"session/new", "session/resume"}:
                    result = {
                        "sessionId": "s-current",
                        "models": {
                            "currentModelId": "provider/model-a",
                            "availableModels": [
                                {"modelId": "provider/model-a", "name": "Model A"}
                            ],
                        },
                        "configOptions": [{"id": "active-model", "category": "model"}],
                    }
                elif method in {"session/set_config_option", "session/set_model"}:
                    print(json.dumps({
                        "jsonrpc": "2.0",
                        "id": mid,
                        "error": {"code": -1, "message": "redundant model update"},
                    }), flush=True)
                    continue
                elif method == "session/prompt":
                    print(json.dumps({
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": "s-current",
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": "model unchanged"},
                            },
                        },
                    }), flush=True)
                    result = {"stopReason": "end_turn"}
                else:
                    print(json.dumps({
                        "jsonrpc": "2.0",
                        "id": mid,
                        "error": {"code": -32601, "message": "unsupported"},
                    }), flush=True)
                    continue
                print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}), flush=True)
            """
        ),
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

    output = await run_acp_prompt(
        "hello",
        AcpAdapterConfig(
            executable_path=str(executable),
            provider="standard-acp",
            model="provider/model-a",
            cwd=str(tmp_path),
            resume_session_id=resume_session_id,
        ),
    )

    assert output == "model unchanged"


async def test_acp_executor_uses_external_task_payload_for_single_agent(tmp_path, monkeypatch):
    from crew.agent.external.acp_adapter import AcpStreamEvent

    store = ExternalAgentStore(str(tmp_path / "crew.db"))
    runtime = store.upsert_runtime({
        "id": "kimi_payload_single",
        "provider": "kimi",
        "name": "Kimi",
        "executable_path": str(tmp_path / "kimi"),
        "version": "1.2.3",
        "protocol": "acp",
    })
    agent = store.create_agent(name="Kimi Payload", runtime_id=runtime["id"])
    seen: dict[str, str] = {}

    async def fake_stream(prompt, config):
        seen["prompt"] = prompt
        seen["system_prompt"] = config.system_prompt
        yield AcpStreamEvent(kind="thinking", text="先核对测试范围。")
        yield AcpStreamEvent(kind="text", text="ok")

    monkeypatch.setattr(acp_adapter, "stream_acp_events", fake_stream)
    ctx = ExecutionContext(
        session_id="crew_s1",
        request_id="r",
        system_prompt="",
        messages=[],
        query="帮我简短介绍一下项目状态",
        cwd=str(tmp_path),
    )

    chunks = [
        chunk async for chunk in AcpExecutor({
            "external_agent_id": agent["id"],
            "external_store": store,
            "model": "kimi-code/k3",
        }).execute(ctx)
    ]

    assert chunks[-1].kind == "final"
    assert any(chunk.kind == "thinking" and chunk.body["text"] == "先核对测试范围。" for chunk in chunks)
    assert ctx.messages[-1].thinking == "先核对测试范围。"
    assert "# Crew External Task Payload" in seen["prompt"]
    assert "- team_role: none" in seen["prompt"]
    assert "- model: kimi-code/k3" in seen["prompt"]
    assert "帮我简短介绍一下项目状态" in seen["prompt"]
    assert "这是单外部智能体会话" in seen["system_prompt"]
    assert "team_plan_create 创建 TeamPlan" not in seen["system_prompt"]


async def test_acp_executor_uses_team_chat_payload_for_team_leader(tmp_path, monkeypatch):
    from crew.agent.external.acp_adapter import AcpStreamEvent

    store = ExternalAgentStore(str(tmp_path / "crew.db"))
    runtime = store.upsert_runtime({
        "id": "kimi_payload_leader_chat",
        "provider": "kimi",
        "name": "Kimi",
        "executable_path": str(tmp_path / "kimi"),
        "version": "1.2.3",
        "protocol": "acp",
    })
    agent = store.create_agent(name="Kimi Team Leader", runtime_id=runtime["id"])
    seen: dict[str, str] = {}

    async def fake_stream(prompt, config):
        seen["prompt"] = prompt
        seen["system_prompt"] = config.system_prompt
        yield AcpStreamEvent(kind="text", text="ok")

    monkeypatch.setattr(acp_adapter, "stream_acp_events", fake_stream)
    ctx = ExecutionContext(
        session_id="team_s1::leader",
        request_id="r",
        system_prompt="",
        messages=[],
        query="团队成员有哪些？",
        params={
            "team_session_id": "team_s1",
            "member_session_id": "team_s1::leader",
            "agent_id": "leader",
            "team_goal": "团队日常协作",
            "team_upstream_summary": "- 成员：hh 负责实现，kk 负责测试。",
        },
        cwd=str(tmp_path),
    )

    chunks = [
        chunk async for chunk in AcpExecutor({
            "external_agent_id": agent["id"],
            "external_store": store,
            "crew_session_id": "team_s1::leader",
            "display_session_id": "team_s1",
            "control_session_id": "team_s1",
        }).execute(ctx)
    ]

    assert chunks[-1].kind == "final"
    assert "- team_role: leader" in seen["prompt"]
    assert "- mode:" not in seen["prompt"]
    assert "以当前用户最新消息和 Current Message/Current Execution Node 为本轮任务目标" in seen["prompt"]
    assert "历史上下文只用于理解指代、延续和举例，不能替代本轮任务" in seen["prompt"]
    assert "代表当前团队直接回答用户" in seen["prompt"]
    assert "不派活，不创建 TeamPlan" in seen["prompt"]
    assert "当前交互模式" not in seen["system_prompt"]
    assert "可用 Crew 控制面/MCP 真实执行" in seen["system_prompt"]


async def test_acp_executor_uses_external_task_payload_for_team_member(tmp_path, monkeypatch):
    from crew.agent.external.acp_adapter import AcpStreamEvent

    store = ExternalAgentStore(str(tmp_path / "crew.db"))
    runtime = store.upsert_runtime({
        "id": "kimi_payload_team",
        "provider": "kimi",
        "name": "Kimi",
        "executable_path": str(tmp_path / "kimi"),
        "version": "1.2.3",
        "protocol": "acp",
    })
    agent = store.create_agent(name="Kimi Team Payload", runtime_id=runtime["id"])
    seen: dict[str, object] = {}

    async def fake_stream(prompt, config):
        seen["prompt"] = prompt
        seen["system_prompt"] = config.system_prompt
        seen["mcp_servers"] = config.mcp_servers
        seen["timeout"] = config.timeout
        yield AcpStreamEvent(kind="thinking", text="先核对团队测试范围。")
        yield AcpStreamEvent(kind="text", text="ok")

    monkeypatch.setattr(acp_adapter, "stream_acp_events", fake_stream)
    ctx = ExecutionContext(
        session_id="team_s1::kk",
        request_id="r",
        system_prompt="",
        messages=[],
        query="测试设计\n\n设计贪吃蛇验收路径，并输出关键风险。",
        params={
            "team_session_id": "team_s1",
            "member_session_id": "team_s1::kk",
            "agent_id": "kk",
            "team_goal": "帮我测试之前开发的贪吃蛇",
            "team_plan_node_id": "test_plan_1",
            "team_node_title": "测试设计：贪吃蛇",
            "team_node_detail": "设计验收路径、边界场景和失败判断。",
            "team_upstream_summary": "- Leader 拆分任务：已确认只测试，不开发。",
            "team_upstream_artifacts": "/tmp/team-turn/测试方案.md",
            "team_collaboration_mode": "leader_mesh",
            "external_task_budget": "focused",
        },
        cwd=str(tmp_path),
    )

    chunks = [
        chunk async for chunk in AcpExecutor({
            "external_agent_id": agent["id"],
            "external_store": store,
            "crew_session_id": "team_s1::kk",
            "display_session_id": "team_s1",
            "control_session_id": "team_s1",
        }).execute(ctx)
    ]

    assert chunks[-1].kind == "final"
    assert any(chunk.kind == "thinking" and chunk.body["text"] == "先核对团队测试范围。" for chunk in chunks)
    prompt_text = str(seen["prompt"])
    system_prompt = str(seen["system_prompt"])
    assert "- mode:" not in prompt_text
    assert "- team_role: member" in prompt_text
    assert "- member_id: kk" in prompt_text
    assert "帮我测试之前开发的贪吃蛇" in prompt_text
    assert "测试设计：贪吃蛇" in prompt_text
    assert "已确认只测试，不开发" in prompt_text
    assert "## Upstream Artifacts" in prompt_text
    assert "/tmp/team-turn/测试方案.md" in prompt_text
    assert "以当前用户最新消息和 Current Message/Current Execution Node 为本轮任务目标" in prompt_text
    assert "历史上下文只用于理解指代、延续和举例，不能替代本轮任务" in prompt_text
    assert "只完成 Current Execution Node" in prompt_text
    assert "不能创建、修改或重排 TeamPlan" in prompt_text
    assert "team_mention @leader 提交结果" in prompt_text
    assert "当前交互模式" not in system_prompt
    assert "不能直接向用户发起 follow-up" in system_prompt
    assert "team_plan_create 创建 TeamPlan" not in system_prompt
    assert seen["mcp_servers"] == []
    assert seen["timeout"] == 330.0


async def test_acp_executor_reuses_bound_acp_session_id(tmp_path, monkeypatch):
    from crew.agent.external.acp_adapter import AcpStreamEvent

    store = ExternalAgentStore(str(tmp_path / "crew.db"))
    runtime = store.upsert_runtime({
        "id": "hermes_resume",
        "provider": "hermes",
        "name": "Hermes",
        "executable_path": str(tmp_path / "hermes"),
        "version": "1.0.0",
        "protocol": "acp",
    })
    agent = store.create_agent(name="Hermes", runtime_id=runtime["id"])
    seen_resume_ids = []

    async def fake_stream(_prompt, config):
        seen_resume_ids.append(config.resume_session_id)
        yield AcpStreamEvent(kind="session", session_id="h1", session_resumed=bool(config.resume_session_id))
        yield AcpStreamEvent(kind="text", text="ok")

    monkeypatch.setattr(acp_adapter, "stream_acp_events", fake_stream)

    executor = AcpExecutor({
        "external_agent_id": agent["id"],
        "external_store": store,
    })

    for query in ("first", "second"):
        ctx = ExecutionContext(
            session_id="crew_s1",
            request_id=f"r_{query}",
            system_prompt="",
            messages=[],
            query=query,
            cwd=str(tmp_path),
        )
        chunks = [chunk async for chunk in executor.execute(ctx)]
        assert chunks[-1].kind == "final"

    assert seen_resume_ids == ["", "h1"]
    binding = store.get_acp_session_binding(
        crew_session_id="crew_s1",
        external_agent_id=agent["id"],
        runtime_id=runtime["id"],
        provider="hermes",
        cwd=str(tmp_path),
    )
    assert binding is not None
    assert binding["acp_session_id"] == "h1"


async def test_acp_executor_surfaces_acp_stream_error_event(tmp_path, monkeypatch):
    from crew.agent.external.acp_adapter import AcpStreamEvent

    store = ExternalAgentStore(str(tmp_path / "crew.db"))
    runtime = store.upsert_runtime({
        "id": "kimi_long_line",
        "provider": "kimi",
        "name": "Kimi",
        "executable_path": str(tmp_path / "kimi"),
        "version": "1.2.3",
        "protocol": "acp",
    })
    agent = store.create_agent(name="Kimi Long Line", runtime_id=runtime["id"])

    async def fake_stream(_prompt, _config):
        yield AcpStreamEvent(kind="error", text="ACP stdout JSONL 单行超过读取上限")

    monkeypatch.setattr(acp_adapter, "stream_acp_events", fake_stream)

    ctx = ExecutionContext(
        session_id="crew_s1",
        request_id="r",
        system_prompt="",
        messages=[],
        query="hello",
        cwd=str(tmp_path),
    )
    chunks = [
        chunk async for chunk in AcpExecutor({
            "external_agent_id": agent["id"],
            "external_store": store,
        }).execute(ctx)
    ]

    assert chunks[-1].kind == "error"
    assert "ACP stdout JSONL 单行超过读取上限" in chunks[-1].body["message"]


async def test_acp_executor_marks_failed_acp_binding_unsafe_and_skips_resume(tmp_path, monkeypatch):
    from crew.agent.external.acp_adapter import AcpStreamEvent

    store = ExternalAgentStore(str(tmp_path / "crew.db"))
    runtime = store.upsert_runtime({
        "id": "kimi_binding_failure",
        "provider": "kimi",
        "name": "Kimi",
        "executable_path": str(tmp_path / "kimi"),
        "version": "1.2.3",
        "protocol": "acp",
    })
    agent = store.create_agent(name="Kimi Binding Failure", runtime_id=runtime["id"])
    seen_resume_ids: list[str] = []
    seen_system_prompts: list[str] = []
    calls = 0

    async def fake_stream(_prompt, config):
        nonlocal calls
        calls += 1
        seen_resume_ids.append(config.resume_session_id)
        seen_system_prompts.append(config.system_prompt)
        if calls == 1:
            yield AcpStreamEvent(kind="session", session_id="bad-session")
            yield AcpStreamEvent(kind="error", text="ACP 调用超时（stage=session/prompt, process=running）")
            return
        yield AcpStreamEvent(kind="session", session_id="fresh-session")
        yield AcpStreamEvent(kind="text", text="ok")

    monkeypatch.setattr(acp_adapter, "stream_acp_events", fake_stream)
    executor = AcpExecutor({
        "external_agent_id": agent["id"],
        "external_store": store,
    })

    first_ctx = ExecutionContext(
        session_id="crew_s1",
        request_id="r1",
        system_prompt="",
        messages=[],
        query="first",
        cwd=str(tmp_path),
    )
    first_chunks = [chunk async for chunk in executor.execute(first_ctx)]
    assert first_chunks[-1].kind == "error"
    failed_binding = store.get_acp_session_binding(
        crew_session_id="crew_s1",
        external_agent_id=agent["id"],
        runtime_id=runtime["id"],
        provider="kimi",
        cwd=str(tmp_path),
    )
    assert failed_binding is not None
    assert failed_binding["acp_session_id"] == "bad-session"
    assert failed_binding["status"] == "unsafe_failed"

    second_ctx = ExecutionContext(
        session_id="crew_s1",
        request_id="r2",
        system_prompt="",
        messages=[
            Message.user("用户目标：继续测试贪吃蛇"),
            Message.assistant("上一轮已经定位到 snake.test.js 有 3 个失败。"),
        ],
        query="second",
        cwd=str(tmp_path),
    )
    second_chunks = [chunk async for chunk in executor.execute(second_ctx)]
    assert second_chunks[-1].kind == "final"
    assert seen_resume_ids == ["", ""]
    assert "Crew 侧连续上下文" not in seen_system_prompts[0]
    assert "Crew 侧连续上下文" in seen_system_prompts[1]
    assert "继续测试贪吃蛇" in seen_system_prompts[1]
    assert "snake.test.js 有 3 个失败" in seen_system_prompts[1]
    fresh_binding = store.get_acp_session_binding(
        crew_session_id="crew_s1",
        external_agent_id=agent["id"],
        runtime_id=runtime["id"],
        provider="kimi",
        cwd=str(tmp_path),
    )
    assert fresh_binding is not None
    assert fresh_binding["acp_session_id"] == "fresh-session"
    assert fresh_binding["status"] == "active"


async def test_acp_executor_injects_scoped_interaction_mcp(tmp_path, monkeypatch):
    from crew.agent.external.acp_adapter import AcpStreamEvent

    store = ExternalAgentStore(str(tmp_path / "crew.db"))
    runtime = store.upsert_runtime({
        "id": "kimi_mcp",
        "provider": "kimi",
        "name": "Kimi",
        "executable_path": str(tmp_path / "kimi"),
        "version": "1.2.3",
        "protocol": "acp",
    })
    agent = store.create_agent(name="Kimi MCP", runtime_id=runtime["id"])
    seen = {}

    async def fake_stream(_prompt, config):
        seen["mcp_servers"] = config.mcp_servers
        yield AcpStreamEvent(kind="text", text="done")

    monkeypatch.setattr(acp_adapter, "stream_acp_events", fake_stream)

    class FakeBridge:
        removed = []

        def create_binding(self, **kwargs):
            seen["binding"] = kwargs
            return SimpleNamespace(token="bind-token")

        def mcp_server_config(self, binding):
            assert binding.token == "bind-token"
            return {"name": "crew-interaction", "command": "python", "args": []}

        def remove_binding(self, token):
            self.removed.append(token)

    bridge = FakeBridge()
    ctx = ExecutionContext(
        session_id="main::child",
        request_id="r",
        system_prompt="",
        messages=[],
        query="ask user",
        cwd=str(tmp_path),
    )

    chunks = [
        chunk async for chunk in AcpExecutor({
            "external_agent_id": agent["id"],
            "external_store": store,
            "interaction_bridge": bridge,
        }).execute(ctx)
    ]

    assert seen["binding"]["display_session_id"] == "main"
    assert seen["binding"]["control_session_id"] == "main"
    assert seen["binding"]["origin_session_id"] == "main::child"
    assert seen["mcp_servers"][0]["name"] == "crew-interaction"
    assert bridge.removed == ["bind-token"]
    assert chunks[-1].kind == "final"


async def test_acp_executor_persists_followup_answer_when_acp_errors(tmp_path, monkeypatch):
    from crew.core.followup import get_followup_waiter

    store = ExternalAgentStore(str(tmp_path / "crew.db"))
    runtime = store.upsert_runtime({
        "id": "kimi_error_after_followup",
        "provider": "kimi",
        "name": "Kimi",
        "executable_path": str(tmp_path / "kimi"),
        "version": "1.2.3",
        "protocol": "acp",
    })
    agent = store.create_agent(name="Kimi Error", runtime_id=runtime["id"])

    waiter = get_followup_waiter()
    qid = waiter.create("main", [{
        "id": "q1",
        "question": "请选择方向",
        "options": [{"label": "风险分析", "value": "risk"}],
    }])
    assert waiter.resolve("main", qid, [{"question_id": "q1", "answers": ["risk"]}])

    async def fake_stream(_prompt, _config):
        raise AcpAdapterError("kimi failed after followup")
        yield  # pragma: no cover

    monkeypatch.setattr(acp_adapter, "stream_acp_events", fake_stream)
    ctx = ExecutionContext(
        session_id="main::child",
        request_id="r",
        system_prompt="",
        messages=[],
        query="ask user",
        cwd=str(tmp_path),
    )

    chunks = [
        chunk async for chunk in AcpExecutor({
            "external_agent_id": agent["id"],
            "external_store": store,
            "interaction_bridge": SimpleNamespace(create_binding=lambda **_: None),
        }).execute(ctx)
    ]

    assert chunks[-1].kind == "error"
    assert any(m.role == "user" and m.content == "已选择：风险分析" for m in ctx.messages)


async def test_acp_executor_streams_acp_tool_events(tmp_path):
    kimi = _fake_acp_with_tool_events(tmp_path)
    store = ExternalAgentStore(str(tmp_path / "crew.db"))
    runtime = store.upsert_runtime({
        "id": "kimi_tools",
        "provider": "kimi",
        "name": "Kimi",
        "executable_path": str(kimi),
        "version": "1.2.3",
        "protocol": "acp",
    })
    agent = store.create_agent(name="Kimi Tools", runtime_id=runtime["id"])
    ctx = ExecutionContext(
        session_id="s",
        request_id="r",
        system_prompt="",
        messages=[],
        query="read file",
        cwd=str(tmp_path),
    )

    chunks = [
        ch async for ch in AcpExecutor({
            "external_agent_id": agent["id"],
            "external_store": store,
        }).execute(ctx)
    ]
    tool_chunks = [ch for ch in chunks if ch.kind == "tool"]

    assert [ch.body["phase"] for ch in tool_chunks] == ["start", "result"]
    assert tool_chunks[0].body["name"] == "file_read"
    assert tool_chunks[0].body["tool_call_id"] == "tool_1"
    assert json.loads(tool_chunks[0].body["args"]) == {"path": "README.md"}
    assert tool_chunks[1].body["detail"] == "read ok"
    assert chunks[-1].kind == "final"
    assert chunks[-1].body["text"] == "工具完成"
    assert len(ctx.messages) == 1
    persisted = ctx.messages[0].tool_calls[0]
    assert persisted.id == "tool_1"
    assert persisted.name == "file_read"
    assert persisted.arguments == {"path": "README.md"}
    assert persisted.result == "read ok"
    assert persisted.status == "done"


async def test_acp_executor_reports_hermes_missing_acp_dependencies(tmp_path):
    hermes = tmp_path / "hermes"
    hermes.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import sys
            if "--version" in sys.argv:
                print("Hermes Agent v0.16.0")
                raise SystemExit(0)
            print("ACP dependencies not installed.", file=sys.stderr)
            print("Install them with: pip install -e '.[acp]'", file=sys.stderr)
            raise SystemExit(1)
            """
        ),
        encoding="utf-8",
    )
    hermes.chmod(hermes.stat().st_mode | stat.S_IXUSR)
    store = ExternalAgentStore(str(tmp_path / "crew.db"))
    runtime = store.upsert_runtime({
        "id": "hermes_test",
        "provider": "hermes",
        "name": "Hermes",
        "executable_path": str(hermes),
        "version": "0.16.0",
        "protocol": "acp",
    })
    agent = store.create_agent(name="Hermes Coder", runtime_id=runtime["id"])
    ctx = ExecutionContext(
        session_id="s",
        request_id="r",
        system_prompt="",
        messages=[],
        query="hello hermes",
        cwd=str(tmp_path),
    )

    chunks = [
        ch async for ch in AcpExecutor({
            "external_agent_id": agent["id"],
            "external_store": store,
        }).execute(ctx)
    ]

    assert chunks[-1].kind == "error"
    assert "Hermes ACP 依赖未安装" in chunks[-1].body["message"]
    assert "pip install -e '.[acp]'" in chunks[-1].body["message"]


async def test_client_executor_runs_generic_module(tmp_path, monkeypatch):
    module = tmp_path / "fake_client_agent.py"
    module.write_text(
        "def run_agent(prompt, **kwargs):\n"
        "    return {'text': 'Client收到: ' + prompt[-5:]}\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("fake_client_agent", None)
    ctx = ExecutionContext(
        session_id="s",
        request_id="r",
        system_prompt="",
        messages=[],
        query="hello client",
    )

    chunks = [ch async for ch in ClientExecutor({"module": "fake_client_agent"}).execute(ctx)]

    assert chunks[-1].kind == "final"
    assert chunks[-1].body["text"] == "Client收到: lient"


def test_sessions_include_agent_label_for_sidebar_badge():
    class ConfigStore:
        def get_agent_config(self, session_id, owner_account_id=""):
            if session_id == "kimi_session" and owner_account_id == "u1":
                return {
                    "executor": "external",
                    "external": {
                        "external_agent_id": "agent_kimi",
                        "model": "kimi/k3",
                    },
                }
            return None

    class AgentStore:
        def agent_with_runtime(self, agent_id, *, owner_account_id=""):
            assert agent_id == "agent_kimi"
            assert owner_account_id in {"u1", "u2"}
            return (
                {"id": agent_id, "name": "Kimi Coder", "provider": "kimi"},
                {"id": "runtime_kimi", "provider": "kimi", "metadata": {}},
            )

    crew = SimpleNamespace(session_store=ConfigStore(), external_agents=AgentStore(), config=Config())
    sessions = [
        {"session_id": "crew_session", "title": "默认会话"},
        {"session_id": "kimi_session", "title": "Kimi 会话"},
    ]

    enriched = with_session_agent_labels(crew, sessions, owner_account_id="u1")

    assert enriched[0]["agent_label"] == {
        "name": "Crew",
        "provider": "crew",
        "display_badge": "M",
    }
    assert enriched[1]["agent_label"] == {
        "name": "Kimi Coder",
        "provider": "kimi",
        "display_badge": "K",
        "model": "kimi/k3",
    }

    other_owner = with_session_agent_labels(crew, sessions, owner_account_id="u2")
    assert other_owner[1]["agent_label"] == {
        "name": "Crew",
        "provider": "crew",
        "display_badge": "M",
    }


def test_acp_followup_missing_tool_diagnostic_is_provider_agnostic():
    assert _looks_like_missing_followup_tool("我当前环境中没有 `ask_followup_question` 这个工具")
    assert _looks_like_missing_followup_tool(
        "工具列表里没有 `mcp_crew_interaction_ask_followup_question`，无法调用它"
    )
    assert not _looks_like_missing_followup_tool("我已经调用 ask_followup_question 并等待用户选择")

    diagnostic = _followup_mcp_diagnostic("kimi")
    assert "provider='kimi'" in diagnostic
    assert "RuntimeAdapter" in diagnostic
    assert "mcp_crew_interaction_ask_followup_question" in diagnostic


def test_cli_followup_missing_tool_diagnostic_explains_runtime_limit():
    diagnostic = _followup_cli_diagnostic("codex")

    assert "provider='codex'" in diagnostic
    assert "CLI runtime" in diagnostic
    assert "不会注入 Crew Interaction MCP" in diagnostic
    assert "不是因为会话处于 Plan 模式" in diagnostic
    assert "切换 Plan/Code 模式也不会" in diagnostic


def test_acp_prompt_requires_crew_followup_mcp_instead_of_native_question_tool():
    agent = {"name": "Kimi", "provider": "kimi"}
    runtime = {"name": "Kimi", "provider": "kimi"}

    single_prompt = _build_compact_acp_system_prompt(
        agent,
        runtime,
        mode="single_agent",
    )
    leader_prompt = _build_compact_acp_system_prompt(
        agent,
        runtime,
        mode="team_execute",
        team_role="leader",
    )
    member_prompt = _build_compact_acp_system_prompt(
        agent,
        runtime,
        mode="team_execute",
        team_role="member",
    )

    for prompt in (single_prompt, leader_prompt):
        assert "mcp__crew-interaction__ask_followup_question" in prompt
        assert "禁止调用 runtime 内置 `AskUserQuestion`" in prompt
        assert "只会在 ACP 中返回 dismissed" in prompt
    assert "不能直接向用户发起 follow-up" in member_prompt
    assert "mcp__crew-interaction__ask_followup_question" not in member_prompt


def test_external_prompt_uses_native_runtime_for_normal_skill():
    active = SkillActivation(
        skill_id="local-report",
        name="本地报告",
        instruction="读取输入并生成报告。",
        skill_root="/skills/local-report",
        entrypoints=(
            SkillEntrypoint(
                id="build",
                path="scripts/build.py",
                runtime="python",
            ),
        ),
    )

    prompt = _build_compact_acp_system_prompt(
        {"name": "Kimi", "provider": "kimi"},
        {"name": "Kimi", "provider": "kimi"},
        mode="single_agent",
        active_skills=(active,),
    )

    assert "Runtime 自带的文件与 terminal 工具原生执行" in prompt
    assert "build=scripts/build.py" in prompt


def _fake_claude_stream_runtime(tmp_path):
    script = tmp_path / "claude-stream"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import os
            import sys

            with open(os.environ["ARGV_FILE"], "w", encoding="utf-8") as handle:
                json.dump(sys.argv[1:], handle)
            if "--mcp-config" in sys.argv:
                config_path = sys.argv[sys.argv.index("--mcp-config") + 1]
                with open(config_path, encoding="utf-8") as source:
                    mcp_config = json.load(source)
                with open(os.environ["MCP_CAPTURE_FILE"], "w", encoding="utf-8") as target:
                    json.dump(mcp_config, target)
            request = json.loads(sys.stdin.readline())
            assert request["type"] == "user"
            print(json.dumps({"type": "system", "subtype": "init", "session_id": "claude-s1"}), flush=True)
            print(json.dumps({
                "type": "stream_event",
                "session_id": "claude-s1",
                "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hello"}},
            }), flush=True)
            print(json.dumps({
                "type": "assistant",
                "session_id": "claude-s1",
                "message": {"role": "assistant", "content": [{
                    "type": "tool_use", "id": "tool-1", "name": "Read", "input": {"path": "README.md"},
                }]},
            }), flush=True)
            print(json.dumps({
                "type": "control_request",
                "request_id": "permission-1",
                "session_id": "claude-s1",
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": "Read",
                    "input": {"path": "README.md"},
                },
            }), flush=True)
            response = json.loads(sys.stdin.readline())
            assert response["response"]["response"]["behavior"] == "allow"
            print(json.dumps({
                "type": "user",
                "session_id": "claude-s1",
                "message": {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": "tool-1", "content": "read ok",
                }]},
            }), flush=True)
            print(json.dumps({
                "type": "result",
                "session_id": "claude-s1",
                "usage": {"input_tokens": 7, "output_tokens": 3},
            }), flush=True)
            # Real stream-json mode can remain resident for another stdin turn.
            # Crew must treat result as terminal and close stdin instead of
            # waiting for process exit or the full idle timeout.
            assert sys.stdin.readline() == ""
            """
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


@pytest.mark.asyncio
async def test_claude_stream_json_emits_session_text_tools_usage_and_permission(tmp_path):
    script = _fake_claude_stream_runtime(tmp_path)
    argv_file = tmp_path / "claude-argv.json"
    mcp_capture_file = tmp_path / "claude-mcp.json"
    permissions = []

    async def allow(request):
        permissions.append(request)
        return "allow"

    events = [
        event
        async for event in stream_claude_events(RuntimeExecutionRequest(
            executable_path=str(script),
            provider="claude-code",
            prompt="hello",
            model="default",
            cwd=str(tmp_path),
            custom_env={
                "ARGV_FILE": str(argv_file),
                "MCP_CAPTURE_FILE": str(mcp_capture_file),
            },
            mcp_servers=[{
                "name": "crew-interaction",
                "command": "/usr/bin/python3",
                "args": ["-m", "crew.cli"],
                "env": {"CREW_INTERACTION_TOKEN": "token"},
            }],
            permission_handler=allow,
            timeout=5,
        ))
    ]

    assert events[0].kind == "session"
    assert events[0].session_id == "claude-s1"
    assert "".join(event.text for event in events if event.kind == "text") == "hello"
    tools = [event.tool for event in events if event.kind == "tool"]
    assert [(tool.phase, tool.tool_call_id) for tool in tools] == [
        ("start", "tool-1"),
        ("result", "tool-1"),
    ]
    assert [event.usage for event in events if event.kind == "usage"] == [{
        "input_tokens": 7,
        "output_tokens": 3,
        "total_tokens": 10,
    }]
    assert permissions[0].tool_call["rawInput"]["arguments"]["path"] == "README.md"
    argv = json.loads(argv_file.read_text(encoding="utf-8"))
    assert "--input-format" in argv
    assert "--include-partial-messages" in argv
    assert "--permission-mode" not in argv
    mcp_path = argv[argv.index("--mcp-config") + 1]
    assert not os.path.exists(mcp_path)
    assert json.loads(mcp_capture_file.read_text(encoding="utf-8")) == {
        "mcpServers": {
            "crew-interaction": {
                "command": "/usr/bin/python3",
                "args": ["-m", "crew.cli"],
                "env": {"CREW_INTERACTION_TOKEN": "token"},
            }
        }
    }


@pytest.mark.asyncio
async def test_claude_reports_resume_rejection_to_unified_executor(tmp_path):
    script = tmp_path / "claude-resume"
    calls_file = tmp_path / "claude-resume-calls.txt"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import os
            import sys

            with open(os.environ["CALLS_FILE"], "a", encoding="utf-8") as handle:
                handle.write(" ".join(sys.argv[1:]) + "\\n")
            if "--resume" in sys.argv:
                print("session not found", file=sys.stderr)
                raise SystemExit(2)
            json.loads(sys.stdin.readline())
            print(json.dumps({"type": "system", "subtype": "init", "session_id": "fresh-s1"}), flush=True)
            print(json.dumps({
                "type": "stream_event",
                "session_id": "fresh-s1",
                "event": {"delta": {"type": "text_delta", "text": "fresh"}},
            }), flush=True)
            print(json.dumps({"type": "result", "session_id": "fresh-s1"}), flush=True)
            """
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    with pytest.raises(RuntimeResumeRejected):
        _ = [
            event
            async for event in stream_claude_events(RuntimeExecutionRequest(
                executable_path=str(script),
                provider="claude-code",
                prompt="continue",
                cwd=str(tmp_path),
                resume_session_id="missing-s1",
                custom_env={"CALLS_FILE": str(calls_file)},
                timeout=2,
            ))
        ]

    calls = calls_file.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 1
    assert "--resume missing-s1" in calls[0]


@pytest.mark.asyncio
async def test_unified_executor_restarts_once_after_safe_resume_rejection():
    class FakeAdapter:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            if request.resume_session_id:
                raise RuntimeResumeRejected("missing")
            yield ExternalStreamEvent(kind="session", session_id="fresh-s1")
            yield ExternalStreamEvent(kind="text", text="fresh")

    adapter = FakeAdapter()
    request = RuntimeExecutionRequest(
        executable_path="/fake",
        provider="fake",
        prompt="continue",
        resume_session_id="missing-s1",
        system_prompt="base",
    )
    events = [
        event
        async for event in _stream_runtime_with_safe_resume(
            adapter,
            request,
            reset_memory="Crew summary",
        )
    ]

    assert len(adapter.requests) == 2
    assert adapter.requests[1].resume_session_id == ""
    assert "Crew summary" in adapter.requests[1].system_prompt
    assert events[0].kind == "session"
    assert events[0].session_id == "fresh-s1"
    assert events[0].session_reset is True
    assert "".join(event.text for event in events if event.kind == "text") == "fresh"


def _fake_codex_app_server(tmp_path):
    script = tmp_path / "codex-app"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import os
            import sys

            for line in sys.stdin:
                message = json.loads(line)
                method = message.get("method")
                mid = message.get("id")
                if method == "initialize":
                    print(json.dumps({"id": mid, "result": {"serverInfo": {"name": "fake"}}}), flush=True)
                elif method == "thread/start":
                    capture = os.environ.get("THREAD_PARAMS_FILE")
                    if capture:
                        with open(capture, "w", encoding="utf-8") as handle:
                            json.dump(message["params"], handle)
                    print(json.dumps({"id": mid, "result": {"thread": {"id": "thread-1"}}}), flush=True)
                elif method == "turn/start":
                    capture = os.environ.get("TURN_PARAMS_FILE")
                    if capture:
                        with open(capture, "w", encoding="utf-8") as handle:
                            json.dump(message["params"], handle)
                    print(json.dumps({"id": mid, "result": {"turn": {"id": "turn-1"}}}), flush=True)
                    if os.environ.get("EMIT_DYNAMIC_TOOL"):
                        print(json.dumps({
                            "id": 901,
                            "method": "item/tool/call",
                            "params": {
                                "threadId": "thread-1",
                                "turnId": "turn-1",
                                "callId": "dynamic-1",
                                "namespace": "crew_interaction",
                                "tool": "ask_followup_question",
                                "arguments": {"questions": [{"id": "q1", "question": "Pick"}]},
                            },
                        }), flush=True)
                        dynamic_result = json.loads(sys.stdin.readline())
                        assert dynamic_result["result"]["success"] is True
                        assert dynamic_result["result"]["contentItems"][0]["text"] == "ALPHA"
                    print(json.dumps({
                        "method": "item/agentMessage/delta",
                        "params": {"threadId": "thread-1", "turnId": "old-turn", "delta": "old"},
                    }), flush=True)
                    print(json.dumps({
                        "method": "item/agentMessage/delta",
                        "params": {"threadId": "thread-1", "turnId": "turn-1", "delta": "codex"},
                    }), flush=True)
                    print(json.dumps({
                        "method": "item/reasoning/summaryTextDelta",
                        "params": {"threadId": "thread-1", "turnId": "turn-1", "delta": "inspect"},
                    }), flush=True)
                    print(json.dumps({
                        "method": "item/reasoning/textDelta",
                        "params": {"threadId": "thread-1", "turnId": "turn-1", "delta": " plan"},
                    }), flush=True)
                    print(json.dumps({
                        "method": "item/started",
                        "params": {
                            "threadId": "thread-1",
                            "turnId": "turn-1",
                            "item": {"id": "cmd-1", "type": "commandExecution", "command": "pwd"},
                        },
                    }), flush=True)
                    print(json.dumps({
                        "id": 900,
                        "method": "item/commandExecution/requestApproval",
                        "params": {
                            "threadId": "thread-1",
                            "turnId": "turn-1",
                            "item": {"id": "cmd-1", "type": "commandExecution", "command": "pwd"},
                        },
                    }), flush=True)
                    approval = json.loads(sys.stdin.readline())
                    assert approval["result"]["decision"] == "accept"
                    print(json.dumps({
                        "method": "item/completed",
                        "params": {
                            "threadId": "thread-1",
                            "turnId": "turn-1",
                            "item": {
                                "id": "cmd-1",
                                "type": "commandExecution",
                                "command": "pwd",
                                "aggregatedOutput": "/tmp",
                                "status": "completed",
                            },
                        },
                    }), flush=True)
                    print(json.dumps({
                        "method": "thread/tokenUsage/updated",
                        "params": {
                            "threadId": "thread-1",
                            "turnId": "turn-1",
                            "tokenUsage": {"last": {"inputTokens": 4, "outputTokens": 2}},
                        },
                    }), flush=True)
                    print(json.dumps({
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thread-1",
                            "turn": {"id": "turn-1", "status": "completed"},
                        },
                    }), flush=True)
            """
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


@pytest.mark.asyncio
async def test_codex_app_server_gates_turn_events_and_emits_tools_usage_approval(tmp_path):
    script = _fake_codex_app_server(tmp_path)
    thread_params_file = tmp_path / "codex-thread-params.json"
    turn_params_file = tmp_path / "codex-turn-params.json"
    permissions = []

    async def allow(request):
        permissions.append(request)
        return "allow"

    events = [
        event
        async for event in stream_codex_events(RuntimeExecutionRequest(
            executable_path=str(script),
            provider="codex",
            prompt="work",
            cwd=str(tmp_path),
            system_prompt="Crew instructions",
            custom_env={
                "THREAD_PARAMS_FILE": str(thread_params_file),
                "TURN_PARAMS_FILE": str(turn_params_file),
            },
            mcp_servers=[{
                "name": "crew-interaction",
                "command": "/usr/bin/python3",
                "args": ["-m", "crew.cli"],
                "env": {"CREW_INTERACTION_TOKEN": "token"},
            }],
            permission_handler=allow,
            timeout=5,
        ))
    ]

    assert events[0].kind == "session"
    assert events[0].session_id == "thread-1"
    assert "".join(event.text for event in events if event.kind == "text") == "codex"
    assert "".join(event.text for event in events if event.kind == "thinking") == "inspect plan"
    assert [(event.tool.phase, event.tool.tool_call_id) for event in events if event.kind == "tool"] == [
        ("start", "cmd-1"),
        ("result", "cmd-1"),
    ]
    assert [event.usage for event in events if event.kind == "usage"] == [{
        "input_tokens": 4,
        "output_tokens": 2,
        "total_tokens": 6,
    }]
    assert permissions[0].tool_call["rawInput"]["name"] == "shell"
    thread_params = json.loads(thread_params_file.read_text(encoding="utf-8"))
    turn_params = json.loads(turn_params_file.read_text(encoding="utf-8"))
    assert thread_params["developerInstructions"] == "Crew instructions"
    assert turn_params["effort"] == "medium"
    assert turn_params["summary"] == "concise"
    assert thread_params["config"]["mcp_servers"] == {
        "crew-interaction": {
            "command": "/usr/bin/python3",
            "args": ["-m", "crew.cli"],
            "env": {"CREW_INTERACTION_TOKEN": "token"},
        }
    }


@pytest.mark.asyncio
async def test_codex_app_server_invokes_dynamic_control_tool(tmp_path):
    script = _fake_codex_app_server(tmp_path)
    thread_params_file = tmp_path / "codex-dynamic-thread-params.json"
    calls = []

    async def handle(tool_name, arguments, *, namespace=""):
        calls.append((namespace, tool_name, arguments))
        return "ALPHA"

    async def allow(_request):
        return "allow"

    events = [
        event
        async for event in stream_codex_events(RuntimeExecutionRequest(
            executable_path=str(script),
            provider="codex",
            prompt="ask",
            cwd=str(tmp_path),
            custom_env={
                "THREAD_PARAMS_FILE": str(thread_params_file),
                "EMIT_DYNAMIC_TOOL": "1",
            },
            dynamic_tools=[{
                "type": "namespace",
                "name": "crew_interaction",
                "description": "Crew controls",
                "tools": [{
                    "type": "function",
                    "name": "ask_followup_question",
                    "description": "Ask",
                    "inputSchema": {"type": "object"},
                }],
            }],
            dynamic_tool_handler=handle,
            permission_handler=allow,
            timeout=5,
        ))
    ]

    assert calls == [(
        "crew_interaction",
        "ask_followup_question",
        {"questions": [{"id": "q1", "question": "Pick"}]},
    )]
    dynamic_events = [
        event.tool
        for event in events
        if event.kind == "tool" and event.tool and event.tool.tool_call_id == "dynamic-1"
    ]
    assert [(event.name, event.phase, event.detail) for event in dynamic_events] == [
        ("ask_followup_question", "start", ""),
        ("ask_followup_question", "result", "ALPHA"),
    ]
    thread_params = json.loads(thread_params_file.read_text(encoding="utf-8"))
    assert thread_params["dynamicTools"][0]["name"] == "crew_interaction"


@pytest.mark.asyncio
async def test_codex_falls_back_only_when_app_server_is_unsupported(tmp_path):
    script = tmp_path / "codex-legacy"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import sys

            if sys.argv[1:2] == ["app-server"]:
                print("error: unrecognized subcommand 'app-server'", file=sys.stderr)
                raise SystemExit(2)
            print("legacy result")
            """
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    events = [
        event
        async for event in stream_codex_events(RuntimeExecutionRequest(
            executable_path=str(script),
            provider="codex",
            prompt="work",
            cwd=str(tmp_path),
            timeout=2,
        ))
    ]

    assert [(event.kind, event.text) for event in events] == [("text", "legacy result")]


@pytest.mark.asyncio
async def test_codex_never_replays_with_legacy_exec_after_turn_started(tmp_path):
    script = tmp_path / "codex-unsafe-fallback"
    calls_file = tmp_path / "codex-calls.txt"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import os
            import sys

            with open(os.environ["CALLS_FILE"], "a", encoding="utf-8") as handle:
                handle.write(" ".join(sys.argv[1:]) + "\\n")
            for line in sys.stdin:
                message = json.loads(line)
                method = message.get("method")
                mid = message.get("id")
                if method == "initialize":
                    print(json.dumps({"id": mid, "result": {}}), flush=True)
                elif method == "thread/start":
                    print(json.dumps({"id": mid, "result": {"thread": {"id": "thread-1"}}}), flush=True)
                elif method == "turn/start":
                    print(json.dumps({"id": mid, "result": {"turn": {"id": "turn-1"}}}), flush=True)
                    raise SystemExit(3)
            """
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    with pytest.raises(Exception, match="Codex"):
        _ = [
            event
            async for event in stream_codex_events(RuntimeExecutionRequest(
                executable_path=str(script),
                provider="codex",
                prompt="work",
                cwd=str(tmp_path),
                custom_env={"CALLS_FILE": str(calls_file)},
                timeout=2,
            ))
        ]

    calls = calls_file.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 1
    assert calls[0].startswith("app-server")


@pytest.mark.asyncio
async def test_external_executor_uses_claude_runtime_adapter_and_persists_session(tmp_path):
    script = _fake_claude_stream_runtime(tmp_path)
    argv_file = tmp_path / "executor-claude-argv.json"
    store = ExternalAgentStore(str(tmp_path / "crew.db"))
    runtime = store.upsert_runtime({
        "id": "claude-native",
        "provider": "claude-code",
        "name": "Claude Code",
        "executable_path": str(script),
        "version": "test",
        "protocol": "cli",
        "metadata": {
            "adapter_id": "claude-stream-json",
            "models": [
                {"id": "sonnet", "label": "Claude Sonnet（当前）", "default": True},
            ],
            "default_model_id": "sonnet",
            "model_migrations": {"default": "sonnet"},
        },
    })
    agent = store.create_agent(
        name="Claude Native",
        runtime_id=runtime["id"],
        model="sonnet",
        custom_env={"ARGV_FILE": str(argv_file)},
    )
    ctx = ExecutionContext(
        session_id="crew-claude",
        request_id="request-claude",
        system_prompt="",
        messages=[],
        query="hello",
        cwd=str(tmp_path),
    )

    chunks = [
        chunk
        async for chunk in AcpExecutor({
            "external_agent_id": agent["id"],
            # Simulate a pre-migration Session override.  The runtime-declared
            # migration must win before argv and message persistence.
            "model": "default",
            "external_store": store,
        }).execute(ctx)
    ]

    assert "".join(chunk.body.get("text", "") for chunk in chunks if chunk.kind == "delta") == "hello"
    assert [chunk.kind for chunk in chunks].count("tool") == 2
    assert chunks[-1].kind == "final"
    assert chunks[-1].body["usage"]["total_tokens"] == 10
    assert ctx.messages[-1].model == "sonnet"
    argv = json.loads(argv_file.read_text(encoding="utf-8"))
    assert argv[argv.index("--model") + 1] == "sonnet"
    binding = store.get_runtime_session_binding(
        crew_session_id="crew-claude",
        external_agent_id=agent["id"],
        runtime_id=runtime["id"],
        adapter_id="claude-stream-json",
        cwd=str(tmp_path),
    )
    assert binding is not None
    assert binding["native_session_id"] == "claude-s1"


@pytest.mark.asyncio
async def test_external_executor_uses_codex_app_server_adapter(tmp_path):
    script = _fake_codex_app_server(tmp_path)
    store = ExternalAgentStore(str(tmp_path / "crew.db"))
    runtime = store.upsert_runtime({
        "id": "codex-native",
        "provider": "codex",
        "name": "Codex",
        "executable_path": str(script),
        "version": "test",
        "protocol": "cli",
        "metadata": {
            "runtime_profile_version": 1,
            "runtime_descriptor_source": "builtin",
            "models": [{"id": "default", "label": "CLI 默认模型", "default": True}],
        },
    })
    agent = store.create_agent(
        name="Codex Native",
        runtime_id=runtime["id"],
        model="default",
    )

    class FakeBridge:
        def create_binding(self, **_kwargs):
            return SimpleNamespace(token="codex-binding")

        def mcp_server_config(self, _binding):
            return {"name": "crew-interaction", "command": "python", "args": []}

        async def ask_permission(self, *_args, **_kwargs):
            return True

        def remove_binding(self, _token):
            return None

    ctx = ExecutionContext(
        session_id="crew-codex",
        request_id="request-codex",
        system_prompt="",
        messages=[],
        query="work",
        cwd=str(tmp_path),
    )
    chunks = [
        chunk
        async for chunk in AcpExecutor({
            "external_agent_id": agent["id"],
            "external_store": store,
            "interaction_bridge": FakeBridge(),
        }).execute(ctx)
    ]

    assert "".join(chunk.body.get("text", "") for chunk in chunks if chunk.kind == "delta") == "codex"
    assert "".join(chunk.body.get("text", "") for chunk in chunks if chunk.kind == "thinking") == "inspect plan"
    assert chunks[-1].kind == "final"
    assert chunks[-1].body["usage"]["total_tokens"] == 6
    assert ctx.messages[-1].thinking == "inspect plan"
    binding = store.get_runtime_session_binding(
        crew_session_id="crew-codex",
        external_agent_id=agent["id"],
        runtime_id=runtime["id"],
        adapter_id="codex-app-server",
        cwd=str(tmp_path),
    )
    assert binding is not None
    assert binding["native_session_id"] == "thread-1"


@pytest.mark.asyncio
async def test_codex_dynamic_control_profile_resets_legacy_native_session_once(tmp_path):
    script = _fake_codex_app_server(tmp_path)
    store = ExternalAgentStore(str(tmp_path / "crew.db"))
    runtime = store.upsert_runtime({
        "id": "codex-native-profile",
        "provider": "codex",
        "name": "Codex",
        "executable_path": str(script),
        "version": "test",
        "protocol": "cli",
        "metadata": {
            "adapter_id": "codex-app-server",
            "models": [{"id": "default", "label": "CLI 默认模型", "default": True}],
        },
    })
    agent = store.create_agent(
        name="Codex Native",
        runtime_id=runtime["id"],
        model="default",
    )
    binding_key = {
        "crew_session_id": "crew-codex-profile",
        "external_agent_id": agent["id"],
        "runtime_id": runtime["id"],
        "adapter_id": "codex-app-server",
        "cwd": str(tmp_path),
    }
    store.save_runtime_session_binding(
        **binding_key,
        native_session_id="legacy-thread",
    )

    class FakeDynamicBridge:
        def create_binding(self, **_kwargs):
            return SimpleNamespace(token="codex-binding")

        def dynamic_tool_specs(self, _binding):
            return []

        async def invoke_tool_json(self, *_args, **_kwargs):
            return "{}"

        async def ask_permission(self, *_args, **_kwargs):
            return True

        def remove_binding(self, _token):
            return None

    ctx = ExecutionContext(
        session_id="crew-codex-profile",
        request_id="request-codex-profile",
        system_prompt="",
        messages=[Message.user("previous context")],
        query="work",
        cwd=str(tmp_path),
    )
    chunks = [
        chunk
        async for chunk in AcpExecutor({
            "external_agent_id": agent["id"],
            "external_store": store,
            "interaction_bridge": FakeDynamicBridge(),
        }).execute(ctx)
    ]

    assert chunks[-1].kind == "final"
    binding = store.get_runtime_session_binding(**binding_key)
    assert binding is not None
    assert binding["native_session_id"] == "thread-1"
    assert binding["session_profile"] == "codex-app-server:crew-dynamic-tools-v1"


def test_legacy_acp_bindings_migrate_to_protocol_neutral_session_table(tmp_path):
    db_path = tmp_path / "crew.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE external_acp_session_binding (
              owner_account_id TEXT NOT NULL DEFAULT '',
              crew_session_id TEXT NOT NULL,
              external_agent_id TEXT NOT NULL,
              runtime_id TEXT NOT NULL,
              provider TEXT NOT NULL,
              cwd TEXT NOT NULL DEFAULT '',
              acp_session_id TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'active',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY (
                owner_account_id, crew_session_id, external_agent_id, runtime_id, provider, cwd
              )
            )
            """
        )
        conn.execute(
            """
            INSERT INTO external_acp_session_binding VALUES
            ('owner', 'crew-s1', 'agent-1', 'runtime-1', 'kimi', '/tmp', 'acp-s1',
             'active', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """
        )

    store = ExternalAgentStore(str(db_path))
    binding = store.get_runtime_session_binding(
        owner_account_id="owner",
        crew_session_id="crew-s1",
        external_agent_id="agent-1",
        runtime_id="runtime-1",
        adapter_id="acp-stdio",
        cwd="/tmp",
    )

    assert binding is not None
    assert binding["native_session_id"] == "acp-s1"
    assert binding["session_profile"] == ""
    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert "external_runtime_session_binding" in tables
    assert "external_acp_session_binding" not in tables
