import type { ChatMessage } from './chat-render';

export type TurnOutcomeStatus = 'completed' | 'partial' | 'failed' | 'stopped' | 'awaiting_input';

export interface TurnOutcome {
  status: TurnOutcomeStatus;
  reason?: string;
  code?: string;
  category?: string;
  retryable?: boolean;
  retry_delay?: number;
  message?: string;
}

export interface SettledTurnOutcome extends TurnOutcome {
  request_id: string;
  timestamp?: number;
}

function shortString(value: unknown, maximum: number): string | undefined {
  return typeof value === 'string' && value.trim() ? value.trim().slice(0, maximum) : undefined;
}

export function readTurnOutcome(value: unknown, fallbackMessage?: unknown): TurnOutcome | undefined {
  if (typeof value !== 'object' || value === null) return undefined;
  const raw = value as Record<string, unknown>;
  const status = raw.status;
  if (status !== 'completed' && status !== 'partial' && status !== 'failed'
    && status !== 'stopped' && status !== 'awaiting_input') return undefined;

  const reason = shortString(raw.reason, 120);
  const code = shortString(raw.code, 120);
  const category = shortString(raw.category, 120);
  const message = shortString(raw.message, 800) ?? shortString(fallbackMessage, 800);
  const retryDelay = raw.retry_delay;
  return {
    status,
    ...(reason ? { reason } : {}),
    ...(code ? { code } : {}),
    ...(category ? { category } : {}),
    ...(typeof raw.retryable === 'boolean' ? { retryable: raw.retryable } : {}),
    ...(typeof retryDelay === 'number' && Number.isFinite(retryDelay) && retryDelay >= 0
      ? { retry_delay: retryDelay } : {}),
    ...(message ? { message } : {}),
  };
}

export function readSettledTurnOutcome(value: unknown, fallbackMessage?: unknown): SettledTurnOutcome | undefined {
  const outcome = readTurnOutcome(value, fallbackMessage);
  if (!outcome || !value) return undefined;
  const raw = value as Record<string, unknown>;
  const requestId = typeof raw.request_id === 'string' && raw.request_id.trim() ? raw.request_id : undefined;
  if (!requestId) return undefined;
  const timestamp = typeof raw.timestamp === 'number' && Number.isFinite(raw.timestamp) && raw.timestamp > 0
    ? raw.timestamp : undefined;
  return { ...outcome, request_id: requestId, ...(timestamp !== undefined ? { timestamp } : {}) };
}

export function isInterruptedOutcome(outcome: TurnOutcome | undefined): boolean {
  return outcome?.status === 'partial' || outcome?.status === 'stopped';
}

export function isOutcomeOnlyMessage(message: ChatMessage): boolean {
  return message.role === 'assistant' && Boolean(message.turnOutcome)
    && !message.content.trim() && !message.thinking?.trim() && !message.toolCalls?.length && !message.planReview;
}

export function startsAnotherOutcomeTurn(requestId: string | undefined, next: ChatMessage, previous?: ChatMessage): boolean {
  if (previous?.requestId && isOutcomeOnlyMessage(previous) && next.requestId !== previous.requestId) return true;
  if (!next.requestId) return false;
  return requestId ? next.requestId !== requestId : isOutcomeOnlyMessage(next);
}

export function userTurnOutcomeMarker(message: ChatMessage): ChatMessage | undefined {
  if (message.role !== 'user' || !message.requestId || !message.turnOutcome
    || message.turnOutcome.status === 'completed') return undefined;
  return {
    id: 'user-outcome-' + encodeURIComponent(message.historyId ?? message.requestId),
    role: 'assistant',
    content: '',
    timestamp: message.timestamp,
    requestId: message.requestId,
    turnOutcome: message.turnOutcome,
  };
}
