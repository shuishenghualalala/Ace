/** @vitest-environment happy-dom */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { initAuthFlow } from '../../src/ui/features/login';

describe('email login wall', () => {
  beforeEach(() => {
    document.body.className = '';
    document.body.innerHTML = `
      <div class="app-shell"></div>
      <div id="login-modal" style="display:none">
        <span id="login-title"></span>
        <label id="login-identifier-label"></label>
        <input id="login-phone">
        <div id="login-code-group"><input id="login-code"></div>
        <button id="login-get-code"></button>
        <button id="login-submit"></button>
      <button id="login-quit"></button>
      <div id="login-message"></div>
      <div id="user-section" hidden>
        <button class="user-avatar-btn"></button>
        <span id="sidebar-user-avatar"></span>
        <span id="sidebar-user-name"></span>
        <span id="sidebar-user-email"></span>
      </div>
      </div>
      <div id="set-account-logged-in"></div>
      <div id="set-account-logged-out"></div>
      <button id="set-account-go-login"></button>
      <button id="set-account-logout"></button>
    `;
    Object.defineProperty(window, 'Crew', {
      configurable: true,
      value: {
        authGetState: vi.fn(async () => ({
          mode: 'email',
          configured: true,
          providerId: 'email',
          isLoggedIn: false,
          user: null,
        })),
        authSendCode: vi.fn(),
        authLogin: vi.fn(),
        authLogout: vi.fn(),
        appQuit: vi.fn(),
        rendererInitialStateReady: vi.fn(async () => ({ ok: true })),
        onAuthSessionState: vi.fn(() => () => undefined),
      },
    });
  });

  it('opens after auth state loads and uses the Ace entry label', async () => {
    const openAccount = vi.fn();
    window.addEventListener('user:open-account', openAccount, { once: true });
    await expect(initAuthFlow()).resolves.toBe(false);
    expect(window.Crew.rendererInitialStateReady).toHaveBeenCalledOnce();

    expect(document.body.classList.contains('auth-wall-active')).toBe(true);
    expect(document.getElementById('login-modal')?.style.display).toBe('flex');
    expect(document.getElementById('login-submit')?.textContent).toBe('进入 Ace');
    expect(document.getElementById('user-section')?.hidden).toBe(true);

    const crew = window.Crew as unknown as {
      onAuthSessionState: ReturnType<typeof vi.fn>;
    };
    const applySessionState = crew.onAuthSessionState.mock.calls[0][0] as (state: unknown) => void;
    applySessionState({
      mode: 'email',
      configured: true,
      providerId: 'email',
      isLoggedIn: true,
      user: { userId: 'tenant@example.com', email: 'tenant@example.com', phoneNumber: '' },
    });

    expect(document.getElementById('user-section')?.hidden).toBe(false);
    expect(document.getElementById('sidebar-user-avatar')?.textContent).toBe('t');
    expect(document.getElementById('sidebar-user-email')?.textContent).toBe('tenant@example.com');

    (document.querySelector('.user-avatar-btn') as HTMLButtonElement).click();
    expect(openAccount).toHaveBeenCalledOnce();
  });
});
