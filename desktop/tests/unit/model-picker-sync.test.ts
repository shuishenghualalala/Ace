/**
 * @vitest-environment happy-dom
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { __resetAllStoresForTest, configStore } from '../../src/ui/stores/stores';
import { openModelSelectPopover, syncModelUi } from '../../src/ui/features/model-picker';

beforeEach(() => {
  __resetAllStoresForTest();
  document.body.innerHTML = `
    <span class="chat-model-trigger-text"></span>
    <span id="chat-model-picker-inline-label">old</span>
  `;
});

describe('syncModelUi', () => {
  it('设置页切换后同步 composer 内联模型标签', () => {
    configStore.set({
      config: {
        model: 'minimax/MiniMax-M3',
        has_key: true,
        base_url: 'https://api.minimaxi.com/anthropic',
        active_model_id: 'minimax-m3',
        models: [
          { id: 'craft', name: 'Craft', model: 'craft', has_key: true, loaded: true },
          { id: 'minimax-m3', name: 'MiniMax-M3', model: 'MiniMax-M3', has_key: true, loaded: true },
        ],
      },
      configModel: 'Craft',
    });
    syncModelUi();
    expect(document.getElementById('chat-model-picker-inline-label')?.textContent).toBe('MiniMax-M3');
    expect(document.querySelector('.chat-model-trigger-text')?.textContent).toBe('MiniMax-M3');
  });
});

describe('openModelSelectPopover', () => {
  function seedConfig(): void {
    configStore.set({
      config: {
        model: 'craft',
        has_key: true,
        base_url: '',
        active_model_id: 'craft',
        models: [
          { id: 'craft', name: 'Craft', model: 'craft', has_key: true, loaded: true },
          { id: 'glm-fast', name: 'GLM 快速', model: 'glm-4-flash', has_key: true, loaded: true },
        ],
      },
    });
  }

  function mountAnchor(): HTMLElement {
    const anchor = document.createElement('button');
    document.body.appendChild(anchor);
    return anchor;
  }

  it('渲染模型列表并高亮当前项，点击触发 onPick 并关闭', () => {
    seedConfig();
    const onPick = vi.fn();
    const onClose = vi.fn();
    openModelSelectPopover({ anchor: mountAnchor(), activeId: 'craft', onPick, onClose });

    const popover = document.querySelector('.composer-select-popover')!;
    expect(popover).not.toBeNull();
    const items = popover.querySelectorAll('[data-model-id]');
    expect(items).toHaveLength(2);
    expect(popover.querySelector('.composer-select-item.is-selected')?.getAttribute('data-model-id')).toBe('craft');
    expect(popover.textContent).toContain('GLM 快速');

    (items[1] as HTMLElement).click();
    expect(onPick).toHaveBeenCalledWith('glm-fast');
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(document.querySelector('.composer-select-popover')).toBeNull();
  });

  it('无模型时显示空态', () => {
    configStore.set({
      config: { model: '', has_key: false, base_url: '', active_model_id: '', models: [] },
    });
    openModelSelectPopover({ anchor: mountAnchor(), activeId: '', onPick: vi.fn() });
    expect(document.querySelector('.composer-select-popover__empty')?.textContent).toContain('暂无模型');
  });

  it('点击浮层外部与 Escape 均关闭且幂等', () => {
    seedConfig();
    const onClose = vi.fn();
    const close = openModelSelectPopover({ anchor: mountAnchor(), activeId: 'craft', onPick: vi.fn(), onClose });

    document.body.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(document.querySelector('.composer-select-popover')).toBeNull();

    // 已关闭后重复 close / Escape 不再触发 onClose
    close();
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('点击锚点本身不关闭（由调用方做 toggle）', () => {
    seedConfig();
    const anchor = mountAnchor();
    const onClose = vi.fn();
    openModelSelectPopover({ anchor, activeId: 'craft', onPick: vi.fn(), onClose });
    anchor.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    expect(onClose).not.toHaveBeenCalled();
    expect(document.querySelector('.composer-select-popover')).not.toBeNull();
  });
});
