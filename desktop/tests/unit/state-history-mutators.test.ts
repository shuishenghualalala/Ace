/**
 * 历史回填 mutator 单测。
 * 目标：loadBackendHistory 写回消息、失败 error 追加、bookkeeping 重置走 store，
 * 避免 `state.messages[sessionId] = ...` 这类原地写。
 */
import { beforeEach, describe, expect, it } from 'vitest';
import {
  appendSessionMessage,
  ensureSessionMessages,
  replaceSessionMessages,
  resetBook,
  ensureSessionBook,
} from '../../src/ui/state';
import { __resetAllStoresForTest, messageStore, sessionStore } from '../../src/ui/stores/stores';
import type { ChatMessage } from '../../src/ui/chat-render';

function makeMsg(partial: Partial<ChatMessage>): ChatMessage {
  return {
    id: 'm1',
    role: 'user',
    content: 'hi',
    timestamp: 1,
    ...partial,
  };
}

describe('history mutators', () => {
  beforeEach(() => {
    __resetAllStoresForTest();
  });

  it('replaceSessionMessages overwrites the list and returns the same reference semantics', () => {
    const first = ensureSessionMessages('sid-1');
    expect(first).toEqual([]);
    const list = [makeMsg({ id: 'a' }), makeMsg({ id: 'b' })];
    replaceSessionMessages('sid-1', list);
    const stored = messageStore.get().messages['sid-1'];
    expect(stored).toEqual(list);
    expect(stored).not.toBe(first);
  });

  it('replaceSessionMessages does not mutate previous store snapshot', () => {
    const list = [makeMsg({ id: 'a' })];
    replaceSessionMessages('sid-1', list);
    const before = messageStore.get().messages['sid-1'];
    replaceSessionMessages('sid-1', [makeMsg({ id: 'b' })]);
    expect(before).toEqual([makeMsg({ id: 'a' })]);
    expect(messageStore.get().messages['sid-1']).toEqual([makeMsg({ id: 'b' })]);
  });

  it('appendSessionMessage appends to the tail without touching previous entries', () => {
    replaceSessionMessages('sid-1', [makeMsg({ id: 'a' })]);
    appendSessionMessage('sid-1', makeMsg({ id: 'b' }));
    expect(messageStore.get().messages['sid-1'].map((m) => m.id)).toEqual(['a', 'b']);
  });

  it('appendSessionMessage on empty/missing session creates the list', () => {
    appendSessionMessage('sid-1', makeMsg({ id: 'a' }));
    expect(messageStore.get().messages['sid-1']).toEqual([makeMsg({ id: 'a' })]);
  });

  it('resetBook clears toolMap / assistantId / firstChunkAt / user-wait state', () => {
    const book = ensureSessionBook('sid-1');
    book.toolMap.set('tc-1', {
      toolCallId: 'tc-1',
      name: 'search',
      args: '{}',
      status: 'running',
      startedAt: 1,
    });
    book.assistantId = 'assistant-1';
    book.firstChunkAt = 100;
    book.pendingPlan = { plan: 'p', planFile: 'f' };
    expect(sessionStore.get().books['sid-1']).toBe(book);

    resetBook('sid-1');
    const reset = sessionStore.get().books['sid-1'];
    expect(reset).not.toBe(book);
    expect(reset.toolMap.size).toBe(0);
    expect(reset.assistantId).toBeNull();
    expect(reset.firstChunkAt).toBeNull();
    expect(reset.pendingPlan).toBeNull();
  });

  it('resetBook on unknown session still records an entry', () => {
    resetBook('sid-new');
    const created = sessionStore.get().books['sid-new'];
    expect(created).toBeDefined();
    expect(created.assistantId).toBeNull();
  });
});
