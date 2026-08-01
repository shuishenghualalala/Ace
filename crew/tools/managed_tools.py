"""Managed ripgrep：自动下载、校验、安装 pin 版本的 rg 二进制。

该模块让 Crew 的 glob/grep 工具不依赖系统是否预装 rg。解析顺序：

    managed rg（本模块自动安装到 {CREW_HOME}/bin）
      → 系统 PATH 上的 rg
      → Python 纯实现（glob/grep handler 内的兜底）

纯 stdlib 实现（urllib 下载 / hashlib 校验 / tarfile+zipfile 解压），不引入新依赖。
RIPGREP_VERSION 与 RIPGREP_ASSETS 的 SHA-256 是唯一真相来源，升级 rg 时两者一起更新。

配置开关：
    CREW_RIPGREP_INSTALLER=system  禁用 managed 下载，改用系统 PATH 上的 rg
    CREW_OFFLINE=1                 禁止任何下载（离线环境）
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

RIPGREP_VERSION = "14.1.1"
"""pin 的上游 ripgrep 版本。升级时与 RIPGREP_ASSETS 的 SHA-256 一起更新。"""

_RELEASE_URL_PREFIX = (
    "https://github.com/BurntSushi/ripgrep/releases/download/" + RIPGREP_VERSION
)

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
_VERSION_CHECK_TIMEOUT_SECONDS = 5
_DOWNLOAD_CHUNK_BYTES = 1 << 16

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


def _managed_binary_is_current(binary: Path) -> bool:
    """磁盘上的 managed rg 是否匹配 RIPGREP_VERSION。

    任何具体失败（OSError / 非零退出 / 空输出 / 版本不匹配）都按 stale 处理，
    让损坏或架构错误的二进制被重新下载。只有 TimeoutExpired「放行」——
    那通常意味着沙箱化的子进程而非损坏的二进制。
    """
    import subprocess

    try:
        result = subprocess.run(
            [str(binary), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=_VERSION_CHECK_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        logger.debug("rg --version 探测超时 %s，假定当前", binary)
        return True
    except OSError:
        return False
    if result.returncode != 0:
        return False
    first_line = (result.stdout or "").splitlines()[:1]
    if not first_line:
        return False
    return RIPGREP_VERSION in first_line[0]


def _download_to(url: str, dest: Path) -> None:
    """流式下载 url 到 dest，端到端 deadline。

    urllib 的 timeout 只约束单次 socket 等待，慢速对端能把传输拖过配置超时，
    因此分块读之间检查端到端 deadline。非 200 响应在写盘前拒绝，避免下游
    误导成 SHA-256 失败（被读成供应链异常）。
    """
    import time
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + _DOWNLOAD_TIMEOUT_SECONDS
    with (
        urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as resp,
        dest.open("wb") as fh,
    ):
        status = getattr(resp, "status", None)
        if status is not None and status != 200:
            raise urllib.error.URLError(f"意外的 HTTP {status} 响应: {url}")
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError(f"下载超时（>{_DOWNLOAD_TIMEOUT_SECONDS}s）: {url}")
            chunk = resp.read(_DOWNLOAD_CHUNK_BYTES)
            if not chunk:
                break
            fh.write(chunk)


def _verify_sha256(path: Path, expected_hex: str) -> None:
    """校验文件 SHA-256，不匹配抛 ChecksumMismatchError。"""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != expected_hex:
        raise ChecksumMismatchError(
            f"校验和不匹配 {path.name}: 期望 {expected_hex}, 实际 {actual}"
        )


def _validate_legacy_tar_member(member, extract_root: Path) -> None:
    """拒绝解压会逃出 extract_root 或类型不支持的 tar 成员（无 filter 兜底用）。"""
    import tarfile

    target = extract_root / member.name
    try:
        target.resolve().relative_to(extract_root.resolve())
    except ValueError as exc:
        raise tarfile.TarError(f"拒绝解压越界 tar 成员 {member.name!r}") from exc
    if not (member.isfile() or member.isdir()):
        raise tarfile.TarError(f"拒绝解压不支持的 tar 成员 {member.name!r}")


def _extract_tar_data(tf, extract_root: Path) -> None:
    """优先用 PEP 706 data filter 解压；旧 Python 无 filter 时走校验过的 legacy 解压。"""

    try:
        tf.extractall(extract_root, filter="data")
    except TypeError as exc:
        if "filter" not in str(exc):
            raise
        members = tf.getmembers()
        for member in members:
            _validate_legacy_tar_member(member, extract_root)
        tf.extractall(extract_root, members=members)


def _extract_zip_validated(zf, extract_root: Path) -> None:
    """逐成员校验路径后解压 zip（防 zip-slip 的纵深防御）。"""
    import zipfile

    extract_root.mkdir(parents=True, exist_ok=True)
    root = extract_root.resolve()
    for member in zf.infolist():
        target = (extract_root / member.filename).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise zipfile.BadZipFile(f"拒绝解压越界 zip 成员 {member.filename!r}") from exc
    zf.extractall(extract_root)


def _extract_rg(archive: Path, extract_root: Path) -> Path:
    """解压 archive 并定位其中的 rg 二进制。release archive 把二进制嵌在版本子目录下。"""
    import tarfile
    import zipfile

    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            _extract_zip_validated(zf, extract_root)
    else:
        with tarfile.open(archive, mode="r:*") as tf:
            _extract_tar_data(tf, extract_root)
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
    bin_dir.mkdir(parents=True, exist_ok=True)
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
    3. managed 不存在但 PATH 有系统 rg → 用系统的（managed 一旦存在，pin 版本始终胜出）
    4. offline → 返回 None（调用方走 Python 兜底）
    5. 平台不支持 → 用系统 rg 或抛 ManagedToolUnavailableError
    6. 否则下载 → 校验 → 解压 → 安装 → prepend PATH → 返回路径

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

    if not managed_exists:
        system_rg = shutil.which("rg")
        if system_rg:
            return Path(system_rg)

    if is_offline():
        logger.debug("跳过 rg 安装: CREW_OFFLINE 已设置")
        return None

    if sys.platform == "android":
        return non_managed_rg()

    import asyncio

    arch = _normalized_arch()
    if arch is None:
        system_rg = non_managed_rg()
        if system_rg:
            return system_rg
        raise ManagedToolUnavailableError(
            f"当前架构不支持 managed ripgrep ({sys.platform})，"
            "请手动安装 rg，或设置 CREW_RIPGREP_INSTALLER=system"
        )

    asset_entry = RIPGREP_ASSETS.get((sys.platform, arch))
    if asset_entry is None:
        system_rg = non_managed_rg()
        if system_rg:
            return system_rg
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
    except (urllib.error.URLError, TimeoutError):
        logger.warning("从 %s 下载 rg 失败", _RELEASE_URL_PREFIX, exc_info=True)
        return None
    except (tarfile.TarError, zipfile.BadZipFile, FileNotFoundError):
        logger.exception("rg 安装失败: archive 错误")
        return None
    except (PermissionError, OSError) as exc:
        logger.warning("rg 安装失败: 无法写入 %s (%s)", _bin_dir(), type(exc).__name__)
        return None
    else:
        prepend_managed_bin_to_path()
        return installed
