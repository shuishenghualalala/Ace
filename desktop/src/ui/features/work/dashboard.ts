/**
 * 每日工作台：简报、关注事项、逾期、待确认、执行状态与实时更新。
 * 数据来自 workApi.getDashboard（{brief: WorkDailyBrief | null}）。
 * 刷新经 workApi.refreshDashboard；实时更新不破坏 UI 状态。
 */

import { workApi } from '../../backend-client';
import type {
  WorkDashboard,
  WorkItem,
  WorkPeriodReport,
  WorkSourceState,
} from '../../backend-client';
import { workStore, loadWorkDashboard } from '../../stores/work-store';
import { instantiateTemplate, mostUsed } from './templates';
import { epochMilliseconds, relativeTime } from './time';
import { renderOfficeListSection } from './office-panels';
import { state } from '../../state';
import { createIcon } from '../../components/icon';

export { relativeTime } from './time';

/** 从 brief.content 中安全取数组字段。 */
function contentArray(brief: WorkDashboard['brief'], key: string): WorkItem[] {
  if (!brief) return [];
  const value = brief.content[key];
  return Array.isArray(value) ? (value as WorkItem[]) : [];
}

function sourceStates(brief: WorkDashboard['brief']): WorkSourceState[] {
  if (!brief) return [];
  const value = brief.content.source_states;
  return Array.isArray(value) ? value as WorkSourceState[] : [];
}

/** 从 brief.content 中安全取字符串字段。 */
function contentString(brief: WorkDashboard['brief'], key: string): string {
  if (!brief) return '';
  const value = brief.content[key];
  return typeof value === 'string' ? value : '';
}

export interface DashboardView {
  summary: string;
  todayItems: WorkItem[];
  focusItems: WorkItem[];
  overdueItems: WorkItem[];
  meetingItems: WorkItem[];
  mailItems: WorkItem[];
  pendingConfirmations: WorkItem[];
  executionItems: WorkItem[];
  sourceStates: WorkSourceState[];
  updatedAt: number;
  archived?: boolean;
}

export interface DashboardCommands {
  openItem?(itemId: string): void | Promise<void>;
  openItems?(): void;
  newItem?(trigger: HTMLElement): void;
}

/** 将后端 brief 映射为视图模型。 */
export function parseDashboard(dashboard: WorkDashboard): DashboardView {
  const brief = dashboard.brief;
  return {
    summary: contentString(brief, 'summary'),
    todayItems: contentArray(brief, 'today_items'),
    focusItems: contentArray(brief, 'focus_items'),
    overdueItems: contentArray(brief, 'overdue_items'),
    meetingItems: contentArray(brief, 'meeting_items'),
    mailItems: contentArray(brief, 'mail_items'),
    pendingConfirmations: contentArray(brief, 'pending_confirmations'),
    executionItems: contentArray(brief, 'execution_items'),
    sourceStates: sourceStates(brief),
    updatedAt: brief?.updated_at ?? 0,
    archived: brief?.archived ?? false,
  };
}

/** 主动刷新工作台（POST /dashboard/refresh）。 */
export async function refreshDashboard(): Promise<void> {
  await workApi.refreshDashboard(workStore.get().selectedWorkspaceId);
  await loadWorkDashboard();
}

/** 归档当前 Workspace 的今日简报。 */
export async function archiveDashboard(): Promise<void> {
  const dashboard = await workApi.archiveDashboard(workStore.get().selectedWorkspaceId);
  workStore.set({ dashboard });
}

function localDate(): string {
  const now = new Date();
  const offset = now.getTimezoneOffset() * 60_000;
  return new Date(now.getTime() - offset).toISOString().slice(0, 10);
}

export function renderWorkReportPanel(container: HTMLElement): void {
  const section = document.createElement('section');
  const header = document.createElement('div');
  const heading = document.createElement('h3');
  const controls = document.createElement('div');
  const body = document.createElement('div');
  let selected: WorkPeriodReport['period'] = 'day';
  let current: WorkPeriodReport | null = null;
  section.className = 'mw-work-report';
  header.className = 'mw-work-report__header';
  heading.className = 'mw-work-dashboard__heading';
  heading.textContent = '工作量';
  controls.className = 'mw-work-report__periods';
  controls.setAttribute('aria-label', '工作量统计周期');
  body.className = 'mw-work-report__body';
  body.setAttribute('aria-live', 'polite');

  const draw = (): void => {
    body.replaceChildren();
    if (!current) {
      body.textContent = '正在加载统计…';
      return;
    }
    const range = document.createElement('p');
    const metrics = document.createElement('div');
    const archive = document.createElement('button');
    range.className = 'mw-work-report__range';
    range.textContent = `${current.period_start} 至 ${current.period_end}`;
    metrics.className = 'mw-work-report__metrics';
    const summaryMetrics: Array<[string, string | number]> = [
      ['新建', current.metrics.created],
      ['完成', current.metrics.completed],
      ['进行中', current.metrics.in_progress],
      ['逾期', current.metrics.overdue],
      ['完成率', `${Math.round(current.metrics.completion_rate * 100)}%`],
    ];
    for (const [label, value] of summaryMetrics) {
      const metric = document.createElement('div');
      const number = document.createElement('strong');
      const copy = document.createElement('span');
      number.textContent = String(value);
      copy.textContent = label;
      metric.append(number, copy);
      metrics.append(metric);
    }
    body.append(range, metrics);
    const categories = Object.entries(current.metrics.category_counts);
    if (categories.length > 0) {
      const list = document.createElement('div');
      list.className = 'mw-work-report__categories';
      for (const [category, count] of categories) {
        const item = document.createElement('span');
        item.textContent = `${category} ${count}`;
        list.append(item);
      }
      body.append(list);
    }
    archive.type = 'button';
    archive.className = 'mw-work-report__archive';
    archive.textContent = current.archived ? '已归档' : '归档当前周期';
    archive.disabled = current.archived;
    archive.addEventListener('click', () => {
      archive.disabled = true;
      archive.textContent = '正在归档…';
      void workApi.archiveReport(
        selected,
        localDate(),
        workStore.get().selectedWorkspaceId,
      ).then(({ report }) => {
        current = report;
        draw();
      }).catch((error) => {
        archive.disabled = false;
        archive.textContent = '归档当前周期';
        archive.title = error instanceof Error ? error.message : String(error);
      });
    });
    body.append(archive);
  };
  const load = (period: WorkPeriodReport['period']): void => {
    selected = period;
    current = null;
    draw();
    void workApi.getReport(period, localDate(), workStore.get().selectedWorkspaceId)
      .then(({ report }) => {
        current = report;
        draw();
      })
      .catch((error) => {
        body.textContent = `统计加载失败：${error instanceof Error ? error.message : String(error)}`;
      });
  };
  for (const [period, label] of [
    ['day', '日'],
    ['week', '周'],
    ['month', '月'],
  ] as const) {
    const button = document.createElement('button');
    button.type = 'button';
    button.dataset.reportPeriod = period;
    button.textContent = label;
    button.setAttribute('aria-pressed', String(period === selected));
    button.addEventListener('click', () => {
      controls.querySelectorAll('button').forEach((candidate) => {
        candidate.setAttribute('aria-pressed', String(candidate === button));
      });
      load(period);
    });
    controls.append(button);
  }
  header.append(heading, controls);
  section.append(header, body);
  container.append(section);
  load(selected);
}

/** 渲染工作台到容器。 */
export function renderDashboard(
  container: HTMLElement,
  view: DashboardView,
  notice = '',
  commands: DashboardCommands = {},
): void {
  container.className = 'mw-work-dashboard';
  container.innerHTML = '';

  const intro = document.createElement('header');
  const greeting = document.createElement('h2');
  const hour = new Date().getHours();
  intro.className = 'mw-work-dashboard__intro';
  const period = hour < 6 || hour >= 18
    ? '晚上好'
    : hour < 9
      ? '早上好'
      : hour < 12
        ? '上午好'
        : '下午好';
  const staffName = state.userInfo?.staffName?.trim();
  greeting.textContent = staffName ? `${period}，${staffName}` : period;
  intro.append(greeting);

  const toolbar = document.createElement('div');
  const feedback = document.createElement('p');
  const refresh = document.createElement('button');
  const archive = document.createElement('button');
  const manageItems = document.createElement('button');
  toolbar.className = 'mw-work-dashboard__toolbar';
  feedback.className = 'mw-work-dashboard__feedback';
  feedback.setAttribute('aria-live', 'polite');
  feedback.textContent = notice;
  refresh.type = 'button';
  refresh.className = 'mw-work-dashboard__refresh mw-work-dashboard__refresh-action';
  refresh.textContent = '刷新简报';
  refresh.addEventListener('click', () => {
    refresh.disabled = true;
    feedback.textContent = '正在刷新…';
    void refreshDashboard()
      .then(() => renderDashboard(container, getDashboardSnapshot(), '简报已更新', commands))
      .catch((error) => {
        refresh.disabled = false;
        feedback.dataset.state = 'error';
        feedback.textContent = `刷新失败：${error instanceof Error ? error.message : String(error)}`;
      });
  });
  archive.type = 'button';
  archive.className = 'mw-work-dashboard__refresh';
  archive.textContent = view.archived ? '今日简报已归档' : '归档今日简报';
  archive.disabled = Boolean(view.archived || !view.updatedAt);
  archive.addEventListener('click', () => {
    archive.disabled = true;
    feedback.textContent = '正在归档…';
    void archiveDashboard()
      .then(() => renderDashboard(container, getDashboardSnapshot(), '今日简报已归档', commands))
      .catch((error) => {
        archive.disabled = false;
        feedback.dataset.state = 'error';
        feedback.textContent = `归档失败：${error instanceof Error ? error.message : String(error)}`;
      });
  });
  manageItems.type = 'button';
  manageItems.className = 'mw-work-dashboard__refresh mw-work-dashboard__manage-items';
  manageItems.append(createIcon('icon-plus', { size: 18 }), '新建工作');
  manageItems.addEventListener('click', () => {
    if (commands.newItem) commands.newItem(manageItems);
    else commands.openItems?.();
  });
  manageItems.classList.add('mw-work-dashboard__primary-action');
  toolbar.append(manageItems);
  const topbar = document.createElement('div');
  topbar.className = 'mw-work-dashboard__topbar';
  topbar.append(intro, toolbar);
  container.append(topbar);

  const daily = document.createElement('section');
  const dailyHead = document.createElement('div');
  const dailyTitle = document.createElement('div');
  const dailyHeading = document.createElement('h3');
  const dailySource = document.createElement('p');
  const dailyActions = document.createElement('div');
  daily.className = 'mw-work-dashboard__daily';
  dailyHead.className = 'mw-work-dashboard__daily-head';
  dailyTitle.className = 'mw-work-dashboard__daily-title';
  dailyHeading.textContent = '今日简报';
  dailySource.textContent = '来源：事项、邮件、日历与工作空间';
  dailyActions.className = 'mw-work-dashboard__daily-actions';
  dailyActions.append(feedback);
  if (!view.archived) dailyActions.append(refresh);
  dailyActions.append(archive);
  dailyTitle.append(dailyHeading, dailySource);
  dailyHead.append(dailyTitle, dailyActions);
  daily.append(dailyHead);

  const statRow = document.createElement('section');
  statRow.className = 'mw-work-dashboard__stat-row';
  statRow.append(
    renderStatCard(
      'pending',
      '待处理',
      view.todayItems.filter((item) => item.business_status === 'pending'),
    ),
    renderStatCard('execution', '进行中', view.executionItems),
    renderStatCard('confirmation', '等待确认', view.pendingConfirmations),
    renderStatCard(
      'completed',
      '今日完成',
      view.todayItems.filter((item) => item.business_status === 'completed'),
    ),
  );
  daily.append(statRow);
  container.append(daily);

  // ── 来源同步异常 ──
  const sourceIssues = view.sourceStates.filter((source) =>
    source.status === 'error'
    || source.status === 'unavailable'
    || source.status === 'syncing');
  if (sourceIssues.length > 0) {
    const section = document.createElement('section');
    const heading = document.createElement('h3');
    section.className = 'mw-work-dashboard__source-status';
    heading.className = 'mw-work-dashboard__heading';
    heading.textContent = '来源同步状态';
    section.append(heading);
    for (const source of sourceIssues) {
      const row = document.createElement('div');
      const copy = document.createElement('span');
      const retry = document.createElement('button');
      row.className = 'mw-work-dashboard__source-row';
      row.dataset.sourceKey = source.connector_key;
      copy.textContent = source.last_error
        ? `${source.connector_key}：${source.last_error}`
        : `${source.connector_key}：${source.status === 'syncing' ? '同步中' : '来源不可用'}`;
      retry.type = 'button';
      retry.className = 'mw-work-dashboard__source-refresh';
      retry.dataset.sourceRefresh = source.connector_key;
      retry.textContent = '刷新';
      retry.disabled = source.status === 'syncing';
      retry.addEventListener('click', () => {
        retry.disabled = true;
        void workApi.refreshSource(source.connector_key)
          .then(refreshDashboard)
          .catch((error) => {
            retry.disabled = false;
            copy.textContent = `${source.connector_key}：${error instanceof Error ? error.message : String(error)}`;
          });
      });
      row.append(copy, retry);
      section.append(row);
    }
    container.append(section);
  }

  const templates = mostUsed(workStore.get().templates, 4);
  const dashboardGrid = document.createElement('div');
  dashboardGrid.className = 'mw-work-dashboard__content-grid';
  dashboardGrid.append(
    renderAttentionSection(view, commands),
    renderTemplateSection(templates, commands),
  );
  container.append(dashboardGrid);

  // ── 办公动态（邮件/待办/日程/会议，真实后台快照；自订阅轮询自动重绘）──
  renderOfficeListSection(container);

  // ── 更新时间 ──
  if (view.updatedAt) {
    const time = document.createElement('p');
    time.className = 'mw-work-dashboard__updated';
    time.textContent = `更新于 ${relativeTime(view.updatedAt)}`;
    container.append(time);
  }

  // 工作量统计（日/周/月）已移至「计划 / 统计」视图，不再堆在工作台。
}

/**
 * 紧凑指标卡：大数字 + 标签 + 副标题，横向排列，不带 TOP5 列表。
 * 去掉列表后单卡高度大幅降低，工作台首屏不再被统计卡撑高；事项详情在「计划」页查看。
 */
function renderStatCard(
  stat: string,
  label: string,
  items: WorkItem[],
): HTMLElement {
  const card = document.createElement('div');
  card.className = 'mw-work-dashboard__stat-card';
  card.dataset.dashboardStat = stat;
  const num = document.createElement('div');
  num.className = 'mw-work-dashboard__stat-num';
  num.textContent = String(items.length);
  const labelEl = document.createElement('div');
  labelEl.className = 'mw-work-dashboard__stat-label';
  labelEl.textContent = label;
  card.append(labelEl, num);
  return card;
}

function attentionState(
  item: WorkItem,
  view: DashboardView,
): { label: string; tone: string } {
  if (view.overdueItems.some((candidate) => candidate.item_id === item.item_id)) {
    return { label: '已逾期', tone: 'danger' };
  }
  if (view.pendingConfirmations.some((candidate) => candidate.item_id === item.item_id)) {
    return { label: '待确认', tone: 'warning' };
  }
  if (item.execution_status && item.execution_status !== 'not_started') {
    return { label: '进行中', tone: 'info' };
  }
  if (item.priority === 'high') return { label: '高优先', tone: 'warning' };
  return { label: '待处理', tone: 'neutral' };
}

function attentionMeta(item: WorkItem): string {
  if (item.description?.trim()) return item.description.trim();
  const details: string[] = [];
  if (item.category) details.push(item.category);
  if (item.source?.connector_key) details.push(item.source.connector_key);
  if (item.due_at) {
    details.push(`截止 ${new Date(epochMilliseconds(item.due_at)).toLocaleString('zh-CN', {
      month: 'numeric',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    })}`);
  }
  return details.join(' · ') || '等待处理';
}

function renderAttentionSection(
  view: DashboardView,
  commands: DashboardCommands,
): HTMLElement {
  const section = document.createElement('section');
  const header = document.createElement('div');
  const heading = document.createElement('h3');
  const viewAll = document.createElement('button');
  const list = document.createElement('div');
  section.className = 'mw-work-dashboard__panel mw-work-dashboard__attention';
  header.className = 'mw-work-dashboard__panel-head';
  heading.textContent = '需要关注';
  viewAll.type = 'button';
  viewAll.className = 'mw-work-dashboard__panel-action';
  viewAll.textContent = '查看全部';
  viewAll.addEventListener('click', () => commands.openItems?.());
  header.append(heading, viewAll);
  list.className = 'mw-work-dashboard__attention-list';

  const items = view.focusItems.slice(0, 5);
  if (items.length === 0) {
    const empty = document.createElement('p');
    empty.className = 'mw-work-dashboard__panel-empty';
    empty.textContent = '今天没有需要特别关注的事项';
    list.append(empty);
  } else {
    for (const item of items) {
      const row = document.createElement('button');
      const marker = document.createElement('span');
      const copy = document.createElement('span');
      const title = document.createElement('strong');
      const meta = document.createElement('span');
      const status = document.createElement('span');
      const itemState = attentionState(item, view);
      row.type = 'button';
      row.className = 'mw-work-dashboard__attention-row';
      row.addEventListener('click', () => commands.openItem?.(item.item_id));
      marker.className = 'mw-work-dashboard__attention-marker';
      marker.dataset.tone = itemState.tone;
      copy.className = 'mw-work-dashboard__attention-copy';
      title.textContent = item.title;
      meta.textContent = attentionMeta(item);
      status.className = 'mw-work-dashboard__status';
      status.dataset.tone = itemState.tone;
      status.textContent = itemState.label;
      copy.append(title, meta);
      row.append(marker, copy, status);
      list.append(row);
    }
  }
  section.append(header, list);
  return section;
}

function renderTemplateSection(
  templates: ReturnType<typeof mostUsed>,
  commands: DashboardCommands,
): HTMLElement {
  const section = document.createElement('section');
  const header = document.createElement('div');
  const heading = document.createElement('h3');
  const list = document.createElement('div');
  section.className = 'mw-work-dashboard__panel mw-work-dashboard__templates';
  header.className = 'mw-work-dashboard__panel-head';
  heading.textContent = '常用模板';
  header.append(heading);
  list.className = 'mw-work-dashboard__template-list';

  for (const template of templates) {
    const button = document.createElement('button');
    const copy = document.createElement('span');
    const title = document.createElement('strong');
    const description = document.createElement('span');
    button.type = 'button';
    button.className = 'mw-work-dashboard__template';
    button.dataset.templateId = template.template_id;
    title.textContent = template.name;
    description.textContent = template.description;
    copy.append(title, description);
    button.append(createIcon('icon-task', { size: 18 }), copy);
    button.addEventListener('click', () => {
      button.disabled = true;
      void instantiateTemplate(template.template_id)
        .then((item) => commands.openItem?.(item.item_id))
        .catch((error) => {
          button.disabled = false;
          button.title = error instanceof Error ? error.message : String(error);
        });
    });
    list.append(button);
  }
  if (templates.length === 0) {
    const empty = document.createElement('p');
    empty.className = 'mw-work-dashboard__panel-empty';
    empty.textContent = '暂无常用模板';
    list.append(empty);
  }
  section.append(header, list);
  return section;
}

/** 同步读取当前 dashboard 快照。 */
export function getDashboardSnapshot(): DashboardView {
  const dashboard = workStore.get().dashboard;
  if (!dashboard) {
    return {
      summary: '',
      todayItems: [],
      focusItems: [],
      overdueItems: [],
      meetingItems: [],
      mailItems: [],
      pendingConfirmations: [],
      executionItems: [],
      sourceStates: [],
      updatedAt: 0,
      archived: false,
    };
  }
  return parseDashboard(dashboard);
}
