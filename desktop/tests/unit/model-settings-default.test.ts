/**
 * @vitest-environment happy-dom
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
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
    <section id="settings-pane-model"></section>
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

describe('设置页默认模型展示', () => {
  it('按当前设置页契约展示默认模型和可选模型', async () => {
    await renderConfigModels();
    const active = document.querySelector<HTMLElement>('[data-integration-id="craft"]');
    const selectable = document.querySelector<HTMLButtonElement>('[data-integration-id="deepseek"] [data-integration-select]');
    expect(active?.dataset.active).toBe('true');
    expect(active?.textContent).toContain('默认模型');
    expect(selectable).not.toBeNull();
    expect(selectable?.textContent).toContain('DeepSeek');
  });
});
