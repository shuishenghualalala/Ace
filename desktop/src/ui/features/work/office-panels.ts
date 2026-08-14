/**
 * Work 工作台「办公动态」区：邮件 / 待办 / 日程 / 会议 四类真实数据的列表、详情与写操作。
 *
 * 数据来自 officeStore 的四份后台快照（GET /api/{mail,todo,schedule,meeting}/latest），
 * 订阅 officeStore 响应式重绘（轮询自动更新）。详情 / 写操作走 officeApi 实时端点，
 * 弹窗复用 openDialog，业务链接统一进入会话级内置浏览器。
 *
 * 欢迎页采用收件箱 / 待办紧凑双栏与日程 / 会议双月历；完整列表复用共享弹窗，
 * 不新增依赖或第二套数据模型。
 */

import DOMPurify from 'dompurify';
import { openDialog, type OverlayHandle } from '../../components/overlays';
import {
  officeApi,
  type MailMessage,
  type ScheduleItem,
  type MeetingItem,
  type TodoGroup,
} from '../../backend-client';
import { officeStore, loadOfficeSnapshots } from '../../stores/office-store';
import { openBrowserWorkbench } from '../inspector';
import { relativeTime } from './time';

/** 工作台只保留少量预览；完整列表进入弹窗，避免欢迎页被来源数据拉长。 */
const DASHBOARD_PREVIEW_SIZE = 3;
const OFFICE_DIALOG_PAGE_SIZE = 10;

interface OfficeDialogRow {
  cells: string[];
  searchText: string;
  disabled?: boolean;
  onOpen?: (trigger: HTMLButtonElement) => void;
  secondaryAction?: {
    label: string;
    ariaLabel: string;
    onPress: (trigger: HTMLButtonElement) => void;
  };
}

interface OfficeDialogOptions {
  kind: 'mail' | 'schedule' | 'meeting' | 'todo';
  title: string;
  columns: string[];
  rows: OfficeDialogRow[];
  trigger: HTMLElement;
}

interface OfficeCalendarEvent {
  dateKey: string;
  title: string;
  meta: string;
  onOpen?: (trigger: HTMLButtonElement) => void;
}

/** 失败来源的具体原因；label 为中文系统名，error 为后端 snapshot.error 原文。 */
export interface OfficeErrorEntry {
  label: string;
  error: string;
}

export interface OfficeDashboardView {
  mailCount: number;
  mailItems: MailMessage[];
  todoCount: number;
  todoGroups: TodoGroup[];
  scheduleCount: number;
  scheduleItems: ScheduleItem[];
  meetingCount: number;
  meetings: MeetingItem[];
  fetchedAt: number | null;
  stale: boolean;
  hasError: boolean;
  errors: OfficeErrorEntry[];
  loaded: boolean;
}

/** 从 officeStore 四份快照合成工作台视图模型。 */
export function getOfficeSnapshot(): OfficeDashboardView {
  const s = officeStore.get();
  const mail = s.mail;
  const todo = s.todo;
  const schedule = s.schedule;
  const meeting = s.meeting;
  const fetchedAts = [mail?.fetched_at, todo?.fetched_at, schedule?.fetched_at, meeting?.fetched_at].filter(
    (v): v is number => v != null,
  );
  // 收集每个失败来源的具体 error 文本，供状态行展示原因（不用翻日志即可区分 HTTPS / 网络 / 业务）。
  const sources = [
    { label: '邮件', snap: mail },
    { label: '待办', snap: todo },
    { label: '日程', snap: schedule },
    { label: '会议', snap: meeting },
  ];
  const errors: OfficeErrorEntry[] = [];
  for (const { label, snap } of sources) {
    if (snap && !snap.ok && snap.error) errors.push({ label, error: snap.error });
  }
  return {
    mailCount: mail?.data?.count ?? 0,
    mailItems: mail?.data?.results ?? [],
    todoCount: todo?.data?.groups?.reduce((n, g) => n + g.count, 0) ?? 0,
    todoGroups: todo?.data?.groups ?? [],
    scheduleCount: schedule?.data?.total ?? 0,
    scheduleItems: schedule?.data?.results ?? [],
    meetingCount: meeting?.data?.wait_count ?? 0,
    meetings: meeting?.data?.meetings ?? [],
    fetchedAt: fetchedAts.length ? Math.max(...fetchedAts) : null,
    stale: [mail, todo, schedule, meeting].some((v) => v?.stale),
    hasError: [mail, todo, schedule, meeting].some((v) => v && !v.ok),
    errors,
    loaded: mail != null || todo != null || schedule != null || meeting != null,
  };
}

/**
 * 状态行文案：text 为行内摘要（溢出省略），title 为鼠标悬停的完整原因。
 * 相同 error 文本合并来源 label（如邮件/待办/会议同因 HTTPS 失败只列一次），首条作摘要。
 */
function officeMetaDisplay(view: OfficeDashboardView): { text: string; title: string } {
  if (officeStore.get().loading && !view.loaded) return { text: '正在加载…', title: '' };
  if (view.hasError) {
    if (view.errors.length) {
      const grouped = new Map<string, string[]>();
      for (const e of view.errors) {
        const labels = grouped.get(e.error) ?? [];
        labels.push(e.label);
        grouped.set(e.error, labels);
      }
      const entries = [...grouped.entries()];
      return {
        text: `部分刷新失败：${entries[0][0]}`,
        title: entries.map(([err, labels]) => `[${labels.join('/')}] ${err}`).join('\n'),
      };
    }
    return { text: '部分刷新失败，展示旧数据', title: '' };
  }
  if (view.stale) return { text: '数据可能不是最新', title: '' };
  if (view.fetchedAt) return { text: `更新于 ${relativeTime(view.fetchedAt)}`, title: '' };
  return { text: view.loaded ? '暂无数据' : '正在加载…', title: '' };
}

function openOfficeUrl(url: string | undefined): void {
  if (url) openBrowserWorkbench({ url });
}

/** 取待办的跳转链接。 */
export function todoOpenUrl(item: TodoGroup['dataList'][number]): string | undefined {
  return item.url;
}

function smallButton(label: string, ariaLabel: string): HTMLButtonElement {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'mw-work-dashboard__source-refresh';
  button.textContent = label;
  button.setAttribute('aria-label', ariaLabel);
  return button;
}

/** 当前快照的完整办公列表：即时搜索、原生分页，并保留各来源原有打开行为。 */
function openOfficeListDialog(options: OfficeDialogOptions): OverlayHandle {
  const content = document.createElement('div');
  const toolbar = document.createElement('div');
  const searchLabel = document.createElement('label');
  const search = document.createElement('input');
  const resultCount = document.createElement('span');
  const table = document.createElement('div');
  const tableHead = document.createElement('div');
  const tableBody = document.createElement('div');
  const pagination = document.createElement('div');
  const previous = document.createElement('button');
  const pageStatus = document.createElement('span');
  const next = document.createElement('button');
  let query = '';
  let page = 1;

  content.className = 'mw-work-office-list';
  content.dataset.officeKind = options.kind;
  toolbar.className = 'mw-work-office-list__toolbar';
  searchLabel.className = 'mw-work-office-list__search';
  searchLabel.append('搜索');
  search.type = 'search';
  search.placeholder = '搜索标题、人员或来源';
  search.setAttribute('aria-label', `搜索${options.title}`);
  searchLabel.append(search);
  resultCount.className = 'mw-work-office-list__result-count';
  resultCount.setAttribute('aria-live', 'polite');
  toolbar.append(searchLabel, resultCount);

  table.className = 'mw-work-office-list__table';
  table.setAttribute('role', 'table');
  table.setAttribute('aria-label', options.title);
  tableHead.className = 'mw-work-office-list__row mw-work-office-list__row--head';
  tableHead.setAttribute('role', 'row');
  for (const column of options.columns) {
    const cell = document.createElement('span');
    cell.setAttribute('role', 'columnheader');
    cell.textContent = column;
    tableHead.append(cell);
  }
  tableBody.className = 'mw-work-office-list__body';
  tableBody.setAttribute('role', 'rowgroup');
  table.append(tableHead, tableBody);

  pagination.className = 'mw-work-office-list__pagination';
  previous.type = 'button';
  previous.className = 'mw-work-office-list__page-button';
  previous.textContent = '‹';
  previous.setAttribute('aria-label', '上一页');
  next.type = 'button';
  next.className = 'mw-work-office-list__page-button';
  next.textContent = '›';
  next.setAttribute('aria-label', '下一页');
  pageStatus.className = 'mw-work-office-list__page-status';
  pagination.append(previous, pageStatus, next);
  content.append(toolbar, table, pagination);

  const draw = (): void => {
    const filtered = options.rows.filter((row) => row.searchText.includes(query));
    const pageCount = Math.max(1, Math.ceil(filtered.length / OFFICE_DIALOG_PAGE_SIZE));
    page = Math.min(page, pageCount);
    const visible = filtered.slice(
      (page - 1) * OFFICE_DIALOG_PAGE_SIZE,
      page * OFFICE_DIALOG_PAGE_SIZE,
    );
    tableBody.replaceChildren();
    if (visible.length === 0) {
      const empty = document.createElement('p');
      empty.className = 'mw-work-office-list__empty';
      empty.textContent = query ? '没有匹配结果' : '暂无数据';
      tableBody.append(empty);
    } else {
      for (const row of visible) {
        const item = document.createElement('div');
        const main = document.createElement('button');
        item.className = 'mw-work-office-list__row';
        item.setAttribute('role', 'row');
        main.type = 'button';
        main.className = 'mw-work-office-list__row-main';
        main.disabled = row.disabled ?? false;
        for (const [index, value] of row.cells.entries()) {
          const cell = document.createElement('span');
          cell.setAttribute('role', 'cell');
          cell.dataset.label = options.columns[index] ?? '';
          if (index === 0) cell.className = 'mw-work-office-list__primary';
          cell.textContent = value || '—';
          main.append(cell);
        }
        if (row.onOpen) main.addEventListener('click', () => row.onOpen?.(main));
        item.append(main);
        if (row.secondaryAction) {
          const action = document.createElement('button');
          action.type = 'button';
          action.className = 'mw-work-office-list__row-action';
          action.textContent = row.secondaryAction.label;
          action.setAttribute('aria-label', row.secondaryAction.ariaLabel);
          action.addEventListener('click', () => row.secondaryAction?.onPress(action));
          item.append(action);
        }
        tableBody.append(item);
      }
    }
    resultCount.textContent = `共 ${filtered.length} 条`;
    pageStatus.textContent = `第 ${page} / ${pageCount} 页`;
    previous.disabled = page === 1;
    next.disabled = page === pageCount;
  };

  search.addEventListener('input', () => {
    query = search.value.trim().toLocaleLowerCase();
    page = 1;
    draw();
  });
  previous.addEventListener('click', () => {
    page -= 1;
    draw();
  });
  next.addEventListener('click', () => {
    page += 1;
    draw();
  });
  draw();
  return openDialog({ trigger: options.trigger, title: options.title, content });
}

function formatDateKey(date: Date): string {
  return [
    date.getFullYear(),
    String(date.getMonth() + 1).padStart(2, '0'),
    String(date.getDate()).padStart(2, '0'),
  ].join('-');
}

function officeDateKey(value: string | undefined): string | null {
  if (!value) return null;
  const explicit = value.match(/^(\d{4})[-/](\d{1,2})[-/](\d{1,2})/);
  if (explicit) {
    return [
      explicit[1],
      explicit[2].padStart(2, '0'),
      explicit[3].padStart(2, '0'),
    ].join('-');
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : formatDateKey(parsed);
}

/** 日程与会议共用月历：日期格显示事件，点击日期后在月历下方查看当天明细。 */
function renderOfficeCalendar(options: {
  kind: 'schedule' | 'meeting';
  title: string;
  count: number;
  events: OfficeCalendarEvent[];
  headerAction?: HTMLElement;
  onViewAll: (trigger: HTMLButtonElement) => void;
}): HTMLElement {
  const today = new Date();
  const firstEvent = options.events[0]?.dateKey;
  const initial = firstEvent ? new Date(`${firstEvent}T00:00:00`) : today;
  let year = initial.getFullYear();
  let month = initial.getMonth();
  let selectedKey = firstEvent ?? formatDateKey(today);
  const eventsByDate = new Map<string, OfficeCalendarEvent[]>();
  for (const event of options.events) {
    eventsByDate.set(event.dateKey, [...(eventsByDate.get(event.dateKey) ?? []), event]);
  }

  const card = document.createElement('section');
  const head = document.createElement('div');
  const label = document.createElement('span');
  const count = document.createElement('span');
  const headActions = document.createElement('div');
  const viewAll = document.createElement('button');
  const calendar = document.createElement('div');
  const nav = document.createElement('div');
  const previous = document.createElement('button');
  const monthLabel = document.createElement('strong');
  const next = document.createElement('button');
  const weekdays = document.createElement('div');
  const days = document.createElement('div');
  const detail = document.createElement('div');

  card.className = 'mw-work-dashboard__office-card mw-work-office-calendar';
  card.dataset.officeKind = options.kind;
  head.className = 'mw-work-dashboard__office-card-head';
  label.className = 'mw-work-dashboard__office-card-label';
  label.textContent = options.title;
  count.className = 'mw-work-dashboard__office-card-count';
  count.textContent = String(options.count);
  headActions.className = 'mw-work-office-calendar__head-actions';
  viewAll.type = 'button';
  viewAll.className = 'mw-work-dashboard__office-action';
  viewAll.textContent = '查看全部';
  viewAll.addEventListener('click', () => options.onViewAll(viewAll));
  if (options.headerAction) headActions.append(options.headerAction);
  headActions.append(viewAll);
  head.append(label, count, headActions);

  calendar.className = 'mw-work-office-calendar__body';
  nav.className = 'mw-work-office-calendar__nav';
  previous.type = 'button';
  previous.className = 'mw-work-office-calendar__nav-button';
  previous.textContent = '‹';
  previous.setAttribute('aria-label', '上个月');
  next.type = 'button';
  next.className = 'mw-work-office-calendar__nav-button';
  next.textContent = '›';
  next.setAttribute('aria-label', '下个月');
  monthLabel.className = 'mw-work-office-calendar__month';
  nav.append(previous, monthLabel, next);
  weekdays.className = 'mw-work-office-calendar__weekdays';
  for (const weekday of ['一', '二', '三', '四', '五', '六', '日']) {
    const cell = document.createElement('span');
    cell.textContent = weekday;
    weekdays.append(cell);
  }
  days.className = 'mw-work-office-calendar__days';
  detail.className = 'mw-work-office-calendar__detail';
  calendar.append(nav, weekdays, days, detail);
  card.append(head, calendar);

  const drawDetail = (): void => {
    detail.replaceChildren();
    const selected = new Date(`${selectedKey}T00:00:00`);
    const title = document.createElement('strong');
    title.className = 'mw-work-office-calendar__detail-title';
    title.textContent = selected.toLocaleDateString('zh-CN', {
      month: 'long',
      day: 'numeric',
      weekday: 'short',
    });
    detail.append(title);
    const selectedEvents = eventsByDate.get(selectedKey) ?? [];
    if (selectedEvents.length === 0) {
      const empty = document.createElement('span');
      empty.className = 'mw-work-office-calendar__detail-empty';
      empty.textContent = `当天暂无${options.title}`;
      detail.append(empty);
      return;
    }
    for (const event of selectedEvents) {
      const row = document.createElement('button');
      const eventTitle = document.createElement('strong');
      const meta = document.createElement('span');
      row.type = 'button';
      row.className = 'mw-work-office-calendar__event';
      row.disabled = !event.onOpen;
      eventTitle.textContent = event.title;
      meta.textContent = event.meta;
      row.append(eventTitle, meta);
      if (event.onOpen) row.addEventListener('click', () => event.onOpen?.(row));
      detail.append(row);
    }
  };

  const drawMonth = (): void => {
    monthLabel.textContent = `${year} 年 ${month + 1} 月`;
    days.replaceChildren();
    const first = new Date(year, month, 1);
    const offset = (first.getDay() + 6) % 7;
    const cellCount = Math.ceil((offset + new Date(year, month + 1, 0).getDate()) / 7) * 7;
    const start = new Date(year, month, 1 - offset);
    for (let index = 0; index < cellCount; index += 1) {
      const date = new Date(start);
      date.setDate(start.getDate() + index);
      const key = formatDateKey(date);
      const dayEvents = eventsByDate.get(key) ?? [];
      const button = document.createElement('button');
      const number = document.createElement('span');
      button.type = 'button';
      button.className = 'mw-work-office-calendar__day';
      button.dataset.date = key;
      button.dataset.outside = String(date.getMonth() !== month);
      button.dataset.selected = String(key === selectedKey);
      button.dataset.today = String(key === formatDateKey(today));
      button.setAttribute('aria-label', `${key}，${dayEvents.length} 项${options.title}`);
      number.className = 'mw-work-office-calendar__day-number';
      number.textContent = String(date.getDate());
      button.append(number);
      if (dayEvents.length > 0) {
        const event = document.createElement('span');
        event.className = 'mw-work-office-calendar__day-event';
        event.textContent = dayEvents[0].title;
        button.append(event);
        if (dayEvents.length > 1) {
          const more = document.createElement('span');
          more.className = 'mw-work-office-calendar__day-more';
          more.textContent = `+${dayEvents.length - 1}`;
          button.append(more);
        }
      }
      button.addEventListener('click', () => {
        selectedKey = key;
        if (date.getMonth() !== month) {
          year = date.getFullYear();
          month = date.getMonth();
        }
        drawMonth();
      });
      days.append(button);
    }
    drawDetail();
  };

  previous.addEventListener('click', () => {
    month -= 1;
    if (month < 0) {
      month = 11;
      year -= 1;
    }
    selectedKey = formatDateKey(new Date(year, month, 1));
    drawMonth();
  });
  next.addEventListener('click', () => {
    month += 1;
    if (month > 11) {
      month = 0;
      year += 1;
    }
    selectedKey = formatDateKey(new Date(year, month, 1));
    drawMonth();
  });
  drawMonth();
  return card;
}

/** 通用分组卡片：标题 + 计数 + 3 条预览；完整内容固定进入“查看全部”弹窗。 */
function officeCard(
  title: string,
  count: number,
  rows: HTMLElement[],
  headerAction?: HTMLElement,
  kind = '',
  onViewAll?: (trigger: HTMLButtonElement) => void,
): HTMLElement {
  const card = document.createElement('section');
  card.className = 'mw-work-dashboard__office-card';
  if (kind) card.dataset.officeKind = kind;
  const head = document.createElement('div');
  head.className = 'mw-work-dashboard__office-card-head';
  const label = document.createElement('span');
  label.className = 'mw-work-dashboard__office-card-label';
  label.textContent = title;
  const num = document.createElement('span');
  num.className = 'mw-work-dashboard__office-card-count';
  num.textContent = String(count);
  head.append(label, num);
  if (headerAction) head.append(headerAction);
  card.append(head);
  const body = document.createElement('div');
  body.className = 'mw-work-dashboard__office-card-body';
  if (rows.length === 0) {
    const empty = document.createElement('p');
    empty.className = 'mw-work-dashboard__empty';
    empty.textContent = '暂无';
    body.append(empty);
  } else {
    body.append(...rows);
    const entries = Array.from(
      body.querySelectorAll<HTMLElement>('[data-office-entry]'),
    );
    const groups = Array.from(
      body.querySelectorAll<HTMLElement>('[data-office-group]'),
    );
    entries.forEach((entry, index) => {
      entry.hidden = index >= DASHBOARD_PREVIEW_SIZE;
    });
    groups.forEach((group) => {
      group.hidden = !group.querySelector('[data-office-entry]:not([hidden])');
    });
    entries.filter((entry) => !entry.hidden).at(-1)?.classList.add('is-list-end');
    if (onViewAll) {
      const more = document.createElement('button');
      more.type = 'button';
      more.className = 'mw-work-dashboard__office-more';
      more.textContent = '查看全部';
      more.addEventListener('click', () => onViewAll(more));
      body.append(more);
    }
  }
  card.append(body);
  return card;
}

/** 卡片头部小动作按钮（写邮件 / 新建日程）：统一 office 卡片动作样式。 */
function officeAction(label: string, ariaLabel: string): HTMLButtonElement {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'mw-work-dashboard__office-action';
  button.textContent = label;
  button.setAttribute('aria-label', ariaLabel);
  return button;
}

function renderMailGroup(view: OfficeDashboardView): HTMLElement {
  const compose = officeAction('写邮件', '写新邮件');
  compose.addEventListener('click', () => openMailComposeDialog({}, compose));
  const rows = view.mailItems.map((mail) => {
    const row = document.createElement('button');
    const status = document.createElement('span');
    const copy = document.createElement('span');
    const heading = document.createElement('span');
    const sender = document.createElement('strong');
    const subject = document.createElement('span');
    const time = document.createElement('time');
    const isUnread = mail.read === false || mail.readStatus === '未读';
    row.type = 'button';
    row.className = 'mw-work-dashboard__office-row mw-work-dashboard__mail-row';
    row.dataset.officeEntry = '';
    row.dataset.unread = String(isUnread);
    row.setAttribute('aria-label', `${isUnread ? '未读邮件：' : ''}${mail.subject || '(无主题)'}`);
    status.className = 'mw-work-dashboard__mail-status';
    copy.className = 'mw-work-dashboard__mail-copy';
    heading.className = 'mw-work-dashboard__mail-heading';
    sender.className = 'mw-work-dashboard__mail-from';
    sender.textContent = mail.from || '未知发件人';
    subject.className = 'mw-work-dashboard__office-row-preview';
    subject.textContent = mail.subject || '(无主题)';
    time.className = 'mw-work-dashboard__mail-time';
    time.textContent = (mail.sendDate || '').slice(11, 16) || mail.sendDate || '';
    heading.append(sender, time);
    copy.append(heading, subject);
    row.append(status, copy);
    row.addEventListener('click', () => {
      row.dataset.unread = 'false';
      row.setAttribute('aria-label', mail.subject || '(无主题)');
      openMailDetailDialog(mail.mid, row);
    });
    return row;
  });
  return officeCard('收件箱', view.mailCount, rows, compose, 'mail', (trigger) => {
    openOfficeListDialog({
      kind: 'mail',
      title: '全部邮件',
      columns: ['主题', '发件人', '接收时间', '状态'],
      trigger,
      rows: view.mailItems.map((mail) => {
        const cells = [
          mail.subject || '(无主题)',
          mail.from || '未知发件人',
          mail.sendDate || '',
          mail.read === false || mail.readStatus === '未读' ? '未读' : '已读',
        ];
        return {
          cells,
          searchText: cells.join(' ').toLocaleLowerCase(),
          onOpen: (row) => openMailDetailDialog(mail.mid, row),
        };
      }),
    });
  });
}

function renderScheduleGroup(view: OfficeDashboardView): HTMLElement {
  const create = officeAction('新建日程', '新建日程');
  create.addEventListener('click', () => openScheduleComposeDialog(undefined, create));
  const events = view.scheduleItems.flatMap((item): OfficeCalendarEvent[] => {
    const dateKey = officeDateKey(item.scheduleStartDate);
    if (!dateKey) return [];
    return [{
      dateKey,
      title: item.scheduleTheme || '(无主题)',
      meta: [item.scheduleStartTime?.slice(0, 5), item.scheduleEndTime?.slice(0, 5)]
        .filter(Boolean)
        .join('–'),
      onOpen: (row) => openScheduleComposeDialog(item, row),
    }];
  });
  return renderOfficeCalendar({
    kind: 'schedule',
    title: '日程',
    count: view.scheduleCount,
    events,
    headerAction: create,
    onViewAll: (trigger) => {
    openOfficeListDialog({
      kind: 'schedule',
      title: '全部日程',
      columns: ['主题', '开始', '结束', '日期'],
      trigger,
      rows: view.scheduleItems.map((item) => {
        const cells = [
          item.scheduleTheme || '(无主题)',
          item.scheduleStartTime || '',
          item.scheduleEndTime || '',
          [item.scheduleStartDate, item.scheduleEndDate].filter(Boolean).join(' 至 '),
        ];
        return {
          cells,
          searchText: cells.join(' ').toLocaleLowerCase(),
          onOpen: (row) => openScheduleComposeDialog(item, row),
          secondaryAction: {
            label: '删除',
            ariaLabel: `删除日程：${item.scheduleTheme || '(无主题)'}`,
            onPress: (button) => deleteSchedule(item, button),
          },
        };
      }),
    });
    },
  });
}

function renderMeetingGroup(view: OfficeDashboardView): HTMLElement {
  const events = view.meetings.flatMap((meeting): OfficeCalendarEvent[] => {
    const dateKey = officeDateKey(meeting.time);
    if (!dateKey) return [];
    const event: OfficeCalendarEvent = {
      dateKey,
      title: meeting.infoName,
      meta: [meeting.time.slice(11, 16), meeting.conferenceTypeName].filter(Boolean).join(' · '),
    };
    if (meeting.url) event.onOpen = () => openOfficeUrl(meeting.url);
    return [event];
  });
  return renderOfficeCalendar({
    kind: 'meeting',
    title: '会议',
    count: view.meetingCount,
    events,
    onViewAll: (trigger) => {
    openOfficeListDialog({
      kind: 'meeting',
      title: '全部会议',
      columns: ['会议', '时间', '类型', '状态'],
      trigger,
      rows: view.meetings.map((meeting) => {
        const cells = [
          meeting.infoName,
          meeting.time,
          meeting.conferenceTypeName || '',
          meeting.status === 2 ? '进行中' : meeting.status === 3 ? '已暂停' : '已发布',
        ];
        return {
          cells,
          searchText: cells.join(' ').toLocaleLowerCase(),
          disabled: !meeting.url,
          onOpen: () => openOfficeUrl(meeting.url),
        };
      }),
    });
    },
  });
}

function renderTodoGroup(view: OfficeDashboardView): HTMLElement {
  const rows: HTMLElement[] = [];
  for (const group of view.todoGroups) {
    if (!group.dataList.length) continue;
    const groupSection = document.createElement('div');
    const heading = document.createElement('div');
    groupSection.className = 'mw-work-dashboard__todo-section';
    groupSection.dataset.officeGroup = '';
    heading.className = 'mw-work-dashboard__todo-group';
    heading.textContent = group.groupName;
    groupSection.append(heading);
    for (const item of group.dataList) {
      const targetUrl = todoOpenUrl(item);
      const row = document.createElement('button');
      const copy = document.createElement('span');
      const title = document.createElement('strong');
      const meta = document.createElement('span');
      const system = document.createElement('span');
      row.type = 'button';
      row.className = 'mw-work-dashboard__office-row mw-work-dashboard__todo-row';
      row.dataset.officeEntry = '';
      row.disabled = !targetUrl;
      row.setAttribute(
        'aria-label',
        targetUrl ? `打开待办：${item.itemTitle}` : `${item.itemTitle}（无可用链接）`,
      );
      if (!targetUrl) row.title = '来源未提供可打开链接';
      copy.className = 'mw-work-dashboard__todo-copy';
      title.textContent = item.itemTitle;
      meta.className = 'mw-work-dashboard__office-row-meta';
      meta.textContent = [item.drafterName, item.itemCreateTime].filter(Boolean).join(' · ');
      system.className = 'mw-work-dashboard__todo-system';
      system.textContent = item.systemName || group.groupName;
      copy.append(title, meta);
      row.append(copy, system);
      row.addEventListener('click', () => openOfficeUrl(targetUrl));
      groupSection.append(row);
    }
    rows.push(groupSection);
  }
  return officeCard('待办', view.todoCount, rows, undefined, 'todo', (trigger) => {
    const items = view.todoGroups.flatMap((group) => (
      group.dataList.map((item) => ({ groupName: group.groupName, item }))
    ));
    openOfficeListDialog({
      kind: 'todo',
      title: '全部待办',
      columns: ['标题', '分类', '来源系统', '创建信息'],
      trigger,
      rows: items.map(({ groupName, item }) => {
        const targetUrl = todoOpenUrl(item);
        const cells = [
          item.itemTitle,
          groupName,
          item.systemName || groupName,
          [item.drafterName, item.itemCreateTime].filter(Boolean).join(' · '),
        ];
        return {
          cells,
          searchText: cells.join(' ').toLocaleLowerCase(),
          disabled: !targetUrl,
          onOpen: () => openOfficeUrl(targetUrl),
        };
      }),
    });
  });
}

/**
 * 渲染办公动态区到容器；订阅 officeStore 响应式重绘，容器移出 DOM 时自动解订阅。
 * 四个来源按办公语义呈现：收件箱 / 待办为紧凑列表，日程 / 会议为可交互月历。
 */
export function renderOfficeListSection(container: HTMLElement): void {
  const section = document.createElement('section');
  section.className = 'mw-work-dashboard__office';
  const head = document.createElement('div');
  head.className = 'mw-work-dashboard__office-head';
  const title = document.createElement('h3');
  title.className = 'mw-work-dashboard__heading mw-work-dashboard__office-title';
  title.textContent = '办公动态';
  const meta = document.createElement('span');
  meta.className = 'mw-work-dashboard__office-meta';
  const refresh = document.createElement('button');
  refresh.type = 'button';
  refresh.className = 'mw-work-dashboard__office-action';
  refresh.textContent = '刷新';
  const grid = document.createElement('div');
  grid.className = 'mw-work-dashboard__office-grid';
  head.append(title, meta, refresh);
  section.append(head, grid);
  container.append(section);

  const draw = (): void => {
    const view = getOfficeSnapshot();
    const { text, title } = officeMetaDisplay(view);
    meta.textContent = text;
    // 悬停查看全部失败来源的完整原因（去重后的 [来源] 原因 列表）。
    if (title) meta.title = title;
    else meta.removeAttribute('title');
    meta.dataset.state = view.hasError ? 'error' : '';
    refresh.disabled = officeStore.get().loading;
    grid.replaceChildren(
      renderMailGroup(view),
      renderScheduleGroup(view),
      renderMeetingGroup(view),
      renderTodoGroup(view),
    );
  };
  draw();
  const unsubscribe = officeStore.subscribe(() => draw());
  refresh.addEventListener('click', () => {
    void loadOfficeSnapshots();
  });
  // 容器从 DOM 移除时解订阅，避免轮询回调在脱离 DOM 后继续重绘。
  const observer = new MutationObserver(() => {
    if (document.body.contains(section)) return;
    unsubscribe();
    observer.disconnect();
  });
  observer.observe(document.body, { childList: true, subtree: true });
}

// ── 邮件详情 ──

/** 邮件正文可能是 HTML，经 DOMPurify 清洗后渲染（模型输出为不可信输入）。 */
export function openMailDetailDialog(mid: string, trigger?: HTMLElement): OverlayHandle {
  const content = document.createElement('div');
  content.className = 'mw-work-dashboard__settings-dialog';
  content.textContent = '正在加载邮件…';
  const dialog = openDialog({ trigger, title: '邮件详情', content });
  void officeApi
    .mailDetail(mid)
    .then((res) => {
      if (!res.ok) {
        content.textContent = res.error || '邮件加载失败';
        return;
      }
      content.replaceChildren(...buildMailDetailBody(res, mid, dialog, trigger));
    })
    .catch((err) => {
      content.textContent = `邮件加载失败：${err instanceof Error ? err.message : String(err)}`;
    });
  return dialog;
}

function buildMailDetailBody(
  res: { subject: string; content: string; from: string },
  mid: string,
  dialog: OverlayHandle,
  trigger?: HTMLElement,
): HTMLElement[] {
  const header = document.createElement('header');
  const subject = document.createElement('h2');
  const sender = document.createElement('div');
  const avatar = document.createElement('span');
  const senderCopy = document.createElement('span');
  const senderName = document.createElement('strong');
  const senderAddress = document.createElement('span');
  header.className = 'mw-work-mail-reader__header';
  subject.className = 'mw-work-mail-reader__subject';
  subject.textContent = res.subject;
  sender.className = 'mw-work-mail-reader__sender';
  avatar.className = 'mw-work-mail-reader__avatar';
  avatar.textContent = (res.from || '邮').trim().slice(0, 1).toUpperCase();
  senderCopy.className = 'mw-work-mail-reader__sender-copy';
  senderName.textContent = res.from || '未知发件人';
  senderAddress.textContent = '发送至当前账号';
  senderCopy.append(senderName, senderAddress);
  sender.append(avatar, senderCopy);
  header.append(subject, sender);
  const body = document.createElement('div');
  body.className = 'mw-work-mail-reader__body';
  body.innerHTML = DOMPurify.sanitize(res.content || '');
  const actions = document.createElement('div');
  actions.className = 'mw-work-mail-reader__actions';
  const reply = smallButton('回复', '回复邮件');
  reply.addEventListener('click', () => {
    dialog.close();
    openMailComposeDialog({ to: res.from, subject: `Re: ${res.subject}` }, trigger);
  });
  const forward = smallButton('转发', '转发邮件');
  forward.addEventListener('click', () => {
    dialog.close();
    openMailForwardDialog(mid, res.subject, trigger);
  });
  actions.append(reply, forward);
  return [header, body, actions];
}

// ── 写邮件（发送前确认摘要，满足 /api/mail/send 调用方责任）──

export interface MailComposePrefill {
  to?: string;
  subject?: string;
  cc?: string;
  content?: string;
}

export function openMailComposeDialog(prefill: MailComposePrefill = {}, trigger?: HTMLElement): OverlayHandle {
  const content = document.createElement('form');
  content.className = 'mw-work-mail-compose mw-work-dashboard__settings-dialog';
  const sender = document.createElement('div');
  const to = document.createElement('input');
  const subject = document.createElement('input');
  const cc = document.createElement('input');
  const body = document.createElement('textarea');
  const ccToggle = document.createElement('button');
  const editorLabel = document.createElement('div');
  const feedback = document.createElement('p');
  const actions = document.createElement('div');
  const send = document.createElement('button');
  const cancel = document.createElement('button');
  to.type = 'email';
  to.required = true;
  to.value = prefill.to ?? '';
  to.setAttribute('aria-label', '收件人邮箱');
  to.placeholder = 'name@example.com';
  subject.required = true;
  subject.value = prefill.subject ?? '';
  subject.setAttribute('aria-label', '邮件主题');
  body.required = true;
  body.value = prefill.content ?? '';
  body.setAttribute('aria-label', '邮件正文');
  body.rows = 8;
  cc.value = prefill.cc ?? '';
  cc.setAttribute('aria-label', '抄送（可选）');
  cc.placeholder = '抄送邮箱（可选）';
  ccToggle.type = 'button';
  ccToggle.className = 'mw-work-mail-compose__cc-toggle';
  ccToggle.textContent = '抄送';
  editorLabel.className = 'mw-work-mail-compose__editor-label';
  editorLabel.textContent = '邮件正文';
  sender.className = 'mw-work-mail-compose__sender';
  sender.innerHTML = '<span>发件账号</span><strong>当前登录账号</strong>';
  feedback.className = 'mw-work-items-page__feedback';
  feedback.setAttribute('aria-live', 'polite');
  actions.className = 'mw-work-items-page__create-actions';
  send.type = 'button';
  send.textContent = '发送';
  send.className = 'mw-work-items-page__create-submit';
  cancel.type = 'button';
  cancel.textContent = '取消';
  cancel.className = 'mw-work-items-page__create-cancel';
  const field = (label: string, control: HTMLElement, required = false): HTMLLabelElement => {
    const wrapper = document.createElement('label');
    const caption = document.createElement('span');
    wrapper.className = 'mw-work-mail-compose__field';
    caption.className = 'mw-work-mail-compose__label';
    caption.textContent = `${label}${required ? ' *' : ''}`;
    wrapper.append(caption, control);
    return wrapper;
  };
  const toField = field('收件人', to, true);
  const ccField = field('抄送', cc);
  const subjectField = field('主题', subject, true);
  const bodyField = field('', body, true);
  toField.classList.add('mw-work-mail-compose__recipient');
  toField.append(ccToggle);
  ccField.classList.add('mw-work-mail-compose__cc');
  ccField.hidden = !cc.value;
  subjectField.classList.add('mw-work-mail-compose__subject');
  bodyField.classList.add('mw-work-mail-compose__editor');
  bodyField.prepend(editorLabel);
  content.append(sender, toField, ccField, subjectField, bodyField, feedback, actions);
  actions.append(cancel, send);
  const dialog = openDialog({ trigger, title: '写邮件', content });
  cancel.addEventListener('click', () => dialog.close());
  ccToggle.addEventListener('click', () => {
    ccField.hidden = !ccField.hidden;
    ccToggle.setAttribute('aria-expanded', String(!ccField.hidden));
    if (!ccField.hidden) cc.focus();
  });
  ccToggle.setAttribute('aria-expanded', String(!ccField.hidden));

  const validate = (): boolean => {
    if (!to.value.trim() || !subject.value.trim() || !body.value.trim()) {
      feedback.dataset.state = 'error';
      feedback.textContent = '收件人、主题、正文不能为空';
      return false;
    }
    return true;
  };

  send.addEventListener('click', () => {
    if (!validate()) return;
    // 进入确认态：展示发送摘要 + 二次确认，避免误发。
    showSendConfirm(content, feedback, {
      to: to.value.trim(),
      subject: subject.value.trim(),
      cc: cc.value.trim(),
      content: body.value,
    }, dialog);
  });
  return dialog;
}

interface SendPayload {
  to: string;
  subject: string;
  cc: string;
  content: string;
}

function showSendConfirm(
  form: HTMLFormElement,
  feedback: HTMLElement,
  payload: SendPayload,
  dialog: OverlayHandle,
): void {
  const summary = document.createElement('div');
  summary.className = 'mw-work-items-page__create';
  const lines: Array<[string, string]> = [
    ['收件人', payload.to],
    ['抄送', payload.cc || '（无）'],
    ['主题', payload.subject],
    ['正文预览', payload.content.slice(0, 120) + (payload.content.length > 120 ? '…' : '')],
  ];
  for (const [label, value] of lines) {
    const row = document.createElement('p');
    const caption = document.createElement('span');
    caption.className = 'mw-work-dashboard__heading';
    caption.textContent = `${label}：`;
    row.append(caption, document.createTextNode(value));
    summary.append(row);
  }
  const confirmBar = document.createElement('div');
  confirmBar.className = 'mw-work-items-page__create-actions';
  const back = document.createElement('button');
  const confirm = document.createElement('button');
  back.type = 'button';
  back.textContent = '再改改';
  back.className = 'mw-work-items-page__create-cancel';
  confirm.type = 'button';
  confirm.textContent = '确认发送';
  confirm.className = 'mw-work-items-page__create-submit';
  confirmBar.append(back, confirm);
  // 暂存原表单，确认态替换；返回时还原。
  const original = Array.from(form.children);
  form.replaceChildren(summary, feedback, confirmBar);
  feedback.dataset.state = '';
  feedback.textContent = '请确认收件人与内容后发送。';
  back.addEventListener('click', () => {
    form.replaceChildren(...original);
  });
  confirm.addEventListener('click', () => {
    confirm.disabled = true;
    back.disabled = true;
    feedback.textContent = '正在发送…';
    void officeApi
      .mailSend({
        to: payload.to,
        subject: payload.subject,
        content: payload.content,
        ...(payload.cc ? { cc: payload.cc } : {}),
      })
      .then((res) => {
        if (res.ok) {
          dialog.close();
          void loadOfficeSnapshots();
        } else {
          feedback.dataset.state = 'error';
          feedback.textContent = res.error || '发送失败';
          confirm.disabled = false;
          back.disabled = false;
        }
      })
      .catch((err) => {
        feedback.dataset.state = 'error';
        feedback.textContent = `发送失败：${err instanceof Error ? err.message : String(err)}`;
        confirm.disabled = false;
        back.disabled = false;
      });
  });
}

// ── 转发 ──

function openMailForwardDialog(mid: string, subject: string, trigger?: HTMLElement): OverlayHandle {
  const content = document.createElement('div');
  content.className = 'mw-work-items-page__create mw-work-dashboard__settings-dialog';
  const input = document.createElement('input');
  input.type = 'email';
  input.required = true;
  input.setAttribute('aria-label', '转发收件人邮箱');
  input.placeholder = 'name@example.com';
  const feedback = document.createElement('p');
  feedback.className = 'mw-work-items-page__feedback';
  feedback.setAttribute('aria-live', 'polite');
  const actions = document.createElement('div');
  actions.className = 'mw-work-items-page__create-actions';
  const send = document.createElement('button');
  const cancel = document.createElement('button');
  send.type = 'button';
  send.textContent = '确认转发';
  send.className = 'mw-work-items-page__create-submit';
  cancel.type = 'button';
  cancel.textContent = '取消';
  cancel.className = 'mw-work-items-page__create-cancel';
  actions.append(cancel, send);
  const field = (label: string, control: HTMLElement): HTMLLabelElement => {
    const wrapper = document.createElement('label');
    const caption = document.createElement('span');
    wrapper.className = 'mw-work-items-page__create-field';
    caption.className = 'mw-work-items-page__create-label';
    caption.textContent = label;
    wrapper.append(caption, control);
    return wrapper;
  };
  content.append(field('转发收件人', input), feedback, actions);
  const dialog = openDialog({ trigger, title: `转发：${subject}`, content });
  cancel.addEventListener('click', () => dialog.close());
  send.addEventListener('click', () => {
    if (!input.value.trim()) {
      feedback.dataset.state = 'error';
      feedback.textContent = '请填写转发收件人';
      return;
    }
    send.disabled = true;
    feedback.textContent = '正在转发…';
    void officeApi
      .mailForward(mid, input.value.trim())
      .then((res) => {
        if (res.ok) {
          dialog.close();
          void loadOfficeSnapshots();
        } else {
          feedback.dataset.state = 'error';
          feedback.textContent = res.error || '转发失败';
          send.disabled = false;
        }
      })
      .catch((err) => {
        feedback.dataset.state = 'error';
        feedback.textContent = `转发失败：${err instanceof Error ? err.message : String(err)}`;
        send.disabled = false;
      });
  });
  return dialog;
}

// ── 日程新增 / 修改 / 删除 ──

function todayStr(): string {
  const d = new Date();
  const offset = d.getTimezoneOffset() * 60_000;
  return new Date(d.getTime() - offset).toISOString().slice(0, 10);
}

/** <input type=time> 返回 HH:MM，后端要求 HH:MM:SS。 */
function toTimeSeconds(value: string): string {
  return /^\d{2}:\d{2}$/.test(value) ? `${value}:00` : value;
}

/** 新建或修改日程（item 存在则 operate_id=1 修改，否则 0 新增）。 */
export function openScheduleComposeDialog(
  item?: ScheduleItem,
  trigger?: HTMLElement,
): OverlayHandle {
  const content = document.createElement('form');
  content.className = 'mw-work-items-page__create mw-work-dashboard__settings-dialog';
  const theme = document.createElement('input');
  const startDate = document.createElement('input');
  const startTime = document.createElement('input');
  const endDate = document.createElement('input');
  const endTime = document.createElement('input');
  const allDay = document.createElement('input');
  const remind = document.createElement('select');
  const location = document.createElement('input');
  const remark = document.createElement('textarea');
  const feedback = document.createElement('p');
  const actions = document.createElement('div');
  const submit = document.createElement('button');
  const cancel = document.createElement('button');
  theme.required = true;
  theme.value = item?.scheduleTheme ?? '';
  theme.setAttribute('aria-label', '日程主题');
  startDate.type = 'date';
  startDate.required = true;
  startDate.value = item?.scheduleStartDate ?? todayStr();
  startTime.type = 'time';
  startTime.required = true;
  startTime.value = (item?.scheduleStartTime ?? '09:00:00').slice(0, 5);
  endDate.type = 'date';
  endDate.required = true;
  endDate.value = item?.scheduleEndDate ?? item?.scheduleStartDate ?? todayStr();
  endTime.type = 'time';
  endTime.required = true;
  endTime.value = (item?.scheduleEndTime ?? '10:00:00').slice(0, 5);
  allDay.type = 'checkbox';
  allDay.setAttribute('aria-label', '全天');
  const reminders = [
    ['0', '不提醒'],
    ['1', '开始时'],
    ['2', '提前 5 分钟'],
    ['3', '提前 10 分钟'],
    ['4', '提前 15 分钟'],
    ['5', '提前 30 分钟'],
    ['6', '提前 1 小时'],
    ['7', '提前 1 天'],
    ['8', '提前 2 天'],
  ];
  for (const [value, label] of reminders) {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = label;
    remind.append(option);
  }
  remind.value = '1';
  remind.setAttribute('aria-label', '提醒');
  location.setAttribute('aria-label', '地点（可选）');
  location.placeholder = '会议室或线上会议';
  remark.setAttribute('aria-label', '备注（可选）');
  remark.rows = 3;
  feedback.className = 'mw-work-items-page__feedback';
  feedback.setAttribute('aria-live', 'polite');
  actions.className = 'mw-work-items-page__create-actions';
  submit.type = 'button';
  submit.textContent = item ? '保存修改' : '创建';
  submit.className = 'mw-work-items-page__create-submit';
  cancel.type = 'button';
  cancel.textContent = '取消';
  cancel.className = 'mw-work-items-page__create-cancel';
  const field = (label: string, control: HTMLElement, required = false): HTMLLabelElement => {
    const wrapper = document.createElement('label');
    const caption = document.createElement('span');
    wrapper.className = 'mw-work-items-page__create-field';
    caption.className = 'mw-work-items-page__create-label';
    caption.textContent = `${label}${required ? ' *' : ''}`;
    wrapper.append(caption, control);
    return wrapper;
  };
  const allDayField = field('全天', allDay);
  allDayField.classList.add('mw-work-schedule-compose__all-day');
  const dateTimeGrid = document.createElement('div');
  dateTimeGrid.className = 'mw-work-schedule-compose__time-grid';
  dateTimeGrid.append(
    field('开始日期', startDate, true),
    field('开始时间', startTime, true),
    field('结束日期', endDate, true),
    field('结束时间', endTime, true),
  );
  content.classList.add('mw-work-schedule-compose');
  content.append(
    field('标题', theme, true),
    allDayField,
    dateTimeGrid,
    field('提醒', remind),
    field('地点', location),
    field('备注', remark),
    feedback,
    actions,
  );
  actions.append(cancel, submit);
  const dialog = openDialog({ trigger, title: item ? '修改日程' : '新建日程', content });
  cancel.addEventListener('click', () => dialog.close());
  allDay.addEventListener('change', () => {
    startTime.disabled = allDay.checked;
    endTime.disabled = allDay.checked;
    if (allDay.checked) {
      startTime.value = '00:00';
      endTime.value = '23:59';
    }
  });
  submit.addEventListener('click', () => {
    if (!theme.value.trim() || !startDate.value || !startTime.value || !endDate.value || !endTime.value) {
      feedback.dataset.state = 'error';
      feedback.textContent = '主题、起止日期与时间不能为空';
      return;
    }
    const remindVal = Number.parseInt(remind.value, 10) || 0;
    submit.disabled = true;
    feedback.textContent = '正在保存…';
    void officeApi
      .scheduleSync({
        operate_id: item ? 1 : 0,
        theme: theme.value.trim(),
        start_date: startDate.value,
        start_time: toTimeSeconds(startTime.value),
        end_date: endDate.value,
        end_time: toTimeSeconds(endTime.value),
        remind_mode: remindVal,
        ...(remark.value.trim() ? { remark: remark.value.trim() } : {}),
        ...(location.value.trim() ? { meeting_place: location.value.trim() } : {}),
        ...(item ? { schdule_id: item.scheduleId } : {}),
      })
      .then((res) => {
        if (res.ok) {
          dialog.close();
          void loadOfficeSnapshots();
        } else {
          feedback.dataset.state = 'error';
          feedback.textContent = res.error || '保存失败';
          submit.disabled = false;
        }
      })
      .catch((err) => {
        feedback.dataset.state = 'error';
        feedback.textContent = `保存失败：${err instanceof Error ? err.message : String(err)}`;
        submit.disabled = false;
      });
  });
  return dialog;
}

/** 删除日程（operate_id=2，需 schdule_id 与必填字段）。失败时保留弹窗并提示，与其它写操作一致。 */
function deleteSchedule(item: ScheduleItem, trigger?: HTMLElement): void {
  const content = document.createElement('div');
  content.className = 'mw-work-dashboard__settings-dialog';
  const summary = document.createElement('p');
  summary.textContent = `确认删除日程「${item.scheduleTheme ?? ''}」？该操作不可撤销。`;
  const feedback = document.createElement('p');
  feedback.className = 'mw-work-items-page__feedback';
  feedback.setAttribute('aria-live', 'polite');
  const actions = document.createElement('div');
  actions.className = 'mw-work-items-page__create-actions';
  const cancel = document.createElement('button');
  const confirm = document.createElement('button');
  cancel.type = 'button';
  cancel.textContent = '取消';
  cancel.className = 'mw-work-items-page__create-cancel';
  confirm.type = 'button';
  confirm.textContent = '确认删除';
  confirm.className = 'mw-work-items-page__create-submit';
  actions.append(cancel, confirm);
  content.append(summary, feedback, actions);
  const dialog = openDialog({ trigger, title: '删除日程', content });
  cancel.addEventListener('click', () => dialog.close());
  confirm.addEventListener('click', () => {
    confirm.disabled = true;
    feedback.textContent = '正在删除…';
    void officeApi
      .scheduleSync({
        operate_id: 2,
        theme: item.scheduleTheme ?? '',
        start_date: item.scheduleStartDate ?? todayStr(),
        start_time: item.scheduleStartTime ?? '00:00:00',
        end_date: item.scheduleEndDate ?? item.scheduleStartDate ?? todayStr(),
        end_time: item.scheduleEndTime ?? '00:00:00',
        remind_mode: 0,
        schdule_id: item.scheduleId,
      })
      .then((res) => {
        if (res.ok) {
          dialog.close();
          void loadOfficeSnapshots();
        } else {
          feedback.dataset.state = 'error';
          feedback.textContent = res.error || '删除失败';
          confirm.disabled = false;
        }
      })
      .catch((err) => {
        feedback.dataset.state = 'error';
        feedback.textContent = `删除失败：${err instanceof Error ? err.message : String(err)}`;
        confirm.disabled = false;
      });
  });
}
