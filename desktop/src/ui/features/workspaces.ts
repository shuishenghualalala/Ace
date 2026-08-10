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
import { openDialog, type OverlayHandle } from '../components/overlays';
import { isChannelSessionId, type ChannelSessionGroup } from './channel-sessions';
import { isSessionVisibleWithExternalAgentsFlag } from './external-agents-feature';
import { externalAgentInitial, externalAgentTone } from './external-agent-avatar';
import { applySessionModelBinding, activeComposerModelId, mergeSessionModelsFromBackend, modelLabelForId, syncSessionModelUi } from './session-model';
import { assignSecurityMode } from './security-approval';
import {
  patchMountedSessionHistoryStatus,
  renderSessionHistory,
  SESSION_HISTORY_GROUP_LIMIT,
} from './session-history-view';
import {
  createWorkspaceView,
  type WorkspaceView,
  type WorkspaceViewOptions,
} from './workspace-view';

import { sessionStatusClass } from '../chat-render';

import type { SessionStatus } from '../chat-render';
import { sessionStore } from '../stores/session-store';

/** 历史拉取失败状态（不写到 store，避免破坏现有 Proxy 消费者）。
 *  原实现把 catch 静默吞掉写 `state.workspaces = []`，导致「加载失败」和「真空」
 *  在 UI 上长得一样——用户分不清 gateway 没起来还是自己还没建工作空间。
 *  这里只记「最近一次拉取有没有失败 + 失败原因」，数据数组保持原状（不清空），
 *  renderWorkspaceHistory 据此切换空态文案。 */
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

  setSessionStatus,

  setWsShowAll,

  state,

  type SessionRow,

  type TabKey,

} from '../state';

import { isRendererLoggedIn, requireRendererLogin } from './auth-gate';
import { openSessionActionsMenu } from './session-actions';
import { showConfirmDialog } from '../ui-feedback';



export type OpenSessionFn = (sessionId: string) => void | Promise<void>;

export type RefreshSessionsFn = () => Promise<void>;



function formatTime(ts: number): string {

  const diff = Date.now() / 1000 - ts;

  if (diff < 60) return '刚刚';

  if (diff < 3600) return `${Math.max(1, Math.floor(diff / 60))}分`;

  if (diff < 86400) return `${Math.floor(diff / 3600)}小时`;

  if (diff < 7 * 86400) return `${Math.floor(diff / 86400)}天`;

  if (diff < 30 * 86400) return `${Math.floor(diff / (7 * 86400))}周`;

  return `${Math.max(1, Math.floor(diff / (30 * 86400)))}月`;

}



function toggleSectionCollapsed(key: string): boolean {
  const next = localStorage.getItem(key) !== 'true';

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

/** Compatibility entry used by chat-controller while the new view owns the DOM. */
export function patchSessionRowStatus(sessionId: string, status: SessionStatus): void {
  patchMountedSessionHistoryStatus(sessionId, status);
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



const CONVERSATION_ICON = '<svg class="mw-icon" viewBox="0 0 24 24" aria-hidden="true"><use href="#icon-task"></use></svg>';

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
    return `<span class="session__external-agent-icon agent-provider-tone-${externalAgentTone(provider)}" aria-hidden="true">${escapeHtml(externalAgentInitial(provider, s.agentLabel?.display_badge))}</span>`;
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



function bindHistoryListEvents(list: HTMLElement, openSession: OpenSessionFn): void {

  list.querySelectorAll('[data-section-toggle]').forEach((el) => {

    el.addEventListener('click', () => {

      const key = el.getAttribute('data-section-toggle');

      if (!key) return;

      toggleSectionCollapsed(key);

      renderWorkspaceHistory(openSession);

      renderStudioHistory(openSession);

    });

  });



  list.querySelectorAll('[data-project-toggle]').forEach((el) => {

    el.addEventListener('click', (e) => {

      if ((e.target as HTMLElement).closest('[data-project-settings],[data-project-new]')) return;

      const id = el.getAttribute('data-project-toggle')!;

      setCurrentWorkspaceId(id);

      const currentlyExpanded = state.expandedWorkspaces[id] !== false;

      setExpandedWorkspace(id, !currentlyExpanded);

      renderWorkspaceHistory(openSession);

      renderStudioHistory(openSession);

    });

  });



  list.querySelectorAll('[data-project-new]').forEach((btn) => {

    btn.addEventListener('click', (e) => {

      e.stopPropagation();

      const wsId = btn.getAttribute('data-project-new')!;

      setCurrentWorkspaceId(wsId);

      createSessionInWorkspace(wsId, openSession);

    });

  });



  list.querySelectorAll('[data-project-settings]').forEach((btn) => {

    btn.addEventListener('click', (e) => {

      e.stopPropagation();

      const wsId = btn.getAttribute('data-project-settings')!;

      const ws = state.workspaces.find((w) => w.id === wsId);

      if (ws) openWorkspaceModal(ws);

    });

  });



  list.querySelectorAll('[data-ws-show-all]').forEach((btn) => {

    btn.addEventListener('click', (e) => {

      e.stopPropagation();

      const wsId = btn.getAttribute('data-ws-show-all')!;

      setWsShowAll(wsId, true);

      renderWorkspaceHistory(openSession);

      renderStudioHistory(openSession);

    });

  });



  list.querySelectorAll('.history-item-main').forEach((item) => {

    item.addEventListener('click', () => {

      const row = item.closest('[data-session-id]');

      const id = row?.getAttribute('data-session-id');

      if (id) void openSession(id);

    });

  });



  list.querySelectorAll('[data-session-menu]').forEach((btn) => {

    btn.addEventListener('click', (e) => {

      e.stopPropagation();

      const id = btn.getAttribute('data-session-menu')!;

      openSessionActionsMenu(id, btn as HTMLElement, refreshSessionsWrapper);

    });

  });

}



/** 工作室侧栏：按工作空间分组展示会话（项目 / 对话）。 */

function studioHistoryGroupHtml(label: string, sessions: (typeof state.sessions)[0][]): string {
  if (sessions.length === 0) return '';
  const sorted = [...sessions].sort(sessionListComparator);
  return `
    <section class="studio-history-group">
      <div class="studio-history-group-label">${escapeHtml(label)}</div>
      <div class="studio-history-group-items">
        ${sorted.map((s) => sessionRowHtml(s)).join('')}
      </div>
    </section>
  `;
}

export function renderStudioHistory(openSession: OpenSessionFn): void {

  const list = $('#studio-history-list');

  if (!list) return;

  const filter = state.historyFilter.toLowerCase().trim();

  const sessions = state.sessions

    .filter((s) => !s.archived)

    .filter((s) => !isChannelSessionId(s.id))

    .filter(isSessionVisibleWithExternalAgentsFlag)

    .filter((s) => {
      if (!filter) return true;
      return matchesFilter(s.title, filter) || matchesFilter(s.id, filter);
    });



  const projects = visibleProjectWorkspaces();
  const projectGroups = projects
    .map((ws) => {
      const wsSessions = sessions.filter((s) => s.workspaceId === ws.id);
      if (wsSessions.length === 0) return '';
      if (
        filter
        && !wsSessions.some(
          (s) => matchesFilter(s.title, filter) || matchesFilter(s.id, filter),
        )
        && !matchesFilter(ws.name, filter)
        && !matchesFilter(ws.id, filter)
      ) {
        return '';
      }
      return studioHistoryGroupHtml(ws.name, wsSessions);
    })
    .filter(Boolean);

  const defaultSessions = sessions.filter((s) => s.workspaceId === 'default');

  const sections: string[] = [];
  if (projectGroups.length > 0) {
    sections.push(`
      <div class="studio-history-section">
        <div class="studio-history-section-label studio-history-section-label--major">项目</div>
        ${projectGroups.join('')}
      </div>
    `);
  }
  const conversationsGroup = studioHistoryGroupHtml('对话', defaultSessions);
  if (conversationsGroup) sections.push(conversationsGroup);

  list.innerHTML = sections.length

    ? sections.join('')

    : '<div class="history-empty history-empty--inline">暂无对话</div>';



  bindHistoryListEvents(list, openSession);

}



export const CONVERSATIONS_LIMIT = SESSION_HISTORY_GROUP_LIMIT;

let latestOpenSession: OpenSessionFn = () => {};

/** Compatibility facade for existing controllers; SessionHistoryView owns the DOM. */
export function renderWorkspaceHistory(openSession: OpenSessionFn): void {
  latestOpenSession = openSession;
  renderSessionHistory();
  if (typeof document !== 'undefined' && document.getElementById('studio-history-list')) {
    renderStudioHistory(openSession);
  }
}

const SESSION_MENU_ICON = '<svg class="mw-icon" viewBox="0 0 24 24" aria-hidden="true"><use href="#icon-more"></use></svg>';

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
  if (!requireRendererLogin()) return '';
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

  if (!requireRendererLogin()) return '';

  const seedModel = activeComposerModelId() || state.config?.active_model_id || '';

  discardDraft();

  const id = newSessionId();

  void assignSecurityMode(id, workspaceId);

  draftSession = { id, workspaceId, ...(seedModel ? { modelProfileId: seedModel } : {}) };

  if (seedModel) {
    applySessionModelBinding(id, {
      model_profile_id: seedModel,
      model_label: modelLabelForId(seedModel),
    });
  }

  state.currentWorkspaceId = workspaceId;

  setActiveSessionId(id);

  window.dispatchEvent(new CustomEvent('session:changed', { detail: { sessionId: id } }));

  ensureSessionMessages(id);

  setSessionStatus(id, 'idle');

  // 新建对话不继承上一会话的外援团队绑定
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

async function findWorkspaceByRoot(
  rootPath: string,
  excludedId?: string,
): Promise<Workspace | undefined> {
  const candidates = state.workspaces.filter((workspace) =>
    workspace.id !== excludedId && Boolean(workspace.root_path?.trim()));
  const normalized = normalizeFolderPathForCompare(rootPath);
  const direct = candidates.find((workspace) =>
    normalizeFolderPathForCompare(workspace.root_path!) === normalized);
  if (direct) return direct;

  const directoryInfo = window.Crew?.workspaceDirectoryInfo;
  if (!directoryInfo) return undefined;
  const canonical = await Promise.all(candidates.map(async (workspace) => {
    try {
      return {
        workspace,
        path: (await directoryInfo(workspace.id)).canonicalPath,
      };
    } catch {
      return { workspace, path: null };
    }
  }));
  return canonical.find((item) =>
    item.path && normalizeFolderPathForCompare(item.path) === normalized)?.workspace;
}

async function findWorkspaceByRootAfterReload(
  rootPath: string,
  excludedId?: string,
): Promise<Workspace | undefined> {
  await loadWorkspaces();
  return findWorkspaceByRoot(rootPath, excludedId);
}

function focusVisibleWorkspace(workspace: Workspace, openSession: OpenSessionFn): void {
  if (workspace.hidden) return;
  setCurrentWorkspaceId(workspace.id);
  setExpandedWorkspace(workspace.id, true);
  renderWorkspaceHistory(openSession);
  window.dispatchEvent(new CustomEvent('workspace:context-changed'));
}

/**
 * 新建工作空间：先弹出系统文件夹选择器，再在后端创建工作空间并刷新侧栏。
 * 名称默认取所选目录名；若该目录已绑定则聚焦已有项目。
 */
export async function createWorkspaceFromFolderPicker(
  openSession: OpenSessionFn,
): Promise<string | void> {
  if (!requireRendererLogin('请先登录后再创建工作空间')) return;
  const selectFolder = window.Crew?.selectFolder;
  if (!selectFolder) {
    notify('请在桌面客户端中使用「新建工作空间」以选择本地文件夹');
    return;
  }
  const paths = await selectFolder();
  if (!paths?.length) return;
  const rootPath = paths[0]!.trim();
  if (!rootPath) return;

  const existing = await findWorkspaceByRoot(rootPath);
  if (existing) {
    focusVisibleWorkspace(existing, openSession);
    notify(`该目录已是工作空间「${existing.name}」`);
    return existing.id;
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
    return created.id;
  } catch (err) {
    const concurrent = await findWorkspaceByRootAfterReload(rootPath);
    if (concurrent) {
      focusVisibleWorkspace(concurrent, openSession);
      notify(`该目录已是工作空间「${concurrent.name}」`);
      return concurrent.id;
    }
    const msg = err instanceof Error ? err.message : String(err);
    notify(`创建工作空间失败：${msg || '未知错误'}`);
  }
}

export async function loadWorkspaces(): Promise<void> {

  if (!isRendererLoggedIn()) return;

  // 标记进入加载、清掉旧的错误态（让 UI 不再显示「加载失败」banner）
  historyLoadError.workspaces = null;
  try {

    const next = await backendApi.workspaces();
    state.workspaces = next;

  } catch (err) {

    // 关键修复：原实现吞错 + 写空数组，UI 看到「暂无工作空间」会误以为"还没建"。
    // 现在记下错误、弹通知、保留旧数据（不抹掉用户之前能看到的内容）。
    const msg = err instanceof Error ? err.message : String(err);
    historyLoadError.workspaces = msg;
    notify(`加载工作空间失败：${msg}`);
    // 注意：不清空 state.workspaces——保留上次的成功数据，避免错误态下连旧数据都看不到。

  }

}

/** 与 loadWorkspaces 同构：会话列表的「加载失败 vs 真空」区分。
 *  现有调用方在 [session-controller.ts:400](Ace/desktop/src/ui/features/session-controller.ts#L400)（Promise.all 里），
 *  这里提供一个同款 try-catch 包装让调用方用，不重写主调用链。 */
export async function loadSessionsList(): Promise<BackendSession[]> {
  if (!isRendererLoggedIn()) return [];
  historyLoadError.sessions = null;
  try {
    const list = await backendApi.sessions(undefined, { includeArchived: true });
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
  const sessions = state.sessions
    .filter((session) => !session.archived)
    .filter(isSessionVisibleWithExternalAgentsFlag);
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

  if (!isRendererLoggedIn()) return;

  try {

    const rows = await backendApi.sessions(undefined, { includeArchived: true });

    syncSessionsFromBackend(rows);

  } catch {

    /* keep local */

  }

}



export async function relinkWorkspaceFromFolderPicker(
  workspaceId: string,
  openSession: OpenSessionFn = openSessionWrapper,
): Promise<string | void> {
  if (!requireRendererLogin('请先登录后再关联工作空间目录')) return;
  const selectFolder = window.Crew?.selectFolder;
  if (!selectFolder) {
    notify('请在桌面客户端中选择本地文件夹');
    return;
  }
  const paths = await selectFolder();
  const rootPath = paths?.[0]?.trim();
  if (!rootPath) return;

  const existing = await findWorkspaceByRoot(rootPath, workspaceId);
  if (existing) {
    notify(`该目录已属于工作空间「${existing.name}」`);
    return existing.id;
  }

  try {
    await backendApi.updateWorkspace(workspaceId, { root_path: rootPath });
    await loadWorkspaces();
    renderWorkspaceHistory(openSession);
    window.dispatchEvent(new CustomEvent('workspace:context-changed'));
    notify('工作空间目录已重新关联');
    return workspaceId;
  } catch (error) {
    const concurrent = await findWorkspaceByRootAfterReload(rootPath, workspaceId);
    if (concurrent) {
      notify(`该目录已属于工作空间「${concurrent.name}」`);
      return concurrent.id;
    }
    throw error;
  }
}

export function createWorkspaceViewOptions(
  openSession: OpenSessionFn = openSessionWrapper,
): WorkspaceViewOptions {
  const directoryInfo = window.Crew?.workspaceDirectoryInfo;
  return {
    getWorkspaces: () => state.workspaces.map((workspace) => ({
      ...workspace,
      instructions: displayProjectPrompt(workspace.instructions, workspace.root_path),
    })),
    getSessionCount: (workspaceId) =>
      state.sessions.filter((session) => session.workspaceId === workspaceId).length,
    getLoadError: () => historyLoadError.workspaces,
    reloadWorkspaces: async () => {
      await loadWorkspaces();
      renderWorkspaceHistory(openSession);
    },
    ...(directoryInfo
      ? { directoryInfo: (workspaceId: string) => directoryInfo(workspaceId) }
      : {}),
    createWorkspace: () => createWorkspaceFromFolderPicker(openSession),
    saveWorkspace: async (workspaceId, fields) => {
      await backendApi.updateWorkspace(workspaceId, {
        name: fields.name,
        description: '',
        instructions: fields.instructions,
      });
      await loadWorkspaces();
      await refreshSessionsWrapper();
      renderWorkspaceHistory(openSession);
      notify('工作空间已更新');
    },
    relinkWorkspace: (workspaceId) =>
      relinkWorkspaceFromFolderPicker(workspaceId, openSession),
    setWorkspaceHidden: async (workspaceId, hidden) => {
      if (hidden) {
        const confirmed = await showConfirmDialog({
          title: '隐藏工作空间',
          message: '隐藏后侧栏不再显示该工作空间及其会话；记录会保留，可在设置中取消隐藏。',
          confirmText: '隐藏',
          cancelText: '取消',
        });
        if (!confirmed) return;
      }
      await setWorkspaceHidden(workspaceId, hidden, openSession);
      notify(hidden ? '工作空间已隐藏' : '已取消隐藏');
    },
    deleteWorkspace: (workspaceId) => deleteWorkspaceById(workspaceId, openSession),
  };
}

let workspaceDialog: {
  handle: OverlayHandle<HTMLDivElement>;
  view: WorkspaceView;
} | null = null;

export function openWorkspaceModal(workspace: Workspace | null): void {
  if (!requireRendererLogin('请先登录后再管理工作空间') || !workspace?.id) return;
  workspaceDialog?.handle.close();
  const host = document.createElement('div');
  const view = createWorkspaceView(
    host,
    createWorkspaceViewOptions(openSessionWrapper),
    workspace.id,
  );
  const handle = openDialog({
    title: '管理工作空间',
    content: host,
    onClose: () => {
      view.dispose();
      if (workspaceDialog?.handle === handle) workspaceDialog = null;
    },
  });
  workspaceDialog = { handle, view };
}

export function bindWorkspaceUi(
  refresh: RefreshSessionsFn,
  openSession: OpenSessionFn,
): () => void {
  setRefreshSessions(refresh);
  setOpenSessionForWorkspaces(openSession);
  return () => {
    workspaceDialog?.handle.close();
    workspaceDialog = null;
  };
}



export function bindHistoryManage(): void {

  // 兼容性占位：实际逻辑已迁出到 features/session-manage.ts 的 bindSessionManageUi

}
