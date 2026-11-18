import * as fs from 'node:fs/promises';
import * as http from 'node:http';
import * as os from 'node:os';
import * as path from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({
  tmpDir: '',
  encryptionAvailable: true,
}));

vi.mock('electron', () => ({
  app: { getPath: () => state.tmpDir },
  safeStorage: {
    isEncryptionAvailable: () => state.encryptionAvailable,
    encryptString: (value: string) => Buffer.from(value),
    decryptString: (value: Buffer) => value.toString(),
  },
}));

import { DesktopAuthSession } from '../../src/main/auth-session';

function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  });
}

let server: http.Server | undefined;

beforeEach(async () => {
  state.tmpDir = await fs.mkdtemp(path.join(os.tmpdir(), 'ace-auth-session-'));
  state.encryptionAvailable = true;
  vi.restoreAllMocks();
});

afterEach(async () => {
  vi.unstubAllGlobals();
  if (server) {
    server.closeAllConnections();
    await new Promise<void>((resolve) => server!.close(() => resolve()));
    server = undefined;
  }
  await fs.rm(state.tmpDir, { recursive: true, force: true });
});

describe('DesktopAuthSession email login', () => {
  it('does not authorize an unknown state or install auth from a superseded Gateway', async () => {
    const session = new DesktopAuthSession();
    expect(session.state()).toMatchObject({ mode: 'unknown', isLoggedIn: false });
    const obsolete = Promise.withResolvers<Response>();
    vi.stubGlobal('fetch', vi.fn()
      .mockReturnValueOnce(obsolete.promise)
      .mockResolvedValueOnce(jsonResponse({ ok: true, mode: 'email', providerId: 'email' })));
    const controller = new AbortController();
    const first = session.refreshConfig('http://127.0.0.1:8000', controller.signal);
    controller.abort();
    await session.refreshConfig('http://127.0.0.1:8002');
    obsolete.resolve(jsonResponse({ ok: true, mode: 'local', providerId: 'local' }));
    await expect(first).rejects.toMatchObject({ name: 'AbortError' });
    expect(session.state()).toMatchObject({ mode: 'email', isLoggedIn: false, providerId: 'email' });
  });

  it('clears the invalid cookie but preserves the Gateway session error', async () => {
    const responses = [
      jsonResponse(
        {
          ok: true,
          user: {
            userId: 'user-a@example.com',
            email: 'user-a@example.com',
            phoneNumber: '',
          },
        },
        200,
        { 'Set-Cookie': 'crew_auth_session=session-a; Path=/' },
      ),
      jsonResponse({
        ok: true,
        mode: 'email',
        configured: true,
        providerId: 'email',
      }),
      jsonResponse(
        {
          ok: false,
          error: 'Gateway 已由其他账号登录',
          code: 'ACTIVE_OWNER_CONFLICT',
        },
        423,
      ),
    ];
    const fetchMock = vi.fn(async () => responses.shift()!);
    vi.stubGlobal('fetch', fetchMock);
    const session = new DesktopAuthSession();

    const result = await session.loginWithEmail(
      'http://127.0.0.1:8000',
      'user-a@example.com',
    );

    expect(result).toMatchObject({
      ok: false,
      error: 'Gateway 已由其他账号登录',
      code: 'ACTIVE_OWNER_CONFLICT',
      status: 423,
    });
    expect(session.state().isLoggedIn).toBe(false);
    expect(session.cookieHeader()).toBe('');
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });
});

async function authGateway() {
  let verify = (_request: http.IncomingMessage, response: http.ServerResponse) => {
    response.end(JSON.stringify({ ok: true }));
  };
  server = http.createServer(async (request, response) => {
    response.setHeader('Content-Type', 'application/json');
    switch (request.url) {
      case '/api/auth/config':
        response.end(JSON.stringify({ ok: true, mode: 'email', providerId: 'email' }));
        break;
      case '/api/auth/login': {
        const body: Buffer[] = [];
        for await (const chunk of request) body.push(chunk);
        const { email } = JSON.parse(Buffer.concat(body).toString());
        response.setHeader('Set-Cookie', `crew_auth_session=${email}; Path=/`);
        response.end(JSON.stringify({ ok: true, user: { userId: email } }));
        break;
      }
      case '/api/auth/session':
        verify(request, response);
        break;
      case '/api/auth/logout':
        response.end(JSON.stringify({ ok: true }));
        break;
      default:
        response.writeHead(404).end();
    }
  });
  await new Promise<void>((resolve) => server!.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('Missing fixture port');
  return {
    url: `http://127.0.0.1:${address.port}`,
    onSession(handler: typeof verify) { verify = handler; },
  };
}

describe('DesktopAuthSession over HTTP', () => {
  it.each([true, false])('retains recoverable credentials after a 503 (storage=%s)', async (persistent) => {
    state.encryptionAvailable = persistent;
    const gateway = await authGateway();
    let session = new DesktopAuthSession();
    expect(await session.loginWithEmail(gateway.url, 'alice')).toMatchObject({ ok: true });
    gateway.onSession((_req, res) => res.writeHead(503).end(JSON.stringify({ ok: false })));
    expect(await session.refreshConfig(gateway.url)).toMatchObject({ isLoggedIn: false, user: null });
    expect(session.cookieHeader()).toBe('');
    expect(session.ownerAccountId()).toBeNull();
    if (persistent) session = new DesktopAuthSession();
    gateway.onSession((_req, res) => res.end(JSON.stringify({ ok: true })));
    expect(await session.refreshConfig(gateway.url)).toMatchObject({ isLoggedIn: true, user: { userId: 'alice' } });
    expect(session.ownerAccountId()).toBe('email:alice');
    expect(session.cookieHeader()).toBe('crew_auth_session=alice');
  });

  it.each([401, 403])('removes an authoritatively rejected credential (%s)', async (status) => {
    const gateway = await authGateway();
    const session = new DesktopAuthSession();
    expect(await session.loginWithEmail(gateway.url, 'alice')).toMatchObject({ ok: true });
    gateway.onSession((_req, res) => res.writeHead(status).end(JSON.stringify({ ok: false })));
    expect(await session.refreshConfig(gateway.url)).toMatchObject({ isLoggedIn: false });
    let usedCookie = false;
    gateway.onSession((_req, res) => { usedCookie = true; res.end(JSON.stringify({ ok: true })); });
    expect(await new DesktopAuthSession().refreshConfig(gateway.url)).toMatchObject({ isLoggedIn: false });
    expect(usedCookie).toBe(false);
  });

  it.each(['logout', 'switch'])('ignores a previous identity check after %s', async (operation) => {
    const gateway = await authGateway();
    const session = new DesktopAuthSession();
    await session.loginWithEmail(gateway.url, 'alice');
    const checkingAlice = Promise.withResolvers<http.ServerResponse>();
    gateway.onSession((_req, res) => checkingAlice.resolve(res));
    const obsolete = session.refreshConfig(gateway.url);
    const delayed = await checkingAlice.promise;
    gateway.onSession((_req, res) => res.end(JSON.stringify({ ok: true })));
    const switching = operation === 'switch';
    const result = switching
      ? await session.loginWithEmail(gateway.url, 'bob')
      : await session.logout(gateway.url);
    expect(result.ok).toBe(true);
    delayed.writeHead(switching ? 401 : 200).end(JSON.stringify({ ok: !switching }));
    await obsolete;
    const user = switching ? { userId: 'bob' } : null;
    expect(session.state()).toMatchObject({ isLoggedIn: switching, user });
    expect(await new DesktopAuthSession().refreshConfig(gateway.url)).toMatchObject({ isLoggedIn: switching, user });
  });
});
