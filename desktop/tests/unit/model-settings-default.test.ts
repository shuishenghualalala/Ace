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
    const setDefault = document.querySelector<HTMLButtonElement>('[data-integration-id="deepseek"] [data-integration-action="set-default"]');
    expect(active?.dataset.active).toBe('true');
    expect(active?.textContent).toContain('默认模型');
    expect(selectable).not.toBeNull();
    expect(selectable?.textContent).toContain('DeepSeek');
    expect(setDefault?.textContent).toBe('设为默认');
  });

  it('可把已配置模型设为 owner 默认模型', async () => {
    const current = configStore.get().config!;
    const next = { ...current, model: 'deepseek-chat', active_model_id: 'deepseek' };
    const switchModel = vi.spyOn(backendApi, 'switchModel').mockResolvedValue(next);
    vi.spyOn(backendApi, 'config').mockResolvedValue(next);

    await renderConfigModels();
    document.querySelector<HTMLButtonElement>(
      '[data-integration-id="deepseek"] [data-integration-action="set-default"]',
    )?.click();

    await vi.waitFor(() => expect(switchModel).toHaveBeenCalledWith('deepseek'));
    await vi.waitFor(() => {
      expect(configStore.get().config?.active_model_id).toBe('deepseek');
      expect(document.querySelector<HTMLElement>('[data-integration-id="deepseek"]')?.dataset.active).toBe('true');
    });
  });

  it('允许编辑 owner 可覆盖的 default profile', async () => {
    configStore.set({
      config: {
        model: 'craft',
        has_key: true,
        base_url: '',
        active_model_id: 'craft',
        model_profiles: [
          ...models,
          {
            id: 'default',
            name: 'Default',
            model: 'your-model-name',
            has_key: false,
            loaded: true,
            builtin: true,
            editable: true,
          },
        ],
      },
    });

    await renderConfigModels();
    const selectable = document.querySelector<HTMLButtonElement>(
      '[data-integration-id="default"] [data-integration-select]',
    );
    expect(selectable).not.toBeNull();
  });
});
