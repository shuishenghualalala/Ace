"""Team capability compatibility exports and role mapping."""

from crew.agent.external.capabilities import (
    AGENT_PROFILE_VERSION,
    CAPABILITIES,
    CAPABILITY_ALIASES,
    CAPABILITY_IMPLICATIONS,
    CAPABILITY_LABELS,
    CAPABILITY_SIGNALS,
    capabilities_from_text,
    capability_label,
    implied_capabilities,
    normalize_capabilities,
    normalize_capability,
)

__all__ = [
    "AGENT_PROFILE_VERSION",
    "CAPABILITIES",
    "CAPABILITY_ALIASES",
    "CAPABILITY_IMPLICATIONS",
    "CAPABILITY_LABELS",
    "CAPABILITY_ROLE_KEYS",
    "CAPABILITY_SIGNALS",
    "capabilities_from_text",
    "capability_label",
    "implied_capabilities",
    "normalize_capabilities",
    "normalize_capability",
]

CAPABILITY_ROLE_KEYS = {
    "planning": "project_manager",
    "requirements": "product_manager",
    "information_retrieval": "research_analyst",
    "research": "research_analyst",
    "analysis": "research_analyst",
    "synthesis": "technical_writer",
    "review": "independent_reviewer",
    "design": "ui_designer",
    "frontend": "frontend_developer",
    "backend": "backend_developer",
    "implementation": "fullstack_developer",
    "testing": "qa_engineer",
    "verification": "independent_reviewer",
    "documentation": "technical_writer",
}
