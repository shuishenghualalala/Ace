"""Subagent public API with implementation modules resolved on first use."""

from __future__ import annotations

from importlib import import_module

_EXPORT_GROUPS = {
    ".definition": (
        "SubagentDefinition",
        "parse_definition",
        "to_preset_contribution",
    ),
    ".registry": ("SubagentRegistry",),
    ".providers": (
        "INPROCESS_FORK_PROVIDER",
        "INPROCESS_SPAWN_PROVIDER",
        "InProcessForkProvider",
        "InProcessSpawnProvider",
        "fork_completed_prefix",
    ),
    ".feature": (
        "SUBAGENT_FEATURE_ID",
        "SubagentFeatureBundle",
        "SubagentFeatureHost",
        "build_subagent_feature",
    ),
    ".runtime": (
        "BatchSchedulePolicy",
        "ContinuableActivation",
        "LegacySubagentBackgroundRuntime",
        "RunLogRecorder",
        "SubagentRuntime",
        "fold_result",
        "fold_run_outcome",
        "fold_subagent_result",
        "looks_rate_limited",
    ),
    ".service": (
        "DuplicateProviderError",
        "ProviderRegistry",
        "ResolvedStartRequest",
        "StartRequest",
        "SUBAGENT_SERVICE",
        "SUBAGENT_SERVICE_KEY",
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
        "SubagentService",
        "SubagentValidationError",
        "UnknownProviderError",
        "validate_structured_output",
    ),
    ".policy": (
        "SUBAGENT_BLOCKED_TOOLSETS",
        "SUBAGENT_BLOCKED_TOOLSET_PREFIXES",
        "apply_model_capability_policy",
        "apply_preset_tool_policy",
        "delegation_blocked_toolsets",
        "normalize_policy_names",
        "resolve_subagent_tool_filter",
    ),
    ".context": (
        "ParentDeliveryState",
        "SubagentNotificationQueue",
        "SubagentNotificationRouter",
    ),
    ".store": (
        "SUBAGENT_SCHEMA_FEATURE",
        "SUBAGENT_SCHEMA_VERSION",
        "SubagentStore",
    ),
    ".tools": (
        "SUBAGENT_TOOLSET",
        "ActiveSubagents",
        "build_subagent_tools",
        "build_structured_output_schema",
        "build_collect_subagent_schema",
        "build_delegate_task_schema",
        "build_run_agent_schema",
        "build_interrupt_agent_schema",
        "build_list_agents_schema",
        "build_send_message_schema",
        "register_subagent_tools",
    ),
}
_EXPORTS = {
    name: (module, name)
    for module, names in _EXPORT_GROUPS.items()
    for name in names
}

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
    "ContinuableActivation",
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
    "InProcessForkProvider",
    "INPROCESS_FORK_PROVIDER",
    "INPROCESS_SPAWN_PROVIDER",
    "fork_completed_prefix",
    "validate_structured_output",
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
    "build_structured_output_schema",
    "build_delegate_task_schema",
    "build_run_agent_schema",
    "build_send_message_schema",
    "build_list_agents_schema",
    "build_interrupt_agent_schema",
    "build_collect_subagent_schema",
    "SUBAGENT_BLOCKED_TOOLSETS",
    "SUBAGENT_BLOCKED_TOOLSET_PREFIXES",
    "apply_model_capability_policy",
    "apply_preset_tool_policy",
    "normalize_policy_names",
    "resolve_subagent_tool_filter",
]


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
