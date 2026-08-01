/**
 * @vitest-environment happy-dom
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { backendApi } from '../../src/ui/backend-client';
import { renderConfigModels } from '../../src/ui/features/config-panes';
import { __resetAllStoresForTest, configStore } from '../../src/ui/stores/stores';

const models = [
  { id: 'craft', name: 'Craft', model: 'craft', has_key: true, loaded: true },
  { id: 'deepseek', name: 'DeepSeek', model: 'deepseek-chat', has_key: true, loaded: true },
];

beforeEach(() => {
  vi.restoreAllMocks();
  __resetAllStoresForTest();
  document.body.innerHTML = `
    <div id="cfg-model-list"></div>
    <span id="cfg-stat-active"></span>
  `;
  configStore.set({
    config: {
      model: 'craft',
      has_key: true,
      base_url: '',
      active_model_id: 'craft',
      models,
    },
  });
});

describe('设置页默认模型切换', () => {
  it('调用全局模型接口并刷新默认模型状态', async () => {
    const switchSpy = vi.spyOn(backendApi, 'switchModel').mockResolvedValue({
      model: 'deepseek-chat',
      has_key: true,
      base_url: '',
      active_model_id: 'deepseek',
      models,
    });

    await renderConfigModels();
    expect(document.getElementById('cfg-stat-active')?.textContent).toBe('Craft');

    document.querySelector<HTMLButtonElement>('[data-model-activate="deepseek"]')?.click();

    await vi.waitFor(() => {
      expect(switchSpy).toHaveBeenCalledWith('deepseek');
      expect(document.getElementById('cfg-stat-active')?.textContent).toBe('DeepSeek');
      expect(document.querySelector<HTMLButtonElement>('[data-model-activate="deepseek"]')?.disabled).toBe(true);
    });
  });
});
