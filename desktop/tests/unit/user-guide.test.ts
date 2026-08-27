/**
 * @vitest-environment happy-dom
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  disposeUserGuide,
  maybeStartUserGuideOnce,
  startUserGuide,
} from '../../src/ui/features/user-guide';
import { authStore } from '../../src/ui/stores/auth-store';

const GUIDE_SEEN_KEY = 'Crew.desktop.userGuideSeen.v1:tenant-a%3Auser-1';

function mountGuideTargets(): void {
  document.body.innerHTML = `
    <div id="chat-composer-root"></div>
    <div class="mw-app-navigation__list"></div>
    <button id="settings-btn">设置</button>
    <button data-shell-command="help">?</button>
  `;
  document.querySelectorAll<HTMLElement>(
    '#chat-composer-root, .mw-app-navigation__list, #settings-btn, [data-shell-command="help"]',
  ).forEach((element, index) => {
    const top = 100 + index * 80;
    element.getBoundingClientRect = () => ({
      x: 80,
      y: top,
      left: 80,
      top,
      right: 280,
      bottom: top + 40,
      width: 200,
      height: 40,
      toJSON: () => ({}),
    });
  });
}

describe('first-use user guide', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    localStorage.clear();
    authStore.replace({ isLoggedIn: true, userInfo: { staffCode: 'tenant-a:user-1' } });
    mountGuideTargets();
  });

  afterEach(() => {
    disposeUserGuide();
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
    document.body.innerHTML = '';
    localStorage.clear();
    authStore.replace({ isLoggedIn: true, userInfo: { staffCode: 'local' } });
  });

  it('shows a welcome card once and completes the four-step tour', () => {
    maybeStartUserGuideOnce();
    expect(document.querySelector('.user-guide')).toBeNull();

    vi.advanceTimersByTime(450);
    expect(document.querySelector('.user-guide')?.textContent).toContain('第一次使用 Ace？');

    document.querySelector<HTMLButtonElement>('[data-tour-start]')?.click();
    expect(document.querySelector('.wiki-tour__title')?.textContent).toBe('第一步，先配置模型');
    expect(document.querySelector('.wiki-tour__progress')?.textContent).toBe('1 / 4');

    for (let index = 0; index < 4; index += 1) {
      document.querySelector<HTMLButtonElement>('[data-tour-next]')?.click();
    }

    expect(document.querySelector('.user-guide')).toBeNull();
    expect(localStorage.getItem(GUIDE_SEEN_KEY)).toBe('1');
    maybeStartUserGuideOnce();
    vi.runOnlyPendingTimers();
    expect(document.querySelector('.user-guide')).toBeNull();
  });

  it('replays from the help entry and can be skipped', () => {
    startUserGuide();
    expect(document.querySelector('.wiki-tour__title')?.textContent).toBe('第一步，先配置模型');
    document.querySelector<HTMLButtonElement>('[data-tour-skip]')?.click();
    expect(document.querySelector('.user-guide')).toBeNull();
    expect(localStorage.getItem(GUIDE_SEEN_KEY)).toBe('1');
  });

  it('keeps first-use state separate between tenants', () => {
    localStorage.setItem(GUIDE_SEEN_KEY, '1');
    authStore.replace({ isLoggedIn: true, userInfo: { staffCode: 'tenant-b:user-1' } });

    maybeStartUserGuideOnce();
    vi.advanceTimersByTime(450);

    expect(document.querySelector('.user-guide')?.textContent).toContain('第一次使用 Ace？');
  });
});
