"""Desktop Gateway lease and its separate authenticated launch proof."""

from __future__ import annotations

import hmac
import os
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest
import uvicorn
from fastapi.testclient import TestClient

from crew.gateway import app as gateway_app
from crew.gateway import desktop_parent_lease as lease_module
from crew.gateway import instance_auth
from crew.gateway.desktop_parent_lease import (
    DESKTOP_PARENT_LEASE_ARG,
    DESKTOP_PARENT_LEASE_KEY_ENV,
    DesktopParentLease,
    bootstrap_desktop_parent_lease,
)


def test_opted_out_gateway_scrubs_secret_without_using_stdin(monkeypatch):
    monkeypatch.setenv(DESKTOP_PARENT_LEASE_KEY_ENV, "a1" * 32)
    monkeypatch.setattr(lease_module, "_active_lease", None)

    class _NoStdin:
        def fileno(self):
            pytest.fail("an ordinary Gateway must leave stdin alone")

    monkeypatch.setattr(lease_module.sys, "stdin", _NoStdin())
    assert bootstrap_desktop_parent_lease([]) is None
    assert DESKTOP_PARENT_LEASE_KEY_ENV not in os.environ
    assert lease_module.create_desktop_launch_proof(b"ab" * 32) is None


@pytest.mark.parametrize("key", ["", "AB" * 32, "a" * 63])
def test_opted_in_gateway_rejects_invalid_lease_key(monkeypatch, key):
    monkeypatch.setenv(DESKTOP_PARENT_LEASE_KEY_ENV, key)
    with pytest.raises(SystemExit, match="Invalid Desktop Gateway parent lease key"):
        bootstrap_desktop_parent_lease([DESKTOP_PARENT_LEASE_ARG])
    assert DESKTOP_PARENT_LEASE_KEY_ENV not in os.environ


def test_opted_in_gateway_proof_uses_launch_key_even_while_draining(monkeypatch):
    launch_key = bytes.fromhex("a1" * 32)
    instance_key = bytes.fromhex("b2" * 32)
    challenge = "c3" * 32
    read_fd, write_fd = os.pipe()
    monkeypatch.setenv(DESKTOP_PARENT_LEASE_KEY_ENV, launch_key.hex())
    monkeypatch.setattr(lease_module, "_active_lease", None)
    monkeypatch.setattr(instance_auth, "_load_instance_key", lambda: instance_key)
    with os.fdopen(read_fd, "rb") as read_pipe, os.fdopen(write_fd, "wb") as write_pipe:
        monkeypatch.setattr(lease_module.sys, "stdin", read_pipe)
        lease = bootstrap_desktop_parent_lease([DESKTOP_PARENT_LEASE_ARG])
        assert lease is not None
        try:
            assert DESKTOP_PARENT_LEASE_KEY_ENV not in os.environ
            context = b"crew-gateway-desktop-launch-v1\x00"
            expected = hmac.digest(launch_key, context + challenge.encode("ascii"), "sha256").hex()
            previous = hmac.digest(
                instance_key,
                b"crew-gateway-instance-v1\x00" + challenge.encode("ascii"),
                "sha256",
            ).hex()
            payload, status = instance_auth.build_gateway_health_payload(challenge)
            assert status == 200
            assert payload["desktop_launch_proof"] == expected
            assert payload["instance_proof"] == previous
            assert "desktop_launch_proof" not in instance_auth.build_gateway_health_payload(None)[0]
            assert instance_auth.build_gateway_health_payload("invalid")[1] == 400
            write_pipe.close()
            assert lease._closed.wait(2)
            payload, status = instance_auth.build_gateway_health_payload(challenge)
            assert status == 200 and payload["instance_proof"] == previous
            assert payload["desktop_launch_proof"] == expected
        finally:
            lease.finish()


def test_preclosed_lease_skips_gateway_boot(monkeypatch):
    def fail_build():
        pytest.fail("a preclosed Desktop lease must stop before building Gateway")

    monkeypatch.setattr(gateway_app, "build_app", fail_build)
    gateway_app.run(parent_lease=SimpleNamespace(closed=True))


def test_gateway_closes_resources_if_parent_exits_during_build(monkeypatch):
    lease = SimpleNamespace(closed=False)
    closed = []

    async def shutdown():
        closed.append(True)

    def build():
        lease.closed = True
        return SimpleNamespace(shutdown=shutdown)

    def fail_serve(*_args, **_kwargs):
        pytest.fail("Gateway must not serve after the parent lease closes during boot")

    monkeypatch.setattr(gateway_app, "build_app", build)
    monkeypatch.setattr(gateway_app, "create_app", fail_serve)
    gateway_app.run(parent_lease=lease)
    assert closed == [True]


def test_standalone_gateway_runs_without_managed_lease(monkeypatch):
    config = SimpleNamespace(gateway_host="127.0.0.1", gateway_port=28180)
    gateway = object()
    calls = []
    monkeypatch.setattr(gateway_app, "build_app", lambda: SimpleNamespace(config=config))
    monkeypatch.setattr(gateway_app, "create_app", lambda _crew: gateway)
    monkeypatch.setattr(gateway_app, "_write_gateway_discovery_file", lambda _host, _port: None)
    monkeypatch.setattr(gateway_app.uvicorn, "run", lambda api, **kwargs: calls.append((api, kwargs)))

    gateway_app.run()
    assert calls == [(gateway, {"host": "127.0.0.1", "port": 28180, "log_level": "warning"})]


@pytest.mark.parametrize("drain_timed_out", [False, True])
def test_managed_exit_code_reflects_dispatcher_drain_after_server_returns(monkeypatch, drain_timed_out):
    config = SimpleNamespace(gateway_host="127.0.0.1", gateway_port=28180)
    dispatcher = SimpleNamespace(begin_shutdown=lambda: None, shutdown_timed_out=False)

    def run_server(_server):
        # Simulate the server running the lifespan cleanup before returning.
        dispatcher.shutdown_timed_out = drain_timed_out

    monkeypatch.setattr(gateway_app, "build_app", lambda: SimpleNamespace(config=config, dispatcher=dispatcher))
    monkeypatch.setattr(gateway_app, "create_app", lambda _crew: lambda: None)
    monkeypatch.setattr(gateway_app, "_write_gateway_discovery_file", lambda _host, _port: None)
    monkeypatch.setattr(gateway_app._DesktopLeaseServer, "run", run_server)
    lease = SimpleNamespace(closed=False)
    if drain_timed_out:
        with pytest.raises(SystemExit) as exc_info:
            gateway_app.run(parent_lease=lease)
        assert exc_info.value.code == 1
    else:
        assert gateway_app.run(parent_lease=lease) is None


def test_managed_store_construction_failure_remains_observable(monkeypatch):
    def fail_build():
        raise RuntimeError("private store failure")

    def run_server(server):
        assert server.config.port == 28180
        client = TestClient(server.config.app)
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert "private store failure" not in response.text
        readiness = response.json()["readiness"]
        assert readiness["status"] == "failed" and readiness["error_code"] == "GATEWAY_CORE_BUILD_FAILED"
        assert client.get("/api/health").json()["readiness"] == readiness
        assert client.get("/api/sessions").status_code == 404

    monkeypatch.setattr(gateway_app, "build_app", fail_build)
    monkeypatch.setattr(gateway_app._DesktopLeaseServer, "run", run_server)
    monkeypatch.setenv("GATEWAY_PORT", "28180")

    gateway_app.run(parent_lease=SimpleNamespace(closed=False))
    with pytest.raises(RuntimeError, match="private store failure"):
        gateway_app.run()


async def test_eof_signals_managed_uvicorn_main_loop():
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "rb") as read_pipe, os.fdopen(write_fd, "wb") as write_pipe:
        lease = DesktopParentLease(b"test" * 8, read_pipe.fileno())
        try:
            pre_stop_states = []
            server = gateway_app._DesktopLeaseServer(
                uvicorn.Config(lambda: None), lease, lambda: pre_stop_states.append(server.should_exit),
            )
            assert not await server.on_tick(1)
            write_pipe.close()
            assert lease._closed.wait(2)
            assert await server.on_tick(1)
            assert server.should_exit
            assert await server.on_tick(1)
            assert pre_stop_states == [False]
        finally:
            lease.finish()


def test_emergency_exit_does_not_wait_unboundedly_for_child_cleanup(monkeypatch):
    cleaning = threading.Event()
    allow_cleanup = threading.Event()
    exited = threading.Event()
    exit_codes = []

    def cleanup():
        cleaning.set()
        allow_cleanup.wait(3)

    def hard_exit(code):
        exit_codes.append(code)
        exited.set()

    monkeypatch.setattr(lease_module, "_terminate_registered_children", cleanup)
    monkeypatch.setattr(lease_module.os, "_exit", hard_exit)
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    try:
        lease = DesktopParentLease(b"test" * 8, read_fd, shutdown_grace=0, cleanup_grace=0.01)
        assert lease._closed.wait(2)
        assert cleaning.wait(2)
        assert exited.wait(2)
        assert exit_codes == [1]
    finally:
        allow_cleanup.set()
        os.close(read_fd)


def test_blocked_lease_reader_does_not_prevent_normal_process_exit():
    script = (
        "import sys, time; "
        "from crew.gateway.desktop_parent_lease import DesktopParentLease; "
        "DesktopParentLease(b'test' * 8, sys.stdin.fileno()); time.sleep(0.1)"
    )
    with subprocess.Popen([sys.executable, "-B", "-c", script], stdin=subprocess.PIPE) as process:
        try:
            # Leave stdin open across wait. The daemon is blocked at exit.
            assert process.wait(timeout=5) == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
