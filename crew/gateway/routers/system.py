"""系统监控路由：宿主机资源指标 + 进程内日志查询。

- GET /api/system/metrics  CPU/内存/磁盘/网络/运行时长（psutil，进程级 + 系统级）
- GET /api/system/logs     查询环形缓冲日志，支持 level/keyword/limit/offset 筛选

指标只反映「网关所在宿主机」的实时状态；前端系统总览页据此渲染资源条。
"""

from __future__ import annotations

import os
import shutil
import time

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from crew.gateway.auth import AuthenticationError, account_from_request, require_admin
from crew.state.logging import query_logs

# 路由模块导入时刻 ≈ 网关进程启动时刻，用于计算运行时长。
# 放模块级而非 lifespan，是因为 create_app() 在 lifespan yield 之前完成，
# 二者差几十毫秒，对「运行时长」展示无实际影响，且这样对单测更友好。
_START_MONOTONIC = time.monotonic()


def _gb(n: float) -> float:
    """字节转 GB，保留 1 位小数精度（前端展示用）。"""
    return round(n / (1024 ** 3), 1)


def create_system_router(crew) -> APIRouter:
    """Create system monitoring routes.

    - /api/system/metrics: any authenticated account（系统总览页资源占用 / 运行时长
      对所有登录账号开放，不限 admin；未登录由 require_gateway_login 中间件拦截为 401）。
    - /api/system/logs: admin 可见全部，非 admin 仅可见因果归属自己的日志。
    """

    router = APIRouter()

    @router.get("/api/system/metrics")
    async def system_metrics() -> JSONResponse:
        """返回宿主机 + 网关进程的实时资源指标。

        psutil 不可用时降级为 stdlib（仅磁盘 + CPU 核数），不报错，
        让前端能继续展示部分真实数据而非全 mock。

        鉴权：只需登录，不要求 admin。
        """
        uptime_s = round(time.monotonic() - _START_MONOTONIC, 0)
        payload: dict = {"uptime_s": uptime_s, "cpu_count": os.cpu_count() or 1}

        # ---- 磁盘：stdlib 即可 ----
        try:
            du = shutil.disk_usage(os.getcwd())
            payload["disk"] = {
                "total_gb": _gb(du.total),
                "used_gb": _gb(du.used),
                "free_gb": _gb(du.free),
                "percent": round(du.used / du.total * 100, 1) if du.total else 0,
            }
        except OSError:  # 路径不可访问等磁盘探测失败仅降级为缺字段，前端按缺盘处理
            pass

        # ---- CPU/内存/网络：需要 psutil ----
        try:
            import psutil  # 延迟导入，未安装时降级
            vm = psutil.virtual_memory()
            payload["cpu_percent"] = psutil.cpu_percent(interval=None)
            payload["memory"] = {
                "total_gb": _gb(vm.total),
                "used_gb": _gb(vm.used),
                "percent": round(vm.percent, 1),
            }
            # 网络累计 IO（自进程启动）
            net = psutil.net_io_counters()
            payload["network"] = {
                "bytes_sent": net.bytes_sent,
                "bytes_recv": net.bytes_recv,
            }
            # 网关进程自身内存（RSS），便于区分「进程占用」与「系统占用」
            try:
                proc = psutil.Process()
                mem_info = proc.memory_info()
                payload["process"] = {
                    "rss_mb": round(mem_info.rss / (1024 ** 2), 1),
                    "pid": proc.pid,
                }
            except psutil.Error:  # 进程丢失或权限不足仅在 payload 缺 process 字段，前端按缺处理
                pass
        except ImportError:
            # psutil 未安装：前端据 cpu_percent 是否存在决定显示「—」
            payload["psutil_unavailable"] = True

        return JSONResponse(payload)

    @router.get("/api/system/logs")
    async def system_logs(
        request: Request,
        level: str | None = Query(None, description="日志级别过滤，如 DEBUG/INFO/WARNING/ERROR"),
        q: str | None = Query(None, description="对 logger 名 + 消息做子串搜索"),
        limit: int = Query(500, ge=1, le=2000, description="返回条数上限"),
        offset: int = Query(0, ge=0, description="从最新向前跳过的条数（分页）"),
    ) -> JSONResponse:
        """查询进程内环形缓冲日志，最新在前。

        数据源是 RingBufferHandler（见 state/logging.py），容量 2000 条，
        覆盖最近一段时间的所有 crew.* 日志，重启后清空。
        """
        account = account_from_request(request)
        owner_filter: str | None = None
        try:
            require_admin(account, crew.config)
        except AuthenticationError:
            owner_filter = account.owner_account_id

        result = query_logs(
            level=level,
            keyword=q,
            owner_account_id=owner_filter,
            limit=limit,
            offset=offset,
        )
        return JSONResponse(result)

    return router
