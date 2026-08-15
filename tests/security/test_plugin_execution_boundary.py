from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from crew.core.runctx import (
    current_owner_account_id,
    current_request_id,
    current_session_id,
)
from crew.plugins import manager as plugin_manager
from crew.plugins.manager import CommandAttribution, PluginManager
from crew.plugins.security import PluginSecurityError
from crew.security.capability_discovery import capability_discovery_slot
from crew.tools.registry import Registry


def _write_plugin(
    root: Path,
    name: str,
    *,
    capabilities: tuple[str, ...] = ("commands",),
    commands: tuple[str, ...] = ("status",),
    body: str = "",
) -> Path:
    plugin = root / name
    plugin.mkdir(parents=True)
    lines = [
        "schema_version: crew.plugin.v1",
        f"name: {name}",
        'version: "1.0.0"',
        "kind: standalone",
        "capabilities:",
        *(f"  - {item}" for item in capabilities),
    ]
    if commands:
        lines.extend(["provides_commands:", *(f"  - {item}" for item in commands)])
    (plugin / "plugin.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (plugin / "__init__.py").write_text(
        body
        or (
            f"def command(raw_args):\n    return {name!r}\n\n"
            "def register(ctx):\n"
            "    ctx.register_command('status', command)\n"
        ),
        encoding="utf-8",
    )
    return plugin


def _trusted_manager(root: Path) -> PluginManager:
    with patch.object(plugin_manager, "get_bundled_plugins_dir", return_value=root):
        return PluginManager(
            registry=Registry(),
            developer_mode=True,
            audit_path=root.parent / "plugin-audit.jsonl",
        )


def test_discovery_snapshot_is_bounded_and_contains_member_digests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "plugins"
    plugin = _write_plugin(root, "alpha")
    manager = _trusted_manager(root)

    snapshot = manager.discover_snapshot([root])

    assert snapshot.members[0].tree_sha256
    assert snapshot.members[0].manifest_sha256
    assert {
        item.relative_path for item in snapshot.members[0].files
    } == {"__init__.py", "plugin.yaml"}
    assert all(item.sha256 for item in snapshot.members[0].files)
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.snapshot_id = "changed"  # type: ignore[misc]

    total_bytes = sum(path.stat().st_size for path in plugin.iterdir() if path.is_file())
    monkeypatch.setattr(
        plugin_manager,
        "_PLUGIN_DISCOVERY_MAX_AGGREGATE_BYTES",
        total_bytes - 1,
    )
    with pytest.raises(PluginSecurityError, match="aggregate byte"):
        _trusted_manager(root).discover_snapshot([root])


@pytest.mark.parametrize(
    ("constant", "value", "message"),
    [
        ("_PLUGIN_DISCOVERY_MAX_DEPTH", 0, "depth"),
        ("_PLUGIN_DISCOVERY_MAX_DIRECTORIES", 1, "directory"),
        ("_PLUGIN_DISCOVERY_MAX_FILES", 1, "file"),
        ("_PLUGIN_DISCOVERY_MAX_BUNDLES", 1, "bundle"),
        ("_PLUGIN_DISCOVERY_MAX_ENTRIES", 1, "entry"),
    ],
)
def test_discovery_flood_budgets_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
    value: int,
    message: str,
) -> None:
    root = tmp_path / "plugins"
    _write_plugin(root, "alpha")
    _write_plugin(root, "bravo")
    monkeypatch.setattr(plugin_manager, constant, value)

    with pytest.raises(PluginSecurityError, match=message):
        _trusted_manager(root).discover_snapshot([root])


def test_plugin_and_skill_discovery_share_one_concurrency_slot(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    _write_plugin(root, "alpha")
    manager = _trusted_manager(root)

    with capability_discovery_slot(), pytest.raises(PluginSecurityError) as denied:
        manager.discover_snapshot([root])

    assert denied.value.code == "plugin_discovery_concurrency_limit"


def test_discovery_failure_is_memoized_for_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "plugins"
    _write_plugin(root, "alpha")
    manager = _trusted_manager(root)
    attempts = 0

    def fail(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise PluginSecurityError("budget exhausted", code="plugin_discovery_limit")

    monkeypatch.setattr(plugin_manager, "snapshot_plugin_roots", fail)
    token = current_request_id.set("plugin-discovery-failure")
    try:
        for _ in range(2):
            with pytest.raises(PluginSecurityError, match="budget exhausted"):
                manager.discover_snapshot([root])
    finally:
        current_request_id.reset(token)

    assert attempts == 1


def test_request_snapshot_rejects_swap_and_refreshes_next_request(
    tmp_path: Path,
) -> None:
    root = tmp_path / "plugins"
    plugin = _write_plugin(root, "alpha")
    manager = _trusted_manager(root)
    owner_token = current_owner_account_id.set("owner-plugin")
    session_token = current_session_id.set("session-plugin")
    request_token = current_request_id.set("request-one")
    try:
        first = manager.discover_snapshot([root])
        old = tmp_path / "alpha-old"
        plugin.rename(old)
        replacement = _write_plugin(root, "alpha")
        marker = tmp_path / "swapped-imported.txt"
        (replacement / "__init__.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('unsafe')\n",
            encoding="utf-8",
        )

        assert manager.discover_snapshot([root]) is first
        with pytest.raises(PluginSecurityError) as stale:
            manager.discover_and_load([root], enabled=["alpha"])
        assert stale.value.code == "plugin_discovery_snapshot_stale"
        assert not marker.exists()

        current_request_id.set("request-two")
        refreshed = manager.discover_snapshot([root])
        assert refreshed.snapshot_id != first.snapshot_id
    finally:
        current_request_id.reset(request_token)
        current_session_id.reset(session_token)
        current_owner_account_id.reset(owner_token)


def test_discovery_rejects_link_or_reparse_inside_plugin_tree(
    tmp_path: Path,
) -> None:
    root = tmp_path / "plugins"
    plugin = _write_plugin(root, "linked")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    try:
        (plugin / "escape.txt").symlink_to(outside)
    except OSError:
        pytest.skip("link creation unavailable on this host")

    with pytest.raises(PluginSecurityError, match="link or reparse point"):
        _trusted_manager(root).discover_snapshot([root])


def test_command_collision_is_rejected_and_first_attribution_survives(
    tmp_path: Path,
) -> None:
    root = tmp_path / "plugins"
    _write_plugin(root, "alpha")
    _write_plugin(root, "bravo")
    manager = _trusted_manager(root)

    manager.discover_and_load([root], enabled=["alpha", "bravo"])

    alpha = manager.get_plugin("alpha")
    bravo = manager.get_plugin("bravo")
    assert alpha is not None and alpha.enabled
    assert bravo is not None and not bravo.enabled
    assert "collides" in str(bravo.error)
    entry = manager.plugin_commands["status"]
    assert entry["plugin"] == "alpha"
    assert isinstance(entry["attribution"], CommandAttribution)


def test_command_collision_with_builtin_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    _write_plugin(
        root,
        "alpha",
        commands=("help",),
        body=(
            "def command(raw_args):\n"
            "    return 'shadowed'\n\n"
            "def register(ctx):\n"
            "    ctx.register_command('help', command)\n"
        ),
    )
    manager = _trusted_manager(root)

    manager.discover_and_load([root], enabled=["alpha"])

    loaded = manager.get_plugin("alpha")
    assert loaded is not None and not loaded.enabled
    assert "built-in" in str(loaded.error)
    assert "help" not in manager.plugin_commands


async def test_command_attribution_rejects_stale_tree_before_handler_runs(
    tmp_path: Path,
) -> None:
    root = tmp_path / "plugins"
    plugin = _write_plugin(root, "alpha")
    manager = _trusted_manager(root)
    manager.discover_and_load([root], enabled=["alpha"])
    assert await manager.run_plugin_command("/status") == "alpha"

    (plugin / "__init__.py").write_text(
        "raise RuntimeError('replacement must not execute')\n",
        encoding="utf-8",
    )

    assert (
        await manager.run_plugin_command("/status")
        == "插件命令 /status 已拒绝：安全归属失效"
    )
    loaded = manager.get_plugin("alpha")
    assert loaded is not None and not loaded.enabled
    assert manager.plugin_commands == {}


async def test_plugin_command_inherits_parent_owner_capability_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "plugins"
    marker = tmp_path / "command-ran.txt"
    _write_plugin(
        root,
        "alpha",
        body=(
            "from pathlib import Path\n"
            "def command(raw_args):\n"
            f"    Path({str(marker)!r}).write_text('ran')\n"
            "    return 'ran'\n\n"
            "def register(ctx):\n"
            "    ctx.register_command('status', command)\n"
        ),
    )

    class Preferences:
        @staticmethod
        def get_enabled(_owner: str, _key: str) -> bool:
            return True

    config = SimpleNamespace(
        access_control=SimpleNamespace(
            resolve_for=lambda _user_type: {"enabled_plugins": []},
        )
    )
    monkeypatch.setattr(plugin_manager, "get_bundled_plugins_dir", lambda: root)
    manager = PluginManager(
        registry=Registry(),
        services={"config": config, "plugin_prefs": Preferences()},
        developer_mode=True,
        audit_path=tmp_path / "plugin-audit.jsonl",
    )
    manager.discover_and_load([root], enabled=["alpha"])

    result = await manager.run_plugin_command(
        "/status",
        owner_account_id="owner-denied",
        user_type="external",
    )

    assert result == "插件命令 /status 已拒绝：安全归属失效"
    assert not marker.exists()


def test_command_attribution_rejects_handler_outside_plugin_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "plugins"
    outside = tmp_path / "outside_command.py"
    outside.write_text("def command(raw_args):\n    return 'outside'\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_plugin(
        root,
        "alpha",
        body=(
            "from outside_command import command\n\n"
            "def register(ctx):\n"
            "    ctx.register_command('status', command)\n"
        ),
    )
    manager = _trusted_manager(root)

    manager.discover_and_load([root], enabled=["alpha"])

    loaded = manager.get_plugin("alpha")
    assert loaded is not None and not loaded.enabled
    assert "escapes" in str(loaded.error)
    assert manager.plugin_commands == {}


def test_untrusted_capabilities_cannot_run_direct_host_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "plugins"
    marker = tmp_path / "host-io.txt"
    calls: list[str] = []

    monkeypatch.setattr(
        "socket.create_connection",
        lambda *_args, **_kwargs: calls.append("network"),
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: calls.append("process"),
    )
    if hasattr(os, "fork"):
        monkeypatch.setattr(os, "fork", lambda: calls.append("fork"))

    body = (
        "from pathlib import Path\n"
        "import os\n"
        "import socket\n"
        "import subprocess\n"
        f"Path({str(marker)!r}).write_text('filesystem')\n"
        "socket.create_connection(('127.0.0.1', 9))\n"
        "subprocess.Popen(['not-a-real-command'])\n"
        "if hasattr(os, 'fork'):\n"
        "    os.fork()\n"
        "def register(ctx):\n"
        "    ctx.register_hook('pre_tool_call', lambda **kwargs: None)\n"
    )
    _write_plugin(
        root,
        "host-io",
        capabilities=("hooks", "filesystem", "network", "process"),
        commands=(),
        body=body,
    )
    manager = PluginManager(
        registry=Registry(),
        developer_mode=True,
        audit_path=tmp_path / "plugin-audit.jsonl",
    )

    manager.discover_and_load([root], enabled=["host-io"])

    loaded = manager.get_plugin("host-io")
    assert loaded is not None and not loaded.enabled and loaded.declarative_only
    assert loaded.hooks_registered == []
    assert calls == []
    assert not marker.exists()
    assert "crew_runtime_plugins.host_io" not in sys.modules


def test_untrusted_infinite_loop_is_never_imported(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    _write_plugin(
        root,
        "loop",
        capabilities=("hooks",),
        commands=(),
        body="while True:\n    pass\n",
    )
    code = (
        "from crew.plugins.manager import PluginManager\n"
        "from crew.tools.registry import Registry\n"
        f"m=PluginManager(registry=Registry(), developer_mode=True, "
        f"audit_path={str(tmp_path / 'audit.jsonl')!r})\n"
        f"m.discover_and_load([{str(root)!r}], enabled=['loop'])\n"
        "p=m.get_plugin('loop')\n"
        "raise SystemExit(0 if p is not None and not p.enabled else 3)\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_untrusted_executable_cleanup_retains_only_declarative_assets(
    tmp_path: Path,
) -> None:
    root = tmp_path / "plugins"
    plugin = _write_plugin(
        root,
        "mixed",
        capabilities=("skills", "hooks", "filesystem", "network", "process"),
        commands=(),
        body="raise RuntimeError('must never import')\n",
    )
    skill = plugin / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\n---\nbody\n", encoding="utf-8")
    manager = PluginManager(
        registry=Registry(),
        developer_mode=True,
        audit_path=tmp_path / "plugin-audit.jsonl",
    )

    manager.discover_and_load([root], enabled=["mixed"])

    loaded = manager.get_plugin("mixed")
    assert loaded is not None and loaded.enabled and loaded.declarative_only
    assert loaded.hooks_registered == []
    assert manager.plugin_skill_roots() == [str((plugin / "skills").resolve())]
    assert manager.unload_plugin("mixed")
    assert manager.plugin_skill_roots() == []
    assert "crew_runtime_plugins.mixed" not in sys.modules


def test_configured_local_execution_trust_cannot_authorize_host_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "plugins"
    marker = tmp_path / "local-imported.txt"
    host_calls: list[str] = []
    monkeypatch.setattr(
        "socket.create_connection",
        lambda *_args, **_kwargs: host_calls.append("network"),
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: host_calls.append("process"),
    )
    _write_plugin(
        root,
        "configured-local",
        capabilities=("hooks", "filesystem", "network", "process"),
        commands=(),
        body=(
            "from pathlib import Path\n"
            "import socket\n"
            "import subprocess\n"
            f"Path({str(marker)!r}).write_text('filesystem')\n"
            "socket.create_connection(('127.0.0.1', 9))\n"
            "subprocess.Popen(['must-not-run'])\n"
            "def register(ctx):\n"
            "    ctx.register_hook('pre_tool_call', lambda **kwargs: None)\n"
        ),
    )
    manager = PluginManager(
        registry=Registry(),
        developer_mode=True,
        audit_path=tmp_path / "plugin-audit.jsonl",
        trusted_executable_roots={root},
    )

    manager.discover_and_load([root], enabled=["configured-local"])

    loaded = manager.get_plugin("configured-local")
    assert loaded is not None and not loaded.enabled and loaded.declarative_only
    assert loaded.hooks_registered == []
    assert host_calls == []
    assert not marker.exists()
    assert "crew_runtime_plugins.configured_local" not in sys.modules


def test_nonbundled_code_cannot_register_executable_surfaces(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    plugin = _write_plugin(
        root,
        "blocked-surfaces",
        capabilities=("tools", "hooks", "middleware", "commands", "api_router"),
        commands=("blocked",),
        body=(
            "from fastapi import APIRouter\n"
            "def handler(args):\n"
            "    return 'blocked'\n"
            "def callback(**kwargs):\n"
            "    return None\n"
            "def command(raw_args):\n"
            "    return 'blocked'\n"
            "def register(ctx):\n"
            "    ctx.register_tool(\n"
            "        name='blocked_tool', toolset='blocked',\n"
            "        schema={'name': 'blocked_tool', 'description': 'blocked', "
            "'parameters': {'type': 'object', 'properties': {}}},\n"
            "        handler=handler,\n"
            "    )\n"
            "    ctx.register_hook('pre_tool_call', callback)\n"
            "    ctx.register_middleware('tool_request', callback)\n"
            "    ctx.register_command('blocked', command)\n"
            "    ctx.register_api_router(APIRouter())\n"
        ),
    )
    manifest = (plugin / "plugin.yaml").read_text("utf-8")
    (plugin / "plugin.yaml").write_text(
        manifest
        + "provides_tools:\n"
        + "  - blocked_tool\n"
        + "provides_hooks:\n"
        + "  - pre_tool_call\n"
        + "provides_middleware:\n"
        + "  - tool_request\n",
        encoding="utf-8",
    )
    registry = Registry()
    manager = PluginManager(
        registry=registry,
        developer_mode=True,
        audit_path=tmp_path / "plugin-audit.jsonl",
        trusted_executable_roots={root},
    )

    manager.discover_and_load([root], enabled=["blocked-surfaces"])

    loaded = manager.get_plugin("blocked-surfaces")
    assert loaded is not None and not loaded.enabled and loaded.declarative_only
    assert registry.names() == []
    assert manager._hooks == {}
    assert manager._middleware == {}
    assert manager.plugin_commands == {}
    assert manager.api_routers == []
    assert "crew_runtime_plugins.blocked_surfaces" not in sys.modules


def test_load_module_rejects_forged_bundled_source_before_import(
    tmp_path: Path,
) -> None:
    root = tmp_path / "plugins"
    marker = tmp_path / "forged-imported.txt"
    _write_plugin(
        root,
        "forged",
        body=(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('executed')\n"
            "def register(ctx):\n"
            "    pass\n"
        ),
    )
    manager = PluginManager(
        registry=Registry(),
        developer_mode=True,
        audit_path=tmp_path / "plugin-audit.jsonl",
    )
    snapshot = manager.discover_snapshot([root])
    manifest = manager._read_manifest_from_snapshot(
        snapshot.members[0],
        snapshot_id=snapshot.snapshot_id,
    )
    assert manifest is not None
    manifest.source = "bundled"
    manifest.execution_trusted = True

    with pytest.raises(PluginSecurityError) as denied:
        manager._load_module(manifest)

    assert denied.value.code == "plugin_execution_untrusted"
    assert not marker.exists()
    assert "crew_runtime_plugins.forged" not in sys.modules


async def test_activation_audit_binds_contract_and_replay_is_revoked(
    tmp_path: Path,
) -> None:
    root = tmp_path / "plugins"
    _write_plugin(root, "audited")
    manager = _trusted_manager(root)

    manager.discover_and_load([root], enabled=["audited"])

    loaded = manager.get_plugin("audited")
    assert loaded is not None and loaded.enabled
    records = [
        json.loads(line)
        for line in (tmp_path / "plugin-audit.jsonl").read_text("utf-8").splitlines()
    ]
    activation = next(
        record
        for record in reversed(records)
        if record["action"] == "activate_plugin" and record["result"] == "success"
    )
    assert activation["source"] == "bundled"
    assert activation["version"] == "1.0.0"
    assert activation["capabilities"] == ["commands"]
    assert activation["tree_sha256"] == loaded.manifest.tree_sha256
    assert activation["manifest_sha256"] == loaded.manifest.manifest_sha256
    assert activation["discovery_snapshot_id"] == loaded.manifest.discovery_snapshot_id
    assert activation["execution_mode"] == "gateway_in_process"
    assert len(activation["binding_sha256"]) == 64

    loaded.manifest.version = "0.9.0"
    assert (
        await manager.run_plugin_command("/status")
        == "插件命令 /status 已拒绝：安全归属失效"
    )
    assert not loaded.enabled
    assert manager.plugin_commands == {}


def test_bundled_code_does_not_import_when_activation_audit_is_unavailable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "plugins"
    marker = tmp_path / "unaudited-imported.txt"
    _write_plugin(
        root,
        "unaudited",
        body=(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('executed')\n"
            "def register(ctx):\n"
            "    pass\n"
        ),
    )
    audit_directory = tmp_path / "audit-directory"
    audit_directory.mkdir()
    with patch.object(plugin_manager, "get_bundled_plugins_dir", return_value=root):
        manager = PluginManager(
            registry=Registry(),
            audit_path=audit_directory,
        )

    manager.discover_and_load([root], enabled=["unaudited"])

    loaded = manager.get_plugin("unaudited")
    assert loaded is not None and not loaded.enabled
    assert "audit is unavailable" in str(loaded.error)
    assert not marker.exists()
    assert "crew_runtime_plugins.unaudited" not in sys.modules


def test_manifest_cannot_forge_bundled_source_authority(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    plugin = _write_plugin(root, "forged-source")
    manifest = (plugin / "plugin.yaml").read_text("utf-8")
    (plugin / "plugin.yaml").write_text(
        manifest + "source: bundled\n",
        encoding="utf-8",
    )
    marker = tmp_path / "forged-source-imported.txt"
    (plugin / "__init__.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    manager = PluginManager(
        registry=Registry(),
        developer_mode=True,
        audit_path=tmp_path / "plugin-audit.jsonl",
        trusted_executable_roots={root},
    )

    manager.discover_and_load([root], enabled=["forged-source"])

    loaded = manager.get_plugin("forged-source")
    assert loaded is not None and not loaded.enabled
    assert "unsupported fields" in str(loaded.error)
    assert not marker.exists()
