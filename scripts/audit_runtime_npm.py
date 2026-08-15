"""Audit production dependencies for every npm lockfile in the repository."""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path


def package_lock_directories(repo_root: Path) -> list[Path]:
    """Return source directories for package locks bound to the Git checkout."""
    repo_root = repo_root.resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "--cached", "-z"],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"could not enumerate committed npm lockfiles: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"could not enumerate committed npm lockfiles: {detail or 'git ls-files failed'}"
        )
    locks = [
        repo_root / value.decode("utf-8", errors="surrogateescape")
        for value in result.stdout.split(b"\0")
        if value
        and Path(value.decode("utf-8", errors="surrogateescape")).name
        == "package-lock.json"
    ]
    missing = [path.relative_to(repo_root).as_posix() for path in locks if not path.is_file()]
    if missing:
        raise RuntimeError(
            f"committed npm lockfile is missing from checkout: {', '.join(missing)}"
        )
    return sorted(path.parent for path in locks)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()
    npm = shutil.which("npm")
    if npm is None:
        raise SystemExit("npm is unavailable")

    try:
        lock_directories = package_lock_directories(repo_root)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    failed: list[str] = []
    for directory in lock_directories:
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
