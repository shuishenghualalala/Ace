import { describe, expect, it } from 'vitest';

import type { ChatMessage } from '../../src/ui/chat-render';
import { firstChangedMessageIndex, recordMessageChange } from '../../src/ui/conversation-message-changes';
import { emptyFeatureReducerResult } from '../../src/ui/features/event-reducer-registry';
import { applyMessageUpserts } from '../../src/ui/reducers/chat-reducer';
import { appendSessionMessage, replaceSessionMessages, truncateMessagesFrom } from '../../src/ui/state';
import { messageStore } from '../../src/ui/stores/stores';

function message(id: string, content = id): ChatMessage {
  return { id, content, role: 'user', timestamp: 1 };
}

describe('conversation message changes', () => {
  it('finds the earliest change across coalesced appends, patches and truncation', () => {
    const original = [message('first'), message('second'), message('third')];
    const appended = [...original, message('fourth')];
    const patched = [original[0], message('second', 'updated'), ...appended.slice(2)];
    const truncated = patched.slice(0, 3);
    recordMessageChange(original, appended, 3);
    recordMessageChange(appended, patched, 1);
    recordMessageChange(patched, truncated, 3);

    expect(firstChangedMessageIndex(original, appended)).toBe(3);
    expect(firstChangedMessageIndex(original, truncated)).toBe(1);
    expect(firstChangedMessageIndex(appended, truncated)).toBe(1);
    expect(firstChangedMessageIndex(truncated, truncated)).toBe(truncated.length);
  });

  it('only follows ancestors after a fork and never infers continuity across replacement', () => {
    const original = [message('a'), message('b')];
    const left = [...original, message('left')];
    const right = [...original, message('right')];
    const descendant = [...right, message('descendant')];
    const replacement = [...original];
    const later = [...replacement, message('later')];
    recordMessageChange(original, left, 2);
    recordMessageChange(original, right, 2);
    recordMessageChange(right, descendant, 3);
    recordMessageChange(replacement, later, 2);

    expect(firstChangedMessageIndex(original, descendant)).toBe(2);
    expect(firstChangedMessageIndex(left, descendant)).toBeUndefined();
    expect(firstChangedMessageIndex(right, left)).toBeUndefined();
    expect(firstChangedMessageIndex(descendant, original)).toBeUndefined();
    expect(firstChangedMessageIndex(original, replacement)).toBeUndefined();
    expect(firstChangedMessageIndex(original, later)).toBeUndefined();
    expect(firstChangedMessageIndex(replacement, later)).toBe(2);
  });

  it('falls back after retained links are overwritten and still tracks new changes', () => {
    const original = [message('a')];
    let previous = original;
    for (let index = 0; index < 512; index += 1) {
      const next = [message('a', `revision ${index}`)];
      recordMessageChange(previous, next, 0);
      previous = next;
    }
    const latest = [...previous, message('b')];
    recordMessageChange(previous, latest, 1);

    expect(firstChangedMessageIndex(original, latest)).toBeUndefined();
    expect(firstChangedMessageIndex(previous, latest)).toBe(1);
  });

  it('records store append and truncation before notifying subscribers', () => {
    const sid = 'message-change-subscribe';
    replaceSessionMessages(sid, [message('a'), message('b')]);
    const observed: Array<number | undefined> = [];
    const unsubscribe = messageStore.subscribe((next, previous) => {
      observed.push(firstChangedMessageIndex(previous.messages[sid], next.messages[sid]));
    });
    try {
      appendSessionMessage(sid, message('c'));
      expect(truncateMessagesFrom(sid, -2).map(({ id }) => id)).toEqual(['b', 'c']);
      replaceSessionMessages(sid, [message('replacement')]);
    } finally {
      unsubscribe();
    }
    expect(observed).toEqual([2, 1, undefined]);
  });

  it('tracks current reducer operations while preserving duplicate-ID matching and wholesale replacement', () => {
    const original = [message('first'), message('remove'), message('same'), message('same')];
    const changed = applyMessageUpserts(original, {
      ...emptyFeatureReducerResult(),
      messageUpserts: [
        { op: 'remove', messageId: 'remove' },
        { op: 'patch', messageId: 'same', patch: { content: 'new text' } },
        { op: 'append', message: message('last') },
      ],
    });
    const untouched = applyMessageUpserts(changed, {
      ...emptyFeatureReducerResult(),
      messageUpserts: [{ op: 'patch', messageId: 'missing', patch: { content: 'absent' } }],
    });
    const replaced = applyMessageUpserts(changed, {
      ...emptyFeatureReducerResult(),
      replaceMessages: [message('history')],
      messageUpserts: [{ op: 'append', message: message('new') }],
    });

    expect(original.map(({ content }) => content)).toEqual(['first', 'remove', 'same', 'same']);
    expect(changed).toEqual([original[0], message('same', 'new text'), message('same', 'new text'), message('last')]);
    expect(firstChangedMessageIndex(original, changed)).toBe(1);
    expect(untouched).toEqual(changed);
    expect(firstChangedMessageIndex(changed, untouched)).toBe(changed.length);
    expect(replaced.map(({ id }) => id)).toEqual(['history', 'new']);
    expect(firstChangedMessageIndex(changed, replaced)).toBeUndefined();
  });
});
