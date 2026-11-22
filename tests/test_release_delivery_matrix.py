"""Release-matrix facts that must not drift from packaging and path resolution."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

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
    for name, cache_copy in (("pack_exe.ps1", "Invoke-RoboCopy $pythonCacheDir $pythonRtDir"), ("pack_mac.sh", 'cp -R "$PY_CACHE"')):
        script = (ROOT / "deb-package" / name).read_text(encoding="utf-8")
        assert script.index("sync_workspace_wheels.py") < script.index(cache_copy)


def test_windows_pack_script_declares_utf8_for_windows_powershell() -> None:
    script = (ROOT / "deb-package" / "pack_exe.ps1").read_bytes()
    assert script.startswith(b"\xef\xbb\xbf")


def test_windows_runtime_archives_pin_sha256() -> None:
    script = (ROOT / "deb-package" / "pack_exe.ps1").read_text(encoding="utf-8-sig")
    assert '$BundledPythonSha256 = "368474c69f476e7de4adaf50b61d9fcf6ec8b4db88cc43c5f71c860b3cd29c69"' in script
    assert '$BundledNodeSha256 = "11d483dfba711bc7c9bcb513e80a2941be0c2e7cbf62753755785b9a6e80a731"' in script


def test_windows_python_archive_verifies_sha256_and_recovers(tmp_path: Path) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    windows_tar = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "tar.exe"
    tar = str(windows_tar) if windows_tar.is_file() else shutil.which("tar")
    if not powershell or not tar:
        pytest.skip("PowerShell and tar are required")

    script = (ROOT / "deb-package" / "pack_exe.ps1").read_text(encoding="utf-8-sig")
    functions = "function Test-TarGzArchive" + script.split("function Test-TarGzArchive", 1)[1].split(
        "\nif (-not (Test-Path $pythonCacheExe))", 1
    )[0]
    download_loop = "    $pyDownloaded = $false" + script.split("    $pyDownloaded = $false", 1)[1].split(
        "    $tempExtract =", 1
    )[0]
    harness = tmp_path / "verify-archive.ps1"
    harness.write_text(
        "param([string]$Valid, [string]$Invalid, [string]$Unexpected, [string]$TarExe)\n"
        "$ErrorActionPreference = 'Stop'\n"
        "function Invoke-WebRequest([string]$Uri, [string]$OutFile, [switch]$UseBasicParsing, [int]$TimeoutSec) {\n"
        "    Copy-Item -LiteralPath $Uri -Destination $OutFile -Force\n"
        "}\n"
        + functions
        + "\nif (-not (Test-TarGzArchive $Valid $TarExe)) { throw 'valid archive rejected' }\n"
        + "if (Test-TarGzArchive $Invalid $TarExe) { throw 'truncated archive accepted' }\n"
        + "if (Test-TarGzArchive \"$Invalid.missing\" $TarExe) { throw 'missing archive accepted' }\n"
        + "$BundledPythonSha256 = (Get-FileHash $Valid).Hash\n"
        + "$pyTar = Join-Path $PSScriptRoot 'download.tar.gz'\n"
        + "$pyUrls = @($Invalid, $Unexpected, $Valid)\n"
        + download_loop
        + "if ((Get-FileHash $pyTar).Hash -ne (Get-FileHash $Valid).Hash) { throw 'valid retry not retained' }\n",
        encoding="utf-8-sig",
    )
    payload = tmp_path / "python.exe"
    payload.write_bytes(b"fixture")
    valid = tmp_path / "python.tar.gz"
    with tarfile.open(valid, "w:gz") as archive:
        archive.add(payload, arcname="python/python.exe")
    invalid = tmp_path / "truncated.tar.gz"
    invalid.write_bytes(valid.read_bytes()[: valid.stat().st_size // 2])
    unexpected = tmp_path / "unexpected.tar.gz"
    payload.write_bytes(b"unexpected fixture")
    with tarfile.open(unexpected, "w:gz") as archive:
        archive.add(payload, arcname="python/python.exe")

    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(harness),
            str(valid),
            str(invalid),
            str(unexpected),
            tar,
        ],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert script.index("Test-FileSha256 $pyTar $BundledPythonSha256") < script.index("Test-TarGzArchive $pyTar $tarExe")


def test_windows_node_download_verifies_sha256_and_recovers(tmp_path: Path) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        pytest.skip("PowerShell is required")

    script = (ROOT / "deb-package" / "pack_exe.ps1").read_text(encoding="utf-8-sig")
    hash_function = "function Test-FileSha256" + script.split("function Test-FileSha256", 1)[1].split("\n}\n", 1)[0]
    download_loop = "    $nodeDownloaded = $false" + script.split("    $nodeDownloaded = $false", 1)[1].split(
        "    Move-Item -Path $innerDir.FullName", 1
    )[0]
    harness = tmp_path / "download-node.ps1"
    harness.write_text(
        "param([string]$Valid, [string]$Invalid, [string]$Unexpected)\n"
        "$ErrorActionPreference = 'Stop'\n"
        "function Invoke-WebRequest([string]$Uri, [string]$OutFile, [switch]$UseBasicParsing, [int]$TimeoutSec) {\n"
        "    Copy-Item -LiteralPath $Uri -Destination $OutFile -Force\n"
        "}\n"
        + hash_function
        + "\n}\n"
        + "$BundledNodeSha256 = (Get-FileHash $Valid).Hash\n"
        + "$nodeZipPath = Join-Path $PSScriptRoot 'download.zip'\n"
        + "$tempExtract = Join-Path $PSScriptRoot 'extract'\n"
        + "$nodeZipUrls = @($Invalid, $Unexpected, $Valid)\n"
        + download_loop
        + "if (-not (Test-Path (Join-Path $innerDir.FullName 'node.exe'))) { throw 'valid retry not extracted' }\n",
        encoding="utf-8-sig",
    )
    valid = tmp_path / "node.zip"
    with zipfile.ZipFile(valid, "w") as archive:
        archive.writestr("node-v20.18.3-win-x64/node.exe", b"node fixture")
    invalid = tmp_path / "truncated.zip"
    invalid.write_bytes(valid.read_bytes()[: valid.stat().st_size // 2])
    unexpected = tmp_path / "unexpected.zip"
    with zipfile.ZipFile(unexpected, "w") as archive:
        archive.writestr("node-v20.18.3-win-x64/node.exe", b"unexpected fixture")

    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-File", str(harness), str(valid), str(invalid), str(unexpected)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (tmp_path / "download.zip").exists()
    assert (tmp_path / "extract/node-v20.18.3-win-x64/node.exe").read_bytes() == b"node fixture"
    assert "https://npmmirror.com/mirrors/node/" in script
    assert "Invoke-WebRequest -Uri $u -OutFile $nodeZipPath -UseBasicParsing -TimeoutSec 60" in script
    assert script.index("Test-FileSha256 $nodeZipPath $BundledNodeSha256") < script.index("Expand-Archive -Path $nodeZipPath")


def test_windows_runtime_copy_supports_deep_paths(tmp_path: Path) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell or not shutil.which("robocopy"):
        pytest.skip("PowerShell and robocopy are required")

    script = (ROOT / "deb-package" / "pack_exe.ps1").read_text(encoding="utf-8-sig")
    functions = "function Invoke-RoboCopy" + script.split("function Invoke-RoboCopy", 1)[1].split(
        "\n$stage =", 1
    )[0]
    harness = tmp_path / "copy-runtime.ps1"
    harness.write_text(
        "param([string]$Source, [string]$Destination, [switch]$Remove)\n"
        "$ErrorActionPreference = 'Stop'\n"
        + functions
        + "\nif ($Remove) {\n"
        "    Remove-RoboCopyTree $Destination\n"
        "    if (Test-Path -LiteralPath $Destination) { throw 'runtime tree survived removal' }\n"
        "    exit 0\n"
        "}\n"
        "Invoke-RoboCopy $Source $Destination @() @()\n",
        encoding="utf-8-sig",
    )
    relative = Path("Lib/site-packages/onnxruntime/tools") / Path(*["segment"] * 18) / "RuntimeOptimizationRecordContainerEntry.py"
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source_file = source / relative
    extended_source_file = Path("\\\\?\\" + str(source_file)) if os.name == "nt" else source_file
    extended_source_file.parent.mkdir(parents=True)
    extended_source_file.write_bytes(b"runtime fixture")
    destination_file = destination / relative
    assert len(str(source_file)) > 260
    assert len(str(destination_file)) > 260

    command = [powershell, "-NoProfile", "-NonInteractive", "-File", str(harness), str(source), str(destination)]
    result = subprocess.run(
        command,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    extended_destination_file = Path("\\\\?\\" + str(destination_file)) if os.name == "nt" else destination_file
    assert extended_destination_file.read_bytes() == b"runtime fixture"
    removal = subprocess.run(
        [*command, "-Remove"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert removal.returncode == 0, removal.stdout + removal.stderr
    assert not destination.exists()
    assert "Invoke-RoboCopy $pythonCacheDir $pythonRtDir @() @()" in script
    assert "Invoke-RoboCopy $nodeCacheDir $nodeRtDir @() @()" in script
    assert "Remove-RoboCopyTree $depPath" in script


def test_optional_catalog_development_path_is_repository_root() -> None:
    assert skills.get_optional_skills_dir() == ROOT / "optional-skills"


def test_windows_compiler_and_chinese_translation_share_a_release() -> None:
    import re

    workflow = (ROOT / ".github/workflows/release-desktop.yml").read_text(encoding="utf-8")
    pinned = re.search(r"choco install innosetup --version=(\d+\.\d+\.\d+)", workflow)
    assert pinned is not None
    tag = "is-" + pinned.group(1).replace(".", "_")
    assert f"/issrc/{tag}/Files/Languages/Unofficial/ChineseSimplified.isl" in workflow
    assert re.search(r"Get-FileHash -LiteralPath \$language\).Hash -ne '[A-F0-9]{64}'", workflow)


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


def test_linux_runtime_archives_verify_sha256_before_extract() -> None:
    source = (ROOT / "Dockerfile.pack").read_text(encoding="utf-8")
    cases = (
        (
            "78b1c16a9fd032997ba92a60f46a64f795cd18ff335659dfdf6096df277b24d5",
            "/tmp/python.tar.gz",
            "tar -xzf /tmp/python.tar.gz",
        ),
        (
            "595bcc9a28e6d1ee5fc7277b5c3cb029275b98ec0524e162a0c566c992a7ee5c",
            "/tmp/node.tar.xz",
            "tar -xf /tmp/node.tar.xz -C /tmp/runtimes/node",
        ),
        (
            "c1bfeecf1d7404fa74728f9db72e697decbd8119ccc6f5a294d795756dfcfca7",
            "/tmp/node.tar.xz",
            "tar -xf /tmp/node.tar.xz -C /usr/local",
        ),
    )
    for digest, archive, extract in cases:
        check = f'echo "{digest}  {archive}" | sha256sum -c -'
        assert check in source
        assert source.index(check) < source.index(extract)


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


def test_archives_exclude_runtime_model_trace() -> None:
    windows = (ROOT / "deb-package/pack_exe.ps1").read_text(encoding="utf-8")
    macos = (ROOT / "deb-package/pack_mac.sh").read_text(encoding="utf-8")
    assert r'Excludes: "crew-gateway\.crew\logs\llm.jsonl"' in windows
    assert macos.index('if [ -e "$DMG_GW/.crew/logs/llm.jsonl" ]') < macos.index("hdiutil create")


def test_interactive_windows_installer_prepares_sandbox_before_launch() -> None:
    installer = (ROOT / "deb-package" / "pack_exe.ps1").read_text(encoding="utf-8")
    main = (ROOT / "desktop" / "src" / "main" / "index.ts").read_text(encoding="utf-8")
    assert "PrivilegesRequired=lowest" in installer
    assert "CurStep <> ssPostInstall) or WizardSilent" in installer
    postinstall = next(line for line in installer.splitlines() if 'Description: "{cm:LaunchProgram,Crew}"' in line)
    assert "skipifsilent" in postinstall.split("Flags:", 1)[1].split()
    assert "ExecAsOriginalUser(DesktopPath, '--crew-provision-windows-sandbox'" in installer
    assert "ewWaitUntilTerminated" in installer
    assert "packagedSecurityRuntimeEnv({ bundledOnly: true })" in main
    assert main.index("process.argv.includes('--crew-provision-windows-sandbox')") < main.index(
        "app.requestSingleInstanceLock()"
    )


def test_mac_runtime_archives_pin_sha256_by_architecture() -> None:
    source = (ROOT / "deb-package" / "pack_mac.sh").read_text(encoding="utf-8")
    arm64 = source.split("arm64 | aarch64)", 1)[1].split("x64 | x86_64 | amd64)", 1)[0]
    x64 = source.split("x64 | x86_64 | amd64)", 1)[1].split("*)", 1)[0]
    assert 'PYTHON_SHA256="7af7058f7c268b4d87ed7e08c2c7844ef8460863b3e679db3afdce8bb1eedfae"' in arm64
    assert 'NODE_SHA256="1f15b7ed18a580af31cf32bc126572292d820f547bf55bf9cdce08041a24e1d9"' in arm64
    assert 'PYTHON_SHA256="9afd734f63a23783cf0257bef25c9231ffc80e7747486dc54cf72f325213fd15"' in x64
    assert 'NODE_SHA256="ba668f64df9239843fefcef095ee539f5ac5aa1b0fc15a71f1ecca16abedec7a"' in x64


def test_mac_runtime_checksum_gate_rejects_mismatch(tmp_path: Path, posix_bash: str) -> None:
    source = (ROOT / "deb-package" / "pack_mac.sh").read_text(encoding="utf-8")
    function = "verify_sha256() {" + source.split("verify_sha256() {", 1)[1].split("\n}\n", 1)[0]
    harness = tmp_path / "verify-runtime.sh"
    harness.write_text(
        "set -euo pipefail\n"
        "if ! command -v shasum >/dev/null; then\n"
        "    shasum() {\n"
        "        [ \"$1\" = '-a' ] && [ \"$2\" = '256' ] || return 2\n"
        "        shift 2\n"
        "        sha256sum \"$@\"\n"
        "    }\n"
        "fi\n"
        + function
        + "\n}\nverify_sha256 \"$1\" \"$2\" \"runtime fixture\"\n",
        encoding="utf-8",
    )
    valid = tmp_path / "valid archive"
    valid.write_bytes(b"fixture")
    expected = hashlib.sha256(valid.read_bytes()).hexdigest()
    accepted = subprocess.run(
        [posix_bash, str(harness), valid.as_posix(), expected],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr

    invalid = tmp_path / "invalid archive"
    invalid.write_bytes(b"unexpected")
    rejected = subprocess.run(
        [posix_bash, str(harness), invalid.as_posix(), expected],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert rejected.returncode != 0
    assert "SHA-256" in rejected.stderr
    assert source.index('verify_sha256 "$PY_TAR"') < source.index('tar -xzf "$PY_TAR"')
    assert source.index('verify_sha256 "$NODE_TAR"') < source.index('tar -xzf "$NODE_TAR"')


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


def test_release_assets_require_packaged_desktop_smoke() -> None:
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/release-desktop.yml").read_text(encoding="utf-8"))
    for platform in ("mac-arm64", "mac-x64", "linux", "windows"):
        steps = workflow["jobs"][platform]["steps"]
        smoke = next(i for i, step in enumerate(steps) if "scripts/smoke-packaged-desktop.mjs" in step.get("run", ""))
        upload = next(i for i, step in enumerate(steps) if step.get("uses", "").startswith("actions/upload-artifact"))
        assert smoke < upload
        assert not steps[smoke].get("continue-on-error")
        command = steps[smoke]["run"]
        if platform.startswith("mac-"):
            assert "hdiutil attach" in command and "-readonly" in command
        elif platform == "linux":
            assert "for distro in uos kylin" in command and "dpkg-deb -x" in command
            assert "mount -o remount,bind,ro /opt" in command
        else:
            assert "dist/Crew_Setup_v$env:VERSION.exe" in command
            assert "/VERYSILENT" in command and "/DIR=" in command and "$env:RUNNER_TEMP" in command
            assert "unins000.dat" in command
            assert command.index("Start-Process") < command.index("node desktop/scripts/smoke-packaged-desktop.mjs $desktop.FullName")


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
