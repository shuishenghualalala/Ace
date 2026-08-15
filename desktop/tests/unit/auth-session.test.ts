import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({
  backend: 'gnome_libsecret',
  encryptionAvailable: true,
  tmpDir: '',
}));

vi.mock('electron', () => ({
  app: { getPath: () => state.tmpDir },
  safeStorage: {
    decryptString: (value: Buffer) => value.toString('utf8').replace(/^encrypted:/, ''),
    encryptString: (value: string) => Buffer.from(`encrypted:${value}`, 'utf8'),
    getSelectedStorageBackend: () => state.backend,
    isEncryptionAvailable: () => state.encryptionAvailable,
  },
}));

import {
  DesktopAuthSession,
  isOsBackedSessionStorageAvailable,
} from '../../src/main/auth-session';
import { desktopPrefsPath } from '../../src/main/desktop-prefs';

function response(payload: object, init: ResponseInit = {}): Response {
  const headers = new Headers(init.headers);
  headers.set('content-type', 'application/json');
  return new Response(JSON.stringify(payload), {
    status: 200,
    ...init,
    headers,
  });
}

function installEmailFetch(): void {
  vi.stubGlobal('fetch', vi.fn(async (input: string | URL) => {
    const pathname = new URL(String(input)).pathname;
    if (pathname === '/api/auth/config') {
      return response({
        ok: true,
        mode: 'email',
        configured: true,
        providerId: 'email',
      });
    }
    if (pathname === '/api/auth/login') {
      return response(
        {
          ok: true,
          user: {
            userId: 'user@example.com',
            email: 'user@example.com',
            phoneNumber: '',
          },
        },
        {
          headers: {
            'set-cookie': 'crew_auth_session=payload.signature; HttpOnly; SameSite=Strict',
          },
        },
      );
    }
    if (pathname === '/api/auth/session') {
      return response({ ok: true });
    }
    throw new Error(`unexpected request: ${pathname}`);
  }));
}

function authSession(): DesktopAuthSession {
  const session = new DesktopAuthSession();
  session.setGatewayProofProvider(() => 'test-proof');
  return session;
}

beforeEach(() => {
  vi.restoreAllMocks();
  state.tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'auth-session-'));
  state.backend = 'gnome_libsecret';
  state.encryptionAvailable = true;
  installEmailFetch();
});

describe('desktop auth session secure persistence', () => {
  it('rejects Linux basic_text and unknown storage backends', () => {
    state.backend = 'basic_text';
    expect(isOsBackedSessionStorageAvailable('linux')).toBe(false);
    state.backend = 'unknown';
    expect(isOsBackedSessionStorageAvailable('linux')).toBe(false);
    state.backend = 'kwallet6';
    expect(isOsBackedSessionStorageAvailable('linux')).toBe(true);
  });

  it('keeps a login memory-only when OS-backed encryption is unavailable', async () => {
    vi.spyOn(process, 'platform', 'get').mockReturnValue('linux');
    state.backend = 'basic_text';
    const session = authSession();
    await session.refreshConfig('http://127.0.0.1:8000');

    const result = await session.loginWithEmail(
      'http://127.0.0.1:8000',
      'user@example.com',
    );

    expect(result.ok).toBe(true);
    expect(session.cookieHeader()).toBe('crew_auth_session=payload.signature');
    expect(fs.existsSync(desktopPrefsPath())).toBe(false);
  });

  it('persists only encrypted cookie bytes with an approved backend', async () => {
    const session = authSession();
    await session.refreshConfig('http://127.0.0.1:8000');

    await session.loginWithEmail('http://127.0.0.1:8000', 'user@example.com');

    const persisted = fs.readFileSync(desktopPrefsPath(), 'utf8');
    const parsed = JSON.parse(persisted);
    expect(persisted).not.toContain('payload.signature');
    expect(parsed.authSession.providerId).toBe('email');
    expect(parsed.authSession.encryptedCookie).toBeTypeOf('string');
    expect(parsed.encryptedJwt).toBeUndefined();
  });

  it('deletes legacy and weakly encrypted persisted credentials', async () => {
    vi.spyOn(process, 'platform', 'get').mockReturnValue('linux');
    fs.writeFileSync(
      desktopPrefsPath(),
      JSON.stringify({
        encryptedJwt: 'legacy-secret',
        userInfo: { staffCode: 'legacy' },
        authSession: {
          providerId: 'email',
          encryptedCookie: Buffer.from(
            'encrypted:crew_auth_session=payload.signature',
            'utf8',
          ).toString('base64'),
          user: {
            userId: 'user@example.com',
            email: 'user@example.com',
            phoneNumber: '',
          },
        },
      }),
    );
    state.backend = 'basic_text';
    const session = authSession();

    const snapshot = await session.refreshConfig('http://127.0.0.1:8000');

    expect(snapshot.isLoggedIn).toBe(false);
    expect(JSON.parse(fs.readFileSync(desktopPrefsPath(), 'utf8'))).toEqual({});
  });
});
