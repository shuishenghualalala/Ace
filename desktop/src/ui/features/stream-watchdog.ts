/**
 * 运行中会话 watchdog：长时间无新 chunk 时拉 status / 触发 replay 恢复。
 */

import { backendApi } from '../backend-client';
import { addSubscribedSessions, isBusySession, state } from '../state';
import { sessionStore } from '../stores/stores';
import { getLastGatewaySequences, getLastStreamActivity, clearStreamActivity, touchStreamActivity } from './gateway-sequence';
import { logStream } from '../stream-debug';
import { syncSessionLiveFromBackend } from './session-busy';

const WATCHDOG_STALL_MS = 60_000;
const WATCHDOG_TICK_MS = 15_000;

let watchdogTimer: number | null = null;
const recovering = new Set<string>();

function collectWatchTargets(): string[] {
  const sids = new Set<string>(state.subscribedSessions);
  for (const sid of Object.keys(state.messages)) {
    if (isBusySession(sid)) sids.add(sid);
  }
  return Array.from(sids).filter(Boolean);
}

async function recoverStalledSession(sessionId: string): Promise<void> {
  if (recovering.has(sessionId)) return;
  recovering.add(sessionId);
  logStream('watchdog', 'stall-detected', { sessionId });
  try {
    const st = await backendApi.sessionStatus(sessionId);
    syncSessionLiveFromBackend(sessionId, st?.live, st?.last_status, st?.active_request_id);
    if (st?.live === 'idle' || st?.live === 'failed' || !st?.live) {
      const { loadBackendHistory } = await import('./session-controller');
      await loadBackendHistory(sessionId);
      return;
    }
    if (st?.live === 'running' || st?.live === 'queued') {
      const sessions = addSubscribedSessions([sessionId]);
      void state.socket?.subscribe(sessions, getLastGatewaySequences(sessions));
      touchStreamActivity(sessionId);
      logStream('watchdog', 'resubscribe-replay', { sessionId, live: st.live });
    }
  } catch (err) {
    logStream('watchdog', 'recover-failed', { sessionId, error: String(err) });
  } finally {
    recovering.delete(sessionId);
  }
}

function tickWatchdog(): void {
  const now = Date.now();
  for (const sid of collectWatchTargets()) {
    if (!isBusySession(sid)) {
      clearStreamActivity(sid);
      continue;
    }
    const book = sessionStore.get().books[sid];
    const last = getLastStreamActivity(sid) ?? book?.firstChunkAt ?? now;
    if (now - last >= WATCHDOG_STALL_MS) {
      void recoverStalledSession(sid);
    }
  }
}

/** 在 bootstrapBackend 后启动；重复调用幂等。 */
export function startStreamWatchdog(): void {
  if (typeof window === 'undefined') return;
  if (watchdogTimer !== null) return;
  watchdogTimer = window.setInterval(tickWatchdog, WATCHDOG_TICK_MS);
}

export function stopStreamWatchdog(): void {
  if (watchdogTimer !== null) {
    window.clearInterval(watchdogTimer);
    watchdogTimer = null;
  }
}

/** 单测：重置内部状态。 */
export function _resetWatchdogForTests(): void {
  stopStreamWatchdog();
  recovering.clear();
}
