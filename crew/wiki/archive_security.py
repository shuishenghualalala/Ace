"""Bounded, link-safe ZIP validation and extraction for Wiki documents."""

from __future__ import annotations

import os
import re
import stat
import unicodedata
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath

from crew.tools.file_utils import (
    FileConflictError,
    FileVersion,
    _ensure_private_directory,
    _pinned_parent,
    atomic_replace_bytes,
    read_verified_bytes,
    snapshot_file,
)


class ArchiveSecurityError(ValueError):
    """Raised when an archive member or resource budget is unsafe."""


@dataclass(frozen=True)
class ZipExtractionLimits:
    max_archive_bytes: int = 20 * 1024 * 1024
    max_entries: int = 4096
    max_depth: int = 32
    max_member_bytes: int = 64 * 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024
    max_compression_ratio: float = 1000.0


DEFAULT_ZIP_LIMITS = ZipExtractionLimits()
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_WINDOWS_DEVICE_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def validate_zip_bytes(
    content: bytes,
    *,
    limits: ZipExtractionLimits = DEFAULT_ZIP_LIMITS,
) -> tuple[zipfile.ZipInfo, ...]:
    """Validate ZIP metadata without writing any archive member."""

    try:
        _validate_limits(limits)
        if len(content) > limits.max_archive_bytes:
            raise ArchiveSecurityError("归档文件超过压缩包大小上限")
        with zipfile.ZipFile(BytesIO(content), "r") as archive:
            return _validate_infos(archive.infolist(), limits)
    except ArchiveSecurityError:
        raise
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        raise ArchiveSecurityError("归档格式无效") from exc


def safe_extract_zip(
    archive_path: Path,
    destination: Path,
    *,
    limits: ZipExtractionLimits = DEFAULT_ZIP_LIMITS,
) -> list[Path]:
    """Read one ZIP through a verified handle, then extract it safely."""

    try:
        content = read_verified_bytes(archive_path, max_bytes=limits.max_archive_bytes)
    except ArchiveSecurityError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise ArchiveSecurityError("归档读取失败") from exc
    return safe_extract_zip_bytes(content, destination, limits=limits)


def safe_extract_zip_bytes(
    content: bytes,
    destination: Path,
    *,
    limits: ZipExtractionLimits = DEFAULT_ZIP_LIMITS,
) -> list[Path]:
    """Extract regular ZIP members with path, type, and resource bounds."""

    extracted: list[Path] = []
    try:
        _validate_limits(limits)
        if len(content) > limits.max_archive_bytes:
            raise ArchiveSecurityError("归档文件超过压缩包大小上限")
        destination = Path(os.path.abspath(destination.expanduser()))
        _ensure_secure_directory(destination)
        with zipfile.ZipFile(BytesIO(content), "r") as archive:
            infos = _validate_infos(archive.infolist(), limits)
            planned_files: list[tuple[zipfile.ZipInfo, Path, FileVersion]] = []
            for info in infos:
                parts = _member_parts(info.filename, limits.max_depth)
                target = destination.joinpath(*parts)
                if info.is_dir() or info.filename.replace("\\", "/").endswith("/"):
                    _ensure_secure_directory(target)
                    continue
                _ensure_secure_directory(target.parent)
                expected = snapshot_file(target)
                if expected.exists:
                    raise ArchiveSecurityError("归档目标路径已存在，拒绝覆盖")
                planned_files.append((info, target, expected))

            actual_total = 0
            prepared_files: list[tuple[Path, FileVersion, bytes]] = []
            for info, target, expected in planned_files:
                data = bytearray()
                with archive.open(info, "r") as source:
                    while True:
                        chunk = source.read(64 * 1024)
                        if not chunk:
                            break
                        data.extend(chunk)
                        actual_total += len(chunk)
                        if len(data) > limits.max_member_bytes:
                            raise ArchiveSecurityError("归档单文件解压大小超过上限")
                        if actual_total > limits.max_total_bytes:
                            raise ArchiveSecurityError("归档解压总大小超过上限")
                if len(data) != info.file_size:
                    raise ArchiveSecurityError("归档成员声明大小与实际内容不一致")
                prepared_files.append((target, expected, bytes(data)))

            # Read every member before publishing any one of them. A corrupt or
            # unexpectedly unreadable late member must not leave an earlier file
            # looking successfully extracted.
            for target, expected, data in prepared_files:
                atomic_replace_bytes(
                    target,
                    data,
                    expected,
                    max_bytes=limits.max_member_bytes,
                )
                extracted.append(target)
    except ArchiveSecurityError:
        raise
    except Exception as exc:  # noqa: BLE001 - sanitize host/archive implementation errors
        raise ArchiveSecurityError("归档安全解压失败") from exc
    return extracted


def _validate_infos(
    infos: list[zipfile.ZipInfo],
    limits: ZipExtractionLimits,
) -> tuple[zipfile.ZipInfo, ...]:
    if len(infos) > limits.max_entries:
        raise ArchiveSecurityError("归档条目数量超过上限")

    total = 0
    seen: set[str] = set()
    for info in infos:
        parts = _member_parts(info.filename, limits.max_depth)
        key = unicodedata.normalize("NFC", "/".join(parts)).casefold()
        if key in seen:
            raise ArchiveSecurityError("归档包含重复或大小写冲突路径")
        seen.add(key)
        _validate_member_type(info)
        if info.flag_bits & 0x1:
            raise ArchiveSecurityError("不支持加密归档成员")
        if info.file_size < 0 or info.compress_size < 0:
            raise ArchiveSecurityError("归档成员大小无效")
        if info.file_size > limits.max_member_bytes:
            raise ArchiveSecurityError("归档单文件解压大小超过上限")
        total += info.file_size
        if total > limits.max_total_bytes:
            raise ArchiveSecurityError("归档解压总大小超过上限")
        if info.file_size:
            if info.compress_size <= 0:
                raise ArchiveSecurityError("归档成员压缩大小无效")
            ratio = info.file_size / info.compress_size
            if ratio > limits.max_compression_ratio:
                raise ArchiveSecurityError("归档成员压缩比超过上限")
    return tuple(infos)


def _validate_limits(limits: ZipExtractionLimits) -> None:
    if (
        limits.max_archive_bytes < 0
        or limits.max_entries < 0
        or limits.max_depth < 0
        or limits.max_member_bytes < 0
        or limits.max_total_bytes < 0
        or limits.max_compression_ratio <= 0
    ):
        raise ArchiveSecurityError("归档安全限制无效")


def _member_parts(name: str, max_depth: int) -> tuple[str, ...]:
    if not name or "\x00" in name:
        raise ArchiveSecurityError("归档成员路径为空或包含 NUL")
    normalized = unicodedata.normalize("NFC", name.replace("\\", "/"))
    if (
        normalized.startswith("/")
        or normalized.startswith("//")
        or _DRIVE_PREFIX.match(normalized)
    ):
        raise ArchiveSecurityError("归档成员路径不能是绝对路径、盘符或 UNC")
    raw_parts = normalized.split("/")
    if any(part == ".." for part in raw_parts):
        raise ArchiveSecurityError("归档成员路径包含上级目录")
    parts = tuple(part for part in PurePosixPath(normalized).parts if part not in {"", "."})
    if not parts:
        raise ArchiveSecurityError("归档成员路径无效")
    if len(parts) > max_depth:
        raise ArchiveSecurityError("归档成员目录深度超过上限")
    for part in parts:
        if ":" in part:
            raise ArchiveSecurityError("归档成员路径包含盘符或数据流")
        device_stem = part.rstrip(" .").split(".", 1)[0].upper()
        if device_stem in _WINDOWS_DEVICE_NAMES:
            raise ArchiveSecurityError("归档成员路径使用设备名")
    return parts


def _validate_member_type(info: zipfile.ZipInfo) -> None:
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    kind = stat.S_IFMT(unix_mode)
    if kind == stat.S_IFLNK:
        raise ArchiveSecurityError("归档包含符号链接成员")
    if kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise ArchiveSecurityError("归档包含特殊文件成员")
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if info.external_attr & reparse_flag:
        raise ArchiveSecurityError("归档包含 reparse 成员")


def _ensure_secure_directory(directory: Path) -> None:
    try:
        _ensure_private_directory(directory)
    except (FileConflictError, OSError) as exc:
        raise ArchiveSecurityError("归档目标目录路径不安全") from exc
    try:
        info = directory.lstat()
    except OSError as exc:
        raise ArchiveSecurityError("无法验证归档目标目录") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & reparse_flag:
        raise ArchiveSecurityError("归档目标目录包含链接或 reparse point")
    if not stat.S_ISDIR(info.st_mode):
        raise ArchiveSecurityError("归档目标路径不是目录")
    try:
        with _pinned_parent(directory / ".ace-archive-probe"):
            pass
    except (FileConflictError, OSError) as exc:
        raise ArchiveSecurityError("归档目标目录路径不安全") from exc
