"""Audit production dependencies for every npm lockfile in the repository."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

_SKIP_DIRECTORIES = {".git", ".venv", "dist", "node_modules", "target"}


def package_lock_directories(repo_root: Path) -> list[Path]:
    """Return every source directory containing a package-lock.json."""
    found: list[Path] = []
    for current, directories, files in os.walk(repo_root):
        directories[:] = sorted(
            name for name in directories if name not in _SKIP_DIRECTORIES
        )
        if "package-lock.json" in files:
            found.append(Path(current))
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
