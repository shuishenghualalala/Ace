"""Built-in descriptors for external-agent runtime discovery.

The catalog describes commands Crew knows how to drive today.  Discovery and
execution stay provider-neutral: a descriptor supplies the command, protocol,
and fixed argv while the detector resolves and probes it generically.

Only locally installed commands are considered.  Crew never downloads packages
or executes package-manager runners during a scan.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeDescriptor:
    """Static declaration of one supported external runtime."""

    provider: str
    name: str
    display_badge: str
    env_var: str
    command: str
    protocol: str
    adapter_id: str = ""
    launch_args: tuple[str, ...] = ()
    probe_env: tuple[tuple[str, str], ...] = ()
    credential_home_paths: tuple[str, ...] = ()
    network_endpoints: tuple[str, ...] = ()
    command_aliases: tuple[str, ...] = ()
    source: str = "builtin"

    @property
    def commands(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.command, *self.command_aliases)))

    @property
    def descriptor_id(self) -> str:
        """Stable runtime kind identity, independent from an installation path."""

        return f"{self.source}:{self.provider}"


# Keep the original four entries first: callers and stored runtime identities
# rely on their provider names and launch contracts.  Additional entries are
# standard ACP runtimes with documented local commands; the generic ACP adapter
# performs the actual handshake, model discovery, and execution.
BUILTIN_RUNTIME_DESCRIPTORS: tuple[RuntimeDescriptor, ...] = (
    RuntimeDescriptor(
        provider="kimi",
        name="Kimi",
        display_badge="K",
        env_var="CREW_KIMI_PATH",
        command="kimi",
        protocol="acp",
        adapter_id="acp-stdio",
        launch_args=("acp",),
        credential_home_paths=(
            ".kimi-code/config.toml",
            ".kimi-code/oauth/kimi-code",
            ".kimi-code/credentials/kimi-code.json",
        ),
        network_endpoints=("https://auth.kimi.com",),
    ),
    RuntimeDescriptor(
        provider="codex",
        name="Codex",
        display_badge="X",
        env_var="CREW_CODEX_PATH",
        command="codex",
        protocol="cli",
        adapter_id="codex-app-server",
        credential_home_paths=(".codex/auth.json", ".codex/config.toml"),
    ),
    RuntimeDescriptor(
        provider="claude",
        name="Claude Agent ACP",
        display_badge="CA",
        env_var="CREW_CLAUDE_ACP_PATH",
        command="claude-agent-acp",
        protocol="acp",
        adapter_id="acp-stdio",
    ),
    RuntimeDescriptor(
        provider="hermes",
        name="Hermes",
        display_badge="H",
        env_var="CREW_HERMES_PATH",
        command="hermes",
        protocol="acp",
        adapter_id="acp-stdio",
        launch_args=("acp",),
        probe_env=(("HERMES_YOLO_MODE", "1"),),
        credential_home_paths=(
            ".hermes/.env",
            ".hermes/config.yaml",
            ".hermes/auth.json",
        ),
    ),
    RuntimeDescriptor(
        provider="kiro",
        name="Kiro CLI",
        display_badge="KI",
        env_var="CREW_KIRO_PATH",
        command="kiro-cli",
        protocol="acp",
        adapter_id="acp-stdio",
        launch_args=("acp",),
    ),
    RuntimeDescriptor(
        provider="qoder",
        name="Qoder CLI",
        display_badge="QD",
        env_var="CREW_QODER_PATH",
        command="qodercli",
        protocol="acp",
        adapter_id="acp-stdio",
        launch_args=("--yolo", "--acp"),
    ),
    RuntimeDescriptor(
        provider="trae",
        name="TRAE CLI",
        display_badge="T",
        env_var="CREW_TRAE_PATH",
        command="traecli",
        protocol="acp",
        adapter_id="acp-stdio",
        launch_args=("acp", "serve"),
    ),
    RuntimeDescriptor(
        provider="grok",
        name="Grok CLI",
        display_badge="GR",
        env_var="CREW_GROK_PATH",
        command="grok",
        protocol="acp",
        adapter_id="acp-stdio",
        launch_args=("agent", "stdio"),
    ),
    RuntimeDescriptor(
        provider="gemini",
        name="Gemini CLI ACP",
        display_badge="GE",
        env_var="CREW_GEMINI_PATH",
        command="gemini",
        protocol="acp",
        adapter_id="acp-stdio",
        launch_args=("--experimental-acp",),
    ),
    RuntimeDescriptor(
        provider="qwen",
        name="Qwen Code ACP",
        display_badge="QW",
        env_var="CREW_QWEN_PATH",
        command="qwen",
        protocol="acp",
        adapter_id="acp-stdio",
        launch_args=("--acp", "--experimental-skills"),
    ),
    RuntimeDescriptor(
        provider="auggie",
        name="Auggie CLI",
        display_badge="A",
        env_var="CREW_AUGGIE_PATH",
        command="auggie",
        protocol="acp",
        adapter_id="acp-stdio",
        launch_args=("--acp",),
    ),
    RuntimeDescriptor(
        provider="kilo",
        name="Kilo Code",
        display_badge="KL",
        env_var="CREW_KILO_PATH",
        command="kilo",
        protocol="acp",
        adapter_id="acp-stdio",
        launch_args=("acp",),
    ),
    RuntimeDescriptor(
        provider="mistral-vibe",
        name="Mistral Vibe ACP",
        display_badge="MV",
        env_var="CREW_MISTRAL_VIBE_PATH",
        command="vibe-acp",
        protocol="acp",
        adapter_id="acp-stdio",
    ),
    RuntimeDescriptor(
        provider="codex-acp",
        name="Codex ACP",
        display_badge="XA",
        env_var="CREW_CODEX_ACP_PATH",
        command="codex-acp",
        protocol="acp",
        adapter_id="acp-stdio",
    ),
    RuntimeDescriptor(
        provider="copilot-acp",
        name="GitHub Copilot ACP",
        display_badge="CP",
        env_var="CREW_COPILOT_ACP_PATH",
        command="copilot-language-server",
        protocol="acp",
        adapter_id="acp-stdio",
        launch_args=("--acp",),
    ),
    RuntimeDescriptor(
        provider="claude-code",
        name="Claude Code",
        display_badge="C",
        env_var="CREW_CLAUDE_PATH",
        command="claude",
        protocol="cli",
        adapter_id="claude-stream-json",
        credential_home_paths=(".claude/.credentials.json", ".claude.json"),
    ),
)


def runtime_descriptors() -> tuple[RuntimeDescriptor, ...]:
    """Return the immutable built-in discovery catalog."""

    return BUILTIN_RUNTIME_DESCRIPTORS


def builtin_descriptor(provider: str) -> RuntimeDescriptor:
    return next(
        descriptor
        for descriptor in BUILTIN_RUNTIME_DESCRIPTORS
        if descriptor.provider == provider
    )


def resolve_runtime_credential_home_paths(
    *,
    provider: str,
    metadata: dict[str, object] | None = None,
) -> tuple[str, ...]:
    """Resolve declared credential paths for persisted and newly detected runtimes.

    Newly detected rows carry the declaration in metadata. Older persisted rows
    may not have that field, so a known built-in descriptor remains the
    compatibility source. Custom runtimes never inherit a built-in path list.
    """

    runtime_metadata = metadata or {}
    if "credential_home_paths" in runtime_metadata:
        raw_paths = runtime_metadata.get("credential_home_paths")
        if isinstance(raw_paths, (list, tuple)):
            return tuple(
                str(item).strip()
                for item in raw_paths
                if isinstance(item, str) and str(item).strip()
            )
        return ()

    descriptor_id = str(runtime_metadata.get("descriptor_id") or "").strip()
    if not descriptor_id:
        descriptor_source = str(
            runtime_metadata.get("runtime_descriptor_source") or ""
        ).strip()
        if descriptor_source:
            descriptor_id = f"{descriptor_source}:{provider}"
    descriptor = next(
        (
            item
            for item in BUILTIN_RUNTIME_DESCRIPTORS
            if descriptor_id and item.descriptor_id == descriptor_id
        ),
        None,
    )
    if descriptor is None and not descriptor_id:
        descriptor = next(
            (item for item in BUILTIN_RUNTIME_DESCRIPTORS if item.provider == provider),
            None,
        )
    return descriptor.credential_home_paths if descriptor is not None else ()


def resolve_runtime_network_endpoints(
    *,
    provider: str,
    metadata: dict[str, object] | None = None,
) -> tuple[str, ...]:
    """Resolve exact host-owned API endpoints for a detected runtime.

    Endpoint declarations are descriptor data, not provider branches or model
    input. Persisted metadata wins so custom runtimes can carry their own
    declaration; older built-in rows fall back to the matching descriptor.
    """

    runtime_metadata = metadata or {}
    if "network_endpoints" in runtime_metadata:
        raw_endpoints = runtime_metadata.get("network_endpoints")
        if isinstance(raw_endpoints, (list, tuple)):
            return tuple(
                str(item).strip()
                for item in raw_endpoints
                if isinstance(item, str) and str(item).strip()
            )
        return ()

    descriptor_id = str(runtime_metadata.get("descriptor_id") or "").strip()
    if not descriptor_id:
        descriptor_source = str(
            runtime_metadata.get("runtime_descriptor_source") or ""
        ).strip()
        if descriptor_source:
            descriptor_id = f"{descriptor_source}:{provider}"
    descriptor = next(
        (
            item
            for item in BUILTIN_RUNTIME_DESCRIPTORS
            if descriptor_id and item.descriptor_id == descriptor_id
        ),
        None,
    )
    if descriptor is None and not descriptor_id:
        descriptor = next(
            (item for item in BUILTIN_RUNTIME_DESCRIPTORS if item.provider == provider),
            None,
        )
    return descriptor.network_endpoints if descriptor is not None else ()


def _normalize_display_badge(value: object) -> str:
    """Normalize a compact text badge without accepting layout-bearing text."""

    compact = "".join(str(value or "").split())
    if not compact:
        return ""
    return "".join(list(compact)[:2]).upper()


def resolve_runtime_display_badge(
    *,
    provider: str,
    metadata: dict[str, object] | None = None,
) -> str:
    """Resolve the authoritative UI badge for detected and persisted runtimes.

    Built-in descriptors stay authoritative for known runtimes. Existing rows
    are reconciled against descriptor identity/provider, so this contract does
    not require a database migration. Metadata is accepted only for future
    constrained custom descriptors; other unknown runtimes get one neutral
    server-generated provider initial. Clients never infer badges.
    """

    runtime_metadata = metadata or {}
    descriptor_id = str(runtime_metadata.get("descriptor_id") or "").strip()
    descriptor_source = str(
        runtime_metadata.get("runtime_descriptor_source") or ""
    ).strip()
    if not descriptor_id and descriptor_source and provider:
        descriptor_id = f"{descriptor_source}:{provider}"

    descriptor = next(
        (
            item
            for item in BUILTIN_RUNTIME_DESCRIPTORS
            if descriptor_id and item.descriptor_id == descriptor_id
        ),
        None,
    )
    if descriptor is None:
        descriptor = next(
            (
                item
                for item in BUILTIN_RUNTIME_DESCRIPTORS
                if item.provider == provider
            ),
            None,
        )
    if descriptor is not None:
        return descriptor.display_badge

    configured = _normalize_display_badge(runtime_metadata.get("display_badge"))
    if configured:
        return configured

    return _normalize_display_badge(provider)[:1] or "?"


def resolve_runtime_adapter_id(
    *,
    provider: str,
    protocol: str,
    metadata: dict[str, object] | None = None,
) -> str:
    """Resolve a persisted runtime to its protocol driver without provider branches.

    Old RuntimeProfile rows may predate ``metadata.adapter_id``.  Built-in
    descriptors are the canonical compatibility fallback; custom ACP runtimes
    retain the protocol-level generic adapter.
    """

    runtime_metadata = metadata or {}
    configured = str(runtime_metadata.get("adapter_id") or "").strip()
    if configured:
        return configured

    descriptor_id = str(runtime_metadata.get("descriptor_id") or "").strip()
    if not descriptor_id:
        descriptor_source = str(
            runtime_metadata.get("runtime_descriptor_source") or ""
        ).strip()
        if descriptor_source:
            descriptor_id = f"{descriptor_source}:{provider}"
    matches = [
        descriptor
        for descriptor in BUILTIN_RUNTIME_DESCRIPTORS
        if descriptor_id and descriptor.descriptor_id == descriptor_id
    ]
    if len(matches) == 1:
        return matches[0].adapter_id or matches[0].protocol
    return "acp-stdio" if protocol == "acp" else ""
