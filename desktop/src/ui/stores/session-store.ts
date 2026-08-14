/**
 * session-store：会话列表 / 当前活跃会话 / 每会话状态 / busy / 订阅 / 折页 / 编辑起点
 */
import { createStore, type Store } from '../reducers/store-bus';
import type { BackendSession } from '../backend-client';
import type { SessionStatus } from '../chat-render';
import type { Bookkeeping, SessionRow } from '../state';

export interface SessionStoreState {
  sessions: SessionRow[];
  backendSessions: BackendSession[];
  activeSessionId: string | null;
  sessionStatuses: Record<string, SessionStatus>;
  busySessions: Record<string, boolean>;
  subscribedSessions: Set<string>;
  books: Record<string, Bookkeeping>;
  suppressChunks: Set<string>;
  editFromIdx: Record<string, number>;
  userFoldedTurns: Set<string>;
  userUnfoldedTurns: Set<string>;
  /** 后台已完成、用户尚未点进查看的会话（侧栏行尾显示绿点）。 */
  unreadCompletedSessions: Set<string>;
}

export const sessionStore: Store<SessionStoreState> = createStore<SessionStoreState>(
  {
    sessions: [],
    backendSessions: [],
    activeSessionId: null,
    sessionStatuses: {},
    busySessions: {},
    subscribedSessions: new Set(),
    books: {},
    suppressChunks: new Set(),
    editFromIdx: {},
    userFoldedTurns: new Set(),
    userUnfoldedTurns: new Set(),
    unreadCompletedSessions: new Set(),
  },
  'session',
);
