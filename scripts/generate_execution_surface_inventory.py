"""Render the human execution-surface inventory from its canonical JSON ledger."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INVENTORY = ROOT / "docs" / "security" / "execution-surface-inventory.json"


def load_inventory(path: Path = DEFAULT_INVENTORY) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _inline(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _links(items: list[dict[str, str]]) -> str:
    return "<br>".join(
        f"`{_inline(item['id'])}` — `{_inline(item['path'])}`" for item in items
    )


def _evidence(items: list[dict[str, str]]) -> str:
    return "<br>".join(
        f"`{_inline(item['type'])}` — `{_inline(item['path'])}`" for item in items
    )


def render_markdown(inventory: dict[str, Any]) -> str:
    surfaces = sorted(inventory["surfaces"], key=lambda item: item["id"])
    counts = Counter(category for item in surfaces for category in item["categories"])

    lines = [
        "# Ace execution-surface inventory",
        "",
        "<!-- GENERATED FILE: edit docs/security/execution-surface-inventory.json, then run",
        "     python scripts/generate_execution_surface_inventory.py -->",
        "",
        "The JSON ledger is the canonical security migration and release-gate input. Registration does",
        "not itself claim that a surface is sandboxed: `status`, the final enforcement point, and the",
        "fail-closed behavior state the current boundary. Model output and remote content remain",
        "untrusted even when a surface is listed.",
        "Primitive references are bounded review aids: tests cover the current Python network/file,",
        "JavaScript network/file/browser/IPC, and Rust network/file patterns; they do not claim",
        "complete network or callsite discovery.",
        "",
        f"- Schema version: `{inventory['schema_version']}`",
        f"- Inventory ID: `{inventory['inventory_id']}`",
        f"- Surface records: `{len(surfaces)}`",
        "- Category counts: "
        + ", ".join(f"`{name}`={counts[name]}" for name in sorted(counts)),
        "",
        "| ID | Categories | Locator | Owner | Status | Final enforcement |",
        "|---|---|---|---|---|---|",
    ]

    for item in surfaces:
        categories = ", ".join(f"`{value}`" for value in item["categories"])
        locator = f"`{item['path']}::{_inline(item['symbol_or_route'])}`"
        lines.append(
            f"| `{item['id']}` | {categories} | {locator} | "
            f"{_inline(item['owner'])} | `{item['status']}` | "
            f"{_inline(item['final_enforcement_point'])} |"
        )

    lines.extend(["", "## Control details", ""])
    for item in surfaces:
        expiry = item["exception_expiry"] or "none"
        primitives = (
            ", ".join(
                f"`{value['kind']}:{value['path']}:{_inline(value['primitive'])}`"
                for value in item["primitive_refs"]
            )
            or "none"
        )
        routes = (
            ", ".join(
                f"`{value['path']}:{_inline(value['route'])}`"
                for value in item["covered_routes"]
            )
            or "none"
        )
        lines.extend(
            [
                f"### {item['id']}",
                "",
                f"- Locator: `{item['path']}::{_inline(item['symbol_or_route'])}`",
                f"- Trust source: {_inline(item['trust_source'])}",
                f"- Fail closed: {_inline(item['fail_closed_behavior'])}",
                f"- Lifecycle/revocation owner: {_inline(item['lifecycle_revocation_owner'])}",
                f"- Tests: {_links(item['tests'])}",
                f"- Evidence: {_evidence(item['evidence'])}",
                f"- Artifact references: {_evidence(item['artifact_refs'])}",
                f"- Reviewed primitive references: {primitives}",
                f"- Covered routes/channels: {routes}",
                f"- Exception expiry: `{expiry}`",
                f"- Review deadline: `{_inline(item['review_deadline'])}`",
                f"- Review trigger: {_inline(item['review_trigger'])}",
                "",
            ]
        )

    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    inventory = load_inventory(args.inventory)
    target = ROOT / inventory["summary_path"]
    rendered = render_markdown(inventory)
    if args.check:
        if not target.exists() or target.read_text(encoding="utf-8") != rendered:
            raise SystemExit(
                f"{target.relative_to(ROOT).as_posix()} is stale; regenerate it from the JSON ledger"
            )
        return 0
    target.write_text(rendered, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
