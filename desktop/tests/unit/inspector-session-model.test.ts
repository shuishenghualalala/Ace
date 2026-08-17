/**
 * @vitest-environment happy-dom
 *
 * Inspector「上下文」页的供应商 / 模型 / 上下文限制必须绑定到**会话级**模型，
 * 而非全局 active_model_id —— 否则用户在 Composer 给某会话单独切模型后，
 * Inspector 仍显示全局默认模型。
 *
 * 覆盖两层数据契约：
 *   1. session-model 数据层：resolveSessionModelId / resolveSessionModelWindow 反映会话绑定，
 *      applySessionModelBinding 派发 session:model-changed 事件。
 *   2. Inspector 渲染层：context tab 文本随会话模型变化，不再回退全局默认。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  __resetAllStoresForTest,
  configStore,
  messageStore,
  sessionStore,
  uiStore,
  workspaceStore,
} from '../../src/ui/stores/stores';
import { setActiveSessionId, setBusy } from '../../src/ui/state';
import {
  __resetSessionModelBindingsForTest,
  applySessionModelBinding,
  composerModelOptions,
  isExternalTeamSession,
  loadSessionModel,
  mergeSessionModelsFromBackend,
  persistDraftSessionModel,
  resolveSessionModelId,
  resolveSessionModelWindow,
  setSessionModel,
  sessionMessageModelLabel,
  syncSessionModelAvailabilityUi,
} from '../../src/ui/features/session-model';
import {
  defaultInspectorTabForSession,
  openInspectorToTab,
  refreshInspectorChrome,
} from '../../src/ui/features/inspector';
import {
  createSessionInWorkspace,
  getDraftSessionModelId,
} from '../../src/ui/features/workspaces';

// Inspector 渲染会触发 loadInspectorContext → backendApi.sessionContext；mock 掉避免网络。
const backendMocks = vi.hoisted(() => ({
  getSessionModel: vi.fn(),
  setSessionModel: vi.fn(),
  sessionContext: vi.fn(async () => ({ used_tokens: 0, max_tokens: 0, ratio: 0 })),
}));

vi.mock('../../src/ui/backend-client', () => ({
  backendApi: backendMocks,
}));

/** 全局默认模型 vs 会话级模型：base_url host 与 context_window 均不同，便于断言区分。 */
const MODELS = [
  { id: 'global-default', name: 'Global-Default', model: 'global-model', base_url: 'https://api.global.dev/v1', context_window: 128000, has_key: true, loaded: true },
  { id: 'session-model-x', name: 'Session Model X', model: 'model-x', base_url: 'https://api.xhost.com/v1', context_window: 256000, has_key: true, loaded: true },
];

beforeEach(() => {
  __resetAllStoresForTest();
  __resetSessionModelBindingsForTest();
  configStore.set({
    config: {
      model: 'global-model',
      has_key: true,
      base_url: 'https://api.global.dev/v1',
      active_model_id: 'global-default',
      models: MODELS,
    },
  });
  uiStore.set({ backendConnected: true });
  workspaceStore.set({
    currentWorkspaceId: 'default',
    workspaces: [{ id: 'default', name: '对话', description: '', instructions: '' }],
  });
  backendMocks.getSessionModel.mockReset();
  backendMocks.setSessionModel.mockReset();
  setActiveSessionId('sess-a');
  sessionStore.set({
    sessions: [{ id: 'sess-a', title: 'Sess A', workspaceId: 'default', updatedAt: 1, preview: '', badge: '' }],
  });
  // 一条 user 消息满足 canOpenInspector 的 hasConversationInfo
  messageStore.set({ messages: { 'sess-a': [{ id: 'm1', role: 'user', content: 'hello', timestamp: 1 }] } });
});

describe('session-model 会话级解析（根因：不再读全局 active_model_id）', () => {
  it('未绑定会话模型时回退全局 active_model_id', () => {
    expect(resolveSessionModelId()).toBe('global-default');
    expect(resolveSessionModelWindow()).toBe(128000);
    expect(sessionMessageModelLabel('sess-a', 'global-model')).toBe('Global-Default');
  });

  it('绑定会话模型后，id 与上下文窗口随会话模型走', () => {
    applySessionModelBinding('sess-a', {
      model_profile_id: 'session-model-x',
      model_label: 'Session Model X',
      has_pending: false,
      pending: false,
    });
    expect(resolveSessionModelId()).toBe('session-model-x');
    expect(resolveSessionModelWindow()).toBe(256000);
  });

  it('加载历史 Team Session 时恢复持久化的 Team identity 供 mention 使用', async () => {
    backendMocks.getSessionModel.mockResolvedValue({
      source: 'team',
      external_team_id: 'team-pixel',
      model_profile_id: 'team-model',
      model_label: 'Team Model',
    });

    expect((await loadSessionModel('sess-a'))?.external_team_id).toBe('team-pixel');
    expect((await import('../../src/ui/state')).state.activeExternalTeamIdBySession['sess-a'])
      .toBe('team-pixel');
  });

  it('pending（下条消息生效）时优先返回将生效的模型', () => {
    applySessionModelBinding('sess-a', {
      model_profile_id: 'global-default',
      pending_model_profile_id: 'session-model-x',
      model_label: 'Global-Default',
      pending_label: 'Session Model X',
      has_pending: true,
      pending: true,
    });
    expect(resolveSessionModelId()).toBe('session-model-x');
    expect(resolveSessionModelWindow()).toBe(256000);
  });

  it('外部单 Agent 使用 Runtime 模型目录，不混入 Crew 模型', () => {
    applySessionModelBinding('sess-a', {
      source: 'external',
      model_profile_id: 'kimi/k3',
      model_label: 'Kimi K3',
      model_switchable: true,
      models: [
        { id: 'kimi/default', label: 'Kimi Default', default: true },
        { id: 'kimi/k3', label: 'Kimi K3' },
      ],
    });

    expect(resolveSessionModelId()).toBe('kimi/k3');
    expect(composerModelOptions()).toEqual([
      { id: 'kimi/default', label: 'Kimi Default', description: 'kimi/default', selectable: true, warning: false, default: true },
      { id: 'kimi/k3', label: 'Kimi K3', description: 'kimi/k3', selectable: true, warning: false },
    ]);
    expect(sessionMessageModelLabel('sess-a', 'kimi/default')).toBe('Kimi Default');
  });

  it('Claude Code 气泡从 Runtime 模型目录解析具体模型标签', () => {
    applySessionModelBinding('sess-a', {
      source: 'external',
      model_profile_id: 'sonnet',
      model_label: 'Claude Sonnet（当前）',
      model_switchable: true,
      models: [
        { id: 'sonnet', label: 'Claude Sonnet（当前）', default: true },
        { id: 'claude-sonnet-4-5', label: 'Claude Sonnet 4.5' },
      ],
    });

    expect(sessionMessageModelLabel('sess-a', 'sonnet')).toBe('Claude Sonnet（当前）');
    expect(sessionMessageModelLabel('sess-a', 'claude-sonnet-4-5')).toBe('Claude Sonnet 4.5');
  });

  it('外部草稿切换模型后，首发持久化不会回退到创建时模型', async () => {
    const externalBinding = (modelId: string) => ({
      source: 'external' as const,
      model_profile_id: modelId,
      model_label: modelId === 'gpt-5.5' ? 'GPT-5.5' : 'GPT-5.6-Sol',
      model_switchable: true,
      models: [
        { id: 'gpt-5.6-sol', label: 'GPT-5.6-Sol', default: true },
        { id: 'gpt-5.5', label: 'GPT-5.5' },
      ],
    });
    backendMocks.getSessionModel.mockResolvedValue(externalBinding('gpt-5.6-sol'));
    backendMocks.setSessionModel.mockImplementation(async (_sid: string, modelId: string) => (
      externalBinding(modelId)
    ));

    const sessionId = createSessionInWorkspace('default', vi.fn());
    await loadSessionModel(sessionId);
    expect(getDraftSessionModelId()).toBe('gpt-5.6-sol');

    await setSessionModel('gpt-5.5');
    expect(getDraftSessionModelId()).toBe('gpt-5.5');

    await persistDraftSessionModel(sessionId);
    expect(backendMocks.setSessionModel).toHaveBeenLastCalledWith(
      sessionId,
      'gpt-5.5',
      { workspace_id: 'default' },
    );
  });

  it('外部单 Agent 运行时模型目录不可选择', () => {
    document.body.innerHTML = '<button id="chat-model-picker-inline-btn" class="composer-chip"></button>';
    const closeHandler = vi.fn();
    window.addEventListener('session:model-picker-disabled', closeHandler);
    applySessionModelBinding('sess-a', {
      source: 'external',
      model_profile_id: 'kimi/default',
      model_label: 'Kimi Default',
      model_switchable: true,
      models: [{ id: 'kimi/default', label: 'Kimi Default' }],
    });
    setBusy('sess-a', true);
    syncSessionModelAvailabilityUi();

    expect(composerModelOptions()[0]?.selectable).toBe(false);
    const trigger = document.getElementById('chat-model-picker-inline-btn') as HTMLButtonElement;
    expect(trigger.disabled).toBe(true);
    expect(trigger.title).toBe('任务运行中，结束后可切换模型');
    expect(closeHandler).toHaveBeenCalledTimes(1);
    window.removeEventListener('session:model-picker-disabled', closeHandler);
  });

  it('外部 Team Session 隐藏 Composer 模型按钮，切回普通会话后恢复', () => {
    document.body.innerHTML = `
      <div id="chat-model-picker-inline">
        <button id="chat-model-picker-inline-btn" class="composer-chip"></button>
      </div>
    `;
    sessionStore.set({
      sessions: [{
        id: 'sess-a',
        title: '研发团队',
        workspaceId: 'default',
        updatedAt: 1,
        preview: '',
        badge: '',
        agentLabel: { name: '研发团队', provider: 'team' },
      }],
    });

    syncSessionModelAvailabilityUi();

    const picker = document.getElementById('chat-model-picker-inline') as HTMLElement;
    const trigger = document.getElementById('chat-model-picker-inline-btn') as HTMLButtonElement;
    expect(picker.hidden).toBe(true);
    expect(trigger.disabled).toBe(true);
    expect(trigger.title).toBe('团队成员模型由团队配置决定');

    sessionStore.set({
      sessions: [{ id: 'sess-a', title: '普通会话', workspaceId: 'default', updatedAt: 1, preview: '', badge: '' }],
    });
    syncSessionModelAvailabilityUi();
    expect(picker.hidden).toBe(false);
    expect(trigger.disabled).toBe(false);
    expect(trigger.title).toBe('选择模型');
  });

  it('仅 provider=team 的 Session 被识别为协作会话', () => {
    sessionStore.set({
      sessions: [{
        id: 'sess-a',
        title: '研发团队',
        workspaceId: 'default',
        updatedAt: 1,
        preview: '',
        badge: '',
        agentLabel: { name: '研发团队', provider: 'team' },
      }],
    });
    expect(isExternalTeamSession('sess-a')).toBe(true);

    sessionStore.set({
      sessions: [{ id: 'sess-a', title: '普通会话', workspaceId: 'default', updatedAt: 1, preview: '', badge: '' }],
    });
    expect(isExternalTeamSession('sess-a')).toBe(false);
  });

  it('外部 Team Session 默认打开协作看板', () => {
    sessionStore.set({
      sessions: [{
        id: 'sess-a',
        title: '研发团队',
        workspaceId: 'default',
        updatedAt: 1,
        preview: '',
        badge: '',
        agentLabel: { name: '研发团队', provider: 'team' },
      }],
    });

    expect(defaultInspectorTabForSession('sess-a')).toBe('collaboration');

    sessionStore.set({
      sessions: [{ id: 'sess-a', title: '普通会话', workspaceId: 'default', updatedAt: 1, preview: '', badge: '' }],
    });
    expect(defaultInspectorTabForSession('sess-a')).toBe('context');
  });

  it('Inspector「协作」Tab 仅在 Team Session 显示', () => {
    document.body.innerHTML = `
      <button id="task-board-toggle"></button>
      <button id="ins-collaboration-tab" class="chat-inspector__tab is-hidden" data-inspector-tab="collaboration" hidden></button>
      <span id="ins-collaboration-count"></span>
    `;
    sessionStore.set({
      sessions: [{
        id: 'sess-a',
        title: '研发团队',
        workspaceId: 'default',
        updatedAt: 1,
        preview: '',
        badge: '',
        agentLabel: { name: '研发团队', provider: 'team' },
      }],
    });
    refreshInspectorChrome();
    const collaborationTab = document.getElementById('ins-collaboration-tab') as HTMLButtonElement;
    expect(collaborationTab.hidden).toBe(false);
    expect(collaborationTab.classList.contains('is-hidden')).toBe(false);

    sessionStore.set({
      sessions: [{ id: 'sess-a', title: '普通会话', workspaceId: 'default', updatedAt: 1, preview: '', badge: '' }],
    });
    refreshInspectorChrome();
    expect(collaborationTab.hidden).toBe(true);
    expect(collaborationTab.classList.contains('is-hidden')).toBe(true);
  });

  it('会话列表刷新不会用 Crew 摘要覆盖外部 Runtime 模型目录', () => {
    applySessionModelBinding('sess-a', {
      source: 'external',
      model_profile_id: 'kimi/k3',
      model_label: 'Kimi K3',
      model_switchable: true,
      models: [
        { id: 'kimi/default', label: 'Kimi Default' },
        { id: 'kimi/k3', label: 'Kimi K3' },
      ],
    });

    mergeSessionModelsFromBackend([{
      session_id: 'sess-a',
      model_profile_id: 'deepseek',
      model_label: 'DeepSeek',
    }]);

    expect(resolveSessionModelId()).toBe('kimi/k3');
    expect(composerModelOptions().map((model) => model.id)).toEqual(['kimi/default', 'kimi/k3']);
  });

  it('applySessionModelBinding 在当前会话变化时派发 session:model-changed', () => {
    const handler = vi.fn();
    window.addEventListener('session:model-changed', handler);
    applySessionModelBinding('sess-a', {
      model_profile_id: 'session-model-x',
      model_label: 'Session Model X',
      has_pending: false,
      pending: false,
    });
    expect(handler).toHaveBeenCalledTimes(1);
    window.removeEventListener('session:model-changed', handler);
  });

  it('非当前会话的绑定变化不派发事件', () => {
    const handler = vi.fn();
    window.addEventListener('session:model-changed', handler);
    applySessionModelBinding('other-sess', {
      model_profile_id: 'session-model-x',
      model_label: 'Session Model X',
      has_pending: false,
      pending: false,
    });
    expect(handler).not.toHaveBeenCalled();
    window.removeEventListener('session:model-changed', handler);
  });
});

describe('Inspector「上下文」页随会话模型变化', () => {
  beforeEach(() => {
    // renderBody 写 #chat-inspector-body；#task-board-toggle 供 syncInspectorToggleState 使用
    document.body.innerHTML = `
      <div id="chat-inspector"><div id="chat-inspector-body"></div></div>
      <button id="task-board-toggle"></button>
    `;
  });

  it('显示会话绑定模型的供应商/模型/上下文限制，而非全局默认', () => {
    applySessionModelBinding('sess-a', {
      model_profile_id: 'session-model-x',
      model_label: 'Session Model X',
      has_pending: false,
      pending: false,
    });
    openInspectorToTab('context');

    const text = document.getElementById('chat-inspector-body')?.textContent ?? '';
    // 模型名 = 会话模型
    expect(text).toContain('Session Model X');
    // 供应商 = 会话模型 base_url 的 host
    expect(text).toContain('api.xhost.com');
    // 上下文限制 = 会话模型窗口（fmtNum(256000) = "256.0k"）
    expect(text).toContain('256.0k');
    // 不应回退全局默认模型 / 全局窗口
    expect(text).not.toContain('Global-Default');
    expect(text).not.toContain('128.0k');
  });

  it('未绑定会话模型时回退全局默认（向后兼容）', () => {
    openInspectorToTab('context');
    const text = document.getElementById('chat-inspector-body')?.textContent ?? '';
    expect(text).toContain('Global-Default');
    expect(text).toContain('128.0k');
  });
});
