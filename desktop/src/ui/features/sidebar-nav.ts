/** Canonical Application Shell navigation inventory and feature-state resolver. */

import type { IconId } from '../components/icon';
import type { ProductMode } from '../stores/product-mode-store';
import type { TabKey } from '../state';
import { registerWikiNavigation } from './wiki-navigation';

export type WorkLocation =
  | 'workbench'
  | 'items'
  | 'workspaces'
  | 'knowledge'
  | 'templates';
export type ShellLocation = TabKey | WorkLocation;
export type FeatureState = 'available' | 'unavailable' | 'hidden';

export interface ShellFeatureStates {
  agents?: FeatureState;
  wiki?: FeatureState;
  security?: FeatureState;
  work?: Partial<Record<WorkLocation, FeatureState>>;
}

export interface ShellNavigationItem {
  id: ShellLocation;
  label: string;
  icon: IconId;
  featureState: FeatureState;
}

export interface ShellNavigationContribution {
  id: ShellLocation;
  label: string;
  icon: IconId;
  order: number;
  productModes?: readonly ProductMode[];
  resolveFeatureState?: ((features: ShellFeatureStates) => FeatureState) | undefined;
}

export class ShellNavigationRegistry {
  private readonly entries = new Map<ShellLocation, ShellNavigationContribution>();

  register(contribution: ShellNavigationContribution): () => void {
    if (this.entries.has(contribution.id)) {
      throw new Error(`Shell navigation contribution already registered: ${contribution.id}`);
    }
    if (!Number.isFinite(contribution.order)) {
      throw new Error(`Shell navigation contribution has invalid order: ${contribution.id}`);
    }
    this.entries.set(contribution.id, contribution);
    let disposed = false;
    return () => {
      if (disposed) return;
      disposed = true;
      if (this.entries.get(contribution.id) === contribution) {
        this.entries.delete(contribution.id);
      }
    };
  }

  unregister(id: ShellLocation): boolean {
    return this.entries.delete(id);
  }

  resolve(productMode: ProductMode, features: ShellFeatureStates = {}): ShellNavigationItem[] {
    return Array.from(this.entries.values())
      .filter((item) => !item.productModes || item.productModes.includes(productMode))
      .sort((a, b) => a.order - b.order || a.id.localeCompare(b.id))
      .map((item) => ({
        id: item.id,
        label: item.label,
        icon: item.icon,
        featureState: item.resolveFeatureState?.(features) ?? 'available',
      }))
      .filter((item) => item.featureState !== 'hidden');
  }
}

function registerCoreNavigation(registry: ShellNavigationRegistry): void {
  const register = (item: ShellNavigationContribution): void => { registry.register(item); };
  register({ id: 'chat', label: '对话', icon: 'process-thinking', order: 10, productModes: ['assistant'] });
  register({
    id: 'agents', label: '外援', icon: 'icon-external-agent', order: 20, productModes: ['assistant'],
    resolveFeatureState: (features) => features.agents ?? 'hidden',
  });
  register({ id: 'skills', label: '技能', icon: 'process-skill', order: 30, productModes: ['assistant'] });
  register({ id: 'sites', label: '灵感', icon: 'icon-inspiration', order: 40, productModes: ['assistant'] });
  register({ id: 'cron', label: '任务', icon: 'process-clock', order: 50 });
  register({
    id: 'security', label: '安全', icon: 'icon-security', order: 60,
    resolveFeatureState: (features) => features.security ?? 'unavailable',
  });
  register({ id: 'system', label: '系统', icon: 'icon-folder', order: 70 });
  register({
    id: 'workbench', label: '工作', icon: 'icon-task', order: 10, productModes: ['work'],
    resolveFeatureState: (features) => features.work?.workbench ?? 'unavailable',
  });
  register({
    id: 'items', label: '计划', icon: 'process-clock', order: 20, productModes: ['work'],
    resolveFeatureState: (features) => features.work?.items ?? 'unavailable',
  });
  register({
    id: 'knowledge', label: '知识', icon: 'icon-wiki', order: 30, productModes: ['work'],
    resolveFeatureState: (features) => features.work?.knowledge ?? 'unavailable',
  });
}

/** Build a host-owned registry and explicitly mount first-party contributions. */
export function createShellNavigationRegistry(): ShellNavigationRegistry {
  const registry = new ShellNavigationRegistry();
  registerCoreNavigation(registry);
  registerWikiNavigation(registry);
  return registry;
}

export function isWorkLocation(location: ShellLocation): location is WorkLocation {
  return ['workbench', 'items', 'workspaces', 'knowledge', 'templates'].includes(location);
}

/** Returns the one canonical top-level navigation inventory for a product mode. */
export function resolveShellNavigation(
  productMode: ProductMode,
  features: ShellFeatureStates = {},
): ShellNavigationItem[] {
  return createShellNavigationRegistry().resolve(productMode, features);
}
