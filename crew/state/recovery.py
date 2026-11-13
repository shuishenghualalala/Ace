"""启动期恢复序列注册表（D5）。

多份进程/会话状态 checkpoint（processes.json 后台进程、cron running fires 等）
的恢复入口收敛到一条序列：各子系统装配时注册恢复 callable，启动流程在统一
入口按注册顺序执行。各 store 的恢复实现原地不动，只改接入方式。

检索类索引（FTS 等）定位为可弃读模型：不注册进恢复序列，损坏即重建。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

StartupRecoveryFn = Callable[[], Any]

_entries: list[tuple[str, StartupRecoveryFn]] = []


def register_startup_recovery(name: str, fn: StartupRecoveryFn) -> None:
    """注册一个启动恢复步骤；同名步骤只注册一次（重复 start 幂等）。"""
    if any(existing == name for existing, _ in _entries):
        return
    _entries.append((name, fn))


def run_startup_recovery() -> dict[str, Any]:
    """按注册顺序执行全部恢复步骤；单步失败只记日志、不阻断后续步骤。"""
    results: dict[str, Any] = {}
    for name, fn in list(_entries):
        try:
            results[name] = fn()
        except Exception:  # noqa: BLE001
            log.exception("启动恢复步骤失败: %s", name)
            results[name] = None
    return results


def clear_startup_recovery() -> None:
    """清空注册表（测试隔离用）。"""
    _entries.clear()
