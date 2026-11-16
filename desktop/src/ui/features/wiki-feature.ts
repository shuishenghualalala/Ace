import type { BackendConfig } from '../backend-client';
import { state, type TabKey } from '../state';

export const WIKI_DISABLED_MESSAGE = 'Wiki 功能暂未开放，请联系管理员开启。';

/** Wiki 在 /api/config feature_capabilities 能力快照中的 feature id（与后端 Runtime 约定一致）。 */
export const WIKI_FEATURE_ID = 'product.wiki';

/**
 * 入口可用性判定（ADR-0041 legacy-safe）：
 * - config 未加载或旧后端未返回 feature_capabilities 字段 → 回落旧 wiki.enabled 语义（!== false 默认可见）；
 * - 能力快照存在时以其为准：product.wiki 条目缺席或 available=false（如 failed）均不可用，
 *   能力恢复后导航入口 / Wiki 页面 / reducer 注册经本函数同步重新可用（单一判定源）。
 */
export function wikiFeatureEnabled(config: BackendConfig | null | undefined = state.config): boolean {
  if (config == null || config.feature_capabilities == null) {
    return config?.wiki?.enabled !== false;
  }
  return Boolean(config.feature_capabilities[WIKI_FEATURE_ID]?.available);
}

export function canNavigateToWiki(config: BackendConfig | null | undefined): boolean {
  return wikiFeatureEnabled(config);
}

export function resolveTabAfterWikiCapabilityChange(activeTab: TabKey, enabled: boolean): TabKey {
  return activeTab === 'wiki' && !enabled ? 'chat' : activeTab;
}

export function syncWikiFeatureUi(): void {
  window.dispatchEvent(new CustomEvent('wiki:config-change'));
}

export function bindWikiFeatureUi(onChange: (enabled: boolean) => void): () => void {
  const handler = (): void => onChange(wikiFeatureEnabled());
  window.addEventListener('wiki:config-change', handler);
  handler();
  return () => window.removeEventListener('wiki:config-change', handler);
}
