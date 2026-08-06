"""H-4 regression: the managed Python grep fallback must not follow file symlinks.

When the host ``rg`` is disabled under a managed profile, grep falls back to a pure
Python ``os.walk``. A workspace file symlink that points outside the authorized
root used to be opened via ``full.open()``, leaking the link target's contents past
the file-read approval and the native sandbox. The walk now skips file symlinks
discovered during recursion (directory symlinks were already not followed).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from crew.tools import file_tools
from crew.tools.file_tools import _glob_via_python, _grep_via_python


def _can_make_symlink(link: Path, target: Path) -> bool:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        return False
    return link.is_symlink()


@pytest.mark.skipif(
    sys.platform == "win32" and not os.environ.get("ACE_RUN_WINDOWS_SYMLINK_TESTS"),
    reason="Windows symlink creation needs developer mode / admin; covered on Linux CI",
)
def test_grep_python_fallback_skips_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "real.txt").write_text("inner match here\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("SECRET_CANARY match\n", encoding="utf-8")
    link = workspace / "linked.txt"
    if not _can_make_symlink(link, outside):
        pytest.skip("symlink creation not supported on this host")

    result = _grep_via_python({"pattern": "match", "-n": True}, workspace, "content")
    assert "SECRET_CANARY" not in result, "grep followed a workspace symlink out of the authorized root (H-4)"
    assert "inner match" in result, "grep skipped a real workspace file"


def test_grep_python_fallback_rejects_leaf_swapped_after_link_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "target.txt"
    target.write_text("inner match\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("SECRET_CANARY match\n", encoding="utf-8")
    original_is_symlink = Path.is_symlink
    swapped = False

    def swap_after_check(path: Path) -> bool:
        nonlocal swapped
        if path == target and not swapped:
            path.unlink()
            try:
                path.symlink_to(outside)
            except OSError:
                pytest.skip("symlink creation unavailable")
            swapped = True
            return False
        return original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", swap_after_check)
    result = _grep_via_python({"pattern": "match", "-n": True}, workspace, "content")

    assert swapped
    assert "SECRET_CANARY" not in result


def test_python_file_search_skips_linked_directory_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "inside.txt").write_text("inner match\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET_CANARY match\n", encoding="utf-8")
    link = workspace / "escape"

    if sys.platform == "win32":
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            check=False,
        )
        if created.returncode != 0:
            pytest.skip("junction creation unavailable")
    else:
        link.symlink_to(outside, target_is_directory=True)

    try:
        grep_result = _grep_via_python({"pattern": "match", "-n": True}, workspace, "content")
        glob_result = _glob_via_python("*.txt", workspace)
    finally:
        if sys.platform == "win32":
            os.rmdir(link)
        else:
            link.unlink()

    assert "SECRET_CANARY" not in grep_result
    assert "escape/secret.txt" not in glob_result


def test_glob_rejects_directory_swapped_to_link_after_pruning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "ws"
    nested = workspace / "nested"
    nested.mkdir(parents=True)
    (nested / "inside.txt").write_text("inside\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret-name.txt").write_text("SECRET_CANARY\n", encoding="utf-8")
    parked = workspace / "nested-original"
    original_prune = file_tools._prune_linked_directories
    swapped = False

    def swap_after_prune(dirpath: str, dirnames: list[str]) -> None:
        nonlocal swapped
        original_prune(dirpath, dirnames)
        if Path(dirpath) != workspace or "nested" not in dirnames or swapped:
            return
        nested.rename(parked)
        if sys.platform == "win32":
            created = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(nested), str(outside)],
                capture_output=True,
                check=False,
            )
            if created.returncode != 0:
                parked.rename(nested)
                pytest.skip("junction creation unavailable")
        else:
            nested.symlink_to(outside, target_is_directory=True)
        swapped = True

    monkeypatch.setattr(file_tools, "_prune_linked_directories", swap_after_prune)
    try:
        result = _glob_via_python("*.txt", workspace)
    finally:
        if swapped:
            if sys.platform == "win32":
                os.rmdir(nested)
            else:
                nested.unlink()
            parked.rename(nested)

    assert swapped
    assert "nested/secret-name.txt" not in result
