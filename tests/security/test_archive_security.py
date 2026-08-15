from __future__ import annotations

import io
import os
import stat
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

from crew.wiki.archive_security import (
    ArchiveSecurityError,
    ZipExtractionLimits,
    safe_extract_zip_bytes,
    validate_zip_bytes,
)


def _zip_bytes(entries: list[tuple[str | zipfile.ZipInfo, bytes]]) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return payload.getvalue()


@pytest.mark.parametrize(
    "member_name",
    [
        "../escape.txt",
        "/absolute.txt",
        r"C:\drive.txt",
        "C:/drive.txt",
        r"\\server\share\escape.txt",
        "//server/share/escape.txt",
    ],
)
def test_safe_zip_rejects_traversal_absolute_drive_and_unc_paths(
    tmp_path: Path,
    member_name: str,
) -> None:
    destination = tmp_path / "extract"
    outside = tmp_path / "escape.txt"

    with pytest.raises(ArchiveSecurityError, match="路径"):
        safe_extract_zip_bytes(
            _zip_bytes([(member_name, b"owned")]),
            destination,
        )

    assert not outside.exists()


def test_safe_zip_rejects_symlink_and_special_entries(tmp_path: Path) -> None:
    symlink = zipfile.ZipInfo("linked.txt")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    fifo = zipfile.ZipInfo("pipe")
    fifo.create_system = 3
    fifo.external_attr = (stat.S_IFIFO | 0o600) << 16

    for info in (symlink, fifo):
        with pytest.raises(ArchiveSecurityError, match="链接|特殊"):
            safe_extract_zip_bytes(
                _zip_bytes([(info, b"target")]),
                tmp_path / info.filename,
            )


def test_zip_preflight_enforces_entry_depth_size_and_compression_budgets() -> None:
    with pytest.raises(ArchiveSecurityError, match="条目"):
        validate_zip_bytes(
            _zip_bytes([("one", b"1"), ("two", b"2")]),
            limits=ZipExtractionLimits(max_entries=1),
        )

    with pytest.raises(ArchiveSecurityError, match="深度"):
        validate_zip_bytes(
            _zip_bytes([("one/two/three.txt", b"x")]),
            limits=ZipExtractionLimits(max_depth=2),
        )

    with pytest.raises(ArchiveSecurityError, match="单文件|总大小"):
        validate_zip_bytes(
            _zip_bytes([("large.txt", b"x" * 32)]),
            limits=ZipExtractionLimits(max_member_bytes=16, max_total_bytes=16),
        )

    with pytest.raises(ArchiveSecurityError, match="压缩比"):
        validate_zip_bytes(
            _zip_bytes([("bomb.txt", b"\0" * 4096)]),
            limits=ZipExtractionLimits(max_compression_ratio=2.0),
        )


@pytest.mark.parametrize(
    "first,second",
    [
        ("Folder/File.txt", "folder/file.TXT"),
        ("caf\u00e9.txt", "cafe\u0301.txt"),
    ],
)
def test_zip_preflight_rejects_case_and_unicode_identity_collisions(
    first: str,
    second: str,
) -> None:
    with pytest.raises(ArchiveSecurityError, match="冲突|重复"):
        validate_zip_bytes(
            _zip_bytes([(first, b"one"), (second, b"two")]),
        )


def test_safe_zip_rejects_preexisting_link_parent(tmp_path: Path) -> None:
    destination = tmp_path / "extract"
    outside = tmp_path / "outside"
    destination.mkdir()
    outside.mkdir()
    linked = destination / "nested"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation unavailable")

    with pytest.raises((ArchiveSecurityError, OSError), match="链接|reparse|路径"):
        safe_extract_zip_bytes(
            _zip_bytes([("nested/escape.txt", b"owned")]),
            destination,
        )

    assert not (outside / "escape.txt").exists()


def test_safe_zip_extracts_regular_files_with_private_mode(tmp_path: Path) -> None:
    destination = tmp_path / "extract"

    extracted = safe_extract_zip_bytes(
        _zip_bytes([("nested/file.txt", b"safe")]),
        destination,
    )

    target = destination / "nested" / "file.txt"
    assert extracted == [target]
    assert target.read_bytes() == b"safe"
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_safe_zip_preflights_all_targets_before_publishing_files(tmp_path: Path) -> None:
    destination = tmp_path / "extract"
    destination.mkdir()
    conflict = destination / "second.txt"
    conflict.write_bytes(b"keep")

    with pytest.raises(ArchiveSecurityError, match="已存在"):
        safe_extract_zip_bytes(
            _zip_bytes(
                [
                    ("first.txt", b"must not publish"),
                    ("second.txt", b"must not replace"),
                ]
            ),
            destination,
        )

    assert not (destination / "first.txt").exists()
    assert conflict.read_bytes() == b"keep"


def test_safe_zip_sanitizes_host_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import crew.wiki.archive_security as archive_security

    def fail_publish(*_args, **_kwargs):
        raise OSError("SECRET_HOST_PATH permission details")

    monkeypatch.setattr(archive_security, "atomic_replace_bytes", fail_publish)
    with pytest.raises(ArchiveSecurityError) as raised:
        safe_extract_zip_bytes(
            _zip_bytes([("file.txt", b"safe")]),
            tmp_path / "extract",
        )

    assert "SECRET_HOST_PATH" not in str(raised.value)
    assert "permission" not in str(raised.value)


def test_safe_zip_does_not_publish_partial_results_on_late_member_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import crew.wiki.archive_security as archive_security

    payload = _zip_bytes([("first.txt", b"first"), ("second.txt", b"second")])
    real_open = archive_security.zipfile.ZipFile.open

    def fail_second(archive, info, *args, **kwargs):
        if info.filename == "second.txt":
            raise OSError("SECRET_HOST_PATH late read failure")
        return real_open(archive, info, *args, **kwargs)

    monkeypatch.setattr(archive_security.zipfile.ZipFile, "open", fail_second)
    destination = tmp_path / "extract"

    with pytest.raises(ArchiveSecurityError):
        safe_extract_zip_bytes(payload, destination)

    assert not (destination / "first.txt").exists()
    assert not (destination / "second.txt").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory-entry race")
def test_safe_zip_rejects_missing_target_created_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import crew.tools.file_utils as file_utils

    destination = tmp_path / "extract"
    target = destination / "file.txt"
    real_replace = file_utils.os.replace

    def create_target_then_replace(source, name, *args, **kwargs):
        target.write_bytes(b"attacker")
        return real_replace(source, name, *args, **kwargs)

    monkeypatch.setattr(file_utils.os, "replace", create_target_then_replace)
    with pytest.raises(ArchiveSecurityError):
        safe_extract_zip_bytes(_zip_bytes([("file.txt", b"archive")]), destination)

    assert target.read_bytes() == b"attacker"


def test_ooxml_parser_rejects_unsafe_archive_before_loading_library(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import crew.wiki.parser as parser_mod

    fake_openpyxl = ModuleType("openpyxl")

    def forbidden_load(*_args, **_kwargs):
        raise AssertionError("unsafe OOXML reached openpyxl before archive validation")

    fake_openpyxl.load_workbook = forbidden_load
    monkeypatch.setitem(sys.modules, "openpyxl", fake_openpyxl)
    payload = _zip_bytes(
        [
            ("xl/workbook.xml", b"<workbook/>"),
            ("../escape.txt", b"owned"),
        ]
    )

    with pytest.raises(ArchiveSecurityError, match="路径"):
        parser_mod.parse_document_from_bytes(payload, "unsafe.xlsx")
    assert not (tmp_path / "escape.txt").exists()


def test_ooxml_parser_preflights_prefixed_zip_before_loading_library(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import crew.wiki.parser as parser_mod

    fake_openpyxl = ModuleType("openpyxl")

    def forbidden_load(*_args, **_kwargs):
        raise AssertionError("prefixed unsafe OOXML reached openpyxl")

    fake_openpyxl.load_workbook = forbidden_load
    monkeypatch.setitem(sys.modules, "openpyxl", fake_openpyxl)
    payload = b"self-extracting-stub" + _zip_bytes(
        [
            ("xl/workbook.xml", b"<workbook/>"),
            ("../escape.txt", b"owned"),
        ]
    )

    with pytest.raises(ArchiveSecurityError, match="路径"):
        parser_mod.parse_document_from_bytes(payload, "unsafe.xlsx")
