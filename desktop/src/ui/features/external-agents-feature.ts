/**
 * 外部智能体/Team 产品开关：仅控制入口与日常历史展示，不删除任何会话数据。
 *
 * 后端 /api/config 返回的 feature_capabilities 决定入口是否可用（ADR-0041 legacy-safe）：
 * - config 未加载（null）或旧后端未返回 feature_capabilities 字段 → 视为可用；
 * - 否则按 EXTERNAL_AGENTS_FEATURE_ID 查 available。
 */

import type { BackendConfig, SessionAgentBinding } from '../backend-client';
import { state, type SessionRow } from '../state';

export const EXTERNAL_AGENTS_DISABLED_MESSAGE = '外援功能暂未开放，请联系管理员开启。';

export const EXTERNAL_AGENTS_FEATURE_ID = 'product.external-agents';

function externalAgentsCapabilityEnabled(config: BackendConfig | null | undefined): boolean {
  if (config == null || config.feature_capabilities == null) return true;
  return Boolean(config.feature_capabilities[EXTERNAL_AGENTS_FEATURE_ID]?.available);
}

export function externalAgentsEnabled(config: BackendConfig | null | undefined = state.config): boolean {
  return externalAgentsCapabilityEnabled(config);
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
  // 入口显隐统一由 shell capability 与 page-registry isAvailable 驱动，
  // 本函数保留触发事件的能力，供旧调用方继续感知配置变化。
  window.dispatchEvent(new CustomEvent('external-agents:config-change'));
}

export function bindExternalAgentsFeatureUi(onChange: (enabled: boolean) => void): () => void {
  const handler = (): void => onChange(externalAgentsEnabled());
  window.addEventListener('external-agents:config-change', handler);
  handler();
  return () => window.removeEventListener('external-agents:config-change', handler);
}
