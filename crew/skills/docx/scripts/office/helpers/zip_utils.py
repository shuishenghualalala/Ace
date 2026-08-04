"""Safe ZIP extraction helpers for Office Open XML files."""

import os
import shutil
import zipfile
from pathlib import Path


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

    for info in zip_file.infolist():
        name = info.filename

        if os.path.isabs(name):
            raise ValueError(f"Absolute path in archive: {name}")
        if any(part == ".." for part in Path(name).parts):
            raise ValueError(f"Path traversal in archive: {name}")

        target = (output_path / name).resolve()
        try:
            target.relative_to(output_resolved)
        except ValueError:
            raise ValueError(f"Archive entry escapes output directory: {name}")

        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        with zip_file.open(info) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
