/**
 * @vitest-environment happy-dom
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { __resetAllStoresForTest, configStore } from '../../src/ui/stores/stores';
import {
  EXTERNAL_AGENTS_FEATURE_ID,
  externalAgentsEnabled,
  isExternalAgentOrTeamSession,
  isSessionVisibleWithExternalAgentsFlag,
  syncExternalAgentsFeatureUi,
} from '../../src/ui/features/external-agents-feature';
import type { BackendConfig } from '../../src/ui/backend-client';
import type { SessionRow } from '../../src/ui/state';

type CapabilityMap = NonNullable<BackendConfig['feature_capabilities']>;

function setCapabilities(capabilities: CapabilityMap): void {
  configStore.set({
    config: {
      model: 'test',
      has_key: true,
      base_url: '',
      active_model_id: 'test',
      models: [],
      feature_capabilities: capabilities,
    },
  });
}

const featureDisabled: CapabilityMap = {
  [EXTERNAL_AGENTS_FEATURE_ID]: { state: 'discovered', available: false, generation: null },
};

const featureEnabled: CapabilityMap = {
  [EXTERNAL_AGENTS_FEATURE_ID]: { state: 'active', available: true, generation: 'g1' },
};

const session = (provider: string): SessionRow => ({
  id: `session-${provider}`,
  title: provider,
  updatedAt: 1,
  preview: '',
  badge: '',
  workspaceId: 'default',
  agentLabel: { name: provider, provider },
});

beforeEach(() => {
  __resetAllStoresForTest();
});

describe('external agents feature flag', () => {
  it('能力可用性来自 feature_capabilities：config 未加载默认可用，下线不可用，事件同步广播', () => {
    const events: CustomEvent[] = [];
    const handler = (event: Event): void => {
      events.push(event as CustomEvent);
    };
    window.addEventListener('external-agents:config-change', handler);

    // config 未加载 → ADR-0041 legacy-safe：视为可用
    expect(externalAgentsEnabled()).toBe(true);
    syncExternalAgentsFeatureUi();
    expect(events).toHaveLength(1);

    setCapabilities(featureDisabled);
    syncExternalAgentsFeatureUi();
    expect(externalAgentsEnabled()).toBe(false);
    expect(events).toHaveLength(2);

    window.removeEventListener('external-agents:config-change', handler);
  });

  it('只隐藏外部智能体和外部 Team，不影响 Crew 或 Client 会话', () => {
    setCapabilities(featureDisabled);

    const externalAgent = session('hermes');
    externalAgent.agentBinding = { kind: 'external_agent', id: 'agent-hermes' };
    const externalTeam = session('team');
    externalTeam.agentBinding = { kind: 'external_team', id: 'team-hermes' };
    expect(isExternalAgentOrTeamSession(externalAgent)).toBe(true);
    expect(isExternalAgentOrTeamSession(externalTeam)).toBe(true);
    expect(isSessionVisibleWithExternalAgentsFlag(externalAgent)).toBe(false);
    expect(isSessionVisibleWithExternalAgentsFlag(externalTeam)).toBe(false);
    expect(isSessionVisibleWithExternalAgentsFlag(session('crew'))).toBe(true);
    expect(isSessionVisibleWithExternalAgentsFlag(session('builtin'))).toBe(true);
    expect(isSessionVisibleWithExternalAgentsFlag(session('client'))).toBe(true);

  });

  it('缺少 agent_binding 时不再通过 Provider 猜测外援身份', () => {
    const providerOnly = session('codex');
    expect(isExternalAgentOrTeamSession(providerOnly)).toBe(false);
    expect(isSessionVisibleWithExternalAgentsFlag(providerOnly)).toBe(true);
  });

  it('重新打开开关后外部历史会话恢复可见', () => {
    const external = session('codex');
    external.agentBinding = { kind: 'external_agent', id: 'agent-codex' };
    setCapabilities(featureDisabled);
    expect(isSessionVisibleWithExternalAgentsFlag(external)).toBe(false);
    setCapabilities(featureEnabled);
    expect(isSessionVisibleWithExternalAgentsFlag(external)).toBe(true);
  });
});
