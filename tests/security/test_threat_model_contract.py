"""Executable schema checks for the production threat-model ownership contract."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
THREAT_MODEL = ROOT / "docs" / "security" / "threat-model-and-responsibility-matrix.md"


def _table_rows(text: str, prefix: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in text.splitlines():
        if not line.startswith(f"| {prefix}"):
            continue
        rows.append([cell.strip() for cell in line.strip("|").split("|")])
    return rows


def test_threat_model_has_closed_actor_boundary_and_owner_schema() -> None:
    text = THREAT_MODEL.read_text(encoding="utf-8")
    actors = _table_rows(text, "TA-")
    boundaries = _table_rows(text, "TB-")
    owners = [
        [cell.strip() for cell in line.strip("|").split("|")]
        for line in text.splitlines()
        if line.startswith("| ")
        and line.count("|") == 6
        and line.split("|", 2)[1].strip()
        in {
            "policy decision",
            "process enforcement",
            "file enforcement",
            "network enforcement",
            "identity and tenancy",
            "plugin and content trust",
            "audit and recovery",
            "build and release",
        }
    ]

    assert [row[0] for row in actors] == [f"TA-{index:02d}" for index in range(1, 9)]
    assert [row[0] for row in boundaries] == [f"TB-{index:02d}" for index in range(1, 9)]
    assert len(owners) == 8
    assert all(len(row) == 4 and all(row) for row in actors)
    assert all(len(row) == 11 and all(row) for row in boundaries)
    assert all(len(row) == 5 and all(row) for row in owners)

    referenced_actors = {
        actor for row in boundaries for actor in re.findall(r"\bTA-\d{2}\b", row[2])
    }
    assert referenced_actors == {row[0] for row in actors}

    owner_layers = {row[0] for row in owners}
    assert all(row[4] in owner_layers for row in boundaries)
    assert all(row[10] == "reviewed-automated" for row in boundaries)
    for row in boundaries:
        for cell_index in (5, 7):
            references = re.findall(r"`([^`]+)`", row[cell_index])
            assert references, (row[0], cell_index)
            for reference in references:
                assert (ROOT / reference).exists(), (row[0], reference)
    assert "not human signoff" in text


def test_threat_model_references_existing_primary_security_sources() -> None:
    text = THREAT_MODEL.read_text(encoding="utf-8")
    required_sources = {
        "crew/security/service.py",
        "crew/security/snapshot.py",
        "crew/security/launch.py",
        "crew/security/runtime_client.py",
        "crew/tools/file_utils.py",
        "security-runtime/src",
        "execution-surface-inventory.md",
    }
    assert all(source in text for source in required_sources)
    for source in required_sources - {"execution-surface-inventory.md"}:
        assert (ROOT / source).exists(), source
    assert (ROOT / "docs" / "security" / "execution-surface-inventory.md").is_file()
