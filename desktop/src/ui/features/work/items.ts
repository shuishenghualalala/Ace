/**
 * WorkItem 列表与创建：日期分组、搜索、状态筛选、手工创建。
 * 三状态轴（business / execution / sync）+ disposition 可辨；低置信来源待确认。
 * 数据来自 work-store，写入经 workApi + mergeItem，不复制 fetch。
 */

import { workApi } from '../../backend-client';
import type { WorkItem } from '../../backend-client';
import { openDialog, type OverlayHandle } from '../../components/overlays';
import { workStore, loadWorkItems, mergeItem } from '../../stores/work-store';
import { createIcon } from '../../components/icon';
import { renderWorkReportPanel } from './dashboard';
import {
  applyItemAction,
  availableActions,
  ITEM_ACTION_LABELS,
  type ItemAction,
  renderItemSpace,
} from './item-space';
import { epochMilliseconds, epochSeconds } from './time';

export interface ItemFilter {
  query: string;
  /** 业务状态筛选；空串表示全部。 */
  businessStatus: string;
  category?: string;
  scope?: 'active' | 'completed' | 'archived' | 'all';
}

export const BUSINESS_STATUS_LABELS: Record<string, string> = {
  pending_confirmation: '待确认',
  pending: '待处理',
  in_progress: '进行中',
  completed: '已完成',
};

export const DISPOSITION_LABELS: Record<string, string> = {
  active: '活跃',
  cancelled: '已取消',
  archived: '已归档',
  tracking_stopped: '已停止跟踪',
};

/** 优先级 pill 文案；与设计稿 .detail-head 的 pill 一致（高/中/低）。 */
const PRIORITY_LABELS: Record<string, string> = {
  high: '高',
  medium: '中',
  low: '低',
};

export interface NewWorkItemInput {
  title: string;
  description?: string;
  dueAt?: number;
  priority?: 'high' | 'medium' | 'low';
  category?: string;
  relatedSystem?: string;
  workspaceId?: string;
}

type PlanView = 'calendar' | 'list' | 'board' | 'report';

const RELATED_SYSTEM_OPTIONS = [
  ['', '不关联'],
  ['portal', '门户 / 公文'],
  ['mail', '邮件系统'],
  ['calendar', '日程系统'],
  ['todo', '待办系统'],
  ['meeting', '会议系统'],
  ['hr', '人力系统'],
] as const;

/** 复用 store 快照，缺失时再读取单条事项。 */
export async function resolveWorkItem(itemId: string): Promise<WorkItem> {
  return workStore.get().items.find((item) => item.item_id === itemId)
    ?? workApi.getItem(itemId);
}

/** 按日期分组：今天 / 昨天 / 本周 / 更早。 */
export type DateGroupKey = 'today' | 'yesterday' | 'thisWeek' | 'earlier';

export const DATE_GROUP_LABELS: Record<DateGroupKey, string> = {
  today: '今天',
  yesterday: '昨天',
  thisWeek: '本周',
  earlier: '更早',
};

function toDateGroup(timestamp: number, now: number): DateGroupKey {
  if (!timestamp) return 'earlier';
  const day = 86_400_000;
  const startOfToday = new Date(now).setHours(0, 0, 0, 0);
  const delta = startOfToday - new Date(epochMilliseconds(timestamp)).setHours(0, 0, 0, 0);
  if (delta <= 0) return 'today';
  if (delta <= day) return 'yesterday';
  if (delta <= day * 6) return 'thisWeek';
  return 'earlier';
}

/** 按更新时间倒序分组。 */
export function groupItemsByDate(
  items: WorkItem[],
  now: number = Date.now(),
): Record<DateGroupKey, WorkItem[]> {
  const groups: Record<DateGroupKey, WorkItem[]> = {
    today: [],
    yesterday: [],
    thisWeek: [],
    earlier: [],
  };
  for (const item of items) {
    groups[toDateGroup(item.updated_at || item.created_at, now)].push(item);
  }
  return groups;
}

/** 搜索 + 业务状态过滤。 */
export function filterItems(items: WorkItem[], filter: ItemFilter): WorkItem[] {
  const q = filter.query.trim().toLowerCase();
  return items.filter((item) => {
    if (
      filter.scope === 'active'
      && (isInactive(item) || item.business_status === 'completed')
    ) return false;
    if (
      filter.scope === 'completed'
      && (item.business_status !== 'completed' || item.disposition === 'archived')
    ) return false;
    if (filter.scope === 'archived' && item.disposition !== 'archived') return false;
    if (filter.businessStatus && item.business_status !== filter.businessStatus) return false;
    if (filter.category && item.category !== filter.category) return false;
    if (q && !(item.title || '').toLowerCase().includes(q)) return false;
    return true;
  });
}

/** 是否为低置信来源、待人工确认的事项。 */
export function isPendingConfirmation(item: WorkItem): boolean {
  return item.business_status === 'pending_confirmation';
}

/** 是否已处置（取消 / 归档 / 停止跟踪），渲染时弱化。 */
export function isInactive(item: WorkItem): boolean {
  return item.disposition !== 'active' && item.disposition !== undefined;
}

export interface WorkItemContextOptions {
  /** 返回 Work 工作台；提供时在上下文条最左侧显示明确入口。 */
  onBackToWorkbench?: () => void;
  /** 点击「事项详情」打开右侧 Drawer 的回调。 */
  onOpenDetails: (trigger: HTMLButtonElement) => void;
  /** 事项动作（完成 / 重开 / 延期）成功后，用最新 item 重渲染上下文条。 */
  onUpdated?: (item: WorkItem) => void;
}

/**
 * 在共享 Conversation Surface 顶部渲染事项上下文条（设计稿的 detail-head）：
 * 返回工作台 + 标题 + meta（状态 · 优先级 pill · 截止）+ 稳定动作
 * （完成/重开 · 延期 · 事项详情）。
 *
 * 顶部动作保持稳定，不随对话正文变化移动（SPEC §7.6）；完成/重开/延期复用 item-space 的
 * availableActions / applyItemAction，「事项详情」打开 Drawer 承载其余动作（取消/归档/编辑/删除等）。
 * 布局约束见 docs/frontend/work-mode-panel.html 的 .detail-head。
 */
export function renderWorkItemContext(
  container: HTMLElement,
  item: WorkItem,
  options: WorkItemContextOptions,
): void {
  container.className = 'mw-work-item-context';
  container.dataset.itemId = item.item_id;

  const lead = document.createElement('div');
  lead.className = 'mw-work-item-context__lead';
  if (options.onBackToWorkbench) {
    const back = document.createElement('button');
    const label = document.createElement('span');
    back.type = 'button';
    back.className = 'mw-work-item-context__back';
    back.setAttribute('aria-label', '返回工作台');
    label.textContent = '工作台';
    back.append(createIcon('icon-back', { size: 18 }), label);
    back.addEventListener('click', options.onBackToWorkbench);
    lead.append(back);
  }

  // ── 左侧：标题 + meta ──
  const info = document.createElement('div');
  info.className = 'mw-work-item-context__info';
  const title = document.createElement('strong');
  title.className = 'mw-work-item-context__title';
  title.textContent = item.title;
  const meta = document.createElement('div');
  meta.className = 'mw-work-item-context__meta';
  const statusLabel =
    BUSINESS_STATUS_LABELS[item.business_status ?? ''] ?? item.business_status ?? '';
  if (statusLabel) {
    const status = document.createElement('span');
    status.className = 'mw-work-item-context__status';
    status.textContent = statusLabel;
    meta.append(status);
  }
  if (item.priority && PRIORITY_LABELS[item.priority]) {
    const pill = document.createElement('span');
    pill.className = `mw-work-item-context__pill mw-work-item-context__pill--${item.priority}`;
    pill.textContent = PRIORITY_LABELS[item.priority];
    meta.append(pill);
  }
  if (item.due_at) {
    const due = document.createElement('span');
    due.textContent = `截止 ${new Date(epochMilliseconds(item.due_at)).toLocaleDateString('zh-CN')}`;
    meta.append(due);
  }
  info.append(title, meta);
  lead.append(info);

  // ── 右侧：稳定动作 ──
  const actions = document.createElement('div');
  actions.className = 'mw-work-item-context__actions';
  const acts = availableActions(item);
  const primaryAction: ItemAction | null = acts.includes('complete')
    ? 'complete'
    : acts.includes('reopen')
      ? 'reopen'
      : null;

  // 动作执行：成功用 onUpdated 重渲染；失败在动作栏左侧插一条短反馈，不禁用整条。
  const runAction = (action: ItemAction, dueAt: number | undefined, btn: HTMLButtonElement): void => {
    btn.disabled = true;
    void applyItemAction(item, action, dueAt)
      .then((updated) => options.onUpdated?.(updated))
      .catch((error) => {
        btn.disabled = false;
        const feedback = document.createElement('span');
        feedback.className = 'mw-work-item-context__feedback';
        feedback.textContent = `操作失败：${error instanceof Error ? error.message : String(error)}`;
        actions.before(feedback);
      });
  };

  if (primaryAction) {
    const primary = document.createElement('button');
    primary.type = 'button';
    primary.className = 'mw-work-item-context__action mw-work-item-context__action--primary';
    primary.dataset.action = primaryAction;
    primary.textContent = ITEM_ACTION_LABELS[primaryAction];
    primary.addEventListener('click', () => runAction(primaryAction, undefined, primary));
    actions.append(primary);
  }

  if (acts.includes('postpone')) {
    const postpone = document.createElement('button');
    postpone.type = 'button';
    postpone.className = 'mw-work-item-context__action';
    postpone.dataset.action = 'postpone';
    postpone.textContent = '延期';
    postpone.addEventListener('click', () =>
      openPostponeDialog(item, postpone, (dueAt) => runAction('postpone', dueAt, postpone)));
    actions.append(postpone);
  }

  const details = document.createElement('button');
  details.type = 'button';
  details.className = 'mw-work-item-context__action mw-work-item-context__details';
  details.textContent = '事项详情';
  details.addEventListener('click', () => options.onOpenDetails(details));
  actions.append(details);

  container.replaceChildren(lead, actions);
  container.hidden = false;
}

/**
 * 延期对话框：选新的截止时间后回传 Unix 秒。复用 openDialog，不另开执行旁路。
 * 默认填当前截止时间，便于在其基础上顺延。
 */
function openPostponeDialog(
  item: WorkItem,
  trigger: HTMLElement,
  onConfirm: (dueAt: number) => void,
): OverlayHandle {
  const form = document.createElement('form');
  form.className = 'mw-work-items-page__create mw-work-items-page__create--compact';
  const input = document.createElement('input');
  input.type = 'datetime-local';
  input.required = true;
  input.setAttribute('aria-label', '新的截止时间');
  if (item.due_at) {
    const local = new Date(epochMilliseconds(item.due_at) - new Date().getTimezoneOffset() * 60_000);
    input.value = local.toISOString().slice(0, 16);
  }
  const feedback = document.createElement('p');
  feedback.className = 'mw-work-items-page__feedback';
  feedback.setAttribute('aria-live', 'polite');
  const actionsEl = document.createElement('div');
  actionsEl.className = 'mw-work-items-page__create-actions';
  const cancel = document.createElement('button');
  const confirm = document.createElement('button');
  cancel.type = 'button';
  cancel.textContent = '取消';
  cancel.className = 'mw-work-items-page__create-cancel';
  confirm.type = 'button';
  confirm.textContent = '确认延期';
  confirm.className = 'mw-work-items-page__create-submit';
  actionsEl.append(cancel, confirm);
  form.append(input, feedback, actionsEl);
  const dialog = openDialog({ trigger, title: '延期事项', content: form });
  cancel.addEventListener('click', () => dialog.close());
  confirm.addEventListener('click', () => {
    const dueAt = epochSeconds(new Date(input.value).getTime());
    if (!Number.isFinite(dueAt) || !input.value) {
      feedback.dataset.state = 'error';
      feedback.textContent = '请选择新的截止时间';
      return;
    }
    onConfirm(dueAt);
    dialog.close();
  });
  return dialog;
}

function formatDueDate(item: WorkItem): { label: string; dateTime: string; overdue: boolean } {
  if (!item.due_at) return { label: '未排期', dateTime: '', overdue: false };
  const date = new Date(epochMilliseconds(item.due_at));
  if (Number.isNaN(date.getTime())) return { label: '时间未知', dateTime: '', overdue: false };
  return {
    label: date.toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' }),
    dateTime: date.toISOString(),
    overdue: date.getTime() < Date.now() && item.business_status !== 'completed',
  };
}

function renderItemCard(
  item: WorkItem,
  onOpen?: (item: WorkItem) => void,
  showStatus = true,
): HTMLElement {
  const card = document.createElement('button');
  card.type = 'button';
  card.className = 'mw-work-items__card';
  card.dataset.itemId = item.item_id;
  if (isPendingConfirmation(item)) card.dataset.pendingConfirmation = 'true';
  if (isInactive(item)) card.dataset.inactive = 'true';

  const marker = document.createElement('span');
  marker.className = 'mw-work-items__marker';
  marker.setAttribute('aria-hidden', 'true');

  const copy = document.createElement('span');
  copy.className = 'mw-work-items__copy';
  const title = document.createElement('strong');
  title.className = 'mw-work-items__title';
  title.textContent = item.title;
  const description = document.createElement('span');
  description.className = 'mw-work-items__description';
  description.textContent = item.description || '暂无补充说明';
  copy.append(title, description);

  const badge = document.createElement('span');
  badge.className = 'mw-work-items__status';
  badge.textContent = BUSINESS_STATUS_LABELS[item.business_status ?? ''] ?? item.business_status ?? '';

  const meta = document.createElement('span');
  meta.className = 'mw-work-items__meta';
  const source = document.createElement('span');
  source.className = 'mw-work-items__source';
  source.textContent = item.category || item.related_system || '未分类';
  const due = document.createElement('time');
  const dueValue = formatDueDate(item);
  due.className = 'mw-work-items__due';
  due.textContent = dueValue.overdue ? `已逾期 · ${dueValue.label}` : dueValue.label;
  if (dueValue.dateTime) due.dateTime = dueValue.dateTime;
  if (dueValue.overdue) due.dataset.overdue = 'true';
  const priority = document.createElement('span');
  priority.className = 'mw-work-items__priority';
  priority.textContent = `${PRIORITY_LABELS[item.priority ?? ''] ?? '未设'}优先级`;
  meta.append(source, due, priority);
  if (showStatus) meta.append(badge);
  card.append(marker, copy, meta);
  if (onOpen) card.addEventListener('click', () => onOpen(item));
  return card;
}

/** 打开紧凑的新建事项模态，创建成功后回传完整事项。 */
export function openCreateWorkItemDialog(
  trigger: HTMLElement,
  onCreated: (item: WorkItem) => void | Promise<void>,
): OverlayHandle {
  const form = document.createElement('form');
  const title = document.createElement('input');
  const description = document.createElement('textarea');
  const due = document.createElement('input');
  const priority = document.createElement('select');
  const category = document.createElement('input');
  const relatedSystem = document.createElement('select');
  const submit = document.createElement('button');
  const cancel = document.createElement('button');
  const feedback = document.createElement('p');
  const dialogRef: { current?: OverlayHandle } = {};
  form.className = 'mw-work-items-page__create';
  title.required = true;
  title.placeholder = '例如：完成季度经营复盘';
  title.setAttribute('aria-label', '事项标题');
  description.placeholder = '说明目标、交付要求或需要助手关注的背景';
  description.setAttribute('aria-label', '事项描述');
  due.type = 'date';
  due.required = true;
  const tomorrow = new Date();
  tomorrow.setDate(tomorrow.getDate() + 1);
  due.value = tomorrow.toISOString().slice(0, 10);
  due.setAttribute('aria-label', '截止日期');
  priority.required = true;
  priority.setAttribute('aria-label', '优先级');
  for (const [value, label] of [
    ['high', '高优先级'],
    ['medium', '中优先级'],
    ['low', '低优先级'],
  ]) {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = label;
    priority.append(option);
  }
  priority.value = 'medium';
  category.placeholder = '例如：经营分析、会议、项目交付';
  category.setAttribute('aria-label', '事项分类');
  const categoryList = document.createElement('datalist');
  categoryList.id = 'mw-work-category-options';
  for (const value of [...new Set(
    workStore.get().items.map((item) => item.category).filter(Boolean) as string[],
  )].sort()) {
    const option = document.createElement('option');
    option.value = value;
    categoryList.append(option);
  }
  category.setAttribute('list', categoryList.id);
  relatedSystem.setAttribute('aria-label', '关联系统');
  for (const [value, label] of RELATED_SYSTEM_OPTIONS) {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = label;
    relatedSystem.append(option);
  }
  submit.type = 'submit';
  submit.textContent = '创建';
  submit.className = 'mw-work-items-page__create-submit';
  cancel.type = 'button';
  cancel.textContent = '取消';
  cancel.className = 'mw-work-items-page__create-cancel';
  cancel.addEventListener('click', () => dialogRef.current?.close());
  feedback.className = 'mw-work-items-page__feedback';
  feedback.setAttribute('aria-live', 'polite');
  const actions = document.createElement('div');
  actions.className = 'mw-work-items-page__create-actions';
  actions.append(cancel, submit);
  const field = (label: string, control: HTMLElement, required = false): HTMLLabelElement => {
    const wrapper = document.createElement('label');
    const caption = document.createElement('span');
    wrapper.className = 'mw-work-items-page__create-field';
    caption.className = 'mw-work-items-page__create-label';
    caption.textContent = `${label}${required ? ' *' : ''}`;
    wrapper.append(caption, control);
    return wrapper;
  };
  form.append(
    field('事项标题', title, true),
    field('截止日期', due, true),
    field('事项描述', description),
    field('优先级', priority, true),
    field('事项分类', category),
    field('关联系统', relatedSystem),
    categoryList,
    feedback,
    actions,
  );
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const value = title.value.trim();
    if (!value) return;
    submit.disabled = true;
    feedback.textContent = '正在创建…';
    const dueAt = due.value
      ? Math.floor(new Date(`${due.value}T23:59:00`).getTime() / 1000)
      : undefined;
    void createWorkItem({
      title: value,
      ...(description.value.trim() ? { description: description.value.trim() } : {}),
      ...(dueAt ? { dueAt } : {}),
      ...(priority.value
        ? { priority: priority.value as NonNullable<NewWorkItemInput['priority']> }
        : {}),
      ...(category.value.trim() ? { category: category.value.trim() } : {}),
      ...(relatedSystem.value ? { relatedSystem: relatedSystem.value } : {}),
    }).then((created) => {
      dialogRef.current?.close();
      return onCreated(created);
    }).catch((error) => {
      submit.disabled = false;
      feedback.dataset.state = 'error';
      feedback.textContent = `创建失败：${error instanceof Error ? error.message : String(error)}`;
    });
  });
  const dialog = openDialog({
    trigger,
    title: '新建事项',
    content: form,
  });
  dialogRef.current = dialog;
  return dialog;
}

/**
 * 渲染 WorkItem 列表到容器：按日期分组、搜索与状态筛选、空 / 长列表状态完整。
 */
export function renderItemsList(
  container: HTMLElement,
  filter: ItemFilter,
  onOpen?: (item: WorkItem) => void,
): void {
  container.className = 'mw-work-items';
  container.innerHTML = '';
  const all = workStore.get().items;
  const filtered = filterItems(all, filter);

  if (filtered.length === 0) {
    const empty = document.createElement('p');
    empty.className = 'mw-work-items__empty';
    empty.textContent = all.length === 0 ? '暂无事项，创建一个开始吧' : '没有匹配的事项';
    container.append(empty);
    return;
  }

  const groups = groupItemsByDate(filtered);
  const order: DateGroupKey[] = ['today', 'yesterday', 'thisWeek', 'earlier'];
  for (const key of order) {
    const items = groups[key];
    if (items.length === 0) continue;
    const header = document.createElement('div');
    header.className = 'mw-work-items__group-header';
    header.textContent = DATE_GROUP_LABELS[key];
    container.append(header);
    for (const item of items) container.append(renderItemCard(item, onOpen));
  }
}

/** 归档事项专用视图：保留日期、处置结果和详情活动入口。 */
export function renderArchivedItemsView(
  container: HTMLElement,
  filter: ItemFilter,
  onOpen: (item: WorkItem) => void,
): void {
  const items = filterItems(workStore.get().items, { ...filter, scope: 'archived' })
    .sort((left, right) => right.updated_at - left.updated_at);
  container.className = 'mw-work-archive';
  container.replaceChildren();
  if (!items.length) {
    const empty = document.createElement('p');
    empty.className = 'mw-work-items__empty';
    empty.textContent = '暂无归档事项';
    container.append(empty);
    return;
  }

  const summary = document.createElement('section');
  summary.className = 'mw-work-archive__summary';
  const metrics: Array<[string, number]> = [
    ['归档事项', items.length],
    ['完成后归档', items.filter((item) => item.business_status === 'completed').length],
    ['AI 协作', items.filter((item) => item.processing_session_id).length],
  ];
  for (const [label, value] of metrics) {
    const metric = document.createElement('div');
    const number = document.createElement('strong');
    const caption = document.createElement('span');
    number.textContent = String(value);
    caption.textContent = label;
    metric.append(number, caption);
    summary.append(metric);
  }

  const list = document.createElement('div');
  list.className = 'mw-work-archive__list';
  for (const item of items) {
    const row = document.createElement('button');
    const date = document.createElement('time');
    const copy = document.createElement('span');
    const title = document.createElement('strong');
    const result = document.createElement('span');
    const activity = document.createElement('span');
    const archivedAt = new Date(epochMilliseconds(item.updated_at));
    row.type = 'button';
    row.className = 'mw-work-archive__row';
    row.dataset.itemId = item.item_id;
    row.setAttribute('aria-label', `查看归档事项：${item.title}`);
    date.className = 'mw-work-archive__date';
    date.textContent = Number.isNaN(archivedAt.getTime())
      ? '时间未知'
      : archivedAt.toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' });
    copy.className = 'mw-work-archive__copy';
    title.textContent = item.title;
    result.className = 'mw-work-archive__result';
    result.textContent = [
      item.business_status === 'completed' ? '已完成' : '已归档',
      item.category,
      item.description,
    ].filter(Boolean).join(' · ');
    activity.className = 'mw-work-archive__activity';
    activity.textContent = '查看活动';
    copy.append(title, result);
    row.append(date, copy, activity);
    row.addEventListener('click', () => onOpen(item));
    list.append(row);
  }
  container.append(summary, list);
}

function renderCalendarView(
  container: HTMLElement,
  filter: ItemFilter,
  onOpen: (item: WorkItem) => void,
): void {
  const items = filterItems(workStore.get().items, filter);
  container.className = 'mw-work-plan-calendar';
  container.replaceChildren();
  if (items.length === 0) {
    const empty = document.createElement('p');
    empty.className = 'mw-work-items__empty';
    empty.textContent = workStore.get().items.length === 0
      ? '当前范围没有事项'
      : '没有匹配的事项';
    container.append(empty);
    return;
  }
  const groups = new Map<string, WorkItem[]>();
  for (const item of items) {
    const date = item.due_at ? new Date(epochMilliseconds(item.due_at)) : null;
    const key = date && !Number.isNaN(date.getTime())
      ? [
          date.getFullYear(),
          String(date.getMonth() + 1).padStart(2, '0'),
          String(date.getDate()).padStart(2, '0'),
        ].join('-')
      : '未排期';
    groups.set(key, [...(groups.get(key) ?? []), item]);
  }
  const sorted = [...groups.entries()].sort(([left], [right]) => {
    if (left === '未排期') return 1;
    if (right === '未排期') return -1;
    return left.localeCompare(right);
  });
  const summary = document.createElement('header');
  const summaryCopy = document.createElement('div');
  const summaryTitle = document.createElement('strong');
  const summaryMeta = document.createElement('span');
  const summaryCounts = document.createElement('div');
  const overdue = items.filter((item) => formatDueDate(item).overdue).length;
  const unscheduled = items.filter((item) => !item.due_at).length;
  summary.className = 'mw-work-plan-calendar__summary';
  summaryCopy.className = 'mw-work-plan-calendar__summary-copy';
  summaryTitle.textContent = '工作议程';
  summaryMeta.textContent = `${sorted.length} 个日期 · ${items.length} 个事项`;
  summaryCopy.append(summaryTitle, summaryMeta);
  for (const [label, value] of [['已逾期', overdue], ['未排期', unscheduled]] as const) {
    const count = document.createElement('span');
    count.textContent = `${label} ${value}`;
    summaryCounts.append(count);
  }
  summaryCounts.className = 'mw-work-plan-calendar__counts';
  summary.append(summaryCopy, summaryCounts);
  container.append(summary);
  for (const [dateKey, datedItems] of sorted) {
    const day = document.createElement('section');
    const date = document.createElement('div');
    const dayLabel = document.createElement('strong');
    const weekday = document.createElement('span');
    const agenda = document.createElement('div');
    const parsed = dateKey === '未排期' ? null : new Date(`${dateKey}T00:00:00`);
    day.className = 'mw-work-plan-calendar__day';
    date.className = 'mw-work-plan-calendar__date';
    dayLabel.textContent = parsed
      ? parsed.toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' })
      : '未排期';
    weekday.textContent = parsed
      ? parsed.toLocaleDateString('zh-CN', { weekday: 'short' })
      : '稍后安排';
    agenda.className = 'mw-work-plan-calendar__agenda';
    date.append(dayLabel, weekday);
    for (const item of datedItems) agenda.append(renderItemCard(item, onOpen));
    day.append(date, agenda);
    container.append(day);
  }
}

function renderBoardView(
  container: HTMLElement,
  filter: ItemFilter,
  onOpen: (item: WorkItem) => void,
  onChanged: () => void,
  onError: (message: string) => void,
): void {
  const items = filterItems(workStore.get().items, filter);
  const columns = [
    ['pending_confirmation', '待确认'],
    ['pending', '待处理'],
    ['in_progress', '进行中'],
    ['completed', '已完成'],
  ] as const;
  container.className = 'mw-work-plan-board';
  container.replaceChildren();
  for (const [key, label] of columns) {
    const column = document.createElement('section');
    const heading = document.createElement('h3');
    const count = document.createElement('span');
    const columnItems = items.filter((item) =>
      item.disposition !== 'archived' && item.business_status === key);
    column.className = 'mw-work-plan-board__column';
    column.dataset.status = key;
    heading.className = 'mw-work-plan-board__heading';
    heading.textContent = label;
    count.textContent = String(columnItems.length);
    heading.append(count);
    column.append(heading);
    column.addEventListener('dragover', (event) => {
      if (key === 'pending_confirmation') return;
      event.preventDefault();
      column.dataset.dragOver = 'true';
    });
    column.addEventListener('dragleave', () => delete column.dataset.dragOver);
    column.addEventListener('drop', (event) => {
      event.preventDefault();
      delete column.dataset.dragOver;
      const itemId = event.dataTransfer?.getData('text/work-item-id') ?? '';
      const item = items.find((candidate) => candidate.item_id === itemId);
      if (!item || key === 'pending_confirmation') return;
      void moveWorkItem(item, key).then(onChanged).catch((error) => {
        onError(`状态更新失败：${error instanceof Error ? error.message : String(error)}`);
      });
    });
    if (columnItems.length === 0) {
      const empty = document.createElement('p');
      empty.className = 'mw-work-plan-board__empty';
      empty.textContent = '暂无';
      column.append(empty);
    } else {
      for (const item of columnItems) {
        const wrapper = document.createElement('article');
        const card = renderItemCard(item, onOpen, false);
        wrapper.className = 'mw-work-plan-board__item';
        wrapper.draggable = key !== 'pending_confirmation';
        wrapper.addEventListener('dragstart', (event) => {
          if (event.dataTransfer) {
            event.dataTransfer.setData('text/work-item-id', item.item_id);
            event.dataTransfer.effectAllowed = 'move';
          }
          wrapper.dataset.dragging = 'true';
        });
        wrapper.addEventListener('dragend', () => delete wrapper.dataset.dragging);
        wrapper.append(card);
        const select = document.createElement('select');
        select.className = 'mw-work-plan-board__status-select';
        select.setAttribute('aria-label', `更改“${item.title}”状态`);
        const statusOptions = [
          ['pending', '待处理'],
          ['in_progress', '进行中'],
          ['completed', '已完成'],
        ];
        if (key === 'pending_confirmation') {
          statusOptions.unshift(['pending_confirmation', '待确认']);
        }
        for (const [value, text] of statusOptions) {
          const option = document.createElement('option');
          option.value = value;
          option.textContent = text;
          option.disabled = value === 'pending_confirmation';
          select.append(option);
        }
        select.value = item.business_status ?? 'pending';
        select.disabled = key === 'pending_confirmation';
        select.addEventListener('change', () => {
          select.disabled = true;
          void moveWorkItem(item, select.value)
            .then(onChanged)
            .catch((error) => {
              select.disabled = false;
              onError(`状态更新失败：${error instanceof Error ? error.message : String(error)}`);
            });
        });
        wrapper.append(select);
        column.append(wrapper);
      }
    }
    container.append(column);
  }
}

/** 通过看板拖拽或键盘状态选择更新事项。 */
export async function moveWorkItem(item: WorkItem, target: string): Promise<WorkItem> {
  const updated = target === 'archived'
    ? await workApi.actOnItem(item.item_id, {
      action: 'archive',
      expected_version: item.version,
    })
    : await workApi.updateItem(item.item_id, {
      expected_version: item.version,
      business_status: target,
    });
  mergeItem(updated);
  return updated;
}

/** 渲染可操作的事项页：搜索、筛选、刷新、新建和详情。 */
export function renderItemsPage(
  container: HTMLElement,
  initialItemId?: string,
  onOpenSession?: (sessionId: string) => void,
  onBackToWorkbench?: () => void,
): void {
  container.className = 'mw-work-items-page';
  container.replaceChildren();
  const toolbar = document.createElement('div');
  const search = document.createElement('input');
  const scope = document.createElement('select');
  const status = document.createElement('select');
  const category = document.createElement('select');
  const refresh = document.createElement('button');
  const create = document.createElement('button');
  const viewSwitch = document.createElement('div');
  const feedback = document.createElement('p');
  const list = document.createElement('div');
  const filter: ItemFilter = { query: '', businessStatus: '', category: '', scope: 'active' };
  let view: PlanView = 'calendar';
  toolbar.className = 'mw-work-items-page__toolbar';
  search.type = 'search';
  search.className = 'mw-work-items-page__search';
  search.placeholder = '搜索事项';
  search.setAttribute('aria-label', '搜索事项');
  scope.className = 'mw-work-items-page__scope';
  scope.setAttribute('aria-label', '事项范围');
  for (const [value, label] of [
    ['active', '待办事项'],
    ['completed', '已完成'],
    ['archived', '已归档'],
    ['all', '全部事项'],
  ]) {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = label;
    scope.append(option);
  }
  status.className = 'mw-work-items-page__filter';
  status.setAttribute('aria-label', '按状态筛选');
  for (const [value, label] of [['', '全部状态'], ...Object.entries(BUSINESS_STATUS_LABELS)]) {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = label;
    status.append(option);
  }
  category.className = 'mw-work-items-page__category-filter';
  category.setAttribute('aria-label', '按分类筛选');
  for (const value of ['', ...new Set(
    workStore.get().items.map((item) => item.category).filter(Boolean) as string[],
  )]) {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = value || '全部分类';
    category.append(option);
  }
  refresh.type = 'button';
  refresh.className = 'mw-work-items-page__action';
  refresh.textContent = '刷新';
  create.type = 'button';
  create.className = 'mw-work-items-page__action mw-work-items-page__action--primary';
  create.textContent = '新建事项';
  viewSwitch.className = 'mw-work-items-page__views';
  viewSwitch.setAttribute('aria-label', '计划视图');
  for (const [value, label] of [
    ['calendar', '日历'],
    ['list', '列表'],
    ['board', '看板'],
    ['report', '统计'],
  ] as const) {
    const button = document.createElement('button');
    button.type = 'button';
    button.dataset.itemsView = value;
    button.textContent = label;
    button.setAttribute('aria-pressed', String(value === view));
    button.addEventListener('click', () => {
      view = value;
      if (value === 'board') {
        scope.value = 'all';
        filter.scope = 'all';
      }
      for (const candidate of viewSwitch.querySelectorAll<HTMLButtonElement>('button')) {
        candidate.setAttribute('aria-pressed', String(candidate === button));
      }
      drawList();
    });
    viewSwitch.append(button);
  }
  feedback.className = 'mw-work-items-page__feedback';
  feedback.setAttribute('aria-live', 'polite');
  list.className = 'mw-work-items-page__list';
  toolbar.append(search, scope, status, category, refresh, create);
  container.append(viewSwitch, toolbar, feedback, list);

  const drawList = (): void => {
    delete list.dataset.itemId;
    if (filter.scope === 'archived') renderArchivedItemsView(list, filter, openItem);
    else if (view === 'calendar') renderCalendarView(list, filter, openItem);
    else if (view === 'board') renderBoardView(list, filter, openItem, drawList, showError);
    else if (view === 'report') {
      list.className = 'mw-work-plan-report';
      list.replaceChildren();
      renderWorkReportPanel(list);
    }
    else renderItemsList(list, filter, openItem);
  };
  const showError = (message: string): void => {
    feedback.dataset.state = 'error';
    feedback.textContent = message;
  };
  const load = async (): Promise<void> => {
    refresh.disabled = true;
    feedback.removeAttribute('data-state');
    feedback.textContent = '正在加载事项…';
    workStore.set({ error: null });
    await refreshWorkItems();
    refresh.disabled = false;
    const error = workStore.get().error;
    if (error) showError(`加载失败：${error}`);
    else feedback.textContent = '';
    drawList();
  };
  async function openItem(itemOrId: WorkItem | string): Promise<void> {
    try {
      const item = typeof itemOrId === 'string'
        ? await resolveWorkItem(itemOrId)
        : itemOrId;
      const pageHeading = container.closest('.mw-work-page')?.querySelector<HTMLElement>(
        '.mw-work-page__heading',
      );
      if (pageHeading) pageHeading.hidden = true;
      viewSwitch.hidden = true;
      toolbar.hidden = true;
      feedback.textContent = '';
      renderItemSpace(list, item, {
        onBack: () => {
          if (initialItemId && onBackToWorkbench) {
            onBackToWorkbench();
            return;
          }
          if (pageHeading) pageHeading.hidden = false;
          viewSwitch.hidden = false;
          toolbar.hidden = false;
          feedback.textContent = '';
          drawList();
        },
        ...(initialItemId ? { backLabel: '返回工作台' } : {}),
        onDeleted: () => {
          if (pageHeading) pageHeading.hidden = false;
          viewSwitch.hidden = false;
          toolbar.hidden = false;
          feedback.textContent = '事项已删除';
          drawList();
        },
        ...(onOpenSession ? { onOpenSession } : {}),
      });
    } catch (error) {
      showError(`事项加载失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }
  search.addEventListener('input', () => {
    filter.query = search.value;
    drawList();
  });
  status.addEventListener('change', () => {
    filter.businessStatus = status.value;
    drawList();
  });
  category.addEventListener('change', () => {
    filter.category = category.value;
    drawList();
  });
  scope.addEventListener('change', () => {
    filter.scope = scope.value as NonNullable<ItemFilter['scope']>;
    drawList();
  });
  refresh.addEventListener('click', () => void load());
  create.addEventListener('click', () => {
    openCreateWorkItemDialog(create, (created) => {
      feedback.textContent = '事项已创建';
      return openItem(created);
    });
  });
  drawList();
  if (initialItemId) void openItem(initialItemId);
  else void load();
}

/**
 * 手工创建事项：经 workApi.createItem 并合并到 store。
 * 低置信来源事项由后端标记 pending_confirmation，列表渲染会提示确认。
 */
export async function createWorkItem(
  input: string | NewWorkItemInput,
  workspaceId = workStore.get().selectedWorkspaceId ?? 'default',
): Promise<WorkItem> {
  const values: NewWorkItemInput = typeof input === 'string'
    ? { title: input, workspaceId }
    : input;
  const created = await workApi.createItem({
    title: values.title,
    workspace_id: values.workspaceId ?? workspaceId,
    ...(values.description ? { description: values.description } : {}),
    ...(values.dueAt ? { due_at: values.dueAt } : {}),
    ...(values.priority ? { priority: values.priority } : {}),
    ...(values.category ? { category: values.category } : {}),
    ...(values.relatedSystem ? { related_system: values.relatedSystem } : {}),
  });
  mergeItem(created);
  return created;
}

/** 重新拉取事项列表（导航进入或刷新时）。 */
export async function refreshWorkItems(): Promise<void> {
  await loadWorkItems();
}
