"""Model-aware AgentProfile construction and ProfileEnvelope persistence helpers.

The public/domain object remains :class:`AgentProfile`.  ``ProfileEnvelope``
is an internal, rebuildable materialized cache stored in
``external_agent.profile_json``.  Consumers must resolve one explicit model
and must not depend on the envelope layout.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping

from crew.agent.external.runtime_profile import (
    canonical_runtime_model_id,
    model_binding_status,
    runtime_execution_features,
    runtime_model,
    runtime_model_fingerprint,
)
from crew.team.capabilities import (
    AGENT_PROFILE_VERSION,
    CAPABILITIES,
    CAPABILITY_IMPLICATIONS,
    CAPABILITY_SIGNALS,
    normalize_capabilities,
    normalize_capability,
)
from crew.team.roles import CREW_BUILTIN_AGENT_ID

RUNTIME_DEFAULT_MODEL_ID = "__runtime_default__"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _clamp_score(value: Any, default: float) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(score, 1.0))


def _stable_fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CapabilityEvidence:
    source: str
    value: str
    weight: float


@dataclass(frozen=True)
class CapabilityAssessment:
    score: float
    confidence: float
    evidence: list[CapabilityEvidence] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 4),
            "confidence": round(self.confidence, 4),
            "evidence": [asdict(item) for item in self.evidence],
        }


@dataclass
class AgentProfile:
    agent_id: str
    capabilities: dict[str, CapabilityAssessment]
    availability: str = "ready"
    runtime: str = "unknown"
    model: dict[str, Any] = field(default_factory=dict)
    version: int = AGENT_PROFILE_VERSION

    def score(self, capability: str) -> float:
        assessment = self.capabilities.get(capability)
        return assessment.score if assessment is not None else 0.0

    def confidence(self, capability: str) -> float:
        assessment = self.capabilities.get(capability)
        return assessment.confidence if assessment is not None else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "agent_id": self.agent_id,
            "availability": self.availability,
            "runtime": self.runtime,
            "model": dict(self.model),
            "capabilities": {
                capability: assessment.to_dict()
                for capability, assessment in self.capabilities.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentProfile | None:
        if not isinstance(data, dict) or int(data.get("version") or 1) < AGENT_PROFILE_VERSION:
            return None
        raw_capabilities = data.get("capabilities")
        if not isinstance(raw_capabilities, dict):
            return None
        capabilities = _assessments_from_dict(raw_capabilities)
        if not capabilities:
            return None
        return cls(
            version=AGENT_PROFILE_VERSION,
            agent_id=str(data.get("agent_id") or ""),
            availability=str(data.get("availability") or "ready"),
            runtime=str(data.get("runtime") or "unknown"),
            model=dict(data.get("model") or {}) if isinstance(data.get("model"), dict) else {},
            capabilities=capabilities,
        )


@dataclass(frozen=True)
class CapabilityCoverage:
    """One deterministic capability check for a team or workflow node.

    ``AgentProfile`` remains the capability source for Formation and Runtime.
    The planner may pass the persisted member capability assignment through
    ``capability_sets`` because planning does not own the external-agent
    store.  Both paths produce this same result model and use the same
    normalized capability keys and threshold.
    """

    required: list[str]
    covered: list[str]
    missing: list[str]
    unavailable: list[str]
    unknown: list[str]
    covered_by: dict[str, list[str]] = field(default_factory=dict)
    assigned_agent_ids: list[str] = field(default_factory=list)
    min_score: float = 0.5

    @property
    def status(self) -> str:
        if not self.missing and not self.unavailable and not self.unknown:
            return "covered"
        if self.unknown and not self.missing and not self.unavailable:
            return "unknown"
        if self.unavailable and not self.missing and not self.unknown:
            return "unavailable"
        if self.covered:
            return "partial"
        return "missing"

    @property
    def ratio(self) -> float:
        return round(len(self.covered) / len(self.required), 4) if self.required else 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "required": list(self.required),
            "covered": list(self.covered),
            "missing": list(self.missing),
            "unavailable": list(self.unavailable),
            "unknown": list(self.unknown),
            "covered_by": {key: list(value) for key, value in self.covered_by.items()},
            "assigned_agent_ids": list(self.assigned_agent_ids),
            "min_score": self.min_score,
            "status": self.status,
            "ratio": self.ratio,
        }


def is_agent_profile_available(profile: AgentProfile) -> bool:
    """Return whether a resolved profile can be used for execution/selection."""

    return (
        profile.availability == "ready"
        and str(profile.model.get("binding_status") or "") != "missing"
    )


def evaluate_capability_coverage(
    required_capabilities: Iterable[object],
    profiles: Mapping[str, AgentProfile] | Iterable[AgentProfile] | None = None,
    *,
    capability_sets: Mapping[str, Iterable[object]] | None = None,
    assigned_agent_ids: Iterable[object] | None = None,
    min_score: float = 0.5,
    require_profile_availability: bool = False,
) -> CapabilityCoverage:
    """Evaluate one capability requirement against the supplied members.

    Formation and Runtime pass resolved ``AgentProfile`` objects.  The DAG
    compiler passes the confirmed ``TeamMemberSpec.capabilities`` snapshot via
    ``capability_sets``; it is an assignment contract, not a second profile.
    Runtime can set ``require_profile_availability`` when that contract is
    paired with a live external profile: the contract still supplies the
    capability, while the profile gates current Runtime/model readiness. The
    function deliberately does not inspect candidates outside the supplied
    members and never performs I/O or LLM calls.
    """

    required = normalize_capabilities(required_capabilities)
    threshold = max(0.0, min(float(min_score), 1.0))
    profile_map: dict[str, AgentProfile] = {}
    if isinstance(profiles, Mapping):
        profile_map = {
            str(agent_id).strip(): profile
            for agent_id, profile in profiles.items()
            if str(agent_id).strip() and isinstance(profile, AgentProfile)
        }
    else:
        profile_map = {
            str(profile.agent_id).strip(): profile
            for profile in (profiles or ())
            if isinstance(profile, AgentProfile) and str(profile.agent_id).strip()
        }
    assigned = list(dict.fromkeys(
        str(agent_id).strip()
        for agent_id in (assigned_agent_ids if assigned_agent_ids is not None else profile_map.keys())
        if str(agent_id).strip()
    ))
    if capability_sets and assigned_agent_ids is None:
        for agent_id in capability_sets:
            normalized_id = str(agent_id).strip()
            if normalized_id and normalized_id not in assigned:
                assigned.append(normalized_id)

    covered: list[str] = []
    missing: list[str] = []
    unavailable: list[str] = []
    unknown: list[str] = []
    covered_by: dict[str, list[str]] = {}

    for capability in required:
        capability_members: list[str] = []
        unavailable_members: list[str] = []
        unknown_members: list[str] = []
        for agent_id in assigned:
            profile = profile_map.get(agent_id)
            if require_profile_availability:
                if profile is None:
                    unknown_members.append(agent_id)
                    continue
                if not is_agent_profile_available(profile):
                    unavailable_members.append(agent_id)
                    continue
            if capability_sets is not None and agent_id in capability_sets:
                assigned_caps = normalize_capabilities(capability_sets.get(agent_id) or [])
                if capability in assigned_caps:
                    capability_members.append(agent_id)
                continue
            if profile is None:
                unknown_members.append(agent_id)
                continue
            if not is_agent_profile_available(profile):
                unavailable_members.append(agent_id)
                continue
            if profile.score(capability) >= threshold:
                capability_members.append(agent_id)

        if capability_members:
            covered.append(capability)
            covered_by[capability] = capability_members
        elif unavailable_members:
            unavailable.append(capability)
        elif unknown_members:
            unknown.append(capability)
        else:
            missing.append(capability)

    return CapabilityCoverage(
        required=required,
        covered=covered,
        missing=missing,
        unavailable=unavailable,
        unknown=unknown,
        covered_by=covered_by,
        assigned_agent_ids=assigned,
        min_score=threshold,
    )


# Compatibility name for callers that imported the original transient model.
AgentCapabilityProfile = AgentProfile


def _assessment_from_dict(raw: Any) -> CapabilityAssessment | None:
    if not isinstance(raw, dict):
        return None
    evidence = [
        CapabilityEvidence(
            source=str(item.get("source") or "unknown"),
            value=str(item.get("value") or ""),
            weight=_clamp_score(item.get("weight"), 0.0),
        )
        for item in (raw.get("evidence") or [])
        if isinstance(item, dict)
    ]
    return CapabilityAssessment(
        score=_clamp_score(raw.get("score"), 0.2),
        confidence=_clamp_score(raw.get("confidence"), 0.15),
        evidence=evidence,
    )


def _assessments_from_dict(raw: Any) -> dict[str, CapabilityAssessment]:
    if not isinstance(raw, dict):
        return {}
    assessments: dict[str, CapabilityAssessment] = {}
    for capability in CAPABILITIES:
        parsed = _assessment_from_dict(raw.get(capability))
        if parsed is not None:
            assessments[capability] = parsed
    return assessments


def _assessments_to_dict(assessments: dict[str, CapabilityAssessment]) -> dict[str, Any]:
    return {
        capability: assessment.to_dict()
        for capability, assessment in assessments.items()
    }


def _capability_dict(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, float] = {}
    for raw_key, raw in value.items():
        capability = normalize_capability(raw_key)
        if not capability:
            continue
        try:
            score = float(raw)
        except (TypeError, ValueError):
            continue
        out[capability] = max(out.get(capability, 0.0), max(0.0, min(score, 1.0)))
    return out


def _runtime_evidence_blob(runtime: dict[str, Any] | None) -> tuple[str, str]:
    runtime_payload = runtime if isinstance(runtime, dict) else {}
    metadata = runtime_payload.get("metadata") if isinstance(runtime_payload.get("metadata"), dict) else {}
    declared = metadata.get("skills") or metadata.get("declared_skills") or []
    tools = metadata.get("tools") or metadata.get("tool_manifest") or []
    declared_blob = " ".join(str(item) for item in declared) if isinstance(declared, list) else str(declared or "")
    tool_blob = " ".join(str(item) for item in tools) if isinstance(tools, list) else str(tools or "")
    return declared_blob.lower(), tool_blob.lower()


def canonical_profile_model_id(
    agent: dict[str, Any],
    runtime: dict[str, Any] | None,
    model_id: str | None = None,
) -> str:
    """Resolve one stable overlay key without mutating the Agent default."""

    runtime_payload = runtime if isinstance(runtime, dict) else {}
    metadata = runtime_payload.get("metadata") if isinstance(runtime_payload.get("metadata"), dict) else {}
    requested = str(model_id or agent.get("model") or metadata.get("default_model_id") or "").strip()
    canonical = canonical_runtime_model_id(runtime_payload, requested)
    return canonical or RUNTIME_DEFAULT_MODEL_ID


def _base_source_payload(agent: dict[str, Any], runtime: dict[str, Any] | None) -> dict[str, Any]:
    runtime_skills, runtime_tools = _runtime_evidence_blob(runtime)
    return {
        "agent_id": str(agent.get("id") or ""),
        "name": str(agent.get("name") or ""),
        "description": str(agent.get("description") or ""),
        "system_prompt": str(agent.get("system_prompt") or ""),
        "capabilities": agent.get("capabilities") or agent.get("capability_profile") or {},
        "declared_skills": agent.get("declared_skills") or agent.get("skills") or [],
        "runtime_skills": runtime_skills,
        "runtime_tools": runtime_tools,
    }


def _build_base_assessments(
    agent: dict[str, Any],
    runtime: dict[str, Any] | None,
) -> dict[str, CapabilityAssessment]:
    scores = {capability: 0.2 for capability in CAPABILITIES}
    confidences = {capability: 0.15 for capability in CAPABILITIES}
    evidence: dict[str, list[CapabilityEvidence]] = {
        capability: [CapabilityEvidence("weak_prior", "no structured evidence", 0.15)]
        for capability in CAPABILITIES
    }

    explicit = _capability_dict(agent.get("capabilities") or agent.get("capability_profile"))
    for capability, score in explicit.items():
        scores[capability] = score
        confidences[capability] = max(confidences[capability], 0.9)
        evidence[capability] = [CapabilityEvidence("declared_capability", str(score), 0.9)]

    declared = agent.get("declared_skills") or agent.get("skills")
    if isinstance(declared, list):
        blob = " ".join(str(item) for item in declared).lower()
        for capability, signals in CAPABILITY_SIGNALS.items():
            if any(signal in blob for signal in signals):
                scores[capability] = max(scores[capability], 0.75)
                confidences[capability] = max(confidences[capability], 0.75)
                evidence[capability].append(CapabilityEvidence("declared_skill", blob[:300], 0.75))

    runtime_skills, runtime_tools = _runtime_evidence_blob(runtime)
    for capability, signals in CAPABILITY_SIGNALS.items():
        if runtime_skills and any(signal in runtime_skills for signal in signals):
            scores[capability] = max(scores[capability], 0.7)
            confidences[capability] = max(confidences[capability], 0.65)
            evidence[capability].append(CapabilityEvidence("runtime_skill", runtime_skills[:300], 0.65))
        if runtime_tools and any(signal in runtime_tools for signal in signals):
            scores[capability] = max(scores[capability], 0.55)
            confidences[capability] = max(confidences[capability], 0.5)
            evidence[capability].append(CapabilityEvidence("tool_manifest", runtime_tools[:300], 0.5))

    text_blob = " ".join(
        str(agent.get(key) or "")
        for key in ("name", "system_prompt", "description")
    ).lower()
    for capability, signals in CAPABILITY_SIGNALS.items():
        if any(signal in text_blob for signal in signals):
            scores[capability] = max(scores[capability], 0.65)
            confidences[capability] = max(confidences[capability], 0.4)
            evidence[capability].append(CapabilityEvidence("text_inference", text_blob[:300], 0.4))

    if str(agent.get("id") or "") == CREW_BUILTIN_AGENT_ID:
        scores["planning"] = max(scores["planning"], 0.9)
        confidences["planning"] = max(confidences["planning"], 0.85)
        evidence["planning"].append(CapabilityEvidence("builtin_contract", "crew leader orchestration", 0.85))
        scores["documentation"] = max(scores["documentation"], 0.45)
        scores["testing"] = max(scores["testing"], 0.45)

    return {
        capability: CapabilityAssessment(
            score=scores[capability],
            confidence=confidences[capability],
            evidence=list(dict.fromkeys(evidence[capability])),
        )
        for capability in CAPABILITIES
    }


def _build_model_assessments(
    runtime: dict[str, Any] | None,
    model_id: str,
) -> dict[str, CapabilityAssessment]:
    if model_id == RUNTIME_DEFAULT_MODEL_ID:
        return {}
    selected_model = runtime_model(runtime, model_id)
    capability_blob = " ".join(selected_model.capabilities).lower() if selected_model else ""
    assessments: dict[str, CapabilityAssessment] = {}
    for capability, signals in CAPABILITY_SIGNALS.items():
        if capability_blob and any(signal in capability_blob for signal in signals):
            assessments[capability] = CapabilityAssessment(
                score=0.4,
                confidence=0.35,
                evidence=[CapabilityEvidence("runtime_model_capability", capability_blob[:300], 0.35)],
            )
    return assessments


def summarize_execution_observations(observations: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Aggregate exact-model observations into a compact, rebuildable summary."""

    stats: dict[str, dict[str, Any]] = {
        capability: {
            "positive_weight": 0.0,
            "negative_weight": 0.0,
            "success": 0,
            "revise": 0,
            "failure": 0,
            "neutral": 0,
            "last_observed_at": "",
        }
        for capability in CAPABILITIES
    }
    for observation in observations:
        if not isinstance(observation, dict):
            continue
        capabilities = normalize_capabilities(observation.get("capabilities") or [])
        if not capabilities:
            continue
        outcome = str(observation.get("outcome") or "neutral").strip().lower()
        if outcome not in {"success", "revise", "failure", "neutral"}:
            continue
        weight = _clamp_score(observation.get("quality_weight"), 0.0) / len(capabilities)
        observed_at = str(observation.get("observed_at") or "")
        for capability in capabilities:
            stat = stats[capability]
            stat[outcome] += 1
            stat["last_observed_at"] = max(str(stat["last_observed_at"]), observed_at)
            if outcome == "success":
                stat["positive_weight"] += weight
            elif outcome in {"revise", "failure"}:
                stat["negative_weight"] += weight
    return {
        capability: {
            **stat,
            "positive_weight": round(float(stat["positive_weight"]), 6),
            "negative_weight": round(float(stat["negative_weight"]), 6),
        }
        for capability, stat in stats.items()
        if any(int(stat[key]) for key in ("success", "revise", "failure", "neutral"))
    }


def _merge_assessments(
    base: dict[str, CapabilityAssessment],
    overlay: dict[str, CapabilityAssessment],
) -> dict[str, CapabilityAssessment]:
    merged = dict(base)
    for capability, addition in overlay.items():
        current = merged.get(capability)
        if current is None:
            merged[capability] = addition
            continue
        merged[capability] = CapabilityAssessment(
            score=max(current.score, addition.score),
            confidence=max(current.confidence, addition.confidence),
            evidence=list(dict.fromkeys([*current.evidence, *addition.evidence])),
        )
    return merged


def _apply_capability_implications(
    assessments: dict[str, CapabilityAssessment],
) -> dict[str, CapabilityAssessment]:
    result = dict(assessments)
    for source, implied_items in CAPABILITY_IMPLICATIONS.items():
        source_assessment = result.get(source)
        if source_assessment is None:
            continue
        for implied in implied_items:
            current = result.get(implied) or CapabilityAssessment(0.2, 0.15, [])
            implied_score = source_assessment.score * 0.85
            implied_confidence = source_assessment.confidence * 0.8
            if implied_score <= current.score and implied_confidence <= current.confidence:
                continue
            result[implied] = CapabilityAssessment(
                score=max(current.score, implied_score),
                confidence=max(current.confidence, implied_confidence),
                evidence=[
                    *current.evidence,
                    CapabilityEvidence(
                        "capability_implication",
                        f"{source} implies {implied}",
                        round(implied_confidence, 4),
                    ),
                ],
            )
    return result


def _apply_observation_summary(
    assessments: dict[str, CapabilityAssessment],
    summary: dict[str, Any],
) -> dict[str, CapabilityAssessment]:
    if not isinstance(summary, dict) or not summary:
        return assessments
    result: dict[str, CapabilityAssessment] = {}
    for capability, assessment in assessments.items():
        stat = summary.get(capability)
        if not isinstance(stat, dict):
            result[capability] = assessment
            continue
        sample_count = sum(int(stat.get(key) or 0) for key in ("success", "revise", "failure"))
        total_count = sample_count + int(stat.get("neutral") or 0)
        if total_count == 0:
            result[capability] = assessment
            continue
        positive = float(stat.get("positive_weight") or 0.0)
        negative = float(stat.get("negative_weight") or 0.0)
        effective_weight = positive + negative
        blend = (
            min(0.5, effective_weight / (effective_weight + 6.0))
            if sample_count >= 3 and effective_weight > 0
            else 0.0
        )
        empirical_score = (2.0 + positive) / (4.0 + effective_weight) if effective_weight > 0 else 0.5
        score = assessment.score * (1.0 - blend) + empirical_score * blend
        confidence = min(
            0.95,
            max(assessment.confidence, assessment.confidence + blend * (1.0 - assessment.confidence)),
        )
        evidence = list(assessment.evidence)
        if blend > 0:
            evidence.append(CapabilityEvidence(
                source="execution_observation",
                value=(
                    f"samples={sample_count}; success={int(stat.get('success') or 0)}; "
                    f"revise={int(stat.get('revise') or 0)}; failure={int(stat.get('failure') or 0)}; "
                    f"neutral={int(stat.get('neutral') or 0)}; "
                    f"last={stat.get('last_observed_at') or 'unknown'!s}"
                ),
                weight=round(blend, 4),
            ))
        result[capability] = CapabilityAssessment(
            score=_clamp_score(score, assessment.score),
            confidence=_clamp_score(confidence, assessment.confidence),
            evidence=evidence,
        )
    return result


def is_profile_envelope(value: Any) -> bool:
    return bool(
        isinstance(value, dict)
        and int(value.get("version") or 0) >= AGENT_PROFILE_VERSION
        and isinstance(value.get("base"), dict)
        and isinstance(value.get("model_overlays"), dict)
    )


def build_agent_profile_envelope(
    agent: dict[str, Any],
    *,
    runtime: dict[str, Any] | None,
    observations: list[dict[str, Any]] | None = None,
    existing: dict[str, Any] | None = None,
    model_id: str | None = None,
    refresh_existing_models: bool = True,
) -> dict[str, Any]:
    """Build or refresh the V4 base + model overlay materialized cache."""

    now = _now()
    current = existing if is_profile_envelope(existing) else {}
    base_source_fingerprint = _stable_fingerprint(_base_source_payload(agent, runtime))
    current_base = current.get("base") if isinstance(current.get("base"), dict) else {}
    if (
        current_base.get("source_fingerprint") == base_source_fingerprint
        and _assessments_from_dict(current_base.get("direct_capabilities"))
    ):
        base = dict(current_base)
    else:
        base = {
            "source_fingerprint": base_source_fingerprint,
            "updated_at": now,
            "direct_capabilities": _assessments_to_dict(_build_base_assessments(agent, runtime)),
        }

    overlays = {
        str(key): dict(value)
        for key, value in (current.get("model_overlays") or {}).items()
        if str(key) and isinstance(value, dict)
    }
    selected_model_id = canonical_profile_model_id(agent, runtime, model_id)
    refresh_ids = {selected_model_id}
    if refresh_existing_models:
        refresh_ids.update(overlays)

    raw_observations = observations or []
    for requested_id in sorted(refresh_ids):
        canonical_id = (
            RUNTIME_DEFAULT_MODEL_ID
            if requested_id == RUNTIME_DEFAULT_MODEL_ID
            else canonical_runtime_model_id(runtime, requested_id) or requested_id
        )
        if canonical_id != requested_id and requested_id in overlays and canonical_id not in overlays:
            overlays[canonical_id] = overlays.pop(requested_id)
        relevant_observations = [
            item
            for item in raw_observations
            if isinstance(item, dict) and str(item.get("model_id") or "").strip() == canonical_id
        ]
        summary = summarize_execution_observations(relevant_observations)
        fingerprint = runtime_model_fingerprint(runtime, canonical_id)
        selected_model = None if canonical_id == RUNTIME_DEFAULT_MODEL_ID else runtime_model(runtime, canonical_id)
        public_model_id = "" if canonical_id == RUNTIME_DEFAULT_MODEL_ID else canonical_id
        previous = overlays.get(canonical_id) if isinstance(overlays.get(canonical_id), dict) else {}
        previous_model = previous.get("model") if isinstance(previous.get("model"), dict) else {}
        if selected_model is None and previous_model:
            model_payload = {
                **previous_model,
                "binding_status": model_binding_status(runtime, public_model_id),
                "runtime_id": str((runtime or {}).get("id") or agent.get("runtime_id") or ""),
                "fingerprint": fingerprint,
            }
            model_evidence = (
                dict(previous.get("model_evidence"))
                if isinstance(previous.get("model_evidence"), dict)
                else {}
            )
            execution_features = (
                dict(previous.get("execution_features"))
                if isinstance(previous.get("execution_features"), dict)
                else runtime_execution_features(runtime, public_model_id)
            )
        else:
            execution_features = runtime_execution_features(runtime, public_model_id)
            model_payload = {
                "id": public_model_id,
                "label": selected_model.label if selected_model else public_model_id,
                "binding_status": model_binding_status(runtime, public_model_id),
                "capabilities": list(selected_model.capabilities) if selected_model else [],
                "thinking_levels": list(selected_model.thinking_levels) if selected_model else [],
                "runtime_id": str((runtime or {}).get("id") or agent.get("runtime_id") or ""),
                "fingerprint": fingerprint,
                "execution_features": execution_features,
            }
            model_evidence = _assessments_to_dict(_build_model_assessments(runtime, canonical_id))
        overlay = {
            "runtime_id": model_payload["runtime_id"],
            "model_fingerprint": fingerprint,
            "updated_at": str(previous.get("updated_at") or now),
            "model": model_payload,
            "model_evidence": model_evidence,
            "execution_features": execution_features,
            "observation_summary": summary,
        }
        comparable_previous = {key: value for key, value in previous.items() if key != "updated_at"}
        comparable_overlay = {key: value for key, value in overlay.items() if key != "updated_at"}
        if comparable_previous != comparable_overlay:
            overlay["updated_at"] = now
        overlays[canonical_id] = overlay

    runtime_payload = runtime if isinstance(runtime, dict) else {}
    metadata = runtime_payload.get("metadata") if isinstance(runtime_payload.get("metadata"), dict) else {}
    return {
        "version": AGENT_PROFILE_VERSION,
        "agent_id": str(agent.get("id") or ""),
        "projection": {
            "runtime": str(
                agent.get("runtime")
                or agent.get("runtime_id")
                or agent.get("provider")
                or "unknown"
            ),
            "availability": str(agent.get("availability") or metadata.get("availability_status") or "ready"),
        },
        "base": base,
        "model_overlays": overlays,
    }


def resolve_agent_profile_envelope(
    envelope: dict[str, Any],
    model_id: str,
) -> AgentProfile | None:
    """Resolve one standard AgentProfile from an internal V4 envelope."""

    if not is_profile_envelope(envelope):
        return None
    overlay_key = str(model_id or RUNTIME_DEFAULT_MODEL_ID)
    overlays = envelope.get("model_overlays") if isinstance(envelope.get("model_overlays"), dict) else {}
    overlay = overlays.get(overlay_key)
    if not isinstance(overlay, dict):
        return None
    base = envelope.get("base") if isinstance(envelope.get("base"), dict) else {}
    base_assessments = _assessments_from_dict(base.get("direct_capabilities"))
    if not base_assessments:
        return None
    model_assessments = _assessments_from_dict(overlay.get("model_evidence"))
    assessments = _merge_assessments(base_assessments, model_assessments)
    assessments = _apply_capability_implications(assessments)
    assessments = _apply_observation_summary(assessments, overlay.get("observation_summary") or {})
    projection = envelope.get("projection") if isinstance(envelope.get("projection"), dict) else {}
    return AgentProfile(
        version=AGENT_PROFILE_VERSION,
        agent_id=str(envelope.get("agent_id") or ""),
        availability=str(projection.get("availability") or "ready"),
        runtime=str(projection.get("runtime") or "unknown"),
        model=dict(overlay.get("model") or {}) if isinstance(overlay.get("model"), dict) else {},
        capabilities=assessments,
    )


def build_agent_profile(
    agent: dict[str, Any],
    *,
    runtime: dict[str, Any] | None = None,
    observations: list[dict[str, Any]] | None = None,
    model_id: str | None = None,
) -> AgentProfile:
    """Build one model-aware AgentProfile from declared, model and observed facts."""

    persisted = agent.get("profile") if isinstance(agent.get("profile"), dict) else {}
    if runtime is None and observations is None:
        resolved = AgentProfile.from_dict(persisted)
        if resolved is not None:
            return resolved
        if is_profile_envelope(persisted):
            key = canonical_profile_model_id(agent, None, model_id)
            resolved = resolve_agent_profile_envelope(persisted, key)
            if resolved is not None:
                return resolved

    envelope = build_agent_profile_envelope(
        agent,
        runtime=runtime,
        observations=observations,
        existing=persisted,
        model_id=model_id,
    )
    key = canonical_profile_model_id(agent, runtime, model_id)
    resolved = resolve_agent_profile_envelope(envelope, key)
    if resolved is None:  # pragma: no cover - guarded by the builder invariant
        raise RuntimeError("AgentProfile envelope 解析失败")
    return resolved


def apply_execution_observations(
    profile: AgentProfile,
    observations: list[dict[str, Any]],
) -> AgentProfile:
    """Compatibility helper for callers with an already resolved profile."""

    capabilities = _apply_observation_summary(
        profile.capabilities,
        summarize_execution_observations(observations),
    )
    return AgentProfile(
        version=profile.version,
        agent_id=profile.agent_id,
        availability=profile.availability,
        runtime=profile.runtime,
        model=dict(profile.model),
        capabilities=capabilities,
    )


def build_agent_capability_profile(agent: dict[str, Any]) -> AgentProfile:
    """Compatibility wrapper for the old public helper name."""

    return build_agent_profile(agent)
