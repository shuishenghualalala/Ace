"""User-registered skill market sources: local directories and HTTPS manifests.

The registry lives beside ``config.yaml`` so registering or removing a source
never rewrites the whole application configuration. Local directories are
scanned in place. HTTPS manifests carry inline ``SKILL.md`` content; a
validated copy is cached under the Crew home and installed like any other
prepared skill. This module never imports or executes anything from a source.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from crew.state.home import get_crew_home
from crew.state.logging import get_logger
from .core import get_local_skills_dir, get_user_skills_dir

log = get_logger("skills.sources")

_MAX_SOURCES = 16
_MAX_PATH_CHARS = 1024
_MAX_URL_CHARS = 2048
_MAX_MANIFEST_BYTES = 256 * 1024
_MAX_SKILLS = 50
_MAX_SKILL_CONTENT = 64 * 1024
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_REGISTRY_NAME = "skill_market_sources.json"
_CACHE_DIR = "skill_market_cache"
_lock = threading.Lock()


class SkillSourceError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _registry_path() -> Path:
    return get_crew_home() / _REGISTRY_NAME


def _cache_parent() -> Path:
    return get_crew_home() / _CACHE_DIR


def _normalized_dir(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise SkillSourceError("source_not_absolute", "市场源必须是绝对路径或 HTTPS 地址")
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _public(entry: dict) -> dict:
    return {key: value for key, value in entry.items() if key != "cache_dir"}


def _read_registry() -> list[dict]:
    path = _registry_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        log.warning("忽略无法解析的 Skill 市场源登记：%s", path)
        return []
    if not isinstance(raw, dict):
        return []
    items = raw.get("sources")
    if not isinstance(items, list):
        items = raw.get("directories") if isinstance(raw.get("directories"), list) else []
    result: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        added_at = str(item.get("added_at") or "")
        url = str(item.get("url") or "").strip()
        if item.get("kind") == "url" or url:
            cache_dir = str(item.get("cache_dir") or "").strip()
            if not url or not cache_dir:
                continue
            result.append({
                "kind": "url", "url": url, "cache_dir": cache_dir,
                "label": label or (urlsplit(url).hostname or url), "added_at": added_at,
            })
            continue
        source = str(item.get("path") or "").strip()
        if not source:
            continue
        result.append({
            "kind": "dir", "path": source, "label": label or Path(source).name or source,
            "added_at": added_at,
        })
    return result


def _write_registry(items: list[dict]) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 2, "sources": [dict(item) for item in items]}
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
    """Return registered sources (without the internal cache path)."""
    with _lock:
        return [_public(item) for item in _read_registry()]


def _skill_source_entries() -> list[dict]:
    """Internal view that keeps URL cache directories for listing/install."""
    with _lock:
        return _read_registry()


def _validate_remote_url(raw: str) -> str:
    if len(raw) > _MAX_URL_CHARS:
        raise SkillSourceError("source_too_long", "市场源 URL 过长")
    parsed = urlsplit(raw)
    if parsed.scheme.lower() != "https":
        raise SkillSourceError("source_insecure", "远程市场源必须使用 HTTPS")
    if parsed.username or parsed.password:
        raise SkillSourceError("source_credentials", "市场源 URL 不能包含账号或密码")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise SkillSourceError("source_invalid_url", "市场源 URL 缺少主机名")
    if host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
        raise SkillSourceError("source_private_host", "市场源不能指向本机或内部主机")
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise SkillSourceError("source_invalid_url", "市场源 URL 端口无效") from exc
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SkillSourceError("source_unresolvable", "市场源主机无法解析") from exc
    addresses = {str(info[4][0]).split("%", 1)[0] for info in infos}
    if not addresses:
        raise SkillSourceError("source_unresolvable", "市场源主机无法解析")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise SkillSourceError("source_invalid_host", "市场源主机地址无效") from exc
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise SkillSourceError("source_private_host", "市场源不能指向私有或本机地址")
    return raw


def _fetch_manifest(url: str) -> list[dict]:
    import httpx

    try:
        with httpx.Client(
            timeout=httpx.Timeout(5.0, connect=5.0), follow_redirects=False, trust_env=False,
        ) as client:
            with client.stream("GET", url, headers={"Accept": "application/json"}) as response:
                if 300 <= response.status_code < 400:
                    raise SkillSourceError("source_redirect", "市场源不允许重定向")
                if response.status_code != 200:
                    raise SkillSourceError(
                        "source_http_error", f"市场源返回 HTTP {response.status_code}",
                    )
                length = response.headers.get("content-length")
                if length and length.isdigit() and int(length) > _MAX_MANIFEST_BYTES:
                    raise SkillSourceError("source_too_large", "市场源清单超过大小上限")
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > _MAX_MANIFEST_BYTES:
                        raise SkillSourceError("source_too_large", "市场源清单超过大小上限")
                    chunks.append(chunk)
    except SkillSourceError:
        raise
    except Exception as exc:  # noqa: BLE001 - network failures become a stable code
        raise SkillSourceError("source_fetch_failed", "市场源下载失败") from exc
    try:
        payload = json.loads(b"".join(chunks).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SkillSourceError("source_bad_manifest", "市场源不是有效 JSON") from exc
    return _parse_manifest(payload)


def _parse_manifest(payload: object) -> list[dict]:
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise SkillSourceError("source_bad_manifest", "市场源清单必须含 version: 1")
    raw_skills = payload.get("skills")
    if not isinstance(raw_skills, list) or not 1 <= len(raw_skills) <= _MAX_SKILLS:
        raise SkillSourceError("source_bad_manifest", f"市场源需要 1-{_MAX_SKILLS} 个技能")
    skills: list[dict] = []
    seen: set[str] = set()
    for item in raw_skills:
        if not isinstance(item, dict):
            raise SkillSourceError("source_bad_manifest", "市场源技能条目必须是对象")
        slug = str(item.get("slug") or "").strip()
        name = str(item.get("name") or "").strip()
        description = str(item.get("description") or "").strip()
        content = item.get("content")
        if not _SLUG_RE.fullmatch(slug) or slug in seen:
            raise SkillSourceError("source_bad_manifest", f"市场源技能 slug 无效或重复：{slug}")
        if not name or len(name) > 120:
            raise SkillSourceError("source_bad_manifest", f"市场源技能名称无效：{slug}")
        if len(description) > 1000:
            raise SkillSourceError("source_bad_manifest", f"市场源技能描述过长：{slug}")
        if not isinstance(content, str) or not content.strip():
            raise SkillSourceError("source_bad_manifest", f"市场源技能缺少 content：{slug}")
        if len(content.encode("utf-8")) > _MAX_SKILL_CONTENT:
            raise SkillSourceError("source_bad_manifest", f"市场源技能正文过长：{slug}")
        seen.add(slug)
        skills.append({"slug": slug, "name": name, "description": description, "content": content})
    return skills


def _materialize(url: str, skills: list[dict]) -> Path:
    parent = _cache_parent()
    parent.mkdir(parents=True, exist_ok=True)
    token = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    cache_dir = parent / token
    staging = parent / f".{token}-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        for skill in skills:
            skill_dir = staging / skill["slug"]
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(skill["content"], encoding="utf-8")
        shutil.rmtree(cache_dir, ignore_errors=True)
        os.replace(staging, cache_dir)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return cache_dir


def _add_url_source(url: str, label: str) -> dict:
    normalized = _validate_remote_url(url)
    skills = _fetch_manifest(normalized)
    with _lock:
        items = _read_registry()
        key = normalized.lower()
        if any(item.get("kind") == "url" and item.get("url", "").lower() == key for item in items):
            raise SkillSourceError("source_exists", "该市场源已登记")
        if len(items) >= _MAX_SOURCES:
            raise SkillSourceError("source_limit", f"最多登记 {_MAX_SOURCES} 个市场源")
        cache_dir = _materialize(normalized, skills)
        entry = {
            "kind": "url", "url": normalized, "cache_dir": str(cache_dir),
            "label": label or urlsplit(normalized).hostname or normalized,
            "added_at": _now(),
        }
        try:
            _write_registry([*items, entry])
        except Exception:
            shutil.rmtree(cache_dir, ignore_errors=True)
            raise
        return entry


def add_skill_source(raw_path: str, *, label: str = "") -> dict:
    text = str(raw_path or "").strip()
    if not text:
        raise SkillSourceError("source_empty", "市场源不能为空")
    display = str(label or "").strip()[:80]
    if text.lower().startswith(("http://", "https://")):
        if text.lower().startswith("http://"):
            raise SkillSourceError("source_insecure", "远程市场源必须使用 HTTPS")
        return _public(_add_url_source(text, display))
    if len(text) > _MAX_PATH_CHARS:
        raise SkillSourceError("source_too_long", "市场源路径过长")
    path = _normalized_dir(text)
    for forbidden in _forbidden_roots():
        if path == forbidden or _inside(path, forbidden):
            raise SkillSourceError("source_overlaps_skills", "市场源不能位于已安装或本地技能目录内")
    with _lock:
        items = _read_registry()
        key = os.path.normcase(str(path))
        if any(item.get("kind") == "dir" and os.path.normcase(item["path"]) == key for item in items):
            raise SkillSourceError("source_exists", "该市场源已登记")
        if len(items) >= _MAX_SOURCES:
            raise SkillSourceError("source_limit", f"最多登记 {_MAX_SOURCES} 个市场源")
        entry = {
            "kind": "dir", "path": str(path),
            "label": display or path.name or str(path), "added_at": _now(),
        }
        _write_registry([*items, entry])
        return _public(entry)


def remove_skill_source(raw_path: str) -> bool:
    text = str(raw_path or "").strip()
    if not text:
        raise SkillSourceError("source_empty", "市场源不能为空")
    is_url = text.lower().startswith(("http://", "https://"))
    if is_url:
        key = text.lower()
    else:
        path = Path(text).expanduser()
        key = os.path.normcase(str(path.resolve())) if path.is_absolute() else os.path.normcase(text)
    with _lock:
        items = _read_registry()
        removed = []
        kept = []
        for item in items:
            matches = (
                item.get("url", "").lower() == key if is_url
                else item.get("kind") == "dir" and os.path.normcase(item.get("path", "")) == key
            )
            if not matches and os.path.normcase(item.get("label", "")) == key:
                matches = True
            (removed if matches else kept).append(item)
        if not removed:
            return False
        _write_registry(kept)
    for item in removed:
        cache_dir = str(item.get("cache_dir") or "").strip()
        if cache_dir:
            shutil.rmtree(cache_dir, ignore_errors=True)
    return True
