"""Platform credential-store contracts for local Ace secrets."""

from __future__ import annotations

import os

import pytest

from crew.security.secret_store import (
    PlatformSecretStore,
    SecretIdentifier,
    SecretNotFound,
    SecretStoreUnavailable,
)


class _MemoryBackend:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.reads = 0

    def get_password(self, service: str, account: str) -> str | None:
        self.reads += 1
        return self.values.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        self.values[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        self.values.pop((service, account), None)


@pytest.mark.parametrize(
    ("namespace", "scope", "name"),
    [
        ("", "owner", "key"),
        ("mcp/server", "owner", "key"),
        ("mcp", "../owner", "key"),
        ("mcp", "owner", "key/name"),
        ("mcp", "owner", "a" * 129),
    ],
)
def test_secret_identifier_rejects_path_and_namespace_injection(
    namespace: str,
    scope: str,
    name: str,
) -> None:
    with pytest.raises(ValueError):
        SecretIdentifier(namespace=namespace, scope=scope, name=name)


def test_secret_namespaces_and_scopes_are_cryptographically_separate() -> None:
    backend = _MemoryBackend()
    store = PlatformSecretStore.for_backend(backend)
    first = SecretIdentifier("provider", "owner-a", "OPENAI_API_KEY")
    second = SecretIdentifier("provider", "owner-b", "OPENAI_API_KEY")
    third = SecretIdentifier("mcp", "owner-a", "OPENAI_API_KEY")

    store.set(first, "secret-a")
    store.set(second, "secret-b")
    store.set(third, "secret-c")

    assert store.get(first) == "secret-a"
    assert store.get(second) == "secret-b"
    assert store.get(third) == "secret-c"
    assert len(backend.values) == 3


def test_store_has_no_plaintext_cache_and_observes_backend_rotation() -> None:
    backend = _MemoryBackend()
    store = PlatformSecretStore.for_backend(backend)
    identifier = SecretIdentifier("provider", "owner", "API_KEY")
    store.set(identifier, "first-secret")
    assert store.get(identifier) == "first-secret"

    store.set(identifier, "rotated-secret")

    assert store.get(identifier) == "rotated-secret"
    assert backend.reads == 2


def test_marker_is_versioned_non_secret_and_bound_to_identifier() -> None:
    backend = _MemoryBackend()
    store = PlatformSecretStore.for_backend(backend)
    identifier = SecretIdentifier("provider", "owner", "API_KEY")
    store.set(identifier, "do-not-persist-me")

    marker = store.marker(identifier)

    assert marker.startswith("@ace-secret:v1:")
    assert "do-not-persist-me" not in marker
    assert store.resolve_marker(identifier, marker) == "do-not-persist-me"
    with pytest.raises(SecretStoreUnavailable):
        store.resolve_marker(
            SecretIdentifier("provider", "other-owner", "API_KEY"),
            marker,
        )


def test_secret_rotation_invalidates_the_previous_authenticated_marker() -> None:
    store = PlatformSecretStore.for_backend(_MemoryBackend())
    identifier = SecretIdentifier("provider", "owner", "API_KEY")
    store.set(identifier, "first-secret")
    old_marker = store.marker(identifier)

    store.set(identifier, "rotated-secret")

    with pytest.raises(SecretStoreUnavailable):
        store.resolve_marker(identifier, old_marker)
    assert store.resolve_marker(identifier, store.marker(identifier)) == "rotated-secret"


def test_secret_rollback_restores_exact_record_and_refuses_concurrent_change() -> None:
    store = PlatformSecretStore.for_backend(_MemoryBackend())
    identifier = SecretIdentifier("provider", "owner", "API_KEY")
    store.set(identifier, "first-secret")
    original_marker = store.marker(identifier)

    mutation = store.replace(identifier, "second-secret")
    store.rollback(mutation)
    assert store.resolve_marker(identifier, original_marker) == "first-secret"

    mutation = store.replace(identifier, "third-secret")
    store.set(identifier, "concurrent-secret")
    with pytest.raises(SecretStoreUnavailable, match="concurrently"):
        store.rollback(mutation)
    assert store.get(identifier) == "concurrent-secret"


def test_missing_or_failed_backend_never_falls_back_to_plaintext(monkeypatch) -> None:
    monkeypatch.setattr(
        PlatformSecretStore,
        "_load_platform_backend",
        staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("no keyring"))),
    )

    with pytest.raises(SecretStoreUnavailable):
        PlatformSecretStore.platform()


def test_delete_is_idempotent_and_missing_reads_are_explicit() -> None:
    backend = _MemoryBackend()
    store = PlatformSecretStore.for_backend(backend)
    identifier = SecretIdentifier("provider", "owner", "API_KEY")

    with pytest.raises(SecretNotFound):
        store.get(identifier)
    store.delete(identifier)
    store.set(identifier, "secret")
    store.delete(identifier)
    store.delete(identifier)
    with pytest.raises(SecretNotFound):
        store.get(identifier)


@pytest.mark.parametrize("value", ["", "contains\x00nul", "x" * 16_385])
def test_secret_values_are_bounded(value: str) -> None:
    store = PlatformSecretStore.for_backend(_MemoryBackend())
    identifier = SecretIdentifier("provider", "owner", "API_KEY")

    with pytest.raises(ValueError):
        store.set(identifier, value)


def test_runtime_env_persists_only_bound_marker_and_resolves_from_keyring(
    tmp_path,
    monkeypatch,
) -> None:
    from crew.state.config import _load_crew_home_env_file, write_secret_env_key

    backend = _MemoryBackend()
    monkeypatch.setattr(
        PlatformSecretStore,
        "_load_platform_backend",
        staticmethod(lambda: backend),
    )
    env_path = tmp_path / ".env"
    secret = "provider-secret-value"

    write_secret_env_key(env_path, "PROVIDER_API_KEY", secret)

    persisted = env_path.read_text(encoding="utf-8")
    assert secret not in persisted
    assert "@ace-secret:v1:" in persisted
    monkeypatch.delenv("PROVIDER_API_KEY", raising=False)
    _load_crew_home_env_file(tmp_path)
    assert os.environ["PROVIDER_API_KEY"] == secret


def test_tampered_or_cross_path_marker_is_not_loaded(tmp_path, monkeypatch) -> None:
    from crew.state.config import _load_crew_home_env_file, write_secret_env_key

    backend = _MemoryBackend()
    monkeypatch.setattr(
        PlatformSecretStore,
        "_load_platform_backend",
        staticmethod(lambda: backend),
    )
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    write_secret_env_key(first / ".env", "PROVIDER_API_KEY", "secret")
    (second / ".env").write_text(
        (first / ".env").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.delenv("PROVIDER_API_KEY", raising=False)

    _load_crew_home_env_file(second)

    assert "PROVIDER_API_KEY" not in os.environ


def test_secret_env_write_fails_closed_without_keyring(tmp_path, monkeypatch) -> None:
    from crew.state.config import write_secret_env_key

    monkeypatch.setattr(
        PlatformSecretStore,
        "_load_platform_backend",
        staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("unavailable"))),
    )
    env_path = tmp_path / ".env"

    with pytest.raises(SecretStoreUnavailable):
        write_secret_env_key(env_path, "PROVIDER_API_KEY", "secret")

    assert not env_path.exists()


def test_failed_marker_rewrite_restores_previous_authenticated_record(
    tmp_path,
    monkeypatch,
) -> None:
    from crew.state import config as config_module

    backend = _MemoryBackend()
    monkeypatch.setattr(
        PlatformSecretStore,
        "_load_platform_backend",
        staticmethod(lambda: backend),
    )
    env_path = tmp_path / ".env"
    config_module.write_secret_env_key(
        env_path,
        "PROVIDER_API_KEY",
        "first-secret",
        sync_process_env=False,
    )
    original_marker = env_path.read_text(encoding="utf-8")
    monkeypatch.setattr(
        config_module,
        "write_env_key",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("marker write failed")
        ),
    )

    with pytest.raises(OSError, match="marker write failed"):
        config_module.write_secret_env_key(
            env_path,
            "PROVIDER_API_KEY",
            "second-secret",
            sync_process_env=False,
        )

    assert env_path.read_text(encoding="utf-8") == original_marker
    assert config_module._load_env_map(env_path)["PROVIDER_API_KEY"] == "first-secret"


def test_failed_marker_removal_restores_deleted_keyring_record(
    tmp_path,
    monkeypatch,
) -> None:
    from crew.state import config as config_module

    backend = _MemoryBackend()
    monkeypatch.setattr(
        PlatformSecretStore,
        "_load_platform_backend",
        staticmethod(lambda: backend),
    )
    env_path = tmp_path / ".env"
    config_module.write_secret_env_key(
        env_path,
        "PROVIDER_API_KEY",
        "first-secret",
        sync_process_env=False,
    )
    original_marker = env_path.read_text(encoding="utf-8")
    monkeypatch.setattr(
        config_module,
        "remove_env_key",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("marker removal failed")
        ),
    )

    with pytest.raises(OSError, match="marker removal failed"):
        config_module.remove_secret_env_key(
            env_path,
            "PROVIDER_API_KEY",
            sync_process_env=False,
        )

    assert env_path.read_text(encoding="utf-8") == original_marker
    assert config_module._load_env_map(env_path)["PROVIDER_API_KEY"] == "first-secret"


def test_legacy_owner_plaintext_secret_is_migrated_before_use(tmp_path, monkeypatch) -> None:
    from crew.state.config import _load_env_map

    backend = _MemoryBackend()
    monkeypatch.setattr(
        PlatformSecretStore,
        "_load_platform_backend",
        staticmethod(lambda: backend),
    )
    env_path = tmp_path / ".env"
    env_path.write_text(
        "PUBLIC_URL=https://example.test\nPROVIDER_API_KEY=legacy-secret\n",
        encoding="utf-8",
    )

    values = _load_env_map(env_path)

    assert values == {
        "PUBLIC_URL": "https://example.test",
        "PROVIDER_API_KEY": "legacy-secret",
    }
    persisted = env_path.read_text(encoding="utf-8")
    assert "legacy-secret" not in persisted
    assert "PROVIDER_API_KEY=@ace-secret:v1:" in persisted


def test_legacy_plaintext_secret_is_not_loaded_when_migration_fails(
    tmp_path,
    monkeypatch,
) -> None:
    from crew.state.config import _load_env_map

    monkeypatch.setattr(
        PlatformSecretStore,
        "_load_platform_backend",
        staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("unavailable"))),
    )
    env_path = tmp_path / ".env"
    env_path.write_text(
        "PUBLIC_URL=https://example.test\nPROVIDER_API_KEY=legacy-secret\n",
        encoding="utf-8",
    )

    values = _load_env_map(env_path)

    assert values == {"PUBLIC_URL": "https://example.test"}
