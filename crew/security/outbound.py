"""Canonical outbound destination policy and DNS-pinned connection plans.

Callers must keep the returned :class:`ConnectionPlan` through the actual
socket connect.  Re-parsing the URL or resolving the hostname again would
reintroduce the DNS rebinding gap this module closes.
"""

from __future__ import annotations

import http.client
import ipaddress
import logging
import math
import re
import secrets
import socket
import os
import struct
import ssl
import threading
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

log = logging.getLogger("crew.security.outbound")

_MAX_URL_LENGTH = 4096
_MAX_DNS_ANSWERS = 32
_MAX_REQUEST_HEADERS = 96
_MAX_REQUEST_HEADER_BYTES = 64 * 1024
_MAX_CONNECT_TIMEOUT_SECONDS = 600.0
_MAX_REQUEST_BODY_BYTES = 128 * 1024 * 1024
_DEFAULT_AGGREGATE_CONNECTIONS = 64
_DEFAULT_AGGREGATE_BYTES = 64 * 1024 * 1024
_MAX_AGGREGATE_SCOPES = 4096
_UNICODE_DOTS = str.maketrans({"。": ".", "．": ".", "｡": "."})
_SCHEME_DEFAULT_PORTS = {
    "http": 80,
    "https": 443,
    "ws": 80,
    "wss": 443,
}
_METADATA_HOSTS = frozenset(
    {
        "instance-data",
        "metadata",
        "metadata.aws.internal",
        "metadata.azure.internal",
        "metadata.google.internal",
    }
)
_METADATA_SUFFIXES = (
    ".metadata.aws.internal",
    ".metadata.google.internal",
    ".metadata.azure.internal",
)
_METADATA_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "169.254.169.254/32",
        "169.254.170.2/32",
        "168.63.129.16/32",
        "100.100.100.200/32",
        "192.0.0.192/32",
        "fd00:ec2::254/128",
    )
)
_NAT64_NETWORKS = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)
_SPECIAL_USE_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "0.0.0.0/8",
        "100.64.0.0/10",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "240.0.0.0/4",
    )
)
_METHOD_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Z-]{1,32}")
_HEADER_NAME_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}")
_AUDIT_SOURCES = frozenset(
    {"browser_proxy", "mcp-interaction-proxy", "test", "unspecified"}
)
_DOMAIN_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_LEGACY_IPV4_RE = re.compile(
    r"(?:0[xX][0-9a-fA-F]+|[0-9]+)(?:\.(?:0[xX][0-9a-fA-F]+|[0-9]+)){0,3}"
)


class OutboundDenied(ValueError):
    """Stable, non-sensitive outbound denial."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(f"outbound denied: {self.code}")


@dataclass(frozen=True)
class OutboundContext:
    """Host-derived identity bound to a sensitive outbound capability."""

    owner: str
    session: str
    task: str
    request: str
    source: str
    environment: str = "default"

    def require_complete(self) -> None:
        if not all(
            str(value).strip()
            for value in (
                self.owner,
                self.session,
                self.task,
                self.request,
                self.source,
                self.environment,
            )
        ):
            raise OutboundDenied("context_incomplete")


@dataclass(frozen=True)
class OutboundTarget:
    scheme: str
    host: str
    port: int
    method: str
    path: str = field(default="/", repr=False)
    query: str = field(default="", repr=False)

    @property
    def authority(self) -> str:
        display = f"[{self.host}]" if ":" in self.host else self.host
        default = _SCHEME_DEFAULT_PORTS.get(self.scheme)
        return display if self.port == default else f"{display}:{self.port}"

    @property
    def canonical_url(self) -> str:
        return urlunsplit(
            (self.scheme, self.authority, self.path or "/", self.query, "")
        )

    @property
    def audit_summary(self) -> str:
        """Origin-only summary; never includes userinfo, query, fragment, or credentials."""
        return f"{self.scheme}://{self.authority}"


@dataclass(frozen=True)
class ResolvedEndpoint:
    family: int
    address: str
    sockaddr: tuple[Any, ...]


@dataclass(frozen=True)
class ConnectionPlan:
    target: OutboundTarget
    endpoints: tuple[ResolvedEndpoint, ...]
    context: OutboundContext | None = None
    private_grant: str = field(default="", repr=False)
    expires_monotonic: float = 0.0
    _consumed: bool = field(default=False, init=False, repr=False)
    _consume_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )

    def consume(self) -> None:
        with self._consume_lock:
            if self._consumed:
                raise OutboundDenied("plan_reused")
            object.__setattr__(self, "_consumed", True)


@dataclass(frozen=True)
class _PrivateGrant:
    token: str = field(repr=False)
    context: OutboundContext
    host: str
    port: int
    scheme: str
    method: str
    expires_monotonic: float


class OutboundGrantRegistry:
    """In-memory, exact, short-lived and single-use private-network grants."""

    _MAX_TTL_SECONDS = 60.0
    _MAX_ACTIVE_GRANTS = 4096

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._grants: dict[str, _PrivateGrant] = {}

    def issue_private(
        self,
        context: OutboundContext,
        *,
        host: str,
        port: int,
        scheme: str,
        method: str = "GET",
        ttl_seconds: float = 30.0,
    ) -> str:
        context.require_complete()
        canonical_host = canonicalize_host(host)
        normalized_scheme = str(scheme).lower()
        if normalized_scheme not in _SCHEME_DEFAULT_PORTS:
            raise OutboundDenied("scheme_forbidden")
        normalized_method = str(method).upper()
        if _METHOD_RE.fullmatch(normalized_method) is None:
            raise OutboundDenied("invalid_method")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise OutboundDenied("invalid_port")
        ttl = float(ttl_seconds)
        if not 0 < ttl <= self._MAX_TTL_SECONDS:
            raise OutboundDenied("grant_ttl_invalid")
        now = self._clock()
        with self._lock:
            expired = [
                token
                for token, grant in self._grants.items()
                if now >= grant.expires_monotonic
            ]
            for token in expired:
                self._grants.pop(token, None)
            if len(self._grants) >= self._MAX_ACTIVE_GRANTS:
                raise OutboundDenied("grant_capacity")
            token = secrets.token_urlsafe(32)
            while token in self._grants:
                token = secrets.token_urlsafe(32)
            grant = _PrivateGrant(
                token=token,
                context=context,
                host=canonical_host,
                port=port,
                scheme=normalized_scheme,
                method=normalized_method,
                expires_monotonic=now + ttl,
            )
            self._grants[token] = grant
        return token

    def validate(
        self,
        token: str,
        context: OutboundContext | None,
        target: OutboundTarget,
        *,
        consume: bool,
    ) -> None:
        if context is None:
            raise OutboundDenied("private_grant_invalid")
        context.require_complete()
        with self._lock:
            grant = self._grants.get(str(token))
            if grant is None:
                raise OutboundDenied("private_grant_invalid")
            if self._clock() >= grant.expires_monotonic:
                self._grants.pop(grant.token, None)
                raise OutboundDenied("private_grant_invalid")
            if (
                grant.context != context
                or grant.host != target.host
                or grant.port != target.port
                or grant.scheme != target.scheme
                or grant.method != target.method
            ):
                raise OutboundDenied("private_grant_invalid")
            if consume:
                self._grants.pop(grant.token, None)


@dataclass(frozen=True)
class OutboundHttpResponse:
    final_url: str
    status: int
    headers: dict[str, str]
    body: bytes
    content_type: str
    charset: str


_BudgetKey = tuple[bool, str, str]


class OutboundBudgetLease:
    """One released-once reservation in an aggregate outbound budget."""

    def __init__(
        self,
        registry: OutboundBudgetRegistry,
        key: _BudgetKey,
        byte_budget: int,
    ) -> None:
        self._registry = registry
        self._key = key
        self._byte_budget = byte_budget
        self._released = False
        self._release_lock = threading.Lock()

    def release(self) -> None:
        with self._release_lock:
            if self._released:
                return
            self._registry._release(self._key, self._byte_budget)
            self._released = True

class OutboundBudgetRegistry:
    """Shared fail-closed in-flight connection and byte reservations."""

    def __init__(
        self,
        *,
        max_connections: int = _DEFAULT_AGGREGATE_CONNECTIONS,
        max_bytes: int = _DEFAULT_AGGREGATE_BYTES,
        max_scopes: int = _MAX_AGGREGATE_SCOPES,
    ) -> None:
        self._max_connections = self._positive_int(max_connections)
        self._max_bytes = self._positive_int(max_bytes)
        self._max_scopes = self._positive_int(max_scopes)
        # ponytail: process-local lock; shard by owner/task only if contention is measured.
        self._lock = threading.Lock()
        self._usage: dict[_BudgetKey, list[int]] = {}

    @staticmethod
    def _positive_int(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise OutboundDenied("aggregate_budget_invalid")
        return value

    @staticmethod
    def _key(context: OutboundContext | None) -> _BudgetKey:
        if context is None:
            return False, "", ""
        context.require_complete()
        return True, str(context.owner), str(context.task)

    @staticmethod
    def _audit_scope(context: OutboundContext | None) -> str:
        if context is None:
            return "unbound"
        source = str(context.source)
        return source if source in _AUDIT_SOURCES else "redacted"

    def acquire(
        self,
        context: OutboundContext | None,
        *,
        byte_budget: int,
    ) -> OutboundBudgetLease:
        if isinstance(byte_budget, bool) or not isinstance(byte_budget, int) or byte_budget < 0:
            raise OutboundDenied("aggregate_byte_limit")
        key = self._key(context)
        with self._lock:
            usage = self._usage.get(key)
            if usage is None:
                if len(self._usage) >= self._max_scopes:
                    self._deny(context, "aggregate_scope_limit")
                usage = [0, 0]
            if usage[0] >= self._max_connections:
                self._deny(context, "aggregate_connection_limit")
            if usage[1] + byte_budget > self._max_bytes:
                self._deny(context, "aggregate_byte_limit")
            if key not in self._usage:
                self._usage[key] = usage
            usage[0] += 1
            usage[1] += byte_budget
        return OutboundBudgetLease(self, key, byte_budget)

    def usage(self, context: OutboundContext | None = None) -> tuple[int, int]:
        key = self._key(context)
        with self._lock:
            usage = self._usage.get(key)
            return (0, 0) if usage is None else (usage[0], usage[1])

    def _deny(self, context: OutboundContext | None, code: str) -> None:
        log.warning(
            "outbound_budget_denied scope=%s reason=%s",
            self._audit_scope(context),
            code,
        )
        raise OutboundDenied(code)

    def _release(self, key: _BudgetKey, byte_budget: int) -> None:
        with self._lock:
            usage = self._usage.get(key)
            if usage is None or usage[0] < 1 or usage[1] < byte_budget:
                raise RuntimeError("outbound budget release mismatch")
            usage[0] -= 1
            usage[1] -= byte_budget
            if usage[0] == 0:
                self._usage.pop(key, None)


_DEFAULT_OUTBOUND_BUDGET = OutboundBudgetRegistry()


Resolver = Callable[..., Iterable[tuple[Any, ...]]]
SocketFactory = Callable[[int, int], Any]


class OutboundPolicy:
    """Default-deny parser plus all-answer public-address policy."""

    def __init__(
        self,
        *,
        resolver: Resolver = socket.getaddrinfo,
        socket_factory: SocketFactory = socket.socket,
        grants: OutboundGrantRegistry | None = None,
        clock: Callable[[], float] = time.monotonic,
        plan_ttl_seconds: float = 30.0,
    ) -> None:
        self._resolver = resolver
        self._socket_factory = socket_factory
        self._grants = grants
        self._clock = clock
        self._plan_ttl_seconds = min(60.0, max(0.1, float(plan_ttl_seconds)))

    @staticmethod
    def _audit_denial(
        target: OutboundTarget | None,
        context: OutboundContext | None,
        exc: OutboundDenied,
        *,
        phase: str = "policy",
    ) -> None:
        source = context.source if context is not None else "unspecified"
        if source not in _AUDIT_SOURCES:
            source = "redacted"
        if target is None:
            summary = "unparsed"
            host = "unparsed"
            port = 0
            protocol = ""
            method = ""
        else:
            try:
                host = canonicalize_host(target.host)
                port = target.port if isinstance(target.port, int) else 0
                protocol = target.scheme if target.scheme in _SCHEME_DEFAULT_PORTS else ""
                method = (
                    target.method
                    if isinstance(target.method, str)
                    and _METHOD_RE.fullmatch(target.method)
                    else "redacted"
                )
                summary = (
                    f"{protocol}://"
                    f"{f'[{host}]' if ':' in host else host}"
                    f"{f':{port}' if port and port != _SCHEME_DEFAULT_PORTS.get(protocol) else ''}"
                )
            except (OutboundDenied, TypeError):
                summary = "redacted"
                host = "redacted"
                port = 0
                protocol = ""
                method = ""
        log.warning(
            "outbound_denied target=%s reason=%s source=%s",
            summary,
            exc.code,
            source,
            extra={
                "outbound_event": "network_decision",
                "outbound_decision": "deny",
                "outbound_phase": phase,
                "outbound_host": host,
                "outbound_port": port,
                "outbound_protocol": protocol,
                "outbound_method": method,
                "outbound_reason": exc.code,
                "outbound_source": source,
            },
        )

    @staticmethod
    def _audit_allow(
        target: OutboundTarget,
        context: OutboundContext | None,
        *,
        phase: str,
    ) -> None:
        source = context.source if context is not None else "unspecified"
        if source not in _AUDIT_SOURCES:
            source = "redacted"
        log.info(
            "outbound_allowed target=%s phase=%s source=%s",
            target.audit_summary,
            phase,
            source,
            extra={
                "outbound_event": "network_decision",
                "outbound_decision": "allow",
                "outbound_phase": phase,
                "outbound_host": target.host,
                "outbound_port": target.port,
                "outbound_protocol": target.scheme,
                "outbound_method": target.method,
                "outbound_reason": "approved",
                "outbound_source": source,
            },
        )

    def plan_url(
        self,
        url: str,
        *,
        method: str = "GET",
        allowed_schemes: frozenset[str] = frozenset({"http", "https"}),
        context: OutboundContext | None = None,
        private_grant: str = "",
    ) -> ConnectionPlan:
        target: OutboundTarget | None = None
        try:
            parsed, target = self._canonicalize_url(
                url,
                method=method,
                allowed_schemes=allowed_schemes,
            )
            del parsed
            allow_private = self._validate_private_grant(
                private_grant,
                context,
                target,
                consume=False,
            )
            plan = ConnectionPlan(
                target=target,
                endpoints=self._resolve(
                    target,
                    allow_private=allow_private,
                    grantable=context is not None,
                ),
                context=context,
                private_grant=str(private_grant),
                expires_monotonic=self._clock() + self._plan_ttl_seconds,
            )
            self._audit_allow(target, context, phase="plan")
            return plan
        except OutboundDenied as exc:
            self._audit_denial(target, context, exc)
            raise

    def canonicalize_url(
        self,
        url: str,
        *,
        method: str = "GET",
        allowed_schemes: frozenset[str] = frozenset({"http", "https"}),
    ) -> tuple[SplitResult, OutboundTarget]:
        try:
            return self._canonicalize_url(
                url,
                method=method,
                allowed_schemes=allowed_schemes,
            )
        except OutboundDenied as exc:
            self._audit_denial(None, None, exc)
            raise

    def _canonicalize_url(
        self,
        url: str,
        *,
        method: str = "GET",
        allowed_schemes: frozenset[str] = frozenset({"http", "https"}),
    ) -> tuple[SplitResult, OutboundTarget]:
        raw = str(url)
        if not raw or len(raw) > _MAX_URL_LENGTH:
            raise OutboundDenied("invalid_url")
        if raw != raw.strip():
            raise OutboundDenied("invalid_url")
        if _contains_control(raw):
            raise OutboundDenied("control_character")
        if "\\" in raw:
            raise OutboundDenied("invalid_url")
        if re.search(r"%(?![0-9a-fA-F]{2})", raw):
            raise OutboundDenied("invalid_url")
        try:
            parsed = urlsplit(raw)
        except ValueError as exc:
            raise OutboundDenied("invalid_url") from exc
        scheme = parsed.scheme.lower()
        if scheme not in allowed_schemes or scheme not in _SCHEME_DEFAULT_PORTS:
            raise OutboundDenied("scheme_forbidden")
        if parsed.username is not None or parsed.password is not None:
            raise OutboundDenied("userinfo_forbidden")
        if not parsed.netloc or parsed.hostname is None:
            raise OutboundDenied("authority_required")
        if parsed.netloc.endswith(":"):
            raise OutboundDenied("invalid_port")
        host = canonicalize_host(parsed.hostname)
        try:
            parsed_port = parsed.port
        except ValueError as exc:
            raise OutboundDenied("invalid_port") from exc
        port = (
            parsed_port
            if parsed_port is not None
            else _SCHEME_DEFAULT_PORTS[scheme]
        )
        if isinstance(port, bool) or not 1 <= int(port) <= 65535:
            raise OutboundDenied("invalid_port")
        normalized_method = str(method).upper()
        if _METHOD_RE.fullmatch(normalized_method) is None:
            raise OutboundDenied("invalid_method")
        path = parsed.path or "/"
        if not is_safe_authorization_path(path):
            raise OutboundDenied("ambiguous_path")
        target = OutboundTarget(
            scheme=scheme,
            host=host,
            port=int(port),
            method=normalized_method,
            path=path,
            query=parsed.query,
        )
        self._reject_metadata_name(host)
        return parsed, target

    def plan_authority(
        self,
        host: str,
        port: int,
        *,
        scheme: str,
        method: str = "CONNECT",
        context: OutboundContext | None = None,
        private_grant: str = "",
    ) -> ConnectionPlan:
        target: OutboundTarget | None = None
        try:
            normalized_scheme = str(scheme).lower()
            if normalized_scheme not in _SCHEME_DEFAULT_PORTS:
                raise OutboundDenied("scheme_forbidden")
            canonical_host = canonicalize_host(host)
            if (
                isinstance(port, bool)
                or not isinstance(port, int)
                or not 1 <= port <= 65535
            ):
                raise OutboundDenied("invalid_port")
            normalized_method = str(method).upper()
            if _METHOD_RE.fullmatch(normalized_method) is None:
                raise OutboundDenied("invalid_method")
            target = OutboundTarget(
                scheme=normalized_scheme,
                host=canonical_host,
                port=port,
                method=normalized_method,
            )
            self._reject_metadata_name(canonical_host)
            allow_private = self._validate_private_grant(
                private_grant,
                context,
                target,
                consume=False,
            )
            plan = ConnectionPlan(
                target=target,
                endpoints=self._resolve(
                    target,
                    allow_private=allow_private,
                    grantable=context is not None,
                ),
                context=context,
                private_grant=str(private_grant),
                expires_monotonic=self._clock() + self._plan_ttl_seconds,
            )
            self._audit_allow(target, context, phase="plan")
            return plan
        except OutboundDenied as exc:
            self._audit_denial(target, context, exc)
            raise

    def connect_socket(
        self,
        plan: ConnectionPlan,
        *,
        context: OutboundContext | None = None,
        timeout: float = 10.0,
    ) -> Any:
        try:
            return self._connect_socket(
                plan,
                context=context,
                timeout=timeout,
            )
        except OutboundDenied as exc:
            self._audit_denial(plan.target, context, exc, phase="connect")
            raise

    def _connect_socket(
        self,
        plan: ConnectionPlan,
        *,
        context: OutboundContext | None = None,
        timeout: float = 10.0,
    ) -> Any:
        if plan.context != context:
            raise OutboundDenied("context_mismatch")
        try:
            connect_budget = float(timeout)
        except (TypeError, ValueError) as exc:
            raise OutboundDenied("timeout_invalid") from exc
        if (
            not math.isfinite(connect_budget)
            or connect_budget <= 0
            or connect_budget > _MAX_CONNECT_TIMEOUT_SECONDS
        ):
            raise OutboundDenied("timeout_invalid")
        if self._clock() >= plan.expires_monotonic:
            if plan.private_grant:
                raise OutboundDenied("private_grant_invalid")
            raise OutboundDenied("plan_expired")
        plan.consume()
        allow_private = self._validate_private_grant(
            plan.private_grant,
            context,
            plan.target,
            consume=True,
        )
        self._validate_plan_endpoints(
            plan,
            allow_private=allow_private,
            grantable=context is not None,
        )
        deadline = self._clock() + connect_budget
        last_error: OSError | None = None
        for endpoint in plan.endpoints:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise OutboundDenied("connect_timeout") from last_error
            try:
                sock = self._socket_factory(endpoint.family, socket.SOCK_STREAM)
            except OSError as exc:
                last_error = exc
                continue
            try:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise OutboundDenied("connect_timeout")
                sock.settimeout(remaining)
                sock.connect(endpoint.sockaddr)
                peer_info = sock.getpeername()
                peer = ipaddress.ip_address(str(peer_info[0]).split("%", 1)[0])
                if peer.compressed != endpoint.address:
                    raise OutboundDenied("peer_address_mismatch")
                if int(peer_info[1]) != plan.target.port:
                    raise OutboundDenied("peer_port_mismatch")
                self._validate_address(
                    peer,
                    allow_private=allow_private,
                    grantable=context is not None,
                )
                self._audit_allow(plan.target, context, phase="connect")
                return sock
            except OutboundDenied:
                sock.close()
                raise
            except OSError as exc:
                last_error = exc
                sock.close()
        if self._clock() >= deadline:
            raise OutboundDenied("connect_timeout") from last_error
        raise OutboundDenied("connect_failed") from last_error

    def _validate_plan_endpoints(
        self,
        plan: ConnectionPlan,
        *,
        allow_private: bool,
        grantable: bool,
    ) -> None:
        """Recheck the immutable plan immediately before its final socket."""
        target = plan.target
        if not isinstance(target.scheme, str) or target.scheme not in _SCHEME_DEFAULT_PORTS:
            raise OutboundDenied("scheme_forbidden")
        if canonicalize_host(target.host) != target.host:
            raise OutboundDenied("plan_target_not_canonical")
        if (
            isinstance(target.port, bool)
            or not isinstance(target.port, int)
            or not 1 <= target.port <= 65535
        ):
            raise OutboundDenied("invalid_port")
        if not isinstance(target.method, str) or _METHOD_RE.fullmatch(target.method) is None:
            raise OutboundDenied("invalid_method")
        if not is_safe_authorization_path(target.path or "/"):
            raise OutboundDenied("ambiguous_path")
        self._reject_metadata_name(target.host)
        if not plan.endpoints:
            raise OutboundDenied("dns_no_answers")
        for endpoint in plan.endpoints:
            try:
                sockaddr = endpoint.sockaddr
                address = ipaddress.ip_address(
                    str(endpoint.address).split("%", 1)[0]
                )
                sockaddr_address = ipaddress.ip_address(
                    str(sockaddr[0]).split("%", 1)[0]
                )
                sockaddr_port = sockaddr[1]
            except (IndexError, TypeError, ValueError) as exc:
                raise OutboundDenied("endpoint_invalid") from exc
            expected_family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
            if (
                endpoint.family != expected_family
                or sockaddr_address != address
                or isinstance(sockaddr_port, bool)
                or not isinstance(sockaddr_port, int)
                or sockaddr_port != target.port
                or len(sockaddr) != (4 if address.version == 6 else 2)
            ):
                raise OutboundDenied("endpoint_invalid")
            if address.version == 6 and (sockaddr[2] != 0 or sockaddr[3] != 0):
                raise OutboundDenied("endpoint_invalid")
            if address.compressed != str(endpoint.address).lower():
                raise OutboundDenied("endpoint_invalid")
            self._validate_address(
                address,
                allow_private=allow_private,
                grantable=grantable,
            )

    def validate_resolved_address(
        self,
        value: str,
        *,
        allow_private: bool = False,
        grantable: bool = False,
    ) -> str:
        """Validate one already-resolved numeric address with the shared rules."""
        try:
            address = ipaddress.ip_address(str(value).split("%", 1)[0])
        except ValueError as exc:
            raise OutboundDenied("dns_invalid_answer") from exc
        self._validate_address(
            address,
            allow_private=bool(allow_private),
            grantable=bool(grantable),
        )
        return address.compressed

    def _validate_private_grant(
        self,
        token: str,
        context: OutboundContext | None,
        target: OutboundTarget,
        *,
        consume: bool,
    ) -> bool:
        if not token:
            return False
        if self._grants is None:
            raise OutboundDenied("private_grant_invalid")
        self._grants.validate(token, context, target, consume=consume)
        return True

    def _resolve(
        self,
        target: OutboundTarget,
        *,
        allow_private: bool,
        grantable: bool,
    ) -> tuple[ResolvedEndpoint, ...]:
        localhost_name = target.host == "localhost" or target.host.endswith(
            ".localhost"
        )
        if localhost_name and not allow_private:
            raise OutboundDenied(
                "private_grant_required" if grantable else "non_public_target"
            )
        literal = _ip_literal(target.host)
        if literal is not None:
            self._validate_address(
                literal,
                allow_private=allow_private,
                grantable=grantable,
            )
            family = socket.AF_INET6 if literal.version == 6 else socket.AF_INET
            sockaddr: tuple[Any, ...] = (
                (literal.compressed, target.port, 0, 0)
                if family == socket.AF_INET6
                else (literal.compressed, target.port)
            )
            return (ResolvedEndpoint(family, literal.compressed, sockaddr),)
        try:
            answers = tuple(
                self._resolver(
                    target.host,
                    target.port,
                    type=socket.SOCK_STREAM,
                    proto=socket.IPPROTO_TCP,
                )
            )
        except OSError as exc:
            raise OutboundDenied("dns_resolution_failed") from exc
        if not answers:
            raise OutboundDenied("dns_no_answers")
        if len(answers) > _MAX_DNS_ANSWERS:
            raise OutboundDenied("dns_answer_limit")
        endpoints: list[ResolvedEndpoint] = []
        seen: set[tuple[int, str]] = set()
        for answer in answers:
            try:
                family = int(answer[0])
                raw_address = str(answer[4][0]).split("%", 1)[0]
                address = ipaddress.ip_address(raw_address)
            except (IndexError, TypeError, ValueError) as exc:
                raise OutboundDenied("dns_invalid_answer") from exc
            if localhost_name and not address.is_loopback:
                raise OutboundDenied("localhost_address_mismatch")
            self._validate_address(
                address,
                allow_private=allow_private,
                grantable=grantable,
            )
            key = (family, address.compressed)
            if key in seen:
                continue
            seen.add(key)
            if address.version == 6:
                family = socket.AF_INET6
                sockaddr = (address.compressed, target.port, 0, 0)
            else:
                family = socket.AF_INET
                sockaddr = (address.compressed, target.port)
            endpoints.append(ResolvedEndpoint(family, address.compressed, sockaddr))
        if not endpoints:
            raise OutboundDenied("dns_no_answers")
        return tuple(endpoints)

    @staticmethod
    def _reject_metadata_name(host: str) -> None:
        if host in _METADATA_HOSTS or any(host.endswith(suffix) for suffix in _METADATA_SUFFIXES):
            raise OutboundDenied("metadata_target")

    @staticmethod
    def _validate_address(
        address: ipaddress.IPv4Address | ipaddress.IPv6Address,
        *,
        allow_private: bool,
        grantable: bool,
    ) -> None:
        embedded = _embedded_ipv4(address)
        if any(address in network for network in _METADATA_NETWORKS) or (
            embedded is not None
            and any(embedded in network for network in _METADATA_NETWORKS)
        ):
            raise OutboundDenied("metadata_target")
        sensitive = (
            embedded is not None and _address_is_non_public(embedded)
        ) or _address_is_non_public(address)
        if sensitive and not allow_private:
            raise OutboundDenied(
                "private_grant_required" if grantable else "non_public_target"
            )


class OutboundHttpClient:
    """Bounded HTTP(S) client whose socket is created only from a connection plan."""

    _REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
    _MAX_CONNECTIONS = 64
    _SENSITIVE_HEADERS = frozenset({"authorization", "cookie"})
    _FORBIDDEN_HEADERS = frozenset(
        {
            "connection",
            "content-length",
            "host",
            "keep-alive",
            "proxy-authenticate",
            "proxy-authorization",
            "proxy-connection",
            "te",
            "trailer",
            "transfer-encoding",
            "upgrade",
        }
    )

    def __init__(
        self,
        policy: OutboundPolicy | None = None,
        *,
        budget: OutboundBudgetRegistry | None = None,
    ) -> None:
        self.policy = policy or OutboundPolicy()
        self._connection_slots = threading.BoundedSemaphore(self._MAX_CONNECTIONS)
        self._budget = budget if budget is not None else _DEFAULT_OUTBOUND_BUDGET

    def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
        max_bytes: int = 2_000_000,
        max_request_bytes: int = 10_000_000,
        max_redirects: int = 0,
        return_redirect_response: bool = False,
        context: OutboundContext | None = None,
        private_grant: str = "",
        redirect_authorizer: Callable[[OutboundTarget, OutboundTarget], bool] | None = None,
    ) -> OutboundHttpResponse:
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes < 0
            or max_bytes > 100_000_000
        ):
            raise OutboundDenied("response_limit_invalid")
        if max_redirects < 0 or max_redirects > 10:
            raise OutboundDenied("redirect_limit_invalid")
        current_url = str(url)
        current_method = str(method).upper()
        current_body = body
        current_headers = self._request_headers(headers)
        first_grant = str(private_grant)
        previous_target: OutboundTarget | None = None

        for hop in range(max_redirects + 1):
            plan = self.policy.plan_url(
                current_url,
                method=current_method,
                context=context,
                private_grant=first_grant if hop == 0 else "",
            )
            if previous_target is not None and previous_target.audit_summary != plan.target.audit_summary:
                current_headers = {
                    name: value
                    for name, value in current_headers.items()
                    if name.lower() not in self._SENSITIVE_HEADERS
                }
                try:
                    authorized = (
                        redirect_authorizer(previous_target, plan.target)
                        if redirect_authorizer is not None
                        else False
                    )
                except Exception as exc:
                    denied = OutboundDenied("redirect_reauthorization_failed")
                    self.policy._audit_denial(
                        plan.target,
                        context,
                        denied,
                        phase="redirect",
                    )
                    raise denied from exc
                if not authorized:
                    denied = OutboundDenied("redirect_reauthorization_required")
                    self.policy._audit_denial(
                        plan.target,
                        context,
                        denied,
                        phase="redirect",
                    )
                    raise denied
                log.info(
                    "outbound_redirect from=%s to=%s decision=allow",
                    previous_target.audit_summary,
                    plan.target.audit_summary,
                    extra={
                        "outbound_event": "network_redirect",
                        "outbound_decision": "allow",
                        "outbound_phase": "redirect",
                        "outbound_from": previous_target.audit_summary,
                        "outbound_to": plan.target.audit_summary,
                        "outbound_source": (
                            context.source
                            if context is not None and context.source in _AUDIT_SOURCES
                            else "redacted"
                        ),
                    },
                )
            previous_target = plan.target
            response = self.fetch_plan(
                plan,
                method=current_method,
                body=current_body,
                headers=current_headers,
                timeout=timeout,
                max_bytes=max_bytes,
                max_request_bytes=max_request_bytes,
                context=context,
            )
            if response.status in self._REDIRECT_STATUSES:
                if return_redirect_response:
                    return response
                if hop >= max_redirects:
                    denied = OutboundDenied(
                        "redirect_forbidden" if max_redirects == 0 else "redirect_limit"
                    )
                    self.policy._audit_denial(
                        plan.target,
                        context,
                        denied,
                        phase="redirect",
                    )
                    raise denied
                location = response.headers.get("location", "")
                if not location:
                    denied = OutboundDenied("redirect_location_missing")
                    self.policy._audit_denial(
                        plan.target,
                        context,
                        denied,
                        phase="redirect",
                    )
                    raise denied
                current_url = urljoin(plan.target.canonical_url, location)
                if response.status == 303 or (
                    response.status in {301, 302} and current_method == "POST"
                ):
                    current_method = "GET"
                    current_body = None
                    current_headers = {
                        name: value
                        for name, value in current_headers.items()
                        if name.lower() not in {"content-length", "content-type"}
                    }
                continue
            return response
        raise OutboundDenied("redirect_limit")

    def fetch_plan(
        self,
        plan: ConnectionPlan,
        *,
        method: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
        max_bytes: int = 2_000_000,
        max_request_bytes: int = 10_000_000,
        context: OutboundContext | None = None,
    ) -> OutboundHttpResponse:
        if not self._connection_slots.acquire(blocking=False):
            raise OutboundDenied("connection_limit")
        try:
            return self._fetch_plan_unbounded(
                plan,
                method=method,
                body=body,
                headers=headers,
                timeout=timeout,
                max_bytes=max_bytes,
                max_request_bytes=max_request_bytes,
                context=context,
            )
        finally:
            self._connection_slots.release()

    def _fetch_plan_unbounded(
        self,
        plan: ConnectionPlan,
        *,
        method: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
        max_bytes: int = 2_000_000,
        max_request_bytes: int = 10_000_000,
        context: OutboundContext | None = None,
    ) -> OutboundHttpResponse:
        """Perform exactly one request using an already-authorized, pinned plan."""
        normalized_method = str(method).upper()
        if normalized_method != plan.target.method:
            raise OutboundDenied("authorization_mismatch")
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes < 0
            or max_bytes > 100_000_000
        ):
            raise OutboundDenied("response_limit_invalid")
        if max_request_bytes < 0 or max_request_bytes > _MAX_REQUEST_BODY_BYTES:
            raise OutboundDenied("request_limit_invalid")
        if body is not None and not isinstance(body, (bytes, bytearray, memoryview)):
            raise OutboundDenied("invalid_request_body")
        request_body = bytes(body) if body is not None else None
        if request_body is not None and len(request_body) > max_request_bytes:
            raise OutboundDenied("request_too_large")
        current_headers = self._request_headers(headers)
        try:
            request_timeout = float(timeout)
        except (TypeError, ValueError) as exc:
            raise OutboundDenied("timeout_invalid") from exc
        if (
            not math.isfinite(request_timeout)
            or request_timeout <= 0
            or request_timeout > _MAX_CONNECT_TIMEOUT_SECONDS
        ):
            raise OutboundDenied("timeout_invalid")
        deadline = time.monotonic() + request_timeout
        connection: http.client.HTTPConnection
        if plan.target.scheme == "https":
            connection = _PolicyHTTPSConnection(
                self.policy,
                plan,
                context=context,
                timeout=timeout,
            )
        else:
            connection = _PolicyHTTPConnection(
                self.policy,
                plan,
                context=context,
                timeout=timeout,
            )
        request_headers = dict(current_headers)
        request_headers["Host"] = plan.target.authority
        request_headers.setdefault("User-Agent", "Crew/0.1")
        request_headers["Connection"] = "close"
        request_path = plan.target.path or "/"
        if plan.target.query:
            request_path += f"?{plan.target.query}"
        budget_lease = self._budget.acquire(
            context,
            byte_budget=len(request_body or b"") + max_bytes,
        )
        try:
            connection.request(
                normalized_method,
                request_path,
                body=request_body,
                headers=request_headers,
            )
            response = connection.getresponse()
            response_pairs = self._response_headers(response.getheaders())
            chunks: list[bytes] = []
            received = 0
            while received <= max_bytes:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OutboundDenied("response_timeout")
                response_socket = connection.sock
                if response_socket is None:
                    response_socket = getattr(
                        getattr(getattr(response, "fp", None), "raw", None),
                        "_sock",
                        None,
                    )
                if response_socket is not None:
                    response_socket.settimeout(remaining)
                chunk = response.read1(min(64 * 1024, max_bytes + 1 - received))
                if not chunk:
                    break
                chunks.append(chunk)
                received += len(chunk)
            raw = b"".join(chunks)
            if len(raw) > max_bytes:
                raise OutboundDenied("response_too_large")
            response_headers = {
                name: value
                for name, value in response_pairs
            }
            content_type = response_headers.get("content-type", "")
            charset = response.headers.get_content_charset() or "utf-8"
            return OutboundHttpResponse(
                final_url=plan.target.canonical_url,
                status=int(response.status),
                headers=response_headers,
                body=raw,
                content_type=content_type,
                charset=charset,
            )
        except OutboundDenied:
            raise
        except TimeoutError as exc:
            raise OutboundDenied("response_timeout") from exc
        except Exception as exc:
            raise OutboundDenied("http_transport_failed") from exc
        finally:
            try:
                connection.close()
            finally:
                budget_lease.release()

    @classmethod
    def _request_headers(cls, headers: dict[str, str] | None) -> dict[str, str]:
        result: dict[str, str] = {}
        total_bytes = 0
        for raw_name, raw_value in (headers or {}).items():
            name = str(raw_name)
            value = str(raw_value)
            normalized = name.lower()
            if (
                _HEADER_NAME_RE.fullmatch(name) is None
                or _contains_control(value)
                or normalized in cls._FORBIDDEN_HEADERS
                or normalized.startswith("proxy-")
            ):
                raise OutboundDenied("forbidden_header")
            try:
                encoded_value = value.encode("latin-1")
            except UnicodeEncodeError as exc:
                raise OutboundDenied("forbidden_header") from exc
            total_bytes += len(name) + len(encoded_value) + 4
            if (
                len(result) >= _MAX_REQUEST_HEADERS
                or total_bytes > _MAX_REQUEST_HEADER_BYTES
            ):
                raise OutboundDenied("request_headers_too_large")
            result[name] = value
        return result

    @staticmethod
    def _response_headers(
        headers: Iterable[tuple[str, str]],
    ) -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []
        total_bytes = 0
        for raw_name, raw_value in headers:
            name = str(raw_name).lower()
            value = str(raw_value)
            if (
                _HEADER_NAME_RE.fullmatch(name) is None
                or any(
                    unicodedata.category(char) == "Cc" and char != "\t"
                    for char in value
                )
            ):
                raise OutboundDenied("invalid_response_header")
            try:
                encoded_value = value.encode("latin-1")
            except UnicodeEncodeError as exc:
                raise OutboundDenied("invalid_response_header") from exc
            total_bytes += len(name) + len(encoded_value) + 4
            if (
                len(result) >= _MAX_REQUEST_HEADERS
                or total_bytes > _MAX_REQUEST_HEADER_BYTES
            ):
                raise OutboundDenied("response_headers_too_large")
            result.append((name, value))

        content_lengths = [value for name, value in result if name == "content-length"]
        transfer_encodings = [
            value for name, value in result if name == "transfer-encoding"
        ]
        content_encodings = [
            value.strip()
            for name, value in result
            if name == "content-encoding"
        ]
        if (
            len(content_lengths) > 1
            or len(transfer_encodings) > 1
            or content_lengths and transfer_encodings
        ):
            raise OutboundDenied("ambiguous_response_framing")
        if transfer_encodings and transfer_encodings[0].strip().lower() != "chunked":
            raise OutboundDenied("ambiguous_response_framing")
        if content_encodings and (
            len(content_encodings) != 1
            or content_encodings[0].lower() != "identity"
        ):
            raise OutboundDenied("unsupported_response_encoding")
        return result


class _PolicyHTTPConnection(http.client.HTTPConnection):
    def __init__(
        self,
        policy: OutboundPolicy,
        plan: ConnectionPlan,
        *,
        context: OutboundContext | None,
        timeout: float,
    ) -> None:
        super().__init__(plan.target.host, plan.target.port, timeout=timeout)
        self._outbound_policy = policy
        self._outbound_plan = plan
        self._outbound_context = context

    def connect(self) -> None:
        self.sock = self._outbound_policy.connect_socket(
            self._outbound_plan,
            context=self._outbound_context,
            timeout=float(self.timeout),
        )


class _PolicyHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        policy: OutboundPolicy,
        plan: ConnectionPlan,
        *,
        context: OutboundContext | None,
        timeout: float,
    ) -> None:
        super().__init__(
            plan.target.host,
            plan.target.port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._outbound_policy = policy
        self._outbound_plan = plan
        self._outbound_context = context

    def connect(self) -> None:
        raw = self._outbound_policy.connect_socket(
            self._outbound_plan,
            context=self._outbound_context,
            timeout=float(self.timeout),
        )
        try:
            self.sock = self._context.wrap_socket(
                raw,
                server_hostname=self._outbound_plan.target.host,
            )
        except Exception:
            raw.close()
            raise


def canonicalize_host(value: str) -> str:
    raw = str(value)
    if not raw or _contains_control(raw) or any(char.isspace() for char in raw):
        raise OutboundDenied("invalid_host")
    if "%" in raw:
        raise OutboundDenied("zone_id_forbidden")
    raw = raw.translate(_UNICODE_DOTS)
    raw = raw.removesuffix(".")
    if not raw or raw.endswith("."):
        raise OutboundDenied("invalid_host")
    literal = _ip_literal(raw)
    if literal is not None:
        return literal.compressed.lower()
    try:
        ascii_host = raw.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise OutboundDenied("invalid_host") from exc
    legacy = _legacy_ipv4(ascii_host)
    if legacy is not None:
        return legacy.compressed
    if _LEGACY_IPV4_RE.fullmatch(ascii_host):
        raise OutboundDenied("invalid_host")
    if len(ascii_host) > 253:
        raise OutboundDenied("invalid_host")
    labels = ascii_host.split(".")
    if any(_DOMAIN_LABEL_RE.fullmatch(label) is None for label in labels):
        raise OutboundDenied("invalid_host")
    return ascii_host


def _legacy_ipv4(value: str) -> ipaddress.IPv4Address | None:
    if _LEGACY_IPV4_RE.fullmatch(value) is None:
        return None
    raw_parts = value.split(".")
    try:
        parts = [_legacy_component(part) for part in raw_parts]
    except ValueError as exc:
        raise OutboundDenied("invalid_host") from exc
    if len(parts) == 1:
        number = parts[0]
        if number > 0xFFFFFFFF:
            raise OutboundDenied("invalid_host")
    elif len(parts) == 2:
        if parts[0] > 0xFF or parts[1] > 0xFFFFFF:
            raise OutboundDenied("invalid_host")
        number = (parts[0] << 24) | parts[1]
    elif len(parts) == 3:
        if parts[0] > 0xFF or parts[1] > 0xFF or parts[2] > 0xFFFF:
            raise OutboundDenied("invalid_host")
        number = (parts[0] << 24) | (parts[1] << 16) | parts[2]
    else:
        if any(part > 0xFF for part in parts):
            raise OutboundDenied("invalid_host")
        number = (
            (parts[0] << 24)
            | (parts[1] << 16)
            | (parts[2] << 8)
            | parts[3]
        )
    return ipaddress.IPv4Address(number)


def _legacy_component(value: str) -> int:
    if value.lower().startswith("0x"):
        # URL parsers historically accept hexadecimal IPv4 components; base=0
        # cannot also preserve the explicit legacy-octal branch below.
        return int(value[2:], 16)  # noqa: FURB166
    if len(value) > 1 and value.startswith("0"):
        return int(value[1:] or "0", 8)
    return int(value, 10)


def _ip_literal(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    candidate = value.strip("[]")
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return _legacy_ipv4(candidate)


def _embedded_ipv4(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | None:
    if isinstance(address, ipaddress.IPv4Address):
        return None
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address.sixtofour is not None:
        return address.sixtofour
    if address.teredo is not None:
        return address.teredo[1]
    packed = address.packed
    if packed[:12] == b"\x00" * 12:
        return ipaddress.IPv4Address(packed[-4:])
    if any(address in network for network in _NAT64_NETWORKS):
        return ipaddress.IPv4Address(packed[-4:])
    return None


def _address_is_non_public(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    return bool(
        not address.is_global
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or any(address in network for network in _SPECIAL_USE_NETWORKS)
        or (
            isinstance(address, ipaddress.IPv6Address)
            and (address.is_site_local or address.sixtofour is not None or address.teredo is not None)
        )
    )


def _contains_control(value: str) -> bool:
    return any(unicodedata.category(char) == "Cc" for char in value)


def is_safe_authorization_path(path: str) -> bool:
    """Return whether a URL path has one unambiguous authorization reading.

    This is a direct port of
    ``codex-rs/network-proxy/src/authorization_path.rs``.  Upstream servers may
    decode ``%2e``/``%2f``/``%5c`` or collapse dot segments after policy has
    already authorized the request, so those spellings must not reach a shared
    Browser/MCP/file decision as if they were an ordinary literal path.
    """
    for segment in str(path).split("/"):
        decoded_dots = 0
        has_non_dot = False
        index = 0
        while index < len(segment):
            char = segment[index]
            if char == ".":
                decoded_dots += 1
                index += 1
                continue
            if char == "\\":
                return False
            if char == "%":
                escape = segment[index + 1 : index + 3]
                if len(escape) != 2:
                    return False
                try:
                    decoded = int(escape, 16)
                except ValueError:
                    return False
                if decoded in {ord("%"), ord("/"), ord("\\")}:
                    return False
                if decoded == ord("."):
                    decoded_dots += 1
                else:
                    has_non_dot = True
                index += 3
                continue
            has_non_dot = True
            index += 1
        if not has_non_dot and decoded_dots in {1, 2}:
            return False
    return True


# ---------------------------------------------------------------------------
# Public HTTP boundary (dev lineage): SSRF-guarded public targets with
# DNS-pinned redirects and optional upstream proxy cascading. Authorization
# still happens per-hop through security_guard.authorize_network_tool before
# these functions are called.
# ---------------------------------------------------------------------------



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
