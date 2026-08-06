/**
 * @vitest-environment node
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';

const readTextFile = vi.fn();
const pathExists = vi.fn();

vi.mock('../../src/ui/state', () => ({
  getBookFileChanges: vi.fn(() => []),
}));

vi.mock('../../src/ui/stores/stores', () => {
  let messages: Record<string, unknown[]> = {};
  return {
    messageStore: {
      get: () => ({ messages }),
      set: (patch: { messages: Record<string, unknown[]> }) => {
        messages = { ...patch.messages };
      },
    },
  };
});

import { getBookFileChanges } from '../../src/ui/state';
import { messageStore } from '../../src/ui/stores/stores';
import {
  mergeCountsFromFileChanges,
  needsCountHydration,
  hydrateMissingTurnFileCounts,
} from '../../src/ui/features/turn-file-counts';
import type { TurnFileChangeSummary } from '../../src/ui/chat-render';
import type { FileChange } from '../../src/ui/state';

describe('needsCountHydration', () => {
  it('is true when any file has 0/0', () => {
    expect(needsCountHydration([{ path: 'a', name: 'a', added: 0, removed: 0, status: 'modified' }])).toBe(true);
    expect(needsCountHydration([{ path: 'a', name: 'a', added: 3, removed: 1, status: 'modified' }])).toBe(false);
    expect(needsCountHydration([{ path: 'a.pptx', name: 'a.pptx', added: 0, removed: 0, status: 'added', binary: true }])).toBe(false);
  });
});

describe('mergeCountsFromFileChanges', () => {
  it('fills zero counts from book fileChanges / diff', () => {
    const files: TurnFileChangeSummary[] = [
      { path: 'a.html', name: 'a.html', added: 0, removed: 0, status: 'modified' },
      { path: 'b.ts', name: 'b.ts', added: 2, removed: 1, status: 'modified' },
    ];
    const sources: FileChange[] = [
      {
        path: 'a.html',
        name: 'a.html',
        added: 927,
        removed: 0,
        status: 'added',
        diff: [],
      },
    ];
    const out = mergeCountsFromFileChanges(files, sources);
    expect(out[0]).toMatchObject({ added: 927, removed: 0, status: 'added' });
    expect(out[1]).toMatchObject({ added: 2, removed: 1 });
  });

  it('counts from diff rows when added/removed fields are zero', () => {
    const files: TurnFileChangeSummary[] = [
      { path: 'a.txt', name: 'a.txt', added: 0, removed: 0, status: 'modified' },
    ];
    const sources: FileChange[] = [
      {
        path: 'a.txt',
        name: 'a.txt',
        added: 0,
        removed: 0,
        status: 'modified',
        diff: [
          { line: 0, kind: 'add', text: 'x' },
          { line: 0, kind: 'add', text: 'y' },
          { line: 0, kind: 'del', text: 'z' },
        ],
      },
    ];
    expect(mergeCountsFromFileChanges(files, sources)[0]).toMatchObject({ added: 2, removed: 1 });
  });
});

describe('hydrateMissingTurnFileCounts', () => {
  beforeEach(() => {
    readTextFile.mockReset();
    pathExists.mockReset();
    pathExists.mockResolvedValue(true);
    (getBookFileChanges as ReturnType<typeof vi.fn>).mockReturnValue([]);
    messageStore.set({ messages: {} });
    (globalThis as { window?: unknown }).window = {
      Crew: { readTextFile, pathExists },
    };
  });

  it('patches message turnFileChanges from disk when counts are missing', async () => {
    messageStore.set({
      messages: {
        s1: [
          {
            id: 'a1',
            role: 'assistant',
            content: 'done',
            timestamp: 1,
            turnFileChanges: [
              { path: 'C:\\tmp\\tetris.html', name: 'tetris.html', added: 0, removed: 0, status: 'modified' },
            ],
          },
        ],
      },
    });
    readTextFile.mockResolvedValue('line1\nline2\nline3');
    const changed = await hydrateMissingTurnFileCounts('s1');
    expect(changed).toBe(true);
    const msg = messageStore.get().messages.s1![0] as { turnFileChanges: TurnFileChangeSummary[] };
    expect(msg.turnFileChanges[0].added).toBe(3);
    expect(msg.turnFileChanges[0].removed).toBe(0);
    expect(msg.turnFileChanges[0].status).toBe('added');
  });

  it('drops ghost paths that no longer exist on disk', async () => {
    messageStore.set({
      messages: {
        s1: [
          {
            id: 'a1',
            role: 'assistant',
            content: 'done',
            timestamp: 1,
            turnFileChanges: [
              { path: 'C:\\tmp\\_smoke.js', name: '_smoke.js', added: 5, removed: 0, status: 'modified' },
              { path: 'C:\\tmp\\lalala.html', name: 'lalala.html', added: 10, removed: 0, status: 'added' },
            ],
          },
        ],
      },
    });
    pathExists.mockImplementation(async (path: string) => !path.includes('_smoke'));
    readTextFile.mockImplementation(async (path: string) => {
      if (path.includes('_smoke')) throw Object.assign(new Error('ENOENT'), { code: 'ENOENT' });
      return '<html></html>\n';
    });
    const changed = await hydrateMissingTurnFileCounts('s1');
    expect(changed).toBe(true);
    const msg = messageStore.get().messages.s1![0] as { turnFileChanges: TurnFileChangeSummary[] };
    expect(msg.turnFileChanges).toHaveLength(1);
    expect(msg.turnFileChanges[0].path).toContain('lalala.html');
    // 幽灵路径只走 pathExists，不应再触发 readTextFile 刷 ENOENT
    expect(readTextFile).not.toHaveBeenCalledWith(expect.stringContaining('_smoke'));
  });

  it('keeps file card when readTextFile fails with permission error', async () => {
    messageStore.set({
      messages: {
        s1: [
          {
            id: 'a1',
            role: 'assistant',
            content: 'done',
            timestamp: 1,
            turnFileChanges: [
              { path: 'C:\\tmp\\secret.html', name: 'secret.html', added: 0, removed: 0, status: 'modified' },
            ],
          },
        ],
      },
    });
    pathExists.mockResolvedValue(true);
    readTextFile.mockRejectedValue(new Error('EACCES: permission denied'));
    const changed = await hydrateMissingTurnFileCounts('s1');
    // 计数未补上但条目保留 → 仍可能 changed=false（同 prev）；若 filter 未改则 false
    const msg = messageStore.get().messages.s1![0] as { turnFileChanges: TurnFileChangeSummary[] };
    expect(msg.turnFileChanges).toHaveLength(1);
    expect(msg.turnFileChanges[0].path).toContain('secret.html');
    expect(changed).toBe(false);
  });

  it('does not rewrite exact persisted history from the current disk state', async () => {
    const persisted = [
      { path: 'C:\\tmp\\removed-later.txt', name: 'removed-later.txt', added: 2, removed: 0, status: 'added' as const },
      { path: 'C:\\tmp\\changed.txt', name: 'changed.txt', added: 0, removed: 0, status: 'modified' as const },
    ];
    messageStore.set({
      messages: {
        s1: [{
          id: 'a1',
          role: 'assistant',
          content: 'done',
          timestamp: 1,
          turnFileChanges: persisted,
          turnFileChangesPersistedPaths: persisted.map((file) => file.path),
        }],
      },
    });
    pathExists.mockResolvedValue(false);
    readTextFile.mockResolvedValue('current\nfile\ncontent');

    const changed = await hydrateMissingTurnFileCounts('s1');

    expect(changed).toBe(false);
    const msg = messageStore.get().messages.s1![0] as { turnFileChanges: TurnFileChangeSummary[] };
    expect(msg.turnFileChanges).toEqual(persisted);
    expect(pathExists).not.toHaveBeenCalled();
    expect(readTextFile).not.toHaveBeenCalled();
  });
});
