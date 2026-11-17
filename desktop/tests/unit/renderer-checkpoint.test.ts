/** @vitest-environment happy-dom */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { DesktopChatOutboxSnapshot } from '../../src/shared/chat-outbox';
import type { RendererCheckpoint, RendererCheckpointSnapshot } from '../../src/shared/renderer-checkpoint';
import {
  bindPrimaryRendererCheckpoint,
  restoreRendererCheckpointNavigation,
} from '../../src/ui/features/renderer-checkpoint';
import { appendAttachment, setActiveSessionId, setCurrentWorkspaceId } from '../../src/ui/state';
import { __resetAllStoresForTest, authStore, messageStore, sessionStore, workspaceStore } from '../../src/ui/stores/stores';

const scope = 'a'.repeat(64);
const attachment = { id: 'file-a', name: '说明.txt', path: 'uploads/a.txt', type: 'file' as const };
const checkpoint: RendererCheckpoint = {
  workspaceId: 'project', sessionId: 'previous-session', draftSession: false,
  text: { value: '一次尚未完成的编辑', revision: 'saved-text' },
  attachments: [{ ...attachment, revision: 'saved-attachment' }],
};

function mocks(snapshot: RendererCheckpoint | null = checkpoint) {
  return {
    rendererCheckpointGet: vi.fn(async (): Promise<RendererCheckpointSnapshot> => ({ scope, checkpoint: snapshot })),
    rendererCheckpointPut: vi.fn(async (_scope: string, _checkpoint: RendererCheckpoint) => {}),
    chatOutboxList: vi.fn(async (): Promise<DesktopChatOutboxSnapshot> => ({ scope, entries: [] })),
  };
}

let input: HTMLTextAreaElement;
let binding: ReturnType<typeof bindPrimaryRendererCheckpoint>;

function edit(value: string): void {
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
}

beforeEach(() => {
  __resetAllStoresForTest();
  input = document.createElement('textarea');
  authStore.set({ isLoggedIn: true, userInfo: { staffCode: 'account-one' } });
  sessionStore.set({
    sessions: [{ id: 'previous-session', workspaceId: 'project', title: '对话', preview: '', updatedAt: 1, badge: '' }],
  });
  workspaceStore.set({
    workspaces: [
      { id: 'default', name: '默认', description: '', instructions: '' },
      { id: 'project', name: '项目', description: '', instructions: '' },
    ],
  });
});

afterEach(() => {
  binding?.dispose();
  binding = null;
  vi.unstubAllGlobals();
});

describe('primary composer after renderer recovery', () => {
  it('restores the draft and media. It opens the prior session once fresh ownership data is available', async () => {
    vi.stubGlobal('Crew', mocks());
    binding = bindPrimaryRendererCheckpoint(input);
    await vi.waitFor(() => expect(input.value).toBe(checkpoint.text!.value));
    expect(messageStore.get().attachments).toEqual([attachment]);
    const open = vi.fn();
    await restoreRendererCheckpointNavigation(open);
    await restoreRendererCheckpointNavigation(open);
    expect(open).toHaveBeenCalledExactlyOnceWith('previous-session');
    expect(binding?.capture()).toEqual({
      textRevision: 'saved-text', attachmentRevisions: ['saved-attachment'],
    });
  });

  it('does not overwrite a draft or navigation changed while the old checkpoint is read', async () => {
    const bridge = mocks();
    const pending = Promise.withResolvers<RendererCheckpointSnapshot>();
    bridge.rendererCheckpointGet.mockReturnValueOnce(pending.promise);
    vi.stubGlobal('Crew', bridge);
    binding = bindPrimaryRendererCheckpoint(input);
    edit('当前正在编辑');
    appendAttachment({ ...attachment, id: 'new-attachment' });
    setActiveSessionId('new-session');
    pending.resolve({ scope, checkpoint });
    const open = vi.fn();
    await restoreRendererCheckpointNavigation(open);
    expect(open).not.toHaveBeenCalled();
    expect(input.value).toBe('当前正在编辑');
    expect(messageStore.get().attachments.map(({ id }) => id)).toEqual(['new-attachment']);
    await vi.waitFor(() => expect(bridge.rendererCheckpointPut).toHaveBeenCalledWith(scope, expect.objectContaining({
      sessionId: 'new-session',
      text: expect.objectContaining({ value: '当前正在编辑' }),
    })));
  });

  it('keeps the new edit while the old text and attachment revisions are covered by the outbox', async () => {
    const newerCheckpoint: RendererCheckpoint = {
      ...checkpoint,
      text: { value: '受理前输入的新文本', revision: 'new-text' },
      attachments: [...checkpoint.attachments, { ...attachment, id: 'new-file', revision: 'new-file' }],
    };
    const bridge = mocks(newerCheckpoint);
    bridge.chatOutboxList.mockResolvedValue({
      scope,
      entries: [{
        sessionId: 'previous-session', requestId: 'submitted-a', phase: 'submitted',
        value: { composerCheckpoint: { textRevision: 'saved-text', attachmentRevisions: ['saved-attachment'] } },
      }],
    });
    vi.stubGlobal('Crew', bridge);
    binding = bindPrimaryRendererCheckpoint(input);
    await vi.waitFor(() => expect(input.value).toBe(newerCheckpoint.text!.value));
    expect(messageStore.get().attachments.map(({ id }) => id)).toEqual(['new-file']);
    expect(binding?.capture()).toEqual({ textRevision: 'new-text', attachmentRevisions: ['new-file'] });
  });

  it('keeps the original unsent draft editable after a durable Stop', async () => {
    const bridge = mocks();
    bridge.chatOutboxList.mockResolvedValue({
      scope,
      entries: [{
        sessionId: 'previous-session', requestId: 'withdrawn-a', phase: 'withdrawing',
        value: { composerCheckpoint: { textRevision: 'saved-text', attachmentRevisions: ['saved-attachment'] } },
      }],
    });
    vi.stubGlobal('Crew', bridge);
    binding = bindPrimaryRendererCheckpoint(input);
    await vi.waitFor(() => expect(input.value).toBe(checkpoint.text!.value));
    expect(messageStore.get().attachments).toEqual([attachment]);
  });

  it('does not restore a late previous-account response or a deleted session', async () => {
    const bridge = mocks();
    const oldAccount = Promise.withResolvers<RendererCheckpointSnapshot>();
    const newScope = 'b'.repeat(64);
    bridge.chatOutboxList.mockResolvedValueOnce({ scope, entries: [] });
    bridge.chatOutboxList.mockResolvedValueOnce({ scope: newScope, entries: [] });
    bridge.rendererCheckpointGet.mockReturnValueOnce(oldAccount.promise);
    bridge.rendererCheckpointGet.mockResolvedValueOnce({
      scope: newScope,
      checkpoint: { ...checkpoint, sessionId: 'deleted-session', text: null, attachments: [] },
    });
    vi.stubGlobal('Crew', bridge);
    binding = bindPrimaryRendererCheckpoint(input);
    await vi.waitFor(() => expect(bridge.rendererCheckpointGet).toHaveBeenCalledOnce());
    authStore.set({ isLoggedIn: true, userInfo: { staffCode: 'account-two' } });
    const open = vi.fn();
    await restoreRendererCheckpointNavigation(open);
    oldAccount.resolve({ scope, checkpoint });
    await Promise.resolve();
    expect(open).not.toHaveBeenCalled();
    expect(input.value).toBe('');
    expect(messageStore.get().attachments).toEqual([]);
    expect(workspaceStore.get().currentWorkspaceId).toBe('project');
    setCurrentWorkspaceId('default');
    await vi.waitFor(() => expect(bridge.rendererCheckpointPut).toHaveBeenCalledWith(newScope, expect.objectContaining({
      workspaceId: 'default', text: null, attachments: [],
    })));
  });
});
