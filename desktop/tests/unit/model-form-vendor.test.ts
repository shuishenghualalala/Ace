/**
 * @vitest-environment happy-dom
 *
 * 模型表单的「厂商选择」联动：
 *   - 厂商模式：Base URL 只读自动填充、接口模型名走目录下拉、
 *     context_window / capabilities 取目录元数据、provider 提交厂商 id
 *   - 自定义模式：恢复手填表单，provider 提交协议名（openai/anthropic）
 *   - 目录未加载（加载失败）：静默回退为纯手填表单
 *   - 编辑回填：provider 命中目录反选厂商与模型，未命中落自定义
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { __resetAllStoresForTest } from '../../src/ui/stores/stores';
import {
  __setVendorCatalogForTest,
  openModelConfigModal,
  readModelForm,
} from '../../src/ui/features/config-panes';
import type { VendorProfileOption } from '../../src/ui/backend-client';

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

beforeEach(() => {
  __resetAllStoresForTest();
  __setVendorCatalogForTest(VENDORS);
  document.body.innerHTML = `
    <div id="model-connect-overlay" hidden></div>
    <form id="cfg-model-form">
      <div id="cfg-model-vendor-wrap"><select id="cfg-model-vendor"></select></div>
      <input id="cfg-model-id" />
      <select id="cfg-model-model-select" hidden></select>
      <input id="cfg-model-model" />
      <div id="cfg-model-protocol-wrap">
        <input type="radio" name="cfg-model-protocol" value="openai" checked />
        <input type="radio" name="cfg-model-protocol" value="anthropic" />
      </div>
      <div id="cfg-model-base-url-wrap"><input id="cfg-model-base-url" /></div>
      <div id="cfg-model-api-key-wrap"><input id="cfg-model-api-key" /></div>
      <div id="cfg-model-context-window-wrap">
        <select id="cfg-model-context-window">
          <option value="128000">128k</option>
          <option value="256000" selected>256k（默认）</option>
          <option value="1000000">1M</option>
        </select>
      </div>
      <div id="cfg-model-max-tokens-wrap"><input id="cfg-model-max-tokens" type="number" /></div>
    </form>
  `;
  // happy-dom 不总尊重 parse-time selected attribute，显式设默认值模拟浏览器
  el<HTMLSelectElement>('cfg-model-context-window').value = '256000';
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
