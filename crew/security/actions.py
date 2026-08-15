"""Canonical, hashable descriptions of actions that may need approval."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path


class ActionKind(StrEnum):
    EXEC = "exec"
    FILE = "file"
    NETWORK = "network"


@dataclass(frozen=True)
class NormalizedAction:
    """Stable action fields used for exact grants and persistent rules."""

    kind: ActionKind
    executable: str = ""
    executable_digest: str = ""
    argv: tuple[str, ...] = ()
    raw_command: str = ""
    shell_kind: str = ""
    parsed_commands: tuple[tuple[str, ...], ...] = ()
    command_identities: tuple[tuple[str, str], ...] = ()
    canonical_digest: str = ""
    cwd: str = ""
    path: str = ""
    operation: str = ""
    host: str = ""
    port: int = 0
    protocol: str = ""
    method: str = ""
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
        if not self.executable_digest:
            payload.pop("executable_digest", None)
        if not self.command_identities:
            payload.pop("command_identities", None)
        if not self.method:
            payload.pop("method", None)
        payload.pop("shell_kind", None)
        payload.pop("parsed_commands", None)
        payload.pop("canonical_digest", None)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def serialize_normalized_action(action: NormalizedAction) -> dict[str, object]:
    """Return the complete, deterministic action representation used by snapshots."""
    if not isinstance(action, NormalizedAction):
        raise TypeError("action 必须是 NormalizedAction")
    payload = asdict(action)
    payload["kind"] = action.kind.value
    payload["argv"] = list(action.argv)
    payload["parsed_commands"] = [list(command) for command in action.parsed_commands]
    payload["command_identities"] = [list(identity) for identity in action.command_identities]
    return payload


def normalize_exec_action(
    argv: Sequence[str],
    cwd: str | Path,
    *,
    raw_command: str = "",
    shell_kind: str = "",
    parsed_commands: Sequence[Sequence[str]] = (),
    canonical_digest: str = "",
    executable_digest: str = "",
    command_identities: Sequence[Sequence[str]] = (),
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
    normalized_executable_digest = str(executable_digest).strip().lower()
    if normalized_executable_digest and not re.fullmatch(
        r"[0-9a-f]{64}", normalized_executable_digest
    ):
        raise ValueError("executable_digest 必须是 SHA-256 hex")
    normalized_command_identities = tuple(
        (
            _nonempty_text(identity[0], "command identity path"),
            str(identity[1]).strip().lower(),
        )
        for identity in command_identities
        if len(identity) == 2
    )
    if len(normalized_command_identities) != len(tuple(command_identities)):
        raise ValueError("command_identities 必须是 [path, digest] 数组")
    if any(
        not re.fullmatch(r"[0-9a-f]{64}", digest)
        for _path, digest in normalized_command_identities
    ):
        raise ValueError("command identity digest 必须是 SHA-256 hex")
    if (normalized_shell_kind or normalized_commands or classification_digest) and not visible_command:
        raise ValueError("shell classification 必须绑定 raw_command")
    return NormalizedAction(
        kind=ActionKind.EXEC,
        executable=executable,
        executable_digest=normalized_executable_digest,
        argv=tokens,
        raw_command=visible_command,
        shell_kind=normalized_shell_kind,
        parsed_commands=normalized_commands,
        command_identities=normalized_command_identities,
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


def normalize_network_action(
    host: str,
    port: int,
    protocol: str,
    *,
    method: str = "",
) -> NormalizedAction:
    """Normalize one exact network target and optional application method."""
    normalized_host = _normalize_host(host)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("port 必须在 1..65535")
    normalized_protocol = _nonempty_text(protocol, "protocol").lower()
    if normalized_protocol not in {"http", "https", "tcp", "udp", "socks5_tcp", "socks5_udp"}:
        raise ValueError(f"不支持的网络协议: {protocol!r}")
    normalized_method = str(method).strip().upper()
    if normalized_method and (
        len(normalized_method) > 32
        or not all(
            char.isascii()
            and (char.isupper() or char.isdigit() or char in "!#$%&'*+-.^_`|~")
            for char in normalized_method
        )
    ):
        raise ValueError(f"不支持的网络方法: {method!r}")
    return NormalizedAction(
        kind=ActionKind.NETWORK,
        host=normalized_host,
        port=port,
        protocol=normalized_protocol,
        method=normalized_method,
    )


def _normalize_host(raw: str) -> str:
    from crew.security.outbound import OutboundDenied, canonicalize_host

    host = _nonempty_text(raw, "host")
    if any(marker in host for marker in ("*", "://", "/", "?", "#")):
        raise ValueError("host 必须是精确主机名或 IP，不能包含 wildcard、scheme 或 path")
    try:
        return canonicalize_host(host)
    except OutboundDenied as exc:
        raise ValueError(f"host 不是有效主机名: {exc}") from exc


def _nonempty_text(value: object, field: str) -> str:
    text = str(value)
    if not text or not text.strip() or "\x00" in text:
        raise ValueError(f"{field} 不能为空或包含 NUL")
    return text
