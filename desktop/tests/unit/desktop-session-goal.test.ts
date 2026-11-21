// @vitest-environment happy-dom

import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { closeDesktopSessionGoal, openDesktopSessionGoal } from '../../src/ui/features/session-goal';
import { setActiveSessionId } from '../../src/ui/state';
import { __resetAllStoresForTest, authStore, sessionStore } from '../../src/ui/stores/stores';

const sid = 'goal-session';
const path = `/api/session/${sid}/goal`;
const goal = (version: string, objective: string, status: string) => ({ version, goal: { objective, status } });
const step = (version: string, state: string, code = '', status = 'active') => ({
  ...goal(version, 'Check the saved sample', status),
  run: { request_id: 'goal_sample', state, code },
});
const check = (version: string, state: string, {
  code = '', status = 'active', stepRequestId = 'goal_sample', evidence = [] as number[],
} = {}) => ({
  ...step(version, 'finished', '', status), supports_verify: true,
  verification: { request_id: 'verify_sample', step_request_id: stepRequestId, state, code, evidence_sequences: evidence },
});
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
  vi.useRealTimers();
  vi.unstubAllGlobals();
  document.body.replaceChildren();
});

function button(label: string): HTMLButtonElement {
  const element = [...document.querySelectorAll<HTMLButtonElement>('button')]
    .find((candidate) => !candidate.hidden && candidate.textContent === label);
  if (!element) throw new Error(`missing goal button: ${label}`);
  return element;
}

it('advances the saved goal exactly once per click and shows the accepted step separately', async () => {
  const fetch = vi.fn(async (_url: string, options?: RequestInit) => {
    const current = { ...goal('current-version', 'Review a sample', 'active'), supports_advance: true };
    if (options?.method !== 'POST') return json(current);
    return json({ ...current, version: 'advanced-version', run: {
      request_id: 'goal_sample', state: 'accepted', code: '',
    } }, 202);
  });
  vi.stubGlobal('fetch', fetch);
  openDesktopSessionGoal('');
  await vi.waitFor(() => expect(button('推进一轮').disabled).toBe(false));
  expect(fetch.mock.calls).toHaveLength(1);
  button('推进一轮').click();
  await vi.waitFor(() => expect(document.body.textContent).toContain('本轮已接收'));
  const writes = fetch.mock.calls.filter(([, options]) => options?.method === 'POST');
  expect(writes).toHaveLength(1);
  expect(JSON.parse(String(writes[0]?.[1]?.body))).toEqual({ action: 'advance', version: 'current-version' });
  expect(document.body.textContent).toContain('会话目标 · 已启用');
  expect(() => button('推进一轮')).toThrow();
});

it('requests async verification through the Desktop bridge and waits for durable completion', async () => {
  vi.useFakeTimers();
  let verifying = false;
  const gatewayFetch = vi.fn(async (url: string, init?: { method?: string; body?: string; headers?: Record<string, string> }) => {
    expect(new URL(url).pathname).toBe(path);
    let data: unknown = { ...step('ready', 'finished'), supports_verify: true };
    let status = 200;
    if (init?.method === 'POST') {
      verifying = true;
      data = check('reserved', 'running');
      status = 202;
    } else if (verifying) {
      data = check('settled', 'passed', { status: 'completed', evidence: [7, 11] });
    }
    return { body: JSON.stringify(data), status, headers: { 'content-type': 'application/json' } };
  });
  vi.stubGlobal('Crew', { gatewayFetch });
  openDesktopSessionGoal('');
  await vi.advanceTimersByTimeAsync(0);
  expect(button('验收目标').disabled).toBe(false);
  expect(document.body.textContent).not.toContain('目标验收通过');
  button('验收目标').click();
  await vi.advanceTimersByTimeAsync(0);
  expect(document.body.textContent).toContain('正在验收目标');
  expect(document.body.textContent).toContain('会话目标 · 已启用');
  expect(document.body.textContent).not.toContain('会话目标 · 已完成');
  expect(gatewayFetch.mock.calls).toHaveLength(2);
  expect(gatewayFetch.mock.calls[1]?.[1]).toMatchObject({ method: 'POST', headers: { Prefer: 'respond-async' } });
  expect(JSON.parse(String(gatewayFetch.mock.calls[1]?.[1]?.body))).toEqual({ action: 'verify', version: 'ready' });
  await vi.advanceTimersByTimeAsync(6000);
  expect(document.body.textContent).toContain('目标验收通过');
  expect(document.body.textContent).toContain('会话目标 · 已完成');
  expect(() => button('验收目标')).toThrow();
  expect(gatewayFetch.mock.calls).toHaveLength(3);
  expect(gatewayFetch.mock.calls[2]?.[1]?.headers).not.toHaveProperty('Prefer');
});

it('keeps Pause available with a replacement draft and discards a stale verification read', async () => {
  vi.useFakeTimers();
  const delayed = Promise.withResolvers<Response>();
  let reads = 0;
  const fetch = vi.fn(async (_url: string, options?: RequestInit) => {
    if (options?.method === 'POST') return json(check('paused', 'interrupted', { status: 'paused', code: 'goal_changed' }));
    reads += 1;
    return reads === 1 ? json(check('reserved', 'running')) : delayed.promise;
  });
  vi.stubGlobal('fetch', fetch);
  openDesktopSessionGoal('Keep this replacement draft');
  await vi.advanceTimersByTimeAsync(2000);
  expect(reads).toBe(2);
  expect(document.body.textContent).toContain('正在验收目标');
  expect(button('保存目标').disabled).toBe(true);
  expect(button('暂停目标').disabled).toBe(false);
  expect(() => button('推进一轮')).toThrow();
  expect(() => button('验收目标')).toThrow();
  button('暂停目标').click();
  await vi.advanceTimersByTimeAsync(0);
  const posted = fetch.mock.calls.find(([, init]) => init?.method === 'POST')?.[1];
  expect(JSON.parse(String(posted?.body))).toEqual({ action: 'pause', version: 'reserved' });
  expect(posted?.headers).not.toHaveProperty('Prefer');
  expect(document.body.textContent).toContain('本次目标验收已中断');
  expect(document.body.textContent).toContain('会话目标 · 已暂停');
  delayed.resolve(json(check('wrong', 'passed', { status: 'completed', evidence: [7] })));
  await vi.advanceTimersByTimeAsync(6500);
  expect(fetch.mock.calls).toHaveLength(3);
  expect(document.body.textContent).not.toContain('目标验收通过');
  expect(document.body.textContent).not.toContain('会话目标 · 已完成');
  expect(document.querySelector('textarea')?.value).toBe('Keep this replacement draft');
  expect(button('保存目标').disabled).toBe(false);
});

it.each([
  ['incomplete', '证据不足，目标未完成'],
  ['unconfirmed', '未能确认目标达成'],
  ['interrupted', '本次目标验收已中断'],
])('shows an older synchronous Gateway verdict %s without completing or replaying', async (state, label) => {
  vi.useFakeTimers();
  const fetch = vi.fn(async (_url: string, options?: RequestInit) => options?.method === 'POST'
    ? json(check('settled', state))
    : json({ ...step('ready', 'finished'), supports_verify: true }));
  vi.stubGlobal('fetch', fetch);
  openDesktopSessionGoal('');
  await vi.advanceTimersByTimeAsync(0);
  button('验收目标').click();
  await vi.advanceTimersByTimeAsync(6000);
  expect(document.body.textContent).toContain(label);
  expect(document.body.textContent).toContain('会话目标 · 已启用');
  expect(document.body.textContent).not.toContain('会话目标 · 已完成');
  expect(button('验收目标').disabled).toBe(false);
  expect(fetch.mock.calls).toHaveLength(2);
});

it.each([
  step('older', 'finished'),
  { ...step('running', 'running'), supports_verify: true },
  { ...step('paused', 'finished', '', 'paused'), supports_verify: true },
])('does not offer verification without exact capability, active goal and finished evidence', async (snapshot) => {
  vi.useFakeTimers();
  const fetch = vi.fn(async () => json(snapshot));
  vi.stubGlobal('fetch', fetch);
  openDesktopSessionGoal('');
  await vi.advanceTimersByTimeAsync(0);
  expect(() => button('验收目标')).toThrow();
  expect(fetch.mock.calls).toHaveLength(1);
});

it('does not trust a verdict for a different step even when the goal capability is present', async () => {
  vi.useFakeTimers();
  const fetch = vi.fn(async () => json(check('current', 'passed', { stepRequestId: 'goal_previous', evidence: [7] })));
  vi.stubGlobal('fetch', fetch);
  openDesktopSessionGoal('');
  await vi.advanceTimersByTimeAsync(6000);
  expect(document.body.textContent).not.toContain('目标验收通过');
  expect(button('验收目标').disabled).toBe(false);
  expect(fetch.mock.calls).toHaveLength(1);
});

it('requires an explicit refresh on ambiguous verification POST and never retries it', async () => {
  vi.useFakeTimers();
  let reads = 0;
  const fetch = vi.fn(async (_url: string, options?: RequestInit) => {
    if (options?.method === 'POST') throw new TypeError('Connection closed after request');
    reads += 1;
    return reads === 1 ? json({ ...step('ready', 'finished'), supports_verify: true })
      : json(check('settled', 'passed', { status: 'completed', evidence: [7] }));
  });
  vi.stubGlobal('fetch', fetch);
  openDesktopSessionGoal('');
  await vi.advanceTimersByTimeAsync(0);
  button('验收目标').click();
  await vi.advanceTimersByTimeAsync(6000);
  expect(document.body.textContent).toContain('未能确认目标更新');
  expect(document.body.textContent).not.toContain('会话目标 · 已完成');
  expect(fetch.mock.calls).toHaveLength(2);
  button('重新读取目标').click();
  await vi.advanceTimersByTimeAsync(0);
  expect(document.body.textContent).toContain('目标验收通过');
  expect(fetch.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1);
});

it('closes verification observation without mutating it and reopens from a GET only', async () => {
  vi.useFakeTimers();
  const delayed = Promise.withResolvers<Response>();
  const fetch = vi.fn<(url: string, options?: RequestInit) => Promise<Response>>()
    .mockResolvedValueOnce(json(check('running', 'running')))
    .mockReturnValueOnce(delayed.promise)
    .mockResolvedValueOnce(json(check('latest', 'incomplete')));
  vi.stubGlobal('fetch', fetch);
  openDesktopSessionGoal('');
  await vi.advanceTimersByTimeAsync(2000);
  expect(fetch.mock.calls).toHaveLength(2);
  const observationSignal = fetch.mock.calls[1]?.[1]?.signal;
  button('关闭目标面板').click();
  expect(observationSignal?.aborted).toBe(true);
  openDesktopSessionGoal('');
  await vi.advanceTimersByTimeAsync(0);
  delayed.resolve(json(check('old', 'passed', { status: 'completed', evidence: [11] })));
  await vi.advanceTimersByTimeAsync(6000);
  expect(document.body.textContent).toContain('证据不足，目标未完成');
  expect(document.body.textContent).not.toContain('目标验收通过');
  expect(fetch.mock.calls).toHaveLength(3);
  expect(fetch.mock.calls.every(([, init]) => init?.method !== 'POST')).toBe(true);
});

it('refreshes serially during a live step without letting a delayed GET undo Pause', async () => {
  vi.useFakeTimers();
  const delayed = Promise.withResolvers<Response>();
  let reads = 0;
  const fetch = vi.fn(async (_url: string, options?: RequestInit) => {
    if (options?.method === 'POST') {
      expect(JSON.parse(String(options.body))).toEqual({ action: 'pause', version: 'live' });
      return json(step('paused', 'stopping', 'pause_requested', 'paused'));
    }
    reads += 1;
    if (reads === 1) return json(step('live', 'accepted'));
    if (reads === 2) return json(step('live', 'running'));
    if (reads === 3) return delayed.promise;
    return json(step('paused', 'stopped', 'goal_step_stopped', 'paused'));
  });
  vi.stubGlobal('fetch', fetch);
  openDesktopSessionGoal('Keep my replacement draft');
  await vi.advanceTimersByTimeAsync(0);
  expect(document.body.textContent).toContain('本轮已接收');
  expect(button('保存目标').disabled).toBe(true);
  expect(button('暂停目标').disabled).toBe(false);
  await vi.advanceTimersByTimeAsync(2000);
  expect(document.body.textContent).toContain('正在推进本轮');
  await vi.advanceTimersByTimeAsync(6000);
  expect(reads).toBe(3);
  button('暂停目标').click();
  await vi.advanceTimersByTimeAsync(0);
  expect(document.body.textContent).toContain('正在停止本轮');
  expect(button('保存目标').disabled).toBe(true);
  expect(document.querySelector('textarea')?.value).toBe('Keep my replacement draft');
  delayed.resolve(json(step('live', 'running')));
  await vi.advanceTimersByTimeAsync(0);
  expect(document.body.textContent).toContain('会话目标 · 已暂停');
  expect(document.body.textContent).not.toContain('正在推进本轮');
  await vi.advanceTimersByTimeAsync(2000);
  expect(document.body.textContent).toContain('本轮已停止');
  expect(button('保存目标').disabled).toBe(false);
  await vi.advanceTimersByTimeAsync(6000);
  expect(reads).toBe(4);
  expect(fetch.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1);
});

it.each([
  ['goal_session_busy', '会话还有已接收的消息，暂不推进目标'],
  ['goal_plan_active', '请先退出计划模式，再推进目标'],
  ['goal_step_active', '目标已在推进'],
  ['goal_unsupported_session', '只有内置主会话可以推进目标'],
])('preserves the %s rejection without assuming a version conflict', async (code, error) => {
  vi.useFakeTimers();
  const fetch = vi.fn(async (_url: string, options?: RequestInit) => options?.method === 'POST'
    ? json({ code, error }, 409)
    : json({ ...goal('current', 'Check the saved sample', 'active'), supports_advance: true }));
  vi.stubGlobal('fetch', fetch);
  openDesktopSessionGoal('');
  await vi.advanceTimersByTimeAsync(0);
  button('推进一轮').click();
  await vi.advanceTimersByTimeAsync(6000);
  expect(document.body.textContent).toContain(error);
  expect(document.body.textContent).not.toContain('目标或会话已更新');
  expect(fetch.mock.calls).toHaveLength(2);
});

it('keeps manual actions without a capability flag or prior step', async () => {
  vi.useFakeTimers();
  const fetch = vi.fn(async () => json(goal('legacy', 'Manual only', 'active')));
  vi.stubGlobal('fetch', fetch);
  openDesktopSessionGoal('');
  await vi.advanceTimersByTimeAsync(6000);
  expect(button('暂停目标').disabled).toBe(false);
  expect(button('标记目标完成').disabled).toBe(false);
  expect(() => button('推进一轮')).toThrow();
  expect(fetch.mock.calls).toHaveLength(1);
});

it.each([
  ['finished', '', '本轮已结束', '本轮结束不代表目标达成。'],
  ['failed', 'goal_deadline', '本轮推进失败', '已达到本轮推进时限。'],
])('shows %s without completing the goal and detects support from an existing run', async (state, code, label, reason) => {
  vi.useFakeTimers();
  const fetch = vi.fn(async () => json(step('current', state, code)));
  vi.stubGlobal('fetch', fetch);
  openDesktopSessionGoal('');
  await vi.advanceTimersByTimeAsync(6000);
  expect(document.body.textContent).toContain(label);
  expect(document.body.textContent).toContain(reason);
  expect(document.body.textContent).toContain('会话目标 · 已启用');
  expect(document.body.textContent).not.toContain('会话目标 · 已完成');
  expect(button('推进一轮').disabled).toBe(false);
  expect(fetch.mock.calls).toHaveLength(1);
});

it('halts observation on an error and only resumes after an explicit read', async () => {
  vi.useFakeTimers();
  let reads = 0;
  const fetch = vi.fn(async () => {
    reads += 1;
    if (reads === 1) return json(step('current', 'running'));
    if (reads === 2) throw new Error('Connection closed');
    if (reads === 3) return json(step('paused', 'stopping', 'pause_requested', 'paused'));
    return json(step('paused', 'interrupted', 'gateway_restarted', 'paused'));
  });
  vi.stubGlobal('fetch', fetch);
  openDesktopSessionGoal('');
  await vi.advanceTimersByTimeAsync(8000);
  expect(reads).toBe(2);
  expect(document.body.textContent).toContain('无法刷新本轮推进状态');
  expect(button('暂停目标').disabled).toBe(false);
  button('重新读取目标').click();
  await vi.advanceTimersByTimeAsync(0);
  expect(document.body.textContent).toContain('正在停止本轮');
  expect(button('恢复目标').disabled).toBe(true);
  expect(button('标记目标完成').disabled).toBe(true);
  await vi.advanceTimersByTimeAsync(6000);
  expect(reads).toBe(4);
  expect(document.body.textContent).toContain('本轮已中断');
  expect(document.body.textContent).toContain('网关已重启，本轮不会重放。');
  expect(button('恢复目标').disabled).toBe(false);
});

it('closing the panel aborts a pending refresh without sending a pause or another read', async () => {
  vi.useFakeTimers();
  const delayed = Promise.withResolvers<Response>();
  const fetch = vi.fn<(url: string, options?: RequestInit) => Promise<Response>>()
    .mockResolvedValueOnce(json(step('current', 'accepted'))).mockReturnValueOnce(delayed.promise);
  vi.stubGlobal('fetch', fetch);
  openDesktopSessionGoal('');
  await vi.advanceTimersByTimeAsync(2000);
  expect(fetch.mock.calls).toHaveLength(2);
  const signal = fetch.mock.calls[1]?.[1]?.signal;
  button('关闭目标面板').click();
  expect(signal?.aborted).toBe(true);
  delayed.resolve(json(step('current', 'running')));
  await vi.advanceTimersByTimeAsync(8000);
  expect(fetch.mock.calls).toHaveLength(2);
  expect(fetch.mock.calls.every(([, init]) => init?.method !== 'POST')).toBe(true);
  expect(document.querySelector('[aria-label="会话目标"]')).toBeNull();
});

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
