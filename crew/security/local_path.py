"""Immutable local-path references parsed before host authorization or I/O."""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import unquote_to_bytes, urlsplit
from urllib.request import url2pathname

_URI_SCHEME = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*):")
_WINDOWS_DRIVE = re.compile(r"[A-Za-z]:")
_WINDOWS_URI_DRIVE = re.compile(r"/[A-Za-z]:/")
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)
_MAX_PERCENT_DECODE_DEPTH = 4


class LocalPathReferenceError(ValueError):
    """Raised when untrusted text cannot denote one unambiguous local path."""


class LocalPathReferenceKind(StrEnum):
    """The syntax retained by a local path reference until final resolution."""

    PLAIN_PATH = "plain_path"
    FILE_URI = "file_uri"


@dataclass(frozen=True, slots=True, init=False)
class LocalPathReference:
    """A validated local path expression that deliberately is not path-like.

    Construction validates syntax only.  It performs no ``expanduser``, filesystem
    lookup, current-directory binding, URI decoding, or canonicalization.  Callers
    must carry this value to an authorization or I/O seam and explicitly call
    :meth:`resolve_at_boundary`.
    """

    _raw: str
    _kind: LocalPathReferenceKind

    @classmethod
    def parse(cls, value: object) -> LocalPathReference:
        """Parse one untrusted model/caller value without touching the filesystem."""

        if not isinstance(value, str):
            raise LocalPathReferenceError("local path reference must be text")
        _validate_text(value)
        if not value:
            raise LocalPathReferenceError("local path reference cannot be empty")

        stripped = value.strip()
        if stripped != value and (
            _URI_SCHEME.match(stripped)
            or stripped.startswith(("//", "\\\\"))
        ):
            raise LocalPathReferenceError(
                "local path reference has ambiguous surrounding whitespace"
            )

        drive = _WINDOWS_DRIVE.match(value)
        scheme = _URI_SCHEME.match(value)
        if drive is not None:
            _validate_plain_path(value)
            kind = LocalPathReferenceKind.PLAIN_PATH
        elif scheme is not None:
            if scheme.group("scheme").casefold() != "file":
                raise LocalPathReferenceError("only local file URIs are accepted")
            _validate_file_uri(value)
            kind = LocalPathReferenceKind.FILE_URI
        else:
            _validate_plain_path(value)
            kind = LocalPathReferenceKind.PLAIN_PATH
        instance = object.__new__(cls)
        object.__setattr__(instance, "_raw", value)
        object.__setattr__(instance, "_kind", kind)
        return instance

    @classmethod
    def from_host_path(cls, value: Path) -> LocalPathReference:
        """Wrap a host-produced ``Path`` while retaining the same syntax checks."""

        if not isinstance(value, Path):
            raise TypeError("host local path must be a pathlib.Path")
        reference = cls.parse(str(value))
        if reference.kind is not LocalPathReferenceKind.PLAIN_PATH:
            raise LocalPathReferenceError("host path was interpreted as a URI")
        return reference

    @property
    def raw(self) -> str:
        return self._raw

    @property
    def kind(self) -> LocalPathReferenceKind:
        return self._kind

    def resolve_at_boundary(
        self,
        *,
        base: Path | None = None,
        strict: bool = False,
    ) -> Path:
        """Decode and canonicalize this reference at an authorization/I/O seam."""

        if not isinstance(strict, bool):
            raise TypeError("strict path resolution flag must be boolean")
        candidate = self._native_path()
        if not candidate.is_absolute():
            if base is None:
                raise LocalPathReferenceError(
                    "relative local path requires a host-owned base directory"
                )
            if not isinstance(base, Path):
                raise TypeError("local path base must be a pathlib.Path")
            try:
                canonical_base = base.expanduser().resolve(strict=True)
            except (OSError, RuntimeError, ValueError) as exc:
                raise LocalPathReferenceError(
                    "host local path base is unavailable"
                ) from exc
            if not canonical_base.is_dir():
                raise LocalPathReferenceError(
                    "host local path base is not a directory"
                )
            candidate = canonical_base / candidate
        try:
            return candidate.expanduser().resolve(strict=strict)
        except (OSError, RuntimeError, ValueError) as exc:
            raise LocalPathReferenceError(
                "local path could not be canonically resolved"
            ) from exc

    def _native_path(self) -> Path:
        if self.kind is LocalPathReferenceKind.PLAIN_PATH:
            return Path(self.raw)
        parsed = urlsplit(self.raw)
        try:
            native = url2pathname(parsed.path)
        except (OSError, ValueError) as exc:
            raise LocalPathReferenceError(
                "file URI cannot be represented on this host"
            ) from exc
        return Path(native)


def decode_file_uri_path(reference: LocalPathReference) -> str:
    """Decode one already-validated file URI for a final legacy adapter."""

    if not isinstance(reference, LocalPathReference):
        raise TypeError("file URI decoder requires a LocalPathReference")
    if reference.kind is not LocalPathReferenceKind.FILE_URI:
        raise LocalPathReferenceError("local path reference is not a file URI")
    return str(reference._native_path())


def _validate_text(value: str) -> None:
    for character in value:
        codepoint = ord(character)
        category = unicodedata.category(character)
        if (
            character == "\x00"
            or codepoint < 0x20
            or 0x7F <= codepoint <= 0x9F
            or category in {"Cc", "Cs"}
        ):
            raise LocalPathReferenceError(
                "local path reference contains a control character"
            )


def _validate_plain_path(value: str) -> None:
    normalized = value.replace("\\", "/")
    folded = normalized.casefold()
    if normalized.startswith("//") or folded.startswith(
        ("//?/", "//./", "/??/")
    ):
        raise LocalPathReferenceError("UNC and Windows device paths are forbidden")

    drive = _WINDOWS_DRIVE.match(value)
    if drive is not None:
        if len(value) == 2 or value[2] not in {"/", "\\"}:
            raise LocalPathReferenceError(
                "drive-relative Windows paths are ambiguous"
            )
        if os.name != "nt":
            raise LocalPathReferenceError(
                "Windows drive paths are not local to this host"
            )
    elif os.name == "nt" and value.startswith(("/", "\\")):
        raise LocalPathReferenceError(
            "drive-less rooted Windows paths are ambiguous"
        )
    elif os.name != "nt" and "\\" in value:
        raise LocalPathReferenceError(
            "Windows separators are not local to this host"
        )

    if os.name == "nt":
        _validate_windows_components(value, has_drive=drive is not None)


def _validate_windows_components(value: str, *, has_drive: bool) -> None:
    normalized = value.replace("\\", "/")
    remainder = normalized[2:] if has_drive else normalized
    if ":" in remainder:
        raise LocalPathReferenceError(
            "Windows alternate data streams are forbidden"
        )
    for component in remainder.split("/"):
        if component in {"", ".", ".."}:
            continue
        if component.endswith((" ", ".")):
            raise LocalPathReferenceError(
                "Windows path components cannot end in space or dot"
            )
        if any(character in '<>"|?*' for character in component):
            raise LocalPathReferenceError(
                "Windows path contains a forbidden character"
            )
        basename = component.split(".", 1)[0].upper()
        if basename in _WINDOWS_RESERVED_NAMES:
            raise LocalPathReferenceError(
                "Windows reserved device names are forbidden"
            )


def _validate_file_uri(value: str) -> None:
    if not value[:5].casefold() == "file:":
        raise LocalPathReferenceError("local file URI scheme is invalid")
    if "?" in value[5:] or "#" in value[5:]:
        raise LocalPathReferenceError(
            "local file URI cannot contain a query or fragment"
        )
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise LocalPathReferenceError("local file URI is malformed") from exc
    if parsed.scheme.casefold() != "file":
        raise LocalPathReferenceError("local file URI scheme is invalid")
    if parsed.netloc.casefold() not in {"", "localhost"}:
        raise LocalPathReferenceError(
            "local file URI cannot name a remote authority"
        )
    if not parsed.path.startswith("/"):
        raise LocalPathReferenceError(
            "relative and opaque file URIs are forbidden"
        )
    if parsed.path.startswith("//") or "\\" in parsed.path:
        raise LocalPathReferenceError(
            "UNC and backslash file URI forms are forbidden"
        )

    decoded = _validate_percent_encoding(parsed.path)
    if decoded.startswith(("//", "\\\\")):
        raise LocalPathReferenceError("decoded file URI cannot be a UNC path")
    if "|" in decoded[:4]:
        raise LocalPathReferenceError(
            "legacy Windows drive URI syntax is forbidden"
        )
    encoded_drive_uri = _WINDOWS_URI_DRIVE.match(parsed.path)
    drive_uri = _WINDOWS_URI_DRIVE.match(decoded)
    if drive_uri is not None and encoded_drive_uri is None:
        raise LocalPathReferenceError(
            "Windows file URI drive syntax must not be percent encoded"
        )
    if drive_uri is not None and os.name != "nt":
        raise LocalPathReferenceError(
            "Windows file URI is not local to this host"
        )
    if os.name == "nt":
        if drive_uri is None:
            raise LocalPathReferenceError(
                "drive-less Windows file URIs are ambiguous"
            )
        windows_value = decoded[1:]
        _validate_windows_components(
            windows_value,
            has_drive=True,
        )


def _validate_percent_encoding(value: str) -> str:
    probe = value
    for _depth in range(_MAX_PERCENT_DECODE_DEPTH):
        index = 0
        while index < len(probe):
            if probe[index] != "%":
                index += 1
                continue
            escape = probe[index + 1:index + 3]
            if len(escape) != 2 or any(char not in _HEX_DIGITS for char in escape):
                raise LocalPathReferenceError(
                    "local file URI contains invalid percent encoding"
                )
            decoded_byte = int(escape, 16)
            if decoded_byte in {0x2F, 0x5C}:
                raise LocalPathReferenceError(
                    "local file URI contains an encoded path separator"
                )
            if decoded_byte < 0x20 or 0x7F <= decoded_byte <= 0x9F:
                raise LocalPathReferenceError(
                    "local file URI contains an encoded control character"
                )
            index += 3
        try:
            decoded = unquote_to_bytes(probe).decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise LocalPathReferenceError(
                "local file URI encoding is not valid UTF-8"
            ) from exc
        _validate_text(decoded)
        if decoded == probe:
            return decoded
        probe = decoded
    if "%" in probe:
        index = probe.find("%")
        escape = probe[index + 1:index + 3]
        if len(escape) == 2 and all(char in _HEX_DIGITS for char in escape):
            raise LocalPathReferenceError(
                "local file URI percent encoding is nested too deeply"
            )
    return probe
