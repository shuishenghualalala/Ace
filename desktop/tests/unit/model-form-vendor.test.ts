/**
 * @vitest-environment happy-dom
 *
 * 模型表单的「厂商选择」联动：
 *   - 厂商模式：Base URL 只读自动填充、接口模型名走目录下拉、
 *     context_window 取目录元数据、provider 提交厂商 id，模型能力支持用户覆盖
 *   - 自定义模式：恢复手填表单，provider 提交协议名（openai/anthropic）
 *   - 目录加载失败：支持重试，也可继续手填
 *   - 编辑回填：provider 命中目录反选厂商与模型，未命中落自定义
 */
import { readFileSync } from 'node:fs';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { backendApi, type BackendConfig, type ModelOption, type VendorProfileOption } from '../../src/ui/backend-client';
import { __resetAllStoresForTest, configStore } from '../../src/ui/stores/stores';
import {
  __setVendorCatalogForTest,
  openModelConfigModal,
  readModelForm,
  renderConfigModels,
} from '../../src/ui/features/config-panes';

const VENDORS: VendorProfileOption[] = [
  {
    id: 'deepseek',
    name: 'DeepSeek',
    protocol: 'openai',
    base_url: 'https://api.deepseek.com',
    api_key_env: 'DEEPSEEK_API_KEY',
    models: [
      { id: 'deepseek-v4-flash', context_window: 1_000_000, max_tokens: 384_000, reasoning: true, vision: false },
      { id: 'deepseek-v4-flash-vision-exp', context_window: 1_000_000, max_tokens: 384_000, reasoning: true, vision: true },
    ],
  },
  {
    id: 'kimi-coding',
    name: 'Kimi For Coding',
    protocol: 'anthropic',
    base_url: 'https://api.kimi.com/coding',
    api_key_env: 'KIMI_API_KEY',
    models: [
      { id: 'kimi-for-coding', context_window: 262_144, max_tokens: 32_768, reasoning: true, vision: false },
    ],
  },
];

function el<T extends HTMLElement>(id: string): T {
  return document.getElementById(id) as T;
}

const desktopHtml = readFileSync('assets/index.html', 'utf8');

function changeVendor(id: string): void {
  const select = el<HTMLSelectElement>('cfg-model-vendor');
  select.value = id;
  select.dispatchEvent(new Event('change', { bubbles: true }));
}

function configWith(profile: ModelOption): BackendConfig & { ok: boolean; profile: ModelOption } {
  return {
    ok: true,
    profile,
    model: profile.model,
    has_key: profile.has_key,
    base_url: profile.base_url ?? '',
    active_model_id: profile.id,
    models: [profile],
  };
}

const savedModel: ModelOption = {
  id: 'deepseek-v4-flash', name: '我的 DeepSeek', model: 'deepseek-v4-flash', provider: 'deepseek',
  base_url: 'https://api.deepseek.com', loaded: true, has_key: true, max_tokens: 384_000,
};

beforeEach(() => {
  vi.restoreAllMocks();
  __resetAllStoresForTest();
  __setVendorCatalogForTest(VENDORS);
  const template = document.createElement('template');
  template.innerHTML = desktopHtml;
  document.body.replaceChildren(template.content.querySelector('#model-connect-overlay')!);
});

describe('厂商模式', () => {
  it('新增默认选第一个厂商，Base URL 只读并自动填充', () => {
    openModelConfigModal();
    expect(el<HTMLSelectElement>('cfg-model-vendor').value).toBe('deepseek');
    const baseUrl = el<HTMLInputElement>('cfg-model-base-url');
    expect(baseUrl.value).toBe('https://api.deepseek.com');
    expect(baseUrl.readOnly).toBe(true);
    expect(el<HTMLElement>('cfg-model-protocol-wrap').hidden).toBe(true);
  });

  it('目录模型：模型名走下拉、隐藏上下文窗口字段、提交厂商 id 与元数据', () => {
    openModelConfigModal();
    const modelSelect = el<HTMLSelectElement>('cfg-model-model-select');
    expect(modelSelect.hidden).toBe(false);
    expect(Array.from(modelSelect.options).map((o) => o.value)).toContain('deepseek-v4-flash');
    expect(el<HTMLElement>('cfg-model-context-window-wrap').hidden).toBe(true);
    expect(el('cfg-model-capabilities-wrap').hidden).toBe(false);

    modelSelect.value = 'deepseek-v4-flash-vision-exp';
    modelSelect.dispatchEvent(new Event('change'));
    const payload = readModelForm();
    expect(payload.provider).toBe('deepseek');
    expect(payload.base_url).toBe('https://api.deepseek.com');
    expect(payload.model).toBe('deepseek-v4-flash-vision-exp');
    expect(payload.context_window).toBe(1_000_000);
    expect(payload.max_tokens).toBe(384_000);
    expect(payload.capabilities).toEqual(['text', 'tools', 'vision']);
  });

  it('目录模型无 vision 时能力为 text+tools', () => {
    openModelConfigModal();
    const modelSelect = el<HTMLSelectElement>('cfg-model-model-select');
    modelSelect.value = 'deepseek-v4-flash';
    modelSelect.dispatchEvent(new Event('change'));
    expect(readModelForm().capabilities).toEqual(['text', 'tools']);
  });

  it('厂商内「自定义模型…」恢复手填输入与上下文窗口/最大输出字段', () => {
    openModelConfigModal();
    const modelSelect = el<HTMLSelectElement>('cfg-model-model-select');
    modelSelect.value = '__custom';
    modelSelect.dispatchEvent(new Event('change'));
    expect(el<HTMLInputElement>('cfg-model-model').hidden).toBe(false);
    expect(el<HTMLElement>('cfg-model-context-window-wrap').hidden).toBe(false);
    expect(el<HTMLElement>('cfg-model-max-tokens-wrap').hidden).toBe(false);
    el<HTMLInputElement>('cfg-model-model').value = 'deepseek-custom-x';
    const payload = readModelForm();
    expect(payload.provider).toBe('deepseek');
    expect(payload.model).toBe('deepseek-custom-x');
    expect(payload.context_window).toBe(256000);
    expect(payload.max_tokens).toBe(32768);
  });

  it('新增时模型 ID 为空则用目录模型 id 兜底', () => {
    openModelConfigModal();
    expect(el<HTMLInputElement>('cfg-model-id').value).toBe('deepseek-v4-flash');
  });
  it('切换供应商和推荐模型会更新自动 ID，并保留手填 ID', () => {
    openModelConfigModal();
    changeVendor('kimi-coding');
    const id = el<HTMLInputElement>('cfg-model-id');
    expect(id.value).toBe('kimi-for-coding');
    expect(readModelForm().base_url).toBe('https://api.kimi.com/coding');

    changeVendor('deepseek');
    const model = el<HTMLSelectElement>('cfg-model-model-select');
    model.value = 'deepseek-v4-flash-vision-exp';
    model.dispatchEvent(new Event('change', { bubbles: true }));
    expect(id.value).toBe('deepseek-v4-flash-vision-exp');

    id.value = 'my-model';
    id.dispatchEvent(new Event('input', { bubbles: true }));
    changeVendor('kimi-coding');
    expect(id.value).toBe('my-model');
  });

});

describe('自定义模式', () => {
  it('选自定义（Anthropic）后协议联动、Base URL 可编辑', () => {
    openModelConfigModal();
    const vendorSelect = el<HTMLSelectElement>('cfg-model-vendor');
    vendorSelect.value = 'custom:anthropic';
    vendorSelect.dispatchEvent(new Event('change'));

    expect(el<HTMLElement>('cfg-model-protocol-wrap').hidden).toBe(false);
    expect(el<HTMLInputElement>('cfg-model-base-url').readOnly).toBe(false);
    expect(el<HTMLSelectElement>('cfg-model-model-select').hidden).toBe(true);
    expect(
      document.querySelector<HTMLInputElement>('input[name="cfg-model-protocol"]:checked')?.value,
    ).toBe('anthropic');

    el<HTMLInputElement>('cfg-model-id').value = 'my-claude';
    el<HTMLInputElement>('cfg-model-model').value = 'claude-sonnet-4';
    el<HTMLInputElement>('cfg-model-base-url').value = 'https://api.anthropic.com';
    const payload = readModelForm();
    expect(payload.provider).toBe('anthropic');
    expect(payload.model).toBe('claude-sonnet-4');
    expect(payload.base_url).toBe('https://api.anthropic.com');
    expect(payload.max_tokens).toBe(32768);
  });
});

describe('编辑回填', () => {
  it('provider 命中目录：反选厂商与模型，应用目录元数据', () => {
    openModelConfigModal({
      id: 'kimi', name: 'kimi', model: 'kimi-for-coding', provider: 'kimi-coding',
      base_url: 'https://api.kimi.com/coding', has_key: true, loaded: true,
    });
    expect(el<HTMLSelectElement>('cfg-model-vendor').value).toBe('kimi-coding');
    expect(el<HTMLSelectElement>('cfg-model-model-select').value).toBe('kimi-for-coding');
    const payload = readModelForm();
    expect(payload.provider).toBe('kimi-coding');
    expect(payload.context_window).toBe(262_144);
  });

  it('provider 命中目录但模型不在目录：落「自定义模型…」并保留手填值', () => {
    openModelConfigModal({
      id: 'ds', name: 'ds', model: 'deepseek-custom-x', provider: 'deepseek',
      base_url: 'https://api.deepseek.com', context_window: 128000, has_key: true, loaded: true,
    });
    expect(el<HTMLSelectElement>('cfg-model-vendor').value).toBe('deepseek');
    expect(el<HTMLSelectElement>('cfg-model-model-select').value).toBe('__custom');
    expect(el<HTMLInputElement>('cfg-model-model').value).toBe('deepseek-custom-x');
    expect(el<HTMLSelectElement>('cfg-model-context-window').value).toBe('128000');
    const payload = readModelForm();
    expect(payload.provider).toBe('deepseek');
    expect(payload.model).toBe('deepseek-custom-x');
    expect(payload.context_window).toBe(128000);
  });

  it('provider 不在目录（通用 openai）：落自定义模式', () => {
    openModelConfigModal({
      id: 'my', name: 'my', model: 'gpt-4o-mini', provider: 'openai',
      base_url: 'https://api.example.com/v1', has_key: true, loaded: true,
    });
    expect(el<HTMLSelectElement>('cfg-model-vendor').value).toBe('custom:openai');
    expect(el<HTMLInputElement>('cfg-model-base-url').value).toBe('https://api.example.com/v1');
    expect(readModelForm().provider).toBe('openai');
  });
});

describe('目录加载失败回退', () => {
  it('目录为空时厂商下拉只有自定义项，表单行为与引入前一致', () => {
    __setVendorCatalogForTest([]);
    openModelConfigModal();
    const vendorSelect = el<HTMLSelectElement>('cfg-model-vendor');
    expect(Array.from(vendorSelect.options).map((o) => o.value)).toEqual([
      'custom:openai',
      'custom:anthropic',
    ]);
    expect(el<HTMLElement>('cfg-model-protocol-wrap').hidden).toBe(false);
    expect(el<HTMLInputElement>('cfg-model-base-url').readOnly).toBe(false);
  });
});

describe('目录请求与重试', () => {
  it('弹窗与设置页共享请求，加载成功后显示推荐模型', async () => {
    __setVendorCatalogForTest(null);
    configStore.set({ config: configWith(savedModel) });
    const pane = document.createElement('section');
    pane.id = 'settings-pane-model';
    document.body.append(pane);
    const fetchCatalog = vi.spyOn(backendApi, 'configVendors').mockResolvedValue({ vendors: VENDORS });

    const rendering = renderConfigModels();
    openModelConfigModal();
    await rendering;
    expect(fetchCatalog).toHaveBeenCalledTimes(1);
    expect(el<HTMLSelectElement>('cfg-model-vendor').value).toBe('deepseek');
    expect(el<HTMLSelectElement>('cfg-model-model-select').value).toBe('deepseek-v4-flash');
  });

  it('失败后可重试，并保持已填写的自定义配置', async () => {
    __setVendorCatalogForTest(null);
    const fetchCatalog = vi.spyOn(backendApi, 'configVendors')
      .mockRejectedValueOnce(new Error('connection refused'))
      .mockResolvedValueOnce({ vendors: VENDORS });
    openModelConfigModal();
    const retry = el<HTMLButtonElement>('cfg-model-vendor-retry');
    await vi.waitFor(() => expect(retry.hidden).toBe(false));
    expect(el('cfg-model-vendor-status').textContent).toContain('加载失败');
    const base = el<HTMLInputElement>('cfg-model-base-url');
    base.value = 'https://example.test/v1';
    base.dispatchEvent(new Event('input', { bubbles: true }));

    retry.click();
    await vi.waitFor(() => expect(el('cfg-model-vendor-status').textContent).toBe(''));
    expect(fetchCatalog).toHaveBeenCalledTimes(2);
    expect(base.value).toBe('https://example.test/v1');
    expect(el<HTMLSelectElement>('cfg-model-vendor').value).toBe('custom:openai');

    changeVendor('kimi-coding');
    expect(readModelForm()).toMatchObject({ provider: 'kimi-coding', model: 'kimi-for-coding' });
  });

  it('目录加载失败时保留已有配置的供应商和端点', async () => {
    __setVendorCatalogForTest(null);
    vi.spyOn(backendApi, 'configVendors').mockRejectedValue(new Error('offline'));
    const existing = { ...savedModel, base_url: 'https://example.test/v1' };
    openModelConfigModal(existing);

    await vi.waitFor(() => expect(el('cfg-model-vendor-retry').hidden).toBe(false));
    expect(readModelForm()).toMatchObject({
      provider: existing.provider, base_url: existing.base_url, model: existing.model,
    });
  });
});

describe('保存模型', () => {
  it('保存自定义模型的默认输出长度，并直接使用保存响应更新配置', async () => {
    __setVendorCatalogForTest([]);
    const profile = { ...savedModel, id: 'custom-model', model: 'custom-model', provider: 'openai', max_tokens: 32768 };
    const post = vi.spyOn(backendApi, 'createModel').mockResolvedValue(configWith(profile));
    const getConfig = vi.spyOn(backendApi, 'config');
    openModelConfigModal();
    el<HTMLInputElement>('cfg-model-id').value = profile.id;
    el<HTMLInputElement>('cfg-model-base-url').value = profile.base_url!;
    el<HTMLInputElement>('cfg-model-api-key').value = 'test-key';
    el<HTMLButtonElement>('cfg-model-save').click();

    expect(post).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
      id: profile.id, max_tokens: 32768, api_key: 'test-key',
    }));
    await vi.waitFor(() => expect(el('model-connect-overlay').hidden).toBe(true));
    expect(configStore.get().config?.models).toEqual([profile]);
    expect(getConfig).not.toHaveBeenCalled();
    expect(el<HTMLInputElement>('cfg-model-api-key').value).toBe('');
  });

  it('首次配置默认模型即可修改能力，保存后重新打开会回填', async () => {
    const placeholder: ModelOption = {
      id: 'default', name: '默认模型（未配置）', model: 'your-model-name', provider: 'openai',
      base_url: 'https://api.example.com/v1', has_key: false, loaded: true, builtin: true, editable: true,
    };
    const configured = { ...savedModel, id: 'default', name: savedModel.model, capabilities: ['text', 'vision'] };
    const put = vi.spyOn(backendApi, 'updateModel').mockResolvedValue(configWith(configured));
    openModelConfigModal(placeholder);
    changeVendor('deepseek');

    const group = el('cfg-model-capabilities-wrap');
    const tools = group.querySelector<HTMLInputElement>('input[value="tools"]')!;
    const vision = group.querySelector<HTMLInputElement>('input[value="vision"]')!;
    expect(group.hidden).toBe(false);
    expect(vision.checked).toBe(false);
    vision.click();
    tools.click();
    el<HTMLInputElement>('cfg-model-api-key').value = 'test-key';
    el<HTMLButtonElement>('cfg-model-save').click();

    expect(put).toHaveBeenCalledExactlyOnceWith('default', expect.objectContaining({
      capabilities: ['text', 'vision'], model: savedModel.model,
    }));
    expect(put.mock.calls[0]?.[1]).not.toHaveProperty('name');
    await vi.waitFor(() => expect(el('model-connect-overlay').hidden).toBe(true));

    openModelConfigModal(configStore.get().config!.models[0]!);
    expect(vision.checked).toBe(true);
    expect(tools.checked).toBe(false);
    expect(el('model-connect-title').textContent).toBe('编辑：deepseek-v4-flash');
  });

  it('编辑目录模型时保留已有密钥，失败后可用原表单重试', async () => {
    const put = vi.spyOn(backendApi, 'updateModel')
      .mockRejectedValueOnce(new Error('配置写入失败'))
      .mockResolvedValueOnce(configWith(savedModel));
    openModelConfigModal(savedModel);
    const maxTokens = el<HTMLInputElement>('cfg-model-max-tokens');
    const save = el<HTMLButtonElement>('cfg-model-save');
    expect(maxTokens.disabled).toBe(true);
    save.click();
    expect(save.disabled).toBe(true);
    await vi.waitFor(() => expect(el('cfg-model-error').textContent).toContain('配置写入失败'));
    expect(el('model-connect-overlay').hidden).toBe(false);
    expect(el<HTMLInputElement>('cfg-model-id').value).toBe(savedModel.id);
    expect(save.disabled).toBe(false);

    save.click();
    await vi.waitFor(() => expect(el('model-connect-overlay').hidden).toBe(true));
    expect(put).toHaveBeenCalledTimes(2);
    expect(put.mock.calls[1]?.[1]).not.toHaveProperty('api_key');
    expect(put.mock.calls[1]?.[1]).not.toHaveProperty('name');
    expect(put).toHaveBeenLastCalledWith(savedModel.id, expect.objectContaining({
      max_tokens: savedModel.max_tokens,
    }));
  });
});
