"""Fail a release when external-service or supplier acceptance evidence is incomplete."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from scripts.generate_security_sbom import generate_sbom
from scripts.write_security_runtime_evidence import (
    _normalize_repository,
    _repository_host,
    _source_hash,
)


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "docs" / "release-gates-2026-07-14.json"
_VERSION_RE = re.compile(r"^\s*version:\s*([^\s#]+)", re.MULTILINE)
_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "request", "head"}
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PLATFORM_TARGETS = {
    "linux": {"x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu"},
    "windows": {"x86_64-pc-windows-msvc", "aarch64-pc-windows-msvc"},
    "macos": {"x86_64-apple-darwin", "aarch64-apple-darwin"},
}
_PLATFORMS = tuple(_PLATFORM_TARGETS)
_TRUSTED_PACKAGE_SIGNATURE_KINDS = {
    "apple-codesign",
    "authenticode",
    "gpg-detached",
    "sigstore",
}
_PLATFORM_PACKAGE_SIGNATURE_KINDS = {
    "linux": {"gpg-detached", "sigstore"},
    "windows": {"authenticode"},
    "macos": {"apple-codesign"},
}
_PLATFORM_PACKAGE_SUFFIXES = {
    "linux": (".deb", ".rpm", ".appimage", ".tar.gz", ".tar.xz"),
    "windows": (".exe", ".msi", ".msix"),
    "macos": (".dmg", ".pkg"),
}
_PACKAGE_SIGNING_POLICY = Path("deploy/security/package-signing-policy.json")
_DEFAULT_POLICY: dict[str, Any] = {
    "gates": [
        {
            "id": "shared-deployment-https",
            "status": "blocked",
        },
        {
            "id": "cm-cloud-manage-timeouts",
            "status": "blocked",
            "blocked_scopes": ["cm-cloud-manage"],
        },
        {
            "id": "vlm-credential-contract",
            "status": "blocked",
            "blocked_scopes": ["image-understanding", "video-understanding"],
        },
        {
            "id": "native-security-matrix",
            "status": "blocked",
            "blocked_scopes": ["desktop-release"],
        },
    ]
}
AttestationVerifier = Callable[[Path, Path, str, str, str], tuple[bool, str]]


def _is_release_sensitive_ignored(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    first = normalized.split("/", 1)[0]
    if normalized.startswith(
        (
            ".github/workflows/",
            "crew/",
            "desktop/src/",
            "scripts/",
            "security-runtime/src/",
            "security-runtime/tests/",
            "security-runtime/target/",
            "security-runtime/bin/",
            "security-runtime/prebuilt/",
            "desktop/security-runtime-bin/",
            "tests/",
            "web/src/",
        )
    ):
        return True
    if first == "crew-tui":
        return True
    if first.startswith(
        (
            ".ace-test-temp",
            "mobileworkacetmp_pytest",
            "pytest-",
            "pytest_",
        )
    ):
        return True
    name = normalized.rstrip("/").rsplit("/", 1)[-1]
    return (
        name.startswith("security-")
        and name.endswith("-evidence.json")
        or name.startswith("package-evidence")
        or name.startswith("package-signing-policy")
        or name in {"cargo.lock", "cargo.toml", "package-lock.json"}
        or Path(name).suffix
        in {
            ".c",
            ".cjs",
            ".cpp",
            ".cs",
            ".go",
            ".h",
            ".hpp",
            ".java",
            ".js",
            ".jsx",
            ".kt",
            ".mjs",
            ".py",
            ".pyi",
            ".rs",
            ".swift",
            ".ts",
            ".tsx",
        }
    )


def inspect_release_checkout(root: Path) -> dict[str, Any]:
    """Return every tracked, untracked, or release-sensitive ignored change."""
    try:
        status = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--ignored=matching",
            ],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "clean": False,
            "tracked_changes": [],
            "untracked_files": [],
            "ignored_security_artifacts": [],
            "error": f"git status unavailable: {exc}",
        }

    if status.returncode != 0:
        error = status.stderr.decode("utf-8", errors="replace").strip()
        return {
            "clean": False,
            "tracked_changes": [],
            "untracked_files": [],
            "ignored_security_artifacts": [],
            "error": error or "git status failed",
        }

    tracked: list[str] = []
    untracked: list[str] = []
    ignored_security_artifacts: list[str] = []
    records = status.stdout.split(b"\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        code = record[:2].decode("ascii", errors="replace")
        path = record[3:].decode("utf-8", errors="surrogateescape")
        if code == "??":
            untracked.append(path)
        elif code == "!!":
            if _is_release_sensitive_ignored(path):
                ignored_security_artifacts.append(path)
        else:
            tracked.append(path)
        if "R" in code or "C" in code:
            index += 1

    return {
        "clean": not tracked and not untracked and not ignored_security_artifacts,
        "tracked_changes": sorted(tracked),
        "untracked_files": sorted(untracked),
        "ignored_security_artifacts": sorted(ignored_security_artifacts),
        "error": "",
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_evidence(path: Path | None) -> tuple[dict[str, Any], list[str]]:
    if path is None:
        return {}, ["evidence file was not configured"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"evidence file is unavailable or invalid: {exc}"]
    if not isinstance(payload, dict):
        return {}, ["evidence root must be a JSON object"]
    return payload, []


def _workflow_run_matches(
    value: object,
    repository: str,
    repository_host: str,
) -> bool:
    parsed = urlparse(value if isinstance(value, str) else "")
    expected = f"/{repository}/actions/runs/"
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").lower() == repository_host
        and parsed.path.startswith(expected)
        and parsed.path.rstrip("/").rsplit("/", 1)[-1].isdigit()
        and parsed.path.rstrip("/").count("/") == expected.count("/")
    )


def _verify_github_attestation(
    subject: Path,
    bundle: Path,
    repository: str,
    signer_workflow: str,
    source_digest: str,
) -> tuple[bool, str]:
    gh = shutil.which("gh")
    if gh is None:
        return False, "GitHub CLI is unavailable for artifact attestation verification"
    try:
        result = subprocess.run(
            [
                gh,
                "attestation",
                "verify",
                str(subject),
                "--bundle",
                str(bundle),
                "--repo",
                repository,
                "--signer-workflow",
                signer_workflow,
                "--source-digest",
                source_digest,
                "--deny-self-hosted-runners",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"artifact attestation verification failed to run: {exc}"
    detail = (result.stdout if result.returncode == 0 else result.stderr).strip()
    return result.returncode == 0, detail[:500]


def _validate_native_evidence(
    *,
    platform: str,
    payload: dict[str, Any],
    evidence_path: Path | None,
    repo_root: Path,
    expected_commit: str,
    expected_repository: str,
    expected_repository_host: str,
    expected_source_hash: str,
    expected_cargo_lock_hash: str,
) -> list[str]:
    errors: list[str] = []
    if payload.get("schema") != 2:
        errors.append("unsupported evidence schema")
    if payload.get("status") != "passed" or payload.get("real_runner") is not True:
        errors.append("real runner did not report a passing result")
    if payload.get("platform") != platform:
        errors.append(f"evidence platform is not {platform}")
    target = payload.get("target_triple")
    if target not in _PLATFORM_TARGETS[platform]:
        errors.append("target triple is inconsistent with the evidence platform")
    if payload.get("commit") != expected_commit:
        errors.append("commit does not match checkout HEAD")
    if str(payload.get("repository", "")).strip() != expected_repository:
        errors.append("repository does not match checkout origin")
    if not _workflow_run_matches(
        payload.get("workflow_run"),
        expected_repository,
        expected_repository_host,
    ):
        errors.append("workflow run does not identify this repository")
    expected_workflow = f"security-{platform}"
    expected_workflow_ref = (
        f"{expected_repository}/.github/workflows/{expected_workflow}.yml@"
    )
    expected_runner_os = {
        "linux": "Linux",
        "windows": "Windows",
        "macos": "macOS",
    }[platform]
    expected_runner_arch = (
        "ARM64" if isinstance(target, str) and target.startswith("aarch64-") else "X64"
    )
    run_attempt = str(payload.get("workflow_run_attempt", ""))
    workflow_ref = str(payload.get("workflow_ref", ""))
    runner_identity_valid = (
        payload.get("workflow_name") == expected_workflow
        and workflow_ref.startswith(expected_workflow_ref)
        and len(workflow_ref) > len(expected_workflow_ref)
        and ".." not in workflow_ref
        and payload.get("workflow_job") == "native-security"
        and run_attempt.isdigit()
        and int(run_attempt) >= 1
        and payload.get("runner_os") == expected_runner_os
        and payload.get("runner_arch") == expected_runner_arch
    )
    if not runner_identity_valid:
        errors.append("runner identity is incomplete")
    if payload.get("source_hash") != expected_source_hash:
        errors.append("source hash does not match the checkout")
    if payload.get("cargo_lock_sha256") != expected_cargo_lock_hash:
        errors.append("Cargo.lock digest does not match the checkout")
    manifest_filename = str(payload.get("runtime_manifest_filename", ""))
    manifest_path: Path | None = None
    if manifest_filename != "runtime-manifest.json":
        errors.append("runtime manifest filename is not bound")
    elif evidence_path is None:
        errors.append("runtime manifest cannot be located without native evidence")
    else:
        manifest_path = evidence_path.parent / manifest_filename
    for field in (
        "artifact_sha256",
        "desktop_staged_artifact_sha256",
        "runtime_manifest_sha256",
    ):
        if not _SHA256_RE.fullmatch(str(payload.get(field, ""))):
            errors.append(f"{field} is not a SHA-256 digest")
    if payload.get("artifact_sha256") != payload.get("desktop_staged_artifact_sha256"):
        errors.append("tested and Desktop-staged artifact digests differ")
    filename = str(payload.get("artifact_filename", ""))
    if not filename or Path(filename).name != filename:
        errors.append("artifact filename is missing or unsafe")
    elif platform == "windows" and not filename.endswith(".exe"):
        errors.append("Windows evidence does not name a Windows runtime")
    elif platform != "windows" and filename.endswith(".exe"):
        errors.append("non-Windows evidence names a Windows runtime")
    artifact_path: Path | None = None
    if evidence_path is not None and filename and Path(filename).name == filename:
        artifact_path = evidence_path.parent / filename
    if artifact_path is not None:
        if artifact_path.is_symlink() or not artifact_path.is_file():
            errors.append("attested runtime artifact is missing")
        elif _sha256(artifact_path) != payload.get("artifact_sha256"):
            errors.append("attested runtime artifact digest does not match evidence")
    manifest_payload: dict[str, Any] = {}
    if manifest_path is not None:
        if manifest_path.is_symlink() or not manifest_path.is_file():
            errors.append("attested runtime manifest is missing")
        elif _sha256(manifest_path) != payload.get("runtime_manifest_sha256"):
            errors.append("runtime manifest digest does not match evidence")
        elif manifest_path.stat().st_size > 1024 * 1024:
            errors.append("runtime manifest exceeds the size limit")
        else:
            try:
                loaded_manifest = json.loads(
                    manifest_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                errors.append("runtime manifest is invalid")
            else:
                if isinstance(loaded_manifest, dict):
                    manifest_payload = loaded_manifest
                else:
                    errors.append("runtime manifest root is invalid")
    if manifest_payload:
        expected_manifest_platform, expected_manifest_arch = {
            "linux": ("linux", "x64" if str(target).startswith("x86_64-") else "arm64"),
            "windows": (
                "win32",
                "x64" if str(target).startswith("x86_64-") else "arm64",
            ),
            "macos": (
                "darwin",
                "x64" if str(target).startswith("x86_64-") else "arm64",
            ),
        }[platform]
        if (
            manifest_payload.get("schema") != 2
            or manifest_payload.get("platform") != expected_manifest_platform
            or manifest_payload.get("arch") != expected_manifest_arch
            or manifest_payload.get("source_hash") != expected_source_hash
            or manifest_payload.get("binary_name") != filename
            or manifest_payload.get("binary_sha256") != payload.get("artifact_sha256")
        ):
            errors.append("runtime manifest identity does not match native evidence")
        manifest_files = manifest_payload.get("files")
        valid_files: dict[str, dict[str, Any]] = {}
        if not isinstance(manifest_files, list):
            errors.append("runtime manifest file records are missing")
        else:
            for item in manifest_files:
                if not isinstance(item, dict):
                    errors.append("runtime manifest contains invalid file records")
                    break
                name = item.get("name")
                digest = item.get("sha256")
                size = item.get("size")
                if (
                    not isinstance(name, str)
                    or not name
                    or Path(name).name != name
                    or name in valid_files
                    or not _SHA256_RE.fullmatch(str(digest or ""))
                    or not isinstance(size, int)
                    or isinstance(size, bool)
                    or size < 0
                ):
                    errors.append("runtime manifest contains unsafe file metadata")
                    break
                valid_files[name] = item
        binary_record = valid_files.get(filename)
        if (
            not binary_record
            or binary_record.get("sha256") != payload.get("artifact_sha256")
        ):
            errors.append("runtime manifest does not list the tested artifact")
        if platform == "linux":
            provenance = manifest_payload.get("bwrap_provenance")
            if (
                "bwrap" not in valid_files
                or "BWRAP-LICENSE" not in valid_files
                or not isinstance(provenance, dict)
                or any(
                    not isinstance(provenance.get(field), str)
                    or not str(provenance.get(field)).strip()
                    or provenance.get(field) == "unrecorded"
                    for field in ("source", "version", "license_file")
                )
                or provenance.get("license_file") != "BWRAP-LICENSE"
            ):
                errors.append("Linux bundled bwrap provenance is incomplete")
    if (
        payload.get("dependency_lock_policy") != "committed-and-frozen"
        or payload.get("vulnerability_threshold") != "HIGH,CRITICAL"
        or payload.get("secret_scan_policy") != "required"
    ):
        errors.append("supply-chain enforcement policy is incomplete")
    sbom_filename = str(payload.get("sbom_filename", ""))
    sbom_path: Path | None = None
    if (
        not sbom_filename
        or Path(sbom_filename).name != sbom_filename
        or not sbom_filename.endswith(".cdx.json")
    ):
        errors.append("SBOM filename is missing or unsafe")
    elif evidence_path is None:
        errors.append("SBOM cannot be located without native evidence")
    else:
        sbom_path = evidence_path.parent / sbom_filename
    if not _SHA256_RE.fullmatch(str(payload.get("sbom_sha256", ""))):
        errors.append("sbom_sha256 is not a SHA-256 digest")
    if payload.get("sbom_format") != "CycloneDX-1.6":
        errors.append("SBOM format is not CycloneDX 1.6")
    if sbom_path is not None:
        if not sbom_path.is_file():
            errors.append("native release SBOM is missing")
        elif _sha256(sbom_path) != payload.get("sbom_sha256"):
            errors.append("native release SBOM digest does not match evidence")
        else:
            try:
                sbom_payload = json.loads(sbom_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                errors.append("native release SBOM is invalid")
            else:
                try:
                    expected_sbom = generate_sbom(
                        repo_root,
                        runtime_manifest=manifest_path,
                    )
                except (OSError, ValueError, RuntimeError) as exc:
                    errors.append(f"native release SBOM identity is invalid: {exc}")
                else:
                    if sbom_payload != expected_sbom:
                        errors.append(
                            "native release SBOM does not match committed locks "
                            "and attested runtime files"
                        )
    return errors


def _load_package_signing_policy(
    root: Path,
) -> tuple[dict[str, Any], list[str]]:
    path = root / _PACKAGE_SIGNING_POLICY
    if path.is_symlink() or not path.is_file():
        return {}, ["trusted package signing policy is unavailable"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, ["trusted package signing policy is unavailable"]
    errors: list[str] = []
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != 1
        or set(payload)
        != {
            "schema",
            "attestation_signer_workflow",
            "update_public_key_sha256",
            "update_base_url",
            "platforms",
        }
    ):
        return {}, ["trusted package signing policy is invalid"]

    workflow = payload.get("attestation_signer_workflow")
    workflow_path = root / workflow if isinstance(workflow, str) else root
    if (
        not isinstance(workflow, str)
        or not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", workflow)
        or workflow_path.is_symlink()
        or not workflow_path.is_file()
    ):
        errors.append("trusted package attestation workflow is unavailable")
    key_digest = str(payload.get("update_public_key_sha256", ""))
    if not _SHA256_RE.fullmatch(key_digest) or key_digest == "0" * 64:
        errors.append("trusted update public key digest is invalid")
    update_base_url = str(payload.get("update_base_url", ""))
    try:
        parsed_update_base = urlparse(update_base_url)
        update_host = parsed_update_base.hostname
    except ValueError:
        parsed_update_base = urlparse("")
        update_host = None
    if (
        parsed_update_base.scheme != "https"
        or not update_host
        or parsed_update_base.username
        or parsed_update_base.password
        or parsed_update_base.query
        or parsed_update_base.fragment
        or not parsed_update_base.path.endswith("/")
    ):
        errors.append("trusted update base URL is invalid")

    platforms = payload.get("platforms")
    if not isinstance(platforms, dict) or set(platforms) != set(_PLATFORMS):
        errors.append("package signing policy does not cover all release platforms")
        platforms = {}
    for platform in _PLATFORMS:
        entry = platforms.get(platform)
        if not isinstance(entry, dict) or set(entry) != {
            "kinds",
            "identities",
            "issuers",
        }:
            errors.append(f"trusted package signer policy is invalid for {platform}")
            continue
        kinds = entry.get("kinds")
        identities = entry.get("identities")
        issuers = entry.get("issuers")
        if (
            not isinstance(kinds, list)
            or not kinds
            or any(
                not isinstance(kind, str)
                or kind not in _PLATFORM_PACKAGE_SIGNATURE_KINDS[platform]
                for kind in kinds
            )
            or len(kinds) != len(set(kinds))
        ):
            errors.append(f"trusted package signature kinds are invalid for {platform}")
        for field, values in (("identities", identities), ("issuers", issuers)):
            if (
                not isinstance(values, list)
                or not values
                or any(
                    not isinstance(value, str)
                    or not value.strip()
                    or len(value) > 500
                    for value in values
                )
                or len(values) != len(set(values))
            ):
                errors.append(
                    f"trusted package signature {field} are invalid for {platform}"
                )
    return payload, errors


def _validate_package_evidence(
    *,
    payload: dict[str, Any],
    package_evidence_path: Path,
    native_payloads: Mapping[str, dict[str, Any]],
    native_file_hashes: Mapping[str, str],
    expected_commit: str,
    expected_repository: str,
    expected_repository_host: str,
    expected_source_hash: str,
    expected_cargo_lock_hash: str,
    signing_policy: Mapping[str, Any],
) -> list[str]:
    errors: list[str] = []
    if payload.get("schema") != 2:
        errors.append("unsupported package evidence schema")
    if payload.get("status") != "passed":
        errors.append("package evidence did not report a passing result")
    if payload.get("commit") != expected_commit:
        errors.append("package evidence commit does not match checkout HEAD")
    if str(payload.get("repository", "")).strip() != expected_repository:
        errors.append("package evidence repository does not match checkout origin")
    if payload.get("source_hash") != expected_source_hash:
        errors.append("package source hash does not match the checkout")
    if (
        payload.get("cargo_lock_sha256") != expected_cargo_lock_hash
        or payload.get("cargo_lock_verified") is not True
    ):
        errors.append("package evidence does not verify this Cargo.lock")
    walkthroughs = payload.get("desktop_walkthroughs")
    if (
        not isinstance(walkthroughs, dict)
        or set(walkthroughs) != set(_PLATFORMS)
        or any(walkthroughs.get(platform) is not True for platform in _PLATFORMS)
    ):
        errors.append("per-platform Desktop package walkthrough evidence is missing")
    update_trust_root = payload.get("update_trust_root")
    if not isinstance(update_trust_root, dict):
        errors.append("embedded update trust-root evidence is missing")
    else:
        embedded_verified = update_trust_root.get("embedded_verified")
        if update_trust_root.get("algorithm") != "Ed25519":
            errors.append("update trust-root algorithm is not Ed25519")
        if not _SHA256_RE.fullmatch(
            str(update_trust_root.get("public_key_sha256", ""))
        ):
            errors.append("update trust-root public key digest is invalid")
        elif update_trust_root.get("public_key_sha256") != signing_policy.get(
            "update_public_key_sha256"
        ):
            errors.append("embedded update trust root does not match signing policy")
        if (
            not isinstance(embedded_verified, dict)
            or set(embedded_verified) != set(_PLATFORMS)
            or any(
                embedded_verified.get(platform) is not True
                for platform in _PLATFORMS
            )
        ):
            errors.append(
                "per-platform embedded update trust-root verification is missing"
            )
    update_source = payload.get("update_source")
    if not isinstance(update_source, dict):
        errors.append("embedded update source evidence is missing")
    else:
        source_verified = update_source.get("embedded_verified")
        if update_source.get("base_url") != signing_policy.get("update_base_url"):
            errors.append("embedded update source does not match signing policy")
        if (
            not isinstance(source_verified, dict)
            or set(source_verified) != set(_PLATFORMS)
            or any(
                source_verified.get(platform) is not True
                for platform in _PLATFORMS
            )
        ):
            errors.append(
                "per-platform embedded update source verification is missing"
            )

    packages = payload.get("packages")
    if not isinstance(packages, dict) or set(packages) != set(_PLATFORMS):
        errors.append("package evidence does not cover all three release platforms")
        packages = {}
    for platform in _PLATFORMS:
        package = packages.get(platform)
        if not isinstance(package, dict):
            continue
        package_filename = str(package.get("filename", ""))
        package_path: Path | None = None
        if (
            not package_filename
            or Path(package_filename).name != package_filename
            or Path(package_filename).is_absolute()
        ):
            errors.append(f"{platform} package filename is missing or unsafe")
        elif not package_filename.lower().endswith(
            _PLATFORM_PACKAGE_SUFFIXES[platform]
        ):
            errors.append(f"{platform} package type is not release-eligible")
        else:
            package_path = package_evidence_path.parent / package_filename
            if package_path.is_symlink() or not package_path.is_file():
                errors.append(f"{platform} release package is unavailable")
            elif _sha256(package_path) != package.get("sha256"):
                errors.append(
                    f"{platform} release package digest does not match package evidence"
                )
        package_hash = str(package.get("sha256", ""))
        if not _SHA256_RE.fullmatch(package_hash):
            errors.append(f"{platform} package digest is not a SHA-256 digest")

        signature = package.get("signature")
        if package.get("signature_verified") is not True or not isinstance(
            signature, dict
        ):
            errors.append("verified package signature evidence is missing")
            continue
        if signature.get("subject_sha256") != package_hash:
            errors.append("package signature is not bound to the package digest")
        signature_kind = signature.get("kind")
        if (
            signature_kind not in _TRUSTED_PACKAGE_SIGNATURE_KINDS
            or signature_kind not in _PLATFORM_PACKAGE_SIGNATURE_KINDS[platform]
        ):
            errors.append("package signature kind is not trusted")
        policy_platforms = signing_policy.get("platforms")
        platform_policy = (
            policy_platforms.get(platform, {})
            if isinstance(policy_platforms, Mapping)
            else {}
        )
        if signature_kind not in platform_policy.get("kinds", []):
            errors.append(
                f"package signature kind is not trusted for {platform}"
            )
        if signature.get("identity") not in platform_policy.get("identities", []):
            errors.append(
                f"package signature identity is not trusted for {platform}"
            )
        if signature.get("issuer") not in platform_policy.get("issuers", []):
            errors.append(
                f"package signature issuer is not trusted for {platform}"
            )
        for field in ("kind", "identity", "issuer"):
            if not str(signature.get(field, "")).strip():
                errors.append(f"package signature {field} is missing")
        if not _workflow_run_matches(
            signature.get("verification_run"),
            expected_repository,
            expected_repository_host,
        ):
            errors.append("package signature verification run is not traceable")

    evidence_hashes = payload.get("runtime_evidence_sha256")
    bindings = payload.get("runtime_bindings")
    if not isinstance(evidence_hashes, dict) or set(evidence_hashes) != set(
        _PLATFORMS
    ):
        errors.append("package evidence does not bind all three runtime evidence files")
    if not isinstance(bindings, dict) or set(bindings) != set(_PLATFORMS):
        errors.append("package evidence does not bind all three runtime artifacts")
    for platform in _PLATFORMS:
        native = native_payloads.get(platform, {})
        if isinstance(evidence_hashes, dict) and evidence_hashes.get(
            platform
        ) != native_file_hashes.get(platform):
            errors.append(f"{platform} runtime evidence digest is not package-bound")
        binding = bindings.get(platform) if isinstance(bindings, dict) else None
        if not isinstance(binding, dict):
            continue
        for field in (
            "target_triple",
            "artifact_sha256",
            "desktop_staged_artifact_sha256",
            "runtime_manifest_sha256",
            "sbom_sha256",
        ):
            if binding.get(field) != native.get(field):
                errors.append(f"{platform} {field} is not package-bound")
    return errors


def evaluate_security_release_gate(
    root: Path = ROOT,
    *,
    evidence_paths: Mapping[str, Path] | None = None,
    native_attestation_paths: Mapping[str, Path] | None = None,
    package_evidence_path: Path | None = None,
    package_attestation_path: Path | None = None,
    attestation_signer_workflow: str | None = None,
    attestation_verifier: AttestationVerifier | None = None,
) -> dict[str, Any]:
    """Evaluate the clean-checkout, native-runtime, and signed-package closure."""
    checkout = inspect_release_checkout(root)
    head_commit = _current_head_commit(root)
    repository = _current_repository(root)
    repository_host = _current_repository_host(root)
    identity_errors: list[str] = []
    if not _COMMIT_RE.fullmatch(head_commit):
        identity_errors.append("checkout HEAD is unavailable")
    if not repository:
        identity_errors.append("checkout origin repository is unavailable")
    if not repository_host:
        identity_errors.append("checkout origin host is unavailable")
    try:
        source_hash = _source_hash(root / "security-runtime")
        cargo_lock_hash = _sha256(root / "security-runtime" / "Cargo.lock")
    except OSError as exc:
        source_hash = ""
        cargo_lock_hash = ""
        identity_errors.append(f"runtime source identity is unavailable: {exc}")
    try:
        generate_sbom(root)
    except (OSError, ValueError, RuntimeError) as exc:
        identity_errors.append(f"dependency lock identity is unavailable: {exc}")
    signing_policy, signing_policy_errors = _load_package_signing_policy(root)

    configured_paths = dict(evidence_paths or {})
    if evidence_paths is None:
        for platform in _PLATFORMS:
            value = os.environ.get(
                f"ACE_SECURITY_{platform.upper()}_EVIDENCE", ""
            ).strip()
            if value:
                configured_paths[platform] = Path(value)
    native_payloads: dict[str, dict[str, Any]] = {}
    native_hashes: dict[str, str] = {}
    configured_native_attestations = dict(native_attestation_paths or {})
    evidence_summary: dict[str, Any] = {"checkout": checkout}
    native_ready = True
    verifier = attestation_verifier or _verify_github_attestation
    for platform in _PLATFORMS:
        path = configured_paths.get(platform)
        payload, errors = _read_evidence(path)
        if path is not None and path.is_file():
            native_hashes[platform] = _sha256(path)
        errors.extend(
            _validate_native_evidence(
                platform=platform,
                payload=payload,
                evidence_path=path,
                repo_root=root,
                expected_commit=head_commit,
                expected_repository=repository,
                expected_repository_host=repository_host,
                expected_source_hash=source_hash,
                expected_cargo_lock_hash=cargo_lock_hash,
            )
        )
        attestation_path = configured_native_attestations.get(platform)
        if attestation_path is None and path is not None:
            attestation_path = path.with_name(f"{path.stem}.sigstore.json")
        if attestation_path is None or not attestation_path.is_file():
            errors.append("native evidence provenance attestation is missing")
        elif (
            checkout.get("clean") is True
            and not identity_errors
            and not errors
            and path is not None
        ):
            signer_workflow = (
                f"{repository_host}/{repository}/.github/workflows/"
                f"security-{platform}.yml"
            )
            verified, detail = verifier(
                path,
                attestation_path,
                repository,
                signer_workflow,
                head_commit,
            )
            if not verified:
                errors.append(
                    detail or "native evidence provenance attestation is invalid"
                )
            else:
                sbom_filename = str(payload.get("sbom_filename", ""))
                sbom_path = path.parent / sbom_filename
                sbom_verified, sbom_detail = verifier(
                    sbom_path,
                    attestation_path,
                    repository,
                    signer_workflow,
                    head_commit,
                )
                if not sbom_verified:
                    errors.append(
                        sbom_detail or "native SBOM provenance attestation is invalid"
                    )
                manifest_filename = str(
                    payload.get("runtime_manifest_filename", "")
                )
                manifest_path = path.parent / manifest_filename
                manifest_verified, manifest_detail = verifier(
                    manifest_path,
                    attestation_path,
                    repository,
                    signer_workflow,
                    head_commit,
                )
                if not manifest_verified:
                    errors.append(
                        manifest_detail
                        or "runtime manifest provenance attestation is invalid"
                    )
        errors = list(dict.fromkeys(errors))
        native_payloads[platform] = payload
        native_ready = native_ready and not errors
        evidence_summary[platform] = {
            "passed": not errors,
            "path": str(path)[:500] if path is not None else "",
            "attestation": (
                str(attestation_path)[:500] if attestation_path is not None else ""
            ),
            "target_triple": str(payload.get("target_triple", ""))[:80],
            "sbom": str(payload.get("sbom_filename", ""))[:500],
            "runtime_manifest": str(
                payload.get("runtime_manifest_filename", "")
            )[:500],
            "errors": errors,
        }

    if package_evidence_path is None:
        configured_package = os.environ.get(
            "ACE_SECURITY_PACKAGE_EVIDENCE", ""
        ).strip()
        package_evidence_path = Path(configured_package) if configured_package else None
    if package_attestation_path is None:
        configured_attestation = os.environ.get(
            "ACE_SECURITY_PACKAGE_ATTESTATION", ""
        ).strip()
        package_attestation_path = (
            Path(configured_attestation) if configured_attestation else None
        )
    package_payload, package_errors = _read_evidence(package_evidence_path)
    package_errors.extend(signing_policy_errors)
    if package_evidence_path is not None and not package_errors:
        validation_errors = _validate_package_evidence(
            payload=package_payload,
            package_evidence_path=package_evidence_path,
            native_payloads=native_payloads,
            native_file_hashes=native_hashes,
            expected_commit=head_commit,
            expected_repository=repository,
            expected_repository_host=repository_host,
            expected_source_hash=source_hash,
            expected_cargo_lock_hash=cargo_lock_hash,
            signing_policy=signing_policy,
        )
        package_errors.extend(validation_errors)
    if package_attestation_path is None or not package_attestation_path.is_file():
        package_errors.append("package evidence attestation bundle is missing")
    policy_workflow = str(signing_policy.get("attestation_signer_workflow", ""))
    policy_signer_workflow = (
        f"{repository_host}/{repository}/{policy_workflow}"
        if repository_host and repository and policy_workflow
        else ""
    )
    if attestation_signer_workflow is None:
        attestation_signer_workflow = policy_signer_workflow
    if (
        not policy_signer_workflow
        or attestation_signer_workflow != policy_signer_workflow
    ):
        package_errors.append(
            "package attestation signer does not match trusted signing policy"
        )
    if (
        checkout.get("clean") is True
        and not identity_errors
        and not package_errors
        and package_evidence_path is not None
        and package_attestation_path is not None
    ):
        verified, detail = verifier(
            package_evidence_path,
            package_attestation_path,
            repository,
            attestation_signer_workflow,
            head_commit,
        )
        if not verified:
            package_errors.append(
                detail or "package evidence artifact attestation is invalid"
            )
    package_errors = list(dict.fromkeys(package_errors))
    evidence_summary["package"] = {
        "passed": not package_errors,
        "path": str(package_evidence_path)[:500]
        if package_evidence_path is not None
        else "",
        "attestation": str(package_attestation_path)[:500]
        if package_attestation_path is not None
        else "",
        "errors": package_errors,
    }
    all_ready = (
        checkout.get("clean") is True
        and not identity_errors
        and native_ready
        and not package_errors
    )
    evidence_summary["identity_errors"] = identity_errors
    return {
        "id": "native-security-matrix",
        "status": "ready" if all_ready else "blocked",
        "reason": (
            "clean checkout, three native runners, package bindings, and signed "
            "attested package evidence are required"
        ),
        "evidence": evidence_summary,
    }


def _load_policy(root: Path) -> dict[str, Any]:
    """Load the reviewable release policy without consulting runtime credentials."""
    path = root / "docs" / POLICY_PATH.name
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return json.loads(json.dumps(_DEFAULT_POLICY))


def _skill_version(skill_dir: Path) -> str:
    """Read the declared Skill version used to identify a supplier artifact."""
    metadata = skill_dir / "SKILL.md"
    if not metadata.is_file():
        return "missing"
    match = _VERSION_RE.search(metadata.read_text(encoding="utf-8"))
    return match.group(1) if match else "unknown"


def _requests_without_timeout(path: Path) -> list[int]:
    """Return source lines where direct requests calls have no finite timeout keyword."""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    missing: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in _HTTP_METHODS:
            continue
        owner = node.func.value
        if not isinstance(owner, ast.Name) or owner.id != "requests":
            continue
        if not any(keyword.arg == "timeout" for keyword in node.keywords):
            missing.append(node.lineno)
    return sorted(missing)


def _vlm_credential_violations(path: Path) -> list[str]:
    """Detect the legacy paths that the accepted VLM credential contract forbids."""
    if not path.is_file():
        return ["source file missing"]
    source = path.read_text(encoding="utf-8")
    violations: list[str] = []
    if ".openclaw" in source:
        violations.append("legacy OpenClaw path")
    if 'Path("CREW_ENV_FILE")' in source or "Path('CREW_ENV_FILE')" in source:
        violations.append("literal CREW_ENV_FILE path")
    if 'parent.parent.parent / ".env"' in source or "parent.parent.parent / '.env'" in source:
        violations.append("repository-relative .env path")
    return violations


def collect_gate_results(root: Path = ROOT) -> list[dict[str, Any]]:
    """Evaluate release gates and return credential-free, machine-readable results."""
    policy = _load_policy(root)
    gates = {gate["id"]: gate for gate in policy["gates"]}
    results: list[dict[str, Any]] = []

    shared = gates["shared-deployment-https"]
    results.append(
        {
            "id": shared["id"],
            "status": shared["status"],
            "reason": "HTTPS endpoint, certificate and interface acceptance evidence is pending",
        }
    )

    cm_dir = root / "crew" / "skills" / "cm-cloud-manage"
    cm_version = _skill_version(cm_dir)
    missing_timeouts: list[str] = []
    for source_path in sorted((cm_dir / "scripts").glob("*.py")):
        missing_timeouts.extend(
            f"{source_path.name}:{line}" for line in _requests_without_timeout(source_path)
        )
    cm_policy = gates["cm-cloud-manage-timeouts"]
    results.append(
        {
            "id": cm_policy["id"],
            "status": cm_policy["status"],
            "version": cm_version,
            "blocked_scopes": cm_policy["blocked_scopes"],
            "reason": "supplier timeout acceptance is pending",
            "evidence": {"requests_without_timeout": missing_timeouts},
        }
    )

    vlm_policy = gates["vlm-credential-contract"]
    vlm_evidence: dict[str, Any] = {}
    for slug in ("image-understanding", "video-understanding"):
        skill_dir = root / "crew" / "skills" / slug
        source_path = skill_dir / "scripts" / ("image_understand.py" if slug.startswith("image") else "video_understand.py")
        vlm_evidence[slug] = {
            "version": _skill_version(skill_dir),
            "violations": _vlm_credential_violations(source_path),
        }
    results.append(
        {
            "id": vlm_policy["id"],
            "status": vlm_policy["status"],
            "blocked_scopes": vlm_policy["blocked_scopes"],
            "reason": "supplier credential-contract acceptance is pending",
            "evidence": vlm_evidence,
        }
    )
    native_policy = gates["native-security-matrix"]
    native_result = evaluate_security_release_gate(root)
    native_result["id"] = native_policy["id"]
    native_result["blocked_scopes"] = native_policy["blocked_scopes"]
    results.append(native_result)
    return results


def _current_head_commit(root: Path) -> str:
    """Return the full HEAD commit sha, or empty string if git is unavailable."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        commit = out.stdout.strip()
        return commit if out.returncode == 0 and _COMMIT_RE.fullmatch(commit) else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _current_repository(root: Path) -> str:
    """Return the owner/repository identity of origin, or empty when unavailable."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return _normalize_repository(out.stdout) if out.returncode == 0 else ""


def _current_repository_host(root: Path) -> str:
    """Return the trusted host of origin, or empty when unavailable."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return _repository_host(out.stdout) if out.returncode == 0 else ""


def main(argv: list[str] | None = None) -> int:
    """Print gate results and return non-zero while any required gate is blocked."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument(
        "--security-release",
        action="store_true",
        help="evaluate only the native and signed-package release closure",
    )
    args = parser.parse_args(argv)

    results = (
        [evaluate_security_release_gate(ROOT)]
        if args.security_release
        else collect_gate_results()
    )
    if args.json:
        print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
    else:
        for result in results:
            print(f"{result['status'].upper():7} {result['id']}: {result['reason']}")
    return 1 if any(result["status"] != "ready" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
