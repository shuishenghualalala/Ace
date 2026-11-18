"""Release-matrix facts that must not drift from packaging and path resolution."""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

from crew.agent import skills
from crew.agent.skills import core as skills_core


ROOT = Path(__file__).resolve().parents[1]


def test_member_wheel_cache_compares_packaged_sources_and_removes_stale_assets(tmp_path, monkeypatch):
    import importlib.metadata
    import runpy
    from pathlib import PurePosixPath

    checker = runpy.run_path(str(ROOT / "deb-package" / "sync_workspace_wheels.py"))
    member = tmp_path / "packages" / "fixture"
    sources = member / "src" / "crew" / "fixture"
    sources.mkdir(parents=True)
    (member / "pyproject.toml").write_text(
        '[project]\nname="fixture"\n[tool.hatch.build.targets.wheel]\npackages=["src/crew"]\n',
        encoding="utf-8",
    )
    installed = tmp_path / "site-packages"
    (installed / "crew" / "fixture").mkdir(parents=True)
    module = PurePosixPath("crew/fixture/worker.py")
    asset = PurePosixPath("crew/fixture/template.md")

    class InstalledWheel:
        files = [module, asset, PurePosixPath("fixture.dist-info/METADATA")]

        @staticmethod
        def locate_file(entry):
            return installed / entry

    monkeypatch.setattr(importlib.metadata, "distribution", lambda _: InstalledWheel())
    for relative in (module, asset):
        (member / "src" / relative).write_bytes(b"v1")
        (installed / relative).write_bytes(b"v1")
    assert checker["wheel_matches"](member)

    (member / "src" / module).write_bytes(b"v2")
    assert not checker["wheel_matches"](member)
    (installed / module).write_bytes(b"v2")
    (member / "src" / asset).unlink()
    assert not checker["wheel_matches"](member)

    InstalledWheel.files.remove(asset)
    assert checker["wheel_matches"](member)
    (sources / "new.md").write_bytes(b"new")
    assert not checker["wheel_matches"](member)


def test_warm_packaging_checks_member_wheels_before_staging() -> None:
    for name, cache_copy in (("pack_exe.ps1", "Copy-Item -Path $pythonCacheDir"), ("pack_mac.sh", 'cp -R "$PY_CACHE"')):
        script = (ROOT / "deb-package" / name).read_text(encoding="utf-8")
        assert script.index("sync_workspace_wheels.py") < script.index(cache_copy)


def test_optional_catalog_development_path_is_repository_root() -> None:
    assert skills.get_optional_skills_dir() == ROOT / "optional-skills"


def test_wheel_configuration_does_not_claim_optional_catalog() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    wheel = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["crew"]


def test_frozen_catalog_resolver_requires_meipass_content(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(skills_core, "_REPO_ROOT", tmp_path)

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

    # The current packages copy the whole crew tree, which contains skills,
    # scenarios, MCP servers, and presets, rather than listing each child.
    for directory in ("skills", "scenarios", "mcp_servers", "agent/subagent/presets"):
        assert (ROOT / "crew" / directory).is_dir()
    assert 'copy_tree "$ROOT_DIR/crew" "$STAGE_GW/crew"' in pack_mac
    assert 'COPY crew ./crew' in dockerfile
    assert 'COPY --from=gateway-builder /app/crew ./opt/crew-gateway/crew' in dockerfile
    assert 'Invoke-RoboCopy (Join-Path $ProjectRoot "crew")' in pack_exe
    for source in (pack_mac, dockerfile, pack_exe):
        assert "plugins" in source


def test_interactive_windows_installer_prepares_sandbox_before_launch() -> None:
    installer = (ROOT / "deb-package" / "pack_exe.ps1").read_text(encoding="utf-8")
    main = (ROOT / "desktop" / "src" / "main" / "index.ts").read_text(encoding="utf-8")
    assert "PrivilegesRequired=lowest" in installer
    assert "CurStep <> ssPostInstall" in installer
    assert "ExecAsOriginalUser(DesktopPath, '--crew-provision-windows-sandbox'" in installer
    assert "ewWaitUntilTerminated" in installer
    assert "packagedSecurityRuntimeEnv({ bundledOnly: true })" in main
    assert main.index("process.argv.includes('--crew-provision-windows-sandbox')") < main.index(
        "app.requestSingleInstanceLock()"
    )


def test_mac_pack_script_logs_app_path_with_nounset(posix_bash: str) -> None:
    script = (ROOT / "deb-package" / "pack_mac.sh").read_text(encoding="utf-8")
    log_line = next(line for line in script.splitlines() if "Electron 客户端:" in line)
    result = subprocess.run(
        [posix_bash, "-u", "-c", log_line],
        env={"APP_PATH": "/tmp/crew-desktop.app"},
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "/tmp/crew-desktop.app" in result.stdout


def test_pack_mac_supports_arm64_and_x64() -> None:
    source = (ROOT / "deb-package" / "pack_mac.sh").read_text(encoding="utf-8")

    assert "ELECTRON_ARCH" in source
    assert "aarch64-apple-darwin" in source
    assert "x86_64-apple-darwin" in source
    assert "darwin-arm64" in source
    assert "darwin-x64" in source
    assert 'DMG_NAME="crew-desktop_${VERSION}_${ARCH}.dmg"' in source


def test_mac_package_verifies_signed_runtime_from_dmg_input() -> None:
    source = (ROOT / "deb-package" / "pack_mac.sh").read_text(encoding="utf-8")

    finish_signing, dmg = source.split('DMG_APP="$STAGE_DMG/crew-desktop.app"', 1)
    sign_helper = finish_signing.index('codesign --force --sign - --timestamp=none "$APP_SECURITY_RUNTIME"')
    stage_manifest = finish_signing.index('--runtime "$APP_SECURITY_RUNTIME"')
    seal_manifest = finish_signing.index('codesign --force --sign - --timestamp=none "$APP_PATH"')
    assert sign_helper < stage_manifest < seal_manifest
    assert 'codesign --force --deep' not in finish_signing[stage_manifest:]

    verify_manifest = dmg.index('verify-security-runtime.mjs "$DMG_RESOURCES"')
    smoke_runtime = dmg.index('--platform macos --runtime "$DMG_SECURITY_RUNTIME"')
    build_dmg = dmg.index("hdiutil create")
    assert 'DMG_SECURITY_RUNTIME="$DMG_RESOURCES/ace-security-runtime"' in dmg
    assert '"$DMG_GW/runtimes/python/bin/python3" -s "$ROOT_DIR/tests/security/security_matrix.py"' in dmg
    assert verify_manifest < smoke_runtime < build_dmg


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
