"""H-1 regression: the managed background bridge must not import ``crew`` from cwd.

The bridge launches as a host subprocess whose ``cwd`` is the task workspace. With a
naive ``python -m crew.security.background_runner`` the interpreter puts ``cwd`` on
``sys.path[0]``; a workspace-dropped ``crew/security/background_runner.py`` (and a
matching fake ``runtime_client``) would then execute on the host *before* the native
helper, the shell classifier, or the sandbox can intervene. The launcher rebuilds
``sys.path`` under ``-I`` so only the installed crew package root is trusted.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_background_bridge_ignores_workspace_fake_crew(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    fake_security = workspace / "crew" / "security"
    fake_security.mkdir(parents=True)
    (workspace / "crew" / "__init__.py").write_text("", encoding="utf-8")
    (fake_security / "__init__.py").write_text("", encoding="utf-8")
    # A harmless canary: if this module ever loads, it prints PWNED and exits 0.
    (fake_security / "background_runner.py").write_text(
        "print('PWNED', flush=True)\nraise SystemExit(0)\n",
        encoding="utf-8",
    )

    from crew.tools.process_registry import _BACKGROUND_BRIDGE_LAUNCHER

    # The real runner reads stdin; send an empty line so it returns cleanly (126)
    # without ever touching the workspace's fake package.
    proc = subprocess.run(
        [sys.executable, "-I", "-c", _BACKGROUND_BRIDGE_LAUNCHER],
        cwd=workspace,
        input="\n",
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert "PWNED" not in proc.stdout, (
        f"managed bridge loaded a workspace-dropped fake crew package "
        f"(stdout={proc.stdout!r}, stderr={proc.stderr!r})"
    )
