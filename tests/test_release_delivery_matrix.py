"""Release-matrix facts that must not drift from packaging and path resolution."""

from __future__ import annotations

import tomllib
from pathlib import Path

from crew.agent import skills


ROOT = Path(__file__).resolve().parents[1]


def test_optional_catalog_development_path_is_repository_root() -> None:
    assert skills.get_optional_skills_dir() == ROOT / "optional-skills"


def test_wheel_configuration_does_not_claim_optional_catalog() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    wheel = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["crew"]


def test_frozen_catalog_resolver_requires_meipass_content(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(skills, "_REPO_ROOT", tmp_path)

    assert skills.get_optional_skills_dir() == tmp_path / "optional-skills"
    assert not skills.get_optional_skills_dir().exists()

    (tmp_path / "optional-skills").mkdir()
    assert skills.get_optional_skills_dir().is_dir()


def test_pack_scripts_exclude_web_frontend() -> None:
    pack_mac = (ROOT / "deb-package" / "pack_mac.sh").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile.pack").read_text(encoding="utf-8")
    pack_exe = (ROOT / "deb-package" / "pack_exe.ps1").read_text(encoding="utf-8")

    assert "web/dist" not in pack_mac
    assert "web 前端构建" not in pack_mac
    assert "web/dist" not in dockerfile
    assert "COPY web" not in dockerfile
    assert "cd web" not in dockerfile
    assert "web\\dist" not in pack_exe
    assert "Push-Location web" not in pack_exe

    for source in (pack_mac, dockerfile, pack_exe):
        assert "crew/skills" in source or "crew\\skills" in source
        assert "crew/scenarios" in source or "crew\\scenarios" in source
        assert "crew/mcp_servers" in source or "crew\\mcp_servers" in source
        assert "presets" in source
        assert "plugins" in source


def test_pack_mac_supports_arm64_and_x64() -> None:
    source = (ROOT / "deb-package" / "pack_mac.sh").read_text(encoding="utf-8")

    assert "ELECTRON_ARCH" in source
    assert "aarch64-apple-darwin" in source
    assert "x86_64-apple-darwin" in source
    assert "darwin-arm64" in source
    assert "darwin-x64" in source
    assert 'DMG_NAME="crew-desktop_${VERSION}_${ARCH}.dmg"' in source


def test_release_desktop_workflow_covers_all_platforms() -> None:
    source = (ROOT / ".github" / "workflows" / "release-desktop.yml").read_text(
        encoding="utf-8"
    )

    assert "workflow_dispatch" in source
    assert "push:" in source
    assert "tags:" in source
    assert '"v*"' in source
    assert "macos-15" in source
    assert "macos-15-intel" in source
    assert "ubuntu-24.04" in source
    assert "windows-2025" in source
    assert "pack_mac.sh" in source
    assert "pack_deb.ps1" in source
    assert "pack_exe.ps1" in source
    assert "SHA256SUMS" in source
    assert "gh release upload" in source
