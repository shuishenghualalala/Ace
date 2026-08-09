"""Tests for the cross-platform native runtime staging script."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "security-runtime" / "scripts" / "build-security-runtime.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("ace_runtime_builder", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_runtime_targets_cover_supported_platforms() -> None:
    builder = _load_script()
    assert builder.binary_name("aarch64-apple-darwin") == "ace-security-runtime"
    assert builder.binary_name("x86_64-unknown-linux-gnu") == "ace-security-runtime"
    assert builder.binary_name("x86_64-pc-windows-msvc") == "ace-security-runtime.exe"


def test_stage_writes_gateway_and_desktop_compatible_manifest(tmp_path: Path) -> None:
    builder = _load_script()
    crate = tmp_path / "security-runtime"
    (crate / "src").mkdir(parents=True)
    (crate / "tests").mkdir()
    (crate / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
    (crate / "Cargo.toml").write_text("[package]\nname='fixture'\n", encoding="utf-8")
    (crate / "Cargo.lock").write_text("# lock\n", encoding="utf-8")
    artifact = tmp_path / "ace-security-runtime"
    artifact.write_bytes(b"runtime")
    output = tmp_path / "bin"

    staged = builder.stage(crate, artifact, output, "aarch64-apple-darwin")
    manifest = json.loads((output / "runtime-manifest.json").read_text(encoding="utf-8"))

    assert staged.name == "ace-security-runtime"
    assert manifest["binary_name"] == "ace-security-runtime"
    assert manifest["binary_sha256"] == manifest["files"][0]["sha256"]
    assert manifest["source_files"] == 3
    assert manifest["built_for"] == "aarch64-apple-darwin"


def test_stage_preserves_other_platform_artifact_provenance(tmp_path: Path) -> None:
    builder = _load_script()
    crate = tmp_path / "security-runtime"
    (crate / "src").mkdir(parents=True)
    (crate / "tests").mkdir()
    (crate / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
    (crate / "Cargo.toml").write_text("[package]\nname='fixture'\n", encoding="utf-8")
    (crate / "Cargo.lock").write_text("# lock\n", encoding="utf-8")
    output = tmp_path / "bin"
    output.mkdir()
    (output / "ace-security-runtime.exe").write_bytes(b"windows-runtime")
    (output / "runtime-manifest.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "binary_name": "ace-security-runtime.exe",
                "source_hash": "windows-source",
            }
        ),
        encoding="utf-8",
    )
    artifact = tmp_path / "ace-security-runtime"
    artifact.write_bytes(b"macos-runtime")

    builder.stage(crate, artifact, output, "aarch64-apple-darwin")
    manifest = json.loads((output / "runtime-manifest.json").read_text(encoding="utf-8"))
    entries = {item["name"]: item for item in manifest["files"]}

    assert set(entries) == {"ace-security-runtime", "ace-security-runtime.exe"}
    assert entries["ace-security-runtime"]["source_hash"] == manifest["source_hash"]
    assert entries["ace-security-runtime.exe"]["source_hash"] == "windows-source"
