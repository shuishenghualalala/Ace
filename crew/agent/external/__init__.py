"""Agent-owned external runtime support."""

from crew.agent.external.detector import (
    scan_claude_runtime,
    scan_codex_runtime,
    scan_hermes_runtime,
    scan_kimi_runtime,
    scan_runtimes,
)

__all__ = [
    "ExternalAgentStore",
    "scan_claude_runtime",
    "scan_codex_runtime",
    "scan_hermes_runtime",
    "scan_kimi_runtime",
    "scan_runtimes",
]


def __getattr__(name: str):
    if name == "ExternalAgentStore":
        from crew.agent.external.store import ExternalAgentStore
        return ExternalAgentStore
    raise AttributeError(name)
