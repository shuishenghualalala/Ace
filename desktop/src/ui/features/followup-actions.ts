import type { FollowupAnswer } from '../api/feature-session';
import type { ChatChunk, FollowupCommand } from '../api/transport';
import { state, type PendingFollowup } from '../state';
import { interactiveOwnerKey } from './interactive-owner';
import { chatOutbox, desktopOutboxBridge } from './chat-outbox';

export type FollowupDraft = Record<string, { selected: string[]; freeText: string }>;
type ReceiptStatus = 'applied' | 'cancelled' | 'expired' | 'rejected' | 'unknown' | 'unavailable' | 'uncertain';
export type FollowupSubmissionStatus = Exclude<ReceiptStatus, 'applied' | 'cancelled'> | 'confirming';

interface FollowupTarget {
  key: string;
  owner: string;
  sessionId: string;
  wireSessionId: string;
  requestId: string;
  questionId: string;
}

interface FollowupAttempt {
  id: string;
  receipted: boolean;
  answers?: FollowupAnswer[];
  transport?: { scope: string; storeId: string };
  timer?: number;
}

interface FollowupEntry {
  target: FollowupTarget;
  question: PendingFollowup;
  draft: FollowupDraft;
  status?: FollowupSubmissionStatus;
  changed?: (sessionId: string) => void;
  attempt?: FollowupAttempt;
}

export interface FollowupCardView {
  key: string;
  question: PendingFollowup;
  draft?: FollowupDraft;
  status?: FollowupSubmissionStatus;
  locked: boolean;
  actionable: boolean;
  canRecheck: boolean;
  saveDraft(draft: FollowupDraft): void;
  recheck(): void;
  dismiss(): void;
}

const limit = 32;
const confirmationDelay = 2000;
const entries = new Map<string, FollowupEntry>();
let receiptsAvailable = false;

export function receiveFollowupCapability(chunk: ChatChunk): void {
  if (chunk.kind !== 'send_receipt_capability') return;
  receiptsAvailable = chunk.body.version === 1 && chunk.body.followup_receipts === 1;
}

export function disconnectFollowupReceipts(): void {
  receiptsAvailable = false;
}

function followupTarget(sessionId: string, question: PendingFollowup): FollowupTarget {
  const owner = interactiveOwnerKey();
  const wireSessionId = question.sourceSessionId ?? sessionId;
  const requestId = question.requestId ?? '';
  return {
    owner, sessionId, wireSessionId, requestId, questionId: question.questionId,
    key: JSON.stringify([owner, sessionId, wireSessionId, requestId, question.questionId]),
  };
}

function clearTimer(entry: FollowupEntry): void {
  if (entry.attempt?.timer === undefined) return;
  window.clearTimeout(entry.attempt.timer);
  delete entry.attempt.timer;
}

function removeEntry(entry: FollowupEntry): void {
  clearTimer(entry);
  entries.delete(entry.target.key);
}

function entryFor(target: FollowupTarget, question: PendingFollowup): FollowupEntry {
  const existing = entries.get(target.key);
  if (existing) return existing;
  if (entries.size >= limit) {
    const oldest = Array.from(entries.values()).find((entry) => !entry.attempt);
    if (oldest) removeEntry(oldest);
  }
  const entry: FollowupEntry = { target, question, draft: {} };
  entries.set(target.key, entry);
  return entry;
}

export function followupCardView(sessionId: string, pending?: PendingFollowup | null): FollowupCardView | undefined {
  const owner = interactiveOwnerKey();
  const retained = pending ? undefined : Array.from(entries.values()).reverse().find((entry) =>
    entry.target.owner === owner && entry.target.sessionId === sessionId && entry.status);
  const question = pending ?? retained?.question;
  if (!question) return;
  const target = followupTarget(sessionId, question);
  const entry = entries.get(target.key);
  return {
    key: target.key,
    question,
    ...(entry?.draft ? { draft: entry.draft } : {}),
    ...(entry?.status ? { status: entry.status } : {}),
    locked: !pending || entry?.status === 'expired' || Boolean(entry?.attempt && entry.status !== 'unknown'),
    actionable: Boolean(pending) && entry?.status !== 'expired',
    canRecheck: entry?.attempt?.receipted === true,
    saveDraft(draft) {
      if (target.owner !== interactiveOwnerKey()) return;
      const current = entryFor(target, question);
      current.draft = draft;
    },
    recheck() {
      const current = entries.get(target.key);
      if (current?.attempt && target.owner === interactiveOwnerKey()
        && (current.status === 'uncertain' || current.status === 'unavailable')) {
        setStatus(current, 'confirming');
        void queryReceipt(current, current.attempt);
      }
    },
    dismiss() {
      const current = entries.get(target.key);
      if (current?.status !== 'expired' || target.owner !== interactiveOwnerKey()) return;
      removeEntry(current);
      current.changed?.(target.sessionId);
    },
  };
}

function setStatus(entry: FollowupEntry, status: FollowupSubmissionStatus): void {
  if (entry.status === status) return;
  entry.status = status;
  entry.changed?.(entry.target.sessionId);
}

function commandIdentity(entry: FollowupEntry, attempt: FollowupAttempt) {
  const target = entry.target;
  return {
    session_id: target.wireSessionId,
    question_id: target.questionId,
    request_id: target.requestId,
    submission_id: attempt.id,
  };
}

function isCurrent(entry: FollowupEntry, attempt: FollowupAttempt): boolean {
  return entry.target.owner === interactiveOwnerKey()
    && entries.get(entry.target.key) === entry && entry.attempt === attempt;
}

function scheduleQuery(entry: FollowupEntry, attempt: FollowupAttempt): void {
  clearTimer(entry);
  attempt.timer = window.setTimeout(() => { void queryReceipt(entry, attempt); }, confirmationDelay);
}

async function queryReceipt(entry: FollowupEntry, attempt: FollowupAttempt): Promise<void> {
  if (!isCurrent(entry, attempt) || !matchesTransport(entry, attempt)) return;
  clearTimer(entry);
  if (!attempt.receipted || !receiptsAvailable) {
    setStatus(entry, 'uncertain');
    return;
  }
  attempt.timer = window.setTimeout(() => {
    if (isCurrent(entry, attempt)) setStatus(entry, 'uncertain');
  }, confirmationDelay);
  try {
    const frame: FollowupCommand = { action: 'followup_receipt_query', ...commandIdentity(entry, attempt) };
    if (attempt.transport) {
      await state.socket?.send(frame, attempt.transport.scope, attempt.transport.storeId);
    } else {
      await state.socket?.send(frame);
    }
  } catch {
    // A transport failure still leaves the original submission unresolved.
  }
}

function sameAction(attempt: FollowupAttempt, answers: FollowupAnswer[] | undefined): boolean {
  return JSON.stringify(attempt.answers) === JSON.stringify(answers);
}

export function submitFollowupDecision(
  sessionId: string,
  question: PendingFollowup,
  answers: FollowupAnswer[] | undefined,
  changed: (sessionId: string) => void,
): void {
  const target = followupTarget(sessionId, question);
  const entry = entryFor(target, question);
  if ((entry.attempt && entry.status !== 'unknown') || entry.status === 'expired') return;
  entry.changed = changed;
  const transport = chatOutbox.transportIdentity();
  if (desktopOutboxBridge() && !transport) {
    setStatus(entry, 'unavailable');
    return;
  }
  clearTimer(entry);
  const attempt = entry.attempt && sameAction(entry.attempt, answers) ? entry.attempt : {
    id: Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, '0')).join(''),
    receipted: receiptsAvailable && Boolean(target.requestId),
    ...(answers ? { answers: structuredClone(answers) } : {}),
    ...(transport ? { transport } : {}),
  };
  entry.attempt = attempt;
  setStatus(entry, 'confirming');
  void sendDecision(entry, attempt);
}

function matchesTransport(entry: FollowupEntry, attempt: FollowupAttempt): boolean {
  if (!attempt.transport) return true;
  const current = chatOutbox.transportIdentity();
  if (current?.scope === attempt.transport.scope && current.storeId === attempt.transport.storeId) return true;
  clearTimer(entry);
  setStatus(entry, 'uncertain');
  return false;
}

async function sendDecision(entry: FollowupEntry, attempt: FollowupAttempt): Promise<void> {
  if (!matchesTransport(entry, attempt)) return;
  const socket = state.socket;
  const target = entry.target;
  const identity = attempt.receipted ? commandIdentity(entry, attempt)
    : { session_id: target.wireSessionId, question_id: target.questionId };
  const frame: FollowupCommand = attempt.answers
    ? { action: 'followup_answer', ...identity, answers: attempt.answers }
    : { action: 'followup_cancel', ...identity };
  scheduleQuery(entry, attempt);
  try {
    const sent = attempt.transport
      ? await socket?.send(frame, attempt.transport.scope, attempt.transport.storeId)
      : await socket?.send(frame);
    if (!isCurrent(entry, attempt) || entry.status !== 'confirming') return;
    if (!sent && (!socket || !attempt.receipted)) {
      clearTimer(entry);
      delete entry.attempt;
      setStatus(entry, 'unavailable');
      return;
    }
  } catch {
    if (!isCurrent(entry, attempt) || entry.status !== 'confirming') return;
  }
}

export function reconnectFollowupDecisions(): void {
  for (const entry of entries.values()) {
    if (entry.attempt && ['confirming', 'unavailable', 'uncertain'].includes(entry.status ?? '')) {
      void queryReceipt(entry, entry.attempt);
    }
  }
}

export function resetFollowupDecisions(): void {
  disconnectFollowupReceipts();
  for (const entry of entries.values()) clearTimer(entry);
  entries.clear();
}

export function retireFollowupDecision(
  sessionId: string,
  question: PendingFollowup,
  changed: (sessionId: string) => void,
): void {
  const entry = entryFor(followupTarget(sessionId, question), question);
  entry.changed = changed;
  setStatus(entry, entry.attempt ? 'uncertain' : 'expired');
}

export function settleFollowupDecision(
  sessionId: string,
  question: PendingFollowup,
  status: 'applied' | 'cancelled' | 'expired',
  changed: (sessionId: string) => void,
): void {
  const entry = entries.get(followupTarget(sessionId, question).key);
  if (!entry) return;
  clearTimer(entry);
  if (status === 'expired') {
    entry.changed = changed;
    delete entry.attempt;
    setStatus(entry, 'expired');
  } else {
    removeEntry(entry);
    entry.changed?.(sessionId);
  }
}

export interface ConfirmedFollowupReceipt {
  sessionId: string;
  question: PendingFollowup;
  status: 'applied' | 'cancelled' | 'expired';
}

function receiptStatus(value: unknown): ReceiptStatus | undefined {
  switch (value) {
    case 'applied': case 'cancelled': case 'expired': case 'rejected':
    case 'unknown': case 'unavailable': case 'uncertain':
      return value;
    default: return undefined;
  }
}

export function acceptFollowupReceipt(chunk: ChatChunk): ConfirmedFollowupReceipt | undefined {
  const body = chunk.body;
  const status = receiptStatus(body?.status);
  const owner = interactiveOwnerKey();
  if (chunk.kind !== 'interaction_receipt' || !status || body?.type !== 'followup' || !chunk.session_id || typeof chunk.request_id !== 'string'
    || typeof body.interaction_id !== 'string' || typeof body.submission_id !== 'string'
    || chunk.is_final || typeof chunk.gateway_sequence === 'number') return;
  const entry = Array.from(entries.values()).find(({ target, attempt }) =>
    target.owner === owner && target.wireSessionId === chunk.session_id && target.requestId === chunk.request_id
    && target.questionId === body.interaction_id && attempt?.id === body.submission_id);
  if (!entry?.attempt?.receipted) return;
  clearTimer(entry);
  if (status === 'applied' || status === 'cancelled' || status === 'expired') {
    return { sessionId: entry.target.sessionId, question: entry.question, status };
  }
  if (status === 'rejected') delete entry.attempt;
  setStatus(entry, status);
  return undefined;
}

export function submittedFollowupAnswers(sessionId: string, question: PendingFollowup): FollowupAnswer[] | undefined {
  return entries.get(followupTarget(sessionId, question).key)?.attempt?.answers;
}
