"""上下文服务：文件上传、路径补全、附件内容解析与注入。

统一由 gateway/server.py 调用，把附件和 @引用 的业务逻辑集中在这里，
避免 server.py 膨胀。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from crew.state.logging import get_logger
from crew.state.home import get_owner_runtime_home, task_workspace_path
from crew.browser.driver import BrowserDriverError

log = get_logger("context")

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
    upload_dir.mkdir(parents=True, exist_ok=True)
    return upload_dir


# ---------------------------------------------------------------------------
# 文件上传
# ---------------------------------------------------------------------------

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
    upload_dir = _ensure_upload_dir(owner_account_id)

    # 校验 filename：禁止路径分隔符与 NUL（防 ../、绝对路径、NUL 注入）
    if any(sep in filename for sep in ("/", "\\")) or "\x00" in filename:
        raise ValueError("非法文件名")
    safe_name = Path(filename).name
    if not safe_name or safe_name in (".", ".."):
        raise ValueError("非法文件名")

    stem = Path(safe_name).stem or "untitled"
    suffix = Path(safe_name).suffix
    # 内嵌 uuid 消除 TOCTOU：并发同名上传落不同文件，互不覆盖
    unique = f"{stem}_{uuid4().hex}{suffix}"
    dest = upload_dir / unique
    dest.write_bytes(content_bytes)
    log.info("附件已保存: %s", dest)

    file_id = f"att_{dest.stem}"
    return {
        "id": file_id,
        "name": filename,
        "path": str(dest),
        "type": _classify_file(filename),
        "size": len(content_bytes),
        # 前端可直接用此 URL 预览图像（/api/uploads 静态服务）
        "previewUrl": f"/api/uploads/{dest.name}" if _classify_file(filename) == "image" else None,
    }


def _classify_file(filename: str) -> str:
    """根据扩展名分类附件类型。"""
    ext = Path(filename).suffix.lower()
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"):
        return "image"
    return "file"


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
    refs: list[dict[str, str]] = []
    for tab_id in tab_ids:
        if manager is None:
            refs.append({"tab_id": tab_id, "error": "Browser Use 未启用"})
            continue
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
