import { promises as fs } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DesktopChatOutbox, desktopChatOutboxScope } from '../../src/main/chat-outbox';
import type { DesktopChatOutboxEntry } from '../../src/shared/chat-outbox';

let root: string;
let scope: string;
let store: DesktopChatOutbox;

function item(requestId: string, phase: DesktopChatOutboxEntry['phase'] = 'queued'): DesktopChatOutboxEntry {
  return {
    requestId, sessionId: 'session-1', phase,
    value: { text: '测试内容', attachments: [{ path: '/uploaded/example.txt' }] },
  };
}

beforeEach(async () => {
  root = await fs.mkdtemp(path.join(tmpdir(), 'ace-chat-outbox-'));
  scope = desktopChatOutboxScope('local', 'local', path.join(root, 'gateway'));
  store = new DesktopChatOutbox(root);
});

afterEach(async () => {
  vi.restoreAllMocks();
  await fs.rm(root, { recursive: true, force: true });
});

describe('desktop chat outbox', () => {
  it('restores queued and submitted inputs in creation order with their references', async () => {
    await Promise.all([store.put(scope, item('z-first')), store.put(scope, item('a-second'))]);
    await store.put(scope, item('z-first', 'submitted'));

    const reopened = new DesktopChatOutbox(root);
    expect(await reopened.list(scope)).toEqual([item('z-first', 'submitted'), item('a-second')]);
    await reopened.remove(scope, 'z-first');
    await reopened.remove(scope, 'z-first');
    expect(await new DesktopChatOutbox(root).list(scope)).toEqual([item('a-second')]);
  });

  it('isolates the same request ID between authenticated accounts and gateway homes', async () => {
    const accountScope = desktopChatOutboxScope('local', 'provider:other', path.join(root, 'gateway'));
    const otherHome = desktopChatOutboxScope('local', 'local', path.join(root, 'other-gateway'));
    const devScope = desktopChatOutboxScope('dev', 'local', path.join(root, 'gateway'));
    await store.put(scope, item('req_repeat'));
    await store.put(accountScope, item('req_repeat', 'withdrawing'));

    expect(await store.list(otherHome)).toEqual([]);
    expect(await store.list(devScope)).toEqual([]);
    await store.remove(accountScope, 'req_repeat');
    expect(await store.list(scope)).toEqual([item('req_repeat')]);
  });

  it('preserves the original entry when replacing the file fails and retries cleanly', async () => {
    await store.put(scope, item('req_1'));
    vi.spyOn(fs, 'rename').mockRejectedValueOnce(Object.assign(new Error('rename failed'), { code: 'EIO' }));
    await expect(store.put(scope, item('req_1', 'submitted'))).rejects.toThrow('rename failed');
    expect(await store.list(scope)).toEqual([item('req_1')]);
    expect(await fs.readdir(path.join(root, scope))).toEqual([expect.stringMatching(/\.json$/)]);

    await store.put(scope, item('req_1', 'submitted'));
    expect(await store.list(scope)).toEqual([item('req_1', 'submitted')]);
  });

  it('rejects malformed keys and attempts to move a request to another session', async () => {
    await store.put(scope, item('req_1'));
    await expect(store.put(scope, { ...item('req_1'), sessionId: 'session-2' })).rejects.toThrow('different session');
    await expect(store.put(scope, item('../bad'))).rejects.toThrow('Invalid chat outbox entry');
    await expect(store.remove(scope, '../bad')).rejects.toThrow('Invalid chat outbox request id');
    await expect(store.list('../bad')).rejects.toThrow('Invalid chat outbox scope');
    expect(await store.list(scope)).toEqual([item('req_1')]);
  });
});
