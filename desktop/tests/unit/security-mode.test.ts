// @vitest-environment happy-dom

import { describe, expect, it } from 'vitest';
import {
  detectedRuntimePlatform,
  formatCapabilitySummary,
  isWindowsPlatform,
} from '../../src/ui/features/security-mode';

describe('security capability summary', () => {
  it('does not claim protection when the native helper is absent', () => {
    expect(formatCapabilitySummary({ helper_present: false, detail: '组件缺失' })).toContain('未启用');
  });

  it('reports the native sandbox only when file and network controls are ready', () => {
    expect(formatCapabilitySummary({ helper_present: true, filesystem_sandbox: true, managed_network: true })).toContain('已启用');
  });

  it('recognizes the backend Windows platform name', () => {
    expect(isWindowsPlatform('windows')).toBe(true);
  });

  it('does not treat an unknown platform as Windows', () => {
    expect(isWindowsPlatform(undefined)).toBe(false);
    expect(isWindowsPlatform('')).toBe(false);
  });

  it('uses the desktop runtime platform when the gateway has not reported one', () => {
    Object.assign(window, { Crew: { runtimePlatform: 'darwin' } });
    expect(detectedRuntimePlatform()).toBe('darwin');
  });
});
