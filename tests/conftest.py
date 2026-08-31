"""Shared pytest fixtures for gateway tests."""

from __future__ import annotations

import os

import pytest

@pytest.fixture
def auth_headers(monkeypatch) -> dict[str, str]:
    """Use the historical test owner without sending identity headers."""

    monkeypatch.setattr("crew.gateway.auth.LOCAL_OWNER_ACCOUNT_ID", "A:uid-a")
    return {}


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
