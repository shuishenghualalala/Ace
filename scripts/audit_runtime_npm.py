"""Audit production dependencies for every npm lockfile in the repository."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path


def package_lock_directories(repo_root: Path) -> list[Path]:
    """Return every source directory containing a committed package-lock.json.

    以 git 追踪范围为事实来源（审计对象 = 提交进仓库的 lockfile）：内嵌
    worktree、构建产物等未跟踪目录天然被排除——既避免对同一份 lockfile 的
    重复审计，也不需要随目录布局漂移维护 skip 列表。
    """
    result = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z"],
        check=True,
        capture_output=True,
        text=True,
    )
    found = {
        (repo_root / entry).parent
        for entry in result.stdout.split("\0")
        if os.path.basename(entry) == "package-lock.json"
    }
    return sorted(found)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()
    npm = shutil.which("npm")
    if npm is None:
        raise SystemExit("npm is unavailable")

    failed: list[str] = []
    for directory in package_lock_directories(repo_root):
        relative = directory.relative_to(repo_root).as_posix()
        print(f"auditing runtime npm dependencies: {relative}", flush=True)
        result = subprocess.run(
            [
                npm,
                "audit",
                "--package-lock-only",
                "--omit=dev",
                "--audit-level=moderate",
            ],
            cwd=directory,
            check=False,
        )
        if result.returncode != 0:
            failed.append(relative)
    if failed:
        print(f"runtime npm dependency audit failed: {', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
