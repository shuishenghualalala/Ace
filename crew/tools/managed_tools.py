"""Managed ripgrep：自动下载、校验、安装 pin 版本的 rg 二进制。

该模块让 Crew 的 glob/grep 工具不依赖系统是否预装 rg。解析顺序：

    managed rg（本模块自动安装到 {CREW_HOME}/bin）
      → Python 纯实现（glob/grep handler 内的兜底）

系统 PATH 上的 rg 只在操作员显式选择 ``CREW_RIPGREP_INSTALLER=system``
时启用，默认路径不会执行未固定身份的二进制。

纯 stdlib 实现（urllib 下载 / hashlib 校验 / tarfile+zipfile 解压），不引入新依赖。
RIPGREP_VERSION 与 RIPGREP_ASSETS 的 SHA-256 是唯一真相来源，升级 rg 时两者一起更新。

配置开关：
    CREW_RIPGREP_INSTALLER=system  禁用 managed 下载，改用系统 PATH 上的 rg
    CREW_OFFLINE=1                 禁止任何下载（离线环境）
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import sys
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Literal

from crew.security.outbound import OutboundDenied, OutboundHttpClient
from crew.tools.file_utils import (
    FileConflictError,
    _ensure_private_directory,
    atomic_replace_bytes,
    read_verified_bytes,
    snapshot_file,
    stat_verified_file,
)

logger = logging.getLogger(__name__)
_MANAGED_TOOL_HTTP = OutboundHttpClient()

RIPGREP_VERSION = "14.1.1"
"""pin 的上游 ripgrep 版本。升级时与 RIPGREP_ASSETS 的 SHA-256 一起更新。"""

_RELEASE_URL_PREFIX = "https://github.com/BurntSushi/ripgrep/releases/download/" + RIPGREP_VERSION

RIPGREP_ASSETS: dict[tuple[str, str], tuple[str, str]] = {
    ("darwin", "arm64"): (
        f"ripgrep-{RIPGREP_VERSION}-aarch64-apple-darwin.tar.gz",
        "24ad76777745fbff131c8fbc466742b011f925bfa4fffa2ded6def23b5b937be",
    ),
    ("darwin", "x86_64"): (
        f"ripgrep-{RIPGREP_VERSION}-x86_64-apple-darwin.tar.gz",
        "fc87e78f7cb3fea12d69072e7ef3b21509754717b746368fd40d88963630e2b3",
    ),
    ("linux", "arm64"): (
        f"ripgrep-{RIPGREP_VERSION}-aarch64-unknown-linux-gnu.tar.gz",
        "c827481c4ff4ea10c9dc7a4022c8de5db34a5737cb74484d62eb94a95841ab2f",
    ),
    ("linux", "x86_64"): (
        f"ripgrep-{RIPGREP_VERSION}-x86_64-unknown-linux-musl.tar.gz",
        "4cf9f2741e6c465ffdb7c26f38056a59e2a2544b51f7cc128ef28337eeae4d8e",
    ),
    # upstream 不发布 arm64-windows 构建，两个 Windows 条目都指向同一个 x86_64 MSVC asset
    ("win32", "arm64"): (
        f"ripgrep-{RIPGREP_VERSION}-x86_64-pc-windows-msvc.zip",
        "d0f534024c42afd6cb4d38907c25cd2b249b79bbe6cc1dbee8e3e37c2b6e25a1",
    ),
    ("win32", "x86_64"): (
        f"ripgrep-{RIPGREP_VERSION}-x86_64-pc-windows-msvc.zip",
        "d0f534024c42afd6cb4d38907c25cd2b249b79bbe6cc1dbee8e3e37c2b6e25a1",
    ),
}
"""`(sys.platform, 归一化 arch) -> (asset 文件名, sha256 hex)`。"""

_DOWNLOAD_TIMEOUT_SECONDS = 120
_DOWNLOAD_MAX_BYTES = 64 * 1024 * 1024
_ARCHIVE_MAX_MEMBERS = 256
_ARCHIVE_MAX_MEMBER_BYTES = 64 * 1024 * 1024
_ARCHIVE_MAX_TOTAL_BYTES = 128 * 1024 * 1024
_ARCHIVE_MAX_COMPRESSION_RATIO = 200
_ARCHIVE_MAX_PATH_CHARS = 512
_ARCHIVE_MAX_PATH_DEPTH = 12

_ARCH_ALIASES = {
    "aarch64": "arm64",
    "arm64": "arm64",
    "amd64": "x86_64",
    "x86_64": "x86_64",
    "x64": "x86_64",
}


class ChecksumMismatchError(Exception):
    """下载的 archive SHA-256 校验失败。

    供应链异常（CDN 投毒 / MITM / 被篡改的镜像），必须大声告警，
    不能当「离线」静默处理。
    """


class ManagedToolUnavailableError(Exception):
    """当前平台没有可用的 managed 二进制（架构/平台不支持或 asset 缺失）。"""


def _bin_dir() -> Path:
    """managed 二进制安装目录：{CREW_HOME}/bin。CREW_HOME 由 crew.state.home 解析。"""
    from crew.state.home import get_crew_home

    return get_crew_home() / "bin"


def _env_truthy(name: str) -> bool:
    val = os.environ.get(name, "").strip().lower()
    return val in {"1", "true", "yes", "on"}


def _normalized_arch() -> str | None:
    """返回归一化 arch 键，不支持的平台返回 None。"""
    import platform

    raw = platform.machine().lower()
    return _ARCH_ALIASES.get(raw)


def managed_rg_path() -> Path:
    """managed rg 二进制路径（Windows 带 .exe）。"""
    name = "rg.exe" if sys.platform == "win32" else "rg"
    return _bin_dir() / name


def is_offline() -> bool:
    """CREW_OFFLINE 为真时禁用下载。"""
    return _env_truthy("CREW_OFFLINE")


RipgrepInstaller = Literal["managed", "system"]
INSTALLER_MANAGED: RipgrepInstaller = "managed"
"""默认模式：下载 pin + 校验过的上游二进制。"""

INSTALLER_SYSTEM: RipgrepInstaller = "system"
"""system 模式：交给系统包管理器 / PATH 上的 rg。"""


def ripgrep_installer() -> RipgrepInstaller:
    """读取 CREW_RIPGREP_INSTALLER，归一化为 managed/system，默认 managed。"""
    raw = os.environ.get("CREW_RIPGREP_INSTALLER", "").strip().lower()
    if raw == INSTALLER_SYSTEM:
        return INSTALLER_SYSTEM
    if raw and raw != INSTALLER_MANAGED:
        logger.warning(
            "未识别的 CREW_RIPGREP_INSTALLER=%r，应为 managed 或 system，按 managed 处理。",
            raw,
        )
    return INSTALLER_MANAGED


def prefers_system_ripgrep() -> bool:
    return ripgrep_installer() == INSTALLER_SYSTEM


def prepend_managed_bin_to_path() -> None:
    """idempotent 把 {CREW_HOME}/bin 前置到 PATH。目录不存在也无害。"""
    bin_str = str(_bin_dir())
    current = os.environ.get("PATH", "")
    parts = current.split(os.pathsep) if current else []
    if parts and parts[0] == bin_str:
        return
    parts = [bin_str, *(p for p in parts if p != bin_str)]
    os.environ["PATH"] = os.pathsep.join(parts)


def _path_without_managed_bin() -> str | None:
    """返回去掉了 {CREW_HOME}/bin 的 PATH。"""
    current = os.environ.get("PATH")
    if not current:
        return None
    managed_dir = _bin_dir().resolve()
    parts = [
        part
        for part in current.split(os.pathsep)
        if not part or Path(part).resolve() != managed_dir
    ]
    return os.pathsep.join(parts)


def _cached_archive_path(asset: str) -> Path:
    return _bin_dir() / f".{asset}"


def _managed_binary_is_current(binary: Path) -> bool:
    """Verify the installed executable against the pinned upstream archive."""
    import tempfile

    arch = _normalized_arch()
    asset_entry = RIPGREP_ASSETS.get((sys.platform, arch or ""))
    if asset_entry is None:
        return False
    asset, archive_digest = asset_entry
    archive = _cached_archive_path(asset)
    try:
        _ensure_private_directory(_bin_dir())
        archive_bytes = read_verified_bytes(
            archive,
            max_bytes=_DOWNLOAD_MAX_BYTES,
            expected_digest=archive_digest,
        )
        real_binary = binary.resolve(strict=True)
        real_binary.relative_to(_bin_dir().resolve(strict=True))
        installed_bytes = read_verified_bytes(
            real_binary,
            max_bytes=_DOWNLOAD_MAX_BYTES,
        )
        with tempfile.TemporaryDirectory(
            prefix=".crew-rg-verify-",
            dir=_bin_dir(),
        ) as tmp_str:
            snapshot = Path(tmp_str) / asset
            expected_snapshot = snapshot_file(
                snapshot,
                max_bytes=_DOWNLOAD_MAX_BYTES,
            )
            atomic_replace_bytes(
                snapshot,
                archive_bytes,
                expected_snapshot,
                max_bytes=_DOWNLOAD_MAX_BYTES,
            )
            expected_binary = _extract_rg(snapshot, Path(tmp_str) / "unpacked")
            expected_bytes = read_verified_bytes(
                expected_binary,
                max_bytes=_DOWNLOAD_MAX_BYTES,
            )
    except (OSError, RuntimeError, ValueError):
        return False
    return hashlib.sha256(installed_bytes).digest() == hashlib.sha256(expected_bytes).digest()


def _download_to(url: str, dest: Path) -> None:
    """Download through the shared DNS-pinning client before writing to disk."""
    response = _MANAGED_TOOL_HTTP.fetch(
        url,
        method="GET",
        timeout=_DOWNLOAD_TIMEOUT_SECONDS,
        max_bytes=_DOWNLOAD_MAX_BYTES,
        max_redirects=3,
    )
    if response.status != 200:
        raise OutboundDenied("http_status_rejected")
    _ensure_private_directory(dest.parent)
    expected = snapshot_file(dest, max_bytes=_DOWNLOAD_MAX_BYTES)
    atomic_replace_bytes(
        dest,
        response.body,
        expected,
        max_bytes=_DOWNLOAD_MAX_BYTES,
    )


def _verify_sha256(path: Path, expected_hex: str) -> None:
    """校验文件 SHA-256，不匹配抛 ChecksumMismatchError。"""
    try:
        read_verified_bytes(
            path,
            max_bytes=_DOWNLOAD_MAX_BYTES,
            expected_digest=expected_hex,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise ChecksumMismatchError(f"校验和不匹配或文件身份不安全: {path.name}") from exc


def _validated_archive_target(name: str, extract_root: Path, error_type: type[Exception]) -> Path:
    if (
        not name
        or len(name) > _ARCHIVE_MAX_PATH_CHARS
        or "\x00" in name
        or "\\" in name
        or name.startswith("/")
    ):
        raise error_type(f"拒绝不安全 archive 成员路径 {name!r}")
    raw_parts = name.rstrip("/").split("/")
    if (
        not raw_parts
        or len(raw_parts) > _ARCHIVE_MAX_PATH_DEPTH
        or any(part in {"", ".", ".."} for part in raw_parts)
        or ":" in raw_parts[0]
    ):
        raise error_type(f"拒绝不安全 archive 成员路径 {name!r}")
    relative = PurePosixPath(*raw_parts)
    target = extract_root.joinpath(*relative.parts)
    try:
        target.resolve(strict=False).relative_to(extract_root.resolve(strict=False))
    except ValueError as exc:
        raise error_type(f"拒绝解压越界 archive 成员 {name!r}") from exc
    return target


def _write_archive_member(
    source, target: Path, declared_size: int, error_type: type[Exception]
) -> None:
    if declared_size < 0 or declared_size > _ARCHIVE_MAX_MEMBER_BYTES:
        raise error_type(f"archive 成员超过大小上限: {target.name!r}")
    try:
        _ensure_private_directory(target.parent)
    except (FileConflictError, OSError) as exc:
        raise error_type("archive 成员父目录不安全") from exc
    total = 0
    chunks: list[bytes] = []
    try:
        while chunk := source.read(1024 * 1024):
            total += len(chunk)
            if total > declared_size or total > _ARCHIVE_MAX_MEMBER_BYTES:
                raise error_type(f"archive 成员展开大小不可信: {target.name!r}")
            chunks.append(chunk)
    except OSError as exc:
        raise error_type("archive 成员读取失败") from exc
    if total != declared_size:
        raise error_type(f"archive 成员声明大小不匹配: {target.name!r}")
    try:
        expected = snapshot_file(
            target,
            max_bytes=_ARCHIVE_MAX_MEMBER_BYTES,
        )
        if expected.exists:
            raise FileConflictError("archive 成员路径冲突")
        atomic_replace_bytes(
            target,
            b"".join(chunks),
            expected,
            max_bytes=_ARCHIVE_MAX_MEMBER_BYTES,
        )
    except (FileConflictError, OSError, ValueError) as exc:
        raise error_type("archive 成员发布失败") from exc


def _archive_identity_key(target: Path) -> str:
    return unicodedata.normalize("NFC", str(target)).casefold()


def _extract_tar_data(tf, extract_root: Path, archive_size: int) -> None:
    """Preflight and stream regular tar members without extractall()."""
    import tarfile

    members = tf.getmembers()
    if len(members) > _ARCHIVE_MAX_MEMBERS:
        raise tarfile.TarError("tar 成员数量超过安全上限")
    total = 0
    targets: set[str] = set()
    prepared: list[tuple[object, Path]] = []
    for member in members:
        if not (member.isfile() or member.isdir()):
            raise tarfile.TarError(f"拒绝解压不支持的 tar 成员 {member.name!r}")
        target = _validated_archive_target(member.name, extract_root, tarfile.TarError)
        key = _archive_identity_key(target)
        if key in targets:
            raise tarfile.TarError(f"tar 成员路径重复 {member.name!r}")
        targets.add(key)
        if member.isfile():
            if member.size < 0 or member.size > _ARCHIVE_MAX_MEMBER_BYTES:
                raise tarfile.TarError(f"tar 成员超过大小上限 {member.name!r}")
            total += member.size
            if total > _ARCHIVE_MAX_TOTAL_BYTES:
                raise tarfile.TarError("tar 展开总量超过安全上限")
        prepared.append((member, target))
    if archive_size <= 0 or total > archive_size * _ARCHIVE_MAX_COMPRESSION_RATIO:
        raise tarfile.TarError("tar 压缩比超过安全上限")

    try:
        _ensure_private_directory(extract_root)
    except (FileConflictError, OSError) as exc:
        raise tarfile.TarError("tar 解压目录不安全") from exc
    for member, target in prepared:
        if member.isdir():
            try:
                _ensure_private_directory(target)
            except (FileConflictError, OSError) as exc:
                raise tarfile.TarError(f"tar 目录成员路径冲突 {member.name!r}") from exc
            continue
        source = tf.extractfile(member)
        if source is None:
            raise tarfile.TarError(f"无法读取 tar 成员 {member.name!r}")
        with source:
            _write_archive_member(source, target, member.size, tarfile.TarError)


def _extract_zip_validated(zf, extract_root: Path) -> None:
    """Preflight and stream regular ZIP members with hard resource budgets."""
    import stat
    import zipfile

    members = zf.infolist()
    if len(members) > _ARCHIVE_MAX_MEMBERS:
        raise zipfile.BadZipFile("zip 成员数量超过安全上限")
    total = 0
    targets: set[str] = set()
    prepared: list[tuple[object, Path]] = []
    for member in members:
        target = _validated_archive_target(member.filename, extract_root, zipfile.BadZipFile)
        key = _archive_identity_key(target)
        if key in targets:
            raise zipfile.BadZipFile(f"zip 成员路径重复 {member.filename!r}")
        targets.add(key)
        mode = (member.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(mode)
        if file_type and file_type not in {stat.S_IFREG, stat.S_IFDIR}:
            raise zipfile.BadZipFile(f"拒绝解压不支持的 zip 成员 {member.filename!r}")
        if member.flag_bits & 0x1:
            raise zipfile.BadZipFile("拒绝加密 zip 成员")
        if member.file_size < 0 or member.file_size > _ARCHIVE_MAX_MEMBER_BYTES:
            raise zipfile.BadZipFile(f"zip 成员超过大小上限 {member.filename!r}")
        total += member.file_size
        if total > _ARCHIVE_MAX_TOTAL_BYTES:
            raise zipfile.BadZipFile("zip 展开总量超过安全上限")
        if member.file_size > 0 and (
            member.compress_size <= 0
            or member.file_size > member.compress_size * _ARCHIVE_MAX_COMPRESSION_RATIO
        ):
            raise zipfile.BadZipFile(f"zip 成员压缩比超过安全上限 {member.filename!r}")
        prepared.append((member, target))

    try:
        _ensure_private_directory(extract_root)
    except (FileConflictError, OSError) as exc:
        raise zipfile.BadZipFile("zip 解压目录不安全") from exc
    for member, target in prepared:
        if member.is_dir():
            try:
                _ensure_private_directory(target)
            except (FileConflictError, OSError) as exc:
                raise zipfile.BadZipFile(
                    f"zip 目录成员路径冲突 {member.filename!r}"
                ) from exc
            continue
        with zf.open(member, "r") as source:
            _write_archive_member(source, target, member.file_size, zipfile.BadZipFile)


def _extract_rg(archive: Path, extract_root: Path) -> Path:
    """解压 archive 并定位其中的 rg 二进制。release archive 把二进制嵌在版本子目录下。"""
    import tarfile
    import zipfile

    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            _extract_zip_validated(zf, extract_root)
    else:
        with tarfile.open(archive, mode="r:*") as tf:
            _extract_tar_data(tf, extract_root, archive.stat().st_size)
    target_name = "rg.exe" if sys.platform == "win32" else "rg"
    for path in extract_root.rglob(target_name):
        if path.is_file():
            return path
    raise FileNotFoundError(f"在 {archive.name} 内找不到 {target_name}")


def _install_ripgrep_sync(asset: str, sha256: str) -> Path:
    """下载 → SHA-256 校验 → 解压 → 原子安装 rg。

    staging 发生在 BIN_DIR 内，最终 rename 在同一文件系统上 POSIX 原子完成。
    POSIX 用版本化真二进制 + 相对 symlink，移动/bind-mount {CREW_HOME} 不会
    烤入原始家目录路径。校验失败在 move 前抛出，abort 安装。
    """
    import tempfile

    bin_dir = _bin_dir()
    _ensure_private_directory(bin_dir)
    url = f"{_RELEASE_URL_PREFIX}/{asset}"
    with tempfile.TemporaryDirectory(prefix=".crew-rg-", dir=bin_dir) as tmp_str:
        tmp = Path(tmp_str)
        archive = tmp / asset
        _download_to(url, archive)
        _verify_sha256(archive, sha256)
        extracted = _extract_rg(archive, tmp / "unpacked")
        if sys.platform != "win32":
            extracted.chmod(0o755)
        dest = managed_rg_path()
        cached_archive = _cached_archive_path(asset)
        archive.chmod(0o600)
        archive.replace(cached_archive)
        if sys.platform == "win32":
            extracted.replace(dest)
            return dest
        real = bin_dir / f"rg-{RIPGREP_VERSION}"
        extracted.replace(real)
        link = tmp / "rg-link"
        link.symlink_to(os.path.relpath(real, start=bin_dir))
        link.replace(dest)
        return dest


async def ensure_ripgrep() -> Path | None:
    """确保有一个可用的 rg，必要时下载安装。解析顺序：

    1. INSTALLER=system → 用 PATH 上的系统 rg，找不到返回 None
    2. managed rg 已存在且版本匹配 → 直接用
    3. offline → 返回 None（调用方走 Python 兜底）
    4. 平台不支持 → 抛 ManagedToolUnavailableError
    5. 否则下载 → 校验 → 解压 → 安装 → prepend PATH → 返回路径

    stale 的 managed 二进制不会被主动删除，安装成功时原子覆盖；失败时保留旧版
    比让用户一个 rg 都没有更好。
    """
    import shutil
    import tarfile
    import urllib.error
    import zipfile

    managed = managed_rg_path()
    managed_exists = managed.exists() or managed.is_symlink()

    def non_managed_rg() -> Path | None:
        system_rg = shutil.which("rg", path=_path_without_managed_bin())
        return Path(system_rg) if system_rg else None

    if prefers_system_ripgrep():
        return non_managed_rg()

    if managed_exists and _managed_binary_is_current(managed):
        prepend_managed_bin_to_path()
        return managed

    if is_offline():
        logger.debug("跳过 rg 安装: CREW_OFFLINE 已设置")
        return None

    if sys.platform == "android":
        raise ManagedToolUnavailableError("Android 不支持 managed ripgrep")

    import asyncio

    arch = _normalized_arch()
    if arch is None:
        raise ManagedToolUnavailableError(
            f"当前架构不支持 managed ripgrep ({sys.platform})，"
            "请手动安装 rg，或设置 CREW_RIPGREP_INSTALLER=system"
        )

    asset_entry = RIPGREP_ASSETS.get((sys.platform, arch))
    if asset_entry is None:
        raise ManagedToolUnavailableError(
            f"pin 的 ripgrep {RIPGREP_VERSION} 没有 ({sys.platform}/{arch}) 的 asset，"
            "请手动安装 rg，或设置 CREW_RIPGREP_INSTALLER=system"
        )
    asset, sha256 = asset_entry

    if managed_exists:
        logger.info("managed rg (%s) 已过期，替换为 %s", managed, RIPGREP_VERSION)

    try:
        installed = await asyncio.to_thread(_install_ripgrep_sync, asset, sha256)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            logger.warning("managed rg asset 未找到: %s/%s", sys.platform, arch)
        else:
            logger.warning("从 %s 下载 rg 失败", _RELEASE_URL_PREFIX, exc_info=True)
        return None
    except (urllib.error.URLError, TimeoutError, OutboundDenied):
        logger.warning("从 %s 下载 rg 失败", _RELEASE_URL_PREFIX, exc_info=True)
        return None
    except (tarfile.TarError, zipfile.BadZipFile, FileNotFoundError):
        logger.exception("rg 安装失败: archive 错误")
        return None
    except (PermissionError, OSError) as exc:
        logger.warning("rg 安装失败: 无法写入 %s (%s)", _bin_dir(), type(exc).__name__)
        return None
    else:
        if not _managed_binary_is_current(installed):
            raise ManagedToolUnavailableError("新安装的 managed ripgrep 完整性验证失败")
        prepend_managed_bin_to_path()
        return installed
