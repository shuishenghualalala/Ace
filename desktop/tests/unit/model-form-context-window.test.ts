/**
 * @vitest-environment happy-dom
 *
 * 模型编辑表单的「上下文窗口」档位下拉：
 *   - readModelForm 把 select 值读成 number 写入 ModelPayload.context_window
 *   - fillModelForm 标准档位选中；非标准值动态加 option 承接（不丢值）
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { __resetAllStoresForTest } from '../../src/ui/stores/stores';
import { openModelConfigModal, readModelForm } from '../../src/ui/features/config-panes';

beforeEach(() => {
  __resetAllStoresForTest();
  document.body.innerHTML = `
    <div id="model-connect-overlay"></div>
    <form id="cfg-model-form">
      <input id="cfg-model-id" />
      <input id="cfg-model-model" />
      <input id="cfg-model-base-url" />
      <input id="cfg-model-api-key" />
      <select id="cfg-model-context-window">
        <option value="128000">128k</option>
        <option value="200000">200k</option>
        <option value="256000" selected>256k（默认）</option>
        <option value="512000">512k</option>
        <option value="1000000">1M</option>
      </select>
    </form>
    <div id="cfg-model-context-window-wrap"></div>
  `;
  // happy-dom 不总尊重 parse-time selected attribute，显式设默认值模拟浏览器
  (document.getElementById('cfg-model-context-window') as HTMLSelectElement).value = '256000';
});

describe('模型表单 context_window 读', () => {
  it('readModelForm 把 1M 档读成 1000000', () => {
    (document.getElementById('cfg-model-context-window') as HTMLSelectElement).value = '1000000';
    expect(readModelForm().context_window).toBe(1000000);
  });

  it('readModelForm 默认 256k', () => {
    expect(readModelForm().context_window).toBe(256000);
  });
});

describe('模型表单 context_window 填（openModelConfigModal → fillModelForm）', () => {
  it('标准档位（512k）直接选中', () => {
    openModelConfigModal({ id: 'm', name: 'M', model: 'm', context_window: 512000, has_key: true, loaded: true });
    const select = document.getElementById('cfg-model-context-window') as HTMLSelectElement;
    expect(select.value).toBe('512000');
  });

  it('非标准值（180000）动态加 option 并选中，不丢值', () => {
    openModelConfigModal({ id: 'm', name: 'M', model: 'm', context_window: 180000, has_key: true, loaded: true });
    const select = document.getElementById('cfg-model-context-window') as HTMLSelectElement;
    expect(select.value).toBe('180000');
    expect(Array.from(select.options).some((o) => o.value === '180000')).toBe(true);
  });

  it('无 context_window 时回退默认 256k', () => {
    openModelConfigModal({ id: 'm', name: 'M', model: 'm', context_window: null, has_key: true, loaded: true });
    const select = document.getElementById('cfg-model-context-window') as HTMLSelectElement;
    expect(select.value).toBe('256000');
  });
});
