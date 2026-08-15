"""Gateway authentication helpers for desktop-facing HTTP and WebSocket traffic."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import Request as FastAPIRequest
from fastapi import WebSocket

from crew.gateway.instance_auth import (
    GATEWAY_INSTANCE_AUTH_HEADER,
    verify_desktop_security_proof,
)
from crew.gateway.windows_acl import (
    fd_is_secure as _windows_fd_is_secure,
)
from crew.gateway.windows_acl import (
    path_is_secure as _windows_path_is_secure,
)
from crew.security.secret_store import (
    PlatformSecretStore,
    SecretIdentifier,
    SecretNotFound,
    SecretStoreError,
)
from crew.security.settings import strict_security_enabled

LOCAL_OWNER_ACCOUNT_ID = "local"
REMOTE_AUTH_COOKIE = "crew_auth_session"
DESKTOP_REQUEST_ORIGIN = "ace-desktop://main"
_REMOTE_SESSION_VERSION = 2
_REMOTE_SESSION_AUDIENCE = "ace-gateway-remote-session"
_REMOTE_SESSION_PURPOSE = "owner-authentication"
_REMOTE_KEY_DIRECTORY = ".auth"
_REMOTE_KEY_FILENAME = "session.key"
_IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}\Z")
_MAX_AUTHENTICATED_REQUEST_BODY_BYTES = 64 * 1024 * 1024
_IS_WINDOWS = os.name == "nt"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_remote_session_lock = threading.RLock()
_remote_sessions: dict[str, tuple[str, str, int]] = {}
_remote_owner_sessions: dict[tuple[str, str], str] = {}


@dataclass(frozen=True)
class AccountContext:
    """Gateway owner identity established by a trusted transport."""

    owner_account_id: str
    is_local: bool = False
    provider_id: str = ""
    user_id: str = ""
    process_authorization_generation: str = ""
    process_authorization_expires_at: float = 0.0


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


def _session_secret_identifier() -> SecretIdentifier:
    """Bind the signing key to this exact Gateway state root."""

    from crew.state.home import get_crew_home

    home = str(get_crew_home().resolve(strict=False))
    scope = hashlib.sha256(home.encode("utf-8")).hexdigest()
    return SecretIdentifier(
        namespace="gateway-auth",
        scope=f"gateway-home-{scope}",
        name="remote-session-signing-key-v2",
    )


def _session_instance_id() -> str:
    return _session_secret_identifier().scope.removeprefix("gateway-home-")


def _metadata_is_secure(
    info: os.stat_result,
    *,
    directory: bool = False,
    path: Path | None = None,
    fd: int | None = None,
) -> bool:
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(info.st_mode):
        return False
    if int(getattr(info, "st_file_attributes", 0)) & _FILE_ATTRIBUTE_REPARSE_POINT:
        return False
    if _IS_WINDOWS:
        if (path is None) == (fd is None):
            return False
        try:
            if path is not None:
                return _windows_path_is_secure(path, directory=directory)
            assert fd is not None
            return _windows_fd_is_secure(fd, directory=directory)
        except (OSError, ValueError):
            return False
    getuid = getattr(os, "getuid", None)
    if callable(getuid) and info.st_uid != getuid():
        return False
    return stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600)


def _read_session_key(path: Path) -> bytes:
    try:
        before = os.lstat(path)
        if stat.S_ISLNK(before.st_mode) or not _metadata_is_secure(before, path=path):
            raise AuthenticationError("本地认证会话密钥权限无效")
        flags = os.O_RDONLY
        if _IS_WINDOWS and hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if not _IS_WINDOWS and hasattr(os, "O_NOFOLLOW"):
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
        if not _metadata_is_secure(opened, fd=fd) or opened.st_size != 32:
            raise AuthenticationError("本地认证会话密钥无效")
        raw = os.read(fd, 33)
        if len(raw) != 32:
            raise AuthenticationError("本地认证会话密钥无效")
        return raw
    finally:
        os.close(fd)


def _read_legacy_session_key(path: Path) -> bytes:
    try:
        parent = os.lstat(path.parent)
        if stat.S_ISLNK(parent.st_mode) or not _metadata_is_secure(
            parent,
            directory=True,
            path=path.parent,
        ):
            raise AuthenticationError("本地认证目录权限无效")
    except AuthenticationError:
        raise
    except (OSError, ValueError) as exc:
        raise AuthenticationError("无法访问本地认证目录") from exc
    return _read_session_key(path)


def _remove_legacy_session_key(path: Path) -> None:
    if not (path.exists() or path.is_symlink()):
        return
    try:
        path.unlink()
        with contextlib.suppress(OSError):
            path.parent.rmdir()
    except OSError as exc:
        raise AuthenticationError("无法删除旧版认证会话密钥") from exc


def _load_or_create_session_key() -> bytes:
    """Load a keyring-backed signing key, migrating the legacy protected file."""

    identifier = _session_secret_identifier()
    try:
        store = PlatformSecretStore.platform()
    except SecretStoreError as exc:
        raise AuthenticationError("本地认证安全存储不可用") from exc

    try:
        encoded = store.get(identifier)
    except SecretNotFound:
        encoded = ""
    except SecretStoreError as exc:
        raise AuthenticationError("无法读取本地认证安全存储") from exc

    if encoded:
        try:
            key = _b64decode(encoded)
        except (ValueError, TypeError, UnicodeError) as exc:
            raise AuthenticationError("本地认证安全存储记录无效") from exc
        if len(key) != 32:
            raise AuthenticationError("本地认证安全存储记录无效")
        _remove_legacy_session_key(_session_key_path())
        return key

    legacy_path = _session_key_path()
    key = (
        _read_legacy_session_key(legacy_path)
        if legacy_path.exists() or legacy_path.is_symlink()
        else secrets.token_bytes(32)
    )
    mutation = None
    try:
        mutation = store.replace(identifier, _b64encode(key))
        persisted = _b64decode(store.get(identifier))
    except (SecretStoreError, ValueError, TypeError, UnicodeError) as exc:
        if mutation is not None:
            with contextlib.suppress(SecretStoreError):
                store.rollback(mutation)
        raise AuthenticationError("无法写入本地认证安全存储") from exc
    if len(persisted) != 32 or not hmac.compare_digest(persisted, key):
        with contextlib.suppress(SecretStoreError):
            store.rollback(mutation)
        raise AuthenticationError("本地认证安全存储校验失败")

    try:
        _remove_legacy_session_key(legacy_path)
    except AuthenticationError:
        with contextlib.suppress(SecretStoreError):
            store.rollback(mutation)
        raise
    return persisted


def rotate_remote_session_signing_key() -> None:
    """Invalidate every existing remote session by replacing the signing key."""

    new_key = secrets.token_bytes(32)
    mutation = None
    try:
        store = PlatformSecretStore.platform()
        mutation = store.replace(
            _session_secret_identifier(),
            _b64encode(new_key),
        )
        persisted = _b64decode(store.get(_session_secret_identifier()))
        if len(persisted) != 32 or not hmac.compare_digest(persisted, new_key):
            raise AuthenticationError("本地认证会话密钥轮换校验失败")
        _remove_legacy_session_key(_session_key_path())
    except (AuthenticationError, SecretStoreError, ValueError, TypeError, UnicodeError) as exc:
        if mutation is not None:
            with contextlib.suppress(SecretStoreError):
                store.rollback(mutation)
        raise AuthenticationError("无法轮换本地认证会话密钥") from exc
    with _remote_session_lock:
        _remote_sessions.clear()
        _remote_owner_sessions.clear()


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
    """Create and register a rotated, signed owner session token."""

    provider, user = _validated_identity(provider_id, user_id)
    session_id = secrets.token_urlsafe(24)
    expires_at = int(time.time()) + max(300, int(ttl_seconds))
    payload = json.dumps(
        {
            "v": _REMOTE_SESSION_VERSION,
            "aud": _REMOTE_SESSION_AUDIENCE,
            "purpose": _REMOTE_SESSION_PURPOSE,
            "instance": _session_instance_id(),
            "sid": session_id,
            "providerId": provider,
            "userId": user,
            "exp": expires_at,
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
    with _remote_session_lock:
        now = int(time.time())
        for stale_sid, record in list(_remote_sessions.items()):
            if record[2] <= now:
                _remote_sessions.pop(stale_sid, None)
                _remote_owner_sessions.pop((record[0], record[1]), None)
        owner_key = (provider, user)
        previous = _remote_owner_sessions.get(owner_key)
        if previous:
            _remote_sessions.pop(previous, None)
        _remote_sessions[session_id] = (provider, user, expires_at)
        _remote_owner_sessions[owner_key] = session_id
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
        if (
            not isinstance(payload, dict)
            or set(payload)
            != {
                "aud",
                "exp",
                "instance",
                "providerId",
                "purpose",
                "sid",
                "userId",
                "v",
            }
            or payload.get("v") != _REMOTE_SESSION_VERSION
            or payload.get("aud") != _REMOTE_SESSION_AUDIENCE
            or payload.get("purpose") != _REMOTE_SESSION_PURPOSE
            or not hmac.compare_digest(
                str(payload.get("instance") or ""),
                _session_instance_id(),
            )
            or not isinstance(payload.get("sid"), str)
            or len(payload["sid"]) < 24
        ):
            raise AuthenticationError("登录会话无效")
        if int(payload.get("exp") or 0) <= int(time.time()):
            raise AuthenticationError("登录会话无效")
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
            raise AuthenticationError("登录会话无效")
        session_id = str(payload["sid"])
        with _remote_session_lock:
            registered = _remote_sessions.get(session_id)
            if registered != (provider, user, int(payload["exp"])):
                raise AuthenticationError("登录会话无效")
        return AccountContext(
            owner_account_id=f"{provider}:{user}",
            provider_id=provider,
            user_id=user,
            process_authorization_generation=hashlib.sha256(
                (
                    "ace-remote-process-authority-v1\n"
                    f"{_session_instance_id()}\n{session_id}\n{provider}:{user}"
                ).encode()
            ).hexdigest(),
            process_authorization_expires_at=float(payload["exp"]),
        )
    except AuthenticationError:
        raise
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise AuthenticationError("登录会话无效") from exc


def process_authority_for_account(
    account: AccountContext,
    *,
    lease_claimed_at: float,
    ttl_seconds: int,
    now: float | None = None,
) -> tuple[str, float]:
    """Derive a secret-free process generation from current authenticated facts."""
    current = time.time() if now is None else float(now)
    if (
        account.process_authorization_generation
        and account.process_authorization_expires_at > current
    ):
        return (
            account.process_authorization_generation,
            account.process_authorization_expires_at,
        )
    if not account.is_local:
        raise AuthenticationError("登录会话缺少进程授权代次")
    owner = str(account.owner_account_id or "").strip()
    claimed_at = float(lease_claimed_at)
    if not owner or not math.isfinite(claimed_at) or claimed_at <= 0:
        raise AuthenticationError("本地登录租约无效")
    generation = hashlib.sha256(
        (
            "ace-local-process-authority-v1\n"
            f"{_session_instance_id()}\n{owner}\n{claimed_at!r}"
        ).encode()
    ).hexdigest()
    return generation, current + max(300, int(ttl_seconds))


def revoke_remote_session_token(token: str, config: Any | None) -> bool:
    """Revoke one live cookie server-side so replay remains invalid."""

    account = account_from_remote_session_token(token, config)
    try:
        encoded, _signature = str(token).split(".", 1)
        payload = json.loads(_b64decode(encoded))
        session_id = str(payload["sid"])
    except (KeyError, ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise AuthenticationError("登录会话无效") from exc
    with _remote_session_lock:
        removed = _remote_sessions.pop(session_id, None)
        owner_key = (account.provider_id, account.user_id)
        if _remote_owner_sessions.get(owner_key) == session_id:
            _remote_owner_sessions.pop(owner_key, None)
        return removed is not None


def revoke_remote_owner_sessions(owner_account_id: str) -> int:
    """Revoke every login session for one authenticated owner."""

    owner = str(owner_account_id or "").strip()
    if ":" not in owner:
        return 0
    provider, user = owner.split(":", 1)
    with _remote_session_lock:
        session_id = _remote_owner_sessions.pop((provider, user), None)
        if not session_id:
            return 0
        return int(_remote_sessions.pop(session_id, None) is not None)


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


def require_trusted_request_origin(origin: str, config: Any | None = None) -> None:
    """Require the packaged Desktop or the exact loopback Gateway web origin."""

    supplied = str(origin or "").strip()
    if supplied == DESKTOP_REQUEST_ORIGIN:
        return
    try:
        parsed = urlsplit(supplied)
    except ValueError as exc:
        raise AuthenticationError("请求 Origin 非法") from exc
    configured_port = int(getattr(config, "gateway_port", 8000) or 8000)
    try:
        supplied_port = parsed.port
    except ValueError as exc:
        raise AuthenticationError("请求 Origin 非法") from exc
    if (
        parsed.scheme != "http"
        or not is_loopback_host(parsed.hostname)
        or supplied_port != configured_port
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise AuthenticationError("请求 Origin 不受信任")


async def _read_bounded_request_body(request: FastAPIRequest) -> bytes:
    """Read and cache the exact wire body without allowing unbounded buffering."""

    raw_length = str(request.headers.get("content-length") or "").strip()
    if raw_length:
        try:
            declared_length = int(raw_length, 10)
        except ValueError as exc:
            raise AuthenticationError("请求 Content-Length 非法") from exc
        if declared_length < 0 or declared_length > _MAX_AUTHENTICATED_REQUEST_BODY_BYTES:
            raise AuthenticationError("请求体超过安全上限")

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _MAX_AUTHENTICATED_REQUEST_BODY_BYTES:
            raise AuthenticationError("请求体超过安全上限")
        chunks.append(chunk)
    body = b"".join(chunks)
    # Starlette replays this cached body to downstream multipart/JSON parsers.
    setattr(request, "_body", body)
    return body


async def authenticate_gateway_instance_request(
    request: FastAPIRequest,
) -> None:
    """Authenticate one loopback request as the paired Desktop instance."""

    client_host = request.client.host if request.client else None
    if not is_loopback_client(client_host):
        raise AuthenticationError("仅允许本机访问")
    body = await _read_bounded_request_body(request)
    proof = str(request.headers.get(GATEWAY_INSTANCE_AUTH_HEADER) or "")
    if not verify_desktop_security_proof(
        proof,
        method=request.method,
        path=request.url.path,
        body=body,
    ):
        raise AuthenticationError("Desktop/Gateway 实例证明无效")


async def authenticate_http_request(request: FastAPIRequest, config: Any | None = None) -> AccountContext:
    """Authenticate the paired Desktop and its local/remote owner."""

    await authenticate_gateway_instance_request(request)
    client_host = request.client.host if request.client else None
    if _effective_auth_mode(config) in {"remote", "email"}:
        if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            require_trusted_request_origin(
                str(request.headers.get("origin") or ""),
                config,
            )
        return _remote_account_from_cookie(request.cookies, config)
    local_account = _trusted_local_account(config, client_host)
    if local_account is not None:
        return local_account
    raise AuthenticationError("仅允许本机访问")


async def authenticate_websocket(socket: WebSocket, config: Any | None = None) -> AccountContext:
    """Authenticate a paired Desktop WebSocket with the same owner policy."""

    existing = getattr(socket.state, "account", None)
    if isinstance(existing, AccountContext):
        return existing

    origin = str(socket.headers.get("origin") or "").strip()
    if _effective_auth_mode(config) in {"remote", "email"}:
        require_trusted_request_origin(origin, config)
    elif strict_security_enabled() and origin and origin not in {"null", "file://"}:
        try:
            parsed = urlsplit(origin)
        except ValueError as exc:
            raise AuthenticationError("WebSocket Origin 非法") from exc
        if parsed.scheme not in {"http", "https"} or not is_loopback_host(parsed.hostname):
            raise AuthenticationError("WebSocket Origin 不受信任")

    client_host = socket.client.host if socket.client else None
    if not is_loopback_client(client_host):
        raise AuthenticationError("仅允许本机访问")
    proof = str(socket.headers.get(GATEWAY_INSTANCE_AUTH_HEADER) or "")
    if not verify_desktop_security_proof(
        proof,
        method="GET",
        path=socket.url.path,
        body=b"",
    ):
        raise AuthenticationError("Desktop/Gateway 实例证明无效")
    if _effective_auth_mode(config) in {"remote", "email"}:
        account = _remote_account_from_cookie(socket.cookies, config)
        socket.state.account = account
        return account
    local_account = _trusted_local_account(config, client_host)
    if local_account is not None:
        socket.state.account = local_account
        return local_account
    raise AuthenticationError("仅允许本机访问")


__all__ = [
    "DESKTOP_REQUEST_ORIGIN",
    "LOCAL_OWNER_ACCOUNT_ID",
    "REMOTE_AUTH_COOKIE",
    "AccountContext",
    "AuthenticationError",
    "account_from_remote_session_token",
    "account_from_request",
    "authenticate_gateway_instance_request",
    "authenticate_http_request",
    "authenticate_websocket",
    "create_remote_session_token",
    "is_loopback_client",
    "process_authority_for_account",
    "require_admin",
    "require_trusted_request_origin",
    "revoke_remote_owner_sessions",
    "revoke_remote_session_token",
    "rotate_remote_session_signing_key",
]
