/**
 * 侧栏会话历史的挂载 helper：重置 document.body，挂一个 #session-history-root 宿主，
 * 并通过 mountSessionHistoryView 挂生产 SessionHistoryView 单例（DOM 契约为
 * mw-session-history__*）。renderWorkspaceHistory / patchSessionRowStatus 兼容层
 * 都作用于该单例；重复挂载会自动 dispose 前一个实例，保证测试隔离。
 */
import { mountSessionHistoryView } from '../../../src/ui/features/session-history-view';

export interface MountHistoryListOptions {
  openSession?: (sessionId: string) => void;
}

export function mountHistoryList(options: MountHistoryListOptions = {}): HTMLElement {
  const host = document.createElement('div');
  host.id = 'session-history-root';
  document.body.innerHTML = '';
  document.body.appendChild(host);
  mountSessionHistoryView(host, {
    openSession: options.openSession ?? (() => {}),
    createSession: () => {},
    createWorkspace: () => {},
    manageHistory: () => {},
    openWorkspace: () => {},
    refreshSessions: async () => undefined,
    retrySessions: async () => undefined,
    retryWorkspaces: async () => undefined,
    getLoadErrors: () => ({ sessions: null, workspaces: null }),
  });
  return host;
}
