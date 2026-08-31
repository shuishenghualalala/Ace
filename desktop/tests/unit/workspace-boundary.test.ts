/**
 * @vitest-environment happy-dom
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { __resetAllStoresForTest, configStore, sessionStore, workspaceStore } from '../../src/ui/stores/stores';
import { setActiveSessionId, setCurrentWorkspaceId } from '../../src/ui/state';
import {
  canSwitchComposerWorkspace,
  assignSessionAgentDisplay,
  commitDraftSession,
  composerWorkspaceId,
  createSessionInWorkspace,
  getSessionAgentDisplay,
  refreshSidebarAfterHydrate,
  setWorkspaceHidden,
  syncSessionsFromBackend,
  workspaceForSessionDispatch,
} from '../../src/ui/features/workspaces';
import { backendApi } from '../../src/ui/backend-client';
import {
  __resetSessionModelBindingsForTest,
  applySessionModelBinding,
  sessionDisplayModelLabel,
} from '../../src/ui/features/session-model';

vi.mock('../../src/ui/backend-client', () => ({
  backendApi: {
    updateWorkspace: vi.fn(async (_id: string, fields: Record<string, unknown>) => ({ id: _id, ...fields })),
    workspaces: vi.fn(async () => []),
  },
}));

vi.mock('../../src/ui/ui-feedback', () => ({
  showConfirmDialog: vi.fn(async () => true),
}));

beforeEach(() => {
  __resetAllStoresForTest();
  __resetSessionModelBindingsForTest();
  vi.clearAllMocks();
});

describe('workspace/session boundary', () => {
  it('draft ACP identity survives first-send commit before backend hydration', () => {
    const id = createSessionInWorkspace('default', vi.fn());
    assignSessionAgentDisplay(
      id,
      { name: 'Kimi', provider: 'kimi', model: 'kimi' },
      'kimi',
      { kind: 'external_agent', id: 'agent-kimi' },
    );

    expect(getSessionAgentDisplay(id)).toEqual({
      agentLabel: { name: 'Kimi', provider: 'kimi', model: 'kimi' },
      agentBinding: { kind: 'external_agent', id: 'agent-kimi' },
      modelLabel: 'kimi',
    });
    applySessionModelBinding(id, {
      model_profile_id: 'deepseek',
      model_label: 'DeepSeek',
    });
    expect(sessionDisplayModelLabel(id)).toBe('kimi');

    commitDraftSession(id, '你好', '你好', vi.fn());
    expect(sessionStore.get().sessions.find((session) => session.id === id)).toMatchObject({
      agentLabel: { name: 'Kimi', provider: 'kimi', model: 'kimi' },
      agentBinding: { kind: 'external_agent', id: 'agent-kimi' },
      modelLabel: 'kimi',
    });
  });

  it('first-send title replaces an early hydrated placeholder session', () => {
    const id = createSessionInWorkspace('default', vi.fn());
    assignSessionAgentDisplay(
      id,
      { name: '小游戏团队', provider: 'team' },
      '',
      { kind: 'external_team', id: 'team-game' },
    );
    sessionStore.set({
      sessions: [{
        id,
        title: '新会话',
        updatedAt: 1,
        preview: '',
        badge: '主智能体',
        workspaceId: 'default',
        agentBinding: { kind: 'external_team', id: 'team-game' },
      }],
    });

    commitDraftSession(id, '实现一个贪吃蛇小游戏', '实现一个贪吃蛇小游戏', vi.fn());

    expect(sessionStore.get().sessions.find((session) => session.id === id)).toMatchObject({
      title: '实现一个贪吃蛇小游戏',
      preview: '实现一个贪吃蛇小游戏',
      agentLabel: { name: '小游戏团队', provider: 'team' },
    });
  });

  it('dispatch uses the existing session workspace instead of the composer workspace', () => {
    sessionStore.set({
      sessions: [
        { id: 'sid-a', title: 'A', updatedAt: 1, preview: '', badge: '工作空间', workspaceId: 'ws-a' },
      ],
      activeSessionId: 'sid-a',
    });
    setCurrentWorkspaceId('ws-b');

    expect(workspaceForSessionDispatch('sid-a')).toBe('ws-a');
  });

  it('dispatch uses the current workspace for a new draft session', () => {
    const id = createSessionInWorkspace('ws-new', vi.fn());

    expect(workspaceForSessionDispatch(id)).toBe('ws-new');
  });

  it('hiding the active session workspace clears the active session', async () => {
    workspaceStore.set({
      workspaces: [
        { id: 'default', name: '默认', description: '', instructions: '' },
        { id: 'ws-a', name: 'A', description: '', instructions: '', hidden: false },
      ],
      currentWorkspaceId: 'ws-a',
    });
    sessionStore.set({
      sessions: [
        { id: 'sid-a', title: 'A', updatedAt: 1, preview: '', badge: '工作空间', workspaceId: 'ws-a' },
      ],
    });
    setActiveSessionId('sid-a');

    await setWorkspaceHidden('ws-a', true, vi.fn());

    expect(sessionStore.get().activeSessionId).toBeNull();
    expect(workspaceStore.get().currentWorkspaceId).toBe('default');
    expect(backendApi.updateWorkspace).toHaveBeenCalledWith('ws-a', { hidden: true });
  });

  it('blue new chat can create a normal conversation even when a project is selected', () => {
    setCurrentWorkspaceId('ws-a');

    const id = createSessionInWorkspace('default', vi.fn());

    expect(sessionStore.get().activeSessionId).toBe(id);
    expect(workspaceForSessionDispatch(id)).toBe('default');
  });

  it('new Crew chat resets an external runtime model to the Crew default', () => {
    configStore.set({
      config: {
        model: 'crew-model',
        has_key: true,
        base_url: '',
        active_model_id: 'crew-default',
        models: [{
          id: 'crew-default',
          name: 'Crew Default',
          model: 'crew-model',
          has_key: true,
          loaded: true,
        }],
      },
    });
    sessionStore.set({
      sessions: [{
        id: 'external-session',
        title: 'Kimi task',
        updatedAt: 1,
        preview: '',
        badge: 'K',
        workspaceId: 'default',
        agentLabel: { name: 'Kimi', provider: 'kimi', model: 'kimi-code/k3' },
        agentBinding: { kind: 'external_agent', id: 'kimi-runtime' },
      }],
      activeSessionId: 'external-session',
    });
    applySessionModelBinding('external-session', {
      source: 'external',
      model_profile_id: 'kimi-code/k3',
      model_label: 'kimi-code/k3',
    });

    const id = createSessionInWorkspace('default', vi.fn());

    expect(sessionStore.get().activeSessionId).toBe(id);
    expect(sessionDisplayModelLabel(id)).toBe('Crew Default');
  });

  it('composer workspace is switchable only for drafts or empty chat', () => {
    workspaceStore.set({ currentWorkspaceId: 'ws-a' });
    sessionStore.set({
      sessions: [
        { id: 'sid-a', title: 'A', updatedAt: 1, preview: '', badge: '工作空间', workspaceId: 'ws-a' },
      ],
      activeSessionId: 'sid-a',
    });

    expect(composerWorkspaceId()).toBe('ws-a');
    expect(canSwitchComposerWorkspace()).toBe(false);

    createSessionInWorkspace('ws-b', vi.fn());

    expect(composerWorkspaceId()).toBe('ws-b');
    expect(canSwitchComposerWorkspace()).toBe(true);
  });

  it('backend refresh does not move an active project draft back to default', () => {
    const id = createSessionInWorkspace('ws-test', vi.fn());

    syncSessionsFromBackend([
      {
        session_id: id,
        title: '新会话',
        message_count: 2,
        updated_at: 10,
        created_at: 1,
        workspace_id: 'default',
      },
    ]);

    expect(sessionStore.get().sessions.find((s) => s.id === id)?.workspaceId).toBe('ws-test');
  });

  it('an active non-draft session missing from the sidebar does not inherit the composer workspace', () => {
    setCurrentWorkspaceId('ws-test');
    setActiveSessionId('sid-missing');

    expect(composerWorkspaceId()).toBe('default');
    expect(workspaceForSessionDispatch('sid-missing')).toBe('default');
  });

  it('refreshSidebarAfterHydrate keeps default on the welcome page even if the latest session was in a project', () => {
    // 复现「初次打开欢迎页直接发消息落到 test」：最近一条会话在 test，
    // hydrate 不应把 currentWorkspaceId 偷偷切到 test，否则与选择器显示的「对话」不一致。
    workspaceStore.set({
      workspaces: [
        { id: 'default', name: '对话', description: '', instructions: '' },
        { id: 'test', name: 'test', description: '', instructions: '' },
      ],
      currentWorkspaceId: 'default',
    });
    sessionStore.set({
      sessions: [
        { id: 's-test', title: '打招呼', updatedAt: 100, preview: '', badge: '工作空间', workspaceId: 'test' },
        { id: 's-def', title: '今天天气如何', updatedAt: 50, preview: '', badge: '主智能体', workspaceId: 'default' },
      ],
      activeSessionId: null,
    });

    refreshSidebarAfterHydrate(vi.fn());

    expect(workspaceStore.get().currentWorkspaceId).toBe('default');
    // test 文件夹仍展开，便于用户看到历史
    expect(workspaceStore.get().expandedWorkspaces['test']).toBe(true);
  });
});
