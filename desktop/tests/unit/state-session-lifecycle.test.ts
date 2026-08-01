/**
 * 会话生命周期（删除 / 订阅清理 / suppress）store mutator 单测。
 *
 * @vitest-environment happy-dom
 */
import { beforeEach, describe, expect, it } from 'vitest';
import {
  addSubscribedSessions,
  addSuppressedSession,
  enqueuePending,
  ensureSessionBook,
  ensureSessionMessages,
  loadFromStorage,
  setActiveSessionId,
  setBusy,
  setEditFrom,
  setQueueHint,
  setSessionStatus,
  removeSessionState,
  removeSubscribedSession,
  replaceSessionMessages,
} from '../../src/ui/state';
import { __resetAllStoresForTest, messageStore, sessionStore } from '../../src/ui/stores/stores';

const LAST_ACTIVE_SESSION_KEY = 'crew.lastActiveSession';

beforeEach(() => {
  __resetAllStoresForTest();
});

describe('session lifecycle mutators', () => {
  it('removeSessionState clears every per-session slot', () => {
    ensureSessionMessages('sid-1');
    setBusy('sid-1', true);
    setSessionStatus('sid-1', 'running');
    setQueueHint('sid-1', '排队中');
    ensureSessionBook('sid-1');
    setEditFrom('sid-1', 3);
    addSuppressedSession('sid-1');
    enqueuePending('sid-1', { id: 'q1', query: 'foo', attachments: [] });
    addSubscribedSessions(['sid-1', 'sid-2']);

    removeSessionState('sid-1');

    expect(messageStore.get().messages['sid-1']).toBeUndefined();
    expect(messageStore.get().queueHints['sid-1']).toBeUndefined();
    expect(messageStore.get().pendingQueues['sid-1']).toBeUndefined();
    expect(sessionStore.get().books['sid-1']).toBeUndefined();
    expect(sessionStore.get().sessionStatuses['sid-1']).toBeUndefined();
    expect(sessionStore.get().busySessions['sid-1']).toBeUndefined();
    expect(sessionStore.get().editFromIdx['sid-1']).toBeUndefined();
    expect(sessionStore.get().suppressChunks.has('sid-1')).toBe(false);
    // 其它 session 保持
    expect(sessionStore.get().subscribedSessions.has('sid-2')).toBe(true);
  });

  it('removeSessionState on unknown session is a no-op', () => {
    replaceSessionMessages('sid-real', [{ id: 'm', role: 'user', content: 'hi', timestamp: 1 }]);
    removeSessionState('sid-unknown');
    expect(messageStore.get().messages['sid-real'].length).toBe(1);
  });

  it('removeSubscribedSession prunes the local subscription set', () => {
    addSubscribedSessions(['a', 'b', 'c']);
    removeSubscribedSession('b');
    expect(Array.from(sessionStore.get().subscribedSessions)).toEqual(['a', 'c']);
  });
});

describe('setActiveSessionId persistence', () => {
  beforeEach(() => {
    localStorage.removeItem(LAST_ACTIVE_SESSION_KEY);
  });

  it('writes last active session id to localStorage', () => {
    setActiveSessionId('session-abc');
    expect(sessionStore.get().activeSessionId).toBe('session-abc');
    expect(loadFromStorage<string | null>(LAST_ACTIVE_SESSION_KEY, null)).toBe('session-abc');
  });

  it('removes last active session key when id is null', () => {
    setActiveSessionId('session-abc');
    expect(loadFromStorage<string | null>(LAST_ACTIVE_SESSION_KEY, null)).toBe('session-abc');
    setActiveSessionId(null);
    expect(sessionStore.get().activeSessionId).toBeNull();
    expect(localStorage.getItem(LAST_ACTIVE_SESSION_KEY)).toBeNull();
  });
});
