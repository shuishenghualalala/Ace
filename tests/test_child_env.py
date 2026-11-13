"""派生环境白名单测试：宿主密钥不得进入 shell 子进程。"""

from __future__ import annotations

import json
import re

import pytest

from crew.tools import child_env
from crew.tools.child_env import (
    PassThroughRegistry,
    build_spawn_env,
    pass_through_registry,
)

_SECRET_KEY_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD)", re.IGNORECASE)


def _host_env() -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/host",
        "LANG": "en_US.UTF-8",
        "LC_CTYPE": "en_US.UTF-8",
        "TERM": "xterm-256color",
        "TZ": "UTC",
        "HTTP_PROXY": "http://proxy.local:8080",
        "USER": "host-user",
        # 宿主机密：一律不得进入派生环境
        "OPENAI_API_KEY": "sk-host-secret",
        "GITHUB_TOKEN": "ghp-host-secret",
        "AWS_SECRET_ACCESS_KEY": "aws-host-secret",
        "DB_PASSWORD": "db-host-secret",
        # ambient ACE_* 覆盖：一律丢弃
        "ACE_PROFILE": "hijack",
        "ACE_OWNER_TOKEN": "ace-ambient-secret",
        # 词表外普通变量
        "VIRTUAL_ENV": "/opt/venv",
        "STARSHIP_SESSION_KEY": "starship",
    }


def test_whitelist_drops_host_secrets_and_ace_namespace() -> None:
    env = build_spawn_env(base=_host_env())

    leaked = [key for key in env if _SECRET_KEY_RE.search(key)]
    assert leaked == []
    assert not any(key.startswith("ACE_") for key in env)
    # 词表外普通变量同样不进入
    assert "VIRTUAL_ENV" not in env
    assert "STARSHIP_SESSION_KEY" not in env


def test_whitelist_keeps_operational_variables() -> None:
    env = build_spawn_env(base=_host_env())

    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/home/host"
    assert env["LANG"] == "en_US.UTF-8"
    assert env["LC_CTYPE"] == "en_US.UTF-8"
    assert env["TERM"] == "xterm-256color"
    assert env["TZ"] == "UTC"
    assert env["HTTP_PROXY"] == "http://proxy.local:8080"
    assert env["USER"] == "host-user"


def test_overrides_are_applied_last() -> None:
    env = build_spawn_env(
        {"CREW_HOME": "/tmp/crew", "PYTHONUNBUFFERED": "1"},
        base=_host_env(),
    )
    assert env["CREW_HOME"] == "/tmp/crew"
    assert env["PYTHONUNBUFFERED"] == "1"
    # 覆盖值可以替换白名单变量（受信注册表快照优先于 ambient）
    env = build_spawn_env({"PATH": "/safe/bin"}, base=_host_env())
    assert env["PATH"] == "/safe/bin"


def test_each_spawn_rebuilds_from_current_ambient(monkeypatch: pytest.MonkeyPatch) -> None:
    ambient = _host_env()
    monkeypatch.setattr(child_env.os, "environ", ambient)
    first = build_spawn_env()
    ambient["TZ"] = "Asia/Shanghai"
    ambient["NEW_HOST_SECRET"] = "later-secret"
    second = build_spawn_env()
    # 每次 spawn 重建：后续 ambient 变化被反映，但机密依旧进不来
    assert second["TZ"] == "Asia/Shanghai"
    assert "NEW_HOST_SECRET" not in second
    assert first["TZ"] == "UTC"


def test_pass_through_register_conflict_and_enumerate() -> None:
    registry = PassThroughRegistry()
    unregister = registry.register("DOCKER_HOST", owner="docker-skill")
    entries = registry.list()
    assert [(item.key, item.owner) for item in entries] == [("DOCKER_HOST", "docker-skill")]

    with pytest.raises(ValueError, match="已属"):
        registry.register("DOCKER_HOST", owner="other-skill")
    with pytest.raises(ValueError, match="非空属主"):
        registry.register("SOME_VAR", owner="")

    env = build_spawn_env(base=_host_env() | {"DOCKER_HOST": "tcp://127.0.0.1:2375"}, registry=registry)
    assert env["DOCKER_HOST"] == "tcp://127.0.0.1:2375"

    unregister()
    assert registry.list() == []
    env = build_spawn_env(base=_host_env() | {"DOCKER_HOST": "tcp://127.0.0.1:2375"}, registry=registry)
    assert "DOCKER_HOST" not in env


def test_global_registry_is_enumerable() -> None:
    unregister = pass_through_registry.register("TEST_PASSTHROUGH_VAR", owner="child-env-test")
    try:
        keys = {item.key for item in pass_through_registry.list()}
        assert "TEST_PASSTHROUGH_VAR" in keys
    finally:
        unregister()


def test_windows_case_folding_for_keys_and_ownership(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(child_env, "_IS_WINDOWS", True)
    # 折叠后匹配白名单
    env = build_spawn_env(base={"path": "/usr/bin", "home": "/home/host"})
    assert env == {"path": "/usr/bin", "home": "/home/host"}
    # 折叠后去重：同键不同大小写只保留首个
    env = build_spawn_env(base={"Path": "/a", "PATH": "/b"})
    assert len(env) == 1
    # 折叠后属主冲突即抛
    registry = PassThroughRegistry()
    registry.register("MyVar", owner="owner-a")
    with pytest.raises(ValueError, match="已属"):
        registry.register("MYVAR", owner="owner-b")


def test_spawn_local_child_env_excludes_host_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """端到端：spawn_local 派生环境不含 KEY/TOKEN/SECRET/PASSWORD 变量。"""
    import sys

    from crew.tools.process_registry import ProcessRegistry

    ambient = _host_env()
    monkeypatch.setattr(child_env.os, "environ", ambient)
    registry = ProcessRegistry()
    command = (
        f"{sys.executable} -c "
        "\"import os,json;print(json.dumps(sorted(os.environ)))\""
    )
    session = registry.spawn_local(
        command,
        session_key="env-check",
        owner_account_id="owner-a",
    )
    try:
        assert session.process is not None
        session.process.wait(timeout=30)
        keys = json.loads(session.output_buffer.strip().splitlines()[-1])
        leaked = [key for key in keys if _SECRET_KEY_RE.search(key)]
        assert leaked == [], f"派生环境泄漏宿主机密变量: {leaked}"
        assert not any(key.startswith("ACE_") for key in keys)
        assert "PATH" in keys and "HOME" in keys
    finally:
        registry.kill_process(session.id, owner_account_id=session.owner_account_id)
