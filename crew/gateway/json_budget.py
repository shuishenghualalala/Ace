"""Streaming structural budget for Gateway JSON request bodies."""

from __future__ import annotations

import re
import json
from dataclasses import dataclass, field


class JSONStructureBudgetExceeded(ValueError):
    """The JSON body exceeded a configured structural budget."""


class JSONStructureInvalid(ValueError):
    """The byte stream is not structurally valid JSON."""


@dataclass(frozen=True)
class JSONBudgetLimits:
    max_depth: int = 32
    max_nodes: int = 100_000
    max_object_keys: int = 10_000
    max_array_items: int = 10_000
    max_string_bytes: int = 4 * 1024 * 1024
    max_number_chars: int = 128


@dataclass
class _Container:
    kind: str
    state: str
    keys: int = 0
    items: int = 0
    key_names: set[str] = field(default_factory=set)


_NUMBER_RE = re.compile(
    rb"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$"
)
_WHITESPACE = frozenset(b" \t\r\n")
_NUMBER_CHARS = frozenset(b"-+0123456789.eE")
_VALUE_DELIMITERS = _WHITESPACE | frozenset(b",]}")
_HEX = frozenset(b"0123456789abcdefABCDEF")


class JSONStructureBudget:
    """Scan JSON syntax incrementally without materializing or parsing it."""

    def __init__(self, limits: JSONBudgetLimits | None = None) -> None:
        self.limits = limits or JSONBudgetLimits()
        self._stack: list[_Container] = []
        self._root_started = False
        self._root_done = False
        self._nodes = 0
        self._total_keys = 0
        self._mode = "normal"
        self._string_kind = ""
        self._string_bytes = 0
        self._string_token = bytearray()
        self._escaped = False
        self._unicode_remaining = 0
        self._token = bytearray()
        self._literal = b""

    def _budget(self) -> None:
        raise JSONStructureBudgetExceeded

    def _invalid(self) -> None:
        raise JSONStructureInvalid

    def _start_value(self) -> None:
        if self._root_done or (not self._stack and self._root_started):
            self._invalid()
        if self._stack:
            parent = self._stack[-1]
            if parent.kind == "object":
                if parent.state != "value":
                    self._invalid()
            elif parent.state not in {"value_or_end", "value_required"}:
                self._invalid()
            if parent.kind == "array":
                parent.items += 1
                if parent.items > self.limits.max_array_items:
                    self._budget()
        if len(self._stack) >= self.limits.max_depth:
            self._budget()
        self._root_started = True
        self._nodes += 1
        if self._nodes > self.limits.max_nodes:
            self._budget()

    def _finish_value(self) -> None:
        if self._stack:
            self._stack[-1].state = "after_value"
        else:
            self._root_done = True

    def _start_container(self, kind: str) -> None:
        self._start_value()
        initial = "key_or_end" if kind == "object" else "value_or_end"
        self._stack.append(_Container(kind, initial))

    def _start_key(self) -> None:
        if not self._stack or self._stack[-1].kind != "object":
            self._invalid()
        container = self._stack[-1]
        if container.state not in {"key_or_end", "key_required"}:
            self._invalid()
        container.keys += 1
        self._total_keys += 1
        if (
            container.keys > self.limits.max_object_keys
            or self._total_keys > self.limits.max_nodes
        ):
            self._budget()
        self._string_kind = "key"
        self._string_bytes = 0
        self._string_token = bytearray()
        self._escaped = False
        self._unicode_remaining = 0
        self._mode = "string"

    def _start_string_value(self) -> None:
        self._start_value()
        self._string_kind = "value"
        self._string_bytes = 0
        self._string_token = bytearray()
        self._escaped = False
        self._unicode_remaining = 0
        self._mode = "string"

    def _start_scalar(self, first: int) -> None:
        self._start_value()
        if first in b"-0123456789":
            self._mode = "number"
            self._token = bytearray((first,))
            return
        literal = {ord("t"): b"true", ord("f"): b"false", ord("n"): b"null"}.get(first)
        if literal is None:
            self._invalid()
        self._mode = "literal"
        self._literal = literal
        self._token = bytearray((first,))

    def _finish_number(self) -> None:
        if not _NUMBER_RE.fullmatch(bytes(self._token)):
            self._invalid()
        self._mode = "normal"
        self._finish_value()

    def _finish_container(self, closing: int) -> None:
        if not self._stack:
            self._invalid()
        container = self._stack[-1]
        if closing == ord("]"):
            if container.kind != "array" or container.state not in {"value_or_end", "after_value"}:
                self._invalid()
        elif container.kind != "object" or container.state not in {"key_or_end", "after_value"}:
            self._invalid()
        self._stack.pop()
        self._finish_value()

    def _consume_string(self, byte: int) -> None:
        if self._unicode_remaining:
            if byte not in _HEX:
                self._invalid()
            self._unicode_remaining -= 1
            self._string_bytes += 1
            return
        if self._escaped:
            if byte == ord("u"):
                self._unicode_remaining = 4
            elif byte not in b'"\\/bfnrt':
                self._invalid()
            self._escaped = False
            self._string_bytes += 1
            return
        if byte == ord('"'):
            self._mode = "normal"
            if self._string_kind == "key":
                try:
                    key = json.loads(b'"' + bytes(self._string_token) + b'"')
                except (ValueError, UnicodeError):
                    self._invalid()
                if not isinstance(key, str):
                    self._invalid()
                container = self._stack[-1]
                if key in container.key_names:
                    self._invalid()
                container.key_names.add(key)
                self._stack[-1].state = "colon"
            else:
                self._finish_value()
            return
        if byte == ord("\\"):
            self._escaped = True
        elif byte < 0x20:
            self._invalid()
        if self._string_kind == "key":
            self._string_token.append(byte)
        self._string_bytes += 1
        if self._string_bytes > self.limits.max_string_bytes:
            self._budget()

    def feed(self, chunk: bytes | bytearray | memoryview) -> None:
        """Consume one ASGI body chunk, preserving state across chunks."""
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            self._invalid()
        data = bytes(chunk)
        index = 0
        while index < len(data):
            byte = data[index]
            if self._mode == "string":
                self._consume_string(byte)
                index += 1
                continue
            if self._mode == "number":
                if byte in _NUMBER_CHARS:
                    self._token.append(byte)
                    if len(self._token) > self.limits.max_number_chars:
                        self._budget()
                    index += 1
                    continue
                if byte in _VALUE_DELIMITERS:
                    self._finish_number()
                    continue
                self._invalid()
            if self._mode == "literal":
                expected = self._literal[len(self._token)] if len(self._token) < len(self._literal) else None
                if expected is None:
                    self._invalid()
                if byte != expected:
                    self._invalid()
                self._token.append(byte)
                index += 1
                if len(self._token) == len(self._literal):
                    self._mode = "normal"
                    self._finish_value()
                continue

            if byte in _WHITESPACE:
                index += 1
                continue
            if byte == ord('"'):
                if (
                    self._stack
                    and self._stack[-1].kind == "object"
                    and self._stack[-1].state in {"key_or_end", "key_required"}
                ):
                    self._start_key()
                else:
                    self._start_string_value()
                index += 1
                continue
            if byte == ord("{"):
                self._start_container("object")
                index += 1
                continue
            if byte == ord("["):
                self._start_container("array")
                index += 1
                continue
            if byte in b"-0123456789tfn":
                self._start_scalar(byte)
                index += 1
                continue
            if byte == ord(":"):
                if not self._stack or self._stack[-1].kind != "object" or self._stack[-1].state != "colon":
                    self._invalid()
                self._stack[-1].state = "value"
                index += 1
                continue
            if byte == ord(","):
                if not self._stack or self._stack[-1].state != "after_value":
                    self._invalid()
                self._stack[-1].state = "key_required" if self._stack[-1].kind == "object" else "value_required"
                index += 1
                continue
            if byte in b"]}":
                self._finish_container(byte)
                index += 1
                continue
            self._invalid()

    def finish(self) -> None:
        """Finish the stream and reject incomplete JSON."""
        if self._mode == "number":
            self._finish_number()
        if self._mode != "normal" or self._stack or not self._root_done:
            self._invalid()
