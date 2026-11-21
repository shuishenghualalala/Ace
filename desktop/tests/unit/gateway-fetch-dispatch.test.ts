import { once } from 'node:events';
import { createServer } from 'node:http';
import type { AddressInfo } from 'node:net';
import { afterEach, expect, it, vi } from 'vitest';
import { dispatchRendererGatewayFetch } from '../../src/main/gateway-fetch-dispatch';

const goal = new URL('http://127.0.0.1:8000/api/session/team%3Aplan%20A/goal');
const post = { method: 'POST', body: JSON.stringify({ action: 'verify', version: 'ready' }) };

afterEach(() => vi.unstubAllGlobals());

it.each(['ECONNRESET', 'EPIPE'])('does not re-send a Goal mutation during Gateway startup after %s', async (code) => {
  const failure = Object.assign(new Error('Connection lost before a response'), { code });
  const fetch = vi.fn()
    .mockRejectedValueOnce(failure)
    .mockResolvedValueOnce(new Response('{}', { status: 202 }));
  vi.stubGlobal('fetch', fetch);
  const startupFetch = vi.fn(async (...args: [string, RequestInit]) => {
    try {
      return await fetch(...args);
    } catch {
      return fetch(...args);
    }
  });
  await expect(dispatchRendererGatewayFetch(goal, post, () => true, startupFetch)).rejects.toBe(failure);
  expect(startupFetch).not.toHaveBeenCalled();
  expect(fetch).toHaveBeenCalledExactlyOnceWith(goal.href, { ...post, redirect: 'error' });
});

it.each(['dropped response', 'redirect'])('uses one real loopback POST when the native fetch sees a %s', async (failure) => {
  const received: Array<{ url: string | undefined; body: string }> = [];
  const server = createServer((request, response) => {
    let body = '';
    request.setEncoding('utf8').on('data', (chunk: string) => { body += chunk; });
    request.on('end', () => {
      received.push({ url: request.url, body });
      if (received.length === 1 && failure === 'dropped response') request.socket.destroy();
      else if (received.length === 1) response.writeHead(307, { Location: '/api/session/test/goal' }).end();
      else response.writeHead(202).end();
    });
  });
  server.listen(0, '127.0.0.1');
  try {
    await once(server, 'listening');
    const { port } = server.address() as AddressInfo;
    const path = '/api/session/test/goal';
    const target = new URL(`http://127.0.0.1:${port}${path}`);
    const startupFetch = vi.fn(async (url: string, init: RequestInit) => {
      try {
        return await fetch(url, init);
      } catch {
        return fetch(url, init);
      }
    });
    await expect(dispatchRendererGatewayFetch(target, post, () => true, startupFetch)).rejects.toThrow();
    expect(received).toEqual([{ url: path, body: post.body }]);
    expect(startupFetch).not.toHaveBeenCalled();
  } finally {
    const closed = new Promise<void>((resolve) => { server.close(() => resolve()); });
    server.closeAllConnections();
    await closed;
  }
});

it.each(['document', 'owner', 'generation', 'authEpoch'] as const)(
  'refuses a Goal mutation when its %s changed during Gateway readiness', async (changed) => {
    const current = { document: 'old-doc', owner: 'account-A', generation: 7, authEpoch: 1 };
    const captured = { ...current };
    const readiness = Promise.withResolvers<void>();
    const fetch = vi.fn(async () => new Response('{}'));
    vi.stubGlobal('fetch', fetch);
    const startupFetch = vi.fn(async () => new Response('{}'));
    const scopeIsCurrent = () => current.document === captured.document && current.owner === captured.owner
      && current.generation === captured.generation && current.authEpoch === captured.authEpoch;
    const dispatch = readiness.promise.then(() => dispatchRendererGatewayFetch(goal, post, scopeIsCurrent, startupFetch));
    if (changed === 'generation' || changed === 'authEpoch') current[changed] += 1;
    else current[changed] = 'replacement';
    readiness.resolve();
    await expect(dispatch).rejects.toThrow('目标操作未提交');
    expect(fetch).not.toHaveBeenCalled();
    expect(startupFetch).not.toHaveBeenCalled();
  },
);

it.each([
  ['GET', goal.href],
  ['POST', 'http://127.0.0.1:8000/api/session/id/goal/other'],
  ['POST', 'http://127.0.0.1:8000/api/session/id/goal-not-the-route'],
  ['POST', 'http://127.0.0.1:8000/api/session/id%2Fchild/goal'],
  ['POST', 'http://127.0.0.1:8000/api/sessions/id/goal'],
])('keeps existing retry for %s %s', async (method, url) => {
  const init = { method };
  const response = new Response('{}');
  const start = vi.fn(async () => response);
  const stale = vi.fn(() => false);
  expect(await dispatchRendererGatewayFetch(new URL(url), init, stale, start)).toBe(response);
  expect(start).toHaveBeenCalledExactlyOnceWith(url, init);
  expect(stale).not.toHaveBeenCalled();
});

it.each([
  '/api/session/id/goal?trace=fixture',
  '/api/session/id/goal/',
  '/api/%73ession/id/%67oal',
  '/api/session/%E5%AE%9E%E9%AA%8C/goal',
  '/api/session/id/sub/../goal',
])('treats the decoded exact Goal route as single-dispatch: %s', async (path) => {
  const target = new URL(path, goal);
  const response = new Response('{}', { status: 202 });
  const fetch = vi.fn(async () => response);
  vi.stubGlobal('fetch', fetch);
  const startupFetch = vi.fn(async () => new Response('{}'));
  expect(await dispatchRendererGatewayFetch(target, { ...post, method: 'PoSt' }, () => true, startupFetch)).toBe(response);
  expect(fetch).toHaveBeenCalledExactlyOnceWith(target.href, { ...post, method: 'PoSt', redirect: 'error' });
  expect(startupFetch).not.toHaveBeenCalled();
});
