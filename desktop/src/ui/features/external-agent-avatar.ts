/** Shared external-agent pixel avatar markup for Desktop surfaces. */

import { escapeHtml } from '../state';

export function externalAgentInitial(provider: string, displayBadge?: string): string {
  const source = displayBadge?.trim() || provider.trim();
  return Array.from(source)[0]?.toLocaleUpperCase() || '?';
}

export function externalAgentTone(provider: string): number {
  const normalized = provider.trim().toLowerCase();
  const knownProviders: Record<string, number> = {
    kimi: 0,
    codex: 1,
    hermes: 2,
    'claude-code': 3,
    claude: 3,
    gemini: 4,
    sites: 5,
  };
  if (normalized in knownProviders) return knownProviders[normalized];
  let hash = 0;
  for (const char of normalized) hash = ((hash << 5) - hash + char.charCodeAt(0)) | 0;
  return Math.abs(hash) % 6;
}

export function externalAgentAvatarMarkup(
  provider: string,
  displayBadge?: string,
  className = 'composer-agent-avatar',
): string {
  const classes = `${className} agent-provider-tone-${externalAgentTone(provider)}`;
  return `<span class="${escapeHtml(classes)}" aria-hidden="true"><span>${escapeHtml(externalAgentInitial(provider, displayBadge))}</span></span>`;
}
