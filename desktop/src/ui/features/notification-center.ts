/**
 * 通知中心：左上角 brand 块内的铃铛按钮（#notification-bell-btn，由 application-shell 创建）
 * + 未读角标（#notification-badge）+ 点击弹出的通知面板。
 *
 * 数据流：
 * - 未读状态单一数据源：notifications（未读条目，上限 UNREAD_LIST_LIMIT）+ unreadCount
 *   （后端权威未读总数，可能大于条目数）。启动与面板打开都走同一次 REST 未读拉取；
 * - WS notification 帧（chat-controller 分发到这里）负责「唤醒」：角标 +1 + toast 轻提示；
 *   来自「用户此刻正看着的会话」的推送例外：结果已在聊天区内联展示，直接静默置为已读。
 *   「正看着」= 该会话是当前聊天会话，且聊天页签在前台、应用窗口可见——切到其他页签、
 *   窗口最小化或隐藏到托盘时照常提醒，否则人不在聊天区就永远收不到任务完成通知；
 * - 单条已读 / 全部已读走 REST，成功后同步本地状态与角标（已读条目随即从列表移除）。
 *   后端历史不动：已读仅是展示过滤，刷新列表仍可拉全量（拉取固定 unreadOnly）。
 *
 * 托盘菜单双向同步：
 * - 应用 → 托盘：上述每次变更都通过 window.Crew.traySetNotifications 推送
 *   未读摘要（未读数 + 最近 5 条未读，由单一数据源切片派生），主进程据此重建托盘菜单；
 * - 托盘 → 应用：托盘菜单点击通知 / 全部标为已读经 preload 事件回到这里，
 *   复用 openNotification / handleMarkAllRead，与面板点击行为一致；
 * - 新推送到达时额外调用 markSystemTrayNotification() 切换托盘图标态。
 */
import { notificationApi, type BackendNotification } from '../backend-client';
import type { TrayNotificationSummary } from '../../shared/types';
import { showToast } from '../components/overlays';
import { renderMarkdownHtml } from '../markdown';
import { openSessionInChat } from './chat-controller';
import { markSystemTrayNotification } from './system-tray';
import { uiStore } from '../stores/ui-store';
import { sessionStore } from '../stores/session-store';
import { relativeTime } from './work/time';

const PANEL_MAX_HEIGHT = 480;
const PANEL_VIEWPORT_GAP = 8;
/** 本地未读列表与 REST 拉取共用的大小上限；超出部分以后端 unread_count 体现。 */
const UNREAD_LIST_LIMIT = 50;
/** 托盘菜单展示的最近未读条数上限。 */
const TRAY_MENU_MAX_ITEMS = 5;

/** 来源标识 → 中文标签；未登记的来源原样展示。 */
const SOURCE_LABELS: Record<string, string> = {
  cron: '定时任务',
  tasks: '任务',
  approval: '审批',
  companion: '同伴',
  followup: '追问',
  plan: '计划',
};

let bound = false;
let panel: HTMLDivElement | null = null;
let panelOpen = false;
let loading = false;
/** 未读条目单一数据源：推送时增补，刷新时整体替换，面板与托盘都从这里取数。 */
let notifications: BackendNotification[] = [];
let unreadCount = 0;
/** 本次运行已见过的推送 id：WS 重连 replay 等场景下去重，避免角标重复 +1。 */
const seenPushIds = new Set<string>();
let onDocumentPointerDown: ((event: MouseEvent) => void) | null = null;
let onDocumentKeyDown: ((event: KeyboardEvent) => void) | null = null;

function bellButton(): HTMLButtonElement | null {
  return document.getElementById('notification-bell-btn') as HTMLButtonElement | null;
}

function badgeElement(): HTMLElement | null {
  return document.getElementById('notification-badge');
}

function sourceLabel(source: string): string {
  return SOURCE_LABELS[source] ?? source;
}

function renderBadge(): void {
  const badge = badgeElement();
  if (!badge) return;
  badge.hidden = unreadCount <= 0;
  badge.textContent = unreadCount > 99 ? '99+' : String(unreadCount);
  const bell = bellButton();
  if (bell) {
    const label = unreadCount > 0 ? `通知（${unreadCount > 99 ? '99+' : unreadCount} 条未读）` : '通知';
    bell.title = label;
    bell.setAttribute('aria-label', label);
  }
}

/**
 * 通知状态 → 托盘菜单：每次变更（推送 / 已读 / 全部已读 / 初始加载）都推送
 * 最新摘要，保证托盘不会展示过期未读。窗口隐藏到托盘时 Renderer 仍在运行，无需特判。
 * bridge 缺失（preview / 测试）或主进程校验失败时静默降级，不影响通知中心主流程。
 */
function pushTraySummary(): void {
  const summary: TrayNotificationSummary = {
    unreadCount,
    items: notifications.slice(0, TRAY_MENU_MAX_ITEMS).map((item) => ({
      id: item.id,
      title: item.title,
      sourceLabel: item.source ? sourceLabel(item.source) : undefined,
    })),
  };
  const request = window.Crew?.traySetNotifications?.(summary);
  if (!request) return;
  void request.catch((error: unknown) => {
    console.warn('[notification-center] failed to sync tray menu:', error);
  });
}

function isValidNotification(value: unknown): value is BackendNotification {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as Partial<BackendNotification>;
  return typeof candidate.id === 'string' && typeof candidate.title === 'string';
}

/**
 * 推送是否属于「用户此刻正看着的会话」（这类结果已在聊天区内联展示，无需再走未读提醒）。
 * 三个条件缺一不可：会话是当前聊天会话 + 聊天页签在前台 + 应用窗口可见。
 * 只比对会话 id 会把「切去别的页签 / 窗口最小化」误判成正在看，通知被静默吞掉。
 */
function isViewingSessionPush(notification: BackendNotification): boolean {
  const sessionId = typeof notification.payload?.session_id === 'string'
    ? notification.payload.session_id.trim()
    : '';
  if (!sessionId || sessionId !== sessionStore.get().activeSessionId) return false;
  return uiStore.get().activeTab === 'chat' && document.visibilityState === 'visible';
}

/** WS notification 帧入口（chat-controller 分发）：角标 +1 + toast；面板开着则同步插入列表。 */
export function handleNotificationPush(notification: BackendNotification | undefined): void {
  if (!isValidNotification(notification)) return;
  // 用户此刻正看着的会话：完成/审批/追问卡片已在聊天区内联展示，
  // 角标/toast/托盘都属于噪音，直接静默置为已读，保持后端未读状态一致。
  if (isViewingSessionPush(notification)) {
    seenPushIds.add(notification.id);
    void notificationApi.markRead(notification.id).catch(() => {
      // 已读失败静默降级：下次刷新未读数时自然收敛。
    });
    return;
  }
  if (!seenPushIds.has(notification.id)) {
    seenPushIds.add(notification.id);
    unreadCount += 1;
    notifications.unshift(notification);
    if (notifications.length > UNREAD_LIST_LIMIT) notifications.length = UNREAD_LIST_LIMIT;
    if (panelOpen) renderList();
    renderBadge();
    pushTraySummary();
  }
  // 新推送到达时把菜单栏图标切到「有通知」态（点击托盘图标后由 system-tray 解除）。
  markSystemTrayNotification();
  showToast({ message: notification.title || '收到新通知' });
}

/** 未读列表的 REST 拉取。失败返回 null，由调用方决定提示策略。 */
async function fetchUnreadPage(): Promise<{ items: BackendNotification[]; unread: number } | null> {
  try {
    const result = await notificationApi.list({ limit: UNREAD_LIST_LIMIT, offset: 0, unreadOnly: true });
    return {
      items: Array.isArray(result?.notifications) ? result.notifications : [],
      unread: Math.max(0, Number(result?.unread_count) || 0),
    };
  } catch {
    return null;
  }
}

/** 用拉取结果整体替换未读单一数据源，并同步角标与托盘。 */
function applyUnreadPage(items: BackendNotification[], unread: number): void {
  notifications = items;
  unreadCount = unread;
  renderBadge();
  pushTraySummary();
}

/**
 * REST 拉取未读（面板只展示未读：历史已读条目留在后端，这里仅是展示过滤）。
 * quiet 用于启动时的静默兜底（后端未就绪时不打扰）；面板打开时给加载态与失败提示。
 */
async function refreshList(options: { quiet?: boolean } = {}): Promise<void> {
  if (!options.quiet) {
    loading = true;
    renderList();
  }
  const page = await fetchUnreadPage();
  if (!options.quiet) {
    loading = false;
    if (!page) {
      showToast({ message: '通知加载失败，请稍后重试', tone: 'danger' });
    }
    renderList();
  }
  if (page) applyUnreadPage(page.items, page.unread);
}

function createPanel(): HTMLDivElement {
  const element = document.createElement('div');
  element.className = 'mw-notification-panel';
  element.id = 'notification-panel';
  element.setAttribute('role', 'dialog');
  element.setAttribute('aria-label', '通知');
  element.hidden = true;
  document.body.append(element);
  return element;
}

function positionPanel(): void {
  if (!panel) return;
  const rect = bellButton()?.getBoundingClientRect();
  const left = rect ? rect.right + PANEL_VIEWPORT_GAP : PANEL_VIEWPORT_GAP;
  const top = rect ? rect.top : PANEL_VIEWPORT_GAP;
  const maxTop = Math.max(
    PANEL_VIEWPORT_GAP,
    window.innerHeight - PANEL_MAX_HEIGHT - PANEL_VIEWPORT_GAP,
  );
  // 定位值通过 token 命名空间交给 CSS（面板布局留在样式表里）。
  panel.style.setProperty('--mw-notification-panel-left', `${Math.max(PANEL_VIEWPORT_GAP, left)}px`);
  panel.style.setProperty('--mw-notification-panel-top', `${Math.min(top, maxTop)}px`);
}

function openPanel(): void {
  if (panelOpen) return;
  panelOpen = true;
  panel ??= createPanel();
  positionPanel();
  panel.hidden = false;
  renderPanel();
  void refreshList();
  onDocumentPointerDown = (event: MouseEvent) => {
    const target = event.target instanceof Element ? event.target : null;
    if (!target) return;
    if (panel?.contains(target) || bellButton()?.contains(target)) return;
    closePanel();
  };
  onDocumentKeyDown = (event: KeyboardEvent) => {
    if (event.key === 'Escape') closePanel();
  };
  document.addEventListener('mousedown', onDocumentPointerDown);
  document.addEventListener('keydown', onDocumentKeyDown);
}

function closePanel(): void {
  if (!panelOpen) return;
  panelOpen = false;
  if (panel) panel.hidden = true;
  if (onDocumentPointerDown) document.removeEventListener('mousedown', onDocumentPointerDown);
  if (onDocumentKeyDown) document.removeEventListener('keydown', onDocumentKeyDown);
  onDocumentPointerDown = null;
  onDocumentKeyDown = null;
}

function togglePanel(): void {
  if (panelOpen) closePanel();
  else openPanel();
}

/** 条目跳转：优先按 payload.session_id 切会话；审批类打开审批面板；无法跳转就只标记已读。 */
async function navigateToNotification(notification: BackendNotification): Promise<void> {
  const sessionId = typeof notification.payload?.session_id === 'string'
    ? notification.payload.session_id.trim()
    : '';
  if (sessionId) {
    await openSessionInChat(sessionId);
    return;
  }
  if (notification.source === 'approval') {
    window.dispatchEvent(new CustomEvent('security:approval-pending'));
  }
}

/** 本地单条已读：从未读列表移除（面板只展示未读），并同步托盘摘要与角标。 */
function applyLocalRead(id: string): void {
  notifications = notifications.filter((item) => item.id !== id);
  unreadCount = Math.max(0, unreadCount - 1);
  renderBadge();
  renderList();
  pushTraySummary();
}

/** 单条已读 + 跳转的公共路径：面板条目点击与托盘菜单点击共用。 */
async function openNotification(notification: BackendNotification): Promise<void> {
  if (notification.read_at === null) {
    try {
      await notificationApi.markRead(notification.id);
      applyLocalRead(notification.id);
    } catch (err) {
      showToast({ message: `标记已读失败：${(err as Error)?.message ?? err}`, tone: 'danger' });
    }
  }
  await navigateToNotification(notification);
}

async function handleItemClick(notification: BackendNotification): Promise<void> {
  closePanel();
  await openNotification(notification);
}

/** 托盘菜单点击通知：本地找不到时拉一次列表兜底，然后走与面板点击相同的已读 + 跳转。 */
async function handleTrayNotificationSelected(id: string): Promise<void> {
  let notification = notifications.find((item) => item.id === id);
  if (!notification) {
    const page = await fetchUnreadPage();
    if (page) {
      // 列表只存未读条目，兜底拉取同样限定未读，避免已读历史混入。
      applyUnreadPage(page.items, page.unread);
      notification = notifications.find((item) => item.id === id);
    }
  }
  if (!notification) return;
  await openNotification(notification);
}

async function handleMarkAllRead(): Promise<void> {
  try {
    await notificationApi.markAllRead();
    // 面板只展示未读：全部已读即清空本地列表（后端历史保留）。
    notifications = [];
    unreadCount = 0;
    renderBadge();
    renderList();
    pushTraySummary();
  } catch (err) {
    showToast({ message: `全部已读失败：${(err as Error)?.message ?? err}`, tone: 'danger' });
  }
}

function renderList(): void {
  const list = panel?.querySelector<HTMLElement>('.mw-notification-panel__list');
  if (!list) return;
  list.replaceChildren();
  if (loading) {
    const status = document.createElement('div');
    status.className = 'mw-notification-panel__empty';
    status.textContent = '加载中…';
    list.append(status);
    return;
  }
  if (notifications.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'mw-notification-panel__empty';
    empty.textContent = '没有未读通知';
    list.append(empty);
    return;
  }
  for (const item of notifications) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'mw-notification-item';
    button.dataset.notificationId = item.id;
    button.classList.toggle('is-unread', item.read_at === null);

    const head = document.createElement('span');
    head.className = 'mw-notification-item__head';
    const source = document.createElement('span');
    source.className = 'mw-notification-item__source';
    source.textContent = sourceLabel(item.source);
    const time = document.createElement('span');
    time.className = 'mw-notification-item__time';
    time.textContent = relativeTime(item.created_at);
    head.append(source, time);

    const title = document.createElement('span');
    title.className = 'mw-notification-item__title';
    title.textContent = item.title;

    button.append(head, title);
    if (item.body) {
      const body = document.createElement('div');
      body.className = 'mw-notification-item__body';
      // renderMarkdownHtml 内部 micromark escape + DOMPurify 消毒，覆盖推送内容的不可信输入。
      const template = document.createElement('template');
      template.innerHTML = renderMarkdownHtml(item.body);
      body.appendChild(template.content.cloneNode(true));
      button.append(body);
    }
    button.addEventListener('click', () => void handleItemClick(item));
    list.append(button);
  }
}

function renderPanel(): void {
  if (!panel) return;
  panel.replaceChildren();

  const header = document.createElement('div');
  header.className = 'mw-notification-panel__header';
  const title = document.createElement('strong');
  title.className = 'mw-notification-panel__title';
  title.textContent = '通知';

  const actions = document.createElement('div');
  actions.className = 'mw-notification-panel__actions';
  const markAllButton = document.createElement('button');
  markAllButton.type = 'button';
  markAllButton.className = 'mw-notification-panel__action';
  markAllButton.textContent = '全部已读';
  markAllButton.addEventListener('click', () => void handleMarkAllRead());
  actions.append(markAllButton);
  header.append(title, actions);

  const list = document.createElement('div');
  list.className = 'mw-notification-panel__list';
  list.style.setProperty('--mw-notification-panel-list-max-height', `${PANEL_MAX_HEIGHT - 48}px`);
  panel.append(header, list);
  renderList();
}

/** 装配入口：app 初始化时调用一次，返回 dispose。 */
export function bindNotificationCenter(): () => void {
  if (bound) return () => {};
  bound = true;
  const bell = bellButton();
  bell?.addEventListener('click', togglePanel);
  // 启动时静默拉一次未读：角标、面板初始列表与托盘摘要共用这份数据。
  void refreshList({ quiet: true });
  // 托盘 → 应用：点击托盘通知 / 全部标为已读，复用通知中心已有逻辑。
  const offTraySelected = window.Crew?.onTrayNotificationSelected?.(
    (id: string) => void handleTrayNotificationSelected(id),
  ) ?? (() => undefined);
  const offTrayMarkAllRead = window.Crew?.onTrayNotificationsMarkAllRead?.(
    () => void handleMarkAllRead(),
  ) ?? (() => undefined);
  return () => {
    bell?.removeEventListener('click', togglePanel);
    offTraySelected();
    offTrayMarkAllRead();
    closePanel();
    panel?.remove();
    panel = null;
    bound = false;
  };
}

/** 测试用：复位模块内部状态。 */
export function resetNotificationCenterForTest(): void {
  closePanel();
  panel?.remove();
  panel = null;
  notifications = [];
  unreadCount = 0;
  seenPushIds.clear();
  loading = false;
  bound = false;
}
