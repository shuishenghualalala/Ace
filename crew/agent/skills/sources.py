"""User-registered local skill market directories.

The file is a small owner-global registry, separate from config.yaml so a
register/unregister never rewrites the whole application configuration. Only
directories are accepted; their skills are listed and copied on install. This
module never imports or executes anything from the registered directories.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from crew.state.home import get_crew_home
from crew.state.logging import get_logger
from .core import get_local_skills_dir, get_user_skills_dir

log = get_logger("skills.sources")

_MAX_SOURCES = 16
_MAX_PATH_CHARS = 1024
_REGISTRY_NAME = "skill_market_sources.json"
_lock = threading.Lock()


class SkillSourceError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _registry_path() -> Path:
    return get_crew_home() / _REGISTRY_NAME


def _normalized(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise SkillSourceError("source_not_absolute", "市场源必须是绝对路径")
    resolved = path.resolve()
    if not resolved.is_dir():
        raise SkillSourceError("source_not_directory", "市场源必须是已存在的目录")
    return resolved


def _forbidden_roots() -> tuple[Path, ...]:
    roots = []
    for getter in (get_user_skills_dir, get_local_skills_dir):
        try:
            roots.append(getter().resolve())
        except OSError:
            continue
    return tuple(roots)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _read_registry() -> list[dict]:
    path = _registry_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        log.warning("忽略无法解析的 Skill 市场源登记：%s", path)
        return []
    items = raw.get("directories") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return []
    result: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        source = str(item.get("path") or "").strip()
        label = str(item.get("label") or "").strip()
        added_at = str(item.get("added_at") or "")
        if not source:
            continue
        result.append({"path": source, "label": label or Path(source).name or source, "added_at": added_at})
    return result


def _write_registry(items: list[dict]) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "directories": [
            {"path": item["path"], "label": item["label"], "added_at": item.get("added_at")}
            for item in items
        ],
    }
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def list_skill_sources() -> list[dict]:
    """Return the registered source directories in registration order."""
    with _lock:
        return _read_registry()


def add_skill_source(raw_path: str, *, label: str = "") -> dict:
    text = str(raw_path or "").strip()
    if not text:
        raise SkillSourceError("source_empty", "市场源路径不能为空")
    if len(text) > _MAX_PATH_CHARS:
        raise SkillSourceError("source_too_long", "市场源路径过长")
    path = _normalized(text)
    for forbidden in _forbidden_roots():
        if path == forbidden or _inside(path, forbidden):
            raise SkillSourceError("source_overlaps_skills", "市场源不能位于已安装或本地技能目录内")
    display = str(label or "").strip()
    if len(display) > 80:
        display = display[:80]
    with _lock:
        items = _read_registry()
        key = os.path.normcase(str(path))
        if any(os.path.normcase(item["path"]) == key for item in items):
            raise SkillSourceError("source_exists", "该市场源已登记")
        if len(items) >= _MAX_SOURCES:
            raise SkillSourceError("source_limit", f"最多登记 {_MAX_SOURCES} 个市场源")
        entry = {
            "path": str(path),
            "label": display or path.name or str(path),
            "added_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_registry([*items, entry])
        return entry


def remove_skill_source(raw_path: str) -> bool:
    text = str(raw_path or "").strip()
    if not text:
        raise SkillSourceError("source_empty", "市场源路径不能为空")
    path = Path(text).expanduser()
    if path.is_absolute():
        try:
            key = os.path.normcase(str(path.resolve()))
        except OSError:
            key = os.path.normcase(str(path))
    else:
        key = os.path.normcase(text)
    with _lock:
        items = _read_registry()
        kept = [
            item for item in items
            if os.path.normcase(item["path"]) != key
            and os.path.normcase(item["label"]) != key
        ]
        if len(kept) == len(items):
            return False
        _write_registry(kept)
        return True
