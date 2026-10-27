/**
 * @vitest-environment happy-dom
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
// 共享 mock 必须先于 chat-controller 加载，见 helpers/mock-chat-controller-deps.ts
import './helpers/mock-chat-controller-deps';
import { _resetQueueEditDraftForTests, dispatchWs, editQueueItem, sendMessage, sendInlineWidgetMessage } from '../../src/ui/features/chat-controller';
import { enqueuePending, getPendingQueue, state } from '../../src/ui/state';
import { __resetAllStoresForTest, configStore, messageStore, sessionStore, uiStore, workspaceStore } from '../../src/ui/stores/stores';

vi.mock('../../src/ui/features/session-model', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/ui/features/session-model')>();
  return {
    ...actual,
    persistDraftSessionModel: vi.fn(async () => undefined),
  };
});
vi.mock('../../src/ui/features/inline-widget-capability', () => ({ ensureInlineWidgetCapability: vi.fn(async () => undefined) }));
beforeEach(() => {
  __resetAllStoresForTest();
  state.isLoggedIn = true;
  state.userInfo = { uid: 'test-owner' };
  _resetQueueEditDraftForTests();
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
});


describe('inline component submissions', () => {
  it('queues a snapshot without consuming composer attachments or drafts', async () => {
    sessionStore.set({ activeSessionId: 'inline-s', busySessions: { 'inline-s': true } });
    state.attachments = [{ id: 'draft-file', name: 'draft.txt' }] as never;
    const input = document.querySelector<HTMLTextAreaElement>('[data-composer-input]')!;
    input.value = '尚未发送的草稿';
    expect(await sendInlineWidgetMessage('inline-s', '{"cost":100}')).toBe(true);
    expect(getPendingQueue('inline-s')[0]).toMatchObject({ query: '{"cost":100}', attachments: [] });
    expect(state.attachments[0].id).toBe('draft-file');
    expect(input.value).toBe('尚未发送的草稿');
    expect(await sendInlineWidgetMessage('another-session', 'no')).toBe(false);
    expect(getPendingQueue('inline-s')).toHaveLength(1);
  });
});
