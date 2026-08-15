"""后台进程注册表测试：输出捕获 / poll / wait / kill / watch_patterns / notify_on_complete。"""

from __future__ import annotations

import json
import os
import shlex
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from crew.core.runctx import current_owner_account_id
from crew.security.context import SecurityContext
from crew.security.launch import issue_process_launch
from crew.security.models import PermissionProfile, PermissionProfileKind
from crew.tools.process_registry import (
    ProcessRegistry,
    ProcessSession,
    _handle_process,
    format_process_notification,
    process_registry,
)


@pytest.fixture(autouse=True)
def _isolate_checkpoint(tmp_path_factory, monkeypatch):
    """把 checkpoint 指向临时目录，避免测试写脏真实 .crew/processes.json。

    单独 monkeypatch 自己 tmp 的用例会再覆盖一次，互不影响。
    """
    import crew.tools.process_registry as pr

    d = tmp_path_factory.mktemp("ckpt")
    monkeypatch.setattr(pr, "_checkpoint_path", lambda: d / "processes.json")
    monkeypatch.setattr(pr, "_checkpoint_signing_key", lambda: b"k" * 32, raising=False)
    pr.process_registry.reset_lifecycle_configuration()
    pr.process_registry._running.clear()
    pr.process_registry._finished.clear()
    pr.process_registry._frozen.clear()
    yield
    pr.process_registry.reset_lifecycle_configuration()


def _wait_exit(reg: ProcessRegistry, sid: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = reg.poll(sid)
        if r.get("status") == "exited":
            return r
        time.sleep(0.05)
    raise AssertionError(f"进程 {sid} 未在 {timeout}s 内退出: {reg.poll(sid)}")


def _py_cmd(code: str) -> str:
    """Build a shell command that runs Python consistently on Windows and POSIX."""
    if os.name == "nt":
        exe = sys.executable.replace("'", "''")
        ps_code = code.replace("'", "''")
        return f"& '{exe}' -c '{ps_code}'"
    parts = [sys.executable, "-c", code]
    return " ".join(shlex.quote(part) for part in parts)


def _spawn_disabled(registry: ProcessRegistry, command: str, **kwargs):
    cwd = Path(kwargs.get("cwd") or os.getcwd()).resolve()
    owner = str(kwargs.get("owner_account_id") or "local")
    session = str(kwargs.get("session_key") or "test-session")
    launch = issue_process_launch(
        SecurityContext(
            os_user="host-user",
            owner_account_id=owner,
            workspace_id="workspace-a",
            workspace_root=cwd,
            session_id=session,
            request_id="request-a",
            task_id=str(kwargs.get("task_id") or "task-a"),
            cwd=cwd,
        ),
        PermissionProfile(PermissionProfileKind.DISABLED),
    )
    return registry.spawn_local(command, launch=launch, **kwargs)


def test_spawn_local_python_prints_unicode_emoji():
    reg = ProcessRegistry()
    s = _spawn_disabled(reg, _py_cmd("print(chr(0x1f4cc))"), session_key="unicode-emoji")
    r = _wait_exit(reg, s.id)
    assert r["exit_code"] == 0
    assert "\U0001f4cc" in r.get("output_preview", "")


def test_capture_output_and_poll():
    reg = ProcessRegistry()
    s = _spawn_disabled(reg, _py_cmd("print('hello-crew')"), session_key="sess1")
    r = _wait_exit(reg, s.id)
    assert r["exit_code"] == 0
    assert "hello-crew" in r["output_preview"]


def test_process_status_redacts_command_cwd_and_output() -> None:
    secret = "sk-proj-abcdef1234567890"
    reg = ProcessRegistry()
    session = ProcessSession(
        id="proc-redact",
        command=f"echo {secret}",
        cwd=f"C:/workspace/token={secret}",
        output_buffer=f"output {secret}",
        exited=True,
        exit_code=0,
    )
    reg._finished[session.id] = session

    poll = reg.poll(session.id)
    listing = reg.list_sessions()
    log = reg.read_log(session.id)
    waited = reg.wait(session.id)

    for value in (
        poll["command"],
        poll["output_preview"],
        listing[0]["command"],
        listing[0]["cwd"],
        listing[0]["output_preview"],
        log["output"],
        waited["output"],
    ):
        assert secret not in value


def test_output_reference_is_atomically_bounded(tmp_path):
    import crew.tools.process_registry as pr

    output_ref = tmp_path / "process-output.log"
    reg = ProcessRegistry()
    session = _spawn_disabled(
        reg,
        _py_cmd(f"print('x' * {pr.MAX_OUTPUT_REF_BYTES + 50_000}); print('final-tail')"),
        session_key="bounded-output-ref",
        output_ref=str(output_ref),
    )

    result = _wait_exit(reg, session.id)
    stored = output_ref.read_bytes()

    assert result["exit_code"] == 0
    assert len(stored) <= pr.MAX_OUTPUT_REF_BYTES
    assert stored.startswith(pr._OUTPUT_REF_TRUNCATED)
    assert b"final-tail" in stored


def test_child_env_uses_only_runtime_defaults(monkeypatch):
    import crew.tools.process_registry as pr

    monkeypatch.setattr(
        "crew.state.home.runtime_env_overrides",
        lambda **kwargs: {"CREW_RUNTIME_HOME": "runtime"},
    )

    env, secret_values = pr.ProcessRegistry._child_env("owner-a")

    assert env == {"CREW_RUNTIME_HOME": "runtime", "PYTHONUNBUFFERED": "1"}
    assert secret_values == ()


def test_spawn_local_does_not_inherit_ambient_credentials(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-secret")
    monkeypatch.setenv("HTTPS_PROXY", "ambient-proxy")
    reg = ProcessRegistry()
    s = _spawn_disabled(
        reg,
        _py_cmd("import os; print(os.getenv('OPENAI_API_KEY', 'missing')); print(os.getenv('HTTPS_PROXY', 'missing'))"),
        session_key="minimal-env",
    )
    result = _wait_exit(reg, s.id)
    assert result["exit_code"] == 0
    assert "ambient-secret" not in result["output_preview"]
    assert "ambient-proxy" not in result["output_preview"]
    assert "missing" in result["output_preview"]


def test_spawn_local_can_bind_an_exact_explicit_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-secret")
    environment = {"SAFE_VALUE": "bound"}
    if os.name == "nt":
        environment["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    reg = ProcessRegistry()
    session = _spawn_disabled(
        reg,
        "explicit environment",
        launch_argv=(
            sys.executable,
            "-c",
            (
                "import os; "
                "print(os.getenv('SAFE_VALUE', 'missing')); "
                "print(os.getenv('OPENAI_API_KEY', 'missing'))"
            ),
        ),
        explicit_environment=environment,
        session_key="explicit-env",
    )

    result = _wait_exit(reg, session.id)
    assert result["exit_code"] == 0
    assert "bound" in result["output_preview"]
    assert "ambient-secret" not in result["output_preview"]
    assert "missing" in result["output_preview"]


def test_read_log():
    reg = ProcessRegistry()
    s = _spawn_disabled(
        reg,
        _py_cmd("print('l1'); print('l2'); print('l3')"),
        session_key="s",
    )
    _wait_exit(reg, s.id)
    log = reg.read_log(s.id, offset=0, limit=200)
    assert log["total_lines"] == 3
    assert "l2" in log["output"]


def test_wait_returns_exit():
    reg = ProcessRegistry()
    s = _spawn_disabled(reg, _py_cmd("import sys; sys.exit(0)"), session_key="s")
    r = reg.wait(s.id, timeout=10)
    assert r["status"] == "exited"
    assert r["exit_code"] == 0


def test_wait_timeout():
    reg = ProcessRegistry()
    s = _spawn_disabled(reg, _py_cmd("import time; time.sleep(5)"), session_key="s")
    r = reg.wait(s.id, timeout=1)
    assert r["status"] == "timeout"
    reg.kill_process(s.id, owner_account_id=s.owner_account_id)


def test_kill_running_process():
    reg = ProcessRegistry()
    s = _spawn_disabled(reg, _py_cmd("import time; time.sleep(30)"), session_key="s")
    time.sleep(0.2)
    assert reg.poll(s.id)["status"] == "running"
    r = reg.kill_process(s.id, owner_account_id=s.owner_account_id)
    assert r["status"] == "killed"
    _wait_exit(reg, s.id)
    assert reg.poll(s.id)["status"] == "exited"


def test_kill_requires_explicit_owner_scope():
    reg = ProcessRegistry()
    s = _spawn_disabled(
        reg,
        _py_cmd("import time; time.sleep(30)"),
        session_key="owner-required",
        owner_account_id="owner-a",
    )
    try:
        result = reg.kill_process(s.id)
        assert result["status"] == "forbidden"
        assert reg.poll(s.id, owner_account_id="owner-a")["status"] == "running"
    finally:
        reg.kill_process(s.id, owner_account_id="owner-a")


def test_process_lifecycle_audit_chain_covers_spawn_kill_and_exit(tmp_path):
    import crew.tools.process_registry as pr

    events: list[dict] = []
    registry = pr.ProcessRegistry()
    registry.configure_lifecycle(
        workspace_root_resolver=lambda _owner, _workspace: tmp_path,
        output_root_resolver=lambda _owner: tmp_path,
        session_validator=lambda *_args: True,
        policy_digest_resolver=lambda *_args: "b" * 64,
        audit_recorder=events.append,
    )
    registry.activate_owner(
        "owner-a",
        authorization_generation="a" * 64,
        authorization_expires_at=time.time() + 3600,
    )
    session = _spawn_disabled(
        registry,
        _py_cmd("import time; time.sleep(30)"),
        cwd=str(tmp_path),
        session_key="audit-chain",
        owner_account_id="owner-a",
    )
    try:
        result = registry.kill_process(session.id, owner_account_id="owner-a")
        assert result["status"] == "killed"
        _wait_exit(registry, session.id)

        types = [event["event_type"] for event in events]
        assert "process_spawn_checkpointed" in types
        assert "process_cleanup_completed" in types
        assert "process_exited" in types
        for event in events:
            assert event["owner_account_id"] == "owner-a"
            assert event["session_key"] == "audit-chain"
            assert event["workspace_id"] == "workspace-a"
            assert event["task_id"] == "task-a"
            payload = json.dumps(event, ensure_ascii=False)
            assert "import time" not in payload
            assert "sleep" not in payload
    finally:
        registry.kill_process(session.id, owner_account_id="owner-a")


def test_notify_on_complete_enqueues():
    reg = ProcessRegistry()
    s = _spawn_disabled(
        reg,
        _py_cmd("print('done')"),
        session_key="sessX",
        owner_account_id="A:uid-a",
        notify_on_complete=True,
    )
    _wait_exit(reg, s.id)
    # reader 线程的 finally 里 enqueue，给一点时间
    time.sleep(0.1)
    events = reg.drain_for_session("sessX", owner_account_id="A:uid-a")
    assert len(events) == 1
    assert events[0]["type"] == "completion"
    assert events[0]["exit_code"] == 0
    assert "done" in events[0]["output"]
    # drain 后应清空
    assert reg.drain_for_session("sessX", owner_account_id="A:uid-a") == []


def test_process_notifications_are_owner_scoped():
    reg = ProcessRegistry()
    a = _spawn_disabled(
        reg,
        _py_cmd("print('owner-a')"),
        session_key="same",
        owner_account_id="A:uid-a",
        notify_on_complete=True,
    )
    b = _spawn_disabled(
        reg,
        _py_cmd("print('owner-b')"),
        session_key="same",
        owner_account_id="B:uid-b",
        notify_on_complete=True,
    )
    _wait_exit(reg, a.id)
    _wait_exit(reg, b.id)
    time.sleep(0.1)

    events_a = reg.drain_for_session("same", owner_account_id="A:uid-a")
    events_b = reg.drain_for_session("same", owner_account_id="B:uid-b")

    assert len(events_a) == 1 and "owner-a" in events_a[0]["output"]
    assert len(events_b) == 1 and "owner-b" in events_b[0]["output"]


def test_watch_patterns_match():
    reg = ProcessRegistry()
    s = _spawn_disabled(
        reg,
        _py_cmd("print('READY-NOW')"),
        session_key="sessW",
        watch_patterns=["READY"],
    )
    _wait_exit(reg, s.id)
    time.sleep(0.1)
    events = reg.drain_for_session("sessW")
    matches = [e for e in events if e["type"] == "watch_match"]
    assert matches, f"期望 watch_match, got {events}"
    assert matches[0]["pattern"] == "READY"
    assert "READY-NOW" in matches[0]["output"]


def test_watch_no_match_no_event():
    reg = ProcessRegistry()
    s = _spawn_disabled(
        reg,
        _py_cmd("print('nothing-here')"),
        session_key="sessN",
        watch_patterns=["ZZZ"],
    )
    _wait_exit(reg, s.id)
    time.sleep(0.1)
    assert reg.drain_for_session("sessN") == []


def test_format_notification():
    text = format_process_notification({
        "type": "completion", "session_id": "proc_x", "command": "echo hi",
        "exit_code": 0, "output": "hi",
    })
    assert "proc_x" in text and "退出码 0" in text

    text2 = format_process_notification({
        "type": "watch_match", "session_id": "proc_y", "command": "tail -f log",
        "pattern": "ERROR", "output": "ERROR boom", "suppressed": 2,
    })
    assert "watch 模式" in text2 and "ERROR" in text2 and "2 条" in text2


def test_recover_from_checkpoint(tmp_path, monkeypatch):
    import crew.tools.process_registry as pr

    # 把 checkpoint 指向临时目录
    monkeypatch.setattr(pr, "_checkpoint_path", lambda: tmp_path / "processes.json")

    reg = pr.ProcessRegistry()
    # 起一个真实存活的长进程，让它写进 checkpoint
    s = _spawn_disabled(
        reg,
        _py_cmd("import time; time.sleep(30)"),
        session_key="recov",
        notify_on_complete=True,
    )
    assert (tmp_path / "processes.json").exists()
    real_pid = s.pid

    # 模拟主程序重启：startup 只能冻结暂存，尚不可查询或控制。
    reg2 = pr.ProcessRegistry()
    n = reg2.recover_from_checkpoint()
    assert n == 1
    assert reg2.poll(s.id)["status"] == "not_found"
    assert s.id in reg2._frozen

    result = reg2.activate_owner(
        s.owner_account_id,
        authorization_generation=s.authorization_generation,
        authorization_expires_at=s.authorization_expires_at,
    )
    assert result["activated"] == 1
    r = reg2.poll(s.id)
    assert r["status"] == "running"
    assert r.get("detached") is True
    assert "无输出历史" in r.get("note", "")

    # detached 进程可被 kill（走 host PID）
    kr = reg2.kill_process(s.id, owner_account_id=s.owner_account_id)
    assert kr["status"] == "killed"
    # 真实进程应已被杀
    time.sleep(0.3)
    assert not pr._pid_alive(real_pid)
    # 原 registry 也清理掉
    reg.kill_process(s.id, owner_account_id=s.owner_account_id)


def test_spawn_fails_closed_and_terminates_when_checkpoint_cannot_persist(
    monkeypatch,
) -> None:
    import crew.tools.process_registry as pr

    real_terminate = pr.terminate_process_tree
    terminated: list[int] = []

    def record_terminate(pid: int) -> None:
        terminated.append(pid)
        real_terminate(pid)

    monkeypatch.setattr(pr, "terminate_process_tree", record_terminate)
    monkeypatch.setattr(
        pr,
        "_write_checkpoint_document",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PermissionError("checkpoint unavailable")
        ),
    )
    registry = pr.ProcessRegistry()

    with pytest.raises(pr.ProcessCheckpointError, match="checkpoint"):
        _spawn_disabled(
            registry,
            _py_cmd("import time; time.sleep(30)"),
            session_key="checkpoint-failure",
        )

    assert len(terminated) == 1
    assert not pr._pid_alive(terminated[0])
    assert registry._running == {}


def test_required_checkpoint_rejects_live_session_with_incomplete_identity() -> None:
    import crew.tools.process_registry as pr

    class LiveProcess:
        def poll(self):
            return None

    registry = pr.ProcessRegistry()
    session = pr.ProcessSession(
        id="proc-incomplete",
        command="redacted",
        owner_account_id="owner-a",
        workspace_id="workspace-a",
        session_key="session-a",
        pid=123,
        process=LiveProcess(),
        authorization_digest="a" * 64,
    )
    registry._running[session.id] = session

    with pytest.raises(pr.ProcessCheckpointError, match="recovery identity"):
        registry._write_checkpoint(required=True)


def test_checkpoint_is_versioned_signed_private_and_contains_no_command_secret(
    tmp_path,
    monkeypatch,
):
    import crew.tools.process_registry as pr

    path = tmp_path / "private" / "processes.json"
    monkeypatch.setattr(pr, "_checkpoint_path", lambda: path)
    monkeypatch.setattr(pr, "_checkpoint_signing_key", lambda: b"k" * 32)
    secret = "checkpoint-secret-should-not-persist"
    env_canary = "cp-checkpoint-env-canary"
    reg = pr.ProcessRegistry()
    session = _spawn_disabled(
        reg,
        _py_cmd(f"import time; marker={secret!r}; time.sleep(30)"),
        session_key="checkpoint-session",
        owner_account_id="owner-a",
        explicit_environment={"CREW_CP_CANARY": env_canary},
    )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        raw = path.read_text(encoding="utf-8")
        assert set(document) == {"boot_id", "entries", "mac", "schema", "version"}
        assert document["version"] == pr.PROCESS_CHECKPOINT_VERSION
        assert document["schema"] == pr.PROCESS_CHECKPOINT_SCHEMA
        assert len(document["mac"]) == 64
        assert secret not in raw
        assert env_canary not in raw
        assert "command" not in document["entries"][0]
        assert "cwd" not in document["entries"][0]
        assert "env" not in document["entries"][0]
        assert "explicit_environment" not in document["entries"][0]
        assert "output_ref" not in document["entries"][0]
        identity = document["entries"][0]["process_identity"]
        assert identity["create_time"] > 0
        assert "executable" not in identity
        assert len(identity["executable_digest"]) == 64
        assert identity["os_owner"]
        assert document["entries"][0]["workspace_id"] == "workspace-a"
        assert document["entries"][0]["session_key"] == "checkpoint-session"
        assert len(document["entries"][0]["authorization_digest"]) == 64
        assert pr._checkpoint_path_is_secure(path)
    finally:
        reg.kill_process(session.id, owner_account_id="owner-a")


@pytest.mark.parametrize(
    "format_kind",
    ["tampered", "legacy_unsigned", "missing_authorization"],
)
def test_checkpoint_mac_or_legacy_format_is_never_recovered_or_killed(
    tmp_path,
    monkeypatch,
    format_kind,
):
    import crew.tools.process_registry as pr

    path = tmp_path / "processes.json"
    monkeypatch.setattr(pr, "_checkpoint_path", lambda: path)
    monkeypatch.setattr(pr, "_checkpoint_signing_key", lambda: b"k" * 32)
    reg = pr.ProcessRegistry()
    session = _spawn_disabled(
        reg,
        _py_cmd("import time; time.sleep(30)"),
        session_key="signed-session",
        owner_account_id="owner-a",
    )
    try:
        if format_kind == "tampered":
            document = json.loads(path.read_text(encoding="utf-8"))
            document["entries"][0]["owner_account_id"] = "owner-b"
            path.write_text(json.dumps(document), encoding="utf-8")
        elif format_kind == "legacy_unsigned":
            path.write_text(
                json.dumps(
                    [
                        {
                            "session_id": session.id,
                            "pid": session.pid,
                            "owner_account_id": "owner-a",
                        }
                    ]
                ),
                encoding="utf-8",
            )
        else:
            document = json.loads(path.read_text(encoding="utf-8"))
            document["entries"][0]["authorization_digest"] = ""
            payload = {
                "boot_id": document["boot_id"],
                "entries": document["entries"],
                "schema": document["schema"],
                "version": document["version"],
            }
            document["mac"] = pr._checkpoint_mac(payload)
            path.write_text(json.dumps(document), encoding="utf-8")
        recovered = pr.ProcessRegistry()
        assert recovered.recover_from_checkpoint(owner_account_id="owner-a") == 0
        assert recovered.kill_process(session.id, owner_account_id="owner-a")["status"] == "not_found"
        assert pr._pid_alive(session.pid)
    finally:
        reg.kill_process(session.id, owner_account_id="owner-a")


def test_checkpoint_recovery_rejects_cross_owner_pid_reuse_and_executable_replacement(
    tmp_path,
    monkeypatch,
):
    import crew.tools.process_registry as pr

    path = tmp_path / "processes.json"
    monkeypatch.setattr(pr, "_checkpoint_path", lambda: path)
    monkeypatch.setattr(pr, "_checkpoint_signing_key", lambda: b"k" * 32)
    reg = pr.ProcessRegistry()
    session = _spawn_disabled(
        reg,
        _py_cmd("import time; time.sleep(30)"),
        session_key="identity-session",
        owner_account_id="owner-a",
    )
    real_identity = session.process_identity
    real_process_identity = pr._process_identity
    checkpoint = path.read_text(encoding="utf-8")
    try:
        cross_owner = pr.ProcessRegistry()
        assert cross_owner.recover_from_checkpoint(owner_account_id="owner-b") == 0

        monkeypatch.setattr(
            pr,
            "_process_identity",
            lambda _pid: pr.ProcessIdentity(
                create_time=real_identity.create_time + 100,
                executable=real_identity.executable,
                executable_digest=real_identity.executable_digest,
                os_owner=real_identity.os_owner,
            ),
        )
        reused = pr.ProcessRegistry()
        assert reused.recover_from_checkpoint(owner_account_id="owner-a") == 0
        assert pr._pid_alive(session.pid)

        path.write_text(checkpoint, encoding="utf-8")
        monkeypatch.setattr(
            pr,
            "_process_identity",
            lambda _pid: pr.ProcessIdentity(
                create_time=real_identity.create_time,
                executable=real_identity.executable,
                executable_digest="0" * 64,
                os_owner=real_identity.os_owner,
            ),
        )
        replaced_executable = pr.ProcessRegistry()
        assert (
            replaced_executable.recover_from_checkpoint(owner_account_id="owner-a")
            == 0
        )
        assert pr._pid_alive(session.pid)
    finally:
        monkeypatch.setattr(pr, "_process_identity", real_process_identity)
        reg.kill_process(session.id, owner_account_id="owner-a")


def test_checkpoint_recovery_requires_matching_session_and_workspace(
    tmp_path,
    monkeypatch,
):
    import crew.tools.process_registry as pr

    path = tmp_path / "processes.json"
    monkeypatch.setattr(pr, "_checkpoint_path", lambda: path)
    monkeypatch.setattr(pr, "_checkpoint_signing_key", lambda: b"k" * 32)
    reg = pr.ProcessRegistry()
    session = _spawn_disabled(
        reg,
        _py_cmd("import time; time.sleep(30)"),
        session_key="session-a",
        owner_account_id="owner-a",
    )
    try:
        assert (
            pr.ProcessRegistry().recover_from_checkpoint(
                owner_account_id="owner-a",
                session_key="session-b",
                workspace_id="workspace-a",
            )
            == 0
        )
        assert (
            pr.ProcessRegistry().recover_from_checkpoint(
                owner_account_id="owner-a",
                session_key="session-a",
                workspace_id="workspace-b",
            )
            == 0
        )
        assert pr._pid_alive(session.pid)
    finally:
        reg.kill_process(session.id, owner_account_id="owner-a")


def test_detached_kill_refuses_pid_identity_mismatch(monkeypatch):
    import crew.tools.process_registry as pr

    registry = pr.ProcessRegistry()
    session = ProcessSession(
        id="proc_reused",
        command="[recovered]",
        owner_account_id="owner-a",
        workspace_id="workspace-a",
        session_key="session-a",
        pid=4242,
        detached=True,
        started_at=time.time(),
        authorization_digest="c" * 64,
        authorization_generation="d" * 64,
        authorization_expires_at=time.time() + 3600,
        authorization_policy_digest="e" * 64,
        process_identity=pr.ProcessIdentity(
            create_time=10.0,
            executable="/trusted/python",
            executable_digest="a" * 64,
            os_owner="host-user",
        ),
    )
    registry._running[session.id] = session
    monkeypatch.setattr(
        pr,
        "_process_identity",
        lambda _pid: pr.ProcessIdentity(
            create_time=20.0,
            executable="/attacker/process",
            executable_digest="b" * 64,
            os_owner="other-user",
        ),
    )
    monkeypatch.setattr(
        pr,
        "terminate_process_tree",
        lambda _pid: pytest.fail("PID reuse must never be killed"),
    )

    result = registry.kill_process(session.id, owner_account_id="owner-a")

    assert result["status"] == "identity_mismatch"


@pytest.mark.parametrize("quota_kind", ["owner", "global"])
def test_running_process_quota_applies_backpressure_before_popen(
    tmp_path,
    monkeypatch,
    quota_kind,
):
    import crew.tools.process_registry as pr
    from crew.security.runtime_client import NativeRuntimeError, RuntimeErrorCode

    registry = pr.ProcessRegistry()
    registry._running["existing"] = ProcessSession(
        id="existing",
        command="sleep",
        owner_account_id="owner-a" if quota_kind == "owner" else "owner-b",
        pid=111,
    )
    monkeypatch.setattr(pr, "MAX_RUNNING_PROCESSES_PER_OWNER", 1)
    monkeypatch.setattr(pr, "MAX_RUNNING_PROCESSES_GLOBAL", 1)
    monkeypatch.setattr(
        pr.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("quota overflow reached Popen"),
    )
    launch = issue_process_launch(
        SecurityContext(
            os_user="host-user",
            owner_account_id="owner-a",
            workspace_id="workspace-a",
            workspace_root=tmp_path,
            session_id="session-a",
            request_id="request-a",
            task_id="task-a",
            cwd=tmp_path,
        ),
        PermissionProfile(PermissionProfileKind.DISABLED),
    )

    with pytest.raises(NativeRuntimeError) as caught:
        registry.spawn_local(
            "echo blocked",
            launch=launch,
            cwd=str(tmp_path),
            owner_account_id="owner-a",
            session_key="session-a",
        )

    assert caught.value.code is RuntimeErrorCode.PROCESS_LIMIT_REACHED


def test_recover_skips_dead_pid(tmp_path, monkeypatch):
    import crew.tools.process_registry as pr

    monkeypatch.setattr(pr, "_checkpoint_path", lambda: tmp_path / "processes.json")
    # 手写一个指向已死 PID 的 checkpoint（99999 基本不存在）
    (tmp_path / "processes.json").write_text(
        json.dumps([{"session_id": "proc_dead", "command": "x", "pid": 999999, "session_key": "k"}]),
        encoding="utf-8",
    )
    reg = pr.ProcessRegistry()
    assert reg.recover_from_checkpoint() == 0
    assert reg.poll("proc_dead")["status"] == "not_found"


def test_detached_dead_pid_transitions_to_exited(tmp_path, monkeypatch):
    import crew.tools.process_registry as pr

    monkeypatch.setattr(pr, "_checkpoint_path", lambda: tmp_path / "processes.json")
    reg = pr.ProcessRegistry()
    # 起一个短进程并认领为 detached，等它自己退出后 poll 应转 exited
    s = _spawn_disabled(reg, _py_cmd("import time; time.sleep(0.2)"), session_key="d")
    pid = s.pid
    reg2 = pr.ProcessRegistry()
    reg2.recover_from_checkpoint()
    reg2.activate_owner(
        s.owner_account_id,
        authorization_generation=s.authorization_generation,
        authorization_expires_at=s.authorization_expires_at,
    )
    # 等真实进程退出
    deadline = time.monotonic() + 5
    while pr._pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.1)
    r = reg2.poll(s.id)
    assert r["status"] == "exited"
    reg.kill_process(s.id, owner_account_id=s.owner_account_id)


def test_process_tool_handler():
    # 走全局单例（工具 handler 用的是它）
    s = _spawn_disabled(
        process_registry,
        _py_cmd("print('tool-test')"),
        session_key="th",
    )
    _wait_exit(process_registry, s.id)
    out = json.loads(_handle_process({"action": "poll", "session_id": s.id}))
    assert out["status"] == "exited"
    listed = json.loads(_handle_process({"action": "list"}))
    assert any(p["session_id"] == s.id for p in listed["processes"])
    err = json.loads(_handle_process({"action": "poll"}))  # 缺 session_id
    assert "error" in err


def test_process_tool_handler_is_owner_scoped():
    s = _spawn_disabled(
        process_registry,
        _py_cmd("print('tool-owner')"),
        session_key="own",
        owner_account_id="A:uid-a",
    )
    _wait_exit(process_registry, s.id)

    token = current_owner_account_id.set("B:uid-b")
    try:
        listed = json.loads(_handle_process({"action": "list"}))
        assert all(p["session_id"] != s.id for p in listed["processes"])
        polled = json.loads(_handle_process({"action": "poll", "session_id": s.id}))
        assert polled["status"] == "not_found"
        killed = json.loads(_handle_process({"action": "kill", "session_id": s.id}))
        assert killed["status"] == "not_found"
    finally:
        current_owner_account_id.reset(token)


def test_wrong_authenticated_owner_terminates_frozen_orphan():
    import crew.tools.process_registry as pr

    registry = pr.ProcessRegistry()
    session = _spawn_disabled(
        registry,
        _py_cmd("import time; time.sleep(30)"),
        session_key="owner-a-session",
        owner_account_id="owner-a",
    )
    recovered = pr.ProcessRegistry()
    assert recovered.recover_from_checkpoint() == 1
    assert recovered.poll(session.id, owner_account_id="owner-a")["status"] == "not_found"

    result = recovered.activate_owner(
        "owner-b",
        authorization_generation="b" * 64,
        authorization_expires_at=time.time() + 3600,
    )

    assert result["activated"] == 0
    assert result["cleaned"] == 1
    assert not pr._pid_alive(session.pid)
    assert recovered.poll(session.id, owner_account_id="owner-a")["status"] == "not_found"


def test_expired_recovery_authority_is_terminated():
    import crew.tools.process_registry as pr

    registry = pr.ProcessRegistry()
    session = _spawn_disabled(
        registry,
        _py_cmd("import time; time.sleep(30)"),
        session_key="expired-session",
        owner_account_id="owner-a",
    )
    recovered = pr.ProcessRegistry()
    assert recovered.recover_from_checkpoint() == 1

    result = recovered.activate_owner(
        "owner-a",
        authorization_generation=session.authorization_generation,
        authorization_expires_at=session.authorization_expires_at + 300,
        now=session.authorization_expires_at + 1,
    )

    assert result["activated"] == 0
    assert result["cleaned"] == 1
    assert not pr._pid_alive(session.pid)


def test_policy_revocation_terminates_frozen_process(tmp_path):
    import crew.tools.process_registry as pr

    policy = {"digest": "a" * 64}
    audit_events: list[dict] = []

    def configure(registry: pr.ProcessRegistry) -> None:
        registry.configure_lifecycle(
            workspace_root_resolver=lambda _owner, _workspace: tmp_path,
            output_root_resolver=lambda _owner: tmp_path,
            session_validator=lambda *_args: True,
            policy_digest_resolver=lambda *_args: policy["digest"],
            audit_recorder=audit_events.append,
        )

    generation = "b" * 64
    expires_at = time.time() + 3600
    registry = pr.ProcessRegistry()
    configure(registry)
    registry.activate_owner(
        "owner-a",
        authorization_generation=generation,
        authorization_expires_at=expires_at,
    )
    session = _spawn_disabled(
        registry,
        _py_cmd("import time; time.sleep(30)"),
        cwd=str(tmp_path),
        session_key="policy-session",
        owner_account_id="owner-a",
    )

    recovered = pr.ProcessRegistry()
    configure(recovered)
    assert recovered.recover_from_checkpoint() == 1
    policy["digest"] = "c" * 64

    result = recovered.activate_owner(
        "owner-a",
        authorization_generation=generation,
        authorization_expires_at=expires_at,
    )

    assert result["activated"] == 0
    assert result["cleaned"] == 1
    assert not pr._pid_alive(session.pid)
    assert any(
        event["reason"] == "POLICY_GENERATION_REVOKED"
        for event in audit_events
    )


def test_transient_permission_process_is_not_recovered(tmp_path):
    import crew.tools.process_registry as pr
    from crew.security.models import (
        AdditionalPermissionProfile,
        FilesystemAccess,
        FilesystemEntry,
    )

    context = SecurityContext(
        os_user="host-user",
        owner_account_id="owner-a",
        workspace_id="workspace-a",
        workspace_root=tmp_path,
        session_id="transient-session",
        request_id="request-a",
        task_id="task-a",
        cwd=tmp_path,
    )
    launch = issue_process_launch(
        context,
        PermissionProfile(PermissionProfileKind.DISABLED),
        additional_permissions=AdditionalPermissionProfile(
            filesystem=(
                FilesystemEntry(tmp_path, FilesystemAccess.READ),
            ),
        ),
    )
    registry = pr.ProcessRegistry()
    session = registry.spawn_local(
        _py_cmd("import time; time.sleep(30)"),
        launch=launch,
        cwd=str(tmp_path),
        session_key=context.session_id,
        owner_account_id=context.owner_account_id,
    )
    assert not session.authorization_revalidatable

    recovered = pr.ProcessRegistry()
    assert recovered.recover_from_checkpoint() == 0
    assert not pr._pid_alive(session.pid)
    assert recovered.poll(session.id)["status"] == "not_found"


def test_checkpoint_writers_are_serialized_and_publish_complete_snapshot(
    tmp_path,
    monkeypatch,
):
    import crew.tools.process_registry as pr

    path = tmp_path / "processes.json"
    monkeypatch.setattr(pr, "_checkpoint_path", lambda: path)
    registry = pr.ProcessRegistry()
    now = time.time()
    identity = pr.ProcessIdentity(now, str(tmp_path / "python"), "a" * 64, "owner")
    for index in range(12):
        session = ProcessSession(
            id=f"proc_{index:02d}",
            command=f"secret-command-{index}",
            owner_account_id="owner-a",
            workspace_id="workspace-a",
            session_key="session-a",
            pid=1000 + index,
            started_at=now,
            process_identity=identity,
            authorization_digest="b" * 64,
            authorization_generation="c" * 64,
            authorization_expires_at=now + 3600,
            authorization_policy_digest="d" * 64,
            sandbox_preference="auto",
        )
        registry._running[session.id] = session

    real_write = pr._write_checkpoint_document
    active_writers = 0
    max_active_writers = 0
    writer_lock = threading.Lock()

    def observed_write(target, document):
        nonlocal active_writers, max_active_writers
        with writer_lock:
            active_writers += 1
            max_active_writers = max(max_active_writers, active_writers)
        try:
            time.sleep(0.01)
            real_write(target, document)
        finally:
            with writer_lock:
                active_writers -= 1

    monkeypatch.setattr(pr, "_write_checkpoint_document", observed_write)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _index: registry._write_checkpoint(required=True), range(24)))

    document = json.loads(path.read_text(encoding="utf-8"))
    assert max_active_writers == 1
    assert [entry["session_id"] for entry in document["entries"]] == [
        f"proc_{index:02d}" for index in range(12)
    ]
    assert document["mac"] == pr._checkpoint_mac(
        {
            "boot_id": document["boot_id"],
            "entries": document["entries"],
            "schema": document["schema"],
            "version": document["version"],
        }
    )


def test_cleanup_failure_retains_tombstone_until_retry(tmp_path, monkeypatch):
    import crew.tools.process_registry as pr

    path = tmp_path / "processes.json"
    monkeypatch.setattr(pr, "_checkpoint_path", lambda: path)
    registry = pr.ProcessRegistry()
    now = time.time()
    identity = pr.ProcessIdentity(now, str(tmp_path / "python"), "a" * 64, "owner")
    session = ProcessSession(
        id="proc_cleanup",
        command="secret",
        owner_account_id="owner-a",
        workspace_id="workspace-a",
        session_key="session-a",
        pid=4242,
        started_at=now,
        process_identity=identity,
        authorization_digest="b" * 64,
        authorization_generation="c" * 64,
        authorization_expires_at=now + 3600,
        authorization_policy_digest="d" * 64,
        sandbox_preference="auto",
    )
    registry._running[session.id] = session
    monkeypatch.setattr(pr, "_process_identity", lambda _pid: identity)
    attempts = 0

    def terminate_once(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise pr.ProcessCheckpointError("simulated cleanup failure")

    monkeypatch.setattr(pr, "_terminate_verified_process_tree", terminate_once)

    with pytest.raises(pr.ProcessCleanupError):
        registry.revoke_session("owner-a", "session-a")
    tombstone = json.loads(path.read_text(encoding="utf-8"))["entries"][0]
    assert tombstone["recovery_state"] == "cleanup_pending"
    assert tombstone["cleanup_reason"] == "SESSION_REVOKED"
    assert tombstone["cleanup_attempts"] == 1

    assert registry.retry_pending_cleanup() == 1
    assert json.loads(path.read_text(encoding="utf-8"))["entries"] == []
    assert session.id not in registry._running


def test_exit_audit_failure_retains_durable_tombstone(tmp_path, monkeypatch):
    import crew.tools.process_registry as pr

    path = tmp_path / "processes.json"
    monkeypatch.setattr(pr, "_checkpoint_path", lambda: path)
    registry = pr.ProcessRegistry()

    def record(payload):
        if payload["event_type"] == "process_exited":
            raise RuntimeError("audit unavailable")

    registry.configure_lifecycle(
        workspace_root_resolver=lambda _owner, _workspace: tmp_path,
        output_root_resolver=lambda _owner: tmp_path,
        session_validator=lambda *_args: True,
        policy_digest_resolver=lambda *_args: "d" * 64,
        audit_recorder=record,
    )
    now = time.time()
    session = ProcessSession(
        id="proc_exit_audit",
        command="secret",
        owner_account_id="owner-a",
        workspace_id="workspace-a",
        session_key="session-a",
        pid=4242,
        started_at=now,
        exited=True,
        process_identity=pr.ProcessIdentity(
            now,
            str(tmp_path / "python"),
            "a" * 64,
            "owner",
        ),
        authorization_digest="b" * 64,
        authorization_generation="c" * 64,
        authorization_expires_at=now + 3600,
        authorization_policy_digest="d" * 64,
        sandbox_preference="auto",
    )
    registry._running[session.id] = session

    registry._move_to_finished(session)

    tombstone = json.loads(path.read_text(encoding="utf-8"))["entries"][0]
    assert tombstone["recovery_state"] == "cleanup_pending"
    assert tombstone["cleanup_reason"] == "AUDIT_WRITE_FAILED"
    assert session.id in registry._running
    assert session.id not in registry._finished


def test_checkpoint_secret_corpus_is_never_serialized(tmp_path, monkeypatch):
    import crew.tools.process_registry as pr

    path = tmp_path / "processes.json"
    monkeypatch.setattr(pr, "_checkpoint_path", lambda: path)
    secrets = (
        "Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature",
        "sk-live-0123456789abcdef",
        "password=hunter2",
        "AKIAIOSFODNN7EXAMPLE",
        "cookie=session-secret",
    )
    now = time.time()
    registry = pr.ProcessRegistry()
    session = ProcessSession(
        id="proc_secret_corpus",
        command=" ".join(secrets),
        owner_account_id="owner-a",
        workspace_id="workspace-a",
        session_key="session-a",
        pid=5151,
        cwd=str(tmp_path / secrets[1]),
        output_ref=str(tmp_path / secrets[2]),
        started_at=now,
        process_identity=pr.ProcessIdentity(
            now,
            str(tmp_path / secrets[3]),
            "a" * 64,
            "host-owner",
        ),
        authorization_digest="b" * 64,
        authorization_generation="c" * 64,
        authorization_expires_at=now + 3600,
        authorization_policy_digest="d" * 64,
        sandbox_preference="auto",
        _secret_values=secrets,
    )
    registry._running[session.id] = session

    registry._write_checkpoint(required=True)

    raw = path.read_text(encoding="utf-8")
    assert all(secret not in raw for secret in secrets)
    entry = json.loads(raw)["entries"][0]
    assert set(entry) == {
        "authorization_digest",
        "authorization_expires_at",
        "authorization_generation",
        "authorization_policy_digest",
        "authorization_revalidatable",
        "cleanup_attempts",
        "cleanup_reason",
        "notify_on_complete",
        "owner_account_id",
        "pid",
        "process_identity",
        "recovery_state",
        "sandbox_preference",
        "sandbox_system_surface",
        "sandboxed",
        "session_id",
        "session_key",
        "started_at",
        "task_id",
        "workspace_id",
    }


def test_terminate_process_tree_kills_descendants(tmp_path):
    import subprocess

    import psutil

    import crew.tools.process_registry as pr

    helper = tmp_path / "spawner.py"
    helper.write_text(
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    parent = subprocess.Popen(
        [sys.executable, str(helper)],
        stdout=subprocess.PIPE,
        text=True,
    )
    child_pid = -1
    try:
        child_pid = int(parent.stdout.readline().strip())
        pids = (parent.pid, child_pid)
        assert all(psutil.pid_exists(pid) for pid in pids)

        pr.terminate_process_tree(parent.pid)
        deadline = time.time() + 5
        for pid in pids:
            while time.time() < deadline:
                if not psutil.pid_exists(pid):
                    break
                time.sleep(0.05)
            assert not psutil.pid_exists(pid), f"pid {pid} still alive"
    finally:
        for pid in (parent.pid, child_pid):
            if pid > 0 and psutil.pid_exists(pid):
                pr.terminate_process_tree(pid)
        parent.stdout.close()
        try:
            parent.wait(timeout=5)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
