from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from crew.security.mcp_secrets import (
    mcp_secret_identifier,
    resolve_mcp_server_secrets,
)
from crew.security.secret_store import (
    PlatformSecretStore,
    SecretStoreUnavailable,
)
import crew.state.config as state_config
from crew.state.config import Config


def _config(tmp_path: Path) -> tuple[Config, Path]:
    path = tmp_path / "config.yaml"
    path.write_text("mcp_servers: {}\n", encoding="utf-8")
    return Config(config_path=str(path)), path


def test_mcp_credentials_persist_only_as_bound_keyring_markers(tmp_path: Path) -> None:
    config, path = _config(tmp_path)
    config.mcp_servers = {
        "remote": {
            "url": "https://mcp.example.test/rpc",
            "transport": "http",
            "env": {"MCP_TOKEN": "env-secret", "LANG": "C"},
            "headers": {
                "Authorization": "Bearer header-secret",
                "X-Trace": "safe",
            },
        }
    }

    config.persist_mcp_servers()

    text = path.read_text(encoding="utf-8")
    persisted = yaml.safe_load(text)["mcp_servers"]["remote"]
    assert "env-secret" not in text
    assert "header-secret" not in text
    assert persisted["env"]["MCP_TOKEN"].startswith("@ace-secret:v1:")
    assert persisted["headers"]["Authorization"].startswith("@ace-secret:v1:")
    assert persisted["env"]["LANG"] == "C"
    assert persisted["headers"]["X-Trace"] == "safe"
    assert resolve_mcp_server_secrets("remote", persisted) == {
        "url": "https://mcp.example.test/rpc",
        "transport": "http",
        "env": {"MCP_TOKEN": "env-secret", "LANG": "C"},
        "headers": {
            "Authorization": "Bearer header-secret",
            "X-Trace": "safe",
        },
    }
    network_only = resolve_mcp_server_secrets(
        "remote",
        persisted,
        sections=("headers",),
    )
    assert network_only["env"]["MCP_TOKEN"].startswith("@ace-secret:v1:")
    assert network_only["headers"]["Authorization"] == "Bearer header-secret"


def test_mcp_marker_cannot_be_replayed_for_another_server(tmp_path: Path) -> None:
    config, _path = _config(tmp_path)
    config.mcp_servers = {
        "one": {
            "url": "https://one.example.test",
            "headers": {"Authorization": "Bearer secret"},
        }
    }
    config.persist_mcp_servers()
    marker = config.mcp_servers["one"]["headers"]["Authorization"]

    with pytest.raises(SecretStoreUnavailable, match="another scope"):
        resolve_mcp_server_secrets(
            "two",
            {
                "url": "https://two.example.test",
                "headers": {"Authorization": marker},
            },
        )


def test_mcp_marker_cannot_be_reused_for_another_network_origin(
    tmp_path: Path,
) -> None:
    config, _path = _config(tmp_path)
    config.mcp_servers = {
        "remote": {
            "url": "https://one.example.test/rpc",
            "headers": {"Authorization": "Bearer secret"},
        }
    }
    config.persist_mcp_servers()
    marker = config.mcp_servers["remote"]["headers"]["Authorization"]

    with pytest.raises(SecretStoreUnavailable, match="another scope"):
        resolve_mcp_server_secrets(
            "remote",
            {
                "url": "https://two.example.test/rpc",
                "headers": {"Authorization": marker},
            },
        )


def test_stdio_secret_marker_is_bound_to_pinned_executable_digest(
    tmp_path: Path,
) -> None:
    config, _path = _config(tmp_path)
    config.mcp_servers = {
        "local": {
            "command": "/trusted/mcp-server",
            "command_sha256": "a" * 64,
            "args": ["serve"],
            "env": {"MCP_TOKEN": "secret"},
        }
    }
    config.persist_mcp_servers()
    marker = config.mcp_servers["local"]["env"]["MCP_TOKEN"]

    with pytest.raises(SecretStoreUnavailable, match="another scope"):
        resolve_mcp_server_secrets(
            "local",
            {
                "command": "/trusted/mcp-server",
                "command_sha256": "b" * 64,
                "args": ["serve"],
                "env": {"MCP_TOKEN": marker},
            },
        )


def test_stdio_secret_requires_pinned_executable_digest(tmp_path: Path) -> None:
    config, path = _config(tmp_path)
    before = path.read_text(encoding="utf-8")
    config.mcp_servers = {
        "local": {
            "command": "/trusted/mcp-server",
            "args": ["serve"],
            "env": {"MCP_TOKEN": "secret"},
        }
    }

    with pytest.raises(ValueError, match="command digest"):
        config.persist_mcp_servers()

    assert path.read_text(encoding="utf-8") == before


def test_mcp_plaintext_credential_is_rejected_at_runtime() -> None:
    with pytest.raises(SecretStoreUnavailable, match="plaintext"):
        resolve_mcp_server_secrets(
            "remote",
            {
                "url": "https://mcp.example.test",
                "headers": {"Authorization": "Bearer plaintext"},
            },
        )


@pytest.mark.parametrize(
    "server",
    [
        {
            "url": "https://user:password@mcp.example.test/rpc",
            "transport": "http",
        },
        {
            "url": "https://mcp.example.test/rpc?access_token=query-secret",
            "transport": "http",
        },
        {
            "url": "https://mcp.example.test/rpc#access_token=fragment-secret",
            "transport": "http",
        },
        {
            "command": "/trusted/mcp-server",
            "args": ["--token", "argv-secret"],
        },
    ],
)
def test_mcp_credentials_in_url_or_argv_fail_closed(
    tmp_path: Path,
    server: dict[str, object],
) -> None:
    config, path = _config(tmp_path)
    before = path.read_text(encoding="utf-8")
    config.mcp_servers = {"forbidden": server}

    with pytest.raises(SecretStoreUnavailable, match="URL or argv"):
        config.persist_mcp_servers()
    with pytest.raises(SecretStoreUnavailable, match="URL or argv"):
        resolve_mcp_server_secrets("forbidden", server)

    assert path.read_text(encoding="utf-8") == before


def test_failed_yaml_rewrite_rolls_back_mcp_secret_rotation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, path = _config(tmp_path)
    config.mcp_servers = {
        "remote": {
            "url": "https://mcp.example.test",
            "headers": {"Authorization": "Bearer old-secret"},
        }
    }
    config.persist_mcp_servers()
    old_marker = config.mcp_servers["remote"]["headers"]["Authorization"]
    config.mcp_servers["remote"]["headers"]["Authorization"] = "Bearer new-secret"

    original_replace = state_config.atomic_replace_bytes

    def fail_config_replace(
        target: Path,
        content: bytes,
        expected,
        *,
        max_bytes: int,
    ) -> None:
        if Path(target) == path:
            raise OSError("simulated config replacement failure")
        return original_replace(target, content, expected, max_bytes=max_bytes)

    monkeypatch.setattr(state_config, "atomic_replace_bytes", fail_config_replace)
    with pytest.raises(OSError, match="simulated"):
        config.persist_mcp_servers()

    store = PlatformSecretStore.platform()
    identifier = mcp_secret_identifier(
        "remote",
        "headers",
        "Authorization",
        config.mcp_servers["remote"],
    )
    assert store.resolve_marker(identifier, old_marker) == "Bearer old-secret"
    assert "new-secret" not in path.read_text(encoding="utf-8")


def test_rotated_mcp_marker_cannot_be_replayed_after_successful_rotation(
    tmp_path: Path,
) -> None:
    config, _path = _config(tmp_path)
    server = {
        "url": "https://mcp.example.test/rpc",
        "headers": {"Authorization": "Bearer old-secret"},
    }
    config.mcp_servers = {"remote": dict(server)}
    config.persist_mcp_servers()
    old_marker = config.mcp_servers["remote"]["headers"]["Authorization"]

    config.mcp_servers["remote"]["headers"]["Authorization"] = "Bearer new-secret"
    config.persist_mcp_servers()

    with pytest.raises(SecretStoreUnavailable):
        resolve_mcp_server_secrets(
            "remote",
            {
                "url": "https://mcp.example.test/rpc",
                "headers": {"Authorization": old_marker},
            },
        )
    resolved = resolve_mcp_server_secrets(
        "remote",
        config.mcp_servers["remote"],
    )
    assert resolved["headers"]["Authorization"] == "Bearer new-secret"


def test_removing_mcp_secret_deletes_platform_record(tmp_path: Path) -> None:
    config, _path = _config(tmp_path)
    config.mcp_servers = {
        "remote": {
            "url": "https://mcp.example.test",
            "headers": {"Authorization": "Bearer secret"},
        }
    }
    config.persist_mcp_servers()
    marker = config.mcp_servers["remote"]["headers"]["Authorization"]
    identifier = mcp_secret_identifier(
        "remote",
        "headers",
        "Authorization",
        config.mcp_servers["remote"],
    )

    config.mcp_servers = {}
    config.persist_mcp_servers()

    with pytest.raises(SecretStoreUnavailable, match="another scope"):
        PlatformSecretStore.platform().resolve_marker(identifier, marker)
