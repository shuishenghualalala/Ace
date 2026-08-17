"""Platform credential-store contracts for local Ace secrets."""

from __future__ import annotations

import os

import pytest

from crew.security.secret_store import (
    PlatformSecretStore,
    SecretBinding,
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


class _FailAfterWriteBackend(_MemoryBackend):
    def __init__(self) -> None:
        super().__init__()
        self.fail = False

    def set_password(self, service: str, account: str, value: str) -> None:
        super().set_password(service, account, value)
        if self.fail:
            self.fail = False
            raise RuntimeError("simulated backend write failure")


class _FailingReadBackend(_MemoryBackend):
    def get_password(self, service: str, account: str) -> str | None:
        raise RuntimeError("secret-backend-canary")


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


def test_bound_secret_requires_matching_context_and_expires() -> None:
    store = PlatformSecretStore.for_backend(_MemoryBackend())
    identifier = SecretIdentifier("provider", "owner", "API_KEY")
    binding = SecretBinding(
        owner="owner-a",
        task="task-a",
        host="api.example.test",
        purpose="provider-api",
        ttl_seconds=60,
    )
    store.set(identifier, "bound-secret", binding=binding)
    marker = store.marker(identifier)

    assert store.resolve_marker(identifier, marker, binding=binding) == "bound-secret"
    with pytest.raises(SecretStoreUnavailable):
        store.resolve_marker(
            identifier,
            marker,
            binding=SecretBinding(
                owner="owner-b",
                task="task-a",
                host="api.example.test",
                purpose="provider-api",
                ttl_seconds=60,
            ),
        )
    with pytest.raises(SecretStoreUnavailable):
        expired_binding = SecretBinding(
            owner="owner-a",
            task="task-a",
            host="api.example.test",
            purpose="provider-api",
            ttl_seconds=60,
            issued_at=binding.issued_at - 120,
        )
        expired_store = PlatformSecretStore.for_backend(_MemoryBackend())
        expired_store.set(identifier, "expired-secret", binding=expired_binding)
        expired_store.resolve_marker(
            identifier,
            expired_store.marker(identifier),
            binding=expired_binding,
        )


def test_rotation_upgrades_legacy_record_to_a_bound_record() -> None:
    backend = _MemoryBackend()
    store = PlatformSecretStore.for_backend(backend)
    identifier = SecretIdentifier("provider", "owner", "API_KEY")
    binding = SecretBinding(
        owner="owner-a",
        task="task-a",
        host="api.example.test",
        purpose="provider-api",
        ttl_seconds=60,
    )

    store.set(identifier, "legacy-secret")
    old_marker = store.marker(identifier)
    mutation = store.replace(identifier, "rotated-secret", binding=binding)
    new_marker = store.marker_for_mutation(identifier, mutation, binding=binding)

    with pytest.raises(SecretStoreUnavailable):
        store.resolve_marker(identifier, old_marker, binding=binding)
    assert store.resolve_marker(identifier, new_marker, binding=binding) == "rotated-secret"


def test_secure_secret_writer_does_not_publish_secret_to_process_environment(
    tmp_path,
    monkeypatch,
) -> None:
    from crew.state.config import write_secret_env_key

    backend = _MemoryBackend()
    monkeypatch.setattr(
        PlatformSecretStore,
        "_load_platform_backend",
        staticmethod(lambda: backend),
    )
    monkeypatch.delenv("PROVIDER_API_KEY", raising=False)

    write_secret_env_key(tmp_path / ".env", "PROVIDER_API_KEY", "process-secret")

    assert "PROVIDER_API_KEY" not in os.environ


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


def test_partial_platform_write_is_cleaned_before_failure_is_returned() -> None:
    backend = _FailAfterWriteBackend()
    store = PlatformSecretStore.for_backend(backend)
    identifier = SecretIdentifier("provider", "owner", "API_KEY")
    store.set(identifier, "old-secret")
    original = dict(backend.values)
    backend.fail = True

    with pytest.raises(SecretStoreUnavailable):
        store.replace(identifier, "new-secret")

    assert backend.values == original


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
    from crew.state.config import _load_crew_home_env_file, _load_env_map, write_secret_env_key

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
    assert "PROVIDER_API_KEY" not in os.environ
    assert _load_env_map(env_path)["PROVIDER_API_KEY"] == secret


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


def test_backend_errors_do_not_echo_secret_details() -> None:
    store = PlatformSecretStore.for_backend(_FailingReadBackend())
    identifier = SecretIdentifier("provider", "owner", "API_KEY")

    with pytest.raises(SecretStoreUnavailable) as caught:
        store.get(identifier)

    assert "secret-backend-canary" not in str(caught.value)
    assert caught.value.__cause__ is None


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


def test_failed_marker_write_scrubs_legacy_plaintext_line(
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
    env_path.write_text("PROVIDER_API_KEY=legacy-secret\n", encoding="utf-8")
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
            "replacement-secret",
            sync_process_env=False,
        )

    assert "legacy-secret" not in env_path.read_text(encoding="utf-8")
    assert "replacement-secret" not in env_path.read_text(encoding="utf-8")


def test_secret_marker_rewrite_removes_duplicate_plaintext_entries(
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
    env_path.write_text(
        "PROVIDER_API_KEY = first-legacy\nexport PROVIDER_API_KEY=second-legacy\n",
        encoding="utf-8",
    )

    config_module.write_secret_env_key(
        env_path,
        "PROVIDER_API_KEY",
        "replacement-secret",
        sync_process_env=False,
    )

    persisted = env_path.read_text(encoding="utf-8")
    assert "first-legacy" not in persisted
    assert "second-legacy" not in persisted
    assert persisted.count("PROVIDER_API_KEY=@ace-secret:v1:") == 1


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


def test_orphan_owner_marker_failure_is_logged_once_until_replaced(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    from crew.state import config as config_module
    from crew.state.config import _load_env_map

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
    backend.values.clear()
    backend.reads = 0

    with caplog.at_level("ERROR", logger="crew.state.config"):
        assert _load_env_map(env_path) == {}
        assert _load_env_map(env_path) == {}

    failures = [
        record
        for record in caplog.records
        if "owner credential marker validation failed" in record.message
    ]
    assert len(failures) == 1
    assert "重新保存" in failures[0].message
    assert backend.reads == 1


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
    assert "legacy-secret" not in env_path.read_text(encoding="utf-8")
