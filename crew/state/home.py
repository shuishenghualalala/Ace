"""Workspace Home 管理：项目级 Crew 家目录初始化、任务工作目录与上下文文件加载。

采用 的 ~/.Crew/ 机制，为 Crew 提供持久化的 agent 上下文。
Home 目录默认放在项目根目录下 {DEFAULT_HOME_DIRNAME}/，可通过 CREW_HOME 环境变量覆盖。

{DEFAULT_HOME_DIRNAME}/ 目录结构：
  SOUL.md              — Agent 身份（注入 system prompt stable 层）
  memories/
    MEMORY.md          — 跨会话持久记忆（注入 volatile 层）
    USER.md            — 用户画像（注入 volatile 层）
  skills/              — 用户自定义 skills（每个子目录一个 SKILL.md）
  agents/default/      — 默认单 Agent 配置空间
  accounts/acct_xxx/    — owner 级私有 runtime home（目录名由 owner_account_id hash 派生）

任务产物目录默认是 {DEFAULT_HOME_DIRNAME}/task_workspaces/，不放在 crew_home 下；可用
CREW_TASK_WORKSPACE_ROOT 覆盖。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import sys
import threading
import time
from io import StringIO
from pathlib import Path

from dotenv import dotenv_values

logger = logging.getLogger(__name__)

# 项目根目录（crew/state 的上两层）
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    ROOT = Path(sys._MEIPASS)
else:
    ROOT = Path(__file__).resolve().parents[2]

# 默认 Crew 家目录名（冻结态/开发态均使用此常量，修改此处即可全局切换）
DEFAULT_HOME_DIRNAME = ".Crew"
OWNER_DIR_HASH_CHARS = 16
OWNER_DIR_PREFIX = "acct_"
WINDOWS_WORKSPACE_PATH_BUDGET = 240
POSIX_WORKSPACE_PATH_BUDGET = 4096
_ENV_LOCK = threading.RLock()
_RUNTIME_ENV_BASELINE: dict[str, str | None] = {}
_RUNTIME_ENV_KEYS: set[str] = set()
_FILE_CACHE: dict[str, tuple[tuple[int, int] | None, object]] = {}
_PROTECTED_RUNTIME_ENV_NAMES = frozenset(
    {
        "ALL_PROXY",
        "APPDATA",
        "BASH_ENV",
        "COMSPEC",
        "CREW_CONFIG",
        "CREW_DB_PATH",
        "CREW_HOME",
        "CREW_OWNER_ACCOUNT_ID",
        "ENV",
        "FTP_PROXY",
        "GATEWAY_HOST",
        "GATEWAY_PORT",
        "GCONV_PATH",
        "HOME",
        "HOSTALIASES",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LOCALAPPDATA",
        "LOCPATH",
        "NO_PROXY",
        "PATH",
        "PATHEXT",
        "PERL5OPT",
        "PSMODULEPATH",
        "RUBYOPT",
        "SHELLOPTS",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
        "_JAVA_OPTIONS",
    }
)
_PROTECTED_RUNTIME_ENV_PREFIXES = (
    "ACE_",
    "CORECLR_",
    "COR_",
    "CREW_GATEWAY_",
    "CREW_SECURITY_",
    "CREW_TASK_",
    "DOTNET_",
    "DYLD_",
    "JAVA_TOOL_OPTIONS",
    "JDK_JAVA_OPTIONS",
    "LD_",
    "NODE_",
    "NPM_CONFIG_",
    "PYTHON",
)


def _mask_owner_for_log(owner_account_id: str | None) -> str:
    owner = str(owner_account_id or "").strip()
    if not owner:
        return "legacy"
    return owner_path_segment(owner)


def _bundled_runtime_paths() -> list[str]:
    """返回打包内嵌运行时的目录路径列表（仅冻结态有效）。

    下游发行流程可将 Python embeddable 和 Node.js portable 放入
    ``crew-gateway/_internal/runtimes/`` 子目录（与可执行文件同级）。
    运行时通过 ``sys.executable`` 定位 exe 所在目录。

    返回的目录应 **前置** 到子进程 PATH，使 ``python`` / ``node`` 命令
    解析到打包版本，而非依赖系统安装。
    """
    import logging

    log = logging.getLogger(__name__)

    frozen = getattr(sys, "frozen", False)
    log.debug(f"[bundled_runtime] frozen={frozen}, executable={sys.executable}")

    if not frozen:
        log.debug("[bundled_runtime] Not in frozen mode, returning []")
        return []

    # PyInstaller --onedir 模式：exe 在 crew-gateway/，运行时在 crew-gateway/_internal/runtimes/
    exe_dir = Path(sys.executable).parent
    runtimes_dir = exe_dir / "_internal" / "runtimes"

    log.debug(f"[bundled_runtime] Looking for runtimes_dir: {runtimes_dir}")

    if not runtimes_dir.is_dir():
        log.warning(f"[bundled_runtime] runtimes_dir does not exist: {runtimes_dir}")
        return []

    paths: list[str] = []
    python_dir = runtimes_dir / "python"
    node_dir = runtimes_dir / "node"

    if python_dir.is_dir():
        # Windows: python.exe 在根目录; Linux (indygreg): python3 在 bin/ 子目录
        python_exe = python_dir / "python.exe"
        python_bin = python_dir / "bin" / "python3"
        if python_exe.exists():
            paths.append(str(python_dir))
            log.debug(f"[bundled_runtime] Found python_dir (Windows): {python_dir}")
        elif python_bin.exists():
            paths.append(str(python_dir / "bin"))
            log.debug(f"[bundled_runtime] Found python_dir (Linux): {python_dir / 'bin'}")
        else:
            log.warning(
                f"[bundled_runtime] python_dir exists but no executable found: {python_dir}"
            )
    else:
        log.warning(f"[bundled_runtime] python_dir not found: {python_dir}")

    if node_dir.is_dir():
        # Windows: node.exe 在根目录; Linux: node 在 bin/ 子目录
        node_exe = node_dir / "node.exe"
        node_bin = node_dir / "bin" / "node"
        if node_exe.exists():
            paths.append(str(node_dir))
            log.debug(f"[bundled_runtime] Found node_dir (Windows): {node_dir}")
        elif node_bin.exists():
            paths.append(str(node_dir / "bin"))
            log.debug(f"[bundled_runtime] Found node_dir (Linux): {node_dir / 'bin'}")
        else:
            log.warning(f"[bundled_runtime] node_dir exists but no executable found: {node_dir}")
    else:
        log.warning(f"[bundled_runtime] node_dir not found: {node_dir}")

    # 可选的下游发行包可以把 cua-driver 放到 _internal/runtimes/cua-driver/bin/。
    # 检测到预置文件时将目录前置进 PATH，使 config.yaml 中的裸命令可被 MCP
    # 子进程解析；官方 Crew 源码与自构建安装包不携带该第三方二进制。
    cua_dir = runtimes_dir / "cua-driver" / "bin"
    if cua_dir.is_dir():
        cua_exe = cua_dir / "cua-driver.exe"
        cua_bin = cua_dir / "cua-driver"
        if cua_exe.exists() or cua_bin.exists():
            paths.append(str(cua_dir))
            log.debug(f"[bundled_runtime] Found cua_driver_dir: {cua_dir}")
        else:
            log.debug(f"[bundled_runtime] cua-driver dir exists but no binary: {cua_dir}")
    # cua-driver 不存在不警告：未预置场景属正常，用户可以在设置页按需安装。

    log.info(f"[bundled_runtime] Returning paths: {paths}")
    return paths


def _managed_system_path_dirs() -> list[str]:
    """Return OS-owned executable directories that the native sandbox exposes read-only."""
    if os.name == "nt":
        system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
        return [str(Path(system_root) / "System32"), system_root]
    if sys.platform == "darwin":
        return ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    return ["/usr/local/bin", "/usr/bin", "/bin"]


def _development_node_toolchain() -> tuple[list[str], tuple[Path, ...]]:
    """Expose the developer machine's Node toolchain when no packaged runtime exists."""
    if getattr(sys, "frozen", False):
        return [], ()
    path_dirs: list[str] = []
    readable_roots: list[Path] = []
    for name in ("node", "npm", "npx"):
        value = shutil.which(name)
        if not value:
            continue
        source = Path(value).expanduser()
        wrapper = source.resolve(strict=False)
        path_dir = str(source.parent.resolve(strict=False))
        if path_dir not in path_dirs:
            path_dirs.append(path_dir)
        wrapper_dir = Path(path_dir)
        if wrapper_dir.is_dir() and wrapper_dir not in readable_roots:
            readable_roots.append(wrapper_dir)
        # Unix npm/npx are commonly symlinks into <prefix>/lib/node_modules/npm/bin;
        # Node itself commonly lives in <prefix-or-version>/bin. Expose the smallest
        # package/version root plus the launcher directory. Do not expose the complete
        # Homebrew/user prefix merely because one executable lives below its bin/.
        if wrapper.parent != wrapper_dir:
            runtime_root = (
                wrapper.parent.parent
                if wrapper.parent.name.lower() == "bin"
                else wrapper.parent
            )
            if runtime_root.exists() and runtime_root not in readable_roots:
                readable_roots.append(runtime_root)
    return path_dirs, tuple(readable_roots)


def _development_python_toolchain() -> tuple[list[str], tuple[Path, ...]]:
    """Expose the developer machine's Python interpreter when no packaged runtime exists.

    打包态用 ``_internal/runtimes/python`` 整目录可读模拟；开发态用当前解释器
    (``sys.executable``) 定位 venv，把 venv 根目录与真实解释器根目录都作为 readable
    root 暴露，让沙箱里 ``python3`` 既能被 PATH 解析、又能 import venv 里装好的包。

    venv 往往跨两个目录树：``.venv/``（pyvenv.cfg + site-packages）与解释器真实
    根目录（uv/pyenv 创建的 ``.venv/bin/python`` 是符号链接，指向 ``~/.local/share/uv/
    python/.../bin/python3``，其动态库与标准库在同级 ``lib/`` 下）。只放行 venv 根
    会导致 ``libpython*.dylib`` 被沙箱拦截（dyld 报错），所以两个根目录都要放行。
    非 venv 解释器（系统 python）降级为只放行解释器所在 bin 目录，避免误放行 ``/usr``。
    """
    if getattr(sys, "frozen", False):
        return [], ()
    executable = Path(sys.executable).expanduser()
    if not executable.is_file():
        return [], ()
    bin_dir = executable.parent.resolve(strict=False)
    path_dirs = [str(bin_dir)] if bin_dir.is_dir() else []

    roots: list[Path] = []

    # ① 向上找 venv 根：含 pyvenv.cfg 的目录，或 bin 同级有 lib/<py>/site-packages。
    venv_root: Path | None = None
    for candidate in (bin_dir, *bin_dir.parents):
        if (candidate / "pyvenv.cfg").is_file():
            venv_root = candidate
            break
    if venv_root is None:
        # conda 环境没有 pyvenv.cfg，靠 bin 同级存在 lib/python*/site-packages 识别。
        for candidate in (bin_dir, *bin_dir.parents):
            lib = candidate / "lib"
            if lib.is_dir() and any(
                child.is_dir() and (child / "site-packages").is_dir()
                for child in lib.iterdir()
                if child.name.startswith("python")
            ):
                venv_root = candidate
                break
    if venv_root is not None:
        roots.append(venv_root.resolve(strict=False))

    # ② 解析符号链接后的真实解释器根目录（含 lib/ 动态库 + 标准库）。
    # uv/pyenv 的 venv python 是符号链接；自包含解释器（indygreg/Homebrew）解析后
    # 通常仍在 venv 内或自身目录，此时与 venv_root 相同，去重后不会重复放行。
    real_executable = executable.resolve(strict=True)
    real_root = real_executable.parent.parent.resolve(strict=False)
    if real_root != venv_root and real_root.is_dir() and (real_root / "lib").is_dir():
        # 只放行带 lib/ 的根（能装下 libpython*.dylib/标准库），避免误放行上层目录。
        if real_root not in roots:
            roots.append(real_root)

    if not roots:
        # 非 venv（系统 python）：保守只放行 bin 目录，不放大到 /usr。
        roots.append(bin_dir)
    return path_dirs, tuple(roots)


def _development_runtime_toolchain() -> tuple[list[str], tuple[Path, ...]]:
    """Combine the dev-mode Python and Node toolchains into one PATH + readable set."""
    py_dirs, py_roots = _development_python_toolchain()
    node_dirs, node_roots = _development_node_toolchain()
    # 保序去重合并 PATH 目录与 readable roots。
    path_dirs: list[str] = []
    for d in (*py_dirs, *node_dirs):
        if d not in path_dirs:
            path_dirs.append(d)
    roots: list[Path] = []
    for r in (*py_roots, *node_roots):
        if r not in roots:
            roots.append(r)
    return path_dirs, tuple(roots)


def managed_runtime_read_roots() -> tuple[Path, ...]:
    """Return read-only runtime roots needed by managed terminal commands."""
    roots = list(bundled_runtime_roots())
    if not roots:
        _paths, development_roots = _development_runtime_toolchain()
        roots.extend(development_roots)
    canonical = sorted(
        dict.fromkeys(root.resolve(strict=True) for root in roots if root.exists()),
        key=lambda value: len(value.parts),
    )
    return tuple(
        root
        for index, root in enumerate(canonical)
        if not any(parent == root or parent in root.parents for parent in canonical[:index])
    )


def bundled_runtime_roots() -> tuple[Path, ...]:
    """Return canonical packaged Python/Node roots trusted by managed execution.

    These roots are added to the managed sandbox's trusted-readable allowlist so
    the bundled interpreter/runtime stays usable inside the sandbox. Only
    meaningful in frozen (packaged) mode; dev runs return an empty tuple.
    """
    if not getattr(sys, "frozen", False):
        return ()
    runtimes_dir = Path(sys.executable).parent / "_internal" / "runtimes"
    return tuple(
        runtime.resolve(strict=True)
        for name in ("python", "node")
        if (runtime := runtimes_dir / name).is_dir()
    )


def bundled_python_executable() -> str | None:
    """返回打包内嵌 Python 解释器的可执行文件路径（仅冻结态有效）。

    PyInstaller 打包后 ``sys.executable`` 指向 gateway 二进制本身，而非 Python
    解释器。MCP server 等子进程通过 ``${CREW_PYTHON}`` 环境变量取解释器路径来
    执行 ``crew/mcp_servers/*.py``，若该变量指向 gateway 二进制，会导致 gateway
    把脚本路径当成 argv 重新执行 gateway 入口，**递归繁殖进程**。

    本函数在冻结态下定位打包内嵌的 Python 解释器，供 config.py 设置
    ``CREW_PYTHON``。非冻结态返回 ``None``（调用方应回退到 ``sys.executable``）。
    """
    if not getattr(sys, "frozen", False):
        return None

    exe_dir = Path(sys.executable).parent
    runtimes_dir = exe_dir / "_internal" / "runtimes"
    python_dir = runtimes_dir / "python"

    if not python_dir.is_dir():
        return None

    # Windows: python.exe 在根目录; macOS/Linux: python3 在 bin/ 子目录
    candidates = [
        python_dir / "python.exe",
        python_dir / "bin" / "python3",
        python_dir / "bin" / "python",  # 兜底（符号链接别名）
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def _prepend_path_unique(prefix_dirs: list[str], existing: str) -> str:
    """把 ``prefix_dirs`` 前置到 PATH 风格字符串，并去除 ``existing`` 中已存在的相同条目。

    使 ``runtime_env_overrides`` 在长生命周期进程（如 gateway）内被反复调用时保持
    幂等：不会把 runtime dirs 重复前置到上一轮已前置过的 PATH 上，避免 PATH
    反复膨胀最终超出 Windows 环境块长度限制。仅剔除与 ``prefix_dirs`` 字符串相等的
    条目（这些目录由本模块生成，形态可控），其余条目原序保留。
    """
    sep = os.pathsep
    keep = [p for p in existing.split(sep) if p and p not in prefix_dirs]
    return sep.join(prefix_dirs + keep)


def _current_owner_account_id() -> str:
    """返回当前上下文中的 owner_account_id；无上下文时为空串。"""
    try:
        from crew.core.runctx import current_owner_account_id

        return str(current_owner_account_id.get() or "").strip()
    except Exception:
        return ""


def get_crew_home() -> Path:
    """返回 Crew home 目录。

    默认为项目根目录下 {DEFAULT_HOME_DIRNAME}/。
    可通过 CREW_HOME 环境变量覆盖为任意路径。
    相对路径的 CREW_HOME 会解析为相对于用户家目录（~），而非进程 CWD。
    """
    import os

    val = os.environ.get("CREW_HOME", "").strip()
    if val:
        p = Path(val).expanduser()
        # 相对路径解析为相对于用户家目录，而非进程 CWD（与 load_config 保持一致）
        if not p.is_absolute():
            p = Path.home() / p
        return p
    # 打包运行环境下，默认指向用户家目录 ~/DEFAULT_HOME_DIRNAME，避免在只读 /opt 下写入导致失败
    if getattr(sys, "frozen", False):
        return Path.home() / DEFAULT_HOME_DIRNAME
    return ROOT / DEFAULT_HOME_DIRNAME


def get_owner_runtime_home(
    owner_account_id: str | None = None,
    *,
    create: bool = True,
) -> Path:
    """返回 owner 级运行时 home。

    - 传入 owner 时：``{CREW_HOME}/accounts/{owner_path_segment(owner)}/``
    - 未传入且当前上下文存在 owner 时：同上
    - 无 owner 时：回退基础 ``CREW_HOME``，保持 CLI / 启动期兼容
    """
    owner = str(owner_account_id or _current_owner_account_id() or "").strip()
    base = get_crew_home()
    if not owner:
        path = base
    elif _is_owner_runtime_home(base, owner):
        # CREW_HOME 已指向当前 owner home 时直接复用，避免重复拼接 accounts/<owner>。
        path = base
    else:
        path = _owner_runtime_home_for_base(base, owner, create=create)
    _validate_no_repeated_account_segment(path, owner_account_id=owner)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def runtime_env_overrides(
    env_file: str | Path | None = None,
    *,
    owner_account_id: str | None = None,
) -> dict[str, str]:
    """构造当前 owner 的运行时路径变量，不直接写入 ``os.environ``。

    冻结态（PyInstaller 打包产物）运行时，会将内嵌的 Python / Node.js
    运行时目录 **前置** 到 PATH，确保技能脚本的 ``python`` / ``node``
    命令解析到打包版本。
    """
    crew_home = get_crew_home().expanduser().resolve()
    owner_home = get_owner_runtime_home(owner_account_id).expanduser().resolve()
    env_path = (Path(env_file).expanduser() if env_file else owner_home / ".env").resolve()
    owner = str(owner_account_id or _current_owner_account_id() or "").strip()
    values: dict[str, str] = {
        # CREW_HOME 是安装/基础 home，不能在 owner 场景下改成 owner home。
        # 否则子进程再次解析 owner home 时会继续追加 accounts/<owner>。
        "CREW_HOME": str(crew_home),
        "CREW_OWNER_HOME": str(owner_home),
        "CREW_RUNTIME_HOME": str(owner_home),
        "CREW_SKILLS_DIR": str(owner_home / "skills"),
        "CREW_ENV_FILE": str(env_path),
        "DOTENV_CONFIG_PATH": str(env_path),
    }
    if owner:
        values["CREW_OWNER_ACCOUNT_ID"] = owner

    # 经 terminal 启动的子进程（尤其 Windows 管道场景）Python 默认 GBK stdout，
    # 技能脚本 print emoji 等 Unicode 会在子进程内 UnicodeEncodeError。
    # 与 process_registry 的 chcp 65001 + Popen(encoding=utf-8) 读端配合，从入口统一 UTF-8。
    values["PYTHONIOENCODING"] = "utf-8"
    if sys.platform == "win32":
        values["PYTHONUTF8"] = "1"

    # --- 打包运行时：将内嵌 Python / Node.js 注入 PATH ---
    runtime_dirs = _bundled_runtime_paths()
    if runtime_dirs:
        existing_path = os.environ.get("PATH", "")
        # 幂等前置：先剔除 existing_path 中已存在的 runtime dirs，再前置一次。
        # 否则长生命周期进程（gateway 每轮 envelope 热刷新）会把 runtime dirs 反复
        # 前置到上一轮已前置过的 PATH 上，导致 PATH 不断重复累积。
        values["PATH"] = _prepend_path_unique(runtime_dirs, existing_path)

        # --- 配置 pip 使用用户目录安装（避免安装包目录只读） ---
        # PIP_USER=1 使 pip install 默认使用 --user，安装到 ~/.local/ 或 %APPDATA%\Python\
        # 这样技能脚本的 `pip install requests` 不会因权限问题失败
        values["PIP_USER"] = "1"

        # 将用户 site-packages 加入 PYTHONPATH，确保 Python 能找到用户安装的包
        # Windows: %APPDATA%\Python\Python311\site-packages
        # Linux: ~/.local/lib/python3.11/site-packages
        # 注意：首次运行前目录可能不存在，但仍需设置 PYTHONPATH，pip 会自动创建
        if sys.platform == "win32":
            user_site = (
                Path(os.environ.get("APPDATA", ""))
                / "Python"
                / f"Python{sys.version_info.major}{sys.version_info.minor}"
                / "site-packages"
            )
        else:
            user_site = (
                Path.home()
                / ".local"
                / "lib"
                / f"python{sys.version_info.major}.{sys.version_info.minor}"
                / "site-packages"
            )

        # 无条件设置 PYTHONPATH（目录可能尚未创建，pip install 时会自动创建）
        # 幂等前置：与 PATH 同理，避免反复调用时 user_site 重复累积。
        existing_pythonpath = os.environ.get("PYTHONPATH", "")
        values["PYTHONPATH"] = _prepend_path_unique([str(user_site)], existing_pythonpath)

    return values


def managed_runtime_env_overrides(
    env_file: str | Path | None = None,
    *,
    owner_account_id: str | None = None,
) -> dict[str, str]:
    """Build a child environment without ambient executable/module paths."""
    values = runtime_env_overrides(env_file, owner_account_id=owner_account_id)
    runtime_paths = _bundled_runtime_paths()
    if not runtime_paths:
        runtime_paths, _roots = _development_runtime_toolchain()
    values["PATH"] = _prepend_path_unique(runtime_paths, os.pathsep.join(_managed_system_path_dirs()))
    values.pop("PYTHONPATH", None)
    values.pop("PIP_USER", None)
    return values


def _valid_env_key(key: object) -> str:
    text = str(key or "").strip()
    if not text or "\x00" in text or "=" in text:
        return ""
    return text


def _is_protected_runtime_env_name(name: str) -> bool:
    normalized = name.upper()
    return normalized in _PROTECTED_RUNTIME_ENV_NAMES or normalized.startswith(
        _PROTECTED_RUNTIME_ENV_PREFIXES
    )


def _load_runtime_env_map(path: Path) -> dict[str, str]:
    env_map, _ = _load_runtime_env_map_with_cache(path)
    return env_map


def _load_runtime_env_map_with_cache(path: Path) -> tuple[dict[str, str], bool]:
    path = path.expanduser().resolve()
    cache_key = f"env:{path}"
    signature = _file_signature(path)
    cached = _FILE_CACHE.get(cache_key)
    if cached is not None and cached[0] == signature:
        return (dict(cached[1]) if isinstance(cached[1], dict) else {}, True)
    if signature is None:
        _FILE_CACHE[cache_key] = (signature, {})
        return {}, False
    try:
        from crew.tools.file_utils import read_verified_bytes

        content = read_verified_bytes(path, max_bytes=1024 * 1024).decode("utf-8")
        raw = dotenv_values(stream=StringIO(content), interpolate=False)
    except (OSError, RuntimeError, UnicodeError, ValueError):
        logger.warning("ignored unsafe or unreadable runtime env file: %s", path)
        _FILE_CACHE[cache_key] = (signature, {})
        return {}, False
    result: dict[str, str] = {}
    for key, value in raw.items():
        name = _valid_env_key(key)
        if name and _is_protected_runtime_env_name(name):
            logger.warning("ignored protected runtime env variable %s", name)
            continue
        if name and value not in (None, ""):
            result[name] = str(value)
    _FILE_CACHE[cache_key] = (signature, dict(result))
    return result, False


def _file_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _system_env_paths() -> list[Path]:
    """Env files shared by all owners and supported by hot refresh."""
    return [ROOT / "config" / ".env"]


def _merged_runtime_env_map(owner_env_path: Path) -> dict[str, str]:
    env_map, _ = _merged_runtime_env_map_with_stats(owner_env_path)
    return env_map


def _merged_runtime_env_map_with_stats(owner_env_path: Path) -> tuple[dict[str, str], int, int]:
    merged: dict[str, str] = {}
    hits = 0
    total = 0
    for path in _system_env_paths():
        total += 1
        env_map, hit = _load_runtime_env_map_with_cache(path.expanduser().resolve())
        hits += 1 if hit else 0
        merged.update(env_map)
    total += 1
    env_map, hit = _load_runtime_env_map_with_cache(owner_env_path.expanduser().resolve())
    hits += 1 if hit else 0
    merged.update(env_map)
    return merged, hits, total


def _apply_runtime_env_map_unlocked(env_map: dict[str, str]) -> None:
    """Apply the merged system+owner env overlay and restore removed keys."""
    global _RUNTIME_ENV_KEYS
    env_map = {
        name: value for name, value in env_map.items() if not _is_protected_runtime_env_name(name)
    }
    new_keys = set(env_map)

    for key in sorted(_RUNTIME_ENV_KEYS - new_keys):
        if key not in _RUNTIME_ENV_BASELINE:
            os.environ.pop(key, None)
            continue
        previous = _RUNTIME_ENV_BASELINE.pop(key)
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous

    for key, value in env_map.items():
        if key not in _RUNTIME_ENV_BASELINE:
            _RUNTIME_ENV_BASELINE[key] = os.environ.get(key)
        os.environ[key] = value

    _RUNTIME_ENV_KEYS = new_keys


def refresh_owner_runtime_env(
    owner_account_id: str | None = None,
    *,
    env_file: str | Path | None = None,
    load_env_files: bool = True,
) -> dict[str, str]:
    """Refresh process env for the current owner without restarting the process.

    This is the hot path for owner context switching:
    - recompute owner runtime paths (CREW_ENV_FILE / CREW_SKILLS_DIR / etc.)
    - load config/.env plus the owner env overlay from CREW_ENV_FILE
    """
    total_started = time.perf_counter()
    paths_started = time.perf_counter()
    values = runtime_env_overrides(env_file, owner_account_id=owner_account_id)
    paths_ms = (time.perf_counter() - paths_started) * 1000.0

    with _ENV_LOCK:
        env_started = time.perf_counter()
        if load_env_files:
            env_map, env_cache_hits, env_cache_total = _merged_runtime_env_map_with_stats(
                Path(values["CREW_ENV_FILE"])
            )
        else:
            env_map, env_cache_hits, env_cache_total = {}, 0, 0
        env_ms = (time.perf_counter() - env_started) * 1000.0

        apply_started = time.perf_counter()
        for key, value in values.items():
            os.environ[key] = value
        if "CREW_OWNER_ACCOUNT_ID" not in values:
            os.environ.pop("CREW_OWNER_ACCOUNT_ID", None)
        if load_env_files:
            _apply_runtime_env_map_unlocked(env_map)
        return_values = dict(values)
        apply_ms = (time.perf_counter() - apply_started) * 1000.0
        total_ms = (time.perf_counter() - total_started) * 1000.0
        logger.info(
            "[PERF] runtime_env_refresh owner=%s total=%.2fms paths=%.2fms env=%.2fms "
            "apply=%.2fms env_cache=%d/%d",
            _mask_owner_for_log(owner_account_id),
            total_ms,
            paths_ms,
            env_ms,
            apply_ms,
            env_cache_hits,
            env_cache_total,
        )
        return return_values


def export_crew_runtime_env(
    env_file: str | Path | None = None,
    owner_account_id: str | None = None,
) -> dict[str, str]:
    """把 Crew 运行时路径注入 os.environ，供 skill 脚本和子进程直接读取。

    路径变量只描述路径，不包含 secret：
      CREW_HOME             基础 Crew 家目录（不会随 owner 改成账号目录）
      CREW_OWNER_HOME       owner 级私有 runtime home
      CREW_RUNTIME_HOME     owner 级私有 runtime home（兼容更通用命名）
      CREW_SKILLS_DIR       owner 私有 skills 目录
      CREW_ENV_FILE         Crew 可写 .env 路径
      DOTENV_CONFIG_PATH    Node dotenv/config 的标准路径变量
      PATH                  冻结态时前置内嵌 Python/Node.js 运行时目录

    """
    import os

    values = runtime_env_overrides(env_file, owner_account_id=owner_account_id)
    for key, value in values.items():
        os.environ[key] = value
    if "CREW_OWNER_ACCOUNT_ID" not in values:
        os.environ.pop("CREW_OWNER_ACCOUNT_ID", None)
    return values


def ensure_crew_home() -> Path:
    """确保 Crew home 目录结构存在，首次运行时初始化默认文件。

    创建子目录并写入默认的 SOUL.md、USER.md、MEMORY.md（仅当文件不存在时）。
    """
    home = get_crew_home()
    home.mkdir(parents=True, exist_ok=True)
    export_crew_runtime_env()

    # 子目录
    (home / "memories").mkdir(parents=True, exist_ok=True)
    (home / "skills").mkdir(parents=True, exist_ok=True)
    (home / "plugins").mkdir(parents=True, exist_ok=True)
    (home / "agents" / "default").mkdir(parents=True, exist_ok=True)
    (home / "teams").mkdir(parents=True, exist_ok=True)
    # 初始化默认文件（仅不存在时创建）
    _ensure_default_soul_md(home)
    _ensure_default_user_md(home)
    _ensure_default_memory_md(home)

    logger.info("Crew home 已就绪: %s", home)
    return home


def safe_path_segment(value: str | None, fallback: str = "default") -> str:
    """把 workspace/session/agent id 收敛成可用于目录名的安全片段。"""
    text = str(value or "").strip()
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._-")
    return text[:96] or fallback


def owner_path_segment(value: str | None, fallback: str = "legacy") -> str:
    """把 owner_account_id 映射成短且不泄露身份的目录名。

    owner id 是业务身份，不应直接进入文件系统路径：邮箱或外部账号标识
    既可能很长，也可能泄露隐私。这里使用稳定短 hash 作为目录段，并在入口
    拒绝路径形状的字符串，避免上游把 cwd / owner home 误写回 owner id 后继续
    拼出嵌套目录。
    """
    text = str(value or "").strip()
    if not text:
        return fallback
    _reject_path_like_owner_id(text)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:OWNER_DIR_HASH_CHARS]
    return f"{OWNER_DIR_PREFIX}{digest}"


def _reject_path_like_owner_id(owner_account_id: str) -> None:
    """拒绝把路径误当 owner id 传入目录派生链路。"""
    if (
        any(sep in owner_account_id for sep in ("/", "\\"))
        or owner_account_id in {".", ".."}
        or owner_account_id.startswith("~")
    ):
        raise ValueError(
            "owner_account_id 不能是路径形状的字符串；"
            f"got {owner_account_id!r}，疑似上游把 cwd 或 owner home 误写回 owner id"
        )


def _legacy_owner_path_segment(owner_account_id: str) -> str:
    """旧版本 owner 目录名；仅用于兼容迁移和污染环境识别。"""
    return safe_path_segment(owner_account_id, "legacy")


def _owner_runtime_home_for_base(base: Path, owner_account_id: str, *, create: bool) -> Path:
    """从基础 CREW_HOME 派生 owner home，并尽量迁移旧明文目录名。"""
    target = base / "accounts" / owner_path_segment(owner_account_id)
    legacy = base / "accounts" / _legacy_owner_path_segment(owner_account_id)
    if target == legacy:
        return target
    if create and legacy.exists() and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            legacy.rename(target)
            logger.info("已迁移 owner runtime home: %s -> %s", legacy, target)
        except OSError as exc:
            # 保留旧目录比让用户配置/会话突然不可见更稳；新的短 hash 路径会在下次可迁移时使用。
            logger.warning(
                "迁移 owner runtime home 失败，继续使用旧目录: %s -> %s (%s)", legacy, target, exc
            )
            return legacy
    return target


def _is_owner_runtime_home(base: Path, owner_account_id: str) -> bool:
    """判断传入 base 是否已经是当前 owner home（兼容旧污染环境）。"""
    if not owner_account_id or base.parent.name != "accounts":
        return False
    candidates = {
        owner_path_segment(owner_account_id),
        _legacy_owner_path_segment(owner_account_id),
    }
    name = base.name.lower() if os.name == "nt" else base.name
    normalized = {item.lower() if os.name == "nt" else item for item in candidates}
    return name in normalized


def _validate_no_repeated_account_segment(path: Path, *, owner_account_id: str) -> None:
    """提前拦截 accounts/<same>/accounts/<same> 这类自我嵌套。"""
    parts = path.parts
    seen: set[str] = set()
    for index, part in enumerate(parts[:-1]):
        if part != "accounts":
            continue
        segment = parts[index + 1]
        key = segment.lower() if os.name == "nt" else segment
        if key in seen:
            raise RuntimeError(
                "owner runtime home 出现重复 accounts 片段，疑似 CREW_HOME 被设置成 owner home："
                f"owner_account_id={owner_account_id!r} path={path}"
            )
        seen.add(key)


def _validate_task_workspace_path(path: Path, *, owner_account_id: str, workspace_id: str) -> None:
    """在 mkdir/Popen 前给任务工作目录做可读的结构与长度守卫。"""
    _validate_no_repeated_account_segment(path, owner_account_id=owner_account_id)
    budget = WINDOWS_WORKSPACE_PATH_BUDGET if os.name == "nt" else POSIX_WORKSPACE_PATH_BUDGET
    text = str(path)
    if len(text) > budget:
        raise RuntimeError(
            f"workspace_path 超长（{len(text)}>{budget}），疑似 owner/workspace 路径污染："
            f" owner_account_id={owner_account_id!r} workspace_id={workspace_id!r} path={path}"
        )


def get_task_workspace_root(*, create: bool = True) -> Path:
    """返回任务产物根目录。

    优先级由 config.py 写入环境变量：
    CREW_TASK_WORKSPACE_ROOT > runtime.task_workspace_root > {get_crew_home()}/task_workspaces。
    """
    import os

    val = os.environ.get("CREW_TASK_WORKSPACE_ROOT", "").strip()
    root = Path(val).expanduser() if val else get_crew_home() / "task_workspaces"
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def task_workspace_path(
    workspace_id: str = "default",
    *,
    owner_account_id: str | None = None,
    create: bool = True,
) -> Path:
    """返回 Layer 3 任务工作空间目录（backing workspace_id，在仓库外）。

    路径：
    - legacy / 无 owner：{task_workspace_root}/{workspace_id}/
    - owner 运行时：{owner_home}/task_workspaces/{workspace_id}/
    同一 workspace 下的所有 session / agent 共享此目录，产物统一落这里。
    """
    owner = str(owner_account_id or _current_owner_account_id() or "").strip()
    base = (
        get_owner_runtime_home(owner, create=create) / "task_workspaces"
        if owner
        else get_task_workspace_root(create=create)
    )
    root = base / safe_path_segment(workspace_id, "default")
    _validate_task_workspace_path(root, owner_account_id=owner, workspace_id=workspace_id)
    if create:
        base.mkdir(parents=True, exist_ok=True)
        root.mkdir(parents=True, exist_ok=True)
    return root


def agent_workspace_path(
    workspace_id: str = "default",
    agent_id: str = "main",
    *,
    owner_account_id: str | None = None,
    create: bool = True,
) -> Path:
    """返回团队任务中某个 agent 的独立执行子目录（预留给将来多 agent 场景）。

    路径：{task_workspace_path}/{workspace_id}/agents/{agent_id}/
    现阶段单 agent 不使用此路径；主 work_dir 始终是 task_workspace_path。
    """
    root = task_workspace_path(
        workspace_id,
        owner_account_id=owner_account_id,
        create=create,
    )
    path = root / "agents" / safe_path_segment(agent_id, "main")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def external_session_workspace_path(
    workspace_id: str,
    session_id: str,
    external_agent_id: str,
    *,
    owner_account_id: str | None = None,
    create: bool = True,
) -> Path:
    """Return the isolated default workspace for one external Agent session.

    Explicit ``cwd`` and user-bound ``workspace_root_path`` continue to take
    precedence in ``SingleAgent``.  This path only replaces the old shared
    fallback for new, unbound external sessions.
    """

    root = task_workspace_path(
        workspace_id,
        owner_account_id=owner_account_id,
        create=create,
    )
    path = (
        root
        / "external_sessions"
        / safe_path_segment(session_id, "session")
        / safe_path_segment(external_agent_id, "agent")
    )
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# 文件加载
# ---------------------------------------------------------------------------


def load_soul_md() -> str | None:
    """从 CREW_HOME/SOUL.md 加载 Agent 身份。

    返回文件内容（去除首尾空白），或 None（文件不存在或为空）。
    """
    soul_path = get_crew_home() / "SOUL.md"
    if not soul_path.exists():
        return None
    try:
        content = soul_path.read_text(encoding="utf-8").strip()
        return content or None
    except Exception as e:
        logger.debug("无法读取 SOUL.md: %s", e)
        return None


def load_memory_md(owner_account_id: str | None = None) -> str:
    """从 CREW_HOME/memories/MEMORY.md 加载持久记忆。

    返回文件内容（去除首尾空白），不存在或为空则返回空串。
    """
    path = get_owner_runtime_home(owner_account_id) / "memories" / "MEMORY.md"
    if not path.exists():
        return ""
    try:
        content = path.read_text(encoding="utf-8").strip()
        return content
    except Exception as e:
        logger.debug("无法读取 MEMORY.md: %s", e)
        return ""


def load_user_md(owner_account_id: str | None = None) -> str:
    """从 CREW_HOME/memories/USER.md 加载用户画像。

    返回文件内容（去除首尾空白），不存在或为空则返回空串。
    """
    path = get_owner_runtime_home(owner_account_id) / "memories" / "USER.md"
    if not path.exists():
        return ""
    try:
        content = path.read_text(encoding="utf-8").strip()
        return content
    except Exception as e:
        logger.debug("无法读取 USER.md: %s", e)
        return ""


# ---------------------------------------------------------------------------
# 默认文件内容
# ---------------------------------------------------------------------------

_DEFAULT_SOUL_MD = (
    "你是 Crew 平台的智能助手。你可以调用工具来完成任务。\n"
    "- 需要查看文件、执行命令时，主动使用提供的工具。\n"
    "- 工具返回结果后，结合结果继续推理，直到任务完成再给出最终回答。\n"
    "- 回答简洁、准确，用中文。\n"
)

_DEFAULT_USER_MD = (
    "_了解你正在帮助的人。随时间更新此文件。_\n"
    "\n"
    "**姓名：**\n"
    "\n"
    "**称呼：**\n"
    "\n"
    "**时区：**\n"
    "\n"
    "**备注：**\n"
)

_DEFAULT_MEMORY_MD = ""


def _ensure_default_soul_md(home: Path) -> None:
    """如果 SOUL.md 不存在则写入默认内容。"""
    soul_path = home / "SOUL.md"
    if not soul_path.exists():
        soul_path.write_text(_DEFAULT_SOUL_MD, encoding="utf-8")
        logger.info("已创建默认 SOUL.md: %s", soul_path)


def _ensure_default_user_md(home: Path) -> None:
    """如果 memories/USER.md 不存在则写入模板。"""
    path = home / "memories" / "USER.md"
    if not path.exists():
        path.write_text(_DEFAULT_USER_MD, encoding="utf-8")
        logger.info("已创建默认 USER.md: %s", path)


def _ensure_default_memory_md(home: Path) -> None:
    """如果 memories/MEMORY.md 不存在则创建空文件。"""
    path = home / "memories" / "MEMORY.md"
    if not path.exists():
        path.write_text(_DEFAULT_MEMORY_MD, encoding="utf-8")
        logger.info("已创建空 MEMORY.md: %s", path)
