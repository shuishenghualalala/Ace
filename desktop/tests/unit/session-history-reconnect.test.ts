/**
 * @vitest-environment happy-dom
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { BackendHistoryItem, BackendHistoryWindow } from '../../src/ui/api/feature-session';
import { backendApi } from '../../src/ui/backend-client';
import { bindBackendAuth, bootstrapBackend, loadBackendHistory, loadOlderHistoryPage, openSession, shouldSkipHistoryReloadOnReconnect } from '../../src/ui/features/session-controller';
import { createSessionHistoryView } from '../../src/ui/features/session-history-view';
import { firstChangedMessageIndex } from '../../src/ui/conversation-message-changes';
import { openTurnForRequest } from '../../src/ui/features/session-busy';
import { appendMessage, applyChunk, consumePending, renderChat, respondToFollowup, stopGeneration } from '../../src/ui/features/chat-controller';
import { disposeConversationRenderer, getConversationScrollAnchor } from '../../src/ui/features/conversation-renderer';
import { getGatewayCursors, resetGatewaySequence } from '../../src/ui/features/gateway-sequence';
import { cancelSessionStreamRecovery, getSessionStreamRecovery } from '../../src/ui/features/session-stream-recovery';
import { stopStreamWatchdog } from '../../src/ui/features/stream-watchdog';
import { authStore } from '../../src/ui/stores/auth-store';
import { chatOutbox } from '../../src/ui/features/chat-outbox';
import {
  __resetAllStoresForTest,
  configStore,
  messageStore,
  sessionStore,
  uiStore,
} from '../../src/ui/stores/stores';
import {
  addSubscribedSessions, enqueuePending, ensureSessionBook, getPendingQueue, patchBook,
  setBusy, setActiveSessionId, setEditFrom, setHistoryPaging, state,
} from '../../src/ui/state';

beforeEach(() => {
  __resetAllStoresForTest();
  document.body.innerHTML = '';
  setActiveSessionId('sid-1');
});

function historyResult(items: BackendHistoryItem[] = []): BackendHistoryWindow {
  return { items, has_more: false, next_before: null };
}

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

  function snapshotRequests() {
    return vi.mocked(window.Crew!.gatewayWsSend!).mock.calls.flatMap(([frame]) => {
      const candidate = frame as { repair_id?: string };
      return candidate.repair_id ? [candidate as { repair_id: string }] : [];
    });
  }

  it('repairs the canonical answer boundary when only the direct followup receipt survived', async () => {
    const status = vi.spyOn(backendApi, 'sessionStatus').mockResolvedValue({
      ...settledStatus, live: 'running', active_request_id: oldRequest,
    });
    vi.spyOn(backendApi, 'historyWindow').mockResolvedValue(historyResult([
      { role: 'user', content: 'Original prompt', request_id: oldRequest, history_id: 'original-user' },
      { role: 'assistant', content: 'Before question', request_id: oldRequest, history_id: 'before-question' },
      { role: 'user', content: '已选择：Recovered answer', request_id: oldRequest, history_id: 'followup:q-lost' },
      { role: 'assistant', content: 'After question', request_id: oldRequest, history_id: 'after-question' },
    ]));
    emitSocket({ type: 'open' });
    emitSocket({ type: 'message', data: JSON.stringify({
      kind: 'send_receipt_capability', sequence: 0, is_final: false,
      body: { version: 1, followup_receipts: 1, store_id: 'a'.repeat(32) },
    }) });
    emitFrame('delta', 1, { text: 'Before question' });
    emitFrame('followup_question', 2, {
      question_id: 'q-lost', record_history: true, questions: [{
        id: 'q', question: 'Confirm?', options: [{ label: 'Recovered answer', value: 'yes' }],
      }],
    });
    respondToFollowup(sid, 'q-lost', [{ question_id: 'q', answers: ['yes'] }]);
    const send = vi.mocked(window.Crew!.gatewayWsSend!);
    const submitted = send.mock.calls.map(([value]) => value as Record<string, unknown>)
      .find((value) => value.action === 'followup_answer')!;
    expect(submitted).toMatchObject({ request_id: oldRequest, question_id: 'q-lost' });
    // The ordered resolved frame was lost while live continuation resumed.
    emitFrame('delta', 3, { text: 'After question' });
    emitSocket({ type: 'message', data: JSON.stringify({
      kind: 'interaction_receipt', session_id: sid, request_id: oldRequest, sequence: 0, is_final: false,
      body: {
        type: 'followup', interaction_id: 'q-lost', submission_id: submitted.submission_id,
        status: 'applied', answer_history_id: 'followup:q-lost',
      },
    }) });
    expect(snapshotRequests()).toHaveLength(1);
    expect(getSessionStreamRecovery(sid)).toBeDefined();
    expect(messageStore.get().messages[sid]?.filter((message) => message.role === 'user')).toEqual([]);
    status.mockResolvedValue({ ...settledStatus, live: 'idle' });
    emitFrame('final', 5, { text: 'After question' });
    await vi.advanceTimersByTimeAsync(1);
    const rows = messageStore.get().messages[sid]!;
    expect(rows.filter((row) => row.historyId === 'followup:q-lost')).toHaveLength(1);
    expect(rows.map((row) => row.content)).toEqual([
      'Original prompt', 'Before question', '已选择：Recovered answer', 'After question',
    ]);
    expect(getSessionStreamRecovery(sid)).toBeUndefined();
  });

  it('retires a verified question when its original request ends during recovery', async () => {
    const status = vi.spyOn(backendApi, 'sessionStatus').mockResolvedValue({
      ...settledStatus, live: 'running', active_request_id: oldRequest,
    });
    vi.spyOn(backendApi, 'historyWindow').mockResolvedValue(historyResult([
      { role: 'assistant', content: 'Request ended without answering', request_id: oldRequest },
    ]));
    emitSocket({ type: 'open' });
    emitFrame('gateway_resync', 10, {
      reason: 'cursor_expired', floor: 6,
      pending_followups: [{
        ...snapshotFrame('followup_question', 9, {
          question_id: 'q-terminal', questions: [{ id: 'q', question: 'Answer?', options: [] }],
        }),
      }],
    });
    expect(sessionStore.get().books[sid]?.pendingFollowup?.questionId).toBe('q-terminal');
    expect(getSessionStreamRecovery(sid)?.verifiedFollowupId).toBe('q-terminal');
    status.mockResolvedValue({ ...settledStatus, live: 'idle' });
    emitFrame('final', 11, { text: 'Request ended without answering' });
    await vi.advanceTimersByTimeAsync(1);
    expect(sessionStore.get().books[sid]?.pendingFollowup).toBeNull();
    expect(getSessionStreamRecovery(sid)).toBeUndefined();
    expect(messageStore.get().messages[sid]?.at(-1)?.content).toBe('Request ended without answering');
  });

  it.each([false, true])('reconciles restored uncertain history without replacing a newer request (newer=%s)', async (newer) => {
    const history = vi.spyOn(backendApi, 'historyWindow').mockResolvedValue(historyResult([
      { role: 'user', content: 'Original prompt', request_id: oldRequest, history_id: 'original-user' },
      { role: 'user', content: '已补充：Recovered answer', request_id: oldRequest, history_id: 'followup:durable-answer' },
    ]));
    vi.spyOn(backendApi, 'sessionStatus').mockResolvedValue(settledStatus);
    state.socket?.dispose();
    state.socket = null;
    chatOutbox.reset();
    uiStore.set({ backendReadiness: { status: 'ready', generation: 1 } });
    Object.assign(window.Crew!, {
      chatOutboxList: vi.fn(async () => ({
        scope: 'owner-a',
        entries: [{
          requestId: oldRequest, sessionId: sid, phase: 'accepted',
          value: {
            storeId: 'a'.repeat(32),
            payload: {
              query: 'Original prompt', session_id: sid, request_id: oldRequest,
              mode: 'agent', workspace_id: 'default', send_receipt: true,
            },
          },
        }],
      })),
      chatOutboxPut: vi.fn(async () => undefined),
      chatOutboxRemove: vi.fn(async () => undefined),
    });
    const dispose = bindBackendAuth();
    try {
      await vi.advanceTimersByTimeAsync(1);
      expect(messageStore.get().messages[sid]?.some((row) => row.content === 'Original prompt')).toBe(true);
      emitSocket({ type: 'open', gatewayGeneration: 1 });
      emitSocket({ type: 'message', gatewayGeneration: 1, data: JSON.stringify({
        kind: 'send_receipt_capability', sequence: 0, is_final: false,
        body: { version: 1, followup_receipts: 1, store_id: 'a'.repeat(32) },
      }) });
      await vi.advanceTimersByTimeAsync(1);
      if (newer) {
        openTurnForRequest(sid, 'new-request');
        emitSocket({ type: 'message', gatewayGeneration: 1, data: JSON.stringify({
          kind: 'delta', session_id: sid, request_id: 'new-request',
          gateway_sequence: 3, gateway_epoch: 'epoch-1', sequence: 3, is_final: false, body: { text: 'New reply' },
        }) });
      }
      emitSocket({ type: 'message', gatewayGeneration: 1, data: JSON.stringify({
        kind: 'send_receipt', session_id: sid, request_id: oldRequest, sequence: 0, is_final: false,
        body: { status: 'uncertain' },
      }) });
      await vi.advanceTimersByTimeAsync(20);
      if (newer) {
        expect(history).not.toHaveBeenCalled();
        expect(sessionStore.get().books[sid]?.activeRequestId).toBe('new-request');
        expect(sessionStore.get().books[sid]?.turnSealed).toBe(false);
        expect(sessionStore.get().busySessions[sid]).toBe(true);
        const assistantId = sessionStore.get().books[sid]?.assistantId;
        expect(messageStore.get().messages[sid]?.find((row) => row.id === assistantId)?.content).toBe('New reply');
      } else {
        expect(history).toHaveBeenCalled();
        expect(messageStore.get().messages[sid]?.filter((row) => row.historyId === 'followup:durable-answer'))
          .toEqual([expect.objectContaining({ content: '已补充：Recovered answer' })]);
      }
    } finally {
      dispose();
      chatOutbox.reset();
    }
  });

  it('restores an accepted Plan execution and binds its request before querying delivery', async () => {
    const execution = 'plan-execution-restored';
    vi.spyOn(backendApi, 'sessionStatus').mockResolvedValue(settledStatus);
    vi.spyOn(backendApi, 'historyWindow').mockResolvedValue(historyResult([
      { role: 'user', content: 'Prepare a plan', request_id: oldRequest },
    ]));
    vi.mocked(backendApi.sessionPlan).mockResolvedValue({
      session_id: sid, active: false, awaiting_approval: false, has_plan: true,
      plan: '# Approved', plan_file: 'plan.md', version: 'plan-v2',
      status: 'approved', execution_request_id: execution, execution_status: 'accepted',
    });
    vi.spyOn(chatOutbox, 'transportIdentity').mockReturnValue({ scope: 'owner-a', storeId: 'a'.repeat(32) });
    emitSocket({ type: 'open' });
    await vi.advanceTimersByTimeAsync(1);
    vi.mocked(window.Crew!.gatewayWsSend!).mockImplementation(async (payload) => {
      const frame = payload as { action?: string; request_id?: string };
      if (frame.action === 'send_receipt_query' && frame.request_id === execution) {
        expect(sessionStore.get().books[sid]?.activeRequestId).toBe(execution);
      }
      return { ok: true };
    });
    await loadBackendHistory(sid);
    expect(sessionStore.get().books[sid]?.activeRequestId).toBe(execution);
    expect(window.Crew!.gatewayWsSend).toHaveBeenCalledWith({
      action: 'send_receipt_query', session_id: sid, request_id: execution,
    }, 'owner-a', 'a'.repeat(32));
    emitSocket({ type: 'message', data: JSON.stringify({
      kind: 'send_receipt', session_id: sid, request_id: execution, sequence: 0, is_final: false,
      body: { status: 'uncertain' },
    }) });
    expect(sessionStore.get().busySessions[sid]).toBe(false);
    expect(sessionStore.get().books[sid]?.turnSealed).toBe(true);
  });

  it('holds suspect text, retries one repair on a new socket, and accepts only its matching same-cut snapshot', () => {
    vi.spyOn(backendApi, 'sessionStatus').mockResolvedValue({ ...settledStatus, live: 'running', active_request_id: oldRequest });
    emitSocket({ type: 'open' });
    emitFrame('gateway_resync', 10, snapshotBody([snapshotFrame('delta', 8, { text: 'Last good text' })]));
    emitFrame('delta', 11, { text: 'Suspect prefix', delta_parts: [[11, 2]] });

    expect(snapshotRequests()).toEqual([expect.objectContaining({
      action: 'subscribe', sessions: [sid], force_snapshot_sessions: [sid], gateway_snapshots: true,
      gateway_cursors: { [sid]: { epoch: 'epoch-1', sequence: 10 } },
      last_gateway_sequences: { [sid]: 10 },
    })]);
    expect(getSessionStreamRecovery(sid)).toBeDefined();
    const firstRepair = snapshotRequests()[0]!.repair_id;
    emitFrame('delta', 11, { text: 'Another suspect frame', delta_parts: [[11, 3]] });
    expect(snapshotRequests()).toHaveLength(1);
    expect(messageStore.get().messages[sid].at(-1)?.content).toBe('Last good text');

    emitSocket({ type: 'close', code: 1012 });
    emitSocket({ type: 'open' });
    expect(snapshotRequests()).toHaveLength(2);
    const secondRepair = snapshotRequests()[1]!.repair_id;
    expect(secondRepair).not.toBe(firstRepair);
    const reply = {
      ...snapshotBody([snapshotFrame('delta', 8, { text: 'Authoritative prefix' })]),
      reason: 'client_stream_gap',
    };
    emitFrame('gateway_resync', 10, { ...reply, repair_id: firstRepair });
    emitFrame('gateway_resync', 9, { ...reply, repair_id: secondRepair });
    expect(messageStore.get().messages[sid].at(-1)?.content).toBe('Last good text');
    expect(getSessionStreamRecovery(sid)).toBeDefined();

    emitFrame('gateway_resync', 10, { ...reply, repair_id: secondRepair });
    expect(getSessionStreamRecovery(sid)).toBeUndefined();
    emitFrame('delta', 12, { text: 'Authoritative prefix + live', delta_parts: [[8, 20], [12, 7]] });
    expect(messageStore.get().messages[sid].at(-1)?.content).toBe('Authoritative prefix + live');
    expect(snapshotRequests()).toHaveLength(2);
  });

  it('routes queued history chunks through repair and uses durable history if no snapshot arrives', async () => {
    const status = vi.spyOn(backendApi, 'sessionStatus').mockResolvedValue({
      ...settledStatus, live: 'running', active_request_id: oldRequest,
    });
    const delayed = Promise.withResolvers<BackendHistoryWindow>();
    const history = vi.spyOn(backendApi, 'historyWindow').mockReturnValueOnce(delayed.promise)
      .mockResolvedValueOnce(historyResult([{ role: 'assistant', content: 'Durable completion' }]));
    emitFrame('delta', 1, { text: 'Partial' });
    emitSocket({ type: 'open' });
    const loading = loadBackendHistory(sid);
    emitFrame('delta', 2, { text: 'Broken replay', delta_parts: [[2, 1]] });
    expect(snapshotRequests()).toHaveLength(0);
    delayed.resolve(historyResult());
    await expect(loading).resolves.toBe(true);
    expect(getSessionStreamRecovery(sid)).toBeDefined();
    expect(snapshotRequests()).toHaveLength(1);
    expect(messageStore.get().messages[sid].at(-1)?.content).toBe('Partial');
    await vi.advanceTimersByTimeAsync(0);

    status.mockResolvedValue(settledStatus);
    emitFrame('final', 3, { text: 'Do not append a terminal suffix' });
    await vi.advanceTimersByTimeAsync(0);
    expect(getSessionStreamRecovery(sid)).toBeUndefined();
    expect(messageStore.get().messages[sid].at(-1)?.content).toBe('Durable completion');
    expect(history).toHaveBeenCalledTimes(2);
    expect(snapshotRequests()).toHaveLength(1);
  });

  it('rejects a matching repair cut older than the last text accepted before recovery', () => {
    vi.spyOn(backendApi, 'sessionStatus').mockResolvedValue({ ...settledStatus, live: 'running', active_request_id: oldRequest });
    emitSocket({ type: 'open' });
    emitFrame('gateway_resync', 10, snapshotBody([snapshotFrame('delta', 8, { text: 'Prefix' })]));
    emitFrame('delta', 14, { text: ' accepted' });
    emitFrame('delta', 15, { text: 'Invalid', delta_parts: [[15, 1]] });
    const repairId = snapshotRequests()[0]!.repair_id;
    emitFrame('gateway_resync', 12, {
      ...snapshotBody([snapshotFrame('delta', 8, { text: 'Stale prefix' })]), reason: 'client_stream_gap', repair_id: repairId,
    });
    expect(getSessionStreamRecovery(sid)).toBeDefined();
    expect(messageStore.get().messages[sid].at(-1)?.content).toBe('Prefix accepted');
    expect(getGatewayCursors([sid])).toEqual({ [sid]: { epoch: 'epoch-1', sequence: 14 } });
  });

  it('atomically projects the active snapshot, preserves local input and queue, and extends the seeded text', async () => {
    const status = Promise.withResolvers<typeof settledStatus>();
    vi.spyOn(backendApi, 'sessionStatus').mockReturnValue(status.promise);
    const history = vi.spyOn(backendApi, 'historyWindow')
      .mockResolvedValue(historyResult([{ role: 'assistant', content: 'Stale terminal response' }]));
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
      const history = vi.spyOn(backendApi, 'historyWindow').mockResolvedValue(historyResult());
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
    const history = vi.spyOn(backendApi, 'historyWindow')
      .mockResolvedValue(historyResult([{ role: 'assistant', content: 'Canonical answer' }]));
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
    const historyResponse = Promise.withResolvers<BackendHistoryWindow>();
    const history = vi.spyOn(backendApi, 'historyWindow').mockReturnValue(historyResponse.promise);
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

    historyResponse.resolve(historyResult([{ role: 'assistant', content: 'Canonical complete response' }]));
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
    const history = vi.spyOn(backendApi, 'historyWindow')
      .mockResolvedValue(historyResult([{ role: 'assistant', content: 'Completed followup' }]));
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
    const history = vi.spyOn(backendApi, 'historyWindow')
      .mockResolvedValue(historyResult([{ role: 'assistant', content: 'Previously completed' }]));
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
    const response = Promise.withResolvers<BackendHistoryWindow>();
    const history = vi.spyOn(backendApi, 'historyWindow').mockReturnValue(response.promise);
    emitFrame('delta', 1, { text: 'Keep the partial during recovery' });
    emitSocket({ type: 'open' });
    emitFrame('gateway_resync', 10, { reason: 'cursor_expired', floor: 6 });
    await vi.advanceTimersByTimeAsync(0);
    expect(history).toHaveBeenCalledOnce();

    status.mockResolvedValue({ ...settledStatus, live: 'running', active_request_id: 'external-new-request' });
    emitFrame('status', 11, { message: 'Started' }, 'epoch-1', 'external-new-request');
    response.resolve(historyResult([{ role: 'assistant', content: 'Outdated terminal history' }]));
    await vi.advanceTimersByTimeAsync(0);
    expect(messageStore.get().messages[sid][0].content).toBe('Keep the partial during recovery');
    expect(getSessionStreamRecovery(sid)).toBeDefined();
  });

  it('refetches after a second disconnect and discards the stale first connection history', async () => {
    vi.spyOn(backendApi, 'sessionStatus').mockResolvedValue(settledStatus);
    const oldHistory = Promise.withResolvers<BackendHistoryWindow>();
    const history = vi.spyOn(backendApi, 'historyWindow')
      .mockReturnValueOnce(oldHistory.promise)
      .mockResolvedValueOnce(historyResult([{ role: 'assistant', content: 'New connection history' }]));
    emitFrame('delta', 1, { text: 'Prefix' });
    emitSocket({ type: 'open' });
    emitFrame('gateway_resync', 10, { reason: 'cursor_expired', floor: 6 });
    await vi.advanceTimersByTimeAsync(0);

    emitSocket({ type: 'close', code: 1012 });
    emitSocket({ type: 'open' });
    await vi.advanceTimersByTimeAsync(0);
    expect(history).toHaveBeenCalledTimes(2);
    expect(messageStore.get().messages[sid][0].content).toBe('New connection history');

    oldHistory.resolve(historyResult([{ role: 'assistant', content: 'Outdated first connection history' }]));
    await vi.advanceTimersByTimeAsync(0);
    expect(messageStore.get().messages[sid][0].content).toBe('New connection history');
  });

  it('discards a delayed resync history after a user stops the affected request', async () => {
    vi.spyOn(backendApi, 'sessionStatus').mockResolvedValue(settledStatus);
    const response = Promise.withResolvers<BackendHistoryWindow>();
    const history = vi.spyOn(backendApi, 'historyWindow').mockReturnValue(response.promise);
    emitFrame('delta', 1, { text: 'Keep this partial' });
    emitSocket({ type: 'open' });
    emitFrame('gateway_resync', 10, { reason: 'cursor_expired', floor: 6 });
    await vi.advanceTimersByTimeAsync(0);
    expect(history).toHaveBeenCalledOnce();

    stopGeneration(sid);
    response.resolve(historyResult([{ role: 'assistant', content: 'Stale recovery result' }]));
    await vi.advanceTimersByTimeAsync(0);
    expect(messageStore.get().messages[sid][0].content).toBe('Keep this partial');
    expect(getSessionStreamRecovery(sid)).toBeUndefined();
  });

  it.each([true, false])('recovers durable history when the interrupted request finished offline (foreground=%s)', async (active) => {
    if (!active) setActiveSessionId(null);
    const status = vi.spyOn(backendApi, 'sessionStatus').mockResolvedValue(settledStatus);
    const history = vi.spyOn(backendApi, 'historyWindow').mockResolvedValue(historyResult([
      { role: 'user', content: 'Prompt' },
      { role: 'assistant', content: 'Completed while offline' },
    ]));

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
    const history = vi.spyOn(backendApi, 'historyWindow').mockResolvedValue(historyResult());
    disconnectAndReconnect();
    openTurnForRequest(sid, 'req-new');
    delayed.resolve(settledStatus);
    await vi.advanceTimersByTimeAsync(0);

    expect(history).not.toHaveBeenCalled();
    expect(sessionStore.get().books[sid]?.activeRequestId).toBe('req-new');
    expect(sessionStore.get().books[sid]?.turnSealed).toBe(false);
  });

  it('sends only the negotiated request and keeps a late unconfirmed tool on its original row', async () => {
    const store = 'b'.repeat(32);
    vi.spyOn(chatOutbox, 'transportIdentity').mockReturnValue({ scope: 'owner-a', storeId: store });
    emitSocket({ type: 'open' });
    emitSocket({ type: 'message', data: JSON.stringify({ kind: 'send_receipt_capability', body: { version: 1, request_stop: 1, store_id: store } }) });
    emitFrame('delta', 1, { text: 'Original' });
    const original = messageStore.get().messages[sid][0]!;
    stopGeneration(sid);
    expect(window.Crew!.gatewayWsSend).toHaveBeenCalledWith(
      { action: 'stop', session_id: sid, request_id: oldRequest }, 'owner-a', store,
    );
    openTurnForRequest(sid, 'next-request');
    setBusy(sid, true);
    emitFrame('tool', 2, { phase: 'result', result_metadata: { code: 'tool_exit_unconfirmed', details: { exit_confirmed: false } } });
    expect(messageStore.get().messages[sid].find((row) => row.id === original.id)?.stopNotice).toContain('实际退出尚未确认');
    expect(sessionStore.get().books[sid]?.activeRequestId).toBe('next-request');
    expect(sessionStore.get().busySessions[sid]).toBe(true);
  });

  it('keeps late ordinary replay out of post-stop Goal repair while honoring the exact Stop terminal', async () => {
    const goalId = `goal_${'c'.repeat(32)}`;
    const store = 'b'.repeat(32);
    authStore.set({ userInfo: { uid: 'post-stop-test-owner' } });
    vi.spyOn(chatOutbox, 'transportIdentity').mockReturnValue({ scope: 'owner-a', storeId: store });
    const status = vi.spyOn(backendApi, 'sessionStatus').mockResolvedValue({
      ...settledStatus, live: 'running', active_request_id: oldRequest,
    });
    emitSocket({ type: 'open' });
    emitSocket({ type: 'message', data: JSON.stringify({ kind: 'send_receipt_capability', body: { version: 1, request_stop: 1, store_id: store } }) });
    await vi.advanceTimersByTimeAsync(0);
    emitFrame('delta', 1, { text: 'Frozen ordinary output' });
    stopGeneration(sid);
    status.mockResolvedValue({ ...settledStatus, live: 'running', active_request_id: goalId });
    emitFrame('delta', 2, { text: 'Current Goal output' }, 'epoch-1', goalId);
    await vi.advanceTimersByTimeAsync(0);
    const recovery = getSessionStreamRecovery(sid);
    expect(recovery).toBeDefined();
    const previousProgress = recovery?.progress;
    const repair = snapshotRequests().at(-1);
    expect(repair?.repair_id).toBeTruthy();

    emitFrame('delta', 3, { text: 'Stale ordinary replay' });
    emitSocket({ type: 'message', data: JSON.stringify({
      kind: 'delta', session_id: sid, gateway_epoch: 'epoch-1', gateway_sequence: 4, sequence: 4,
      body: { text: 'Anonymous late ordinary replay' }, is_final: false,
    }) });
    emitSocket({ type: 'message', data: JSON.stringify({
      kind: 'error', session_id: sid, request_id: oldRequest, gateway_epoch: 'epoch-1', gateway_sequence: 5, sequence: 5,
      body: { outcome: { status: 'stopped', reason: 'user_stop' } }, is_final: true,
    }) });
    expect(getSessionStreamRecovery(sid)?.progress).toBe(previousProgress);
    expect(messageStore.get().messages[sid][0]?.turnOutcome?.status).toBe('stopped');
    expect(sessionStore.get().books[sid]?.activeRequestId).toBe(goalId);

    emitFrame('gateway_resync', 6, {
      ...snapshotBody(), reason: 'client_stream_gap', repair_id: repair!.repair_id,
      turn_snapshot: {
        request_id: goalId, started_at: 1_700_000_000,
        history: {
          ...historyResult([{ role: 'assistant', content: 'Frozen ordinary output', request_id: oldRequest },
            { role: 'user', content: 'Canonical Goal prompt', request_id: goalId }]),
        },
        frames: [{ ...snapshotFrame('delta', 2, { text: 'Current Goal output' }), request_id: goalId }],
      },
    }, 'epoch-1', goalId);
    await vi.advanceTimersByTimeAsync(0);
    expect(getSessionStreamRecovery(sid)).toBeUndefined();
    expect(messageStore.get().messages[sid].map(({ content }) => content)).toEqual([
      'Frozen ordinary output', 'Canonical Goal prompt', 'Current Goal output',
    ]);
    expect(sessionStore.get().busySessions[sid]).toBe(true);
  });

  it('discards a stopped click if another request starts while the original outbox withdrawal is pending', async () => {
    const deferred = Promise.withResolvers<void>();
    const withdraw = vi.spyOn(chatOutbox, 'withdraw').mockReturnValue(deferred.promise);
    Object.assign(window.Crew!, {
      chatOutboxList: vi.fn(), chatOutboxPut: vi.fn(), chatOutboxRemove: vi.fn(),
    });
    emitSocket({ type: 'open' });
    stopGeneration(sid);
    expect(withdraw).toHaveBeenCalledWith(sid, oldRequest);
    openTurnForRequest(sid, 'next-request');
    deferred.resolve();
    await vi.advanceTimersByTimeAsync(0);
    expect(sessionStore.get().books[sid]?.activeRequestId).toBe('next-request');
    expect(sessionStore.get().busySessions[sid]).toBe(true);
    expect(vi.mocked(window.Crew!.gatewayWsSend!).mock.calls.some(([frame]) => (frame as { action?: string }).action === 'stop')).toBe(false);
  });

  it('shows only the matching terminal stopped outcome without reopening the locally stopped stream', async () => {
    vi.spyOn(backendApi, 'sessionStatus').mockResolvedValue(settledStatus);
    vi.spyOn(backendApi, 'historyWindow').mockResolvedValue(historyResult());
    emitSocket({ type: 'open' });
    emitFrame('delta', 1, { text: 'Keep stopped partial text' });
    const original = messageStore.get().messages[sid][0];
    stopGeneration(sid);

    const stopped = (request: string, sequence: number, isFinal: boolean, outcome: object) => emitSocket({
      type: 'message',
      data: JSON.stringify({
        kind: 'error', session_id: sid, request_id: request, gateway_epoch: 'epoch-1',
        gateway_sequence: sequence, sequence, is_final: isFinal,
        body: { text: 'Must not replace the frozen renderer text', outcome },
      }),
    });
    stopped('unrelated', 2, true, { status: 'stopped', reason: 'user_stop' });
    stopped(oldRequest, 3, false, { status: 'stopped', reason: 'user_stop' });
    stopped(oldRequest, 4, true, { status: 'failed', reason: 'provider_error' });
    emitFrame('delta', 5, { text: 'Late model text must stay suppressed' });
    expect(messageStore.get().messages[sid][0]?.turnOutcome).toBeUndefined();

    stopped(oldRequest, 6, true, { status: 'stopped', reason: 'user_stop' });
    stopped(oldRequest, 7, true, { status: 'stopped', reason: 'duplicate' });
    await vi.advanceTimersByTimeAsync(0);

    expect(messageStore.get().messages[sid]).toEqual([expect.objectContaining({
      id: original.id, content: 'Keep stopped partial text', streaming: false, interrupted: true,
      requestId: oldRequest, turnOutcome: { status: 'stopped', reason: 'user_stop' },
    })]);
    expect(sessionStore.get().books[sid]?.pendingStopOutcome).toBeUndefined();
    expect(sessionStore.get().books[sid]?.activeRequestId).toBeNull();
    expect(sessionStore.get().books[sid]?.turnSealed).toBe(true);
    expect(sessionStore.get().busySessions[sid]).toBeFalsy();
    expect(state.suppressChunks.has(sid)).toBe(true);
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

describe('older history page viewport', () => {
  const sid = 'sid-1';
  let container: HTMLElement;

  beforeEach(() => {
    authStore.set({ isLoggedIn: true });
    document.body.innerHTML = '<div id="welcome-panel"></div><div id="chat-panel"><div id="chat-messages"></div></div>';
    container = document.getElementById('chat-messages')!;
    appendMessage(sid, 'user', 'First current row');
    appendMessage(sid, 'user', 'Reading row');
    setHistoryPaging(sid, { hasMore: true, nextBefore: 'previous-page' });
    Object.defineProperties(container, {
      scrollHeight: { get: () => (state.messages[sid].length === 2 ? 1000 : 2800) },
      clientHeight: { value: 300 },
    });
    vi.spyOn(container, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 70, 500, 300));
    renderChat();
  });

  afterEach(() => {
    disposeConversationRenderer('chat-messages');
    authStore.set({ isLoggedIn: false });
    vi.restoreAllMocks();
  });

  it('retains the visible message after an in-flight scroll and unrelated total-height changes', async () => {
    const history = Promise.withResolvers<Awaited<ReturnType<typeof backendApi.historyWindow>>>();
    vi.spyOn(backendApi, 'historyWindow').mockReturnValue(history.promise);
    const [first, reading] = container.querySelectorAll<HTMLElement>('.msg[data-message-id]');
    vi.spyOn(first, 'getBoundingClientRect').mockImplementation(() => new DOMRect(0, 70 - container.scrollTop, 500, 100));
    vi.spyOn(reading, 'getBoundingClientRect').mockImplementation(() => {
      const actualPrependHeight = state.messages[sid].length === 2 ? 0 : 180;
      return new DOMRect(0, 70 + 155 + actualPrependHeight - container.scrollTop, 500, 120);
    });
    container.scrollTop = 30;
    getConversationScrollAnchor(container.id).disarm();
    const loading = loadOlderHistoryPage(sid);
    container.scrollTop = 130;
    const offset = reading.getBoundingClientRect().top - container.getBoundingClientRect().top;
    // Chromium can apply part of the prepend shift before our geometry read.
    window.addEventListener('messages:changed', () => { container.scrollTop += 75; }, { once: true });
    history.resolve({ items: [{ role: 'user', content: 'Older page' }], has_more: false, next_before: null });

    await expect(loading).resolves.toBe(true);
    expect(container.contains(reading)).toBe(true);
    expect(reading.getBoundingClientRect().top - container.getBoundingClientRect().top).toBe(offset);
    expect(container.scrollTop).toBe(310);
    expect(container.scrollHeight).toBe(2800);
  });

  it('keeps sticky-bottom policy in charge of the scroll position', async () => {
    vi.spyOn(backendApi, 'historyWindow').mockResolvedValue({
      items: [{ role: 'user', content: 'Older page' }], has_more: false, next_before: null,
    });
    const anchor = getConversationScrollAnchor(container.id);
    container.scrollTop = 700;

    await expect(loadOlderHistoryPage(sid)).resolves.toBe(true);
    expect(anchor.isStickyBottom()).toBe(true);
    // The renderer will pin to bottom on its usual next frame.
    expect(container.scrollTop).toBe(700);
  });
});

describe('session todo hydration', () => {
  const sid = 'sid-1';
  const previous = [{ id: 'old', content: 'Earlier task', status: 'pending' as const }];
  const latest = [{ id: 'new', content: 'Current task', status: 'in_progress' as const }];

  beforeEach(() => {
    authStore.set({ isLoggedIn: true });
    vi.spyOn(backendApi, 'historyWindow').mockResolvedValue(historyResult([
      { role: 'assistant', content: 'Restored reply', history_id: 'reply' },
    ]));
    vi.spyOn(backendApi, 'sessionStatus').mockResolvedValue({
      session_id: sid, live: 'idle', queue_depth: 0, last_status: 'completed', last_error: '',
    });
    vi.spyOn(backendApi, 'sessionPlan').mockRejectedValue(new Error('unavailable'));
    vi.spyOn(backendApi, 'getSessionModel').mockRejectedValue(new Error('unavailable'));
    vi.spyOn(backendApi, 'getSessionAgentConfig').mockRejectedValue(new Error('unavailable'));
    vi.spyOn(backendApi, 'sessionContext').mockRejectedValue(new Error('unavailable'));
    vi.spyOn(backendApi, 'sessionTodos').mockResolvedValue({ todos: latest });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    authStore.set({ isLoggedIn: false });
  });

  it('retains the direct history snapshot after resetting the idle book', async () => {
    await expect(loadBackendHistory(sid)).resolves.toBe(true);
    expect(backendApi.sessionTodos).toHaveBeenCalledExactlyOnceWith(sid);
    expect(sessionStore.get().books[sid].todos).toEqual(latest);
    expect(messageStore.get().messages[sid].at(-1)?.todoSnapshot).toEqual(latest);
  });

  it.each([{ name: 'items', todos: latest }, { name: 'empty', todos: [] }])(
    'cold open applies the authoritative $name list from a single read',
    async ({ todos }) => {
      patchBook(sid, { todos: previous });
      vi.mocked(backendApi.sessionTodos).mockResolvedValue({ todos });
      await openSession(sid);
      expect(backendApi.sessionTodos).toHaveBeenCalledExactlyOnceWith(sid);
      expect(sessionStore.get().books[sid].todos).toEqual(todos);
    },
  );

  it.each([true, false])('retries an unavailable first read, recovery succeeds=%s', async (recovers) => {
    patchBook(sid, { todos: previous });
    const read = vi.mocked(backendApi.sessionTodos).mockRejectedValueOnce(new Error('offline'));
    if (!recovers) read.mockRejectedValueOnce(new Error('still offline'));
    await openSession(sid);
    expect(read).toHaveBeenCalledTimes(2);
    expect(sessionStore.get().books[sid].todos).toEqual(recovers ? latest : previous);
  });

  it('refreshes todos exactly once on a cached reopen without fetching history', async () => {
    appendMessage(sid, 'assistant', 'Cached reply');
    patchBook(sid, { todos: previous });
    await openSession(sid);
    expect(backendApi.historyWindow).not.toHaveBeenCalled();
    expect(backendApi.sessionTodos).toHaveBeenCalledExactlyOnceWith(sid);
    expect(sessionStore.get().books[sid].todos).toEqual(latest);
  });

  it.each(['navigate', 'reload', 'live'] as const)('rejects a cached open response after %s', async (action) => {
    appendMessage(sid, 'assistant', 'Cached reply');
    patchBook(sid, { todos: previous });
    const started = Promise.withResolvers<void>();
    const pending = Promise.withResolvers<Awaited<ReturnType<typeof backendApi.sessionTodos>>>();
    vi.mocked(backendApi.sessionTodos).mockImplementationOnce(() => {
      started.resolve();
      return pending.promise;
    });
    const opening = openSession(sid);
    await started.promise;
    if (action === 'navigate') {
      setActiveSessionId('sid-2');
      setActiveSessionId(sid);
    } else if (action === 'reload') {
      await expect(loadBackendHistory(sid)).resolves.toBe(true);
    } else {
      applyChunk({ kind: 'todo_updated', session_id: sid, sequence: 1, is_final: false, body: { todos: latest } });
      expect(sessionStore.get().books[sid].todos).toEqual(latest);
    }
    pending.resolve({ todos: [] });
    await opening;
    expect(sessionStore.get().books[sid].todos).toEqual(action === 'navigate' ? previous : latest);
  });
});

describe('versioned history windows', () => {
  const sid = 'sid-1';
  const initial: BackendHistoryWindow = {
    mode: 'snapshot', revision: 'rev-1', head: 'head-1', has_more: true, next_before: 'before-1',
    items: [
      { role: 'user', content: 'same question', history_id: 'q2' },
      { role: 'assistant', content: 'same answer', history_id: 'a2' },
    ],
  };
  const older: BackendHistoryWindow = {
    mode: 'page', revision: 'rev-1', head: 'head-ahead', has_more: false, next_before: null,
    items: [
      { role: 'user', content: 'same question', history_id: 'q1' },
      { role: 'assistant', content: 'same answer', history_id: 'a1' },
    ],
  };

  beforeEach(() => {
    authStore.set({ isLoggedIn: true });
    vi.spyOn(backendApi, 'sessionStatus').mockResolvedValue({
      session_id: sid, live: 'idle', queue_depth: 0, last_status: 'completed', last_error: '',
    });
    vi.spyOn(backendApi, 'sessionPlan').mockRejectedValue(new Error('plan endpoint unavailable'));
    vi.spyOn(backendApi, 'sessionTodos').mockResolvedValue({ todos: [] });
  });

  afterEach(() => {
    authStore.set({ isLoggedIn: false });
    vi.restoreAllMocks();
  });

  it('keeps the loaded prefix and append checkpoint when a page observes a newer head', async () => {
    const fullHistory = vi.spyOn(backendApi, 'history');
    const readWindow = vi.spyOn(backendApi, 'historyWindow')
      .mockResolvedValueOnce(initial)
      .mockResolvedValueOnce(older)
      .mockResolvedValueOnce({
        ...historyResult([
          { role: 'user', content: 'new question', history_id: 'q3' },
          { role: 'assistant', content: 'new answer', history_id: 'a3' },
        ]),
        mode: 'append', revision: 'rev-1', head: 'head-3',
      });
    await expect(loadBackendHistory(sid)).resolves.toBe(true);
    const initialRows = messageStore.get().messages[sid];
    await expect(loadOlderHistoryPage(sid)).resolves.toBe(true);
    expect(readWindow).toHaveBeenLastCalledWith(sid, { limit: 100, before: 'before-1', revision: 'rev-1' });
    expect(messageStore.get().historyPaging[sid].head).toBe('head-1');
    const sent = appendMessage(sid, 'user', 'new question');
    appendMessage(sid, 'assistant', 'Locally replayed');
    const pending = appendMessage(sid, 'user', 'still queued', { requestId: 'queued-request' });
    enqueuePending(sid, { id: 'queued', query: pending.content, attachments: [], optimisticUserMessageId: pending.id });
    const beforeAppend = messageStore.get().messages[sid];

    const seen: Array<string | undefined> = [];
    const unsubscribe = messageStore.subscribe((next, previous) => {
      if (next.historyPaging[sid] !== previous.historyPaging[sid]) {
        seen.push(next.messages[sid].find((row) => row.historyId === next.historyPaging[sid].lastHistoryId)?.content);
      }
    });
    await expect(loadBackendHistory(sid)).resolves.toBe(true);
    unsubscribe();

    expect(readWindow).toHaveBeenLastCalledWith(sid, { limit: 100, after: 'head-1', revision: 'rev-1' });
    expect(fullHistory).not.toHaveBeenCalled();
    const rows = messageStore.get().messages[sid];
    expect(rows.map((row) => row.historyId)).toEqual(['q1', 'a1', 'q2', 'a2', 'q3', 'a3', undefined]);
    expect(rows[2]).toBe(initialRows[0]);
    expect(firstChangedMessageIndex(beforeAppend, rows)).toBe(4);
    expect(rows.some((row) => row.id === sent.id)).toBe(false);
    expect(rows.at(-1)).toBe(pending);
    expect(seen).toEqual(['new answer']);
  });

  it('discards an old page after a replacement and accepts the latest version atomically', async () => {
    const stale = Promise.withResolvers<BackendHistoryWindow>();
    const readWindow = vi.spyOn(backendApi, 'historyWindow')
      .mockResolvedValueOnce(initial)
      .mockReturnValueOnce(stale.promise)
      .mockResolvedValueOnce({
        ...historyResult([{ role: 'assistant', content: 'new fork', history_id: 'forked' }]),
        mode: 'snapshot', revision: 'rev-2', head: 'head-2', has_more: true, next_before: 'before-2',
      });
    await expect(loadBackendHistory(sid)).resolves.toBe(true);
    const oldPage = loadOlderHistoryPage(sid);
    await expect(loadBackendHistory(sid)).resolves.toBe(true);
    stale.resolve(older);
    await expect(oldPage).resolves.toBe(false);
    expect(readWindow).toHaveBeenLastCalledWith(sid, { limit: 100, after: 'head-1', revision: 'rev-1' });
    expect(messageStore.get().messages[sid].map((row) => row.historyId)).toEqual(['forked']);
    expect(messageStore.get().historyPaging[sid]).toEqual({
      hasMore: true, nextBefore: 'before-2', revision: 'rev-2', head: 'head-2', lastHistoryId: 'forked',
    });
  });

  it('applies a replacement returned by a stale server cursor without another history request', async () => {
    const readWindow = vi.spyOn(backendApi, 'historyWindow')
      .mockResolvedValueOnce(initial)
      .mockResolvedValueOnce({
        ...historyResult([{ role: 'user', content: 'rewound prompt', history_id: 'root' }]),
        mode: 'snapshot', revision: 'rev-2', head: 'head-2',
      });
    await expect(loadBackendHistory(sid)).resolves.toBe(true);
    await expect(loadOlderHistoryPage(sid)).resolves.toBe(true);
    expect(readWindow).toHaveBeenCalledTimes(2);
    expect(messageStore.get().messages[sid].map((row) => row.content)).toEqual(['rewound prompt']);
    expect(messageStore.get().historyPaging[sid].revision).toBe('rev-2');
  });

  it('rejects responses after an A → B → A navigation or a replaced cursor', async () => {
    const switched = Promise.withResolvers<BackendHistoryWindow>();
    const replaced = Promise.withResolvers<BackendHistoryWindow>();
    vi.spyOn(backendApi, 'historyWindow')
      .mockResolvedValueOnce(initial)
      .mockReturnValueOnce(switched.promise)
      .mockReturnValueOnce(replaced.promise);
    await expect(loadBackendHistory(sid)).resolves.toBe(true);
    const afterSwitch = loadOlderHistoryPage(sid);
    setActiveSessionId('sid-2');
    setActiveSessionId(sid);
    switched.resolve(older);
    await expect(afterSwitch).resolves.toBe(false);

    const afterCursor = loadOlderHistoryPage(sid);
    setHistoryPaging(sid, { ...messageStore.get().historyPaging[sid], nextBefore: 'different-before' });
    replaced.resolve(older);
    await expect(afterCursor).resolves.toBe(false);
    expect(messageStore.get().messages[sid].map((row) => row.historyId)).toEqual(['q2', 'a2']);
    expect(messageStore.get().historyPaging[sid].nextBefore).toBe('different-before');
  });

  it('keeps an edit attached to the same user message when a page is prepended', async () => {
    vi.spyOn(backendApi, 'historyWindow').mockResolvedValueOnce(initial).mockResolvedValueOnce(older);
    await expect(loadBackendHistory(sid)).resolves.toBe(true);
    const edit = messageStore.get().messages[sid][0];
    setEditFrom(sid, 0);
    await expect(loadOlderHistoryPage(sid)).resolves.toBe(true);
    const newIndex = sessionStore.get().editFromIdx[sid];
    expect(newIndex).toBe(2);
    expect(messageStore.get().messages[sid][newIndex!]).toBe(edit);
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
