from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from crew.tools.mcp_client import _verify_command_integrity


def test_pinned_mcp_command_rejects_post_configuration_replacement(
    tmp_path: Path,
) -> None:
    command = tmp_path / "mcp-server"
    command.write_bytes(b"trusted server")
    config = {
        "command": str(command.resolve()),
        "command_sha256": hashlib.sha256(command.read_bytes()).hexdigest(),
    }

    _verify_command_integrity(config)
    command.write_bytes(b"attacker replacement")

    with pytest.raises(ValueError, match="integrity"):
        _verify_command_integrity(config)


def test_pinned_mcp_command_requires_canonical_absolute_path() -> None:
    with pytest.raises(ValueError, match="absolute"):
        _verify_command_integrity(
            {
                "command": "mcp-server",
                "command_sha256": "a" * 64,
            }
        )
