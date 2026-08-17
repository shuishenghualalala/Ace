from __future__ import annotations

import os
from pathlib import Path

import pytest

from crew.state.config import (
    Config,
    ModelProfile,
    _load_env_file,
    _load_env_map,
    write_env_key,
)


def test_dotenv_cannot_override_process_security_or_loader_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "ACE_STRICT_SECURITY=0",
                "ACE_SECURITY_RUNTIME=/attacker/runtime",
                "ACE_DESKTOP_SECURITY_RUNTIME=/attacker/runtime",
                "ACE_GATEWAY_LAUNCH_SECRET_STDIN=1",
                "CREW_RIPGREP_INSTALLER=system",
                "PATH=/attacker/bin",
                "SAFE_VALUE=allowed",
                "PROVIDER_API_KEY=${ACE_STRICT_SECURITY}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ACE_STRICT_SECURITY", "1")
    monkeypatch.delenv("ACE_SECURITY_RUNTIME", raising=False)
    monkeypatch.delenv("ACE_DESKTOP_SECURITY_RUNTIME", raising=False)
    monkeypatch.delenv("ACE_GATEWAY_LAUNCH_SECRET_STDIN", raising=False)
    monkeypatch.setenv("CREW_RIPGREP_INSTALLER", "managed")
    monkeypatch.setenv("PATH", "trusted-path")
    monkeypatch.setenv("SAFE_VALUE", "old")
    monkeypatch.setenv("PROVIDER_API_KEY", "old-key")

    secure_values = _load_env_file(env_file)

    assert os.environ["ACE_STRICT_SECURITY"] == "1"
    assert "ACE_SECURITY_RUNTIME" not in os.environ
    assert "ACE_DESKTOP_SECURITY_RUNTIME" not in os.environ
    assert "ACE_GATEWAY_LAUNCH_SECRET_STDIN" not in os.environ
    assert os.environ["CREW_RIPGREP_INSTALLER"] == "managed"
    assert os.environ["PATH"] == "trusted-path"
    assert os.environ["SAFE_VALUE"] == "allowed"
    assert "PROVIDER_API_KEY" not in os.environ
    assert secure_values["PROVIDER_API_KEY"] == "${ACE_STRICT_SECURITY}"
    persisted = env_file.read_text(encoding="utf-8")
    assert "${ACE_STRICT_SECURITY}" not in persisted
    assert "PROVIDER_API_KEY=@ace-secret:v1:" in persisted


def test_owner_dotenv_excludes_protected_names_and_interpolation(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ACE_CUA_BINARY_SHA256_LINUX=" + ("a" * 64) + "\nPROVIDER_API_KEY=${PATH}\n",
        encoding="utf-8",
    )

    assert _load_env_map(env_file) == {
        "PROVIDER_API_KEY": "${PATH}",
    }


@pytest.mark.parametrize(
    "name",
    [
        "ACE_STRICT_SECURITY",
        "ACE_SECURITY_RUNTIME",
        "ACE_DESKTOP_SECURITY_RUNTIME",
        "ACE_GATEWAY_LAUNCH_SECRET_STDIN",
        "ACE_CUA_BINARY_SHA256_WINDOWS",
        "CREW_RIPGREP_INSTALLER",
        "PATH",
        "LD_PRELOAD",
    ],
)
def test_runtime_config_api_cannot_write_protected_environment_names(
    tmp_path: Path,
    name: str,
) -> None:
    with pytest.raises(ValueError, match="not writable"):
        write_env_key(tmp_path / ".env", name, "attacker")


def test_runtime_config_api_cannot_write_sensitive_environment_values(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="platform secret storage"):
        write_env_key(tmp_path / ".env", "PROVIDER_API_KEY", "plaintext")


def test_env_write_rejects_symlink_target(tmp_path: Path) -> None:
    outside = tmp_path / "outside.env"
    outside.write_text("SAFE=original\n", encoding="utf-8")
    link = tmp_path / ".env"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    assert _load_env_map(link) == {}
    with pytest.raises(RuntimeError):
        write_env_key(link, "SAFE", "replacement", sync_process_env=False)

    assert outside.read_text(encoding="utf-8") == "SAFE=original\n"


@pytest.mark.parametrize(
    "base_url",
    [
        "https://user:password@api.example.test/v1",
        "https://api.example.test/v1?api_key=query-secret",
        "https://api.example.test/v1#access_token=fragment-secret",
    ],
)
def test_model_profile_rejects_credential_bearing_base_url(
    tmp_path: Path,
    base_url: str,
) -> None:
    path = tmp_path / "config.yaml"
    original = "llm:\n  active: safe\n"
    path.write_text(original, encoding="utf-8")
    config = Config(config_path=str(path), active_model_id="forbidden")
    config.model_profiles = {
        "forbidden": ModelProfile(
            id="forbidden",
            base_url=base_url,
        )
    }

    with pytest.raises(ValueError, match="must not contain credentials"):
        config.persist_model_profiles()

    assert path.read_text(encoding="utf-8") == original
