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

from crew.core.errors import ToolError
from crew.tools import file_tools
from crew.tools.file_tools import _glob_via_python, _grep_via_python


@pytest.mark.asyncio
async def test_authorized_search_does_not_delegate_unpinned_tree_to_host_rg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model-facing search boundary must not hand an approved path to rg."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "safe.txt").write_text("safe\n", encoding="utf-8")

    async def authorize(*_args, **_kwargs):
        return workspace

    async def resolve_rg():
        return "rg"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("unpinned host rg received an authorized search tree")

    monkeypatch.setattr(file_tools, "authorize_file_tool", authorize)
    monkeypatch.setattr(file_tools, "_resolve_rg", resolve_rg)
    monkeypatch.setattr(file_tools, "_glob_via_rg", forbidden)

    result = await file_tools.handle_glob({"pattern": "*.txt", "path": str(workspace)})

    assert "safe.txt" in result


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


def test_python_search_rejects_file_count_budget_exhaustion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    for index in range(3):
        (workspace / f"{index}.txt").write_text("no match\n", encoding="utf-8")
    monkeypatch.setattr(file_tools, "_MAX_SEARCH_FILES", 2)

    with pytest.raises(ToolError, match="文件数量"):
        _grep_via_python({"pattern": "needle"}, workspace, "content")


def test_python_search_rejects_depth_budget_exhaustion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "ws"
    nested = workspace / "one" / "two"
    nested.mkdir(parents=True)
    (nested / "deep.txt").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(file_tools, "_MAX_SEARCH_DEPTH", 1)

    with pytest.raises(ToolError, match="目录深度"):
        _grep_via_python({"pattern": "needle"}, workspace, "content")


def test_python_grep_rejects_aggregate_read_budget_exhaustion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "large.txt").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(file_tools, "_MAX_SEARCH_TOTAL_BYTES", 3)

    with pytest.raises(ToolError, match="读取总量"):
        _grep_via_python({"pattern": "needle"}, workspace, "content")


def test_python_grep_bounds_context_output_during_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "large.txt").write_text("needle " + "x" * 200, encoding="utf-8")
    monkeypatch.setattr(file_tools, "_MAX_SEARCH_OUTPUT_CHARS", 32)

    result = json.loads(
        _grep_via_python(
            {"pattern": "needle", "-C": 100},
            workspace,
            "content",
        )
    )

    assert len(result["content"]) <= 32
    assert result["applied_limit"] == file_tools._DEFAULT_HEAD_LIMIT


def test_python_grep_bounds_file_list_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    workspace = tmp_path / "ws"
    workspace.mkdir()
    for name in ("first.txt", "second.txt"):
        (workspace / name).write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(file_tools, "_MAX_SEARCH_OUTPUT_CHARS", 10)

    result = json.loads(
        _grep_via_python(
            {"pattern": "needle"},
            workspace,
            "files_with_matches",
        )
    )

    assert len(result["files"]) == 1
    assert sum(len(item) for item in result["files"]) <= 10
    assert result["applied_limit"] == file_tools._DEFAULT_HEAD_LIMIT


def test_rg_searches_explicitly_disable_link_following(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def capture(args: list[str], _cwd: Path, timeout: int = 30) -> tuple[int, str, str]:
        del timeout
        commands.append(args)
        return 1, "", ""

    monkeypatch.setattr(file_tools, "_run_rg", capture)
    _ = file_tools._glob_via_rg("rg", "*.txt", tmp_path, 1)
    _ = file_tools._grep_via_rg(
        "rg",
        {"pattern": "needle"},
        tmp_path,
        "content",
    )

    assert len(commands) == 2
    assert all("--no-follow" in command for command in commands)


def test_rg_search_preflights_file_count_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(2):
        (tmp_path / f"{index}.txt").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(file_tools, "_MAX_SEARCH_FILES", 1)

    def forbidden_run(*_args, **_kwargs):
        raise AssertionError("rg launched before tree budget preflight")

    monkeypatch.setattr(file_tools, "_run_rg", forbidden_run)
    with pytest.raises(ToolError, match="文件数量"):
        file_tools._glob_via_rg("rg", "*.txt", tmp_path, 1)


def test_rg_runner_enforces_streaming_output_byte_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(file_tools, "_MAX_RG_STDOUT_BYTES", 32)

    with pytest.raises(ToolError, match="输出超过"):
        file_tools._run_rg(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'x' * 4096)",
            ],
            tmp_path,
        )


def test_rg_runner_does_not_inherit_ambient_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-secret")

    return_code, stdout, _stderr = file_tools._run_rg(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ.get('OPENAI_API_KEY', 'absent'))",
        ],
        tmp_path,
    )

    assert return_code == 0
    assert stdout.strip() == "absent"


def test_python_grep_has_absolute_result_cap_when_head_limit_is_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    workspace = tmp_path / "ws"
    workspace.mkdir()
    for index in range(4):
        (workspace / f"{index}.txt").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(file_tools, "_MAX_SEARCH_RESULTS", 2)

    result = json.loads(
        _grep_via_python(
            {"pattern": "needle", "head_limit": 0},
            workspace,
            "files_with_matches",
        )
    )

    assert result["num_files"] == 2
    assert result["applied_limit"] == 2


def test_python_glob_stops_at_bounded_result_collection(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    for index in range(5):
        (workspace / f"{index}.txt").write_text("x\n", encoding="utf-8")

    results = _glob_via_python("*.txt", workspace, max_results=2)

    assert len(results) == 3  # limit + one sentinel used to report truncation
