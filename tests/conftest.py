"""Shared pytest fixtures for gateway tests."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest


class _TestCredentialBackend:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        self.values[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        self.values.pop((service, account), None)


@pytest.fixture(autouse=True)
def _isolated_platform_secret_backend(monkeypatch) -> _TestCredentialBackend:
    """Never let tests write developer credentials to the host OS keyring."""
    from crew.security.secret_store import PlatformSecretStore

    backend = _TestCredentialBackend()
    monkeypatch.setattr(
        PlatformSecretStore,
        "_load_platform_backend",
        staticmethod(lambda: backend),
    )
    return backend


@pytest.fixture(autouse=True)
def _gateway_transport_boundary_test_defaults(monkeypatch) -> None:
    """Keep domain tests focused; boundary tests replace these stubs explicitly."""

    from crew.gateway import app as gateway_app
    from crew.gateway import auth as gateway_auth

    monkeypatch.setattr(
        gateway_auth,
        "verify_desktop_security_proof",
        lambda _proof, **_kwargs: True,
    )
    monkeypatch.setattr(
        gateway_auth,
        "require_trusted_request_origin",
        lambda _origin, _config=None: None,
    )
    monkeypatch.setattr(
        gateway_app,
        "require_trusted_request_origin",
        lambda _origin, _config=None: None,
    )


@pytest.fixture
def auth_headers(monkeypatch) -> dict[str, str]:
    """Use the historical test owner without sending identity headers."""

    monkeypatch.setattr("crew.gateway.auth.LOCAL_OWNER_ACCOUNT_ID", "A:uid-a")
    return {}


@pytest.fixture
def send_ws_json() -> Callable[[Any, dict[str, Any]], None]:
    """Send one valid, strictly sequenced Gateway WebSocket test frame."""

    sequences: dict[Any, int] = {}
    nonce_counter = 0

    def send(socket: Any, payload: dict[str, Any]) -> None:
        nonlocal nonce_counter
        sequence = sequences.get(socket, 0) + 1
        sequences[socket] = sequence
        nonce_counter += 1
        socket.send_json(
            {
                **payload,
                "protocol_version": 1,
                "client_sequence": sequence,
                "nonce": f"test-nonce-{nonce_counter:016d}",
            }
        )

    return send


@pytest.fixture(autouse=True)
def _packaged_runtime_stub_for_dev_checkouts(monkeypatch, tmp_path_factory):
    """开发检出缺少编译好的原生安全运行时二进制时，为网关全链路用例兜底。

    所有对话模式都会编译出 MANAGED profile，``validate_process_launch`` 对
    helper 做 fail-closed 校验——没有二进制的开发检出里每个网关回合都会
    NativeRuntimeError（dev 分支的 WS 全链路用例因此挂死）。这里提供一个
    通过真实完整性校验的本地 helper（manifest 模式与
    tests/security/test_sandboxable_preference._managed_helper 相同）：

    - 桌面打包/预编译目录里已有真实二进制时不干预；
    - ``validate_process_launch`` / ``verify_helper_integrity`` 全程真实执行；
    - 专门的安全边界用例自带 ``helper_argv``，不受影响。

    stub 放在 basetemp 下的共享目录而不是用例自己的 tmp_path：不少用例会
    断言 tmp_path 为空/精确清单，塞进去会污染断言。
    """
    from crew.security import launch as launch_mod

    try:
        argv = launch_mod.packaged_runtime_argv()
    except Exception:  # noqa: BLE001 - 解析失败同样视为不可用
        argv = ()
    if argv and Path(argv[0]).is_file():
        return
    # 标记 stub 已生效：探测真实运行时的用例（如 CLI sandbox-run）据此跳过。
    monkeypatch.setenv("ACE_TEST_PACKAGED_RUNTIME_STUB", "1")
    name = "ace-security-runtime.exe" if os.name == "nt" else "ace-security-runtime"
    # basetemp 下的固定目录：安全用例会在 tmp_path 根目录放自己的
    # helper/manifest，共用目录会让两份 manifest 互相覆盖（binary_name 对不上）。
    helper = tmp_path_factory.getbasetemp() / "_packaged_runtime_stub" / name
    helper.parent.mkdir(parents=True, exist_ok=True)
    helper.write_bytes(b"test-runtime")
    manifest = helper.with_name("runtime-manifest.json")
    if not manifest.is_file():  # 幂等：manifest 与二进制一经写入不再重写
        manifest.write_text(
            json.dumps(
                {
                    "schema": 2,
                    "binary_name": helper.name,
                    "binary_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
                }
            ),
            encoding="utf-8",
        )
    monkeypatch.setattr(launch_mod, "packaged_runtime_argv", lambda: (str(helper),))


@pytest.fixture(autouse=True)
def _restore_process_env():
    """每个用例后还原进程环境变量。

    load_config() 会把 config.yaml 的 runtime.crew_home 写回 os.environ
    ["CREW_HOME"]（生产桥接行为，见 crew/state/config.py）。未隔离的用例一旦
    触发 load_config，本机真实 home/模型配置会泄漏给后续所有用例——后续
    build_app 会读到真实模型并发起真实 HTTP 调用（慢、烧钱、顺序依赖失败）。
    """
    before = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(before)


@pytest.fixture(autouse=True)
def _close_live_crew_apps():
    """兜底释放本用例构建、但未走 ASGI lifespan 的 CrewApp 句柄。

    直接 ASGITransport 调 app 的用例不会触发 lifespan，crew.shutdown() 不会
    执行；每个 CrewApp 在构造期就打开多个 SQLite/WAL 连接，不关闭会跨用例
    累积 fd 直至耗尽（Errno 24）。没跑过 lifespan 的 App 尚未启动任何后台
    任务，因此同步关闭持有连接的 Store 即可，无需完整 async shutdown（后者
    会给每个用例增加秒级开销）。正常 shutdown 过的实例已自行从注册表移除。
    """
    yield

    from crew.app import _LIVE_APPS
    from crew.gateway.hooks import hook_registry
    from crew.tools.process_registry import process_registry

    while _LIVE_APPS:
        app = _LIVE_APPS.pop()
        try:
            app._close_persistent_stores()  # noqa: SLF001
        except Exception:  # noqa: BLE001 - 尽力释放，不影响后续 App
            pass
        app._shutdown_complete = True  # noqa: SLF001 - 标记已释放，避免重复清理
    # build_app 会把 app.tasks 挂到全局 process_registry（configure_task_runtime）。
    # stores 关闭后该引用成为指向已关闭数据库的死 runtime——后续用例的
    # handle_terminal 一拿到它就炸（Cannot operate on a closed database）。
    process_registry.configure_task_runtime(None)
    # 全局 hook 注册表兜底清空：create_app/build_app 会向 hook_registry 注册
    # 带实例闭包的 handler，未走 lifespan/shutdown 的用例会泄漏注册并跨用例
    # 累积扇出（每次 emit 调用数百个失效 handler）
    hook_registry.clear()
