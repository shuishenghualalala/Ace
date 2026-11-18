"""Refresh changed first-party wheels in a packaged Python dependency cache."""

from __future__ import annotations

import argparse
from importlib import metadata
from pathlib import Path
import subprocess
import sys
import tomllib


def _shipped_file(path: Path) -> bool:
    return (
        "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
        and path.name != ".DS_Store"
    )


def wheel_matches(member: Path) -> bool:
    config = tomllib.loads((member / "pyproject.toml").read_text(encoding="utf-8"))
    try:
        dist = metadata.distribution(config["project"]["name"])
    except metadata.PackageNotFoundError:
        return False
    included = [member / p for p in config["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]]
    sources = {
        file.relative_to(root.parent).as_posix(): file
        for root in included
        for file in root.rglob("*")
        if file.is_file() and _shipped_file(file)
    }
    roots = {root.name for root in included}
    installed = {
        entry.as_posix(): Path(dist.locate_file(entry))
        for entry in dist.files or ()
        if entry.parts[0] in roots and _shipped_file(entry)
    }
    return sources.keys() == installed.keys() and all(
        installed[name].is_file() and source.read_bytes() == installed[name].read_bytes()
        for name, source in sources.items()
    )


def sync(root: Path, *, check: bool = False) -> None:
    members = sorted(p.parent for p in (root / "packages").glob("*/pyproject.toml"))
    changed = [member for member in members if not wheel_matches(member)]
    if not changed:
        return
    if not check:
        subprocess.run(
            [sys.executable, "-I", "-m", "pip", "install", "--no-deps", "--force-reinstall",
             "--no-warn-script-location", *(str(member) for member in changed)],
            check=True,
        )
        changed = [member for member in changed if not wheel_matches(member)]
    if changed:
        raise SystemExit("Installed workspace wheels differ from source: " + ", ".join(member.name for member in changed))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--check", action="store_true", help="verify without installing")
    args = parser.parse_args()
    sync(args.root.resolve(), check=args.check)
