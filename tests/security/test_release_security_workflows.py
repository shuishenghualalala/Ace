"""Release security workflows must test and stage the final runtime artifact."""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

import scripts.check_release_readiness as release_readiness
from scripts.audit_runtime_npm import package_lock_directories
from scripts.check_release_readiness import (
    evaluate_security_release_gate,
    inspect_release_checkout,
)
from scripts.generate_security_sbom import generate_sbom
from scripts.write_security_runtime_evidence import _source_hash, write_evidence

ROOT = Path(__file__).resolve().parents[2]
TEST_SIGNER_WORKFLOW = (
    "github.com/owner/repo/.github/workflows/release-attestation.yml"
)
TEST_UPDATE_KEY_SHA256 = hashlib.sha256(
    b"release update verification key"
).hexdigest()
TEST_UPDATE_BASE_URL = "https://updates.example.test/releases/"


def test_windows_package_uses_admin_owned_install_boundary() -> None:
    pack_script = (ROOT / "deb-package" / "pack_exe.ps1").read_text(encoding="utf-8")

    assert "DefaultDirName={autopf}\\Crew" in pack_script
    assert "PrivilegesRequired=admin" in pack_script
    assert "PrivilegesRequired=lowest" not in pack_script


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _commit_fixture(root: Path) -> None:
    _git(root, "init")
    _git(root, "add", ".")
    _git(
        root,
        "-c",
        "user.name=Release Test",
        "-c",
        "user.email=release-test@example.invalid",
        "commit",
        "-m",
        "fixture",
    )


def _runtime_fixture(tmp_path: Path) -> dict[str, Path | str]:
    root = tmp_path / "repo"
    crate = root / "security-runtime"
    (crate / "src").mkdir(parents=True)
    (crate / "tests").mkdir()
    (crate / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
    (crate / "Cargo.toml").write_text("[package]\nname='fixture'\n", encoding="utf-8")
    (crate / "Cargo.lock").write_text("# lock\n", encoding="utf-8")
    (root / ".gitignore").write_text("/target/\n/staged/\n", encoding="utf-8")

    runtime = root / "target" / "release" / "ace-security-runtime"
    staged = root / "staged" / "ace-security-runtime"
    runtime.parent.mkdir(parents=True)
    staged.parent.mkdir()
    runtime.write_bytes(b"release-runtime")
    staged.write_bytes(runtime.read_bytes())
    runtime_sha256 = hashlib.sha256(runtime.read_bytes()).hexdigest()
    manifest = {
        "schema": 2,
        "platform": "linux",
        "arch": "x64",
        "binary_name": staged.name,
        "binary_sha256": runtime_sha256,
        "source_hash": _source_hash(crate),
        "files": [
            {
                "name": staged.name,
                "sha256": runtime_sha256,
                "size": staged.stat().st_size,
            }
        ],
    }
    manifest_path = staged.parent / "runtime-manifest.json"
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    signer_workflow = root / ".github" / "workflows" / "release-attestation.yml"
    signer_workflow.parent.mkdir(parents=True)
    signer_workflow.write_text("name: release-attestation\n", encoding="utf-8")
    signing_policy = root / "deploy" / "security" / "package-signing-policy.json"
    signing_policy.parent.mkdir(parents=True)
    signing_policy.write_text(
        json.dumps(
            {
                "schema": 1,
                "attestation_signer_workflow": (
                    ".github/workflows/release-attestation.yml"
                ),
                "update_public_key_sha256": TEST_UPDATE_KEY_SHA256,
                "update_base_url": TEST_UPDATE_BASE_URL,
                "platforms": {
                    platform: {
                        "kinds": {
                            "linux": ["gpg-detached"],
                            "windows": ["authenticode"],
                            "macos": ["apple-codesign"],
                        }[platform],
                        "identities": [f"{platform}-release@example.invalid"],
                        "issuers": ["release signing service"],
                    }
                    for platform in ("linux", "windows", "macos")
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    _commit_fixture(root)
    _git(root, "remote", "add", "origin", "https://github.com/owner/repo.git")
    sbom = tmp_path / "security-linux-sbom.cdx.json"
    sbom.write_text(
        json.dumps(
            generate_sbom(root, runtime_manifest=manifest_path),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "root": root,
        "crate": crate,
        "runtime": runtime,
        "staged": staged,
        "manifest": manifest_path,
        "sbom": sbom,
        "commit": _git(root, "rev-parse", "HEAD").stdout.strip(),
    }


def _runner_environment(
    fixture: dict[str, Path | str],
    *,
    platform: str = "linux",
    runner_os: str = "Linux",
    runner_arch: str = "X64",
) -> dict[str, str]:
    return {
        "GITHUB_ACTIONS": "true",
        "GITHUB_SHA": str(fixture["commit"]),
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_RUN_ID": "1",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_WORKFLOW": f"security-{platform}",
        "GITHUB_WORKFLOW_REF": (
            f"owner/repo/.github/workflows/security-{platform}.yml@refs/heads/main"
        ),
        "GITHUB_JOB": "native-security",
        "RUNNER_OS": runner_os,
        "RUNNER_ARCH": runner_arch,
    }


def _release_evidence_bundle(
    tmp_path: Path,
    fixture: dict[str, Path | str],
) -> tuple[dict[str, Path], Path, Path]:
    root = fixture["root"]
    crate = fixture["crate"]
    assert isinstance(root, Path)
    assert isinstance(crate, Path)
    commit = str(fixture["commit"])
    source_hash = _source_hash(crate)
    cargo_lock_hash = hashlib.sha256((crate / "Cargo.lock").read_bytes()).hexdigest()
    evidence_dir = tmp_path / "release-evidence"
    evidence_dir.mkdir()
    targets = {
        "linux": "x86_64-unknown-linux-gnu",
        "windows": "x86_64-pc-windows-msvc",
        "macos": "aarch64-apple-darwin",
    }
    evidence_paths: dict[str, Path] = {}
    runtime_bindings: dict[str, dict[str, str]] = {}
    evidence_hashes: dict[str, str] = {}
    for platform, target in targets.items():
        artifact_bytes = f"{platform}-runtime".encode()
        artifact_hash = hashlib.sha256(artifact_bytes).hexdigest()
        platform_dir = evidence_dir / platform
        platform_dir.mkdir()
        artifact_filename = (
            "ace-security-runtime.exe"
            if platform == "windows"
            else "ace-security-runtime"
        )
        (platform_dir / artifact_filename).write_bytes(artifact_bytes)
        manifest_files = [
            {
                "name": artifact_filename,
                "sha256": artifact_hash,
                "size": len(f"{platform}-runtime"),
            }
        ]
        manifest_payload: dict[str, object] = {
            "schema": 2,
            "platform": {
                "linux": "linux",
                "windows": "win32",
                "macos": "darwin",
            }[platform],
            "arch": "arm64" if target.startswith("aarch64-") else "x64",
            "binary_name": artifact_filename,
            "binary_sha256": artifact_hash,
            "source_hash": source_hash,
            "files": manifest_files,
        }
        if platform == "linux":
            manifest_files.extend(
                [
                    {"name": "bwrap", "sha256": "b" * 64, "size": 456},
                    {
                        "name": "BWRAP-LICENSE",
                        "sha256": "c" * 64,
                        "size": 789,
                    },
                ]
            )
            manifest_payload["bwrap_provenance"] = {
                "source": "distribution package copied at build time",
                "version": "0.11.0-1",
                "license_file": "BWRAP-LICENSE",
            }
        manifest_path = platform_dir / "runtime-manifest.json"
        manifest_path.write_text(
            json.dumps(manifest_payload, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        sbom_path = platform_dir / f"security-{platform}-sbom.cdx.json"
        sbom_path.write_text(
            json.dumps(
                generate_sbom(root, runtime_manifest=manifest_path),
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        sbom_hash = hashlib.sha256(sbom_path.read_bytes()).hexdigest()
        runner_os = {
            "linux": "Linux",
            "windows": "Windows",
            "macos": "macOS",
        }[platform]
        runner_arch = "ARM64" if target.startswith("aarch64-") else "X64"
        payload = {
            "schema": 2,
            "status": "passed",
            "real_runner": True,
            "platform": platform,
            "target_triple": target,
            "repository": "owner/repo",
            "commit": commit,
            "workflow_run": f"https://github.com/owner/repo/actions/runs/{len(evidence_paths) + 1}",
            "workflow_name": f"security-{platform}",
            "workflow_ref": (
                f"owner/repo/.github/workflows/security-{platform}.yml@"
                "refs/heads/main"
            ),
            "workflow_job": "native-security",
            "workflow_run_attempt": "1",
            "runner_os": runner_os,
            "runner_arch": runner_arch,
            "artifact_filename": artifact_filename,
            "artifact_sha256": artifact_hash,
            "desktop_staged_artifact_sha256": artifact_hash,
            "source_hash": source_hash,
            "cargo_lock_sha256": cargo_lock_hash,
            "runtime_manifest_filename": "runtime-manifest.json",
            "runtime_manifest_sha256": manifest_hash,
            "sbom_filename": sbom_path.name,
            "sbom_sha256": sbom_hash,
            "sbom_format": "CycloneDX-1.6",
            "dependency_lock_policy": "committed-and-frozen",
            "vulnerability_threshold": "HIGH,CRITICAL",
            "secret_scan_policy": "required",
        }
        evidence_path = platform_dir / f"security-{platform}-evidence.json"
        evidence_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        evidence_path.with_name(
            f"security-{platform}-evidence.sigstore.json"
        ).write_text("test native verifier input\n", encoding="utf-8")
        evidence_paths[platform] = evidence_path
        evidence_hashes[platform] = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
        runtime_bindings[platform] = {
            "target_triple": target,
            "artifact_sha256": artifact_hash,
            "desktop_staged_artifact_sha256": artifact_hash,
            "runtime_manifest_sha256": manifest_hash,
            "sbom_sha256": sbom_hash,
        }

    packages: dict[str, dict[str, object]] = {}
    package_contract = {
        "linux": ("ace-release.deb", "gpg-detached"),
        "windows": ("ace-release.msix", "authenticode"),
        "macos": ("ace-release.dmg", "apple-codesign"),
    }
    for platform, (filename, signature_kind) in package_contract.items():
        package = evidence_dir / filename
        package.write_bytes(f"signed {platform} release package".encode())
        package_hash = hashlib.sha256(package.read_bytes()).hexdigest()
        packages[platform] = {
            "filename": package.name,
            "sha256": package_hash,
            "signature_verified": True,
            "signature": {
                "kind": signature_kind,
                "subject_sha256": package_hash,
                "identity": f"{platform}-release@example.invalid",
                "issuer": "release signing service",
                "verification_run": "https://github.com/owner/repo/actions/runs/10",
            },
        }
    package_evidence = evidence_dir / "package-evidence.json"
    package_evidence.write_text(
        json.dumps(
            {
                "schema": 2,
                "status": "passed",
                "repository": "owner/repo",
                "commit": commit,
                "source_hash": source_hash,
                "cargo_lock_sha256": cargo_lock_hash,
                "cargo_lock_verified": True,
                "desktop_walkthroughs": {
                    "linux": True,
                    "windows": True,
                    "macos": True,
                },
                "update_trust_root": {
                    "algorithm": "Ed25519",
                    "public_key_sha256": TEST_UPDATE_KEY_SHA256,
                    "embedded_verified": {
                        "linux": True,
                        "windows": True,
                        "macos": True,
                    },
                },
                "update_source": {
                    "base_url": TEST_UPDATE_BASE_URL,
                    "embedded_verified": {
                        "linux": True,
                        "windows": True,
                        "macos": True,
                    },
                },
                "packages": packages,
                "runtime_evidence_sha256": evidence_hashes,
                "runtime_bindings": runtime_bindings,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    package_attestation = evidence_dir / "package-evidence.sigstore.json"
    package_attestation.write_text("test verifier input\n", encoding="utf-8")
    return evidence_paths, package_evidence, package_attestation


def test_release_checkout_reports_modified_and_untracked_files(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    source = root / "security-runtime" / "src" / "main.rs"
    source.parent.mkdir(parents=True)
    source.write_text("fn main() {}\n", encoding="utf-8")
    _commit_fixture(root)

    source.write_text("fn main() { println!(\"dirty\"); }\n", encoding="utf-8")
    evidence = root / "security-linux-evidence.json"
    evidence.write_text("{}\n", encoding="utf-8")

    state = inspect_release_checkout(root)

    assert state["clean"] is False
    assert state["tracked_changes"] == ["security-runtime/src/main.rs"]
    assert state["untracked_files"] == ["security-linux-evidence.json"]


def test_release_checkout_reports_ignored_security_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "security-runtime" / "src").mkdir(parents=True)
    (root / "security-runtime" / "src" / "main.rs").write_text(
        "fn main() {}\n", encoding="utf-8"
    )
    (root / ".gitignore").write_text(
        "/security-runtime/target/\n"
        "/deploy/security/package-signing-policy.json\n"
        "/.github/workflows/release-package-security.yml\n",
        encoding="utf-8",
    )
    _commit_fixture(root)
    runtime = root / "security-runtime" / "target" / "release" / "ace-security-runtime"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"local build")
    ignored_policy = root / "deploy" / "security" / "package-signing-policy.json"
    ignored_policy.parent.mkdir(parents=True)
    ignored_policy.write_text("{}\n", encoding="utf-8")
    ignored_workflow = (
        root / ".github" / "workflows" / "release-package-security.yml"
    )
    ignored_workflow.parent.mkdir(parents=True)
    ignored_workflow.write_text("name: ignored\n", encoding="utf-8")

    state = inspect_release_checkout(root)

    assert state["clean"] is False
    assert state["ignored_security_artifacts"] == [
        ".github/workflows/release-package-security.yml",
        "deploy/security/package-signing-policy.json",
        "security-runtime/target/",
    ]


def test_release_checkout_reports_ignored_source_files(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    source_dir = root / "security-runtime" / "src"
    source_dir.mkdir(parents=True)
    (source_dir / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
    (root / ".gitignore").write_text(
        "/security-runtime/src/local.rs\n", encoding="utf-8"
    )
    _commit_fixture(root)
    (source_dir / "local.rs").write_text("fn ignored() {}\n", encoding="utf-8")

    state = inspect_release_checkout(root)

    assert state["clean"] is False
    assert state["ignored_security_artifacts"] == [
        "security-runtime/src/local.rs"
    ]


def test_security_release_gate_is_ready_only_for_a_clean_checkout(
    tmp_path: Path,
) -> None:
    fixture = _runtime_fixture(tmp_path)
    root = fixture["root"]
    assert isinstance(root, Path)
    evidence_paths, package_evidence, package_attestation = _release_evidence_bundle(
        tmp_path, fixture
    )
    verifier_calls: list[tuple[Path, Path, str, str, str]] = []

    def verify_attestation(
        subject: Path,
        bundle: Path,
        repository: str,
        signer_workflow: str,
        source_digest: str,
    ) -> tuple[bool, str]:
        verifier_calls.append(
            (subject, bundle, repository, signer_workflow, source_digest)
        )
        return True, "verified by test seam"

    ready = evaluate_security_release_gate(
        root,
        evidence_paths=evidence_paths,
        package_evidence_path=package_evidence,
        package_attestation_path=package_attestation,
        attestation_signer_workflow=TEST_SIGNER_WORKFLOW,
        attestation_verifier=verify_attestation,
    )

    assert ready["status"] == "ready"
    expected_subjects: list[Path] = []
    for platform in ("linux", "windows", "macos"):
        expected_subjects.extend(
            [
                evidence_paths[platform],
                evidence_paths[platform].with_name(
                    f"security-{platform}-sbom.cdx.json"
                ),
                evidence_paths[platform].with_name("runtime-manifest.json"),
            ]
        )
    expected_subjects.append(package_evidence)
    assert [call[0] for call in verifier_calls] == expected_subjects
    assert verifier_calls[-1] == (
        package_evidence,
        package_attestation,
        "owner/repo",
        TEST_SIGNER_WORKFLOW,
        fixture["commit"],
    )

    source = root / "security-runtime" / "src" / "main.rs"
    source.write_text("fn main() { println!(\"dirty\"); }\n", encoding="utf-8")
    blocked = evaluate_security_release_gate(
        root,
        evidence_paths=evidence_paths,
        package_evidence_path=package_evidence,
        package_attestation_path=package_attestation,
        attestation_signer_workflow=TEST_SIGNER_WORKFLOW,
        attestation_verifier=verify_attestation,
    )

    assert blocked["status"] == "blocked"
    assert blocked["evidence"]["checkout"]["clean"] is False


def test_security_release_gate_rejects_a_self_reported_package_signature(
    tmp_path: Path,
) -> None:
    fixture = _runtime_fixture(tmp_path)
    root = fixture["root"]
    assert isinstance(root, Path)
    evidence_paths, package_evidence, package_attestation = _release_evidence_bundle(
        tmp_path, fixture
    )
    payload = json.loads(package_evidence.read_text(encoding="utf-8"))
    payload["packages"]["windows"]["signature"]["kind"] = "self-reported"
    package_evidence.write_text(
        json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
    )

    result = evaluate_security_release_gate(
        root,
        evidence_paths=evidence_paths,
        package_evidence_path=package_evidence,
        package_attestation_path=package_attestation,
        attestation_signer_workflow=TEST_SIGNER_WORKFLOW,
        attestation_verifier=lambda *_: (True, "verified by test seam"),
    )

    assert result["status"] == "blocked"
    assert "package signature kind is not trusted" in result["evidence"]["package"][
        "errors"
    ]


@pytest.mark.parametrize(
    ("tamper", "expected_error"),
    [
        ("identity", "package signature identity is not trusted for windows"),
        ("issuer", "package signature issuer is not trusted for windows"),
        ("update-key", "embedded update trust root does not match signing policy"),
        ("update-source", "embedded update source does not match signing policy"),
    ],
)
def test_security_release_gate_binds_signatures_to_the_trusted_signing_policy(
    tmp_path: Path,
    tamper: str,
    expected_error: str,
) -> None:
    fixture = _runtime_fixture(tmp_path)
    root = fixture["root"]
    assert isinstance(root, Path)
    evidence_paths, package_evidence, package_attestation = _release_evidence_bundle(
        tmp_path, fixture
    )
    payload = json.loads(package_evidence.read_text(encoding="utf-8"))
    if tamper == "identity":
        payload["packages"]["windows"]["signature"]["identity"] = (
            "attacker@example.invalid"
        )
    elif tamper == "issuer":
        payload["packages"]["windows"]["signature"]["issuer"] = "untrusted issuer"
    elif tamper == "update-key":
        payload["update_trust_root"]["public_key_sha256"] = "f" * 64
    else:
        payload["update_source"]["base_url"] = "https://attacker.example/releases/"
    package_evidence.write_text(
        json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
    )

    result = evaluate_security_release_gate(
        root,
        evidence_paths=evidence_paths,
        package_evidence_path=package_evidence,
        package_attestation_path=package_attestation,
        attestation_signer_workflow=TEST_SIGNER_WORKFLOW,
        attestation_verifier=lambda *_: (True, "verified by test seam"),
    )

    assert result["status"] == "blocked"
    assert expected_error in result["evidence"]["package"]["errors"]


def test_security_release_gate_rejects_missing_runner_identity(
    tmp_path: Path,
) -> None:
    fixture = _runtime_fixture(tmp_path)
    root = fixture["root"]
    assert isinstance(root, Path)
    evidence_paths, package_evidence, package_attestation = _release_evidence_bundle(
        tmp_path, fixture
    )
    linux_evidence = evidence_paths["linux"]
    native_payload = json.loads(linux_evidence.read_text(encoding="utf-8"))
    for field in (
        "workflow_name",
        "workflow_ref",
        "workflow_job",
        "workflow_run_attempt",
        "runner_os",
        "runner_arch",
    ):
        native_payload.pop(field)
    linux_evidence.write_text(
        json.dumps(native_payload, sort_keys=True) + "\n", encoding="utf-8"
    )
    package_payload = json.loads(package_evidence.read_text(encoding="utf-8"))
    package_payload["runtime_evidence_sha256"]["linux"] = hashlib.sha256(
        linux_evidence.read_bytes()
    ).hexdigest()
    package_evidence.write_text(
        json.dumps(package_payload, sort_keys=True) + "\n", encoding="utf-8"
    )

    result = evaluate_security_release_gate(
        root,
        evidence_paths=evidence_paths,
        package_evidence_path=package_evidence,
        package_attestation_path=package_attestation,
        attestation_signer_workflow=TEST_SIGNER_WORKFLOW,
        attestation_verifier=lambda *_: (True, "verified by test seam"),
    )

    assert result["status"] == "blocked"
    assert "runner identity is incomplete" in result["evidence"]["linux"]["errors"]


def test_security_release_gate_requires_a_committed_package_signing_policy(
    tmp_path: Path,
) -> None:
    fixture = _runtime_fixture(tmp_path)
    root = fixture["root"]
    assert isinstance(root, Path)
    evidence_paths, package_evidence, package_attestation = _release_evidence_bundle(
        tmp_path, fixture
    )
    (root / "deploy" / "security" / "package-signing-policy.json").unlink()

    result = evaluate_security_release_gate(
        root,
        evidence_paths=evidence_paths,
        package_evidence_path=package_evidence,
        package_attestation_path=package_attestation,
        attestation_verifier=lambda *_: (True, "verified by test seam"),
    )

    assert result["status"] == "blocked"
    assert "trusted package signing policy is unavailable" in result["evidence"][
        "package"
    ]["errors"]


def test_security_release_gate_rejects_a_forged_runner_origin(
    tmp_path: Path,
) -> None:
    fixture = _runtime_fixture(tmp_path)
    root = fixture["root"]
    assert isinstance(root, Path)
    evidence_paths, package_evidence, package_attestation = _release_evidence_bundle(
        tmp_path, fixture
    )
    linux_evidence = evidence_paths["linux"]
    native_payload = json.loads(linux_evidence.read_text(encoding="utf-8"))
    native_payload["workflow_run"] = (
        "https://evil.invalid/owner/repo/actions/runs/1"
    )
    linux_evidence.write_text(
        json.dumps(native_payload, sort_keys=True) + "\n", encoding="utf-8"
    )
    package_payload = json.loads(package_evidence.read_text(encoding="utf-8"))
    package_payload["runtime_evidence_sha256"]["linux"] = hashlib.sha256(
        linux_evidence.read_bytes()
    ).hexdigest()
    package_evidence.write_text(
        json.dumps(package_payload, sort_keys=True) + "\n", encoding="utf-8"
    )

    result = evaluate_security_release_gate(
        root,
        evidence_paths=evidence_paths,
        package_evidence_path=package_evidence,
        package_attestation_path=package_attestation,
        attestation_signer_workflow=TEST_SIGNER_WORKFLOW,
        attestation_verifier=lambda *_: (True, "verified by test seam"),
    )

    assert result["status"] == "blocked"
    assert "workflow run does not identify this repository" in result["evidence"][
        "linux"
    ]["errors"]


@pytest.mark.parametrize(
    ("tamper", "failed_section"),
    [
        ("wrong-platform", "linux"),
        ("wrong-target", "linux"),
        ("wrong-repository", "linux"),
        ("missing-platform", "macos"),
        ("missing-native-attestation", "linux"),
        ("missing-runtime-manifest", "linux"),
        ("missing-runtime-artifact", "linux"),
        ("tampered-runtime-manifest", "linux"),
        ("missing-sbom", "linux"),
        ("tampered-sbom", "linux"),
        ("missing-supply-policy", "linux"),
        ("tampered-artifact", "linux"),
        ("missing-signature", "package"),
        ("missing-package", "package"),
        ("missing-walkthrough", "package"),
        ("missing-update-trust-root", "package"),
        ("missing-update-source", "package"),
        ("missing-attestation", "package"),
    ],
)
def test_security_release_gate_rejects_incomplete_or_tampered_evidence(
    tamper: str,
    failed_section: str,
    tmp_path: Path,
) -> None:
    fixture = _runtime_fixture(tmp_path)
    root = fixture["root"]
    assert isinstance(root, Path)
    evidence_paths, package_evidence, package_attestation = _release_evidence_bundle(
        tmp_path, fixture
    )
    package_payload = json.loads(package_evidence.read_text(encoding="utf-8"))

    if tamper == "missing-platform":
        evidence_paths = {
            platform: path
            for platform, path in evidence_paths.items()
            if platform != "macos"
        }
    elif tamper == "missing-native-attestation":
        evidence_paths["linux"].with_name(
            "security-linux-evidence.sigstore.json"
        ).unlink()
    elif tamper == "missing-sbom":
        evidence_paths["linux"].with_name("security-linux-sbom.cdx.json").unlink()
    elif tamper == "missing-runtime-manifest":
        evidence_paths["linux"].with_name("runtime-manifest.json").unlink()
    elif tamper == "missing-runtime-artifact":
        evidence_paths["linux"].with_name("ace-security-runtime").unlink()
    elif tamper == "tampered-runtime-manifest":
        evidence_paths["linux"].with_name("runtime-manifest.json").write_text(
            '{"schema":2,"files":[]}\n',
            encoding="utf-8",
        )
    elif tamper == "tampered-sbom":
        evidence_paths["linux"].with_name("security-linux-sbom.cdx.json").write_text(
            '{"bomFormat":"CycloneDX","components":[]}\n',
            encoding="utf-8",
        )
    elif tamper == "missing-signature":
        package_payload["packages"]["windows"].pop("signature")
        package_evidence.write_text(
            json.dumps(package_payload, sort_keys=True) + "\n", encoding="utf-8"
        )
    elif tamper == "missing-package":
        package_payload["packages"].pop("macos")
        package_evidence.write_text(
            json.dumps(package_payload, sort_keys=True) + "\n", encoding="utf-8"
        )
    elif tamper == "missing-walkthrough":
        package_payload["desktop_walkthroughs"]["macos"] = False
        package_evidence.write_text(
            json.dumps(package_payload, sort_keys=True) + "\n", encoding="utf-8"
        )
    elif tamper == "missing-update-trust-root":
        package_payload.pop("update_trust_root")
        package_evidence.write_text(
            json.dumps(package_payload, sort_keys=True) + "\n", encoding="utf-8"
        )
    elif tamper == "missing-update-source":
        package_payload.pop("update_source")
        package_evidence.write_text(
            json.dumps(package_payload, sort_keys=True) + "\n", encoding="utf-8"
        )
    elif tamper == "missing-attestation":
        package_attestation = tmp_path / "missing-attestation.sigstore.json"
    else:
        linux_evidence = evidence_paths["linux"]
        native_payload = json.loads(linux_evidence.read_text(encoding="utf-8"))
        if tamper == "wrong-platform":
            native_payload["platform"] = "windows"
        elif tamper == "wrong-target":
            native_payload["target_triple"] = "x86_64-pc-windows-msvc"
            package_payload["runtime_bindings"]["linux"]["target_triple"] = (
                "x86_64-pc-windows-msvc"
            )
        elif tamper == "wrong-repository":
            native_payload["repository"] = "attacker/repo"
        elif tamper == "tampered-artifact":
            native_payload["artifact_sha256"] = "0" * 64
            package_payload["runtime_bindings"]["linux"]["artifact_sha256"] = "0" * 64
        elif tamper == "missing-supply-policy":
            native_payload.pop("secret_scan_policy")
        linux_evidence.write_text(
            json.dumps(native_payload, sort_keys=True) + "\n", encoding="utf-8"
        )
        package_payload["runtime_evidence_sha256"]["linux"] = hashlib.sha256(
            linux_evidence.read_bytes()
        ).hexdigest()
        package_evidence.write_text(
            json.dumps(package_payload, sort_keys=True) + "\n", encoding="utf-8"
        )

    result = evaluate_security_release_gate(
        root,
        evidence_paths=evidence_paths,
        package_evidence_path=package_evidence,
        package_attestation_path=package_attestation,
        attestation_signer_workflow=TEST_SIGNER_WORKFLOW,
        attestation_verifier=lambda *_: (True, "verified by test seam"),
    )

    assert result["status"] == "blocked"
    assert result["evidence"][failed_section]["passed"] is False


def test_collect_gate_results_keeps_a_dirty_checkout_blocked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fixture = _runtime_fixture(tmp_path)
    root = fixture["root"]
    assert isinstance(root, Path)
    evidence_paths, package_evidence, package_attestation = _release_evidence_bundle(
        tmp_path, fixture
    )
    source = root / "security-runtime" / "src" / "main.rs"
    source.write_text("fn main() { println!(\"dirty\"); }\n", encoding="utf-8")
    policy = {
        "gates": [
            {"id": "shared-deployment-https", "status": "ready"},
            {
                "id": "cm-cloud-manage-timeouts",
                "status": "ready",
                "blocked_scopes": [],
            },
            {
                "id": "vlm-credential-contract",
                "status": "ready",
                "blocked_scopes": [],
            },
            {
                "id": "native-security-matrix",
                "status": "ready",
                "blocked_scopes": ["desktop-release"],
            },
        ]
    }
    monkeypatch.setattr(release_readiness, "_load_policy", lambda _: policy)
    monkeypatch.setattr(release_readiness, "_skill_version", lambda _: "fixture")
    monkeypatch.setattr(
        release_readiness, "_vlm_credential_violations", lambda _: []
    )
    monkeypatch.setattr(
        release_readiness,
        "_verify_github_attestation",
        lambda *_: (True, "verified by test seam"),
    )
    for platform, path in evidence_paths.items():
        monkeypatch.setenv(f"ACE_SECURITY_{platform.upper()}_EVIDENCE", str(path))
    monkeypatch.setenv("ACE_SECURITY_PACKAGE_EVIDENCE", str(package_evidence))
    monkeypatch.setenv("ACE_SECURITY_PACKAGE_ATTESTATION", str(package_attestation))

    native_gate = next(
        result
        for result in release_readiness.collect_gate_results(root)
        if result["id"] == "native-security-matrix"
    )

    assert native_gate["status"] == "blocked"
    assert native_gate["evidence"]["checkout"]["clean"] is False


def test_release_policy_is_available_from_a_committed_checkout(
    tmp_path: Path,
) -> None:
    policy = release_readiness._load_policy(tmp_path)

    assert {gate["id"] for gate in policy["gates"]} == {
        "shared-deployment-https",
        "cm-cloud-manage-timeouts",
        "vlm-credential-contract",
        "native-security-matrix",
    }


def test_release_gate_fails_closed_when_supplier_inputs_are_missing(
    tmp_path: Path,
) -> None:
    results = release_readiness.collect_gate_results(tmp_path)

    assert len(results) == 4
    assert all(result["status"] == "blocked" for result in results)


def test_security_matrix_documents_the_release_evidence_closure() -> None:
    matrix = (ROOT / "docs" / "security" / "security-test-matrix.md").read_text(
        encoding="utf-8"
    )

    for control in (
        "REL-001",
        "SUP-012",
        "TEST-007",
        "TEST-013",
        "TEST-014",
        "TEST-015",
        "ACE-020",
    ):
        assert control in matrix
    for field in (
        "source_hash",
        "cargo_lock_sha256",
        "runtime_manifest_sha256",
        "artifact_sha256",
        "desktop_staged_artifact_sha256",
        "workflow_ref",
        "runner_os",
        "runner_arch",
        "packages",
        "desktop_walkthroughs",
        "update_trust_root",
        "update_source",
        "sbom_sha256",
        "runtime_evidence_sha256",
        "ACE_SECURITY_PACKAGE_ATTESTATION",
        "package-signing-policy.json",
    ):
        assert field in matrix
    assert "ACE_SECURITY_ATTESTATION_SIGNER_WORKFLOW" not in matrix
    assert "must not be manufactured locally" in matrix


def test_native_security_workflows_bind_release_artifact_evidence() -> None:
    writer = (ROOT / "scripts" / "write_security_runtime_evidence.py").read_text(encoding="utf-8")
    for field in ("artifact_sha256", "repository", "commit"):
        assert f'"{field}"' in writer

    for platform in ("linux", "windows", "macos"):
        source = (ROOT / ".github" / "workflows" / f"security-{platform}.yml").read_text(
            encoding="utf-8"
        )

        assert "git config --global core.autocrlf false" in source
        assert source.index("git config --global core.autocrlf false") < source.index(
            "actions/checkout@"
        )
        assert "target/debug" not in source
        assert "cargo build" in source and "--release" in source and "--locked" in source
        assert "uv sync --extra dev --frozen" in source
        assert "write_security_runtime_evidence.py" in source
        assert "python scripts/audit_runtime_npm.py" in source
        desktop_staging = next(
            line
            for line in source.splitlines()
            if "prepare-security-runtime.mjs" in line
            and "--output desktop/security-runtime-bin" in line
        )
        assert "--source-root security-runtime" in desktop_staging
        assert "actions/attest-build-provenance@" in source
        assert f"security-{platform}-evidence.sigstore.json" in source
        assert "runtime artifact evidence" in source
        artifact_filename = (
            "ace-security-runtime.exe"
            if platform == "windows"
            else "ace-security-runtime"
        )
        assert source.count(artifact_filename) >= 4

        if platform == "macos":
            assert "runs-on: macos-15" in source
            assert "--target-triple aarch64-apple-darwin" in source


def test_runtime_build_scripts_are_locked_and_manifest_output_is_canonical() -> None:
    powershell = (ROOT / "scripts" / "build-security-runtime.ps1").read_text(
        encoding="utf-8"
    )
    shell = (ROOT / "scripts" / "build-security-runtime.sh").read_text(
        encoding="utf-8"
    )

    assert "cargo build --release --locked" in powershell
    assert "cargo build --release --locked" in shell
    manifest_write = (
        'write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\\n", '
        "encoding=\"utf-8\")"
    )
    assert manifest_write in powershell
    assert manifest_write in shell


def test_desktop_runtime_verifier_rejects_manifest_path_escape(tmp_path: Path) -> None:
    staged = tmp_path / "staged"
    staged.mkdir()
    runtime = staged / "ace-security-runtime.exe"
    runtime.write_bytes(b"runtime")
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    digest = hashlib.sha256(runtime.read_bytes()).hexdigest()
    outside_digest = hashlib.sha256(outside.read_bytes()).hexdigest()
    (staged / "runtime-manifest.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "platform": "win32",
                "arch": "x64",
                "binary_name": runtime.name,
                "binary_sha256": digest,
                "files": [
                    {"name": runtime.name, "sha256": digest, "size": runtime.stat().st_size},
                    {"name": "../outside", "sha256": outside_digest, "size": outside.stat().st_size},
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "node",
            str(ROOT / "desktop" / "scripts" / "verify-security-runtime.mjs"),
            str(staged),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "invalid security runtime manifest file metadata" in result.stderr.lower()


def test_desktop_runtime_verifier_requires_source_hash_when_source_is_present(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    staged = root / "desktop" / "security-runtime-bin"
    crate = root / "security-runtime"
    (crate / "src").mkdir(parents=True)
    (crate / "Cargo.toml").write_text("[package]\nname = 'fixture'\n", encoding="utf-8")
    (crate / "Cargo.lock").write_text("# lock\n", encoding="utf-8")
    staged.mkdir(parents=True)
    runtime = staged / "ace-security-runtime.exe"
    runtime.write_bytes(b"runtime")
    digest = hashlib.sha256(runtime.read_bytes()).hexdigest()
    (staged / "runtime-manifest.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "platform": "win32",
                "arch": "x64",
                "binary_name": runtime.name,
                "binary_sha256": digest,
                "files": [{"name": runtime.name, "sha256": digest, "size": runtime.stat().st_size}],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "node",
            str(ROOT / "desktop" / "scripts" / "verify-security-runtime.mjs"),
            str(staged),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "source hash" in result.stderr.lower()


def test_release_evidence_schemas_require_artifact_sbom_and_trust_bindings(
    tmp_path: Path,
) -> None:
    fixture = _runtime_fixture(tmp_path)
    evidence_paths, package_evidence, _attestation = _release_evidence_bundle(
        tmp_path,
        fixture,
    )
    native_schema = json.loads(
        (ROOT / "deploy" / "security" / "native-evidence.schema.json").read_text(
            encoding="utf-8"
        )
    )
    package_schema = json.loads(
        (ROOT / "deploy" / "security" / "package-evidence.schema.json").read_text(
            encoding="utf-8"
        )
    )
    signing_policy_schema = json.loads(
        (
            ROOT
            / "deploy"
            / "security"
            / "package-signing-policy.schema.json"
        ).read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(native_schema)
    Draft202012Validator.check_schema(package_schema)
    Draft202012Validator.check_schema(signing_policy_schema)

    native_validator = Draft202012Validator(native_schema)
    for path in evidence_paths.values():
        native_validator.validate(json.loads(path.read_text(encoding="utf-8")))
    package_validator = Draft202012Validator(package_schema)
    package_validator.validate(
        json.loads(package_evidence.read_text(encoding="utf-8"))
    )
    signing_policy_validator = Draft202012Validator(signing_policy_schema)
    signing_policy = json.loads(
        (
            fixture["root"]
            / "deploy"
            / "security"
            / "package-signing-policy.json"
        ).read_text(encoding="utf-8")
    )
    signing_policy_validator.validate(signing_policy)

    incomplete = json.loads(evidence_paths["linux"].read_text(encoding="utf-8"))
    incomplete.pop("sbom_sha256")
    assert list(native_validator.iter_errors(incomplete))
    incomplete_package = json.loads(package_evidence.read_text(encoding="utf-8"))
    incomplete_package.pop("update_trust_root")
    assert list(package_validator.iter_errors(incomplete_package))
    signing_policy["platforms"]["windows"]["identities"] = []
    assert list(signing_policy_validator.iter_errors(signing_policy))


def test_all_workflow_actions_are_pinned_to_immutable_commits() -> None:
    reviewed_revisions = {
        "actions/attest-build-provenance": "4d101475d8b20a2381f78447822ac1eab6504dd8",
        "actions/checkout": "11d5960a326750d5838078e36cf38b85af677262",
        "actions/download-artifact": "d3f86a106a0bac45b974a628896c90dbdf5c8093",
        "actions/setup-node": "49933ea5288caeca8642d1e84afbd3f7d6820020",
        "actions/upload-artifact": "ea165f8d65b6e75b540449e92b4886f43607fa02",
        "aquasecurity/trivy-action": "57a97c7e7821a5776cebc9bb87c984fa69cba8f1",
        "astral-sh/setup-uv": "d4b2f3b6ecc6e67c4457f6d3e41ec42d3d0fcb86",
        "dtolnay/rust-toolchain": "4360b52568e2003a75bf9bc1d59f33a8e3fc893c",
    }
    observed_actions: set[str] = set()
    workflow_root = ROOT / ".github" / "workflows"
    workflow_paths = sorted(
        (*workflow_root.glob("*.yml"), *workflow_root.glob("*.yaml"))
    )
    assert workflow_paths
    for workflow_path in workflow_paths:
        source = workflow_path.read_text(encoding="utf-8")
        action_refs = [
            line.split("uses:", 1)[1].strip().split()[0]
            for line in source.splitlines()
            if "uses:" in line
        ]
        assert action_refs
        for action_ref in action_refs:
            action, revision = action_ref.rsplit("@", 1)
            observed_actions.add(action)
            assert len(revision) == 40
            assert all(character in "0123456789abcdef" for character in revision)
            assert reviewed_revisions.get(action) == revision
    assert observed_actions == set(reviewed_revisions)


def test_all_workflow_npm_installs_disable_implicit_lifecycle_scripts() -> None:
    workflow_root = ROOT / ".github" / "workflows"
    install_lines = [
        line.strip()
        for path in (*workflow_root.glob("*.yml"), *workflow_root.glob("*.yaml"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if "npm ci" in line or "npm install" in line
    ]

    assert install_lines
    assert all("--ignore-scripts" in line for line in install_lines)


def test_all_workflow_checkouts_drop_persisted_repository_credentials() -> None:
    workflow_root = ROOT / ".github" / "workflows"
    checkout_windows = [
        lines[index : index + 6]
        for path in (*workflow_root.glob("*.yml"), *workflow_root.glob("*.yaml"))
        for lines in [path.read_text(encoding="utf-8").splitlines()]
        for index, line in enumerate(lines)
        if "uses: actions/checkout@" in line
    ]

    assert checkout_windows
    assert all(
        any("persist-credentials: false" in line for line in window)
        for window in checkout_windows
    )


def test_native_workflows_generate_sboms_and_enforce_supply_chain_thresholds() -> None:
    for platform in ("linux", "windows", "macos"):
        source = (
            ROOT / ".github" / "workflows" / f"security-{platform}.yml"
        ).read_text(encoding="utf-8")
        sbom = f"security-{platform}-sbom.cdx.json"
        staged_sbom = f"desktop/security-runtime-bin/{sbom}"
        assert f"generate_security_sbom.py --output {staged_sbom}" in source
        assert (
            "--runtime-manifest desktop/security-runtime-bin/runtime-manifest.json"
            in source
        )
        assert f"--sbom {staged_sbom}" in source
        assert sbom in source
        assert source.count("runtime-manifest.json") >= 4
        assert "toolchain: '1.97.1'" in source
        assert "version: '0.11.28'" in source
        assert "python-version: '3.11.13'" in source
        assert "node-version: '22.18.0'" in source
        assert "persist-credentials: false" in source
        assert "npm ci --ignore-scripts --prefix desktop" in source
        assert "npm --prefix desktop run typecheck" in source
        assert "tests/unit/bootstrap-hardening.test.ts" in source
        assert "tests/unit/ipc-schemas.test.ts" in source
        assert "node desktop/scripts/check-security.mjs" in source
        assert "attest-release-evidence:" in source
        native_job, attestation_job = source.split("  attest-release-evidence:", 1)
        assert "id-token: write" not in native_job
        assert source.count("id-token: write") == 1
        assert "needs: native-security" in attestation_job
        assert "if: github.event_name == 'workflow_dispatch'" in attestation_job
        assert "actions/download-artifact@" in attestation_job
        assert "actions/attest-build-provenance@" in attestation_job

    linux = (ROOT / ".github" / "workflows" / "security-linux.yml").read_text(
        encoding="utf-8"
    )
    assert "aquasecurity/trivy-action@" in linux
    assert "trivy-config: deploy/security/trivy.yaml" in linux
    assert "cache-dir: ${{ runner.temp }}/trivy-cache" in linux
    assert "scanners: 'vuln,secret,misconfig'" in linux
    assert "severity: 'HIGH,CRITICAL'" in linux
    assert "exit-code: '1'" in linux
    assert "ignore-unfixed: false" in linux
    assert linux.count("aquasecurity/trivy-action@") == 2
    assert "scan-type: sbom" in linux
    assert (
        "scan-ref: desktop/security-runtime-bin/security-linux-sbom.cdx.json"
        in linux
    )
    assert linux.index(
        "generate_security_sbom.py --output"
    ) < linux.rindex("aquasecurity/trivy-action@")
    assert linux.index("aquasecurity/trivy-action@") < linux.index(
        "npm ci --ignore-scripts --prefix desktop"
    )
    assert linux.index("aquasecurity/trivy-action@") < linux.index(
        "uv sync --extra dev --frozen"
    )
    trivy_config = (
        ROOT / "deploy" / "security" / "trivy.yaml"
    ).read_text(encoding="utf-8")
    secret_config = (
        ROOT / "deploy" / "security" / "trivy-secret.yaml"
    ).read_text(encoding="utf-8")
    assert "config: deploy/security/trivy-secret.yaml" in trivy_config
    assert "desktop/node_modules" in trivy_config
    assert "\n    path:" not in secret_config
    assert "AKIAEXAMPLEACCESSKEY" in secret_config


def test_security_release_gate_requires_all_attested_platform_and_package_runs() -> None:
    workflow = (
        ROOT / ".github" / "workflows" / "security-release-gate.yml"
    ).read_text(encoding="utf-8")

    assert "workflow_call:" in workflow
    assert "workflow_dispatch:" in workflow
    for required_input in (
        "source_commit:",
        "linux_run_id:",
        "windows_run_id:",
        "macos_run_id:",
        "package_run_id:",
    ):
        assert required_input in workflow
    assert workflow.count("actions/download-artifact@") == 4
    assert "security-linux-evidence" in workflow
    assert "security-windows-evidence" in workflow
    assert "security-macos-evidence" in workflow
    assert "security-package-evidence" in workflow
    assert "scripts/check_release_readiness.py --security-release --json" in workflow
    assert "continue-on-error:" not in workflow
    assert "security-release-gate.json" in workflow


@pytest.mark.parametrize(("status", "expected_exit"), [("ready", 0), ("blocked", 1)])
def test_security_release_cli_scopes_to_the_blocking_release_closure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
    expected_exit: int,
) -> None:
    monkeypatch.setattr(
        release_readiness,
        "evaluate_security_release_gate",
        lambda _root: {"id": "native-security-matrix", "status": status},
    )
    monkeypatch.setattr(
        release_readiness,
        "collect_gate_results",
        lambda: pytest.fail("unrelated readiness gates must not be evaluated"),
    )

    assert (
        release_readiness.main(["--security-release", "--json"]) == expected_exit
    )
    output = json.loads(capsys.readouterr().out)
    assert output["results"] == [
        {"id": "native-security-matrix", "status": status}
    ]


def test_release_sbom_is_deterministic_and_binds_every_committed_lockfile(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    (root / "security-runtime").mkdir(parents=True)
    (root / "web").mkdir()
    (root / "uv.lock").write_text(
        'version = 1\n[[package]]\nname = "httpx"\nversion = "1.2.3"\n'
        'sdist = { hash = "sha256:'
        + ("a" * 64)
        + '" }\n',
        encoding="utf-8",
    )
    (root / "security-runtime" / "Cargo.lock").write_text(
        'version = 4\n[[package]]\nname = "serde"\nversion = "1.0.0"\n'
        f'checksum = "{"b" * 64}"\n',
        encoding="utf-8",
    )
    (root / "web" / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "web",
                "version": "1.0.0",
                "lockfileVersion": 3,
                "packages": {
                    "": {"name": "web", "version": "1.0.0"},
                    "node_modules/ws": {
                        "version": "8.0.0",
                        "integrity": "sha512-dGVzdA==",
                    },
                    "node_modules/vitest": {
                        "version": "4.1.0",
                        "integrity": "sha512-dGVzdA==",
                        "dev": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    _commit_fixture(root)
    (root / "package-lock.json").write_text(
        '{"packages":{"node_modules/untracked":{"version":"9.9.9"}}}',
        encoding="utf-8",
    )

    first = generate_sbom(root)
    second = generate_sbom(root)

    assert first == second
    assert first["bomFormat"] == "CycloneDX"
    assert first["specVersion"] == "1.6"
    purls = {component["purl"] for component in first["components"]}
    assert purls == {
        "pkg:cargo/serde@1.0.0",
        "pkg:npm/vitest@4.1.0",
        "pkg:npm/ws@8.0.0",
        "pkg:pypi/httpx@1.2.3",
    }
    lock_properties = first["metadata"]["properties"]
    assert {item["name"] for item in lock_properties} == {
        "ace:lockfile:security-runtime/Cargo.lock",
        "ace:lockfile:uv.lock",
        "ace:lockfile:web/package-lock.json",
    }


def test_release_sbom_binds_runtime_manifest_files_and_bwrap_provenance(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    (root / "security-runtime").mkdir(parents=True)
    (root / "security-runtime" / "Cargo.lock").write_text(
        'version = 4\n[[package]]\nname = "serde"\nversion = "1.0.0"\n'
        f'checksum = "{"b" * 64}"\n',
        encoding="utf-8",
    )
    _commit_fixture(root)
    manifest = tmp_path / "runtime-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": 2,
                "platform": "linux",
                "arch": "x64",
                "binary_name": "ace-security-runtime",
                "binary_sha256": "c" * 64,
                "files": [
                    {
                        "name": "ace-security-runtime",
                        "sha256": "c" * 64,
                        "size": 123,
                    },
                    {"name": "bwrap", "sha256": "d" * 64, "size": 456},
                    {
                        "name": "BWRAP-LICENSE",
                        "sha256": "e" * 64,
                        "size": 789,
                    },
                ],
                "bwrap_provenance": {
                    "source": "distribution package copied at build time",
                    "version": "0.11.0-1",
                    "license_file": "BWRAP-LICENSE",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    document = generate_sbom(root, runtime_manifest=manifest)

    artifact_components = {
        component["name"]: component
        for component in document["components"]
        if component["type"] == "file"
    }
    assert set(artifact_components) == {
        "ace-security-runtime",
        "bwrap",
        "BWRAP-LICENSE",
    }
    assert artifact_components["bwrap"]["hashes"] == [
        {"alg": "SHA-256", "content": "d" * 64}
    ]
    assert {
        item["name"]: item["value"]
        for item in artifact_components["bwrap"]["properties"]
    }["ace:bwrap:version"] == "0.11.0-1"
    assert (
        artifact_components["bwrap"]["purl"]
        == "pkg:deb/ubuntu/bubblewrap@0.11.0-1?arch=amd64&distro=ubuntu-24.04"
    )
    assert any(
        item["name"] == "ace:runtime-manifest:sha256"
        for item in document["metadata"]["properties"]
    )


def test_runtime_npm_audit_discovers_every_committed_lockfile() -> None:
    discovered = {
        path.relative_to(ROOT).as_posix() for path in package_lock_directories(ROOT)
    }

    assert discovered == {
        ".",
        "web",
        "desktop",
        "crew/skills/html-to-pdf",
    }


def test_runtime_npm_audit_ignores_untracked_lockfiles(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "package-lock.json").write_text("{}\n", encoding="utf-8")
    _commit_fixture(root)
    untracked = root / "local-output" / "package-lock.json"
    untracked.parent.mkdir()
    untracked.write_text("{}\n", encoding="utf-8")

    assert package_lock_directories(root) == [root]


def test_evidence_writer_rejects_manifest_source_hash_drift(tmp_path: Path) -> None:
    fixture = _runtime_fixture(tmp_path)
    root = fixture["root"]
    runtime = fixture["runtime"]
    staged = fixture["staged"]
    manifest_path = fixture["manifest"]
    assert isinstance(root, Path)
    assert isinstance(runtime, Path)
    assert isinstance(staged, Path)
    assert isinstance(manifest_path, Path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_hash"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="manifest source hash"):
        write_evidence(
            repo_root=root,
            runtime=runtime,
            staged_runtime=staged,
            output=tmp_path / "evidence.json",
            platform="linux",
            target_triple="x86_64-unknown-linux-gnu",
            repository="owner/repo",
            commit=str(fixture["commit"]),
            workflow_run="https://github.com/owner/repo/actions/runs/1",
            runner_environment=_runner_environment(fixture),
        )


def test_runtime_evidence_writer_rejects_a_dirty_checkout(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    crate = root / "security-runtime"
    (crate / "src").mkdir(parents=True)
    (crate / "tests").mkdir()
    source = crate / "src" / "main.rs"
    source.write_text("fn main() {}\n", encoding="utf-8")
    (crate / "Cargo.toml").write_text("[package]\nname='fixture'\n", encoding="utf-8")
    (crate / "Cargo.lock").write_text("# lock\n", encoding="utf-8")
    runtime = root / "target" / "release" / "runtime"
    staged = root / "staged" / "runtime"
    runtime.parent.mkdir(parents=True)
    staged.parent.mkdir()
    runtime.write_bytes(b"release-runtime")
    staged.write_bytes(runtime.read_bytes())
    (staged.parent / "runtime-manifest.json").write_text("{}\n", encoding="utf-8")
    _commit_fixture(root)
    source.write_text("fn main() { println!(\"dirty\"); }\n", encoding="utf-8")

    with pytest.raises(ValueError, match="clean checkout"):
        write_evidence(
            repo_root=root,
            runtime=runtime,
            staged_runtime=staged,
            output=tmp_path / "evidence.json",
            platform="linux",
            target_triple="x86_64-unknown-linux-gnu",
            repository="owner/repo",
            commit=_git(root, "rev-parse", "HEAD").stdout.strip(),
            workflow_run="https://github.com/owner/repo/actions/runs/1",
        )


def test_runtime_evidence_writer_rejects_a_local_runner_claim(
    tmp_path: Path,
) -> None:
    fixture = _runtime_fixture(tmp_path)
    root = fixture["root"]
    runtime = fixture["runtime"]
    staged = fixture["staged"]
    assert isinstance(root, Path)
    assert isinstance(runtime, Path)
    assert isinstance(staged, Path)

    with pytest.raises(ValueError, match="GitHub Actions"):
        write_evidence(
            repo_root=root,
            runtime=runtime,
            staged_runtime=staged,
            output=tmp_path / "evidence.json",
            platform="linux",
            target_triple="x86_64-unknown-linux-gnu",
            repository="owner/repo",
            commit=str(fixture["commit"]),
            workflow_run="https://github.com/owner/repo/actions/runs/1",
            runner_environment={},
        )


def test_runtime_evidence_writer_rejects_a_forged_workflow_origin(
    tmp_path: Path,
) -> None:
    fixture = _runtime_fixture(tmp_path)
    root = fixture["root"]
    runtime = fixture["runtime"]
    staged = fixture["staged"]
    assert isinstance(root, Path)
    assert isinstance(runtime, Path)
    assert isinstance(staged, Path)

    with pytest.raises(ValueError, match="Actions run"):
        write_evidence(
            repo_root=root,
            runtime=runtime,
            staged_runtime=staged,
            output=tmp_path / "evidence.json",
            platform="linux",
            target_triple="x86_64-unknown-linux-gnu",
            repository="owner/repo",
            commit=str(fixture["commit"]),
            workflow_run="https://evil.invalid/owner/repo/actions/runs/1",
        )


def test_runtime_evidence_writer_requires_the_canonical_repository_slug(
    tmp_path: Path,
) -> None:
    fixture = _runtime_fixture(tmp_path)
    root = fixture["root"]
    runtime = fixture["runtime"]
    staged = fixture["staged"]
    assert isinstance(root, Path)
    assert isinstance(runtime, Path)
    assert isinstance(staged, Path)

    with pytest.raises(ValueError, match="checkout origin"):
        write_evidence(
            repo_root=root,
            runtime=runtime,
            staged_runtime=staged,
            output=tmp_path / "evidence.json",
            platform="linux",
            target_triple="x86_64-unknown-linux-gnu",
            repository="https://evil.invalid/owner/repo",
            commit=str(fixture["commit"]),
            workflow_run="https://github.com/owner/repo/actions/runs/1",
        )


def test_runtime_evidence_writer_requires_a_lock_bound_cyclonedx_sbom(
    tmp_path: Path,
) -> None:
    fixture = _runtime_fixture(tmp_path)
    root = fixture["root"]
    runtime = fixture["runtime"]
    staged = fixture["staged"]
    assert isinstance(root, Path)
    assert isinstance(runtime, Path)
    assert isinstance(staged, Path)

    with pytest.raises(ValueError, match="SBOM"):
        write_evidence(
            repo_root=root,
            runtime=runtime,
            staged_runtime=staged,
            output=tmp_path / "evidence.json",
            platform="linux",
            target_triple="x86_64-unknown-linux-gnu",
            repository="owner/repo",
            commit=str(fixture["commit"]),
            workflow_run="https://github.com/owner/repo/actions/runs/1",
            runner_environment=_runner_environment(fixture),
        )


def test_runtime_evidence_binds_the_same_tested_and_staged_bytes(tmp_path: Path) -> None:
    fixture = _runtime_fixture(tmp_path)
    root = fixture["root"]
    crate = fixture["crate"]
    runtime = fixture["runtime"]
    staged = fixture["staged"]
    manifest = fixture["manifest"]
    sbom = fixture["sbom"]
    assert isinstance(root, Path)
    assert isinstance(crate, Path)
    assert isinstance(runtime, Path)
    assert isinstance(staged, Path)
    assert isinstance(manifest, Path)
    assert isinstance(sbom, Path)
    output = tmp_path / "evidence.json"

    write_evidence(
        repo_root=root,
        runtime=runtime,
        staged_runtime=staged,
        output=output,
        platform="linux",
        target_triple="x86_64-unknown-linux-gnu",
        repository="owner/repo",
        commit=str(fixture["commit"]),
        workflow_run="https://github.com/owner/repo/actions/runs/1",
        runner_environment=_runner_environment(fixture),
        sbom=sbom,
    )

    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert evidence["schema"] == 2
    assert evidence["artifact_sha256"] == evidence["desktop_staged_artifact_sha256"]
    assert evidence["artifact_sha256"] == json.loads(
        manifest.read_text(encoding="utf-8")
    )["binary_sha256"]
    assert evidence["runtime_manifest_sha256"] == hashlib.sha256(
        manifest.read_bytes()
    ).hexdigest()
    assert evidence["sbom_filename"] == "security-linux-sbom.cdx.json"
    assert evidence["sbom_sha256"] == hashlib.sha256(sbom.read_bytes()).hexdigest()
    assert evidence["sbom_format"] == "CycloneDX-1.6"
    assert evidence["source_hash"] == _source_hash(crate)
    assert evidence["cargo_lock_sha256"] == hashlib.sha256(
        (crate / "Cargo.lock").read_bytes()
    ).hexdigest()
    assert evidence["commit"] == fixture["commit"]
    assert evidence["repository"] == "owner/repo"
    assert evidence["platform"] == "linux"
    assert evidence["target_triple"] == "x86_64-unknown-linux-gnu"
    assert evidence["workflow_ref"].startswith(
        "owner/repo/.github/workflows/security-linux.yml@"
    )
    assert evidence["workflow_job"] == "native-security"
    assert evidence["workflow_run_attempt"] == "1"
    assert evidence["runner_os"] == "Linux"
    assert evidence["runner_arch"] == "X64"
    staged.write_bytes(b"substituted")
    with pytest.raises(ValueError, match="staging changed"):
        write_evidence(
            repo_root=root,
            runtime=runtime,
            staged_runtime=staged,
            output=output,
            platform="linux",
            target_triple="x86_64-unknown-linux-gnu",
            repository="owner/repo",
            commit=str(fixture["commit"]),
            workflow_run="https://github.com/owner/repo/actions/runs/1",
            runner_environment=_runner_environment(fixture),
        )
