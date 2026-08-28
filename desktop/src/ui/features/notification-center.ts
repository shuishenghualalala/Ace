/**
 * 通知中心：左上角 brand 块内的铃铛按钮（#notification-bell-btn，由 application-shell 创建）
 * + 未读角标（#notification-badge）+ 点击弹出的通知面板。
 *
 * 数据流：
 * - 启动时 GET /api/notifications/unread-count 初始化角标；
 * - WS notification 帧（chat-controller 分发到这里）只负责「唤醒」：角标 +1 + toast 轻提示；
 * - 面板每次打开都走 REST 全量拉取，作为断线兜底；
 * - 单条已读 / 全部已读 / 清空走 REST，成功后同步本地状态与角标。
 */
import { notificationApi, type BackendNotification } from '../backend-client';
import { showToast } from '../components/overlays';
import { showConfirmDialog } from '../ui-feedback';
import { openSessionInChat } from './chat-controller';
import { relativeTime } from './work/time';

const PANEL_MAX_HEIGHT = 480;
const PANEL_VIEWPORT_GAP = 8;

/** 来源标识 → 中文标签；未登记的来源原样展示。 */
const SOURCE_LABELS: Record<string, string> = {
  cron: '定时任务',
  tasks: '任务',
  approval: '审批',
  companion: '同伴',
};

let bound = false;
let panel: HTMLDivElement | null = null;
let panelOpen = false;
let loading = false;
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

function isValidNotification(value: unknown): value is BackendNotification {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as Partial<BackendNotification>;
  return typeof candidate.id === 'string' && typeof candidate.title === 'string';
}

/** WS notification 帧入口（chat-controller 分发）：角标 +1 + toast；面板开着则同步插入列表。 */
export function handleNotificationPush(notification: BackendNotification | undefined): void {
  if (!isValidNotification(notification)) return;
  if (!seenPushIds.has(notification.id)) {
    seenPushIds.add(notification.id);
    unreadCount += 1;
    if (panelOpen) {
      notifications.unshift(notification);
      renderList();
    }
    renderBadge();
  }
  showToast({ message: notification.title || '收到新通知' });
}

async function refreshUnreadCount(): Promise<void> {
  try {
    const result = await notificationApi.unreadCount();
    unreadCount = Math.max(0, Number(result?.unread_count) || 0);
    renderBadge();
  } catch {
    // 后端未就绪或版本未支持时静默降级：角标保持隐藏，不影响主流程。
  }
}

async function refreshList(): Promise<void> {
  loading = true;
  renderList();
  try {
    const result = await notificationApi.list({ limit: 50, offset: 0 });
    notifications = Array.isArray(result?.notifications) ? result.notifications : [];
    unreadCount = Math.max(0, Number(result?.unread_count) || 0);
  } catch (err) {
    notifications = [];
    showToast({ message: `通知加载失败：${(err as Error)?.message ?? err}`, tone: 'danger' });
  } finally {
    loading = false;
    renderBadge();
    renderList();
  }
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
  panel.style.left = `${Math.max(PANEL_VIEWPORT_GAP, left)}px`;
  panel.style.top = `${Math.min(top, maxTop)}px`;
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

async function handleItemClick(notification: BackendNotification): Promise<void> {
  closePanel();
  if (notification.read_at === null) {
    try {
      await notificationApi.markRead(notification.id);
      notification.read_at = Date.now() / 1000;
      unreadCount = Math.max(0, unreadCount - 1);
      renderBadge();
    } catch (err) {
      showToast({ message: `标记已读失败：${(err as Error)?.message ?? err}`, tone: 'danger' });
    }
  }
  await navigateToNotification(notification);
}

async function handleMarkAllRead(): Promise<void> {
  try {
    await notificationApi.markAllRead();
    const now = Date.now() / 1000;
    for (const item of notifications) {
      if (item.read_at === null) item.read_at = now;
    }
    unreadCount = 0;
    renderBadge();
    renderList();
  } catch (err) {
    showToast({ message: `全部已读失败：${(err as Error)?.message ?? err}`, tone: 'danger' });
  }
}

async function handleClearAll(): Promise<void> {
  const confirmed = await showConfirmDialog({
    title: '清空通知',
    message: '确定要清空全部通知吗？此操作不可恢复。',
    confirmText: '清空',
  });
  if (!confirmed) return;
  try {
    await notificationApi.clear();
    notifications = [];
    unreadCount = 0;
    renderBadge();
    renderList();
  } catch (err) {
    showToast({ message: `清空通知失败：${(err as Error)?.message ?? err}`, tone: 'danger' });
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
    empty.textContent = '暂无通知';
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
      const body = document.createElement('span');
      body.className = 'mw-notification-item__body';
      body.textContent = item.body;
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
  const clearButton = document.createElement('button');
  clearButton.type = 'button';
  clearButton.className = 'mw-notification-panel__action mw-notification-panel__action--danger';
  clearButton.textContent = '清空';
  clearButton.addEventListener('click', () => void handleClearAll());
  actions.append(markAllButton, clearButton);
  header.append(title, actions);

  const list = document.createElement('div');
  list.className = 'mw-notification-panel__list';
  list.style.maxHeight = `${PANEL_MAX_HEIGHT - 48}px`;
  panel.append(header, list);
  renderList();
}

/** 装配入口：app 初始化时调用一次，返回 dispose。 */
export function bindNotificationCenter(): () => void {
  if (bound) return () => {};
  bound = true;
  const bell = bellButton();
  bell?.addEventListener('click', togglePanel);
  void refreshUnreadCount();
  return () => {
    bell?.removeEventListener('click', togglePanel);
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
