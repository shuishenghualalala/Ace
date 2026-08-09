import { createIcon } from '../components/icon';
import {
  getPendingQueue,
  isDynamicKanbanSession,
  movePendingQueueItem,
  removePendingQueueItem,
  setQueueHint,
} from '../state';
import {
  authStore,
  messageStore,
  sessionStore,
  uiStore,
} from '../stores/stores';
import {
  autoresizeTextarea,
  bindComposerIme,
  createComposerImeState,
  resetTextareaHeight,
  shouldComposerSend,
} from './composer-input';
import { serializeMentionInput } from './composer-mention';
import { registerPrimaryComposerRoot } from './composer-scope';
import type { PanelAttachments } from './attachments';

const MAX_INPUT_HEIGHT = 180;

export interface ComposerViewOptions {
  submit(text: string): void | Promise<void>;
  stop(): void;
  cancelEdit(): void;
  editQueueItem(sessionId: string, index: number): void;
  steerQueueItem(sessionId: string, index: number): void;
  isCompletionOpen?(): boolean;
  /** 本实例的会话来源（缺省 = 全局活跃会话）；Wiki 问答面板注入自己的 resolver。 */
  getSessionId?(): string | null;
  /** 附件流来源（缺省 = 主对话全局 messageStore.attachments）；Wiki 问答面板注入自己的 adapter。 */
  attachments?: PanelAttachments;
  /** 输入框占位文案（缺省「输入消息...」）；Wiki 问答面板注入自己的文案。 */
  placeholder?: string;
  /** 主对话 Composer 置 true：注册为 primary root，供 composer-scope 的 scope 查询锚定。 */
  primary?: boolean;
}

export interface ComposerView {
  refresh(): void;
  renderQueue(): void;
  dispose(): void;
}

type ComposerContextSlot =
  | 'project'
  | 'before-input'
  | 'input-overlay'
  | 'toolbar-left'
  | 'toolbar-right'
  | 'approval';

function createActionButton(
  label: string,
  icon: 'icon-send' | 'icon-stop',
  dataName: 'composerSend' | 'composerStop',
): HTMLButtonElement {
  const button = document.createElement('button');
  button.type = 'button';
  const action = dataName === 'composerSend' ? 'send' : 'stop';
  button.className = `mw-button mw-button--icon mw-button--${action === 'send' ? 'primary' : 'danger'} mw-composer__action mw-composer__action--${action}`;
  button.dataset[dataName] = '';
  button.title = label;
  button.setAttribute('aria-label', label);
  button.append(createIcon(icon, { className: 'mw-composer__action-icon', size: 18 }));
  return button;
}

function createQueueButton(
  label: string,
  data: Record<string, string>,
  icon?: 'icon-back' | 'icon-close' | 'icon-chevron-up' | 'icon-chevron-down',
): HTMLButtonElement {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'mw-composer-queue__action';
  button.title = label;
  button.setAttribute('aria-label', label);
  Object.assign(button.dataset, data);
  if (icon) button.append(createIcon(icon, { size: 16 }));
  else button.textContent = label;
  return button;
}

export function createComposerView(
  host: HTMLElement,
  options: ComposerViewOptions,
  contextStaging?: HTMLElement | null,
): ComposerView {
  const controller = new AbortController();
  // 多实例共存（主对话 + Wiki 问答面板）：不再使用全局唯一 id，
  // 槽位一律用 class 标记，外部经 composer-scope 在 primary root 内 scope 查询。
  const getSessionId = options.getSessionId ?? (() => sessionStore.get().activeSessionId);
  const root = document.createElement('section');
  root.className = 'chat-composer mw-composer';
  root.dataset.composerView = '';

  const queueSlot = document.createElement('div');
  queueSlot.className = 'chat-queue-slot mw-composer__queue-slot';
  const todoSlot = document.createElement('div');
  todoSlot.className = 'chat-todo-slot';
  const runningSlot = document.createElement('div');
  runningSlot.className = 'chat-running-intro';

  const editBanner = document.createElement('div');
  editBanner.className = 'composer-edit-banner mw-composer__edit-banner';
  editBanner.dataset.composerEditBanner = '';
  editBanner.hidden = true;
  editBanner.setAttribute('role', 'status');
  const editText = document.createElement('span');
  editText.textContent = '正在编辑消息，修改后重新发送';
  const cancelEdit = document.createElement('button');
  cancelEdit.type = 'button';
  cancelEdit.id = 'composer-edit-cancel';
  cancelEdit.dataset.composerEditCancel = '';
  cancelEdit.title = '取消编辑';
  cancelEdit.setAttribute('aria-label', '取消编辑');
  cancelEdit.append(createIcon('icon-close', { size: 16 }));
  editBanner.append(editText, cancelEdit);

  const panel = document.createElement('div');
  panel.className = 'chat-input-container mw-composer__panel';

  const inputShell = document.createElement('div');
  inputShell.className = 'mw-composer__input-shell';

  const project = document.createElement('div');
  project.className = 'mw-composer__project';
  project.dataset.composerContextTarget = 'project';
  project.hidden = true;
  const beforeInput = document.createElement('div');
  beforeInput.className = 'mw-composer__context-before';
  beforeInput.dataset.composerContextTarget = 'before-input';
  const inputRow = document.createElement('div');
  inputRow.className = 'chat-input-row mw-composer__input-row';
  const input = document.createElement('textarea');
  input.dataset.composerInput = '';
  input.rows = 1;
  input.placeholder = options.placeholder ?? '输入消息...';
  input.setAttribute('aria-label', '输入消息');
  const inputOverlay = document.createElement('div');
  inputOverlay.className = 'mw-composer__input-overlay';
  inputOverlay.dataset.composerContextTarget = 'input-overlay';
  inputRow.append(input, inputOverlay);

  const toolbar = document.createElement('div');
  toolbar.className = 'chat-input-toolbar mw-composer__toolbar';
  toolbar.id = 'chat-input-toolbar';
  const toolbarLeft = document.createElement('div');
  toolbarLeft.className = 'chat-input-toolbar-left mw-composer__toolbar-left';
  toolbarLeft.dataset.composerContextTarget = 'toolbar-left';
  const toolbarRight = document.createElement('div');
  toolbarRight.className = 'chat-input-toolbar-right mw-composer__toolbar-right';
  toolbarRight.dataset.composerContextTarget = 'toolbar-right';
  const controls = document.createElement('div');
  controls.className = 'input-controls composer-controls mw-composer__controls';
  controls.id = 'composer-controls';
  const stop = createActionButton('停止', 'icon-stop', 'composerStop');
  stop.id = 'chat-stop-btn';
  stop.hidden = true;
  const send = createActionButton('发送', 'icon-send', 'composerSend');
  send.id = 'chat-send-btn';
  send.append(createIcon('loading-frame', {
    className: 'mw-composer__action-loading',
    size: 18,
  }));
  controls.append(stop, send);
  toolbarRight.append(controls);
  toolbar.append(toolbarLeft, toolbarRight);

  const status = document.createElement('p');
  status.className = 'mw-composer__status';
  status.dataset.composerStatus = '';
  status.id = 'composer-status';
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');
  input.setAttribute('aria-describedby', status.id);

  const approval = document.createElement('div');
  approval.className = 'mw-composer__approval';
  approval.dataset.composerContextTarget = 'approval';
  panel.append(beforeInput, inputRow, toolbar, status, approval);
  inputShell.append(project, panel);
  root.append(queueSlot, runningSlot, todoSlot, editBanner, inputShell);
  host.replaceChildren(root);
  // 主对话 Composer 注册为 primary root：composer-scope 的 scope 查询都锚定到它，
  // 避免与 Wiki 问答面板的 Composer 实例混淆。
  if (options.primary) registerPrimaryComposerRoot(root);

  const movedContext: Array<{ source: HTMLElement; nodes: Node[] }> = [];
  if (contextStaging) {
    const targets = new Map<ComposerContextSlot, HTMLElement>([
      ['project', project],
      ['before-input', beforeInput],
      ['input-overlay', inputOverlay],
      ['toolbar-left', toolbarLeft],
      ['toolbar-right', toolbarRight],
      ['approval', approval],
    ]);
    contextStaging
      .querySelectorAll<HTMLElement>('[data-composer-context-source]')
      .forEach((source) => {
        const slot = source.dataset.composerContextSource as ComposerContextSlot | undefined;
        const target = slot ? targets.get(slot) : undefined;
        if (!target) return;
        const nodes = [...source.childNodes];
        target.append(...nodes);
        movedContext.push({ source, nodes });
      });
    contextStaging.hidden = true;
    toolbarRight.append(controls);
  }

  let disposed = false;
  let submitting = false;
  let submitError = '';
  let queueSignature = '';
  const imeState = createComposerImeState();
  const unbindIme = bindComposerIme(input, imeState);

  const renderQueue = (): void => {
    if (disposed) return;
    const sessionId = getSessionId();
    const queue = sessionId ? getPendingQueue(sessionId) : [];
    const canSteer = Boolean(sessionId && !isDynamicKanbanSession(sessionId));
    const signature = `${sessionId ?? ''}|${canSteer}|${queue.map((item) =>
      `${item.id}:${item.query}:${item.optimisticUserMessageId ?? ''}`).join('|')}`;
    if (signature === queueSignature) return;
    queueSignature = signature;
    const focused = queueSlot.contains(document.activeElement)
      ? (document.activeElement as HTMLElement).dataset.composerQueueFocus
      : undefined;
    const list = document.createElement('div');
    list.className = 'mw-composer-queue';
    list.setAttribute('role', 'list');
    list.setAttribute('aria-label', '待发消息');
    let visibleIndex = 0;
    queue.forEach((item, originalIndex) => {
      if (item.optimisticUserMessageId) return;
      visibleIndex += 1;
      const row = document.createElement('article');
      row.className = 'mw-composer-queue__item';
      row.dataset.composerQueueItem = '';
      row.dataset.queueIndex = String(originalIndex);
      row.setAttribute('role', 'listitem');

      const order = document.createElement('span');
      order.className = 'mw-composer-queue__order';
      order.textContent = String(visibleIndex);
      order.setAttribute('aria-label', `等待队列第 ${visibleIndex} 条`);
      const text = document.createElement('span');
      text.className = 'mw-composer-queue__text';
      text.textContent = item.query;
      text.title = item.query;
      const actions = document.createElement('div');
      actions.className = 'mw-composer-queue__actions';
      if (canSteer) {
        actions.append(createQueueButton(
          '引导',
          { queueSteer: String(originalIndex), composerQueueFocus: `${item.id}:steer` },
          'icon-back',
        ));
      }
      actions.append(
        createQueueButton(
          '编辑',
          { queueEdit: String(originalIndex), composerQueueFocus: `${item.id}:edit` },
        ),
        createQueueButton(
          '删除',
          { queueRemove: String(originalIndex), composerQueueFocus: `${item.id}:remove` },
          'icon-close',
        ),
        createQueueButton(
          '上移',
          { queueMove: String(originalIndex), queueMoveDir: '-1', composerQueueFocus: `${item.id}:up` },
          'icon-chevron-up',
        ),
        createQueueButton(
          '下移',
          { queueMove: String(originalIndex), queueMoveDir: '1', composerQueueFocus: `${item.id}:down` },
          'icon-chevron-down',
        ),
      );
      actions.querySelector<HTMLButtonElement>('[data-queue-move-dir="-1"]')!.disabled =
        originalIndex === 0;
      actions.querySelector<HTMLButtonElement>('[data-queue-move-dir="1"]')!.disabled =
        originalIndex === queue.length - 1;
      row.append(order, text, actions);
      list.append(row);
    });
    queueSlot.replaceChildren(...(visibleIndex ? [list] : []));
    if (focused) {
      queueSlot.querySelector<HTMLElement>(
        `[data-composer-queue-focus="${CSS.escape(focused)}"]`,
      )?.focus();
    }
  };

  const refresh = (): void => {
    if (disposed) return;
    const session = sessionStore.get();
    const messages = messageStore.get();
    const sessionId = getSessionId();
    const busy = Boolean(sessionId && session.busySessions[sessionId]);
    // hasDraft 的附件部分走面板注入的 adapter（缺省 = 主对话全局附件流）。
    const attachmentCount = options.attachments
      ? options.attachments.list().length
      : messages.attachments.length;
    const hasDraft = Boolean(input.value.trim() || attachmentCount);
    const blocked = !authStore.get().isLoggedIn || !uiStore.get().backendConnected;
    input.disabled = blocked;
    send.disabled = blocked || submitting || !hasDraft;
    send.hidden = busy && !hasDraft;
    stop.hidden = !busy || hasDraft;
    send.title = busy ? '加入队列' : '发送';
    send.setAttribute('aria-label', send.title);
    send.setAttribute('aria-busy', submitting ? 'true' : 'false');
    send.dataset.loading = submitting ? 'true' : 'false';
    editBanner.hidden = !(sessionId && session.editFromIdx[sessionId] != null);
    status.textContent = submitError || (!authStore.get().isLoggedIn
      ? '登录后可发送消息'
      : (!uiStore.get().backendConnected ? '服务离线' : ''));
    renderQueue();
  };

  const submit = async (): Promise<void> => {
    if (submitting || send.disabled) return;
    submitting = true;
    submitError = '';
    refresh();
    try {
      await options.submit(serializeMentionInput(input.value));
      input.value = '';
      resetTextareaHeight(input);
      input.dispatchEvent(new Event('input', { bubbles: true }));
    } catch (error) {
      submitError = `发送失败：${(error as Error).message}`;
    } finally {
      submitting = false;
      refresh();
      input.focus();
    }
  };

  input.addEventListener('input', () => {
    submitError = '';
    autoresizeTextarea(input, MAX_INPUT_HEIGHT);
    refresh();
  }, { signal: controller.signal });
  input.addEventListener('keydown', (event) => {
    if (!shouldComposerSend(event, imeState) || options.isCompletionOpen?.()) return;
    event.preventDefault();
    void submit();
  }, { signal: controller.signal });
  send.addEventListener('click', () => { void submit(); }, { signal: controller.signal });
  stop.addEventListener('click', options.stop, { signal: controller.signal });
  cancelEdit.addEventListener('click', options.cancelEdit, { signal: controller.signal });
  queueSlot.addEventListener('click', (event) => {
    const target = event.target as HTMLElement;
    const sessionId = getSessionId();
    if (!sessionId) return;
    const remove = target.closest<HTMLElement>('[data-queue-remove]');
    if (remove) {
      removePendingQueueItem(sessionId, Number(remove.dataset.queueRemove));
      if (getPendingQueue(sessionId).length === 0) setQueueHint(sessionId, '');
      return;
    }
    const steer = target.closest<HTMLElement>('[data-queue-steer]');
    if (steer) {
      options.steerQueueItem(sessionId, Number(steer.dataset.queueSteer));
      return;
    }
    const edit = target.closest<HTMLElement>('[data-queue-edit]');
    if (edit) {
      options.editQueueItem(sessionId, Number(edit.dataset.queueEdit));
      return;
    }
    const move = target.closest<HTMLElement>('[data-queue-move]');
    if (move) {
      const index = Number(move.dataset.queueMove);
      movePendingQueueItem(sessionId, index, index + Number(move.dataset.queueMoveDir));
    }
  }, { signal: controller.signal });
  const unsubscribeAuth = authStore.subscribe(refresh);
  const unsubscribeUi = uiStore.subscribe(refresh);
  const unsubscribeSession = sessionStore.subscribe(refresh);
  const unsubscribeMessages = messageStore.subscribe(refresh);
  refresh();

  return {
    refresh,
    renderQueue,
    dispose() {
      if (disposed) return;
      disposed = true;
      if (options.primary) registerPrimaryComposerRoot(null);
      controller.abort();
      unbindIme();
      unsubscribeAuth();
      unsubscribeUi();
      unsubscribeSession();
      unsubscribeMessages();
      for (const { source, nodes } of movedContext) source.append(...nodes);
      host.replaceChildren();
    },
  };
}
