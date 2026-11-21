/**
 * @vitest-environment happy-dom
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
// 共享 mock 必须先于 chat-controller 加载，见 helpers/mock-chat-controller-deps.ts
import './helpers/mock-chat-controller-deps';
import { backendApi } from '../../src/ui/backend-client';
import {
  _resetQueueEditDraftForTests, appendMessage, applyChunk, consumePending, dispatchWs,
  editQueueItem, finalizeStreamingTurn, sendMessage, sendNativeVisualMessage,
} from '../../src/ui/features/chat-controller';
import { createComposerView, type ComposerView } from '../../src/ui/features/composer-view';
import { bindComposerMention } from '../../src/ui/features/composer-mention';
import { createMainComposerActions } from '../../src/ui/features/conversation-panel';
import { currentSecurityMode, rememberBrowserDraftSecurityScope, selectNextConversationMode } from '../../src/ui/features/security-approval';
import { fullAccessChoiceEnabled, loadFullAccessChoice, preferredNewConversationMode } from '../../src/ui/features/security-permission-settings';
import { commitDraftSession, createSessionInWorkspace, discardDraft, isDraftSession, setComposerTargetWorkspace } from '../../src/ui/features/workspaces';
import { chatOutbox } from '../../src/ui/features/chat-outbox';
import { appendAttachment, enqueuePending, getPendingQueue, patchBook, setBusy, setEditFrom, state } from '../../src/ui/state';
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

let composer: ComposerView | undefined;
afterEach(() => {
  chatOutbox.reset();
  vi.unstubAllGlobals();
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
    backendCanSend: true,
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

const serverGoalId = `goal_${'a'.repeat(32)}`;
function goalFrame(sid: string, kind: 'status' | 'delta' | 'final', sequence: number, text: string) {
  return {
    kind, session_id: sid, request_id: serverGoalId, sequence,
    gateway_epoch: 'fixture-goal-epoch', gateway_sequence: sequence, is_final: kind === 'final',
    body: kind === 'status' ? { message: text } : { text },
  };
}

function sealedGoalSession(sid: string): void {
  sessionStore.set({ activeSessionId: sid });
  appendMessage(sid, 'user', 'Previous ordinary request', { requestId: 'previous-ordinary' });
  appendMessage(sid, 'assistant', 'Previous ordinary answer', { requestId: 'previous-ordinary' });
  patchBook(sid, { turnSealed: true, activeRequestId: 'previous-ordinary', acceptingNewRequest: false });
}

function stubGoalHistory(sid: string) {
  const delayed = Promise.withResolvers<Response>();
  const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } });
  const fetch = vi.fn(async (url: string) => {
    const path = new URL(url).pathname;
    if (path === `/api/session/${sid}/history`) return delayed.promise;
    if (path === `/api/session/${sid}/status`) return json({ session_id: sid, live: 'idle', last_status: 'completed', queue_depth: 0 });
    if (path === `/api/session/${sid}/plan`) return json({ active: false, has_plan: false });
    return json([]);
  });
  vi.stubGlobal('fetch', fetch);
  return { fetch, resolve: () => delayed.resolve(json({
    items: [
      { role: 'user', content: 'Previous ordinary request', request_id: 'previous-ordinary', history_id: 'old-user' },
      { role: 'assistant', content: 'Previous ordinary answer', request_id: 'previous-ordinary', history_id: 'old-assistant' },
      { role: 'user', content: 'Durable prompt from the Goal step', request_id: serverGoalId, history_id: 'step-user' },
      { role: 'assistant', content: 'Durable Goal reply', request_id: serverGoalId, history_id: 'step-assistant' },
    ],
    has_more: false, next_before: null,
  })) };
}

describe('server-owned Goal steps', () => {
  it('adopts a verified frame after sealed history, queues ordinary input, and hydrates the real Goal boundary before sending', async () => {
    const sid = 'external-goal-queued-ordinary';
    sealedGoalSession(sid);
    const history = stubGoalHistory(sid);
    applyChunk(goalFrame(sid, 'status', 1, 'Model starting'));
    expect(applyChunk(goalFrame(sid, 'delta', 2, 'Streamed answer'))).toBe('repair');
    expect((messageStore.get().messages[sid] ?? []).map((message) => message.content)).toEqual([
      'Previous ordinary request', 'Previous ordinary answer',
    ]);
    expect(await sendMessage('After the Goal finishes')).toBe(true);
    expect(getPendingQueue(sid).map((item) => item.query)).toEqual(['After the Goal finishes']);
    expect(socketSend()).not.toHaveBeenCalled();
    applyChunk(goalFrame(sid, 'final', 3, 'Durable Goal reply'));
    await vi.waitFor(() => expect(history.fetch.mock.calls.some(([url]) => new URL(url).pathname.endsWith('/history'))).toBe(true));
    expect(await sendMessage('Second draft during Goal history reconciliation')).toBe(true);
    expect(getPendingQueue(sid).map((item) => item.query)).toEqual([
      'After the Goal finishes', 'Second draft during Goal history reconciliation',
    ]);
    expect(socketSend()).not.toHaveBeenCalled();
    history.resolve();
    await vi.waitFor(() => expect(socketSend()).toHaveBeenCalledTimes(1));
    expect(socketSend().mock.calls[0]?.[0]).toMatchObject({ query: 'After the Goal finishes' });
    expect((messageStore.get().messages[sid] ?? []).filter((message) => message.role === 'user').map((message) => message.content)).toEqual([
      'Previous ordinary request', 'Durable prompt from the Goal step', 'After the Goal finishes',
    ]);
  });

  it('does not reopen a sealed turn from a late Goal frame below the confirmed sequence', () => {
    const sid = 'external-goal-delayed-before-boundary';
    sealedGoalSession(sid);
    applyChunk({ ...goalFrame(sid, 'status', 5, 'Previous turn settled'), request_id: 'previous-ordinary' });
    expect(applyChunk(goalFrame(sid, 'delta', 4, 'Late Goal chunk'))).toBeUndefined();
    expect(sessionStore.get().books[sid]).toMatchObject({ activeRequestId: 'previous-ordinary', turnSealed: true });
  });

  it('never adopts a Goal frame over a known active ordinary request', async () => {
    const sid = 'external-goal-known-ordinary';
    sealedGoalSession(sid);
    expect(await sendMessage('Keep the active ordinary turn')).toBe(true);
    const ordinary = sessionStore.get().books[sid]?.activeRequestId;
    expect(ordinary).toBeTruthy();
    expect(applyChunk(goalFrame(sid, 'delta', 1, 'Different Goal output'))).toBeUndefined();
    expect(sessionStore.get().books[sid]?.activeRequestId).toBe(ordinary);
    expect((messageStore.get().messages[sid] ?? []).some((message) => message.content === 'Different Goal output')).toBe(false);
  });

  it('does not let a delayed Goal history response overwrite a newer ordinary turn', async () => {
    const sid = 'external-goal-newer-ordinary';
    sealedGoalSession(sid);
    const history = stubGoalHistory(sid);
    applyChunk(goalFrame(sid, 'final', 1, 'Durable Goal reply'));
    await vi.waitFor(() => expect(history.fetch.mock.calls.some(([url]) => new URL(url).pathname.endsWith('/history'))).toBe(true));
    expect(await sendMessage('New ordinary turn')).toBe(true);
    const ordinary = sessionStore.get().books[sid]?.activeRequestId;
    expect(ordinary).toBeTruthy();
    history.resolve();
    await vi.waitFor(() => expect(messageStore.get().historyLoading.has(sid)).toBe(false));
    expect(sessionStore.get().books[sid]?.activeRequestId).toBe(ordinary);
    expect((messageStore.get().messages[sid] ?? []).some((message) => message.content === 'New ordinary turn')).toBe(true);
    expect((messageStore.get().messages[sid] ?? []).some((message) => message.content === 'Durable prompt from the Goal step')).toBe(false);
  });
});

describe('sendMessage', () => {
  it('submits a multiline goal to the local command without sending chat', async () => {
    sessionStore.set({ activeSessionId: 'saved-goal-owner' });
    const goalEditor = document.body.appendChild(document.createElement('textarea'));
    const manageGoal = vi.fn(() => goalEditor.focus());
    const { input, send } = mountMainComposer();
    const skills = vi.spyOn(backendApi, 'skills').mockResolvedValue([]);
    const unbind = bindComposerMention(() => [{
      token: '/goal', detail: '管理会话目标', acceptsArgument: true, run: manageGoal,
    }]);
    try {
      setInput(input, '/GOAL  Reconcile A  and B\nVerify the totals');
      send.click();
      await vi.waitFor(() => expect(manageGoal).toHaveBeenCalledWith('Reconcile A  and B\nVerify the totals'));
      expect(socketSend()).not.toHaveBeenCalled();
      expect(input.value).toBe('');
      expect(document.activeElement).toBe(goalEditor);
    } finally {
      unbind();
      skills.mockRestore();
    }
  });

  it('retains a typed objective when its local Goal action cannot open', async () => {
    const { input, send, status } = mountMainComposer();
    const skills = vi.spyOn(backendApi, 'skills').mockResolvedValue([]);
    const run = vi.fn(() => false);
    const unbind = bindComposerMention(() => [{ token: '/goal', detail: '管理会话目标', acceptsArgument: true, run }]);
    try {
      setInput(input, '/goal Keep this text for later');
      send.click();
      await vi.waitFor(() => expect(run).toHaveBeenCalledWith('Keep this text for later'));
      expect(input.value).toBe('/goal Keep this text for later');
      expect(status.textContent).toContain('草稿已保留');
      expect(socketSend()).not.toHaveBeenCalled();
    } finally {
      unbind();
      skills.mockRestore();
    }
  });

  it('keeps text and attachments editable during startup while both entry points reject send', async () => {
    const sid = 'startup-draft';
    sessionStore.set({ activeSessionId: sid });
    uiStore.set({
      backendConnected: true,
      backendTransportState: 'unknown',
      backendReadiness: { status: 'starting', generation: 4 },
      backendCanSend: false,
    });
    const sendFrame = vi.mocked(uiStore.get().socket!.send);
    const { input, send, status } = mountMainComposer();
    expect(input.disabled).toBe(false);
    setInput(input, '启动期间编辑的草稿');
    appendAttachment(attachment);
    expect(send.disabled).toBe(true);
    expect(status.textContent).toContain('草稿可继续编辑');
    await expect(sendMessage(input.value)).resolves.toBe(false);
    await expect(dispatchWs(sid, input.value, [attachment])).resolves.toBe(false);
    expect(sendFrame).not.toHaveBeenCalled();
    expect(input.value).toBe('启动期间编辑的草稿');
    expect(state.attachments).toEqual([attachment]);

    uiStore.set({
      backendReadiness: { status: 'ready', generation: 4 },
      backendTransportState: 'connected',
      backendCanSend: true,
    });
    send.click();
    await vi.waitFor(() => expect(input.value).toBe(''));
    expect(sendFrame).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
      query: '启动期间编辑的草稿', attachments: [attachment],
    }));
  });

  it('reserves a queued message until accepted even when two completion signals try to dispatch it', async () => {
    const sid = 'recover-queued-turn';
    sessionStore.set({ activeSessionId: sid });
    const sendFrame = vi.mocked(uiStore.get().socket!.send);
    const first = Promise.withResolvers<boolean>();
    sendFrame.mockReturnValueOnce(first.promise);
    const item = { id: 'queued', query: 'Queued text', attachments: [attachment] };
    enqueuePending(sid, item);
    consumePending(sid);
    consumePending(sid);
    await vi.waitFor(() => expect(sendFrame).toHaveBeenCalledOnce());
    expect(getPendingQueue(sid)).toEqual([item]);
    first.resolve(false);
    await vi.waitFor(() => expect(sessionStore.get().busySessions[sid]).toBeFalsy());
    expect(getPendingQueue(sid)).toEqual([item]);

    consumePending(sid);
    consumePending(sid);
    await vi.waitFor(() => expect(getPendingQueue(sid)).toEqual([]));
    expect(sendFrame).toHaveBeenCalledTimes(2);
    expect(state.messages[sid].filter(({ role }) => role === 'user')).toHaveLength(1);
  });

  it('drains a durable queue item when the previous turn ends during its local write', async () => {
    const sid = 'queue-write-race';
    sessionStore.set({ activeSessionId: sid });
    setBusy(sid, true);
    appendAttachment(attachment);
    const staged = Promise.withResolvers<void>();
    const bridge = {
      chatOutboxList: vi.fn(async () => ({ scope: 'owner', entries: [] })),
      chatOutboxPut: vi.fn(async () => {}),
      chatOutboxRemove: vi.fn(async () => {}),
    };
    bridge.chatOutboxPut.mockReturnValueOnce(staged.promise);
    vi.stubGlobal('Crew', bridge);
    await chatOutbox.bind(bridge, { onRestored: vi.fn(), onReceipt: vi.fn() });
    const send = vi.fn(async () => true);
    chatOutbox.connect(send);
    chatOutbox.handle({
      kind: 'send_receipt_capability', body: { version: 1, store_id: '1'.repeat(32) },
      sequence: 0, is_final: false,
    });

    const submission = sendMessage('消息写入时上一轮结束');
    await vi.waitFor(() => expect(bridge.chatOutboxPut).toHaveBeenCalledOnce());
    expect(getPendingQueue(sid)).toEqual([]);
    expect(state.attachments).toEqual([attachment]);
    setBusy(sid, false);
    consumePending(sid);
    staged.resolve();
    await expect(submission).resolves.toBe(true);
    await vi.waitFor(() => expect(send).toHaveBeenCalledOnce());
    const [{ request_id }] = send.mock.calls[0] as unknown as [{ request_id: string }];
    expect(send).toHaveBeenCalledWith(expect.objectContaining({
      query: '消息写入时上一轮结束', attachments: [attachment], send_receipt: true, request_id,
    }), 'owner', '1'.repeat(32));
    expect(getPendingQueue(sid)[0]?.requestId).toBe(request_id);
    chatOutbox.handle({
      kind: 'send_receipt', session_id: sid, request_id, body: { status: 'accepted' },
      sequence: 0, is_final: false,
    });
    await vi.waitFor(() => expect(getPendingQueue(sid)).toEqual([]));
    expect(send).toHaveBeenCalledOnce();
  });

  it('keeps newly added files when editing a pending message with an attachment', () => {
    const sid = 'edit-queue-attachments';
    sessionStore.set({ activeSessionId: sid });
    const { input } = mountMainComposer();
    const newAttachment = { ...attachment, id: 'new-file', path: 'files/new.txt' };
    appendAttachment(newAttachment);
    enqueuePending(sid, { id: 'pending-one', query: '附件保留', attachments: [attachment] });
    editQueueItem(sid, 0);
    expect(input.value).toBe('附件保留');
    expect(state.attachments).toEqual([newAttachment, attachment]);
  });

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
    // An older history page arrived while the edit's send was in flight.
    const current = messageStore.get().messages;
    const oldest = { id: 'older', role: 'user' as const, content: '更早的问题', timestamp: 1 };
    messageStore.set({ messages: { ...current, [sid]: [oldest, ...current[sid]] } });
    setEditFrom(sid, 1);
    resolveSend(true);

    await vi.waitFor(() => expect(send.getAttribute('aria-busy')).toBe('false'));
    expect(input.value).toBe('下一份草稿');
    expect(state.attachments).toEqual([nextAttachment]);
    expect(sessionStore.get().editFromIdx[sid]).toBeFalsy();
    expect(state.messages[sid]).toContain(earlyResponse);
    expect(state.messages[sid].filter(({ role }) => role === 'user').map(({ content }) => content))
      .toEqual(['更早的问题', '修改后的问题']);
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
