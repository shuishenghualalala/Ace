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
import type { PlanReviewStatus } from '../chat-render';
import { renderWorkspaceHistory } from './workspaces';
import { refreshKanbanBoard } from './kanban-board';
import { refreshCronJobs } from './cron-page';
import { findChannelSession, loadChannelSessions } from './channel-sessions';
import { discardDraft, loadWorkspaces, loadSessionsList, refreshSidebarAfterHydrate } from './workspaces';
import { loadConfig } from './model-picker';
import { externalAgentsEnabled, isExternalAgentOrTeamSession } from './external-agents-feature';
import { resetToAgentMode } from './session-mode';
import { loadSessionModel, syncSessionModelUi } from './session-model';
import { loadInspectorContext, refreshInspector } from './inspector';
import { syncCraftLabel } from './composer-toolbar';
import {
  ensureSessionMessages,
  isBusySession,
  newMessageId,
  notify,
  replaceSessionMessages,
  resetBook,
  patchBook,
  setBookTodos,
  setActiveSessionId,
  setCurrentWorkspaceId,
  setEditFrom,
  state,
  type TabKey,
  type TodoItem,
} from '../state';
import {
  applyChunk,
  flushPendingChunks,
  getMessages,
  jumpChatToBottom,
  renderChat,
  seedStatuses,
  setBusyWithUi,
  setHistoryLoading,
  subscribeSessions,
  finalizeStreamingTurn,
  updateGatewayDot,
} from './chat-controller';
import { mapBackendHistoryItem, mergeTeamInternalMessages } from './history-mapping';
import { mergeBackendHistory } from './history-merge';
import { hydrateMissingTurnFileCounts } from './turn-file-counts';
import { getLastGatewaySequences } from './gateway-sequence';
import { resetSessionExcept } from '../stream-reassembly';
import { syncSessionLiveFromBackend } from './session-busy';
import { startStreamWatchdog } from './stream-watchdog';
import { isRendererLoggedIn, requireRendererLogin } from './auth-gate';
import { logStream } from '../stream-debug';
import { sessionStore } from '../stores/stores';
import { hydrateTurnFoldFromStorage } from './fold-state';

// setTab 由 index.ts 注入（避免 session-controller → index.ts 循环）
let setTabFn: (tab: TabKey) => void = () => {};
export function setSessionControllerSetTab(fn: (tab: TabKey) => void): void {
  setTabFn = fn;
}

/** Hydrates a session and optionally leaves the current product page active. */
export async function openSession(
  sessionId: string,
  options: { activateChat?: boolean } = {},
): Promise<void> {
  if (!requireRendererLogin()) return;
  discardDraft();
  // 重新打开会话会重载消息，清掉可能残留的编辑态（避免 editFromIdx 指向失效下标）
  setEditFrom(sessionId, null);
  ensureSessionMessages(sessionId);
  setActiveSessionId(sessionId);
  subscribeSessions([sessionId]);
  const row = state.sessions.find((s) => s.id === sessionId) ?? findChannelSession(sessionId);
  if (row) setCurrentWorkspaceId(row.workspaceId);
  window.dispatchEvent(new CustomEvent('workspace:context-changed'));
  // 历史消息优先读取逐消息 model；旧消息缺失时回退当前 Session 模型，
  // 因此需先恢复绑定再做历史映射，避免外部 Session 回退成 Crew 默认模型。
  await loadSessionModel(sessionId);
  if (getMessages(sessionId).length === 0) {
    await loadBackendHistory(sessionId);
  } else {
    // 内存里已有消息（含修复前落库的幽灵路径）：仍做一次磁盘对账，清掉对话「已编辑」卡里的临时文件。
    void hydrateMissingTurnFileCounts(sessionId).then((changed) => {
      if (changed && state.activeSessionId === sessionId) renderChat();
    });
  }
  // 打开已有会话：默认 agent 模式（专家/专家团功能已随旧品牌残留移除）
  state.mode = 'agent';
  state.taskBoardOpen = false;
  syncCraftLabel();
  syncSessionModelUi();
  renderWorkspaceHistory(openSession);
  void refreshKanbanBoard(sessionId);
  renderChat();
  // 会话切换：强制跳到底部（重置 stickyBottom）。对齐 hermes 的「session change → jumpToBottom」。
  jumpChatToBottom();
  void refreshCronJobs();
  await loadSessionTodos(sessionId);
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
 * WS 重连后：若本地仍有在途回合或 streaming 助手消息，跳过全量 history 替换，
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

/**
 * 真断连时仅处理订阅中或 busy 的会话，避免无关会话被全局 finalize。
 */
export function sessionsAffectedByDisconnect(): string[] {
  const sids = new Set<string>(state.subscribedSessions);
  for (const sid of Object.keys(state.messages)) {
    if (isBusySession(sid)) sids.add(sid);
  }
  return Array.from(sids).filter(Boolean);
}

/** loadBackendHistory 的请求序号：start/end 通知、重连等可能并发触发同一会话的
 * history 回填，用 latest-request-wins 防止旧快照覆盖新快照。 */
const historyLoadSeq = new Map<string, number>();

export async function loadBackendHistory(sessionId: string): Promise<void> {
  if (!isRendererLoggedIn()) return;
  const loadSeq = (historyLoadSeq.get(sessionId) ?? 0) + 1;
  historyLoadSeq.set(sessionId, loadSeq);
  const isLatestLoad = () => historyLoadSeq.get(sessionId) === loadSeq;
  setHistoryLoading(sessionId, true);
  try {
    const [items, st, planState, todoState] = await Promise.all([
      backendApi.history(sessionId),
      backendApi.sessionStatus(sessionId).catch(() => null),
      backendApi.sessionPlan(sessionId).catch(() => null),
      backendApi.sessionTodos(sessionId).catch(() => ({ todos: [] })),
    ]);
    // 已有更新的回填请求在跑：丢弃旧快照，写回/flush 都由最新请求收尾。
    if (!isLatestLoad()) return;
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
      return;
    }
    const history = mergeTeamInternalMessages(items.map((item) => mapBackendHistoryItem(item, sessionId)));
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
    const todos: TodoItem[] = rawTodos.map((t: any) => ({
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
    if (todos.length > 0) setBookTodos(sessionId, todos);
    replaceSessionMessages(sessionId, merged);
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
  } catch {
    if (isLatestLoad()) replaceSessionMessages(sessionId, []);
  } finally {
    // history 写回完成：flush 期间排队的迟到分片（P2-2），再清 loading 标记。
    // 仅最新请求收尾：旧请求不得清 loading（新请求仍在排队窗口内）也不得 flush。
    if (isLatestLoad()) {
      setHistoryLoading(sessionId, false);
      const queued = flushPendingChunks(sessionId);
      if (queued) {
        for (const c of queued) applyChunk(c);
      }
    }
  }
}

async function syncSubscribedLiveStates(exceptSessionId?: string): Promise<void> {
  const sessions = Array.from(state.subscribedSessions).filter((sid) => sid && sid !== exceptSessionId);
  await Promise.all(sessions.map(async (sid) => {
    try {
      const st = await backendApi.sessionStatus(sid);
      syncSessionLiveFromBackend(sid, st?.live, st?.last_status, st?.active_request_id);
    } catch {
      /* 单个会话状态同步失败不影响其它会话恢复 */
    }
  }));
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
  state.socket = new BackendChatSocket(
    (chunk) => applyChunk(chunk),
    (open, meta) => {
      logStream('ws-renderer', open ? 'socket-open' : 'socket-close', {
        subscribed: Array.from(state.subscribedSessions),
        activeSessionId: state.activeSessionId,
        transient: meta?.transient ?? false,
      });
      state.backendConnected = meta?.transient && !open ? state.backendConnected : open;
      updateGatewayDot();
      if (!open) {
        // 主进程换 socket（close reason=reconnect）时紧接 open，不应结算 streaming / 封回合。
        if (meta?.transient) {
          renderWorkspaceHistory(openSession);
          return;
        }
        // 真断连：仅冻结订阅/busy 会话的 streaming，避免全局误伤。
        for (const sid of sessionsAffectedByDisconnect()) {
          finalizeStreamingTurn(sid);
          setBusyWithUi(sid, false);
          patchBook(sid, { turnSealed: true, acceptingNewRequest: false });
        }
      }
      renderWorkspaceHistory(openSession);
    },
    () => {
      subscribeSessions(Array.from(state.subscribedSessions));
      const active = state.activeSessionId;
      void syncSubscribedLiveStates(active ?? undefined).then(() => renderWorkspaceHistory(openSession));
      if (active && !shouldSkipHistoryReloadOnReconnect(active)) {
        void loadBackendHistory(active).then(() => {
          renderChat();
          renderWorkspaceHistory(openSession);
        });
      } else if (active) {
        logStream('history', 'skip-on-open-live-turn', { sessionId: active });
        void backendApi.sessionStatus(active).then((st) => {
          syncSessionLiveFromBackend(active, st?.live, st?.last_status, st?.active_request_id);
          renderChat();
          renderWorkspaceHistory(openSession);
        }).catch(() => {
          renderChat();
        });
      }
    },
  );
  state.socket.bindLastGatewaySequences(getLastGatewaySequences);
  state.socket.connect();
  startStreamWatchdog();
}

export async function hydrateBackendState(): Promise<void> {
  if (!isRendererLoggedIn()) return;
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
