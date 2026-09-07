import { createElement, type ReactNode } from 'react';
import type { UiFeatureRegistry, UiNavigationContribution } from './ui-feature-registry';

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

export function wikiNavigationEnabled(config: { wiki?: { enabled?: boolean } } | null | undefined): boolean {
  // Preserve the existing visible behavior while config is loading or retrying.
  return config?.wiki?.enabled !== false;
}

export function canNavigateToSidebarView(
  view: string,
  config: { wiki?: { enabled?: boolean } } | null | undefined,
): boolean {
  return view !== 'wiki' || wikiNavigationEnabled(config);
}

export function resolveSidebarViewAfterCapabilitiesChange<View extends string>(
  view: View,
  config: { wiki?: { enabled?: boolean } } | null | undefined,
): View | 'chat' {
  return view === 'wiki' && !wikiNavigationEnabled(config) ? 'chat' : view;
}
