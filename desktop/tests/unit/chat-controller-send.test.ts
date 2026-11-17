/**
 * @vitest-environment happy-dom
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
// 共享 mock 必须先于 chat-controller 加载，见 helpers/mock-chat-controller-deps.ts
import './helpers/mock-chat-controller-deps';
import { _resetQueueEditDraftForTests, appendMessage, dispatchWs, editQueueItem, finalizeStreamingTurn, sendMessage, sendNativeVisualMessage } from '../../src/ui/features/chat-controller';
import { createComposerView, type ComposerView } from '../../src/ui/features/composer-view';
import { createMainComposerActions } from '../../src/ui/features/conversation-panel';
import { isDraftSession } from '../../src/ui/features/workspaces';
import { appendAttachment, enqueuePending, getPendingQueue, patchBook, setBusy, setEditFrom, state } from '../../src/ui/state';
import { __resetAllStoresForTest, authStore, configStore, messageStore, sessionStore, uiStore, workspaceStore } from '../../src/ui/stores/stores';

vi.mock('../../src/ui/features/session-model', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/ui/features/session-model')>();
  return {
    ...actual,
    persistDraftSessionModel: vi.fn(async () => undefined),
  };
});
vi.mock('../../src/ui/features/native-visual-capability', () => ({ ensureNativeVisualCapability: vi.fn(async () => undefined) }));

let composer: ComposerView | undefined;
afterEach(() => {
  composer?.dispose();
  composer = undefined;
});

function mountMainComposer() {
  const host = document.createElement('div');
  document.body.append(host);
  composer = createComposerView(host, { ...createMainComposerActions(), primary: true });
  return {
    input: host.querySelector<HTMLTextAreaElement>('[data-composer-input]')!,
    send: host.querySelector<HTMLButtonElement>('[data-composer-send]')!,
    status: host.querySelector<HTMLElement>('#composer-status')!,
  };
}

function setInput(input: HTMLTextAreaElement, value: string): void {
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
}

const attachment = { id: 'report', name: 'report.txt', path: 'files/report.txt', type: 'file' as const };

beforeEach(() => {
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
  it('keeps the first draft and attachments when the socket refuses the message, then submits them once', async () => {
    const sendFrame = vi.mocked(uiStore.get().socket!.send);
    sendFrame.mockResolvedValueOnce(false);
    const { input, send, status } = mountMainComposer();
    appendAttachment(attachment);
    setInput(input, '请读取附件');
    send.click();
    await vi.waitFor(() => expect(status.textContent).toContain('草稿已保留'));

    const sessionId = sessionStore.get().activeSessionId!;
    expect(isDraftSession(sessionId)).toBe(true);
    expect(input.value).toBe('请读取附件');
    expect(state.attachments).toEqual([attachment]);
    expect(state.messages[sessionId].map(({ role }) => role)).toEqual(['error']);
    expect(sessionStore.get().busySessions[sessionId]).toBeFalsy();
    expect(sessionStore.get().books[sessionId].activeRequestId).toBeNull();

    send.click();
    await vi.waitFor(() => expect(input.value).toBe(''));
    expect(sendFrame).toHaveBeenCalledTimes(2);
    expect(sendFrame.mock.calls[1][0]).toMatchObject({
      query: '请读取附件', session_id: sessionId, attachments: [attachment],
    });
    expect(isDraftSession(sessionId)).toBe(false);
    expect(state.attachments).toEqual([]);
    expect(state.messages[sessionId].filter(({ role }) => role === 'user')).toHaveLength(1);
  });

  it('keeps the original edit history and attachment when IPC rejects', async () => {
    const sid = 'edit-failed';
    sessionStore.set({ activeSessionId: sid });
    const original = appendMessage(sid, 'user', '原问题');
    const reply = appendMessage(sid, 'assistant', '原回答');
    setEditFrom(sid, 0);
    appendAttachment(attachment);
    vi.mocked(uiStore.get().socket!.send).mockRejectedValueOnce(new Error('IPC disconnected'));
    const { input, send, status } = mountMainComposer();
    setInput(input, '修改后的问题');
    send.click();
    await vi.waitFor(() => expect(status.textContent).toContain('草稿已保留'));
    expect(input.value).toBe('修改后的问题');
    expect(state.attachments).toEqual([attachment]);
    expect(sessionStore.get().editFromIdx[sid]).toBe(0);
    expect(state.messages[sid].slice(0, 2)).toEqual([original, reply]);
    expect(state.messages[sid].at(-1)?.content).toContain('发送状态未确认');
  });

  it('removes the optimistic bubbles when disconnect seals the turn before send returns false', async () => {
    const sid = 'disconnect-before-send-result';
    sessionStore.set({ activeSessionId: sid });
    const sendFrame = vi.mocked(uiStore.get().socket!.send);
    let resolveSend!: (sent: boolean) => void;
    sendFrame.mockReturnValueOnce(new Promise((resolve) => { resolveSend = resolve; }));
    const submission = sendMessage('待发送');
    await vi.waitFor(() => expect(sendFrame).toHaveBeenCalledTimes(1));

    finalizeStreamingTurn(sid);
    setBusy(sid, false);
    patchBook(sid, { turnSealed: true, acceptingNewRequest: false });
    resolveSend(false);

    expect(await submission).toBe(false);
    expect(state.messages[sid].map(({ role }) => role)).toEqual(['error']);
    expect(sessionStore.get().books[sid].activeRequestId).toBeNull();
  });

  it('settles an edit without removing the new response or text and files added during the send', async () => {
    const sid = 'edit-accepted';
    sessionStore.set({ activeSessionId: sid });
    appendMessage(sid, 'user', '原问题');
    appendMessage(sid, 'assistant', '原回答');
    setEditFrom(sid, 0);
    appendAttachment(attachment);
    const sendFrame = vi.mocked(uiStore.get().socket!.send);
    let resolveSend!: (sent: boolean) => void;
    sendFrame.mockReturnValueOnce(new Promise((resolve) => { resolveSend = resolve; }));
    const { input, send } = mountMainComposer();
    setInput(input, '修改后的问题');
    send.click();
    await vi.waitFor(() => expect(sendFrame).toHaveBeenCalledTimes(1));
    expect(send.getAttribute('aria-busy')).toBe('true');
    setInput(input, '下一份草稿');
    const nextAttachment = { ...attachment, id: 'second-file' };
    appendAttachment(nextAttachment);
    const earlyResponse = appendMessage(sid, 'assistant', '已收到回复');
    resolveSend(true);

    await vi.waitFor(() => expect(send.getAttribute('aria-busy')).toBe('false'));
    expect(input.value).toBe('下一份草稿');
    expect(state.attachments).toEqual([nextAttachment]);
    expect(sessionStore.get().editFromIdx[sid]).toBeFalsy();
    expect(state.messages[sid]).toContain(earlyResponse);
    expect(state.messages[sid].filter(({ role }) => role === 'user').map(({ content }) => content))
      .toEqual(['修改后的问题']);
  });

  it('transfers attachment-only drafts to the queue while the current turn is busy', async () => {
    const sid = 'busy-with-attachment';
    sessionStore.set({ activeSessionId: sid, busySessions: { [sid]: true } });
    const { send } = mountMainComposer();
    appendAttachment(attachment);
    send.click();
    await vi.waitFor(() => expect(state.attachments).toEqual([]));
    expect(getPendingQueue(sid)).toMatchObject([{ attachments: [attachment] }]);
    expect(uiStore.get().socket!.send).not.toHaveBeenCalled();
  });

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
    expect(await sendNativeVisualMessage('inline-s', '{"cost":100}')).toBe(true);
    expect(getPendingQueue('inline-s')[0]).toMatchObject({ query: '{"cost":100}', attachments: [] });
    expect(state.attachments[0].id).toBe('draft-file');
    expect(input.value).toBe('尚未发送的草稿');
    expect(await sendNativeVisualMessage('another-session', 'no')).toBe(false);
    expect(getPendingQueue('inline-s')).toHaveLength(1);
  });
});
