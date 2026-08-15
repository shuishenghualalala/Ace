from __future__ import annotations

import os
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

import crew.security.local_path as local_path_module
from crew.browser.driver import BrowserDriverError
from crew.browser.manager import BrowserManager
from crew.browser.types import BrowserConfig
from crew.security.local_path import (
    LocalPathReference,
    LocalPathReferenceError,
    LocalPathReferenceKind,
)

ROOT = Path(__file__).resolve().parents[2]


def test_local_path_reference_distinguishes_syntax_without_resolving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_resolve(*_args, **_kwargs):
        raise AssertionError("parse touched the filesystem")

    monkeypatch.setattr(Path, "resolve", forbidden_resolve)

    plain = LocalPathReference.parse("folder/report.txt")
    uri_text = (
        "file:///C:/workspace/report.txt"
        if os.name == "nt"
        else "file:///workspace/report.txt"
    )
    uri = LocalPathReference.parse(uri_text)

    assert plain.kind is LocalPathReferenceKind.PLAIN_PATH
    assert plain.raw == "folder/report.txt"
    assert uri.kind is LocalPathReferenceKind.FILE_URI
    assert uri.raw == uri_text
    with pytest.raises(TypeError):
        os.fspath(plain)
    with pytest.raises(TypeError):
        LocalPathReference("folder/report.txt")  # type: ignore[call-arg]
    with pytest.raises(FrozenInstanceError):
        plain._raw = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "value",
    [
        "https://example.test/file.txt",
        "file://server/share/file.txt",
        "file:///workspace/file.txt?download=1",
        "file:///workspace/file.txt?",
        "file:///workspace/file.txt#section",
        "file:///workspace/file.txt#",
        "file:///workspace/a%2fb.txt",
        "file:///workspace/a%5Cb.txt",
        "file:///workspace/a%252fb.txt",
        "file:///workspace/a%255Cb.txt",
        "file:///workspace/a%00b.txt",
        "file:///workspace/a%2500b.txt",
        "file:///workspace/a%0ab.txt",
        "file:///workspace/a%C2%80b.txt",
        "file:///workspace/%ZZ.txt",
        "file:relative.txt",
        "file:////server/share/file.txt",
        "//server/share/file.txt",
        r"\\server\share\file.txt",
        r"\\?\C:\Windows\file.txt",
        r"\\.\PIPE\ace",
        "C:relative.txt",
        "line\nbreak.txt",
        "nul\x00byte.txt",
    ],
)
def test_local_path_reference_rejects_ambiguous_or_nonlocal_inputs(
    value: str,
) -> None:
    with pytest.raises(LocalPathReferenceError):
        LocalPathReference.parse(value)


def test_local_path_reference_resolves_only_at_explicit_boundary(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    target = nested / "report name.txt"
    target.write_text("ok", encoding="utf-8")

    relative = LocalPathReference.parse("nested/report name.txt")
    uri = LocalPathReference.parse(target.as_uri())

    assert relative.resolve_at_boundary(base=tmp_path, strict=True) == target.resolve()
    assert uri.resolve_at_boundary(strict=True) == target.resolve()
    with pytest.raises(LocalPathReferenceError, match="host-owned base"):
        relative.resolve_at_boundary(strict=True)


def test_windows_specific_ambiguous_names_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(local_path_module, "os", SimpleNamespace(name="nt"))

    for value in (
        r"C:\workspace\NUL.txt",
        r"C:\workspace\file.txt:secret",
        r"C:\workspace\trailing.",
        r"\drive-relative\file.txt",
        "file:///workspace/file.txt",
        "file:///C%3A/workspace/file.txt",
    ):
        with pytest.raises(LocalPathReferenceError):
            LocalPathReference.parse(value)

    assert (
        LocalPathReference.parse("file:///C:/workspace/file.txt").kind
        is LocalPathReferenceKind.FILE_URI
    )


def test_posix_rejects_windows_drive_forms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(local_path_module, "os", SimpleNamespace(name="posix"))

    for value in ("C:/workspace/file.txt", "file:///C:/workspace/file.txt"):
        with pytest.raises(LocalPathReferenceError):
            LocalPathReference.parse(value)

    assert (
        LocalPathReference.parse("file:///workspace/file.txt").kind
        is LocalPathReferenceKind.FILE_URI
    )


def test_browser_upload_boundary_accepts_typed_file_uri_and_rejects_encoded_separator(
    tmp_path: Path,
) -> None:
    target = tmp_path / "upload.txt"
    target.write_text("payload", encoding="utf-8")
    manager = BrowserManager(BrowserConfig(), object())  # type: ignore[arg-type]

    assert manager._resolved_upload_paths(
        "owner",
        [target.as_uri()],
        workdir=str(tmp_path),
    ) == [str(target.resolve())]
    with pytest.raises(BrowserDriverError, match="不存在|不可读取"):
        manager._resolved_upload_paths(
            "owner",
            [target.as_uri().replace("upload.txt", "folder%2fupload.txt")],
            workdir=str(tmp_path),
        )


def test_model_reachable_file_browser_and_wiki_surfaces_use_typed_references() -> None:
    guard = (ROOT / "crew" / "tools" / "security_guard.py").read_text(encoding="utf-8")
    browser = (ROOT / "crew" / "browser" / "manager.py").read_text(encoding="utf-8")
    wiki = (ROOT / "crew" / "wiki" / "tools.py").read_text(encoding="utf-8")

    assert "path_reference = LocalPathReference.parse(raw_path)" in guard
    assert "reference = LocalPathReference.parse(raw)" in browser
    assert "reference = LocalPathReference.parse(filename)" in browser
    assert "path_reference = LocalPathReference.parse(raw_path)" in wiki
