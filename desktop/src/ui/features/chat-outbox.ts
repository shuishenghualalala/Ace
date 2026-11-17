import type { DesktopChatOutboxEntry, DesktopChatOutboxSnapshot } from '../../shared/chat-outbox';
import type { ComposerCheckpointRef } from '../../shared/renderer-checkpoint';
import { CrewBridge, type ChatChunk, type ReceiptedSendPayload, type SendReceiptAction } from '../api/transport';
import type { PendingMessage } from '../chat-render';
import { uiStore } from '../stores/ui-store';

type ReceiptStatus = 'accepted' | 'running' | 'completed' | 'failed' | 'uncertain' | 'unknown' | 'rejected' | 'unavailable' | 'cancelled';
type Delivery = 'accepted' | 'rejected' | 'cancelled';
type OutboxFrame = ReceiptedSendPayload | SendReceiptAction;

interface OutboxBridge {
  chatOutboxList(): Promise<DesktopChatOutboxSnapshot>;
  chatOutboxPut(scope: string, entry: DesktopChatOutboxEntry): Promise<void>;
  chatOutboxRemove(scope: string, requestId: string): Promise<void>;
}

export interface OutboxTurn {
  payload: ReceiptedSendPayload;
  userMessageId?: string;
  queuedItem?: PendingMessage;
  composerCheckpoint?: ComposerCheckpointRef;
}

interface StoredOutboxTurn extends OutboxTurn {
  storeId: string;
}

interface Tracked {
  entry: DesktopChatOutboxEntry;
  writes: Promise<void>;
  delivery?: PromiseWithResolvers<Delivery>;
  processing?: boolean;
  staging?: boolean;
  removing?: boolean;
  terminal?: boolean;
  finishing?: boolean;
  accepting?: Promise<void>;
  acceptedPersisted?: boolean;
  restored?: boolean;
}

interface Binding {
  bridge: OutboxBridge;
  scope: string;
}

export interface OutboxObserver {
  onRestored(entry: DesktopChatOutboxEntry): void;
  onReceipt(entry: DesktopChatOutboxEntry, status: ReceiptStatus, code: string | undefined, restored: boolean): void;
}

export function desktopOutboxBridge(): OutboxBridge | null {
  const bridge = CrewBridge();
  if (!bridge?.chatOutboxList || !bridge.chatOutboxPut || !bridge.chatOutboxRemove) return null;
  return {
    chatOutboxList: bridge.chatOutboxList,
    chatOutboxPut: bridge.chatOutboxPut,
    chatOutboxRemove: bridge.chatOutboxRemove,
  };
}

export function outboxTurn(entry: DesktopChatOutboxEntry): StoredOutboxTurn | null {
  const payload = entry.value.payload as ReceiptedSendPayload | undefined;
  if (payload?.send_receipt !== true || payload.request_id !== entry.requestId || payload.session_id !== entry.sessionId) return null;
  return entry.value as unknown as StoredOutboxTurn;
}

export function outboxQueueItem(entry: DesktopChatOutboxEntry): PendingMessage | null {
  const item = entry.value.item as PendingMessage | undefined;
  return item?.requestId === entry.requestId && typeof item.id === 'string' && typeof item.query === 'string' ? item : null;
}

/** One owner binding and one native WebSocket at a time. */
export class ChatOutbox {
  private entries = new Map<string, Tracked>();
  private binding: Binding | null = null;
  private observer: OutboxObserver | null = null;
  private version = 0;
  private storeId: string | null = null;
  private retry: ReturnType<typeof setTimeout> | undefined;
  private send: ((frame: OutboxFrame, scope: string, storeId: string) => Promise<boolean>) | null = null;

  constructor(private readonly setReady: (ready: boolean) => void, private readonly pollMs = 4_000) {}

  async bind(bridge: OutboxBridge, observer: OutboxObserver): Promise<void> {
    this.reset();
    const version = this.version;
    this.observer = observer;
    const snapshot = await bridge.chatOutboxList();
    if (version !== this.version) return;
    this.binding = { bridge, scope: snapshot.scope };
    for (const entry of snapshot.entries) {
      if (entry.phase === 'queued' ? !outboxQueueItem(entry) : !outboxTurn(entry)) continue;
      this.entries.set(entry.requestId, {
        entry, writes: Promise.resolve(), acceptedPersisted: entry.phase === 'accepted', restored: true,
      });
      observer.onRestored(entry);
    }
    this.updateReady();
  }

  reset(): void {
    this.version++;
    this.disconnect();
    this.binding = null;
    this.observer = null;
    for (const tracked of this.entries.values()) tracked.delivery?.resolve('cancelled');
    this.entries.clear();
  }

  connect(send: (frame: OutboxFrame, scope: string, storeId: string) => Promise<boolean>): void {
    this.send = send;
    this.updateReady();
  }

  disconnect(): void {
    this.send = null;
    this.storeId = null;
    clearTimeout(this.retry);
    this.retry = undefined;
    this.setReady(false);
  }

  private updateReady(): void {
    const ready = Boolean(this.storeId && this.send && this.binding);
    this.setReady(ready);
    if (ready) this.poll();
  }

  private async transmit(frame: OutboxFrame, storeId: string): Promise<void> {
    const binding = this.binding;
    const send = this.send;
    if (!binding || !send || this.storeId !== storeId) return;
    // A bridge result can be lost after the Gateway read the frame. The
    // authoritative receipt, or a later query, must decide what happened.
    await send(frame, binding.scope, storeId).catch(() => false);
  }

  private poll(): void {
    clearTimeout(this.retry);
    this.retry = undefined;
    const { binding, storeId } = this;
    if (!this.send || !storeId || !binding) return;
    let unresolved = false;
    for (const tracked of this.entries.values()) {
      const { entry, staging, removing, finishing, terminal } = tracked;
      if (entry.phase === 'queued') continue;
      unresolved = true;
      if (staging || removing || finishing) continue;
      if (terminal) {
        void this.removeTerminal(tracked, binding).catch(() => {});
        continue;
      }
      if (outboxTurn(entry)?.storeId !== storeId) {
        void this.reconcile(tracked, binding, 'uncertain', 'receipt_store_changed').catch(() => {});
        continue;
      }
      void this.transmit({
        action: entry.phase === 'withdrawing' ? 'send_receipt_cancel' : 'send_receipt_query',
        session_id: entry.sessionId,
        request_id: entry.requestId,
      }, storeId);
    }
    if (unresolved) this.retry = setTimeout(() => this.poll(), this.pollMs);
  }

  private write(tracked: Tracked, entry: DesktopChatOutboxEntry | null, binding: Binding): Promise<void> {
    const next = tracked.writes.catch(() => {}).then(() => entry
      ? binding.bridge.chatOutboxPut(binding.scope, entry)
      : binding.bridge.chatOutboxRemove(binding.scope, tracked.entry.requestId));
    tracked.writes = next;
    return next;
  }

  async queue(
    sessionId: string,
    item: PendingMessage & { requestId: string },
    composerCheckpoint?: ComposerCheckpointRef,
  ): Promise<boolean> {
    const binding = this.binding;
    if (!binding || !this.storeId || !this.send) return false;
    const entry: DesktopChatOutboxEntry = {
      requestId: item.requestId, sessionId, phase: 'queued',
      value: { item: structuredClone(item), ...(composerCheckpoint ? { composerCheckpoint } : {}) },
    };
    const tracked: Tracked = { entry, writes: Promise.resolve(), staging: true };
    this.entries.set(item.requestId, tracked);
    try {
      await this.write(tracked, entry, binding);
      return this.binding === binding && this.entries.get(item.requestId) === tracked && !tracked.removing;
    } catch (error) {
      if (this.entries.get(item.requestId) === tracked) this.entries.delete(item.requestId);
      throw error;
    } finally {
      tracked.staging = false;
    }
  }

  async updateQueued(sessionId: string, item: PendingMessage & { requestId: string }): Promise<boolean> {
    const binding = this.binding;
    const tracked = this.entries.get(item.requestId);
    if (!binding || !tracked || tracked.entry.sessionId !== sessionId
      || tracked.entry.phase !== 'queued' || tracked.staging || tracked.removing) return false;
    const updated = { ...tracked.entry, value: { ...tracked.entry.value, item: structuredClone(item) } };
    tracked.staging = true;
    try {
      await this.write(tracked, updated, binding);
      if (this.binding !== binding || tracked.removing || this.entries.get(item.requestId) !== tracked) return false;
      tracked.entry = updated;
      return true;
    } finally {
      tracked.staging = false;
    }
  }

  async submit(turn: OutboxTurn): Promise<Delivery> {
    const { binding, storeId } = this;
    if (!binding || !storeId || !this.send) return 'rejected';
    const queuedCheckpoint = this.entries.get(turn.payload.request_id)?.entry.value.composerCheckpoint;
    const storedTurn = {
      ...(queuedCheckpoint ? { composerCheckpoint: queuedCheckpoint } : {}),
      ...structuredClone(turn),
      storeId,
    };
    const { payload } = storedTurn;
    const entry: DesktopChatOutboxEntry = {
      requestId: payload.request_id, sessionId: payload.session_id, phase: 'submitted', value: storedTurn,
    };
    const previous = this.entries.get(entry.requestId);
    const tracked: Tracked = previous ?? { entry, writes: Promise.resolve() };
    if (tracked.delivery) return tracked.delivery.promise;
    if (tracked.staging || tracked.removing || (previous && tracked.entry.phase !== 'queued')) return 'cancelled';
    const previousEntry = previous?.entry;
    const delivery = Promise.withResolvers<Delivery>();
    tracked.delivery = delivery;
    tracked.entry = entry;
    tracked.staging = true;
    this.entries.set(entry.requestId, tracked);
    try {
      await this.write(tracked, entry, binding);
    } catch (error) {
      delete tracked.delivery;
      if (tracked.entry === entry && this.entries.get(entry.requestId) === tracked) {
        if (previousEntry) tracked.entry = previousEntry;
        else this.entries.delete(entry.requestId);
      }
      throw error;
    } finally {
      tracked.staging = false;
    }
    if (this.binding !== binding) return 'cancelled';
    if (tracked.entry === entry) void this.transmit(payload, storeId);
    if (this.retry === undefined) this.retry = setTimeout(() => this.poll(), this.pollMs);
    return delivery.promise;
  }

  async withdraw(sessionId: string, requestId?: string): Promise<void> {
    const binding = this.binding;
    if (!binding) return;
    const entries = Array.from(this.entries.values()).filter(({ entry }) =>
      entry.sessionId === sessionId && (!requestId || entry.requestId === requestId));
    await Promise.all(entries.map(async (tracked) => {
      const previous = tracked.entry;
      if (tracked.removing || tracked.terminal) return;
      if (previous.phase === 'queued') {
        tracked.removing = true;
        try {
          await this.write(tracked, null, binding);
          if (this.binding === binding) this.entries.delete(previous.requestId);
        } finally {
          tracked.removing = false;
        }
        return;
      }
      const withdrawing = { ...previous, phase: 'withdrawing' } as const;
      tracked.entry = withdrawing;
      try {
        await this.write(tracked, withdrawing, binding);
      } catch (error) {
        tracked.entry = previous;
        throw error;
      }
      tracked.delivery?.resolve('cancelled');
      if (this.binding === binding) this.poll();
    }));
  }

  hasUnsettledTurn(sessionId: string): boolean {
    for (const { entry, terminal } of this.entries.values()) {
      if (entry.sessionId === sessionId && !terminal
        && (entry.phase === 'submitted' || entry.phase === 'accepted')) return true;
    }
    return false;
  }

  handle(chunk: ChatChunk): boolean {
    if (chunk.kind === 'send_receipt_capability') {
      const storeId = chunk.body.store_id;
      this.storeId = chunk.body.version === 1 && typeof storeId === 'string' && /^[a-f0-9]{32}$/.test(storeId)
        ? storeId : null;
      this.updateReady();
      return true;
    }
    if (chunk.kind !== 'send_receipt') return false;
    const tracked = chunk.request_id ? this.entries.get(chunk.request_id) : undefined;
    const binding = this.binding;
    if (binding && tracked && tracked.entry.sessionId === chunk.session_id
      && this.storeId && outboxTurn(tracked.entry)?.storeId === this.storeId) {
      void this.reconcile(tracked, binding, chunk.body.status, chunk.body.code).catch(() => {});
    }
    return true;
  }

  private async reconcile(tracked: Tracked, binding: Binding, rawStatus: unknown, rawCode: unknown): Promise<void> {
    const { entry } = tracked;
    if (entry.phase === 'queued' || tracked.staging || tracked.removing || tracked.finishing) return;
    if (tracked.terminal) {
      await this.removeTerminal(tracked, binding);
      return;
    }
    if (rawStatus === 'unknown' && entry.phase === 'accepted') rawStatus = 'uncertain';
    const code = typeof rawCode === 'string' ? rawCode : undefined;
    if (rawStatus === 'unknown' && entry.phase === 'submitted' && !tracked.processing) {
      const turn = outboxTurn(entry);
      if (!turn) return;
      tracked.processing = true;
      try {
        await this.transmit(turn.payload, turn.storeId);
      } finally {
        tracked.processing = false;
      }
      return;
    }
    if (rawStatus === 'accepted' || rawStatus === 'running') {
      if (entry.phase === 'submitted' || (entry.phase === 'accepted' && !tracked.acceptedPersisted)) {
        await this.persistAcceptance(tracked, binding);
        if (this.binding === binding && tracked.entry.phase === 'accepted') {
          tracked.delivery?.resolve('accepted');
        }
      }
      return;
    }
    if (rawStatus !== 'completed' && rawStatus !== 'failed' && rawStatus !== 'uncertain'
      && rawStatus !== 'rejected' && rawStatus !== 'cancelled') return;
    if (rawStatus === 'rejected' && entry.phase === 'accepted') return;
    if (rawStatus === 'uncertain' || (rawStatus === 'rejected' && entry.phase === 'submitted')) {
      const turn = outboxTurn(entry);
      if (turn && (rawStatus === 'uncertain' || !tracked.delivery || turn.queuedItem)) {
        const item: PendingMessage = {
          id: turn.queuedItem?.id ?? entry.requestId,
          ...turn.queuedItem,
          requestId: entry.requestId,
          query: turn.payload.query,
          ...(turn.payload.attachments ? { attachments: turn.payload.attachments } : {}),
          requiresReview: true,
          reviewReason: rawStatus,
          workspaceId: turn.payload.workspace_id,
        };
        delete item.optimisticUserMessageId;
        const held = {
          ...entry,
          phase: 'queued',
          value: { item, ...(turn.composerCheckpoint ? { composerCheckpoint: turn.composerCheckpoint } : {}) },
        } as const;
        tracked.entry = held;
        try {
          await this.write(tracked, held, binding);
        } catch (error) {
          if (tracked.entry === held) tracked.entry = entry;
          throw error;
        }
        if (this.binding === binding) {
          const delivery = tracked.delivery;
          delete tracked.delivery;
          delivery?.resolve(rawStatus === 'uncertain' ? 'accepted' : 'rejected');
          this.observer?.onReceipt(entry, rawStatus, code, Boolean(tracked.restored));
          this.observer?.onRestored(held);
        }
        return;
      }
    }
    tracked.finishing = true;
    try {
      if ((rawStatus === 'completed' || rawStatus === 'failed') && entry.phase !== 'withdrawing') {
        await this.persistAcceptance(tracked, binding);
      }
    } finally {
      tracked.finishing = false;
    }
    if (this.binding !== binding) return;
    tracked.terminal = true;
    tracked.delivery?.resolve(
      entry.phase === 'withdrawing' || rawStatus === 'cancelled' ? 'cancelled' : rawStatus === 'rejected' ? 'rejected' : 'accepted',
    );
    this.observer?.onReceipt(entry, rawStatus, code, Boolean(tracked.restored));
    await this.removeTerminal(tracked, binding);
  }

  private async persistAcceptance(tracked: Tracked, binding: Binding): Promise<void> {
    if (tracked.acceptedPersisted || tracked.entry.phase === 'withdrawing') return;
    if (tracked.accepting) return tracked.accepting;
    const accepted = { ...tracked.entry, phase: 'accepted' } as const;
    // Fence retransmission immediately, then wait for durable local acceptance
    // before the composer is allowed to discard its input.
    tracked.entry = accepted;
    tracked.accepting = this.write(tracked, accepted, binding);
    try {
      await tracked.accepting;
      tracked.acceptedPersisted = true;
    } finally {
      delete tracked.accepting;
    }
  }

  private async removeTerminal(tracked: Tracked, binding: Binding): Promise<void> {
    tracked.removing = true;
    try {
      await this.write(tracked, null, binding);
      if (this.binding === binding && this.entries.get(tracked.entry.requestId) === tracked) {
        this.entries.delete(tracked.entry.requestId);
      }
    } finally {
      tracked.removing = false;
    }
  }
}

export const chatOutbox = new ChatOutbox((backendReceiptsReady) => uiStore.set({ backendReceiptsReady }));

export function canSubmitDesktopChat(): boolean {
  return uiStore.get().backendCanSend && (!desktopOutboxBridge() || uiStore.get().backendReceiptsReady);
}
