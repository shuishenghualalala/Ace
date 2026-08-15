"""Execute the malicious-HTML suite for the bundled PDF renderer."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "crew" / "skills" / "html-to-pdf"
NODE_TESTS = tuple(sorted((SKILL / "tests").glob("*.test.cjs")))


def _node_environment() -> dict[str, str]:
    """Do not leak developer credentials into the test runner."""
    allowed = {
        "COMSPEC",
        "HOME",
        "LANG",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
    }
    return {name: value for name, value in os.environ.items() if name.upper() in allowed}


def test_malicious_html_suite() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.fail("Node.js is required to run the HTML-to-PDF security suite")

    completed = subprocess.run(
        [str(Path(node).resolve()), "--test", *(str(path) for path in NODE_TESTS)],
        cwd=SKILL,
        env=_node_environment(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, (
        "HTML-to-PDF malicious-input suite failed\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )
