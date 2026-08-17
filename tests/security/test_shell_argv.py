from __future__ import annotations

import os
from pathlib import Path

import pytest

import crew.security.launch as launch_module


@pytest.mark.skipif(os.name != "nt", reason="Windows shell selection")
def test_shell_argv_skips_inaccessible_pwsh_shim(monkeypatch: pytest.MonkeyPatch) -> None:
    windows_apps_pwsh = (
        Path(os.environ["LOCALAPPDATA"])
        / "Microsoft"
        / "WindowsApps"
        / "pwsh.exe"
    )
    windows_powershell = (
        Path(os.environ["WINDIR"])
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )

    def fake_which(name: str) -> str | None:
        if name == "pwsh":
            return str(windows_apps_pwsh)
        if name == "powershell":
            return str(windows_powershell)
        return None

    monkeypatch.setattr(launch_module.shutil, "which", fake_which)

    argv = launch_module.shell_argv("Get-Location")

    assert Path(argv[0]) == windows_powershell.resolve()
