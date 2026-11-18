/**
 * 会话生命周期：打开 / 历史回填 / 网关 socket 引导 / 后端状态水合。
 *
 * 从 ui/index.ts 抽出（X2）：openSession / loadBackendHistory / bootstrapBackend /
 * hydrateBackendState / ensureDesktopGateway 原样搬迁。
 *
 * 依赖方向（无循环）：session-controller → chat-controller（applyChunk / renderChat 等）
 * 及其它 feature 模块；index.ts init 时调用本模块完成引导。
 */

import { BackendChatSocket, backendApi } from '../backend-client';
import type { BackendHistoryWindow } from '../api/feature-session';
import type { ChatChunk } from '../api/transport';
import { batchStoreUpdates } from '../reducers/store-bus';
import { recordMessageChange } from '../conversation-message-changes';
import type { ChatMessage, PlanReviewStatus } from '../chat-render';
import type { HistoryPagingState } from '../stores/message-store';
import { renderWorkspaceHistory } from './workspaces';
import { refreshKanbanBoard } from './board-hooks';
import { refreshCronJobs } from './cron-page';
import { findChannelSession, loadChannelSessions } from './channel-sessions';
import { discardDraft, getHistoryLoadError, loadWorkspaces, loadSessionsList, refreshSidebarAfterHydrate } from './workspaces';
import { loadConfig } from './model-picker';
import { externalAgentsEnabled, isExternalAgentOrTeamSession } from './external-agents-feature';
import { resetToAgentMode } from './session-mode';
import { loadSessionModel, syncSessionModelUi } from './session-model';
import { loadInspectorContext, refreshInspector } from './inspector';
import { syncCraftLabel } from './composer-toolbar';
import {
  ensureSessionMessages,
  getPendingQueue,
  getSessionNavigationVersion,
  isBusySession,
  markHistoryLoadError,
  newMessageId,
  notify,
  resetBook,
  patchBook,
  setBookTodos,
  setActiveSessionId,
  setCurrentWorkspaceId,
  setEditFrom,
  state,
  type PendingFollowup,
  type TabKey,
  type TodoItem,
} from '../state';
import {
  applyChunk,
  chatOutboxObserver,
  consumePending,
  flushPendingChunks,
  getMessages,
  jumpChatToBottom,
  renderChat,
  seedStatuses,
  setHistoryLoading,
  subscribeSessions,
  updateGatewayDot,
} from './chat-controller';
import {
  getConversationScrollAnchor, preserveConversationHistoryViewport, restoreConversationViewport,
} from './conversation-renderer';
import { mapBackendHistoryItems, mergeTeamInternalMessages } from './history-mapping';
import {
  mergeBackendHistory, prependDurableHistoryPage, prependHistoryWindow, reconcileHistoryWindow,
} from './history-merge';
import { resolveChatRenderTargetId, isStudioView } from './studio-chrome-state';
import { hydrateMissingTurnFileCounts } from './turn-file-counts';
import {
  acceptGatewayResync,
  getGatewayCursors,
  getGatewayResyncCut,
  getLastGatewaySequences,
  isDuplicateGatewayChunk,
  noteGatewaySequence,
  touchStreamActivity,
  trimGatewayDeltaAtCut,
} from './gateway-sequence';
import { installSessionReassembly, resetSession, resetSessionExcept } from '../stream-reassembly';
import { projectActiveTurnSnapshot } from './active-turn-snapshot';
import {
  beginSessionStreamRecovery,
  cancelSessionStreamRecovery,
  getSessionStreamRecovery,
} from './session-stream-recovery';
import { newTurnRequestId, syncSessionLiveFromBackend } from './session-busy';
import { startStreamWatchdog, stopStreamWatchdog } from './stream-watchdog';
import { isRendererLoggedIn, requireRendererLogin } from './auth-gate';
import { logStream } from '../stream-debug';
import { authStore, messageStore, sessionStore, uiStore } from '../stores/stores';
import { hydrateTurnFoldFromStorage } from './fold-state';
import { canSendToBackend, setBackendTransportState } from './backend-status-guard';
import { canSubmitDesktopChat, chatOutbox, desktopOutboxBridge } from './chat-outbox';
import { restoreRendererCheckpointNavigation } from './renderer-checkpoint';

// setTab 由 index.ts 注入（避免 session-controller → index.ts 循环）
let setTabFn: (tab: TabKey) => void = () => {};
let openSessionGeneration = 0;
export function setSessionControllerSetTab(fn: (tab: TabKey) => void): void {
  setTabFn = fn;
}

/** Hydrates a session and optionally leaves the current product page active. */
export async function openSession(
  sessionId: string,
  options: { activateChat?: boolean } = {},
): Promise<void> {
  if (!requireRendererLogin()) return;
  const generation = ++openSessionGeneration;
  discardDraft();
  // 重新打开会话会重载消息，清掉可能残留的编辑态（避免 editFromIdx 指向失效下标）
  setEditFrom(sessionId, null);
  ensureSessionMessages(sessionId);
  setActiveSessionId(sessionId);
  const navigation = getSessionNavigationVersion();
  const isCurrent = () => isRendererLoggedIn() && generation === openSessionGeneration
    && navigation === getSessionNavigationVersion() && state.activeSessionId === sessionId;
  subscribeSessions([sessionId]);
  const row = state.sessions.find((s) => s.id === sessionId) ?? findChannelSession(sessionId);
  if (row) setCurrentWorkspaceId(row.workspaceId);
  window.dispatchEvent(new CustomEvent('workspace:context-changed'));
  // P1-6：订阅后立即渲染首屏（空消息 + historyLoading → 骨架占位），不等历史返回——
  // 首屏可交互时间与历史总量解耦；下方原序列在骨架之上继续执行。
  const showingSkeleton = getMessages(sessionId).length === 0 && !getSessionStreamRecovery(sessionId);
  const beforeModelLoad = historyLoadSeq.get(sessionId);
  if (showingSkeleton) setHistoryLoading(sessionId, true);
  const releaseInitialSkeleton = () => {
    if (showingSkeleton && historyLoadSeq.get(sessionId) === beforeModelLoad) finishHistoryLoading(sessionId);
  };
  renderChat();
  const restoredViewport = restoreConversationViewport(resolveChatRenderTargetId(isStudioView()), sessionId);
  if (!restoredViewport) jumpChatToBottom();
  // 历史消息优先读取逐消息 model；旧消息缺失时回退当前 Session 模型，
  // 因此需先恢复绑定再做历史映射，避免外部 Session 回退成 Crew 默认模型。
  try {
    await loadSessionModel(sessionId);
  } catch (err) {
    if (isCurrent() || state.activeSessionId !== sessionId) releaseInitialSkeleton();
    if (!isCurrent()) return;
    throw err;
  }
  if (!isCurrent()) {
    // Another open of the same session owns the current skeleton.
    if (state.activeSessionId !== sessionId) releaseInitialSkeleton();
    return;
  }
  if (getMessages(sessionId).length === 0) {
    await loadBackendHistory(sessionId, { canApply: isCurrent });
  } else {
    // 内存里已有消息（含修复前落库的幽灵路径）：仍做一次磁盘对账，清掉对话「已编辑」卡里的临时文件。
    void hydrateMissingTurnFileCounts(sessionId).then((changed) => {
      if (changed && state.activeSessionId === sessionId) renderChat();
    });
  }
  if (!isCurrent()) return;
  // 打开已有会话：默认 agent 模式（专家/专家团功能已随旧品牌残留移除）
  state.mode = 'agent';
  state.taskBoardOpen = false;
  syncCraftLabel();
  syncSessionModelUi();
  renderWorkspaceHistory(openSession);
  void refreshKanbanBoard(sessionId);
  renderChat();
  if (!restoredViewport) jumpChatToBottom();
  void refreshCronJobs();
  await loadSessionTodos(sessionId);
  if (!isCurrent()) return;
  renderChat();
  void loadInspectorContext(sessionId);
  refreshInspector();
  window.dispatchEvent(new CustomEvent('session:changed', { detail: { sessionId } }));
  window.dispatchEvent(new CustomEvent('messages:changed', { detail: { sessionId } }));
  if (options.activateChat !== false) setTabFn('chat');
  // Debug: URL hash auto-switch
  if (typeof window !== 'undefined' && window.location.hash) {
    const m = window.location.hash.match(/tab=([a-z]+)/);
    if (m && ['chat','skills','cron','audit','system'].includes(m[1])) {
      setTabFn(m[1] as TabKey);
    }
  }
}

async function loadSessionTodos(sessionId: string): Promise<void> {
  try {
    const { todos } = await backendApi.sessionTodos(sessionId);
    setBookTodos(
      sessionId,
      (todos ?? []).map((t) => ({
        id: t.id,
        content: t.content,
        status: (['pending', 'in_progress', 'completed', 'cancelled'].includes(t.status)
          ? (t.status as TodoItem['status'])
          : 'pending'),
      })),
    );
  } catch {
    /* 后端无此端点或离线：忽略，todo 走 todo_updated 帧实时同步 */
  }
}

/**
 * WS 重连后：若本地仍有在途回合或 streaming 助手消息，跳过 history 替换，
 * 避免把内存里正在累积的流式正文 wipe 掉。
 */
export function shouldSkipHistoryReloadOnReconnect(sessionId: string): boolean {
  if (!sessionId) return false;
  const book = sessionStore.get().books[sessionId];
  const msgs = state.messages[sessionId] ?? [];
  const hasStreamingAssistant = msgs.some((m) => m.role === 'assistant' && m.streaming);
  const hasLocalActiveTurn = Boolean(book?.activeRequestId && !book.turnSealed);
  return hasStreamingAssistant || hasLocalActiveTurn || isBusySession(sessionId);
}

/** loadBackendHistory 的请求序号：start/end 通知、重连等可能并发触发同一会话的
 * history 回填，用 latest-request-wins 防止旧快照覆盖新快照。 */
const historyLoadSeq = new Map<string, number>();

/** 首屏尾部窗口大小（P1-6）：与历史总量解耦的常数；更早历史触顶翻页补。 */
const HISTORY_WINDOW_SIZE = 100;
type BackendSessionStatus = Awaited<ReturnType<typeof backendApi.sessionStatus>>;

function finishHistoryLoading(sessionId: string): void {
  setHistoryLoading(sessionId, false);
  for (const chunk of flushPendingChunks(sessionId) ?? []) handleGatewayChunk(chunk);
}

function recordHistoryChange(previous: ChatMessage[] | undefined, messages: ChatMessage[]): void {
  if (!previous || previous === messages) return;
  const limit = Math.min(previous.length, messages.length);
  let firstChanged = 0;
  while (firstChanged < limit && previous[firstChanged] === messages[firstChanged]) firstChanged += 1;
  recordMessageChange(previous, messages, firstChanged);
}

function commitHistory(sessionId: string, messages: ChatMessage[], paging: HistoryPagingState): void {
  const current = messageStore.get();
  const previous = current.messages[sessionId];
  recordHistoryChange(previous, messages);
  const historyLoadErrors = new Set(current.historyLoadErrors);
  historyLoadErrors.delete(sessionId);
  batchStoreUpdates(() => {
    const editing = state.editFromIdx[sessionId];
    if (editing != null) {
      const target = previous?.[editing];
      const next = target ? messages.findIndex((row) => row.id === target.id) : -1;
      if (next !== editing) setEditFrom(sessionId, next >= 0 ? next : null);
    }
    messageStore.set({
      messages: { ...current.messages, [sessionId]: messages },
      historyPaging: { ...current.historyPaging, [sessionId]: paging },
      historyLoadErrors,
    });
  });
}

export async function loadBackendHistory(
  sessionId: string,
  options: {
    canApply?: () => boolean;
    terminalStatus?: BackendSessionStatus;
    pendingFollowup?: PendingFollowup;
    snapshot?: BackendHistoryWindow;
  } = {},
): Promise<boolean> {
  if (!isRendererLoggedIn() || (getSessionStreamRecovery(sessionId) && !options.terminalStatus)) return false;
  const loadSeq = (historyLoadSeq.get(sessionId) ?? 0) + 1;
  historyLoadSeq.set(sessionId, loadSeq);
  const isLatestLoad = () => historyLoadSeq.get(sessionId) === loadSeq;
  const owner = authStore.get().userInfo?.staffCode;
  const canApply = () => isLatestLoad() && isRendererLoggedIn()
    && authStore.get().userInfo?.staffCode === owner && state.historyLoading.has(sessionId)
    && (options.canApply?.() ?? true);
  const previousPaging = options.snapshot ? undefined : state.historyPaging[sessionId];
  setHistoryLoading(sessionId, true);
  try {
    const [window, st, planState, todoState] = await Promise.all([
      options.snapshot ?? backendApi.historyWindow(sessionId, {
        limit: HISTORY_WINDOW_SIZE,
        ...(previousPaging?.revision && previousPaging.head
          ? { after: previousPaging.head, revision: previousPaging.revision } : {}),
      }),
      options.terminalStatus ?? backendApi.sessionStatus(sessionId).catch(() => null),
      backendApi.sessionPlan(sessionId).catch(() => null),
      backendApi.sessionTodos(sessionId).catch(() => ({ todos: [] })),
    ]);
    if (!canApply()) return false;
    const localMsgs = state.messages[sessionId] ?? [];
    const hasStreamingAssistant = localMsgs.some((m) => m.role === 'assistant' && m.streaming);
    const hasStreamingTeamTurn = localMsgs.some((m) => m.role === 'team_internal' && m.streaming);
    const book = sessionStore.get().books[sessionId];
    const hasLocalActiveTurn = Boolean(book?.activeRequestId && !book.turnSealed);
    if ((st?.live === 'running' || st?.live === 'queued') && (hasStreamingAssistant || hasStreamingTeamTurn || hasLocalActiveTurn)) {
      logStream('history', 'skip-replace-live-streaming', { sessionId, live: st?.live });
      subscribeSessions([sessionId]);
      syncSessionLiveFromBackend(sessionId, st?.live, st?.last_status, st?.active_request_id);
      renderWorkspaceHistory(openSession);
      return true;
    }
    const project = (result: BackendHistoryWindow) => reconcileHistoryWindow(
      state.messages[sessionId] ?? [],
      mergeTeamInternalMessages(mapBackendHistoryItems(result.items, sessionId)),
      result,
      previousPaging,
    );
    let projection = project(window);
    if (!projection) {
      const snapshot = await backendApi.historyWindow(sessionId, { limit: HISTORY_WINDOW_SIZE });
      if (!canApply()) return false;
      projection = project(snapshot);
      if (!projection) return false;
    }
    const history = projection.messages;
    if (st?.last_status === 'failed' && st.last_error) {
      history.push({
        id: newMessageId('error'),
        role: 'error',
        content: st.last_error,
        timestamp: Date.now(),
        model: state.configModel,
      });
    }
    const merged = mergeTeamInternalMessages(mergeBackendHistory(localMsgs, history, {
      ...(st?.live ? { live: st.live } : {}),
      preserveLocalTail: hasStreamingAssistant || hasStreamingTeamTurn || hasLocalActiveTurn,
    }));
    // 重建 plan / 最新 todo 快照到回合消息（renderChat 改读 m.planReview / m.todoSnapshot）。
    // planReview 挂到 plan 轮——最后一条 toolCalls 含 exit_plan_mode 的 assistant 消息
    // （后端历史把 exit_plan_mode 作为 tool_call 落在该轮），满足「在哪轮写就留在哪轮」；
    // todoSnapshot 挂到最后一条 assistant（最新进度）。两者可能不是同一条消息，分开挂载。
    const wantPlan = Boolean(planState?.has_plan && planState.plan);
    const rawTodos = Array.isArray(todoState?.todos) ? todoState.todos : [];
    const todos: TodoItem[] = rawTodos.map((t) => ({
      id: typeof t.id === 'string' ? t.id : '?',
      content: typeof t.content === 'string' ? t.content : '',
      status:
        t.status === 'in_progress' || t.status === 'completed' || t.status === 'cancelled'
          ? t.status
          : 'pending',
    }));
    if (wantPlan) {
      let planIdx = -1;
      for (let i = merged.length - 1; i >= 0; i--) {
        if (merged[i].role === 'assistant' && merged[i].toolCalls?.some((tc) => tc.name === 'exit_plan_mode')) {
          planIdx = i;
          break;
        }
      }
      const status: PlanReviewStatus = planState!.status
        ? planState!.status as PlanReviewStatus
        : planState!.awaiting_approval
          ? 'pending'
          : planState!.active
            ? 'editing'
            : 'readonly';
      const review = {
        plan: planState!.plan!,
        planFile: planState!.plan_file ?? '',
        status,
        sessionId,
        phase: planState!.phase,
      };
      if (planIdx >= 0) {
        merged[planIdx] = { ...merged[planIdx], planReview: review };
      } else {
        // 历史缺失 exit_plan_mode（极旧会话），回退到末尾，保证卡片可见。
        merged.push({
          id: newMessageId('plan'),
          role: 'assistant',
          content: '',
          timestamp: Date.now(),
          planReview: review,
        });
      }
    }
    if (todos.length > 0) {
      let todoIdx = -1;
      for (let i = merged.length - 1; i >= 0; i--) {
        if (merged[i].role === 'assistant') {
          todoIdx = i;
          break;
        }
      }
      if (todoIdx >= 0) {
        merged[todoIdx] = { ...merged[todoIdx], todoSnapshot: todos };
      }
    }
    // A locally queued user is still owned by the outbox until its receipt
    // arrives. History reconciliation confirms durable rows, not pending sends.
    const queuedIds = new Set(getPendingQueue(sessionId).map((entry) => entry.optimisticUserMessageId));
    if (queuedIds.size > 0) {
      const requests = new Set(merged.map((message) => message.requestId).filter(Boolean));
      for (const message of state.messages[sessionId] ?? []) {
        if (message.role === 'user' && queuedIds.has(message.id)
          && (!message.requestId || !requests.has(message.requestId)) && !merged.some((row) => row.id === message.id)) {
          merged.push(message);
        }
      }
    }
    if (todos.length > 0) setBookTodos(sessionId, todos);
    if (state.activeSessionId === sessionId) {
      preserveConversationHistoryViewport(resolveChatRenderTargetId(isStudioView()), sessionId);
    }
    commitHistory(sessionId, merged, projection.paging);
    // 修法3：历史替换后清掉已被替换掉的旧回合 delta 重组缓冲；保留仍在 live 流式的尾巴
    // （其 assistantId 在 merged 里）。避免旧回合缓冲残留导致泄漏 / 串轮。
    resetSessionExcept(sessionId, new Set(merged.map((m) => m.id)));
    // 旧历史 turnFileChanges 可能只有路径、计数为 0：异步读盘补全 +/-，再重绘对话卡。
    void hydrateMissingTurnFileCounts(sessionId).then((changed) => {
      if (changed && state.activeSessionId === sessionId) renderChat();
    });
    // 历史写回后：把 localStorage 里持久化的回合折叠偏好灌进 state 集合，
    // 让重启后历史回合仍保持用户上次手动选择的展开/折叠状态。
    hydrateTurnFoldFromStorage(
      merged.map((m) => m.id),
      { unfolded: state.userUnfoldedTurns, folded: state.userFoldedTurns },
    );
    if (st?.live !== 'running' && st?.live !== 'queued') {
      resetBook(sessionId);
      if (options.pendingFollowup) patchBook(sessionId, { pendingFollowup: options.pendingFollowup });
    } else {
      patchBook(sessionId, { hadTeamInternal: merged.some((message) => message.role === 'team_internal') });
    }
    // Plan 状态恢复：从后端 plan_state 重建 planActive / pendingPlan。
    if (planState?.has_plan) {
      const status: PlanReviewStatus = planState.status
        ? planState.status as PlanReviewStatus
        : planState.awaiting_approval
          ? 'pending'
          : planState.active
            ? 'editing'
            : 'readonly';
      patchBook(sessionId, {
        planActive: planState.active,
        pendingPlan: {
          plan: planState.plan,
          planFile: planState.plan_file,
          status,
        },
      });
    } else {
      patchBook(sessionId, { planActive: Boolean(planState?.active), pendingPlan: null });
    }
    subscribeSessions([sessionId]);
    logStream('history', 'sync-live-before', {
      sessionId,
      live: st?.live,
      active_request_id: st?.active_request_id,
      last_status: st?.last_status,
      historyMsgCount: merged.length,
    });
    syncSessionLiveFromBackend(sessionId, st?.live, st?.last_status, st?.active_request_id);
    logStream('history', 'sync-live-after', {
      sessionId,
      book: sessionStore.get().books[sessionId],
      busy: sessionStore.get().busySessions[sessionId],
    });
    renderWorkspaceHistory(openSession);
    return true;
  } catch {
    // 历史加载失败不再静默清空：保留内存中已有消息；无消息可显示时置错误标记，
    // 聊天区渲染「加载失败 + 重试」卡（__history_error 单元），失败对用户可见。
    if (canApply()) {
      const hadMessages = (state.messages[sessionId] ?? []).length > 0;
      if (!hadMessages) markHistoryLoadError(sessionId);
      logStream('history', 'load-failed', { sessionId });
      notify('会话历史加载失败，可点击对话区「重试」');
      if (state.activeSessionId === sessionId) renderChat();
    }
    return false;
  } finally {
    // history 写回完成：flush 期间排队的迟到分片（P2-2），再清 loading 标记。
    // 仅最新请求收尾：旧请求不得清 loading（新请求仍在排队窗口内）也不得 flush。
    if (isLatestLoad() && state.historyLoading.has(sessionId)) finishHistoryLoading(sessionId);
  }
}

// ---------- P1-6：触顶翻页（更早历史按游标前缀补页） ----------

const olderPageInFlight = new Map<string, object>();
const historyRowSelector = ':scope > .messages__inner > .msg[data-message-id]';

interface HistoryViewport {
  height: number;
  top: number;
  anchor: { element: HTMLElement; id: string; offset: number } | undefined;
}

function captureHistoryViewport(container: HTMLElement): HistoryViewport {
  const viewportTop = container.getBoundingClientRect().top + container.clientTop;
  const viewportBottom = viewportTop + container.clientHeight;
  const rows = container.querySelectorAll<HTMLElement>(historyRowSelector);
  // Paging can resolve after the reader scrolled elsewhere. Locate the first visible row
  // without measuring every offscreen message.
  let start = 0;
  let end = rows.length;
  while (start < end) {
    const middle = (start + end) >>> 1;
    if (rows[middle].getBoundingClientRect().bottom <= viewportTop) start = middle + 1;
    else end = middle;
  }
  const element = rows[start];
  const id = element?.dataset.messageId;
  const top = element?.getBoundingClientRect().top;
  const anchor = id && top !== undefined && top < viewportBottom ? { element, id, offset: top - viewportTop } : undefined;
  return { height: container.scrollHeight, top: container.scrollTop, anchor };
}

function restoreHistoryViewport(container: HTMLElement, { height, top, anchor }: HistoryViewport): void {
  const element = anchor && (container.contains(anchor.element) && anchor.element.dataset.messageId === anchor.id
    ? anchor.element
    : Array.from(container.querySelectorAll<HTMLElement>(historyRowSelector)).find((row) => row.dataset.messageId === anchor.id));
  if (anchor && element) {
    const viewportTop = container.getBoundingClientRect().top + container.clientTop;
    const offset = element.getBoundingClientRect().top - viewportTop;
    container.scrollTop += offset - anchor.offset;
  } else {
    container.scrollTop = top + (container.scrollHeight - height);
  }
}

/**
 * 加载更早一页历史并前缀拼接。滚动距顶 < 2 视口时由滚动预取触发；
 * 互斥游标保证不重叠，prependHistoryWindow 只做重写场景的兜底去重。
 * 锚定翻页提交前仍在视口中的消息，保留它到视口顶部的距离。
 */
export async function loadOlderHistoryPage(sessionId: string): Promise<boolean> {
  const paging = state.historyPaging[sessionId];
  if (!paging?.hasMore || !paging.nextBefore) return false;
  if (!isRendererLoggedIn() || state.activeSessionId !== sessionId || state.historyLoading.has(sessionId)
    || olderPageInFlight.has(sessionId) || getSessionStreamRecovery(sessionId)) return false;
  const historyVersion = historyLoadSeq.get(sessionId);
  const owner = authStore.get().userInfo?.staffCode;
  const navigation = getSessionNavigationVersion();
  const pageLoad = {};
  olderPageInFlight.set(sessionId, pageLoad);
  const canApply = () => isRendererLoggedIn() && authStore.get().userInfo?.staffCode === owner
    && navigation === getSessionNavigationVersion() && state.activeSessionId === sessionId
    && paging === state.historyPaging[sessionId]
    && historyVersion === historyLoadSeq.get(sessionId) && olderPageInFlight.get(sessionId) === pageLoad;
  logStream('history', 'load-older-start', { sessionId, before: paging.nextBefore });
  try {
    const res = await backendApi.historyWindow(sessionId, {
      limit: HISTORY_WINDOW_SIZE,
      before: paging.nextBefore,
      ...(paging.revision ? { revision: paging.revision } : {}),
    });
    if (!canApply()) return false;
    if (res.mode === 'snapshot') {
      const applied = await loadBackendHistory(sessionId, {
        snapshot: res,
        canApply: () => navigation === getSessionNavigationVersion() && state.activeSessionId === sessionId,
      });
      if (applied && state.activeSessionId === sessionId) renderChat();
      return applied;
    }
    if ((paging.revision && res.revision !== paging.revision) || res.mode === 'append'
      || (res.has_more && res.next_before === paging.nextBefore)) return false;
    const current = state.messages[sessionId] ?? [];
    if (current.length === 0) {
      // 会话已被清空（删除/重开首窗）：迟到的旧页直接丢弃
      return false;
    }
    const older = mergeTeamInternalMessages(mapBackendHistoryItems(res.items, sessionId));
    const containerId = resolveChatRenderTargetId(isStudioView());
    const container = state.activeSessionId === sessionId ? document.getElementById(containerId) : null;
    const scrollAnchor = container ? getConversationScrollAnchor(containerId) : null;
    const rendererPreservesViewport = container && preserveConversationHistoryViewport(containerId, sessionId);
    const viewport = !rendererPreservesViewport && container?.clientHeight && !scrollAnchor?.isStickyBottom()
      ? captureHistoryViewport(container) : undefined;
    const messages = res.revision
      ? prependDurableHistoryPage(current, older)
      : prependHistoryWindow(current, older);
    commitHistory(sessionId, messages, { ...paging, hasMore: res.has_more, nextBefore: res.next_before });
    window.dispatchEvent(new CustomEvent('messages:changed', { detail: { sessionId } }));
    if (state.activeSessionId === sessionId) {
      renderChat();
      if (container && viewport && !scrollAnchor?.isStickyBottom()
        && state.activeSessionId === sessionId && containerId === resolveChatRenderTargetId(isStudioView())
        && document.getElementById(containerId) === container) {
        restoreHistoryViewport(container, viewport);
      }
    }
    logStream('history', 'load-older-applied', {
      sessionId,
      olderCount: older.length,
      hasMore: res.has_more,
    });
    return true;
  } catch (err) {
    logStream('history', 'load-older-failed', { sessionId, error: String(err) });
    return false;
  } finally {
    if (olderPageInFlight.get(sessionId) === pageLoad) olderPageInFlight.delete(sessionId);
  }
}

/** 滚动预取：距顶 < 2 视口即取上一页（对齐 ZCode 提前量，不等滚到顶）。 */
let historyScrollInstalled = false;

export function installHistoryPagingScroll(): void {
  if (typeof window === 'undefined' || historyScrollInstalled) return;
  historyScrollInstalled = true;
  // scroll 事件不冒泡但可捕获：capture 在 window 上代理，容器元素替换也无需重绑
  window.addEventListener(
    'scroll',
    (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      const sid = state.activeSessionId;
      if (!sid || !state.historyPaging[sid]?.hasMore) return;
      if (target.id !== resolveChatRenderTargetId(isStudioView())) return;
      if (target.scrollTop > target.clientHeight * 2) return;
      void loadOlderHistoryPage(sid);
    },
    { capture: true, passive: true },
  );
}

let recoveryConnection: (() => boolean) | null = null;
const recoveryChecks = new Map<string, { recovery: object; connection: () => boolean; retry: boolean }>();

// An incomplete stream cannot be repaired from the remaining suffix. Reconcile
// via the durable transcript once the Gateway has settled the turn.
export async function reconcileSessionAfterResync(sessionId: string, terminalFrame = false): Promise<void> {
  const recovery = getSessionStreamRecovery(sessionId);
  const connection = recoveryConnection;
  if (!recovery || !connection?.() || state.suppressChunks.has(sessionId)) return;
  const existing = recoveryChecks.get(sessionId);
  if (existing?.recovery === recovery && existing.connection === connection) {
    existing.retry ||= terminalFrame;
    return;
  }
  const check = { recovery, connection, retry: false };
  recoveryChecks.set(sessionId, check);
  const progress = recovery.progress;
  const before = sessionStore.get().books[sessionId];
  const isCurrent = () => isRendererLoggedIn() && connection()
    && getSessionStreamRecovery(sessionId) === recovery
    && !state.suppressChunks.has(sessionId);
  const canApply = () => {
    const current = sessionStore.get().books[sessionId];
    return isCurrent()
      && progress === recovery.progress
      && current?.activeRequestId === before?.activeRequestId
      && current?.turnSealed === before?.turnSealed
      && current?.pendingFollowup === before?.pendingFollowup
      && current?.pendingPlan === before?.pendingPlan;
  };
  try {
    const status = await backendApi.sessionStatus(sessionId).catch(() => null);
    if (!canApply() || !status?.live) return;
    if (status.live === 'running' || status.live === 'queued') {
      syncSessionLiveFromBackend(sessionId, status.live, status.last_status, status.active_request_id);
      return;
    }
    const pendingFollowup = before?.pendingFollowup?.questionId === recovery.verifiedFollowupId
      ? before?.pendingFollowup : null;
    if (pendingFollowup && recovery.historyAppliedForFollowup === pendingFollowup.questionId) return;
    const applied = await loadBackendHistory(sessionId, {
      canApply, terminalStatus: status, ...(pendingFollowup ? { pendingFollowup } : {}),
    });
    if (!applied || !isCurrent()) return;
    if (pendingFollowup && sessionStore.get().books[sessionId]?.pendingFollowup === pendingFollowup) {
      recovery.historyAppliedForFollowup = pendingFollowup.questionId;
      if (state.activeSessionId === sessionId) renderChat();
      return;
    }
    cancelSessionStreamRecovery(sessionId);
    if (state.activeSessionId === sessionId) renderChat();
    consumePending(sessionId);
  } finally {
    if (recoveryChecks.get(sessionId) === check) recoveryChecks.delete(sessionId);
    if (check.retry && isCurrent()) void reconcileSessionAfterResync(sessionId);
  }
}

const INCOMPLETE_STREAM_KINDS = new Set<ChatChunk['kind']>([
  'delta', 'thinking', 'tool', 'status', 'final', 'error', 'todo_updated', 'file_changes',
]);

function requestStreamRepair(sessionId: string): void {
  const repair = getSessionStreamRecovery(sessionId)?.repair;
  const connection = recoveryConnection;
  const socket = state.socket;
  if (!repair || !connection?.() || !socket || repair.attempt?.connection === connection) return;

  const epoch = getGatewayCursors([sessionId])[sessionId]?.epoch;
  const id = newTurnRequestId();
  repair.attempt = { id, connection, ...(epoch ? { epoch } : {}) };
  void socket.requestStreamSnapshot(sessionId, id).catch(() => {});
}

function recoverStreamGap(sessionId: string): void {
  if (getSessionStreamRecovery(sessionId) || state.suppressChunks.has(sessionId)) return;
  beginSessionStreamRecovery(sessionId, getGatewayCursors([sessionId])[sessionId] ?? null);
  historyLoadSeq.set(sessionId, (historyLoadSeq.get(sessionId) ?? 0) + 1);
  olderPageInFlight.delete(sessionId);
  flushPendingChunks(sessionId);
  setHistoryLoading(sessionId, false);
  requestStreamRepair(sessionId);
  if (state.activeSessionId === sessionId) renderChat();
  void reconcileSessionAfterResync(sessionId);
}

function applyActiveTurnSnapshot(sessionId: string, control: ChatChunk): boolean {
  let projection: ReturnType<typeof projectActiveTurnSnapshot>;
  try {
    projection = projectActiveTurnSnapshot(sessionId, control, {
      messages: getMessages(sessionId),
      book: sessionStore.get().books[sessionId],
      queue: getPendingQueue(sessionId),
      paging: state.historyPaging[sessionId],
    });
  } catch (error) {
    logStream('history', 'active-snapshot-invalid', { sessionId, error: String(error) });
    return false;
  }
  if (!projection) return false;

  const { messages, book, paging, status, busy, queueHint, reassembly } = projection;
  if (state.activeSessionId === sessionId) {
    preserveConversationHistoryViewport(resolveChatRenderTargetId(isStudioView()), sessionId);
  }
  installSessionReassembly(sessionId, reassembly);
  cancelSessionStreamRecovery(sessionId);
  touchStreamActivity(sessionId);
  batchStoreUpdates(() => {
    const currentMessages = messageStore.get();
    recordHistoryChange(currentMessages.messages[sessionId], messages);
    const historyLoading = new Set(currentMessages.historyLoading);
    const historyLoadErrors = new Set(currentMessages.historyLoadErrors);
    historyLoading.delete(sessionId);
    historyLoadErrors.delete(sessionId);
    messageStore.set({
      messages: { ...currentMessages.messages, [sessionId]: messages },
      historyPaging: { ...currentMessages.historyPaging, [sessionId]: paging },
      queueHints: { ...currentMessages.queueHints, [sessionId]: queueHint },
      historyLoading,
      historyLoadErrors,
    });
    const currentSessions = sessionStore.get();
    sessionStore.set({
      books: { ...currentSessions.books, [sessionId]: book },
      busySessions: { ...currentSessions.busySessions, [sessionId]: busy },
      sessionStatuses: { ...currentSessions.sessionStatuses, [sessionId]: status },
    });
  });
  hydrateTurnFoldFromStorage(messages.map((message) => message.id), {
    unfolded: state.userUnfoldedTurns, folded: state.userFoldedTurns,
  });
  if (state.activeSessionId === sessionId) renderChat();
  logStream('history', 'active-snapshot-applied', {
    sessionId, requestId: book.activeRequestId, cut: control.gateway_sequence, messageCount: messages.length,
  });
  return true;
}

function handleGatewayChunk(chunk: ChatChunk): void {
  if (chatOutbox.handle(chunk)) return;
  const sessionId = chunk.session_id;
  if (chunk.kind === 'gateway_resync') {
    if (!sessionId || (!state.subscribedSessions.has(sessionId) && state.activeSessionId !== sessionId)) return;
    const previousEpoch = getGatewayCursors([sessionId])[sessionId]?.epoch;
    const previousRecovery = getSessionStreamRecovery(sessionId);
    const correlated = chunk.body?.reason === 'client_stream_gap' || chunk.body?.repair_id !== undefined;
    if (correlated) {
      const attempt = previousRecovery?.repair?.attempt;
      if (chunk.body?.reason !== 'client_stream_gap' || !attempt || attempt.id !== chunk.body.repair_id
        || attempt.connection !== recoveryConnection || !attempt.connection()
        || (attempt.epoch !== previousEpoch && chunk.gateway_epoch !== previousEpoch)) return;
      const minimum = previousRecovery?.repair?.minimumCut;
      if (minimum && chunk.gateway_epoch === minimum.epoch && typeof chunk.gateway_sequence === 'number'
        && chunk.gateway_sequence < minimum.sequence) return;
    }
    if (!acceptGatewayResync(sessionId, chunk, correlated) || state.suppressChunks.has(sessionId)) return;
    historyLoadSeq.set(sessionId, (historyLoadSeq.get(sessionId) ?? 0) + 1);
    olderPageInFlight.delete(sessionId);
    flushPendingChunks(sessionId);
    if (applyActiveTurnSnapshot(sessionId, chunk)) return;
    const recovery = beginSessionStreamRecovery(sessionId);
    if (!correlated && previousRecovery?.repair) recovery.repair = previousRecovery.repair;
    if (previousEpoch === chunk.gateway_epoch && previousRecovery?.verifiedFollowupId) {
      recovery.verifiedFollowupId = previousRecovery.verifiedFollowupId;
    }
    setHistoryLoading(sessionId, false);
    resetSession(sessionId);
    if (previousEpoch && previousEpoch !== chunk.gateway_epoch) patchBook(sessionId, { pendingFollowup: null });
    const followups = chunk.body?.pending_followups;
    if (Array.isArray(followups)) {
      for (const pending of followups) {
        if (pending?.kind !== 'followup_question' || pending.session_id !== sessionId
          || !Array.isArray(pending.body?.questions) || !pending.body?.question_id) continue;
        if (sessionStore.get().books[sessionId]?.pendingFollowup?.questionId !== pending.body.question_id) {
          // The Gateway has verified this is still waiting. It is outside replay
          // and may refer to a request the renderer never saw over the socket.
          applyChunk({ ...pending, request_id: undefined, gateway_epoch: undefined, gateway_sequence: undefined });
        }
        if (sessionStore.get().books[sessionId]?.pendingFollowup?.questionId === pending.body.question_id) {
          recovery.verifiedFollowupId = pending.body.question_id;
        }
      }
    }
    if (state.activeSessionId === sessionId) renderChat();
    void reconcileSessionAfterResync(sessionId);
    return;
  }
  const recovery = sessionId && getSessionStreamRecovery(sessionId);
  if (sessionId && recovery) {
    if (INCOMPLETE_STREAM_KINDS.has(chunk.kind)
      || (chunk.kind === 'feature_event' && chunk.body?.feature === 'team' && chunk.body?.event === 'internal_message')) {
      if (isDuplicateGatewayChunk(sessionId, chunk)) return;
      noteGatewaySequence(sessionId, chunk);
      recovery.progress += 1;
      if (chunk.kind === 'final' || (chunk.kind === 'error' && chunk.is_final)) {
        void reconcileSessionAfterResync(sessionId, true);
      }
      return;
    }
    if (chunk.kind === 'followup_question' || chunk.kind === 'plan_review') {
      if (isDuplicateGatewayChunk(sessionId, chunk)) return;
      recovery.progress += 1;
      if (chunk.kind === 'followup_question' && typeof chunk.body?.question_id === 'string'
        && Array.isArray(chunk.body.questions)) {
        recovery.verifiedFollowupId = chunk.body.question_id;
      }
      // Post-cut controls describe an interaction even when the original
      // request was never opened locally.
      const interaction = { ...chunk };
      delete interaction.request_id;
      applyChunk(interaction);
      if (chunk.kind === 'followup_question') {
        const pending = sessionStore.get().books[sessionId]?.pendingFollowup;
        if (!pending && recovery.historyAppliedForFollowup) void reconcileSessionAfterResync(sessionId, true);
      }
      return;
    }
  }
  if (chunk.kind === 'followup_question' && sessionId && getGatewayResyncCut(sessionId)
    && chunk.body?.question_id && chunk.body.question_id === sessionStore.get().books[sessionId]?.pendingFollowup?.questionId) {
    // A verified waiting question can have a side-channel request id.
    const interaction = { ...chunk };
    delete interaction.request_id;
    applyChunk(interaction);
    return;
  }
  const deltaSessionId = sessionId ?? state.activeSessionId;
  const frame = deltaSessionId ? trimGatewayDeltaAtCut(deltaSessionId, chunk) : chunk;
  if (frame && applyChunk(frame) === 'repair' && deltaSessionId) recoverStreamGap(deltaSessionId);
}

async function reconcileSessionAfterReconnect(
  sessionId: string,
  active: boolean,
  isCurrentConnection: () => boolean,
): Promise<void> {
  if (state.suppressChunks.has(sessionId)) return;
  if (getSessionStreamRecovery(sessionId)) {
    await reconcileSessionAfterResync(sessionId);
    return;
  }
  const before = sessionStore.get().books[sessionId];
  const beforeResync = getGatewayResyncCut(sessionId);
  const canApply = () => {
    const current = sessionStore.get().books[sessionId];
    return isCurrentConnection()
      && getGatewayResyncCut(sessionId) === beforeResync
      && !getSessionStreamRecovery(sessionId)
      && !state.suppressChunks.has(sessionId)
      && current?.activeRequestId === before?.activeRequestId
      && current?.turnSealed === before?.turnSealed;
  };
  const inFlight = shouldSkipHistoryReloadOnReconnect(sessionId);
  if (active && !inFlight) {
    await loadBackendHistory(sessionId, { canApply });
    return;
  }

  const status = await backendApi.sessionStatus(sessionId).catch(() => null);
  if (!canApply() || !status?.live) return;
  if (inFlight && status.live !== 'running' && status.live !== 'queued') {
    if (chatOutbox.hasUnsettledTurn(sessionId)) return;
    // A turn may have settled while disconnected. Read history after observing
    // the terminal state so a replay gap cannot leave a permanently partial turn.
    await loadBackendHistory(sessionId, { canApply, terminalStatus: status });
    return;
  }
  syncSessionLiveFromBackend(sessionId, status.live, status.last_status, status.active_request_id);
}

let channelSessionListenerBound = false;

function bindChannelSessionListener(): void {
  if (channelSessionListenerBound || typeof window === 'undefined') return;
  channelSessionListenerBound = true;
  window.addEventListener('channel-session:updated', (ev) => {
    const d = (ev as CustomEvent<{ sessionId?: string }>).detail;
    if (d?.sessionId && d.sessionId === state.activeSessionId) {
      // 渠道回合结束后历史已落库：全量刷新后必须重绘对话区，
      // 否则 store 已更新但视图不重绘，用户要切换会话才能看到新消息。
      void loadBackendHistory(d.sessionId).then(() => {
        if (state.activeSessionId === d.sessionId) renderChat();
      });
    }
  });
}

export function bootstrapBackend(): void {
  bindChannelSessionListener();
  if (!isRendererLoggedIn()) return;
  state.socket?.dispose();
  chatOutbox.disconnect();
  setBackendTransportState('unknown');
  recoveryConnection = null;
  let transportVersion = 0;
  const socket = new BackendChatSocket(
    handleGatewayChunk,
    (open, meta) => {
      if (state.socket !== socket) return;
      transportVersion += 1;
      if (!open) {
        recoveryConnection = null;
        chatOutbox.disconnect();
      }
      logStream('ws-renderer', open ? 'socket-open' : 'socket-close', {
        subscribed: Array.from(state.subscribedSessions),
        activeSessionId: state.activeSessionId,
        transient: meta?.transient ?? false,
      });
      setBackendTransportState(
        open ? 'connected' : meta?.transient ? 'reconnecting' : 'disconnected',
        meta?.gatewayGeneration,
      );
      updateGatewayDot();
      // The Gateway can keep generating after this socket closes. Keep the
      // request and its accumulated stream open for replay.
      renderWorkspaceHistory(openSession);
    },
    () => {
      if (desktopOutboxBridge()) chatOutbox.connect((frame, scope, storeId) => socket.send(frame, scope, storeId));
      const version = transportVersion;
      const isCurrentConnection = () => state.socket === socket && transportVersion === version;
      recoveryConnection = isCurrentConnection;
      subscribeSessions(Array.from(state.subscribedSessions));
      const active = state.activeSessionId;
      const sessions = new Set(state.subscribedSessions);
      if (active) sessions.add(active);
      for (const sid of sessions) requestStreamRepair(sid);
      const recoveries = Array.from(sessions, (sid) =>
        reconcileSessionAfterReconnect(sid, sid === active, isCurrentConnection).then(() => {
          if (isCurrentConnection() && state.activeSessionId === sid) renderChat();
        }),
      );
      void Promise.allSettled(recoveries).then(() => {
        if (isCurrentConnection()) renderWorkspaceHistory(openSession);
      });
    },
  );
  state.socket = socket;
  const startup = uiStore.get().backendReadiness;
  if (startup) socket.setGatewayGeneration(startup.generation);
  socket.bindLastGatewaySequences(getLastGatewaySequences, getGatewayCursors);
  socket.connect();
  startStreamWatchdog();
  installHistoryPagingScroll();
}

async function resumeOutboxQueues(): Promise<void> {
  const socket = state.socket;
  const owner = state.userInfo?.staffCode;
  const restored = Object.entries(messageStore.get().pendingQueues).filter(
    ([, items]) => items.some((item) => item.requestId && !item.requiresReview),
  );
  await Promise.allSettled(restored.map(async ([sessionId]) => {
    const status = await backendApi.sessionStatus(sessionId);
    if (state.socket === socket && state.userInfo?.staffCode === owner && canSubmitDesktopChat()
      && status.live === 'idle' && !isBusySession(sessionId)) consumePending(sessionId);
  }));
}

/** Activate chat for a verified auth session once the renderer is mounted. */
export function bindBackendAuth(): () => void {
  const sync = (loggedIn: boolean): void => {
    if (loggedIn) {
      if (state.socket) return;
      const bridge = desktopOutboxBridge();
      if (bridge) {
        const owner = authStore.get().userInfo?.staffCode;
        void chatOutbox.bind(bridge, {
          onRestored: chatOutboxObserver.onRestored,
          onReceipt(entry, status, code, restored) {
            chatOutboxObserver.onReceipt(entry, status, code, restored);
            if (!restored || (status !== 'completed' && status !== 'failed' && status !== 'cancelled')) return;
            const socket = state.socket;
            const currentConnection = recoveryConnection;
            if (!socket || !currentConnection) return;
            void reconcileSessionAfterReconnect(entry.sessionId, true, () =>
              currentConnection() && state.socket === socket && authStore.get().userInfo?.staffCode === owner,
            ).then(() => {
              if (currentConnection() && state.activeSessionId === entry.sessionId) renderChat();
            });
          },
        }).catch(() => {
          if (authStore.get().userInfo?.staffCode === owner) notify('无法加载待发消息，草稿已保留');
        });
      }
      bootstrapBackend();
      if (!uiStore.get().backendReadiness) void hydrateBackendState().catch(() => {});
      return;
    }
    const socket = state.socket;
    state.socket = null;
    socket?.dispose();
    chatOutbox.reset();
    stopStreamWatchdog();
    setBackendTransportState('unknown');
    recoveryConnection = null;
  };
  const unsubscribe = authStore.subscribe((next, previous) => {
    if (next.isLoggedIn && previous.isLoggedIn && next.userInfo?.staffCode !== previous.userInfo?.staffCode) {
      sync(false);
      messageStore.set({ pendingQueues: {} });
    }
    if (next.isLoggedIn !== previous.isLoggedIn || next.userInfo?.staffCode !== previous.userInfo?.staffCode) {
      sync(next.isLoggedIn);
    }
  });
  const unsubscribeOutbox = uiStore.subscribe((next, previous) => {
    if (next.backendCanSend && next.backendReceiptsReady
      && (!previous.backendCanSend || !previous.backendReceiptsReady)) void resumeOutboxQueues();
  });
  sync(authStore.get().isLoggedIn);
  return () => {
    unsubscribe();
    unsubscribeOutbox();
    sync(false);
  };
}

export async function hydrateBackendState(): Promise<void> {
  if (!isRendererLoggedIn() || !canSendToBackend()) return;
  // 各路后端加载相互隔离：任一路失败不阻断其余，也不再让 sessions 挂掉连累 workspaces。
  // loadSessionsList / loadWorkspaces 内部已自带 try-catch + 错误态（区分「加载失败 vs 真空」），
  // 这里只对没有内部容错的 loadConfig/loadChannelSessions 包一层。
  const sessions = await loadSessionsList();
  // loadSessionsList 内部已调 syncSessionsFromBackend（含 mergeSessionModelsFromBackend），
  // 这里不再重复同步；sessions 仅用于判断是否拿到数据。
  const statuses = await backendApi.sessionsStatus().catch(() => ({}));
  if (sessions.length) {
    // sessions 已同步进 state；保留判断分支以兼容空数据态。
  }
  seedStatuses(statuses);
  await loadWorkspaces();
  await loadConfig().catch((err) => notify(`加载模型配置失败：${(err as Error).message}`));
  const activeSession = state.activeSessionId
    ? state.sessions.find((session) => session.id === state.activeSessionId)
    : undefined;
  if (!externalAgentsEnabled() && isExternalAgentOrTeamSession(activeSession)) {
    setActiveSessionId(null);
    setCurrentWorkspaceId('default');
    resetToAgentMode();
    renderChat();
  }
  await loadChannelSessions().catch((err) => notify(`加载渠道会话失败：${(err as Error).message}`));
  const { sessions: sessionLoadError, workspaces: workspaceLoadError } = getHistoryLoadError();
  if (!sessionLoadError && !workspaceLoadError) await restoreRendererCheckpointNavigation(openSession);
  updateGatewayDot();
  refreshSidebarAfterHydrate(openSession);
}

export async function ensureDesktopGateway(): Promise<void> {
  const api = (window as Window & {
    Crew?: { ensureGateway?: () => Promise<{ baseUrl?: string; managed?: boolean }> };
  }).Crew;
  const ensureGateway = api?.ensureGateway;
  if (!ensureGateway) return;
  try {
    const result = await ensureGateway();
    if (result?.baseUrl) {
      localStorage.setItem('Crew.gatewayBase', result.baseUrl);
    }
  } catch {
    // 桌面端没有成功拉起 gateway 时，继续走默认连接状态和页面提示。
  }
}
