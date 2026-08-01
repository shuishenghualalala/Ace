import { describe, expect, it } from 'vitest';
import {
  buildDiffFromTexts,
  buildOverviewRuns,
  clearDiffExpandsForPath,
  collapseContextRuns,
  countDiffRows,
  expandCollapsedRegion,
  highlightDiffCodeRaw,
  parseBackendDiffRows,
  parseDiffText,
  renderDiffPanelHtml,
  stripDiffFileHeaders,
} from '../../src/ui/diff-lines';

describe('stripDiffFileHeaders', () => {
  it('drops git preamble before first hunk', () => {
    const raw = [
      '--- a/foo.ts',
      '+++ b/foo.ts',
      '@@ -1,2 +1,3 @@',
      ' context',
      '+added',
    ].join('\n');
    expect(stripDiffFileHeaders(raw)).toBe('@@ -1,2 +1,3 @@\n context\n+added');
  });
});

describe('parseDiffText', () => {
  it('tracks old/new line numbers from hunks', () => {
    const diff = [
      '@@ -1,2 +1,3 @@',
      ' unchanged',
      '-removed',
      '+added',
    ].join('\n');

    const lines = parseDiffText(diff);
    expect(lines).toEqual([
      { kind: 'context', text: 'unchanged', oldNo: 1, newNo: 1 },
      { kind: 'remove', text: 'removed', oldNo: 2 },
      { kind: 'add', text: 'added', newNo: 2 },
    ]);
  });
});

describe('parseBackendDiffRows', () => {
  it('skips meta headers and maps kinds', () => {
    const rows = parseBackendDiffRows([
      { line: 0, kind: 'meta', text: '--- old' },
      { line: 0, kind: 'meta', text: '@@ -1,1 +1,2 @@' },
      { line: 0, kind: 'ctx', text: 'keep' },
      { line: 0, kind: 'add', text: 'new line' },
    ]);

    expect(rows).toHaveLength(2);
    expect(rows[0]).toMatchObject({ kind: 'context', text: 'keep', oldNo: 1, newNo: 1 });
    expect(rows[1]).toMatchObject({ kind: 'add', text: 'new line', newNo: 2 });
  });
});

describe('collapseContextRuns', () => {
  it('collapses long unchanged runs with region bounds', () => {
    const lines = Array.from({ length: 12 }, (_, i) => ({
      kind: 'context' as const,
      text: `line ${i}`,
      oldNo: i + 1,
      newNo: i + 1,
    }));

    const display = collapseContextRuns(lines, 8);
    expect(display).toHaveLength(1);
    expect(display[0]).toEqual({ type: 'collapsed', start: 0, end: 12, count: 12 });
  });

  it('reveals top/bottom chunks from expands map', () => {
    const lines = Array.from({ length: 40 }, (_, i) => ({
      kind: 'context' as const,
      text: `line ${i}`,
      oldNo: i + 1,
      newNo: i + 1,
    }));
    const display = collapseContextRuns(lines, 8, { 0: { top: 20, bottom: 0 } });
    expect(display[0]).toMatchObject({ type: 'line', line: { text: 'line 0' } });
    expect(display[19]).toMatchObject({ type: 'line', line: { text: 'line 19' } });
    expect(display[20]).toEqual({ type: 'collapsed', start: 0, end: 40, count: 20 });
  });

  it('folds only the long context gap between two change hunks', () => {
    const lines = [
      { kind: 'add' as const, text: 'first', newNo: 1 },
      ...Array.from({ length: 15 }, (_, i) => ({
        kind: 'context' as const,
        text: `mid ${i}`,
        oldNo: i + 1,
        newNo: i + 2,
      })),
      { kind: 'add' as const, text: 'second', newNo: 17 },
    ];
    const display = collapseContextRuns(lines, 8);
    expect(display[0]).toMatchObject({ type: 'line', line: { kind: 'add', text: 'first' } });
    expect(display[1]).toEqual({ type: 'collapsed', start: 1, end: 16, count: 15 });
    expect(display[2]).toMatchObject({ type: 'line', line: { kind: 'add', text: 'second' } });
  });

  it('does not fold all-add new-file diffs (no context runs)', () => {
    const lines = Array.from({ length: 20 }, (_, i) => ({
      kind: 'add' as const,
      text: `line ${i}`,
      newNo: i + 1,
    }));
    const display = collapseContextRuns(lines, 8);
    expect(display.every((item) => item.type === 'line')).toBe(true);
    expect(display).toHaveLength(20);
  });
});

describe('expandCollapsedRegion', () => {
  it('adds 20 lines from the requested edge', () => {
    const next = expandCollapsedRegion({}, 0, 'top');
    expect(next[0]).toEqual({ top: 20, bottom: 0 });
    expect(expandCollapsedRegion(next, 0, 'bottom')[0]).toEqual({ top: 20, bottom: 20 });
  });
});

describe('clearDiffExpandsForPath', () => {
  it('removes temporary expand progress for the collapsed file path', () => {
    const map = new Map([
      ['a.ts', { 0: { top: 20, bottom: 40 } }],
      ['b.ts', { 10: { top: 20, bottom: 0 } }],
    ]);
    clearDiffExpandsForPath(map, 'a.ts');
    expect(map.has('a.ts')).toBe(false);
    expect(map.get('b.ts')).toEqual({ 10: { top: 20, bottom: 0 } });
  });

  it('no-ops on empty path', () => {
    const map = new Map([['a.ts', { 0: { top: 20, bottom: 0 } }]]);
    clearDiffExpandsForPath(map, '');
    expect(map.has('a.ts')).toBe(true);
  });
});

describe('buildDiffFromTexts', () => {
  it('marks all lines as add for new file content', () => {
    const rows = buildDiffFromTexts(null, 'a\nb');
    expect(rows).toEqual([
      { line: 0, kind: 'add', text: 'a' },
      { line: 0, kind: 'add', text: 'b' },
    ]);
    expect(countDiffRows(rows)).toEqual({ added: 2, removed: 0 });
  });
});

describe('highlightDiffCodeRaw', () => {
  const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  it('highlights html without leaking span markup into text', () => {
    const out = highlightDiffCodeRaw('<!DOCTYPE html>\n<html lang="zh-CN">', 'html', esc);
    expect(out).toContain('diff-tok-meta');
    expect(out).toContain('diff-tok-tag">html</span>');
    expect(out).toContain('diff-tok-attr">lang</span>');
    expect(out).not.toContain('<class=');
    expect(out).not.toMatch(/&lt;<span/);
  });
});

describe('renderDiffPanelHtml', () => {
  const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  it('embeds syntax tokens at render time for html files', () => {
    // 用带属性的标签，才能同时验证 tag 与 attr 两类 token 在渲染期就已嵌入。
    const html = renderDiffPanelHtml(
      [{ line: 0, kind: 'add', text: '<title lang="zh">rpc的贪吃蛇</title>' }],
      { escapeHtml: esc, filename: 'snake_game.html' },
    );
    expect(html).toContain('diff-tok-tag');
    expect(html).toContain('diff-tok-attr');
    expect(html).not.toContain('<class=');
  });

  it('wraps rows in a scroll-inner so the panel can scroll horizontally as one unit', () => {
    const html = renderDiffPanelHtml(
      [{ line: 0, kind: 'add', text: 'x'.repeat(120) }],
      { escapeHtml: esc, filename: 'long.txt' },
    );
    expect(html).toContain('inspector-file__diff-scroll-inner');
  });

  it('renders +/- signs and expandable unmodified bars', () => {
    const rows = [
      ...Array.from({ length: 12 }, () => ({ line: 0, kind: 'ctx' as const, text: 'keep' })),
      { line: 0, kind: 'add' as const, text: 'new' },
      { line: 0, kind: 'del' as const, text: 'old' },
    ];
    const html = renderDiffPanelHtml(rows, { escapeHtml: esc, filename: 'a.py' });
    expect(html).toContain('unmodified lines');
    expect(html).toContain('data-diff-expand="top"');
    expect(html).toContain('data-diff-expand="bottom"');
    expect(html).toContain('inspector-file__diff-sign--add');
    expect(html).toContain('inspector-file__diff-sign--del');
  });
});

describe('buildOverviewRuns', () => {
  it('groups consecutive add/remove runs', () => {
    const lines = [
      { kind: 'add' as const, text: 'a' },
      { kind: 'add' as const, text: 'b' },
      { kind: 'context' as const, text: 'c' },
      { kind: 'remove' as const, text: 'd' },
    ];

    const runs = buildOverviewRuns(lines);
    expect(runs).toHaveLength(2);
    expect(runs[0]).toMatchObject({ kind: 'add', sizePct: 50, startPct: 0 });
    expect(runs[1]).toMatchObject({ kind: 'remove', sizePct: 25, startPct: 75 });
  });
});
