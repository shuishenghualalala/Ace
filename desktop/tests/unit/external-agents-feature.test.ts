/**
 * @vitest-environment happy-dom
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { __resetAllStoresForTest, configStore } from '../../src/ui/stores/stores';
import {
  externalAgentsEnabled,
  isExternalAgentOrTeamSession,
  isSessionVisibleWithExternalAgentsFlag,
  syncExternalAgentsFeatureUi,
} from '../../src/ui/features/external-agents-feature';
import type { SessionRow } from '../../src/ui/state';

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
  document.body.innerHTML = '<button data-tab="agents" hidden>智能体</button>';
});

describe('external agents feature flag', () => {
  it('配置加载失败或字段缺失时默认关闭，明确 true 时才开启', () => {
    expect(externalAgentsEnabled()).toBe(false);
    syncExternalAgentsFeatureUi();
    expect(document.querySelector<HTMLElement>('[data-tab="agents"]')?.hidden).toBe(true);

    configStore.set({
      config: {
        model: 'test',
        has_key: true,
        base_url: '',
        active_model_id: 'test',
        models: [],
        external_agents: { enabled: true },
      },
    });
    syncExternalAgentsFeatureUi();

    expect(externalAgentsEnabled()).toBe(true);
    expect(document.querySelector<HTMLElement>('[data-tab="agents"]')?.hidden).toBe(false);
  });

  it('只隐藏外部智能体和外部 Team，不影响 Crew 或 Client 会话', () => {
    configStore.set({
      config: {
        model: 'test',
        has_key: true,
        base_url: '',
        active_model_id: 'test',
        models: [],
        external_agents: { enabled: false },
      },
    });

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
    configStore.set({
      config: {
        model: 'test',
        has_key: true,
        base_url: '',
        active_model_id: 'test',
        models: [],
        external_agents: { enabled: false },
      },
    });
    expect(isSessionVisibleWithExternalAgentsFlag(external)).toBe(false);
    configStore.set({
      config: {
        model: 'test',
        has_key: true,
        base_url: '',
        active_model_id: 'test',
        models: [],
        external_agents: { enabled: true },
      },
    });
    expect(isSessionVisibleWithExternalAgentsFlag(external)).toBe(true);
  });
});
