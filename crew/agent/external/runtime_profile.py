"""Normalized runtime and model discovery data for external agents."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

RuntimeAvailability = Literal["ready", "degraded", "unavailable"]
ModelBindingStatus = Literal["valid", "missing", "unverified"]


@dataclass(frozen=True)
class RuntimeModelProfile:
    id: str
    label: str
    provider: str = ""
    default: bool = False
    capabilities: tuple[str, ...] = ()
    thinking_levels: tuple[str, ...] = ()
    context_window: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "label": self.label or self.id,
            "provider": self.provider,
            "default": self.default,
            "capabilities": list(self.capabilities),
            "thinking_levels": list(self.thinking_levels),
        }
        if self.context_window is not None:
            payload["context_window"] = self.context_window
        return payload


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


def normalize_runtime_models(raw: Any) -> list[RuntimeModelProfile]:
    """Normalize current structured models and legacy string catalogs."""

    if not isinstance(raw, list):
        return []
    result: list[RuntimeModelProfile] = []
    seen: set[str] = set()
    for entry in raw:
        if isinstance(entry, str):
            model_id = entry.strip()
            payload: dict[str, Any] = {}
        elif isinstance(entry, dict):
            model_id = str(entry.get("id") or entry.get("model_id") or entry.get("modelId") or "").strip()
            payload = entry
        else:
            continue
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        capabilities = payload.get("capabilities") or []
        thinking = payload.get("thinking_levels") or payload.get("thinkingLevels") or []
        raw_context_window = (
            payload.get("context_window")
            if payload.get("context_window") is not None
            else payload.get("contextWindow")
            if payload.get("contextWindow") is not None
            else payload.get("max_context_tokens")
        )
        try:
            context_window = int(raw_context_window) if raw_context_window is not None else None
        except (TypeError, ValueError):
            context_window = None
        if context_window is not None and context_window <= 0:
            context_window = None
        result.append(RuntimeModelProfile(
            id=model_id,
            label=str(payload.get("label") or payload.get("name") or model_id).strip() or model_id,
            provider=str(payload.get("provider") or "").strip(),
            default=bool(payload.get("default")),
            capabilities=tuple(str(item).strip() for item in capabilities if str(item).strip())
            if isinstance(capabilities, list) else (),
            thinking_levels=tuple(str(item).strip() for item in thinking if str(item).strip())
            if isinstance(thinking, list) else (),
            context_window=context_window,
        ))
    return result


def runtime_model(runtime: dict[str, Any] | None, model_id: str) -> RuntimeModelProfile | None:
    metadata = runtime.get("metadata") if isinstance(runtime, dict) else None
    models = normalize_runtime_models(metadata.get("models") if isinstance(metadata, dict) else None)
    wanted = canonical_runtime_model_id(runtime, model_id)
    if not wanted:
        default_id = str(metadata.get("default_model_id") or "").strip() if isinstance(metadata, dict) else ""
        wanted = default_id
    return next((model for model in models if model.id == wanted), None)


def canonical_runtime_model_id(runtime: dict[str, Any] | None, model_id: str) -> str:
    """Resolve an adapter-declared legacy model id without provider branching."""

    wanted = str(model_id or "").strip()
    return runtime_model_migrations(runtime).get(wanted, wanted) if wanted else ""


def runtime_model_migrations(runtime: dict[str, Any] | None) -> dict[str, str]:
    """Return only adapter migrations whose targets exist in the current catalog."""

    metadata = runtime.get("metadata") if isinstance(runtime, dict) else None
    if not isinstance(metadata, dict) or not isinstance(metadata.get("model_migrations"), dict):
        return {}
    known_ids = {model.id for model in normalize_runtime_models(metadata.get("models"))}
    return {
        str(source).strip(): str(target).strip()
        for source, target in metadata["model_migrations"].items()
        if str(source).strip() and str(target).strip() in known_ids
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
