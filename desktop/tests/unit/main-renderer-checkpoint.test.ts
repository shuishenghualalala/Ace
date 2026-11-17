import { promises as fs } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { RendererCheckpointStore } from '../../src/main/renderer-checkpoint';
import type { RendererCheckpoint } from '../../src/shared/renderer-checkpoint';

const scope = '1'.repeat(64);
const otherScope = '2'.repeat(64);
const file = { id: 'file-id', name: '测试.txt', type: 'file' as const, path: 'upload/f.txt', size: 20 };
const checkpoint: RendererCheckpoint = {
  workspaceId: 'workspace-1', sessionId: 'session-1', draftSession: false,
  text: { value: '编辑中的消息', revision: 'original-text' },
  attachments: [{ ...file, revision: 'original-file' }],
};
let root: string;
let store: RendererCheckpointStore;

beforeEach(async () => {
  root = await fs.mkdtemp(path.join(tmpdir(), 'ace-renderer-checkpoint-'));
  store = new RendererCheckpointStore(root);
});

afterEach(async () => {
  vi.restoreAllMocks();
  await fs.rm(root, { recursive: true, force: true });
});

describe('renderer checkpoint storage', () => {
  it('restores exact navigation, Unicode text and media references after reopening, scoped to the authenticated account', async () => {
    expect(await store.get(scope)).toBeNull();
    await store.put(scope, checkpoint);
    const reopened = new RendererCheckpointStore(root);
    expect(await reopened.get(scope)).toEqual(checkpoint);
    expect(await reopened.get(otherScope)).toBeNull();
  });

  it('settles only submitted parts and rejects late resurrections of their revisions', async () => {
    const newer: RendererCheckpoint = {
      ...checkpoint,
      text: { value: '发送确认期间输入了新草稿', revision: 'edited-text' },
      attachments: [
        ...checkpoint.attachments,
        { ...file, id: 'added-file', revision: 'added-file' },
      ],
    };
    const reference = { textRevision: 'original-text', attachmentRevisions: ['original-file'] };
    await Promise.all([store.put(scope, newer), store.consume(scope, reference), store.put(scope, newer)]);
    expect(await new RendererCheckpointStore(root).get(scope)).toEqual({
      ...newer, attachments: [newer.attachments[1]],
    });
    await store.put(scope, checkpoint);
    expect(await store.get(scope)).toEqual({ ...checkpoint, text: null, attachments: [] });

    const reused = { ...checkpoint, attachments: [{ ...file, revision: 'reattached-file' }] };
    await store.put(scope, reused);
    expect(await store.get(scope)).toEqual({ ...reused, text: null });
  });

  it('records consumption before a delayed draft checkpoint reaches storage', async () => {
    await store.consume(scope, { textRevision: 'original-text', attachmentRevisions: ['original-file'] });
    await new RendererCheckpointStore(root).put(scope, checkpoint);
    expect(await store.get(scope)).toEqual({ ...checkpoint, text: null, attachments: [] });
  });

  it('keeps one corrupt record and recovers future edits, without treating filesystem errors as corruption', async () => {
    await store.put(scope, checkpoint);
    const filePath = path.join(root, `${scope}.json`);
    await fs.writeFile(filePath, '{"version":1');
    await expect(store.get(scope)).resolves.toBeNull();
    expect(await fs.readFile(`${filePath}.corrupt`, 'utf8')).toBe('{"version":1');
    await store.put(scope, checkpoint);
    vi.spyOn(fs, 'readFile').mockRejectedValueOnce(Object.assign(new Error('access denied'), { code: 'EACCES' }));
    await expect(store.get(scope)).rejects.toThrow('access denied');
    await expect(store.get(scope)).resolves.toEqual(checkpoint);
    await fs.writeFile(filePath, '{"version":999}');
    await expect(store.get(scope)).resolves.toBeNull();
    expect(await fs.readFile(`${filePath}.corrupt`, 'utf8')).toBe('{"version":999}');
  });

  it('retains the previous checkpoint on atomic replacement failure and rejects invalid values', async () => {
    await store.put(scope, checkpoint);
    vi.spyOn(fs, 'rename').mockRejectedValueOnce(new Error('rename failed'));
    await expect(store.put(scope, { ...checkpoint, sessionId: 'other-session' })).rejects.toThrow('rename failed');
    await expect(store.get(scope)).resolves.toEqual(checkpoint);
    await expect(store.get('../outside')).rejects.toThrow('Invalid renderer checkpoint scope');
    await expect(store.put(scope, { ...checkpoint, attachments: [{ ...file, revision: '../../invalid' }] }))
      .rejects.toThrow('Invalid renderer draft attachment');
    await expect(store.consume(scope, { textRevision: 'original-text', attachmentRevisions: [42] }))
      .rejects.toThrow('Invalid renderer checkpoint reference');
    expect(await fs.readdir(root)).toEqual([`${scope}.json`]);
  });
});
