"""Retain combined terminal output and serve pages at absolute UTF-8 offsets."""

from __future__ import annotations

import codecs
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from crew.tools.file_utils import FileConflictError, open_verified_read
from crew.tools.output_filters import strip_ansi


MAX_PERSISTED_BYTES = 64 * 1024 * 1024
MAX_PAGE_BYTES = 24 * 1024
MAX_PAGE_JSON_CHARS = 36_000
_LEGACY_SCAN_BYTES = 1024 * 1024


def _decode_prefix(data: bytes) -> tuple[str, int]:
    """Return decoded characters and the number of original bytes consumed."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        text = decoder.decode(data, final=False)
    except UnicodeDecodeError as exc:
        return data[:exc.start].decode("utf-8"), exc.start
    return text, len(data) - len(decoder.getstate()[0])


def _archive_size(path: Path) -> int:
    try:
        with open_verified_read(path) as (info, _):
            return info.st_size
    except (OSError, FileConflictError):
        return 0


@dataclass
class ProcessOutput:
    """Bounded memory tail and contiguous durable prefix of published text."""

    tail: str = ""
    tail_offset: int = 0
    tail_line: int = 0
    tail_partial_line: bool = False
    end: int = 0
    characters: int = 0
    newlines: int = 0
    ends_with_newline: bool = False
    persisted: int = 0
    storage_failed: bool = False
    capture_truncated: bool = False

    def append(self, text: str, *, max_chars: int, output_ref: str) -> None:
        data = text.encode("utf-8")
        if output_ref and not self.storage_failed and self.persisted < MAX_PERSISTED_BYTES:
            limit = MAX_PERSISTED_BYTES - self.persisted
            length = len(data) if len(data) <= limit else _decode_prefix(data[:limit])[1]
            if length < len(data):
                # The durable range remains a prefix, even if the next character did not fit.
                self.storage_failed = True
            if length:
                self._persist(Path(output_ref), data[:length])
        self.end += len(data)
        self.characters += len(text)
        self.newlines += text.count("\n")
        self.ends_with_newline = text.endswith("\n")
        self.tail += text
        overflow = len(self.tail) - max(0, max_chars)
        if overflow > 0:
            removed, self.tail = self.tail[:overflow], self.tail[overflow:]
            self.tail_offset += len(removed.encode("utf-8"))
            self.tail_line += removed.count("\n")
            self.tail_partial_line = not removed.endswith("\n")

    def _persist(self, path: Path, data: bytes) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("ab") as handle:
                if os.fstat(handle.fileno()).st_size != self.persisted:
                    self.storage_failed = True
                    return
                handle.write(data)
            self.persisted += len(data)
        except OSError:
            self.storage_failed = True

    def snapshot(self, output_ref: str) -> OutputSnapshot:
        return OutputSnapshot(
            self.tail, self.tail_offset, self.tail_line, self.tail_partial_line, self.end,
            self.newlines + int(self.end > 0 and not self.ends_with_newline),
            self.persisted, output_ref, self.capture_truncated,
        )


@dataclass(frozen=True)
class OutputSnapshot:
    tail: str
    tail_offset: int
    tail_line: int
    tail_partial_line: bool
    end: int
    total_lines: int | None
    persisted: int
    output_ref: str
    capture_truncated: bool = False
    archived_only: bool = False

    @classmethod
    def archived(
        cls, output_ref: str, *, known_end: int = 0, confirmed_bytes: int | None = None,
        total_lines: int | None = None, capture_truncated: bool = False,
    ) -> OutputSnapshot:
        size = _archive_size(Path(output_ref)) if output_ref else 0
        end = max(known_end, size)
        persisted = min(size, confirmed_bytes) if confirmed_bytes is not None else size
        return cls("", end, 0, False, end, total_lines, persisted, output_ref, capture_truncated, True)

    def metadata(self) -> dict[str, Any]:
        lost = self.persisted < self.tail_offset
        if self.persisted >= self.end and self.persisted:
            retention = "persisted"
        elif not lost and self.tail:
            retention = "memory" if not self.persisted else "partial"
        elif self.persisted or self.tail:
            retention = "partial"
        else:
            retention = "unavailable" if lost or self.archived_only else "memory"
        return {
            "output_ref": self.output_ref if self.persisted else "",
            "output_stream": "combined",
            "total_output_bytes": self.end,
            "total_lines": self.total_lines,
            "persisted_output_bytes": self.persisted,
            "retained_start": 0 if self.persisted else self.tail_offset,
            "retained_tail_start": self.tail_offset,
            "output_retention": retention,
            "output_truncated": lost or self.capture_truncated,
            "capture_truncated": self.capture_truncated,
        }

    def page(
        self, cursor: int, *, max_bytes: int, end_offset: int | None = None, tool_budget: bool = False,
    ) -> dict[str, Any]:
        stop = self.end if end_offset is None else min(end_offset, self.end)
        start = min(max(cursor, 0), stop)
        gap = start != cursor
        if start < stop:
            if start < self.tail_offset and start < self.persisted:
                try:
                    with open_verified_read(Path(self.output_ref)) as (_, handle):
                        handle.seek(start)
                        data = handle.read(min(max_bytes, self.persisted - start, stop - start))
                except (OSError, FileConflictError):
                    data = b""
                if not data:
                    gap = True
                    start = min(self.tail_offset, stop)
                    data = self._tail_slice(start, stop, max_bytes)
            else:
                if start < self.tail_offset:
                    start = min(self.tail_offset, stop)
                    gap = True
                data = self._tail_slice(start, stop, max_bytes)
        else:
            data = b""

        # Arbitrary caller-provided positions may land inside a character.
        prefix = 0
        while prefix < len(data) and data[prefix] & 0xC0 == 0x80:
            prefix += 1
        if prefix:
            start += prefix
            data = data[prefix:]
            gap = True
        text, consumed = _decode_prefix(data)
        if data and not consumed and not prefix:
            # A damaged archive may end in an incomplete record. Advance with a visible gap.
            consumed = len(data)
            gap = True
        if tool_budget:
            text = _fit_json(text)
            if text:
                consumed = len(text.encode("utf-8"))
        return {
            **self.metadata(),
            "offset_unit": "bytes",
            "output": text,
            "offset": start,
            "next_offset": start + consumed,
            "has_more": start + consumed < stop,
            "gap": gap,
        }

    def _tail_slice(self, start: int, stop: int, limit: int) -> bytes:
        index = start - self.tail_offset
        return self.tail.encode("utf-8")[index:index + min(limit, stop - start)]

    def lines(self, offset: int | None, *, limit: int, running: bool) -> dict[str, Any]:
        """Compatibility view with bounded work. Byte cursors cover older/larger pages."""
        tail_view = offset is None
        tail_end = self.end if self.tail else self.persisted
        head = max(0, tail_end - _LEGACY_SCAN_BYTES) if tail_view else 0
        if not tail_view and self.persisted < self.tail_offset and (offset or 0) > self.tail_line:
            # Read the newer available segment after an explicit gap.
            head = self.tail_offset
        window = self.page(head, max_bytes=_LEGACY_SCAN_BYTES, end_offset=tail_end if tail_view else None)
        if tail_view and window["has_more"] and self.tail:
            window = self.page(self.tail_offset, max_bytes=_LEGACY_SCAN_BYTES)
        output = window["output"]
        records = output.split("\n") if output else []
        if records and records[-1] == "":
            records.pop()
        if output and not output.endswith("\n") and (window["has_more"] or (running and not tail_view)):
            records.pop()

        start = window["offset"]
        base: int | None = 0 if start == 0 else None
        begins_partway = False
        if start == self.tail_offset and start:
            base = self.tail_line
            begins_partway = self.tail_partial_line
        elif start:
            # Do not present a partial first line as a complete paginatable record.
            begins_partway = self._preceding_byte(start) != b"\n"
        if begins_partway and records:
            records.pop(0)
            base = base + 1 if base is not None else None
        if (base is None and tail_view and self.total_lines is not None
                and tail_end == self.end and not window["has_more"]):
            base = max(0, self.total_lines - len(records))

        total_lines = self.total_lines
        if total_lines is None and start == 0 and window["next_offset"] == self.end and not running:
            total_lines = len(records)

        skipped = max(0, (offset or 0) - base) if base is not None else 0
        if tail_view:
            skipped = max(0, len(records) - limit)
        records = records[skipped:skipped + limit]
        raw_text = "\n".join(strip_ansi(line.removesuffix("\r")) for line in records)
        text = _fit_json(raw_text)
        clipped = len(text) < len(raw_text)
        shown_offset = base + skipped if base is not None else None
        next_offset = None if shown_offset is None or clipped else shown_offset + len(records)
        result = {
            **self.metadata(),
            "total_lines": total_lines,
            "offset_unit": "lines",
            "output": text,
            "offset": shown_offset,
            "next_offset": next_offset,
            "showing": f"{len(records)} lines",
            "has_more": bool(window["has_more"] or clipped or (
                total_lines is not None and next_offset is not None and next_offset < total_lines
            )),
            "gap": (window["gap"] or bool(tail_view and tail_end < self.end)
                    or bool(not tail_view and base is not None and (offset or 0) < base)),
        }
        if window["has_more"] or result["gap"] or clipped:
            result["hint"] = "该行窗口受日志保留范围或扫描预算限制。使用 cursor 和 max_bytes 按 UTF-8 字节续读。"
        return result

    def _preceding_byte(self, start: int) -> bytes:
        if start <= self.persisted and self.output_ref:
            try:
                with open_verified_read(Path(self.output_ref)) as (_, handle):
                    handle.seek(start - 1)
                    return handle.read(1)
            except (OSError, FileConflictError):
                pass
        if start > self.tail_offset:
            return self._tail_slice(start - 1, start, 1)
        return b""


def _fit_json(text: str) -> str:
    if len(json.dumps(text, ensure_ascii=False)) <= MAX_PAGE_JSON_CHARS:
        return text
    low, high = 0, len(text)
    while low < high:
        midpoint = (low + high + 1) // 2
        if len(json.dumps(text[:midpoint], ensure_ascii=False)) <= MAX_PAGE_JSON_CHARS:
            low = midpoint
        else:
            high = midpoint - 1
    return text[:low]
