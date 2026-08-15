"""Safe ZIP extraction helpers for Office Open XML files."""

import os
import stat
import zipfile
from pathlib import Path


MAX_ARCHIVE_MEMBERS = 4096
MAX_ARCHIVE_DEPTH = 32
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_COMPRESSION_RATIO = 1000.0


def safe_extract_all(
    zip_file: zipfile.ZipFile,
    output_path: Path,
) -> None:
    """Extract all entries from *zip_file* into *output_path* safely.

    Raises:
        ValueError: If an archive entry uses an absolute path, contains
            parent-directory references, or would resolve outside of
            *output_path*.
    """
    output_resolved = output_path.resolve()

    infos = zip_file.infolist()
    if len(infos) > MAX_ARCHIVE_MEMBERS:
        raise ValueError("Archive entry count exceeds the limit")

    total_bytes = 0
    for info in infos:
        name = info.filename

        if os.path.isabs(name):
            raise ValueError(f"Absolute path in archive: {name}")
        if any(part == ".." for part in Path(name).parts):
            raise ValueError(f"Path traversal in archive: {name}")
        parts = tuple(part for part in Path(name).parts if part not in {"", "."})
        if len(parts) > MAX_ARCHIVE_DEPTH:
            raise ValueError("Archive path depth exceeds the limit")
        if info.flag_bits & 0x1:
            raise ValueError("Encrypted archive entries are not supported")
        unix_mode = info.external_attr >> 16
        if stat.S_IFMT(unix_mode) not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise ValueError("Archive contains a special file member")
        if info.file_size < 0 or info.compress_size < 0:
            raise ValueError("Archive member size is invalid")
        if info.file_size > MAX_MEMBER_BYTES:
            raise ValueError("Archive member exceeds the size limit")
        if info.file_size and info.compress_size > 0:
            if info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                raise ValueError("Archive compression ratio exceeds the limit")
        total_bytes += info.file_size
        if total_bytes > MAX_TOTAL_BYTES:
            raise ValueError("Archive expands beyond the size limit")

        target = (output_path / name).resolve()
        try:
            target.relative_to(output_resolved)
        except ValueError:
            raise ValueError(f"Archive entry escapes output directory: {name}")

        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        copied = 0
        with zip_file.open(info) as src, open(target, "wb") as dst:
            while True:
                chunk = src.read(64 * 1024)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > MAX_MEMBER_BYTES:
                    raise ValueError("Archive member exceeded its declared size")
                dst.write(chunk)
        if copied != info.file_size:
            raise ValueError("Archive member size mismatch")
