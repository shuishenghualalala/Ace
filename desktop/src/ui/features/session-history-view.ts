import type { Workspace } from '../backend-client';
import type { SessionStatus } from '../chat-render';
import { createIcon, type IconId } from '../components/icon';
import {
  setCurrentWorkspaceId,
  setExpandedChannel,
  setExpandedWorkspace,
  setWsShowAll,
  state,
  type SessionRow,
} from '../state';
import { sessionStore } from '../stores/session-store';
import { workspaceStore } from '../stores/workspace-store';
import { isChannelSessionId } from './channel-sessions';
import { isSessionVisibleWithExternalAgentsFlag } from './external-agents-feature';
import { openSessionActionsMenu, togglePin } from './session-actions';

const SEARCH_DEBOUNCE_MS = 150;
export const SESSION_HISTORY_GROUP_LIMIT = 10;
const SECTION_KEYS = {
  projects: 'Crew.historyProjectsCollapsed',
  channels: 'Crew.historyChannelsCollapsed',
  conversations: 'Crew.historyConversationsCollapsed',
  archived: 'Crew.historyArchivedCollapsed',
} as const;

export interface SessionHistoryViewOptions {
  openSession(sessionId: string): void | Promise<void>;
  createSession(workspaceId: string): void;
  createWorkspace(): void;
  manageHistory(): void;
  openWorkspace(workspace: Workspace): void;
  refreshSessions(): Promise<void>;
  retrySessions(): Promise<void>;
  retryWorkspaces(): Promise<void>;
  getLoadErrors(): { sessions: string | null; workspaces: string | null };
  renderStudioHistory?: () => void;
}

export interface SessionHistoryView {
  render(): void;
  patchSessionStatus(sessionId: string, status: SessionStatus): void;
  dispose(): void;
}

interface SessionGroup {
  id: string;
  label: string;
  kind: 'workspace' | 'channel' | 'conversation' | 'archived';
  sessions: SessionRow[];
  workspace?: Workspace;
  platform?: string;
}

function formatTime(updatedAt: number): string {
  const elapsed = Math.max(0, Date.now() - updatedAt);
  if (elapsed < 60_000) return '刚刚';
  if (elapsed < 3_600_000) return `${Math.max(1, Math.floor(elapsed / 60_000))} 分钟前`;
  if (elapsed < 86_400_000) return `${Math.floor(elapsed / 3_600_000)} 小时前`;
  if (elapsed < 7 * 86_400_000) return `${Math.floor(elapsed / 86_400_000)} 天前`;
  return new Date(updatedAt).toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' });
}

function sortedSessions(sessions: SessionRow[]): SessionRow[] {
  return [...sessions].sort((left, right) => {
    const pinned = Number(Boolean(right.pinned)) - Number(Boolean(left.pinned));
    return pinned || right.updatedAt - left.updatedAt;
  });
}

function matches(value: string | undefined, query: string): boolean {
  return Boolean(value?.toLocaleLowerCase().includes(query));
}

function isSectionCollapsed(key: string): boolean {
  return localStorage.getItem(key) === 'true';
}

function toggleSection(key: string): void {
  localStorage.setItem(key, String(!isSectionCollapsed(key)));
}

function visibleWorkspaces(): Workspace[] {
  return state.workspaces.filter((workspace) =>
    workspace.id !== 'default' && !workspace.hidden);
}

function sessionIdentity(session: SessionRow, workspace?: Workspace): string {
  if (session.channelPlatform) return `渠道 · ${session.badge || session.channelPlatform}`;
  const provider = String(session.agentLabel?.provider || '').toLocaleLowerCase();
  const name = session.agentLabel?.name?.trim();
  if (provider === 'team') return `Team${name ? ` · ${name}` : ''}`;
  if (provider && !['crew', 'builtin', 'client'].includes(provider)) {
    return `Agent${name ? ` · ${name}` : ''}`;
  }
  if (workspace) return workspace.name;
  return session.workspaceId === 'default' ? '通用助手' : session.workspaceId;
}

function sessionIcon(session: SessionRow): IconId {
  if (session.channelPlatform) return 'icon-task';
  const provider = String(session.agentLabel?.provider || '').toLocaleLowerCase();
  if (provider === 'sites') return 'icon-inspiration';
  if (provider === 'team') return 'icon-team';
  if (provider && !['crew', 'builtin', 'client'].includes(provider)) return 'icon-agent';
  return 'icon-task';
}

function hasVisibleIdentity(session: SessionRow): boolean {
  if (session.channelPlatform) return true;
  const provider = String(session.agentLabel?.provider || '').toLocaleLowerCase();
  return provider === 'team' || Boolean(provider && !['crew', 'builtin', 'client'].includes(provider));
}

function createButton(
  className: string,
  label: string,
  icon: IconId,
  dataName?: string,
): HTMLButtonElement {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = className;
  button.setAttribute('aria-label', label);
  button.title = label;
  if (dataName) button.dataset[dataName] = '';
  button.append(createIcon(icon, { size: 18 }));
  return button;
}

function statusDescriptor(
  session: SessionRow,
  status: SessionStatus | undefined,
): { key: string; text: string; icon?: IconId } {
  if (session.archived) return { key: 'archived', text: '已归档', icon: 'icon-check' };
  if (status === 'running') return { key: 'running', text: '进行中', icon: 'status-running' };
  if (status === 'queued') return { key: 'queued', text: '排队中', icon: 'status-waiting' };
  if (status === 'error') return { key: 'error', text: '失败', icon: 'icon-error' };
  if (sessionStore.get().unreadCompletedSessions.has(session.id)) {
    return { key: 'unread', text: '已完成', icon: 'status-complete' };
  }
  return { key: 'idle', text: formatTime(session.updatedAt) };
}

function setStatusContent(
  element: HTMLElement,
  session: SessionRow,
  status: SessionStatus | undefined,
): void {
  const descriptor = statusDescriptor(session, status);
  if (element.dataset.status === descriptor.key && descriptor.key !== 'idle') return;
  element.dataset.status = descriptor.key;
  element.className = `mw-session-history__status mw-session-history__status--${descriptor.key}`;
  element.replaceChildren();
  if (descriptor.icon) element.append(createIcon(descriptor.icon, { size: 16 }));
  const text = document.createElement('span');
  text.textContent = descriptor.text;
  element.append(text);
}

function groupMatches(group: SessionGroup, query: string): boolean {
  return matches(group.label, query)
    || matches(group.workspace?.id, query)
    || matches(group.platform, query);
}

function filteredSessions(group: SessionGroup, query: string): SessionRow[] {
  if (!query || groupMatches(group, query)) return group.sessions;
  return group.sessions.filter((session) =>
    matches(session.title, query)
    || matches(session.preview, query)
    || matches(session.id, query)
    || matches(session.agentLabel?.name, query));
}

function buildGroups(query: string): SessionGroup[] {
  const visibleWorkspaceIds = new Set(visibleWorkspaces().map((workspace) => workspace.id));
  const ordinary = state.sessions
    .filter((session) => !session.archived)
    .filter((session) => !isChannelSessionId(session.id))
    .filter(isSessionVisibleWithExternalAgentsFlag);
  const projects = visibleWorkspaces().map((workspace): SessionGroup => ({
    id: `workspace:${workspace.id}`,
    label: workspace.name,
    kind: 'workspace',
    workspace,
    sessions: ordinary.filter((session) => session.workspaceId === workspace.id),
  }));
  const channels = state.channelSessionGroups.map((channel): SessionGroup => ({
    id: `channel:${channel.platform}`,
    label: channel.label,
    kind: 'channel',
    platform: channel.platform,
    sessions: channel.sessions,
  }));
  const conversations: SessionGroup = {
    id: 'conversation:default',
    label: '对话',
    kind: 'conversation',
    sessions: ordinary.filter((session) => session.workspaceId === 'default'),
  };
  const archived: SessionGroup = {
    id: 'archived',
    label: '已归档',
    kind: 'archived',
    sessions: state.sessions
      .filter((session) => session.archived)
      .filter((session) => !isChannelSessionId(session.id))
      .filter(isSessionVisibleWithExternalAgentsFlag)
      .filter((session) =>
        session.workspaceId === 'default' || visibleWorkspaceIds.has(session.workspaceId)),
  };

  return [...projects, ...channels, conversations, archived]
    .map((group) => ({ ...group, sessions: sortedSessions(filteredSessions(group, query)) }))
    .filter((group) => group.sessions.length > 0 || (!query && group.kind === 'workspace'));
}

/**
 * Creates the single production owner for assistant session history.
 *
 * Session and Workspace stores remain authoritative. This owner only projects
 * their state, delegates commands, and releases every listener on disposal.
 */
export function createSessionHistoryView(
  host: HTMLElement,
  options: SessionHistoryViewOptions,
): SessionHistoryView {
  const controller = new AbortController();
  const rows = new Map<string, HTMLElement>();
  let searchTimer: number | null = null;
  let renderFrame: number | null = null;
  let disposed = false;

  const root = document.createElement('section');
  root.className = 'mw-session-history';
  root.dataset.sessionHistoryView = '';
  root.setAttribute('aria-label', '会话历史');

  const searchBox = document.createElement('div');
  searchBox.className = 'mw-session-history__search';
  searchBox.append(createIcon('icon-search', { size: 16 }));
  const search = document.createElement('input');
  search.type = 'search';
  search.placeholder = '搜索会话';
  search.value = state.historyFilter;
  search.dataset.sessionHistorySearch = '';
  search.setAttribute('aria-label', '搜索会话');
  const clear = createButton(
    'mw-session-history__clear',
    '清空搜索',
    'icon-close',
    'sessionHistoryClear',
  );
  clear.hidden = !search.value;
  searchBox.append(search, clear);

  const actions = document.createElement('div');
  actions.className = 'mw-session-history__actions';
  const newSession = document.createElement('button');
  newSession.type = 'button';
  newSession.className = 'mw-session-history__new';
  newSession.dataset.sessionHistoryNew = '';
  newSession.append(createIcon('icon-plus', { size: 18 }), document.createTextNode('新建对话'));
  const importWorkspace = createButton(
    'mw-session-history__icon-button',
    '导入工作空间',
    'icon-folder',
    'sessionHistoryCreateWorkspace',
  );
  const manage = createButton(
    'mw-session-history__icon-button',
    '管理会话',
    'icon-settings',
    'sessionHistoryManage',
  );
  actions.append(newSession, importWorkspace, manage);

  const list = document.createElement('nav');
  list.className = 'mw-session-history__list';
  list.dataset.sessionHistoryList = '';
  list.setAttribute('aria-label', '最近会话');
  root.append(searchBox, actions, list);
  host.replaceChildren(root);

  const on = (
    target: EventTarget,
    type: string,
    listener: EventListenerOrEventListenerObject,
  ): void => target.addEventListener(type, listener, { signal: controller.signal });

  const workspaceFor = (session: SessionRow): Workspace | undefined =>
    state.workspaces.find((workspace) => workspace.id === session.workspaceId);

  const updateRow = (row: HTMLElement, session: SessionRow): void => {
    row.dataset.sessionId = session.id;
    row.className = 'mw-session-history__row';
    row.classList.toggle('mw-session-history__row--active', session.id === state.activeSessionId);
    row.classList.toggle('mw-session-history__row--pinned', Boolean(session.pinned));
    row.classList.toggle('mw-session-history__row--archived', Boolean(session.archived));
    if (session.id === state.activeSessionId) row.setAttribute('aria-current', 'true');
    else row.removeAttribute('aria-current');

    const open = row.querySelector<HTMLButtonElement>('[data-session-open]')!;
    open.dataset.sessionOpen = session.id;
    open.dataset.sessionHistoryFocusKey = `open:${session.id}`;
    open.title = session.title;
    open.setAttribute('aria-label', `打开会话：${session.title}`);
    if (session.id === state.activeSessionId) open.setAttribute('aria-current', 'true');
    else open.removeAttribute('aria-current');
    const identityIcon = open.querySelector<HTMLElement>('[data-session-identity-icon]')!;
    const desiredIcon = sessionIcon(session);
    if (identityIcon.dataset.icon !== desiredIcon) {
      identityIcon.dataset.icon = desiredIcon;
      identityIcon.replaceChildren(createIcon(desiredIcon, { size: 16 }));
    }
    const title = open.querySelector<HTMLElement>('[data-session-title]')!;
    title.textContent = session.title;
    title.title = session.title;
    open.querySelector<HTMLElement>('[data-session-preview]')!.textContent =
      session.preview || '暂无预览';
    const identity = open.querySelector<HTMLElement>('[data-session-identity]')!;
    identity.textContent = sessionIdentity(session, workspaceFor(session));
    identity.classList.toggle('mw-sr-only', !hasVisibleIdentity(session));
    const pinned = open.querySelector<HTMLElement>('[data-session-pinned]')!;
    pinned.hidden = !session.pinned;
    pinned.textContent = '已置顶';
    setStatusContent(
      open.querySelector<HTMLElement>('[data-session-status]')!,
      session,
      state.sessionStatuses[session.id] as SessionStatus | undefined,
    );
    const menu = row.querySelector<HTMLButtonElement>('[data-session-menu]')!;
    menu.dataset.sessionMenu = session.id;
    menu.dataset.sessionHistoryFocusKey = `menu:${session.id}`;
    menu.title = `管理会话：${session.title}`;
    menu.setAttribute('aria-label', `管理会话：${session.title}`);
  };

  const createRow = (session: SessionRow): HTMLElement => {
    const row = document.createElement('div');
    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'mw-session-history__row-main';
    open.dataset.sessionOpen = session.id;

    const identityIcon = document.createElement('span');
    identityIcon.className = 'mw-session-history__identity-icon';
    identityIcon.dataset.sessionIdentityIcon = '';
    identityIcon.setAttribute('aria-hidden', 'true');
    const copy = document.createElement('span');
    copy.className = 'mw-session-history__copy';
    const titleLine = document.createElement('span');
    titleLine.className = 'mw-session-history__title-line';
    const title = document.createElement('span');
    title.className = 'mw-session-history__title';
    title.dataset.sessionTitle = '';
    const pinned = document.createElement('span');
    pinned.className = 'mw-session-history__pin mw-sr-only';
    pinned.dataset.sessionPinned = '';
    titleLine.append(title, pinned);
    const preview = document.createElement('span');
    preview.className = 'mw-session-history__preview mw-sr-only';
    preview.dataset.sessionPreview = '';
    const identity = document.createElement('span');
    identity.className = 'mw-session-history__identity';
    identity.dataset.sessionIdentity = '';
    const status = document.createElement('span');
    status.dataset.sessionStatus = '';
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    status.setAttribute('aria-atomic', 'true');
    copy.append(titleLine, preview, identity);
    open.append(identityIcon, copy, status);

    const menu = createButton(
      'mw-session-history__menu',
      `管理会话：${session.title}`,
      'icon-more',
    );
    menu.dataset.sessionMenu = session.id;
    row.append(open, menu);
    updateRow(row, session);
    return row;
  };

  const getRow = (session: SessionRow): HTMLElement => {
    let row = rows.get(session.id);
    if (!row) {
      row = createRow(session);
      rows.set(session.id, row);
    } else {
      updateRow(row, session);
    }
    return row;
  };

  const createSectionHeader = (
    label: string,
    key: string,
    count: number,
  ): HTMLButtonElement => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'mw-session-history__section-toggle';
    button.dataset.sectionToggle = key;
    button.dataset.sessionHistoryFocusKey = `section:${key}`;
    const collapsed = isSectionCollapsed(key);
    button.setAttribute('aria-expanded', String(!collapsed));
    button.append(createIcon('icon-chevron-down', { size: 16 }));
    const text = document.createElement('span');
    text.textContent = label;
    const total = document.createElement('span');
    total.className = 'mw-session-history__section-count';
    total.textContent = String(count);
    button.append(text, total);
    return button;
  };

  const createGroupHeader = (group: SessionGroup): HTMLElement | null => {
    if (group.kind === 'conversation' || group.kind === 'archived') return null;
    const header = document.createElement('div');
    header.className = 'mw-session-history__group-header';
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'mw-session-history__group-toggle';
    if (group.kind === 'workspace') toggle.dataset.workspaceToggle = group.workspace!.id;
    else toggle.dataset.channelToggle = group.platform!;
    toggle.dataset.sessionHistoryFocusKey = group.kind === 'workspace'
      ? `workspace:${group.workspace!.id}`
      : `channel:${group.platform!}`;
    const expanded = group.kind === 'workspace'
      ? state.expandedWorkspaces[group.workspace!.id] !== false
      : state.channelExpanded[group.platform!] !== false;
    toggle.setAttribute('aria-expanded', String(expanded));
    toggle.append(
      createIcon('icon-chevron-down', { size: 16 }),
      createIcon(group.kind === 'workspace' ? 'icon-folder' : 'icon-task', { size: 16 }),
    );
    const name = document.createElement('span');
    name.textContent = group.label;
    const count = document.createElement('span');
    count.className = 'mw-session-history__group-count';
    count.textContent = String(group.sessions.length);
    toggle.append(name, count);
    header.append(toggle);
    if (group.workspace) {
      const settings = createButton(
        'mw-session-history__group-action',
        `设置工作空间：${group.label}`,
        'icon-more',
      );
      settings.dataset.workspaceSettings = group.workspace.id;
      settings.dataset.sessionHistoryFocusKey = `workspace-settings:${group.workspace.id}`;
      const add = createButton(
        'mw-session-history__group-action',
        `在 ${group.label} 新建会话`,
        'icon-plus',
      );
      add.dataset.workspaceNew = group.workspace.id;
      add.dataset.sessionHistoryFocusKey = `workspace-new:${group.workspace.id}`;
      header.append(settings, add);
    }
    return header;
  };

  const appendGroup = (
    container: HTMLElement,
    group: SessionGroup,
    query: string,
  ): number => {
    const wrapper = document.createElement('section');
    wrapper.className = `mw-session-history__group mw-session-history__group--${group.kind}`;
    wrapper.dataset.sessionGroup = group.id;
    const header = createGroupHeader(group);
    if (header) wrapper.append(header);

    const expanded = group.kind === 'workspace'
      ? state.expandedWorkspaces[group.workspace!.id] !== false || Boolean(query)
      : group.kind === 'channel'
        ? state.channelExpanded[group.platform!] !== false || Boolean(query)
        : true;
    if (expanded) {
      const items = document.createElement('div');
      items.className = 'mw-session-history__items';
      const showKey = group.kind === 'workspace'
        ? group.workspace!.id
        : group.kind === 'channel'
          ? `channel:${group.platform}`
          : group.kind === 'archived'
            ? 'archived'
            : 'default';
      const showAll = state.wsShowAll[showKey] === true || Boolean(query);
      const visible = showAll
        ? group.sessions
        : group.sessions.slice(0, SESSION_HISTORY_GROUP_LIMIT);
      for (const session of visible) items.append(getRow(session));
      if (!query && group.sessions.length > SESSION_HISTORY_GROUP_LIMIT) {
        const expand = document.createElement('button');
        expand.type = 'button';
        expand.className = 'mw-session-history__more';
        expand.dataset.showAll = showKey;
        expand.dataset.sessionHistoryFocusKey = `show-all:${showKey}`;
        expand.textContent = showAll
          ? '收起'
          : `再显示 ${group.sessions.length - SESSION_HISTORY_GROUP_LIMIT} 个会话`;
        items.append(expand);
      }
      if (visible.length === 0 && group.kind === 'workspace') {
        const empty = document.createElement('p');
        empty.className = 'mw-session-history__group-empty';
        empty.textContent = '尚无会话';
        items.append(empty);
      }
      wrapper.append(items);
    }
    container.append(wrapper);
    return group.sessions.length;
  };

  const appendSection = (
    fragment: DocumentFragment,
    label: string,
    key: string,
    groups: SessionGroup[],
    query: string,
  ): number => {
    const count = groups.reduce((total, group) => total + group.sessions.length, 0);
    if (groups.length === 0) return 0;
    const section = document.createElement('section');
    section.className = 'mw-session-history__section';
    section.dataset.sessionSection = key;
    section.append(createSectionHeader(label, key, count));
    if (!isSectionCollapsed(key) || query) {
      const content = document.createElement('div');
      content.className = 'mw-session-history__section-content';
      for (const group of groups) appendGroup(content, group, query);
      section.append(content);
    }
    fragment.append(section);
    return count;
  };

  const appendFailure = (
    fragment: DocumentFragment,
    message: string,
    retry: 'sessions' | 'workspaces',
  ): void => {
    const failure = document.createElement('div');
    failure.className = 'mw-session-history__failure';
    failure.setAttribute('role', 'status');
    failure.append(createIcon('icon-error', { size: 18 }));
    const copy = document.createElement('span');
    copy.textContent = message;
    const button = document.createElement('button');
    button.type = 'button';
    button.dataset.retryHistory = retry;
    button.textContent = '重试';
    failure.append(copy, button);
    fragment.append(failure);
  };

  const render = (): void => {
    if (disposed) return;
    const activeElement = document.activeElement as HTMLElement | null;
    const focusKey = activeElement && list.contains(activeElement)
      ? activeElement.dataset.sessionHistoryFocusKey
      : undefined;
    const query = state.historyFilter.trim().toLocaleLowerCase();
    if (search.value !== state.historyFilter && document.activeElement !== search) {
      search.value = state.historyFilter;
    }
    clear.hidden = !search.value.trim();
    const groups = buildGroups(query);
    const projects = groups.filter((group) => group.kind === 'workspace');
    const channels = groups.filter((group) => group.kind === 'channel');
    const conversations = groups.filter((group) => group.kind === 'conversation');
    const archived = groups.filter((group) => group.kind === 'archived');
    const fragment = document.createDocumentFragment();
    const visibleCount =
      appendSection(fragment, '项目', SECTION_KEYS.projects, projects, query)
      + appendSection(fragment, '渠道', SECTION_KEYS.channels, channels, query)
      + appendSection(fragment, '对话', SECTION_KEYS.conversations, conversations, query)
      + appendSection(fragment, '已归档', SECTION_KEYS.archived, archived, query);

    if (visibleCount === 0) {
      const errors = options.getLoadErrors();
      if (!query && errors.workspaces) appendFailure(fragment, `工作空间加载失败：${errors.workspaces}`, 'workspaces');
      if (!query && errors.sessions) appendFailure(fragment, `会话加载失败：${errors.sessions}`, 'sessions');
      if (query || (!errors.sessions && !errors.workspaces)) {
        const empty = document.createElement('div');
        empty.className = 'mw-session-history__empty';
        empty.dataset.sessionHistoryEmpty = '';
        empty.append(createIcon('icon-search', { size: 20 }));
        const text = document.createElement('span');
        text.textContent = query ? '没有找到匹配的会话' : '尚无会话';
        empty.append(text);
        fragment.append(empty);
      }
    }
    root.dataset.updating = 'true';
    list.replaceChildren(fragment);
    if (renderFrame !== null) window.cancelAnimationFrame(renderFrame);
    renderFrame = window.requestAnimationFrame(() => {
      renderFrame = null;
      delete root.dataset.updating;
    });
    if (focusKey) {
      const nextFocus = [...list.querySelectorAll<HTMLElement>('[data-session-history-focus-key]')]
        .find((element) => element.dataset.sessionHistoryFocusKey === focusKey);
      nextFocus?.focus();
    }
    const liveIds = new Set([
      ...state.sessions.map((session) => session.id),
      ...state.channelSessionGroups.flatMap((group) => group.sessions.map((session) => session.id)),
    ]);
    for (const id of rows.keys()) {
      if (!liveIds.has(id)) rows.delete(id);
    }
  };

  const patchSessionStatus = (sessionId: string, status: SessionStatus): void => {
    const row = rows.get(sessionId);
    const session = state.sessions.find((item) => item.id === sessionId)
      ?? state.channelSessionGroups.flatMap((group) => group.sessions)
        .find((item) => item.id === sessionId);
    if (!row || !session) return;
    setStatusContent(row.querySelector<HTMLElement>('[data-session-status]')!, session, status);
  };

  on(search, 'input', () => {
    clear.hidden = !search.value.trim();
    if (searchTimer !== null) window.clearTimeout(searchTimer);
    const commit = (): void => {
      searchTimer = null;
      state.historyFilter = search.value;
    };
    if (!search.value.trim()) commit();
    else searchTimer = window.setTimeout(commit, SEARCH_DEBOUNCE_MS);
  });
  on(clear, 'click', () => {
    if (searchTimer !== null) window.clearTimeout(searchTimer);
    searchTimer = null;
    search.value = '';
    state.historyFilter = '';
    search.focus();
  });
  on(newSession, 'click', () => options.createSession('default'));
  on(importWorkspace, 'click', () => options.createWorkspace());
  on(manage, 'click', () => options.manageHistory());
  on(list, 'click', (event) => {
    const target = event.target as HTMLElement;
    const menu = target.closest<HTMLElement>('[data-session-menu]');
    if (menu) {
      event.stopPropagation();
      openSessionActionsMenu(menu.dataset.sessionMenu!, menu, options.refreshSessions);
      return;
    }
    const workspaceSettings = target.closest<HTMLElement>('[data-workspace-settings]');
    if (workspaceSettings) {
      event.stopPropagation();
      const workspace = state.workspaces.find((item) =>
        item.id === workspaceSettings.dataset.workspaceSettings);
      if (workspace) options.openWorkspace(workspace);
      return;
    }
    const workspaceNew = target.closest<HTMLElement>('[data-workspace-new]');
    if (workspaceNew) {
      event.stopPropagation();
      const workspaceId = workspaceNew.dataset.workspaceNew!;
      setCurrentWorkspaceId(workspaceId);
      options.createSession(workspaceId);
      return;
    }
    const workspaceToggle = target.closest<HTMLElement>('[data-workspace-toggle]');
    if (workspaceToggle) {
      const workspaceId = workspaceToggle.dataset.workspaceToggle!;
      setCurrentWorkspaceId(workspaceId);
      setExpandedWorkspace(workspaceId, state.expandedWorkspaces[workspaceId] === false);
      options.renderStudioHistory?.();
      return;
    }
    const channelToggle = target.closest<HTMLElement>('[data-channel-toggle]');
    if (channelToggle) {
      const platform = channelToggle.dataset.channelToggle!;
      setExpandedChannel(platform, state.channelExpanded[platform] === false);
      options.renderStudioHistory?.();
      return;
    }
    const sectionToggle = target.closest<HTMLElement>('[data-section-toggle]');
    if (sectionToggle) {
      toggleSection(sectionToggle.dataset.sectionToggle!);
      render();
      return;
    }
    const showAll = target.closest<HTMLElement>('[data-show-all]');
    if (showAll) {
      const key = showAll.dataset.showAll!;
      setWsShowAll(key, state.wsShowAll[key] !== true);
      options.renderStudioHistory?.();
      return;
    }
    const open = target.closest<HTMLElement>('[data-session-open]');
    if (open) void options.openSession(open.dataset.sessionOpen!);
  });
  on(list, 'contextmenu', (event) => {
    const pointer = event as MouseEvent;
    const row = (pointer.target as HTMLElement).closest<HTMLElement>('[data-session-id]');
    if (!row || (pointer.target as HTMLElement).closest('[data-session-menu]')) return;
    pointer.preventDefault();
    openSessionActionsMenu(row.dataset.sessionId!, row, options.refreshSessions);
  });
  on(list, 'click', (event) => {
    const pointer = event as MouseEvent;
    if (!pointer.shiftKey) return;
    const row = (pointer.target as HTMLElement).closest<HTMLElement>('[data-session-id]');
    if (!row || (pointer.target as HTMLElement).closest('[data-session-menu]')) return;
    pointer.preventDefault();
    pointer.stopPropagation();
    const session = state.sessions.find((item) => item.id === row.dataset.sessionId);
    if (session) void togglePin(session.id, !session.pinned, options.refreshSessions);
  });
  on(list, 'click', (event) => {
    const retry = (event.target as HTMLElement).closest<HTMLElement>('[data-retry-history]');
    if (!retry) return;
    const command = retry.dataset.retryHistory === 'workspaces'
      ? options.retryWorkspaces
      : options.retrySessions;
    void command().then(render);
  });

  const unsubscribeSession = sessionStore.subscribe((next, previous) => {
    if (next.sessions !== previous.sessions) {
      render();
      return;
    }
    if (next.activeSessionId !== previous.activeSessionId) {
      const sessions = [
        ...state.sessions,
        ...state.channelSessionGroups.flatMap((group) => group.sessions),
      ];
      for (const id of [previous.activeSessionId, next.activeSessionId]) {
        const row = id ? rows.get(id) : undefined;
        const session = id ? sessions.find((item) => item.id === id) : undefined;
        if (row && session) updateRow(row, session);
      }
      return;
    }
    const ids = new Set([
      ...Object.keys(previous.sessionStatuses),
      ...Object.keys(next.sessionStatuses),
      ...previous.unreadCompletedSessions,
      ...next.unreadCompletedSessions,
    ]);
    for (const id of ids) {
      if (
        previous.sessionStatuses[id] !== next.sessionStatuses[id]
        || previous.unreadCompletedSessions.has(id) !== next.unreadCompletedSessions.has(id)
      ) {
        patchSessionStatus(id, next.sessionStatuses[id] ?? 'idle');
      }
    }
  });
  const unsubscribeWorkspace = workspaceStore.subscribe(() => render());

  render();

  return {
    render,
    patchSessionStatus,
    dispose() {
      if (disposed) return;
      disposed = true;
      controller.abort();
      unsubscribeSession();
      unsubscribeWorkspace();
      if (searchTimer !== null) window.clearTimeout(searchTimer);
      if (renderFrame !== null) window.cancelAnimationFrame(renderFrame);
      rows.clear();
      host.replaceChildren();
    },
  };
}

let mountedView: SessionHistoryView | null = null;

/** Mounts the production singleton used by existing controller refresh calls. */
export function mountSessionHistoryView(
  host: HTMLElement,
  options: SessionHistoryViewOptions,
): () => void {
  mountedView?.dispose();
  const view = createSessionHistoryView(host, options);
  mountedView = view;
  return () => {
    if (mountedView !== view) return;
    mountedView = null;
    view.dispose();
  };
}

export function renderSessionHistory(): void {
  mountedView?.render();
}

export function patchMountedSessionHistoryStatus(
  sessionId: string,
  status: SessionStatus,
): void {
  mountedView?.patchSessionStatus(sessionId, status);
}
