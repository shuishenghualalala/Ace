"""Fail-closed trust primitives for directory plugins and remote bundles."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import stat
import time
import zipfile
from collections.abc import Mapping
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlunsplit

from crew.security.outbound import OutboundDenied, OutboundHttpClient, OutboundPolicy

PLUGIN_MANIFEST_SCHEMA_VERSION = "crew.plugin.v1"
PLUGIN_SIGNATURE_SCHEMA_VERSION = "crew.plugin.signature.v1"
PLUGIN_PROVENANCE_SCHEMA_VERSION = "crew.plugin.provenance.v1"
PLUGIN_SIGNATURE_FILE = "plugin.sig.json"
PLUGIN_PROVENANCE_FILE = ".ace-plugin-provenance.json"
PLUGIN_SIGNATURE_DOMAIN = b"crew.plugin.bundle.v1\0"

MAX_PLUGIN_BUNDLE_BYTES = 25 * 1024 * 1024
MAX_PLUGIN_UNPACKED_BYTES = 100 * 1024 * 1024
MAX_PLUGIN_FILE_BYTES = 25 * 1024 * 1024
MAX_PLUGIN_ARCHIVE_MEMBERS = 2048
MAX_PLUGIN_ARCHIVE_DEPTH = 32
MAX_PLUGIN_COMPRESSION_RATIO = 1000.0
PLUGIN_DOWNLOAD_DEADLINE_SECONDS = 60.0

_PLUGIN_OUTBOUND = OutboundPolicy()
_PLUGIN_HTTP = OutboundHttpClient(_PLUGIN_OUTBOUND)

_HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PLUGIN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_KEY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_VERSION = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z"
)

KNOWN_PLUGIN_CAPABILITIES = frozenset(
    {
        "api_router",
        "browser",
        "commands",
        "credentials",
        "dashboard_events",
        "filesystem",
        "hooks",
        "middleware",
        "network",
        "override_tools",
        "platforms",
        "process",
        "skills",
        "tools",
    }
)

_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "name",
        "label",
        "version",
        "description",
        "author",
        "kind",
        "key",
        "capabilities",
        "requires_env",
        "optional_env",
        "provides_tools",
        "provides_hooks",
        "provides_middleware",
        "provides_commands",
        "provides_platforms",
        "config_schema",
        "configSchema",
        "ui_hints",
        "uiHints",
    }
)
_PLUGIN_KINDS = frozenset({"standalone", "backend", "exclusive", "platform", "model-provider"})


class PluginSecurityError(RuntimeError):
    """A plugin artifact failed a security boundary before it could be trusted."""

    def __init__(self, message: str, *, code: str = "plugin_security_error") -> None:
        super().__init__(message)
        self.code = code


def _string_list(raw: Any, field: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise PluginSecurityError(
            f"manifest field {field!r} must be a list of strings",
            code="manifest_schema_invalid",
        )
    values = [item.strip() for item in raw]
    if any(not item for item in values) or len(values) != len(set(values)):
        raise PluginSecurityError(
            f"manifest field {field!r} contains empty or duplicate values",
            code="manifest_schema_invalid",
        )
    return values


def validate_manifest_document(raw: Any, *, directory_key: str) -> dict[str, Any]:
    """Validate and normalize one versioned plugin manifest without importing code."""
    if not isinstance(raw, dict):
        raise PluginSecurityError(
            "plugin manifest must be a YAML object",
            code="manifest_schema_invalid",
        )
    unknown = sorted(str(key) for key in set(raw) - _MANIFEST_FIELDS)
    if unknown:
        raise PluginSecurityError(
            f"manifest contains unsupported fields: {', '.join(unknown)}",
            code="manifest_schema_invalid",
        )
    if raw.get("schema_version") != PLUGIN_MANIFEST_SCHEMA_VERSION:
        raise PluginSecurityError(
            f"manifest schema_version must be {PLUGIN_MANIFEST_SCHEMA_VERSION}",
            code="manifest_schema_invalid",
        )

    name = str(raw.get("name") or "").strip()
    version = str(raw.get("version") or "").strip()
    kind = str(raw.get("kind") or "").strip().lower()
    key = str(raw.get("key") or directory_key or name).strip()
    if not _PLUGIN_NAME.fullmatch(name):
        raise PluginSecurityError(
            "manifest name is missing or unsafe",
            code="manifest_schema_invalid",
        )
    if len(version) > 128 or not _VERSION.fullmatch(version):
        raise PluginSecurityError(
            "manifest version must be valid SemVer",
            code="manifest_schema_invalid",
        )
    if kind not in _PLUGIN_KINDS:
        raise PluginSecurityError(
            f"manifest kind is unsupported: {kind or '<empty>'}",
            code="manifest_schema_invalid",
        )
    key_parts = key.split("/")
    if not key_parts or any(not _PLUGIN_NAME.fullmatch(part) for part in key_parts):
        raise PluginSecurityError(
            "manifest key is unsafe",
            code="manifest_schema_invalid",
        )

    capabilities = _string_list(raw.get("capabilities"), "capabilities")
    if not capabilities:
        raise PluginSecurityError(
            "manifest capabilities must declare at least one capability",
            code="manifest_schema_invalid",
        )
    unsupported_capabilities = sorted(set(capabilities) - KNOWN_PLUGIN_CAPABILITIES)
    if unsupported_capabilities:
        raise PluginSecurityError(
            "manifest declares unsupported capabilities: "
            + ", ".join(unsupported_capabilities),
            code="manifest_schema_invalid",
        )

    for field in ("label", "description", "author"):
        value = raw.get(field)
        if value is not None and not isinstance(value, str):
            raise PluginSecurityError(
                f"manifest field {field!r} must be a string",
                code="manifest_schema_invalid",
            )
    for field in ("requires_env", "optional_env"):
        value = raw.get(field)
        if value is not None and not isinstance(value, list):
            raise PluginSecurityError(
                f"manifest field {field!r} must be a list",
                code="manifest_schema_invalid",
            )
    for field in ("config_schema", "configSchema", "ui_hints", "uiHints"):
        value = raw.get(field)
        if value is not None and not isinstance(value, dict):
            raise PluginSecurityError(
                f"manifest field {field!r} must be an object",
                code="manifest_schema_invalid",
            )

    normalized = dict(raw)
    normalized.update(
        {
            "schema_version": PLUGIN_MANIFEST_SCHEMA_VERSION,
            "name": name,
            "version": version,
            "kind": kind,
            "key": key,
            "capabilities": capabilities,
        }
    )
    for field in (
        "provides_tools",
        "provides_hooks",
        "provides_middleware",
        "provides_commands",
        "provides_platforms",
    ):
        normalized[field] = _string_list(raw.get(field), field)
    return normalized


def _is_link_or_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError as exc:
        raise PluginSecurityError(
            f"plugin path is unreadable: {path}",
            code="plugin_path_unreadable",
        ) from exc
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attrs = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attrs & reparse)


def canonical_plugin_tree_digest(plugin_dir: str | Path) -> str:
    """Hash a plugin tree deterministically while rejecting links and path races."""
    root = Path(plugin_dir)
    try:
        resolved_root = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PluginSecurityError(
            f"plugin root cannot be resolved: {root}",
            code="plugin_path_unreadable",
        ) from exc
    if not resolved_root.is_dir() or _is_link_or_reparse(root):
        raise PluginSecurityError(
            f"plugin root must be a real directory: {root}",
            code="plugin_path_unsafe",
        )

    files: list[tuple[str, Path]] = []
    seen_dirs: set[str] = set()
    for current_raw, dirs, names in os.walk(root, topdown=True, followlinks=False):
        current = Path(current_raw)
        try:
            current_resolved = current.resolve(strict=True)
            current_resolved.relative_to(resolved_root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise PluginSecurityError(
                f"plugin path escapes its root: {current}",
                code="plugin_path_unsafe",
            ) from exc
        current_key = os.path.normcase(str(current_resolved))
        if current_key in seen_dirs:
            raise PluginSecurityError(
                f"plugin directory cycle detected: {current}",
                code="plugin_path_unsafe",
            )
        seen_dirs.add(current_key)

        safe_dirs: list[str] = []
        for name in sorted(dirs):
            child = current / name
            if name == "__pycache__":
                continue
            if _is_link_or_reparse(child):
                raise PluginSecurityError(
                    f"plugin tree contains a link or reparse point: {child}",
                    code="plugin_path_unsafe",
                )
            safe_dirs.append(name)
        dirs[:] = safe_dirs

        for name in sorted(names):
            path = current / name
            if name in {PLUGIN_SIGNATURE_FILE, PLUGIN_PROVENANCE_FILE} or path.suffix == ".pyc":
                continue
            if _is_link_or_reparse(path):
                raise PluginSecurityError(
                    f"plugin tree contains a linked file: {path}",
                    code="plugin_path_unsafe",
                )
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(resolved_root)
            except (OSError, RuntimeError, ValueError) as exc:
                raise PluginSecurityError(
                    f"plugin file escapes its root: {path}",
                    code="plugin_path_unsafe",
                ) from exc
            files.append((resolved.relative_to(resolved_root).as_posix(), resolved))
            if len(files) > MAX_PLUGIN_ARCHIVE_MEMBERS:
                raise PluginSecurityError(
                    "plugin tree contains too many files",
                    code="plugin_tree_too_large",
                )

    digest = hashlib.sha256()
    total_size = 0
    for relative, path in sorted(files):
        relative_bytes = relative.encode("utf-8")
        try:
            before = path.stat()
            if before.st_size > MAX_PLUGIN_FILE_BYTES:
                raise PluginSecurityError(
                    f"plugin file exceeds the size limit: {path}",
                    code="plugin_tree_too_large",
                )
            total_size += before.st_size
            if total_size > MAX_PLUGIN_UNPACKED_BYTES:
                raise PluginSecurityError(
                    "plugin tree exceeds the total size limit",
                    code="plugin_tree_too_large",
                )
            content = path.read_bytes()
            after = path.stat()
        except PluginSecurityError:
            raise
        except OSError as exc:
            raise PluginSecurityError(
                f"plugin file is unreadable: {path}",
                code="plugin_path_unreadable",
            ) from exc
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise PluginSecurityError(
                f"plugin file changed while hashing: {path}",
                code="plugin_path_changed",
            )
        digest.update(len(relative_bytes).to_bytes(4, "big"))
        digest.update(relative_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _decode_base64(value: str, *, label: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise PluginSecurityError(
            f"{label} is not valid base64",
            code="signature_format_invalid",
        ) from exc


def _load_ed25519_public_key(value: str):
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise PluginSecurityError(
            "Ed25519 verification is unavailable",
            code="signature_verifier_unavailable",
        ) from exc

    text = str(value or "").strip()
    if not text:
        raise PluginSecurityError(
            "trusted Ed25519 public key is empty",
            code="trust_anchor_invalid",
        )
    try:
        if "BEGIN PUBLIC KEY" in text:
            key = serialization.load_pem_public_key(text.encode("ascii"))
        else:
            raw = _decode_base64(text, label="trusted public key")
            key = (
                Ed25519PublicKey.from_public_bytes(raw)
                if len(raw) == 32
                else serialization.load_der_public_key(raw)
            )
    except (ValueError, TypeError, UnicodeError) as exc:
        raise PluginSecurityError(
            "trusted Ed25519 public key is invalid",
            code="trust_anchor_invalid",
        ) from exc
    if not isinstance(key, Ed25519PublicKey):
        raise PluginSecurityError(
            "trusted public key is not Ed25519",
            code="trust_anchor_invalid",
        )
    return key


def verify_plugin_signature(
    plugin_dir: str | Path,
    trusted_keys: Mapping[str, str],
) -> dict[str, str]:
    """Verify a detached Ed25519 signature over the exact canonical tree digest."""
    root = Path(plugin_dir)
    signature_path = root / PLUGIN_SIGNATURE_FILE
    try:
        raw = json.loads(signature_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PluginSecurityError(
            "remote plugin is missing its detached signature",
            code="signature_missing",
        ) from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PluginSecurityError(
            "remote plugin signature document is invalid",
            code="signature_format_invalid",
        ) from exc
    if not isinstance(raw, dict) or set(raw) != {
        "schema_version",
        "algorithm",
        "key_id",
        "tree_sha256",
        "signature",
    }:
        raise PluginSecurityError(
            "remote plugin signature schema is invalid",
            code="signature_format_invalid",
        )
    if raw.get("schema_version") != PLUGIN_SIGNATURE_SCHEMA_VERSION:
        raise PluginSecurityError(
            "remote plugin signature schema version is unsupported",
            code="signature_format_invalid",
        )
    if str(raw.get("algorithm") or "").lower() != "ed25519":
        raise PluginSecurityError(
            "remote plugin signature algorithm must be Ed25519",
            code="signature_format_invalid",
        )
    key_id = str(raw.get("key_id") or "").strip()
    if not _KEY_ID.fullmatch(key_id):
        raise PluginSecurityError(
            "remote plugin signer key id is invalid",
            code="signature_format_invalid",
        )
    public_key = trusted_keys.get(key_id)
    if not public_key:
        raise PluginSecurityError(
            f"remote plugin signer is not trusted: {key_id}",
            code="signer_untrusted",
        )
    expected_digest = str(raw.get("tree_sha256") or "").strip().lower()
    if not _HEX_SHA256.fullmatch(expected_digest):
        raise PluginSecurityError(
            "remote plugin tree digest is invalid",
            code="signature_format_invalid",
        )
    actual_digest = canonical_plugin_tree_digest(root)
    if not hmac.compare_digest(expected_digest, actual_digest):
        raise PluginSecurityError(
            "remote plugin tree digest mismatch",
            code="tree_digest_mismatch",
        )
    signature = _decode_base64(str(raw.get("signature") or ""), label="plugin signature")
    try:
        _load_ed25519_public_key(public_key).verify(
            signature,
            PLUGIN_SIGNATURE_DOMAIN + bytes.fromhex(actual_digest),
        )
    except PluginSecurityError:
        raise
    except Exception as exc:  # cryptography exposes InvalidSignature from a backend module
        raise PluginSecurityError(
            "remote plugin Ed25519 signature is invalid",
            code="signature_invalid",
        ) from exc
    return {
        "key_id": key_id,
        "tree_sha256": actual_digest,
    }


def normalized_remote_plugin_url(raw_url: str, *, resolve_dns: bool) -> tuple[str, str]:
    """Validate an HTTPS source and return (fetch URL, credential-free provenance URL)."""
    value = str(raw_url or "")
    if value != value.strip():
        raise PluginSecurityError(
            "remote plugin source URL is invalid",
            code="source_url_invalid",
        )
    try:
        parsed, target = _PLUGIN_OUTBOUND.canonicalize_url(
            value,
            method="GET",
            allowed_schemes=frozenset({"https"}),
        )
        if parsed.fragment:
            raise OutboundDenied("fragment_forbidden")
        if resolve_dns:
            _PLUGIN_OUTBOUND.plan_url(
                target.canonical_url,
                method="GET",
                allowed_schemes=frozenset({"https"}),
            )
    except OutboundDenied as exc:
        if exc.code in {"metadata_target", "non_public_target", "private_grant_required"}:
            code = "source_url_private"
            message = "remote plugin source resolves to a non-public address"
        elif exc.code.startswith("dns_"):
            code = "source_url_unreachable"
            message = "remote plugin source DNS lookup failed"
        else:
            code = "source_url_invalid"
            message = "remote plugin source URL is invalid"
        raise PluginSecurityError(message, code=code) from exc
    provenance_url = urlunsplit(
        ("https", target.authority, target.path or "/", "", "")
    )
    return target.canonical_url, provenance_url


def download_plugin_bundle(source_url: str) -> tuple[bytes, str]:
    """Download one bounded HTTPS artifact without redirects."""
    fetch_url, provenance_url = normalized_remote_plugin_url(source_url, resolve_dns=False)
    started = time.monotonic()
    try:
        response = _PLUGIN_HTTP.fetch(
            fetch_url,
            method="GET",
            headers={"Accept": "application/zip"},
            timeout=30.0,
            max_bytes=MAX_PLUGIN_BUNDLE_BYTES,
            max_redirects=0,
        )
    except OutboundDenied as exc:
        if exc.code == "response_too_large":
            raise PluginSecurityError(
                "remote plugin bundle is too large",
                code="bundle_too_large",
            ) from exc
        if exc.code in {"metadata_target", "non_public_target", "private_grant_required"}:
            raise PluginSecurityError(
                "remote plugin source resolves to a non-public address",
                code="source_url_private",
            ) from exc
        raise PluginSecurityError(
            "remote plugin download failed",
            code="download_failed",
        ) from exc
    if time.monotonic() - started > PLUGIN_DOWNLOAD_DEADLINE_SECONDS:
        raise PluginSecurityError(
            "remote plugin download exceeded its deadline",
            code="download_timeout",
        )
    if response.status != 200:
        raise PluginSecurityError(
            f"remote plugin download failed with HTTP {response.status}",
            code="download_failed",
        )
    return response.body, provenance_url


def extract_plugin_bundle(bundle: bytes, destination: str | Path) -> Path:
    """Extract exactly one plugin directory while blocking traversal, links, and zip bombs."""
    if not isinstance(bundle, bytes) or len(bundle) > MAX_PLUGIN_BUNDLE_BYTES:
        raise PluginSecurityError(
            "remote plugin bundle is too large",
            code="bundle_too_large",
        )
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=False)
    try:
        archive = zipfile.ZipFile(BytesIO(bundle))
    except (zipfile.BadZipFile, OSError) as exc:
        raise PluginSecurityError(
            "remote plugin bundle is not a valid ZIP archive",
            code="bundle_invalid",
        ) from exc

    with archive:
        members = archive.infolist()
        if not members or len(members) > MAX_PLUGIN_ARCHIVE_MEMBERS:
            raise PluginSecurityError(
                "remote plugin archive member count is invalid",
                code="bundle_invalid",
            )
        total_size = 0
        top_levels: set[str] = set()
        seen: set[str] = set()
        files: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
        for member in members:
            raw_name = member.filename
            if (
                not raw_name
                or "\\" in raw_name
                or "\x00" in raw_name
                or member.flag_bits & 0x1
            ):
                raise PluginSecurityError(
                    "unsafe archive path or encrypted member",
                    code="bundle_path_unsafe",
                )
            pure = PurePosixPath(raw_name)
            parts = tuple(part for part in pure.parts if part not in {"", "."})
            if (
                pure.is_absolute()
                or not parts
                or any(part == ".." or ":" in part for part in parts)
            ):
                raise PluginSecurityError(
                    "unsafe archive path",
                    code="bundle_path_unsafe",
                )
            if len(parts) > MAX_PLUGIN_ARCHIVE_DEPTH:
                raise PluginSecurityError(
                    "remote plugin archive path depth exceeds the limit",
                    code="bundle_path_unsafe",
                )
            normalized = "/".join(parts).casefold()
            if normalized in seen:
                raise PluginSecurityError(
                    "remote plugin archive contains duplicate paths",
                    code="bundle_path_unsafe",
                )
            seen.add(normalized)
            top_levels.add(parts[0])
            unix_mode = member.external_attr >> 16
            if stat.S_IFMT(unix_mode) == stat.S_IFLNK:
                raise PluginSecurityError(
                    "remote plugin archive contains a symbolic link",
                    code="bundle_path_unsafe",
                )
            if member.is_dir():
                continue
            if member.file_size > MAX_PLUGIN_FILE_BYTES:
                raise PluginSecurityError(
                    "remote plugin archive contains an oversized file",
                    code="bundle_too_large",
                )
            if member.file_size and member.compress_size > 0:
                if member.file_size / member.compress_size > MAX_PLUGIN_COMPRESSION_RATIO:
                    raise PluginSecurityError(
                        "remote plugin archive compression ratio exceeds the limit",
                        code="bundle_too_large",
                    )
            total_size += member.file_size
            if total_size > MAX_PLUGIN_UNPACKED_BYTES:
                raise PluginSecurityError(
                    "remote plugin archive expands beyond the size limit",
                    code="bundle_too_large",
                )
            files.append((member, parts))
        if len(top_levels) != 1 or not files:
            raise PluginSecurityError(
                "remote plugin archive must contain exactly one top-level directory",
                code="bundle_invalid",
            )

        for member, parts in files:
            target = root.joinpath(*parts)
            try:
                target.resolve(strict=False).relative_to(root.resolve())
            except (OSError, RuntimeError, ValueError) as exc:
                raise PluginSecurityError(
                    "unsafe archive path",
                    code="bundle_path_unsafe",
                ) from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member, "r") as source, target.open("xb") as output:
                copied = 0
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > member.file_size or copied > MAX_PLUGIN_FILE_BYTES:
                        raise PluginSecurityError(
                            "remote plugin archive member exceeded its declared size",
                            code="bundle_too_large",
                        )
                    output.write(chunk)
                if copied != member.file_size:
                    raise PluginSecurityError(
                        "remote plugin archive member size mismatch",
                        code="bundle_invalid",
                    )
        plugin_root = root / next(iter(top_levels))
        if not plugin_root.is_dir():
            raise PluginSecurityError(
                "remote plugin archive root is invalid",
                code="bundle_invalid",
            )
        return plugin_root


def read_plugin_provenance(plugin_dir: str | Path) -> dict[str, Any]:
    path = Path(plugin_dir) / PLUGIN_PROVENANCE_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PluginSecurityError(
            "installed plugin provenance is missing",
            code="provenance_missing",
        ) from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PluginSecurityError(
            "installed plugin provenance is invalid",
            code="provenance_invalid",
        ) from exc
    required = {
        "schema_version",
        "source_url",
        "bundle_sha256",
        "tree_sha256",
        "signer_key_id",
        "installed_by",
        "installed_at",
    }
    if not isinstance(raw, dict) or set(raw) != required:
        raise PluginSecurityError(
            "installed plugin provenance schema is invalid",
            code="provenance_invalid",
        )
    if raw.get("schema_version") != PLUGIN_PROVENANCE_SCHEMA_VERSION:
        raise PluginSecurityError(
            "installed plugin provenance version is unsupported",
            code="provenance_invalid",
        )
    if not _HEX_SHA256.fullmatch(str(raw.get("bundle_sha256") or "")):
        raise PluginSecurityError(
            "installed plugin bundle digest is invalid",
            code="provenance_invalid",
        )
    if not _HEX_SHA256.fullmatch(str(raw.get("tree_sha256") or "")):
        raise PluginSecurityError(
            "installed plugin tree digest is invalid",
            code="provenance_invalid",
        )
    return raw


__all__ = [
    "KNOWN_PLUGIN_CAPABILITIES",
    "MAX_PLUGIN_BUNDLE_BYTES",
    "PLUGIN_MANIFEST_SCHEMA_VERSION",
    "PLUGIN_PROVENANCE_FILE",
    "PLUGIN_PROVENANCE_SCHEMA_VERSION",
    "PLUGIN_SIGNATURE_FILE",
    "PluginSecurityError",
    "canonical_plugin_tree_digest",
    "download_plugin_bundle",
    "extract_plugin_bundle",
    "normalized_remote_plugin_url",
    "read_plugin_provenance",
    "validate_manifest_document",
    "verify_plugin_signature",
]
