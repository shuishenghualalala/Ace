from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from crew.tools import managed_tools
from crew.tools.file_utils import FileConflictError


def _managed_archive_bytes() -> bytes:
    import io

    output = io.BytesIO()
    target = "rg.exe" if sys.platform == "win32" else "rg"
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(f"ripgrep-safe/{target}", b"trusted-ripgrep-binary")
    return output.getvalue()


def _configure_asset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[str, str, bytes]:
    asset = "ripgrep-safe.zip"
    archive_bytes = _managed_archive_bytes()
    digest = hashlib.sha256(archive_bytes).hexdigest()
    monkeypatch.setattr(managed_tools, "_bin_dir", lambda: tmp_path / "bin")
    monkeypatch.setattr(managed_tools, "_normalized_arch", lambda: "test-arch")
    monkeypatch.setattr(
        managed_tools,
        "RIPGREP_ASSETS",
        {(sys.platform, "test-arch"): (asset, digest)},
    )
    monkeypatch.setattr(
        managed_tools,
        "_download_to",
        lambda _url, destination: destination.write_bytes(archive_bytes),
    )
    return asset, digest, archive_bytes


def test_managed_binary_is_verified_without_executing_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    asset, digest, _archive_bytes = _configure_asset(monkeypatch, tmp_path)
    binary = managed_tools._install_ripgrep_sync(asset, digest)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("integrity verification executed the binary"),
    )

    assert managed_tools._managed_binary_is_current(binary) is True

    binary.resolve().write_bytes(b"attacker replacement")
    assert managed_tools._managed_binary_is_current(binary) is False


def test_tampered_cached_archive_cannot_authorize_binary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    asset, digest, _archive_bytes = _configure_asset(monkeypatch, tmp_path)
    binary = managed_tools._install_ripgrep_sync(asset, digest)
    managed_tools._cached_archive_path(asset).write_bytes(b"tampered archive")

    assert managed_tools._managed_binary_is_current(binary) is False


def test_managed_download_rejects_hardlinked_destination(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.bin"
    destination = tmp_path / "download.bin"
    source.write_bytes(b"original")
    try:
        os.link(source, destination)
    except (OSError, NotImplementedError):
        pytest.skip("hardlink creation unavailable")

    class Response:
        status = 200
        body = b"replacement"

    class Client:
        def fetch(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(managed_tools, "_MANAGED_TOOL_HTTP", Client())

    with pytest.raises((OSError, RuntimeError, ValueError)):
        managed_tools._download_to("https://example.invalid/rg.zip", destination)

    assert source.read_bytes() == b"original"


def test_ripgrep_install_rejects_linked_bin_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_bin = tmp_path / "bin"
    try:
        linked_bin.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlink creation unavailable")

    monkeypatch.setattr(managed_tools, "_bin_dir", lambda: linked_bin)

    with pytest.raises(FileConflictError, match="链接|reparse"):
        managed_tools._install_ripgrep_sync("ripgrep-safe.zip", "0" * 64)


def test_managed_zip_extraction_rejects_traversal_and_symlink_members(tmp_path):
    traversal = tmp_path / "traversal.zip"
    with zipfile.ZipFile(traversal, "w") as archive:
        archive.writestr("../outside", b"escape")
    with zipfile.ZipFile(traversal) as archive:
        with pytest.raises(zipfile.BadZipFile):
            managed_tools._extract_zip_validated(archive, tmp_path / "unpacked")
    assert not (tmp_path / "outside").exists()

    linked = tmp_path / "linked.zip"
    info = zipfile.ZipInfo("ripgrep-safe/rg")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(linked, "w") as archive:
        archive.writestr(info, b"../../outside")
    with zipfile.ZipFile(linked) as archive:
        with pytest.raises(zipfile.BadZipFile):
            managed_tools._extract_zip_validated(archive, tmp_path / "linked-out")


def test_managed_archive_extraction_enforces_resource_budgets(tmp_path, monkeypatch):
    archive_path = tmp_path / "bomb.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("ripgrep-safe/rg", b"x" * 4096)
    monkeypatch.setattr(managed_tools, "_ARCHIVE_MAX_TOTAL_BYTES", 1024)

    with zipfile.ZipFile(archive_path) as archive:
        with pytest.raises(zipfile.BadZipFile, match="总量"):
            managed_tools._extract_zip_validated(archive, tmp_path / "bomb-out")


def test_managed_tar_extraction_rejects_links(tmp_path):
    archive_path = tmp_path / "linked.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        directory = tarfile.TarInfo("ripgrep-safe")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        linked = tarfile.TarInfo("ripgrep-safe/rg")
        linked.type = tarfile.SYMTYPE
        linked.linkname = "../../outside"
        archive.addfile(linked)

    with tarfile.open(archive_path, "r:*") as archive:
        with pytest.raises(tarfile.TarError):
            managed_tools._extract_tar_data(
                archive,
                tmp_path / "tar-out",
                archive_path.stat().st_size,
            )


@pytest.mark.asyncio
async def test_default_managed_mode_never_falls_back_to_path_binary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(managed_tools, "_bin_dir", lambda: tmp_path / "bin")
    monkeypatch.setattr(managed_tools, "is_offline", lambda: True)
    monkeypatch.setattr(managed_tools, "prefers_system_ripgrep", lambda: False)
    monkeypatch.setattr(
        "shutil.which",
        lambda *_args, **_kwargs: str(tmp_path / "attacker-rg"),
    )

    assert await managed_tools.ensure_ripgrep() is None
