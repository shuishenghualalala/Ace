/** Work 历史：仅展示办公会话与事项；通用助手历史留在通用模式。 */

import { workStore, type WorkStoreState } from '../../stores/work-store';
import { productModeStore, updateProductModeView } from '../../stores/product-mode-store';
import { state } from '../../state';
import type { WorkHistoryEntry } from '../../backend-client';
import { createIcon } from '../../components/icon';
import { epochMilliseconds } from './time';

const HISTORY_CONTAINER_ID = 'mw-work-history';
type HistoryBucket = 'today' | 'week' | 'month' | 'earlier';

const HISTORY_BUCKETS: Array<{ id: HistoryBucket; label: string }> = [
  { id: 'today', label: '今天' },
  { id: 'week', label: '本周' },
  { id: 'month', label: '本月' },
  { id: 'earlier', label: '更早' },
];

const KIND_LABELS: Record<WorkHistoryEntry['entity_type'], string> = {
  work_session: '办公',
  work_item_session: '事项',
  work_item: '事项',
  agent_session: 'Agent',
};

export interface WorkHistoryCommands {
  newItem?(trigger: HTMLElement): void;
  manageItems?(): void;
  openItem?(itemId: string): void;
  openSession?(
    sessionId: string,
    mode: 'work' | 'assistant',
    initialMessage?: string,
    itemId?: string,
  ): void | Promise<void>;
  openWorkbench?(): void;
}

/** 按最近活动时间归入稳定的工作上下文分组。 */
export function workHistoryBucket(
  timestamp: number | null | undefined,
  now = Date.now(),
): HistoryBucket {
  const value = new Date(epochMilliseconds(timestamp ?? 0));
  if (!timestamp || Number.isNaN(value.getTime())) return 'earlier';
  const current = new Date(now);
  const today = new Date(current.getFullYear(), current.getMonth(), current.getDate());
  const day = current.getDay() || 7;
  const week = new Date(today);
  week.setDate(today.getDate() - day + 1);
  const month = new Date(current.getFullYear(), current.getMonth(), 1);
  if (value >= today) return 'today';
  if (value >= week) return 'week';
  if (value >= month) return 'month';
  return 'earlier';
}

/** 渲染单条办公历史行。 */
function renderHistoryRow(entry: WorkHistoryEntry, commands: WorkHistoryCommands): HTMLElement {
  const row = document.createElement('button');
  const icon = createIcon(
    entry.entity_type === 'work_item' || entry.entity_type === 'work_item_session'
      ? 'process-todo'
      : 'process-thinking',
    { size: 18, className: 'mw-work-history__icon' },
  );
  row.type = 'button';
  row.className = 'mw-work-history__row';
  row.dataset.kind = entry.entity_type;
  // 选中态：当前活动会话匹配时高亮（左侧强调条 + selected 底色），与通用助手一致。
  if (entry.session_id && entry.session_id === state.activeSessionId) {
    row.dataset.active = 'true';
  }
  if (entry.session_id) row.dataset.sessionId = entry.session_id;
  if (entry.work_item_id) row.dataset.itemId = entry.work_item_id;
  if (entry.read_only) row.dataset.readOnly = 'true';

  const badge = document.createElement('span');
  const linkedItem = entry.entity_type === 'work_item'
    ? workStore.get().items.find((item) => item.item_id === entry.work_item_id)
    : undefined;
  badge.className = 'mw-work-history__kind';
  badge.textContent = linkedItem?.processing_session_id
    ? 'AI'
    : KIND_LABELS[entry.entity_type] ?? entry.entity_type;
  if (linkedItem?.processing_session_id) {
    badge.dataset.linkedConversation = 'true';
    badge.title = '已关联 AI 协作';
  }
  badge.dataset.kind = entry.entity_type;

  const title = document.createElement('span');
  title.className = 'mw-work-history__title';
  title.textContent = entry.title || entry.session_id;

  row.append(icon, title, badge);
  row.addEventListener('click', () => {
    if (entry.work_item_id) commands.openItem?.(entry.work_item_id);
    else if (entry.session_id) commands.openSession?.(entry.session_id, entry.open_mode);
  });
  return row;
}

/**
 * 先隔离通用助手历史，再按搜索词过滤办公历史。
 */
export function filterWorkHistory(
  entries: WorkHistoryEntry[],
  query: string,
): WorkHistoryEntry[] {
  const q = query.trim().toLowerCase();
  return entries.filter((entry) =>
    entry.open_mode === 'work'
    && (!q || (entry.title || '').toLowerCase().includes(q)));
}

/**
 * 渲染 Work 历史列表到容器；调用方负责提供容器。
 */
export function renderWorkHistory(
  container: HTMLElement,
  query = productModeStore.get().views.work.historyFilter,
  commands: WorkHistoryCommands = {},
  scope: 'all' | 'items' = 'all',
): void {
  container.id = HISTORY_CONTAINER_ID;
  container.className = 'mw-work-history';
  const state = workStore.get();
  container.innerHTML = '';

  const search = document.createElement('input');
  const list = document.createElement('div');
  const actions = document.createElement('div');
  const create = document.createElement('button');
  actions.className = 'mw-work-history__actions';
  create.type = 'button';
  create.className = 'mw-work-history__create';
  create.title = '新建事项';
  create.setAttribute('aria-label', create.title);
  create.append(createIcon('icon-plus', { size: 18 }));
  create.addEventListener('click', () => commands.newItem?.(create));
  search.type = 'search';
  search.className = 'mw-work-history__search';
  search.placeholder = scope === 'items' ? '搜索事项' : '搜索事项和历史';
  search.setAttribute('aria-label', search.placeholder);
  search.value = query;
  list.className = 'mw-work-history__list';
  actions.append(create);
  container.append(actions, search, list);

  const renderRows = (value: string): void => {
    list.replaceChildren();
    const entries = filterWorkHistory(state.history, value).filter((entry) =>
      (!state.selectedWorkspaceId || entry.workspace_id === state.selectedWorkspaceId)
      && (scope === 'all' || entry.entity_type === 'work_item'));
    if (entries.length === 0) {
      const empty = document.createElement('p');
      empty.className = 'mw-work-history__empty';
      empty.textContent = value
        ? scope === 'items' ? '没有匹配的事项' : '没有匹配的历史'
        : scope === 'items' ? '暂无计划事项' : '暂无办公历史';
      list.append(empty);
      return;
    }
    const itemEntries = entries.filter((entry) => entry.entity_type === 'work_item');
    const conversationEntries = entries.filter((entry) =>
      entry.entity_type === 'work_session' || entry.entity_type === 'work_item_session');
    if (itemEntries.length > 0) {
      const itemSection = document.createElement('section');
      itemSection.className = 'mw-work-history__item-section';
      const groups = new Map<HistoryBucket, WorkHistoryEntry[]>();
      for (const entry of itemEntries) {
        const bucket = workHistoryBucket(entry.updated_at);
        groups.set(bucket, [...(groups.get(bucket) ?? []), entry]);
      }
      for (const group of HISTORY_BUCKETS) {
        const datedEntries = groups.get(group.id);
        if (!datedEntries?.length) continue;
        const details = document.createElement('details');
        const summary = document.createElement('summary');
        const label = document.createElement('span');
        const count = document.createElement('span');
        details.className = 'mw-work-history__group';
        details.open = group.id !== 'earlier' || Boolean(value);
        summary.className = 'mw-work-history__group-summary';
        label.textContent = group.label;
        count.className = 'mw-work-history__group-count';
        count.textContent = String(datedEntries.length);
        summary.append(label, count);
        details.append(summary);
        for (const entry of datedEntries) {
          details.append(renderHistoryRow(entry, commands));
        }
        itemSection.append(details);
      }
      list.append(itemSection);
    }
    if (conversationEntries.length > 0) {
      const conversationSection = document.createElement('section');
      const heading = document.createElement('h3');
      conversationSection.className = 'mw-work-history__conversation-section';
      heading.className = 'mw-work-history__section-title';
      heading.textContent = '对话';
      conversationSection.append(heading);
      for (const entry of conversationEntries) {
        conversationSection.append(renderHistoryRow(entry, commands));
      }
      list.append(conversationSection);
    }
  };
  renderRows(query);
  search.addEventListener('input', () => {
    updateProductModeView({ historyFilter: search.value });
    renderRows(search.value);
  });
}

/** 同步读取当前历史快照（供测试与外部订阅）。 */
export function getWorkHistorySnapshot(state: WorkStoreState): WorkHistoryEntry[] {
  return state.history;
}
