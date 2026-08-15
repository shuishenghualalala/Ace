/**
 * Wiki Agent 对话：Wiki 页右栏内嵌的专用会话。
 *
 * 链路：Wiki 页「上传」（打开右栏附件选择）/ 失败任务「让 AI 处理」（wiki-page 经
 * setWikiAgentEntryHandler 回调注入，互不 import）→ POST /api/wiki/agent-session 拿独立会话
 * → 右栏面板直接收发 → 发送时 payload 带 wiki_kb_id
 * （chat-controller 经 setWikiSendExtrasResolver 注册口取参数，chat 侧不 import 本模块）
 * → wiki_cards 帧经 reducer 挂到消息渲染。
 *
 * 右栏面板（mountWikiAgentPanel）：面板头（标题 + 新建/历史/展开）是 wiki 自己的 DOM；
 * 对话区是**主对话面板本体**（conversation-panel.mountConversationPanel：同一套增量 diff
 * 渲染、scroll anchor 软钉底、完整 Composer）。Composer 工具栏与主对话同一套实例级实现：
 * 模型 chip（model-picker.createComposerModelControl）、上下文环（composer-context-ring）、
 * 附件「+」（composer-context-view 工厂内聚触发文件选择）；wiki 特有扩展经面板槽位注入：
 * 附件预览走 before-input、空态走 emptyState、followup 走 followupHandlers、todo 走 Composer todo 槽位。
 * wiki 页 renderShell 重建时，KB 未变则保留面板活节点（不重挂载）；真正重挂载时
 * （切 KB / 上传入口显式触发）草稿（embeddedDrafts）、宽度档位（embeddedExpanded）、
 * 焦点（embeddedInputFocused）由模块状态存活并恢复；会话模型绑定走共享的
 * session-model.bindingsBySession（按 sessionId 隔离，天然不怕会话切换竞争）。
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
import { createChatRenderCoalescer } from '../render-utils';
import { formatFollowupAnswerMessage } from '../followup';
import {
  escapeHtml,
  enqueuePending,
  newMessageId,
  notify,
  patchBook,
  setBookTodos,
  setQueueHint,
  setSessionStatus,
  state,
  type TodoItem,
  type SessionRow,
} from '../state';
import { sessionStore } from '../stores/stores';
import {
  bindFileDrop,
  bindFilePaste,
  type PanelAttachments,
} from './attachments';
import { requireRendererLogin } from './auth-gate';
import {
  mountConversationPanel,
  type ConversationPanel,
} from './conversation-panel';
import { createComposerModelControl, type ComposerModelControl } from './model-picker';
import { createComposerContextView } from './composer-context-view';
import {
  createContextRingController,
  type ContextRingController,
} from './composer-context-ring';
import { showConfirmDialog } from '../ui-feedback';
import {
  activeComposerModelId,
  loadSessionModel,
  modelContextWindow,
} from './session-model';
import {
  appendMessage,
  bookFor,
  dispatchWs,
  editQueueItem,
  isBusy,
  openSessionInChat,
  setWikiSendExtrasResolver,
  steerQueuedItem,
  subscribeSessions,
  stopGeneration,
} from './chat-controller';
import { ensureFileChangesDelegation } from './conversation-renderer';
import { openInspectorToTab } from './inspector';
import { resumeSessionGeneration } from './session-busy';
import { loadBackendHistory } from './session-controller';
import {
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
  if (activeEmbeddedKbId === normalized) {
    // 同名 KB 重建时不能让 wiki-page 复用旧面板 DOM，否则不会重新创建/加载会话。
    activePanel?.dispose();
    activePanel = null;
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
let activeEmbeddedRoot: HTMLElement | null = null;
let activeEmbeddedKbId = '';
/** 当前挂载的对话面板实例（mountConversationPanel）；重挂载 / 登录态变化时 dispose。 */
let activePanel: ConversationPanel | null = null;
/** 面板每次挂载递增；异步会话加载只允许更新发起它的那次挂载。 */
let embeddedMountVersion = 0;
/** 当前挂载实例的模型 chip / 上下文环控制器（面板 DOM 重建 / 登录态变化时收回）。 */
let activeModelControl: ComposerModelControl | null = null;
let activeRingControl: ContextRingController | null = null;
/** 面板输入框是否持有焦点（focusin/focusout 全局追踪，重挂载后恢复焦点用）。 */
let embeddedInputFocused = false;

/** Wiki 面板附件变更监听（PanelAttachments.subscribe 的回调集）。 */
const embeddedAttachmentListeners = new Set<() => void>();
function notifyEmbeddedAttachmentsChanged(): void {
  for (const cb of embeddedAttachmentListeners) cb();
}

/** Wiki 面板附件 adapter（重构计划步骤 4）：包 per-KB 的 embeddedAttachments Map。 */
function createEmbeddedPanelAttachments(kbId: string): PanelAttachments {
  return {
    list: () => [...(embeddedAttachments.get(kbId) || [])],
    add: (files) => addEmbeddedFiles(files, kbId),
    remove: (attId) => {
      const list = embeddedAttachments.get(kbId) || [];
      embeddedAttachments.set(kbId, list.filter((item) => item.id !== attId));
      notifyEmbeddedAttachmentsChanged();
    },
    takeForSend: () => {
      const list = [...(embeddedAttachments.get(kbId) || [])];
      embeddedAttachments.set(kbId, []);
      notifyEmbeddedAttachmentsChanged();
      return list;
    },
    subscribe: (cb) => {
      embeddedAttachmentListeners.add(cb);
      return () => {
        embeddedAttachmentListeners.delete(cb);
      };
    },
  };
}

/** 清空右栏面板全部 per-KB 状态（登录态变化 / 测试重置共用，防两处漂移漏清）。 */
function clearEmbeddedPanelState(): void {
  activePanel?.dispose();
  activePanel = null;
  embeddedByKb.clear();
  embeddedLoads.clear();
  embeddedAttachments.clear();
  embeddedDrafts.clear();
  embeddedExpanded.clear();
  embeddedAttachmentListeners.clear();
  activeModelControl?.dispose();
  activeModelControl = null;
  activeRingControl?.dispose();
  activeRingControl = null;
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
  // 会话级模型绑定写入共享缓存（按 sessionId 隔离），chip/上下文环经控制器自动刷新。
  void loadSessionModel(sessionId).then(() => {
    activeModelControl?.refresh();
    activeRingControl?.refresh();
  });
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

/** Wiki 空态：居中标语（图标 + 「基于知识库问答」+ 上传引导），经面板 emptyState 注入。 */
function buildWikiEmptyState(): HTMLElement {
  const el = document.createElement('div');
  el.className = 'wiki-agent-pane__void';
  // 内容全是本模块的静态 SVG/文案常量，无用户输入，innerHTML 安全。
  el.innerHTML = `
    <span class="wiki-agent-pane__void-icon" aria-hidden="true">${WIKI_VOID_ICON}</span>
    <p class="wiki-agent-pane__void-text">基于知识库问答</p>
    <p class="wiki-agent-pane__void-hint">可直接粘贴或拖拽文件到此处上传</p>`;
  return el;
}

/** rAF 合并渲染：同一帧内多次 schedule 只渲染一次（复用 render-utils 的通用合并器）。 */
const scheduleEmbeddedRender = createChatRenderCoalescer(() => activePanel?.render(), (cb) => {
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
      // 与主对话一致：busy 时进待发队列（Composer 队列槽位可见、可编辑/排序/删除），
      // 回合结算时由 chat-controller.consumePending 依次派出。
      // 注意：携带 wiki_confirmation_id 的确认消息不排队（确认有时效，过期无意义）。
      if (wikiConfirmationId) {
        notify('Wiki Agent 正在处理上一条消息');
        return;
      }
      enqueuePending(panel.sessionId, {
        id: newMessageId('q'),
        query: query || '(附件)',
        attachments: [...attachments],
        planActive: false,
      });
      embeddedAttachments.set(kbId, []);
      notifyEmbeddedAttachmentsChanged();
      setQueueHint(panel.sessionId, '正在排队…');
      setSessionStatus(panel.sessionId, 'queued');
      return;
    }
    embeddedAttachments.set(kbId, []);
    notifyEmbeddedAttachmentsChanged();
    await dispatchWs(panel.sessionId, query, attachments, {
      planActive: false,
      ...(wikiConfirmationId ? { wikiConfirmationId } : {}),
    });
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

async function addEmbeddedFiles(
  files: FileList | File[] | null,
  kbId = activeEmbeddedKbId,
): Promise<void> {
  if (!files?.length || !kbId) return;
  // 附件随 wiki 会话上传：后端据此把附件收入当前知识库（而非 default）。
  const sessionId = embeddedByKb.get(kbId)?.sessionId;
  // 多文件并行上传（单个失败只提示、不阻断其余），成功结果保持原顺序追加。
  const uploaded = await Promise.all(
    Array.from(files).map(async (file) => {
      try {
        return await backendApi.upload(file.name, await readFileAsBase64(file), { kbId, sessionId });
      } catch (err) {
        notify(`上传失败：${file.name} · ${(err as Error).message}`);
        return null;
      }
    }),
  );
  const next = [...(embeddedAttachments.get(kbId) || [])];
  for (const item of uploaded) {
    if (item) next.push(item);
  }
  embeddedAttachments.set(kbId, next);
  notifyEmbeddedAttachmentsChanged();
}

// ── 右栏 Composer 扩展（附件预览走面板 contextStaging 槽位） ──

const WIKI_VOID_ICON = `<svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/><circle cx="8.5" cy="11.5" r=".4" fill="currentColor"/><circle cx="12" cy="11.5" r=".4" fill="currentColor"/><circle cx="15.5" cy="11.5" r=".4" fill="currentColor"/></svg>`;
const WIKI_EXPAND_ICON = `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 3h6v6"/><path d="M9 21H3v-6"/><path d="M21 3l-7 7"/><path d="M3 21l7-7"/></svg>`;
const WIKI_NEW_CHAT_ICON = `<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h6"/><path d="M18 2v6"/><path d="M15 5h6"/></svg>`;
const WIKI_HISTORY_ICON = `<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>`;
const WIKI_HISTORY_DELETE_ICON = `<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>`;

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
  notifyEmbeddedAttachmentsChanged();
  embeddedDrafts.set(req.kbId, '');
  await activateEmbeddedSession(req.kbId, sessionId, req.kbName || req.kbId);
  const input = root.querySelector<HTMLTextAreaElement>('[data-composer-input]');
  if (input) {
    input.value = '';
    // 派发 input 让 Composer 复位高度与发送按钮态。
    input.dispatchEvent(new Event('input', { bubbles: true }));
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
    activePanel?.focus();
    notify('已新建 Wiki 对话');
  } catch (err) {
    notify(`新建 Wiki 对话失败：${(err as Error).message}`);
  } finally {
    if (button) button.disabled = false;
  }
}

/** 把指定知识库的持久化 Wiki Agent 会话挂到 Wiki 右栏。 */
export function mountWikiAgentPanel(root: HTMLElement, req: WikiAgentEntryRequest): void {
  // 面板 DOM 随 wiki 页 renderShell 整体重建：收回旧实例的控制器（模型 chip 浮层 / 上下文环）。
  activeModelControl?.dispose();
  activeModelControl = null;
  activeRingControl?.dispose();
  activeRingControl = null;
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
    <div class="wiki-agent-pane__conversation" data-wiki-agent-conversation></div>
    <p class="wiki-agent-pane__disclaimer">内容由 AI 生成，请仔细甄别</p>`;
  // 「已编辑文件」卡 / 链接 / 浏览器产物点击委托：消息区由增量 diff 复用/重建节点，
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
  // ── 对话面板本体：与主对话同一个 mountConversationPanel ──
  // wiki 扩展经槽位注入：文件选择 + 附件预览 → before-input；工具栏控件（附件「+」/
  // 模型 chip / 上下文环）由 composer-context-view 的 wiki 表面统一构建。
  const conversationHost = root.querySelector<HTMLElement>('[data-wiki-agent-conversation]')!;
  const staging = document.createElement('div');
  staging.innerHTML = `
    <div data-composer-context-source="before-input">
      <input type="file" data-wiki-agent-file multiple hidden />
      <div class="mw-attachment-list" data-attachment-preview hidden></div>
    </div>`;
  const getEmbeddedSessionId = (): string | null => embeddedByKb.get(req.kbId)?.sessionId ?? null;
  const panel = mountConversationPanel(conversationHost, {
    containerId: 'wiki-agent-messages',
    getSessionId: getEmbeddedSessionId,
    attachments: createEmbeddedPanelAttachments(req.kbId),
    actions: {
      submit: (text) => sendEmbeddedPrompt(text, '', req.kbId),
      stop: () => {
        const current = embeddedByKb.get(req.kbId);
        if (current) stopGeneration(current.sessionId);
      },
      // Wiki 会话没有「撤回编辑」入口（编辑横幅不会出现），取消编辑无需动作。
      cancelEdit: () => {},
      editQueueItem,
      steerQueueItem: steerQueuedItem,
    },
    contextStaging: staging,
    emptyState: buildWikiEmptyState,
    followupHandlers: {
      onSubmit: (questionId, answers) => {
        const sessionId = embeddedByKb.get(req.kbId)?.sessionId;
        if (sessionId) resolveEmbeddedFollowup(sessionId, questionId, answers);
      },
      onCancel: (questionId) => {
        const sessionId = embeddedByKb.get(req.kbId)?.sessionId;
        if (sessionId) cancelEmbeddedFollowup(sessionId, questionId);
      },
    },
    todoFoldKey: (sessionId) => `todo:wiki:${sessionId}`,
    composerPlaceholder: '基于知识库提问',
    autoRender: true,
  });
  activePanel = panel;
  // wiki-page 的 renderShell 滚动位置记忆按 [data-wiki-agent-messages] 找消息容器；
  // 布局沿用旧 .wiki-agent-pane__messages 的 flex/overflow 规则。
  panel.messagesEl?.classList.add('wiki-agent-pane__messages');
  panel.messagesEl?.setAttribute('data-wiki-agent-messages', '');
  // ── Composer 工具栏：与主对话同一套实例级实现（composer-context-view 构建控件，
  //    model-picker / composer-context-ring 控制器驱动行为，按嵌入会话 id 解析状态） ──
  const contextView = createComposerContextView(panel.composerRoot, { surface: 'wiki' });
  activeModelControl = createComposerModelControl(contextView.controls.modelChip, {
    getSessionId: getEmbeddedSessionId,
    workspaceId: WIKI_AGENT_WORKSPACE_ID,
  });
  activeRingControl = createContextRingController(contextView.controls.ring, {
    getSessionId: getEmbeddedSessionId,
    resolveWindow: () => modelContextWindow(activeComposerModelId(getEmbeddedSessionId())),
    // 切走 Wiki 页后不再为看不见的面板拉取；回到 Wiki 页时 renderShell 重挂载会重建并刷新。
    isActive: () => state.activeTab === 'wiki',
  });
  activeRingControl.refresh();
  const input = panel.composerRoot.querySelector<HTMLTextAreaElement>('[data-composer-input]');
  const fileInput = panel.composerRoot.querySelector<HTMLInputElement>('[data-wiki-agent-file]');
  root.querySelector('[data-wiki-agent-new]')?.addEventListener('click', () => {
    void createEmbeddedConversation(root, req);
  });
  root.querySelector('[data-wiki-agent-history]')?.addEventListener('click', () => {
    void openWikiHistory(root, req.kbId);
  });
  // 附件「+」→ file input 的触发由 composer-context-view 工厂内聚；这里只管选中后的上传。
  fileInput?.addEventListener('change', () => {
    void addEmbeddedFiles(fileInput.files);
    fileInput.value = '';
  });
  // 粘贴 / 拖拽上传：与主对话同一套绑定（attachments.ts），上传走 addEmbeddedFiles。
  // 面板随 renderShell 重建后是新 DOM，需每次挂载重新绑定。
  if (input) bindFilePaste(input, (files) => void addEmbeddedFiles(files));
  bindFileDrop(root, (files) => void addEmbeddedFiles(files));
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
    // 附件移除按钮自带监听并 stopPropagation（见 attachments.ts 的预览卡片），不经过本委托。
  });
  if (input) {
    // 草稿存活：renderShell 重建面板不丢输入。发送/IME/自动增高由 Composer 内部处理，
    // 这里只同步草稿；恢复草稿后派发 input 让 Composer 刷新高度与发送按钮态。
    input.addEventListener('input', () => {
      embeddedDrafts.set(req.kbId, input.value);
    });
    input.value = embeddedDrafts.get(req.kbId) || '';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    if (embeddedInputFocused) input.focus();
  }
  panel.render();
  void embeddedState(req.kbId).then((loaded) => {
    if (
      mountVersion !== embeddedMountVersion
      || activeEmbeddedRoot !== root
      || root.dataset.kbId !== req.kbId
    ) return;
    loaded.kbName = req.kbName || req.kbId;
    scheduleEmbeddedRender();
    // 缓存路径（会话已激活）不再过 activateEmbeddedSession，这里兜底确保模型绑定已加载。
    void loadSessionModel(loaded.sessionId).then(() => {
      activeModelControl?.refresh();
      activeRingControl?.refresh();
    });
  }).catch((err) => {
    if (
      mountVersion === embeddedMountVersion
      && activeEmbeddedRoot === root
      && root.dataset.kbId === req.kbId
    ) {
      notify(`连接 Wiki Agent 失败：${(err as Error).message}`);
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
    activePanel?.focus();
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

  // 面板消息渲染的 store 订阅由 conversation-panel（autoRender）按实例持有，
  // 随挂载/卸载注册与释放，这里不再挂模块级订阅。

  // 面板输入框焦点追踪：renderShell 重建面板 DOM 后按此恢复焦点。
  // focusout 时用微任务判定——重建流程中新输入框会在同一同步流程内重新 focus，不误清标记。
  document.addEventListener('focusin', (event) => {
    if ((event.target as Element | null)?.matches?.('[data-wiki-agent-panel] [data-composer-input]')) {
      embeddedInputFocused = true;
    }
  });
  document.addEventListener('focusout', (event) => {
    if (!(event.target as Element | null)?.matches?.('[data-wiki-agent-panel] [data-composer-input]')) return;
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
