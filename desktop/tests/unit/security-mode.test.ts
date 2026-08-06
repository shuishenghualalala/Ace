import { describe, expect, it } from 'vitest';
import { formatCapabilitySummary } from '../../src/ui/features/security-mode';

describe('security capability summary', () => {
  it('does not claim protection when the native helper is absent', () => {
    expect(formatCapabilitySummary({ helper_present: false, detail: '组件缺失' })).toContain('未启用');
  });

  it('reports the native sandbox only when file and network controls are ready', () => {
    expect(formatCapabilitySummary({ helper_present: true, filesystem_sandbox: true, managed_network: true })).toContain('已启用');
  });
});
