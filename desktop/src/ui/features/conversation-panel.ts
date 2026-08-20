/**
 * 对话面板组装（重构计划步骤 5）：消息区（conversation-renderer）+ Composer（composer-view）
 * + 附件预览 / todo 槽位接线。主对话（app.ts）与 Wiki 问答面板（wiki-agent）各挂一个实例，
 * 面板差异（会话来源、空态、followup、模型 chip 等扩展）全部经 options 注入。
 *
 * 两种容器模式：
 *  - 接管既有容器（主对话）：传 resolveMessages + composerHost，面板不自建 DOM；
 *    渲染仍由 chat-controller.renderChat 驱动（可见性逻辑在那里），panel.render 不调用。
 *  - 自建容器（Wiki 面板）：传 containerId，面板在 host 内自建消息区 + Composer 挂载点，
 *    autoRender 订阅 messageStore/sessionStore 自动重渲染。
 */

import { renderTodoProgressPanelHtml, shouldShowTodoPanel } from '../chat-render';
import { createChatRenderCoalescer } from '../render-utils';
import {
  ensureSessionBook,
  setEditFrom,
  state,
  truncateMessagesFrom,
} from '../state';
import { messageStore, sessionStore } from '../stores/stores';
import { requireRendererLogin } from './auth-gate';
import {
  renderAttachmentList,
  type PanelAttachments,
} from './attachments';
import {
  cancelEdit,
  editQueueItem,
  sendMessage,
  steerQueuedItem,
  stopGeneration,
} from './chat-controller';
import { createComposerView, type ComposerView } from './composer-view';
import type { UserAgentMention } from './composer-mention';
import {
  disposeConversationRenderer,
  renderConversation,
  type ConversationRenderHooks,
} from './conversation-renderer';
import { getToolFold, setToolFold } from './fold-state';

/** Composer 动作组：submit/stop/edit/queue（app.ts 主对话原接线逻辑的参数化形态）。 */
export interface ConversationPanelActions {
  submit(text: string, userMentions?: UserAgentMention[]): void | Promise<void>;
  stop(): void;
  cancelEdit(): void;
  editQueueItem(sessionId: string, index: number): void;
  steerQueueItem(sessionId: string, index: number): void;
  isCompletionOpen?(): boolean;
}

export interface ConversationPanelOptions {
  /** 本面板的会话来源。 */
  getSessionId(): string | null;
  /** 附件流 adapter。 */
  attachments: PanelAttachments;
  actions: ConversationPanelActions;
  /** 接管模式：每次渲染时解析消息容器（主对话随 studio 模式切换容器 id）。 */
  resolveMessages?: () => { container: HTMLElement; containerId: string } | null;
  /** 自建模式：消息容器的 id（diff 缓存 / scroll anchor 按它分键，须全局唯一）。 */
  containerId?: string;
  /** 接管模式：既有 Composer 挂载点（缺省 = 面板在 host 尾部自建）。 */
  composerHost?: HTMLElement;
  contextStaging?: HTMLElement | null;
  /** 主对话 Composer 置 true：注册 primary root（composer-scope 查询锚点）。 */
  primary?: boolean;
  emptyState?: ConversationRenderHooks['emptyState'];
  followupHandlers?: ConversationRenderHooks['followupHandlers'];
  /** 渲染后钩子（主对话：inspector 角标 / messages:changed 等 main-only 副作用）。 */
  afterRender?: (sessionId: string | null) => void;
  /** todo 槽位 fold key（提供则面板在每次渲染后刷新自己 Composer 的 todo 槽位）。 */
  todoFoldKey?: (sessionId: string) => string;
  /** Composer 输入框占位文案（缺省「输入消息...」）。 */
  composerPlaceholder?: string;
  /** 订阅 messageStore/sessionStore 自动重渲染（自建模式用；主对话走既有 renderChat 链路）。 */
  autoRender?: boolean;
}

export interface ConversationPanel {
  /** Composer root（scope 查询 / 绑定扩展按钮用）。 */
  readonly composerRoot: HTMLElement;
  /** 自建模式下的消息容器（接管模式为 null）。 */
  readonly messagesEl: HTMLElement | null;
  render(): void;
  /** Composer 刷新 + 附件预览重绘。 */
  refresh(): void;
  focus(): void;
  dispose(): void;
}

/**
 * 主对话 Composer 动作组（app.ts:230 原接线逻辑抽出）：编辑态截断后重发 + 全局 stop/queue 操作。
 * 语义与抽离前完全一致（会话来源仍是全局 activeSessionId）。
 */
export function createMainComposerActions(
  isCompletionOpen?: () => boolean,
): ConversationPanelActions {
  return {
    submit: async (text, userMentions) => {
      if (!requireRendererLogin()) return;
      const sessionId = state.activeSessionId;
      if (sessionId && state.editFromIdx[sessionId] != null) {
        const removed = truncateMessagesFrom(sessionId, state.editFromIdx[sessionId]);
        setEditFrom(sessionId, null);
        for (const message of removed) {
          state.userFoldedTurns.delete(message.id);
          state.userUnfoldedTurns.delete(message.id);
        }
      }
      await sendMessage(text, userMentions);
    },
    stop: () => stopGeneration(),
    cancelEdit,
    editQueueItem,
    steerQueueItem: steerQueuedItem,
    ...(isCompletionOpen ? { isCompletionOpen } : {}),
  };
}

export function mountConversationPanel(
  host: HTMLElement,
  opts: ConversationPanelOptions,
): ConversationPanel {
  // ── 消息区 ──
  let messagesEl: HTMLElement | null = null;
  if (!opts.resolveMessages) {
    if (!opts.containerId) throw new Error('ConversationPanel: 自建模式必须提供 containerId');
    messagesEl = document.createElement('div');
    messagesEl.className = 'chat-messages web-flow conversation-panel__messages';
    messagesEl.id = opts.containerId;
  }

  // ── Composer ──
  const ownComposerHost = !opts.composerHost;
  const composerHost = opts.composerHost ?? document.createElement('div');
  if (messagesEl) host.append(messagesEl, composerHost);
  else if (ownComposerHost) host.append(composerHost);
  const composer: ComposerView = createComposerView(composerHost, {
    submit: (text, userMentions) => opts.actions.submit(text, userMentions),
    stop: () => opts.actions.stop(),
    cancelEdit: () => opts.actions.cancelEdit(),
    editQueueItem: opts.actions.editQueueItem,
    steerQueueItem: opts.actions.steerQueueItem,
    ...(opts.actions.isCompletionOpen ? { isCompletionOpen: opts.actions.isCompletionOpen } : {}),
    getSessionId: opts.getSessionId,
    attachments: opts.attachments,
    ...(opts.composerPlaceholder ? { placeholder: opts.composerPlaceholder } : {}),
    ...(opts.primary ? { primary: true } : {}),
  }, opts.contextStaging);
  const composerRoot = composerHost.querySelector<HTMLElement>('[data-composer-view]');
  if (!composerRoot) throw new Error('ConversationPanel: Composer root 缺失');

  // ── 附件预览：渲染进 Composer 的 [data-attachment-preview]（before-input 槽位内，
  //    主对话由 composer-context-view 创建，Wiki 面板由自己的 contextStaging 提供）──
  const renderPreview = (): void => {
    const box = composerRoot.querySelector<HTMLElement>('[data-attachment-preview]');
    if (!box) return;
    renderAttachmentList(box, opts.attachments.list(), (attId) => opts.attachments.remove(attId));
  };
  const unsubscribeAttachments = opts.attachments.subscribe(() => {
    renderPreview();
    composer.refresh();
  });
  renderPreview();

  // ── todo 槽位（todoFoldKey 提供时启用；主对话仍走 chat-controller.renderTodoSlot 链路） ──
  const renderPanelTodo = (sessionId: string | null): void => {
    if (!opts.todoFoldKey) return;
    const slot = composerRoot.querySelector<HTMLElement>('.chat-todo-slot');
    if (!slot) return;
    const todos = sessionId ? ensureSessionBook(sessionId).todos : [];
    slot.hidden = !shouldShowTodoPanel(todos);
    if (slot.hidden) {
      slot.innerHTML = '';
      return;
    }
    const foldKey = opts.todoFoldKey(sessionId!);
    const open = getToolFold(foldKey) ?? false;
    slot.innerHTML = renderTodoProgressPanelHtml(todos, open, foldKey);
    const toggle = slot.querySelector<HTMLElement>('[data-todo-panel-toggle]');
    if (toggle) {
      toggle.onclick = () => {
        setToolFold(foldKey, toggle.getAttribute('aria-expanded') !== 'true');
        renderPanelTodo(sessionId);
      };
    }
  };

  // ── 渲染 ──
  // 记录渲染过的容器 id，dispose 时统一释放 diff 缓存与 scroll anchor。
  const renderedContainerIds = new Set<string>();
  const render = (): void => {
    const target = opts.resolveMessages
      ? opts.resolveMessages()
      : (messagesEl ? { container: messagesEl, containerId: opts.containerId! } : null);
    if (!target) return;
    renderedContainerIds.add(target.containerId);
    renderConversation(target.container, target.containerId, opts.getSessionId(), {
      getSessionId: opts.getSessionId,
      ...(opts.emptyState ? { emptyState: opts.emptyState } : {}),
      ...(opts.followupHandlers ? { followupHandlers: opts.followupHandlers } : {}),
      afterRender: (sessionId) => {
        renderPanelTodo(sessionId);
        opts.afterRender?.(sessionId);
      },
    });
  };

  // ── store 订阅自动重渲染（自建模式；rAF 合并，同一帧多次变更只渲染一次） ──
  const scheduleRender = createChatRenderCoalescer(render, (cb) => requestAnimationFrame(cb));
  const unsubscribeStores: Array<() => void> = [];
  if (opts.autoRender) {
    unsubscribeStores.push(
      messageStore.subscribe((next, prev) => {
        const sessionId = opts.getSessionId();
        if (sessionId && next.messages[sessionId] !== prev.messages[sessionId]) scheduleRender();
      }),
      sessionStore.subscribe((next, prev) => {
        const sessionId = opts.getSessionId();
        if (
          sessionId
          && (
            next.busySessions[sessionId] !== prev.busySessions[sessionId]
            || next.books[sessionId] !== prev.books[sessionId]
          )
        ) scheduleRender();
      }),
    );
  }

  let disposed = false;
  return {
    composerRoot,
    messagesEl,
    render,
    refresh() {
      composer.refresh();
      renderPreview();
    },
    focus() {
      composerRoot.querySelector<HTMLTextAreaElement>('[data-composer-input]')?.focus();
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      unsubscribeAttachments();
      for (const unsubscribe of unsubscribeStores) unsubscribe();
      composer.dispose();
      for (const containerId of renderedContainerIds) disposeConversationRenderer(containerId);
      messagesEl?.remove();
      if (ownComposerHost) composerHost.remove();
    },
  };
}
