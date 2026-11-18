import type { ChatMessage } from './state';
import { interactiveOwnerKey } from './features/interactive-owner';
import { isStreamDebugEnabled, logStream } from './stream-debug';

interface Timing {
  sid: string;
  owner: string;
  requestId: string;
  started: number;
  received: boolean;
  assistantId?: string;
  mounted: boolean;
  frameTarget?: HTMLElement;
  opportunityLogged: boolean;
}

// Only locally dispatched turns have a known start. Keep the latest turn for each
// session so delayed chunks and animation callbacks cannot claim a later send.
const timings = new Map<string, Timing>();
const maxSessions = 80;

export function beginFirstText(sid: string, requestId: string): void {
  if (!isStreamDebugEnabled()) {
    timings.delete(sid);
    return;
  }
  const owner = interactiveOwnerKey();
  const existing = timings.get(sid);
  if (existing?.requestId === requestId && existing.owner === owner) return;
  timings.delete(sid);
  timings.set(sid, { sid, owner, requestId, started: performance.now(), received: false, mounted: false, opportunityLogged: false });
  if (timings.size > maxSessions) timings.delete(timings.keys().next().value!);
}

function currentTiming(sid: string): Timing | undefined {
  const turn = timings.get(sid);
  return turn && turn.owner === interactiveOwnerKey() ? turn : undefined;
}

export function discardFirstText(sid: string, requestId: string): void {
  if (currentTiming(sid)?.requestId === requestId) timings.delete(sid);
}

export function endFirstText(sid: string, requestId: string | undefined): void {
  const turn = currentTiming(sid);
  if (turn && turn.requestId === requestId && !turn.assistantId) timings.delete(sid);
}

function record(turn: Timing, scope: 'apply-chunk' | 'render', event: string, detail: Record<string, unknown>): void {
  logStream(scope, event, { sid: turn.sid, request_id: turn.requestId, elapsedMs: Math.round(performance.now() - turn.started), ...detail });
}

export function receiveFirstText(sid: string, requestId: string | undefined, length: number): void {
  const turn = currentTiming(sid);
  if (!turn || turn.received || turn.requestId !== requestId || !length) return;
  turn.received = true;
  record(turn, 'apply-chunk', 'ttft-first-text-received', { textLen: length });
}

export function acceptFirstText(
  sid: string, requestId: string | undefined, assistantId: string,
  previous: ChatMessage[], next: ChatMessage[], isForeground: () => boolean,
): void {
  const turn = currentTiming(sid);
  if (!turn || turn.mounted || turn.requestId !== requestId || turn.assistantId === assistantId) return;
  const content = next.find(message => message.id === assistantId)?.content;
  if (!content?.trim() || content === previous.find(message => message.id === assistantId)?.content) return;
  if (!turn.assistantId) record(turn, 'apply-chunk', 'ttft-first-text-accepted', { assistantId, foreground: isForeground() });
  turn.assistantId = assistantId;
}

export function observeFirstText(
  sid: string, source: 'patch' | 'render', getContainer: () => HTMLElement | null,
): void {
  const turn = currentTiming(sid);
  if (!turn?.assistantId || turn.opportunityLogged || !isStreamDebugEnabled()) return;
  const container = getContainer();
  if (!container || document.visibilityState === 'hidden') return;
  const element = container.querySelector<HTMLElement>(`[data-text-for="${CSS.escape(turn.assistantId)}"]`);
  if (!element?.isConnected || !element.textContent?.trim() || element.closest('[hidden]')) return;
  const visible = () => typeof element.checkVisibility !== 'function'
    || element.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true });
  if (!visible()) return;
  if (!turn.mounted) {
    turn.mounted = true;
    record(turn, 'render', 'ttft-first-text-mounted', { assistantId: turn.assistantId, source });
  }
  if (turn.frameTarget === element) return;
  turn.frameTarget = element;
  const stillMounted = () => currentTiming(sid) === turn && turn.frameTarget === element
    && document.visibilityState !== 'hidden' && getContainer()?.contains(element)
    && !!element.textContent?.trim() && !element.closest('[hidden]') && visible();
  requestAnimationFrame(() => {
    if (!stillMounted()) {
      if (turn.frameTarget === element) delete turn.frameTarget;
      return;
    }
    requestAnimationFrame(() => {
      if (stillMounted() && !turn.opportunityLogged && isStreamDebugEnabled()) {
        turn.opportunityLogged = true;
        record(turn, 'render', 'ttft-first-paint-opportunity', { assistantId: turn.assistantId });
      }
      if (turn.frameTarget === element) delete turn.frameTarget;
    });
  });
}
