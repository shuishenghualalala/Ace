"""文件变更服务：编辑/写入类工具的内核载体。

匹配阶梯（LF 归一化域精确 → 模糊兜底，模糊只定位上报、写回保留原文未触碰行）
→ per-path FIFO 串行 → 版本 CAS → 原子写，全部收敛在这一层；工具 handler 只
负责参数解析、授权与结果呈现。写入安全语义（快照 + 冲突检测 + 原子写 +
BOM/CRLF 保真）由 crew.tools.file_utils 提供，这里只组合不另起炉灶。

观察策略（read-before-edit）挂在同一模块：file_read 的观察版本记入
session → {path → version} 映射，编辑/写入以观察版本为 CAS 基；未读即拒并附
补救文案。策略由 tools.file.require_read_before_edit 开关控制，关闭时全部
调用无条件放行。
"""

from __future__ import annotations

import asyncio
import difflib
import unicodedata
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence, TypeVar

from crew.core.errors import ToolError
from crew.core.runctx import current_session_id
from crew.tools.file_utils import (
    FileConflictError,
    FileVersion,
    _detect_line_ending,
    _normalize_line_endings,
    _strip_bom,
    atomic_replace_bytes,
    snapshot_file,
    stat_verified_file,
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
    "guard_file_edit",
    "plan_edits",
    "record_file_observation",
]

_T = TypeVar("_T")


class StaleFileError(ToolError):
    """文件在观察/读取之后被其他写者修改（版本 CAS 不符）。"""


class EditMatchError(ToolError):
    """编辑匹配失败：未找到、多处歧义或编辑间重叠。"""


class FileNotObservedError(ToolError):
    """read-before-edit 策略拒绝：目标文件在当前会话中未被读取过。"""


def _version_key(version: FileVersion) -> tuple[int, int, int, int]:
    return (version.device, version.inode, version.size, version.mtime_ns)


def _stat_key(info: Any) -> tuple[int, int, int, int]:
    return (int(info.st_dev), int(info.st_ino), int(info.st_size), int(info.st_mtime_ns))


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


def plan_edits(original: str, ops: Sequence[EditOp]) -> EditPlan:
    """匹配阶梯：先在 LF 归一化域精确匹配；精确失败且要求唯一的编辑模糊兜底。

    模糊域只做定位与上报，写回时未触碰的行保持原文逐字节不变。匹配是纯计算：
    任何一步失败抛 EditMatchError，调用方保证此时文件未被触碰，从而实现
    「先全部匹配、后统一写入」的事务语义。
    """
    base = _normalize_lf(original)
    for op in ops:
        if not op.old:
            raise EditMatchError("old 不能为空")
        if op.count < 0:
            raise EditMatchError("count 不能为负")

    # 探测：任一编辑在模糊域命中，则整批切换到模糊域统一替换（单域偏移才稳定）。
    fuzzy_base: str | None = None
    fuzzy_needed = False
    for op in ops:
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
        if not starts:
            needle = _normalize_for_fuzzy(old_lf)
            starts = _find_all(domain, needle)
            mode = "fuzzy"
        if not exact_in_base:
            # 原文里并不字面包含 old，只是在归一化后才能定位：如实上报模糊命中。
            mode = "fuzzy"
        if not starts:
            raise EditMatchError(
                _not_found_message(index, len(ops), tried_fuzzy=(op.count == 1))
            )
        if op.count == 1 and len(starts) > 1:
            raise EditMatchError(_ambiguous_message(domain, index, len(ops), starts))
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
                "请合并为一处编辑或改为互不重叠的片段，未修改文件"
            )


# ---------------------------------------------------------------------------
# per-path FIFO 串行锁
# ---------------------------------------------------------------------------


class _PathLockTable:
    """同一目标路径的操作按到达顺序串行执行（promise 链式尾任务）。

    每条事件循环一张表，跨循环互不干扰；表中只存吞掉结果的尾任务，
    真正的结果/异常由调用方 await 的业务任务承载。
    """

    def __init__(self) -> None:
        self._tails: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, dict[str, asyncio.Task[None]]
        ] = weakref.WeakKeyDictionary()

    def _table(self, loop: asyncio.AbstractEventLoop) -> dict[str, asyncio.Task[None]]:
        table = self._tails.get(loop)
        if table is None:
            table = {}
            self._tails[loop] = table
        return table

    async def run(self, key: str, op: Callable[[], Awaitable[_T]]) -> _T:
        loop = asyncio.get_running_loop()
        table = self._table(loop)
        prior = table.get(key)
        gate: asyncio.Future[None] = loop.create_future()
        if prior is None:
            gate.set_result(None)
        else:
            prior.add_done_callback(lambda _prior: gate.set_result(None))

        async def _run() -> _T:
            await gate
            return await op()

        run_task = asyncio.create_task(_run())

        async def _tail() -> None:
            try:
                await run_task
            except BaseException:
                pass

        tail_task = asyncio.create_task(_tail())
        table[key] = tail_task
        try:
            return await run_task
        finally:
            if table.get(key) is tail_task:
                del table[key]


# ---------------------------------------------------------------------------
# 观察策略（read-before-edit）
# ---------------------------------------------------------------------------

_observed: dict[str, dict[str, tuple[int, int, int, int]]] = {}


def _read_before_edit_enabled() -> bool:
    try:
        from crew.state.config import load_config

        cfg = load_config()
        return bool(cfg.raw_config.get("tools", {}).get("file", {}).get("require_read_before_edit", False))
    except Exception:
        return False


def _record_observation(path: Path, key: tuple[int, int, int, int]) -> None:
    session = current_session_id.get()
    if not session:
        return
    _observed.setdefault(session, {})[str(path)] = key


def record_file_observation(path: Path, version: FileVersion) -> None:
    """file_read 成功后记录观察版本；文件不存在时不记录（创建不受策略约束）。"""
    if not version.exists:
        return
    _record_observation(Path(path), _version_key(version))


def guard_file_edit(path: Path) -> tuple[int, int, int, int] | None:
    """编辑/写入前的策略闸门：返回观察到的版本键作为 CAS 基。

    策略关闭、无会话归属或文件未被观察过（策略开启时抛 FileNotObservedError）
    三种情况语义分明；未读即拒的文案必须带补救动作。
    """
    if not _read_before_edit_enabled():
        return None
    session = current_session_id.get()
    if not session:
        return None
    key = str(Path(path))
    observed = _observed.get(session, {}).get(key)
    if observed is None:
        raise FileNotObservedError(
            f'cannot modify "{path}": file has not been read — '
            "read the file, then retry（请先用 file_read 读取该文件，再重新调用本工具）"
        )
    return observed


def clear_file_observations(session: str | None = None) -> None:
    """丢弃观察记录（会话结束/测试隔离用）。"""
    if session is None:
        _observed.clear()
    else:
        _observed.pop(session, None)


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


@dataclass(frozen=True)
class WriteOutcome:
    path: Path
    bytes_written: int
    append: bool
    existed: bool


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
        max_bytes: int = MAX_READ_FILE_BYTES,
    ) -> EditOutcome:
        target = Path(path)

        async def _op() -> EditOutcome:
            return await asyncio.to_thread(self._edit_text_sync, target, list(ops), expected, max_bytes)

        return await self._locks.run(str(target), _op)

    async def write_text(
        self,
        path: Path,
        content: str,
        *,
        append: bool = False,
        expected: tuple[int, int, int, int] | None = None,
        max_bytes: int = MAX_READ_FILE_BYTES,
    ) -> WriteOutcome:
        target = Path(path)

        async def _op() -> WriteOutcome:
            return await asyncio.to_thread(self._write_text_sync, target, content, append, expected, max_bytes)

        return await self._locks.run(str(target), _op)

    def _edit_text_sync(
        self,
        path: Path,
        ops: list[EditOp],
        expected: tuple[int, int, int, int] | None,
        max_bytes: int,
    ) -> EditOutcome:
        try:
            version = snapshot_file(path, max_bytes=max_bytes)
        except ValueError as exc:
            raise ToolError(f"文件过大，无法整体读取做替换: {path}") from exc
        if not version.exists:
            raise ToolError(f"文件不存在: {path}")
        self._check_fresh(path, version, expected)

        text = version.data.decode("utf-8", errors="replace")
        text, had_bom = _strip_bom(text)
        original_ending = _detect_line_ending(text)

        plan = plan_edits(text, ops)
        updated = _normalize_line_endings(plan.after, original_ending) if original_ending else plan.after
        written = updated
        if had_bom and not written.startswith("﻿"):
            written = "﻿" + written
        self._atomic_write(path, written.encode("utf-8"), version)
        self._observe(path)
        return EditOutcome(
            path=path,
            before=text,
            after=updated,
            plan=plan,
            had_bom=had_bom,
            line_ending=original_ending,
        )

    def _write_text_sync(
        self,
        path: Path,
        content: str,
        append: bool,
        expected: tuple[int, int, int, int] | None,
        max_bytes: int,
    ) -> WriteOutcome:
        path.parent.mkdir(parents=True, exist_ok=True)
        version = snapshot_file(path, max_bytes=max_bytes)
        if version.exists:
            self._check_fresh(path, version, expected)

        original_ending: str | None = None
        had_bom = False
        if version.exists and not append:
            existing = version.data.decode("utf-8", errors="replace")
            existing, had_bom = _strip_bom(existing)
            original_ending = _detect_line_ending(existing)

        normalized = _normalize_line_endings(content, original_ending) if original_ending else content
        if had_bom and not normalized.startswith("﻿"):
            normalized = "﻿" + normalized
        encoded = normalized.encode("utf-8")
        if append and version.exists:
            encoded = version.data + encoded
        self._atomic_write(path, encoded, version)
        self._observe(path)
        return WriteOutcome(
            path=path,
            bytes_written=len(encoded),
            append=append,
            existed=version.exists,
        )

    @staticmethod
    def _check_fresh(path: Path, version: FileVersion, expected: tuple[int, int, int, int] | None) -> None:
        if expected is not None and _version_key(version) != expected:
            raise StaleFileError(
                f'cannot modify "{path}": file has changed since it was read — '
                "re-read the file, then retry（文件在读取后已被修改，请重新读取后重试）"
            )

    @staticmethod
    def _atomic_write(path: Path, data: bytes, version: FileVersion) -> None:
        try:
            atomic_replace_bytes(path, data, version)
        except FileConflictError as exc:
            raise StaleFileError(
                f'cannot modify "{path}": file changed during write — '
                "re-read the file, then retry（文件在写入前被其他进程修改，请重新读取后重试）"
            ) from exc

    @staticmethod
    def _observe(path: Path) -> None:
        try:
            info = stat_verified_file(path)
        except Exception:
            return
        _record_observation(Path(path), _stat_key(info))


file_mutation = FileMutationService()
