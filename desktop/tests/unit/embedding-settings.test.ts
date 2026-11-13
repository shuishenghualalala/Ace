/**
 * @vitest-environment happy-dom
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { backendApi, type BackendConfig } from '../../src/ui/backend-client';
import { renderConfigModels, readSemanticConfigForm } from '../../src/ui/features/config-panes';
import { __resetAllStoresForTest, configStore } from '../../src/ui/stores/stores';

const semantic = {
  enabled: true,
  provider: 'openai' as const,
  model: 'text-embedding-3-small',
  base_url: 'https://example.com/v1',
  api_key_env: 'CREW_WIKI_EMBEDDING_API_KEY',
  has_key: true,
  api_key_masked: 'sk-t****',
};

function makeConfig(overrides: Partial<BackendConfig> = {}): BackendConfig {
  return {
    model: 'craft',
    has_key: true,
    base_url: '',
    active_model_id: 'craft',
    models: [{ id: 'craft', name: 'Craft', model: 'craft', has_key: true, loaded: true }],
    wiki: { enabled: true, semantic },
    ...overrides,
  };
}

beforeEach(() => {
  vi.restoreAllMocks();
  __resetAllStoresForTest();
  document.body.innerHTML = '<section id="settings-pane-model"></section>';
  configStore.set({ config: makeConfig() });
});

describe('设置页 Embedding 配置', () => {
  it('展示当前语义检索配置并读取表单', async () => {
    await renderConfigModels();

    expect(document.querySelector('.semantic-config')?.textContent).toContain('Wiki 语义检索');
    expect(document.getElementById('cfg-wiki-semantic-key-env')).toBeNull();
    expect((document.getElementById('cfg-wiki-semantic-enabled') as HTMLInputElement).checked).toBe(true);
    expect(document.getElementById('cfg-wiki-semantic-body')?.hidden).toBe(false);
    expect(readSemanticConfigForm()).toMatchObject({
      enabled: true,
      provider: 'openai',
      model: 'text-embedding-3-small',
      base_url: 'https://example.com/v1',
      api_key_env: 'CREW_WIKI_EMBEDDING_API_KEY',
      api_key: '',
    });
    expect(document.getElementById('cfg-wiki-semantic-key-status')?.textContent).toContain('已配置 Key');
  });

  it('切换本地 provider 时隐藏 OpenAI 专用字段', async () => {
    await renderConfigModels();
    const provider = document.getElementById('cfg-wiki-semantic-provider') as HTMLSelectElement;
    provider.value = 'local';
    provider.dispatchEvent(new Event('change'));

    expect(document.getElementById('cfg-wiki-semantic-base-url-wrap')?.hidden).toBe(true);
    expect(document.getElementById('cfg-wiki-semantic-api-key-wrap')?.hidden).toBe(true);
    expect(document.getElementById('cfg-wiki-semantic-provider-hint')?.textContent).toContain('本地 fastembed');
  });

  it('开关直接控制配置卡片展开状态并自动保存', async () => {
    await renderConfigModels();
    const next = makeConfig({ wiki: { enabled: true, semantic: { ...semantic, enabled: false } } });
    const update = vi.spyOn(backendApi, 'updateWikiSemantic').mockResolvedValue({ ok: true, ...next });
    vi.spyOn(backendApi, 'config').mockResolvedValue(next);
    const toggle = document.getElementById('cfg-wiki-semantic-enabled') as HTMLInputElement;

    toggle.checked = false;
    toggle.dispatchEvent(new Event('change', { bubbles: true }));

    expect(document.getElementById('cfg-wiki-semantic-body')?.hidden).toBe(true);
    await vi.waitFor(() => expect(update).toHaveBeenCalledWith(expect.objectContaining({ enabled: false })));
  });

  it('保存时调用 owner 级语义配置接口', async () => {
    await renderConfigModels();
    const next = makeConfig({ wiki: { enabled: true, semantic: { ...semantic, enabled: false } } });
    const update = vi.spyOn(backendApi, 'updateWikiSemantic').mockResolvedValue({ ok: true, ...next });
    vi.spyOn(backendApi, 'config').mockResolvedValue(next);

    (document.getElementById('cfg-wiki-semantic-enabled') as HTMLInputElement).checked = false;
    (document.getElementById('cfg-wiki-semantic-api-key') as HTMLInputElement).value = 'new-secret';
    document.getElementById('cfg-wiki-semantic-form')?.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));

    await vi.waitFor(() => expect(update).toHaveBeenCalledWith({
      enabled: false,
      provider: 'openai',
      model: 'text-embedding-3-small',
      base_url: 'https://example.com/v1',
      api_key_env: 'CREW_WIKI_EMBEDDING_API_KEY',
      api_key: 'new-secret',
    }));
    await vi.waitFor(() => expect(document.getElementById('cfg-wiki-semantic-save-status')?.textContent).toBe('已保存'));
  });
});
