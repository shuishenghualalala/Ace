/**
 * 桌面端渲染层共享状态与 DOM 工具。
 *
 * 状态架构过渡说明：
 * - 历史版本：38 字段单例 `state` 对象，10+ 模块直接读写
 * - 现版本（Stage 2 commit 5）：38 字段已拆分到 `src/ui/stores/stores.ts` 的 7 个 store
 *   中。本文件保留 `state` 对象作为"代理 shim"——所有读操作转发到对应 store 的 `.get()`，
 *   所有写操作转发到对应 store 的 `.set()`。
 * - 后续 commit 7：删 shim，全部代码改用 `xxxStore.get() / .set()`。
 *
 * 这样既不阻塞 Stage 2/3 的安全修复（C-4 删 restoreSession 等），也避免一次大爆炸。
 */

import type {
  Attachment,
  BackendConfig,
  BackendSession,
  CronJob,
  Mode,
  Task,
  Workspace,
} from './backend-client';
import type { ChatMessage, PendingMessage, PlanReviewStatus, SessionStatus, ToolCallInfo } from './chat-render';
import type { BackendChatSocket } from './backend-client';
import {
  sessionStore,
  messageStore,
  taskStore,
  configStore,
  workspaceStore,
  authStore,
  uiStore,
  cronStore,
  externalStore,
  type SessionStoreState,
  type MessageStoreState,
  type TaskStoreState,
  type ConfigStoreState,
  type WorkspaceStoreState,
  type AuthStoreState,
  type UiStoreState,
  type CronJobStoreState,
  type ExternalStoreState,
} from './stores/stores';
// stream-reassembly 零依赖（不 import state/features），故 state.ts 可安全 import 无循环。
import { resetSession as resetReassembly } from './stream-reassembly';

export type TabKey =
  | 'chat'
  | 'agents'
  | 'skills'
  | 'wiki'
  | 'sites'
  | 'cron'
  | 'security'
  | 'audit'
  | 'system';
export type SystemPanelKey = 'overview' | 'logs' | 'usage';
export type ComposerMode = 'craft' | 'plan' | 'ask';

export interface SessionRow {
  id: string;
  title: string;
  updatedAt: number;
  preview: string;
  badge: string;
  workspaceId: string;
  /** 标题是否已由后端摘要（session_title chunk 或 set_title）确定。
   *  syncSessionsFromBackend 据此保留前端标题，避免被后端列表里的占位/截断标题覆盖。 */
  titleFromSummary?: boolean;
  /** 是否归档（后端 archived=1）。归档会话默认不进主列表，仅 archive 视图展示。
   *  后端列表默认排除归档，前端此字段主要用于本地一致性。 */
  archived?: boolean;
  /** 是否置顶（后端 pinned=1）。主列表排序优先。 */
  pinned?: boolean;
  /** 渠道会话所属平台（侧栏渠道文件夹用）。 */
  channelPlatform?: string;
  /** 后端会话绑定的智能体/团队展示标签。 */
  agentLabel?: { name?: string; provider?: string; display_badge?: string; model?: string };
  /** 精确的会话执行身份；展示标签不承担类型判断。 */
  agentBinding?: import('./backend-client').SessionAgentBinding;
  /** 当前会话底部展示用模型名；ACP 会话使用外部智能体自己的模型名。 */
  modelLabel?: string;
}

/** 追问选择框待答内容（ask_followup_question 推送，存于 book.pendingFollowup）。 */
export interface PendingFollowup {
  questionId: string;
  title: string;
  /** false for permission side-channel prompts: keep the current assistant turn intact. */
  recordHistory: boolean;
  status?: string;
  note?: string;
  origin?: {
    type?: string;
    agentName?: string;
    originSessionId?: string;
    mentionIntent?: string;
  };
  questions: Array<{
    id: string;
    question: string;
    options: Array<{ label: string; value: string; description?: string }>;
    allowFreeText: boolean;
    multiSelect: boolean;
  }>;
}

export interface TodoItem {
  id: string;
  content: string;
  status: 'pending' | 'in_progress' | 'completed' | 'cancelled';
}

export interface DiffRow {
  line: number;
  kind: 'meta' | 'ctx' | 'add' | 'del';
  text: string;
}

export interface FileChange {
  path: string;
  name: string;
  added: number;
  removed: number;
  status: 'modified' | 'added' | 'deleted';
  diff: DiffRow[];
  /** 二进制结果文件没有可统计的文本行数，但仍应保留在文件改动卡中。 */
  binary?: boolean;
}

export interface DeltaSpan {
  start: number;
  end: number;
  text: string;
}

export interface Bookkeeping {
  toolMap: Map<string, import('./chat-render').ToolCallInfo>;
  assistantId: string | null;
  firstChunkAt: number | null;
  /** 当前回合是否已出现团队成员消息；final 仅作封口，不能再生成团队自身气泡。 */
  hadTeamInternal: boolean;
  planActive: boolean;
  pendingPlan: { plan: string; planFile: string; status: PlanReviewStatus; options?: { label: string; description: string }[] } | null;
  /** ask_followup_question 待答追问（followup_question 帧 → followupQuestionReducer 写入）。 */
  pendingFollowup: PendingFollowup | null;
  /** 后端 todo 工具最新快照（todo_updated 帧 → todoUpdatedReducer 写入）。Inspector Plan tab 读它。 */
  todos: TodoItem[];
  /** 后端 file_write diff 累积（file_changes 帧 → fileChangesReducer 写入）。Inspector Files tab 读它。 */
  fileChanges: FileChange[];
  /** 上一轮 final 时 fileChanges 的签名快照（path → 签名）；用于推算「仅本轮」文件改动差集。
   *  注：fileChanges 不随历史 hydrate 回填，重开会话后为空，故首轮差集基准即空，无需额外 hydrate 初始化。 */
  prevTurnFileSignature: Record<string, string> | null;
  /** 本轮 assistant 正文的有序 delta 分片；用于修复 WS 回放/实时交错导致的流式文字乱序。 */
  deltaSpans: DeltaSpan[];
  /** 兼容旧后端：没有 delta_start/delta_end 的分片仍按到达顺序追加到此前缀。 */
  legacyDeltaText: string;
  /**
   * 本轮是否已 final/error 封口。true 时忽略迟到的 delta/tool 上的 running hint，
   * 防止 UI 在「已结束」后又被打回 busy。
   */
  turnSealed: boolean;
  /** 当前被桌面端接收的后端 request_id；用于拒收已封口回合的迟到生成帧。 */
  activeRequestId: string | null;
  /** 新一轮刚打开但首帧尚未到达时为 true，首个生成帧会绑定 activeRequestId。 */
  acceptingNewRequest: boolean;
}

export interface FeedbackImage {
  name: string;
  dataUrl: string;
}

export interface FeedbackDraft {
  title: string;
  description: string;
  images: FeedbackImage[];
}

/** 服务端反馈记录（管理端 /admin/feedbacks 返回项的 UI 子集）。 */
export type FeedbackStatus = 'PENDING' | 'PROCESSING' | 'RESOLVED' | 'CLOSED';

export interface FeedbackListItem {
  id: number;
  staffCode?: string | undefined;
  title: string;
  description?: string | undefined;
  images?: string | undefined;
  status: FeedbackStatus;
  adminReply?: string | null | undefined;
  createdAt: string;
  updatedAt?: string | undefined;
}

export interface UserInfo {
  staffCode?: string;
  staffName?: string;
  staffUid?: string;
  pid?: string;
  uid?: string;
}

/** 旧 AppState 接口（保留以兼容外部 import）。 */
export interface AppState extends
  SessionStoreState,
  MessageStoreState,
  TaskStoreState,
  ConfigStoreState,
  WorkspaceStoreState,
  AuthStoreState,
  Omit<UiStoreState, 'socket'>,
  CronJobStoreState,
  ExternalStoreState {
  // socket 在新架构里属于 uiStore；旧字段名 `socket` 转发到 uiStore.socket
  socket: import('./backend-client').BackendChatSocket | null;
}

/**
 * shim: 旧 `state` 对象代理到 7 个 store。
 *
 * 关键约束：
 * - 读 `state.xxx` → 从对应 store.get() 取当前快照字段（每次实时读，不缓存）
 * - 写 `state.xxx = v` → 调用对应 store.set({ xxx: v })
 * - 这样旧的 `state.sessions.push(...)` 这类**就地变更**失效，必须改为 `sessionStore.set({...})` 或
 *   `sessionStore.get().sessions.push(...)`（后者绕过订阅，**不推荐**）
 *
 * 实现：使用 Proxy（target = 各 store 当前快照的浅合并）。
 */
function buildSnapshot(): AppState {
  const s = sessionStore.get();
  const m = messageStore.get();
  const t = taskStore.get();
  const c = configStore.get();
  const w = workspaceStore.get();
  const a = authStore.get();
  const u = uiStore.get();
  const cr = cronStore.get();
  const ex = externalStore.get();
  return {
    sessions: s.sessions,
    backendSessions: s.backendSessions,
    activeSessionId: s.activeSessionId,
    sessionStatuses: s.sessionStatuses,
    busySessions: s.busySessions,
    subscribedSessions: s.subscribedSessions,
    books: s.books,
    suppressChunks: s.suppressChunks,
    editFromIdx: s.editFromIdx,
    userFoldedTurns: s.userFoldedTurns,
    userUnfoldedTurns: s.userUnfoldedTurns,
    messages: m.messages,
    queueHints: m.queueHints,
    pendingQueues: m.pendingQueues,
    attachments: m.attachments,
    tasks: t.tasks,
    taskBoardOpen: t.taskBoardOpen,
    taskBoardWidth: t.taskBoardWidth,
    kanbanBoard: t.kanbanBoard,
    config: c.config,
    configModel: c.configModel,
    mode: c.mode,
    composerMode: c.composerMode,
    workspaces: w.workspaces,
    expandedWorkspaces: w.expandedWorkspaces,
    channelExpanded: w.channelExpanded,
    channelSessionGroups: w.channelSessionGroups,
    wsShowAll: w.wsShowAll,
    currentWorkspaceId: w.currentWorkspaceId,
    historyCollapsed: w.historyCollapsed,
    historyFilter: w.historyFilter,
    selectedSessions: w.selectedSessions,
    manageMode: w.manageMode,
    userInfo: a.userInfo,
    isLoggedIn: a.isLoggedIn,
    activeTab: u.activeTab,
    activeSystemPanel: u.activeSystemPanel,
    backendConnected: u.backendConnected,
    socket: u.socket as AppState['socket'],
    feedbackDraft: u.feedbackDraft,
    feedbackList: u.feedbackList,
    editingResend: u.editingResend,
    cronJobs: cr.cronJobs,
    cronJobScope: cr.cronJobScope,
    cronJobDetailId: cr.cronJobDetailId,
    cronDeleteConfirmId: cr.cronDeleteConfirmId,
    activeExternalTeamIdBySession: ex.activeExternalTeamIdBySession,
    unreadCompletedSessions: s.unreadCompletedSessions,
  };
}

// store 名 → 写 set 的目标 store
const FIELD_TO_STORE: Record<string, () => unknown> = {
  sessions: () => sessionStore,
  backendSessions: () => sessionStore,
  activeSessionId: () => sessionStore,
  sessionStatuses: () => sessionStore,
  busySessions: () => sessionStore,
  subscribedSessions: () => sessionStore,
  books: () => sessionStore,
  suppressChunks: () => sessionStore,
  editFromIdx: () => sessionStore,
  userFoldedTurns: () => sessionStore,
  userUnfoldedTurns: () => sessionStore,
  messages: () => messageStore,
  queueHints: () => messageStore,
  pendingQueues: () => messageStore,
  attachments: () => messageStore,
  tasks: () => taskStore,
  taskBoardOpen: () => taskStore,
  taskBoardWidth: () => taskStore,
  kanbanBoard: () => taskStore,
  config: () => configStore,
  configModel: () => configStore,
  mode: () => configStore,
  composerMode: () => configStore,
  workspaces: () => workspaceStore,
  expandedWorkspaces: () => workspaceStore,
  channelExpanded: () => workspaceStore,
  channelSessionGroups: () => workspaceStore,
  wsShowAll: () => workspaceStore,
  currentWorkspaceId: () => workspaceStore,
  historyCollapsed: () => workspaceStore,
  historyFilter: () => workspaceStore,
  selectedSessions: () => workspaceStore,
  manageMode: () => workspaceStore,
  userInfo: () => authStore,
  isLoggedIn: () => authStore,
  activeTab: () => uiStore,
  activeSystemPanel: () => uiStore,
  backendConnected: () => uiStore,
  socket: () => uiStore,
  feedbackDraft: () => uiStore,
  feedbackList: () => uiStore,
  editingResend: () => uiStore,
  cronJobs: () => cronStore,
  cronJobScope: () => cronStore,
  cronJobDetailId: () => cronStore,
  cronDeleteConfirmId: () => cronStore,
  activeExternalTeamIdBySession: () => externalStore,
};

/** 旧 state 对象（Proxy：读实时合并快照，写转发到对应 store）。 */
export const state: AppState = new Proxy({} as AppState, {
  get(_t, prop: string | symbol) {
    if (typeof prop === 'symbol') return undefined;
    const snap = buildSnapshot();
    return (snap as unknown as Record<string, unknown>)[prop];
  },
  set(_t, prop: string | symbol, value: unknown): boolean {
    if (typeof prop === 'symbol') return true;
    const storeGetter = FIELD_TO_STORE[prop];
    if (!storeGetter) {
      console.warn(`[state.shim] unknown field "${String(prop)}" assigned`);
      return true;
    }
    const store = storeGetter() as { set: (patch: Record<string, unknown>) => void };
    store.set({ [prop]: value });
    return true;
  },
  has(_t, prop: string | symbol) {
    if (typeof prop === 'symbol') return false;
    return prop in FIELD_TO_STORE;
  },
  ownKeys() {
    return Object.keys(FIELD_TO_STORE);
  },
  getOwnPropertyDescriptor(_t, prop: string | symbol) {
    if (typeof prop === 'symbol') return undefined;
    if (!(prop in FIELD_TO_STORE)) return undefined;
    return {
      configurable: true,
      enumerable: true,
      writable: true,
      value: (state as unknown as Record<string, unknown>)[prop],
    };
  },
}) as AppState;

export const $ = <T extends Element = HTMLElement>(selector: string): T | null =>
  document.querySelector(selector) as T | null;
export const $$ = <T extends Element = HTMLElement>(selector: string): T[] =>
  Array.from(document.querySelectorAll(selector)) as T[];

// escapeHtml 已抽到 shared/html.ts（避免 3 处重复实现），这里 re-export
// 以保持所有 `import { escapeHtml } from './state'` 调用点不破。
export { escapeHtml } from '../shared/html';

/** notify 节流：在 16ms 窗口内的多次 notify 合并为 1 次 UI 重绘。 */
let pendingToasts: string[] = [];
let notifyScheduled = false;
export function notify(message: string): void {
  pendingToasts.push(message);
  if (notifyScheduled) return;
  notifyScheduled = true;
  // queueMicrotask + requestAnimationFrame 不可靠（Node tests 环境无 RAF），
  // 这里用 setTimeout 16ms 作为统一节流窗口
  setTimeout(() => {
    const toasts = pendingToasts;
    pendingToasts = [];
    notifyScheduled = false;
    if (typeof document === 'undefined') return;
    for (const msg of toasts) {
      const toast = document.createElement('div');
      toast.className = 'ui-toast';
      toast.textContent = msg;
      document.body.appendChild(toast);
      window.setTimeout(() => toast.remove(), 1800);
    }
  }, 16);
}

/** 生成消息 ID（用 crypto.randomUUID 替换旧的 Math.random 方案，S-1）。 */
export function newMessageId(prefix = 'm'): string {
  const uuid = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID().slice(0, 8)
    : `${Date.now().toString(36)}`;
  return `${prefix}-${Date.now()}-${uuid}`;
}

/** 生成 session ID（同样改用 crypto.randomUUID）。 */
export function newSessionId(): string {
  const uuid = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID().slice(0, 6)
    : `${Date.now().toString(36)}`;
  return `web_${Date.now().toString(36)}_${uuid}`;
}

/**
 * 判断某会话是否处于 Dynamic Kanban 模式：当前会话 state.mode === 'dynamic_kanban'。
 */
export function isDynamicKanbanSession(sessionId: string | null | undefined): boolean {
  if (!sessionId) return false;
  const snap = buildSnapshot();
  return snap.activeSessionId === sessionId && snap.mode === 'dynamic_kanban';
}

const LAST_ACTIVE_SESSION_KEY = 'crew.lastActiveSession';

/**
 * 设置当前活跃 session。
 * 走 sessionStore，避免直接修改 state.activeSessionId 绕过订阅（S-5）。
 * 并把 last active session id 持久化到 localStorage，用于客户端重启后恢复。
 */
export function setActiveSessionId(id: string | null): void {
  const previousId = sessionStore.get().activeSessionId;
  if (previousId !== id && typeof window !== 'undefined') {
    // Give session-scoped native surfaces a synchronous chance to detach while
    // the old session is still current. `session:changed` is intentionally a
    // later, fully-hydrated event and is too late for Browser WebContentsView.
    window.dispatchEvent(new CustomEvent('session:changing', {
      detail: { fromSessionId: previousId, toSessionId: id },
    }));
  }
  const unread = sessionStore.get().unreadCompletedSessions;
  if (id && unread.has(id)) {
    const nextUnread = new Set(unread);
    nextUnread.delete(id);
    sessionStore.set({ activeSessionId: id, unreadCompletedSessions: nextUnread });
  } else {
    sessionStore.set({ activeSessionId: id });
  }
  if (typeof window !== 'undefined') {
    if (id) {
      saveToStorage(LAST_ACTIVE_SESSION_KEY, id);
    } else {
      localStorage.removeItem(LAST_ACTIVE_SESSION_KEY);
    }
  }
}

/** 某会话是否在后台完成且用户尚未打开查看。 */
export function isSessionUnreadComplete(sessionId: string): boolean {
  return sessionStore.get().unreadCompletedSessions.has(sessionId);
}

/** 确保某 session 的消息数组存在并返回它（Phase 2 T1 关键写入口）。 */
export function ensureSessionMessages(sessionId: string): ChatMessage[] {
  const cur = messageStore.get().messages[sessionId];
  if (cur) return cur;
  const next = [] as ChatMessage[];
  messageStore.set({ messages: { ...messageStore.get().messages, [sessionId]: next } });
  return messageStore.get().messages[sessionId] ?? next;
}

/** 用整批替换某 session 的消息（Phase 2 T2：历史回填）。 */
export function replaceSessionMessages(sessionId: string, list: ChatMessage[]): ChatMessage[] {
  messageStore.set({ messages: { ...messageStore.get().messages, [sessionId]: list } });
  return messageStore.get().messages[sessionId] ?? list;
}

/** 向某 session 的消息数组追加一条（Phase 2 T2：历史失败回填 error 消息）。 */
export function appendSessionMessage(sessionId: string, msg: ChatMessage): void {
  const cur = messageStore.get().messages[sessionId] ?? [];
  messageStore.set({ messages: { ...messageStore.get().messages, [sessionId]: [...cur, msg] } });
}

/** 从下标 idx 起截断某 session 的消息数组（替代 `state.messages[sid].splice(idx)`）。
 *  返回被移除的消息列表，便于调用方同步清理折叠态等派生集合（行为等价于 Array.splice 的
 *  「删除 idx 及之后全部」用法）。 */
export function truncateMessagesFrom(sessionId: string, idx: number): ChatMessage[] {
  const cur = messageStore.get().messages[sessionId] ?? [];
  if (idx >= cur.length) return [];
  const removed = cur.slice(idx);
  messageStore.set({ messages: { ...messageStore.get().messages, [sessionId]: cur.slice(0, idx) } });
  return removed;
}

/** 重置某 session 的整本 bookkeeping（Phase 2 T2：history 写回后清空残余流式状态）。 */
export function resetBook(sessionId: string): void {
  sessionStore.set({
    books: {
      ...sessionStore.get().books,
      [sessionId]: {
        toolMap: new Map(),
        assistantId: null,
        firstChunkAt: null,
        hadTeamInternal: false,
        planActive: false,
        pendingPlan: null,
        pendingFollowup: null,
        todos: [],
        fileChanges: [],
        prevTurnFileSignature: null,
        deltaSpans: [],
        legacyDeltaText: '',
        turnSealed: true,
        activeRequestId: null,
        acceptingNewRequest: false,
      },
    },

  });
}

/** 不可变更新某 session 的 bookkeeping 局部字段，保证 sessionStore 订阅者收到通知。
 *  直接突变 bookFor(sid) 的字段不会触发 store 订阅，Inspector/侧栏等会读到陈旧值。
 *  patch 中的字段覆盖原值；toolMap 按引用替换（调用方负责 new Map 或 clear 后传新引用）。 */
export function patchBook(sessionId: string, patch: Partial<Bookkeeping>): void {
  const cur = sessionStore.get().books[sessionId] ?? ensureSessionBook(sessionId);
  const next: Bookkeeping = { ...cur, ...patch };
  sessionStore.set({ books: { ...sessionStore.get().books, [sessionId]: next } });
}

// ---------- T4：queue / edit / interrupt / withdraw 显式 mutator ----------

/** 取出某 session 的 pending queue（引用，便于直接 update）。 */
export function getPendingQueue(sessionId: string): PendingMessage[] {
  return messageStore.get().pendingQueues[sessionId] ?? [];
}

/** 用整批替换某 session 的 pending queue。 */
export function replacePendingQueue(sessionId: string, queue: PendingMessage[]): void {
  messageStore.set({ pendingQueues: { ...messageStore.get().pendingQueues, [sessionId]: queue } });
}

/** 头部出队一条，返回 [dequeued, remaining]；空时返回 [null, 现有]。 */
export function shiftPendingQueue(sessionId: string): [PendingMessage | null, PendingMessage[]] {
  const cur = messageStore.get().pendingQueues[sessionId] ?? [];
  if (cur.length === 0) return [null, cur];
  const [head, ...rest] = cur;
  replacePendingQueue(sessionId, rest);
  return [head ?? null, rest];
}

/** 按下标替换某条 queue 项目的 query（用于 editQueueItem）。 */
export function patchPendingQueueItem(sessionId: string, index: number, patch: Partial<PendingMessage>): void {
  const cur = messageStore.get().pendingQueues[sessionId] ?? [];
  if (index < 0 || index >= cur.length) return;
  const next = cur.map((item, i) => (i === index ? { ...item, ...patch } : item));
  replacePendingQueue(sessionId, next);
}

/** 按下标删除一条 queue 项目。 */
export function removePendingQueueItem(sessionId: string, index: number): PendingMessage[] {
  const cur = messageStore.get().pendingQueues[sessionId] ?? [];
  if (index < 0 || index >= cur.length) return cur;
  const next = [...cur.slice(0, index), ...cur.slice(index + 1)];
  replacePendingQueue(sessionId, next);
  return next;
}

/** 将某条队列项提升为“修订式中断”的队首，其他项保持相对顺序。 */
export function promotePendingQueueItemAsRevision(
  sessionId: string,
  index: number,
  patch: Partial<Pick<PendingMessage, 'optimisticUserMessageId'>> = {},
): PendingMessage | null {
  const cur = messageStore.get().pendingQueues[sessionId] ?? [];
  if (index < 0 || index >= cur.length) return null;
  const item = cur[index];
  if (!item) return null;
  const promoted: PendingMessage = { ...item, ...patch, clientIntent: 'revision' };
  const rest = [...cur.slice(0, index), ...cur.slice(index + 1)];
  replacePendingQueue(sessionId, [promoted, ...rest]);
  return promoted;
}

/** 在尾部追加一条 queue 项目（用于 sendMessage enqueue）。 */
export function enqueuePending(sessionId: string, item: PendingMessage): void {
  const cur = messageStore.get().pendingQueues[sessionId] ?? [];
  replacePendingQueue(sessionId, [...cur, item]);
}

/** 移动某条 pending queue 项目；越界时保持原队列不变。 */
export function movePendingQueueItem(sessionId: string, fromIndex: number, toIndex: number): PendingMessage[] {
  const cur = messageStore.get().pendingQueues[sessionId] ?? [];
  if (
    fromIndex < 0
    || fromIndex >= cur.length
    || toIndex < 0
    || toIndex >= cur.length
    || fromIndex === toIndex
  ) {
    return cur;
  }
  const next = [...cur];
  const [item] = next.splice(fromIndex, 1);
  if (!item) return cur;
  next.splice(toIndex, 0, item);
  replacePendingQueue(sessionId, next);
  return next;
}

/** 设置某 session 的 editFromIdx（撤回到 message 下标）。 */
export function setEditFrom(sessionId: string, idx: number | null): void {
  const cur = sessionStore.get().editFromIdx;
  if (idx == null) {
    if (!(sessionId in cur)) return;
    const next = { ...cur };
    delete next[sessionId];
    sessionStore.set({ editFromIdx: next });
  } else {
    sessionStore.set({ editFromIdx: { ...cur, [sessionId]: idx } });
  }
}

/** 把某 session 加入 suppressChunks（迟到的流式分片会被忽略直到清空）。 */
export function addSuppressedSession(sessionId: string): void {
  if (sessionStore.get().suppressChunks.has(sessionId)) return;
  const next = new Set(sessionStore.get().suppressChunks);
  next.add(sessionId);
  sessionStore.set({ suppressChunks: next });
}

/** 取消某 session 的 suppressChunks。 */
export function removeSuppressedSession(sessionId: string): void {
  if (!sessionStore.get().suppressChunks.has(sessionId)) return;
  const next = new Set(sessionStore.get().suppressChunks);
  next.delete(sessionId);
  sessionStore.set({ suppressChunks: next });
}

/** 替换某 session 的 attachments 数组（用于 composer 附件 / 清空）。 */
export function replaceAttachments(list: import('./backend-client').Attachment[]): void {
  messageStore.set({ attachments: list });
}

/** 追加一条 attachment。 */
export function appendAttachment(att: import('./backend-client').Attachment): void {
  messageStore.set({ attachments: [...messageStore.get().attachments, att] });
}

/** 移除指定下标的 attachment。 */
export function removeAttachmentAt(index: number): void {
  const cur = messageStore.get().attachments;
  if (index < 0 || index >= cur.length) return;
  const next = [...cur.slice(0, index), ...cur.slice(index + 1)];
  messageStore.set({ attachments: next });
}

/** 清空 attachments。 */
export function clearAttachments(): void {
  messageStore.set({ attachments: [] });
}

// ---------- T5：workspace / draft / 删除会话 / 订阅清理 ----------

/** 清理某 session 的完整运行时痕迹：messages / books / sessionStatuses /
 *  busySessions / queueHints / editFromIdx / pendingQueues。订阅交给调用方通过
 *  addSubscribedSessions / BackendChatSocket.unsubscribe 处理。 */
export function removeSessionState(sessionId: string): void {
  // 修法3：会话删除 → 清掉它的 delta 重组缓冲，防泄漏。
  resetReassembly(sessionId);
  const curMessages = messageStore.get().messages;
  if (sessionId in curMessages) {
    const next = { ...curMessages };
    delete next[sessionId];
    messageStore.set({ messages: next });
  }
  const curQueueHints = messageStore.get().queueHints;
  if (sessionId in curQueueHints) {
    const next = { ...curQueueHints };
    delete next[sessionId];
    messageStore.set({ queueHints: next });
  }
  const curPending = messageStore.get().pendingQueues;
  if (sessionId in curPending) {
    const next = { ...curPending };
    delete next[sessionId];
    messageStore.set({ pendingQueues: next });
  }
  const curBooks = sessionStore.get().books;
  if (sessionId in curBooks) {
    const next = { ...curBooks };
    delete next[sessionId];
    sessionStore.set({ books: next });
  }
  const curStatuses = sessionStore.get().sessionStatuses;
  if (sessionId in curStatuses) {
    const next = { ...curStatuses };
    delete next[sessionId];
    sessionStore.set({ sessionStatuses: next });
  }
  const curBusy = sessionStore.get().busySessions;
  if (sessionId in curBusy) {
    const next = { ...curBusy };
    delete next[sessionId];
    sessionStore.set({ busySessions: next });
  }
  if (sessionStore.get().unreadCompletedSessions.has(sessionId)) {
    const nextUnread = new Set(sessionStore.get().unreadCompletedSessions);
    nextUnread.delete(sessionId);
    sessionStore.set({ unreadCompletedSessions: nextUnread });
  }
  setEditFrom(sessionId, null);
  removeSuppressedSession(sessionId);
}

/** 移除某 session 的订阅（local 裁剪：gateway 暂不支持 unsubscribe，因此只动本地 Set）。 */
export function removeSubscribedSession(sessionId: string): void {
  if (!sessionStore.get().subscribedSessions.has(sessionId)) return;
  const next = new Set(sessionStore.get().subscribedSessions);
  next.delete(sessionId);
  sessionStore.set({ subscribedSessions: next });
}

/** 确保某 session 的 bookkeeping 存在并返回它。 */
export function ensureSessionBook(sessionId: string): Bookkeeping {
  const cur = sessionStore.get().books[sessionId];
  if (cur) return cur;
  const next: Bookkeeping = {
    toolMap: new Map(),
    assistantId: null,
    firstChunkAt: null,
    hadTeamInternal: false,
    planActive: false,
    pendingPlan: null,
    pendingFollowup: null,
    todos: [],
    fileChanges: [],
    prevTurnFileSignature: null,
    deltaSpans: [],
    legacyDeltaText: '',
    turnSealed: true,
    activeRequestId: null,
    acceptingNewRequest: false,
  };
  sessionStore.set({ books: { ...sessionStore.get().books, [sessionId]: next } });
  return sessionStore.get().books[sessionId] ?? next;
}

/** 读某 session 的最新 todo 快照（todo_updated 帧 → todoUpdatedReducer 写入 book.todos）。 */
export function getBookTodos(sessionId: string): TodoItem[] {
  return sessionStore.get().books[sessionId]?.todos ?? [];
}

/** 写回后端拉取的 todo 快照（openSession / hydrate 用）。 */
export function setBookTodos(sessionId: string, todos: TodoItem[]): void {
  const books = sessionStore.get().books;
  const cur = books[sessionId] ?? ensureSessionBook(sessionId);
  sessionStore.set({ books: { ...books, [sessionId]: { ...cur, todos } } });
}

/** 读某 session 的文件改动清单（file_changes 帧 → fileChangesReducer 写入）。 */
export function getBookFileChanges(sessionId: string): FileChange[] {
  return sessionStore.get().books[sessionId]?.fileChanges ?? [];
}


/** 读取某 session 是否 busy（走 sessionStore，不依赖 state shim）。 */
export function isBusySession(sessionId: string): boolean {
  return !!sessionStore.get().busySessions[sessionId];
}

/** 设置某 session 的 busy 标记。
 *  状态隔离：仅当值真正变化时才写 store 并通知订阅者，返回是否发生变化。
 *  避免流式 chunk 高频重复写入相同 busy 值，触发侧栏无谓重渲 / spinner 动画重启。 */
export function setBusy(sessionId: string, val: boolean): boolean {
  const cur = sessionStore.get().busySessions[sessionId];
  const nextVal = val;
  if (cur === nextVal || (!cur && !nextVal)) return false;
  sessionStore.set({ busySessions: { ...sessionStore.get().busySessions, [sessionId]: nextVal } });
  return true;
}

/** 设置某 session 的状态标签。
 *  状态隔离：仅当值真正变化时才写 store 并通知订阅者，返回是否发生变化。
 *  这是治本 spinner 抽搐的关键——同一 running 状态在流式期间被反复写入时短路，
 *  行 DOM 不被重建，CSS 动画连续。 */
export function setSessionStatus(sessionId: string, status: SessionStatus): boolean {
  const cur = sessionStore.get().sessionStatuses[sessionId];
  if (cur === status) return false;
  const nextStatuses = { ...sessionStore.get().sessionStatuses, [sessionId]: status };
  if (
    status === 'idle'
    && (cur === 'running' || cur === 'queued')
    && sessionId !== sessionStore.get().activeSessionId
  ) {
    const nextUnread = new Set(sessionStore.get().unreadCompletedSessions);
    nextUnread.add(sessionId);
    sessionStore.set({ sessionStatuses: nextStatuses, unreadCompletedSessions: nextUnread });
  } else {
    sessionStore.set({ sessionStatuses: nextStatuses });
  }
  return true;
}

/** 设置某 session 的队列提示。 */
export function setQueueHint(sessionId: string, hint: string): void {
  messageStore.set({ queueHints: { ...messageStore.get().queueHints, [sessionId]: hint } });
}

/** 设置当前 workspace 选中。 */
export function setCurrentWorkspaceId(workspaceId: string): void {
  workspaceStore.set({ currentWorkspaceId: workspaceId });
}

/** 设置某 workspace 的展开/折叠态（替代 `state.expandedWorkspaces[id] = v`）。 */
export function setExpandedWorkspace(workspaceId: string, expanded: boolean): void {
  workspaceStore.set({ expandedWorkspaces: { ...workspaceStore.get().expandedWorkspaces, [workspaceId]: expanded } });
}

/** 设置某渠道文件夹的展开/折叠态。 */
export function setExpandedChannel(platform: string, expanded: boolean): void {
  workspaceStore.set({ channelExpanded: { ...workspaceStore.get().channelExpanded, [platform]: expanded } });
}

/** 设置某 workspace 的「展开显示全部会话」标记（替代 `state.wsShowAll[id] = true`）。
 *  旧行为：调用方先 `if (!state.wsShowAll) state.wsShowAll = {}` 兜底空对象——这里读 store 快照，
 *  永远是对象，无需兜底，行为等价。 */
export function setWsShowAll(workspaceId: string, value: boolean): void {
  workspaceStore.set({ wsShowAll: { ...workspaceStore.get().wsShowAll, [workspaceId]: value } });
}

/** 切换某 session 在管理弹窗里的选中态（替代 `delete state.selectedSessions[id]` /
 *  `state.selectedSessions[id] = true` 这对绕过 Proxy 的嵌套写）。 */
export function toggleSelectedSession(sessionId: string): void {
  const cur = workspaceStore.get().selectedSessions;
  if (cur[sessionId]) {
    const next = { ...cur };
    delete next[sessionId];
    workspaceStore.set({ selectedSessions: next });
  } else {
    workspaceStore.set({ selectedSessions: { ...cur, [sessionId]: true } });
  }
}

/** 设置某 session 的选中态为指定布尔值（替代 `state.selectedSessions[id] = v`）。 */
export function setSelectedSession(sessionId: string, value: boolean): void {
  workspaceStore.set({ selectedSessions: { ...workspaceStore.get().selectedSessions, [sessionId]: value } });
}

/** 从选中集删除某 session（替代 `delete state.selectedSessions[id]`）。 */
export function removeSelectedSession(sessionId: string): void {
  const cur = workspaceStore.get().selectedSessions;
  if (!(sessionId in cur)) return;
  const next = { ...cur };
  delete next[sessionId];
  workspaceStore.set({ selectedSessions: next });
}

/** 整体替换选中集（替代 `state.selectedSessions = {}` 后逐个 `state.selectedSessions[id] = true`）。 */
export function setSelectedSessions(map: Record<string, boolean>): void {
  workspaceStore.set({ selectedSessions: { ...map } });
}

/** 设置某 session 绑定的外源 Team id。 */
export function setActiveExternalTeamForSession(sessionId: string, teamId: string): void {
  externalStore.set({ activeExternalTeamIdBySession: { ...externalStore.get().activeExternalTeamIdBySession, [sessionId]: teamId } });
}

/**
 * 把订阅会话集合并到 sessionStore，返回合并后的快照（去重、去空）。
 * 用于替代 `state.subscribedSessions.add(...)` 这类绕过 Proxy 的嵌套 mutation。
 */
export function addSubscribedSessions(sessionIds: string[]): string[] {
  const merged = new Set(sessionStore.get().subscribedSessions);
  for (const raw of sessionIds) {
    const id = String(raw || '').trim();
    if (id) merged.add(id);
  }
  sessionStore.set({ subscribedSessions: merged });
  return Array.from(merged);
}

/**
 * 通用 localStorage 读取（含容错）
 */
export function loadFromStorage<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return fallback;
    const parsed = JSON.parse(raw);
    return parsed === null || parsed === undefined ? fallback : (parsed as T);
  } catch {
    return fallback;
  }
}

/**
 * 通用 localStorage 写入（容错：quota exceeded 不抛错）
 */
export function saveToStorage<T>(key: string, value: T): boolean {
  try {
    localStorage.setItem(key, JSON.stringify(value));
    return true;
  } catch (err) {
    console.warn('[storage] write failed:', (err as Error).message);
    return false;
  }
}

// 兼容旧导出：BackendChatSocket 类型由 backend-client 提供
export type { BackendChatSocket } from './backend-client';
// 兼容旧导出：Attachment / Task / Workspace / Mode 类型
export type { Attachment, Task, Workspace, Mode, BackendConfig, BackendSession, ChatMessage, PendingMessage, SessionStatus };
