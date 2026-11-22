"""Agent-owned external runtime support.

Exports are resolved on first use so importing one adapter-facing module does
not eagerly scan every runtime or load the full adapter stack during startup.
"""

from __future__ import annotations

from importlib import import_module

__all__ = [
    "ExternalAgentStore",
    "ExternalAgentCatalog",
    "scan_claude_runtime",
    "scan_codex_runtime",
    "scan_hermes_runtime",
    "scan_kimi_runtime",
    "scan_runtimes",
    "AGENT_RUNTIME_PROVIDER_SERVICE_KEY",
    "AdapterRuntimeProvider",
    "AgentRuntimeProvider",
    "DELEGATION_SERVICE_KEY",
    "DelegationService",
    "EXTERNAL_AGENT_CATALOG_SERVICE_KEY",
    "EXTERNAL_AGENT_FEATURE_ID",
    "ExternalAgentFeatureBundle",
    "ExternalAgentRun",
    "ExternalRunError",
    "build_external_agent_feature",
    "ExternalDelegationProvider",
    "ExternalSubagentProvider",
]

_EXPORTS: dict[str, tuple[str, str]] = {
    "ExternalAgentStore": (".store", "ExternalAgentStore"),
    "ExternalAgentCatalog": (".catalog", "ExternalAgentCatalog"),
    "scan_claude_runtime": (".detector", "scan_claude_runtime"),
    "scan_codex_runtime": (".detector", "scan_codex_runtime"),
    "scan_hermes_runtime": (".detector", "scan_hermes_runtime"),
    "scan_kimi_runtime": (".detector", "scan_kimi_runtime"),
    "scan_runtimes": (".detector", "scan_runtimes"),
    "AGENT_RUNTIME_PROVIDER_SERVICE_KEY": (".feature", "AGENT_RUNTIME_PROVIDER_SERVICE_KEY"),
    "AdapterRuntimeProvider": (".feature", "AdapterRuntimeProvider"),
    "AgentRuntimeProvider": (".feature", "AgentRuntimeProvider"),
    "DELEGATION_SERVICE_KEY": (".feature", "DELEGATION_SERVICE_KEY"),
    "DelegationService": (".feature", "DelegationService"),
    "EXTERNAL_AGENT_CATALOG_SERVICE_KEY": (".feature", "EXTERNAL_AGENT_CATALOG_SERVICE_KEY"),
    "EXTERNAL_AGENT_FEATURE_ID": (".feature", "EXTERNAL_AGENT_FEATURE_ID"),
    "ExternalAgentFeatureBundle": (".feature", "ExternalAgentFeatureBundle"),
    "ExternalAgentRun": (".feature", "ExternalAgentRun"),
    "ExternalRunError": (".feature", "ExternalRunError"),
    "build_external_agent_feature": (".feature", "build_external_agent_feature"),
    "ExternalDelegationProvider": (".subagent_provider", "ExternalDelegationProvider"),
    "ExternalSubagentProvider": (".subagent_provider", "ExternalSubagentProvider"),
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value
