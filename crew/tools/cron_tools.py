"""cron 工具兼容层。

实际实现移至 ``crew.cron.tools``。本模块仅保留 re-export 以兼容旧的 import 路径。
"""

from crew.cron.tools import register_cron_tools

__all__ = ["register_cron_tools"]
