import type { ChatMessage } from './chat-render';

const RECENT_CHANGES = 96;

interface Change {
  revision: number;
  parent: number;
  firstIndex: number;
}

interface Lineage {
  latest: number;
  changes: Array<Change | undefined>;
}

interface Revision {
  id: number;
  lineage: Lineage;
}

// Only live snapshots own a lineage. Its bounded links never retain message arrays.
const snapshots = new WeakMap<ChatMessage[], Revision>();

/** Record the earliest changed index before publishing a new immutable snapshot. */
export function recordMessageChange(previous: ChatMessage[], next: ChatMessage[], firstChangedIndex: number): void {
  if (previous === next || snapshots.has(next) || !Number.isSafeInteger(firstChangedIndex)
    || firstChangedIndex < 0 || firstChangedIndex > Math.min(previous.length, next.length)) return;

  let parent = snapshots.get(previous);
  if (!parent) {
    parent = { id: 0, lineage: { latest: 0, changes: [] } };
    snapshots.set(previous, parent);
  }
  const { lineage } = parent;
  const revision = ++lineage.latest;
  lineage.changes[revision % RECENT_CHANGES] = { revision, parent: parent.id, firstIndex: firstChangedIndex };
  snapshots.set(next, { id: revision, lineage });
}

/** Return the common-prefix length guaranteed by retained links, or require a full refresh. */
export function firstChangedMessageIndex(previous: ChatMessage[], next: ChatMessage[]): number | undefined {
  if (previous === next) return previous.length;
  const from = snapshots.get(previous);
  const to = snapshots.get(next);
  if (!from || !to || from.lineage !== to.lineage || from.id >= to.id) return undefined;

  let firstIndex = Math.min(previous.length, next.length);
  let revision = to.id;
  while (revision > from.id) {
    const change = to.lineage.changes[revision % RECENT_CHANGES];
    if (!change || change.revision !== revision) return undefined;
    firstIndex = Math.min(firstIndex, change.firstIndex);
    revision = change.parent;
  }
  return revision === from.id ? firstIndex : undefined;
}
