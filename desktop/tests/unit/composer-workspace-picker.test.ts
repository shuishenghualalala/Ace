/**
 * @vitest-environment happy-dom
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { __resetAllStoresForTest, messageStore, sessionStore, workspaceStore } from '../../src/ui/stores/stores';
import { createSessionInWorkspace, ensureComposerDraftSession } from '../../src/ui/features/workspaces';
import { syncComposerWorkspaceLabel } from '../../src/ui/features/composer-toolbar';

beforeEach(() => {
  __resetAllStoresForTest();
  document.body.innerHTML = `
    <div class="composer-workspace-slot" id="chat-workspace-row" hidden>
      <button type="button" id="chat-workspace-btn">
        <span id="chat-workspace-btn-label">选择工作空间</span>
      </button>
    </div>
  `;
});

describe('composer workspace picker', () => {
  it('shows the row and default label on welcome / draft', () => {
    workspaceStore.set({
      workspaces: [
        { id: 'default', name: '对话', description: '', instructions: '' },
        { id: 'test', name: 'test', description: '', instructions: '', root_path: 'C:\\Users\\test' },
      ],
      currentWorkspaceId: 'default',
    });

    syncComposerWorkspaceLabel();

    const row = document.getElementById('chat-workspace-row')!;
    const label = document.getElementById('chat-workspace-btn-label')!;
    expect(row.hidden).toBe(false);
    expect(label.textContent).toBe('不在项目中工作');
  });

  it('shows project name when a project draft is selected', () => {
    workspaceStore.set({
      workspaces: [
        { id: 'default', name: '对话', description: '', instructions: '' },
        { id: 'test', name: 'test', description: '', instructions: '', root_path: 'C:\\Users\\test' },
      ],
    });
    createSessionInWorkspace('test', vi.fn());

    syncComposerWorkspaceLabel();

    expect(document.getElementById('chat-workspace-row')!.hidden).toBe(false);
    expect(document.getElementById('chat-workspace-btn-label')!.textContent).toBe('test');
  });

  it('hides the row when an existing conversation is active', () => {
    workspaceStore.set({
      workspaces: [
        { id: 'default', name: '对话', description: '', instructions: '' },
        { id: 'test', name: 'test', description: '', instructions: '' },
      ],
      currentWorkspaceId: 'test',
    });
    sessionStore.set({
      sessions: [
        { id: 'sid-a', title: 'A', updatedAt: 1, preview: '', badge: '工作空间', workspaceId: 'test' },
      ],
      activeSessionId: 'sid-a',
    });

    syncComposerWorkspaceLabel();

    expect(document.getElementById('chat-workspace-row')!.hidden).toBe(true);
  });

  it('welcome page without session can lazily create a composer draft', () => {
    workspaceStore.set({
      workspaces: [{ id: 'default', name: '对话', description: '', instructions: '' }],
      currentWorkspaceId: 'default',
    });

    const id = ensureComposerDraftSession(vi.fn());

    expect(id).toBeTruthy();
    expect(sessionStore.get().activeSessionId).toBe(id);
  });

  it('hides the row once the draft already has messages', () => {
    workspaceStore.set({
      workspaces: [{ id: 'default', name: '对话', description: '', instructions: '' }],
    });
    const id = createSessionInWorkspace('default', vi.fn());
    messageStore.set({
      messages: {
        [id]: [{ id: 'm1', role: 'user', content: '你好', timestamp: Date.now() }],
      },
    });

    syncComposerWorkspaceLabel();

    expect(document.getElementById('chat-workspace-row')!.hidden).toBe(true);
  });
});
