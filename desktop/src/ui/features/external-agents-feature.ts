/** 外部智能体/Team 产品开关：仅控制入口与日常历史展示，不删除任何会话数据。 */

import type { BackendConfig, SessionAgentBinding } from '../backend-client';
import { state, type SessionRow } from '../state';

export const EXTERNAL_AGENTS_DISABLED_MESSAGE = '外援功能暂未开放，请联系管理员开启。';

export function externalAgentsEnabled(config: BackendConfig | null | undefined = state.config): boolean {
  return config?.external_agents?.enabled === true;
}

export function isExternalAgentSession(
  binding: SessionAgentBinding | null | undefined,
): boolean {
  return binding?.kind === 'external_agent';
}

export function isExternalTeamSession(
  binding: SessionAgentBinding | null | undefined,
): boolean {
  return binding?.kind === 'external_team';
}

export function isExternalAgentOrTeamSession(
  session: Pick<SessionRow, 'agentBinding'> | null | undefined,
): boolean {
  return isExternalAgentSession(session?.agentBinding) || isExternalTeamSession(session?.agentBinding);
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
