/**
 * @vitest-environment happy-dom
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
// 共享 mock 必须先于 chat-controller 加载，见 helpers/mock-chat-controller-deps.ts
import './helpers/mock-chat-controller-deps';
import { _resetQueueEditDraftForTests, dispatchWs, editQueueItem, sendMessage, sendNativeVisualMessage } from '../../src/ui/features/chat-controller';
import { currentSecurityMode, rememberBrowserDraftSecurityScope, selectNextConversationMode } from '../../src/ui/features/security-approval';
import { fullAccessChoiceEnabled, loadFullAccessChoice, preferredNewConversationMode } from '../../src/ui/features/security-permission-settings';
import { commitDraftSession, createSessionInWorkspace, discardDraft, isDraftSession, setComposerTargetWorkspace } from '../../src/ui/features/workspaces';
import { enqueuePending, getPendingQueue, state } from '../../src/ui/state';
import { __resetAllStoresForTest, authStore, configStore, messageStore, sessionStore, uiStore, workspaceStore } from '../../src/ui/stores/stores';

vi.mock('../../src/ui/features/session-model', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/ui/features/session-model')>();
  return {
    ...actual,
    persistDraftSessionModel: vi.fn(async () => undefined),
  };
});
vi.mock('../../src/ui/features/security-permission-settings', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../src/ui/features/security-permission-settings')>(),
  fullAccessChoiceEnabled: vi.fn(),
  loadFullAccessChoice: vi.fn(),
  preferredNewConversationMode: vi.fn(),
}));

afterEach(() => {
  discardDraft();
  vi.unstubAllGlobals();
});

function enableSecurity(): void {
  configStore.set({
    config: {
      model: 'test-model', has_key: false, base_url: '', active_model_id: 'test-model', models: [],
      security: { enabled: true, default_mode: 'request_approval' },
    },
  });
}

function socketSend() {
  return (uiStore.get().socket as unknown as { send: ReturnType<typeof vi.fn> }).send;
}
vi.mock('../../src/ui/features/native-visual-capability', () => ({ ensureNativeVisualCapability: vi.fn(async () => undefined) }));
beforeEach(() => {
  vi.mocked(fullAccessChoiceEnabled).mockReset().mockReturnValue(true);
  vi.mocked(loadFullAccessChoice).mockReset().mockResolvedValue(undefined);
  vi.mocked(preferredNewConversationMode).mockReset().mockReturnValue(undefined);
  __resetAllStoresForTest();
  state.isLoggedIn = true;
  state.userInfo = { uid: 'test-owner' };
  _resetQueueEditDraftForTests();
  // auth-gate 已实现为真实登录判定（email/remote 登录墙）：local 桌面会话由
  // initAuthFlow 置为已登录，测试模拟该态。
  authStore.set({ isLoggedIn: true });
  const localStorageStub = {
    getItem: vi.fn(() => null),
    setItem: vi.fn(),
    removeItem: vi.fn(),
    clear: vi.fn(),
  };
  Object.defineProperty(window, 'localStorage', {
    configurable: true,
    value: localStorageStub,
  });
  vi.stubGlobal('localStorage', localStorageStub);
  configStore.set({ configModel: 'test-model' });
  workspaceStore.set({
    currentWorkspaceId: 'default',
    workspaces: [{ id: 'default', name: '对话', description: '', instructions: '' }],
  });
  uiStore.set({
    backendConnected: true,
    socket: {
      send: vi.fn(async () => true),
      planEnter: vi.fn(async () => true),
      subscribe: vi.fn(),
    } as never,
  });
  document.body.innerHTML = `
    <div id="history-list"></div>
    <section id="welcome-panel"></section>
    <section id="chat-panel" hidden>
      <div id="chat-messages"></div>
      <div class="chat-queue-slot"></div>
      <div id="composer-controls"></div>
      <div class="chat-running-intro"></div>
      <textarea data-composer-input></textarea>
    </section>
  `;
});

describe('sendMessage', () => {
  it('renders the first message immediately when no session is active', async () => {
    sendMessage('你好');
    await vi.waitFor(() => {
      const sessionId = sessionStore.get().activeSessionId;
      expect(messageStore.get().messages[sessionId!]?.[0]?.content).toBe('你好');
    });
    const sessionId = sessionStore.get().activeSessionId;
    expect(sessionId).toBeTruthy();
    expect((document.getElementById('chat-panel') as HTMLElement).hidden).toBe(false);
    expect(document.getElementById('chat-messages')?.textContent).toContain('你好');
  });

  it('moves an edited pending message to the queue tail when resubmitted while busy', () => {
    sessionStore.set({
      activeSessionId: 'sid-1',
      busySessions: { 'sid-1': true },
      sessions: [{ id: 'sid-1', title: '对话', preview: '', updatedAt: 0, workspaceId: 'default' }] as never,
    });
    enqueuePending('sid-1', { id: 'a', query: '第一条', attachments: [] });
    enqueuePending('sid-1', { id: 'b', query: '第二条', attachments: [] });
    enqueuePending('sid-1', { id: 'c', query: '第三条', attachments: [] });
    enqueuePending('sid-1', { id: 'd', query: '第四条', attachments: [] });

    editQueueItem('sid-1', 2);
    expect(getPendingQueue('sid-1').map((item) => item.query)).toEqual(['第一条', '第二条', '第四条']);

    sendMessage('第三条-修改后');

    expect(getPendingQueue('sid-1').map((item) => item.query)).toEqual([
      '第一条',
      '第二条',
      '第四条',
      '第三条-修改后',
    ]);
    // 入队（busy 时发送，即「引导/待发」卡片的来源）不应改写会话标题：
    // 标题由首条消息决定，待发消息不得抢占。回归 issue 4：标题不再变成 steer/排队内容。
    const enqueued = sessionStore.get().sessions.find((s) => s.id === 'sid-1');
    expect(enqueued?.title).toBe('对话');
  });

  it('sends plan_active when composer is in Plan mode', async () => {
    configStore.set({ composerMode: 'plan' });

    sendMessage('先规划');

    await vi.waitFor(() => {
      const socket = uiStore.get().socket as unknown as { send: ReturnType<typeof vi.fn> };
      expect(socket.send).toHaveBeenCalled();
    });

    const socket = uiStore.get().socket as unknown as { send: ReturnType<typeof vi.fn>; planEnter: ReturnType<typeof vi.fn> };
    expect(socket.planEnter).toHaveBeenCalled();
    expect(socket.send.mock.calls.at(-1)?.[0]).toMatchObject({
      query: '先规划',
      plan_active: true,
    });
  });

  it('uses the queued message plan flag instead of the current composer mode', async () => {
    configStore.set({ composerMode: 'plan' });

    await dispatchWs('sid-queued', '按普通模式发送', [], '', false);

    const socket = uiStore.get().socket as unknown as { send: ReturnType<typeof vi.fn>; planEnter: ReturnType<typeof vi.fn> };
    expect(socket.planEnter).not.toHaveBeenCalled();
    expect(socket.send.mock.calls.at(-1)?.[0]).toMatchObject({
      query: '按普通模式发送',
    });
    expect(socket.send.mock.calls.at(-1)?.[0]).not.toHaveProperty('plan_active');
  });

  it('loads the saved preference before choosing and confirming the mode of the first query', async () => {
    enableSecurity();
    let finishLoading!: () => void;
    vi.mocked(loadFullAccessChoice).mockImplementationOnce(() => new Promise((resolve) => { finishLoading = resolve; }));
    const securitySetMode = vi.fn().mockResolvedValue({ ok: true, body: { mode: 'auto_review' } });
    vi.stubGlobal('Crew', { securitySetMode });

    const firstSend = sendMessage('use saved preference');
    expect(loadFullAccessChoice).toHaveBeenCalledOnce();
    expect(securitySetMode).not.toHaveBeenCalled();
    expect(socketSend()).not.toHaveBeenCalled();
    vi.mocked(preferredNewConversationMode).mockReturnValue('auto_review');
    finishLoading();

    await expect(firstSend).resolves.toBe(true);
    expect(securitySetMode).toHaveBeenCalledWith({
      sessionId: sessionStore.get().activeSessionId, workspaceId: 'default', mode: 'auto_review',
    });
  });

  it('keeps the unsent draft when reading the Desktop permission preference fails', async () => {
    enableSecurity();
    vi.mocked(loadFullAccessChoice).mockRejectedValueOnce(new Error('IPC unavailable'));
    const securitySetMode = vi.fn();
    vi.stubGlobal('Crew', { securitySetMode });

    await expect(sendMessage('keep this draft')).resolves.toBe(false);

    expect(isDraftSession(sessionStore.get().activeSessionId!)).toBe(true);
    expect(securitySetMode).not.toHaveBeenCalled();
    expect(socketSend()).not.toHaveBeenCalled();
  });

  it('requires a scoped Gateway acknowledgement for a trusted Full Access setting on each new conversation', async () => {
    enableSecurity();
    vi.mocked(preferredNewConversationMode).mockReturnValue('full_access');
    const securitySetMode = vi.fn().mockResolvedValue({ ok: true, body: { mode: 'full_access' } });
    vi.stubGlobal('Crew', { securitySetMode });

    await expect(sendMessage('first conversation')).resolves.toBe(true);
    const firstSessionId = sessionStore.get().activeSessionId;
    sessionStore.set({ activeSessionId: null });
    workspaceStore.set({ currentWorkspaceId: 'other-project' });
    await expect(sendMessage('second conversation')).resolves.toBe(true);
    const secondSessionId = sessionStore.get().activeSessionId;

    expect(secondSessionId).not.toBe(firstSessionId);
    expect(securitySetMode.mock.calls).toEqual([
      [{ sessionId: firstSessionId, workspaceId: 'default', mode: 'full_access' }],
      [{ sessionId: secondSessionId, workspaceId: 'other-project', mode: 'full_access' }],
    ]);
  });

  it('discards an in-flight acknowledgement for the Full Access default when choice is turned off', async () => {
    enableSecurity();
    vi.mocked(preferredNewConversationMode).mockReturnValue('full_access');
    let acknowledge!: (value: { ok: boolean; body: { mode: string } }) => void;
    const securitySetMode = vi.fn()
      .mockImplementationOnce(() => new Promise((resolve) => { acknowledge = resolve; }))
      .mockResolvedValueOnce({ ok: true, body: { mode: 'request_approval' } })
      .mockResolvedValueOnce({ ok: true, body: { mode: 'auto_review' } });
    vi.stubGlobal('Crew', { securitySetMode });

    const firstSend = sendMessage('wait for permission');
    const sessionId = sessionStore.get().activeSessionId;
    await vi.waitFor(() => expect(securitySetMode).toHaveBeenCalledWith({
      sessionId, workspaceId: 'default', mode: 'full_access',
    }));
    vi.mocked(fullAccessChoiceEnabled).mockReturnValue(false);
    acknowledge({ ok: true, body: { mode: 'full_access' } });

    await expect(firstSend).resolves.toBe(false);
    expect(socketSend()).not.toHaveBeenCalled();
    expect(securitySetMode).toHaveBeenLastCalledWith({ sessionId, workspaceId: 'default', mode: 'request_approval' });
    await expect(sendMessage('use managed default')).resolves.toBe(true);
    expect(securitySetMode).toHaveBeenLastCalledWith({ sessionId, workspaceId: 'default', mode: 'auto_review' });
  });

  it('awaits scoped full-access acknowledgement before the first query and does not apply it to the next draft', async () => {
    enableSecurity();
    let acknowledge!: (value: { ok: boolean; body: { mode: string } }) => void;
    const securitySetMode = vi.fn()
      .mockImplementationOnce(() => new Promise((resolve) => { acknowledge = resolve; }))
      .mockResolvedValueOnce({ ok: true, body: { mode: 'request_approval' } });
    vi.stubGlobal('Crew', { securitySetMode });
    workspaceStore.set({ currentWorkspaceId: 'project-a' });

    await expect(selectNextConversationMode('full_access', () => true)).resolves.toBe(true);
    const sessionId = sessionStore.get().activeSessionId!;
    const firstSend = sendMessage('first query');
    await vi.waitFor(() => expect(securitySetMode).toHaveBeenCalledWith({
      sessionId, workspaceId: 'project-a', mode: 'full_access',
    }));
    expect(socketSend()).not.toHaveBeenCalled();
    expect(messageStore.get().messages[sessionId]).toEqual([]);
    acknowledge({ ok: true, body: { mode: 'full_access' } });
    await expect(firstSend).resolves.toBe(true);
    expect(socketSend()).toHaveBeenCalledWith(expect.objectContaining({
      session_id: sessionId, workspace_id: 'project-a', query: 'first query',
    }));
    expect(isDraftSession(sessionId)).toBe(false);
    expect(currentSecurityMode()).toBe('full_access');

    sessionStore.set({ activeSessionId: null });
    await expect(sendMessage('next conversation')).resolves.toBe(true);
    const nextSessionId = sessionStore.get().activeSessionId;
    expect(nextSessionId).not.toBe(sessionId);
    expect(securitySetMode).toHaveBeenLastCalledWith({
      sessionId: nextSessionId, workspaceId: 'project-a', mode: 'request_approval',
    });
  });

  it('keeps the draft attachments and sends no query when the Gateway does not confirm the mode', async () => {
    enableSecurity();
    const securitySetMode = vi.fn().mockResolvedValue({ ok: true, body: { mode: 'request_approval' } });
    vi.stubGlobal('Crew', { securitySetMode });
    await selectNextConversationMode('full_access', () => true);
    const sessionId = sessionStore.get().activeSessionId!;
    state.attachments = [{ id: 'draft-file', name: 'notes.txt' }] as never;

    await expect(sendMessage('keep this draft')).resolves.toBe(false);

    expect(socketSend()).not.toHaveBeenCalled();
    expect(isDraftSession(sessionId)).toBe(true);
    expect(state.attachments[0]?.id).toBe('draft-file');
    expect(securitySetMode).toHaveBeenLastCalledWith({ sessionId, workspaceId: 'default', mode: 'request_approval' });
  });

  it('does not dispatch a stale full-access confirmation after its draft switches workspace', async () => {
    enableSecurity();
    let acknowledge!: (value: { ok: boolean; body: { mode: string } }) => void;
    const securitySetMode = vi.fn()
      .mockImplementationOnce(() => new Promise((resolve) => { acknowledge = resolve; }))
      .mockResolvedValue({ ok: true, body: { mode: 'request_approval' } });
    vi.stubGlobal('Crew', { securitySetMode });
    await selectNextConversationMode('full_access', () => true);
    const sessionId = sessionStore.get().activeSessionId!;
    const firstSend = sendMessage('old workspace');
    await vi.waitFor(() => expect(securitySetMode).toHaveBeenCalledOnce());

    setComposerTargetWorkspace('new-project');
    acknowledge({ ok: true, body: { mode: 'full_access' } });

    await expect(firstSend).resolves.toBe(false);
    expect(socketSend()).not.toHaveBeenCalled();
    expect(securitySetMode).toHaveBeenLastCalledWith({ sessionId, workspaceId: 'default', mode: 'request_approval' });
    await expect(sendMessage('new workspace')).resolves.toBe(true);
    expect(securitySetMode).toHaveBeenLastCalledWith({ sessionId, workspaceId: 'new-project', mode: 'request_approval' });
  });

  it('keeps the confirmed mode until the first query if Browser saved the same session first', async () => {
    enableSecurity();
    const securitySetMode = vi.fn().mockResolvedValue({ ok: true, body: { mode: 'full_access' } });
    vi.stubGlobal('Crew', { securitySetMode });
    await selectNextConversationMode('full_access', () => true);
    const sessionId = sessionStore.get().activeSessionId!;
    rememberBrowserDraftSecurityScope(sessionId, 'default');
    commitDraftSession(sessionId, '浏览器', '内置浏览器');

    expect(securitySetMode).not.toHaveBeenCalled();
    expect(isDraftSession(sessionId)).toBe(false);
    await expect(sendMessage('first model turn')).resolves.toBe(true);
    expect(securitySetMode).toHaveBeenCalledWith({ sessionId, workspaceId: 'default', mode: 'full_access' });
  });

  it('requires the saved default to be acknowledged before a Browser-created conversation sends its first model query', async () => {
    enableSecurity();
    vi.mocked(preferredNewConversationMode).mockReturnValue('auto_review');
    const sessionId = createSessionInWorkspace('browser-project', vi.fn());
    rememberBrowserDraftSecurityScope(sessionId, 'browser-project');
    commitDraftSession(sessionId, '浏览器', '内置浏览器');
    sessionStore.set({ activeSessionId: null });
    sessionStore.set({ activeSessionId: sessionId });
    const securitySetMode = vi.fn()
      .mockResolvedValueOnce({ ok: true, body: { mode: 'request_approval' } })
      .mockResolvedValueOnce({ ok: true, body: { mode: 'auto_review' } });
    vi.stubGlobal('Crew', { securitySetMode });

    expect(currentSecurityMode()).toBe('auto_review');
    await expect(sendMessage('keep when permission has not been acknowledged')).resolves.toBe(false);
    expect(socketSend()).not.toHaveBeenCalled();
    await expect(sendMessage('first browser conversation query')).resolves.toBe(true);
    expect(securitySetMode).toHaveBeenCalledTimes(2);
    expect(securitySetMode).toHaveBeenLastCalledWith({
      sessionId, workspaceId: 'browser-project', mode: 'auto_review',
    });
    expect(socketSend()).toHaveBeenCalledWith(expect.objectContaining({
      workspace_id: 'browser-project', session_id: sessionId, query: 'first browser conversation query',
    }));

    vi.mocked(preferredNewConversationMode).mockReturnValue('request_approval');
    expect(currentSecurityMode()).toBe('auto_review');
    await expect(dispatchWs(sessionId, 'later query', [])).resolves.toBe(true);
    expect(securitySetMode).toHaveBeenCalledTimes(2);
  });
});


describe('inline component submissions', () => {
  it('queues a snapshot without consuming composer attachments or drafts', async () => {
    sessionStore.set({ activeSessionId: 'inline-s', busySessions: { 'inline-s': true } });
    state.attachments = [{ id: 'draft-file', name: 'draft.txt' }] as never;
    const input = document.querySelector<HTMLTextAreaElement>('[data-composer-input]')!;
    input.value = '尚未发送的草稿';
    expect(await sendNativeVisualMessage('inline-s', '{"cost":100}')).toBe(true);
    expect(getPendingQueue('inline-s')[0]).toMatchObject({ query: '{"cost":100}', attachments: [] });
    expect(state.attachments[0].id).toBe('draft-file');
    expect(input.value).toBe('尚未发送的草稿');
    expect(await sendNativeVisualMessage('another-session', 'no')).toBe(false);
    expect(getPendingQueue('inline-s')).toHaveLength(1);
  });
});
