/**
 * @vitest-environment happy-dom
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { backendApi } from '../../src/ui/backend-client';
import { bootstrapBackend, loadOlderHistoryPage, shouldSkipHistoryReloadOnReconnect } from '../../src/ui/features/session-controller';
import { createSessionHistoryView } from '../../src/ui/features/session-history-view';
import { openTurnForRequest } from '../../src/ui/features/session-busy';
import { appendMessage, consumePending, stopGeneration } from '../../src/ui/features/chat-controller';
import { getGatewayCursors, resetGatewaySequence } from '../../src/ui/features/gateway-sequence';
import { cancelSessionStreamRecovery, getSessionStreamRecovery } from '../../src/ui/features/session-stream-recovery';
import { stopStreamWatchdog } from '../../src/ui/features/stream-watchdog';
import { authStore } from '../../src/ui/stores/auth-store';
import {
  __resetAllStoresForTest,
  configStore,
  messageStore,
  sessionStore,
} from '../../src/ui/stores/stores';
import {
  addSubscribedSessions, enqueuePending, ensureSessionBook, getPendingQueue, patchBook,
  setBusy, setActiveSessionId, setHistoryPaging, state,
} from '../../src/ui/state';

beforeEach(() => {
  __resetAllStoresForTest();
  document.body.innerHTML = '';
  setActiveSessionId('sid-1');
});

describe('conversation recovery after a socket outage', () => {
  const sid = 'sid-1';
  const oldRequest = 'req-before-disconnect';
  const settledStatus = {
    session_id: sid,
    live: 'idle',
    queue_depth: 0,
    last_status: 'completed',
    last_error: '',
  };
  let emitSocket: (event: unknown) => void;

  beforeEach(() => {
    vi.useFakeTimers();
    authStore.set({ isLoggedIn: true });
    vi.stubGlobal('Crew', {
      gatewayWsConnect: vi.fn().mockResolvedValue({ ok: true }),
      gatewayWsSend: vi.fn().mockResolvedValue({ ok: true }),
      gatewayWsClose: vi.fn().mockResolvedValue({ ok: true }),
      onGatewayWsEvent: (onEvent: (event: unknown) => void) => {
        emitSocket = onEvent;
        return () => {};
      },
    });
    openTurnForRequest(sid, oldRequest);
    setBusy(sid, true);
    addSubscribedSessions([sid]);
    bootstrapBackend();
    vi.spyOn(backendApi, 'sessionPlan').mockRejectedValue(new Error('plan endpoint unavailable'));
    vi.spyOn(backendApi, 'sessionTodos').mockResolvedValue({ todos: [] });
  });

  afterEach(() => {
    state.socket?.dispose();
    stopStreamWatchdog();
    cancelSessionStreamRecovery(sid);
    resetGatewaySequence(sid);
    authStore.set({ isLoggedIn: false });
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  function disconnectAndReconnect(): void {
    emitSocket({ type: 'close', code: 1012 });
    expect(sessionStore.get().busySessions[sid]).toBe(true);
    expect(sessionStore.get().books[sid]?.turnSealed).toBe(false);
    expect(messageStore.get().messages[sid][0].streaming).toBe(true);
    emitSocket({ type: 'open' });
  }

  function emitFrame(kind: string, sequence: number, body: object = {}, epoch = 'epoch-1', request = oldRequest): void {
    emitSocket({ type: 'message', data: JSON.stringify({
      kind, session_id: sid, request_id: request, gateway_epoch: epoch,
      gateway_sequence: sequence, sequence, body, is_final: kind === 'final',
    }) });
  }

  function snapshotFrame(kind: string, sequence: number, body: object) {
    return {
      kind, session_id: sid, request_id: oldRequest, gateway_epoch: 'epoch-1',
      gateway_sequence: sequence, sequence, body, is_final: false,
    };
  }

  function snapshotBody(frames: ReturnType<typeof snapshotFrame>[] = []) {
    return {
      reason: 'cursor_expired', floor: 6,
      turn_snapshot: {
        request_id: oldRequest,
        started_at: 1_700_000_000,
        history: {
          items: [
            { role: 'assistant', content: 'Offline durable turn' },
            { role: 'user', content: 'User from Gateway', request_id: oldRequest },
          ],
          has_more: true,
          next_before: 'new-page',
        },
        frames,
      },
    };
  }

  it('atomically projects the active snapshot, preserves local input and queue, and extends the seeded text', async () => {
    const status = Promise.withResolvers<typeof settledStatus>();
    vi.spyOn(backendApi, 'sessionStatus').mockReturnValue(status.promise);
    const history = vi.spyOn(backendApi, 'history').mockResolvedValue([{ role: 'assistant', content: 'Stale terminal response' }]);
    const attachment = { id: 'file', name: 'notes.txt', path: '/fixture/notes.txt', type: 'file' as const };
    const prompt = appendMessage(sid, 'user', 'Local display', { requestId: oldRequest, attachments: [attachment] });
    const queuedPrompt = appendMessage(sid, 'user', 'Queued change');
    const queued = { id: 'queued', query: 'Queued change', attachments: [], optimisticUserMessageId: queuedPrompt.id };
    enqueuePending(sid, queued);
    emitFrame('delta', 1, { text: 'Old partial' });
    emitSocket({ type: 'open' });
    const seen: Array<{ answer: string | undefined; requestId: string | null | undefined }> = [];
    const unsubscribe = messageStore.subscribe((next) => {
      const book = sessionStore.get().books[sid];
      if (next.historyPaging[sid]?.nextBefore === 'new-page') {
        seen.push({ answer: next.messages[sid].find((row) => row.id === book?.assistantId)?.content,
          requestId: book?.activeRequestId });
      }
    });
    emitFrame('gateway_resync', 10, snapshotBody([
      snapshotFrame('thinking', 2, { text: 'Lost thinking' }),
      snapshotFrame('delta', 3, { text: 'Before tool' }),
      snapshotFrame('tool', 4, { name: 'browser', tool_call_id: 'tool-1', phase: 'start' }),
      snapshotFrame('tool', 5, { name: 'browser', tool_call_id: 'tool-1', phase: 'result', detail: 'Result' }),
      snapshotFrame('delta', 8, { text: 'After tool' }),
    ]));
    unsubscribe();

    expect(seen).toEqual([{ answer: 'After tool', requestId: oldRequest }]);
    expect(getSessionStreamRecovery(sid)).toBeUndefined();
    expect(sessionStore.get().busySessions[sid]).toBe(true);
    expect(getPendingQueue(sid)).toEqual([queued]);
    expect(messageStore.get().messages[sid].map(({ content }) => content)).toEqual([
      'Offline durable turn', 'Local display', 'Before tool', 'After tool', 'Queued change',
    ]);
    expect(messageStore.get().messages[sid][1]).toMatchObject({ id: prompt.id, attachments: [attachment] });
    expect(messageStore.get().messages[sid][2]).toMatchObject({
      thinking: 'Lost thinking', toolCalls: [{ toolCallId: 'tool-1', status: 'done', result: 'Result' }],
    });
    expect(messageStore.get().messages[sid].at(-1)?.id).toBe(queuedPrompt.id);

    status.resolve(settledStatus);
    await vi.advanceTimersByTimeAsync(0);
    expect(history).not.toHaveBeenCalled();
    emitFrame('delta', 8, { text: 'After tool' });
    emitFrame('delta', 11, { text: ' continues' });
    expect(messageStore.get().messages[sid][3].content).toBe('After tool continues');
  });

  it('replaces an in-flight pagination read and applies the snapshot pagination cursor', async () => {
    vi.spyOn(backendApi, 'sessionStatus').mockResolvedValue({ ...settledStatus, live: 'running', active_request_id: oldRequest });
    const oldPage = Promise.withResolvers<Awaited<ReturnType<typeof backendApi.historyWindow>>>();
    const window = vi.spyOn(backendApi, 'historyWindow')
      .mockReturnValueOnce(oldPage.promise)
      .mockResolvedValueOnce({ items: [{ role: 'user', content: 'Older retained page' }], has_more: false, next_before: null });
    setHistoryPaging(sid, { hasMore: true, nextBefore: 'old-page' });
    const outdatedLoad = loadOlderHistoryPage(sid);
    emitSocket({ type: 'open' });
    emitFrame('gateway_resync', 10, snapshotBody([snapshotFrame('delta', 8, { text: 'Recovered' })]));

    await expect(loadOlderHistoryPage(sid)).resolves.toBe(true);
    expect(window).toHaveBeenLastCalledWith(sid, { limit: 100, before: 'new-page' });
    oldPage.resolve({ items: [{ role: 'user', content: 'Outdated page' }], has_more: false, next_before: null });
    await expect(outdatedLoad).resolves.toBe(false);
    expect(messageStore.get().messages[sid].map(({ content }) => content)).toEqual([
      'Older retained page', 'Offline durable turn', 'User from Gateway', 'Recovered',
    ]);
  });

  it('retains a verified question in the projection and accepts its side-channel resolution', () => {
    vi.spyOn(backendApi, 'sessionStatus').mockResolvedValue({ ...settledStatus, live: 'running', active_request_id: oldRequest });
    emitSocket({ type: 'open' });
    emitFrame('gateway_resync', 10, {
      ...snapshotBody([snapshotFrame('delta', 8, { text: 'Before question' })]),
      pending_followups: [{
        ...snapshotFrame('followup_question', 9, { question_id: 'q-verified', questions: [{ id: 'q', question: 'Proceed?' }] }),
        request_id: 'question-side-channel',
      }],
    });
    expect(getSessionStreamRecovery(sid)).toBeUndefined();
    expect(sessionStore.get().busySessions[sid]).toBeFalsy();
    expect(sessionStore.get().books[sid]?.pendingFollowup?.questionId).toBe('q-verified');
    expect(messageStore.get().messages[sid].at(-1)).toMatchObject({ content: 'Before question', streaming: false });
    emitFrame('followup_question', 11, { question_id: 'q-verified', status: 'resolved' }, 'epoch-1', 'question-side-channel');
    expect(sessionStore.get().books[sid]?.pendingFollowup).toBeNull();
  });

  it.each(['different active request', 'out-of-cut frame', 'nonprojection event'])(
    'keeps terminal-history fallback for an unusable active snapshot (%s)', async (scenario) => {
      const newerRequest = 'newer-local-request';
      if (scenario === 'different active request') openTurnForRequest(sid, newerRequest);
      vi.spyOn(backendApi, 'sessionStatus').mockResolvedValue({
        ...settledStatus, live: 'running',
        active_request_id: scenario === 'different active request' ? newerRequest : oldRequest,
      });
      const history = vi.spyOn(backendApi, 'history').mockResolvedValue([]);
      emitSocket({ type: 'open' });
      const frame = scenario === 'nonprojection event'
        ? snapshotFrame('feature_event', 8, { feature: 'wiki', event: 'changed' })
        : snapshotFrame('delta', scenario === 'out-of-cut frame' ? 11 : 8, { text: 'Stale projection' });
      emitFrame('gateway_resync', 10, snapshotBody([frame]));
      await vi.advanceTimersByTimeAsync(0);
      expect(getSessionStreamRecovery(sid)).toBeDefined();
      expect(history).not.toHaveBeenCalled();
      expect(messageStore.get().messages[sid].map(({ content }) => content)).not.toContain('Stale projection');
      if (scenario === 'different active request') {
        expect(sessionStore.get().books[sid]?.activeRequestId).toBe(newerRequest);
      }
    },
  );

  it('rebuilds settled history after an epoch reset and rejects frames from the retired epoch', async () => {
    vi.spyOn(backendApi, 'sessionStatus').mockResolvedValue(settledStatus);
    const history = vi.spyOn(backendApi, 'history').mockResolvedValue([{ role: 'assistant', content: 'Canonical answer' }]);
    emitFrame('delta', 15, { text: 'Old partial' });
    disconnectAndReconnect();
    emitFrame('gateway_resync', 0, { reason: 'epoch_changed', floor: 0 }, 'epoch-2');
    emitFrame('delta', 16, { text: 'Late old chunk' });
    await vi.advanceTimersByTimeAsync(0);

    expect(history).toHaveBeenCalledOnce();
    expect(getGatewayCursors([sid])).toEqual({ [sid]: { epoch: 'epoch-2', sequence: 0 } });
    expect(messageStore.get().messages[sid].map((message) => message.content)).toEqual(['Canonical answer']);
    expect(getSessionStreamRecovery(sid)).toBeUndefined();
  });

  it('keeps the partial and local queue until history replaces an incomplete active stream', async () => {
    const running = { ...settledStatus, live: 'running', active_request_id: oldRequest };
    const status = vi.spyOn(backendApi, 'sessionStatus').mockResolvedValue(running);
    const historyResponse = Promise.withResolvers<Awaited<ReturnType<typeof backendApi.history>>>();
    const history = vi.spyOn(backendApi, 'history').mockReturnValue(historyResponse.promise);
    emitFrame('delta', 1, { text: 'Prefix' });
    emitSocket({ type: 'open' });
    emitFrame('gateway_resync', 10, { reason: 'cursor_expired', floor: 6 });
    await vi.advanceTimersByTimeAsync(0);

    const queued = { id: 'queued', query: 'next', attachments: [] };
    enqueuePending(sid, queued);
    emitFrame('gateway_resync', 10, { reason: 'cursor_expired', floor: 6 });
    emitFrame('delta', 11, { text: 'Incomplete suffix' });
    consumePending(sid);
    expect(getPendingQueue(sid)).toEqual([queued]);
    expect(messageStore.get().messages[sid][0].content).toBe('Prefix');
    expect(getSessionStreamRecovery(sid)).toBeDefined();
    expect(history).not.toHaveBeenCalled();

    status.mockResolvedValue(settledStatus);
    emitFrame('final', 12, { text: 'Final may be incomplete' });
    await vi.advanceTimersByTimeAsync(0);
    expect(history).toHaveBeenCalledOnce();
    expect(getPendingQueue(sid)).toEqual([queued]);
    expect(messageStore.get().messages[sid][0].content).toBe('Prefix');

    historyResponse.resolve([{ role: 'assistant', content: 'Canonical complete response' }]);
    await vi.advanceTimersByTimeAsync(0);
    expect(messageStore.get().messages[sid][0].content).toBe('Canonical complete response');
    expect(getSessionStreamRecovery(sid)).toBeUndefined();
  });

  it('restores a pending followup carried outside the cursor without deduping it against the cut', async () => {
    vi.spyOn(backendApi, 'sessionStatus').mockResolvedValue({
      ...settledStatus, live: 'running', active_request_id: oldRequest,
    });
    emitFrame('delta', 1, { text: 'Prefix' });
    emitSocket({ type: 'open' });
    emitFrame('gateway_resync', 10, {
      reason: 'cursor_expired', floor: 6,
      pending_followups: [{
        kind: 'followup_question', session_id: sid, gateway_epoch: 'epoch-1',
        request_id: oldRequest, gateway_sequence: 5, sequence: 0, is_final: false,
        body: { question_id: 'q-recovered', questions: [{ id: 'q', question: 'Proceed?' }] },
      }],
    });
    await vi.advanceTimersByTimeAsync(0);

    expect(sessionStore.get().books[sid]?.pendingFollowup?.questionId).toBe('q-recovered');
    expect(getGatewayCursors([sid])[sid]?.sequence).toBe(10);
    expect(getSessionStreamRecovery(sid)).toBeDefined();
  });

  it('settles after restored followup even when no terminal frame arrives', async () => {
    const status = vi.spyOn(backendApi, 'sessionStatus').mockResolvedValue({
      ...settledStatus, live: 'running', active_request_id: oldRequest,
    });
    const history = vi.spyOn(backendApi, 'history').mockResolvedValue([{ role: 'assistant', content: 'Completed followup' }]);
    emitFrame('delta', 1, { text: 'Prefix' });
    emitSocket({ type: 'open' });
    emitFrame('gateway_resync', 10, {
      reason: 'cursor_expired', floor: 6,
      pending_followups: [{
        kind: 'followup_question', session_id: sid, request_id: oldRequest,
        sequence: 0, is_final: false,
        body: { question_id: 'q-recovered', questions: [{ id: 'q', question: 'Proceed?' }] },
      }],
    });
    await vi.advanceTimersByTimeAsync(0);
    emitFrame('followup_question', 11, { question_id: 'q-recovered', status: 'resolved' });
    expect(sessionStore.get().books[sid]?.pendingFollowup).toBeNull();
    status.mockResolvedValue(settledStatus);
    await vi.advanceTimersByTimeAsync(15_000);

    expect(history).toHaveBeenCalledOnce();
    expect(messageStore.get().messages[sid][0].content).toBe('Completed followup');
    expect(getSessionStreamRecovery(sid)).toBeUndefined();
  });

  it('preserves a verified pending followup over an idle durable history read', async () => {
    vi.spyOn(backendApi, 'sessionStatus').mockResolvedValue(settledStatus);
    const history = vi.spyOn(backendApi, 'history').mockResolvedValue([{ role: 'assistant', content: 'Previously completed' }]);
    emitFrame('delta', 1, { text: 'Prefix' });
    emitSocket({ type: 'open' });
    const queued = { id: 'queued', query: 'next', attachments: [] };
    enqueuePending(sid, queued);
    emitFrame('gateway_resync', 10, {
      reason: 'cursor_expired', floor: 6,
      pending_followups: [{
        kind: 'followup_question', session_id: sid, request_id: oldRequest,
        sequence: 0, is_final: false,
        body: { question_id: 'q-recovered', questions: [{ id: 'q', question: 'Proceed?' }] },
      }],
    });
    await vi.advanceTimersByTimeAsync(15_000);
    expect(history).toHaveBeenCalledOnce();
    expect(messageStore.get().messages[sid][0].content).toBe('Previously completed');
    expect(sessionStore.get().books[sid]?.pendingFollowup?.questionId).toBe('q-recovered');
    expect(getPendingQueue(sid)).toEqual([queued]);

    emitFrame('followup_question', 11, { question_id: 'q-recovered', status: 'resolved' });
    await vi.advanceTimersByTimeAsync(0);
    expect(history).toHaveBeenCalledTimes(2);
    expect(getSessionStreamRecovery(sid)).toBeUndefined();
  });

  it('discards a terminal history read when post-cut progress describes a new backend request', async () => {
    const status = vi.spyOn(backendApi, 'sessionStatus').mockResolvedValue(settledStatus);
    const response = Promise.withResolvers<Awaited<ReturnType<typeof backendApi.history>>>();
    const history = vi.spyOn(backendApi, 'history').mockReturnValue(response.promise);
    emitFrame('delta', 1, { text: 'Keep the partial during recovery' });
    emitSocket({ type: 'open' });
    emitFrame('gateway_resync', 10, { reason: 'cursor_expired', floor: 6 });
    await vi.advanceTimersByTimeAsync(0);
    expect(history).toHaveBeenCalledOnce();

    status.mockResolvedValue({ ...settledStatus, live: 'running', active_request_id: 'external-new-request' });
    emitFrame('status', 11, { message: 'Started' }, 'epoch-1', 'external-new-request');
    response.resolve([{ role: 'assistant', content: 'Outdated terminal history' }]);
    await vi.advanceTimersByTimeAsync(0);
    expect(messageStore.get().messages[sid][0].content).toBe('Keep the partial during recovery');
    expect(getSessionStreamRecovery(sid)).toBeDefined();
  });

  it('refetches after a second disconnect and discards the stale first connection history', async () => {
    vi.spyOn(backendApi, 'sessionStatus').mockResolvedValue(settledStatus);
    const oldHistory = Promise.withResolvers<Awaited<ReturnType<typeof backendApi.history>>>();
    const history = vi.spyOn(backendApi, 'history')
      .mockReturnValueOnce(oldHistory.promise)
      .mockResolvedValueOnce([{ role: 'assistant', content: 'New connection history' }]);
    emitFrame('delta', 1, { text: 'Prefix' });
    emitSocket({ type: 'open' });
    emitFrame('gateway_resync', 10, { reason: 'cursor_expired', floor: 6 });
    await vi.advanceTimersByTimeAsync(0);

    emitSocket({ type: 'close', code: 1012 });
    emitSocket({ type: 'open' });
    await vi.advanceTimersByTimeAsync(0);
    expect(history).toHaveBeenCalledTimes(2);
    expect(messageStore.get().messages[sid][0].content).toBe('New connection history');

    oldHistory.resolve([{ role: 'assistant', content: 'Outdated first connection history' }]);
    await vi.advanceTimersByTimeAsync(0);
    expect(messageStore.get().messages[sid][0].content).toBe('New connection history');
  });

  it('discards a delayed resync history after a user stops the affected request', async () => {
    vi.spyOn(backendApi, 'sessionStatus').mockResolvedValue(settledStatus);
    const response = Promise.withResolvers<Awaited<ReturnType<typeof backendApi.history>>>();
    const history = vi.spyOn(backendApi, 'history').mockReturnValue(response.promise);
    emitFrame('delta', 1, { text: 'Keep this partial' });
    emitSocket({ type: 'open' });
    emitFrame('gateway_resync', 10, { reason: 'cursor_expired', floor: 6 });
    await vi.advanceTimersByTimeAsync(0);
    expect(history).toHaveBeenCalledOnce();

    stopGeneration(sid);
    response.resolve([{ role: 'assistant', content: 'Stale recovery result' }]);
    await vi.advanceTimersByTimeAsync(0);
    expect(messageStore.get().messages[sid][0].content).toBe('Keep this partial');
    expect(getSessionStreamRecovery(sid)).toBeUndefined();
  });

  it.each([true, false])('recovers durable history when the interrupted request finished offline (foreground=%s)', async (active) => {
    if (!active) setActiveSessionId(null);
    const status = vi.spyOn(backendApi, 'sessionStatus').mockResolvedValue(settledStatus);
    const history = vi.spyOn(backendApi, 'history').mockResolvedValue([
      { role: 'user', content: 'Prompt' },
      { role: 'assistant', content: 'Completed while offline' },
    ]);

    disconnectAndReconnect();
    await vi.advanceTimersByTimeAsync(0);

    expect(status.mock.invocationCallOrder[0]).toBeLessThan(history.mock.invocationCallOrder[0]);
    expect(messageStore.get().messages[sid].map((message) => message.content)).toEqual([
      'Prompt', 'Completed while offline',
    ]);
    expect(sessionStore.get().books[sid]?.turnSealed).toBe(true);
    expect(sessionStore.get().busySessions[sid]).toBeFalsy();
  });

  it('does not treat a status transport error as a completed turn', async () => {
    vi.spyOn(backendApi, 'sessionStatus').mockRejectedValue(new Error('connection unavailable'));
    disconnectAndReconnect();
    await vi.advanceTimersByTimeAsync(0);
    expect(sessionStore.get().books[sid]?.activeRequestId).toBe(oldRequest);
    expect(sessionStore.get().busySessions[sid]).toBe(true);
    expect(messageStore.get().messages[sid][0].streaming).toBe(true);
  });

  it('discards a reconnect status response after a new request starts', async () => {
    const delayed = Promise.withResolvers<typeof settledStatus>();
    vi.spyOn(backendApi, 'sessionStatus').mockReturnValue(delayed.promise);
    const history = vi.spyOn(backendApi, 'history').mockResolvedValue([]);
    disconnectAndReconnect();
    openTurnForRequest(sid, 'req-new');
    delayed.resolve(settledStatus);
    await vi.advanceTimersByTimeAsync(0);

    expect(history).not.toHaveBeenCalled();
    expect(sessionStore.get().books[sid]?.activeRequestId).toBe('req-new');
    expect(sessionStore.get().books[sid]?.turnSealed).toBe(false);
  });

  it('keeps a user-stopped request closed when a reconnect status response arrives', async () => {
    const delayed = Promise.withResolvers<typeof settledStatus>();
    vi.spyOn(backendApi, 'sessionStatus').mockReturnValue(delayed.promise);
    disconnectAndReconnect();
    stopGeneration(sid);
    delayed.resolve({ ...settledStatus, live: 'running' });
    await vi.advanceTimersByTimeAsync(0);

    expect(sessionStore.get().books[sid]?.turnSealed).toBe(true);
    expect(sessionStore.get().busySessions[sid]).toBeFalsy();
    expect(messageStore.get().messages[sid][0].interrupted).toBe(true);
  });
});

describe('shouldSkipHistoryReloadOnReconnect', () => {
  it('renders each external session with its provider initial and stable pattern', () => {
    configStore.set({
      config: { external_agents: { enabled: true } } as never,
    });
    sessionStore.set({
      sessions: [
        {
          id: 'sid-kimi', title: 'Kimi session', updatedAt: 2, preview: '', badge: '', workspaceId: 'default',
          agentLabel: { provider: 'kimi', display_badge: 'K', name: 'Kimi' },
          agentBinding: { kind: 'external_agent', id: 'agent-kimi' },
        },
        {
          id: 'sid-codex', title: 'Codex session', updatedAt: 1, preview: '', badge: '', workspaceId: 'default',
          agentLabel: { provider: 'codex', display_badge: 'X', name: 'Codex' },
          agentBinding: { kind: 'external_agent', id: 'agent-codex' },
        },
      ],
    });
    const host = document.createElement('div');
    document.body.append(host);
    const view = createSessionHistoryView(host, {
      openSession: vi.fn(),
      createSession: vi.fn(),
      createWorkspace: vi.fn(),
      manageHistory: vi.fn(),
      openWorkspace: vi.fn(),
      refreshSessions: async () => undefined,
      retrySessions: async () => undefined,
      retryWorkspaces: async () => undefined,
      getLoadErrors: () => ({ sessions: null, workspaces: null }),
    });

    const kimi = host.querySelector('[data-session-open="sid-kimi"] [data-session-identity-icon]');
    const codex = host.querySelector('[data-session-open="sid-codex"] [data-session-identity-icon]');
    expect(kimi?.textContent).toBe('K');
    expect(kimi?.classList.contains('agent-provider-tone-0')).toBe(true);
    expect(codex?.textContent).toBe('X');
    expect(codex?.classList.contains('agent-provider-tone-1')).toBe(true);
    expect(host.querySelector('[data-session-identity-icon] image')).toBeNull();

    view.dispose();
  });

  it('renders Team sessions with the shared black-white dual Agent logo', () => {
    configStore.set({
      config: { external_agents: { enabled: true } } as never,
    });
    sessionStore.set({
      sessions: [{
        id: 'sid-team', title: 'Team session', updatedAt: 1, preview: '', badge: '', workspaceId: 'default',
        agentLabel: { provider: 'team', display_badge: 'T', name: '研发团队' },
        agentBinding: { kind: 'external_team', id: 'team-dev' },
      }],
    });
    const host = document.createElement('div');
    document.body.append(host);
    const view = createSessionHistoryView(host, {
      openSession: vi.fn(),
      createSession: vi.fn(),
      createWorkspace: vi.fn(),
      manageHistory: vi.fn(),
      openWorkspace: vi.fn(),
      refreshSessions: async () => undefined,
      retrySessions: async () => undefined,
      retryWorkspaces: async () => undefined,
      getLoadErrors: () => ({ sessions: null, workspaces: null }),
    });

    const identityIcon = host.querySelector('[data-session-open="sid-team"] [data-session-identity-icon]');
    expect(identityIcon?.querySelector('.session__team-logo')).not.toBeNull();
    expect(identityIcon?.querySelectorAll('.session__team-logo i')).toHaveLength(2);
    expect(identityIcon?.querySelector('svg')).toBeNull();

    view.dispose();
  });

  it('returns true when session is busy with an active request', () => {
    ensureSessionBook('sid-1');
    patchBook('sid-1', { activeRequestId: 'req-1', turnSealed: false, acceptingNewRequest: false });
    setBusy('sid-1', true);
    expect(shouldSkipHistoryReloadOnReconnect('sid-1')).toBe(true);
  });

  it('returns true when assistant message is still streaming', () => {
    messageStore.set({
      messages: {
        'sid-1': [{
          id: 'm-1',
          role: 'assistant',
          content: 'partial',
          timestamp: Date.now(),
          streaming: true,
        }],
      },
    });
    expect(shouldSkipHistoryReloadOnReconnect('sid-1')).toBe(true);
  });

  it('returns false for idle session with sealed turn', () => {
    ensureSessionBook('sid-1');
    patchBook('sid-1', { turnSealed: true, acceptingNewRequest: false, activeRequestId: null });
    expect(shouldSkipHistoryReloadOnReconnect('sid-1')).toBe(false);
  });
});
