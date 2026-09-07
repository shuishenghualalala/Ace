import type { ShellFeatureStates, ShellNavigationRegistry } from './sidebar-nav';

/** Wiki owns its shell contribution; the host only decides when to mount it. */
export function registerWikiNavigation(registry: ShellNavigationRegistry): () => void {
  return registry.register({
    id: 'wiki',
    label: '笔记',
    icon: 'icon-wiki',
    order: 45,
    resolveFeatureState: (features: ShellFeatureStates) => features.wiki ?? 'available',
  });
}
