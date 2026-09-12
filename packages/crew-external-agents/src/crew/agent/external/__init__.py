"""Agent-owned external runtime support."""

from crew.agent.external.detector import (
    scan_claude_runtime,
    scan_codex_runtime,
    scan_hermes_runtime,
    scan_kimi_runtime,
    scan_runtimes,
)
from crew.agent.external.catalog import ExternalAgentCatalog
from crew.agent.external.store import ExternalAgentStore
from crew.agent.external.feature import (
    AGENT_RUNTIME_PROVIDER_SERVICE_KEY,
    AdapterRuntimeProvider,
    AgentRuntimeProvider,
    DELEGATION_SERVICE_KEY,
    DelegationService,
    EXTERNAL_AGENT_CATALOG_SERVICE_KEY,
    EXTERNAL_AGENT_FEATURE_ID,
    ExternalAgentFeatureBundle,
    ExternalAgentRun,
    ExternalRunError,
    build_external_agent_feature,
)

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
]
