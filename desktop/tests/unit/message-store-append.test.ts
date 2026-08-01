/**
 * D5 regression: appendMessage must publish to messageStore (subscribers fire).
 *
 * Previously appendMessage did `getMessages(sessionId).push(message)`, which
 * mutated the live store array WITHOUT messageStore.set — so subscribers never
 * fired and a concurrent immutable update (e.g. patchMessage) could clobber
 * the appended message. appendMessage now delegates to appendSessionMessage,
 * which does an immutable messageStore.set. This test exercises that mutator
 * directly and asserts the store subscriber callback fires.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { appendSessionMessage } from '../../src/ui/state';
import { __resetAllStoresForTest, messageStore } from '../../src/ui/stores/stores';
import type { ChatMessage } from '../../src/ui/chat-render';

function makeMessage(id: string, role: ChatMessage['role']): ChatMessage {
  return { id, role, content: `hello-${id}`, timestamp: Date.now(), model: 'test-model' };
}

describe('appendSessionMessage (D5: subscribers fire on append)', () => {
  beforeEach(() => {
    __resetAllStoresForTest();
  });

  it('fires the messageStore subscriber exactly once per append', () => {
    let callCount = 0;
    let lastSeen: ChatMessage[] | null = null;
    const unsub = messageStore.subscribe((next) => {
      callCount += 1;
      lastSeen = next.messages['sid-1'] ?? null;
    });

    appendSessionMessage('sid-1', makeMessage('m1', 'user'));

    expect(callCount).toBe(1);
    expect(lastSeen).not.toBeNull();
    expect(lastSeen!.length).toBe(1);
    expect(lastSeen![0].id).toBe('m1');

    appendSessionMessage('sid-1', makeMessage('m2', 'assistant'));
    expect(callCount).toBe(2);
    expect(lastSeen!.length).toBe(2);
    expect(lastSeen![1].id).toBe('m2');

    unsub();
  });

  it('appends without mutating the previously-published array reference', () => {
    appendSessionMessage('sid-1', makeMessage('m1', 'user'));
    const before = messageStore.get().messages['sid-1']!;
    const beforeLen = before.length;

    appendSessionMessage('sid-1', makeMessage('m2', 'assistant'));
    const after = messageStore.get().messages['sid-1']!;

    // The old reference is untouched (immutable update, not in-place push).
    expect(before.length).toBe(beforeLen);
    expect(after).not.toBe(before);
    expect(after.length).toBe(2);
  });

  it('does not clobber an append when a second immutable update follows', () => {
    // Reproduces the original race: append (mutate, no publish) then an
    // immutable patchMessage-style set that read the pre-append list. With the
    // fix, append publishes first so the second set sees both messages.
    appendSessionMessage('sid-1', makeMessage('m1', 'user'));
    // Simulate a concurrent immutable rewrite that reads current state.
    const cur = messageStore.get().messages['sid-1'] ?? [];
    messageStore.set({ messages: { ...messageStore.get().messages, ['sid-1']: [...cur] } });

    const final = messageStore.get().messages['sid-1']!;
    expect(final.length).toBe(1);
    expect(final[0].id).toBe('m1');
  });
});
