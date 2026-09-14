// @vitest-environment happy-dom

/**
 * Wiki 入口能力可用性接线测试（ADR-0041 legacy-safe）。
 *
 * 覆盖：
 * - config 未加载（null / undefined）→ 可用；
 * - 旧后端未返回 feature_capabilities 字段 → 回落旧 wiki.enabled 语义（!== false 默认可见）；
 * - feature_capabilities 存在时以 product.wiki 能力为准：available=false（如 failed）
 *   或条目缺席 → 不可用，与 wiki.enabled 无关；
 * - 能力恢复（available=true）后入口重新可用；
 * - wikiFeatureEnabled() 实时反映 configStore 快照，bind/sync 事件同步跟随。
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import {
  bindWikiFeatureUi,
  canNavigateToWiki,
  syncWikiFeatureUi,
  WIKI_FEATURE_ID,
  wikiFeatureEnabled,
} from '../../src/ui/features/wiki-feature';
import { __resetAllStoresForTest, configStore } from '../../src/ui/stores/stores';
import type { BackendConfig } from '../../src/ui/backend-client';

type CapabilityMap = NonNullable<BackendConfig['feature_capabilities']>;
type StoreConfig = Parameters<typeof configStore.set>[0]['config'];

function capability(available: boolean): CapabilityMap[string] {
  return { state: available ? 'active' : 'failed', available, generation: available ? 'g1' : null };
}

function setConfig(config: BackendConfig | null): void {
  configStore.set({ config: config as StoreConfig });
}

/** 旧后端响应：只有（或不带）wiki.enabled，没有 feature_capabilities 字段。 */
function configWithoutCapabilitySnapshot(wikiEnabled?: boolean): BackendConfig {
  return (
    wikiEnabled === undefined ? {} : { wiki: { enabled: wikiEnabled } }
  ) as BackendConfig;
}

function configWithCapabilities(capabilities: CapabilityMap, wikiEnabled = true): BackendConfig {
  return { wiki: { enabled: wikiEnabled }, feature_capabilities: capabilities } as BackendConfig;
}

beforeEach(() => {
  __resetAllStoresForTest();
});

afterEach(() => {
  __resetAllStoresForTest();
});

describe('wiki 入口能力可用性判定（ADR-0041 legacy-safe）', () => {
  it('config 未加载（null / undefined）时可用', () => {
    expect(wikiFeatureEnabled(null)).toBe(true);
    expect(wikiFeatureEnabled(undefined)).toBe(true);
    setConfig(null);
    expect(wikiFeatureEnabled()).toBe(true);
    expect(canNavigateToWiki(null)).toBe(true);
  });

  it('旧后端无 feature_capabilities 字段时回落 wiki.enabled 语义（!== false 默认可见）', () => {
    expect(wikiFeatureEnabled(configWithoutCapabilitySnapshot())).toBe(true);
    expect(wikiFeatureEnabled(configWithoutCapabilitySnapshot(true))).toBe(true);
    expect(wikiFeatureEnabled(configWithoutCapabilitySnapshot(false))).toBe(false);
    expect(canNavigateToWiki(configWithoutCapabilitySnapshot(false))).toBe(false);
  });

  it('能力快照存在时以其为准：available=false（如 failed）即使 wiki.enabled=true 也不可用', () => {
    const config = configWithCapabilities({ [WIKI_FEATURE_ID]: capability(false) });
    expect(wikiFeatureEnabled(config)).toBe(false);
    expect(canNavigateToWiki(config)).toBe(false);

    setConfig(config);
    expect(wikiFeatureEnabled()).toBe(false);
  });

  it('映射存在而 product.wiki 条目缺席 → 不可用', () => {
    const config = configWithCapabilities({ 'product.other': capability(true) });
    expect(wikiFeatureEnabled(config)).toBe(false);
  });

  it('entry available=true → 可用（state/generation 不影响结果）', () => {
    expect(wikiFeatureEnabled(configWithCapabilities({ [WIKI_FEATURE_ID]: capability(true) }))).toBe(true);
  });

  it('能力恢复（available=false → true）后入口重新可用', () => {
    setConfig(configWithCapabilities({ [WIKI_FEATURE_ID]: capability(false) }));
    expect(wikiFeatureEnabled()).toBe(false);

    setConfig(configWithCapabilities({ [WIKI_FEATURE_ID]: capability(true) }));
    expect(wikiFeatureEnabled()).toBe(true);
  });
});

describe('wiki 入口事件同步', () => {
  it('bindWikiFeatureUi 立即回调当前值并跟随事件更新，disposer 撤销订阅', () => {
    const seen: boolean[] = [];
    const dispose = bindWikiFeatureUi((enabled) => { seen.push(enabled); });
    expect(seen).toEqual([true]);

    setConfig(configWithCapabilities({ [WIKI_FEATURE_ID]: capability(false) }));
    syncWikiFeatureUi();
    expect(seen).toEqual([true, false]);

    dispose();
    setConfig(configWithCapabilities({ [WIKI_FEATURE_ID]: capability(true) }));
    syncWikiFeatureUi();
    expect(seen).toEqual([true, false]);
  });
});
