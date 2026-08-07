/**
 * Work 办公域客户端单一数据源。
 *
 * 与 skill-store 同源思路：模块级缓存 + 加载方法 + 订阅。Work 多一类数据
 * （history / items / dashboard / sources / knowledge / templates / preferences
 * / settings / index），因此合并为一个 store，附带版本去重、断线重拉与 dispose。
 *
 * 不复制 fetch / WebSocket：所有请求经 workApi（复用 gatewayFetch），重连由
 * onBackendStatus 的 disconnected→connected 跃迁触发。
 */

import { createStore, type Store } from '../reducers/store-bus';
import {
  workApi,
  type WorkHistoryEntry,
  type WorkItem,
  type WorkDashboard,
  type WorkSourceState,
  type WorkKnowledgePage,
  type WorkTemplate,
  type WorkPreference,
  type WorkIndexStatus,
} from '../backend-client';

export interface WorkStoreState {
  selectedWorkspaceId: string | null;
  history: WorkHistoryEntry[];
  items: WorkItem[];
  dashboard: WorkDashboard | null;
  sources: WorkSourceState[];
  personalKnowledge: WorkKnowledgePage[];
  templates: WorkTemplate[];
  preferences: WorkPreference[];
  preferenceAutoLearning: boolean;
  settings: Record<string, unknown>;
  indexStatus: Record<string, WorkIndexStatus>;
  loading: boolean;
  error: string | null;
}

export const workStore: Store<WorkStoreState> = createStore<WorkStoreState>(
  {
    history: [],
    selectedWorkspaceId: null,
    items: [],
    dashboard: null,
    sources: [],
    personalKnowledge: [],
    templates: [],
    preferences: [],
    preferenceAutoLearning: true,
    settings: {},
    indexStatus: {},
    loading: false,
    error: null,
  },
  'work',
);

/** 事项版本指纹：version + updated_at 唯一标识一次写入。 */
function itemFingerprint(item: WorkItem): string {
  return `${item.item_id}:${item.version}:${item.updated_at}`;
}

/**
 * 用服务端列表替换本地事项，仅当指纹集合变化时才触发渲染（版本去重）。
 * 返回是否发生了实质变化。
 */
export function replaceItems(next: WorkItem[]): boolean {
  const prev = workStore.get().items;
  const prevFp = new Set(prev.map(itemFingerprint));
  const nextFp = new Set(next.map(itemFingerprint));
  if (prevFp.size === nextFp.size && [...nextFp].every((fp) => prevFp.has(fp))) return false;
  workStore.set({ items: next });
  return true;
}

/** 单条事项合并：命中则按版本去重更新，未命中则插入到列表头。 */
export function mergeItem(item: WorkItem): boolean {
  const items = workStore.get().items;
  const idx = items.findIndex((it) => it.item_id === item.item_id);
  if (idx === -1) {
    workStore.set({ items: [item, ...items] });
    return true;
  }
  if (items[idx].version >= item.version) return false;
  const merged = [...items];
  merged[idx] = item;
  workStore.set({ items: merged });
  return true;
}

/** 移除已删除的事项；返回是否命中。 */
export function removeItem(itemId: string): boolean {
  const items = workStore.get().items;
  if (!items.some((it) => it.item_id === itemId)) return false;
  workStore.set({ items: items.filter((it) => it.item_id !== itemId) });
  return true;
}

async function run<T>(label: string, fn: () => Promise<T>): Promise<T | null> {
  try {
    return await fn();
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    workStore.set({ error: `${label}: ${msg}` });
    return null;
  }
}

export async function loadWorkHistory(): Promise<void> {
  const res = await run('history', () => workApi.history());
  if (res) workStore.set({ history: res.entries });
}

export async function loadWorkItems(): Promise<void> {
  const res = await run('items', () => workApi.listItems(workStore.get().selectedWorkspaceId));
  if (res) replaceItems(res.items);
}

export async function loadWorkDashboard(): Promise<void> {
  const res = await run('dashboard', () => workApi.getDashboard(workStore.get().selectedWorkspaceId));
  if (res) workStore.set({ dashboard: res });
}

export function selectWorkWorkspace(workspaceId: string | null): void {
  workStore.set({ selectedWorkspaceId: workspaceId || null });
}

export async function loadWorkSources(): Promise<void> {
  const res = await run('sources', () => workApi.listSources());
  if (res) workStore.set({ sources: res.items });
}

export async function loadWorkKnowledge(): Promise<void> {
  const personal = await run('personal-knowledge', () => workApi.listPersonalKnowledge());
  workStore.set({
    personalKnowledge: personal?.items ?? workStore.get().personalKnowledge,
  });
}

export async function loadWorkTemplates(): Promise<void> {
  const res = await run('templates', () => workApi.listTemplates());
  if (res) workStore.set({ templates: res.items });
}

export async function loadWorkPreferences(): Promise<void> {
  const [settings, list] = await Promise.all([
    run('preference-settings', () => workApi.getPreferenceSettings()),
    run('preferences', () => workApi.listPreferences()),
  ]);
  workStore.set({
    preferenceAutoLearning: settings?.auto_learning_enabled ?? workStore.get().preferenceAutoLearning,
    preferences: list?.items ?? workStore.get().preferences,
  });
}

export async function loadWorkSettings(): Promise<void> {
  const res = await run('settings', () => workApi.getSettings());
  if (res) workStore.set({ settings: res });
}

export async function loadWorkIndexStatus(workspaceId: string): Promise<void> {
  const res = await run('index-status', () => workApi.getIndexStatus(workspaceId));
  if (res) {
    workStore.set({ indexStatus: { ...workStore.get().indexStatus, [workspaceId]: res } });
  }
}

/** 拉取所有 Work 概览数据；断线重连后调用。 */
export async function refreshWorkOverview(): Promise<void> {
  workStore.set({ loading: true, error: null });
  await Promise.all([
    loadWorkHistory(),
    loadWorkItems(),
    loadWorkDashboard(),
    loadWorkSources(),
    loadWorkTemplates(),
    loadWorkPreferences(),
    loadWorkSettings(),
  ]);
  workStore.set({ loading: false });
}

let statusUnsubscribe: (() => void) | null = null;
let wasConnected = false;

/**
 * 订阅后端状态跃迁：disconnected→connected 时重拉 Work 概览（断线重拉）。
 * 返回 dispose 函数，解除订阅并重置内部标记。
 */
export function connectWorkRefetch(): () => void {
  if (statusUnsubscribe) return statusUnsubscribe;
  statusUnsubscribe = window.Crew?.onBackendStatus?.((status) => {
    const connected = !!status?.connected;
    if (connected && !wasConnected) {
      void refreshWorkOverview().catch(() => {
        /* 重连后首次拉取失败由下一次跃迁或用户操作重试 */
      });
    }
    wasConnected = connected;
  }) ?? null;
  return () => {
    statusUnsubscribe?.();
    statusUnsubscribe = null;
    wasConnected = false;
  };
}
