"""Keep Ace's production threat boundaries and control owners explicit."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
THREAT_MODEL = ROOT / "docs" / "security" / "threat-model-and-responsibility-matrix.md"

REQUIRED_ACTORS = {
    "TA-01",
    "TA-02",
    "TA-03",
    "TA-04",
    "TA-05",
    "TA-06",
    "TA-07",
    "TA-08",
}
REQUIRED_BOUNDARIES = {
    "TB-01",
    "TB-02",
    "TB-03",
    "TB-04",
    "TB-05",
    "TB-06",
    "TB-07",
    "TB-08",
}
REQUIRED_RESPONSIBILITIES = {
    "policy decision",
    "process enforcement",
    "file enforcement",
    "network enforcement",
    "identity and tenancy",
    "plugin and content trust",
    "audit and recovery",
    "build and release",
}


def _table_ids(text: str, prefix: str) -> set[str]:
    return set(re.findall(rf"^\| ({prefix}-\d{{2}}) \|", text, flags=re.MULTILINE))


def test_threat_model_covers_production_actors_assets_and_boundaries() -> None:
    text = THREAT_MODEL.read_text(encoding="utf-8")

    assert _table_ids(text, "TA") == REQUIRED_ACTORS
    assert _table_ids(text, "TB") == REQUIRED_BOUNDARIES
    for asset in (
        "credentials and authentication state",
        "workspace and host data",
        "execution authority",
        "owner-isolated task data",
        "security policy and audit evidence",
        "runtime and release artifacts",
    ):
        assert asset in text


def test_every_boundary_names_controls_evidence_failure_and_residual_risk() -> None:
    text = THREAT_MODEL.read_text(encoding="utf-8")
    boundary_rows = [
        line
        for line in text.splitlines()
        if re.match(r"^\| TB-\d{2} \|", line)
    ]

    assert len(boundary_rows) == len(REQUIRED_BOUNDARIES)
    for row in boundary_rows:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        assert len(cells) == 11
        assert all(cells)
        assert "fail-closed" in cells[8]
        assert cells[9] != "None"
        assert cells[10] == "reviewed-automated"


def test_security_responsibility_matrix_has_no_unowned_layer() -> None:
    text = THREAT_MODEL.read_text(encoding="utf-8")
    responsibilities = {
        cells[0].lower()
        for line in text.splitlines()
        if line.startswith("| ") and not line.startswith("|---")
        if len(cells := [cell.strip() for cell in line.strip("|").split("|")]) == 5
    }

    assert REQUIRED_RESPONSIBILITIES <= responsibilities
    assert "No production security boundary may rely on a prompt" in text
