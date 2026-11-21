import { backendApi } from '../backend-client';
import { notify, state } from '../state';
import { authStore, productModeStore, sessionStore, uiStore } from '../stores/stores';
import type { PanelAttachments } from './attachments';
import { isRendererLoggedIn } from './auth-gate';
import { dispatchWs, isBusy, refreshSessions, respondToFollowup, stopGeneration } from './chat-controller';
import { mountConversationPanel, type ConversationPanel } from './conversation-panel';
import { closeInspector, disableInspectorSurfaceAutoWidth, openInspectorCustomView } from './inspector';
import { interactiveOwnerKey } from './interactive-owner';
import { loadBackendHistory } from './session-controller';
import { isStudioView } from './studio-chrome-state';

const textOnly: PanelAttachments = {
  list: () => [], add: async () => {}, remove: () => {}, takeForSend: () => [], subscribe: () => () => {},
};

let activePane: { current: () => boolean; focus: () => void } | null = null;

export function canOpenDesktopSideChat(): boolean {
  const parent = state.activeSessionId;
  return Boolean(parent && !parent.includes('::') && (
    state.sessions.some(({ id }) => id === parent) || state.messages[parent]?.some(({ role }) => role === 'user')
  ));
}

/** Open an independent conversation without changing the selected main session. */
export async function openDesktopSideChat(): Promise<void> {
  const parent = state.activeSessionId;
  if (!parent || !canOpenDesktopSideChat() || !isRendererLoggedIn()) return;
  if (activePane?.current()) {
    activePane.focus();
    return;
  }
  const host = document.getElementById('chat-inspector-body');
  const heading = document.getElementById('chat-inspector-tabs');
  if (!host || !heading) return;
  const owner = interactiveOwnerKey();
  const controller = new AbortController();
  const { signal } = controller;
  let panel: ConversationPanel | undefined;
  const unsubscribe: Array<() => void> = [];
  const current = () => !signal.aborted && isRendererLoggedIn() && interactiveOwnerKey() === owner
    && state.activeSessionId === parent && state.activeTab === 'chat' && !isStudioView()
    && productModeStore.get().productMode === 'assistant';
  const pane = { current, focus: () => panel?.focus() };
  const dispose = () => {
    controller.abort();
    for (const unbind of unsubscribe) unbind();
    panel?.dispose();
    host.classList.remove('desktop-side-chat-host');
    if (activePane === pane) activePane = null;
  };
  if (!current() || !openInspectorCustomView(dispose)) return;
  activePane = pane;
  const onScopeChange = () => { if (!signal.aborted && !current()) closeInspector(); };
  unsubscribe.push(sessionStore.subscribe(onScopeChange), authStore.subscribe(onScopeChange),
    uiStore.subscribe(onScopeChange), productModeStore.subscribe(onScopeChange));
  disableInspectorSurfaceAutoWidth();

  const title = document.createElement('strong');
  title.className = 'desktop-side-chat__title';
  title.textContent = '侧边聊天';
  heading.replaceChildren(title);
  host.classList.add('desktop-side-chat-host');
  const status = document.createElement('p');
  status.className = 'desktop-side-chat__status';
  status.setAttribute('role', 'status');
  status.textContent = '正在打开侧边聊天…';
  host.replaceChildren(status);

  try {
    const child = await backendApi.createSideChat(parent, signal);
    if (!current()) return;
    const sessionId = child.session_id;
    if (child.ok !== true || child.source_session_id !== parent || !sessionId || sessionId === parent || !child.workspace_id) {
      throw new Error('网关未返回有效的侧边会话');
    }
    const loaded = await loadBackendHistory(sessionId, { canApply: current });
    if (!current()) return;
    if (!loaded) throw new Error('无法加载侧聊历史，可在会话目录重新打开');
    const conversation = document.createElement('section');
    conversation.className = 'desktop-side-chat';
    conversation.setAttribute('aria-label', '独立侧边聊天');
    conversation.dataset.sideChatSession = sessionId;
    host.replaceChildren(conversation);
    panel = mountConversationPanel(conversation, {
      containerId: 'desktop-side-chat-messages',
      getSessionId: () => sessionId,
      attachments: textOnly,
      actions: {
        submit: (text) => {
          if (!current() || !text.trim()) return Promise.resolve(false);
          if (isBusy(sessionId)) {
            notify('侧边聊天仍在回复，请等它结束或先停止。');
            return Promise.resolve(false);
          }
          return dispatchWs(sessionId, text.trim(), [], {
            preparedSession: { workspaceId: child.workspace_id },
            workDisabledPreferenceIds: [], planActive: false, canStart: current,
          });
        },
        stop: () => { if (current()) stopGeneration(sessionId); },
        cancelEdit: () => {}, editQueueItem: () => {}, steerQueueItem: () => {},
      },
      followupHandlers: {
        onSubmit: (id, answers) => { if (current()) respondToFollowup(sessionId, id, answers); },
        onCancel: (id) => { if (current()) respondToFollowup(sessionId, id); },
      },
      workspaceActions: false,
      autoRender: true,
      composerPlaceholder: '给侧边聊天发送消息',
    });
    panel.render();
    panel.focus();
    void refreshSessions();
  } catch (error) {
    if (!current()) return;
    status.setAttribute('role', 'alert');
    status.textContent = `无法打开侧边聊天：${error instanceof Error ? error.message : String(error)}`;
  }
}

export function disposeDesktopSideChat(): void {
  if (activePane) closeInspector();
}
