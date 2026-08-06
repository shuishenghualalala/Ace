"""Write commit-bound evidence for one tested and Desktop-staged security runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_hash(crate: Path) -> str:
    files = sorted(
        path
        for path in (
            *crate.glob("src/**/*"),
            *crate.glob("tests/**/*.rs"),
            crate / "Cargo.toml",
            crate / "Cargo.lock",
        )
        if path.is_file()
    )
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(crate).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def write_evidence(
    *,
    repo_root: Path,
    runtime: Path,
    staged_runtime: Path,
    output: Path,
    platform: str,
    target_triple: str,
    repository: str,
    commit: str,
    workflow_run: str,
) -> None:
    """Fail unless staging preserved the tested artifact, then write bounded metadata."""
    runtime_hash = _sha256(runtime)
    staged_hash = _sha256(staged_runtime)
    if runtime_hash != staged_hash:
        raise ValueError("Desktop staging changed the tested security runtime")
    crate = repo_root / "security-runtime"
    manifest = staged_runtime.parent / "runtime-manifest.json"
    evidence = {
        "status": "passed",
        "real_runner": True,
        "platform": platform,
        "target_triple": target_triple,
        "repository": repository,
        "commit": commit,
        "workflow_run": workflow_run,
        "artifact_filename": runtime.name,
        "artifact_sha256": runtime_hash,
        "desktop_staged_artifact_sha256": staged_hash,
        "source_hash": _source_hash(crate),
        "cargo_lock_sha256": _sha256(crate / "Cargo.lock"),
        "runtime_manifest_sha256": _sha256(manifest) if manifest.is_file() else "",
    }
    output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--staged-runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--target-triple", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--workflow-run", required=True)
    args = parser.parse_args()
    write_evidence(**vars(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
