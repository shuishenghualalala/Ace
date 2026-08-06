/**

 * 工作空间树 + 会话历史：/api/workspaces + /api/sessions。

 *

 * 侧栏分为「项目」（各工作空间下的对话，可折叠）与「对话」（默认工作空间会话）。

 */



import {
  backendApi,
  type BackendSession,
  type SessionAgentBinding,
  type Workspace,
} from '../backend-client';
import { isChannelSessionId, type ChannelSessionGroup } from './channel-sessions';
import { isSessionVisibleWithExternalAgentsFlag } from './external-agents-feature';
import { applySessionModelBinding, activeComposerModelId, mergeSessionModelsFromBackend, modelLabelForId, syncSessionModelUi } from './session-model';

import { sessionStatusClass } from '../chat-render';

import type { SessionStatus } from '../chat-render';

import { sessionStore } from '../stores/session-store';

/** 历史拉取失败状态（不写到 store，避免影响 Proxy 消费者）。
 *  这里只记录最近一次拉取是否失败及失败原因，数据数组保持原状；
 *  renderWorkspaceHistory 据此区分加载失败与正常空态。 */
const historyLoadError: { workspaces: string | null; sessions: string | null } = {
  workspaces: null,
  sessions: null,
};
export function getHistoryLoadError(): { workspaces: string | null; sessions: string | null } {
  return historyLoadError;
}
export function clearHistoryLoadError(kind: 'workspaces' | 'sessions' | 'all' = 'all'): void {
  if (kind === 'all') {
    historyLoadError.workspaces = null;
    historyLoadError.sessions = null;
  } else {
    historyLoadError[kind] = null;
  }
}

import {

  $,

  ensureSessionMessages,

  escapeHtml,

  isBusySession,

  newSessionId,

  notify,


  removeSessionState,

  removeSubscribedSession,

  replaceSessionMessages,


  setActiveExternalTeamForSession,

  setActiveSessionId,

  setCurrentWorkspaceId,

  setExpandedWorkspace,

  setExpandedChannel,

  setSessionStatus,

  setWsShowAll,

  state,

  type SessionRow,

  type TabKey,

} from '../state';

import { openSessionActionsMenu, togglePin } from './session-actions';
import { showConfirmDialog } from '../ui-feedback';



export type OpenSessionFn = (sessionId: string) => void | Promise<void>;

export type RefreshSessionsFn = () => Promise<void>;



const PROJECTS_COLLAPSED_KEY = 'crew.historyProjectsCollapsed';

const CONVERSATIONS_COLLAPSED_KEY = 'crew.historyConversationsCollapsed';

const CHANNELS_COLLAPSED_KEY = 'crew.historyChannelsCollapsed';



function formatTime(ts: number): string {

  const diff = Date.now() / 1000 - ts;

  if (diff < 60) return '刚刚';

  if (diff < 3600) return `${Math.max(1, Math.floor(diff / 60))}分`;

  if (diff < 86400) return `${Math.floor(diff / 3600)}小时`;

  if (diff < 7 * 86400) return `${Math.floor(diff / 86400)}天`;

  if (diff < 30 * 86400) return `${Math.floor(diff / (7 * 86400))}周`;

  return `${Math.max(1, Math.floor(diff / (30 * 86400)))}月`;

}



function isSectionCollapsed(key: string): boolean {

  return localStorage.getItem(key) === 'true';

}



function toggleSectionCollapsed(key: string): boolean {

  const next = !isSectionCollapsed(key);

  localStorage.setItem(key, String(next));

  return next;

}



export function isWorkspaceHidden(ws: Pick<Workspace, 'id' | 'hidden'>): boolean {
  return ws.id !== 'default' && !!ws.hidden;
}



export function visibleProjectWorkspaces(): Workspace[] {
  const list = state.workspaces.length ? state.workspaces : [];
  return list.filter((ws) => ws.id !== 'default' && !isWorkspaceHidden(ws));
}



export function workspaceLabel(workspaceId: string): string {
  if (workspaceId === 'default') return '对话';
  const ws = state.workspaces.find((w) => w.id === workspaceId);
  return ws?.name || workspaceId;
}



export function isSessionInHiddenWorkspace(session: { workspaceId: string }): boolean {
  if (session.workspaceId === 'default') return false;
  const ws = state.workspaces.find((w) => w.id === session.workspaceId);
  return !!ws && isWorkspaceHidden(ws);
}



export function sessionsForManageList(): (typeof state.sessions) {
  return state.sessions.filter((s) => !isSessionInHiddenWorkspace(s));
}



function sessionStatusTrailingInner(status: SessionStatus | undefined, sessionId: string, updatedAt: number): string {
  if (status === 'running' || status === 'queued') {
    return `<span class="history-item-status-spinner history-item-status-spinner--${status}" title="${status === 'queued' ? '排队中' : '进行中'}" aria-hidden="true"></span>`;
  }
  if (status === 'error') {
    return '<span class="history-item-status-dot history-item-status-dot--error" title="出错" aria-hidden="true"></span>';
  }
  if (sessionStore.get().unreadCompletedSessions.has(sessionId)) {
    return '<span class="history-item-status-dot history-item-status-dot--unread" title="已完成，尚未查看" aria-hidden="true"></span>';
  }
  return `<span class="history-item-time">${formatTime(updatedAt / 1000)}</span>`;
}

function desiredTrailingSpinnerClass(status: SessionStatus | undefined): string | null {
  if (status === 'running') return 'history-item-status-spinner--running';
  if (status === 'queued') return 'history-item-status-spinner--queued';
  return null;
}

/** 同步行尾槽位：时间 / 运行 spinner / 错误点 / 未读绿点（互斥，占原时间位）。 */
function syncSessionRowTrailing(node: HTMLElement, s: SessionRow, statusOverride?: SessionStatus): void {
  const status = statusOverride ?? (state.sessionStatuses[s.id] as SessionStatus | undefined);
  const main = node.querySelector('.history-item-main');
  if (!main) return;
  // 迁移：旧版 leading 槽位残留
  main.querySelector(':scope > .history-item-status-spinner, :scope > .history-item-status-dot')?.remove();
  let trailing = main.querySelector('.history-item-trailing') as HTMLElement | null;
  if (!trailing) {
    trailing = document.createElement('span');
    trailing.className = 'history-item-trailing';
    main.appendChild(trailing);
  }
  const spinnerClass = desiredTrailingSpinnerClass(status);
  const oldSpinner = trailing.querySelector('.history-item-status-spinner');
  if (spinnerClass && oldSpinner?.classList.contains(spinnerClass)) {
    trailing.querySelector('.history-item-time')?.remove();
    trailing.querySelector('.history-item-status-dot')?.remove();
    return;
  }
  trailing.innerHTML = sessionStatusTrailingInner(status, s.id, s.updatedAt);
}

/** 局部更新某一行会话的状态表征（status class + 行尾槽位），不重建整树。
 *  治本 spinner 抽搐：流式期间 status 保持 running 时由 setSessionStatus 短路拦截，
 *  本函数不被调用；只在 status 真正变化时才 patch 对应行，spinner 元素复用、CSS 动画连续。
 *  若该行尚未渲染（如新建会话首帧早于侧栏渲染），本函数为 no-op，等下次 renderWorkspaceHistory 按 state 补齐。 */
export function patchSessionRowStatus(sessionId: string, status: SessionStatus): void {
  if (typeof document === 'undefined') return;
  const list = document.getElementById('history-list');
  if (!list) return;
  const row = list.querySelector<HTMLElement>(`[data-session-id="${cssSelectorEscape(sessionId)}"]`);
  if (!row) return;
  row.classList.remove('history-item--running', 'history-item--queued', 'history-item--error');
  const cls = sessionStatusClass(status);
  if (cls) row.classList.add(cls.trim());
  const session = state.sessions.find((item) => item.id === sessionId);
  if (session) {
    syncSessionRowTrailing(row, session, status);
  } else {
    const trailing = row.querySelector('.history-item-trailing');
    if (trailing) {
      trailing.innerHTML = sessionStatusTrailingInner(status, sessionId, Date.now());
    }
  }
}

/** 未读完成标记变化时局部刷新行尾（绿点 ↔ 时间）。 */
function patchSessionRowUnread(sessionId: string): void {
  if (typeof document === 'undefined') return;
  const list = document.getElementById('history-list');
  if (!list) return;
  const row = list.querySelector<HTMLElement>(`[data-session-id="${cssSelectorEscape(sessionId)}"]`);
  if (!row) return;
  const session = state.sessions.find((item) => item.id === sessionId);
  if (!session) return;
  const status = state.sessionStatuses[sessionId] as SessionStatus | undefined;
  if (status === 'running' || status === 'queued' || status === 'error') return;
  syncSessionRowTrailing(row, session);
}

/** CSS 选择器转义：sessionId 可能含特殊字符，querySelector 前需转义。 */
function cssSelectorEscape(value: string): string {
  if (typeof (window as unknown as { CSS?: { escape?: (v: string) => string } }).CSS?.escape === 'function') {
    return (window as unknown as { CSS: { escape: (v: string) => string } }).CSS.escape(value);
  }
  return value.replace(/["\\]/g, '\\$&');
}

// ---------- sessionStatuses 状态隔离订阅 ----------
// 侧栏行状态 UI 作为 sessionStatuses 的纯函数：任何来源（setStatusWithUi、
// resumeSessionGeneration、syncSessionLiveFromBackend 等）写入 status 变化时，
// 通过订阅 diff 局部 patch 对应行，统一收口，避免每条路径各自触发整树渲染。
let statusSubInited = false;
let prevStatuses: Record<string, SessionStatus> = {};
let prevUnread: Set<string> = new Set();

function initSessionStatusSubscriber(): void {
  if (statusSubInited) return;
  statusSubInited = true;
  prevStatuses = { ...sessionStore.get().sessionStatuses };
  prevUnread = new Set(sessionStore.get().unreadCompletedSessions);
  sessionStore.subscribe((next) => {
    const nextStatuses = next.sessionStatuses;
    for (const sid of Object.keys(nextStatuses)) {
      if (nextStatuses[sid] !== prevStatuses[sid]) {
        patchSessionRowStatus(sid, nextStatuses[sid] ?? 'idle');
      }
    }
    for (const sid of Object.keys(prevStatuses)) {
      if (!(sid in nextStatuses) && prevStatuses[sid] && prevStatuses[sid] !== 'idle') {
        patchSessionRowStatus(sid, 'idle');
      }
    }
    prevStatuses = { ...nextStatuses };

    const nextUnread = next.unreadCompletedSessions;
    for (const sid of nextUnread) {
      if (!prevUnread.has(sid)) patchSessionRowUnread(sid);
    }
    for (const sid of prevUnread) {
      if (!nextUnread.has(sid)) patchSessionRowUnread(sid);
    }
    prevUnread = new Set(nextUnread);
  });
}



export function isPlaceholderSessionTitle(title: string): boolean {
  const t = title.trim();
  return !t || t === '新会话' || t === '新对话' || t === '新站点' || t === '新灵感';
}



export function syncSessionsFromBackend(rows: BackendSession[]): void {

  state.backendSessions = rows;
  mergeSessionModelsFromBackend(rows);

  const prevById = new Map(state.sessions.map((s) => [s.id, s]));

  state.sessions = rows.map((row) => {

    const prev = prevById.get(row.session_id);

    const backendTitle = row.title?.trim() || '新会话';

    let title = backendTitle;

    // 占位标题保留前端已有标题（避免刚收到的摘要标题被后端列表的占位冲掉）。
    if (isPlaceholderSessionTitle(backendTitle) && prev && !isPlaceholderSessionTitle(prev.title)) {

      title = prev.title;

    }
    // 即便后端列表回传了非占位标题，只要前端已确认这是后端摘要（session_title chunk），
    // 仍以前端为准——后端可能在摘要生成前的中间态回传了截断的用户原话。
    if (prev?.titleFromSummary && prev.title) {

      title = prev.title;

    }

    // 工作空间归属在创建时确定（草稿/活跃/进行中的会话以前端为准，避免后端中间态把
    // test 会话漂到 default）。其余会话才采纳后端 row.workspace_id。
    const frontendAuthoritative =
      isDraftSession(row.session_id) ||
      row.session_id === state.activeSessionId ||
      isBusySession(row.session_id);

    const workspaceId =
      isDraftSession(row.session_id)
        ? composerWorkspaceId()
        : frontendAuthoritative && prev?.workspaceId
          ? prev.workspaceId
          : row.workspace_id;

    const agentLabel = row.agent_label
      ? {
          name: row.agent_label.name || '',
          provider: row.agent_label.provider || '',
          display_badge: row.agent_label.display_badge || '?',
          ...(row.agent_label.model ? { model: row.agent_label.model } : {}),
        }
      : prev?.agentLabel;
    const agentBinding = row.agent_binding ?? prev?.agentBinding;
    const provider = String(agentLabel?.provider || '').toLowerCase();
    const isExternalAgent = agentBinding
      ? agentBinding.kind === 'external_agent'
      : Boolean(
          provider
          && provider !== 'crew'
          && provider !== 'builtin'
          && provider !== 'client'
          && provider !== 'team',
        );
    const modelLabel = isExternalAgent
      ? agentLabel?.model || prev?.modelLabel
      : row.model_label || prev?.modelLabel;

    return {

    id: row.session_id,

    title,

    updatedAt: row.updated_at * 1000,

    preview: prev?.preview || '',

    badge: workspaceId === 'default' ? '主智能体' : '工作空间',

    workspaceId,

    titleFromSummary: prev?.titleFromSummary ?? false,

    archived: row.archived === true,

    pinned: row.pinned === true,

    ...(agentLabel ? { agentLabel } : {}),

    ...(agentBinding ? { agentBinding } : {}),

    ...(modelLabel ? { modelLabel } : {}),

  };

  });

}



const FOLDER_ICON = `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/></svg>`;

/** 折叠箭头：默认朝右，展开时 CSS rotate(90deg) 朝下（与 composer-chip chevron 同路径）。 */
const CHEVRON_RIGHT_ICON = `<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg>`;

const MORE_ICON = `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="1.5"/><circle cx="19" cy="12" r="1.5"/><circle cx="5" cy="12" r="1.5"/></svg>`;

const NEW_CHAT_ICON = `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>`;

const CONVERSATION_ICON = `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>`;

function sessionAgentProvider(s: SessionRow): string {
  return String(s.agentLabel?.provider || '').trim().toLowerCase();
}

function sessionLeadingInner(s: SessionRow): string {
  const provider = sessionAgentProvider(s);
  if (provider === 'sites') {
    return '<span class="session__sites-logo" data-sites-logo aria-hidden="true"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="3" width="15" height="14" rx="2.25"/><path d="M2.5 7h15M6 11h5M6 14h8"/></svg></span>';
  }
  if (provider === 'team') {
    return '<span class="session__team-logo" aria-hidden="true"><i></i><i></i></span>';
  }
  if (provider && provider !== 'crew' && provider !== 'builtin' && provider !== 'client') {
    return `<span class="session__agent-badge session__agent-badge--external" aria-hidden="true">${escapeHtml(s.agentLabel?.display_badge || '?')}</span>`;
  }
  return CONVERSATION_ICON;
}

function matchesFilter(text: string, filter: string): boolean {

  return text.toLowerCase().includes(filter);

}

/** 会话列表统一排序：pinned 优先，再按 updatedAt 倒序。
 *  与后端 list_sessions 的 ORDER BY pinned DESC, updated_at DESC 对齐，
 *  保证侧栏初次渲染与后端水合顺序一致。 */
function sessionListComparator<T extends { pinned?: boolean; updatedAt: number }>(a: T, b: T): number {
  const pa = a.pinned ? 1 : 0;
  const pb = b.pinned ? 1 : 0;
  if (pa !== pb) return pb - pa;
  return b.updatedAt - a.updatedAt;
}



function createChevronRightIcon(): SVGSVGElement {
  const namespace = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(namespace, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', '13');
  svg.setAttribute('height', '13');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '2.25');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS(namespace, 'path');
  path.setAttribute('d', 'm9 18 6-6-6-6');
  svg.append(path);
  return svg;
}

function createSectionHeader(label: string, storageKey: string): HTMLButtonElement {
  const collapsed = isSectionCollapsed(storageKey);
  const header = document.createElement('button');
  header.type = 'button';
  header.className = `history-section-header${collapsed ? ' collapsed' : ''}`;
  header.dataset.sectionToggle = storageKey;
  header.setAttribute('aria-expanded', collapsed ? 'false' : 'true');

  const title = document.createElement('span');
  title.className = 'history-section-title';
  title.textContent = label;
  const caret = document.createElement('span');
  caret.className = 'history-section-caret';
  caret.setAttribute('aria-hidden', 'true');
  caret.append(createChevronRightIcon());
  header.append(title, caret);
  return header;
}

function createHistorySection(
  modifier: string,
  label: string,
  storageKey: string,
  bodyClass: string,
): HTMLElement {
  const section = document.createElement('div');
  section.className = `history-section history-section--${modifier}`;
  const body = document.createElement('div');
  body.className = bodyClass;
  section.append(createSectionHeader(label, storageKey), body);
  return section;
}



// ---------- 行级 reconciler：按 sessionId/workspaceId 复用 DOM ----------
// 治本 spinner 抽搐：整树 innerHTML 重建会把所有行（含后台 running 行的 spinner）销毁重建，
// CSS @keyframes 重启。reconciler 按 key 复用节点，只更新变化字段 + insertBefore 重排，
// 后台 running 行的 spinner 元素身份不变，动画连续。事件改委托，复用节点无需重绑。

const HISTORY_COLLAPSED_LIMIT = 10;

/** 「对话」分区（默认工作空间）默认渲染条数上限。
 *  导出供测试断言封顶行为，避免测试里硬编码魔数、改了上限就静默失配。 */
export const CONVERSATIONS_LIMIT = 10;

/** 当前 openSession 的最新引用，供事件委托 handler 使用（每次 render 时更新）。 */
let latestOpenSession: OpenSessionFn = () => {};

const SESSION_MENU_ICON = `<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="19" cy="12" r="1.5"/></svg>`;

/** 由 sessionRowHtml 模板构造一个新行节点。 */
function createSessionRow(s: SessionRow): HTMLElement {
  const tpl = document.createElement('template');
  tpl.innerHTML = sessionRowHtml(s).trim();
  return tpl.content.firstElementChild as HTMLElement;
}

/** 就地更新已有行节点的可变字段（active / status class / 行尾槽位 / title），
 *  不重建节点，保留 spinner 元素身份。 */
function updateSessionRow(node: HTMLElement, s: SessionRow): void {
  const status = state.sessionStatuses[s.id] as SessionStatus | undefined;
  node.classList.toggle('active', s.id === state.activeSessionId);
  node.classList.toggle('history-item--pinned', !!s.pinned);
  node.classList.remove('history-item--running', 'history-item--queued', 'history-item--error');
  const cls = sessionStatusClass(status);
  if (cls) node.classList.add(cls.trim());
  syncSessionRowTrailing(node, s);
  const main = node.querySelector('.history-item-main');
  if (main) main.setAttribute('title', s.title);
  const titleEl = node.querySelector('.history-item-title');
  if (titleEl) titleEl.textContent = s.title;
  const leading = node.querySelector('.history-item-leading');
  const leadingInner = sessionLeadingInner(s);
  if (leading && leading.innerHTML !== leadingInner) {
    leading.innerHTML = leadingInner;
  }
}

/** 在一个会话容器（.project-sessions 或 .conversations-list）内按 desired 顺序回收行节点。
 *  - 已存在且仍在 desired 中的行：updateSessionRow 就地更新 + insertBefore 重排
 *  - 不在 desired 中的旧行：remove
 *  - 新增的行：createSessionRow
 *  - 空列表：放占位「暂无对话」
 *  - showExpandLink：在末尾放「展开显示」按钮 */
function reconcileSessionContainer(
  container: HTMLElement,
  desired: SessionRow[],
  showExpandLink: boolean,
  wsId: string,
): void {
  const existing = new Map<string, HTMLElement>();
  for (const child of Array.from(container.children) as HTMLElement[]) {
    const sid = child.getAttribute('data-session-id');
    if (sid) existing.set(sid, child);
    else child.remove(); // 清掉旧的占位/展开链接，下面按需重建
  }
  const desiredIds = new Set(desired.map((s) => s.id));
  for (const [sid, el] of existing) {
    if (!desiredIds.has(sid)) {
      el.remove();
      existing.delete(sid);
    }
  }
  if (desired.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'history-empty history-empty--inline';
    // 加载失败时显示明确文案和重试按钮；正常空态显示「暂无对话」。
    if (historyLoadError.sessions) {
      const errBox = document.createElement('div');
      errBox.className = 'history-empty history-empty--error';
      const msg = document.createElement('div');
      msg.className = 'history-empty__msg';
      msg.textContent = `加载失败：${historyLoadError.sessions}`;
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.className = 'history-empty__retry';
      retry.textContent = '重试';
      retry.onclick = () => { void loadSessionsList().then(() => renderWorkspaceHistory(latestOpenSession)); };
      errBox.append(msg, retry);
      container.appendChild(errBox);
    } else {
      empty.textContent = '暂无对话';
      container.appendChild(empty);
    }
    return;
  }
  let ref: ChildNode | null = container.firstChild;
  for (const s of desired) {
    let node = existing.get(s.id);
    if (!node || !node.isConnected) {
      node = createSessionRow(s);
      existing.set(s.id, node);
    } else {
      updateSessionRow(node, s);
    }
    if (ref === node) {
      ref = node.nextSibling;
    } else {
      container.insertBefore(node, ref);
    }
  }
  if (showExpandLink) {
    const link = document.createElement('button');
    link.type = 'button';
    link.className = 'ws-block__expand';
    link.setAttribute('data-ws-show-all', wsId);
    link.textContent = '查看更多';
    container.appendChild(link);
  }
  if (state.wsShowAll?.[wsId]) {
    const collapse = document.createElement('button');
    collapse.type = 'button';
    collapse.className = 'ws-block__collapse';
    collapse.setAttribute('data-ws-collapse', wsId);
    collapse.textContent = '收起';
    container.appendChild(collapse);
  }
}

/** 由模板构造一个空的项目块骨架（含 .project-row 与操作按钮，不含 .project-sessions）。 */
function createProjectBlock(ws: Workspace): HTMLElement {
  const tpl = document.createElement('template');
  tpl.innerHTML = `
    <section class="project-block" data-project-id="${escapeHtml(ws.id)}">
      <div class="project-row" data-project-toggle="${escapeHtml(ws.id)}">
        <div class="project-label">
          <span class="project-icon" aria-hidden="true">${FOLDER_ICON}</span>
          <span class="project-name"></span>
          <span class="project-chevron" aria-hidden="true">${CHEVRON_RIGHT_ICON}</span>
        </div>
        <div class="project-actions">
          <button type="button" class="project-action-btn" data-project-settings="${escapeHtml(ws.id)}" title="设置" aria-label="设置">${MORE_ICON}</button>
          <button type="button" class="project-action-btn project-action-btn--new-chat" data-project-new="${escapeHtml(ws.id)}" title="在此工作空间新建对话" aria-label="新建对话">${NEW_CHAT_ICON}</button>
        </div>
      </div>
    </section>
  `.trim();
  return tpl.content.firstElementChild as HTMLElement;
}

/** 就地更新项目块头部（展开态、active-workspace、名称）。 */
function updateProjectBlock(block: HTMLElement, ws: Workspace, expanded: boolean, isActiveWorkspace: boolean): void {
  const row = block.querySelector('.project-row') as HTMLElement | null;
  if (!row) return;
  row.classList.toggle('expanded', expanded);
  row.classList.toggle('is-active-workspace', isActiveWorkspace);
  row.setAttribute('aria-expanded', expanded ? 'true' : 'false');
  const nameEl = row.querySelector('.project-name');
  if (nameEl) {
    nameEl.textContent = ws.name;
    nameEl.setAttribute('title', ws.root_path || ws.name);
  }
}

/** 在 .history-folders 内按 desired 项目顺序回收项目块，并回收各块内的会话行。 */
function reconcileProjects(container: HTMLElement, projects: Workspace[]): void {
  const filter = state.historyFilter.toLowerCase().trim();
  const existing = new Map<string, HTMLElement>();
  for (const child of Array.from(container.children) as HTMLElement[]) {
    const wid = child.getAttribute('data-project-id');
    if (wid) existing.set(wid, child);
    else child.remove(); // 旧的占位
  }
  const desiredIds = new Set(projects.map((w) => w.id));
  for (const [wid, el] of existing) {
    if (!desiredIds.has(wid)) {
      el.remove();
      existing.delete(wid);
    }
  }
  if (projects.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'history-empty history-empty--inline';
    // 同 loadSessionsList：区分「真空」vs「加载失败」。
    if (historyLoadError.workspaces) {
      const errBox = document.createElement('div');
      errBox.className = 'history-empty history-empty--error';
      const msg = document.createElement('div');
      msg.className = 'history-empty__msg';
      msg.textContent = `加载失败：${historyLoadError.workspaces}`;
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.className = 'history-empty__retry';
      retry.textContent = '重试';
      retry.onclick = () => { void loadWorkspaces().then(() => renderWorkspaceHistory(latestOpenSession)); };
      errBox.append(msg, retry);
      container.appendChild(errBox);
    } else {
      empty.textContent = '暂无工作空间';
      container.appendChild(empty);
    }
    return;
  }
  let ref: ChildNode | null = container.firstChild;
  for (const ws of projects) {
    let block = existing.get(ws.id);
    if (!block || !block.isConnected) {
      block = createProjectBlock(ws);
      existing.set(ws.id, block);
    }
    const projectMatches = !filter || matchesFilter(ws.name, filter) || matchesFilter(ws.id, filter);
    const sessions = state.sessions
      .filter((s) => s.workspaceId === ws.id)
      .filter((s) => !isChannelSessionId(s.id))
      .filter(isSessionVisibleWithExternalAgentsFlag)
      .filter((s) => !filter || matchesFilter(s.title, filter) || matchesFilter(s.id, filter))
      .sort(sessionListComparator);
    const expanded = !!(state.expandedWorkspaces[ws.id] !== false || (filter && (projectMatches || sessions.length > 0)));
    const showAll = state.wsShowAll?.[ws.id] === true;
    const visible = expanded && !showAll && sessions.length > HISTORY_COLLAPSED_LIMIT
      ? sessions.slice(0, HISTORY_COLLAPSED_LIMIT)
      : sessions;
    const showExpandLink = expanded && !showAll && sessions.length > HISTORY_COLLAPSED_LIMIT;
    const isActiveWorkspace = ws.id === state.currentWorkspaceId;
    updateProjectBlock(block, ws, expanded, isActiveWorkspace);
    let sessionsEl = block.querySelector('.project-sessions') as HTMLElement | null;
    if (expanded) {
      if (!sessionsEl) {
        sessionsEl = document.createElement('div');
        sessionsEl.className = 'project-sessions';
        block.appendChild(sessionsEl);
      }
      reconcileSessionContainer(sessionsEl, visible, showExpandLink, ws.id);
    } else if (sessionsEl) {
      sessionsEl.remove();
    }
    if (ref === block) {
      ref = block.nextSibling;
    } else {
      container.insertBefore(block, ref);
    }
  }
}

function createChannelBlock(group: ChannelSessionGroup): HTMLElement {
  const tpl = document.createElement('template');
  tpl.innerHTML = `
    <section class="project-block channel-folder-block" data-channel-id="${escapeHtml(group.platform)}">
      <div class="project-row" data-channel-toggle="${escapeHtml(group.platform)}">
        <div class="project-label">
          <span class="project-icon" aria-hidden="true">${FOLDER_ICON}</span>
          <span class="project-name"></span>
          <span class="project-chevron" aria-hidden="true">${CHEVRON_RIGHT_ICON}</span>
        </div>
      </div>
    </section>
  `.trim();
  return tpl.content.firstElementChild as HTMLElement;
}

function updateChannelBlock(block: HTMLElement, group: ChannelSessionGroup, expanded: boolean): void {
  const row = block.querySelector('.project-row') as HTMLElement | null;
  if (!row) return;
  row.classList.toggle('expanded', expanded);
  row.setAttribute('aria-expanded', expanded ? 'true' : 'false');
  const nameEl = row.querySelector('.project-name');
  if (nameEl) {
    nameEl.textContent = group.label;
    nameEl.setAttribute('title', group.platform);
  }
}

/** 渠道分区：按平台文件夹回收会话行（样式复用 project-block）。 */
function reconcileChannelFolders(container: HTMLElement, groups: ChannelSessionGroup[]): void {
  const filter = state.historyFilter.toLowerCase().trim();
  const existing = new Map<string, HTMLElement>();
  for (const child of Array.from(container.children) as HTMLElement[]) {
    const pid = child.getAttribute('data-channel-id');
    if (pid) existing.set(pid, child);
    else child.remove();
  }
  const desiredIds = new Set(groups.map((g) => g.platform));
  for (const [pid, el] of existing) {
    if (!desiredIds.has(pid)) {
      el.remove();
      existing.delete(pid);
    }
  }
  if (groups.length === 0) {
    return;
  }
  let ref: ChildNode | null = container.firstChild;
  for (const group of groups) {
    let block = existing.get(group.platform);
    if (!block || !block.isConnected) {
      block = createChannelBlock(group);
      existing.set(group.platform, block);
    }
    const groupMatches = !filter || matchesFilter(group.label, filter) || matchesFilter(group.platform, filter);
    const sessions = group.sessions
      .filter((s) => !filter || matchesFilter(s.title, filter) || matchesFilter(s.id, filter))
      .sort(sessionListComparator);
    const expanded = !!(state.channelExpanded[group.platform] !== false || (filter && (groupMatches || sessions.length > 0)));
    const showAll = state.wsShowAll?.[`channel:${group.platform}`] === true;
    const visible = expanded && !showAll && sessions.length > HISTORY_COLLAPSED_LIMIT
      ? sessions.slice(0, HISTORY_COLLAPSED_LIMIT)
      : sessions;
    const showExpandLink = expanded && !showAll && sessions.length > HISTORY_COLLAPSED_LIMIT;
    updateChannelBlock(block, group, expanded);
    let sessionsEl = block.querySelector('.project-sessions') as HTMLElement | null;
    if (expanded) {
      if (!sessionsEl) {
        sessionsEl = document.createElement('div');
        sessionsEl.className = 'project-sessions';
        block.appendChild(sessionsEl);
      }
      reconcileSessionContainer(sessionsEl, visible, showExpandLink, `channel:${group.platform}`);
    } else if (sessionsEl) {
      sessionsEl.remove();
    }
    if (ref === block) {
      ref = block.nextSibling;
    } else {
      container.insertBefore(block, ref);
    }
  }
}

/** 就地更新分区头（折叠态 class + aria）。 */
function updateSectionHeader(section: Element, storageKey: string): void {
  const header = section.querySelector('.history-section-header');
  if (!header) return;
  const collapsed = isSectionCollapsed(storageKey);
  header.classList.toggle('collapsed', collapsed);
  header.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
}

/** 确保 #history-list 的外层骨架（项目 / 渠道 / 对话分区）只构建一次。 */
function ensureHistorySkeleton(
  list: HTMLElement,
  projectsCollapsed: boolean,
  channelsCollapsed: boolean,
  conversationsCollapsed: boolean,
): { folders: HTMLElement; channelFolders: HTMLElement; conversations: HTMLElement } {
  let projectsSection = list.querySelector('.history-section--projects');
  let channelsSection = list.querySelector('.history-section--channels');
  let convSection = list.querySelector('.history-section--conversations');
  if (!projectsSection || !convSection) {
    projectsSection = createHistorySection(
      'projects', '项目', PROJECTS_COLLAPSED_KEY, 'history-folders',
    );
    channelsSection = createHistorySection(
      'channels', '渠道', CHANNELS_COLLAPSED_KEY, 'channel-folders',
    );
    convSection = createHistorySection(
      'conversations', '对话', CONVERSATIONS_COLLAPSED_KEY, 'conversations-list',
    );
    list.replaceChildren(projectsSection, channelsSection, convSection);
  } else if (!channelsSection) {
    channelsSection = createHistorySection(
      'channels', '渠道', CHANNELS_COLLAPSED_KEY, 'channel-folders',
    );
    convSection.before(channelsSection);
  } else {
    updateSectionHeader(projectsSection, PROJECTS_COLLAPSED_KEY);
    updateSectionHeader(channelsSection, CHANNELS_COLLAPSED_KEY);
    updateSectionHeader(convSection, CONVERSATIONS_COLLAPSED_KEY);
  }
  const folders = projectsSection.querySelector('.history-folders') as HTMLElement;
  const channelFolders = channelsSection!.querySelector('.channel-folders') as HTMLElement;
  const conversations = convSection.querySelector('.conversations-list') as HTMLElement;
  folders.classList.toggle('is-collapsed', projectsCollapsed);
  channelFolders.classList.toggle('is-collapsed', channelsCollapsed);
  conversations.classList.toggle('is-collapsed', conversationsCollapsed);
  return { folders, channelFolders, conversations };
}

/** 事件委托：在 #history-list 上一次性绑定 click，按 closest 选择器分发，
 *  复用节点无需重绑。绑定标记挂在元素自身，避免跨测试/重建列表时残留。 */
function bindHistoryListDelegation(list: HTMLElement): void {
  const marker = list as HTMLElement & { __historyDelegationBound?: boolean };
  if (marker.__historyDelegationBound) return;
  marker.__historyDelegationBound = true;
  list.addEventListener('click', (e: Event) => {
    const target = e.target as HTMLElement | null;
    if (!target) return;
    const menuBtn = target.closest('[data-session-menu]');
    if (menuBtn) {
      e.stopPropagation();
      const id = menuBtn.getAttribute('data-session-menu')!;
      openSessionActionsMenu(id, menuBtn as HTMLElement, refreshSessionsWrapper);
      return;
    }
    const newBtn = target.closest('[data-project-new]');
    if (newBtn) {
      e.stopPropagation();
      const wsId = newBtn.getAttribute('data-project-new')!;
      setCurrentWorkspaceId(wsId);
      createSessionInWorkspace(wsId, latestOpenSession);
      return;
    }
    const setBtn = target.closest('[data-project-settings]');
    if (setBtn) {
      e.stopPropagation();
      const wsId = setBtn.getAttribute('data-project-settings')!;
      const ws = state.workspaces.find((w) => w.id === wsId);
      if (ws) openWorkspaceModal(ws);
      return;
    }
    const showAllBtn = target.closest('[data-ws-show-all]');
    if (showAllBtn) {
      e.stopPropagation();
      const wsId = showAllBtn.getAttribute('data-ws-show-all')!;
      setWsShowAll(wsId, true);
      renderWorkspaceHistory(latestOpenSession);
      return;
    }
    const collapseBtn = target.closest('[data-ws-collapse]');
    if (collapseBtn) {
      e.stopPropagation();
      const wsId = collapseBtn.getAttribute('data-ws-collapse')!;
      setWsShowAll(wsId, false);
      renderWorkspaceHistory(latestOpenSession);
      return;
    }
    const projToggle = target.closest('[data-project-toggle]');
    if (projToggle) {
      const id = projToggle.getAttribute('data-project-toggle')!;
      setCurrentWorkspaceId(id);
      const currentlyExpanded = state.expandedWorkspaces[id] !== false;
      setExpandedWorkspace(id, !currentlyExpanded);
      renderWorkspaceHistory(latestOpenSession);
      return;
    }
    const chToggle = target.closest('[data-channel-toggle]');
    if (chToggle) {
      const platform = chToggle.getAttribute('data-channel-toggle')!;
      const currentlyExpanded = state.channelExpanded[platform] !== false;
      setExpandedChannel(platform, !currentlyExpanded);
      renderWorkspaceHistory(latestOpenSession);
      return;
    }
    const secToggle = target.closest('[data-section-toggle]');
    if (secToggle) {
      const key = secToggle.getAttribute('data-section-toggle')!;
      toggleSectionCollapsed(key);
      renderWorkspaceHistory(latestOpenSession);
      return;
    }
    const sessionRow = target.closest('.history-item[data-session-id]');
    if (sessionRow) {
      const id = sessionRow.getAttribute('data-session-id');
      if (id) void latestOpenSession(id);
      return;
    }
  });

  if (!(list as HTMLElement & { __sessionContextBound?: boolean }).__sessionContextBound) {
    (list as HTMLElement & { __sessionContextBound?: boolean }).__sessionContextBound = true;
    list.addEventListener('contextmenu', (e: Event) => {
      const ev = e as MouseEvent;
      const row = (ev.target as HTMLElement | null)?.closest('[data-session-id]');
      if (!row || (ev.target as HTMLElement).closest('[data-session-menu]')) return;
      const id = row.getAttribute('data-session-id');
      if (!id) return;
      ev.preventDefault();
      openSessionActionsMenu(id, row as HTMLElement, refreshSessionsWrapper);
    });
    list.addEventListener('click', (e: Event) => {
      const ev = e as MouseEvent;
      if (!ev.shiftKey) return;
      const row = (ev.target as HTMLElement | null)?.closest('[data-session-id]');
      if (!row || (ev.target as HTMLElement).closest('[data-session-menu]')) return;
      const id = row.getAttribute('data-session-id');
      if (!id) return;
      ev.preventDefault();
      ev.stopPropagation();
      const cur = !!state.sessions.find((s) => s.id === id)?.pinned;
      void togglePin(id, !cur, refreshSessionsWrapper);
    });
  }
}

export function renderWorkspaceHistory(openSession: OpenSessionFn): void {

  const list = $('#history-list');

  if (!list) return;

  // 首次渲染时挂载 sessionStatuses 订阅（仅一次），后续 status 变化走局部 patch。
  initSessionStatusSubscriber();

  const filter = state.historyFilter.toLowerCase().trim();



  const projects = visibleProjectWorkspaces().filter((ws) => {
    if (!filter) return true;
    return matchesFilter(ws.name, filter) || matchesFilter(ws.id, filter)
      || state.sessions.some((s) => isSessionVisibleWithExternalAgentsFlag(s)
        && s.workspaceId === ws.id
        && (matchesFilter(s.title, filter) || matchesFilter(s.id, filter)));
  });

  const projectsCollapsed = isSectionCollapsed(PROJECTS_COLLAPSED_KEY);

  const channelsCollapsed = isSectionCollapsed(CHANNELS_COLLAPSED_KEY);

  const conversationsCollapsed = isSectionCollapsed(CONVERSATIONS_COLLAPSED_KEY);



  latestOpenSession = openSession;

  const { folders, channelFolders, conversations: conversationsEl } = ensureHistorySkeleton(
    list,
    projectsCollapsed,
    channelsCollapsed,
    conversationsCollapsed,
  );

  // 项目分区：按 workspaceId 回收项目块，块内会话行按 sessionId 回收 + 重排
  reconcileProjects(folders, projects);

  const channelGroups = state.channelSessionGroups ?? [];
  const channelsSection = list.querySelector('.history-section--channels') as HTMLElement | null;
  if (channelsSection) {
    channelsSection.hidden = channelGroups.length === 0;
    if (channelGroups.length > 0) {
      reconcileChannelFolders(channelFolders, channelGroups);
    } else {
      channelFolders.innerHTML = '';
    }
  }

  // 对话分区：默认工作空间会话，按 sessionId 回收行 + 重排
  const conversations = state.sessions

    .filter((s) => s.workspaceId === 'default')

    .filter((s) => !isChannelSessionId(s.id))

    .filter(isSessionVisibleWithExternalAgentsFlag)

    .filter((s) => {

      if (!filter) return true;

      return matchesFilter(s.title, filter) || matchesFilter(s.id, filter);

    })

    .sort(sessionListComparator);

  // 封顶 + 展开显示：复用 wsShowAll 机制（key='default'，项目分区不会用到该 key）。
  // 避免数百条会话全量渲染拖慢侧栏；用户点「展开显示」后看全量。
  const convShowAll = state.wsShowAll?.['default'] === true;
  const convVisible = !convShowAll && conversations.length > CONVERSATIONS_LIMIT
    ? conversations.slice(0, CONVERSATIONS_LIMIT)
    : conversations;
  const convShowExpandLink = !convShowAll && conversations.length > CONVERSATIONS_LIMIT;
  reconcileSessionContainer(conversationsEl, convVisible, convShowExpandLink, 'default');

  // 事件委托（仅绑定一次，复用节点无需重绑）
  bindHistoryListDelegation(list);

}



function sessionRowHtml(s: (typeof state.sessions)[0]): string {
  const status = state.sessionStatuses[s.id] as SessionStatus | undefined;
  const pinnedClass = s.pinned ? ' history-item--pinned' : '';
  const statusCls = sessionStatusClass(status);
  const trailing = sessionStatusTrailingInner(status, s.id, s.updatedAt);
  return `
    <div class="history-item history-item--compact${pinnedClass}${s.id === state.activeSessionId ? ' active' : ''}${statusCls}" data-session-id="${escapeHtml(s.id)}">
      <button type="button" class="history-item-main" title="${escapeHtml(s.title)}">
        <span class="history-item-leading" aria-hidden="true">${sessionLeadingInner(s)}</span>
        <span class="history-item-title">${escapeHtml(s.title)}</span>
        <span class="history-item-trailing">${trailing}</span>
        <span class="history-item-menu-btn" data-session-menu="${escapeHtml(s.id)}" aria-label="会话操作" title="更多">${SESSION_MENU_ICON}</span>
      </button>
    </div>
  `;
}



let refreshSessionsWrapper: RefreshSessionsFn = async () => {};

let openSessionWrapper: OpenSessionFn = () => {};



let draftSession: {
  id: string;
  workspaceId: string;
  modelProfileId?: string;
  agentLabel?: NonNullable<SessionRow['agentLabel']>;
  agentBinding?: SessionAgentBinding;
  modelLabel?: string;
} | null = null;

export function getSessionAgentDisplay(sessionId: string | null | undefined): {
  agentLabel: SessionRow['agentLabel'] | undefined;
  agentBinding: SessionRow['agentBinding'] | undefined;
  modelLabel: SessionRow['modelLabel'] | undefined;
} | null {
  if (!sessionId) return null;
  const row = state.sessions.find((session) => session.id === sessionId);
  if (row) {
    return {
      agentLabel: row.agentLabel,
      agentBinding: row.agentBinding,
      modelLabel: row.modelLabel,
    };
  }
  if (draftSession?.id === sessionId) {
    return {
      agentLabel: draftSession.agentLabel,
      agentBinding: draftSession.agentBinding,
      modelLabel: draftSession.modelLabel,
    };
  }
  return null;
}

export function getDraftSessionModelId(): string {
  return draftSession?.modelProfileId || '';
}

export function setDraftSessionModelId(modelId: string): void {
  if (draftSession) draftSession.modelProfileId = modelId;
}

export function assignSessionAgentDisplay(
  sessionId: string,
  agentLabel: NonNullable<SessionRow['agentLabel']>,
  modelLabel = '',
  agentBinding?: SessionAgentBinding,
): void {
  if (!sessionId) return;
  if (draftSession?.id === sessionId) {
    draftSession.agentLabel = agentLabel;
    if (agentBinding) draftSession.agentBinding = agentBinding;
    if (modelLabel) draftSession.modelLabel = modelLabel;
  }
  state.sessions = state.sessions.map((session) => (
    session.id === sessionId
      ? {
          ...session,
          agentLabel,
          ...(agentBinding ? { agentBinding } : {}),
          ...(modelLabel ? { modelLabel } : {}),
        }
      : session
  ));
  if (modelLabel) {
    const messages = state.messages[sessionId] ?? [];
    if (messages.length > 0) {
      replaceSessionMessages(sessionId, messages.map((message) => ({ ...message, model: modelLabel })));
    }
  }
  renderWorkspaceHistory(latestOpenSession);
  window.dispatchEvent(new CustomEvent('session:agent-assigned', {
    detail: { sessionId, agentBinding },
  }));
}



export function setRefreshSessions(fn: RefreshSessionsFn): void {

  refreshSessionsWrapper = fn;

}



export function setOpenSessionForWorkspaces(fn: OpenSessionFn): void {

  openSessionWrapper = fn;

}



let setTabFn: (tab: TabKey) => void = () => {};

let renderChatFn: () => void = () => {};



export function setWorkspacesUiCallbacks(opts: {

  setTab: (tab: TabKey) => void;

  renderChat: () => void;

}): void {

  setTabFn = opts.setTab;

  renderChatFn = opts.renderChat;

}



function finishNewDraftUi(): void {

  renderChatFn();

  setTabFn('chat');

  window.dispatchEvent(new CustomEvent('workspace:context-changed'));

}



export function setComposerTargetWorkspace(workspaceId: string): void {
  if (state.activeSessionId && !isDraftSession(state.activeSessionId)) return;

  if (!state.activeSessionId) {
    ensureComposerDraftSession();
  }

  if (draftSession) draftSession.workspaceId = workspaceId;

  setCurrentWorkspaceId(workspaceId);
}

export function canSwitchComposerWorkspace(): boolean {
  const sid = state.activeSessionId;
  // 欢迎页：尚无会话 id，允许选空间
  if (!sid) return true;
  // 仅有「未发消息的草稿」可切换；已有对话或草稿里已有消息均锁定
  if (!isDraftSession(sid)) return false;
  return (state.messages[sid] ?? []).length === 0;
}

/**
 * 欢迎页 / Composer 操作前确保存在草稿会话（不落侧栏，首条消息后再 commit）。
 * 与 sendMessage 里 createSessionInWorkspace 语义一致。
 */
export function ensureComposerDraftSession(openSession: OpenSessionFn = openSessionWrapper): string {
  if (state.activeSessionId) return state.activeSessionId;
  return createSessionInWorkspace(composerWorkspaceId(), openSession);
}

export function composerWorkspaceId(): string {
  if (draftSession && draftSession.id === state.activeSessionId) return draftSession.workspaceId;
  const active = state.activeSessionId
    ? state.sessions.find((s) => s.id === state.activeSessionId)
    : null;
  if (active) return active.workspaceId || 'default';
  if (state.activeSessionId) return 'default';
  return state.currentWorkspaceId || 'default';
}

export function workspaceForSessionDispatch(sessionId: string): string {
  const row = state.sessions.find((s) => s.id === sessionId);
  if (row) return row.workspaceId || 'default';
  if (isDraftSession(sessionId)) return composerWorkspaceId();
  return 'default';
}



export async function setWorkspaceHidden(

  id: string,

  hidden: boolean,

  openSession: OpenSessionFn,

): Promise<void> {

  await backendApi.updateWorkspace(id, { hidden });

  await loadWorkspaces();

  if (hidden && state.currentWorkspaceId === id) {

    setCurrentWorkspaceId('default');

    window.dispatchEvent(new CustomEvent('workspace:context-changed'));

  }
  const active = state.activeSessionId
    ? state.sessions.find((s) => s.id === state.activeSessionId)
    : null;
  if (hidden && active?.workspaceId === id) {
    setActiveSessionId(null);
  }

  renderWorkspaceHistory(openSession);

}



export async function deleteWorkspaceById(id: string, openSession: OpenSessionFn): Promise<void> {

  const ws = state.workspaces.find((w) => w.id === id);

  const sessionCount = state.sessions.filter((s) => s.workspaceId === id).length;

  const confirmed = await showConfirmDialog({

    title: '删除工作空间',

    message: `确认删除「${ws?.name || id}」？其下 ${sessionCount} 条对话将一并删除且无法恢复。`,

    confirmText: '删除',

    cancelText: '取消',

  });

  if (!confirmed) return;

  await backendApi.deleteWorkspace(id);

  const ids = new Set(state.sessions.filter((s) => s.workspaceId === id).map((s) => s.id));

  ids.forEach((sid) => {

    removeSessionState(sid);

    state.socket?.unsubscribe([sid]);

    removeSubscribedSession(sid);

  });

  if (state.currentWorkspaceId === id) setCurrentWorkspaceId('default');

  if (state.activeSessionId && ids.has(state.activeSessionId)) setActiveSessionId(null);

  await loadWorkspaces();

  await refreshAllSessions();

  renderWorkspaceHistory(openSession);

  renderChatFn();

  window.dispatchEvent(new CustomEvent('workspace:context-changed'));

  notify('工作空间已删除');

}



export function discardDraft(): void {

  if (!draftSession) return;

  const { id } = draftSession;

  draftSession = null;

  removeSessionState(id);


  if (state.activeSessionId === id) setActiveSessionId(null);

}



export function isDraftSession(sessionId: string): boolean {

  return draftSession?.id === sessionId;

}



export function commitDraftSession(

  sessionId: string,

  title: string,

  preview: string,

  openSession: OpenSessionFn = openSessionWrapper,

): void {

  if (draftSession?.id !== sessionId) return;

  const committedDraft = draftSession;
  const workspaceId = committedDraft.workspaceId;

  draftSession = null;

  const existing = state.sessions.find((session) => session.id === sessionId);
  if (existing) {
    // 外部 Agent / Team 的配置接口会在首发前提前创建后端 Session。此时仍要沿用
    // 普通草稿的首次消息命名逻辑，不能因为已有一行“新会话”就跳过提交。
    if (isPlaceholderSessionTitle(existing.title)) existing.title = title || '新对话';
    existing.preview = preview || existing.preview || '新对话';
    existing.updatedAt = Date.now();
    existing.workspaceId = existing.workspaceId || workspaceId;
    if (committedDraft.agentLabel) existing.agentLabel = committedDraft.agentLabel;
    if (committedDraft.agentBinding) existing.agentBinding = committedDraft.agentBinding;
    if (committedDraft.modelLabel) existing.modelLabel = committedDraft.modelLabel;
    renderWorkspaceHistory(openSession);
    return;
  }

  state.sessions.unshift({

    id: sessionId,

    title: title || '新对话',

    updatedAt: Date.now(),

    preview: preview || '新对话',

    badge: workspaceId === 'default' ? '主智能体' : '工作空间',

    workspaceId,

    ...(committedDraft.agentLabel ? { agentLabel: committedDraft.agentLabel } : {}),

    ...(committedDraft.agentBinding ? { agentBinding: committedDraft.agentBinding } : {}),

    ...(committedDraft.modelLabel ? { modelLabel: committedDraft.modelLabel } : {}),

  });

  renderWorkspaceHistory(openSession);

}



export function createSessionInWorkspace(workspaceId: string, openSession: OpenSessionFn): string {


  const seedModel = activeComposerModelId() || state.config?.active_model_id || '';

  discardDraft();

  const id = newSessionId();

  draftSession = { id, workspaceId, ...(seedModel ? { modelProfileId: seedModel } : {}) };

  if (seedModel) {
    applySessionModelBinding(id, {
      model_profile_id: seedModel,
      model_label: modelLabelForId(seedModel),
    });
  }

  state.currentWorkspaceId = workspaceId;

  setActiveSessionId(id);

  ensureSessionMessages(id);

  setSessionStatus(id, 'idle');

  // 新会话从默认 Agent 状态开始，外援团队由用户按需重新选择。
  setActiveExternalTeamForSession(id, '');

  state.mode = 'agent';

  state.taskBoardOpen = false;

  setExpandedWorkspace(workspaceId, true);

  renderWorkspaceHistory(openSession);

  finishNewDraftUi();
  syncSessionModelUi();
  window.dispatchEvent(new CustomEvent('session:changed', { detail: { sessionId: id } }));

  return id;

}



function displayProjectPrompt(instructions: string | undefined, rootPath?: string): string {
  const raw = (instructions ?? '').trim();
  if (!raw) return '';
  if (raw.startsWith('当前工作目录：') || raw.startsWith('当前工作目录:')) return '';
  if (rootPath && raw === rootPath.trim()) return '';
  return raw;
}

/** 从本地路径取文件夹名，作为默认工作空间名称。 */
function folderBaseName(folderPath: string): string {
  const normalized = folderPath.replace(/\\/g, '/').replace(/\/+$/, '');
  const base = normalized.split('/').filter(Boolean).pop();
  return base?.trim() || '新工作空间';
}

function normalizeFolderPathForCompare(folderPath: string): string {
  return folderPath.replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase();
}

/**
 * 新建工作空间：先弹出系统文件夹选择器，再在后端创建工作空间并刷新侧栏。
 * 名称默认取所选目录名；若该目录已绑定则聚焦已有项目。
 */
export async function createWorkspaceFromFolderPicker(openSession: OpenSessionFn): Promise<void> {
  const selectFolder = window.Crew?.selectFolder;
  if (!selectFolder) {
    notify('请在桌面客户端中使用「新建工作空间」以选择本地文件夹');
    return;
  }
  const paths = await selectFolder();
  if (!paths?.length) return;
  const rootPath = paths[0]!.trim();
  if (!rootPath) return;

  const normalized = normalizeFolderPathForCompare(rootPath);
  const existing = state.workspaces.find((w) => {
    const rp = w.root_path?.trim();
    return rp && normalizeFolderPathForCompare(rp) === normalized;
  });
  if (existing) {
    setCurrentWorkspaceId(existing.id);
    setExpandedWorkspace(existing.id, true);
    renderWorkspaceHistory(openSession);
    window.dispatchEvent(new CustomEvent('workspace:context-changed'));
    notify(`该目录已是工作空间「${existing.name}」`);
    return;
  }

  try {
    const created = await backendApi.createWorkspace({
      name: folderBaseName(rootPath),
      description: '',
      instructions: '',
      root_path: rootPath,
    });
    await loadWorkspaces();
    setCurrentWorkspaceId(created.id);
    setExpandedWorkspace(created.id, true);
    renderWorkspaceHistory(openSession);
    window.dispatchEvent(new CustomEvent('workspace:context-changed'));
    notify(`已添加工作空间「${created.name}」`);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    notify(`创建工作空间失败：${msg || '未知错误'}`);
  }
}

export function openWorkspaceModal(ws: { id: string; name: string; description?: string; instructions?: string; root_path?: string } | null): void {
  const modal = $('#workspace-modal');
  if (!modal) return;
  if (!ws?.id) return;
  const titleEl = $('#ws-modal-title');
  const name = $('#ws-modal-name') as HTMLInputElement;
  const instr = $('#ws-modal-instr') as HTMLTextAreaElement;
  const pathEl = $('#ws-modal-path');
  const dangerEl = modal.querySelector('.workspace-modal__danger') as HTMLElement | null;
  if (titleEl) titleEl.textContent = '编辑工作空间';
  if (name) name.value = ws.name ?? '';
  if (instr) instr.value = displayProjectPrompt(ws.instructions, ws.root_path);
  if (pathEl) {
    const path = ws.root_path?.trim();
    if (path) {
      pathEl.textContent = `项目目录：${path}`;
      pathEl.hidden = false;
    } else {
      pathEl.textContent = '';
      pathEl.hidden = true;
    }
  }
  if (dangerEl) dangerEl.hidden = false;
  modal.dataset.editId = ws.id;
  modal.classList.add('show');
  instr?.focus();
}



export async function loadWorkspaces(): Promise<void> {
  // 标记进入加载、清掉旧的错误态（让 UI 不再显示「加载失败」banner）
  historyLoadError.workspaces = null;
  try {

    const next = await backendApi.workspaces();
    state.workspaces = next;

  } catch (err) {

    // 记录错误并保留上次成功数据，避免把加载失败呈现成正常空态。
    const msg = err instanceof Error ? err.message : String(err);
    historyLoadError.workspaces = msg;
    notify(`加载工作空间失败：${msg}`);
    // 注意：不清空 state.workspaces——保留上次的成功数据，避免错误态下连旧数据都看不到。

  }

}

/** 与 loadWorkspaces 同构：会话列表的「加载失败 vs 真空」区分。
 *  现有调用方在 session-controller.ts 的初始化流程中（Promise.all 里），
 *  这里提供一个同款 try-catch 包装让调用方用，不重写主调用链。 */
export async function loadSessionsList(): Promise<BackendSession[]> {
  historyLoadError.sessions = null;
  try {
    const list = await backendApi.sessions();
    // syncSessionsFromBackend 是 BackendSession→SessionRow 的正规转换器
    // （顺带 mergeSessionModelsFromBackend + 写 state.backendSessions），
    // 不能裸 state.sessions = list（类型是 SessionRow[]，且会丢字段映射）。
    syncSessionsFromBackend(list);
    return list;
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    historyLoadError.sessions = msg;
    notify(`加载会话失败：${msg}`);
    return [];
  }
}



/**
 * 后端水合完成后刷新侧栏：聚焦当前/最近工作空间并展开，避免首屏仍显示空项目提示。
 */
export function refreshSidebarAfterHydrate(openSession: OpenSessionFn): void {
  const sessions = state.sessions.filter(isSessionVisibleWithExternalAgentsFlag);
  if (sessions.length) {
    const activeId = state.activeSessionId;
    const focus = activeId
      ? sessions.find((s) => s.id === activeId)
      : [...sessions].sort((a, b) => b.updatedAt - a.updatedAt)[0];
    const wsId = focus?.workspaceId;
    if (wsId && wsId !== 'default') {
      const ws = state.workspaces.find((w) => w.id === wsId);
      if (ws && !isWorkspaceHidden(ws)) {
        // 仅在确有活跃会话时跟随其 workspace（openSession 已设过，这里幂等对齐）。
        // 欢迎页（无活跃会话）刻意保持 default：否则 composer 选择器仍显示「对话」，
        // 但 currentWorkspaceId 已被切到 test，用户从欢迎页直接发消息会落到 test，
        // 与右下角选择器所见不一致。侧栏仍展开该空间文件夹以便用户能看到历史。
        if (activeId) setCurrentWorkspaceId(wsId);
        setExpandedWorkspace(wsId, true);
      }
    }
  } else if (state.currentWorkspaceId && state.currentWorkspaceId !== 'default') {
    const ws = state.workspaces.find((w) => w.id === state.currentWorkspaceId);
    if (ws && !isWorkspaceHidden(ws)) {
      setExpandedWorkspace(state.currentWorkspaceId, true);
    } else if (ws && isWorkspaceHidden(ws)) {
      setCurrentWorkspaceId('default');
    }
  }
  renderWorkspaceHistory(openSession);
}



export async function refreshAllSessions(): Promise<void> {
  try {

    const rows = await backendApi.sessions();

    syncSessionsFromBackend(rows);

  } catch {

    /* keep local */

  }

}



function closeWorkspaceModal(): void {

  $('#workspace-modal')?.classList.remove('show');

}



export function bindWorkspaceUi(refresh: RefreshSessionsFn, openSession: OpenSessionFn): void {

  setRefreshSessions(refresh);

  setOpenSessionForWorkspaces(openSession);



  $('#workspace-add-btn')?.addEventListener('click', () => {
    void createWorkspaceFromFolderPicker(openSession);
  });



  $('#ws-modal-cancel')?.addEventListener('click', closeWorkspaceModal);



  $('#workspace-modal')?.addEventListener('click', (e) => {

    if (e.target === e.currentTarget) closeWorkspaceModal();

  });



  $('#ws-modal-submit')?.addEventListener('click', () => {

    void submitWorkspaceModal(refresh, openSession);

  });



  const modal = $('#workspace-modal');



  $('#ws-modal-hide')?.addEventListener('click', () => {

    const id = modal?.dataset.editId;

    if (!id) return;

    void (async () => {

      const confirmed = await showConfirmDialog({

        title: '隐藏工作空间',

        message: '隐藏后侧栏与批量删除将不再显示该项目及其对话，对话记录会保留。可在设置 → 项目管理中取消隐藏。',

        confirmText: '隐藏',

        cancelText: '取消',

      });

      if (!confirmed) return;

      await setWorkspaceHidden(id, true, openSession);

      closeWorkspaceModal();

      notify('项目已隐藏');

    })();

  });



  $('#ws-modal-delete')?.addEventListener('click', () => {

    const id = modal?.dataset.editId;

    if (!id) return;

    void (async () => {

      closeWorkspaceModal();

      await deleteWorkspaceById(id, openSession);

    })();

  });



  modal?.addEventListener('keydown', (e) => {

    if ((e as KeyboardEvent).key === 'Escape') closeWorkspaceModal();

  });



  [$('#ws-modal-name'), $('#ws-modal-instr')].forEach((el) => {

    el?.addEventListener('keydown', (e) => {

      const ev = e as KeyboardEvent;

      if (ev.key === 'Enter' && ev.ctrlKey) {

        ev.preventDefault();

        void submitWorkspaceModal(refresh, openSession);

      }

    });

  });

}



async function submitWorkspaceModal(refresh: RefreshSessionsFn, openSession: OpenSessionFn): Promise<void> {


  const modal = $('#workspace-modal');

  if (!modal) return;

  const name = ($('#ws-modal-name') as HTMLInputElement).value.trim();

  const instructions = ($('#ws-modal-instr') as HTMLTextAreaElement).value.trim();

  const editId = modal.dataset.editId?.trim();
  if (!editId) {
    notify('请先通过「新建工作空间」选择本地文件夹');
    return;
  }

  const submitBtn = $('#ws-modal-submit') as HTMLButtonElement | null;

  if (submitBtn) submitBtn.disabled = true;

  try {

    await backendApi.updateWorkspace(editId, { name, description: '', instructions });

    await loadWorkspaces();

    await refresh();

    closeWorkspaceModal();

    renderWorkspaceHistory(openSession);

    notify('工作空间已更新');

  } catch (err) {

    notify(`保存工作空间失败：${(err as Error).message || '未知错误'}`);

  } finally {

    if (submitBtn) submitBtn.disabled = false;

  }

}



export function bindHistoryManage(): void {

  // 兼容性占位：实际逻辑已迁出到 features/session-manage.ts 的 bindSessionManageUi

}
