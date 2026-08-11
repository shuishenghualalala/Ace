"""Shared public HTTP boundary for in-process search and Wiki ingestion.

进程内 HTTP 边界：在发请求前做 SSRF 防护（拒私网/链路本地/保留地址、控制字符、
内嵌凭据、localhost），并对重定向按精确 host:port:protocol 授权。

联网方式与 codex network-proxy 对齐——安全意味着“经过策略判断”，不是“直连”。
判断通过后，默认把流量级联到用户已有的上游代理（HTTP/HTTPS/SOCKS5），由代理
负责到达目标；没有上游代理时才直连目标 IP。上游代理来源（优先级递减）：

1. 调用方显式传入 ``upstream_proxy``（来自 config 的 ``network.upstream_proxy``）；
2. 环境变量 ``HTTP_PROXY``/``HTTPS_PROXY``/``ALL_PROXY``（大小写都读，与 codex 一致）。

纯标准库实现，不引入第三方依赖，Linux/Windows/macOS 行为一致。
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import socket
import ssl
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit


@dataclass(frozen=True)
class PublicHttpTarget:
    url: str
    host: str
    port: int
    protocol: str

    @property
    def authority(self) -> tuple[str, int, str]:
        return self.host, self.port, self.protocol


@dataclass(frozen=True)
class PublicHttpResponse:
    url: str
    body: bytes
    content_type: str
    charset: str
    status: int


class PublicRedirectApprovalRequired(ValueError):
    def __init__(self, url: str) -> None:
        super().__init__("URL 重定向到尚未授权的网络目标")
        self.url = url


def parse_public_http_target(url: str) -> PublicHttpTarget:
    value = str(url or "").strip()
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ValueError("URL 包含控制字符")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("仅允许公开 http/https 地址")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL 不允许内嵌用户名或密码")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError("URL 端口无效") from exc
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("禁止访问 localhost")
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError("URL 主机名无效") from exc
    else:
        if not address.is_global or address.is_multicast or address.is_reserved:
            raise ValueError("禁止访问私网、链路本地或保留地址")
    return PublicHttpTarget(value, host, port, parsed.scheme)


def validate_public_http_target(url: str) -> PublicHttpTarget:
    target = parse_public_http_target(url)
    _resolve_public_addresses(target)
    return target


def _resolve_public_addresses(target: PublicHttpTarget) -> list[tuple]:
    try:
        addresses = socket.getaddrinfo(target.host, target.port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("URL 主机无法解析") from exc
    if not addresses:
        raise ValueError("URL 主机没有可用地址")
    public: list[tuple] = []
    for result in addresses:
        try:
            address = ipaddress.ip_address(result[4][0].split("%", 1)[0])
        except ValueError as exc:
            raise ValueError("URL 主机解析结果无效") from exc
        if address.is_global and not address.is_multicast and not address.is_reserved:
            public.append(result)
    if not public:
        raise ValueError("禁止访问私网、链路本地或保留地址")
    return public


def fetch_public_http(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    headers: Mapping[str, str] | None = None,
    allowed_targets: set[tuple[str, int, str]] | None = None,
    upstream_proxy: str | None = None,
    allow_loopback_proxy: bool | None = None,
) -> tuple[str, bytes, str, str]:
    """Fetch one public URL while pinning connections to validated DNS answers.

    ``upstream_proxy`` 非空时强制走该代理；为空时依次回退到 config 注入的默认
    代理、环境变量 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY。``allow_loopback_proxy``
    为 None 时使用 config 注入的默认值。
    """
    response = request_public_http(
        url,
        method="GET",
        timeout=timeout,
        max_bytes=max_bytes,
        headers=headers,
        allowed_targets=allowed_targets,
        upstream_proxy=upstream_proxy,
        allow_loopback_proxy=allow_loopback_proxy,
    )
    return response.url, response.body, response.content_type, response.charset


@dataclass(frozen=True)
class _UpstreamProxy:
    """解析后的上游代理：scheme 决定隧道建法，authority 决定连哪。"""

    scheme: str  # http | https | socks5 | socks5h
    host: str
    port: int
    username: str = ""
    password: str = ""

    @property
    def is_socks(self) -> bool:
        return self.scheme in {"socks5", "socks5h"}


def _read_env_proxy(*keys: str) -> str:
    for key in keys:
        value = os.getenv(key, "").strip()
        if value:
            return value
    return ""


# 由 config 加载后注入的进程级默认代理；避免 outbound 反向 import config 造成循环依赖。
_DEFAULT_UPSTREAM_PROXY = ""
_DEFAULT_ALLOW_LOOPBACK_PROXY = True


def set_network_defaults(
    upstream_proxy: str = "",
    allow_loopback_proxy: bool = True,
) -> None:
    """注入 config ``network`` 段作为进程级默认，供未显式传参的调用方使用。

    优先级：调用方显式传入 > config 注入的默认 > 环境变量。
    """
    global _DEFAULT_UPSTREAM_PROXY, _DEFAULT_ALLOW_LOOPBACK_PROXY
    _DEFAULT_UPSTREAM_PROXY = str(upstream_proxy or "").strip()
    _DEFAULT_ALLOW_LOOPBACK_PROXY = bool(allow_loopback_proxy)


def resolve_upstream_proxy(
    protocol: str,
    *,
    explicit: str | None = None,
) -> _UpstreamProxy | None:
    """按目标协议解析上游代理，与 codex 的 key 优先级一致。

    优先级：调用方显式传入 > config 注入的默认 > 环境变量
    HTTPS_PROXY/HTTP_PROXY/ALL_PROXY（大小写都读）。返回 None 表示不走代理、
    直连目标。代理 URL 中的用户名/密码会保留用于认证。
    """
    raw = (explicit or "").strip() or _DEFAULT_UPSTREAM_PROXY
    if not raw:
        if protocol == "https":
            raw = _read_env_proxy("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")
        else:
            raw = _read_env_proxy("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
        if not raw:
            return None
    parsed = urlsplit(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https", "socks5", "socks5h"}:
        raise ValueError(f"上游代理协议不支持: {scheme or '(缺失)'}")
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host:
        raise ValueError("上游代理缺少主机名")
    port = parsed.port
    if port is None:
        port = 1080 if scheme in {"socks5", "socks5h"} else (443 if scheme == "https" else 80)
    return _UpstreamProxy(
        scheme=scheme,
        host=host,
        port=port,
        username=parsed.username or "",
        password=parsed.password or "",
    )


def _connect_via_http_tunnel(
    proxy: _UpstreamProxy,
    target_host: str,
    target_port: int,
    timeout: float,
) -> socket.socket:
    """向 HTTP/HTTPS 代理发 CONNECT 建隧道，返回已就绪的裸 TCP socket。

    代理侧只做 TCP 转发，TLS 由调用方在返回的 socket 上对真实目标握手。
    """
    connection = socket.create_connection((proxy.host, proxy.port), timeout=timeout)
    try:
        if proxy.scheme == "https":
            connection = ssl.create_default_context().wrap_socket(
                connection,
                server_hostname=proxy.host,
            )
        # CONNECT 的 authority 始终带端口（RFC 7231），部分代理对省略端口的
        # CONNECT 会回非标准响应，导致后续 TLS 握手收到明文而失败。
        if ":" in target_host:
            authority = f"[{target_host}]:{target_port}"
        else:
            authority = f"{target_host}:{target_port}"
        request = (
            f"CONNECT {authority} HTTP/1.1\r\n"
            f"Host: {authority}\r\n"
            f"Proxy-Connection: keep-alive\r\n\r\n"
        )
        connection.sendall(request.encode("latin-1"))
        # 读取代理的 CONNECT 响应行 + 头部，直到空行。
        buffer = bytearray()
        while b"\r\n\r\n" not in buffer:
            chunk = connection.recv(4096)
            if not chunk:
                raise OSError("上游代理在 CONNECT 握手阶段关闭连接")
            buffer.extend(chunk)
            if len(buffer) > 8192:
                raise OSError("上游代理 CONNECT 响应过大")
        status_line, _, _ = buffer.partition(b"\r\n")
        parts = status_line.split(b" ", 2)
        if len(parts) < 2 or not parts[1].startswith(b"2"):
            status = parts[1].decode("latin-1", "replace") if len(parts) > 1 else "(缺失)"
            raise OSError(f"上游代理拒绝 CONNECT: {status}")
        # CONNECT 握手期间用 timeout 限时，隧道就绪后回到阻塞模式，
        # 否则残留的 socket timeout 会让后续 TLS 握手收到不完整数据。
        connection.settimeout(None)
        return connection
    except OSError:
        try:
            connection.close()
        except OSError:
            pass
        raise


def _connect_via_socks5(
    proxy: _UpstreamProxy,
    target_host: str,
    target_port: int,
    timeout: float,
) -> socket.socket:
    """SOCKS5 握手并代连目标，返回已就绪的裸 TCP socket。

    支持无认证与用户名/密码认证（RFC 1928 / 1929）。socks5h 把域名原样交给
    代理解析；socks5 由本地解析成 IP 再发给代理。调用方在返回 socket 上做 TLS。
    """
    connection = socket.create_connection((proxy.host, proxy.port), timeout=timeout)
    try:
        connection.settimeout(timeout)
        # 方法协商：NO-AUTH(0x00) 与 USERNAME/PASSWORD(0x02)。
        methods = b"\x00" if not proxy.username else b"\x00\x02"
        connection.sendall(b"\x05" + bytes([len(methods)]) + methods)
        reply = _socks_recv_exact(connection, 2)
        if reply[0] != 0x05:
            raise OSError("上游 SOCKS5 代理版本不匹配")
        method = reply[1]
        if method == 0xFF:
            raise OSError("上游 SOCKS5 代理拒绝所有认证方式")
        if method == 0x02:
            username = proxy.username.encode("utf-8")
            password = proxy.password.encode("utf-8")
            if len(username) > 255 or len(password) > 255:
                raise OSError("SOCKS5 用户名/密码过长")
            connection.sendall(
                b"\x01"
                + bytes([len(username)]) + username
                + bytes([len(password)]) + password
            )
            auth_reply = _socks_recv_exact(connection, 2)
            if auth_reply[1] != 0x00:
                raise OSError("上游 SOCKS5 代理认证失败")
        elif method != 0x00:
            raise OSError("上游 SOCKS5 代理要求不支持的认证方式")
        # CONNECT 请求：VER=5 CMD=1 RSV=0 + 地址 + 端口。
        address = _socks_address_bytes(proxy.scheme, target_host)
        connection.sendall(b"\x05\x01\x00" + address + struct.pack(">H", target_port))
        reply = _socks_recv_exact(connection, 4)
        if reply[0] != 0x05 or reply[1] != 0x00:
            raise OSError(f"上游 SOCKS5 代理连接目标失败: 状态 {reply[1]:#04x}")
        # 跳过绑定地址（IPv4 4+2 / 域名 1+N+2 / IPv6 16+2）。
        atype = reply[3]
        if atype == 0x01:
            _socks_recv_exact(connection, 4 + 2)
        elif atype == 0x03:
            length = _socks_recv_exact(connection, 1)[0]
            _socks_recv_exact(connection, length + 2)
        elif atype == 0x04:
            _socks_recv_exact(connection, 16 + 2)
        else:
            raise OSError(f"SOCKS5 绑定地址类型未知: {atype:#04x}")
        connection.settimeout(None)
        return connection
    except OSError:
        try:
            connection.close()
        except OSError:
            pass
        raise


def _socks_recv_exact(connection: socket.socket, n: int) -> bytes:
    data = bytearray()
    while len(data) < n:
        chunk = connection.recv(n - len(data))
        if not chunk:
            raise OSError("上游 SOCKS5 代理在握手阶段关闭连接")
        data.extend(chunk)
    return bytes(data)


def _socks_address_bytes(scheme: str, host: str) -> bytes:
    """构造 SOCKS5 ATYP+ADDR 段。socks5h 用域名；socks5 用解析后的 IP。"""
    if scheme == "socks5h":
        host_bytes = host.encode("idna") if _is_ascii(host) else host.encode("utf-8")
        if len(host_bytes) > 255:
            raise OSError("SOCKS5 目标主机名过长")
        return b"\x03" + bytes([len(host_bytes)]) + host_bytes
    # socks5：本地解析后发 IP，保留 socks5 的“本地 DNS”语义。
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        resolved = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        ip = ipaddress.ip_address(resolved[0][4][0].split("%", 1)[0])
    return (b"\x01" if ip.version == 4 else b"\x04") + ip.packed


def _is_ascii(text: str) -> bool:
    try:
        text.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def _first_loopback_or_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Return the parsed IP if host is a literal IP, else None (hostname)."""
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def _send_http_request(
    connection: socket.socket | ssl.SSLSocket,
    target: PublicHttpTarget,
    parsed,
    request_method: str,
    request_headers: dict[str, str],
    request_body: bytes,
) -> http.client.HTTPResponse:
    """在已就绪的连接（直连或代理隧道）上发送一次 HTTP 请求并读取响应头。"""
    request_path = parsed.path or "/"
    if parsed.query:
        request_path = f"{request_path}?{parsed.query}"
    default_port = 443 if target.protocol == "https" else 80
    host_header = f"[{target.host}]" if ":" in target.host else target.host
    if target.port != default_port:
        host_header = f"{host_header}:{target.port}"
    lines = [f"{request_method} {request_path} HTTP/1.1", f"Host: {host_header}"]
    if request_body:
        lines.append(f"Content-Length: {len(request_body)}")
    lines.extend(f"{name}: {value}" for name, value in request_headers.items())
    connection.sendall(
        ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + request_body
    )
    response = http.client.HTTPResponse(connection)
    response.begin()
    return response


def request_public_http(
    url: str,
    *,
    method: str,
    timeout: float,
    max_bytes: int,
    headers: Mapping[str, str] | None = None,
    json_body: object | None = None,
    allowed_targets: set[tuple[str, int, str]] | None = None,
    upstream_proxy: str | None = None,
    allow_loopback_proxy: bool | None = None,
) -> PublicHttpResponse:
    """Send one public GET/POST request with DNS-pinned redirect handling."""
    if max_bytes <= 0:
        raise ValueError("响应大小限制必须为正数")
    if allow_loopback_proxy is None:
        allow_loopback_proxy = _DEFAULT_ALLOW_LOOPBACK_PROXY
    request_method = str(method).strip().upper()
    if request_method not in {"GET", "POST"}:
        raise ValueError("仅允许 GET 或 POST")
    current = str(url)
    request_headers = {str(name): str(value) for name, value in (headers or {}).items()}
    if any(
        not name or "\r" in name or "\n" in name or "\r" in value or "\n" in value
        for name, value in request_headers.items()
    ):
        raise ValueError("HTTP 请求头无效")
    if any(name.lower() in {"host", "content-length"} for name in request_headers):
        raise ValueError("Host 和 Content-Length 由安全传输层生成")
    request_headers.setdefault("Accept-Encoding", "identity")
    request_headers.setdefault("Connection", "close")
    request_body = b""
    if request_method == "POST":
        request_body = json.dumps(
            json_body,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json; charset=utf-8")

    for _redirect in range(6):
        target = validate_public_http_target(current)
        if allowed_targets is not None and target.authority not in allowed_targets:
            raise PublicRedirectApprovalRequired(current)
        proxy = resolve_upstream_proxy(target.protocol, explicit=upstream_proxy)
        if proxy is not None and not allow_loopback_proxy:
            proxy_ip = _first_loopback_or_ip(proxy.host)
            if proxy_ip is not None and proxy_ip.is_loopback:
                raise ValueError("上游代理指向 loopback，但当前未允许 loopback 代理")
        parsed = urlsplit(current)
        response: http.client.HTTPResponse | None = None
        last_error: Exception | None = None
        if proxy is None:
            # 直连：保留 DNS pinning，按解析出的多个公开 IP 逐个尝试。
            candidates = _resolve_public_addresses(target)
            connection = None
            for result in candidates:
                address = result[4][0].split("%", 1)[0]
                try:
                    connection = socket.create_connection((address, target.port), timeout=timeout)
                    if target.protocol == "https":
                        connection = ssl.create_default_context().wrap_socket(
                            connection,
                            server_hostname=target.host,
                        )
                    response = _send_http_request(
                        connection, target, parsed, request_method,
                        request_headers, request_body,
                    )
                    break
                except (OSError, http.client.HTTPException) as exc:
                    last_error = exc
                    try:
                        if connection is not None:
                            connection.close()
                    except OSError:
                        pass
                    connection = None
        else:
            # 走上游代理：CONNECT 隧道（HTTP/HTTPS 代理）或 SOCKS5 代连。
            # DNS 解析交给代理，本地只校验目标是公开主机名（已在上面完成）。
            try:
                if proxy.is_socks:
                    connection = _connect_via_socks5(proxy, target.host, target.port, timeout)
                else:
                    connection = _connect_via_http_tunnel(proxy, target.host, target.port, timeout)
                if target.protocol == "https":
                    connection = ssl.create_default_context().wrap_socket(
                        connection,
                        server_hostname=target.host,
                    )
                response = _send_http_request(
                    connection, target, parsed, request_method,
                    request_headers, request_body,
                )
            except (OSError, http.client.HTTPException) as exc:
                last_error = exc
                try:
                    if connection is not None:
                        connection.close()
                except OSError:
                    pass
        if response is None:
            raise ValueError("URL 连接失败") from last_error
        try:
            if response.status in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                if not location:
                    raise ValueError("URL 重定向缺少 Location")
                current = urljoin(current, location)
                redirect_target = validate_public_http_target(current)
                if (
                    allowed_targets is not None
                    and redirect_target.authority not in allowed_targets
                ):
                    raise PublicRedirectApprovalRequired(current)
                if response.status == 303 or (
                    response.status in {301, 302} and request_method == "POST"
                ):
                    request_method = "GET"
                    request_body = b""
                    request_headers = {
                        name: value
                        for name, value in request_headers.items()
                        if name.lower() != "content-type"
                    }
                continue
            if not 200 <= response.status < 300:
                raise ValueError(f"URL 返回 HTTP {response.status}")
            raw = response.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise ValueError(f"URL 响应超过 {max_bytes} 字节限制")
            return PublicHttpResponse(
                url=current,
                body=raw,
                content_type=response.headers.get_content_type(),
                charset=response.headers.get_content_charset() or "utf-8",
                status=response.status,
            )
        finally:
            response.close()
    raise ValueError("URL 重定向次数过多")


@dataclass
class NetworkConfig:
    """进程内 HTTP 边界（web_search/web_extract/Wiki 抓取）的上游代理配置。

    与 codex network-proxy 对齐：安全意味着“经过策略判断”，判断通过后默认把
    流量级联到用户已有的上游代理。``upstream_proxy`` 为空时回退到环境变量
    ``HTTP_PROXY``/``HTTPS_PROXY``/``ALL_PROXY``，方便在能直连外网的服务器上
    零配置运行，在需要代理的开发机上只设环境变量即可。
    """

    upstream_proxy: str = ""
    allow_loopback_proxy: bool = True

    @classmethod
    def from_raw(cls, raw: object) -> "NetworkConfig":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            upstream_proxy=str(raw.get("upstream_proxy") or "").strip(),
            allow_loopback_proxy=bool(raw.get("allow_loopback_proxy", True)),
        )
