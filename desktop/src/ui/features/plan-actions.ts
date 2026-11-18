import type { SessionPlanState } from '../api/feature-session';
import { backendApi } from '../backend-client';
import type { ChatChunk, PlanActionKind, PlanCommand } from '../api/transport';
import { state } from '../state';
import { chatOutbox, desktopOutboxBridge } from './chat-outbox';
import { interactiveOwnerKey } from './interactive-owner';

export type PlanActionStatus = 'confirming' | 'applied' | 'confirmed' | 'expired' | 'rejected'
  | 'unknown' | 'unavailable' | 'uncertain';

export interface PlanActionNotice {
  status: PlanActionStatus;
  planState?: SessionPlanState;
  currentVersion?: string;
  executionRequestId?: string;
  code?: string;
}

export interface PlanActionInput {
  action: PlanActionKind;
  sessionId: string;
  expectedVersion?: string;
  plan?: string;
  feedback?: string;
  mode: string;
  workspaceId: string;
}

interface PendingPlanAction {
  owner: string;
  input: PlanActionInput;
  command: PlanCommand;
  status: PlanActionStatus;
  notify(notice: PlanActionNotice): void;
  transport?: { scope: string; storeId: string };
  timer?: number;
}

const pending = new Map<string, PendingPlanAction>();
const legacyChecks = new Map<string, () => Promise<void>>();
let receiptsAvailable = false;

export function receivePlanCapability(chunk: ChatChunk): void {
  if (chunk.kind === 'send_receipt_capability') {
    receiptsAvailable = chunk.body.version === 1 && chunk.body.plan_receipts === 1;
  }
}

export function disconnectPlanReceipts(): void {
  receiptsAvailable = false;
}

export function supportsPlanReceipts(version: string | undefined): boolean {
  return receiptsAvailable && Boolean(version);
}

function key(sessionId: string): string {
  return JSON.stringify([interactiveOwnerKey(), sessionId]);
}

function clearTimer(entry: PendingPlanAction): void {
  if (entry.timer !== undefined) window.clearTimeout(entry.timer);
  delete entry.timer;
}

function publish(entry: PendingPlanAction, notice: PlanActionNotice): void {
  entry.status = notice.status;
  entry.notify(notice);
}

function stillCurrent(entry: PendingPlanAction): boolean {
  return entry.owner === interactiveOwnerKey() && pending.get(key(entry.input.sessionId)) === entry;
}

function matchingTransport(entry: PendingPlanAction): boolean {
  if (!entry.transport) return true;
  const current = chatOutbox.transportIdentity();
  return current?.scope === entry.transport.scope && current.storeId === entry.transport.storeId;
}

function transmit(entry: PendingPlanAction, frame: PlanCommand): void {
  if (!stillCurrent(entry) || !matchingTransport(entry)) {
    publish(entry, { status: 'uncertain' });
    return;
  }
  const promise = entry.transport
    ? state.socket?.send(frame, entry.transport.scope, entry.transport.storeId)
    : state.socket?.send(frame);
  void promise?.catch(() => {});
}

function query(entry: PendingPlanAction): void {
  if (!stillCurrent(entry)) return;
  clearTimer(entry);
  if (!receiptsAvailable || !matchingTransport(entry)) {
    publish(entry, { status: 'uncertain' });
    return;
  }
  publish(entry, { status: 'confirming' });
  entry.timer = window.setTimeout(() => {
    if (stillCurrent(entry)) publish(entry, { status: 'uncertain' });
  }, 2000);
  transmit(entry, {
    action: 'plan_receipt_query', session_id: entry.command.session_id,
    request_id: entry.command.request_id, submission_id: entry.command.submission_id,
    expected_version: entry.command.expected_version,
  });
}

export function submitPlanAction(input: PlanActionInput, notify: (notice: PlanActionNotice) => void): void {
  const previous = pending.get(key(input.sessionId));
  if (previous && !['unknown', 'rejected', 'expired'].includes(previous.status)) {
    previous.notify = notify;
    notify({ status: previous.status });
    return;
  }
  const transport = chatOutbox.transportIdentity();
  if (!supportsPlanReceipts(input.expectedVersion) || (desktopOutboxBridge() && !transport)) {
    notify({ status: 'unavailable' });
    return;
  }
  const reuse = previous?.status === 'unknown' && JSON.stringify(previous.input) === JSON.stringify(input);
  const command: PlanCommand = reuse ? previous.command : {
    action: input.action, session_id: input.sessionId,
    request_id: 'plan_' + crypto.randomUUID().replaceAll('-', ''),
    submission_id: crypto.randomUUID().replaceAll('-', ''),
    expected_version: input.expectedVersion!,
    mode: input.mode, workspace_id: input.workspaceId,
    ...(input.plan !== undefined ? { plan: input.plan } : {}),
    ...(input.feedback !== undefined ? { feedback: input.feedback } : {}),
  };
  if (previous) clearTimer(previous);
  const entry: PendingPlanAction = {
    owner: interactiveOwnerKey(), input: structuredClone(input), command, status: 'confirming', notify,
    ...(reuse && previous.transport ? { transport: previous.transport } : transport ? { transport } : {}),
  };
  pending.set(key(input.sessionId), entry);
  publish(entry, { status: 'confirming' });
  entry.timer = window.setTimeout(() => { query(entry); }, 2000);
  transmit(entry, command);
}

export function queryPlanAction(sessionId: string): void {
  const entry = pending.get(key(sessionId));
  if (entry) query(entry);
  else void legacyChecks.get(key(sessionId))?.();
}

export function reconnectPlanActions(): void {
  for (const entry of pending.values()) {
    if (stillCurrent(entry) && ['confirming', 'unavailable', 'uncertain'].includes(entry.status)) query(entry);
  }
}

export function resetPlanActions(): void {
  for (const entry of pending.values()) clearTimer(entry);
  pending.clear();
  legacyChecks.clear();
  disconnectPlanReceipts();
}

function planState(value: unknown, sessionId: string): SessionPlanState | undefined {
  if (!value || typeof value !== 'object') return;
  const row = value as Record<string, unknown>;
  if (row.session_id !== sessionId || typeof row.plan !== 'string' || typeof row.plan_file !== 'string'
    || typeof row.active !== 'boolean' || typeof row.awaiting_approval !== 'boolean' || typeof row.has_plan !== 'boolean') return;
  return row as unknown as SessionPlanState;
}

export function receivePlanReceipt(chunk: ChatChunk): boolean {
  if (chunk.kind !== 'interaction_receipt' || chunk.body.type !== 'plan') return false;
  if (!chunk.session_id || chunk.is_final || typeof chunk.gateway_sequence === 'number') return true;
  const entry = pending.get(key(chunk.session_id));
  if (!entry || !stillCurrent(entry) || chunk.request_id !== entry.command.request_id
    || chunk.body.submission_id !== entry.command.submission_id) return true;
  const status = chunk.body.status;
  if (!['applied', 'expired', 'rejected', 'unknown', 'unavailable', 'uncertain'].includes(String(status))) return true;
  clearTimer(entry);
  const authoritative = planState(chunk.body.plan_state, chunk.session_id);
  const notice: PlanActionNotice = {
    status: status as PlanActionStatus,
    ...(authoritative ? { planState: authoritative } : {}),
    ...(typeof chunk.body.current_version === 'string' ? { currentVersion: chunk.body.current_version } : {}),
    ...(typeof chunk.body.execution_request_id === 'string' ? { executionRequestId: chunk.body.execution_request_id } : {}),
    ...(typeof chunk.body.code === 'string' ? { code: chunk.body.code } : {}),
  };
  if (status === 'applied' && !authoritative) notice.status = 'uncertain';
  publish(entry, notice);
  if (notice.status === 'applied') pending.delete(key(chunk.session_id));
  return true;
}

/** Confirm older servers through their existing plan state, without resending an ambiguous action. */
export async function submitLegacyPlanAction(
  input: PlanActionInput,
  sendFeedback: (requestId: string) => Promise<boolean>,
  notify: (notice: PlanActionNotice) => void,
): Promise<void> {
  const identity = key(input.sessionId);
  const existing = legacyChecks.get(identity);
  if (existing) {
    await existing();
    return;
  }
  const owner = interactiveOwnerKey();
  const socket = state.socket;
  const current = () => interactiveOwnerKey() === owner && state.socket === socket;
  const requestId = 'plan_' + crypto.randomUUID().replaceAll('-', '');
  const stateMatches = (snapshot: SessionPlanState) => {
    if (input.plan !== undefined && snapshot.plan !== input.plan) return false;
    if (input.action === 'plan_approve') {
      return snapshot.phase === 'approved' || snapshot.status === 'approved' || snapshot.status === 'readonly';
    }
    if (input.action === 'plan_reject_and_exit') return !snapshot.active && !snapshot.awaiting_approval;
    if (input.action === 'plan_reject') return snapshot.phase === 'revising' || snapshot.status === 'revising';
    return true;
  };
  async function readUntil(matches: (snapshot: SessionPlanState) => boolean): Promise<SessionPlanState | undefined> {
    for (let attempt = 0; attempt < 10 && current(); attempt += 1) {
      const snapshot = await backendApi.sessionPlan(input.sessionId).catch(() => undefined);
      if (!current()) return;
      if (snapshot && matches(snapshot)) return snapshot;
      await new Promise((resolve) => window.setTimeout(resolve, 150));
    }
    return undefined;
  }
  const check = async () => {
    if (!current()) return;
    notify({ status: 'confirming' });
    const snapshot = await readUntil(stateMatches);
    if (!current()) return;
    if (!snapshot) {
      notify({ status: 'uncertain' });
      return;
    }
    legacyChecks.delete(identity);
    notify({
      status: 'confirmed', planState: snapshot,
      ...(input.action === 'plan_approve' ? { executionRequestId: requestId } : {}),
    });
  };
  legacyChecks.set(identity, check);
  notify({ status: 'confirming' });
  try {
    let sent = false;
    if (input.action === 'plan_approve') {
      sent = await socket?.planApprove(input.sessionId, input.mode, input.workspaceId, requestId, input.plan) ?? false;
    } else if (input.action === 'plan_reject_and_exit') {
      sent = await socket?.planRejectAndExit(input.sessionId) ?? false;
    } else {
      sent = await socket?.planUpdate(input.sessionId, input.plan ?? '') ?? false;
      if (sent && input.action === 'plan_reject') {
        const updated = await readUntil((snapshot) => snapshot.plan === input.plan && snapshot.active);
        if (!updated || !current()) {
          if (current()) notify({ status: 'uncertain' });
          return;
        }
        sent = await socket?.planReject(input.sessionId) ?? false;
        const revised = sent && await readUntil((snapshot) => snapshot.phase === 'revising' || snapshot.status === 'revising');
        if (!revised || !current()) {
          if (current()) notify({ status: 'uncertain' });
          return;
        }
        if (input.feedback) sent = await sendFeedback(requestId);
      }
    }
    if (!current()) return;
    if (!sent) {
      notify({ status: 'unavailable' });
      return;
    }
    await check();
  } catch {
    if (current()) notify({ status: 'uncertain' });
  }
}
