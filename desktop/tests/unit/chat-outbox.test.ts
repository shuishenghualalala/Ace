import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { DesktopChatOutboxEntry, DesktopChatOutboxSnapshot } from '../../src/shared/chat-outbox';
import type { ChatChunk } from '../../src/ui/api/transport';
import { ChatOutbox, type OutboxTurn } from '../../src/ui/features/chat-outbox';

const scope = 'owner-one';
const storeId = '1'.repeat(32);
const otherStoreId = '2'.repeat(32);

const turn: OutboxTurn = {
  userMessageId: 'user-one',
  payload: {
    session_id: 'session-one',
    request_id: 'request-one',
    query: 'inspect this attachment',
    workspace_id: 'workspace-one',
    mode: 'agent',
    send_receipt: true,
    attachments: [{ id: 'file-one', name: 'note.txt', type: 'file', path: 'uploads/file-one' }],
  },
};

function receipt(status: string, sessionId = turn.payload.session_id): ChatChunk {
  return {
    kind: 'send_receipt', body: { status }, sequence: 0, is_final: false,
    session_id: sessionId, request_id: turn.payload.request_id,
  };
}

function capability(id = storeId): ChatChunk {
  return { kind: 'send_receipt_capability', body: { version: 1, store_id: id }, sequence: 0, is_final: false };
}

function journal(entries: DesktopChatOutboxEntry[] = []) {
  const records = new Map(entries.map((entry) => [entry.requestId, entry]));
  return {
    records,
    chatOutboxList: vi.fn(async (): Promise<DesktopChatOutboxSnapshot> => ({ scope, entries: [...records.values()] })),
    chatOutboxPut: vi.fn(async (_scope: string, entry: DesktopChatOutboxEntry) => {
      records.set(entry.requestId, structuredClone(entry));
    }),
    chatOutboxRemove: vi.fn(async (_scope: string, requestId: string) => { records.delete(requestId); }),
  };
}

function saved(phase: 'submitted' | 'accepted' | 'withdrawing'): DesktopChatOutboxEntry {
  return {
    requestId: turn.payload.request_id, sessionId: turn.payload.session_id, phase,
    value: { ...turn, storeId },
  };
}

describe('native chat send journal', () => {
  let outbox: ChatOutbox;
  let observer: { onRestored: ReturnType<typeof vi.fn>; onReceipt: ReturnType<typeof vi.fn> };
  let setReady: ReturnType<typeof vi.fn>;
  let send: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.useFakeTimers();
    setReady = vi.fn();
    observer = { onRestored: vi.fn(), onReceipt: vi.fn() };
    send = vi.fn(async () => true);
    outbox = new ChatOutbox(setReady, 40);
  });

  afterEach(() => {
    outbox.reset();
    vi.useRealTimers();
  });

  it('requires a valid handshake and persists acceptance before settling the live composer', async () => {
    const bridge = journal();
    await outbox.bind(bridge, observer);
    outbox.connect(send);
    outbox.handle(capability(''));
    await expect(outbox.submit(turn)).resolves.toBe('rejected');
    expect(send).not.toHaveBeenCalled();

    outbox.handle(capability());
    expect(setReady).toHaveBeenLastCalledWith(true);
    const allowStage = Promise.withResolvers<void>();
    bridge.chatOutboxPut.mockImplementationOnce(() => allowStage.promise);
    const settled = vi.fn();
    const delivery = outbox.submit(turn).then(settled);
    await vi.advanceTimersByTimeAsync(0);
    expect(bridge.chatOutboxPut).toHaveBeenCalledWith(scope, saved('submitted'));
    expect(send).not.toHaveBeenCalled();
    allowStage.resolve();
    await vi.advanceTimersByTimeAsync(0);
    expect(send).toHaveBeenCalledWith(turn.payload, scope, storeId);
    expect(settled).not.toHaveBeenCalled();
    outbox.handle(receipt('accepted', 'wrong-session'));
    expect(bridge.chatOutboxPut).toHaveBeenCalledTimes(1);

    const allowAcceptance = Promise.withResolvers<void>();
    bridge.chatOutboxPut.mockImplementationOnce(() => allowAcceptance.promise);
    outbox.handle(receipt('accepted'));
    outbox.handle(receipt('completed'));
    await vi.advanceTimersByTimeAsync(0);
    expect(bridge.chatOutboxPut).toHaveBeenLastCalledWith(scope, saved('accepted'));
    expect(bridge.chatOutboxRemove).not.toHaveBeenCalled();
    expect(settled).not.toHaveBeenCalled();
    allowAcceptance.resolve();
    await delivery;
    expect(settled).toHaveBeenCalledExactlyOnceWith('accepted');
    await vi.advanceTimersByTimeAsync(0);
    expect(bridge.chatOutboxRemove).toHaveBeenCalledExactlyOnceWith(scope, turn.payload.request_id);
  });

  it('queries after reload and retries only an unknown unaccepted ID with its frozen wire frame', async () => {
    const bridge = journal([saved('submitted')]);
    await outbox.bind(bridge, observer);
    outbox.connect(send);
    outbox.handle(capability());
    expect(observer.onRestored).toHaveBeenCalledExactlyOnceWith(saved('submitted'));
    expect(send).toHaveBeenCalledExactlyOnceWith({
      action: 'send_receipt_query', session_id: 'session-one', request_id: 'request-one',
    }, scope, storeId);
    outbox.handle(receipt('unknown'));
    outbox.handle(receipt('unknown'));
    await vi.advanceTimersByTimeAsync(0);
    expect(send).toHaveBeenCalledTimes(2);
    expect(send).toHaveBeenLastCalledWith(turn.payload, scope, storeId);
  });

  it.each([
    { phase: 'accepted' as const, negotiated: storeId, status: 'unknown', code: undefined },
    { phase: 'submitted' as const, negotiated: otherStoreId, status: null, code: 'receipt_store_changed' },
    { phase: 'withdrawing' as const, negotiated: otherStoreId, status: null, code: 'receipt_store_changed' },
  ])('keeps $phase safely available for manual review when its outcome is uncertain', async ({ phase, negotiated, status, code }) => {
    const bridge = journal([saved(phase)]);
    await outbox.bind(bridge, observer);
    outbox.connect(send);
    outbox.handle(capability(negotiated));
    if (status) outbox.handle(receipt(status));
    await vi.advanceTimersByTimeAsync(400);

    const held = bridge.records.get(turn.payload.request_id);
    expect(held).toMatchObject({
      phase: 'queued',
      value: { item: { requiresReview: true, reviewReason: 'uncertain', attachments: turn.payload.attachments } },
    });
    expect(observer.onReceipt).toHaveBeenCalledExactlyOnceWith(saved(phase), 'uncertain', code, true);
    expect(observer.onRestored).toHaveBeenLastCalledWith(held);
    expect(send).toHaveBeenCalledTimes(status ? 1 : 0);
    if (status) expect(send.mock.calls[0]?.[0]).toMatchObject({ action: 'send_receipt_query' });
  });

  it('persists withdrawal, never replays the canceled request, and waits for cancellation proof', async () => {
    const bridge = journal([saved('submitted')]);
    await outbox.bind(bridge, observer);
    outbox.connect(send);
    outbox.handle(capability());
    await outbox.withdraw('session-one', 'request-one');
    expect(bridge.records.get('request-one')).toEqual(saved('withdrawing'));
    outbox.handle(receipt('unknown'));
    await vi.advanceTimersByTimeAsync(80);
    expect(send.mock.calls.slice(1).every(([frame]) =>
      frame && 'action' in frame && frame.action === 'send_receipt_cancel')).toBe(true);
    outbox.handle(receipt('cancelled'));
    await vi.advanceTimersByTimeAsync(0);
    expect(bridge.records.size).toBe(0);
    expect(observer.onReceipt).toHaveBeenCalledExactlyOnceWith(saved('withdrawing'), 'cancelled', undefined, true);
  });

  it('does not expose a previous owner snapshot or resend with the previous owner scope', async () => {
    const previous = journal([saved('submitted')]);
    const pending = Promise.withResolvers<DesktopChatOutboxSnapshot>();
    previous.chatOutboxList.mockReturnValueOnce(pending.promise);
    const oldBinding = outbox.bind(previous, observer);
    const next = journal();
    next.chatOutboxList.mockResolvedValue({ scope: 'owner-two', entries: [] });
    await outbox.bind(next, observer);
    outbox.connect(send);
    outbox.handle(capability());
    pending.resolve({ scope, entries: [saved('submitted')] });
    await oldBinding;
    outbox.handle(receipt('unknown'));
    expect(observer.onRestored).not.toHaveBeenCalled();
    expect(send).not.toHaveBeenCalled();
  });
});
