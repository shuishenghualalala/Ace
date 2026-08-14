"""Desktop 与本机 Gateway 之间的实例身份挑战应答。

该密钥独立于登录 JWT。Desktop 先在基础 ``CREW_HOME`` 中创建密钥，Gateway
仅在收到一次性 challenge 时读取它并返回 HMAC；因此占用 loopback 端口、但不持有
密钥的其他服务无法被 Desktop 误认为 Crew Gateway。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import stat
import threading
import time
from pathlib import Path

from crew.state.home import get_crew_home

GATEWAY_INSTANCE_DIRECTORY = ".gateway-instance"
GATEWAY_INSTANCE_KEY_FILENAME = "gateway-instance.key"
GATEWAY_INSTANCE_CHALLENGE_HEADER = "X-Crew-Gateway-Challenge"
GATEWAY_INSTANCE_PROOF_FIELD = "instance_proof"

_CHALLENGE_RE = re.compile(r"[0-9a-f]{64}\Z")
_KEY_RE = re.compile(rb"[0-9a-f]{64}\Z")
_PROOF_CONTEXT = b"crew-gateway-instance-v1\x00"
_ACCESS_TOKEN_CONTEXT = b"crew-gateway-browser-access-v1\x00"
_SECURITY_CONTEXT = b"crew-security-desktop-v1\x00"


def _key_path() -> Path:
    return get_crew_home() / GATEWAY_INSTANCE_DIRECTORY / GATEWAY_INSTANCE_KEY_FILENAME


def _metadata_is_secure(info: os.stat_result, *, directory: bool = False) -> bool:
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(info.st_mode):
        return False
    if os.name == "nt":
        return True
    getuid = getattr(os, "getuid", None)
    if callable(getuid) and info.st_uid != getuid():
        return False
    expected_mode = 0o700 if directory else 0o600
    return stat.S_IMODE(info.st_mode) == expected_mode


def _load_instance_key() -> bytes | None:
    """安全、定长地读取 Desktop 创建的实例密钥。

    POSIX 使用 ``O_NOFOLLOW``，并比较 lstat/fstat 的 inode，避免把符号链接或
    检查后被替换的文件当成密钥。任何异常都 fail closed。
    """

    path = _key_path()
    try:
        parent = os.lstat(path.parent)
        if stat.S_ISLNK(parent.st_mode) or not _metadata_is_secure(parent, directory=True):
            return None
        before = os.lstat(path)
        if stat.S_ISLNK(before.st_mode) or not _metadata_is_secure(before):
            return None

        flags = os.O_RDONLY
        if os.name != "nt" and hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
    except (OSError, ValueError):
        return None

    try:
        opened = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            return None
        if not _metadata_is_secure(opened) or opened.st_size != 64:
            return None
        chunks: list[bytes] = []
        remaining = 65
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if not _KEY_RE.fullmatch(raw):
            return None
        return bytes.fromhex(raw.decode("ascii"))
    except (OSError, UnicodeError, ValueError):
        return None
    finally:
        os.close(fd)


def create_gateway_instance_proof(challenge: str) -> str | None:
    """返回 challenge 的实例证明；格式或本机密钥无效时返回 ``None``。"""

    if not _CHALLENGE_RE.fullmatch(challenge):
        return None
    key = _load_instance_key()
    if key is None:
        return None
    message = _PROOF_CONTEXT + challenge.encode("ascii")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def is_valid_gateway_instance_challenge(challenge: str) -> bool:
    return _CHALLENGE_RE.fullmatch(challenge) is not None


def verify_gateway_instance_access_token(token: str) -> bool:
    """Validate the Desktop-only token used by privileged Browser sockets."""

    if not _CHALLENGE_RE.fullmatch(str(token or "")):
        return False
    key = _load_instance_key()
    if key is None:
        return False
    expected = hmac.new(key, _ACCESS_TOKEN_CONTEXT, hashlib.sha256).hexdigest()
    return hmac.compare_digest(str(token), expected)


_PROOF_NONCE_RE = re.compile(r"[0-9a-f]{32}\Z")
_PROOF_TTL_SECONDS = 30.0
_PROOF_NONCE_TABLE_LIMIT = 4096
_used_proof_nonces: dict[str, float] = {}
_used_proof_lock = threading.Lock()


def _consume_proof_nonce(nonce: str, timestamp: int) -> bool:
    now = time.time()
    expires = timestamp + _PROOF_TTL_SECONDS + 5
    with _used_proof_lock:
        if len(_used_proof_nonces) > _PROOF_NONCE_TABLE_LIMIT:
            for stale in [key for key, exp in _used_proof_nonces.items() if exp < now]:
                del _used_proof_nonces[stale]
        if nonce in _used_proof_nonces:
            return False
        _used_proof_nonces[nonce] = expires
        return True


def verify_desktop_security_proof(
    proof: str,
    *,
    method: str,
    path: str,
    body: bytes,
    now: float | None = None,
) -> bool:
    """Verify a short-lived, one-time proof for privileged security endpoints."""
    try:
        timestamp_raw, nonce, supplied = str(proof).split(":", 2)
        timestamp = int(timestamp_raw)
    except (TypeError, ValueError):
        return False
    current = int(time.time() if now is None else now)
    if abs(current - timestamp) > 30:
        return False
    if not re.fullmatch(r"[0-9a-f]{64}", supplied) or not _PROOF_NONCE_RE.fullmatch(nonce):
        return False
    key = _load_instance_key()
    if key is None:
        return False
    body_hash = hashlib.sha256(body).hexdigest()
    message = (
        _SECURITY_CONTEXT
        + f"{timestamp}\n{nonce}\n{method.upper()}\n{path}\n{body_hash}".encode("utf-8")
    )
    expected = hmac.new(key, message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, supplied) and _consume_proof_nonce(nonce, timestamp)


__all__ = [
    "GATEWAY_INSTANCE_CHALLENGE_HEADER",
    "GATEWAY_INSTANCE_DIRECTORY",
    "GATEWAY_INSTANCE_KEY_FILENAME",
    "GATEWAY_INSTANCE_PROOF_FIELD",
    "create_gateway_instance_proof",
    "is_valid_gateway_instance_challenge",
    "verify_gateway_instance_access_token",
    "verify_desktop_security_proof",
]
