"""Normalized runtime and model discovery data for external agents.

Model-id normalization（RuntimeModelProfile 与 canonical_runtime_model_id 一族）
is neutral and lives in :mod:`crew.core.interfaces`; this module keeps the
external runtime record shapes and re-exports the moved names during the
migration（条件与删除时点见 interfaces.py 对应段落）.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from crew.core.interfaces import (
    RuntimeModelProfile,
    canonical_runtime_model_id,
    normalize_runtime_models as normalize_runtime_models,
    runtime_model,
    runtime_model_migrations as runtime_model_migrations,
)

RuntimeAvailability = Literal["ready", "degraded", "unavailable"]
ModelBindingStatus = Literal["valid", "missing", "unverified"]


@dataclass(frozen=True)
class RuntimeCapabilities:
    session_resume: bool = False
    model_switch: bool = False
    mcp_servers: bool = False
    images: bool = False
    tool_events: bool = False
    streaming: bool = False
    usage: bool = False
    approval: bool = False

    def to_dict(self) -> dict[str, bool]:
        return asdict(self)


@dataclass(frozen=True)
class ProbeResult:
    source: str
    checked_at: str
    last_success_at: str = ""
    error_code: str = ""
    message: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class RuntimeProfile:
    id: str
    provider: str
    name: str
    protocol: str
    executable_path: str
    version: str
    launch_args: tuple[str, ...] = ()
    availability_status: RuntimeAvailability = "degraded"
    models: list[RuntimeModelProfile] = field(default_factory=list)
    default_model_id: str = ""
    capabilities: RuntimeCapabilities = field(default_factory=RuntimeCapabilities)
    probe: ProbeResult | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_runtime_dict(self) -> dict[str, Any]:
        metadata = dict(self.metadata)
        metadata.update({
            "runtime_profile_version": 1,
            "launch_args": list(self.launch_args),
            "availability_status": self.availability_status,
            "models": [model.to_dict() for model in self.models],
            "default_model_id": self.default_model_id,
            "runtime_capabilities": self.capabilities.to_dict(),
            "probe": self.probe.to_dict() if self.probe else {},
        })
        return {
            "id": self.id,
            "provider": self.provider,
            "name": self.name,
            "executable_path": self.executable_path,
            "version": self.version,
            "protocol": self.protocol,
            "metadata": metadata,
        }


def model_binding_status(runtime: dict[str, Any] | None, model_id: str) -> ModelBindingStatus:
    metadata = runtime.get("metadata") if isinstance(runtime, dict) else None
    if not isinstance(metadata, dict) or metadata.get("availability_status") != "ready":
        return "unverified"
    return "valid" if runtime_model(runtime, model_id) is not None else "missing"


def runtime_execution_features(
    runtime: dict[str, Any] | None,
    model_id: str,
) -> dict[str, Any]:
    """Return normalized hard execution features for one Runtime model."""

    payload = runtime if isinstance(runtime, dict) else {}
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    runtime_capabilities = (
        metadata.get("runtime_capabilities")
        if isinstance(metadata.get("runtime_capabilities"), dict)
        else {}
    )
    model = runtime_model(payload, model_id)
    model_capabilities = {
        str(item or "").strip().lower()
        for item in (model.capabilities if model is not None else ())
        if str(item or "").strip()
    }
    return {
        "text": model is not None,
        "tools": bool(
            "tools" in model_capabilities
            or "tool_use" in model_capabilities
            or runtime_capabilities.get("tool_events")
        ),
        "images": bool(
            "images" in model_capabilities
            or "vision" in model_capabilities
            or runtime_capabilities.get("images")
        ),
        "context_window": model.context_window if model is not None else None,
    }


def runtime_model_fingerprint(runtime: dict[str, Any] | None, model_id: str) -> str:
    """Fingerprint only model facts that affect profile or execution behavior."""

    payload = runtime if isinstance(runtime, dict) else {}
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    runtime_capabilities = (
        metadata.get("runtime_capabilities")
        if isinstance(metadata.get("runtime_capabilities"), dict)
        else {}
    )
    canonical_id = canonical_runtime_model_id(payload, model_id)
    model = runtime_model(payload, canonical_id)
    semantic = {
        "runtime_id": str(payload.get("id") or ""),
        "model_id": canonical_id,
        "capabilities": sorted(model.capabilities) if model is not None else [],
        "thinking_levels": sorted(model.thinking_levels) if model is not None else [],
        "context_window": model.context_window if model is not None else None,
        "execution_features": runtime_execution_features(payload, canonical_id),
        "runtime_capabilities": {
            key: bool(runtime_capabilities.get(key))
            for key in ("model_switch", "images", "tool_events")
        },
    }
    encoded = json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
