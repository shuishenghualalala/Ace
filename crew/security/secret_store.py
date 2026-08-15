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
import os
import re
from dataclasses import dataclass, field
from typing import Protocol

_SERVICE_NAME = "Ace.SecuritySecrets.v1"
_MARKER_PREFIX = "@ace-secret:v1:"
_MAX_SECRET_CHARS = 16_384
_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")
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
        except Exception as exc:
            raise SecretStoreUnavailable(
                "approved platform secret backend is unavailable"
            ) from exc
        return cls(backend)

    @classmethod
    def for_backend(cls, backend: _CredentialBackend) -> PlatformSecretStore:
        """Construct with an isolated backend for contract tests."""
        return cls(backend)

    @staticmethod
    def _load_platform_backend() -> _CredentialBackend:
        try:
            import keyring
        except ImportError as exc:
            raise SecretStoreUnavailable(
                "platform keyring support is not installed"
            ) from exc

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
    def _encode_record(value: str) -> str:
        marker_key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
        return json.dumps(
            {
                "marker_key": marker_key,
                "secret": value,
                "version": 1,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _decode_record(encoded_record: str) -> tuple[str, bytes]:
        try:
            record = json.loads(encoded_record)
            if (
                not isinstance(record, dict)
                or record.get("version") != 1
                or set(record) != {"marker_key", "secret", "version"}
            ):
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
            return value, key
        except Exception as exc:
            raise SecretStoreUnavailable("platform secret record is invalid") from exc

    def _read_record(self, identifier: SecretIdentifier) -> tuple[str, bytes]:
        try:
            encoded_record = self._backend.get_password(
                _SERVICE_NAME,
                self._account(identifier),
            )
        except Exception as exc:
            raise SecretStoreUnavailable("platform secret read failed") from exc
        if encoded_record is None:
            raise SecretNotFound("required platform secret is absent")
        return self._decode_record(encoded_record)

    @classmethod
    def _marker_for_key(
        cls,
        identifier: SecretIdentifier,
        marker_key: bytes,
    ) -> str:
        account = cls._account(identifier)
        binding = hmac.new(
            marker_key,
            _SERVICE_NAME.encode("ascii")
            + b"\0"
            + account.encode("ascii")
            + b"\0"
            + identifier.canonical_bytes(),
            hashlib.sha256,
        ).hexdigest()
        return f"{_MARKER_PREFIX}{binding}"

    def marker(self, identifier: SecretIdentifier) -> str:
        _value, marker_key = self._read_record(identifier)
        return self._marker_for_key(identifier, marker_key)

    def marker_for_mutation(
        self,
        identifier: SecretIdentifier,
        mutation: SecretMutation,
    ) -> str:
        if mutation.account != self._account(identifier):
            raise SecretStoreUnavailable("secret mutation belongs to another identifier")
        _value, marker_key = self._decode_record(mutation.replacement_record)
        return self._marker_for_key(identifier, marker_key)

    @staticmethod
    def is_marker(value: object) -> bool:
        return isinstance(value, str) and value.startswith(_MARKER_PREFIX)

    def set(self, identifier: SecretIdentifier, value: str) -> None:
        if (
            not isinstance(value, str)
            or not value
            or "\x00" in value
            or len(value) > _MAX_SECRET_CHARS
        ):
            raise ValueError("secret value is empty, invalid, or too large")
        try:
            self._backend.set_password(
                _SERVICE_NAME,
                self._account(identifier),
                self._encode_record(value),
            )
        except Exception as exc:
            raise SecretStoreUnavailable("platform secret write failed") from exc

    def replace(self, identifier: SecretIdentifier, value: str) -> SecretMutation:
        """Replace a record and retain exact rollback state for marker persistence."""
        if (
            not isinstance(value, str)
            or not value
            or "\x00" in value
            or len(value) > _MAX_SECRET_CHARS
        ):
            raise ValueError("secret value is empty, invalid, or too large")
        account = self._account(identifier)
        replacement = self._encode_record(value)
        try:
            previous = self._backend.get_password(_SERVICE_NAME, account)
            if previous is not None:
                self._decode_record(previous)
            self._backend.set_password(_SERVICE_NAME, account, replacement)
        except SecretStoreUnavailable:
            raise
        except Exception as exc:
            raise SecretStoreUnavailable("platform secret replacement failed") from exc
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
        except Exception as exc:
            raise SecretStoreUnavailable("platform secret rollback failed") from exc

    def get(self, identifier: SecretIdentifier) -> str:
        value, _marker_key = self._read_record(identifier)
        return value

    def delete(self, identifier: SecretIdentifier) -> None:
        try:
            existing = self._backend.get_password(
                _SERVICE_NAME,
                self._account(identifier),
            )
            if existing is None:
                return
            self._backend.delete_password(
                _SERVICE_NAME,
                self._account(identifier),
            )
        except Exception as exc:
            raise SecretStoreUnavailable("platform secret deletion failed") from exc

    def delete_transactional(self, identifier: SecretIdentifier) -> SecretDeletion:
        """Delete a record while retaining exact rollback state."""
        account = self._account(identifier)
        try:
            previous = self._backend.get_password(_SERVICE_NAME, account)
            if previous is not None:
                self._decode_record(previous)
                self._backend.delete_password(_SERVICE_NAME, account)
        except SecretStoreUnavailable:
            raise
        except Exception as exc:
            raise SecretStoreUnavailable("platform secret deletion failed") from exc
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
        except Exception as exc:
            raise SecretStoreUnavailable(
                "platform secret deletion rollback failed"
            ) from exc

    def resolve_marker(self, identifier: SecretIdentifier, marker: str) -> str:
        try:
            value, marker_key = self._read_record(identifier)
        except SecretNotFound as exc:
            raise SecretStoreUnavailable(
                "secret marker is invalid or belongs to another scope"
            ) from exc
        expected = self._marker_for_key(identifier, marker_key)
        if (
            not isinstance(marker, str)
            or not hmac.compare_digest(marker, expected)
        ):
            raise SecretStoreUnavailable(
                "secret marker is invalid or belongs to another scope"
            )
        return value
