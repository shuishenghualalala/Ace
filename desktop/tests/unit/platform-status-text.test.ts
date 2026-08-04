import { describe, expect, it } from 'vitest';
import type { PlatformRow } from '../../src/ui/backend-client';
import { platformStatusText } from '../../src/ui/features/config-panes';

function row(overrides: Partial<PlatformRow> = {}): PlatformRow {
  return {
    name: 'weixin',
    label: '微信',
    available: true,
    configured: true,
    connected: false,
    ...overrides,
  };
}

describe('微信渠道状态文案', () => {
  it('网络异常时提示用户检查网络', () => {
    expect(platformStatusText(row({ error_kind: 'network' }))).toBe('网络异常，请检查网络');
  });

  it('其它错误仍展示后端错误', () => {
    expect(platformStatusText(row({ error: '微信会话已过期' }))).toBe('错误：微信会话已过期');
  });
});
