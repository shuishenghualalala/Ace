/**
 * Wiki Agent 对话：Wiki 页右栏内嵌的专用会话。
 *
 * 链路：Wiki 页「上传」（打开右栏附件选择）/ 失败任务「让 AI 处理」（wiki-page 经
 * setWikiAgentEntryHandler 回调注入，互不 import）→ POST /api/wiki/agent-session 拿独立会话
 * → 右栏面板直接收发 → 发送时 payload 带 wiki_kb_id
 * （chat-controller 经 setWikiSendExtrasResolver 注册口取参数，chat 侧不 import 本模块）
 * → wiki_cards 帧经 reducer 挂到消息渲染。
 *
 * 右栏面板（mountWikiAgentPanel）：面板头（标题 + 展开/收窄）+ 消息区（空态标语）
 * + 卡片式 Composer + 免责声明。Composer 复用主对话组件：composer-input 的 IME 判定与
 * autoresizeTextarea、model-picker 的 openModelSelectPopover（会话级模型切换走
 * PUT /api/session/{id}/model，只影响 Wiki 会话）、composer-chip / chat-action-btn 全局样式。
 * wiki 页 renderShell 重建时，KB 未变则保留面板活节点（不重挂载）；真正重挂载时
 * （切 KB / 上传入口显式触发）草稿（embeddedDrafts）、宽度档位（embeddedExpanded）、
 * 模型缓存（embeddedModelByKb）、焦点（embeddedInputFocused）由模块状态存活并恢复。
 *
 * 会话状态：本地内存 Map（sessionId → { kbId, kbName }），
 * 每条消息显式携带 wiki_kb_id；登录态变化时清空。
 */

import {
  backendApi,
  type Attachment,
  type FollowupAnswer,
  type WikiAgentSessionSummary,
} from '../backend-client';
import { clearRuntimeStyle, setRuntimeStyle } from '../components/runtime-style';
import {
  renderConversationSurface,
  renderTodoProgressPanelHtml,
  shouldShowTodoPanel,
} from '../chat-render';
import { createChatRenderCoalescer } from '../render-utils';
import {
  bindFollowupCard,
  formatFollowupAnswerMessage,
  renderFollowupCardElement,
} from '../followup';
import {
  escapeHtml,
  notify,
  patchBook,
  setBookTodos,
  state,
  type TodoItem,
  type SessionRow,
} from '../state';
import { messageStore, sessionStore } from '../stores/stores';
import { bindFileDrop, bindFilePaste, buildAttachmentChip } from './attachments';
import { requireRendererLogin } from './auth-gate';
import {
  autoresizeTextarea,
  bindComposerIme,
  createComposerImeState,
  resetTextareaHeight,
  shouldComposerSend,
} from './composer-input';
import { openModelSelectPopover } from './model-picker';
import { showConfirmDialog } from '../ui-feedback';
import { applySessionModelBinding, loadSessionModel, modelLabelForId } from './session-model';
import {
  appendMessage,
  bookFor,
  dispatchWs,
  ensureFileChangesDelegation,
  getMessages,
  isBusy,
  openSessionInChat,
  setWikiSendExtrasResolver,
  subscribeSessions,
  stopGeneration,
} from './chat-controller';
import { openInspectorToTab } from './inspector';
import { getToolFold, setToolFold } from './fold-state';
import { resumeSessionGeneration } from './session-busy';
import { loadBackendHistory } from './session-controller';
import {
  mountWikiDetailFold,
  openWikiPageInHub,
  setWikiAgentPanelRenderer,
  toggleWikiBrowser,
  type WikiAgentEntryRequest,
} from './wiki-page';

// ---------- 专用 Wiki Agent 会话状态 ----------

export interface WikiAgentSessionState {
  kbId: string;
  kbName: string;
}

/** sessionId → Wiki Agent 状态；权威身份由后端持久化会话配置决定。 */
const wikiAgentSessions = new Map<string, WikiAgentSessionState>();
const wikiSessionRows = new Map<string, SessionRow>();

/** KB 被删除后清理仅以 kbId 为键的内嵌会话缓存，避免同名重建复用旧会话。 */
export function forgetWikiAgentKb(kbId: string): void {
  const normalized = kbId.trim();
  if (!normalized) return;
  const embedded = embeddedByKb.get(normalized);
  if (embedded) {
    wikiAgentSessions.delete(embedded.sessionId);
    wikiSessionRows.delete(embedded.sessionId);
    sessionStore.set({
      sessions: state.sessions.filter((session) => session.id !== embedded.sessionId),
    });
  }
  embeddedByKb.delete(normalized);
  embeddedLoads.delete(normalized);
  embeddedAttachments.delete(normalized);
  embeddedDrafts.delete(normalized);
  embeddedExpanded.delete(normalized);
  embeddedModelByKb.delete(normalized);
  if (activeEmbeddedKbId === normalized) {
    // 同名 KB 重建时不能让 wiki-page 复用旧面板 DOM，否则不会重新创建/加载会话。
    activeEmbeddedRoot?.replaceChildren();
    activeEmbeddedRoot?.removeAttribute('data-kb-id');
    activeEmbeddedKbId = '';
    activeEmbeddedRoot = null;
  }
}

/** Wiki Agent 会话在后端属于 workspace=wiki（桌面侧栏无此工作空间分组，不占历史列表）。 */
const WIKI_AGENT_WORKSPACE_ID = 'wiki';
const WIKI_AGENT_SESSION_TITLE = 'Wiki Agent';

interface EmbeddedWikiAgentState extends WikiAgentSessionState {
  sessionId: string;
}

const embeddedByKb = new Map<string, EmbeddedWikiAgentState>();
const embeddedLoads = new Map<string, Promise<EmbeddedWikiAgentState>>();
const embeddedAttachments = new Map<string, Attachment[]>();
/** 输入草稿（kbId → 文本）：wiki 页 renderShell 会整体重建面板 DOM，草稿靠它存活。 */
const embeddedDrafts = new Map<string, string>();
/** 已展开为宽栏的 kbId 集合（重挂载后恢复宽度档位）。 */
const embeddedExpanded = new Set<string>();

// ── 右栏对话面板宽度：可拖拽 + 持久化（与 wiki-page 列表栏同一套 createPaneWidthStore / bindPaneSash 机制） ──
function createPaneWidthStore(opts: {
  key: string;
  min: number;
  max?: number;
  vwFactor: number;
}): {
  clamp: (width: number) => number;
  load: () => number | null;
  persist: (width: number | null) => void;
} {
  const clamp = (width: number): number => {
    const viewportMax = Math.max(opts.min, Math.floor(window.innerWidth * opts.vwFactor));
    return Math.max(opts.min, Math.min(opts.max ?? Infinity, viewportMax, Math.round(width)));
  };
  return {
    clamp,
    load: () => {
      try {
        const width = Number(localStorage.getItem(opts.key));
        return Number.isFinite(width) && width > 0 ? clamp(width) : null;
      } catch {
        return null;
      }
    },
    persist: (width) => {
      try {
        if (width === null) localStorage.removeItem(opts.key);
        else localStorage.setItem(opts.key, String(width));
      } catch {
        // localStorage unavailable: keep the current in-memory width.
      }
    },
  };
}

function bindPaneSash(
  sash: HTMLElement,
  options: {
    sign?: 1 | -1;
    startWidth: () => number;
    onStart?: () => void;
    onDrag: (width: number) => void;
    onCommit: (width: number) => void;
    onReset: () => void;
  },
): void {
  sash.addEventListener('mousedown', (event) => {
    const startX = event.clientX;
    const startWidth = options.startWidth();
    let currentWidth = startWidth;
    const onMove = (moveEvent: MouseEvent): void => {
      currentWidth = startWidth + (options.sign ?? 1) * (moveEvent.clientX - startX);
      options.onDrag(currentWidth);
    };
    const onUp = (): void => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      sash.classList.remove('is-dragging');
      document.body.classList.remove('wiki-resizing');
      options.onCommit(currentWidth);
    };
    sash.classList.add('is-dragging');
    document.body.classList.add('wiki-resizing');
    options.onStart?.();
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
    event.preventDefault();
  });
  sash.addEventListener('dblclick', options.onReset);
}

const agentWidthStore = createPaneWidthStore({ key: 'crew.desktop.wikiAgentWidth.v1', min: 280, max: 760, vwFactor: 0.6 });
/** 会话级模型展示缓存（kbId → { id, label }），chip 高亮与浮层选中态共用。 */
const embeddedModelByKb = new Map<string, { id: string; label: string }>();
let activeEmbeddedRoot: HTMLElement | null = null;
let activeEmbeddedKbId = '';
/** 面板每次挂载递增；异步会话加载只允许更新发起它的那次挂载。 */
let embeddedMountVersion = 0;
/** 当前打开的模型浮层关闭函数（面板重建 / 登录态变化时收回）。 */
let embeddedModelPopoverClose: (() => void) | null = null;
/** 面板输入框是否持有焦点（focusin/focusout 全局追踪，重挂载后恢复焦点用）。 */
let embeddedInputFocused = false;

/** 清空右栏面板全部 per-KB 状态（登录态变化 / 测试重置共用，防两处漂移漏清）。 */
function clearEmbeddedPanelState(): void {
  embeddedByKb.clear();
  embeddedLoads.clear();
  embeddedAttachments.clear();
  embeddedDrafts.clear();
  embeddedExpanded.clear();
  embeddedModelByKb.clear();
  embeddedModelPopoverClose?.();
  embeddedModelPopoverClose = null;
  embeddedInputFocused = false;
  activeEmbeddedRoot = null;
  activeEmbeddedKbId = '';
  embeddedMountVersion += 1;
}

/** 确保 state.sessions 里有该会话行（openSession 工作空间归属 / dispatch workspace_id 解析用）。 */
function ensureWikiSessionRow(sessionId: string): void {
  const existing = state.sessions.find((s) => s.id === sessionId);
  if (existing) wikiSessionRows.set(sessionId, existing);
  const missing = Array.from(wikiSessionRows.values()).filter(
    (row) => !state.sessions.some((session) => session.id === row.id),
  );
  if (!missing.length && existing) return;
  sessionStore.set({
    sessions: [
      ...state.sessions,
      ...(existing ? [] : [
        {
          id: sessionId,
          title: WIKI_AGENT_SESSION_TITLE,
          updatedAt: Date.now(),
          preview: '',
          badge: '工作空间',
          workspaceId: WIKI_AGENT_WORKSPACE_ID,
        } satisfies SessionRow,
      ]),
      ...missing,
    ],
  });
  if (!existing) {
    const row = state.sessions.find((session) => session.id === sessionId);
    if (row) wikiSessionRows.set(sessionId, row);
  }
}

/** 激活一个已创建的 Wiki Agent 会话并接入现有消息与订阅。 */
async function activateEmbeddedSession(
  kbId: string,
  sessionId: string,
  kbName = kbId,
): Promise<EmbeddedWikiAgentState> {
  // 会话一旦切换就立即失效旧模型缓存；放在异步历史/Todo 加载之后会与面板重建
  // 并发，出现 chip 已显示新模型但高亮 id 又被迟到清空的竞争。
  embeddedModelByKb.delete(kbId);
  const item: EmbeddedWikiAgentState = {
    sessionId,
    kbId,
    kbName,
  };
  embeddedByKb.set(kbId, item);
  wikiAgentSessions.set(sessionId, item);
  ensureWikiSessionRow(sessionId);
  subscribeSessions([sessionId]);
  await loadBackendHistory(sessionId);
  await loadEmbeddedTodos(sessionId);
  ensureWikiSessionRow(sessionId);
  scheduleEmbeddedRender();
  void loadEmbeddedModel(kbId, sessionId);
  return item;
}

async function loadEmbeddedTodos(sessionId: string): Promise<void> {
  try {
    const { todos } = await backendApi.sessionTodos(sessionId);
    setBookTodos(
      sessionId,
      (todos ?? []).map((todo) => ({
        id: todo.id,
        content: todo.content,
        status: (['pending', 'in_progress', 'completed', 'cancelled'].includes(todo.status)
          ? todo.status
          : 'pending') as TodoItem['status'],
      })),
    );
  } catch {
    // 兼容旧后端或离线状态；运行中的 todo_updated 仍会更新会话 book。
  }
}

function resolveEmbeddedFollowup(
  sessionId: string,
  questionId: string,
  answers: FollowupAnswer[],
): void {
  const pending = bookFor(sessionId).pendingFollowup;
  void state.socket?.send({
    action: 'followup_answer',
    session_id: sessionId,
    question_id: questionId,
    answers,
  });
  if (pending?.recordHistory !== false) {
    patchBook(sessionId, {
      pendingFollowup: null,
      assistantId: null,
      toolMap: new Map(),
      deltaSpans: [],
      legacyDeltaText: '',
    });
    const message = pending ? formatFollowupAnswerMessage(pending, answers) : null;
    if (message) appendMessage(sessionId, 'user', message);
  } else {
    patchBook(sessionId, { pendingFollowup: null });
  }
  resumeSessionGeneration(sessionId);
  scheduleEmbeddedRender();
}

function cancelEmbeddedFollowup(sessionId: string, questionId: string): void {
  void state.socket?.send({
    action: 'followup_cancel',
    session_id: sessionId,
    question_id: questionId,
  });
  patchBook(sessionId, { pendingFollowup: null });
  scheduleEmbeddedRender();
}

function renderEmbeddedTodo(root: HTMLElement, sessionId: string): void {
  const slot = root.querySelector<HTMLElement>('[data-wiki-agent-todo]');
  if (!slot) return;
  const todos = bookFor(sessionId).todos;
  slot.hidden = !shouldShowTodoPanel(todos);
  if (slot.hidden) {
    slot.innerHTML = '';
    return;
  }
  const foldKey = `todo:wiki:${sessionId}`;
  const open = getToolFold(foldKey) ?? false;
  slot.innerHTML = renderTodoProgressPanelHtml(todos, open, foldKey);
  const toggle = slot.querySelector<HTMLElement>('[data-todo-panel-toggle]');
  if (toggle) {
    toggle.onclick = () => {
      setToolFold(foldKey, toggle.getAttribute('aria-expanded') !== 'true');
      renderEmbeddedTodo(root, sessionId);
    };
  }
}

function embeddedState(kbId: string): Promise<EmbeddedWikiAgentState> {
  const cached = embeddedByKb.get(kbId);
  if (cached) return Promise.resolve(cached);
  const pending = embeddedLoads.get(kbId);
  if (pending) return pending;
  let load: Promise<EmbeddedWikiAgentState>;
  load = backendApi.wikiAgentSession(kbId).then(async (res) => {
    if (embeddedLoads.get(kbId) !== load) {
      throw new Error('Wiki Agent 会话加载已失效');
    }
    if (!res.session_id) throw new Error('后端未返回 Wiki Agent 会话');
    return activateEmbeddedSession(kbId, res.session_id);
  }).finally(() => {
    if (embeddedLoads.get(kbId) === load) embeddedLoads.delete(kbId);
  });
  embeddedLoads.set(kbId, load);
  return load;
}

function renderEmbeddedPanel(): void {
  const root = activeEmbeddedRoot;
  if (!root?.isConnected || root.dataset.kbId !== activeEmbeddedKbId) return;
  const body = root.querySelector<HTMLElement>('[data-wiki-agent-messages]');
  const send = root.querySelector<HTMLButtonElement>('[data-wiki-agent-send]');
  const panel = embeddedByKb.get(activeEmbeddedKbId);
  if (!body || !send) return;
  if (!panel) {
    body.innerHTML = '<p class="wiki-agent-pane__empty">正在连接…</p>';
    send.disabled = true;
    return;
  }
  const busy = isBusy(panel.sessionId);
  const messages = getMessages(panel.sessionId);
  const pendingFollowup = bookFor(panel.sessionId).pendingFollowup;
  if (!messages.length && !busy && !pendingFollowup) {
    // 空态：对齐参考设计的居中标语（图标 + 「基于知识库问答」+ 上传引导）。
    body.innerHTML = `
      <div class="wiki-agent-pane__void">
        <span class="wiki-agent-pane__void-icon" aria-hidden="true">${WIKI_VOID_ICON}</span>
        <p class="wiki-agent-pane__void-text">基于知识库问答</p>
        <p class="wiki-agent-pane__void-hint">可直接粘贴或拖拽文件到此处上传</p>
      </div>`;
  } else {
    renderConversationSurface(body, messages, state.configModel, { showAssistantName: false });
    if (pendingFollowup) {
      let inner = body.querySelector<HTMLElement>('.messages__inner');
      if (!inner) {
        inner = document.createElement('div');
        inner.className = 'messages__inner';
        body.replaceChildren(inner);
      }
      inner.appendChild(renderFollowupCardElement(pendingFollowup));
      bindFollowupCard(body, {
        onSubmit: (questionId, answers) =>
          resolveEmbeddedFollowup(panel.sessionId, questionId, answers),
        onCancel: (questionId) => cancelEmbeddedFollowup(panel.sessionId, questionId),
      });
    }
    body.scrollTop = body.scrollHeight;
  }
  const stop = root.querySelector<HTMLButtonElement>('[data-wiki-agent-stop]');
  const preview = root.querySelector<HTMLElement>('[data-wiki-agent-attachments]');
  send.disabled = busy;
  send.hidden = busy;
  if (stop) stop.hidden = !busy;
  renderEmbeddedTodo(root, panel.sessionId);
  if (preview) {
    const attachments = embeddedAttachments.get(activeEmbeddedKbId) || [];
    // 附件预览卡片与主对话完全同款（扩展名图标 + 文件名 + 类型/大小 + 悬浮移除按钮）。
    preview.replaceChildren(...attachments.map((attachment) =>
      buildAttachmentChip(attachment, (attId) => {
        const list = embeddedAttachments.get(activeEmbeddedKbId) || [];
        embeddedAttachments.set(activeEmbeddedKbId, list.filter((item) => item.id !== attId));
        scheduleEmbeddedRender();
      }),
    ));
    preview.hidden = attachments.length === 0;
  }
}

/** rAF 合并渲染：同一帧内多次 schedule 只渲染一次（复用 render-utils 的通用合并器）。 */
const scheduleEmbeddedRender = createChatRenderCoalescer(renderEmbeddedPanel, (cb) => {
  window.requestAnimationFrame(cb);
});

async function sendEmbeddedPrompt(
  text: string,
  wikiConfirmationId = '',
  kbId = activeEmbeddedKbId,
): Promise<void> {
  const query = text.trim();
  const attachments = embeddedAttachments.get(kbId) || [];
  if ((!query && attachments.length === 0) || !kbId) return;
  try {
    const panel = await embeddedState(kbId);
    if (isBusy(panel.sessionId)) {
      notify('Wiki Agent 正在处理上一条消息');
      return;
    }
    embeddedAttachments.set(kbId, []);
    await dispatchWs(panel.sessionId, query, attachments, {
      planActive: false,
      ...(wikiConfirmationId ? { wikiConfirmationId } : {}),
    });
    scheduleEmbeddedRender();
  } catch (err) {
    notify(`发送 Wiki 问答失败：${(err as Error).message}`);
  }
}

function readFileAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || '').split(',').pop() || '');
    reader.onerror = () => reject(reader.error || new Error('读取文件失败'));
    reader.readAsDataURL(file);
  });
}

async function addEmbeddedFiles(files: FileList | File[] | null): Promise<void> {
  if (!files?.length || !activeEmbeddedKbId) return;
  // 多文件并行上传（单个失败只提示、不阻断其余），成功结果保持原顺序追加。
  const uploaded = await Promise.all(
    Array.from(files).map(async (file) => {
      try {
        return await backendApi.upload(file.name, await readFileAsBase64(file));
      } catch (err) {
        notify(`上传失败：${file.name} · ${(err as Error).message}`);
        return null;
      }
    }),
  );
  const next = [...(embeddedAttachments.get(activeEmbeddedKbId) || [])];
  for (const item of uploaded) {
    if (item) next.push(item);
  }
  embeddedAttachments.set(activeEmbeddedKbId, next);
  scheduleEmbeddedRender();
}

// ── 右栏 Composer（对齐主对话卡片式 Composer，复用 composer-chip / chat-action-btn 全局样式） ──

const WIKI_INPUT_MAX_HEIGHT = 140;

const WIKI_VOID_ICON = `<svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/><circle cx="8.5" cy="11.5" r=".4" fill="currentColor"/><circle cx="12" cy="11.5" r=".4" fill="currentColor"/><circle cx="15.5" cy="11.5" r=".4" fill="currentColor"/></svg>`;
const WIKI_EXPAND_ICON = `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 3h6v6"/><path d="M9 21H3v-6"/><path d="M21 3l-7 7"/><path d="M3 21l7-7"/></svg>`;
const WIKI_NEW_CHAT_ICON = `<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h6"/><path d="M18 2v6"/><path d="M15 5h6"/></svg>`;
const WIKI_HISTORY_ICON = `<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>`;
const WIKI_HISTORY_DELETE_ICON = `<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>`;
const WIKI_MODEL_ICON = `<svg class="composer-chip__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="15" height="15" aria-hidden="true"><path d="M12 2a10 10 0 1 0 10 10 4 4 0 0 1-5-5 4 4 0 0 1-5-5"/><path d="M8.5 8.5v.01"/><path d="M16 15.5v.01"/><path d="M12 12v.01"/></svg>`;
const WIKI_CHIP_CHEVRON = `<svg class="composer-chip__chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="11" height="11" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>`;
const WIKI_ATTACH_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 18 8.84l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>`;
const WIKI_SEND_ICON = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z"/></svg>`;

/** 同步模型 chip 文案（面板可能已被 renderShell 重建，只在仍是当前 KB 时写）。 */
function syncEmbeddedModelChip(kbId: string): void {
  const root = activeEmbeddedRoot;
  if (!root?.isConnected || root.dataset.kbId !== kbId) return;
  const label = root.querySelector<HTMLElement>('[data-wiki-agent-model-label]');
  if (label) label.textContent = embeddedModelByKb.get(kbId)?.label || '模型';
}

/** 加载 Wiki 会话的会话级模型绑定（loadSessionModel 只写缓存；非活跃会话不会动主 Composer）。 */
async function loadEmbeddedModel(kbId: string, sessionId: string): Promise<void> {
  if (embeddedModelByKb.has(kbId)) {
    syncEmbeddedModelChip(kbId);
    return;
  }
  const binding = await loadSessionModel(sessionId);
  // 历史会话快速切换时，迟到的旧请求不能覆盖当前会话的模型展示。
  if (embeddedByKb.get(kbId)?.sessionId !== sessionId) return;
  const id = binding?.model_profile_id || state.config?.active_model_id || '';
  const label = binding?.model_label || modelLabelForId(id);
  embeddedModelByKb.set(kbId, { id, label });
  syncEmbeddedModelChip(kbId);
}

/** 切换 Wiki 会话模型：走会话级接口（与主对话同一 PUT /api/session/{id}/model），只影响本会话。 */
async function pickEmbeddedModel(kbId: string, modelId: string): Promise<void> {
  const panel = embeddedByKb.get(kbId);
  if (!panel) return;
  try {
    const binding = await backendApi.setSessionModel(panel.sessionId, modelId, {
      workspace_id: WIKI_AGENT_WORKSPACE_ID,
    });
    applySessionModelBinding(panel.sessionId, binding);
    const label = binding.pending_label || binding.model_label || modelLabelForId(modelId);
    embeddedModelByKb.set(kbId, {
      id: binding.pending_model_profile_id || binding.model_profile_id || modelId,
      label,
    });
    syncEmbeddedModelChip(kbId);
    notify(binding.pending || isBusy(panel.sessionId) ? `模型将在下条消息生效：${label}` : `已切换模型：${label}`);
  } catch {
    notify('切换模型失败');
  }
}

function formatWikiHistoryTime(value: number): string {
  if (!value) return '';
  const date = new Date(value < 1_000_000_000_000 ? value * 1000 : value);
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit',
  }).format(date);
}

function renderWikiHistoryList(root: HTMLElement, sessions: WikiAgentSessionSummary[]): void {
  const list = root.querySelector<HTMLElement>('[data-wiki-agent-history-list]');
  if (!list) return;
  const activeId = embeddedByKb.get(root.dataset.kbId || '')?.sessionId;
  if (!sessions.length) {
    list.innerHTML = '<p class="wiki-agent-history__empty">暂无历史对话</p>';
    return;
  }
  list.innerHTML = sessions.map((session) => `
    <div class="wiki-agent-history__item${session.session_id === activeId ? ' is-active' : ''}">
      <button type="button" class="wiki-agent-history__item-switch"
        data-wiki-agent-history-session="${escapeHtml(session.session_id)}">
        <span class="wiki-agent-history__item-title">${escapeHtml(session.title || '新对话')}</span>
        <span class="wiki-agent-history__item-meta">${escapeHtml(formatWikiHistoryTime(session.updated_at))} · ${session.message_count || 0} 条消息</span>
      </button>
      <button type="button" class="wiki-agent-history__item-delete"
        data-wiki-agent-history-delete="${escapeHtml(session.session_id)}"
        title="删除该对话" aria-label="删除该 Wiki 对话">${WIKI_HISTORY_DELETE_ICON}</button>
    </div>`).join('');
}

async function openWikiHistory(root: HTMLElement, kbId: string): Promise<void> {
  const popover = root.querySelector<HTMLElement>('[data-wiki-agent-history-popover]');
  const button = root.querySelector<HTMLElement>('[data-wiki-agent-history]');
  if (!popover || !button) return;
  const opening = popover.hidden;
  popover.hidden = !opening;
  button.classList.toggle('is-active', opening);
  button.setAttribute('aria-expanded', opening ? 'true' : 'false');
  if (!opening) return;
  const list = popover.querySelector<HTMLElement>('[data-wiki-agent-history-list]');
  if (list) list.innerHTML = '<p class="wiki-agent-history__empty">正在加载…</p>';
  try {
    const result = await backendApi.wikiAgentSessions(kbId);
    if (root.isConnected && root.dataset.kbId === kbId) renderWikiHistoryList(root, result.sessions);
  } catch (err) {
    if (list) list.innerHTML = `<p class="wiki-agent-history__empty">加载失败：${escapeHtml((err as Error).message)}</p>`;
  }
}

/** 收起历史对话浮层并复位触发按钮状态（新建 / 切换 / 点中当前会话共用）。 */
function closeHistoryPopover(root: HTMLElement): void {
  const popover = root.querySelector<HTMLElement>('[data-wiki-agent-history-popover]');
  if (popover) popover.hidden = true;
  const button = root.querySelector<HTMLElement>('[data-wiki-agent-history]');
  button?.classList.remove('is-active');
  button?.setAttribute('aria-expanded', 'false');
}

/** 删除指定 Wiki 会话：确认 → 后端删除 → 清本地缓存；删的是当前会话则切到最近一条（无则新建）。 */
async function deleteEmbeddedConversation(root: HTMLElement, req: WikiAgentEntryRequest, sessionId: string): Promise<void> {
  if (isBusy(sessionId)) {
    notify('该对话正在生成，请先停止再删除');
    return;
  }
  const confirmed = await showConfirmDialog({
    title: '删除 Wiki 对话',
    message: '确定要删除这条 Wiki 对话吗？该操作不可撤销。',
    confirmText: '删除',
    cancelText: '取消',
  });
  if (!confirmed) return;
  try {
    await backendApi.deleteSession(sessionId);
  } catch (err) {
    notify(`删除 Wiki 对话失败：${(err as Error).message}`);
    return;
  }
  wikiAgentSessions.delete(sessionId);
  sessionStore.set({
    sessions: state.sessions.filter((session) => session.id !== sessionId),
  });
  if (embeddedByKb.get(req.kbId)?.sessionId === sessionId) {
    try {
      // 复用后端「取最近会话、无则新建」语义，前端不用猜下一条。
      const result = await backendApi.wikiAgentSession(req.kbId);
      await activateEmbeddedSession(req.kbId, result.session_id, req.kbName || req.kbId);
    } catch (err) {
      notify(`切换 Wiki 对话失败：${(err as Error).message}`);
    }
  }
  notify('已删除 Wiki 对话');
  // 浮层仍展开时刷新历史列表。
  const popover = root.querySelector<HTMLElement>('[data-wiki-agent-history-popover]');
  if (popover && !popover.hidden) {
    try {
      const result = await backendApi.wikiAgentSessions(req.kbId);
      if (root.isConnected) renderWikiHistoryList(root, result.sessions);
    } catch {
      // 列表刷新失败不影响删除结果，下次打开浮层会重新拉取。
    }
  }
}

/** 切换到指定 Wiki 会话：清附件/草稿 → 激活会话 → 重置输入框 → 收起历史浮层。 */
async function switchEmbeddedConversation(root: HTMLElement, req: WikiAgentEntryRequest, sessionId: string): Promise<void> {
  embeddedAttachments.set(req.kbId, []);
  embeddedDrafts.set(req.kbId, '');
  await activateEmbeddedSession(req.kbId, sessionId, req.kbName || req.kbId);
  const input = root.querySelector<HTMLTextAreaElement>('[data-wiki-agent-input]');
  if (input) {
    input.value = '';
    resetTextareaHeight(input);
  }
  closeHistoryPopover(root);
}

async function createEmbeddedConversation(root: HTMLElement, req: WikiAgentEntryRequest): Promise<void> {
  const current = embeddedByKb.get(req.kbId);
  if (current && isBusy(current.sessionId)) {
    notify('请先停止当前 Wiki Agent 任务');
    return;
  }
  const button = root.querySelector<HTMLButtonElement>('[data-wiki-agent-new]');
  if (button?.disabled) return;
  if (button) button.disabled = true;
  try {
    const result = await backendApi.wikiAgentSession(req.kbId, { forceNew: true });
    await switchEmbeddedConversation(root, req, result.session_id);
    root.querySelector<HTMLTextAreaElement>('[data-wiki-agent-input]')?.focus();
    notify('已新建 Wiki 对话');
  } catch (err) {
    notify(`新建 Wiki 对话失败：${(err as Error).message}`);
  } finally {
    if (button) button.disabled = false;
  }
}

/** 把指定知识库的持久化 Wiki Agent 会话挂到 Wiki 右栏。 */
export function mountWikiAgentPanel(root: HTMLElement, req: WikiAgentEntryRequest): void {
  // 面板 DOM 随 wiki 页 renderShell 整体重建：先收回挂在旧锚点上的模型浮层。
  embeddedModelPopoverClose?.();
  embeddedModelPopoverClose = null;
  activeEmbeddedRoot = root;
  activeEmbeddedKbId = req.kbId;
  const mountVersion = ++embeddedMountVersion;
  root.dataset.kbId = req.kbId;
  const kbName = req.kbName || req.kbId;
  const expanded = embeddedExpanded.has(req.kbId);
  root.classList.toggle('wiki-agent-pane--wide', expanded);
  const customWidth = agentWidthStore.load();
  if (customWidth != null) setRuntimeStyle(root, 'width', `${customWidth}px`);
  else clearRuntimeStyle(root, 'width');
  root.innerHTML = `
    <header class="wiki-agent-pane__header">
      <span class="wiki-agent-pane__title" title="${escapeHtml(kbName)}">Wiki 问答 · ${escapeHtml(kbName)}</span>
      <div class="wiki-agent-pane__header-actions">
        <button type="button" class="wiki-agent-pane__icon-btn" data-wiki-agent-new title="新建对话" aria-label="新建 Wiki 对话">${WIKI_NEW_CHAT_ICON}</button>
        <button type="button" class="wiki-agent-pane__icon-btn" data-wiki-agent-history title="查看历史" aria-label="查看 Wiki 对话历史" aria-haspopup="dialog" aria-expanded="false">${WIKI_HISTORY_ICON}</button>
        <button type="button" class="wiki-agent-pane__icon-btn${expanded ? ' is-active' : ''}" data-wiki-agent-expand title="展开 / 收窄对话栏" aria-label="展开或收窄对话栏">${WIKI_EXPAND_ICON}</button>
      </div>
    </header>
    <section class="wiki-agent-history" data-wiki-agent-history-popover role="dialog" aria-label="Wiki 对话历史" hidden>
      <div class="wiki-agent-history__heading">历史对话</div>
      <div class="wiki-agent-history__list" data-wiki-agent-history-list></div>
    </section>
    <div class="wiki-agent-pane__messages chat-messages web-flow" data-wiki-agent-messages></div>
    <div class="wiki-agent-pane__todo" data-wiki-agent-todo hidden></div>
    <form class="wiki-agent-pane__composer" data-wiki-agent-form>
      <input type="file" data-wiki-agent-file multiple hidden />
      <div class="chat-attachment-preview" data-wiki-agent-attachments hidden></div>
      <textarea data-wiki-agent-input rows="1" placeholder="基于知识库提问" aria-label="Wiki 对话输入框"></textarea>
      <div class="wiki-agent-pane__toolbar">
        <button type="button" class="composer-chip wiki-agent-pane__model" data-wiki-agent-model title="选择模型" aria-haspopup="listbox" aria-expanded="false">
          ${WIKI_MODEL_ICON}
          <span class="composer-chip__label" data-wiki-agent-model-label>${escapeHtml(embeddedModelByKb.get(req.kbId)?.label || '模型')}</span>
          ${WIKI_CHIP_CHEVRON}
        </button>
        <div class="wiki-agent-pane__toolbar-right">
          <button type="button" class="chat-action-btn chat-attach-inline" data-wiki-agent-attach title="添加附件" aria-label="添加附件">${WIKI_ATTACH_ICON}</button>
          <button type="button" class="chat-action-btn chat-stop-btn" data-wiki-agent-stop hidden title="停止生成" aria-label="停止">■</button>
          <button type="submit" class="chat-action-btn send-btn" data-wiki-agent-send title="发送" aria-label="发送">${WIKI_SEND_ICON}</button>
        </div>
      </div>
    </form>
    <p class="wiki-agent-pane__disclaimer">内容由 AI 生成，请仔细甄别</p>`;
  // 「已编辑文件」卡 / 链接 / 浏览器产物点击委托：消息区由 renderConversationSurface 整体重建，
  // 委托绑在面板 root 上（每次重挂载的新 root 各绑一次，WeakSet 不持有旧 DOM）。
  ensureFileChangesDelegation(root);
  // 「查看」/文件行点击：Wiki 页没有看板（openInspectorToTab 仅限 chat 页，点了会静默无效），
  // 这里捕获阶段截获，跳到主聊天区打开同一 Wiki 会话，再展开 Files 看板定位到该文件。
  root.addEventListener('click', (event) => {
    const target = event.target instanceof Element ? event.target : null;
    const openBtn = target?.closest<HTMLElement>('[data-file-changes-open]');
    if (!openBtn || !root.contains(openBtn)) return;
    const sessionId = embeddedByKb.get(root.dataset.kbId || '')?.sessionId;
    if (!sessionId) return;
    event.preventDefault();
    event.stopPropagation();
    const expandFilePath = openBtn.getAttribute('data-file-changes-path');
    ensureWikiSessionRow(sessionId);
    void openSessionInChat(sessionId).then(() => {
      openInspectorToTab('files', { expandFilePath });
    });
  }, true);
  const form = root.querySelector<HTMLFormElement>('[data-wiki-agent-form]');
  const input = root.querySelector<HTMLTextAreaElement>('[data-wiki-agent-input]');
  const fileInput = root.querySelector<HTMLInputElement>('[data-wiki-agent-file]');
  const modelBtn = root.querySelector<HTMLElement>('[data-wiki-agent-model]');
  root.querySelector('[data-wiki-agent-new]')?.addEventListener('click', () => {
    void createEmbeddedConversation(root, req);
  });
  root.querySelector('[data-wiki-agent-history]')?.addEventListener('click', () => {
    void openWikiHistory(root, req.kbId);
  });
  root.querySelector('[data-wiki-agent-attach]')?.addEventListener('click', () => fileInput?.click());
  fileInput?.addEventListener('change', () => {
    void addEmbeddedFiles(fileInput.files);
    fileInput.value = '';
  });
  // 粘贴 / 拖拽上传：与主对话同一套绑定（attachments.ts），上传走 addEmbeddedFiles。
  // 面板随 renderShell 重建后是新 DOM，需每次挂载重新绑定。
  if (input) bindFilePaste(input, (files) => void addEmbeddedFiles(files));
  if (root) bindFileDrop(root, (files) => void addEmbeddedFiles(files));
  root.querySelector('[data-wiki-agent-expand]')?.addEventListener('click', (event) => {
    const btn = event.currentTarget as HTMLElement;
    const next = !embeddedExpanded.has(req.kbId);
    if (next) embeddedExpanded.add(req.kbId);
    else embeddedExpanded.delete(req.kbId);
    // 展开/收窄走 CSS 档位：清掉拖拽产生的内联宽度与持久化值，避免两类宽度打架
    agentWidthStore.persist(null);
    clearRuntimeStyle(root, 'width');
    root.classList.toggle('wiki-agent-pane--wide', next);
    btn.classList.toggle('is-active', next);
    toggleWikiBrowser();
  });
  // ── 对话栏宽度拖拽：手柄是 .wiki-body 内、面板左侧的 flex 兄弟节点（由 wiki-page 渲染） ──
  // 面板与手柄节点在 renderShell 间被保留时已绑定过（enterWikiAgentMode 会在活面板上重复挂载），跳过防重复绑定。
  const agentSash = root.parentElement?.querySelector<HTMLElement>('[data-wiki-agent-sash]');
  if (agentSash && !agentSash.dataset.wikiSashBound) {
    agentSash.dataset.wikiSashBound = '1';
    bindPaneSash(agentSash, {
      sign: -1, // 手柄在面板左缘：向左拖变宽、向右拖变窄
      startWidth: () => root.getBoundingClientRect().width,
      onStart: () => {
        root.classList.add('wiki-agent-pane--resizing');
        // 拖拽后脱离「展开」档位语义，宽度完全由内联样式接管
        if (embeddedExpanded.has(req.kbId)) {
          embeddedExpanded.delete(req.kbId);
          root.classList.remove('wiki-agent-pane--wide');
          root.querySelector('[data-wiki-agent-expand]')?.classList.remove('is-active');
        }
      },
      onDrag: (w) => {
        setRuntimeStyle(root, 'width', `${agentWidthStore.clamp(w)}px`);
      },
      onCommit: (w) => {
        root.classList.remove('wiki-agent-pane--resizing');
        agentWidthStore.persist(agentWidthStore.clamp(w));
      },
      // 双击复位默认宽度（回退到 CSS clamp 档位）
      onReset: () => {
        agentWidthStore.persist(null);
        clearRuntimeStyle(root, 'width');
      },
    });
  }
  modelBtn?.addEventListener('click', (event) => {
    event.stopPropagation();
    if (embeddedModelPopoverClose) {
      embeddedModelPopoverClose();
      return;
    }
    modelBtn.classList.add('is-open');
    modelBtn.setAttribute('aria-expanded', 'true');
    embeddedModelPopoverClose = openModelSelectPopover({
      anchor: modelBtn,
      activeId: embeddedModelByKb.get(req.kbId)?.id || state.config?.active_model_id || '',
      width: 300,
      onPick: (id) => void pickEmbeddedModel(req.kbId, id),
      onClose: () => {
        embeddedModelPopoverClose = null;
        modelBtn.classList.remove('is-open');
        modelBtn.setAttribute('aria-expanded', 'false');
      },
    });
  });
  root.querySelector('[data-wiki-agent-stop]')?.addEventListener('click', () => {
    const panel = embeddedByKb.get(req.kbId);
    if (panel) stopGeneration(panel.sessionId);
  });
  root.addEventListener('click', (event) => {
    const target = event.target instanceof Element ? event.target : null;
    // 消息复制按钮的全局委托绑在 #chat-messages 上，够不到本面板，这里补一份。
    const copyBtn = target?.closest<HTMLElement>('.chat-copy-btn');
    if (copyBtn) {
      void navigator.clipboard.writeText(copyBtn.getAttribute('data-copy') ?? '').then(() => notify('已复制'));
      return;
    }
    const deleteSessionId = target?.closest<HTMLElement>('[data-wiki-agent-history-delete]')
      ?.dataset.wikiAgentHistoryDelete;
    if (deleteSessionId) {
      void deleteEmbeddedConversation(root, req, deleteSessionId);
      return;
    }
    const historySessionId = target?.closest<HTMLElement>('[data-wiki-agent-history-session]')
      ?.dataset.wikiAgentHistorySession;
    if (historySessionId) {
      const current = embeddedByKb.get(req.kbId);
      if (current?.sessionId === historySessionId) {
        closeHistoryPopover(root);
        return;
      }
      if (current && isBusy(current.sessionId)) {
        notify('请先停止当前 Wiki Agent 任务');
        return;
      }
      void (async () => {
        try {
          await switchEmbeddedConversation(root, req, historySessionId);
        } catch (err) {
          notify(`切换 Wiki 对话失败：${(err as Error).message}`);
        }
      })();
      return;
    }
    // 附件移除按钮自带监听并 stopPropagation（见 buildAttachmentChip），不经过本委托。
  });
  form?.addEventListener('submit', (event) => {
    event.preventDefault();
    const text = input?.value ?? '';
    if (input) {
      input.value = '';
      resetTextareaHeight(input);
      embeddedDrafts.set(req.kbId, '');
    }
    void sendEmbeddedPrompt(text, '', req.kbId);
  });
  if (input) {
    // Enter 发送与主对话同一套 IME 判定（中文输入选字 Enter 不误发）。
    const imeState = createComposerImeState();
    bindComposerIme(input, imeState);
    input.addEventListener('keydown', (event) => {
      if (shouldComposerSend(event, imeState)) {
        event.preventDefault();
        form?.requestSubmit();
      }
    });
    input.addEventListener('input', () => {
      embeddedDrafts.set(req.kbId, input.value);
      autoresizeTextarea(input, WIKI_INPUT_MAX_HEIGHT);
    });
    // 恢复草稿（renderShell 重建面板不丢输入）并保持焦点。
    input.value = embeddedDrafts.get(req.kbId) || '';
    requestAnimationFrame(() => autoresizeTextarea(input, WIKI_INPUT_MAX_HEIGHT));
    if (embeddedInputFocused) input.focus();
  }
  renderEmbeddedPanel();
  void embeddedState(req.kbId).then((panel) => {
    if (
      mountVersion !== embeddedMountVersion
      || activeEmbeddedRoot !== root
      || root.dataset.kbId !== req.kbId
    ) return;
    panel.kbName = req.kbName || req.kbId;
    scheduleEmbeddedRender();
    void loadEmbeddedModel(req.kbId, panel.sessionId);
  }).catch((err) => {
    if (
      mountVersion === embeddedMountVersion
      && activeEmbeddedRoot === root
      && root.dataset.kbId === req.kbId
    ) {
      root.querySelector<HTMLElement>('[data-wiki-agent-messages]')!.textContent =
        `连接 Wiki Agent 失败：${(err as Error).message}`;
    }
  });
}

/**
 * 打开 Wiki Agent：创建/复用独立会话并挂载到 Wiki 页右栏。
 * req.assist 存在时（上传失败「让 AI 处理」）随后自动发送一条携带失败上下文的 prompt；
 * req.prompt 存在时（Home.md 推荐问题点击）直接发送该问题。
 */
export async function openWikiAgent(req: WikiAgentEntryRequest): Promise<void> {
  if (!requireRendererLogin('请先登录后再使用 Wiki 问答')) return;
  const kbId = req.kbId.trim();
  if (!kbId) return;
  const embeddedRoot = document.querySelector<HTMLElement>('[data-wiki-agent-panel]');
  if (embeddedRoot && state.activeTab === 'wiki') {
    mountWikiAgentPanel(embeddedRoot, req);
    embeddedRoot.querySelector<HTMLTextAreaElement>('[data-wiki-agent-input]')?.focus();
    if (req.assist) await sendEmbeddedPrompt(buildWikiAssistPrompt(req.assist), '', kbId);
    else if (req.prompt?.trim()) await sendEmbeddedPrompt(req.prompt, '', kbId);
    if (req.openAttachment) embeddedRoot.querySelector<HTMLInputElement>('[data-wiki-agent-file]')?.click();
    return;
  }
  notify('请先打开 Wiki 页面，再使用 Wiki Agent');
}

export const enterWikiAgentMode = openWikiAgent;

/** 上传失败「让 AI 处理」的 prompt（对齐 web WikiHub 的 aiPrompt 文案）。 */
export function buildWikiAssistPrompt(assist: { fileName: string; error: string; sourceId?: string | null }): string {
  const base = `我上传「${assist.fileName}」到 Wiki 时处理失败，错误信息：${assist.error}。请帮我分析原因并重新处理这个文件`;
  return assist.sourceId ? `${base}（source_id: ${assist.sourceId}）。` : `${base}。`;
}

// ---------- 组合根接线（index.ts init 调用一次） ----------

let listenersBound = false;

/**
 * 初始化发送参数 resolver、登录态重置和 Wiki 卡片点击委托。
 * 监听器全局只绑一次（测试 reset 后重入不重复绑定）；resolver 允许重新注册。
 */
export function initWikiAgent(): void {
  // 专用 Wiki Agent 发送消息的附加 payload。
  setWikiSendExtrasResolver((sessionId) => {
    const session = wikiAgentSessions.get(sessionId);
    if (!session) return null;
    return { wikiKbId: session.kbId };
  });
  setWikiAgentPanelRenderer(mountWikiAgentPanel);

  if (listenersBound) return;
  listenersBound = true;

  sessionStore.subscribe((next, prev) => {
    const panel = embeddedByKb.get(activeEmbeddedKbId);
    if (
      panel
      && (
        next.busySessions[panel.sessionId] !== prev.busySessions[panel.sessionId]
        || next.books[panel.sessionId] !== prev.books[panel.sessionId]
      )
    ) {
      scheduleEmbeddedRender();
    }
  });
  messageStore.subscribe((next, prev) => {
    const panel = embeddedByKb.get(activeEmbeddedKbId);
    if (panel && next.messages[panel.sessionId] !== prev.messages[panel.sessionId]) {
      scheduleEmbeddedRender();
    }
  });

  // 面板输入框焦点追踪：renderShell 重建面板 DOM 后按此恢复焦点。
  // focusout 时用微任务判定——重建流程中新输入框会在同一同步流程内重新 focus，不误清标记。
  document.addEventListener('focusin', (event) => {
    if ((event.target as Element | null)?.matches?.('[data-wiki-agent-panel] [data-wiki-agent-input]')) {
      embeddedInputFocused = true;
    }
  });
  document.addEventListener('focusout', (event) => {
    if (!(event.target as Element | null)?.matches?.('[data-wiki-agent-panel] [data-wiki-agent-input]')) return;
    queueMicrotask(() => {
      const el = document.activeElement as Element | null;
      if (!el?.closest?.('[data-wiki-agent-panel]')) embeddedInputFocused = false;
    });
  });

  // 登录态变化：重置专用 Wiki Agent 会话状态。
  window.addEventListener('user:login-changed', () => {
    wikiAgentSessions.clear();
    clearEmbeddedPanelState();
  });

  window.addEventListener('messages:changed', (event) => {
    const sid = (event as CustomEvent<{ sessionId?: string }>).detail?.sessionId;
    const panel = embeddedByKb.get(activeEmbeddedKbId);
    if (panel && sid === panel.sessionId) scheduleEmbeddedRender();
  });

  // 专用 Wiki Agent 卡片交互：「查看」在 Wiki 页内打开详情，建议追问继续发给当前 Agent。
  document.addEventListener('click', (event) => {
    const target = event.target instanceof Element ? event.target : null;
    if (!target) return;
    const viewBtn = target.closest<HTMLElement>('[data-wiki-view-page]');
    if (viewBtn) {
      event.preventDefault();
      const pageId = viewBtn.getAttribute('data-wiki-view-page') ?? '';
      openWikiPageInHub(pageId);
      return;
    }
    const confirm = target.closest<HTMLElement>('[data-wiki-confirm]');
    if (confirm?.dataset.wikiConfirm) {
      event.preventDefault();
      void sendEmbeddedPrompt(`我确认执行 ${confirm.dataset.wikiAction || 'Wiki 操作'}。`, confirm.dataset.wikiConfirm);
      return;
    }
    const cancel = target.closest<HTMLElement>('[data-wiki-cancel]');
    if (cancel?.dataset.wikiCancel) {
      event.preventDefault();
      const panel = embeddedByKb.get(activeEmbeddedKbId);
      if (!panel) return;
      void backendApi.wikiCancelConfirmation(cancel.dataset.wikiCancel, panel.sessionId)
        .then(() => sendEmbeddedPrompt('已取消该 Wiki 操作。'))
        .catch((err) => notify(`取消失败：${(err as Error).message}`));
    }
  });
}

/** 测试钩子：重置模块级会话状态；全局监听器保留。 */
export function __resetWikiAgentForTest(): void {
  wikiAgentSessions.clear();
  wikiSessionRows.clear();
  clearEmbeddedPanelState();
  setWikiSendExtrasResolver(null);
  setWikiAgentPanelRenderer(null);
}
