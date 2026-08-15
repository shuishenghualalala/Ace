"""Static and behavioral proofs for narrow CAP/FS file surfaces.

These tests intentionally fail if a new ingress or execution primitive is
introduced into the file/archive surfaces without a corresponding runtime
policy and adversarial tests.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
FILE_SURFACES = (
    ROOT / "crew" / "tools" / "file_utils.py",
    ROOT / "crew" / "tools" / "file_tools.py",
    ROOT / "crew" / "wiki" / "archive_security.py",
)


def _trees(paths: tuple[Path, ...] = FILE_SURFACES) -> list[ast.AST]:
    return [ast.parse(path.read_text(encoding="utf-8"), filename=str(path)) for path in paths]


def _import_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def _called_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            names.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


def _direct_calls(tree: ast.AST) -> set[str]:
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def test_fs018_archives_never_become_code_or_external_commands() -> None:
    """Archive members are data only; no evaluator or external extractor exists."""

    archive = _trees((ROOT / "crew" / "wiki" / "archive_security.py",))[0]
    assert not (_import_roots(archive) & {"importlib", "subprocess", "tarfile"})
    assert not (_direct_calls(archive) & {"exec", "eval", "compile", "__import__"})
    assert not (_called_names(archive) & {"system", "popen"})


def test_fs015_has_no_unchecked_archive_extraction_call() -> None:
    """Only the in-process hardened ZIP extraction path is present."""

    paths = (
        ROOT / "crew" / "wiki" / "archive_security.py",
        ROOT / "crew" / "wiki" / "parser.py",
        ROOT / "crew" / "tools" / "managed_tools.py",
    )
    archive, parser, managed_tools = _trees(paths)
    assert not (_called_names(archive) & {"extractall", "extract"})
    assert "extractall" not in _called_names(parser)
    assert not (_called_names(managed_tools) & {"extractall", "extract"})


def test_arg0002_has_no_alternate_patch_executable_or_module() -> None:
    """Every model-reachable write remains behind the structured file authority."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = project.get("project", {}).get("scripts", {})
    assert scripts == {"crew": "crew.cli.entrypoint:main"}
    assert importlib.util.find_spec("crew.apply_patch") is None
    assert importlib.util.find_spec("crew.applypatch") is None

    forbidden_names = {
        "apply-patch",
        "apply-patch.exe",
        "apply_patch.py",
        "applypatch",
        "applypatch.exe",
        "applypatch.py",
    }
    entry_roots = (ROOT, ROOT / "scripts", ROOT / "crew" / "cli")
    discovered = {
        path.name.casefold()
        for entry_root in entry_roots
        for path in entry_root.iterdir()
        if path.is_file()
    }
    assert discovered.isdisjoint(forbidden_names)


def test_patch001_structured_writer_is_the_only_patch_equivalent() -> None:
    """The Ace write-equivalent stays behind the same identity-bound primitive."""
    builtin = (ROOT / "crew" / "tools" / "builtin.py").read_text(encoding="utf-8")
    assert "authorize_file_tool(" in builtin
    assert "atomic_replace_bytes(" in builtin
    test_arg0002_has_no_alternate_patch_executable_or_module()


@pytest.mark.parametrize(
    "uri",
    [
        "file:///workspace/a%2fb.html",
        "file:///workspace/a%5Cb.html",
        "file:///workspace/a%252fb.html",
        "file:///workspace/%ZZ.html",
        "file://server/share/file.html",
    ],
)
def test_fs022_file_uri_encoded_separators_fail_closed(uri: str) -> None:
    from crew.tools.file_utils import decode_local_file_uri

    with pytest.raises(ValueError, match="URI|编码|分隔符"):
        decode_local_file_uri(uri)


def test_fs022_browser_artifact_carries_typed_path_reference() -> None:
    source = (ROOT / "crew" / "gateway" / "routers" / "browser.py").read_text(encoding="utf-8")
    assert "LocalPathReference.parse(raw_path)" in source
    assert "artifact_path=path_reference" in source
    assert "Path(raw_path)" not in source


def test_fs022_file_uri_decodes_non_separator_escape_once() -> None:
    from crew.tools.file_utils import decode_local_file_uri

    uri = (
        "file:///C:/workspace/a%2520b.html" if os.name == "nt" else "file:///workspace/a%2520b.html"
    )
    decoded = decode_local_file_uri(uri)
    assert Path(decoded).name == "a%20b.html"
