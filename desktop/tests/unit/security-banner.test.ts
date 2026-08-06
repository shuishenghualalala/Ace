import { describe, expect, it } from 'vitest';

import { deriveState } from '../../src/ui/features/security-banner';

describe('security banner deriveState', () => {
  it('hides on non-win32 platforms', () => {
    expect(deriveState({ platform: 'darwin', helper_present: true, filesystem_sandbox: true, managed_network: true })).toBe('hidden');
  });

  it('reports missing when the helper binary is absent', () => {
    expect(deriveState({ platform: 'win32', helper_present: false })).toBe('missing');
  });

  it('reports stale when the runtime binary lags behind Rust source', () => {
    expect(deriveState({ platform: 'win32', helper_present: true, runtime_stale: true, filesystem_sandbox: true, managed_network: true })).toBe('stale');
  });

  it('reports off when the filesystem sandbox is not enabled', () => {
    expect(deriveState({ platform: 'win32', helper_present: true, filesystem_sandbox: false, managed_network: false })).toBe('off');
  });

  it('reports on when both filesystem and network controls are active', () => {
    expect(deriveState({ platform: 'win32', helper_present: true, filesystem_sandbox: true, managed_network: true })).toBe('on');
  });

  // U3: WFP 缺失时不能显示 on 让用户以为出网受控。
  it('reports net-missing when filesystem sandbox is on but WFP network control is absent (U3)', () => {
    expect(deriveState({ platform: 'win32', helper_present: true, filesystem_sandbox: true, managed_network: false })).toBe('net-missing');
  });

  it('reports net-missing when managed_network is undefined but filesystem sandbox is on', () => {
    // 兼容老 gateway 未返回 managed_network 字段的情况：保守视为缺失，
    // 与 security-mode.ts 的 formatCapabilitySummary 一致（falsy -> 不完整）。
    expect(deriveState({ platform: 'win32', helper_present: true, filesystem_sandbox: true })).toBe('net-missing');
  });
});
