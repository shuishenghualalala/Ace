"""可插拔执行内核子模块。

把「具体运行 agent 这一步」从会话编排里抽出来，统一执行契约：
  base     —— AgentExecutor(抽象) + ExecutionContext(执行上下文)
  builtin  —— BuiltinExecutor：Crew 自带手搓循环（默认）
  external —— ClientExecutor / ExternalExecutor：进程内或 Runtime-backed 外部 agent

对外只暴露这里的符号；选择由 create_executor() 工厂按配置完成。
"""

from __future__ import annotations

from typing import Any

from crew.agent.executor.base import AgentExecutor, ExecutionContext, FinalRequestView
from crew.agent.executor.builtin import BuiltinExecutor
from crew.agent.executor.external import (
    AcpExecutor,
    AcpExecutorConfig,
    ClientExecutor,
    ClientExecutorConfig,
    ExternalExecutor,
    ExternalExecutorConfig,
)
from crew.core.errors import ConfigError
from crew.core.interfaces import LLMProvider, ToolRegistry
from crew.plugins.manager import PluginManager

__all__ = [
    "AgentExecutor",
    "ExecutionContext",
    "FinalRequestView",
    "BuiltinExecutor",
    "ClientExecutor",
    "ClientExecutorConfig",
    "ExternalExecutor",
    "ExternalExecutorConfig",
    "AcpExecutor",
    "AcpExecutorConfig",
    "create_executor",
]


def create_executor(
    kind: str,
    *,
    provider: LLMProvider,
    registry: ToolRegistry,
    plugins: PluginManager,
    config: Any = None,
    max_iterations: int = 20,
    max_retries: int = 2,
    backoff_seconds: float = 1.0,
    guardrail_config: Any = None,
    parallel_tools: bool = True,
    fallback_providers: list | None = None,
    compactor: Any = None,
    empty_retry_max: int = 2,
    continuation_max: int = 2,
    max_parallel_tool_calls: int = 8,
    max_delegate_tool_calls: int = 3,
    plan_manager: Any = None,
    stream_continuation_max: int = 3,
) -> AgentExecutor:
    """按 kind 创建执行内核。

    builtin -> BuiltinExecutor；client -> 进程内 ClientExecutor；
    external/acp/cli -> 协议中性 ExternalExecutor。
    未知 kind 抛 ConfigError。
    """
    kind = (kind or "builtin").strip().lower()
    if kind == "builtin":
        return BuiltinExecutor(
            provider, registry, plugins,
            max_iterations=max_iterations,
            max_retries=max_retries,
            backoff_seconds=backoff_seconds,
            guardrail_config=guardrail_config,
            parallel_tools=parallel_tools,
            fallback_providers=fallback_providers,
            compactor=compactor,
            empty_retry_max=empty_retry_max,
            continuation_max=continuation_max,
            max_parallel_tool_calls=max_parallel_tool_calls,
            max_delegate_tool_calls=max_delegate_tool_calls,
            plan_manager=plan_manager,
            stream_continuation_max=stream_continuation_max,
        )
    if kind == "client":
        return ClientExecutor(config)
    if kind in {"external", "acp", "cli"}:
        executor = ExternalExecutor(config)
        executor.config.plan_manager = plan_manager
        return executor
    raise ConfigError(
        f"未知的 agent executor 类型: {kind}"
        "（可选 builtin | client | external；acp/cli 仅用于旧配置兼容）"
    )
