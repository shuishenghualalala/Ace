// @vitest-environment happy-dom

import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { closeDesktopSessionGoal, openDesktopSessionGoal } from '../../src/ui/features/session-goal';
import { setActiveSessionId } from '../../src/ui/state';
import { __resetAllStoresForTest, authStore, sessionStore } from '../../src/ui/stores/stores';

const sid = 'goal-session';
const path = `/api/session/${sid}/goal`;
const goal = (version: string, objective: string, status: string) => ({ version, goal: { objective, status } });
const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), {
  status, headers: { 'Content-Type': 'application/json' },
});

beforeEach(() => {
  __resetAllStoresForTest();
  document.body.className = '';
  document.body.innerHTML = '<main id="chat-panel"></main>';
  authStore.set({ isLoggedIn: true, userInfo: { uid: 'fixture-owner' } });
  sessionStore.set({ sessions: [{
    id: sid, title: 'Saved session', workspaceId: 'default', updatedAt: 1, preview: '', badge: '',
  }] });
  setActiveSessionId(sid);
});

afterEach(() => {
  closeDesktopSessionGoal();
  vi.unstubAllGlobals();
  document.body.replaceChildren();
});

function button(label: string): HTMLButtonElement {
  const element = [...document.querySelectorAll<HTMLButtonElement>('button')]
    .find((candidate) => !candidate.hidden && candidate.textContent === label);
  if (!element) throw new Error(`missing goal button: ${label}`);
  return element;
}

it('keeps a replacement draft on a version conflict and waits for an explicit retry', async () => {
  const writes: unknown[] = [];
  const fetch = vi.fn(async (url: string, options?: RequestInit) => {
    expect(new URL(url).pathname).toBe(path);
    if (options?.method !== 'POST') {
      return json(writes.length ? goal('new', 'Saved elsewhere', 'paused') : goal('old', 'Original goal', 'active'));
    }
    writes.push(JSON.parse(String(options.body)));
    return writes.length === 1 ? json({ code: 'goal_version_conflict' }, 409) : json(goal('newer', 'Keep my revised text', 'active'));
  });
  vi.stubGlobal('fetch', fetch);
  openDesktopSessionGoal('Keep my revised text');
  await vi.waitFor(() => expect(button('保存目标').disabled).toBe(false));
  button('保存目标').focus();
  button('保存目标').click();
  await vi.waitFor(() => expect(document.querySelector('#chat-panel')?.textContent).toContain('Saved elsewhere'));
  expect(document.querySelector('textarea')?.value).toBe('Keep my revised text');
  expect(document.activeElement).toBe(document.querySelector('textarea'));
  expect(writes).toEqual([{ version: 'old', action: 'set', objective: 'Keep my revised text' }]);
  button('保存目标').click();
  await vi.waitFor(() => expect(button('暂停目标')).toBeDefined());
  expect(writes[1]).toEqual({ version: 'new', action: 'set', objective: 'Keep my revised text' });
});

it('does not show a delayed read when the same session ID is reopened by a different owner', async () => {
  const delayed = Promise.withResolvers<Response>();
  const fetch = vi.fn<(url: string, options?: RequestInit) => Promise<Response>>()
    .mockReturnValueOnce(delayed.promise)
    .mockResolvedValueOnce(json(goal('owner-two', 'New owner goal', 'active')));
  vi.stubGlobal('fetch', fetch);
  openDesktopSessionGoal('');
  const signal = fetch.mock.calls[0]?.[1]?.signal;
  authStore.set({ userInfo: { uid: 'another-owner' } });
  expect(signal?.aborted).toBe(true);
  expect(document.querySelector('[aria-label="会话目标"]')).toBeNull();
  openDesktopSessionGoal('');
  await vi.waitFor(() => expect(document.body.textContent).toContain('New owner goal'));
  delayed.resolve(json(goal('owner-one', 'Previous owner goal', 'active')));
  await new Promise<void>((resolve) => setTimeout(resolve, 0));
  expect(document.body.textContent).not.toContain('Previous owner goal');
});

it('shows why an overlong prefilled goal cannot be saved', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => json({ version: 'empty', goal: null })));
  openDesktopSessionGoal('修'.repeat(4001));
  await vi.waitFor(() => expect(document.body.textContent).toContain('目标最多 4000 个字符'));
  expect(button('保存目标').disabled).toBe(true);
  const input = document.querySelector('textarea')!;
  input.value = '修'.repeat(4000);
  input.dispatchEvent(new Event('input'));
  await vi.waitFor(() => expect(button('保存目标').disabled).toBe(false));
});

it('requires reading the committed state before repeating an unconfirmed update', async () => {
  const fetch = vi.fn(async (_url: string, options?: RequestInit) => {
    if (options?.method === 'POST') throw new TypeError('Connection closed');
    return json(goal('updated', 'Same goal', fetch.mock.calls.length === 1 ? 'active' : 'completed'));
  });
  vi.stubGlobal('fetch', fetch);
  openDesktopSessionGoal('');
  await vi.waitFor(() => expect(button('标记目标完成').disabled).toBe(false));
  button('标记目标完成').click();
  await vi.waitFor(() => expect(document.body.textContent).toContain('未能确认目标更新'));
  expect(fetch.mock.calls).toHaveLength(2);
  button('重新读取目标').click();
  await vi.waitFor(() => expect(document.body.textContent).toContain('会话目标 · 已完成'));
  expect(fetch.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1);
});
