"""Platform-keyring persistence for sensitive MCP configuration fields."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from crew.security.secret_store import (
    PlatformSecretStore,
    SecretBinding,
    SecretDeletion,
    SecretIdentifier,
    SecretMutation,
    SecretNotFound,
    SecretStoreUnavailable,
)
from crew.tools.redact import argv_contains_sensitive_value

_SENSITIVE_NAME_RE = re.compile(
    r"(?:AUTHORIZATION|COOKIE|API[-_]?KEY|KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL)$",
    re.IGNORECASE,
)
_MCP_SECRET_TTL_SECONDS = 30 * 24 * 60 * 60


def mcp_field_is_sensitive(section: str, name: str) -> bool:
    normalized_section = str(section).lower()
    normalized_name = str(name)
    return normalized_section in {"env", "headers", "oauth"} and (
        _SENSITIVE_NAME_RE.search(normalized_name) is not None
        or normalized_name.lower()
        in {"authorization", "cookie", "x-api-key", "access_token", "refresh_token"}
    )


def _secret_config_value(section: str, raw_value: Any) -> str | None:
    """Return a persisted credential value without flattening env provenance."""
    if section == "env" and isinstance(raw_value, dict):
        source = str(raw_value.get("source") or "").strip().lower()
        if source == "remote" and set(raw_value) == {"source"}:
            return None
        if source != "local" or set(raw_value) != {"source", "value"}:
            raise ValueError("invalid MCP env credential provenance")
        value = raw_value.get("value")
    else:
        value = raw_value
    if not isinstance(value, str):
        raise ValueError("MCP credential value must be a string")
    return value


def _replace_secret_config_value(
    section: str,
    raw_value: Any,
    value: str,
) -> Any:
    if section == "env" and isinstance(raw_value, dict):
        return {"source": "local", "value": value}
    return value


def mcp_secret_identifier(
    server_name: str,
    section: str,
    field_name: str,
    config: dict[str, Any],
) -> SecretIdentifier:
    server = str(server_name)
    group = str(section).lower()
    field = str(field_name)
    if (
        not server
        or not field
        or group not in {"env", "headers", "oauth"}
        or "\x00" in server
        or "\x00" in field
    ):
        raise ValueError("invalid MCP secret identity")
    canonical_field = field.casefold() if group == "headers" else field
    audience = _credential_audience(config)
    digest = hashlib.sha256(
        f"{server}\0{group}\0{canonical_field}\0{audience}".encode()
    ).hexdigest()
    return SecretIdentifier(
        namespace="mcp-config",
        scope="gateway-global",
        name=f"{group}-{digest}",
    )


def mcp_secret_binding(
    server_name: str,
    section: str,
    field_name: str,
    config: dict[str, Any],
) -> SecretBinding:
    """Bind an MCP credential to its owner scope, audience, purpose and lease."""
    group = str(section).lower()
    field = str(field_name)
    ttl = config.get("secret_ttl_seconds", _MCP_SECRET_TTL_SECONDS)
    try:
        ttl_seconds = float(ttl)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid MCP secret TTL") from exc
    canonical_field = field.casefold() if group == "headers" else field
    return SecretBinding(
        owner="gateway-global",
        task="mcp-config",
        host=_credential_audience(config),
        purpose=f"mcp:{group}:{canonical_field}",
        ttl_seconds=ttl_seconds,
    )


def _credential_audience(config: dict[str, Any]) -> str:
    url = str(config.get("url") or "").strip()
    if url:
        try:
            parsed = urlsplit(url)
            scheme = parsed.scheme.lower()
            host = (parsed.hostname or "").encode("idna").decode("ascii").lower()
            port = parsed.port or (443 if scheme == "https" else 80)
        except (UnicodeError, ValueError) as exc:
            raise ValueError("invalid MCP credential audience") from exc
        if (
            scheme != "https"
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError("MCP credentials require a canonical HTTPS audience")
        return f"https://{host}:{port}"

    command = str(config.get("command") or "").strip()
    args = config.get("args") or []
    if not command or not isinstance(args, list) or not all(
        isinstance(value, str) and "\x00" not in value for value in args
    ):
        raise ValueError("invalid MCP stdio credential audience")
    command_digest = config.get("command_sha256")
    if (
        not isinstance(command_digest, str)
        or len(command_digest) != 64
        or any(
            char not in "0123456789abcdef"
            for char in command_digest.casefold()
        )
    ):
        raise ValueError("invalid MCP command digest")
    audience = {"args": args, "command": command}
    audience["command_sha256"] = command_digest.casefold()
    payload = json.dumps(
        audience,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"stdio:{hashlib.sha256(payload.encode()).hexdigest()}"


def _sensitive_positions(
    servers: dict[str, Any],
) -> dict[SecretIdentifier, tuple[str, str, str, str]]:
    positions: dict[SecretIdentifier, tuple[str, str, str, str]] = {}
    for raw_server, raw_config in servers.items():
        server = str(raw_server)
        if not isinstance(raw_config, dict):
            continue
        for section in ("env", "headers", "oauth"):
            values = raw_config.get(section)
            if not isinstance(values, dict):
                continue
            for raw_name, raw_value in values.items():
                name = str(raw_name)
                if not mcp_field_is_sensitive(section, name):
                    continue
                secret_value = _secret_config_value(section, raw_value)
                if secret_value is None:
                    continue
                identifier = mcp_secret_identifier(
                    server,
                    section,
                    name,
                    raw_config,
                )
                positions[identifier] = (
                    server,
                    section,
                    name,
                    secret_value,
                )
    return positions


def _config_has_credential_bearing_url_or_argv(config: dict[str, Any]) -> bool:
    url = str(config.get("url") or "").strip()
    if url and (
        argv_contains_sensitive_value((url,))
        or PlatformSecretStore.is_marker(url)
    ):
        return True
    command = str(config.get("command") or "").strip()
    args = config.get("args") or []
    return bool(
        command
        and isinstance(args, list)
        and (
            argv_contains_sensitive_value(
                (command, *(str(value) for value in args))
            )
            or any(
                PlatformSecretStore.is_marker(str(value))
                for value in (command, *args)
            )
        )
    )


def _servers_have_credential_bearing_url_or_argv(servers: dict[str, Any]) -> bool:
    return any(
        isinstance(config, dict)
        and _config_has_credential_bearing_url_or_argv(config)
        for config in servers.values()
    )


def mcp_servers_have_plaintext_secrets(servers: dict[str, Any]) -> bool:
    return _servers_have_credential_bearing_url_or_argv(servers) or any(
        not PlatformSecretStore.is_marker(value)
        for _identifier, (_server, _section, _name, value) in _sensitive_positions(
            servers
        ).items()
    )


@dataclass(slots=True)
class McpSecretTransaction:
    protected_servers: dict[str, Any]
    _store: PlatformSecretStore | None
    _mutations: tuple[SecretMutation, ...]
    _deletions: tuple[SecretDeletion, ...]

    def rollback(self) -> None:
        if self._store is None:
            return
        failures: list[Exception] = []
        for deletion in reversed(self._deletions):
            try:
                self._store.rollback_deletion(deletion)
            except Exception as exc:  # noqa: BLE001 - attempt every rollback
                failures.append(exc)
        for mutation in reversed(self._mutations):
            try:
                self._store.rollback(mutation)
            except Exception as exc:  # noqa: BLE001 - attempt every rollback
                failures.append(exc)
        if failures:
            raise SecretStoreUnavailable(
                "MCP secret transaction rollback failed"
            ) from failures[0]


def prepare_mcp_server_secrets(
    servers: dict[str, Any],
    *,
    previous_servers: dict[str, Any] | None = None,
) -> McpSecretTransaction:
    """Replace plaintext fields with bound markers and stage removed-secret deletion."""
    protected = {
        str(server): dict(config) if isinstance(config, dict) else {}
        for server, config in servers.items()
    }
    if _servers_have_credential_bearing_url_or_argv(protected):
        raise SecretStoreUnavailable(
            "MCP credentials in URL or argv are forbidden"
        )
    current = _sensitive_positions(protected)
    previous = _sensitive_positions(previous_servers or {})
    if not current and not previous:
        return McpSecretTransaction(
            protected,
            None,
            (),
            (),
        )

    store = PlatformSecretStore.platform()
    mutations: list[SecretMutation] = []
    deletions: list[SecretDeletion] = []
    try:
        for identifier, (server, section, name, value) in current.items():
            if not value or value == "***" or "\x00" in value:
                raise ValueError("MCP secret value is invalid")
            config = protected[server]
            binding = mcp_secret_binding(server, section, name, config)
            if PlatformSecretStore.is_marker(value):
                store.resolve_marker(identifier, value, binding=binding)
                marker = value
            else:
                mutation = store.replace(identifier, value, binding=binding)
                mutations.append(mutation)
                marker = store.marker_for_mutation(
                    identifier,
                    mutation,
                    binding=binding,
                )
            values = dict(config.get(section) or {})
            values[name] = _replace_secret_config_value(
                section,
                values.get(name),
                marker,
            )
            config[section] = values

        for identifier, (_server, _section, _name, old_value) in previous.items():
            if identifier in current or not PlatformSecretStore.is_marker(old_value):
                continue
            try:
                old_server, old_section, old_name, _old_value = previous[identifier]
                old_config = (previous_servers or {}).get(old_server)
                if not isinstance(old_config, dict):
                    raise SecretStoreUnavailable("MCP secret owner configuration is invalid")
                deletions.append(
                    store.delete_transactional(
                        identifier,
                        binding=mcp_secret_binding(
                            old_server,
                            old_section,
                            old_name,
                            old_config,
                        ),
                    )
                )
            except SecretNotFound:
                continue
    except Exception:
        transaction = McpSecretTransaction(
            protected,
            store,
            tuple(mutations),
            tuple(deletions),
        )
        transaction.rollback()
        raise
    return McpSecretTransaction(
        protected,
        store,
        tuple(mutations),
        tuple(deletions),
    )


def resolve_mcp_server_secrets(
    server_name: str,
    config: dict[str, Any],
    *,
    sections: tuple[str, ...] = ("env", "headers"),
) -> dict[str, Any]:
    """Resolve only identifier-bound markers for one MCP worker."""
    resolved = dict(config)
    if _config_has_credential_bearing_url_or_argv(resolved):
        raise SecretStoreUnavailable(
            "MCP credentials in URL or argv are forbidden"
        )
    store: PlatformSecretStore | None = None
    if not sections or any(
        section not in {"env", "headers", "oauth"} for section in sections
    ):
        raise ValueError("invalid MCP secret sections")
    for section in sections:
        raw_values = config.get(section)
        if not isinstance(raw_values, dict):
            continue
        values: dict[str, Any] = {}
        for raw_name, raw_value in raw_values.items():
            name = str(raw_name)
            if mcp_field_is_sensitive(section, name):
                value = _secret_config_value(section, raw_value)
                if value is None:
                    values[name] = dict(raw_value)
                    continue
                if not PlatformSecretStore.is_marker(value):
                    raise SecretStoreUnavailable(
                        "plaintext MCP credential reached runtime"
                    )
                if store is None:
                    store = PlatformSecretStore.platform()
                value = store.resolve_marker(
                    mcp_secret_identifier(server_name, section, name, config),
                    value,
                    binding=mcp_secret_binding(server_name, section, name, config),
                )
                values[name] = _replace_secret_config_value(
                    section,
                    raw_value,
                    value,
                )
            else:
                values[name] = raw_value
        resolved[section] = values
    return resolved
