import type { TrayStatus } from '../../shared/types';
import { sessionStore, type SessionStoreState } from '../stores/session-store';

/** 连续无任务 5 分钟后进入休眠态；后续可由设计系统统一调整。 */
export const TRAY_REST_TIMEOUT_MS = 5 * 60 * 1000;

export interface TrayStatusSnapshot {
  busySessions: Record<string, boolean>;
  sessionStatuses: SessionStoreState['sessionStatuses'];
  books: SessionStoreState['books'];
  unreadCompletedSessions: Set<string>;
}

export interface TrayStatusResolutionInput {
  hasNotification: boolean;
  hasWorkingSession: boolean;
  hasDoneSession: boolean;
  idleForMs: number;
}

/** 菜单栏状态优先级：通知 > 工作中 > 完成 > 休眠 > 默认。 */
export function resolveTrayStatus(input: TrayStatusResolutionInput): TrayStatus {
  if (input.hasNotification) return 'notification';
  if (input.hasWorkingSession) return 'working';
  if (input.hasDoneSession) return 'done';
  if (input.idleForMs >= TRAY_REST_TIMEOUT_MS) return 'rest';
  return 'default';
}

export function hasWorkingSession(snapshot: TrayStatusSnapshot): boolean {
  const sessionIds = new Set([
    ...Object.keys(snapshot.busySessions),
    ...Object.keys(snapshot.sessionStatuses),
  ]);
  return Array.from(sessionIds).some((id) =>
    snapshot.busySessions[id] === true
    || snapshot.sessionStatuses[id] === 'running'
    || snapshot.sessionStatuses[id] === 'queued',
  );
}

/** 追问/计划审批是需要用户处理的待办，按会话和问题 id 形成稳定 key。 */
export function pendingAttentionKeys(snapshot: TrayStatusSnapshot): string[] {
  const keys: string[] = [];
  for (const [sessionId, book] of Object.entries(snapshot.books)) {
    if (book.pendingPlan?.status === 'pending') keys.push(`plan:${sessionId}`);
    if (book.pendingFollowup) keys.push(`followup:${sessionId}:${book.pendingFollowup.questionId}`);
  }
  return keys;
}

let initialized = false;
let trayTimer: ReturnType<typeof setTimeout> | null = null;
let idleSince: number | null = null;
let lastStatus: TrayStatus | null = null;
let latchedNotification = false;
const completedSessionIds = new Set<string>();
const acknowledgedNotificationKeys = new Set<string>();

function snapshot(): TrayStatusSnapshot {
  const state = sessionStore.get();
  return {
    busySessions: state.busySessions,
    sessionStatuses: state.sessionStatuses,
    books: state.books,
    unreadCompletedSessions: state.unreadCompletedSessions,
  };
}

function sendStatus(status: TrayStatus): void {
  if (lastStatus === status) return;
  lastStatus = status;
  const request = window.Crew?.traySetStatus?.(status);
  if (!request) return;
  void request.catch((error: unknown) => {
    console.warn('[tray] failed to update status:', error);
  });
}

function scheduleRestTransition(now: number): void {
  if (trayTimer) clearTimeout(trayTimer);
  trayTimer = null;
  if (idleSince === null) return;
  const remaining = TRAY_REST_TIMEOUT_MS - (now - idleSince);
  if (remaining <= 0) return;
  trayTimer = setTimeout(() => {
    trayTimer = null;
    refreshTrayStatus();
  }, remaining);
}

function refreshTrayStatus(now = Date.now()): void {
  const current = snapshot();
  const attentionKeys = pendingAttentionKeys(current);
  const hasNotification = latchedNotification
    || attentionKeys.some((key) => !acknowledgedNotificationKeys.has(key));
  const working = hasWorkingSession(current);
  const hasDone = completedSessionIds.size > 0 || current.unreadCompletedSessions.size > 0;

  if (working || hasNotification || hasDone) {
    idleSince = null;
  } else {
    idleSince ??= now;
  }

  sendStatus(resolveTrayStatus({
    hasNotification,
    hasWorkingSession: working,
    hasDoneSession: hasDone,
    idleForMs: idleSince === null ? 0 : now - idleSince,
  }));
  scheduleRestTransition(now);
}

function acknowledgeTrayState(): void {
  latchedNotification = false;
  completedSessionIds.clear();
  for (const key of pendingAttentionKeys(snapshot())) acknowledgedNotificationKeys.add(key);
  if (sessionStore.get().unreadCompletedSessions.size > 0) {
    sessionStore.set({ unreadCompletedSessions: new Set() });
  }
  refreshTrayStatus();
}

function handleSessionStoreChange(next: SessionStoreState, previous: SessionStoreState): void {
  const ids = new Set([
    ...Object.keys(previous.busySessions),
    ...Object.keys(next.busySessions),
    ...Object.keys(previous.sessionStatuses),
    ...Object.keys(next.sessionStatuses),
  ]);
  for (const id of ids) {
    const becameIdle = previous.busySessions[id] === true
      && next.busySessions[id] !== true
      && next.sessionStatuses[id] === 'idle';
    if (becameIdle) completedSessionIds.add(id);
    if (next.busySessions[id] === true) {
      completedSessionIds.delete(id);
      acknowledgedNotificationKeys.delete(`plan:${id}`);
    }
  }
  refreshTrayStatus();
}

/** 初始化 Renderer → Main 的菜单栏状态同步。 */
export function initSystemTrayStatus(): () => void {
  if (initialized) return () => undefined;
  initialized = true;
  const unsubscribeSession = sessionStore.subscribe(handleSessionStoreChange);
  const unsubscribeTray = window.Crew?.onTrayActivated?.(acknowledgeTrayState) ?? (() => undefined);
  refreshTrayStatus();
  return () => {
    unsubscribeSession();
    unsubscribeTray();
    if (trayTimer) clearTimeout(trayTimer);
    trayTimer = null;
    initialized = false;
    idleSince = null;
    lastStatus = null;
    latchedNotification = false;
    completedSessionIds.clear();
    acknowledgedNotificationKeys.clear();
  };
}

/** Web Notification / Work 事件调用此函数，保持菜单栏通知态直到用户点击菜单栏图标。 */
export function markSystemTrayNotification(): void {
  latchedNotification = true;
  refreshTrayStatus();
}
