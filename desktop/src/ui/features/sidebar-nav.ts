/** Canonical Application Shell navigation inventory and feature-state resolver. */

import type { IconId } from '../components/icon';
import type { ProductMode } from '../stores/product-mode-store';
import type { TabKey } from '../state';

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

type ShellNavigationDefinition = Omit<ShellNavigationItem, 'featureState'>;

const SHARED_NAVIGATION: ReadonlyArray<ShellNavigationDefinition> = [
  { id: 'wiki', label: '笔记', icon: 'icon-wiki' },
  { id: 'cron', label: '任务', icon: 'process-clock' },
  { id: 'security', label: '安全', icon: 'icon-security' },
  { id: 'system', label: '系统', icon: 'icon-folder' },
];

const ASSISTANT_NAVIGATION: ReadonlyArray<ShellNavigationDefinition> = [
  { id: 'chat', label: '对话', icon: 'process-thinking' },
  { id: 'agents', label: '外援', icon: 'icon-external-agent' },
  { id: 'skills', label: '技能', icon: 'process-skill' },
  { id: 'sites', label: '灵感', icon: 'icon-inspiration' },
  ...SHARED_NAVIGATION,
];

const WORK_NAVIGATION: ReadonlyArray<ShellNavigationDefinition> = [
  { id: 'workbench', label: '工作', icon: 'icon-task' },
  { id: 'items', label: '计划', icon: 'process-clock' },
  { id: 'knowledge', label: '知识', icon: 'icon-wiki' },
  ...SHARED_NAVIGATION,
];

export function isWorkLocation(location: ShellLocation): location is WorkLocation {
  return ['workbench', 'items', 'workspaces', 'knowledge', 'templates'].includes(location);
}

/** Returns the one canonical top-level navigation inventory for a product mode. */
export function resolveShellNavigation(
  productMode: ProductMode,
  features: ShellFeatureStates = {},
): ShellNavigationItem[] {
  const inventory = productMode === 'assistant' ? ASSISTANT_NAVIGATION : WORK_NAVIGATION;
  const items = inventory.map((item) => ({
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
