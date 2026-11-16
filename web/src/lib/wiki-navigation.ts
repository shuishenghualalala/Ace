import { createElement, type ReactNode } from 'react';
import type { UiFeatureRegistry, UiNavigationContribution } from './ui-feature-registry';
import { wikiFeatureAvailable } from './featureFlags';
import type { AppConfig } from '../types';

export interface WikiNavigationContext {
  wikiEnabled: boolean;
}

export type WikiNavigationContribution = UiNavigationContribution<'wiki', WikiNavigationContext, ReactNode>;

/** Wiki owns the descriptor; Sidebar only renders the host projection. */
export const wikiNavigationContribution: WikiNavigationContribution = {
  id: 'wiki',
  label: 'Wiki',
  icon: createElement(
    'svg',
    { width: 16, height: 16, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 2, strokeLinecap: 'round', strokeLinejoin: 'round' },
    createElement('path', { d: 'M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H19a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1H6.5a1 1 0 0 0-1.5 2.5H20' }),
  ),
  order: 30,
  isAvailable: ({ wikiEnabled }) => wikiEnabled,
};

/** The host explicitly mounts the Wiki-owned contribution at build time. */
export function registerWikiNavigation<Context extends WikiNavigationContext>(
  registry: Pick<UiFeatureRegistry<'wiki', Context, ReactNode>, 'register'>,
): () => void {
  return registry.register({
    ...wikiNavigationContribution,
    isAvailable: ({ wikiEnabled }) => wikiEnabled,
  });
}

/**
 * 入口可用性判定（ADR-0041 legacy-safe）：能力快照存在时以 product.wiki 能力为准
 * （条目缺席/available=false 均不可用）；config 未加载或旧后端未返回 feature_capabilities
 * 字段时回落旧 wiki.enabled 语义（!== false 默认可见）。能力恢复后入口/页面随宿主同步重新可用。
 */
export function wikiNavigationEnabled(config: AppConfig | null | undefined): boolean {
  if (config == null || config.feature_capabilities == null) {
    // Preserve the existing visible behavior while config is loading or retrying.
    return config?.wiki?.enabled !== false;
  }
  return wikiFeatureAvailable(config);
}

export function canNavigateToSidebarView(
  view: string,
  config: AppConfig | null | undefined,
): boolean {
  return view !== 'wiki' || wikiNavigationEnabled(config);
}

export function resolveSidebarViewAfterCapabilitiesChange<View extends string>(
  view: View,
  config: AppConfig | null | undefined,
): View | 'chat' {
  return view === 'wiki' && !wikiNavigationEnabled(config) ? 'chat' : view;
}
