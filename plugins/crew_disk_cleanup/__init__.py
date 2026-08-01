"""Safe Crew temp-file cleanup plugin.

It only scans Crew-owned temporary locations and never deletes project source files.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from crew.state.home import get_crew_home

_ACTIONS = ["status", "dry_run", "quick"]

SCHEMA = {
    "name": "crew_disk_cleanup",
    "description": "Show or clean Crew temp files under .crew/tmp and /tmp/crew-*.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": _ACTIONS,
                "description": "status/dry_run only report candidates; quick deletes safe temp candidates.",
            },
            "older_than_hours": {
                "type": "number",
                "description": "Only include files older than this many hours. Default 24.",
            },
        },
        "required": ["action"],
    },
}


def _safe_roots() -> list[Path]:
    tmp = Path(tempfile.gettempdir())
    roots = [get_crew_home() / "tmp"]
    roots.extend(p for p in tmp.glob("crew-*") if p.exists())
    return roots


def _is_safe(path: Path, roots: list[Path]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except (OSError, ValueError):
            continue
    return False


def _collect(older_than_hours: float) -> list[dict[str, Any]]:
    import time

    roots = _safe_roots()
    cutoff = time.time() - older_than_hours * 3600
    items: list[dict[str, Any]] = []
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not _is_safe(path, roots) or not path.exists():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime > cutoff:
                continue
            if path.is_file():
                items.append({"path": str(path), "kind": "file", "size": stat.st_size})
            elif path.is_dir() and not any(path.iterdir()):
                items.append({"path": str(path), "kind": "empty_dir", "size": 0})
    return items


def _delete(items: list[dict[str, Any]]) -> dict[str, Any]:
    deleted = 0
    freed = 0
    errors: list[dict[str, str]] = []
    roots = _safe_roots()
    for item in sorted(items, key=lambda x: x["path"], reverse=True):
        path = Path(str(item["path"]))
        if not _is_safe(path, roots):
            continue
        try:
            if path.is_file():
                freed += path.stat().st_size
                path.unlink()
                deleted += 1
            elif path.is_dir() and not any(path.iterdir()):
                shutil.rmtree(path)
                deleted += 1
        except OSError as exc:
            errors.append({"path": str(path), "error": str(exc)})
    return {"deleted": deleted, "freed_bytes": freed, "errors": errors}


def handle_cleanup(args: dict[str, Any], **_: Any) -> str:
    action = str(args.get("action") or "status")
    if action not in _ACTIONS:
        return json.dumps({"error": f"unknown action: {action}", "allowed": _ACTIONS}, ensure_ascii=False)
    older_than_hours = 24.0
    if args.get("older_than_hours") is not None:
        older_than_hours = float(args["older_than_hours"])
    items = _collect(older_than_hours)
    payload: dict[str, Any] = {
        "action": action,
        "roots": [str(p) for p in _safe_roots()],
        "older_than_hours": older_than_hours,
        "candidate_count": len(items),
        "candidate_bytes": sum(int(i["size"]) for i in items),
        "candidates": items[:50],
    }
    if action == "quick":
        payload["cleanup"] = _delete(items)
    return json.dumps(payload, ensure_ascii=False)


def register(ctx) -> None:
    ctx.register_tool(
        name="crew_disk_cleanup",
        toolset="maintenance",
        schema=SCHEMA,
        handler=handle_cleanup,
        description="Show or clean Crew temp files.",
    )
