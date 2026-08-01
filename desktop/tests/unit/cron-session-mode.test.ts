/**
 * cron 新建会话模式重置单测。
 * 验证收到 cron_session_created 时前端会把全局 composer 模式切回智能体默认。
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { resetToAgentMode } from '../../src/ui/features/session-mode';
import { configStore, __resetAllStoresForTest } from '../../src/ui/stores/stores';

beforeEach(() => {
  __resetAllStoresForTest();
});

describe('resetToAgentMode', () => {
  it('resets mode to agent and composerMode to craft', () => {
    configStore.set({ mode: 'dynamic_kanban', composerMode: 'plan' });
    expect(configStore.get().mode).toBe('dynamic_kanban');
    expect(configStore.get().composerMode).toBe('plan');

    resetToAgentMode();

    expect(configStore.get().mode).toBe('agent');
    expect(configStore.get().composerMode).toBe('craft');
  });

  it('keeps agent/craft when called multiple times', () => {
    configStore.set({ mode: 'agent', composerMode: 'craft' });
    resetToAgentMode();
    expect(configStore.get().mode).toBe('agent');
    expect(configStore.get().composerMode).toBe('craft');
  });
});
