import type { BackendHistoryItem } from '../api/feature-session';
import type { ChatChunk } from '../api/transport';
import type { ChatMessage, PendingMessage, SessionStatus } from '../chat-render';
import {
  applyMessageUpserts,
  isStreamToolBoundary,
  normalizeChunk,
  reassembleChunkDelta,
  reduceChunk,
  type AnyChatChunk,
} from '../reducers/chat-reducer';
import { createSessionBook, type Bookkeeping } from '../state';
import {
  createSessionReassembly, noteSessionDelta, noteSessionDeltaBoundary, type SessionReassembly,
} from '../stream-reassembly';
import type { HistoryPagingState } from '../stores/message-store';
import { pagingForHistoryWindow, retainEarlierHistory } from './history-merge';
import { mapBackendHistoryItems, mergeTeamInternalMessages } from './history-mapping';

const SNAPSHOT_KINDS = new Set<AnyChatChunk['kind']>([
  'delta', 'thinking', 'tool', 'file_changes', 'todo_updated', 'status',
]);

interface ActiveTurnProjection {
  messages: ChatMessage[];
  book: Bookkeeping;
  reassembly: SessionReassembly;
  paging: HistoryPagingState;
  status: SessionStatus;
  queueHint: string;
  busy: boolean;
}

function recordOf(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : undefined;
}

/** Builds a complete candidate before touching the renderer's live session. */
export function projectActiveTurnSnapshot(
  sessionId: string,
  control: ChatChunk,
  local: {
    messages: ChatMessage[];
    book: Bookkeeping | undefined;
    queue: PendingMessage[];
    paging?: HistoryPagingState | undefined;
  },
): ActiveTurnProjection | null {
  const snapshot = recordOf(control.body?.turn_snapshot);
  if (!snapshot) return null;
  const requestId = snapshot.request_id;
  const startedAt = typeof snapshot.started_at === 'number' ? snapshot.started_at * 1000 : NaN;
  const history = recordOf(snapshot.history);
  const cut = control.gateway_sequence;
  if (typeof requestId !== 'string' || !requestId.trim()
    || !Number.isFinite(startedAt) || startedAt <= 0
    || !Array.isArray(snapshot.frames) || !Array.isArray(history?.items)
    || typeof history.has_more !== 'boolean'
    || (history.next_before !== null && typeof history.next_before !== 'string')
    || (history.has_more && !history.next_before)
    || typeof cut !== 'number') return null;
  if (local.book && !local.book.turnSealed && local.book.activeRequestId
    && local.book.activeRequestId !== requestId) return null;

  const items = history.items as unknown[];
  if (items.some((item) => {
    const row = recordOf(item);
    return !row || typeof row.role !== 'string' || typeof row.content !== 'string';
  })) return null;
  const latestItem = recordOf(items.at(-1));
  if (latestItem?.role !== 'user' || (latestItem.request_id && latestItem.request_id !== requestId)) return null;

  const durable = mergeTeamInternalMessages(mapBackendHistoryItems(items as BackendHistoryItem[], sessionId));
  const currentUser = durable.at(-1);
  if (currentUser?.role !== 'user') return null;
  const localUser = local.messages.find((message) => message.role === 'user'
    && (currentUser.historyId?.startsWith('followup:')
      ? message.historyId === currentUser.historyId
      : message.requestId === requestId));
  const prompt = { ...(localUser ?? currentUser), requestId, historyId: currentUser.historyId };
  const optimisticIds = new Set(local.queue.map((item) => item.optimisticUserMessageId));
  const queuedUsers = local.messages.filter((message) =>
    message.role === 'user' && message.id !== prompt.id && optimisticIds.has(message.id));
  const assistantId = `m-snapshot-${requestId}`;
  let messages: ChatMessage[] = [...durable.slice(0, -1), prompt, {
    id: assistantId,
    role: 'assistant',
    content: '',
    streaming: true,
    segmentRole: 'process',
    timestamp: startedAt,
    turnStartedAt: startedAt,
    model: prompt.model,
  }];
  let book: Bookkeeping = {
    ...createSessionBook(),
    assistantId,
    activeRequestId: requestId,
    turnSealed: false,
    planActive: local.book?.planActive ?? false,
    pendingPlan: local.book?.pendingPlan ?? null,
    prevTurnFileSignature: local.book?.prevTurnFileSignature ?? null,
  };
  let status: SessionStatus = 'running';
  let queueHint = '';
  const reassembly = createSessionReassembly();
  let previous = 0;
  for (const rawFrame of snapshot.frames as unknown[]) {
    const frame = recordOf(rawFrame);
    const seq = frame?.gateway_sequence;
    if (!frame || frame.session_id !== sessionId || frame.request_id !== requestId
      || frame.gateway_epoch !== control.gateway_epoch || frame.is_final
      || typeof seq !== 'number' || !Number.isSafeInteger(seq) || seq <= previous || seq > cut
      || !recordOf(frame.body)) return null;
    const chunk = normalizeChunk(frame);
    if (!chunk || !SNAPSHOT_KINDS.has(chunk.kind) || (chunk.kind === 'status' && chunk.body.control)) return null;
    previous = seq;
    if (isStreamToolBoundary(chunk) && !noteSessionDeltaBoundary(reassembly, seq)) return null;
    const result = reduceChunk(chunk, {
      sessionId, messages, book, currentStatus: status, now: startedAt, sequence: chunk.sequence,
      externallyOrderedDelta: chunk.kind === 'delta',
    });
    if (chunk.kind === 'delta'
      && !reassembleChunkDelta(result, chunk, seq, (...args) => noteSessionDelta(reassembly, ...args))) return null;
    if (result.replaceBook) book = result.replaceBook;
    messages = applyMessageUpserts(messages, result);
    status = result.statusHint ?? status;
    queueHint = result.queueHint ?? queueHint;
  }

  // The Gateway has verified these questions are still waiting; this projection
  // does not replay the original interactions or raise fresh side effects.
  const pending = control.body?.pending_followups;
  if (Array.isArray(pending)) {
    for (const raw of pending as unknown[]) {
      const question = normalizeChunk(raw);
      if (question?.kind === 'followup_question' && typeof raw === 'object' && raw !== null) {
        const requestId = (raw as { request_id?: unknown }).request_id;
        if (typeof requestId === 'string' && requestId) {
          question.body = { ...question.body, interaction_request_id: requestId };
        }
      }
      if (question?.kind !== 'followup_question' || question.session_id !== sessionId
        || typeof question.body.question_id !== 'string' || !question.body.question_id
        || !Array.isArray(question.body.questions)) continue;
      const result = reduceChunk(question, {
        sessionId, messages, book, currentStatus: status, now: startedAt, sequence: question.sequence,
      });
      if (result.replaceBook) book = result.replaceBook;
    }
  }
  const waiting = Boolean(book.pendingFollowup) || book.pendingPlan?.status === 'pending';
  if (waiting) {
    messages = messages.map((message) => message.streaming
      ? { ...message, streaming: false, interrupted: true } : message);
    status = 'idle';
    queueHint = '';
  }
  if (local.book?.activeRequestId === requestId) book.firstChunkAt = local.book.firstChunkAt;
  const paging = pagingForHistoryWindow({
    has_more: history.has_more,
    next_before: history.next_before,
    ...(typeof history.revision === 'string' ? { revision: history.revision } : {}),
    ...(typeof history.head === 'string' ? { head: history.head } : {}),
  }, currentUser.historyId);
  const retained = retainEarlierHistory(local.messages, [...messages, ...queuedUsers], paging, local.paging);
  return { ...retained, book, reassembly, status, queueHint, busy: !waiting };
}
