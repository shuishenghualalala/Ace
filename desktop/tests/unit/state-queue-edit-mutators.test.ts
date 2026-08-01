/**
 * queue / edit / interrupt / withdraw 显式 mutator 单测。
 */
import { beforeEach, describe, expect, it } from 'vitest';
import {
  addSuppressedSession,
  appendAttachment,
  clearAttachments,
  enqueuePending,
  getPendingQueue,
  movePendingQueueItem,
  patchPendingQueueItem,
  promotePendingQueueItemAsRevision,
  removeAttachmentAt,
  removePendingQueueItem,
  removeSuppressedSession,
  replaceAttachments,
  replacePendingQueue,
  setEditFrom,
  shiftPendingQueue,
} from '../../src/ui/state';
import { __resetAllStoresForTest, messageStore, sessionStore } from '../../src/ui/stores/stores';
import type { PendingMessage } from '../../src/ui/chat-render';
import type { Attachment } from '../../src/ui/backend-client';

function makeItem(overrides: Partial<PendingMessage> = {}): PendingMessage {
  return { id: 'm-1', query: 'hello', attachments: [], ...overrides };
}

describe('queue/edit mutators', () => {
  beforeEach(() => {
    __resetAllStoresForTest();
  });

  it('enqueuePending appends to the session tail', () => {
    enqueuePending('sid-1', makeItem({ id: 'a' }));
    enqueuePending('sid-1', makeItem({ id: 'b' }));
    expect(getPendingQueue('sid-1').map((i) => i.id)).toEqual(['a', 'b']);
  });

  it('shiftPendingQueue returns head and remaining', () => {
    enqueuePending('sid-1', makeItem({ id: 'a' }));
    enqueuePending('sid-1', makeItem({ id: 'b' }));
    const [head, rest] = shiftPendingQueue('sid-1');
    expect(head?.id).toBe('a');
    expect(rest.map((i) => i.id)).toEqual(['b']);
    expect(getPendingQueue('sid-1').map((i) => i.id)).toEqual(['b']);
  });

  it('shiftPendingQueue on empty returns [null, []]', () => {
    const [head, rest] = shiftPendingQueue('sid-1');
    expect(head).toBeNull();
    expect(rest).toEqual([]);
  });

  it('removePendingQueueItem by index', () => {
    enqueuePending('sid-1', makeItem({ id: 'a' }));
    enqueuePending('sid-1', makeItem({ id: 'b' }));
    enqueuePending('sid-1', makeItem({ id: 'c' }));
    removePendingQueueItem('sid-1', 1);
    expect(getPendingQueue('sid-1').map((i) => i.id)).toEqual(['a', 'c']);
  });

  it('promotePendingQueueItemAsRevision moves one item to the head and preserves the others', () => {
    enqueuePending('sid-1', makeItem({ id: 'a', query: 'first' }));
    enqueuePending('sid-1', makeItem({ id: 'b', query: 'revision' }));
    enqueuePending('sid-1', makeItem({ id: 'c', query: 'third' }));

    const promoted = promotePendingQueueItemAsRevision('sid-1', 1);

    expect(promoted?.id).toBe('b');
    expect(promoted?.clientIntent).toBe('revision');
    expect(getPendingQueue('sid-1').map((i) => i.id)).toEqual(['b', 'a', 'c']);
    expect(getPendingQueue('sid-1')[0]?.clientIntent).toBe('revision');
  });

  it('patchPendingQueueItem updates the targeted entry only', () => {
    enqueuePending('sid-1', makeItem({ id: 'a', query: 'old' }));
    enqueuePending('sid-1', makeItem({ id: 'b', query: 'b' }));
    patchPendingQueueItem('sid-1', 0, { query: 'new' });
    expect(getPendingQueue('sid-1').map((i) => i.query)).toEqual(['new', 'b']);
  });

  it('movePendingQueueItem reorders entries and ignores out-of-range moves', () => {
    replacePendingQueue('sid-1', [
      makeItem({ id: 'a' }),
      makeItem({ id: 'b' }),
      makeItem({ id: 'c' }),
    ]);

    movePendingQueueItem('sid-1', 2, 1);
    expect(getPendingQueue('sid-1').map((i) => i.id)).toEqual(['a', 'c', 'b']);

    movePendingQueueItem('sid-1', 0, -1);
    movePendingQueueItem('sid-1', 9, 0);
    expect(getPendingQueue('sid-1').map((i) => i.id)).toEqual(['a', 'c', 'b']);
  });

  it('replacePendingQueue replaces the entire list', () => {
    enqueuePending('sid-1', makeItem({ id: 'a' }));
    replacePendingQueue('sid-1', [makeItem({ id: 'x' }), makeItem({ id: 'y' })]);
    expect(getPendingQueue('sid-1').map((i) => i.id)).toEqual(['x', 'y']);
  });
});

describe('edit / suppress mutators', () => {
  beforeEach(() => {
    __resetAllStoresForTest();
  });

  it('setEditFrom sets and clears the index', () => {
    setEditFrom('sid-1', 3);
    expect(sessionStore.get().editFromIdx['sid-1']).toBe(3);
    setEditFrom('sid-1', null);
    expect('sid-1' in sessionStore.get().editFromIdx).toBe(false);
  });

  it('setEditFrom null on unknown session is a no-op', () => {
    setEditFrom('sid-unknown', null);
    expect('sid-unknown' in sessionStore.get().editFromIdx).toBe(false);
  });

  it('addSuppressedSession + removeSuppressedSession toggle', () => {
    addSuppressedSession('sid-1');
    expect(sessionStore.get().suppressChunks.has('sid-1')).toBe(true);
    addSuppressedSession('sid-1'); // idempotent
    expect(sessionStore.get().suppressChunks.size).toBe(1);
    removeSuppressedSession('sid-1');
    expect(sessionStore.get().suppressChunks.has('sid-1')).toBe(false);
  });
});

describe('attachments mutators', () => {
  beforeEach(() => {
    __resetAllStoresForTest();
  });

  it('appendAttachment / removeAttachmentAt / clearAttachments', () => {
    const a1: Attachment = { type: 'image', path: '/a.png', name: 'a.png' };
    const a2: Attachment = { type: 'file', path: '/b.txt', name: 'b.txt' };
    appendAttachment(a1);
    appendAttachment(a2);
    expect(messageStore.get().attachments).toEqual([a1, a2]);
    removeAttachmentAt(0);
    expect(messageStore.get().attachments).toEqual([a2]);
    clearAttachments();
    expect(messageStore.get().attachments).toEqual([]);
  });

  it('replaceAttachments sets the list', () => {
    replaceAttachments([{ type: 'image', path: '/x.png', name: 'x.png' }]);
    expect(messageStore.get().attachments.length).toBe(1);
  });
});
