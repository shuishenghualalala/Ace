/** Remote-login wall and account settings for the provider-neutral auth flow. */

import type { AuthStateSnapshot } from '../../shared/types';
import { notify } from '../state';

let currentState: AuthStateSnapshot = {
  mode: 'unknown',
  configured: false,
  providerId: 'custom',
  isLoggedIn: false,
  user: null,
};
let bound = false;
let countdownTimer: number | null = null;

function message(text: string, kind: 'error' | 'success' | 'info' = 'info'): void {
  const element = document.getElementById('login-message');
  if (!element) return;
  element.textContent = text;
  element.className = `login-message login-message--${kind}`;
  element.hidden = !text;
}

function setBusy(busy: boolean): void {
  const submit = document.getElementById('login-submit') as HTMLButtonElement | null;
  const code = document.getElementById('login-get-code') as HTMLButtonElement | null;
  if (submit) submit.disabled = busy;
  if (code && countdownTimer === null) code.disabled = busy;
}

function showLoginWall(show: boolean): void {
  document.body.classList.toggle('auth-wall-active', show);
  const modal = document.getElementById('login-modal');
  if (modal) modal.style.display = show ? 'flex' : 'none';
}

function displayValue(id: string, value: string): void {
  const element = document.getElementById(id);
  if (!element) return;
  element.textContent = value || '—';
  element.classList.toggle('is-empty', !value);
}

function renderAccount(): void {
  const loggedIn = document.getElementById('set-account-logged-in');
  const loggedOut = document.getElementById('set-account-logged-out');
  const user = currentState.user;
  const visibleUser = currentState.isLoggedIn && user;
  if (loggedIn) loggedIn.hidden = !visibleUser;
  if (loggedOut) loggedOut.hidden = Boolean(visibleUser);
  if (visibleUser) {
    const name = user.displayName || user.phoneNumber || user.userId;
    displayValue('set-account-name', name);
    displayValue('set-account-provider', currentState.mode === 'remote' ? currentState.providerId : '本机模式');
    displayValue('set-account-user-id', user.userId);
    displayValue('set-account-phone', user.phoneNumber);
    const avatar = document.getElementById('set-account-avatar');
    if (avatar) avatar.textContent = Array.from(name)[0] || '我';
    const logout = document.getElementById('set-account-logout') as HTMLButtonElement | null;
    if (logout) logout.hidden = currentState.mode !== 'remote';
    return;
  }
  const title = document.getElementById('set-account-empty-title');
  const description = document.getElementById('set-account-empty-desc');
  const login = document.getElementById('set-account-go-login') as HTMLButtonElement | null;
  if (title) title.textContent = currentState.configured ? '尚未登录' : '认证服务未配置';
  if (description) {
    description.textContent = currentState.configured
      ? '登录后，会话、模型配置和 Wiki 数据将按用户隔离。'
      : '请配置 auth.remote.base_url 或 CREW_AUTH_BASE_URL 后重启。';
  }
  if (login) login.hidden = currentState.mode !== 'remote' || !currentState.configured;
}

function applyState(state: AuthStateSnapshot): void {
  currentState = state;
  renderAccount();
  const needsLogin = state.mode === 'remote' && !state.isLoggedIn;
  showLoginWall(needsLogin);
  if (needsLogin && !state.configured) {
    message('远程认证已启用，但认证服务地址尚未配置。', 'error');
  }
}

function startCountdown(seconds = 60): void {
  const button = document.getElementById('login-get-code') as HTMLButtonElement | null;
  if (!button) return;
  if (countdownTimer !== null) window.clearInterval(countdownTimer);
  let remaining = seconds;
  button.disabled = true;
  button.textContent = `${remaining}s`;
  countdownTimer = window.setInterval(() => {
    remaining -= 1;
    if (remaining <= 0) {
      if (countdownTimer !== null) window.clearInterval(countdownTimer);
      countdownTimer = null;
      button.disabled = false;
      button.textContent = '获取验证码';
      return;
    }
    button.textContent = `${remaining}s`;
  }, 1000);
}

function responseError(result: Record<string, unknown>, fallback: string): string {
  return typeof result.error === 'string' && result.error.trim() ? result.error : fallback;
}

function bind(): void {
  if (bound) return;
  bound = true;
  document.getElementById('login-get-code')?.addEventListener('click', () => {
    const phone = (document.getElementById('login-phone') as HTMLInputElement | null)?.value.trim() || '';
    if (!phone) {
      message('请输入手机号。', 'error');
      return;
    }
    setBusy(true);
    void window.Crew.authSendCode(phone)
      .then((result) => {
        if (result.ok !== true) {
          message(responseError(result, '验证码发送失败。'), 'error');
          return;
        }
        message(typeof result.message === 'string' ? result.message : '验证码已发送。', 'success');
        startCountdown();
      })
      .catch((error) => message((error as Error).message || '验证码发送失败。', 'error'))
      .finally(() => setBusy(false));
  });

  const submit = (): void => {
    const phone = (document.getElementById('login-phone') as HTMLInputElement | null)?.value.trim() || '';
    const code = (document.getElementById('login-code') as HTMLInputElement | null)?.value.trim() || '';
    if (!phone || !code) {
      message('请输入手机号和验证码。', 'error');
      return;
    }
    setBusy(true);
    message('正在登录…', 'info');
    void window.Crew.authLogin(phone, code)
      .then((result) => {
        if (result.ok !== true) {
          message(responseError(result, '登录失败。'), 'error');
          return;
        }
        message('登录成功，正在加载账号数据…', 'success');
        window.setTimeout(() => window.location.reload(), 80);
      })
      .catch((error) => message((error as Error).message || '登录失败。', 'error'))
      .finally(() => setBusy(false));
  };
  document.getElementById('login-submit')?.addEventListener('click', submit);
  document.getElementById('login-code')?.addEventListener('keydown', (event) => {
    if ((event as KeyboardEvent).key === 'Enter') submit();
  });
  document.getElementById('login-quit')?.addEventListener('click', () => void window.Crew.appQuit());
  document.getElementById('set-account-go-login')?.addEventListener('click', () => showLoginWall(true));
  document.getElementById('set-account-logout')?.addEventListener('click', () => {
    void window.Crew.authLogout()
      .then((result) => {
        if (result.ok !== true) {
          notify(responseError(result, '退出登录失败。'));
          return;
        }
        window.location.reload();
      })
      .catch((error) => notify(`退出登录失败：${(error as Error).message}`));
  });
  window.Crew.onAuthSessionState((state) => applyState(state));
}

export async function initAuthFlow(): Promise<boolean> {
  bind();
  const state = await window.Crew.authGetState();
  applyState(state);
  return state.isLoggedIn;
}

export function renderAuthAccount(): void {
  renderAccount();
}
