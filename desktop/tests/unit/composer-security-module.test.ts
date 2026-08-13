/**
 * @vitest-environment happy-dom
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { createComposerContextView } from '../../src/ui/features/composer-context-view';
import { bindComposerToolbar } from '../../src/ui/features/composer-toolbar';
import { __resetAllStoresForTest, configStore } from '../../src/ui/stores/stores';

function mountComposer(): { disposeToolbar: () => void; disposeView: () => void } {
  document.body.innerHTML = `
    <main id="composer-root">
      <div data-composer-context-target="project"></div>
      <div data-composer-context-target="before-input"></div>
      <div data-composer-context-target="toolbar-left"></div>
      <div data-composer-context-target="toolbar-right"></div>
    </main>
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
});
