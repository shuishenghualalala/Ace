"""Plugin supply-chain, manifest, capability, and lifecycle security tests."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import sys
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from crew.plugins import manager as plugin_manager
from crew.plugins import security as plugin_security
from crew.plugins.manager import PluginManager, PluginSecurityError
from crew.security.outbound import OutboundHttpClient, OutboundHttpResponse, OutboundPolicy
from crew.tools.registry import Registry

_SIGNATURE_FILE = "plugin.sig.json"
_PROVENANCE_FILE = ".ace-plugin-provenance.json"
_SIGNATURE_DOMAIN = b"crew.plugin.bundle.v1\0"


def test_remote_plugin_download_uses_shared_pinned_http_client(monkeypatch) -> None:
    seen: dict[str, object] = {}

    class PinnedClient:
        def fetch(self, url: str, **kwargs):
            seen["url"] = url
            seen["kwargs"] = kwargs
            return OutboundHttpResponse(
                final_url=url,
                status=200,
                headers={"content-length": "3"},
                body=b"zip",
                content_type="application/zip",
                charset="utf-8",
            )

    monkeypatch.setattr(plugin_security, "_PLUGIN_HTTP", PinnedClient(), raising=False)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))
        ],
    )

    bundle, provenance = plugin_security.download_plugin_bundle(
        "https://plugins.example.test/bundle.zip?version=1"
    )

    assert bundle == b"zip"
    assert provenance == "https://plugins.example.test/bundle.zip"
    assert seen["url"] == "https://plugins.example.test/bundle.zip?version=1"
    assert seen["kwargs"] == {
        "method": "GET",
        "headers": {"Accept": "application/zip"},
        "timeout": 30.0,
        "max_bytes": plugin_security.MAX_PLUGIN_BUNDLE_BYTES,
        "max_redirects": 0,
    }


def test_remote_plugin_download_rejects_private_dns_before_socket(
    monkeypatch,
) -> None:
    socket_calls = 0

    def socket_factory(*_args):
        nonlocal socket_calls
        socket_calls += 1
        raise AssertionError("private plugin target must not create a socket")

    policy = OutboundPolicy(
        resolver=lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.7", 443))
        ],
        socket_factory=socket_factory,
    )
    monkeypatch.setattr(plugin_security, "_PLUGIN_OUTBOUND", policy)
    monkeypatch.setattr(plugin_security, "_PLUGIN_HTTP", OutboundHttpClient(policy))

    with pytest.raises(PluginSecurityError) as denied:
        plugin_security.download_plugin_bundle(
            "https://plugins.example.test/bundle.zip"
        )

    assert denied.value.code == "source_url_private"
    assert socket_calls == 0


def test_remote_plugin_url_rejects_boundary_whitespace() -> None:
    with pytest.raises(PluginSecurityError) as denied:
        plugin_security.normalized_remote_plugin_url(
            " https://plugins.example.test/bundle.zip",
            resolve_dns=False,
        )

    assert denied.value.code == "source_url_invalid"


def _tree_digest(plugin_dir: Path) -> str:
    digest = hashlib.sha256()
    ignored = {_SIGNATURE_FILE, _PROVENANCE_FILE}
    files = sorted(
        path
        for path in plugin_dir.rglob("*")
        if path.is_file()
        and path.name not in ignored
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
    )
    for path in files:
        relative = path.relative_to(plugin_dir).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _manifest(
    name: str,
    *,
    version: str = "1.0.0",
    capabilities: tuple[str, ...] = ("tools",),
    provides_tools: tuple[str, ...] = ("signed_echo",),
    provides_hooks: tuple[str, ...] = (),
) -> str:
    lines = [
        "schema_version: crew.plugin.v1",
        f"name: {name}",
        f'version: "{version}"',
        "kind: standalone",
        "capabilities:",
        *(f"  - {item}" for item in capabilities),
    ]
    if provides_tools:
        lines.extend(["provides_tools:", *(f"  - {item}" for item in provides_tools)])
    if provides_hooks:
        lines.extend(["provides_hooks:", *(f"  - {item}" for item in provides_hooks)])
    return "\n".join(lines) + "\n"


def _echo_plugin(
    root: Path,
    name: str,
    *,
    version: str = "1.0.0",
    capabilities: tuple[str, ...] = ("tools",),
    provides_tools: tuple[str, ...] = ("signed_echo",),
    provides_hooks: tuple[str, ...] = (),
    body: str | None = None,
) -> Path:
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        _manifest(
            name,
            version=version,
            capabilities=capabilities,
            provides_tools=provides_tools,
            provides_hooks=provides_hooks,
        ),
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        body
        or (
            "def register(ctx):\n"
            "    ctx.register_tool(\n"
            "        name='signed_echo', toolset='signed',\n"
            "        schema={'name': 'signed_echo', 'description': 'echo', "
            "'parameters': {'type': 'object', 'properties': {}}},\n"
            f"        handler=lambda args: '{version}',\n"
            "    )\n"
        ),
        encoding="utf-8",
    )
    return plugin_dir


def _keypair() -> tuple[Ed25519PrivateKey, str]:
    private = Ed25519PrivateKey.generate()
    raw_public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private, base64.b64encode(raw_public).decode("ascii")


def _sign_plugin(plugin_dir: Path, private: Ed25519PrivateKey, *, key_id: str = "test-key") -> None:
    tree_sha256 = _tree_digest(plugin_dir)
    signature = private.sign(_SIGNATURE_DOMAIN + bytes.fromhex(tree_sha256))
    (plugin_dir / _SIGNATURE_FILE).write_text(
        json.dumps(
            {
                "schema_version": "crew.plugin.signature.v1",
                "algorithm": "ed25519",
                "key_id": key_id,
                "tree_sha256": tree_sha256,
                "signature": base64.b64encode(signature).decode("ascii"),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _zip_plugin(plugin_dir: Path) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(plugin_dir.rglob("*")):
            if path.is_file():
                archive.write(path, f"{plugin_dir.name}/{path.relative_to(plugin_dir).as_posix()}")
    return output.getvalue()


def _manager(
    tmp_path: Path,
    *,
    developer_mode: bool = False,
    trusted_key: str | None = None,
    registry: Registry | None = None,
    services: dict | None = None,
    trust_executable_signer: bool = True,
) -> PluginManager:
    trusted = {"test-key": trusted_key} if trusted_key else {}
    return PluginManager(
        registry=registry or Registry(),
        services=services,
        developer_mode=developer_mode,
        audit_path=tmp_path / "plugin-security-audit.jsonl",
        user_plugins_dir=tmp_path / "installed",
        trusted_plugin_keys=trusted,
        trusted_executable_roots=(
            {tmp_path / "dev"} if developer_mode else set()
        ),
        trusted_executable_signers=(
            {"test-key"} if trusted_key and trust_executable_signer else set()
        ),
        allowed_plugin_capabilities={
            "tools",
            "hooks",
            "middleware",
            "commands",
            "skills",
        },
    )


def test_local_plugin_requires_explicit_developer_mode_and_is_audited(tmp_path):
    source = tmp_path / "dev"
    _echo_plugin(source, "dev-plugin")

    denied = _manager(tmp_path)
    denied.discover_and_load([source], enabled=["dev-plugin"])

    rejected = denied.get_plugin("dev-plugin")
    assert rejected is not None
    assert rejected.enabled is False
    assert rejected.error == "local plugin requires developer mode"
    assert denied.registry.names() == []

    allowed = _manager(tmp_path, developer_mode=True)
    allowed.discover_and_load([source], enabled=["dev-plugin"])

    loaded = allowed.get_plugin("dev-plugin")
    assert loaded is not None and not loaded.enabled and loaded.declarative_only
    assert allowed.registry.names() == []
    records = [
        json.loads(line)
        for line in (tmp_path / "plugin-security-audit.jsonl").read_text("utf-8").splitlines()
    ]
    assert records[-1]["action"] == "developer_load"
    assert records[-1]["result"] == "failure"
    assert records[-1]["plugin"] == "dev-plugin"
    activation = next(
        record
        for record in reversed(records)
        if record["action"] == "activate_plugin"
    )
    assert activation["source"] == "local"
    assert activation["version"] == "1.0.0"
    assert activation["capabilities"] == ["tools"]
    assert activation["execution_mode"] == "declarative_only"


def test_invalid_manifest_is_rejected_before_plugin_code_executes(tmp_path):
    source = tmp_path / "dev"
    plugin_dir = source / "invalid"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        "name: invalid\nversion: 1.0.0\nkind: standalone\n",
        encoding="utf-8",
    )
    marker = tmp_path / "imported.txt"
    (plugin_dir / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
        "def register(ctx):\n    pass\n",
        encoding="utf-8",
    )

    plugins = _manager(tmp_path, developer_mode=True)
    plugins.discover_and_load([source], enabled=["invalid"])

    loaded = plugins.get_plugin("invalid")
    assert loaded is not None
    assert loaded.enabled is False
    assert "schema_version" in str(loaded.error)
    assert not marker.exists()


@pytest.mark.parametrize(
    ("plugin_name", "version", "plugin_key"),
    [
        (".", "1.0.0", ""),
        ("..", "1.0.0", ""),
        ("bad-version", "latest", ""),
        ("bad-key", "1.0.0", "category/../bad-key"),
    ],
)
def test_manifest_rejects_unsafe_plugin_identity_or_version_before_import(
    tmp_path,
    plugin_name,
    version,
    plugin_key,
):
    source = tmp_path / "dev"
    plugin_dir = source / "invalid-identity"
    plugin_dir.mkdir(parents=True)
    manifest = (
        "schema_version: crew.plugin.v1\n"
        f"name: {json.dumps(plugin_name)}\n"
        f"version: {json.dumps(version)}\n"
        "kind: standalone\n"
    )
    if plugin_key:
        manifest += f"key: {json.dumps(plugin_key)}\n"
    manifest += "capabilities:\n  - tools\n"
    (plugin_dir / "plugin.yaml").write_text(
        manifest,
        encoding="utf-8",
    )
    marker = tmp_path / "identity-imported.txt"
    (plugin_dir / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
        "def register(ctx):\n    pass\n",
        encoding="utf-8",
    )

    plugins = _manager(tmp_path, developer_mode=True)
    plugins.discover_and_load([source], enabled=["invalid-identity"])

    loaded = plugins.get_plugin("invalid-identity")
    assert loaded is not None and loaded.enabled is False
    assert "unsafe" in str(loaded.error) or "SemVer" in str(loaded.error)
    assert not marker.exists()


def test_undeclared_capability_rolls_back_every_registration(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "dev"
    body = (
        "def hook(**kwargs):\n    return None\n\n"
        "def register(ctx):\n"
        "    ctx.register_hook('pre_tool_call', hook)\n"
        "    ctx.register_tool(\n"
        "        name='signed_echo', toolset='signed',\n"
        "        schema={'name': 'signed_echo', 'description': 'echo', "
        "'parameters': {'type': 'object', 'properties': {}}},\n"
        "        handler=lambda args: 'should-not-survive',\n"
        "    )\n"
    )
    _echo_plugin(
        source,
        "underdeclared",
        capabilities=("hooks",),
        provides_tools=(),
        provides_hooks=("pre_tool_call",),
        body=body,
    )
    registry = Registry()
    monkeypatch.setattr(plugin_manager, "get_bundled_plugins_dir", lambda: source)
    plugins = _manager(tmp_path, developer_mode=True, registry=registry)

    plugins.discover_and_load([source], enabled=["underdeclared"])

    loaded = plugins.get_plugin("underdeclared")
    assert loaded is not None and loaded.enabled is False
    assert "capability 'tools'" in str(loaded.error)
    assert registry.names() == []
    assert plugins._hooks == {}
    assert not any(name.startswith("crew_runtime_plugins.underdeclared") for name in sys.modules)


def test_signed_remote_bundle_installs_and_tampering_fails_closed(tmp_path):
    private, public = _keypair()
    source = tmp_path / "bundle-source"
    marker = tmp_path / "signed-plugin-imported.txt"
    plugin_dir = _echo_plugin(
        source,
        "signed-plugin",
        body=(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('executed')\n"
            "def register(ctx):\n"
            "    raise RuntimeError('must not register')\n"
        ),
    )
    _sign_plugin(plugin_dir, private)
    bundle = _zip_plugin(plugin_dir)
    bundle_sha256 = hashlib.sha256(bundle).hexdigest()
    registry = Registry()
    plugins = _manager(tmp_path, trusted_key=public, registry=registry)

    loaded = plugins.install_remote_bundle_bytes(
        bundle,
        source_url="https://plugins.example.test/signed-plugin.zip",
        expected_sha256=bundle_sha256,
        actor_id="admin:test",
        enable=False,
    )

    assert not loaded.enabled
    assert not loaded.manifest.execution_trusted
    assert registry.names() == []
    assert not marker.exists()
    installed = tmp_path / "installed" / "signed-plugin"
    provenance = json.loads((installed / _PROVENANCE_FILE).read_text("utf-8"))
    assert provenance["source_url"] == "https://plugins.example.test/signed-plugin.zip"
    assert provenance["bundle_sha256"] == bundle_sha256
    assert provenance["signer_key_id"] == "test-key"
    assert not plugins.enable_plugin("signed-plugin", actor_id="admin:test")
    assert registry.names() == []
    assert not marker.exists()

    (installed / "__init__.py").write_text("def register(ctx):\n    pass\n", encoding="utf-8")
    plugins.discover_and_load([tmp_path / "installed"], enabled=["signed-plugin"])

    rejected = plugins.get_plugin("signed-plugin")
    assert rejected is not None and rejected.enabled is False
    assert "tree digest mismatch" in str(rejected.error)
    assert "signed_echo" not in registry.names()


@pytest.mark.parametrize("replayed_version", ["2.0.0", "1.9.9"])
def test_signed_remote_update_rejects_version_replay(
    tmp_path,
    replayed_version,
):
    private, public = _keypair()
    plugins = _manager(tmp_path, trusted_key=public)
    current_dir = _echo_plugin(
        tmp_path / "current-source",
        "replay-plugin",
        version="2.0.0",
    )
    _sign_plugin(current_dir, private)
    current_bundle = _zip_plugin(current_dir)
    plugins.install_remote_bundle_bytes(
        current_bundle,
        source_url="https://plugins.example.test/replay-plugin-2.zip",
        expected_sha256=hashlib.sha256(current_bundle).hexdigest(),
        actor_id="admin:test",
        enable=False,
    )

    replay_dir = _echo_plugin(
        tmp_path / f"replay-source-{replayed_version}",
        "replay-plugin",
        version=replayed_version,
        body="raise RuntimeError('replayed code must never import')\n",
    )
    _sign_plugin(replay_dir, private)
    replay_bundle = _zip_plugin(replay_dir)

    with pytest.raises(PluginSecurityError) as denied:
        plugins.install_remote_bundle_bytes(
            replay_bundle,
            source_url="https://plugins.example.test/replay-plugin-old.zip",
            expected_sha256=hashlib.sha256(replay_bundle).hexdigest(),
            actor_id="admin:test",
            enable=False,
        )

    assert denied.value.code == "plugin_version_replay"
    installed = plugins.get_plugin("replay-plugin")
    assert installed is not None
    assert installed.manifest.version == "2.0.0"


def test_unsigned_remote_bundle_is_rejected_without_publishing(tmp_path):
    _private, public = _keypair()
    source = tmp_path / "bundle-source"
    plugin_dir = _echo_plugin(source, "unsigned-plugin")
    bundle = _zip_plugin(plugin_dir)
    plugins = _manager(tmp_path, trusted_key=public)

    with pytest.raises(PluginSecurityError, match="signature"):
        plugins.install_remote_bundle_bytes(
            bundle,
            source_url="https://plugins.example.test/unsigned-plugin.zip",
            expected_sha256=hashlib.sha256(bundle).hexdigest(),
            actor_id="admin:test",
        )

    assert not (tmp_path / "installed" / "unsigned-plugin").exists()


def test_remote_bundle_rejects_archive_traversal(tmp_path):
    _private, public = _keypair()
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("../escaped.py", "raise SystemExit")
    bundle = output.getvalue()
    plugins = _manager(tmp_path, trusted_key=public)

    with pytest.raises(PluginSecurityError, match="unsafe archive path"):
        plugins.install_remote_bundle_bytes(
            bundle,
            source_url="https://plugins.example.test/escaped.zip",
            expected_sha256=hashlib.sha256(bundle).hexdigest(),
            actor_id="admin:test",
        )

    assert not (tmp_path / "escaped.py").exists()


def test_extract_plugin_bundle_rejects_compression_ratio_bomb(tmp_path):
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("pkg/zeros.bin", b"0" * (4 * 1024 * 1024))

    with pytest.raises(PluginSecurityError, match="compression ratio"):
        plugin_security.extract_plugin_bundle(output.getvalue(), tmp_path / "out")

    assert not (tmp_path / "out" / "pkg").exists()


def test_extract_plugin_bundle_rejects_excessive_depth(tmp_path):
    output = BytesIO()
    deep = "pkg/" + "/".join(["d"] * 33) + "/file.txt"
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(deep, "content")

    with pytest.raises(PluginSecurityError, match="path depth"):
        plugin_security.extract_plugin_bundle(output.getvalue(), tmp_path / "out")

    assert not (tmp_path / "out" / "pkg").exists()


def test_failed_remote_update_restores_previous_plugin_atomically(tmp_path):
    private, public = _keypair()
    registry = Registry()
    plugins = _manager(tmp_path, trusted_key=public, registry=registry)

    v1_dir = _echo_plugin(tmp_path / "v1-source", "atomic-plugin", version="1.0.0")
    _sign_plugin(v1_dir, private)
    v1_bundle = _zip_plugin(v1_dir)
    plugins.install_remote_bundle_bytes(
        v1_bundle,
        source_url="https://plugins.example.test/atomic-plugin-1.zip",
        expected_sha256=hashlib.sha256(v1_bundle).hexdigest(),
        actor_id="admin:test",
        enable=False,
    )

    broken_body = (
        "def register(ctx):\n"
        "    ctx.register_tool(\n"
        "        name='signed_echo', toolset='signed',\n"
        "        schema={'name': 'signed_echo', 'description': 'echo', "
        "'parameters': {'type': 'object', 'properties': {}}},\n"
        "        handler=lambda args: '2.0.0',\n"
        "    )\n"
        "    raise RuntimeError('broken update')\n"
    )
    v2_dir = _echo_plugin(
        tmp_path / "v2-source",
        "atomic-plugin",
        version="2.0.0",
        body=broken_body,
    )
    _sign_plugin(v2_dir, private)
    v2_bundle = _zip_plugin(v2_dir)

    with pytest.raises(PluginSecurityError, match="activation failed"):
        plugins.install_remote_bundle_bytes(
            v2_bundle,
            source_url="https://plugins.example.test/atomic-plugin-2.zip",
            expected_sha256=hashlib.sha256(v2_bundle).hexdigest(),
            actor_id="admin:test",
            enable=True,
        )

    restored = plugins.get_plugin("atomic-plugin")
    assert restored is not None
    assert not restored.enabled
    assert restored.manifest.version == "1.0.0"
    assert registry.names() == []
    assert not list((tmp_path / "installed").glob(".atomic-plugin.*"))


def test_uninstall_removes_runtime_state_files_and_preferences(tmp_path):
    private, public = _keypair()

    class Preferences:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        def delete_plugin(self, plugin_key: str) -> None:
            self.deleted.append(plugin_key)

    preferences = Preferences()
    plugins = _manager(
        tmp_path,
        trusted_key=public,
        services={"plugin_prefs": preferences},
    )
    source = tmp_path / "bundle-source"
    plugin_dir = _echo_plugin(source, "remove-me")
    _sign_plugin(plugin_dir, private)
    bundle = _zip_plugin(plugin_dir)
    plugins.install_remote_bundle_bytes(
        bundle,
        source_url="https://plugins.example.test/remove-me.zip",
        expected_sha256=hashlib.sha256(bundle).hexdigest(),
        actor_id="admin:test",
        enable=False,
    )

    assert plugins.uninstall_plugin("remove-me", actor_id="admin:test") is True

    assert plugins.get_plugin("remove-me") is None
    assert plugins.registry.names() == []
    assert preferences.deleted == ["remove-me"]
    assert not (tmp_path / "installed" / "remove-me").exists()
    assert not any(name.startswith("crew_runtime_plugins.remove_me") for name in sys.modules)


def test_trusted_remote_signer_cannot_authorize_gateway_execution(tmp_path):
    private, public = _keypair()
    marker = tmp_path / "signed-imported.txt"
    plugin_dir = _echo_plugin(
        tmp_path / "bundle-source",
        "signed-untrusted",
        body=(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('executed')\n"
            "def register(ctx):\n    pass\n"
        ),
    )
    _sign_plugin(plugin_dir, private)
    bundle = _zip_plugin(plugin_dir)
    plugins = _manager(
        tmp_path,
        trusted_key=public,
        trust_executable_signer=True,
    )

    with pytest.raises(PluginSecurityError, match="activation failed"):
        plugins.install_remote_bundle_bytes(
            bundle,
            source_url="https://plugins.example.test/signed-untrusted.zip",
            expected_sha256=hashlib.sha256(bundle).hexdigest(),
            actor_id="admin:test",
            enable=True,
        )

    assert not marker.exists()
    assert not (tmp_path / "installed" / "signed-untrusted").exists()


def test_declarative_signed_plugin_rechecks_revoked_signer(tmp_path):
    private, public = _keypair()
    plugin_dir = _echo_plugin(
        tmp_path / "bundle-source",
        "signed-skills",
        capabilities=("skills",),
        provides_tools=(),
        body="raise RuntimeError('declarative plugin must not import')\n",
    )
    skill = plugin_dir / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: demo\n---\nbody\n",
        encoding="utf-8",
    )
    _sign_plugin(plugin_dir, private)
    bundle = _zip_plugin(plugin_dir)
    plugins = _manager(
        tmp_path,
        trusted_key=public,
        trust_executable_signer=False,
    )

    loaded = plugins.install_remote_bundle_bytes(
        bundle,
        source_url="https://plugins.example.test/signed-skills.zip",
        expected_sha256=hashlib.sha256(bundle).hexdigest(),
        actor_id="admin:test",
        enable=True,
    )

    assert loaded.enabled and loaded.declarative_only
    assert plugins.plugin_skill_roots()
    plugins.trusted_plugin_keys.clear()
    assert plugins.plugin_skill_roots() == []
    assert loaded.enabled is False
    assert "signature" in str(loaded.error) or "trusted" in str(loaded.error)
    assert "crew_runtime_plugins.signed_skills" not in sys.modules


async def test_signed_remote_command_never_registers_or_runs_disposer(tmp_path):
    private, public = _keypair()
    marker = tmp_path / "revoked-disposer.txt"
    plugin_dir = tmp_path / "bundle-source" / "signed-command"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        "schema_version: crew.plugin.v1\n"
        "name: signed-command\n"
        'version: "1.0.0"\n'
        "kind: standalone\n"
        "capabilities:\n"
        "  - commands\n"
        "provides_commands:\n"
        "  - ping\n",
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        "from pathlib import Path\n"
        "def command(raw_args):\n"
        "    return 'pong'\n\n"
        "def register(ctx):\n"
        "    ctx.register_command('ping', command)\n"
        "    ctx.register_disposer(\n"
        f"        lambda: Path({str(marker)!r}).write_text('ran')\n"
        "    )\n",
        encoding="utf-8",
    )
    _sign_plugin(plugin_dir, private)
    bundle = _zip_plugin(plugin_dir)
    plugins = _manager(tmp_path, trusted_key=public)
    loaded = plugins.install_remote_bundle_bytes(
        bundle,
        source_url="https://plugins.example.test/signed-command.zip",
        expected_sha256=hashlib.sha256(bundle).hexdigest(),
        actor_id="admin:test",
        enable=False,
    )
    assert not loaded.enabled
    assert not plugins.enable_plugin("signed-command", actor_id="admin:test")
    assert await plugins.run_plugin_command("/ping") is None
    assert plugins.plugin_commands == {}
    assert not marker.exists()
    assert "crew_runtime_plugins.signed_command" not in sys.modules
