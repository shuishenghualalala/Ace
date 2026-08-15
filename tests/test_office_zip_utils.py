"""Bounded extraction for Office Open XML helper scripts."""

from __future__ import annotations

import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from crew.skills.docx.scripts.office.helpers import zip_utils


def test_safe_extract_all_rejects_compression_ratio_bomb(tmp_path: Path) -> None:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("zeros.bin", b"0" * (4 * 1024 * 1024))

    with zipfile.ZipFile(BytesIO(output.getvalue())) as archive:
        with pytest.raises(ValueError, match="compression ratio"):
            zip_utils.safe_extract_all(archive, tmp_path)

    assert not list(tmp_path.iterdir())


def test_safe_extract_all_rejects_excessive_depth(tmp_path: Path) -> None:
    output = BytesIO()
    deep = "/".join(["d"] * 33) + "/file.txt"
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(deep, "content")

    with zipfile.ZipFile(BytesIO(output.getvalue())) as archive:
        with pytest.raises(ValueError, match="depth"):
            zip_utils.safe_extract_all(archive, tmp_path)

    assert not list(tmp_path.iterdir())


def test_safe_extract_all_rejects_size_mismatch(tmp_path: Path) -> None:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo("file.txt")
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, b"ok")
        archive.filelist[-1].file_size = 10

    with zipfile.ZipFile(BytesIO(output.getvalue())) as archive:
        with pytest.raises(ValueError, match="size mismatch"):
            zip_utils.safe_extract_all(archive, tmp_path)
