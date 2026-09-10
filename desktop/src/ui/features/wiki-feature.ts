import type { BackendConfig } from '../backend-client';
import { state, type TabKey } from '../state';

export const WIKI_DISABLED_MESSAGE = 'Wiki 功能暂未开放，请联系管理员开启。';

/** Keep the existing visible-by-default behavior while /api/config is unavailable. */
export function wikiFeatureEnabled(config: BackendConfig | null | undefined = state.config): boolean {
  return config?.wiki?.enabled !== false;
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
