"""独立线程的极简 health 服务：与业务事件循环隔离。

主 ``/api/health`` 与所有 agent 业务共用 uvicorn 的 asyncio 事件循环，业务卡顿时
健康探测随之超时，desktop 会误判后端死亡。本模块在独立线程跑一个 stdlib 极简
HTTP 服务（仅 ``/api/health``），响应语义与主端点对齐（复用同一份 instance proof
校验），并附带 ``loop_lag_ms``：业务循环上的哨兵 task 每秒戳一次 ``loop.time()``，
health 线程读取该时间戳算出滞后，desktop 据此区分「进程活但循环忙」与「进程死」。
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from crew.gateway.instance_auth import (
    GATEWAY_INSTANCE_CHALLENGE_HEADER,
    build_gateway_health_payload,
)
from crew.state.logging import get_logger

log = get_logger("gateway.health_server")

# --- 事件循环活性哨兵（跨线程只读单值快照，锁开销可忽略） ---
_loop_tick_lock = threading.Lock()
_loop_last_tick: float | None = None

_components_lock = threading.Lock()
_components_snapshot: dict[str, Any] | None = None


def note_loop_tick(loop_time: float) -> None:
    """哨兵 task 每秒调用：记录业务事件循环的 ``loop.time()`` 时间戳。"""

    global _loop_last_tick
    with _loop_tick_lock:
        _loop_last_tick = float(loop_time)


def read_loop_lag_ms() -> float | None:
    """业务循环距上次哨兵戳的滞后（毫秒）；哨兵尚未戳过时返回 ``None``。"""

    with _loop_tick_lock:
        tick = _loop_last_tick
    if tick is None:
        return None
    # ``loop.time()`` 基于单调钟，与 ``time.monotonic()`` 同基期，可直接相减。
    return max(0.0, (time.monotonic() - tick) * 1000.0)


def set_health_components(components: dict[str, Any] | None) -> None:
    """更新 components 快照（哨兵 task 在业务循环侧调用，快照 startup/cron 状态）。"""

    global _components_snapshot
    with _components_lock:
        _components_snapshot = components


def read_health_components() -> dict[str, Any] | None:
    with _components_lock:
        return _components_snapshot


def build_health_components(deferred_startup_status: object, cron_service: object) -> dict:
    """主端点 ``_components`` 与线程服务共用的快照逻辑（入参均为无类型依赖）。"""

    startup_status = str(deferred_startup_status or "starting")
    startup: dict[str, Any] = {"status": startup_status}
    if startup_status == "failed":
        startup["message"] = "运行环境组件初始化失败，请查看 Gateway 日志"
    if cron_service is None:
        cron_status: dict[str, Any] = {"status": "disabled"}
    elif bool(getattr(cron_service, "is_running", False)):
        cron_status = {"status": "ready"}
    elif str(getattr(cron_service, "start_error", "") or ""):
        cron_status = {"status": "failed", "message": "定时任务启动失败，请查看 Gateway 日志"}
    else:
        cron_status = {"status": "starting"}
    return {"startup": startup, "cron": cron_status}


class _HealthRequestHandler(BaseHTTPRequestHandler):
    server_version = "CrewGatewayHealth/1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib 约定命名
        if self.path.split("?", 1)[0] != "/api/health":
            self._send_json({"ok": False, "error": "not found"}, 404)
            return
        challenge = self.headers.get(GATEWAY_INSTANCE_CHALLENGE_HEADER)
        payload, status = build_gateway_health_payload(challenge)
        lag = read_loop_lag_ms()
        if lag is not None:
            payload["loop_lag_ms"] = round(lag, 1)
        components = read_health_components()
        if components is not None:
            payload["components"] = components
        self._send_json(payload, status)

    def _send_json(self, payload: dict, status: int) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib 签名
        # 探针每秒一次，禁用 stdlib 默认访问日志刷屏；异常走服务自身的 warning。
        return


class GatewayHealthServer:
    """独立线程 health 服务；端口绑定失败时降级为「仅主端口」，不阻止启动。"""

    def __init__(self, port: int, host: str = "127.0.0.1") -> None:
        self.host = host
        self.port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> int | None:
        """启动线程服务；成功返回实际绑定端口，绑定失败返回 ``None``（降级）。"""

        # 哨兵 task 首戳之前先给出一个基线，保证 health 端口从启动起就带 loop_lag_ms。
        note_loop_tick(time.monotonic())
        try:
            httpd = ThreadingHTTPServer((self.host, self.port), _HealthRequestHandler)
        except OSError:
            log.warning(
                "Gateway health 线程服务绑定 %s:%s 失败，降级为仅主端口 /api/health",
                self.host,
                self.port,
                exc_info=True,
            )
            return None
        httpd.daemon_threads = True
        self._httpd = httpd
        self.port = int(httpd.server_address[1])
        self._thread = threading.Thread(
            target=httpd.serve_forever,
            name="gateway-health",
            daemon=True,
        )
        self._thread.start()
        log.info("Gateway health 线程服务已启动: http://%s:%s/api/health", self.host, self.port)
        return self.port

    def stop(self, timeout: float = 2.0) -> None:
        """停止线程服务；``shutdown`` 会等待在途请求处理完，join 带超时兜底。"""

        httpd = self._httpd
        self._httpd = None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        set_health_components(None)


__all__ = [
    "GatewayHealthServer",
    "build_health_components",
    "note_loop_tick",
    "read_health_components",
    "read_loop_lag_ms",
    "set_health_components",
]
