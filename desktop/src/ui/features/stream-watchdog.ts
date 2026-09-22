/**
 * 运行中会话 watchdog：长时间无新 chunk 时拉 status / 触发 replay 恢复。
 *
 * 恢复梯子（P0-3）：静默自愈优先（对用户不可见）；停滞在自愈动作完成后仍复发
 * 达两次（恢复动作本身会 touch 活动时间戳，只有「晚于恢复动作的新活动」才算自愈
 * 生效）→ 升级可见：写入一条系统错误消息并停止自动重试，直到该会话重新出现
 * 流式活动或转入空闲再自动复位。
 */

import { backendApi } from '../backend-client';
import {
  addSubscribedSessions,
  appendSessionMessage,
  isBusySession,
  newMessageId,
  notify,
  state,
} from '../state';
import { sessionStore } from '../stores/stores';
import { getLastGatewaySequences, getLastStreamActivity, clearStreamActivity, touchStreamActivity } from './gateway-sequence';
import { logStream } from '../stream-debug';
import { syncSessionLiveFromBackend } from './session-busy';

const WATCHDOG_STALL_MS = 60_000;
const WATCHDOG_TICK_MS = 15_000;
/** 自愈无效达到该次数后升级为可见错误。 */
const WATCHDOG_ESCALATE_AFTER = 2;

let watchdogTimer: number | null = null;
const recovering = new Set<string>();
/** 自愈无效计数（按会话）：停滞在恢复动作完成后复发才累计。 */
const recoveryFailures = new Map<string, number>();
/** 最近一次自愈动作完成时间：晚于它的活动时间戳才是「真实新活动」。 */
const lastRecoveryAt = new Map<string, number>();
/** 已写入可见错误的会话（避免重复追加错误消息）。 */
const escalated = new Set<string>();

function collectWatchTargets(): string[] {
  const sids = new Set<string>(state.subscribedSessions);
  for (const sid of Object.keys(state.messages)) {
    if (isBusySession(sid)) sids.add(sid);
  }
  return Array.from(sids).filter(Boolean);
}

/** 升级可见：写入系统错误消息 + toast，停止对该会话的静默重试。 */
function escalateStalledSession(sessionId: string): void {
  if (escalated.has(sessionId)) return;
  escalated.add(sessionId);
  logStream('watchdog', 'escalated', { sessionId });
  appendSessionMessage(sessionId, {
    id: newMessageId('error'),
    role: 'error',
    content: '输出流长时间无响应，自动恢复未成功，已暂停重试。可重新发送消息或稍后重试。',
    timestamp: Date.now(),
  });
  notify('会话输出流长时间无响应，已暂停自动恢复');
  if (state.activeSessionId === sessionId) {
    void import('./chat-controller').then(({ renderChat }) => renderChat());
  }
}

/** 出现真实新活动 / 会话转入空闲后复位升级态，恢复静默自愈资格。 */
function resetEscalation(sessionId: string): void {
  if (!recoveryFailures.has(sessionId) && !escalated.has(sessionId)) return;
  recoveryFailures.delete(sessionId);
  escalated.delete(sessionId);
  logStream('watchdog', 'escalation-reset', { sessionId });
}

async function recoverStalledSession(sessionId: string): Promise<void> {
  if (recovering.has(sessionId)) return;
  recovering.add(sessionId);
  logStream('watchdog', 'stall-detected', { sessionId, failures: recoveryFailures.get(sessionId) ?? 0 });
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
      resetEscalation(sid);
      lastRecoveryAt.delete(sid);
      continue;
    }
    const book = sessionStore.get().books[sid];
    const last = getLastStreamActivity(sid) ?? book?.firstChunkAt ?? now;
    if (now - last < WATCHDOG_STALL_MS) {
      // 晚于上次自愈动作的新活动 = 自愈生效（自愈内部的 touch 不晚于动作完成时间）。
      if (last > (lastRecoveryAt.get(sid) ?? 0)) resetEscalation(sid);
      continue;
    }
    if (escalated.has(sid)) continue; // 已升级：等新活动复位，不再自动重试
    const failures = recoveryFailures.get(sid) ?? 0;
    if (failures >= WATCHDOG_ESCALATE_AFTER) {
      escalateStalledSession(sid);
      continue;
    }
    void recoverStalledSession(sid).then(() => {
      lastRecoveryAt.set(sid, Date.now());
      recoveryFailures.set(sid, (recoveryFailures.get(sid) ?? 0) + 1);
    });
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
  recoveryFailures.clear();
  lastRecoveryAt.clear();
  escalated.clear();
}

/** 单测：读取升级态。 */
export function _watchdogEscalatedForTests(): Set<string> {
  return escalated;
}
