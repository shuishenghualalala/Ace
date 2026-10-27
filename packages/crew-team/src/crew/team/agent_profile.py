"""Team compatibility exports for the shared external-agent profile model."""

from crew.agent.external.profile import (
    AgentProfile,
    AgentCapabilityProfile,
    CapabilityAssessment,
    CapabilityCoverage,
    CapabilityEvidence,
    RUNTIME_DEFAULT_MODEL_ID,
    apply_execution_observations,
    build_agent_capability_profile,
    build_agent_profile,
    build_agent_profile_envelope,
    canonical_profile_model_id,
    evaluate_capability_coverage,
    is_agent_profile_available,
    is_profile_envelope,
    resolve_agent_profile_envelope,
    summarize_execution_observations,
)

__all__ = [
    "AgentProfile",
    "AgentCapabilityProfile",
    "CapabilityAssessment",
    "CapabilityCoverage",
    "CapabilityEvidence",
    "RUNTIME_DEFAULT_MODEL_ID",
    "apply_execution_observations",
    "build_agent_capability_profile",
    "build_agent_profile",
    "build_agent_profile_envelope",
    "canonical_profile_model_id",
    "evaluate_capability_coverage",
    "is_agent_profile_available",
    "is_profile_envelope",
    "resolve_agent_profile_envelope",
    "summarize_execution_observations",
]
