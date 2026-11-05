"""独立线程 health 服务的契约测试：与业务事件循环隔离、instance proof 与主端点对齐。"""

from __future__ import annotations

import hashlib
import hmac
import json
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from crew.gateway.health_server import (
    GatewayHealthServer,
    build_health_components,
    note_loop_tick,
    read_loop_lag_ms,
    set_health_components,
)
from crew.gateway.instance_auth import (
    GATEWAY_INSTANCE_CHALLENGE_HEADER,
    GATEWAY_INSTANCE_DIRECTORY,
    GATEWAY_INSTANCE_KEY_FILENAME,
)

PROOF_CONTEXT = b"crew-gateway-instance-v1\x00"


def _write_key(crew_home: Path, encoded: bytes = b"11" * 32) -> None:
    directory = crew_home / GATEWAY_INSTANCE_DIRECTORY
    directory.mkdir(parents=True, mode=0o700)
    directory.chmod(0o700)
    key_file = directory / GATEWAY_INSTANCE_KEY_FILENAME
    key_file.write_bytes(encoded)
    key_file.chmod(0o600)


def _get_json(
    port: int,
    path: str = "/api/health",
    headers: dict[str, str] | None = None,
) -> tuple[int, dict]:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


@pytest.fixture
def health_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    crew_home = tmp_path / ".Crew"
    monkeypatch.setenv("CREW_HOME", str(crew_home))
    note_loop_tick(time.monotonic())
    set_health_components(None)
    server = GatewayHealthServer(port=0)
    bound = server.start()
    assert bound is not None
    yield bound, crew_home
    server.stop()
    set_health_components(None)


def test_health_thread_server_without_challenge(health_server):
    port, _ = health_server
    status, body = _get_json(port)

    assert status == 200
    assert body["ok"] is True
    assert body["service"] == "crew-gateway"
    # 哨兵 task 尚未接入时 start() 已写入基线时间戳，字段从启动起就存在。
    assert isinstance(body["loop_lag_ms"], int | float)


def test_health_thread_server_returns_domain_separated_proof(health_server):
    port, crew_home = health_server
    encoded_key = b"23" * 32
    _write_key(crew_home, encoded_key)
    challenge = "ab" * 32
    expected = hmac.new(
        bytes.fromhex(encoded_key.decode("ascii")),
        PROOF_CONTEXT + challenge.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()

    status, body = _get_json(port, headers={GATEWAY_INSTANCE_CHALLENGE_HEADER: challenge})

    assert status == 200
    assert body["ok"] is True
    assert body["instance_proof"] == expected


def test_health_thread_server_challenge_fails_closed(health_server):
    port, _ = health_server
    # 未写入实例密钥：格式非法 challenge → 400，格式合法 challenge → fail closed 503。
    malformed_status, _ = _get_json(
        port, headers={GATEWAY_INSTANCE_CHALLENGE_HEADER: "not-a-challenge"}
    )
    assert malformed_status == 400

    missing_key_status, _ = _get_json(
        port, headers={GATEWAY_INSTANCE_CHALLENGE_HEADER: "cd" * 32}
    )
    assert missing_key_status == 503


def test_health_thread_server_rejects_unknown_path(health_server):
    port, _ = health_server
    status, _ = _get_json(port, path="/api/other")
    assert status == 404


def test_health_thread_server_reports_loop_lag(health_server):
    port, _ = health_server
    note_loop_tick(time.monotonic() - 0.5)
    lag = read_loop_lag_ms()
    assert lag is not None and lag >= 400

    _, body = _get_json(port)
    assert body["loop_lag_ms"] >= 450


def test_health_thread_server_includes_components_snapshot(health_server):
    port, _ = health_server
    set_health_components({"startup": {"status": "starting"}, "cron": {"status": "disabled"}})
    _, body = _get_json(port)
    assert body["components"] == {
        "startup": {"status": "starting"},
        "cron": {"status": "disabled"},
    }


def test_health_port_occupied_degrades_to_main_port_only(health_server):
    occupied_port, _ = health_server
    conflicting = GatewayHealthServer(port=occupied_port)
    try:
        assert conflicting.start() is None
        assert conflicting._httpd is None  # noqa: SLF001 - 降级后无服务句柄
    finally:
        conflicting.stop()


def test_health_thread_server_stop_closes_port(health_server):
    port, _ = health_server
    server = GatewayHealthServer(port=port)
    assert server.start() is None  # 原服务仍占用端口，直接降级

    # 起一个新服务再停掉：停止后端口应拒绝连接，且 stop 幂等。
    fresh = GatewayHealthServer(port=0)
    fresh_port = fresh.start()
    assert fresh_port is not None
    fresh.stop()
    fresh.stop()
    with pytest.raises(OSError):  # ConnectionRefusedError
        socket.create_connection(("127.0.0.1", fresh_port), timeout=3)


def test_build_health_components_statuses():
    class _Cron:
        def __init__(self, *, running: bool = False, start_error: str = "") -> None:
            self.is_running = running
            self.start_error = start_error

    ready = build_health_components("ready", _Cron(running=True))
    assert ready["startup"]["status"] == "ready"
    assert ready["cron"]["status"] == "ready"

    failed = build_health_components("failed", _Cron(start_error="boom"))
    assert failed["startup"]["status"] == "failed"
    assert "message" in failed["startup"]
    assert failed["cron"]["status"] == "failed"

    starting = build_health_components("starting", None)
    assert starting["cron"]["status"] == "disabled"
