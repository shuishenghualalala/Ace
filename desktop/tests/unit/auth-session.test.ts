import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({ tmpDir: '' }));

vi.mock('electron', () => ({
  app: { getPath: () => state.tmpDir },
  safeStorage: {
    isEncryptionAvailable: () => true,
    encryptString: (value: string) => Buffer.from(`encrypted:${value}`, 'utf8'),
    decryptString: (value: Buffer) => value.toString('utf8').replace(/^encrypted:/, ''),
  },
}));

import { DesktopAuthSession } from '../../src/main/auth-session';

beforeEach(() => {
  state.tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'auth-session-'));
  vi.restoreAllMocks();
});

describe('DesktopAuthSession', () => {
  it('logs into email mode and restores the encrypted tenant session', async () => {
    const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/auth/config')) {
        return new Response(JSON.stringify({ ok: true, mode: 'email', configured: true, providerId: 'email' }));
      }
      if (url.endsWith('/api/auth/login')) {
        expect(init?.body).toBe(JSON.stringify({ email: 'tenant@example.com' }));
        return new Response(JSON.stringify({
          ok: true,
          user: { userId: 'tenant@example.com', email: 'tenant@example.com', phoneNumber: '' },
        }), { headers: { 'set-cookie': 'crew_auth_session=email-token; HttpOnly; SameSite=Strict' } });
      }
      if (url.endsWith('/api/auth/session')) {
        return new Response(JSON.stringify({ ok: true, mode: 'email' }));
      }
      throw new Error(`unexpected URL: ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    const first = new DesktopAuthSession();
    const initialState = await first.refreshConfig('http://127.0.0.1:8000');
    expect(initialState.isLoggedIn).toBe(false);
    expect(initialState.user).toBeNull();
    expect(first.ownerAccountId()).toBeNull();
    const login = await first.loginWithEmail('http://127.0.0.1:8000', 'tenant@example.com');
    expect(login.ok).toBe(true);
    expect(first.ownerAccountId()).toBe('email:tenant@example.com');

    const restored = new DesktopAuthSession();
    const state = await restored.refreshConfig('http://127.0.0.1:8000');
    expect(state.isLoggedIn).toBe(true);
    expect(state.user).toEqual({ userId: 'tenant@example.com', email: 'tenant@example.com', phoneNumber: '' });
  });

  it('uses a signed gateway cookie and provider:userId owner without persisting phone in it', async () => {
    const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/auth/config')) {
        return new Response(JSON.stringify({
          ok: true,
          mode: 'remote',
          configured: true,
          providerId: 'example',
        }), { status: 200 });
      }
      if (url.endsWith('/api/auth/login')) {
        expect(init?.body).toBe(JSON.stringify({ phoneNumber: '13800000000', code: '123456' }));
        return new Response(JSON.stringify({
          ok: true,
          user: { userId: 'user-123', phoneNumber: '13800000000' },
        }), {
          status: 200,
          headers: { 'set-cookie': 'crew_auth_session=signed-value; HttpOnly; Path=/' },
        });
      }
      if (url.endsWith('/api/auth/session')) {
        expect((init?.headers as Record<string, string>).Cookie).toBe('crew_auth_session=signed-value');
        return new Response(JSON.stringify({ ok: true }), { status: 200 });
      }
      throw new Error(`unexpected URL ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    const session = new DesktopAuthSession();
    await session.refreshConfig('http://127.0.0.1:8000');
    const login = await session.login('http://127.0.0.1:8000', '13800000000', '123456');

    expect(login.ok).toBe(true);
    expect(session.ownerAccountId()).toBe('example:user-123');
    expect(session.cookieHeader()).toBe('crew_auth_session=signed-value');
    expect(session.cookieHeader()).not.toContain('13800000000');

    const restored = new DesktopAuthSession();
    const restoredState = await restored.refreshConfig('http://127.0.0.1:8000');
    expect(restoredState.isLoggedIn).toBe(true);
    expect(restoredState.user).toEqual({ userId: 'user-123', phoneNumber: '13800000000' });
  });

  it('clears an expired restored cookie after gateway validation', async () => {
    const first = new DesktopAuthSession();
    const responses = [
      new Response(JSON.stringify({ ok: true, mode: 'remote', configured: true, providerId: 'example' })),
      new Response(JSON.stringify({ ok: true, user: { userId: 'user-1', phoneNumber: '' } }), {
        headers: { 'set-cookie': 'crew_auth_session=old-cookie; HttpOnly; Path=/' },
      }),
    ];
    vi.stubGlobal('fetch', vi.fn(async () => responses.shift()!));
    await first.refreshConfig('http://127.0.0.1:8000');
    await first.login('http://127.0.0.1:8000', '1', '2');

    const restored = new DesktopAuthSession();
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) => (
      String(input).endsWith('/api/auth/config')
        ? new Response(JSON.stringify({ ok: true, mode: 'remote', configured: true, providerId: 'example' }))
        : new Response(JSON.stringify({ ok: false }), { status: 401 })
    )));

    const restoredState = await restored.refreshConfig('http://127.0.0.1:8000');
    expect(restoredState.isLoggedIn).toBe(false);
    expect(restored.cookieHeader()).toBe('');
  });
});
