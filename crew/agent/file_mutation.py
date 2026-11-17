"""文件变更服务：编辑/写入类工具的内核载体。

匹配阶梯（LF 归一化域精确 → 模糊兜底，模糊只定位上报、写回保留原文未触碰行）
→ per-path FIFO 串行 → 版本 CAS → 原子写，全部收敛在这一层；工具 handler 只
负责参数解析、授权与结果呈现。写入安全语义（快照 + 冲突检测 + 原子写 +
BOM/CRLF 保真）由 crew.tools.file_utils 提供，这里只组合不另起炉灶。

文件观察按账号、会话和路径隔离。已有观察与显式版本始终参与比较；兼容配置只放宽
从未读取的文件。打开文件、检查、准备变更和提交在同一路径队列内执行。
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import os
import stat
import threading
import unicodedata
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence, TypeVar

from crew.core.errors import StructuredToolError
from crew.core.runctx import current_owner_account_id, current_session_id
from crew.tools.file_utils import (
    FileConflictError,
    FileVersion,
    _detect_line_ending,
    _normalize_line_endings,
    _strip_bom,
    atomic_replace_bytes,
    snapshot_file,
    MAX_READ_FILE_BYTES,
)

__all__ = [
    "EditMatch",
    "EditOp",
    "EditOutcome",
    "EditMatchError",
    "FileMutationService",
    "FileNotObservedError",
    "StaleFileError",
    "WriteOutcome",
    "clear_file_observations",
    "file_mutation",
    "plan_edits",
    "record_file_observation",
]

_T = TypeVar("_T")


class StaleFileError(StructuredToolError):
    """文件在观察/读取之后被其他写者修改（版本 CAS 不符）。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="stale_file", next_action="file_read")


class EditMatchError(StructuredToolError):
    """编辑匹配失败：未找到、多处歧义或编辑间重叠。"""

    def __init__(self, message: str, *, code: str = "patch_no_match", details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=code, next_action="file_read", details=details)


class FileNotObservedError(StructuredToolError):
    """read-before-edit 策略拒绝：目标文件在当前会话中未被读取过。"""

    def __init__(self, path: Path) -> None:
        super().__init__(
            f'cannot modify "{path}": file has not been read — 请先用 file_read 读取该文件，再重试。',
            code="file_not_read", next_action="file_read",
        )


_VersionKey = tuple[int, int, int, str]


def _version_key(version: FileVersion) -> _VersionKey:
    return version.device, version.inode, version.size, version.digest


def _version_token(version: FileVersion) -> str:
    return hashlib.sha256(repr(_version_key(version)).encode("ascii")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# 编辑计划：匹配域归一化 + 精确匹配 + 事务化应用（纯函数，便于单测）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EditOp:
    """一处 old → new 替换。count=1 要求唯一匹配；count=0 替换全部；count=N 替换前 N 处。"""

    old: str
    new: str
    count: int = 1


@dataclass(frozen=True)
class EditMatch:
    """一次命中的定位结果，随 toolResult 上报（命中方式与得分保持诚实可见）。"""

    edit_index: int
    start: int
    length: int
    mode: str  # "exact" | "fuzzy"
    score: float  # 1.0 = 精确命中；模糊命中为原文相似度
    line: int  # 1-based 起始行号


@dataclass(frozen=True)
class EditPlan:
    matches: tuple[EditMatch, ...]
    before: str  # LF 归一化域的原文
    after: str  # LF 归一化域的替换结果


def _normalize_lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


_FUZZY_TRANSLATION = str.maketrans(
    {
        **{ord(ch): "'" for ch in "‘’‚‛"},
        **{ord(ch): '"' for ch in "“”„‟"},
        **{ord(ch): "-" for ch in "‐‑‒–—―−"},
        **{ord(ch): " " for ch in "            　"},
    }
)


def _normalize_for_fuzzy(text: str) -> str:
    """模糊匹配域：NFKC + 行尾空白剥离 + 智能引号/Unicode 破折号/特殊空格归一化。"""
    normalized = unicodedata.normalize("NFKC", text)
    lines = [line.rstrip() for line in normalized.split("\n")]
    return "\n".join(lines).translate(_FUZZY_TRANSLATION)


def _split_lines_lf(text: str) -> list[str]:
    """只按 \n 切行并保留行尾（str.splitlines 会按 \v 等额外分隔符切，不用）。"""
    if not text:
        return []
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _line_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    offset = 0
    for line in _split_lines_lf(text):
        spans.append((offset, offset + len(line)))
        offset += len(line)
    return spans


def _line_range_for(spans: Sequence[tuple[int, int]], start: int, end: int) -> tuple[int, int]:
    start_line = -1
    for index, (line_start, line_end) in enumerate(spans):
        if line_start <= start < line_end:
            start_line = index
            break
    if start_line == -1:
        raise EditMatchError("匹配区域越出文件末尾")
    end_line = start_line
    while end_line < len(spans) and spans[end_line][1] < end:
        end_line += 1
    if end_line >= len(spans):
        raise EditMatchError("匹配区域越出文件末尾")
    return start_line, end_line + 1


def _fuzzy_score(base: str, domain: str, start: int, length: int, old_lf: str) -> float:
    """模糊命中得分：模型给出的 old 与文件原文对应行段的相似度（0..1，写入仍定位原文）。"""
    spans = _line_spans(domain)
    start_line, end_line = _line_range_for(spans, start, start + length)
    base_lines = _split_lines_lf(base)
    span_text = "".join(base_lines[start_line:end_line])
    return difflib.SequenceMatcher(None, old_lf, span_text).ratio()


def _apply_preserving_lines(
    original: str,
    base: str,
    replacements: Sequence[tuple[int, int, str]],
) -> str:
    """在模糊域上替换后，把未触碰的行按原文逐字节贴回（保留原行尾/空白/引号）。"""
    original_lines = _split_lines_lf(original)
    base_spans = _line_spans(base)
    if len(original_lines) != len(base_spans):
        raise EditMatchError("模糊匹配域与原文行数不一致，未修改文件")

    groups: list[dict[str, Any]] = []
    for start, length, new_text in sorted(replacements, key=lambda r: r[0]):
        start_line, end_line = _line_range_for(base_spans, start, start + length)
        if groups and start_line < groups[-1]["end"]:
            groups[-1]["end"] = max(groups[-1]["end"], end_line)
            groups[-1]["repls"].append((start, length, new_text))
        else:
            groups.append({"start": start_line, "end": end_line, "repls": [(start, length, new_text)]})

    chunks: list[str] = []
    cursor = 0
    for group in groups:
        chunks.extend(original_lines[cursor : group["start"]])
        group_start = base_spans[group["start"]][0]
        group_end = base_spans[group["end"] - 1][1]
        chunk = base[group_start:group_end]
        relative = [(s - group_start, length, t) for s, length, t in group["repls"]]
        chunks.append(_apply_replacements(chunk, relative))
        cursor = group["end"]
    chunks.extend(original_lines[cursor:])
    return "".join(chunks)


def _find_all(haystack: str, needle: str) -> list[int]:
    if not needle:
        return []
    starts: list[int] = []
    idx = haystack.find(needle)
    while idx != -1:
        starts.append(idx)
        idx = haystack.find(needle, idx + len(needle))
    return starts


def _line_number_at(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _match_lines(domain: str, starts: Sequence[int], limit: int = 10) -> list[int]:
    lines = [_line_number_at(domain, start) for start in starts[:limit]]
    if len(starts) > limit:
        lines.append(-1)  # 占位：调用方按 "..." 渲染
    return lines


def _apply_replacements(base: str, replacements: Sequence[tuple[int, int, str]]) -> str:
    """按偏移从后往前应用替换，保证前面的偏移不被前面插入/删除的文本扰动。"""
    result = base
    for start, length, new_text in sorted(replacements, key=lambda r: r[0], reverse=True):
        result = result[:start] + new_text + result[start + length:]
    return result


def _not_found_message(edit_index: int, total: int, tried_fuzzy: bool) -> str:
    where = "old" if total == 1 else f"edits[{edit_index}].old"
    hint = "（已尝试空白/引号归一化的模糊匹配，亦未命中）" if tried_fuzzy else ""
    return f"未找到 {where} 文本{hint}，请核对与文件内容完全一致（含缩进与换行），未修改文件"


def _ambiguous_message(
    domain: str,
    edit_index: int,
    total: int,
    starts: Sequence[int],
) -> str:
    where = "old" if total == 1 else f"edits[{edit_index}].old"
    lines = _match_lines(domain, starts)
    shown = ", ".join(str(n) for n in lines if n > 0)
    if lines and lines[-1] == -1:
        shown += ", ..."
    return (
        f"{where} 文本匹配到 {len(starts)} 处（行号: {shown}），"
        "请补充更长的上下文使其唯一，未修改文件"
    )


def plan_edits(original: str, ops: Sequence[EditOp], *, allow_fuzzy: bool = True) -> EditPlan:
    """匹配阶梯：先在 LF 归一化域精确匹配；精确失败且要求唯一的编辑模糊兜底。

    模糊域只做定位与上报，写回时未触碰的行保持原文逐字节不变。匹配是纯计算：
    任何一步失败抛 EditMatchError，调用方保证此时文件未被触碰，从而实现
    「先全部匹配、后统一写入」的事务语义。
    """
    base = _normalize_lf(original)
    for op in ops:
        if not op.old:
            raise EditMatchError("old 不能为空", code="invalid_arguments")
        if op.count < 0:
            raise EditMatchError("count 不能为负", code="invalid_arguments")

    # 探测：任一编辑在模糊域命中，则整批切换到模糊域统一替换（单域偏移才稳定）。
    fuzzy_base: str | None = None
    fuzzy_needed = False
    for op in ops if allow_fuzzy else ():
        if _find_all(base, _normalize_lf(op.old)) or op.count != 1:
            continue
        if fuzzy_base is None:
            fuzzy_base = _normalize_for_fuzzy(base)
        if _find_all(fuzzy_base, _normalize_for_fuzzy(_normalize_lf(op.old))):
            fuzzy_needed = True
    domain = fuzzy_base if (fuzzy_needed and fuzzy_base is not None) else base

    matches: list[EditMatch] = []
    replacements: list[tuple[int, int, str]] = []
    for index, op in enumerate(ops):
        old_lf = _normalize_lf(op.old)
        exact_in_base = bool(_find_all(base, old_lf))
        needle = old_lf
        starts = _find_all(domain, needle)
        mode = "exact"
        if not starts and fuzzy_needed:
            needle = _normalize_for_fuzzy(old_lf)
            starts = _find_all(domain, needle)
            mode = "fuzzy"
        if not exact_in_base:
            # 原文里并不字面包含 old，只是在归一化后才能定位：如实上报模糊命中。
            mode = "fuzzy"
        if not starts:
            raise EditMatchError(
                _not_found_message(index, len(ops), tried_fuzzy=allow_fuzzy and op.count == 1),
                details={"edit_index": index},
            )
        if op.count == 1 and len(starts) > 1:
            raise EditMatchError(
                _ambiguous_message(domain, index, len(ops), starts), code="patch_ambiguous",
                details={"edit_index": index, "matches": len(starts),
                         "lines": [_line_number_at(domain, start) for start in starts[:10]]},
            )
        chosen = starts if op.count == 0 else starts[: max(op.count, 1)]
        for start in chosen:
            score = 1.0 if mode == "exact" else _fuzzy_score(base, domain, start, len(needle), old_lf)
            matches.append(
                EditMatch(
                    edit_index=index,
                    start=start,
                    length=len(needle),
                    mode=mode,
                    score=round(score, 4),
                    line=_line_number_at(domain, start),
                )
            )
            replacements.append((start, len(needle), _normalize_lf(op.new)))

    matches.sort(key=lambda m: (m.start, m.edit_index))
    _check_overlap(matches)
    after = (
        _apply_preserving_lines(base, domain, replacements)
        if fuzzy_needed
        else _apply_replacements(domain, replacements)
    )
    return EditPlan(matches=tuple(matches), before=base, after=after)


def _check_overlap(matches: Sequence[EditMatch]) -> None:
    for prev, cur in zip(matches, matches[1:]):
        if prev.start + prev.length > cur.start:
            raise EditMatchError(
                f"edits[{prev.edit_index}] 与 edits[{cur.edit_index}] 的匹配区域重叠，"
                "请合并为一处编辑或改为互不重叠的片段，未修改文件",
                code="patch_overlap", details={"edit_indices": [prev.edit_index, cur.edit_index]},
            )


# ---------------------------------------------------------------------------
# per-path FIFO 串行锁
# ---------------------------------------------------------------------------


class _PathLockTable:
    """同一事件循环内的写入按目标路径排队，前一操作结束才放行后续操作。"""

    def __init__(self) -> None:
        self._tails: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, dict[str, asyncio.Future[None]]
        ] = weakref.WeakKeyDictionary()

    def _table(self, loop: asyncio.AbstractEventLoop) -> dict[str, asyncio.Future[None]]:
        table = self._tails.get(loop)
        if table is None:
            table = {}
            self._tails[loop] = table
        return table

    async def run(self, key: str, op: Callable[[], Awaitable[_T]]) -> _T:
        loop = asyncio.get_running_loop()
        table = self._table(loop)
        prior = table.get(key)
        finished: asyncio.Future[None] = loop.create_future()
        table[key] = finished
        abandoned = False

        async def _run() -> _T:
            try:
                if prior is not None:
                    await asyncio.shield(prior)
                if abandoned:
                    raise asyncio.CancelledError
                return await op()
            finally:
                finished.set_result(None)
                if table.get(key) is finished:
                    del table[key]

        run_task = asyncio.create_task(_run())
        try:
            # The worker owns the path until its disk operation finishes. A caller
            # cancelled while waiting abandons its operation without releasing the queue.
            return await asyncio.shield(run_task)
        except asyncio.CancelledError:
            abandoned = True
            run_task.add_done_callback(lambda task: None if task.cancelled() else task.exception())
            raise


# ---------------------------------------------------------------------------
# 观察策略（read-before-edit）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Observation:
    version: _VersionKey
    spans: tuple[tuple[int, int], ...]
    complete: bool
    editable: bool


@dataclass(frozen=True)
class _MutationGuard:
    observation: _Observation | None
    session_bound: bool
    expected_version: str | None


_observed: dict[tuple[str, str], dict[str, _Observation]] = {}
_observed_lock = threading.Lock()
_OBSERVED_SESSION_LIMIT = 500
_OBSERVED_FILE_LIMIT = 128
_OBSERVED_SPAN_LIMIT = 256


def _read_before_edit_enabled() -> bool:
    try:
        from crew.state.config import load_config

        cfg = load_config()
        return bool(cfg.raw_config.get("tools", {}).get("file", {}).get("require_read_before_edit", True))
    except Exception:
        return True


def _observation_scope() -> tuple[str, str] | None:
    session = current_session_id.get()
    return (current_owner_account_id.get(), session) if session else None


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _combine_spans(spans: Sequence[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged[:_OBSERVED_SPAN_LIMIT])


def _remember_observation(
    path: Path, version: FileVersion, *, spans: tuple[tuple[int, int], ...] = (),
    complete: bool, editable: bool = True, total_lines: int | None = None,
) -> None:
    scope = _observation_scope()
    if scope is None:
        return
    key = _path_key(path)
    revision = _version_key(version)
    with _observed_lock:
        entries = _observed.pop(scope, {})
        previous = entries.pop(key, None)
        if previous is not None and previous.version == revision:
            spans = _combine_spans((*previous.spans, *spans))
            complete = complete or previous.complete
        if total_lines is not None and spans == ((1, total_lines),):
            complete = True
        entries[key] = _Observation(revision, () if complete else spans, complete, editable)
        _observed[scope] = entries
        if len(entries) > _OBSERVED_FILE_LIMIT:
            entries.pop(next(iter(entries)))
        if len(_observed) > _OBSERVED_SESSION_LIMIT:
            _observed.pop(next(iter(_observed)))


def record_file_observation(
    path: Path, version: FileVersion, *, first_line: int, last_line: int, total_lines: int,
    clipped_lines: tuple[int, ...], editable: bool,
) -> str:
    """Record the revision and only the unabridged lines the tool exposed."""
    spans: list[tuple[int, int]] = []
    if first_line:
        start = first_line
        for clipped in clipped_lines:
            if start < clipped:
                spans.append((start, clipped - 1))
            start = clipped + 1
        if start <= last_line:
            spans.append((start, last_line))
    _remember_observation(
        path, version, spans=tuple(spans), complete=total_lines == 0,
        total_lines=total_lines, editable=editable,
    )
    return _version_token(version)


def _capture_guard(path: Path, expected_version: str | None, guard: bool) -> _MutationGuard | None:
    if not guard and expected_version is None:
        return None
    scope = _observation_scope()
    with _observed_lock:
        observed = _observed.get(scope, {}).get(_path_key(path)) if scope else None
    return _MutationGuard(observed, scope is not None, expected_version)


def clear_file_observations(session: str | None = None) -> None:
    """丢弃观察记录（会话结束/测试隔离用）。"""
    with _observed_lock:
        if session is None:
            _observed.clear()
        else:
            _observed.pop((current_owner_account_id.get(), session), None)


# ---------------------------------------------------------------------------
# 服务本体
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EditOutcome:
    path: Path
    before: str  # 原文（已剥 BOM、保留原行尾）
    after: str  # 新文本（保留原行尾、未加 BOM）
    plan: EditPlan
    had_bom: bool
    line_ending: str | None
    file_version: str


@dataclass(frozen=True)
class WriteOutcome:
    path: Path
    bytes_written: int
    append: bool
    existed: bool
    file_version: str


class FileMutationService:
    """编辑/写入的唯一入口：串行 → 快照 → CAS → 匹配 → 原子写。"""

    def __init__(self) -> None:
        self._locks = _PathLockTable()

    async def edit_text(
        self,
        path: Path,
        ops: Sequence[EditOp],
        *,
        expected: tuple[int, int, int, int] | None = None,
        expected_version: str | None = None,
        guard: bool = False,
        max_bytes: int = MAX_READ_FILE_BYTES,
    ) -> EditOutcome:
        target = Path(path)
        check = _capture_guard(target, expected_version, guard)

        async def _op() -> EditOutcome:
            return await asyncio.to_thread(self._edit_text_sync, target, list(ops), expected, check, max_bytes)

        return await self._locks.run(_path_key(target), _op)

    async def write_text(
        self,
        path: Path,
        content: str,
        *,
        append: bool = False,
        expected: tuple[int, int, int, int] | None = None,
        expected_version: str | None = None,
        guard: bool = False,
        max_bytes: int = MAX_READ_FILE_BYTES,
    ) -> WriteOutcome:
        target = Path(path)
        check = _capture_guard(target, expected_version, guard)

        async def _op() -> WriteOutcome:
            return await asyncio.to_thread(self._write_text_sync, target, content, append, expected, check, max_bytes)

        return await self._locks.run(_path_key(target), _op)

    def _edit_text_sync(
        self,
        path: Path,
        ops: list[EditOp],
        expected: tuple[int, int, int, int] | None,
        check: _MutationGuard | None,
        max_bytes: int,
    ) -> EditOutcome:
        version = self._snapshot(path, max_bytes)
        observation = self._check_fresh(path, version, expected, check)
        if not version.exists:
            raise StructuredToolError(f"文件不存在: {path}", code="file_not_found")

        text, had_bom = self._editable_text(version)
        original_ending = _detect_line_ending(text)

        partial = observation is not None and not observation.complete
        plan = plan_edits(text, ops, allow_fuzzy=not partial)
        if partial:
            self._verify_match_spans(plan, observation)
        updated = _normalize_line_endings(plan.after, original_ending) if original_ending else plan.after
        written = updated
        if had_bom and not written.startswith("﻿"):
            written = "﻿" + written
        committed = self._atomic_write(path, written.encode("utf-8"), version)
        _remember_observation(path, committed, complete=bool(observation and observation.complete))
        return EditOutcome(
            path=path,
            before=text,
            after=updated,
            plan=plan,
            had_bom=had_bom,
            line_ending=original_ending,
            file_version=_version_token(committed),
        )

    def _write_text_sync(
        self,
        path: Path,
        content: str,
        append: bool,
        expected: tuple[int, int, int, int] | None,
        check: _MutationGuard | None,
        max_bytes: int,
    ) -> WriteOutcome:
        version = self._snapshot(path, max_bytes)
        observation = self._check_fresh(path, version, expected, check)
        if not append and observation is not None and not observation.complete:
            raise StructuredToolError(
                "覆盖文件需要读取同一版本的全部内容。请继续 file_read 分页，或用 patch 修改已读的精确片段。",
                code="incomplete_read", next_action="file_read",
            )
        original_ending = None
        had_bom = False
        if version.exists:
            existing, had_bom = self._editable_text(version)
            original_ending = _detect_line_ending(existing)

        normalized = _normalize_line_endings(content, original_ending) if original_ending else content
        if had_bom and not append and not normalized.startswith("﻿"):
            normalized = "﻿" + normalized
        encoded = normalized.encode("utf-8")
        if append and version.exists:
            encoded = version.data + encoded
        committed = self._atomic_write(path, encoded, version)
        complete = not append or not version.exists or bool(observation and observation.complete)
        _remember_observation(path, committed, complete=complete)
        return WriteOutcome(
            path=path,
            bytes_written=len(encoded),
            append=append,
            existed=version.exists,
            file_version=_version_token(committed),
        )

    @staticmethod
    def _snapshot(path: Path, max_bytes: int) -> FileVersion:
        try:
            return snapshot_file(path, max_bytes=max_bytes)
        except ValueError as exc:
            raise StructuredToolError(f"文件过大，超过编辑大小上限: {path}", code="file_too_large") from exc
        except FileConflictError as exc:
            raise StaleFileError(f"文件在检查期间发生变化，请重新读取: {path}") from exc
        except OSError as exc:
            raise StructuredToolError(f"无法读取编辑目标: {exc}", code="file_unavailable") from exc

    @staticmethod
    def _check_fresh(
        path: Path, version: FileVersion, expected: tuple[int, int, int, int] | None,
        check: _MutationGuard | None,
    ) -> _Observation | None:
        stat_key = version.device, version.inode, version.size, version.mtime_ns
        observed = check.observation if check else None
        token = check.expected_version if check else None
        if (expected is not None and (not version.exists or stat_key != expected)
                or observed is not None and (not version.exists or observed.version != _version_key(version))
                or token is not None and (not version.exists or token != _version_token(version))):
            raise StaleFileError(f"文件在读取后已被修改或移除，请重新读取后重试: {path}")
        if (version.exists and observed is None and check is not None and check.session_bound
                and (token is not None or _read_before_edit_enabled())):
            raise FileNotObservedError(path)
        if observed is not None and not observed.editable:
            raise StructuredToolError("编辑需要 UTF-8 文本。请先确认文件编码。", code="file_encoding_not_editable")
        return observed

    @staticmethod
    def _verify_match_spans(plan: EditPlan, observation: _Observation) -> None:
        for match in plan.matches:
            end = match.line + plan.before.count("\n", match.start, match.start + match.length - 1)
            if not any(start <= match.line and end <= last for start, last in observation.spans):
                raise StructuredToolError(
                    f"补丁匹配到的第 {match.line}–{end} 行尚未完整读取，请先用 file_read 读取这些行。",
                    code="incomplete_read", next_action="file_read",
                    details={"start_line": match.line, "end_line": end},
                )

    @staticmethod
    def _editable_text(version: FileVersion) -> tuple[str, bool]:
        try:
            text = version.data.decode("utf-8")
            if "\0" not in text:
                return _strip_bom(text)
        except UnicodeDecodeError:
            pass
        raise StructuredToolError(
            "编辑需要 UTF-8 文本。请先确认文件编码。", code="file_encoding_not_editable",
        )

    @staticmethod
    def _atomic_write(path: Path, data: bytes, version: FileVersion) -> FileVersion:
        if version.exists and not version.mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
            raise StructuredToolError(f"文件为只读，未写入: {path}", code="file_not_writable")
        try:
            return atomic_replace_bytes(path, data, version)
        except FileConflictError as exc:
            raise StaleFileError(
                f"文件在写入前被其他进程修改，请重新读取后重试: {path}"
            ) from exc
        except OSError as exc:
            raise StructuredToolError(f"无法写入文件: {exc}", code="file_not_writable") from exc


file_mutation = FileMutationService()
