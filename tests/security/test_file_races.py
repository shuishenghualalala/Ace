"""Best-effort TOCTOU and hard-link defenses for structured file writes."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from crew.tools import file_utils
from crew.tools.file_utils import (
    FileConflictError,
    atomic_replace_bytes,
    snapshot_file,
)


def test_atomic_replace_rejects_concurrent_content_change(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("before", encoding="utf-8")
    version = snapshot_file(target)
    target.write_text("other writer", encoding="utf-8")

    with pytest.raises(FileConflictError, match="修改或替换"):
        atomic_replace_bytes(target, b"agent write", version)

    assert target.read_text(encoding="utf-8") == "other writer"


def test_atomic_replace_rejects_target_replaced_by_symlink(tmp_path):
    target = tmp_path / "target.txt"
    other = tmp_path / "other.txt"
    target.write_text("before", encoding="utf-8")
    other.write_text("secret", encoding="utf-8")
    version = snapshot_file(target)
    target.unlink()
    try:
        target.symlink_to(other)
    except OSError:
        pytest.skip("symlink creation unavailable")

    with pytest.raises(FileConflictError, match="修改或替换"):
        atomic_replace_bytes(target, b"agent write", version)

    assert other.read_text(encoding="utf-8") == "secret"


def test_snapshot_rejects_symlink_swapped_in_after_authorization(tmp_path):
    authorized = tmp_path / "authorized.txt"
    outside = tmp_path / "outside.txt"
    authorized.write_text("authorized", encoding="utf-8")
    outside.write_text("secret", encoding="utf-8")
    authorized.unlink()
    try:
        authorized.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation unavailable")

    with pytest.raises(FileConflictError, match="符号链接|身份"):
        snapshot_file(authorized)

    assert outside.read_text(encoding="utf-8") == "secret"


def test_verified_read_enforces_byte_limit_on_the_checked_handle(tmp_path):
    target = tmp_path / "target.bin"
    target.write_bytes(b"1234")

    with pytest.raises(ValueError, match="读取上限"):
        file_utils.read_verified_bytes(target, max_bytes=3)

    assert file_utils.read_verified_bytes(target, max_bytes=4) == b"1234"


def test_snapshot_rejects_oversized_file_before_read(tmp_path):
    target = tmp_path / "target.bin"
    target.write_bytes(b"1234")

    with pytest.raises(ValueError, match="读取上限"):
        snapshot_file(target, max_bytes=3)

    assert snapshot_file(target, max_bytes=4).data == b"1234"


def test_structured_write_rejects_existing_hard_link(tmp_path):
    target = tmp_path / "target.txt"
    alias = tmp_path / "alias.txt"
    target.write_text("shared", encoding="utf-8")
    try:
        os.link(target, alias)
    except OSError:
        pytest.skip("hard links unavailable")

    with pytest.raises(FileConflictError, match="硬链接"):
        snapshot_file(target)


def test_atomic_replace_writes_new_file_in_same_directory(tmp_path):
    target = tmp_path / "target.txt"
    version = snapshot_file(target)
    atomic_replace_bytes(target, b"created", version)
    assert target.read_bytes() == b"created"


def test_atomic_replace_cannot_be_redirected_by_parent_directory_swap(tmp_path, monkeypatch):
    parent = tmp_path / "authorized"
    moved = tmp_path / "authorized-original"
    outside = tmp_path / "outside"
    parent.mkdir()
    outside.mkdir()
    target = parent / "target.txt"
    outside_target = outside / "target.txt"
    target.write_text("before", encoding="utf-8")
    outside_target.write_text("secret", encoding="utf-8")
    version = snapshot_file(target)
    real_replace = os.replace
    swapped = False

    def swap_parent_then_replace(source, destination, *args, **kwargs):
        nonlocal swapped
        try:
            parent.rename(moved)
            try:
                parent.symlink_to(outside, target_is_directory=True)
                swapped = True
            except OSError:
                created = subprocess.run(
                    ["cmd.exe", "/d", "/c", "mklink", "/J", str(parent), str(outside)],
                    capture_output=True,
                    check=False,
                )
                swapped = created.returncode == 0
                if not swapped:
                    moved.rename(parent)
            if swapped:
                os.link(moved / Path(source).name, outside / Path(source).name)
        except OSError:
            # Windows secure implementation pins each parent without FILE_SHARE_DELETE.
            pass
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(file_utils.os, "replace", swap_parent_then_replace)
    try:
        atomic_replace_bytes(target, b"agent write", version)
    finally:
        if swapped and parent.exists():
            os.rmdir(parent) if os.name == "nt" else parent.unlink()

    assert outside_target.read_text(encoding="utf-8") == "secret"
    actual_target = moved / "target.txt" if swapped else target
    assert actual_target.read_bytes() == b"agent write"
