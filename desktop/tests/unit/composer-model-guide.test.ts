/**
 * @vitest-environment happy-dom
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { bindComposerToolbar } from '../../src/ui/features/composer-toolbar';
import { __resetSessionModelBindingsForTest } from '../../src/ui/features/session-model';
import {
  __resetAllStoresForTest,
  configStore,
  uiStore,
} from '../../src/ui/stores/stores';

beforeEach(() => {
  __resetAllStoresForTest();
  __resetSessionModelBindingsForTest();
  configStore.set({
    config: {
      model: 'default',
      has_key: false,
      base_url: '',
      active_model_id: 'default',
      models: [
        {
          id: 'default',
          name: 'Default',
          model: 'default',
          has_key: false,
          loaded: true,
          builtin: true,
        },
      ],
    },
  });
  uiStore.set({ backendConnected: true });
  document.body.innerHTML = `
    <button type="button" id="settings-btn">设置</button>
    <div id="chat-model-picker-inline">
      <button type="button" id="chat-model-picker-inline-btn">
        <span id="chat-model-picker-inline-label">模型</span>
      </button>
    </div>
  `;
  const settingsButton = document.getElementById('settings-btn')!;
  settingsButton.getBoundingClientRect = () => ({
    x: 20,
    y: 500,
    left: 20,
    top: 500,
    right: 60,
    bottom: 540,
    width: 40,
    height: 40,
    toJSON: () => ({}),
  });
  vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => {
    callback(0);
    return 1;
  });
});

afterEach(() => {
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
  document.body.innerHTML = '';
  localStorage.clear();
  vi.restoreAllMocks();
});

describe('底部模型选择器的配置引导入口', () => {
  it('在右上角显示问号，并从第一步重播模型配置引导', () => {
    bindComposerToolbar();
    document.getElementById('chat-model-picker-inline-btn')?.click();

    const popover = document.getElementById('chat-model-inline-popover');
    const help = popover?.querySelector<HTMLButtonElement>('[data-model-tour-open]');
    expect(help?.textContent?.trim()).toBe('?');
    expect(help?.getAttribute('aria-label')).toBe('打开模型配置引导');

    help?.click();

    expect(document.getElementById('chat-model-inline-popover')).toBeNull();
    expect(document.querySelector('.model-tour')).not.toBeNull();
    expect(document.querySelector('.wiki-tour__title')?.textContent).toBe('先配置一个自己的模型');
  });
});
