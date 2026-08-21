"""上下文服务：文件上传、路径补全、附件内容解析与注入。

统一由 gateway/server.py 调用，把附件和 @引用 的业务逻辑集中在这里，
避免 server.py 膨胀。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable
from uuid import uuid4

from crew.state.logging import get_logger
from crew.state.home import get_owner_runtime_home, task_workspace_path
from crew.tools.file_utils import (
    _ensure_private_directory,
    atomic_replace_bytes,
    snapshot_file,
    stat_verified_file,
)
from crew.browser.driver import BrowserDriverError

log = get_logger("context")
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024
_MAX_UPLOAD_STORE_BYTES = 256 * 1024 * 1024
_MAX_REQUEST_ATTACHMENT_BYTES = 128 * 1024 * 1024
_MAX_ATTACHMENTS = 32
_MAX_INLINE_ATTACHMENT_CHARS = 100_000
_MAX_UPLOAD_FILENAME_CHARS = 180
_UPLOAD_DEDUP_FILE = ".dedup.json"
_UPLOAD_DEDUP_MAX_ENTRIES = 8192

# 与 runtime / Desktop history-mapping 一致：用户消息里的附件落盘标记。
_ATTACHMENT_MARKER_RE = re.compile(r"^附件「([^」]+)」位于[：:]\s*(.+)$", re.MULTILINE)
_STRUCTURED_PATH_REFERENCE_RE = re.compile(
    r"(?:^|\s)@(?P<kind>file|folder|image):(?P<path>[^\s@]+)"
)
# 桌面端 Composer 的 @浏览器标签页 token：id 为字母数字/中划线/下划线。
_BROWSER_TAB_REFERENCE_RE = re.compile(
    r"(?:^|\s)@browser_tab:(?P<tab_id>[A-Za-z0-9_-]+)"
)
# 单个标签页注入上限：正文在 read_tab_content 内已截到 8000，注入上下文再收紧，
# 防止一个长页面挤占整轮对话。
_BROWSER_TAB_TEXT_LIMIT = 4000


def _get_upload_dir(owner_account_id: str | None = None) -> Path:
    """返回上传文件存储根目录（延迟求值，确保 load_config 已设置 CREW_HOME）。"""
    return get_owner_runtime_home(owner_account_id) / "uploads"


def _ensure_upload_dir(owner_account_id: str | None = None) -> Path:
    upload_dir = _get_upload_dir(owner_account_id)
    _ensure_private_directory(upload_dir)
    return upload_dir


# ---------------------------------------------------------------------------
# 文件上传
# ---------------------------------------------------------------------------
@contextmanager
def _upload_quota_lock(upload_dir: Path):
    """Hold a cross-process owner-local lock while checking the upload quota."""
    lock_path = upload_dir / ".quota.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    locked = False
    try:
        opened = os.fstat(descriptor)
        if opened.st_size == 0:
            os.write(descriptor, b"0")
        actual = lock_path.lstat()
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            not stat.S_ISREG(actual.st_mode)
            or getattr(actual, "st_file_attributes", 0) & reparse_flag
            or (actual.st_dev, actual.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise OSError("上传配额锁不是可验证的普通文件")
        if os.name == "nt":
            import ctypes
            import msvcrt
            from ctypes import wintypes

            class _Overlapped(ctypes.Structure):
                _fields_ = [
                    ("Internal", wintypes.ULONG),
                    ("InternalHigh", wintypes.ULONG),
                    ("Offset", wintypes.DWORD),
                    ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.LockFileEx.argtypes = [
                wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped),
            ]
            kernel32.UnlockFileEx.argtypes = [
                wintypes.HANDLE, wintypes.DWORD,
                wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped),
            ]
            overlapped = _Overlapped()
            if not kernel32.LockFileEx(
                msvcrt.get_osfhandle(descriptor), 0x00000002, 0, 1, 0,
                ctypes.byref(overlapped),
            ):
                raise OSError("无法锁定上传配额", None, os.strerror(ctypes.get_last_error()))
            locked = True
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
            locked = True
        yield
    finally:
        if locked and os.name == "nt":
            import ctypes
            import msvcrt
            from ctypes import wintypes

            class _Overlapped(ctypes.Structure):
                _fields_ = [
                    ("Internal", wintypes.ULONG),
                    ("InternalHigh", wintypes.ULONG),
                    ("Offset", wintypes.DWORD),
                    ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.UnlockFileEx.argtypes = [
                wintypes.HANDLE, wintypes.DWORD,
                wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped),
            ]
            overlapped = _Overlapped()
            kernel32.UnlockFileEx(
                msvcrt.get_osfhandle(descriptor), 0, 1, 0,
                ctypes.byref(overlapped),
            )
        elif locked:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _upload_store_bytes(upload_dir: Path) -> int:
    """Return logical bytes occupied by owner-owned regular upload files."""
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    total = 0
    with os.scandir(upload_dir) as entries:
        for entry in entries:
            if entry.name in {".quota.lock", _UPLOAD_DEDUP_FILE}:
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if (
                stat.S_ISREG(info.st_mode)
                and not getattr(info, "st_file_attributes", 0) & reparse_flag
            ):
                total += info.st_size
    return total


def _upload_dedup_path(upload_dir: Path) -> Path:
    return upload_dir / _UPLOAD_DEDUP_FILE


def _read_upload_dedup_registry(upload_dir: Path) -> dict[str, str]:
    """Load the owner-local content digest to stored-name dedup registry.

    The registry is a trust boundary: a corrupted or path-injecting entry must
    fail the upload closed rather than be ignored and allow a duplicate side
    effect.
    """
    path = _upload_dedup_path(upload_dir)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    if len(raw) > 1_000_000:
        raise ValueError("上传去重登记表过大")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("上传去重登记表损坏") from None
    if not isinstance(payload, dict):
        raise ValueError("上传去重登记表格式无效")
    registry: dict[str, str] = {}
    for digest, stored_name in payload.items():
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise ValueError("上传去重摘要无效")
        if (
            not isinstance(stored_name, str)
            or not stored_name
            or Path(stored_name).name != stored_name
            or stored_name in {".", ".."}
        ):
            raise ValueError("上传去重文件名无效")
        registry[digest] = stored_name
    return registry


def _write_upload_dedup_registry(upload_dir: Path, registry: dict[str, str]) -> None:
    payload = json.dumps(
        registry,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    path = _upload_dedup_path(upload_dir)
    atomic_replace_bytes(path, payload, snapshot_file(path))


def _upload_meta(
    filename: str,
    dest: Path,
    size: int,
    *,
    deduplicated: bool,
) -> dict[str, Any]:
    return {
        "id": f"att_{dest.stem}",
        "name": filename,
        "path": str(dest),
        "type": _classify_file(filename),
        "size": size,
        # 前端可直接用此 URL 预览图像（/api/uploads 静态服务）
        "previewUrl": f"/api/uploads/{dest.name}" if _classify_file(filename) == "image" else None,
        "deduplicated": deduplicated,
    }


def save_upload(
    filename: str,
    content_bytes: bytes,
    owner_account_id: str | None = None,
) -> dict[str, Any]:
    """保存上传文件，返回附件元信息。

    安全：
    - 拒绝含路径分隔符 / NUL 的 filename（防路径穿越）；
    - 存储文件名内嵌 uuid4，消除同名并发上传的 TOCTOU 覆盖（原文件名仍保留在返回的
      ``name`` 字段供前端展示）。
    """
    if (
        not isinstance(filename, str)
        or not filename
        or len(filename) > _MAX_UPLOAD_FILENAME_CHARS
        or any(ord(char) < 0x20 for char in filename)
        or any(char in filename for char in '/\\<>:"|?*')
        or filename.endswith((".", " "))
    ):
        raise ValueError("非法文件名")
    if not isinstance(content_bytes, bytes):
        raise ValueError("附件内容必须是二进制")
    if len(content_bytes) > _MAX_UPLOAD_BYTES:
        raise ValueError(f"附件大小超过上限 {_MAX_UPLOAD_BYTES} 字节")

    # 校验 filename：禁止路径分隔符与 NUL（防 ../、绝对路径、NUL 注入）
    safe_name = Path(filename).name
    if not safe_name or safe_name in (".", ".."):
        raise ValueError("非法文件名")

    upload_dir = _ensure_upload_dir(owner_account_id)
    stem = Path(safe_name).stem or "untitled"
    suffix = Path(safe_name).suffix
    # 幂等键 = 原始文件名 + 内容摘要：同名同内容重试复用同一文件，不重复落盘。
    content_digest = hashlib.sha256(safe_name.encode("utf-8") + b"\x00" + content_bytes).hexdigest()
    with _upload_quota_lock(upload_dir):
        registry = _read_upload_dedup_registry(upload_dir)
        existing_name = registry.get(content_digest)
        if existing_name is not None:
            existing = upload_dir / existing_name
            try:
                existing_version = snapshot_file(existing, max_bytes=_MAX_UPLOAD_BYTES)
            except FileNotFoundError:
                existing_version = None
            if existing_version is not None and existing_version.digest == hashlib.sha256(content_bytes).hexdigest():
                log.info("附件重复上传已去重: %s", existing)
                return _upload_meta(filename, existing, len(content_bytes), deduplicated=True)
            # 文件被回收或内容被替换：丢弃过期登记并重新保存。
            registry.pop(content_digest, None)
        if _upload_store_bytes(upload_dir) + len(content_bytes) > _MAX_UPLOAD_STORE_BYTES:
            raise ValueError(f"owner 上传存储超过上限 {_MAX_UPLOAD_STORE_BYTES} 字节")
        # 内嵌 uuid 保留不可猜测文件名，同时消除并发同名 TOCTOU。
        unique = f"{stem}_{uuid4().hex}{suffix}"
        dest = upload_dir / unique
        atomic_replace_bytes(dest, content_bytes, snapshot_file(dest))
        registry[content_digest] = unique
        # ponytail: 上限内简单淘汰最老登记，超过上限的重放允许重新落盘。
        while len(registry) > _UPLOAD_DEDUP_MAX_ENTRIES:
            registry.pop(next(iter(registry)))
        _write_upload_dedup_registry(upload_dir, registry)
    log.info("附件已保存: %s", dest)

    return _upload_meta(filename, dest, len(content_bytes), deduplicated=False)


def _classify_file(filename: str) -> str:
    """根据扩展名分类附件类型。"""
    ext = Path(filename).suffix.lower()
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"):
        return "image"
    return "file"


def normalize_agent_attachments(
    raw_attachments: object,
    owner_account_id: str,
) -> list[dict[str, Any]]:
    """Normalize WebSocket attachments to files owned by the authenticated account.

    The browser normally sends the result of :func:`save_upload`, but the WebSocket
    payload is still untrusted input.  Keeping only resolved regular files below the
    owner's upload root prevents a client from turning the prompt-building path into
    an arbitrary host-file reader.  The agent performs an identity-checked read later
    to cover the remaining rename/symlink race.
    """
    if not isinstance(raw_attachments, list):
        return []
    try:
        uploads_root = Path(
            os.path.abspath(_get_upload_dir(owner_account_id).expanduser())
        )
    except (OSError, ValueError):
        return []

    normalized: list[dict[str, Any]] = []
    total_bytes = 0
    for raw in raw_attachments:
        if len(normalized) >= _MAX_ATTACHMENTS:
            break
        if not isinstance(raw, dict):
            continue
        raw_path = str(raw.get("path") or "").strip()
        if not raw_path:
            # Text-only attachment payloads contain no host path and are safe to
            # preserve; they never cause a server-side file read.
            if (
                isinstance(raw.get("content"), str)
                and len(raw["content"]) <= _MAX_INLINE_ATTACHMENT_CHARS
            ):
                content = raw["content"]
                content_bytes = len(content.encode("utf-8"))
                if total_bytes + content_bytes > _MAX_REQUEST_ATTACHMENT_BYTES:
                    raise ValueError("本轮附件总量超过安全上限")
                total_bytes += content_bytes
                normalized.append({
                    "id": str(raw.get("id") or f"att_{uuid4().hex}"),
                    "name": str(raw.get("name") or "附件")[:256],
                    "type": "file",
                    "content": raw["content"],
                })
            continue
        try:
            candidate = Path(os.path.abspath(Path(raw_path).expanduser()))
            candidate.relative_to(uploads_root)
            size = stat_verified_file(candidate).st_size
            if size > _MAX_UPLOAD_BYTES:
                continue
        except (OSError, ValueError, RuntimeError):
            continue

        if total_bytes + size > _MAX_REQUEST_ATTACHMENT_BYTES:
            raise ValueError("本轮附件总量超过安全上限")
        total_bytes += size
        name = str(raw.get("name") or candidate.name).replace("\\", "/")
        name = Path(name).name[:256] or candidate.name
        normalized.append({
            "id": str(raw.get("id") or f"att_{uuid4().hex}"),
            "name": name,
            "path": str(candidate),
            "type": _classify_file(name),
            "size": size,
        })
    return normalized


def resolve_structured_path_references(
    query: str,
    *,
    workspace_root: str,
) -> list[dict[str, str]]:
    """Resolve UI-generated @file/@folder references inside the bound workspace.

    Plain-text absolute paths are intentionally not grants.  Only the structured
    tokens emitted by ``complete_path`` are accepted, and every resolved target
    must stay below the server-known workspace root.
    """
    try:
        root = Path(workspace_root).expanduser().resolve()
    except (OSError, ValueError):
        return []
    if not root.is_dir():
        return []
    refs: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in _STRUCTURED_PATH_REFERENCE_RE.finditer(str(query or "")):
        raw = match.group("path").strip()
        try:
            target = (root / raw).resolve()
            target.relative_to(root)
        except (OSError, ValueError):
            continue
        kind = match.group("kind")
        if kind == "folder":
            if not target.is_dir():
                continue
            resource_type = "directory"
        else:
            if not target.is_file():
                continue
            resource_type = "file"
        key = str(target)
        if key in seen:
            continue
        seen.add(key)
        refs.append({"path": key, "resource_type": resource_type})
    return refs


async def resolve_browser_tab_references(
    query: str,
    *,
    manager: Any,
    owner_account_id: str,
    session_id: str,
) -> list[dict[str, str]]:
    """解析消息里的 ``@browser_tab:<id>`` 引用，发送时取回标签页正文。

    与 @file: 的授权语义不同：浏览器标签页没有可授予的文件路径，只能在发送时
    把正文快照并入上下文（消费方为 runtime 的 browser_tab_references 块）。
    标签页已关闭/无浏览器会话/人工接管中等情况返回带原因的占位条目，
    **不阻断发送**。
    """
    tab_ids: list[str] = []
    for match in _BROWSER_TAB_REFERENCE_RE.finditer(str(query or "")):
        tab_id = match.group("tab_id")
        if tab_id not in tab_ids:
            tab_ids.append(tab_id)
    if not tab_ids:
        return []
    if manager is None:
        return [{"tab_id": tab_id, "error": "Browser Use 未启用"} for tab_id in tab_ids]
    refs: list[dict[str, str]] = []
    for tab_id in tab_ids:
        try:
            content = await manager.read_tab_content(
                owner_account_id,
                session_id,
                tab_id,
                max_chars=_BROWSER_TAB_TEXT_LIMIT,
            )
        except BrowserDriverError as exc:
            refs.append({"tab_id": tab_id, "error": str(exc)[:200]})
            continue
        except Exception as exc:  # noqa: BLE001 - 单条引用失败不阻断发送
            log.warning("读取浏览器标签页引用失败 tab=%s: %s", tab_id, exc)
            refs.append({"tab_id": tab_id, "error": f"读取失败: {exc}"[:200]})
            continue
        refs.append({"tab_id": tab_id, **content})
    return refs


# ---------------------------------------------------------------------------
# @引用 注入注册表
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ReferenceResolveContext:
    """发送时 @引用 解析的统一输入（由 app 从 Envelope 与应用单例提取）。"""

    query: str
    owner_account_id: str
    session_id: str
    # 工作区 enrichment 算出的根；None/空表示 enrichment 失败，跳过路径引用解析
    workspace_root: str
    browser_manager: Any  # None 表示 Browser Use 未启用


@dataclass(frozen=True, slots=True)
class ReferenceInjector:
    """一种 @引用 的发送时注入描述。

    - ``token_re``：识别 query 中该类型 token 的正则（兼作解析前的快速判存）；
    - ``resolver``：把 query 解析为引用条目列表，失败须自行降级、不阻断发送；
    - ``formatter``：把引用条目格式化为注入模型上下文的 reminder 块；
      ``None`` 表示该类型不产出文本块（如 @file 路径引用，消费方是读取授权）。
    """

    name: str
    params_key: str
    token_re: re.Pattern[str]
    resolver: Callable[[ReferenceResolveContext], Awaitable[list[dict[str, str]]]]
    formatter: Callable[[object], str] | None


async def _resolve_structured_path_entry(ctx: ReferenceResolveContext) -> list[dict[str, str]]:
    """@file:/@folder:/@image: 路径引用：在绑定工作区内解析为授权路径。"""
    if not ctx.workspace_root:
        return []
    return resolve_structured_path_references(ctx.query, workspace_root=ctx.workspace_root)


async def _resolve_browser_tab_entry(ctx: ReferenceResolveContext) -> list[dict[str, str]]:
    """@browser_tab:<id> 引用：发送时取回标签页正文快照（含失败占位）。"""
    return await resolve_browser_tab_references(
        ctx.query,
        manager=ctx.browser_manager,
        owner_account_id=ctx.owner_account_id,
        session_id=ctx.session_id,
    )


def _format_browser_tab_entry(refs: object) -> str:
    """委托 runtime 的现有格式化器（延迟 import：runtime 依赖本模块注册表，避免循环）。"""
    from crew.agent.runtime import _format_browser_tab_references

    return _format_browser_tab_references(refs)


# 发送时 @引用 注册表：app.handle 按序解析注入 envelope.params，runtime 按序拼接
# reminder 块。新增一种 @引用 类型时在此登记一项即可。
REFERENCE_INJECTORS: tuple[ReferenceInjector, ...] = (
    ReferenceInjector(
        name="structured_path",
        params_key="referenced_paths",
        token_re=_STRUCTURED_PATH_REFERENCE_RE,
        resolver=_resolve_structured_path_entry,
        # 路径引用不产生 reminder 文本：消费方为 external executor / team 的读取授权
        formatter=None,
    ),
    ReferenceInjector(
        name="browser_tab",
        params_key="browser_tab_references",
        token_re=_BROWSER_TAB_REFERENCE_RE,
        resolver=_resolve_browser_tab_entry,
        formatter=_format_browser_tab_entry,
    ),
)


def _safe_unlink_under_uploads(path: Path, uploads_root: Path) -> bool:
    """仅删除落在 uploads_root 下的文件；路径逃逸则拒绝。"""
    try:
        resolved = path.expanduser().resolve()
        root = uploads_root.expanduser().resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return False
    if not resolved.is_file():
        return False
    try:
        resolved.unlink(missing_ok=True)
        return True
    except OSError as exc:
        log.warning("删除上传附件失败 %s: %s", resolved, exc)
        return False


def collect_upload_paths_from_messages(
    messages: Iterable[Any],
    owner_account_id: str | None = None,
) -> list[Path]:
    """从会话历史消息中收集落在该账号 uploads/ 下的附件路径。"""
    uploads_root = _get_upload_dir(owner_account_id)
    try:
        root = uploads_root.expanduser().resolve()
    except OSError:
        return []
    found: list[Path] = []
    seen: set[str] = set()
    for msg in messages:
        content = getattr(msg, "content", None)
        if isinstance(content, str) and content:
            for match in _ATTACHMENT_MARKER_RE.finditer(content):
                raw = str(match.group(2) or "").strip()
                if not raw:
                    continue
                candidate = Path(raw)
                try:
                    resolved = candidate.expanduser().resolve()
                    resolved.relative_to(root)
                except (OSError, ValueError):
                    continue
                key = str(resolved)
                if key not in seen:
                    seen.add(key)
                    found.append(resolved)
        # 结构化 attachments（渠道入站等）
        attachments = getattr(msg, "attachments", None)
        if isinstance(attachments, list):
            for att in attachments:
                raw_path = ""
                if isinstance(att, dict):
                    raw_path = str(att.get("path") or "").strip()
                else:
                    raw_path = str(getattr(att, "path", "") or "").strip()
                if not raw_path:
                    continue
                candidate = Path(raw_path)
                try:
                    resolved = candidate.expanduser().resolve()
                    resolved.relative_to(root)
                except (OSError, ValueError):
                    continue
                key = str(resolved)
                if key not in seen:
                    seen.add(key)
                    found.append(resolved)
    return found


def delete_session_uploads(
    messages: Iterable[Any],
    owner_account_id: str | None = None,
) -> int:
    """按会话历史引用删除 uploads/ 下附件，返回成功删除的文件数。"""
    uploads_root = _get_upload_dir(owner_account_id)
    deleted = 0
    for path in collect_upload_paths_from_messages(messages, owner_account_id):
        if _safe_unlink_under_uploads(path, uploads_root):
            deleted += 1
    return deleted


# ---------------------------------------------------------------------------
# 路径补全（@引用）
# ---------------------------------------------------------------------------

def complete_path(
    query: str,
    cwd: str | None = None,
    *,
    workspace_id: str | None = None,
    workspace_root_path: str | None = None,
    owner_account_id: str | None = None,
) -> list[dict[str, str]]:
    """根据查询字符串补全文件/文件夹路径。

    返回 [{text, display, meta, type}] 格式列表。

    安全：解析后的 search_dir 必须落在 base 内；query 含 ``..`` 时拒绝。

    base 优先级：显式 ``cwd`` > ``workspace_root_path``（用户绑定的本地目录）
    > ``workspace_id`` 任务目录 > default 任务目录。
    绑定本地目录时允许在 crew_home 之外补全（用户已显式授权该路径）。
    """
    crew_home = get_owner_runtime_home(owner_account_id, create=False)

    # ---- 1. 解析 base ----
    if cwd:
        base_raw = Path(cwd)
        require_crew_home = True
    elif workspace_root_path and str(workspace_root_path).strip():
        base_raw = Path(str(workspace_root_path).strip()).expanduser()
        require_crew_home = False
    elif workspace_id:
        base_raw = task_workspace_path(
            workspace_id,
            owner_account_id=owner_account_id,
            create=False,
        )
        require_crew_home = True
    else:
        base_raw = task_workspace_path(
            "default",
            owner_account_id=owner_account_id,
            create=False,
        )
        require_crew_home = True
    # 拒绝 query/cwd 里显式的 ``..`` 片段（无论是否最终逃出，补全接口都不应跟入父目录）
    raw_text = f"{cwd or ''}"
    if ".." in Path(raw_text).parts or ".." in Path(query or "").parts:
        return []
    try:
        base = base_raw.resolve()
    except (OSError, ValueError):
        return []
    try:
        crew_home_resolved = crew_home.resolve()
    except (OSError, ValueError):
        crew_home_resolved = crew_home
    if require_crew_home and not base.is_relative_to(crew_home_resolved):
        return []
    if not base.is_dir():
        return []

    # 去掉 @ 前缀
    clean = query.lstrip("@")
    # 支持 @file: / @folder: / @image: 前缀
    kind_filter = ""
    for prefix in ("file:", "folder:", "image:"):
        if clean.startswith(prefix):
            kind_filter = prefix.rstrip(":")
            clean = clean[len(prefix):]
            break

    search_dir = base
    pattern = clean
    recursive = bool(clean) and "/" not in clean and "\\" not in clean

    # 如果包含路径分隔符，在父目录下搜索；单段查询则递归匹配工作区内的文件名，
    # 让用户不必先知道完整目录层级。
    if "/" in clean or "\\" in clean:
        parts = Path(clean)
        search_dir = base / parts.parent
        pattern = parts.name
        recursive = False

    # 解析后的 search_dir 仍必须在 crew_home 内（防 ../ 逃逸）
    try:
        search_dir_resolved = search_dir.resolve()
    except (OSError, ValueError):
        return []
    if not search_dir_resolved.is_relative_to(base):
        return []

    if not search_dir.is_dir():
        return []

    def iter_entries() -> Iterable[Path]:
        if not recursive:
            yield from sorted(search_dir.iterdir(), key=lambda item: item.name.lower())
            return

        # ponytail: skip generated/dependency trees; recursive completion must stay
        # interactive, and users can still use an explicit path to enter these dirs.
        ignored_dirs = {".git", ".venv", "__pycache__", "node_modules", "dist", "build"}
        try:
            for current, dirnames, filenames in os.walk(search_dir, topdown=True, followlinks=False):
                dirnames[:] = sorted(
                    name
                    for name in dirnames
                    if not name.startswith(".") and name not in ignored_dirs
                )
                current_path = Path(current)
                for name in sorted((*dirnames, *filenames), key=str.lower):
                    yield current_path / name
        except OSError:
            return

    results: list[dict[str, str]] = []
    try:
        for entry in iter_entries():
            if entry.name.startswith(".") and not pattern.startswith("."):
                continue
            if pattern and not entry.name.lower().startswith(pattern.lower()):
                continue

            is_dir = entry.is_dir()
            # 类型过滤
            if kind_filter == "folder" and not is_dir:
                continue
            if kind_filter == "image" and (is_dir or _classify_file(entry.name) != "image"):
                continue
            if kind_filter == "file" and is_dir:
                continue

            rel_path = entry.relative_to(base).as_posix()
            results.append({
                "text": f"@file:{rel_path}" if not is_dir else f"@folder:{rel_path}",
                "display": entry.name,
                # meta 用相对路径，避免把服务器绝对路径泄露给前端
                "meta": rel_path,
                "type": "folder" if is_dir else _classify_file(entry.name),
            })

            if len(results) >= 20:
                break
    except PermissionError:
        pass

    return results
