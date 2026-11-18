import { describe, expect, it } from 'vitest';
import { ConversationViewport, type ConversationViewportRow } from '../../src/ui/features/conversation-viewport';

const row = (key: string, estimatedHeight = 80, live = false): ConversationViewportRow => ({
  key, sig: key, estimatedHeight, live,
});

describe('measured conversation viewport', () => {
  it('indexes measured rows by stable key and accounts for real spacing after prepend', () => {
    const viewport = new ConversationViewport<ConversationViewportRow>();
    viewport.setGeometry(800, 12);
    viewport.replaceFrom(0, [row('a'), row('b', 120), row('c', 45)]);
    viewport.measure('a', 'a', 160);
    viewport.measure('c', 'c', 0);
    expect(viewport.totalHeight).toBe(292);
    expect(viewport.anchorAt(180)).toEqual({ key: 'b', offset: -8 });

    viewport.replaceFrom(0, [row('older', 35), ...viewport.rows]);
    expect(viewport.topOf('b')).toBe(219);
    expect(viewport.spacerHeight(0, 2)).toBe(207);
    expect(viewport.anchorAt(227)).toEqual({ key: 'b', offset: -8 });

    viewport.replaceFrom(3, [{ ...row('c', 45), sig: 'now present' }]);
    expect(viewport.totalHeight).toBe(396);
    expect(viewport.measure('c', 'c', 800)).toBe(false);
  });

  it('bounds ordinary mounts while keeping requested focus or live keys outside the window', () => {
    const viewport = new ConversationViewport<ConversationViewportRow>();
    viewport.setGeometry(800, 12);
    viewport.replaceFrom(0, [
      ...Array.from({ length: 600 }, (_, index) => row('row-' + index, 42 + index % 123)),
      row('live', 300, true),
    ]);
    const selected = viewport.select(22000, 620, false, ['row-0']);
    const keys = selected.map((index) => viewport.rows[index].key);
    expect(keys).toContain('row-0');
    expect(keys).toContain('live');
    expect(selected.length).toBeLessThan(45);
    expect(viewport.select(0, 620, true).at(-1)).toBe(600);
    expect(viewport.select(0, 620, true)).not.toContain(0);
  });
});
