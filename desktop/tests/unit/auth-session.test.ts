import { beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({
  tmpDir: '',
}));

vi.mock('electron', () => ({
  app: { getPath: () => state.tmpDir },
  safeStorage: {
    isEncryptionAvailable: () => false,
    encryptString: vi.fn(),
    decryptString: vi.fn(),
  },
}));

import { DesktopAuthSession } from '../../src/main/auth-session';

function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  });
}

beforeEach(() => {
  state.tmpDir = '';
  vi.restoreAllMocks();
});

describe('DesktopAuthSession email login', () => {
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
