"""TurnControl —— 一轮对话的「可控性」句柄：实时引导(steer) + 协作式中断(interrupt)。

用于 ``run_agent.py`` 的 ``steer() / _drain_pending_steer() / interrupt() /
clear_interrupt()``，保留与 Crew asyncio 执行模型匹配的最小控制接口。

Crew 是 asyncio 单线程跑一轮，无需 Crew 的 per-thread 中断注册表：
loop 在「每轮开始」与「每个工具执行前」轮询本对象的状态即可在安全点优雅停止，
既不撕裂流式输出，也不破坏 canonical 历史。

线程安全：steer/interrupt 可能由 gateway（dispatcher.steer/interrupt）在执行线程之外调用，
故用一把 ``threading.Lock`` 保护内部状态；轮询侧（loop）无锁读取布尔标记即可。
"""

from __future__ import annotations

import threading


class TurnControl:
    """一轮执行的控制句柄。由 SingleAgent 每轮创建/重置，executor 轮询消费。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending_steer: str | None = None
        self._interrupted: bool = False
        self._interrupt_message: str | None = None

    # ---------------- 外部（gateway）调用 ---------------- #
    def steer(self, text: str) -> bool:
        """注入一段补充指令，不打断当前工具调用。

        loop 会在下一轮把它追加到最近一条 tool 结果后（模型下一步即可看到）。
        多次 steer 以换行拼接。空串忽略。返回是否被接受。
        """
        if not text or not text.strip():
            return False
        cleaned = text.strip()
        with self._lock:
            self._pending_steer = (
                self._pending_steer + "\n" + cleaned if self._pending_steer else cleaned
            )
        return True

    def interrupt(self, message: str | None = None) -> None:
        """请求在下一个安全点优雅停止当前轮。message 为触发中断的新消息（可选）。

        中断优先级高于 steer：一旦中断，未注入的 steer 作废（那一步不会再发生）。
        """
        with self._lock:
            self._interrupted = True
            self._interrupt_message = message
            self._pending_steer = None

    # ---------------- loop（executor）消费 ---------------- #
    def drain_steer(self) -> str | None:
        """取出并清空待注入的 steer 文本；无则返回 None。"""
        with self._lock:
            text = self._pending_steer
            self._pending_steer = None
        return text

    @property
    def interrupted(self) -> bool:
        with self._lock:
            return self._interrupted

    @property
    def interrupt_message(self) -> str | None:
        with self._lock:
            return self._interrupt_message

    def reset(self) -> None:
        """新一轮开始时清空状态（SingleAgent 复用同一 control 实例时调用）。"""
        with self._lock:
            self._pending_steer = None
            self._interrupted = False
            self._interrupt_message = None
