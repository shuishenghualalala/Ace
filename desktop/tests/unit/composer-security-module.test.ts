/**
 * @vitest-environment happy-dom
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createComposerContextView } from '../../src/ui/features/composer-context-view';
import { bindComposerToolbar } from '../../src/ui/features/composer-toolbar';
import { renderSecurityPermissionSettings } from '../../src/ui/features/security-permission-settings';
import { discardDraft, isDraftSession, setComposerTargetWorkspace } from '../../src/ui/features/workspaces';
import { __resetAllStoresForTest, authStore, configStore, sessionStore } from '../../src/ui/stores/stores';

function mountComposer(): { disposeToolbar: () => void; disposeView: () => void } {
  document.body.innerHTML = `
    <main id="composer-root">
      <div data-composer-context-target="project"></div>
      <div data-composer-context-target="before-input"></div>
      <div data-composer-context-target="toolbar-left"></div>
      <div data-composer-context-target="toolbar-right"></div>
    </main>
    <div id="confirm-dialog" hidden>
      <span id="confirm-title"></span><span id="confirm-message"></span>
      <button id="confirm-ok"></button><button id="confirm-cancel"></button>
    </div>
  `;
  const root = document.getElementById('composer-root') as HTMLElement;
  const view = createComposerContextView(root);
  const disposeToolbar = bindComposerToolbar();
  return { disposeToolbar, disposeView: () => view.dispose() };
}

function securityChip(): HTMLButtonElement {
  return document.getElementById('chat-security-mode-btn') as HTMLButtonElement;
}

function enableSecurityModule(): void {
  configStore.set({
    config: {
      model: 'craft',
      has_key: false,
      base_url: '',
      active_model_id: 'craft',
      models: [],
      security: { enabled: true, default_mode: 'request_approval' },
    },
  });
  window.dispatchEvent(new CustomEvent('security:config-change'));
}

beforeEach(() => {
  __resetAllStoresForTest();
  document.body.innerHTML = '';
  localStorage.clear();
});

afterEach(() => vi.unstubAllGlobals());

describe('composer 安全入口与安全模块开关', () => {
  it('安全模块关闭时请求批准 chip 禁用并显示开发中提示', () => {
    const { disposeToolbar, disposeView } = mountComposer();
    const chip = securityChip();

    expect(chip.disabled).toBe(true);
    expect(chip.title).toBe('功能正在开发中，敬请期待');

    disposeToolbar();
    disposeView();
  });

  it('安全模块开启时请求批准 chip 恢复可点', () => {
    enableSecurityModule();
    const { disposeToolbar, disposeView } = mountComposer();
    const chip = securityChip();

    expect(chip.disabled).toBe(false);
    expect(chip.title).toBe('请求批准');

    disposeToolbar();
    disposeView();
  });

  it('配置加载完成后入口状态随安全模块开关同步', () => {
    const { disposeToolbar, disposeView } = mountComposer();
    expect(securityChip().disabled).toBe(true);

    enableSecurityModule();
    expect(securityChip().disabled).toBe(false);
    expect(securityChip().title).toBe('请求批准');

    configStore.set({ config: null });
    window.dispatchEvent(new CustomEvent('security:config-change'));
    expect(securityChip().disabled).toBe(true);
    expect(securityChip().title).toBe('功能正在开发中，敬请期待');

    disposeToolbar();
    disposeView();
  });

  it('欢迎页可在确认后为新草稿选择完全访问，换空间后清除选择', async () => {
    enableSecurityModule();
    authStore.set({ isLoggedIn: true, userInfo: { uid: 'composer-owner' } });
    const securitySetMode = vi.fn();
    vi.stubGlobal('Crew', { securitySetMode });
    const { disposeToolbar, disposeView } = mountComposer();
    const fullAccess = () => document.querySelector<HTMLButtonElement>('[data-sec-mode="full_access"]');
    try {
      securityChip().click();
      expect(fullAccess()?.disabled).toBe(false);
      expect(document.querySelector<HTMLButtonElement>('[data-sec-mode="request_approval"]')?.disabled).toBe(false);
      expect(document.querySelector<HTMLButtonElement>('[data-sec-mode="auto_review"]')?.disabled).toBe(false);
      fullAccess()?.click();
      expect(document.getElementById('confirm-dialog')?.hidden).toBe(false);
      expect(sessionStore.get().activeSessionId).toBeNull();
      document.getElementById('confirm-cancel')?.click();
      await vi.waitFor(() => expect(fullAccess()).toBeNull());
      expect(sessionStore.get().activeSessionId).toBeNull();

      securityChip().click();
      fullAccess()?.click();
      document.getElementById('confirm-ok')?.click();
      await vi.waitFor(() => expect(securityChip().title).toBe('完全访问权限'));
      const draftId = sessionStore.get().activeSessionId;
      expect(draftId).toBeTruthy();
      expect(isDraftSession(draftId!)).toBe(true);
      expect(sessionStore.get().sessions).toEqual([]);
      expect(securitySetMode).not.toHaveBeenCalled();

      setComposerTargetWorkspace('other-project');
      window.dispatchEvent(new CustomEvent('workspace:context-changed'));
      expect(securityChip().title).toBe('请求批准');
      setComposerTargetWorkspace('default');
      window.dispatchEvent(new CustomEvent('workspace:context-changed'));
      expect(securityChip().title).toBe('请求批准');
    } finally {
      discardDraft();
      disposeToolbar();
      disposeView();
    }
  });

  it('关闭设置中的完全访问选项后清除草稿选择，重新开启也不恢复旧授权', async () => {
    enableSecurityModule();
    authStore.set({ isLoggedIn: true, userInfo: { uid: 'composer-owner' } });
    vi.stubGlobal('Crew', {
      securityGetFullAccessChoice: vi.fn(async () => ({ enabled: true })),
      securitySetFullAccessChoice: vi.fn(async (enabled: boolean) => ({ enabled })),
    });
    const { disposeToolbar, disposeView } = mountComposer();
    const settings = document.createElement('div');
    document.body.append(settings);
    renderSecurityPermissionSettings(settings);
    const toggle = settings.querySelector<HTMLInputElement>('[role="switch"]')!;
    const fullAccess = () => document.querySelector<HTMLButtonElement>('[data-sec-mode="full_access"]');
    try {
      await vi.waitFor(() => expect(toggle.disabled).toBe(false));
      securityChip().click();
      fullAccess()?.click();
      document.getElementById('confirm-ok')?.click();
      await vi.waitFor(() => expect(securityChip().title).toBe('完全访问权限'));

      toggle.click();
      await vi.waitFor(() => expect(securityChip().title).toBe('请求批准'));
      securityChip().click();
      expect(fullAccess()).toBeNull();

      toggle.click();
      await vi.waitFor(() => expect(toggle.disabled).toBe(false));
      securityChip().click();
      expect(fullAccess()).not.toBeNull();
      expect(securityChip().title).toBe('请求批准');
    } finally {
      discardDraft();
      disposeToolbar();
      disposeView();
    }
  });
});
