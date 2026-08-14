import type { ExternalAgent } from "../types";

export function externalAgentInitial(agent: Pick<ExternalAgent, "provider" | "display_badge">): string {
  const source = agent.display_badge?.trim() || agent.provider.trim();
  return Array.from(source)[0]?.toLocaleUpperCase() || "?";
}

export function externalAgentTone(provider: string): number {
  const normalized = provider.trim().toLowerCase();
  const knownProviders: Record<string, number> = {
    kimi: 0,
    codex: 1,
    hermes: 2,
    "claude-code": 3,
    claude: 3,
    gemini: 4,
    sites: 5,
  };
  if (normalized in knownProviders) return knownProviders[normalized];
  let hash = 0;
  for (const char of normalized) hash = ((hash << 5) - hash + char.charCodeAt(0)) | 0;
  return Math.abs(hash) % 6;
}

export default function ExternalAgentAvatar({
  agent,
  size = "default",
}: {
  agent: Pick<ExternalAgent, "provider" | "display_badge">;
  size?: "default" | "compact";
}) {
  return (
    <span
      className={`external-agent-avatar external-agent-avatar--${size} agent-provider-tone-${externalAgentTone(agent.provider)}`}
      aria-label={`${agent.provider} 外援 ${externalAgentInitial(agent)}`}
    >
      <span>{externalAgentInitial(agent)}</span>
    </span>
  );
}
