"""Write commit-bound evidence for one tested and Desktop-staged security runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

if __package__:
    from .generate_security_sbom import generate_sbom
else:
    from generate_security_sbom import generate_sbom


_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_PLATFORM_TARGETS = {
    "linux": {
        "x86_64-unknown-linux-gnu": ("linux", "x64"),
        "aarch64-unknown-linux-gnu": ("linux", "arm64"),
    },
    "windows": {
        "x86_64-pc-windows-msvc": ("win32", "x64"),
        "aarch64-pc-windows-msvc": ("win32", "arm64"),
    },
    "macos": {
        "x86_64-apple-darwin": ("darwin", "x64"),
        "aarch64-apple-darwin": ("darwin", "arm64"),
    },
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalize_repository(value: str) -> str:
    normalized = value.strip().replace("\\", "/").rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    if "://" in normalized:
        normalized = urlparse(normalized).path.strip("/")
    elif normalized.startswith("git@") and ":" in normalized:
        normalized = normalized.split(":", 1)[1]
    parts = [part for part in normalized.split("/") if part]
    return "/".join(parts[-2:]) if len(parts) >= 2 else ""


def _repository_host(value: str) -> str:
    normalized = value.strip()
    if "://" in normalized:
        return (urlparse(normalized).hostname or "").lower()
    if "@" in normalized and ":" in normalized:
        return normalized.split("@", 1)[1].split(":", 1)[0].lower()
    return ""


def _git_value(repo_root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"git identity could not be verified: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or "git command failed"
        raise ValueError(f"git identity could not be verified: {detail}")
    return result.stdout.strip()


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


def _assert_clean_checkout(repo_root: Path) -> None:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"clean checkout could not be verified: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or "git status failed"
        raise ValueError(f"clean checkout could not be verified: {detail}")
    if result.stdout:
        raise ValueError("release evidence requires a clean checkout")


def _validate_identity(
    repo_root: Path,
    *,
    repository: str,
    commit: str,
    workflow_run: str,
) -> tuple[str, str]:
    head = _git_value(repo_root, "rev-parse", "--verify", "HEAD")
    if not _COMMIT_RE.fullmatch(commit) or commit != head:
        raise ValueError("evidence commit does not match the checkout HEAD")
    origin = _git_value(repo_root, "remote", "get-url", "origin")
    expected_repository = _normalize_repository(origin)
    expected_host = _repository_host(origin)
    supplied_repository = repository.strip()
    if (
        not expected_repository
        or not expected_host
        or supplied_repository != expected_repository
    ):
        raise ValueError("evidence repository does not match the checkout origin")
    parsed_run = urlparse(workflow_run)
    expected_prefix = f"/{expected_repository}/actions/runs/"
    if (
        parsed_run.scheme != "https"
        or (parsed_run.hostname or "").lower() != expected_host
        or not parsed_run.path.startswith(expected_prefix)
        or not parsed_run.path.rstrip("/").rsplit("/", 1)[-1].isdigit()
        or parsed_run.path.rstrip("/").count("/") != expected_prefix.count("/")
    ):
        raise ValueError("workflow run must identify a repository Actions run")
    return supplied_repository, head


def _validate_runner_environment(
    *,
    runner_environment: Mapping[str, str] | None,
    platform: str,
    target_triple: str,
    repository: str,
    commit: str,
    workflow_run: str,
) -> dict[str, str]:
    environment = os.environ if runner_environment is None else runner_environment
    expected_target = _PLATFORM_TARGETS.get(platform, {}).get(target_triple)
    if expected_target is None:
        raise ValueError("platform and target triple are inconsistent")
    manifest_platform, manifest_arch = expected_target
    expected_os = {
        "linux": "Linux",
        "win32": "Windows",
        "darwin": "macOS",
    }[manifest_platform]
    expected_arch = {"x64": "X64", "arm64": "ARM64"}[manifest_arch]
    expected_run = (
        f"{environment.get('GITHUB_SERVER_URL', '').rstrip('/')}/"
        f"{repository}/actions/runs/{environment.get('GITHUB_RUN_ID', '')}"
    )
    expected_workflow = f"security-{platform}"
    expected_ref_prefix = (
        f"{repository}/.github/workflows/{expected_workflow}.yml@"
    )
    mismatches: list[str] = []
    expected_values = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_SHA": commit,
        "GITHUB_REPOSITORY": repository,
        "GITHUB_WORKFLOW": expected_workflow,
        "GITHUB_JOB": "native-security",
        "RUNNER_OS": expected_os,
        "RUNNER_ARCH": expected_arch,
    }
    for name, expected in expected_values.items():
        if environment.get(name) != expected:
            mismatches.append(name)
    workflow_ref = environment.get("GITHUB_WORKFLOW_REF", "")
    if (
        not workflow_ref.startswith(expected_ref_prefix)
        or len(workflow_ref) == len(expected_ref_prefix)
        or ".." in workflow_ref
    ):
        mismatches.append("GITHUB_WORKFLOW_REF")
    run_id = environment.get("GITHUB_RUN_ID", "")
    run_attempt = environment.get("GITHUB_RUN_ATTEMPT", "")
    if not run_id.isdigit():
        mismatches.append("GITHUB_RUN_ID")
    if not run_attempt.isdigit() or int(run_attempt) < 1:
        mismatches.append("GITHUB_RUN_ATTEMPT")
    if workflow_run != expected_run:
        mismatches.append("GITHUB_SERVER_URL")
    if mismatches:
        raise ValueError(
            "GitHub Actions runner identity mismatch: "
            + ", ".join(dict.fromkeys(mismatches))
        )
    return {
        "workflow_name": expected_workflow,
        "workflow_ref": workflow_ref,
        "workflow_job": environment["GITHUB_JOB"],
        "workflow_run_attempt": run_attempt,
        "runner_os": environment["RUNNER_OS"],
        "runner_arch": environment["RUNNER_ARCH"],
    }


def _validate_manifest(
    *,
    manifest_path: Path,
    staged_runtime: Path,
    staged_hash: str,
    source_hash: str,
    platform: str,
    target_triple: str,
) -> None:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"runtime manifest is unavailable or invalid: {exc}") from exc
    expected_platform = _PLATFORM_TARGETS.get(platform, {}).get(target_triple)
    if expected_platform is None:
        raise ValueError("platform and target triple are inconsistent")
    if manifest.get("schema") != 2:
        raise ValueError("runtime manifest schema is not supported")
    if (
        manifest.get("platform"),
        manifest.get("arch"),
    ) != expected_platform:
        raise ValueError("runtime manifest platform does not match the evidence target")
    if manifest.get("source_hash") != source_hash:
        raise ValueError("runtime manifest source hash does not match the checkout")
    if (
        manifest.get("binary_name") != staged_runtime.name
        or manifest.get("binary_sha256") != staged_hash
    ):
        raise ValueError("runtime manifest does not bind the staged artifact")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("runtime manifest file records are missing")
    matching_files = [
        item
        for item in files
        if isinstance(item, dict) and item.get("name") == staged_runtime.name
    ]
    if len(matching_files) != 1:
        raise ValueError("runtime manifest does not list the staged artifact exactly once")
    runtime_record = matching_files[0]
    if (
        runtime_record.get("sha256") != staged_hash
        or runtime_record.get("size") != staged_runtime.stat().st_size
    ):
        raise ValueError("runtime manifest artifact metadata does not match staged bytes")


def _validate_sbom(
    repo_root: Path,
    sbom: Path | None,
    *,
    runtime_manifest: Path,
) -> tuple[str, str]:
    if sbom is None or not sbom.is_file():
        raise ValueError("release SBOM is required")
    if sbom.stat().st_size > 50 * 1024 * 1024:
        raise ValueError("release SBOM exceeds the size limit")
    try:
        document = json.loads(sbom.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"release SBOM is unavailable or invalid: {exc}") from exc
    expected = generate_sbom(repo_root, runtime_manifest=runtime_manifest)
    if document != expected:
        raise ValueError(
            "release SBOM does not match committed locks and staged runtime files"
        )
    return sbom.name, _sha256(sbom)


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
    runner_environment: Mapping[str, str] | None = None,
    sbom: Path | None = None,
) -> None:
    """Fail unless staging preserved the tested artifact, then write bounded metadata."""
    _assert_clean_checkout(repo_root)
    normalized_repository, verified_commit = _validate_identity(
        repo_root,
        repository=repository,
        commit=commit,
        workflow_run=workflow_run,
    )
    runner_identity = _validate_runner_environment(
        runner_environment=runner_environment,
        platform=platform,
        target_triple=target_triple,
        repository=normalized_repository,
        commit=verified_commit,
        workflow_run=workflow_run,
    )
    runtime_hash = _sha256(runtime)
    staged_hash = _sha256(staged_runtime)
    if runtime_hash != staged_hash:
        raise ValueError("Desktop staging changed the tested security runtime")
    crate = repo_root / "security-runtime"
    manifest = staged_runtime.parent / "runtime-manifest.json"
    source_hash = _source_hash(crate)
    cargo_lock_hash = _sha256(crate / "Cargo.lock")
    _validate_manifest(
        manifest_path=manifest,
        staged_runtime=staged_runtime,
        staged_hash=staged_hash,
        source_hash=source_hash,
        platform=platform,
        target_triple=target_triple,
    )
    sbom_filename, sbom_hash = _validate_sbom(
        repo_root,
        sbom,
        runtime_manifest=manifest,
    )
    evidence = {
        "schema": 2,
        "status": "passed",
        "real_runner": True,
        "platform": platform,
        "target_triple": target_triple,
        "repository": normalized_repository,
        "commit": verified_commit,
        "workflow_run": workflow_run,
        **runner_identity,
        "artifact_filename": runtime.name,
        "artifact_sha256": runtime_hash,
        "desktop_staged_artifact_sha256": staged_hash,
        "source_hash": source_hash,
        "cargo_lock_sha256": cargo_lock_hash,
        "runtime_manifest_filename": manifest.name,
        "runtime_manifest_sha256": _sha256(manifest),
        "sbom_filename": sbom_filename,
        "sbom_sha256": sbom_hash,
        "sbom_format": "CycloneDX-1.6",
        "dependency_lock_policy": "committed-and-frozen",
        "vulnerability_threshold": "HIGH,CRITICAL",
        "secret_scan_policy": "required",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
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
    parser.add_argument("--sbom", type=Path, required=True)
    args = parser.parse_args()
    write_evidence(**vars(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
