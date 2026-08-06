/** 外部智能体/Team 产品开关：仅控制入口与日常历史展示，不删除任何会话数据。 */

import type { BackendConfig } from '../backend-client';
import { state, type SessionRow } from '../state';

export const EXTERNAL_AGENTS_DISABLED_MESSAGE = '外援功能暂未开放，请联系管理员开启。';

export function externalAgentsEnabled(config: BackendConfig | null | undefined = state.config): boolean {
  return config?.external_agents?.enabled === true;
}

export function isExternalAgentOrTeamSession(
  session: Pick<SessionRow, 'agentBinding' | 'agentLabel'> | null | undefined,
): boolean {
  const kind = session?.agentBinding?.kind;
  if (kind) return kind === 'external_agent' || kind === 'external_team';
  // 兼容尚未返回 agent_binding 的旧 Gateway。
  const provider = String(session?.agentLabel?.provider || '').trim().toLowerCase();
  if (provider === 'team') return true;
  return Boolean(provider && !['crew', 'builtin', 'client'].includes(provider));
}

export function isSessionVisibleWithExternalAgentsFlag(session: SessionRow): boolean {
  return externalAgentsEnabled() || !isExternalAgentOrTeamSession(session);
}

export function syncExternalAgentsFeatureUi(): void {
  const entry = document.querySelector<HTMLElement>('[data-tab="agents"]');
  if (entry) entry.hidden = !externalAgentsEnabled();
  window.dispatchEvent(new CustomEvent('external-agents:config-change'));
}

export function bindExternalAgentsFeatureUi(onChange: (enabled: boolean) => void): () => void {
  const handler = (): void => onChange(externalAgentsEnabled());
  window.addEventListener('external-agents:config-change', handler);
  handler();
  return () => window.removeEventListener('external-agents:config-change', handler);
}
