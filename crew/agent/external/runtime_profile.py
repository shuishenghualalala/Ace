"""Normalized runtime and model discovery data for external agents."""

from __future__ import annotations

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label or self.id,
            "provider": self.provider,
            "default": self.default,
            "capabilities": list(self.capabilities),
            "thinking_levels": list(self.thinking_levels),
        }


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
        result.append(RuntimeModelProfile(
            id=model_id,
            label=str(payload.get("label") or payload.get("name") or model_id).strip() or model_id,
            provider=str(payload.get("provider") or "").strip(),
            default=bool(payload.get("default")),
            capabilities=tuple(str(item).strip() for item in capabilities if str(item).strip())
            if isinstance(capabilities, list) else (),
            thinking_levels=tuple(str(item).strip() for item in thinking if str(item).strip())
            if isinstance(thinking, list) else (),
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
