"""Fail-closed access to platform-native credential storage.

Ace stores each credential directly in an approved OS credential backend. It
does not maintain a plaintext or decrypted process cache, and persistent config
contains only a versioned identifier-bound marker.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from typing import Protocol

_SERVICE_NAME = "Ace.SecuritySecrets.v1"
_MARKER_PREFIX = "@ace-secret:v1:"
_MAX_SECRET_CHARS = 16_384
_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")
_MAX_BINDING_TEXT = 256
_MAX_BINDING_TTL_SECONDS = 365 * 24 * 60 * 60
_APPROVED_BACKENDS = frozenset(
    {
        "keyring.backends.SecretService.Keyring",
        "keyring.backends.Windows.WinVaultKeyring",
        "keyring.backends.macOS.Keyring",
    }
)


class SecretStoreError(RuntimeError):
    """Base class for stable, non-secret secret-store failures."""


class SecretStoreUnavailable(SecretStoreError):
    """The configured platform backend cannot prove secure storage."""


class SecretNotFound(SecretStoreError):
    """A required secret is absent from the platform backend."""


@dataclass(frozen=True, slots=True)
class SecretBinding:
    """Non-secret access facts authenticated with a platform secret record."""

    owner: str
    task: str
    host: str
    purpose: str
    ttl_seconds: float
    issued_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        for label, value in (
            ("owner", self.owner),
            ("task", self.task),
            ("host", self.host),
            ("purpose", self.purpose),
        ):
            if (
                not isinstance(value, str)
                or not value
                or len(value) > _MAX_BINDING_TEXT
                or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
            ):
                raise ValueError(f"invalid secret binding {label}")
        if (
            not isinstance(self.ttl_seconds, (int, float))
            or not math.isfinite(float(self.ttl_seconds))
            or float(self.ttl_seconds) <= 0
            or float(self.ttl_seconds) > _MAX_BINDING_TTL_SECONDS
        ):
            raise ValueError("invalid secret binding TTL")
        if (
            not isinstance(self.issued_at, (int, float))
            or not math.isfinite(float(self.issued_at))
            or float(self.issued_at) <= 0
        ):
            raise ValueError("invalid secret binding issue time")

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            {
                "host": self.host,
                "issued_at": float(self.issued_at),
                "owner": self.owner,
                "purpose": self.purpose,
                "task": self.task,
                "ttl_seconds": float(self.ttl_seconds),
                "version": 1,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")

    def is_valid(self, now: float | None = None) -> bool:
        current = time.time() if now is None else float(now)
        return current <= float(self.issued_at) + float(self.ttl_seconds)

    def to_record(self) -> dict[str, object]:
        return {
            "host": self.host,
            "issued_at": float(self.issued_at),
            "owner": self.owner,
            "purpose": self.purpose,
            "task": self.task,
            "ttl_seconds": float(self.ttl_seconds),
        }

    @classmethod
    def from_record(cls, raw: object) -> SecretBinding:
        if not isinstance(raw, dict) or set(raw) != {
            "host",
            "issued_at",
            "owner",
            "purpose",
            "task",
            "ttl_seconds",
        }:
            raise ValueError("secret binding schema is invalid")
        return cls(
            owner=raw["owner"],
            task=raw["task"],
            host=raw["host"],
            purpose=raw["purpose"],
            ttl_seconds=raw["ttl_seconds"],
            issued_at=raw["issued_at"],
        )


class _CredentialBackend(Protocol):
    def get_password(self, service: str, account: str) -> str | None: ...

    def set_password(self, service: str, account: str, value: str) -> None: ...

    def delete_password(self, service: str, account: str) -> None: ...


@dataclass(frozen=True, slots=True)
class SecretMutation:
    """Opaque rollback state for one keyring replacement."""

    account: str
    previous_record: str | None = field(repr=False)
    replacement_record: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class SecretDeletion:
    """Opaque rollback state for one keyring deletion."""

    account: str
    previous_record: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class SecretIdentifier:
    """Canonical namespace, security scope, and credential name."""

    namespace: str
    scope: str
    name: str

    def __post_init__(self) -> None:
        for label, value in (
            ("namespace", self.namespace),
            ("scope", self.scope),
            ("name", self.name),
        ):
            if not isinstance(value, str) or _COMPONENT_RE.fullmatch(value) is None:
                raise ValueError(f"invalid secret {label}")

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            {
                "name": self.name,
                "namespace": self.namespace,
                "scope": self.scope,
                "version": 1,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")


class PlatformSecretStore:
    """Versioned wrapper over an approved platform credential backend."""

    def __init__(self, backend: _CredentialBackend) -> None:
        self._backend = backend

    @classmethod
    def platform(cls) -> PlatformSecretStore:
        try:
            backend = cls._load_platform_backend()
        except Exception:
            raise SecretStoreUnavailable(
                "approved platform secret backend is unavailable"
            ) from None
        return cls(backend)

    @classmethod
    def for_backend(cls, backend: _CredentialBackend) -> PlatformSecretStore:
        """Construct with an isolated backend for contract tests."""
        return cls(backend)

    @staticmethod
    def _load_platform_backend() -> _CredentialBackend:
        try:
            import keyring
        except ImportError:
            raise SecretStoreUnavailable(
                "platform keyring support is not installed"
            ) from None

        backend = keyring.get_keyring()
        backend_name = f"{type(backend).__module__}.{type(backend).__qualname__}"
        if backend_name not in _APPROVED_BACKENDS:
            raise SecretStoreUnavailable("unapproved platform keyring backend")
        return backend

    @staticmethod
    def _account(identifier: SecretIdentifier) -> str:
        scope_digest = hashlib.sha256(identifier.scope.encode("utf-8")).hexdigest()
        return f"v1:{identifier.namespace}:{scope_digest}:{identifier.name}"

    @staticmethod
    def _encode_record(value: str, binding: SecretBinding | None = None) -> str:
        marker_key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
        record: dict[str, object] = {
            "marker_key": marker_key,
            "secret": value,
            "version": 2 if binding is not None else 1,
        }
        if binding is not None:
            record["binding"] = binding.to_record()
        return json.dumps(
            record,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _decode_record(
        encoded_record: str,
    ) -> tuple[str, bytes, SecretBinding | None]:
        try:
            record = json.loads(encoded_record)
            version = record.get("version") if isinstance(record, dict) else None
            if type(version) is not int or version not in {1, 2}:
                raise ValueError("secret record schema is invalid")
            expected_keys = {"marker_key", "secret", "version"}
            if version == 2:
                expected_keys.add("binding")
            if set(record) != expected_keys:
                raise ValueError("secret record schema is invalid")
            value = record["secret"]
            encoded_key = record["marker_key"]
            if (
                not isinstance(value, str)
                or not value
                or "\x00" in value
                or len(value) > _MAX_SECRET_CHARS
                or not isinstance(encoded_key, str)
            ):
                raise ValueError("secret record value is invalid")
            key = base64.b64decode(
                encoded_key.encode("ascii"),
                altchars=b"-_",
                validate=True,
            )
            if len(key) != 32:
                raise ValueError("secret record marker key has invalid length")
            binding = (
                SecretBinding.from_record(record["binding"])
                if version == 2
                else None
            )
            return value, key, binding
        except Exception:
            raise SecretStoreUnavailable("platform secret record is invalid") from None

    def _read_record(
        self,
        identifier: SecretIdentifier,
    ) -> tuple[str, bytes, SecretBinding | None]:
        try:
            encoded_record = self._backend.get_password(
                _SERVICE_NAME,
                self._account(identifier),
            )
        except Exception:
            raise SecretStoreUnavailable("platform secret read failed") from None
        if encoded_record is None:
            raise SecretNotFound("required platform secret is absent")
        return self._decode_record(encoded_record)

    @classmethod
    def _marker_for_key(
        cls,
        identifier: SecretIdentifier,
        marker_key: bytes,
        binding: SecretBinding | None = None,
    ) -> str:
        account = cls._account(identifier)
        binding_digest = (
            hmac.new(marker_key, binding.canonical_bytes(), hashlib.sha256)
            .hexdigest()
            .encode("ascii")
            if binding is not None
            else b""
        )
        marker_input = (
            _SERVICE_NAME.encode("ascii")
            + b"\0"
            + account.encode("ascii")
            + b"\0"
            + identifier.canonical_bytes()
            + b"\0"
            + binding_digest
        )
        return f"{_MARKER_PREFIX}{hmac.new(marker_key, marker_input, hashlib.sha256).hexdigest()}"

    @staticmethod
    def _require_binding(
        stored: SecretBinding | None,
        requested: SecretBinding | None,
        *,
        check_expiry: bool = True,
    ) -> None:
        if stored is None:
            if requested is not None:
                raise SecretStoreUnavailable("secret binding is missing")
            return
        if requested is None or any(
            getattr(stored, field_name) != getattr(requested, field_name)
            for field_name in ("owner", "task", "host", "purpose")
        ):
            raise SecretStoreUnavailable("secret binding does not match")
        if float(requested.ttl_seconds) > float(stored.ttl_seconds):
            raise SecretStoreUnavailable("secret binding TTL exceeds stored lease")
        if check_expiry and not stored.is_valid():
            raise SecretStoreUnavailable("secret binding has expired")

    def marker(
        self,
        identifier: SecretIdentifier,
        *,
        binding: SecretBinding | None = None,
    ) -> str:
        _value, marker_key, stored_binding = self._read_record(identifier)
        if binding is not None:
            self._require_binding(stored_binding, binding)
        return self._marker_for_key(identifier, marker_key, stored_binding)

    def marker_for_mutation(
        self,
        identifier: SecretIdentifier,
        mutation: SecretMutation,
        *,
        binding: SecretBinding | None = None,
    ) -> str:
        if mutation.account != self._account(identifier):
            raise SecretStoreUnavailable("secret mutation belongs to another identifier")
        _value, marker_key, stored_binding = self._decode_record(
            mutation.replacement_record
        )
        if binding is not None:
            self._require_binding(stored_binding, binding)
        return self._marker_for_key(identifier, marker_key, stored_binding)

    @staticmethod
    def is_marker(value: object) -> bool:
        return isinstance(value, str) and value.startswith(_MARKER_PREFIX)

    def set(
        self,
        identifier: SecretIdentifier,
        value: str,
        *,
        binding: SecretBinding | None = None,
    ) -> None:
        if (
            not isinstance(value, str)
            or not value
            or "\x00" in value
            or len(value) > _MAX_SECRET_CHARS
        ):
            raise ValueError("secret value is empty, invalid, or too large")
        try:
            account = self._account(identifier)
            if binding is not None:
                previous = self._backend.get_password(_SERVICE_NAME, account)
                if previous is not None:
                    _old_value, _old_key, old_binding = self._decode_record(previous)
                    if old_binding is not None:
                        self._require_binding(
                            old_binding,
                            binding,
                            check_expiry=False,
                        )
            self._backend.set_password(
                _SERVICE_NAME,
                account,
                self._encode_record(value, binding),
            )
        except Exception:
            raise SecretStoreUnavailable("platform secret write failed") from None

    def replace(
        self,
        identifier: SecretIdentifier,
        value: str,
        *,
        binding: SecretBinding | None = None,
    ) -> SecretMutation:
        """Replace a record and retain exact rollback state for marker persistence."""
        if (
            not isinstance(value, str)
            or not value
            or "\x00" in value
            or len(value) > _MAX_SECRET_CHARS
        ):
            raise ValueError("secret value is empty, invalid, or too large")
        account = self._account(identifier)
        previous: str | None = None
        try:
            previous = self._backend.get_password(_SERVICE_NAME, account)
            if previous is not None:
                _old_value, _old_key, old_binding = self._decode_record(previous)
                if old_binding is not None:
                    self._require_binding(
                        old_binding,
                        binding,
                        check_expiry=False,
                    )
            replacement = self._encode_record(value, binding)
            self._backend.set_password(_SERVICE_NAME, account, replacement)
        except SecretStoreUnavailable:
            raise
        except Exception:
            try:
                current = self._backend.get_password(_SERVICE_NAME, account)
                if previous is None:
                    if current is not None:
                        self._backend.delete_password(_SERVICE_NAME, account)
                elif current != previous:
                    self._backend.set_password(_SERVICE_NAME, account, previous)
            except Exception:
                raise SecretStoreUnavailable(
                    "platform secret replacement cleanup failed"
                ) from None
            raise SecretStoreUnavailable("platform secret replacement failed") from None
        return SecretMutation(
            account=account,
            previous_record=previous,
            replacement_record=replacement,
        )

    def rollback(self, mutation: SecretMutation) -> None:
        """Restore a failed replacement without clobbering a concurrent writer."""
        if not isinstance(mutation, SecretMutation):
            raise SecretStoreUnavailable("secret rollback state is invalid")
        try:
            current = self._backend.get_password(
                _SERVICE_NAME,
                mutation.account,
            )
            if (
                not isinstance(current, str)
                or not hmac.compare_digest(current, mutation.replacement_record)
            ):
                raise SecretStoreUnavailable(
                    "secret changed concurrently; rollback was refused"
                )
            if mutation.previous_record is None:
                self._backend.delete_password(
                    _SERVICE_NAME,
                    mutation.account,
                )
            else:
                self._backend.set_password(
                    _SERVICE_NAME,
                    mutation.account,
                    mutation.previous_record,
                )
        except SecretStoreUnavailable:
            raise
        except Exception:
            raise SecretStoreUnavailable("platform secret rollback failed") from None

    def get(
        self,
        identifier: SecretIdentifier,
        *,
        binding: SecretBinding | None = None,
    ) -> str:
        value, _marker_key, stored_binding = self._read_record(identifier)
        self._require_binding(stored_binding, binding)
        return value

    def delete(
        self,
        identifier: SecretIdentifier,
        *,
        binding: SecretBinding | None = None,
    ) -> None:
        try:
            existing = self._backend.get_password(
                _SERVICE_NAME,
                self._account(identifier),
            )
            if existing is None:
                return
            _value, _key, stored_binding = self._decode_record(existing)
            if stored_binding is not None:
                self._require_binding(
                    stored_binding,
                    binding,
                    check_expiry=False,
                )
            self._backend.delete_password(
                _SERVICE_NAME,
                self._account(identifier),
            )
        except Exception:
            raise SecretStoreUnavailable("platform secret deletion failed") from None

    def delete_transactional(
        self,
        identifier: SecretIdentifier,
        *,
        binding: SecretBinding | None = None,
    ) -> SecretDeletion:
        """Delete a record while retaining exact rollback state."""
        account = self._account(identifier)
        try:
            previous = self._backend.get_password(_SERVICE_NAME, account)
            if previous is not None:
                _value, _key, stored_binding = self._decode_record(previous)
                if stored_binding is not None:
                    self._require_binding(
                        stored_binding,
                        binding,
                        check_expiry=False,
                    )
                try:
                    self._backend.delete_password(_SERVICE_NAME, account)
                except Exception:
                    try:
                        current = self._backend.get_password(_SERVICE_NAME, account)
                        if current is None:
                            self._backend.set_password(
                                _SERVICE_NAME,
                                account,
                                previous,
                            )
                    except Exception:
                        raise SecretStoreUnavailable(
                            "platform secret deletion cleanup failed"
                        ) from None
                    raise SecretStoreUnavailable(
                        "platform secret deletion failed"
                    ) from None
        except SecretStoreUnavailable:
            raise
        except Exception:
            raise SecretStoreUnavailable("platform secret deletion failed") from None
        return SecretDeletion(account=account, previous_record=previous)

    def rollback_deletion(self, deletion: SecretDeletion) -> None:
        if not isinstance(deletion, SecretDeletion):
            raise SecretStoreUnavailable("secret deletion rollback state is invalid")
        if deletion.previous_record is None:
            return
        try:
            current = self._backend.get_password(
                _SERVICE_NAME,
                deletion.account,
            )
            if current is not None:
                raise SecretStoreUnavailable(
                    "secret changed concurrently; deletion rollback was refused"
                )
            self._backend.set_password(
                _SERVICE_NAME,
                deletion.account,
                deletion.previous_record,
            )
        except SecretStoreUnavailable:
            raise
        except Exception:
            raise SecretStoreUnavailable(
                "platform secret deletion rollback failed"
            ) from None

    def resolve_marker(
        self,
        identifier: SecretIdentifier,
        marker: str,
        *,
        binding: SecretBinding | None = None,
    ) -> str:
        try:
            value, marker_key, stored_binding = self._read_record(identifier)
        except SecretNotFound:
            raise SecretStoreUnavailable(
                "secret marker is invalid or belongs to another scope"
            ) from None
        try:
            self._require_binding(stored_binding, binding)
        except SecretStoreUnavailable:
            raise SecretStoreUnavailable(
                "secret marker is invalid or belongs to another scope"
            ) from None
        expected = self._marker_for_key(identifier, marker_key, stored_binding)
        if (
            not isinstance(marker, str)
            or not hmac.compare_digest(marker, expected)
        ):
            raise SecretStoreUnavailable(
                "secret marker is invalid or belongs to another scope"
            )
        return value
