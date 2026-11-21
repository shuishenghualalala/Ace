// @vitest-environment happy-dom

import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { backendApi } from '../../src/ui/backend-client';
import * as chatController from '../../src/ui/features/chat-controller';
import * as history from '../../src/ui/features/session-controller';
import { disposeDesktopSideChat, openDesktopSideChat } from '../../src/ui/features/side-chat';
import { setActiveSessionId, state } from '../../src/ui/state';
import { __resetAllStoresForTest, authStore, messageStore, sessionStore, uiStore } from '../../src/ui/stores/stores';

const parent = 'parent-side-fixture';
const child = 'child-side-fixture';
const created = { ok: true as const, session_id: child, source_session_id: parent, workspace_id: 'server-workspace' };

beforeEach(() => {
  __resetAllStoresForTest();
  setActiveSessionId(parent);
  authStore.set({ isLoggedIn: true, userInfo: { uid: 'signed-owner' } });
  sessionStore.set({ sessions: [{ id: parent, title: 'Main chat', updatedAt: 1, workspaceId: 'server-workspace', preview: '', badge: '' }] });
  messageStore.set({ messages: { [parent]: [{ id: 'main', role: 'user', content: 'Saved main text', timestamp: 1 }] } });
  document.body.innerHTML = '<main id="chat-messages"></main><aside id="chat-inspector"><div id="chat-inspector-tabs"></div><div id="chat-inspector-body"></div></aside>';
  vi.spyOn(chatController, 'refreshSessions').mockResolvedValue(undefined);
});

afterEach(() => {
  disposeDesktopSideChat();
  document.body.replaceChildren();
  vi.restoreAllMocks();
});

it('ignores a late fork response after closing the original parent scope', async () => {
  let resolve!: (value: typeof created) => void;
  const request = new Promise<typeof created>((done) => { resolve = done; });
  const create = vi.spyOn(backendApi, 'createSideChat').mockReturnValue(request);
  const load = vi.spyOn(history, 'loadBackendHistory');
  const opening = openDesktopSideChat();
  expect(create).toHaveBeenCalledTimes(1);
  expect(document.body.classList.contains('inspector-open')).toBe(true);

  setActiveSessionId('other-parent');
  expect(create.mock.calls[0]![1].aborted).toBe(true);
  resolve(created);
  await opening;
  expect(load).not.toHaveBeenCalled();
  expect(document.querySelector('[data-side-chat-session]')).toBeNull();
  expect(document.body.classList.contains('inspector-open')).toBe(false);
});

it('submits from the mounted composer only to the prepared child and leaves the main state alone', async () => {
  const send = vi.fn(async () => true);
  uiStore.set({ backendConnected: true, backendCanSend: true, socket: { send, subscribe: vi.fn() } as never });
  state.mode = 'dynamic_kanban';
  vi.spyOn(backendApi, 'createSideChat').mockResolvedValue(created);
  const ensure = vi.spyOn(backendApi, 'ensureSession').mockResolvedValue({ ok: true, session_id: child });
  vi.spyOn(history, 'loadBackendHistory').mockImplementation(async (id) => {
    messageStore.set({ messages: { ...messageStore.get().messages, [id]: [{ id: 'snapshot', role: 'assistant', content: 'Saved child prefix', timestamp: 1 }] } });
    return true;
  });
  await openDesktopSideChat();
  const pane = document.querySelector<HTMLElement>('[data-side-chat-session]')!;
  expect(pane.dataset.sideChatSession).toBe(child);
  expect(pane.textContent).toContain('Saved child prefix');
  const input = pane.querySelector<HTMLTextAreaElement>('[data-composer-input]')!;
  input.value = 'Only the side conversation';
  input.dispatchEvent(new Event('input', { bubbles: true }));
  pane.querySelector<HTMLButtonElement>('[data-composer-send]')!.click();
  await vi.waitFor(() => expect(send).toHaveBeenCalledWith(expect.objectContaining({
    session_id: child, workspace_id: 'server-workspace', query: 'Only the side conversation', mode: 'agent',
  })));
  expect(ensure).not.toHaveBeenCalled();
  expect(state.activeSessionId).toBe(parent);
  expect(state.mode).toBe('dynamic_kanban');
  expect(messageStore.get().messages[parent]?.map(({ content }) => content)).toEqual(['Saved main text']);

  const preview = document.createElement('a');
  preview.href = 'file:///tmp/result.html';
  pane.querySelector('.chat-messages')!.append(preview);
  expect(preview.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }))).toBe(false);
  expect(pane.isConnected).toBe(true);
});
