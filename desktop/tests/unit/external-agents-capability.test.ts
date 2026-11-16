// @vitest-environment happy-dom

/**
 * 外部智能体（external-agents）能力可用性接线测试。
 *
 * 覆盖 ADR-0041 legacy-safe 规则：
 * - config 未加载（null）→ 可用；
 * - 旧后端未返回 feature_capabilities 字段 → 可用；
 * - 空 map / entry 缺席 / available=false → 不可用；
 * - available=true → 可用；
 * - externalAgentsEnabled() 实时反映 state.config 快照。
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import {
  bindExternalAgentsFeatureUi,
  EXTERNAL_AGENTS_FEATURE_ID,
  externalAgentsEnabled,
  syncExternalAgentsFeatureUi,
} from '../../src/ui/features/external-agents-feature';
import { __resetAllStoresForTest, configStore } from '../../src/ui/stores/stores';
import type { BackendConfig } from '../../src/ui/backend-client';

type CapabilityMap = NonNullable<BackendConfig['feature_capabilities']>;

function setCapabilities(capabilities: CapabilityMap): void {
  configStore.set({
    config: { feature_capabilities: capabilities } as Parameters<typeof configStore.set>[0]['config'],
  });
}

const featureEnabled: CapabilityMap = {
  [EXTERNAL_AGENTS_FEATURE_ID]: { state: 'active', available: true, generation: 'g1' },
};

const featureDisabled: CapabilityMap = {
  [EXTERNAL_AGENTS_FEATURE_ID]: { state: 'discovered', available: false, generation: null },
};

beforeEach(() => {
  __resetAllStoresForTest();
});

afterEach(() => {
  __resetAllStoresForTest();
});

describe('external-agents 能力可用性判定（ADR-0041 legacy-safe）', () => {
  it('config 未加载（null / undefined）时可用', () => {
    expect(externalAgentsEnabled(null)).toBe(true);
    expect(externalAgentsEnabled(undefined)).toBe(true);
    configStore.set({ config: null });
    expect(externalAgentsEnabled()).toBe(true);
  });

  it('旧后端无 feature_capabilities 字段时可用', () => {
    configStore.set({ config: {} as Parameters<typeof configStore.set>[0]['config'] });
    expect(externalAgentsEnabled()).toBe(true);
    expect(externalAgentsEnabled({} as BackendConfig)).toBe(true);
  });

  it('空 map → 不可用', () => {
    setCapabilities({});
    expect(externalAgentsEnabled()).toBe(false);
  });

  it('map 中 entry 缺席 → 不可用', () => {
    setCapabilities({ 'product.other': { state: 'active', available: true, generation: 'g1' } });
    expect(externalAgentsEnabled()).toBe(false);
  });

  it('entry available=true → 可用', () => {
    setCapabilities(featureEnabled);
    expect(externalAgentsEnabled()).toBe(true);
  });

  it('entry available=false → 不可用', () => {
    setCapabilities(featureDisabled);
    expect(externalAgentsEnabled()).toBe(false);
  });

  it('externalAgentsEnabled() 实时反映 state.config 快照', () => {
    configStore.set({ config: null });
    expect(externalAgentsEnabled()).toBe(true);

    setCapabilities(featureDisabled);
    expect(externalAgentsEnabled()).toBe(false);

    setCapabilities(featureEnabled);
    expect(externalAgentsEnabled()).toBe(true);

    configStore.set({ config: {} as Parameters<typeof configStore.set>[0]['config'] });
    expect(externalAgentsEnabled()).toBe(true);
  });
});

describe('external-agents 事件同步', () => {
  it('bindExternalAgentsFeatureUi 立即回调当前值并跟随事件更新，disposer 撤销订阅', () => {
    const seen: boolean[] = [];
    const dispose = bindExternalAgentsFeatureUi((enabled) => { seen.push(enabled); });
    // config 未加载 → 默认可用
    expect(seen).toEqual([true]);

    setCapabilities(featureDisabled);
    syncExternalAgentsFeatureUi();
    expect(seen).toEqual([true, false]);

    dispose();
    setCapabilities(featureEnabled);
    syncExternalAgentsFeatureUi();
    expect(seen).toEqual([true, false]);
  });
});
