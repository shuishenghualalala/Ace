"""Gateway authentication helpers for desktop-facing HTTP and WebSocket traffic."""

from __future__ import annotations

import ipaddress
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import Request as FastAPIRequest
from fastapi import WebSocket

from crew.security.settings import strict_security_enabled

LOCAL_OWNER_ACCOUNT_ID = "local"
REMOTE_AUTH_COOKIE = "crew_auth_session"
_REMOTE_SESSION_VERSION = 1
_REMOTE_KEY_DIRECTORY = ".auth"
_REMOTE_KEY_FILENAME = "session.key"
_IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}\Z")


@dataclass(frozen=True)
class AccountContext:
    """Gateway owner identity established by a trusted transport."""

    owner_account_id: str
    is_local: bool = False
    provider_id: str = ""
    user_id: str = ""


class AuthenticationError(RuntimeError):
    """Raised when a request does not come from a trusted gateway client."""


def _trusted_local_account(config: Any | None, client_host: str | None) -> AccountContext | None:
    """Map trusted loopback traffic to Crew's single local owner."""
    if not is_loopback_client(client_host):
        return None
    owner = LOCAL_OWNER_ACCOUNT_ID
    if getattr(config, "gateway_dev_mode", False):
        owner = str(getattr(config, "gateway_dev_account", "dev:dev") or "dev:dev").strip()
    return AccountContext(owner_account_id=owner or LOCAL_OWNER_ACCOUNT_ID, is_local=True)


def _effective_auth_mode(config: Any | None) -> str:
    mode = str(getattr(config, "auth_mode", "local") or "local").strip().lower()
    if mode == "remote":
        return "remote"
    if getattr(config, "gateway_dev_mode", False):
        return "dev"
    if mode == "email":
        return "email"
    return "local"


def _session_key_path() -> Path:
    from crew.state.home import get_crew_home

    return get_crew_home() / _REMOTE_KEY_DIRECTORY / _REMOTE_KEY_FILENAME


def _metadata_is_secure(info: os.stat_result, *, directory: bool = False) -> bool:
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(info.st_mode):
        return False
    if os.name == "nt":
        return True
    getuid = getattr(os, "getuid", None)
    if callable(getuid) and info.st_uid != getuid():
        return False
    return stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600)


def _read_session_key(path: Path) -> bytes:
    try:
        before = os.lstat(path)
        if stat.S_ISLNK(before.st_mode) or not _metadata_is_secure(before):
            raise AuthenticationError("本地认证会话密钥权限无效")
        flags = os.O_RDONLY
        if os.name == "nt" and hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if os.name != "nt" and hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
    except AuthenticationError:
        raise
    except (OSError, ValueError) as exc:
        raise AuthenticationError("无法读取本地认证会话密钥") from exc
    try:
        opened = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise AuthenticationError("本地认证会话密钥已被替换")
        if not _metadata_is_secure(opened) or opened.st_size != 32:
            raise AuthenticationError("本地认证会话密钥无效")
        raw = os.read(fd, 33)
        if len(raw) != 32:
            raise AuthenticationError("本地认证会话密钥无效")
        return raw
    finally:
        os.close(fd)


def _load_or_create_session_key() -> bytes:
    path = _session_key_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    try:
        parent = os.lstat(path.parent)
        if stat.S_ISLNK(parent.st_mode) or not _metadata_is_secure(parent, directory=True):
            raise AuthenticationError("本地认证目录权限无效")
    except AuthenticationError:
        raise
    except (OSError, ValueError) as exc:
        raise AuthenticationError("无法访问本地认证目录") from exc
    if path.exists() or path.is_symlink():
        return _read_session_key(path)
    key = secrets.token_bytes(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if os.name == "nt" and hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if os.name != "nt" and hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        return _read_session_key(path)
    except (OSError, ValueError) as exc:
        raise AuthenticationError("无法创建本地认证会话密钥") from exc
    try:
        written = 0
        while written < len(key):
            count = os.write(fd, key[written:])
            if count <= 0:
                raise AuthenticationError("无法写入本地认证会话密钥")
            written += count
        os.fsync(fd)
    finally:
        os.close(fd)
    return _read_session_key(path)


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(raw: str) -> bytes:
    padded = raw + "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _validated_identity(provider_id: str, user_id: str) -> tuple[str, str]:
    provider = str(provider_id or "").strip()
    user = str(user_id or "").strip()
    if not _IDENTITY_RE.fullmatch(provider) or not _IDENTITY_RE.fullmatch(user):
        raise AuthenticationError("认证服务返回的用户身份格式无效")
    return provider, user


def create_remote_session_token(
    provider_id: str,
    user_id: str,
    *,
    ttl_seconds: int,
) -> str:
    """Create a signed, local-only owner session token without embedding PII."""

    provider, user = _validated_identity(provider_id, user_id)
    payload = json.dumps(
        {
            "v": _REMOTE_SESSION_VERSION,
            "providerId": provider,
            "userId": user,
            "exp": int(time.time()) + max(300, int(ttl_seconds)),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded = _b64encode(payload)
    signature = hmac.new(
        _load_or_create_session_key(),
        encoded.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return f"{encoded}.{signature}"


def account_from_remote_session_token(token: str, config: Any | None) -> AccountContext:
    try:
        encoded, signature = str(token or "").split(".", 1)
        expected = hmac.new(
            _load_or_create_session_key(),
            encoded.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise AuthenticationError("登录会话无效")
        payload = json.loads(_b64decode(encoded))
        if not isinstance(payload, dict) or payload.get("v") != _REMOTE_SESSION_VERSION:
            raise AuthenticationError("登录会话无效")
        if int(payload.get("exp") or 0) <= int(time.time()):
            raise AuthenticationError("登录会话已过期")
        provider, user = _validated_identity(
            str(payload.get("providerId") or ""),
            str(payload.get("userId") or ""),
        )
        configured_provider = (
            "email"
            if _effective_auth_mode(config) == "email"
            else str(getattr(config, "auth_provider_id", "custom") or "custom").strip()
        )
        if provider != configured_provider:
            raise AuthenticationError("登录会话不属于当前认证服务")
        return AccountContext(
            owner_account_id=f"{provider}:{user}",
            provider_id=provider,
            user_id=user,
        )
    except AuthenticationError:
        raise
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise AuthenticationError("登录会话无效") from exc


def _remote_account_from_cookie(cookies: Any, config: Any | None) -> AccountContext:
    token = str(cookies.get(REMOTE_AUTH_COOKIE) or "").strip()
    if not token:
        raise AuthenticationError("请先登录")
    return account_from_remote_session_token(token, config)


def is_loopback_client(host: str | None) -> bool:
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # Starlette TestClient uses this synthetic host for in-process tests.
        return host in {"localhost", "testclient"}


def is_loopback_host(host: str | None) -> bool:
    """Return whether a configured bind host is loopback-only."""
    if not host:
        return False
    normalized = str(host).strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def account_from_request(request: FastAPIRequest) -> AccountContext:
    """Read the authenticated account context attached by middleware."""

    ctx = getattr(request.state, "account", None)
    if not isinstance(ctx, AccountContext):
        raise AuthenticationError("未登录")
    return ctx


def require_admin(account: AccountContext, config: Any | None = None) -> None:
    """Require the authenticated account to be configured as a gateway admin."""

    if account.is_local:
        return
    admins = set(getattr(config, "gateway_admin_accounts", []) or [])
    if not admins or account.owner_account_id not in admins:
        raise AuthenticationError("需要管理员权限")


async def authenticate_http_request(request: FastAPIRequest, config: Any | None = None) -> AccountContext:
    """Authenticate local/dev traffic or a signed remote-login session."""

    client_host = request.client.host if request.client else None
    if not is_loopback_client(client_host):
        raise AuthenticationError("仅允许本机访问")
    if _effective_auth_mode(config) in {"remote", "email"}:
        return _remote_account_from_cookie(request.cookies, config)
    local_account = _trusted_local_account(config, client_host)
    if local_account is not None:
        return local_account
    raise AuthenticationError("仅允许本机访问")


async def authenticate_websocket(socket: WebSocket, config: Any | None = None) -> AccountContext:
    """Authenticate a local WebSocket with the same policy as HTTP."""

    origin = str(socket.headers.get("origin") or "").strip()
    if strict_security_enabled() and origin and origin not in {"null", "file://"}:
        try:
            parsed = urlsplit(origin)
        except ValueError as exc:
            raise AuthenticationError("WebSocket Origin 非法") from exc
        if parsed.scheme not in {"http", "https"} or not is_loopback_host(parsed.hostname):
            raise AuthenticationError("WebSocket Origin 不受信任")

    client_host = socket.client.host if socket.client else None
    if not is_loopback_client(client_host):
        raise AuthenticationError("仅允许本机访问")
    if _effective_auth_mode(config) in {"remote", "email"}:
        return _remote_account_from_cookie(socket.cookies, config)
    local_account = _trusted_local_account(config, client_host)
    if local_account is not None:
        return local_account
    raise AuthenticationError("仅允许本机访问")


__all__ = [
    "AccountContext",
    "AuthenticationError",
    "LOCAL_OWNER_ACCOUNT_ID",
    "REMOTE_AUTH_COOKIE",
    "account_from_remote_session_token",
    "account_from_request",
    "authenticate_http_request",
    "authenticate_websocket",
    "create_remote_session_token",
    "is_loopback_client",
    "require_admin",
]
