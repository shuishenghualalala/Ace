/** Canonical Application Shell navigation inventory and feature-state resolver. */

import type { IconId } from '../components/icon';
import type { ProductMode } from '../stores/product-mode-store';
import type { TabKey } from '../state';
import { createDesktopWorkspaceRegistry, type WorkspaceRegistry } from './workspace-registry';

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
  security?: FeatureState;
  work?: Partial<Record<WorkLocation, FeatureState>>;
}

export interface ShellNavigationItem {
  id: ShellLocation;
  label: string;
  icon: IconId;
  featureState: FeatureState;
}

type ShellNavigationDefinition = Omit<ShellNavigationItem, 'featureState'> & { order: number };

const SHARED_NAVIGATION: ReadonlyArray<ShellNavigationDefinition> = [
  { id: 'cron', label: '任务', icon: 'process-clock', order: 70 },
  { id: 'security', label: '安全', icon: 'icon-security', order: 80 },
  { id: 'system', label: '系统', icon: 'icon-folder', order: 90 },
];

const ASSISTANT_NAVIGATION: ReadonlyArray<ShellNavigationDefinition> = [
  { id: 'skills', label: '技能', icon: 'process-skill', order: 30 },
  { id: 'sites', label: '灵感', icon: 'icon-inspiration', order: 40 },
  ...SHARED_NAVIGATION,
];

const WORK_NAVIGATION: ReadonlyArray<ShellNavigationDefinition> = [
  { id: 'items', label: '计划', icon: 'process-clock', order: 20 },
  { id: 'knowledge', label: '知识', icon: 'icon-wiki', order: 30 },
  ...SHARED_NAVIGATION,
];

export function isWorkLocation(location: ShellLocation): location is WorkLocation {
  return ['workbench', 'items', 'workspaces', 'knowledge', 'templates'].includes(location);
}

function isShellLocation(value: string): value is ShellLocation {
  return [
    'chat', 'agents', 'skills', 'sites', 'wiki', 'nearby', 'cron', 'security', 'system',
    'workbench', 'items', 'workspaces', 'knowledge', 'templates',
  ].includes(value);
}

/** Returns the one canonical top-level navigation inventory for a product mode. */
export function resolveShellNavigation(
  productMode: ProductMode,
  features: ShellFeatureStates = {},
  workspaceRegistry: WorkspaceRegistry = createDesktopWorkspaceRegistry(),
): ShellNavigationItem[] {
  const inventory = productMode === 'assistant' ? ASSISTANT_NAVIGATION : WORK_NAVIGATION;
  const registered = workspaceRegistry.navigation(productMode)
    .filter((item) => isShellLocation(item.id))
    .map((item) => ({
      id: item.id as ShellLocation,
      label: item.label,
      icon: item.icon,
      order: item.order,
    }));
  const items = [...inventory, ...registered]
    .sort((left, right) => left.order - right.order)
    .map((item) => ({
      ...item,
    featureState: item.id === 'agents'
      ? (features.agents ?? 'hidden')
      : item.id === 'security'
        ? (features.security ?? 'unavailable')
        : isWorkLocation(item.id)
          ? (features.work?.[item.id] ?? 'unavailable')
          : 'available',
    }));
  return items.filter((item) => item.featureState !== 'hidden') as ShellNavigationItem[];
}
