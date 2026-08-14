"""Canonical, hashable descriptions of actions that may need approval."""

from __future__ import annotations

import hashlib
import ipaddress
import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Sequence


class ActionKind(StrEnum):
    EXEC = "exec"
    FILE = "file"
    NETWORK = "network"


@dataclass(frozen=True)
class NormalizedAction:
    """Stable action fields used for exact grants and persistent rules."""

    kind: ActionKind
    executable: str = ""
    argv: tuple[str, ...] = ()
    raw_command: str = ""
    shell_kind: str = ""
    parsed_commands: tuple[tuple[str, ...], ...] = ()
    canonical_digest: str = ""
    cwd: str = ""
    path: str = ""
    operation: str = ""
    host: str = ""
    port: int = 0
    protocol: str = ""
    offset: int = 0
    limit: int = 0
    content_digest: str = ""

    @property
    def digest(self) -> str:
        payload = asdict(self)
        # Preserve schema-1 exact digests for file/network/direct-argv actions.
        # A shell command opts into raw_command binding, while classifier evidence is
        # deliberately excluded: parser availability/result must not invalidate a
        # SESSION grant when the user switches approval modes.
        if not self.raw_command:
            payload.pop("raw_command", None)
        if not self.content_digest:
            payload.pop("content_digest", None)
        payload.pop("shell_kind", None)
        payload.pop("parsed_commands", None)
        payload.pop("canonical_digest", None)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def normalize_exec_action(
    argv: Sequence[str],
    cwd: str | Path,
    *,
    raw_command: str = "",
    shell_kind: str = "",
    parsed_commands: Sequence[Sequence[str]] = (),
    canonical_digest: str = "",
) -> NormalizedAction:
    """Normalize argv and bind the exact user-visible shell command when present."""
    tokens = tuple(_nonempty_text(token, "argv token") for token in argv)
    if not tokens:
        raise ValueError("argv 不能为空")
    executable = tokens[0]
    executable_path = Path(executable).expanduser()
    if executable_path.is_absolute():
        executable = str(executable_path.resolve(strict=False))
        tokens = (executable, *tokens[1:])
    visible_command = _nonempty_text(raw_command, "raw_command") if raw_command else ""
    normalized_shell_kind = str(shell_kind).strip().lower()
    normalized_commands = tuple(
        tuple(_nonempty_text(token, "parsed command token") for token in command)
        for command in parsed_commands
    )
    classification_digest = str(canonical_digest).strip().lower()
    if classification_digest and (
        len(classification_digest) != 64
        or any(char not in "0123456789abcdef" for char in classification_digest)
    ):
        raise ValueError("canonical_digest 必须是 SHA-256 hex")
    if (normalized_shell_kind or normalized_commands or classification_digest) and not visible_command:
        raise ValueError("shell classification 必须绑定 raw_command")
    return NormalizedAction(
        kind=ActionKind.EXEC,
        executable=executable,
        argv=tokens,
        raw_command=visible_command,
        shell_kind=normalized_shell_kind,
        parsed_commands=normalized_commands,
        canonical_digest=classification_digest,
        cwd=str(Path(cwd).expanduser().resolve(strict=False)),
    )


def normalize_file_action(
    path: str | Path,
    operation: str,
    *,
    offset: int = 0,
    limit: int = 0,
    content_digest: str = "",
) -> NormalizedAction:
    """Normalize one structured file operation against its final host path."""
    normalized_operation = _nonempty_text(operation, "operation").lower()
    if normalized_operation not in {"read", "write", "patch", "delete"}:
        raise ValueError(f"不支持的文件操作: {operation!r}")
    if offset < 0 or limit < 0:
        raise ValueError("offset/limit 不能为负数")
    normalized_content_digest = str(content_digest).strip().lower()
    if normalized_content_digest and (
        len(normalized_content_digest) != 64
        or any(char not in "0123456789abcdef" for char in normalized_content_digest)
    ):
        raise ValueError("content_digest 必须是 SHA-256 hex")
    return NormalizedAction(
        kind=ActionKind.FILE,
        path=str(Path(path).expanduser().resolve(strict=False)),
        operation=normalized_operation,
        offset=int(offset),
        limit=int(limit),
        content_digest=normalized_content_digest,
    )


def normalize_network_action(host: str, port: int, protocol: str) -> NormalizedAction:
    """Normalize one exact host/port/protocol target and reject wildcard rules."""
    normalized_host = _normalize_host(host)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("port 必须在 1..65535")
    normalized_protocol = _nonempty_text(protocol, "protocol").lower()
    if normalized_protocol not in {"http", "https", "tcp", "udp", "socks5_tcp", "socks5_udp"}:
        raise ValueError(f"不支持的网络协议: {protocol!r}")
    return NormalizedAction(
        kind=ActionKind.NETWORK,
        host=normalized_host,
        port=port,
        protocol=normalized_protocol,
    )


def _normalize_host(raw: str) -> str:
    host = _nonempty_text(raw, "host").rstrip(".").lower()
    if any(marker in host for marker in ("*", "://", "/", "?", "#")):
        raise ValueError("host 必须是精确主机名或 IP，不能包含 wildcard、scheme 或 path")
    if any(char.isspace() for char in host):
        raise ValueError("host 不能包含空白")
    try:
        return ipaddress.ip_address(host.strip("[]")).compressed
    except ValueError:
        try:
            return host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError("host 不是有效主机名") from exc


def _nonempty_text(value: object, field: str) -> str:
    text = str(value)
    if not text or not text.strip() or "\x00" in text:
        raise ValueError(f"{field} 不能为空或包含 NUL")
    return text
