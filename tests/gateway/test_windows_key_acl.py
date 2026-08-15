"""Windows ACL contracts for Gateway authentication keys."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from crew.gateway import auth, instance_auth
from crew.security.secret_store import PlatformSecretStore, SecretStoreUnavailable


def _enable_fake_windows_acl(
    monkeypatch: pytest.MonkeyPatch,
    module,
    *,
    path_secure=None,
    fd_secure=None,
    protect_path=None,
    protect_fd=None,
) -> None:
    monkeypatch.setattr(module, "_IS_WINDOWS", True, raising=False)
    monkeypatch.setattr(
        module,
        "_windows_path_is_secure",
        path_secure or (lambda _path, *, directory: True),
        raising=False,
    )
    monkeypatch.setattr(
        module,
        "_windows_fd_is_secure",
        fd_secure or (lambda _fd, *, directory: True),
        raising=False,
    )
    monkeypatch.setattr(
        module,
        "_protect_windows_path",
        protect_path or (lambda _path, *, directory: None),
        raising=False,
    )
    monkeypatch.setattr(
        module,
        "_protect_windows_fd",
        protect_fd or (lambda _fd, *, directory: None),
        raising=False,
    )


def test_session_key_creation_uses_platform_store_without_legacy_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_platform_secret_backend,
) -> None:
    crew_home = tmp_path / ".crew"
    monkeypatch.setenv("CREW_HOME", str(crew_home))

    token = auth.create_remote_session_token("example", "user-1", ttl_seconds=600)

    assert token.count(".") == 1
    assert not (crew_home / ".auth" / "session.key").exists()
    assert len(_isolated_platform_secret_backend.values) == 1


def test_legacy_session_key_migration_rejects_untrusted_windows_dacl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crew_home = tmp_path / ".crew"
    key_directory = crew_home / ".auth"
    key_directory.mkdir(parents=True, mode=0o700)
    key_file = key_directory / "session.key"
    key_file.write_bytes(b"x" * 32)
    key_file.chmod(0o600)
    monkeypatch.setenv("CREW_HOME", str(crew_home))

    _enable_fake_windows_acl(
        monkeypatch,
        auth,
        path_secure=lambda path, *, directory: directory,
    )

    with pytest.raises(auth.AuthenticationError, match="权限"):
        auth.create_remote_session_token("example", "user-1", ttl_seconds=600)


def test_legacy_session_key_migrates_then_removes_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_platform_secret_backend,
) -> None:
    crew_home = tmp_path / ".crew"
    key_directory = crew_home / ".auth"
    key_directory.mkdir(parents=True, mode=0o700)
    key_file = key_directory / "session.key"
    key_file.write_bytes(b"x" * 32)
    key_file.chmod(0o600)
    monkeypatch.setenv("CREW_HOME", str(crew_home))
    _enable_fake_windows_acl(monkeypatch, auth)

    token = auth.create_remote_session_token("example", "user-1", ttl_seconds=600)

    assert token.count(".") == 1
    assert not key_file.exists()
    assert len(_isolated_platform_secret_backend.values) == 1


def test_session_key_migration_fails_closed_without_platform_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crew_home = tmp_path / ".crew"
    key_directory = crew_home / ".auth"
    key_directory.mkdir(parents=True, mode=0o700)
    key_file = key_directory / "session.key"
    key_file.write_bytes(b"x" * 32)
    key_file.chmod(0o600)
    monkeypatch.setenv("CREW_HOME", str(crew_home))

    def unavailable() -> PlatformSecretStore:
        raise SecretStoreUnavailable("backend unavailable")

    monkeypatch.setattr(
        PlatformSecretStore,
        "platform",
        staticmethod(unavailable),
    )

    with pytest.raises(auth.AuthenticationError, match="安全存储"):
        auth.create_remote_session_token("example", "user-1", ttl_seconds=600)

    assert key_file.exists()


def test_instance_key_read_validates_windows_path_and_open_handle_acl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crew_home = tmp_path / ".crew"
    key_directory = crew_home / instance_auth.GATEWAY_INSTANCE_DIRECTORY
    key_directory.mkdir(parents=True, mode=0o700)
    key_file = key_directory / instance_auth.GATEWAY_INSTANCE_KEY_FILENAME
    key_file.write_bytes(b"11" * 32)
    key_file.chmod(0o600)
    monkeypatch.setenv("CREW_HOME", str(crew_home))
    inspected_paths: list[tuple[Path, bool]] = []
    inspected_fds: list[bool] = []

    _enable_fake_windows_acl(
        monkeypatch,
        instance_auth,
        path_secure=lambda path, *, directory: (
            inspected_paths.append((Path(path), directory)) or True
        ),
        fd_secure=lambda _fd, *, directory: inspected_fds.append(directory) or True,
    )

    proof = instance_auth.create_gateway_instance_proof("ab" * 32)

    assert proof is not None
    assert inspected_paths == [(key_directory, True), (key_file, False)]
    assert inspected_fds == [False]


def test_instance_key_read_fails_closed_for_untrusted_windows_handle_acl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crew_home = tmp_path / ".crew"
    key_directory = crew_home / instance_auth.GATEWAY_INSTANCE_DIRECTORY
    key_directory.mkdir(parents=True, mode=0o700)
    key_file = key_directory / instance_auth.GATEWAY_INSTANCE_KEY_FILENAME
    key_file.write_bytes(b"11" * 32)
    key_file.chmod(0o600)
    monkeypatch.setenv("CREW_HOME", str(crew_home))

    _enable_fake_windows_acl(
        monkeypatch,
        instance_auth,
        fd_secure=lambda _fd, *, directory: False,
    )

    assert instance_auth.create_gateway_instance_proof("cd" * 32) is None


@pytest.mark.skipif(os.name != "nt", reason="requires real Windows owner and DACL APIs")
def test_windows_session_key_creation_uses_platform_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crew_home = tmp_path / ".crew"
    monkeypatch.setenv("CREW_HOME", str(crew_home))

    token = auth.create_remote_session_token("example", "integration-user", ttl_seconds=600)

    assert token.count(".") == 1
    assert not (crew_home / ".auth" / "session.key").exists()


@pytest.mark.skipif(os.name != "nt", reason="requires real Windows owner and DACL APIs")
def test_windows_acl_integration_protects_temp_objects_and_detects_tampering(
    tmp_path: Path,
) -> None:
    from crew.gateway.windows_acl import fd_is_secure, path_is_secure, protect_path

    key_directory = tmp_path / "key-state"
    key_directory.mkdir()
    key_file = key_directory / "dummy.key"
    key_file.write_bytes(b"not-a-real-key")

    protect_path(key_directory, directory=True)
    protect_path(key_file, directory=False)

    assert path_is_secure(key_directory, directory=True)
    assert path_is_secure(key_file, directory=False)
    fd = os.open(key_file, os.O_RDONLY)
    try:
        assert fd_is_secure(fd, directory=False)
    finally:
        os.close(fd)

    completed = subprocess.run(
        ["icacls.exe", str(key_file), "/grant", "*S-1-1-0:R"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert not path_is_secure(key_file, directory=False)
