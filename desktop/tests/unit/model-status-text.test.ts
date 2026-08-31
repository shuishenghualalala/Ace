/**
 * @vitest-environment happy-dom
 */
import { describe, expect, it } from 'vitest';
import { modelStatusText } from '../../src/ui/features/config-panes';
import type { ModelOption } from '../../src/ui/backend-client';

const base = { id: 'x', name: 'X', model: 'x' } as ModelOption;

describe('modelStatusText', () => {
  it('默认模型有 Key：默认模型', () => {
    expect(modelStatusText({ ...base, has_key: true, builtin: false, loaded: true }, true)).toBe('默认模型');
  });

  it('默认模型无 Key：不能被「默认模型」吞掉警示', () => {
    expect(modelStatusText({ ...base, has_key: false, builtin: true, loaded: true }, true)).toBe('默认模型 · 缺少 Key');
  });

  it('内置模型无 Key：内置 · 缺少 Key', () => {
    expect(modelStatusText({ ...base, has_key: false, builtin: true, loaded: true }, false)).toBe('内置 · 缺少 Key');
  });

  it('非默认非内置无 Key：缺少 Key', () => {
    expect(modelStatusText({ ...base, has_key: false, builtin: false, loaded: true }, false)).toBe('缺少 Key');
  });

  it('有 Key 未加载：未加载；有 Key 已加载：已配置', () => {
    expect(modelStatusText({ ...base, has_key: true, builtin: false, loaded: false }, false)).toBe('未加载');
    expect(modelStatusText({ ...base, has_key: true, builtin: false, loaded: true }, false)).toBe('已配置');
  });
});
