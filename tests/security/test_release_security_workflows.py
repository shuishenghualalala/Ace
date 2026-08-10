"""Release security workflows must test and stage the final runtime artifact."""

import hashlib
import json
from pathlib import Path

import pytest

from scripts.audit_runtime_npm import package_lock_directories
from scripts.write_security_runtime_evidence import _source_hash, write_evidence


ROOT = Path(__file__).resolve().parents[2]


def test_native_security_workflows_bind_release_artifact_evidence() -> None:
    writer = (ROOT / "scripts" / "write_security_runtime_evidence.py").read_text(encoding="utf-8")
    for field in ("artifact_sha256", "repository", "commit"):
        assert f'"{field}"' in writer

    for platform in ("linux", "windows", "macos"):
        source = (ROOT / ".github" / "workflows" / f"security-{platform}.yml").read_text(
            encoding="utf-8"
        )

        assert "target/debug" not in source
        assert "cargo build" in source and "--release" in source and "--locked" in source
        assert "write_security_runtime_evidence.py" in source
        assert "python scripts/audit_runtime_npm.py" in source


def test_runtime_npm_audit_discovers_every_committed_lockfile() -> None:
    discovered = {
        path.relative_to(ROOT).as_posix() for path in package_lock_directories(ROOT)
    }

    assert discovered == {
        ".",
        "web",
        "desktop",
    }


def test_evidence_source_hash_matches_platform_prebuilt_manifests() -> None:
    crate = ROOT / "security-runtime"
    manifests = sorted((crate / "prebuilt").glob("*/runtime-manifest.json"))
    assert manifests, "at least one platform-specific prebuilt runtime must be committed"
    expected = _source_hash(crate)
    for manifest_path in manifests:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["source_hash"] == expected


def test_committed_prebuilt_runtimes_match_source_target_and_digest() -> None:
    crate = ROOT / "security-runtime"
    manifests = sorted((crate / "prebuilt").glob("*/runtime-manifest.json"))
    assert manifests, "at least one platform-specific prebuilt runtime must be committed"
    for manifest_path in manifests:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        runtime = manifest_path.parent / manifest["binary_name"]
        assert manifest["schema"] == 2
        assert f"{manifest['platform']}-{manifest['arch']}" == manifest_path.parent.name
        assert manifest["source_hash"] == _source_hash(crate)
        assert runtime.is_file()
        assert hashlib.sha256(runtime.read_bytes()).hexdigest() == manifest["binary_sha256"]


def test_runtime_evidence_binds_the_same_tested_and_staged_bytes(tmp_path: Path) -> None:
    crate = tmp_path / "security-runtime"
    (crate / "src").mkdir(parents=True)
    (crate / "tests").mkdir()
    (crate / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
    (crate / "Cargo.toml").write_text("[package]\nname='fixture'\n", encoding="utf-8")
    (crate / "Cargo.lock").write_text("# lock\n", encoding="utf-8")
    runtime = tmp_path / "runtime"
    staged = tmp_path / "staged" / "runtime"
    runtime.write_bytes(b"release-runtime")
    staged.parent.mkdir()
    staged.write_bytes(runtime.read_bytes())
    output = tmp_path / "evidence.json"

    write_evidence(
        repo_root=tmp_path,
        runtime=runtime,
        staged_runtime=staged,
        output=output,
        platform="linux",
        target_triple="x86_64-unknown-linux-gnu",
        repository="owner/repo",
        commit="a" * 40,
        workflow_run="https://example.invalid/run/1",
    )

    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert evidence["artifact_sha256"] == evidence["desktop_staged_artifact_sha256"]
    assert evidence["commit"] == "a" * 40
    staged.write_bytes(b"substituted")
    with pytest.raises(ValueError, match="staging changed"):
        write_evidence(
            repo_root=tmp_path,
            runtime=runtime,
            staged_runtime=staged,
            output=output,
            platform="linux",
            target_triple="x86_64-unknown-linux-gnu",
            repository="owner/repo",
            commit="a" * 40,
            workflow_run="https://example.invalid/run/1",
        )
