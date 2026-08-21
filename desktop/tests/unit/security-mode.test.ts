// @vitest-environment happy-dom

import { beforeEach, describe, expect, it } from 'vitest';
import type { BackendConfig } from '../../src/ui/backend-client';
import {
  detectedRuntimePlatform,
  formatCapabilitySummary,
  isWindowsPlatform,
  securityModuleEnabled,
} from '../../src/ui/features/security-mode';
import { __resetAllStoresForTest, configStore } from '../../src/ui/stores/stores';

function configWithSecurity(security: BackendConfig['security']): BackendConfig {
  return {
    model: 'craft',
    has_key: false,
    base_url: '',
    active_model_id: 'craft',
    models: [],
    security,
  };
}

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

describe('security module switch', () => {
  beforeEach(() => {
    __resetAllStoresForTest();
  });

  it('treats a missing config as disabled', () => {
    expect(securityModuleEnabled()).toBe(false);
  });

  it('is enabled only when the backend reports security.enabled === true', () => {
    configStore.set({ config: configWithSecurity({ enabled: true }) });
    expect(securityModuleEnabled()).toBe(true);

    configStore.set({ config: configWithSecurity({ enabled: false }) });
    expect(securityModuleEnabled()).toBe(false);
  });
});
