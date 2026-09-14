// @vitest-environment happy-dom

/**
 * Team / Dynamic Kanban 看板能力接线测试。
 *
 * 覆盖：
 * - feature_capabilities 可用性判定（null config / 旧后端无字段 / 按 featureId 查 available）；
 * - installTeamKanbanBoards 动态安装与能力翻转的两看板独立启停；
 * - disposer 撤销订阅后能力事件不再重装；
 * - Inspector 默认 Tab 对被下线看板会话的回退。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  bindBoardCapability,
  disposeTeamKanbanBoards,
  installTeamKanbanBoards,
  kanbanBoardEnabled,
  KANBAN_FEATURE_ID,
  syncBoardCapabilityUi,
  teamBoardEnabled,
  TEAM_FEATURE_ID,
} from '../../src/ui/features/board-capability';
import { featureEventRegistry } from '../../src/ui/features/event-reducer-registry';
import { __resetKanbanBoardForTest } from '../../src/ui/features/kanban-board';
import { __resetTeamCollaborationBoardForTest } from '../../src/ui/features/team-collaboration-board';
import { defaultInspectorTabForSession } from '../../src/ui/features/inspector';
import { __resetAllStoresForTest, configStore, sessionStore } from '../../src/ui/stores/stores';
import type { BackendConfig } from '../../src/ui/backend-client';

const dummyCtx = {
  sessionId: 's1',
  messages: [],
  book: {
    assistantId: null,
    firstChunkAt: null,
    activeRequestId: null,
    turnSealed: false,
    acceptingNewRequest: true,
    hadTeamInternal: false,
  },
  now: Date.now(),
  sequence: 0,
};

type CapabilityMap = NonNullable<BackendConfig['feature_capabilities']>;

function setCapabilities(capabilities: CapabilityMap): void {
  const config = { feature_capabilities: capabilities } as Parameters<typeof configStore.set>[0]['config'];
  configStore.set({ config });
}

const bothEnabled: CapabilityMap = {
  [TEAM_FEATURE_ID]: { state: 'active', available: true, generation: 'g1' },
  [KANBAN_FEATURE_ID]: { state: 'active', available: true, generation: 'g1' },
};

const kanbanDisabled: CapabilityMap = {
  [TEAM_FEATURE_ID]: { state: 'active', available: true, generation: 'g1' },
  [KANBAN_FEATURE_ID]: { state: 'discovered', available: false, generation: null },
};

function dispatchKanban(): ReturnType<typeof featureEventRegistry.dispatch> {
  return featureEventRegistry.dispatch('kanban', 'workflow_progress', 1, { workflow_id: 'wf-1', status: 'running' }, dummyCtx);
}

function dispatchTeam(): ReturnType<typeof featureEventRegistry.dispatch> {
  return featureEventRegistry.dispatch('team', 'internal_message', 1, { text: 'hello' }, dummyCtx);
}

beforeEach(() => {
  disposeTeamKanbanBoards();
  __resetKanbanBoardForTest();
  __resetTeamCollaborationBoardForTest();
  __resetAllStoresForTest();
  document.body.innerHTML = '';
  vi.restoreAllMocks();
});

afterEach(() => {
  disposeTeamKanbanBoards();
  __resetKanbanBoardForTest();
  __resetTeamCollaborationBoardForTest();
});

describe('看板能力可用性判定', () => {
  it('config 未加载（null）时两块看板均可用（保持今日行为）', () => {
    configStore.set({ config: null });
    expect(teamBoardEnabled()).toBe(true);
    expect(kanbanBoardEnabled()).toBe(true);
  });

  it('旧后端无 feature_capabilities 字段时两块看板均可用（legacy-safe）', () => {
    configStore.set({ config: {} as Parameters<typeof configStore.set>[0]['config'] });
    expect(teamBoardEnabled()).toBe(true);
    expect(kanbanBoardEnabled()).toBe(true);
    expect(teamBoardEnabled(null)).toBe(true);
    expect(kanbanBoardEnabled(undefined)).toBe(true);
  });

  it('按 featureId 查 available：available=false 或 feature 缺席 map 均不可用', () => {
    setCapabilities(kanbanDisabled);
    expect(teamBoardEnabled()).toBe(true);
    expect(kanbanBoardEnabled()).toBe(false);

    setCapabilities({ [TEAM_FEATURE_ID]: { state: 'active', available: true, generation: 'g1' } });
    expect(teamBoardEnabled()).toBe(true);
    expect(kanbanBoardEnabled()).toBe(false);
  });

  it('bindBoardCapability 立即回调当前值并跟随事件更新，disposer 撤销订阅', () => {
    const seen: Array<[boolean, boolean]> = [];
    const dispose = bindBoardCapability((team, kanban) => { seen.push([team, kanban]); });
    expect(seen).toEqual([[true, true]]);

    setCapabilities(kanbanDisabled);
    syncBoardCapabilityUi();
    expect(seen).toEqual([[true, true], [true, false]]);

    dispose();
    setCapabilities(bothEnabled);
    syncBoardCapabilityUi();
    expect(seen).toHaveLength(2);
  });
});

describe('installTeamKanbanBoards 动态安装', () => {
  it('两能力可用时安装两块看板，reducer 响应 dispatch', () => {
    setCapabilities(bothEnabled);
    installTeamKanbanBoards();

    expect(dispatchKanban()).not.toBeNull();
    expect(dispatchTeam()).not.toBeNull();
  });

  it('能力翻转独立启停：kanban 下线不影响 team，重开恢复，重复事件不重复注册', () => {
    setCapabilities(bothEnabled);
    installTeamKanbanBoards();

    setCapabilities(kanbanDisabled);
    syncBoardCapabilityUi();
    expect(dispatchKanban()).toBeNull();
    expect(dispatchTeam()).not.toBeNull();

    setCapabilities(bothEnabled);
    syncBoardCapabilityUi();
    expect(dispatchKanban()).not.toBeNull();

    expect(() => {
      window.dispatchEvent(new CustomEvent('team-kanban:config-change'));
      window.dispatchEvent(new CustomEvent('team-kanban:config-change'));
    }).not.toThrow();
    expect(dispatchKanban()).not.toBeNull();
    expect(dispatchTeam()).not.toBeNull();
  });

  it('安装时 team 已下线则初始不安装，翻转补装，team 再下线独立卸载（对称启停）', () => {
    const teamDisabled: CapabilityMap = {
      ...bothEnabled,
      [TEAM_FEATURE_ID]: { state: 'discovered', available: false, generation: null },
    };

    setCapabilities(teamDisabled);
    installTeamKanbanBoards();
    expect(dispatchTeam()).toBeNull();
    expect(dispatchKanban()).not.toBeNull();

    setCapabilities(bothEnabled);
    syncBoardCapabilityUi();
    expect(dispatchTeam()).not.toBeNull();
    expect(dispatchKanban()).not.toBeNull();

    setCapabilities(teamDisabled);
    syncBoardCapabilityUi();
    expect(dispatchTeam()).toBeNull();
    expect(dispatchKanban()).not.toBeNull();
  });
});

describe('安装单元 disposer', () => {
  it('dispose 后两块看板卸载，能力事件不再触发重装', () => {
    setCapabilities(bothEnabled);
    const dispose = installTeamKanbanBoards();
    dispose();

    expect(dispatchKanban()).toBeNull();
    expect(dispatchTeam()).toBeNull();

    setCapabilities(kanbanDisabled);
    syncBoardCapabilityUi();
    expect(dispatchKanban()).toBeNull();
    expect(dispatchTeam()).toBeNull();
  });

  it('disposeTeamKanbanBoards（app 级入口）卸载后可重新安装', () => {
    setCapabilities(bothEnabled);
    installTeamKanbanBoards();
    disposeTeamKanbanBoards();
    expect(dispatchTeam()).toBeNull();

    installTeamKanbanBoards();
    expect(dispatchTeam()).not.toBeNull();
    expect(dispatchKanban()).not.toBeNull();
  });

  it('重复安装幂等：返回同一 disposer，单一 dispose 即完全卸载', () => {
    setCapabilities(bothEnabled);
    const d1 = installTeamKanbanBoards();
    const d2 = installTeamKanbanBoards();
    expect(d1).toBe(d2);

    d1();
    d2();
    expect(dispatchKanban()).toBeNull();
    expect(dispatchTeam()).toBeNull();
  });
});

describe('Inspector 默认 Tab 回退', () => {
  it('team 会话在 team 能力下线时回退 context，可用时为 collaboration', () => {
    sessionStore.set({
      sessions: [{
        id: 'sess-team',
        title: '研发团队',
        workspaceId: 'default',
        updatedAt: 1,
        preview: '',
        badge: '',
        agentLabel: { name: '研发团队', provider: 'team' },
        agentBinding: { kind: 'external_team', id: 'team-a' },
      }],
    });

    // config 未加载 → 默认可用（保持今日行为）
    expect(defaultInspectorTabForSession('sess-team')).toBe('collaboration');

    setCapabilities({ [TEAM_FEATURE_ID]: { state: 'discovered', available: false, generation: null } });
    expect(defaultInspectorTabForSession('sess-team')).toBe('context');
  });

  it('kanban 会话在 kanban 能力下线时回退 context，可用时为 kanban', () => {
    sessionStore.set({ activeSessionId: 'sess-kanban' });
    configStore.set({ mode: 'dynamic_kanban' });

    expect(defaultInspectorTabForSession('sess-kanban')).toBe('kanban');

    setCapabilities({ [KANBAN_FEATURE_ID]: { state: 'discovered', available: false, generation: null } });
    expect(defaultInspectorTabForSession('sess-kanban')).toBe('context');
  });
});
