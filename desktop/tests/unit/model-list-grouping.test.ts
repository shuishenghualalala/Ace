/**
 * @vitest-environment happy-dom
 *
 * 设置页模型列表按厂商分组：
 *   - 命中厂商目录的模型按目录顺序归组，组头显示厂商名
 *   - 未收录的具名厂商排在目录之后，通用协议归入最后的「自定义」组
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  __setVendorCatalogForTest,
  renderConfigModels,
} from '../../src/ui/features/config-panes';
import { __resetAllStoresForTest, configStore } from '../../src/ui/stores/stores';
import type { VendorProfileOption } from '../../src/ui/backend-client';

const VENDORS: VendorProfileOption[] = [
  {
    id: 'deepseek', name: 'DeepSeek', protocol: 'openai',
    base_url: 'https://api.deepseek.com', api_key_env: 'DEEPSEEK_API_KEY',
    models: [{ id: 'deepseek-v4-flash', context_window: 1_000_000, reasoning: true, vision: false }],
  },
  {
    id: 'kimi-coding', name: 'Kimi For Coding', protocol: 'anthropic',
    base_url: 'https://api.kimi.com/coding', api_key_env: 'KIMI_API_KEY',
    models: [{ id: 'kimi-for-coding', context_window: 262_144, reasoning: true, vision: false }],
  },
];

beforeEach(() => {
  __resetAllStoresForTest();
  __setVendorCatalogForTest(VENDORS);
  document.body.innerHTML = `<section id="settings-pane-model"></section>`;
  configStore.set({
    config: {
      model: 'my-proxy',
      has_key: true,
      base_url: '',
      active_model_id: 'my-proxy',
      models: [
        // 故意乱序：自定义在最前，验证渲染按目录顺序重排
        { id: 'my-proxy', name: 'my-proxy', model: 'gpt-4o-mini', provider: 'openai', has_key: true, loaded: true },
        { id: 'kimi', name: 'kimi', model: 'kimi-for-coding', provider: 'kimi-coding', has_key: true, loaded: true },
        { id: 'ds', name: 'ds', model: 'deepseek-v4-flash', provider: 'deepseek', has_key: true, loaded: true },
      ],
    },
  });
});

describe('设置页模型列表分组', () => {
  it('按厂商目录顺序渲染组头，自定义组在最后', async () => {
    await renderConfigModels();
    const groups = Array.from(document.querySelectorAll('.settings-integrations__group')).map(
      (el) => el.textContent,
    );
    expect(groups).toEqual(['DeepSeek', 'Kimi For Coding', '自定义']);
  });

  it('组内模型排在对应组头之后', async () => {
    await renderConfigModels();
    const list = document.querySelector('.settings-integrations__list')!;
    const order = Array.from(list.children).map((el) =>
      el.classList.contains('settings-integrations__group')
        ? `group:${el.textContent}`
        : `item:${(el as HTMLElement).dataset.integrationId}`,
    );
    expect(order).toEqual([
      'group:DeepSeek', 'item:ds',
      'group:Kimi For Coding', 'item:kimi',
      'group:自定义', 'item:my-proxy',
    ]);
  });

  it.each(['zh-CN', 'en-US'])('在 %s 区域中，部分目录的排序保持目录、具名厂商、自定义', async (locale) => {
    __setVendorCatalogForTest([VENDORS[1]]);
    const collator = new Intl.Collator(locale);
    const compare = vi.spyOn(String.prototype, 'localeCompare').mockImplementation(function (this: string, other) {
      return collator.compare(this, other);
    });
    try {
      await renderConfigModels();
      const groups = [...document.querySelectorAll('.settings-integrations__group')].map(el => el.textContent);
      expect(groups).toEqual(['Kimi For Coding', 'deepseek', '自定义']);
    } finally {
      compare.mockRestore();
    }
  });

  it('目录为空时按原样 provider id 分组，通用协议落入「自定义」', async () => {
    __setVendorCatalogForTest([]);
    await renderConfigModels();
    const groups = Array.from(document.querySelectorAll('.settings-integrations__group')).map(
      (el) => el.textContent,
    );
    expect(groups).toEqual(['deepseek', 'kimi-coding', '自定义']);
  });
});
