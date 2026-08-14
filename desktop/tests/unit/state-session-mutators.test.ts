// @vitest-environment happy-dom

/**
 * 关键状态 mutator 单测。
 * 目标：把 openSession 周边依赖的关键写入口从 Proxy/nested mutation 收口到显式 helper。
 */
import { beforeEach, describe, expect, it } from 'vitest';
import {
  addSubscribedSessions,
  ensureSessionBook,
  ensureSessionMessages,
  isBusySession,
  setCurrentWorkspaceId,
  setQueueHint,
  setSessionStatus,
  setBusy,
  setActiveSessionId,
} from '../../src/ui/state';
import { __resetAllStoresForTest, messageStore, sessionStore, workspaceStore } from '../../src/ui/stores/stores';

describe('session mutators', () => {
  beforeEach(() => {
    __resetAllStoresForTest();
  });

  it('ensureSessionMessages creates and reuses the message array', () => {
    const first = ensureSessionMessages('sid-1');
    expect(first).toEqual([]);
    expect(messageStore.get().messages['sid-1']).toBe(first);

    const second = ensureSessionMessages('sid-1');
    expect(second).toBe(first);
  });

  it('ensureSessionBook creates and reuses bookkeeping', () => {
    const first = ensureSessionBook('sid-1');
    expect(first.assistantId).toBeNull();
    expect(first.firstChunkAt).toBeNull();
    expect(first.pendingPlan).toBeNull();
    expect(first.pendingFollowup).toBeNull();
    expect(first.toolMap.size).toBe(0);
    expect(sessionStore.get().books['sid-1']).toBe(first);

    const second = ensureSessionBook('sid-1');
    expect(second).toBe(first);
  });

  it('setBusy / isBusySession roundtrip via sessionStore', () => {
    expect(isBusySession('sid-1')).toBe(false);
    setBusy('sid-1', true);
    expect(isBusySession('sid-1')).toBe(true);
    expect(sessionStore.get().busySessions['sid-1']).toBe(true);
  });

  it('per-session setters write their respective store maps', () => {
    setSessionStatus('sid-1', 'queued');
    expect(sessionStore.get().sessionStatuses['sid-1']).toBe('queued');

    setQueueHint('sid-1', '正在排队…');
    expect(messageStore.get().queueHints['sid-1']).toBe('正在排队…');

    setCurrentWorkspaceId('ws-2');
    expect(workspaceStore.get().currentWorkspaceId).toBe('ws-2');
  });

  it('addSubscribedSessions dedupes and returns the full set snapshot', () => {
    expect(addSubscribedSessions(['a', 'b', 'a', ''])).toEqual(['a', 'b']);
    expect(Array.from(sessionStore.get().subscribedSessions)).toEqual(['a', 'b']);
    expect(addSubscribedSessions(['b', 'c'])).toEqual(['a', 'b', 'c']);
    expect(Array.from(sessionStore.get().subscribedSessions)).toEqual(['a', 'b', 'c']);
  });

  it('announces a session change before replacing activeSessionId', () => {
    setActiveSessionId('session-old');
    let activeDuringEvent: string | null = null;
    const listener = (): void => {
      activeDuringEvent = sessionStore.get().activeSessionId;
    };
    window.addEventListener('session:changing', listener);

    setActiveSessionId('session-new');

    window.removeEventListener('session:changing', listener);
    expect(activeDuringEvent).toBe('session-old');
    expect(sessionStore.get().activeSessionId).toBe('session-new');
  });
});
