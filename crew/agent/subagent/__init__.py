"""Subagent 子包：主 agent 调用子 agent 执行任务。

两种形态（与 crew/team 完全独立、互不影响）：
  - delegate_task：自定义临时委派（Crew）
  - run_agent：调用预设 frontmatter agent（Crew 风格）

初期禁止嵌套：纯策略先收窄父授权，AgentFactory 再装配轻量子 Agent。
"""

from crew.agent.subagent.definition import (
    SubagentDefinition,
    parse_definition,
    to_preset_contribution,
)
from crew.agent.subagent.registry import SubagentRegistry
from crew.agent.subagent.providers import INPROCESS_SPAWN_PROVIDER, InProcessSpawnProvider
from crew.agent.subagent.feature import (
    SUBAGENT_FEATURE_ID,
    SubagentFeatureBundle,
    SubagentFeatureHost,
    build_subagent_feature,
)
from crew.agent.subagent.runtime import (
    BatchSchedulePolicy,
    LegacySubagentBackgroundRuntime,
    RunLogRecorder,
    SubagentRuntime,
    fold_result,
    fold_run_outcome,
    fold_subagent_result,
    looks_rate_limited,
)
from crew.agent.subagent.service import (
    DuplicateProviderError,
    ProviderRegistry,
    ResolvedStartRequest,
    StartRequest,
    SUBAGENT_SERVICE,
    SUBAGENT_SERVICE_KEY,
    SubagentCapabilities,
    SubagentCapabilityError,
    SubagentDepthError,
    SubagentDescriptor,
    SubagentError,
    SubagentProvider,
    SubagentProviderError,
    SubagentPublicationError,
    SubagentResult,
    SubagentRun,
    SubagentService,
    SubagentValidationError,
    UnknownProviderError,
)
from crew.agent.subagent.policy import (
    SUBAGENT_BLOCKED_TOOLSETS,
    SUBAGENT_BLOCKED_TOOLSET_PREFIXES,
    apply_model_capability_policy,
    apply_preset_tool_policy,
    delegation_blocked_toolsets,
    normalize_policy_names,
    resolve_subagent_tool_filter,
)
from crew.agent.subagent.context import (
    ParentDeliveryState,
    SubagentNotificationQueue,
    SubagentNotificationRouter,
)
from crew.agent.subagent.store import (
    SUBAGENT_SCHEMA_FEATURE,
    SUBAGENT_SCHEMA_VERSION,
    SubagentStore,
)
from crew.agent.subagent.tools import (
    SUBAGENT_TOOLSET,
    ActiveSubagents,
    build_subagent_tools,
    build_collect_subagent_schema,
    build_delegate_task_schema,
    build_run_agent_schema,
    register_subagent_tools,
)

__all__ = [
    "SUBAGENT_SCHEMA_VERSION",
    "SUBAGENT_SCHEMA_FEATURE",
    "SubagentStore",
    "SubagentNotificationRouter",
    "SubagentNotificationQueue",
    "ParentDeliveryState",
    "delegation_blocked_toolsets",
    "looks_rate_limited",
    "fold_run_outcome",
    "RunLogRecorder",
    "BatchSchedulePolicy",
    "SubagentDefinition",
    "parse_definition",
    "to_preset_contribution",
    "SubagentRegistry",
    "SubagentCapabilities",
    "SubagentCapabilityError",
    "SubagentDepthError",
    "SubagentDescriptor",
    "SubagentError",
    "SubagentProvider",
    "SubagentProviderError",
    "SubagentPublicationError",
    "SubagentResult",
    "SubagentRun",
    "LegacySubagentBackgroundRuntime",
    "SubagentRuntime",
    "SubagentService",
    "SubagentValidationError",
    "StartRequest",
    "ResolvedStartRequest",
    "ProviderRegistry",
    "DuplicateProviderError",
    "UnknownProviderError",
    "SUBAGENT_SERVICE_KEY",
    "SUBAGENT_SERVICE",
    "InProcessSpawnProvider",
    "INPROCESS_SPAWN_PROVIDER",
    "SUBAGENT_FEATURE_ID",
    "SubagentFeatureBundle",
    "SubagentFeatureHost",
    "build_subagent_feature",
    "fold_result",
    "fold_subagent_result",
    "SUBAGENT_TOOLSET",
    "ActiveSubagents",
    "register_subagent_tools",
    "build_subagent_tools",
    "build_delegate_task_schema",
    "build_run_agent_schema",
    "build_collect_subagent_schema",
    "SUBAGENT_BLOCKED_TOOLSETS",
    "SUBAGENT_BLOCKED_TOOLSET_PREFIXES",
    "apply_model_capability_policy",
    "apply_preset_tool_policy",
    "normalize_policy_names",
    "resolve_subagent_tool_filter",
]
