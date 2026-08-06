/** Provider-neutral authentication state owned by the Electron main process. */

import { safeStorage } from 'electron';
import type { AuthStateSnapshot, AuthUserSnapshot } from '../shared/types';
import { readDesktopPrefsFile, writeDesktopPrefsFile } from './desktop-prefs';

const SESSION_COOKIE_NAME = 'crew_auth_session';
const PREFS_KEY = 'authSession';

interface AuthConfigPayload {
  ok?: boolean;
  mode?: 'local' | 'email' | 'remote' | 'dev';
  configured?: boolean;
  providerId?: string;
}

interface StoredAuthSession {
  providerId?: string;
  encryptedCookie?: string;
  user?: AuthUserSnapshot;
}

function parseJson(text: string): Record<string, unknown> {
  try {
    const parsed = JSON.parse(text) as unknown;
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : {};
  } catch {
    return {};
  }
}

function normalizedUser(raw: unknown): AuthUserSnapshot | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const record = raw as Record<string, unknown>;
  const userId = typeof record.userId === 'string' ? record.userId.trim() : '';
  const phoneNumber = typeof record.phoneNumber === 'string' ? record.phoneNumber.trim() : '';
  const displayName = typeof record.displayName === 'string' ? record.displayName.trim() : '';
  const email = typeof record.email === 'string' ? record.email.trim() : '';
  if (!userId) return null;
  return {
    userId,
    phoneNumber,
    ...(email ? { email } : {}),
    ...(displayName ? { displayName } : {}),
  };
}

function cookiePair(setCookie: string | null): string {
  if (!setCookie) return '';
  const match = setCookie.match(/(?:^|[,;]\s*)crew_auth_session=([^;\s,]+)/i);
  return match?.[1] ? `${SESSION_COOKIE_NAME}=${match[1]}` : '';
}

function syntheticUser(userId: string): AuthUserSnapshot {
  return { userId, phoneNumber: '' };
}

export class DesktopAuthSession {
  private mode: AuthStateSnapshot['mode'] = 'unknown';
  private configured = false;
  private providerId = 'custom';
  private cookie = '';
  private user: AuthUserSnapshot | null = null;

  private endpoint(baseUrl: string, pathname: string): string {
    const target = new URL(baseUrl);
    target.pathname = pathname;
    target.search = '';
    target.hash = '';
    return target.toString();
  }

  async refreshConfig(baseUrl: string): Promise<AuthStateSnapshot> {
    const previousMode = this.mode;
    const previousProviderId = this.providerId;
    const response = await fetch(this.endpoint(baseUrl, '/api/auth/config'), {
      method: 'GET',
      redirect: 'error',
    });
    const payload = parseJson(await response.text()) as AuthConfigPayload;
    if (!response.ok || payload.ok !== true) {
      throw new Error('无法读取认证配置');
    }
    this.mode = payload.mode === 'remote' || payload.mode === 'email' || payload.mode === 'dev' ? payload.mode : 'local';
    this.configured = payload.configured !== false;
    this.providerId = typeof payload.providerId === 'string' && payload.providerId.trim()
      ? payload.providerId.trim()
      : 'custom';
    if (this.mode === 'remote' || this.mode === 'email') {
      if (
        previousMode !== this.mode
        || previousProviderId !== this.providerId
        || !this.cookie
        || !this.user
      ) {
        this.restoreRemoteSession();
      }
      if (this.cookie && this.user) {
        const sessionResponse = await fetch(this.endpoint(baseUrl, '/api/auth/session'), {
          method: 'GET',
          headers: { Cookie: this.cookie },
          redirect: 'error',
        }).catch(() => null);
        if (!sessionResponse || !sessionResponse.ok) {
          this.cookie = '';
          this.user = null;
          this.clearPersistedSession();
        }
      }
    } else {
      this.cookie = '';
      this.user = syntheticUser(this.mode === 'dev' ? 'dev' : 'local');
    }
    return this.state();
  }

  state(): AuthStateSnapshot {
    const requiresSession = this.mode === 'remote' || this.mode === 'email';
    return {
      mode: this.mode,
      configured: this.configured,
      providerId: this.providerId,
      isLoggedIn: !requiresSession || Boolean(this.cookie && this.user),
      user: this.user,
    };
  }

  ownerAccountId(): string | null {
    if (this.mode === 'unknown') return null;
    if (this.mode === 'dev') return 'dev:dev';
    if (this.mode === 'local') return 'local';
    return this.user ? `${this.providerId}:${this.user.userId}` : null;
  }

  cookieHeader(): string {
    return this.mode === 'remote' || this.mode === 'email' ? this.cookie : '';
  }

  private restoreRemoteSession(): void {
    this.cookie = '';
    this.user = null;
    const prefs = readDesktopPrefsFile();
    const stored = prefs[PREFS_KEY] as StoredAuthSession | undefined;
    if (!stored || stored.providerId !== this.providerId || !stored.encryptedCookie) return;
    const user = normalizedUser(stored.user);
    if (!user || !safeStorage.isEncryptionAvailable()) return;
    try {
      const cookie = safeStorage.decryptString(Buffer.from(stored.encryptedCookie, 'base64'));
      if (!cookie.startsWith(`${SESSION_COOKIE_NAME}=`)) return;
      this.cookie = cookie;
      this.user = user;
    } catch {
      this.clearPersistedSession();
    }
  }

  private persistRemoteSession(): void {
    if (!this.cookie || !this.user || !safeStorage.isEncryptionAvailable()) return;
    const prefs = readDesktopPrefsFile();
    const encryptedCookie = safeStorage.encryptString(this.cookie).toString('base64');
    writeDesktopPrefsFile({
      ...prefs,
      [PREFS_KEY]: {
        providerId: this.providerId,
        encryptedCookie,
        user: this.user,
      } satisfies StoredAuthSession,
    });
  }

  private clearPersistedSession(): void {
    const prefs = readDesktopPrefsFile();
    if (!(PREFS_KEY in prefs)) return;
    delete prefs[PREFS_KEY];
    writeDesktopPrefsFile(prefs);
  }

  async sendCode(baseUrl: string, phoneNumber: string): Promise<Record<string, unknown>> {
    const response = await fetch(this.endpoint(baseUrl, '/api/auth/send-code'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phoneNumber }),
      redirect: 'error',
    });
    const payload = parseJson(await response.text());
    return { ...payload, ok: response.ok && payload.ok === true, status: response.status };
  }

  async login(
    baseUrl: string,
    phoneNumber: string,
    code: string,
  ): Promise<Record<string, unknown>> {
    const response = await fetch(this.endpoint(baseUrl, '/api/auth/login'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phoneNumber, code }),
      redirect: 'error',
    });
    const payload = parseJson(await response.text());
    const user = normalizedUser(payload.user);
    const cookie = cookiePair(response.headers.get('set-cookie'));
    if (!response.ok || payload.ok !== true || !user || !cookie) {
      return { ...payload, ok: false, status: response.status };
    }
    this.cookie = cookie;
    this.user = user;
    this.persistRemoteSession();
    return { ok: true, status: response.status, user };
  }

  async loginWithEmail(baseUrl: string, email: string): Promise<Record<string, unknown>> {
    const response = await fetch(this.endpoint(baseUrl, '/api/auth/login'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email }),
      redirect: 'error',
    });
    const payload = parseJson(await response.text());
    const user = normalizedUser(payload.user);
    const cookie = cookiePair(response.headers.get('set-cookie'));
    if (!response.ok || payload.ok !== true || !user || !cookie) {
      return { ...payload, ok: false, status: response.status };
    }
    this.cookie = cookie;
    this.user = user;
    this.persistRemoteSession();
    const verified = await this.refreshConfig(baseUrl);
    if (!verified.isLoggedIn) {
      return { ok: false, status: 401, error: '邮箱会话验证失败，请重试' };
    }
    return { ok: true, status: response.status, user: verified.user };
  }

  async logout(baseUrl: string): Promise<Record<string, unknown>> {
    if ((this.mode !== 'remote' && this.mode !== 'email') || !this.cookie) {
      this.cookie = '';
      this.user = null;
      this.clearPersistedSession();
      return { ok: true, released: true };
    }
    const response = await fetch(this.endpoint(baseUrl, '/api/auth/logout'), {
      method: 'POST',
      headers: { Cookie: this.cookie },
      redirect: 'error',
    });
    const payload = parseJson(await response.text());
    if (!response.ok || payload.ok !== true) {
      return { ...payload, ok: false, status: response.status };
    }
    this.cookie = '';
    this.user = null;
    this.clearPersistedSession();
    return { ...payload, ok: true, status: response.status };
  }
}

export const desktopAuthSession = new DesktopAuthSession();
