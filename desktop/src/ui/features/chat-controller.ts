/**
 * Chat 流式处理 + 渲染入口（从 ui/index.ts 抽出，X2）。
 *
 * 这里集中了「会话消息流」相关的全部逻辑：流式分片 dispatch、回合结算、
 * 消息发送/撤回/编辑，以及与之强耦合的 DOM 渲染（renderChat /
 * patchStreamingTurn）。
 *
 * 与 index.ts 的解耦：本模块不 import index.ts（避免循环）。需要回调
 * openSession / renderWorkspaceHistory 的位置，通过模块级 registry（setCallbacks）
 * 由 index.ts 在 init 时注入——等价于 index.ts 内部函数互相调用的旧行为。
 *
 * 本文件所有函数均为「原样搬迁」，行为与抽离前的 index.ts 完全一致。
 */

import { getLastGatewaySequences, isDuplicateGatewayChunk, noteGatewaySequence, touchStreamActivity } from './gateway-sequence';
import { noteDelta, resetAssistant, resetSession } from '../stream-reassembly';
import {
  type ChatMessage,
  type MessageRole,
  type SessionStatus,
  type ToolCallInfo,
  renderEmptyState,
  renderTodoProgressPanelHtml,
  renderWorkEmptyState,
  resolveLiveFoldLabel,
  resolveTeamTurnFoldLabel,
  resolveTurnDurationMs,
  shouldShowTodoPanel,
  turnHasProcessContent,
} from '../chat-render';
import { patchTranscriptMarkdown } from '../components/transcript';
import { recordTurn } from './usage-tracker';
import { onAfterFinal } from './cron-page';
import {
  getConversationScrollAnchor,
  renderConversation,
} from './conversation-renderer';
import { queryPrimaryComposer } from './composer-scope';
import {
  commitDraftSession,
  composerWorkspaceId,
  createSessionInWorkspace,
  getSessionAgentDisplay,
  isDraftSession,
  refreshAllSessions,
  renderWorkspaceHistory,
  workspaceForSessionDispatch,
  type OpenSessionFn,
} from './workspaces';
import { syncComposerWorkspaceLabel } from './composer-toolbar';
import { refreshKanbanBoard, scheduleRefreshKanbanBoard } from './kanban-board';
import { primeTeamCollaborationIdentity } from './team-collaboration-board';
import { syncCraftLabel, syncComposerModelLabel } from './composer-toolbar';
import { syncModelUi } from './model-picker';
import { resetToAgentMode } from './session-mode';
import { isExternalTeamSession, persistDraftSessionModel, sessionDisplayModelLabel } from './session-model';
import { renderSystemOverview } from './system-page';
import { isInspectorOpen, getInspectorActiveTab, openBrowserWorkbench, openInspectorToTab, refreshInspector, refreshInspectorChrome, invalidateFileDiffCachePaths, setUsageSnapshot, resetPlanBoardDraft } from './inspector';
import { clearSiteAnnotationDraft, composeSiteAnnotationMessage, hasSiteAnnotationDraft } from './sites-page';
import {
  clearBlueprintAnnotationDraft,
  composeBlueprintAnnotationMessage,
  handleBlueprintSurfaceToolChunk,
  hasBlueprintAnnotationDraft,
} from './blueprint-surface';
import { shouldAutoOpenBrowserWorkbench } from './browser-auto-open';
import { syncRunningIntroSlot } from './running-intro';
import {
  applyBusyUi,
  discardEmptyOptimisticAssistant,
  newTurnRequestId,
  openTurnForRequest,
  resumeSessionGeneration,
} from './session-busy';
import { clearScenarioChip, takeArmedSubScenario } from './scenario-arm';
import { requireRendererLogin } from './auth-gate';
import {
  formatFollowupAnswerMessage,
  isRuntimeStaffingFollowup,
} from '../followup';
import type { FollowupAnswer } from '../backend-client';
import type { ChatChunk, WikiIngestProgress } from '../backend-client';
import { makeSessionTitle, mergeTeamInternalMessage, normalizeTurnFileChanges } from './history-mapping';
import { applyFoldState, createChatRenderCoalescer, createStreamingPatchCoalescer } from '../render-utils';
import { getToolFold, setToolFold } from './fold-state';
import { renderSecurityBanner } from './security-banner';
import {
  chunkRequestId,
  isPlanControlStatus,
  normalizeChunk,
  reduceChunk,
  resolveBusyTransition,
  resolveTurnGate,
  USER_WAIT_CHUNK_KINDS,
  type TeamInternalChunk,
  type UsagePayload,
} from '../reducers/chat-reducer';
import {
  $,
  addSubscribedSessions,
  addSuppressedSession,
  appendSessionMessage,
  clearAttachments,
  enqueuePending,
  ensureSessionBook,
  ensureSessionMessages,
  getPendingQueue,
  isBusySession,
  isDynamicKanbanSession,
  newMessageId,
  notify,
  promotePendingQueueItemAsRevision,
  removePendingQueueItem,
  removeSuppressedSession,
  replacePendingQueue,
  resetBook,
  patchBook,
  setEditFrom,
  setQueueHint,
  setSessionStatus,
  shiftPendingQueue,
  state,
  type Bookkeeping,
} from '../state';
import { takeAttachmentsForSend } from './attachments';
import { renderAttachmentPreview } from './attachments';
import { messageStore, sessionStore } from '../stores/stores';
import type { TabKey } from '../state';
import { resolveChatRenderTargetId, openStudioChatPanel, isStudioView } from './studio-chrome-state';
import { isStreamDebugEnabled, logStream } from '../stream-debug';
import { setDisabledWorkPreferenceIdsForTurn, takeDisabledWorkPreferenceIds } from './composer-mention';
import { productModeStore } from '../stores/product-mode-store';

// ---------- registry: 由 index.ts 在 init 时注入的回调（破循环） ----------

let openSessionFn: OpenSessionFn = async () => {};
let setTabFn: (tab: TabKey) => void = () => {};
let queueEditDraft: { sessionId: string } | null = null;
let teamIdentityRefreshBound = false;
const ttftVisibleLoggedRequests = new Set<string>();
const ttftRenderLoggedRequests = new Set<string>();
const ttftRequestStartedAt = new Map<string, number>();

interface DispatchOptions {
  subScenario?: string;
  planActive?: boolean;
  clientIntent?: 'revision';
  optimisticUserMessageId?: string;
  wikiConfirmationId?: string;
  workDisabledPreferenceIds?: string[];
}
/**
 * index.ts init 时调用，把 openSession / setTab 等顶层入口注入本模块。
 * 语义等价于抽离前这些函数作为 index.ts 顶层函数互相直接调用。
 */
export function setChatCallbacks(opts: { openSession: OpenSessionFn; setTab: (tab: TabKey) => void }): void {
  openSessionFn = opts.openSession;
  setTabFn = opts.setTab;
  if (!teamIdentityRefreshBound && typeof window !== 'undefined') {
    teamIdentityRefreshBound = true;
    window.addEventListener('team-collaboration:updated', ((event: Event) => {
      const sessionId = (event as CustomEvent<{ sessionId?: string }>).detail?.sessionId;
      if (sessionId && sessionId === state.activeSessionId) renderChat();
    }) as EventListener);
  }
}

/** Open a session through the current renderer owner, then switch to chat. */
export async function openSessionInChat(sessionId: string): Promise<void> {
  if (!sessionId) return;
  await openSessionFn(sessionId);
  setTabFn('chat');
}

// ---------- Wiki ingest 进度帧转发（Phase 2） ----------
// wiki_ingest_progress 是 /api/wiki/ingest 推到某个会话的带外进度帧，不属于对话回合，
// 不进 reducer；经回调转发给订阅者（wiki-page 由 index.ts 组合根注入，chat 侧不 import wiki-page）。
let wikiIngestProgressCallback: ((progress: WikiIngestProgress) => void) | null = null;

export function setWikiIngestProgressCallback(cb: ((progress: WikiIngestProgress) => void) | null): void {
  wikiIngestProgressCallback = cb;
}

/** 规范化 wiki_ingest_progress body（对齐 web useChat.ts 的字段裁剪）并转发给订阅者。 */
function forwardWikiIngestProgress(chunk: ChatChunk, sid: string): void {
  if (!wikiIngestProgressCallback) return;
  const body = (chunk.body ?? {}) as Record<string, unknown>;
  const progress: WikiIngestProgress = {
    stage: String(body.stage ?? ''),
    percent: Math.max(0, Math.min(100, Number(body.percent ?? 0) || 0)),
    label: String(body.label ?? body.stage ?? ''),
    source_id: String(body.source_id ?? ''),
    session_id: sid,
  };
  if (typeof body.error === 'string') progress.error = body.error;
  if (body.detail && typeof body.detail === 'object') {
    progress.detail = body.detail as Record<string, unknown>;
  }
  wikiIngestProgressCallback(progress);
}

// ---------- 专用 Wiki Agent 发送参数 ----------
// Wiki Agent 消息需带 wiki_kb_id。
// 会话状态由 features/wiki-agent.ts 维护，经此注册口注入 resolver；chat 侧不 import wiki-agent
// （与 setWikiIngestProgressCallback 同一解耦模式）。
export interface WikiSendExtras {
  wikiKbId: string;
}
let wikiSendExtrasResolver: ((sessionId: string) => WikiSendExtras | null) | null = null;

export function setWikiSendExtrasResolver(fn: ((sessionId: string) => WikiSendExtras | null) | null): void {
  wikiSendExtrasResolver = fn;
}

// ---------- 小读取包装（原 index.ts 顶层） ----------

export function getMessages(sessionId: string): ChatMessage[] {
  return ensureSessionMessages(sessionId);
}

export function bookFor(sessionId: string): Bookkeeping {
  return ensureSessionBook(sessionId);
}

export function isBusy(sessionId: string): boolean {
  return isBusySession(sessionId);
}

async function ensurePlanModeForSession(sessionId: string, planActive: boolean): Promise<void> {
  if (!planActive) return;
  const book = bookFor(sessionId);
  if (book.planActive) return;
  const sent = await state.socket?.planEnter(sessionId);
  if (sent) {
    patchBook(sessionId, { planActive: true, pendingPlan: null });
  }
}

// ---------- 连接状态同步（composer 模型标签等） ----------

export function updateGatewayDot(): void {
  syncModelUi();
  syncComposerModelLabel();
  renderSystemOverview();
}

export function updateComposerControls(): void {
  // ComposerView subscribes to stores and owns the execution controls.
  renderSecurityBanner();
}

export function scrollChatToBottom(): void {
  // 旧行为：每个 delta / 每次 renderChat 都无条件 scrollTop = scrollHeight，
  // 用户在流式输出时无法上滑浏览历史——被强制拉回底部。
  // 新行为：通过 scroll-anchor 的 stickyBottom 模型——
  //   - 用户停在底部时才追底（pinToBottomIfSticky）；
  //   - 用户上滑后 stickyBottom disarmed，不追底；
  //   - 用户提交新消息 / 切会话时显式 jumpToBottom 重置 sticky。
  // 保留函数名 scrollChatToBottom 作为外部入口的兼容签名，内部改用 anchor 软钉。
  requestAnimationFrame(() => {
    const anchor = getConversationScrollAnchor(resolveChatRenderTargetId(isStudioView()));
    anchor.pinToBottomIfSticky();
  });
}

// ---------- scroll anchor：会话内 stickyBottom 管理 ----------
// 实例按容器 id 多实例化（conversation-renderer.getConversationScrollAnchor），
// 主对话 / 工作室 / Wiki 问答面板各自独立。

/** 用户提交新消息 / 切会话 / 空→非空 时调用：强制跳到底部并重置 sticky。 */
export function jumpChatToBottom(): void {
  requestAnimationFrame(() => {
    getConversationScrollAnchor(resolveChatRenderTargetId(isStudioView())).jumpToBottom();
  });
}

/** 用户主动点了「回到底部」按钮（未来 UI 扩展用）。当前 scroll-anchor 模型下，
 *  用户只要滚回底部 stickyBottom 会自动 re-arm，所以这个入口主要用于显式场景。 */
export function forceChatScrollToBottom(): void {
  jumpChatToBottom();
}

// ---------- busy / status / queue 的 UI 联动 ----------

export function setBusyWithUi(sessionId: string, val: boolean): void {
  applyBusyUi(sessionId, val);
  syncTurnDurationTicker();
}

export function setStatusWithUi(sessionId: string, status: SessionStatus): void {
  // 状态隔离：setSessionStatus 内部短路，仅在 status 真变化时写 store。
  // 行 UI（status class + spinner）由 workspaces.ts 的 sessionStatuses 订阅统一局部 patch，
  // 不再走整树 renderWorkspaceHistory，避免流式期间 spinner 元素被重建导致动画抽搐。
  setSessionStatus(sessionId, status);
}

export function setQueueHintWithUi(sessionId: string, hint: string): void {
  setQueueHint(sessionId, hint);
}

// ---------- fold / 文件卡事件委托 ----------
// 已随渲染主体迁入 conversation-renderer.ts（ensureFoldDelegation / ensureFileChangesDelegation），
// 主对话经 renderConversation 间接绑定；wiki-agent 直接复用 conversation-renderer 的版本。

function collapseLatestAgentProcess(sessionId: string): void {
  const messages = getMessages(sessionId);
  let end = messages.length - 1;
  while (end >= 0) {
    const msg = messages[end];
    const role = msg?.role;
    if (role === 'assistant' || (role === 'status' && !msg?.workflowProgress) || role === 'error') break;
    end -= 1;
  }
  if (end < 0) return;
  let start = end;
  while (start > 0) {
    const msg = messages[start - 1];
    const role = msg?.role;
    if (role === 'assistant' || (role === 'status' && !msg?.workflowProgress) || role === 'error') start -= 1;
    else break;
  }
  const turnId = messages[start]?.id;
  if (!turnId) return;
  // plan_review 是服务端推送的系统事件，折叠上一回合属于"自动折叠"，只写本次运行的
  // 内存态（applyFoldState），不持久化到 localStorage——否则用户从未手动折叠的回合
  // 重启后会被还原成折叠。手动折叠仍走 ensureFoldDelegation 的 toggle → setTurnFold。
  applyFoldState(turnId, false, {
    unfolded: state.userUnfoldedTurns,
    folded: state.userFoldedTurns,
  });
}

/** 流式渲染合并：同一帧内多次 schedule 只触发一次 renderChat，
 *  把 delta 30/s 的全量 innerHTML 重绘降到每帧 ≤1 次（P2-3）。 */
const scheduleChatRender = createChatRenderCoalescer(
  () => renderChat(),
  (cb) => requestAnimationFrame(cb),
);

const streamingPatchCoalescer = createStreamingPatchCoalescer(
  ({ sid, assistantId }) => { void patchStreamingTurn(sid, assistantId); },
  (cb) => requestAnimationFrame(cb),
);

/** 执行中回合：每秒刷新一次「已等待」标签（不依赖 WS 分片到达频率）。 */
let turnDurationTicker: number | null = null;

function sessionNeedsLiveTurnTimer(sessionId: string): boolean {
  if (!sessionId) return false;
  if (isBusy(sessionId)) return true;
  return getMessages(sessionId).some(
    (message) => (message.role === 'assistant' || message.role === 'team_internal') && message.streaming,
  );
}

function stopTurnDurationTicker(): void {
  if (turnDurationTicker != null) {
    window.clearInterval(turnDurationTicker);
    turnDurationTicker = null;
  }
}

function syncTurnDurationTicker(): void {
  const sid = state.activeSessionId;
  if (sid && sessionNeedsLiveTurnTimer(sid)) {
    if (turnDurationTicker == null) {
      turnDurationTicker = window.setInterval(() => {
        const active = state.activeSessionId;
        if (!active || !sessionNeedsLiveTurnTimer(active)) {
          stopTurnDurationTicker();
          return;
        }
        // 保留单 Agent/专家团原 patch 路径；Team 仅在原路径无活跃
        // assistant 时追加旁路适配，共用同一个 ticker，不新建定时器。
        if (!patchActiveStreamingTurnLabel(active)) patchActiveTeamTurnLabel(active);
      }, 1000);
    }
  } else {
    stopTurnDurationTicker();
  }
}

/** 单测用：重置计时器状态。 */
export function _resetTurnDurationTickerForTests(): void {
  stopTurnDurationTicker();
}

/** 单测用：重置待发队列编辑草稿，避免模块级状态跨用例残留。 */
export function _resetQueueEditDraftForTests(): void {
  queueEditDraft = null;
}

export function renderTodoSlot(): void {
  const slot = queryPrimaryComposer('.chat-todo-slot');
  if (!slot) return;
  const sessionId = state.activeSessionId;
  const todos = sessionId ? bookFor(sessionId).todos : [];
  if (!shouldShowTodoPanel(todos)) {
    slot.innerHTML = '';
    return;
  }
  // 默认折叠：输入框上方只露出当前步骤 + x/x，点开再看全表。
  const open = getToolFold('todo:current') ?? false;
  slot.innerHTML = renderTodoProgressPanelHtml(todos, open, 'todo:current');
  const btn = slot.querySelector<HTMLElement>('[data-todo-panel-toggle]');
  if (btn) {
    btn.onclick = () => {
      const key = btn.getAttribute('data-todo-panel-key');
      if (!key) return;
      const isOpen = btn.getAttribute('aria-expanded') === 'true';
      setToolFold(key, !isOpen);
      renderTodoSlot();
    };
  }
}

// ---------- patchStreamingTurn（流式增量） ----------

/**
 * 流式正文增量 patch（P2-3）：只更新当前 streaming 回合的正文 + 计时 label，
 * 不重建整棵消息树。修两个问题：
 *  1. 长会话下每帧全量 innerHTML 重绘 → 卡顿；
 *  2. 每帧重建会销毁用户正在交互的 <details>，导致手动展开/折叠状态丢失。
 * 找不到目标（首片未渲染 / 切换会话 / 结构变化）→ 返回 null，调用方回退全量 render。
 */
function resolveStreamingTurnTarget(sid: string, assistantId: string): {
  turnEl: HTMLElement;
  messages: ChatMessage[];
  msg: ChatMessage;
} | null {
  if (sid !== state.activeSessionId) return null;
  const containerId = resolveChatRenderTargetId(isStudioView());
  const root = document.getElementById(containerId);
  const turnEl = root?.querySelector<HTMLElement>('.msg[data-streaming="true"]') ?? null;
  if (!turnEl) return null;
  const messages = getMessages(sid);
  const msg = messages.find((m) => m.id === assistantId);
  if (!msg) return null;
  return { turnEl, messages, msg };
}

function scheduleStreamingTurnPatch(sid: string, assistantId: string): boolean {
  if (!resolveStreamingTurnTarget(sid, assistantId)) return false;
  streamingPatchCoalescer.schedule({ sid, assistantId });
  return true;
}

function scheduleThinkingTurnPatch(sid: string, assistantId: string): boolean {
  const target = resolveStreamingTurnTarget(sid, assistantId);
  if (!target?.msg.thinking) return false;
  if (!target.turnEl.querySelector(`[data-thinking-for="${assistantId}"]`)) return false;
  streamingPatchCoalescer.schedule({ sid, assistantId });
  return true;
}

function patchStreamingTurnLabel(sid: string, assistantId: string): boolean {
  const target = resolveStreamingTurnTarget(sid, assistantId);
  if (!target) return false;
  const { turnEl, messages } = target;
  const idx = messages.findIndex((m) => m.id === assistantId);
  if (idx < 0) return false;

  let i = idx;
  while (i > 0) {
    const prev = messages[i - 1];
    if (prev.role === 'user' || (prev.role === 'status' && prev.agentName) || (prev.role === 'status' && prev.workflowProgress)) break;
    i -= 1;
  }
  let j = idx + 1;
  while (j < messages.length) {
    const next = messages[j];
    if (next.role === 'assistant' || next.role === 'error' || (next.role === 'status' && !next.agentName && !next.workflowProgress)) j += 1;
    else break;
  }
  const batch = messages.slice(i, j);
  if (!batch.some((m) => m.streaming === true)) return false;

  const labelEl = turnEl.querySelector<HTMLElement>('.msg__fold-label');
  if (!labelEl) return false;
  const durationMs = resolveTurnDurationMs(batch, { isLive: true });
  labelEl.textContent = resolveLiveFoldLabel(durationMs, turnHasProcessContent(batch));
  return true;
}

function patchActiveStreamingTurnLabel(sid: string): boolean {
  const assistant = [...getMessages(sid)].reverse().find(
    (message) => message.role === 'assistant' && message.streaming,
  );
  return assistant ? patchStreamingTurnLabel(sid, assistant.id) : false;
}

/** Team Turn 对现有计时器的薄适配；不参与普通 assistant 回合。 */
function patchActiveTeamTurnLabel(sid: string): boolean {
  if (sid !== state.activeSessionId || !isExternalTeamSession(sid)) return false;
  const message = [...getMessages(sid)].reverse().find(
    (item) => item.role === 'team_internal' && item.streaming,
  );
  if (!message) return false;
  const containerId = resolveChatRenderTargetId(isStudioView());
  const root = document.getElementById(containerId);
  const turn = Array.from(root?.querySelectorAll<HTMLElement>('.team-internal[data-message-id]') || [])
    .find((element) => element.dataset.messageId === message.id);
  const label = turn?.querySelector<HTMLElement>('.msg__fold-label');
  if (!label) return false;
  const renderMessage: ChatMessage = { ...message, role: 'assistant' };
  const durationMs = resolveTurnDurationMs([renderMessage], { isLive: true });
  label.textContent = resolveTeamTurnFoldLabel(message, durationMs, true);
  return true;
}

function patchStreamingTurn(sid: string, assistantId: string): boolean {
  const target = resolveStreamingTurnTarget(sid, assistantId);
  if (!target) return false;
  const { turnEl, msg } = target;
  const book = bookFor(sid);
  const requestId = book.activeRequestId;
  if (requestId && msg.content && !ttftRenderLoggedRequests.has(requestId)) {
    ttftRenderLoggedRequests.add(requestId);
    logStream('render', 'ttft-first-dom-patch', {
      sid,
      request_id: requestId,
      assistantId,
      contentLen: msg.content.length,
      elapsedMs: msg.turnStartedAt != null ? Math.max(0, Date.now() - msg.turnStartedAt) : undefined,
    });
  }
  const textEl = turnEl.querySelector<HTMLElement>(`[data-text-for="${assistantId}"]`);
  if (textEl) {
    textEl.classList.remove('typing-inline');
    patchTranscriptMarkdown(textEl, msg.content, true);
  }
  const thinkingEl = turnEl.querySelector<HTMLElement>(`[data-thinking-for="${assistantId}"] .process-timeline__thinking`);
  if (thinkingEl && msg.thinking != null) {
    thinkingEl.textContent = msg.thinking;
  }
  // 实时计时：以整回合 batch 为准（工具阶段可能无正文 data-text-for，但仍需刷新 label）。
  patchStreamingTurnLabel(sid, assistantId);
  scrollChatToBottom();
  return true;
}

// ---------- renderChat（渲染主体已抽离到 conversation-renderer.ts） ----------
// X3b keyed 增量 diff 的 unit-plan 构建 / sig / diff apply / 事件委托全部在
// conversation-renderer.renderConversation；这里只保留主对话的可见性逻辑、
// 会话解析与 main-only 渲染后副作用（todo 槽位 / composer / inspector 角标等）。

export function renderChat(): void {
  const welcomePanel = $('#welcome-panel');
  const chatPanel = $('#chat-panel');
  const isStudioMode = isStudioView();
  const containerId = resolveChatRenderTargetId(isStudioMode);
  const container = document.getElementById(containerId);
  if (!welcomePanel || !chatPanel || !container) return;
  const sessionId = state.activeSessionId;
  const allMessages = sessionId ? getMessages(sessionId) : [];
  const busy = sessionId ? isBusy(sessionId) : false;
  const isInspirationSession = sessionId
    ? String(getSessionAgentDisplay(sessionId)?.agentLabel?.provider || '').trim().toLowerCase() === 'sites'
    : false;
  const pendingFollowup = sessionId ? bookFor(sessionId).pendingFollowup : null;

  // 撤回修改中：隐藏 [editFromIdx..] 的内容（不删除），只渲染前面部分
  const editFrom = sessionId ? state.editFromIdx[sessionId] : undefined;
  const editing = editFrom != null;
  const messages = editing ? allMessages.slice(0, editFrom) : allMessages;

  const chatTab = chatPanel.closest('#chat-tab');
  const workOverviewActive = chatTab?.classList.contains('work-overview-active') ?? false;
  // 编辑态 / 等待用户交互 / todo 面板存在时，即使消息为空也保留对话流页面，不切欢迎页。
  const showChat = chatTab?.classList.contains('work-session-active')
    || isInspirationSession
    || messages.length > 0
    || busy
    || editing
    || Boolean(pendingFollowup);
  if (!isStudioMode) {
    welcomePanel.hidden = workOverviewActive || showChat;
    chatPanel.hidden = workOverviewActive || !showChat;
    document.body.classList.toggle(
      'welcome-active',
      state.activeTab === 'chat' && !workOverviewActive && !showChat,
    );
  } else {
    welcomePanel.hidden = true;
    chatPanel.hidden = true;
    document.body.classList.remove('welcome-active');
  }

  renderConversation(container, containerId, sessionId, {
    emptyState: () => {
      // Work 办公会话用专属空态，不复用通用助手的「开始一段对话 / Team 模式」文案。
      const isWorkSession = chatTab?.classList.contains('work-session-active') ?? false;
      return isWorkSession
        ? renderWorkEmptyState(chatTab?.classList.contains('work-item-active') ?? false)
        : renderEmptyState();
    },
    followupHandlers: {
      onSubmit: (questionId: string, answers: FollowupAnswer[]) => {
        const sid = state.activeSessionId;
        if (!sid) return;
        const pending = bookFor(sid).pendingFollowup;
        void state.socket?.send({ action: 'followup_answer', session_id: sid, question_id: questionId, answers });
        if (pending?.recordHistory !== false) {
          // Ordinary follow-ups become a new user turn.
          patchBook(sid, {
            pendingFollowup: null,
            assistantId: null,
            toolMap: new Map(),
            deltaSpans: [],
            legacyDeltaText: '',
          });
          const message = pending ? formatFollowupAnswerMessage(pending, answers) : null;
          if (message) appendMessage(sid, 'user', message);
        } else {
          // Side-channel decisions preserve the current assistant turn. Runtime
          // staffing keeps the same card long enough to show its backend-confirmed
          // applying/applied state; permission prompts retain their old close-now behavior.
          patchBook(sid, {
            pendingFollowup: pending && isRuntimeStaffingFollowup(pending)
              ? {
                  ...pending,
                  status: 'applying',
                  note: '正在邀请协作助手加入……',
                }
              : null,
          });
        }
        resumeSessionGeneration(sid);
        renderChat();
      },
      onCancel: (questionId: string) => {
        const sid = state.activeSessionId;
        if (!sid) return;
        void state.socket?.send({ action: 'followup_cancel', session_id: sid, question_id: questionId });
        patchBook(sid, { pendingFollowup: null });
        renderChat();
      },
    },
    afterRender: (renderedSessionId) => {
      renderTodoSlot();
      updateComposerControls();
      syncRunningIntroSlot();
      // 流式每帧只刷 tab 角标，不重建 inspector body（否则计划 MD / 文件 diff 滚动会被 innerHTML 冲回顶部）。
      if (isInspectorOpen()) refreshInspectorChrome();
      window.dispatchEvent(new CustomEvent('messages:changed', { detail: { sessionId: renderedSessionId } }));
      syncTurnDurationTicker();
    },
  });
}

// ---------- P2-2 迟到分片排队 ----------

// P2-2: loadBackendHistory 期间到达的分片排队，history 写回后再 flush，
// 避免「全量替换 state.messages[sid]」覆盖掉迟到的流式分片。
const pendingChunks: Record<string, ChatChunk[]> = {};
const historyLoading = new Set<string>();

// 每个会话最近一次已自动展开浏览器面板的 request id（一轮任务只自动展开一次）。
const browserAutoOpenedRequests = new Map<string, string>();

export function isHistoryLoading(sessionId: string): boolean {
  return historyLoading.has(sessionId);
}

export function enqueuePendingChunk(sessionId: string, chunk: ChatChunk): void {
  (pendingChunks[sessionId] ??= []).push(chunk);
  logStream('history', 'chunk-queued-during-history-load', {
    sessionId,
    kind: chunk.kind,
    request_id: chunk.request_id,
    queueLen: pendingChunks[sessionId]?.length ?? 0,
  });
}

export function flushPendingChunks(sessionId: string): ChatChunk[] | null {
  const queued = pendingChunks[sessionId];
  delete pendingChunks[sessionId];
  if (queued && queued.length > 0) {
    logStream('history', 'flush-pending-chunks', { sessionId, count: queued.length, kinds: queued.map((c) => c.kind) });
  }
  return queued && queued.length > 0 ? queued : null;
}

export function setHistoryLoading(sessionId: string, loading: boolean): void {
  if (loading) historyLoading.add(sessionId);
  else historyLoading.delete(sessionId);
  logStream('history', loading ? 'history-loading-start' : 'history-loading-end', { sessionId });
}

// ---------- append / patch / preview / finalize ----------

export function appendMessage(sessionId: string, role: MessageRole, content: string, extra?: Partial<ChatMessage>): ChatMessage {
  // 默认记下当前模型，用于消息上显示「模型 · 时间」。extra 可覆盖。
  const message: ChatMessage = { id: newMessageId(role), role, content, timestamp: Date.now(), model: sessionDisplayModelLabel(sessionId), ...extra };
  // Immutable update via the shared mutator. Previously this did
  // getMessages(sessionId).push(message), which mutated the live store array
  // WITHOUT messageStore.set — so subscribers never fired and a concurrent
  // immutable update (e.g. patchMessage) could clobber the appended message.
  // appendSessionMessage reads the current list, builds a new array, and
  // publishes via messageStore.set, mirroring patchMessage.
  appendSessionMessage(sessionId, message);
  return message;
}

export function patchMessage(sessionId: string, messageId: string, patch: Partial<ChatMessage>): void {
  const messages = getMessages(sessionId);
  const index = messages.findIndex((m) => m.id === messageId);
  if (index < 0) return;
  const updated = messages.map((m, i) => (i === index ? { ...m, ...patch } : m));
  messageStore.set({ messages: { ...messageStore.get().messages, [sessionId]: updated } });
}

export function patchPlanReviewMessages(
  sessionId: string,
  status: NonNullable<ChatMessage['planReview']>['status'],
  plan?: string,
): void {
  const messages = getMessages(sessionId);
  if (!messages.some((m) => m.planReview)) return;
  const updated = messages.map((m) =>
    m.planReview
      ? {
          ...m,
          planReview: {
            ...m.planReview,
            status,
            // 批准手改后同步最新正文，避免对话态残留编辑前版本。
            ...(typeof plan === 'string' ? { plan } : {}),
          },
        }
      : m,
  );
  messageStore.set({ messages: { ...messageStore.get().messages, [sessionId]: updated } });
}

export function updateSessionPreview(sessionId: string, text: string): void {
  const session = state.sessions.find((s) => s.id === sessionId);
  if (!session) return;
  // ponytail: 只更新预览/时间，不动 title。标题归摘要帧 applySessionTitle
  //（titleFromSummary=true）或用户 rename；预览才是每条消息更新的对象。
  session.preview = text.slice(0, 48);
  session.updatedAt = Date.now();
}

export function applySessionTitle(sessionId: string, title: string): void {
  const trimmed = title.trim();
  if (!trimmed) return;
  const session = state.sessions.find((s) => s.id === sessionId);
  if (session) {
    session.title = trimmed;
    // 标记标题来自后端摘要，syncSessionsFromBackend 据此保留前端标题，
    // 避免被后端列表里残留的截断用户原话覆盖。
    session.titleFromSummary = true;
  }
  renderWorkspaceHistory(openSessionFn);
}

export function finalizeTurn(sessionId: string): void {
  const book = bookFor(sessionId);
  const finalizedAssistantId = book.assistantId;
  if (finalizedAssistantId) patchMessage(sessionId, finalizedAssistantId, { streaming: false });
  // 修法3：回合封口 → 清该回合的 delta 重组缓冲（assistantId 每回合唯一，不影响后续回合；防泄漏）
  if (finalizedAssistantId) resetAssistant(sessionId, finalizedAssistantId);
  // 不可变更新 book：finalize 后清空本轮记账，触发 sessionStore 订阅
  patchBook(sessionId, {
    toolMap: new Map(),
    assistantId: null,
    firstChunkAt: null,
    hadTeamInternal: false,
    deltaSpans: [],
    legacyDeltaText: '',
    turnSealed: true,
    acceptingNewRequest: false,
  });
  setBusyWithUi(sessionId, false);
  setQueueHintWithUi(sessionId, '');
  setStatusWithUi(sessionId, 'idle');
  void refreshSessions();
  window.setTimeout(() => {
    void refreshSessions();
  }, 2000);
  onAfterFinal();
  consumePending(sessionId);
  syncTurnDurationTicker();
}

/**
 * 把当前回合的「真实」输入/输出字符数 + 用时/首字延迟 写到 usage-tracker。
 *
 * 输入字符数：本回合所有 user/tool 消息的 content 长度之和（不包含上一回合 assistant 内容）。
 * 输出字符数：本回合 assistant 消息 content + thinking + 工具调用 args/results 长度之和。
 */
function recordUsageTurn(
  sessionId: string,
  assistantId: string | null,
  durationMs: number,
  firstTokenMs: number | undefined,
  status: number,
  usage?: UsagePayload,
): void {
  try {
    const msgs = getMessages(sessionId);
    if (msgs.length === 0) return;

    const findLastAssistantIndex = (): number => {
      for (let i = msgs.length - 1; i >= 0; i -= 1) {
        if (msgs[i].role === 'assistant') return i;
      }
      return -1;
    };

    // 找到本回合 assistant 消息的下标（默认取最后一条 assistant 消息）
    const assistantIdx = assistantId
      ? msgs.findIndex((m) => m.id === assistantId)
      : findLastAssistantIndex();
    if (assistantIdx < 0) return;

    // 拼接"完整上下文窗口"作为本回合 input —— 这是 LLM 真正接收的输入。
    // 包含：本回合及其之前的所有 user / assistant / tool / system 消息
    //       （system prompt、skill 注入、之前所有对话、工具结果都会在每次调用时重发）。
    // 后端 /api/usage 的 total_tokens 也是按这个口径累加的（参考
    // crew/state/session_store.py::_estimate_tokens），这样前后端的数字能对齐。
    const inputParts: string[] = [];
    for (let i = 0; i <= assistantIdx; i++) {
      const m = msgs[i];
      if (m.role === 'assistant') continue; // assistant 输出由 outputText 单独算
      const parts: string[] = [];
      if (m.content) parts.push(m.content);
      if (m.thinking) parts.push(`[thinking] ${m.thinking}`);
      for (const tc of m.toolCalls ?? []) {
        if (tc.args) parts.push(`[tool_call:${tc.name}] ${tc.args}`);
        if (tc.result) parts.push(`[tool_result] ${tc.result}`);
      }
      if (parts.length > 0) inputParts.push(parts.join('\n'));
    }
    const inputText = inputParts.join('\n\n');

    // 输出：本回合 assistant 的 content + thinking + 工具调用 args/results
    const assistant = msgs[assistantIdx];
    const outputParts: string[] = [];
    if (assistant.content) outputParts.push(assistant.content);
    if (assistant.thinking) outputParts.push(assistant.thinking);
    for (const tc of assistant.toolCalls ?? []) {
      if (tc.args) outputParts.push(tc.args);
      if (tc.result) outputParts.push(tc.result);
    }
    const outputText = outputParts.join('\n');

    recordTurn({
      sessionId,
      inputText,
      outputText,
      durationMs: Math.max(0, durationMs),
      firstTokenMs: firstTokenMs != null && firstTokenMs >= 0 ? firstTokenMs : undefined,
      status,
      source: 'session_log',
      ...(usage != null ? { usage } : {}),
    });
  } catch (err) {
    // 跟踪写入失败不应影响主流程
    console.warn('[usage] recordTurn failed:', (err as Error).message);
  }
}

// ---------- applyChunk（dispatch + apply + 副作用） ----------

function normalizeTeamText(value: unknown): string {
  if (value == null) return '';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (typeof value === 'object') {
    const record = value as Record<string, unknown>;
    for (const key of ['message', 'text', 'content', 'summary']) {
      if (typeof record[key] === 'string' && String(record[key]).trim()) return String(record[key]);
    }
    try { return JSON.stringify(value); } catch { return ''; }
  }
  return String(value);
}

function backendSecondsToMs(value: unknown, fallback = Date.now()): number {
  const number = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(number) || number <= 0) return fallback;
  return number < 1_000_000_000_000 ? number * 1000 : number;
}

function normalizeTeamToolCalls(raw: unknown): ToolCallInfo[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const calls = raw.map((item, index): ToolCallInfo => {
    const value = item && typeof item === 'object' ? item as Record<string, unknown> : {};
    const status = value.status === 'running' || value.status === 'generating' || value.status === 'error'
      ? value.status
      : 'done';
    const args = typeof value.arguments === 'string'
      ? value.arguments
      : JSON.stringify(value.arguments || {});
    return {
      toolCallId: String(value.id || value.tool_call_id || `team_tool_${index}`),
      name: String(value.name || 'unknown'),
      ...(typeof value.ui_label === 'string' ? { uiLabel: value.ui_label } : {}),
      args,
      ...(typeof value.result === 'string' ? { result: value.result } : {}),
      status,
      startedAt: backendSecondsToMs(value.started_at, 0),
      ...(typeof value.duration === 'number' ? { duration: value.duration * 1000 } : {}),
    };
  });
  return calls.length ? calls : undefined;
}

function applyTeamInternalChunk(sessionId: string, chunk: TeamInternalChunk): void {
  const body = chunk.body;
  const book = bookFor(sessionId);
  const assistantId = book.assistantId;
  if (!book.hadTeamInternal && assistantId) {
    // Desktop 在发送时会预建一个空 assistant；Web 不会把它当 Team 发言。
    // 第一条成员消息到达即移除空占位，避免它固定在 DAG/派活消息之前。
    const messages = getMessages(sessionId);
    const assistant = messages.find((message) => message.id === assistantId);
    const isEmptyOptimistic = assistant?.role === 'assistant'
      && !assistant.content.trim()
      && !assistant.thinking?.trim()
      && !assistant.toolCalls?.length;
    if (isEmptyOptimistic) {
      messageStore.set({
        messages: {
          ...messageStore.get().messages,
          [sessionId]: messages.filter((message) => message.id !== assistantId),
        },
      });
      patchBook(sessionId, { assistantId: null, firstChunkAt: null });
    }
  }
  patchBook(sessionId, { hadTeamInternal: true });
  const timestamp = backendSecondsToMs(body.timestamp);
  const toolCalls = normalizeTeamToolCalls(body.tool_calls);
  const turnFileChanges = normalizeTurnFileChanges(body.turn_file_changes);
  const streaming = body.display_mode === 'stream' || body.event_type === 'team_stream';
  const incoming: ChatMessage = {
    id: newMessageId('team'),
    role: 'team_internal',
    content: typeof body.text === 'string' ? body.text : '',
    timestamp,
    segmentRole: 'answer',
    streaming,
    ...(typeof body.source_session_id === 'string' ? { sourceSessionId: body.source_session_id } : {}),
    ...(typeof body.agent_id === 'string' ? { agentId: body.agent_id } : {}),
    ...(typeof body.agent_name === 'string' ? { agentName: body.agent_name } : {}),
    ...(typeof body.agent_role === 'string' ? { agentRole: body.agent_role } : {}),
    ...(typeof body.agent_tone === 'number' ? { agentTone: body.agent_tone } : {}),
    ...(typeof body.is_leader === 'boolean' ? { isLeader: body.is_leader } : {}),
    ...(typeof body.event_type === 'string' ? { eventType: body.event_type } : {}),
    ...(typeof body.node_id === 'string' ? { nodeId: body.node_id } : {}),
    ...(typeof body.mention_from === 'string' ? { mentionFrom: body.mention_from } : {}),
    ...(Array.isArray(body.mention_to) ? { mentionTo: body.mention_to.map(String) } : {}),
    ...(typeof body.mention_intent === 'string' ? { mentionIntent: body.mention_intent } : {}),
    ...(typeof body.display_mode === 'string' ? { displayMode: body.display_mode } : {}),
    ...(typeof body.collapsed_title === 'string' ? { collapsedTitle: body.collapsed_title } : {}),
    ...(typeof body.process_text === 'string' ? { processText: body.process_text } : {}),
    ...(Array.isArray(body.artifacts) ? { artifacts: body.artifacts } : {}),
    ...(turnFileChanges ? { turnFileChanges } : {}),
    ...(body.thinking != null ? { thinking: normalizeTeamText(body.thinking) } : {}),
    ...(toolCalls ? { toolCalls } : {}),
    ...(typeof body.turn_started_at === 'number'
      ? { turnStartedAt: backendSecondsToMs(body.turn_started_at) }
      : streaming ? { turnStartedAt: Date.now() } : {}),
    ...(typeof body.turn_duration === 'number' ? { turnDurationMs: Math.max(0, body.turn_duration * 1000) } : {}),
  };
  const merged = mergeTeamInternalMessage(getMessages(sessionId), incoming, { append: body.append === true });
  messageStore.set({ messages: { ...messageStore.get().messages, [sessionId]: merged } });
  setQueueHintWithUi(sessionId, '');
  setBusyWithUi(sessionId, true);
  setStatusWithUi(sessionId, 'running');
}

// T3：把 applyChunk 改造成 dispatch + apply + 副作用 的薄适配层。
// 状态迁移全部走 chat-reducer，避免在 index.ts 重复实现 7 个 kind 的迁移逻辑。
export function applyChunk(chunk: ChatChunk): void {
  const sid = chunk.session_id || state.activeSessionId || 'default';
  logStream('apply-chunk', 'recv', {
    sid,
    kind: chunk.kind,
    request_id: chunk.request_id,
    sequence: chunk.sequence,
    gateway_sequence: chunk.gateway_sequence,
    is_final: chunk.is_final,
    activeSessionId: state.activeSessionId,
  });
  // 撤回/中断后忽略该会话的迟到分片，避免重建已被删除的幽灵助手消息。
  if (state.suppressChunks.has(sid)) {
    logStream('apply-chunk', 'drop-suppressed', { sid, kind: chunk.kind });
    return;
  }
  if (chunk.kind === 'session_title') {
    const title = String((chunk.body as { title?: string })?.title || '').trim();
    if (title) applySessionTitle(sid, title);
    return;
  }
  if (chunk.kind === 'channel_session_updated') {
    const body = (chunk.body ?? {}) as { platform?: string };
    // 渠道会话开始/结束时都确保前端已订阅：开始订阅后才能收到实时 delta，
    // 结束订阅后也能通过 replay 补到可能错过的帧。
    subscribeSessions([sid]);
    void import('./channel-sessions').then(({ refreshChannelSessionsOnEvent }) =>
      refreshChannelSessionsOnEvent(body.platform, sid).then(() => renderWorkspaceHistory(openSessionFn)),
    );
    return;
  }
  if (chunk.kind === 'cron_session_created') {
    resetToAgentMode();
    syncCraftLabel();
    void refreshSessions().then(() => {
      subscribeSessions([sid]);
    });
    return;
  }
  if (chunk.kind === 'cron_session_updated') {
    void refreshSessions().then(() => {
      subscribeSessions([sid]);
      if (sid !== sessionStore.get().activeSessionId) {
        const nextUnread = new Set(sessionStore.get().unreadCompletedSessions);
        nextUnread.add(sid);
        sessionStore.set({ unreadCompletedSessions: nextUnread });
        renderWorkspaceHistory(openSessionFn);
      }
    });
    return;
  }
  if (chunk.kind === 'audit_updated') {
    void import('./security-center').then(({ activateSecurityPage }) => activateSecurityPage());
    return;
  }
  if (chunk.kind === 'work_event') {
    const body = (chunk.body ?? {}) as { entity?: string; action?: string; content?: string };
    if (body.entity === 'preference' && body.action === 'auto_enabled') {
      notify(`已自动启用工作偏好：${body.content || '新偏好'}`);
      void import('../stores/work-store').then(({ loadWorkPreferences }) => loadWorkPreferences());
    } else {
      void import('./work/notifications').then(({ handleWorkItemEvent }) => handleWorkItemEvent());
    }
    return;
  }
  if (chunk.kind === 'wiki_ingest_progress') {
    // Wiki 编译进度：带外帧，与对话回合无关；转发给订阅者后直接返回。
    // 与 session_title 等侧信道 kind 一样不参与 gateway_sequence 去重——
    // 进度更新按 source_id 幂等，重连 replay 的重复帧无副作用。
    forwardWikiIngestProgress(chunk, sid);
    return;
  }
  if (chunk.kind === 'wiki_changed') {
    const body = (chunk.body ?? {}) as { changes?: Array<Record<string, unknown>> };
    window.dispatchEvent(new CustomEvent('wiki:changed', {
      detail: { sessionId: sid, changes: body.changes ?? [] },
    }));
    return;
  }
  // history 正在加载：排队，等 loadBackendHistory 写回后统一 flush，防止被全量替换覆盖。
  // 必须在 sequence 登记之前排队；flush 会重新走 applyChunk，若提前登记，
  // 队列里的首帧会被误判为 replay 重复帧而永久丢失。
  if (historyLoading.has(sid)) {
    enqueuePendingChunk(sid, chunk);
    return;
  }
  if (isDuplicateGatewayChunk(sid, chunk)) {
    return;
  }
  noteGatewaySequence(sid, chunk);
  touchStreamActivity(sid);

  const parsed = normalizeChunk(chunk);
  if (!parsed) {
    logStream('apply-chunk', 'drop-unrecognized', { sid, kind: chunk.kind });
    return;
  }

  const requestIdForPerf = chunk.request_id || bookFor(sid).activeRequestId;
  if (
    requestIdForPerf
    && parsed.kind === 'delta'
    && typeof parsed.body.text === 'string'
    && parsed.body.text.length > 0
    && !ttftVisibleLoggedRequests.has(requestIdForPerf)
  ) {
    ttftVisibleLoggedRequests.add(requestIdForPerf);
    logStream('apply-chunk', 'ttft-first-visible-text', {
      sid,
      request_id: requestIdForPerf,
      textLen: parsed.body.text.length,
      elapsedMs: ttftRequestStartedAt.has(requestIdForPerf)
        ? Math.max(0, Date.now() - (ttftRequestStartedAt.get(requestIdForPerf) as number))
        : undefined,
    });
  }

  const book = bookFor(sid);
  const reqId = chunkRequestId(parsed, chunk.request_id);
  const gate = resolveTurnGate(parsed.kind, reqId, {
    turnSealed: book.turnSealed,
    activeRequestId: book.activeRequestId,
    acceptingNewRequest: book.acceptingNewRequest,
  });
  logStream('gate', gate.action === 'drop' ? 'drop' : 'accept', {
    sid,
    kind: parsed.kind,
    request_id: reqId,
    gateAction: gate.action,
    bindRequestId: 'bindRequestId' in gate ? gate.bindRequestId : undefined,
    turnSealed: book.turnSealed,
    activeRequestId: book.activeRequestId,
    acceptingNewRequest: book.acceptingNewRequest,
  });
  if (gate.action === 'drop') return;
  const bindRequestId = 'bindRequestId' in gate ? gate.bindRequestId : null;

  handleBlueprintSurfaceToolChunk(chunk, sid);

  if (parsed.kind === 'team_internal') {
    if (bindRequestId) patchBook(sid, { activeRequestId: bindRequestId, acceptingNewRequest: false });
    applyTeamInternalChunk(sid, parsed);
    renderChat();
    renderWorkspaceHistory(openSessionFn);
    syncTurnDurationTicker();
    // Team 流式帧沿用现有 sticky-bottom 软追随；用户上滑后不得被下一帧强制拉回底部。
    if (sid === state.activeSessionId) scrollChatToBottom();
    return;
  }

  const teamStatusMessage = parsed.kind === 'status' && typeof parsed.body.message === 'string'
    ? parsed.body.message
    : '';
  if (
    parsed.kind === 'status'
    && parsed.body.control !== true
    && !teamStatusMessage.includes('排队')
    && isExternalTeamSession(sid)
  ) {
    // Team 的 transport status 是 Session 级瞬时态，不进入普通 statusReducer，
    // 避免在 Leader 结论后开出空 assistant 气泡。需要被用户看到的
    // 节点过程由结构化 team_internal/process 事件进入 Agent Turn Timeline。
    if (bindRequestId) patchBook(sid, { activeRequestId: bindRequestId, acceptingNewRequest: false });
    setQueueHintWithUi(sid, teamStatusMessage);
    setBusyWithUi(sid, true);
    setStatusWithUi(sid, 'running');
    renderWorkspaceHistory(openSessionFn);
    syncTurnDurationTicker();
    return;
  }

  // Dynamic Kanban 看板：只在回合 gate 接收后刷新，避免旧 request 的迟到帧触发 UI 副作用。
  // 使用 per-session 专家团队状态判断，避免全局 state.mode 与后台会话不一致导致漏刷新。
  // kanban 事件（call_completed / board_changed）到达后立即刷新右侧阶段，不必等轮询。
  if (sid === state.activeSessionId && isDynamicKanbanSession(sid) && ['tool', 'status', 'final', 'error', 'kanban', 'workflow_progress'].includes(parsed.kind)) {
    void scheduleRefreshKanbanBoard();
  }

  // Agent 本轮首个 browser_use 动作：自动展开侧边浏览器面板。每个 request 至多一次，
  // 只展开不抢焦点；用户本轮手动关闭后同一 reqId 不会重复触发（见 browser-auto-open.ts）。
  const browserToolReqId = reqId ?? book.activeRequestId ?? null;
  if (
    browserToolReqId &&
    shouldAutoOpenBrowserWorkbench({
      kind: parsed.kind,
      toolName: parsed.kind === 'tool' ? parsed.body.name : undefined,
      sessionId: sid,
      activeSessionId: state.activeSessionId ?? '',
      requestId: browserToolReqId,
      lastOpenedRequestId: browserAutoOpenedRequests.get(sid),
    })
  ) {
    browserAutoOpenedRequests.set(sid, browserToolReqId);
    openBrowserWorkbench({ createTab: false });
  }

  const messages = getMessages(sid);
  const snapshot = {
    sessionId: sid,
    messages,
    book,
    currentStatus: state.sessionStatuses[sid] ?? 'idle',
    now: Date.now(),
    sequence: typeof chunk.sequence === 'number' ? chunk.sequence : 0,
  };
  const result = reduceChunk(parsed, snapshot);

  // 修法1：delta 按 gateway_sequence 重组，覆盖 reducer 「cur + text 按到达顺序拼接」的脆弱累积。
  // 后端在重连 replay 等场景会把旧低序号帧晚于更高序号帧投递，到达顺序 != 生成顺序 → 盲目追加会
  // 串位/丢字。这里把本片按 gateway_sequence（会话级单调序号，见 crew/gateway/connections.py）
  // 入缓冲，用「按 seq 升序拼接」的重组结果覆盖 upsert 的 content，与到达顺序彻底解耦。
  // 无 gateway_sequence 的旧帧无可靠排序键，回退到 reducer 的到达顺序拼接（向后兼容）。
  if (parsed.kind === 'delta') {
    const aid = result.replaceBook?.assistantId ?? null;
    const seq = typeof chunk.gateway_sequence === 'number' && chunk.gateway_sequence > 0 ? chunk.gateway_sequence : null;
    if (aid && seq !== null) {
      const text = typeof parsed.body.text === 'string' ? parsed.body.text : '';
      const reconstructed = noteDelta(sid, aid, seq, text);
      for (const u of result.messageUpserts) {
        if (u.op === 'append' && u.message) {
          u.message = { ...u.message, content: reconstructed };
        } else if (u.op === 'patch' && u.patch && 'content' in u.patch) {
          u.patch = { ...u.patch, content: reconstructed };
        }
      }
    }
  }

  // 状态 patch：book 整体替换
  if (result.replaceBook) {
    const nextBook = bindRequestId
      ? { ...result.replaceBook, activeRequestId: bindRequestId, acceptingNewRequest: false }
      : result.replaceBook;
    sessionStore.set({ books: { ...sessionStore.get().books, [sid]: nextBook } });
  } else if (bindRequestId) {
    patchBook(sid, { activeRequestId: bindRequestId, acceptingNewRequest: false });
  }
  // 消息 upsert：append / patch，全部走 store 不可变更新
  applyMessageUpserts(sid, result.messageUpserts);
  if (result.messageUpserts.length > 0) {
    window.dispatchEvent(new CustomEvent('messages:changed', { detail: { sessionId: sid } }));
  }

  if (isStreamDebugEnabled() && result.messageUpserts.length > 0) {
    const msgs = getMessages(sid);
    const lastAssistant = [...msgs].reverse().find((m) => m.role === 'assistant');
    logStream('apply-chunk', 'applied', {
      sid,
      kind: parsed.kind,
      upsertCount: result.messageUpserts.length,
      assistantContentLen: lastAssistant?.content?.length ?? 0,
      assistantStreaming: lastAssistant?.streaming ?? false,
      finalize: result.finalize,
      statusHint: result.statusHint,
    });
  }
  if (parsed.kind === 'status' && isPlanControlStatus(String(parsed.body.message ?? ''))) {
    setBusyWithUi(sid, false);
    setQueueHintWithUi(sid, '');
  }
  if (parsed.kind === 'plan_review') {
    collapseLatestAgentProcess(sid);
    setBusyWithUi(sid, false);
    setQueueHintWithUi(sid, '');
    const planText = typeof parsed.body.plan === 'string' ? parsed.body.plan : '';
    resetPlanBoardDraft(planText);
    // A1：仅在看板未开、或已在计划页时切到 Plan，避免打断用户正在看的 Files/Context。
    if (sid === state.activeSessionId && (!isInspectorOpen() || getInspectorActiveTab() === 'plan')) {
      openInspectorToTab('plan');
    }
  }
  let isPresentationOnlyFollowupUpdate = false;
  if (parsed.kind === 'followup_question') {
    const followupStatus = typeof parsed.body.status === 'string' ? parsed.body.status : '';
    isPresentationOnlyFollowupUpdate = Boolean(followupStatus)
      && isRuntimeStaffingFollowup(bookFor(sid).pendingFollowup);
    if (['applied', 'declined', 'failed'].includes(followupStatus)) {
      const followupId = typeof parsed.body.question_id === 'string' ? parsed.body.question_id : '';
      const delay = followupStatus === 'failed' ? 3200 : 1800;
      window.setTimeout(() => {
        const pending = bookFor(sid).pendingFollowup;
        if (
          pending?.questionId === followupId
          && pending.status === followupStatus
          && isRuntimeStaffingFollowup(pending)
        ) {
          patchBook(sid, { pendingFollowup: null });
          if (sid === state.activeSessionId) renderChat();
        }
      }, delay);
    }
  }

  // 状态 hint / busy：仅由 reducer 的 statusHint 与等待用户交互的 kind 驱动，禁止「收到任意 chunk → busy」。
  if (typeof result.queueHint === 'string') setQueueHintWithUi(sid, result.queueHint);
  if (USER_WAIT_CHUNK_KINDS.has(parsed.kind) && !isPresentationOnlyFollowupUpdate) {
    finalizeStreamingTurn(sid);
    setQueueHintWithUi(sid, '');
  }
  const busyNext = resolveBusyTransition(
    parsed.kind,
    result.statusHint,
    bookFor(sid).turnSealed,
    isPresentationOnlyFollowupUpdate,
  );
  if (busyNext !== null) setBusyWithUi(sid, busyNext);
  if (typeof result.statusHint === 'string') setStatusWithUi(sid, result.statusHint);

  // final / error：触发 finalize + usage + 全量重渲染
  if (result.finalize) {
    logStream('apply-chunk', 'finalize-turn', { sid, kind: parsed.kind });
    if (reqId) {
      ttftVisibleLoggedRequests.delete(reqId);
      ttftRenderLoggedRequests.delete(reqId);
      ttftRequestStartedAt.delete(reqId);
    }
    streamingPatchCoalescer.clear();
    if (result.turn) {
      recordUsageTurn(
        sid,
        result.turn.assistantId,
        result.turn.turnDurationMs,
        result.turn.firstTokenMs,
        result.turn.status,
        result.turn.usage,
      );
      const u = result.turn.usage;
      if (u) {
        setUsageSnapshot(sid, {
          promptTokens: u.prompt_tokens,
          completionTokens: u.completion_tokens,
          cacheRead: u.cache_read_input_tokens ?? u.cached_tokens ?? 0,
          cacheWrite: u.cache_creation_input_tokens ?? 0,
          promptBreakdown: u.prompt_breakdown,
        });
      }
    }
    finalizeTurn(sid);
    if (parsed.kind === 'error') setStatusWithUi(sid, 'error');
    renderWorkspaceHistory(openSessionFn);
    renderChat();
    return;
  }

  // 流式路径：delta 首片建骨架 → 全量渲染；后续片走 patch 找不到回退全量。
  if (parsed.kind === 'delta') {
    const assistantId = result.replaceBook?.assistantId ?? null;
    const patched = sid === state.activeSessionId && assistantId
      ? scheduleStreamingTurnPatch(sid, assistantId)
      : false;
    logStream('render', patched ? 'delta-patch-dom' : 'delta-schedule-render', {
      sid,
      assistantId,
      activeSessionId: state.activeSessionId,
    });
    if (sid === state.activeSessionId) {
      // Bug fix: 读取最新的 messages 数组（applyMessageUpserts 已写回 store）
      const currentMessages = getMessages(sid);
      if (currentMessages.length === 0 || !assistantId || !patched) {
        scheduleChatRender();
      }
    }
    // 非活跃（后台）会话的 delta 不重绘侧栏：侧栏项（标题/预览/时间/busy 小圆圈）
    // 已在回合起点由 setStatusWithUi/setBusyWithUi 渲染好，标题更新走 applySessionTitle
    // 自己的 renderWorkspaceHistory。逐 delta 全量 innerHTML 重建会把小圆圈 <span> 反复
    // 重建、CSS 旋转动画从 0 重启，列表一长就表现为「小圆圈卡住抽搐而非转圈」。
    return;
  }

  if (parsed.kind === 'thinking') {
    const assistantId = result.replaceBook?.assistantId ?? null;
    const patched = sid === state.activeSessionId && assistantId
      ? scheduleThinkingTurnPatch(sid, assistantId)
      : false;
    if (sid === state.activeSessionId && !patched) scheduleChatRender();
    return;
  }

  // 其他 kind：合并到下一帧渲染
  if (sid === state.activeSessionId) {
    scheduleChatRender();
    if (parsed.kind === 'todo_updated' || parsed.kind === 'file_changes' || parsed.kind === 'plan_review') {
      if (parsed.kind === 'todo_updated') renderTodoSlot();
      if (parsed.kind === 'file_changes') {
        const rawFiles = parsed.body.files;
        if (Array.isArray(rawFiles)) {
          invalidateFileDiffCachePaths(
            rawFiles
              .map((f) => (f && typeof f === 'object' && typeof (f as { path?: unknown }).path === 'string'
                ? (f as { path: string }).path
                : ''))
              .filter(Boolean),
          );
        }
      }
      refreshInspector();
    } else if (parsed.kind === 'followup_question') {
      scheduleChatRender();
    }
  }
  // 非活跃会话的其它 chunk 同理不重绘侧栏（见上 delta 分支注释）。
}

/** T3：把 reducer 输出的 MessageUpsert 应用到 messageStore。 */
function applyMessageUpserts(sessionId: string, upserts: ReturnType<typeof reduceChunk>['messageUpserts']): void {
  if (upserts.length === 0) return;
  const cur = getMessages(sessionId);
  let next = cur;
  for (const u of upserts) {
    if (u.op === 'append' && u.message) {
      next = [...next, u.message];
    } else if (u.op === 'patch' && u.messageId && u.patch) {
      next = next.map((m) => (m.id === u.messageId ? { ...m, ...u.patch } : m));
    } else if (u.op === 'remove' && u.messageId) {
      next = next.filter((m) => m.id !== u.messageId);
    }
  }
  if (next !== cur) {
    messageStore.set({ messages: { ...messageStore.get().messages, [sessionId]: next } });
  }
}

// ---------- queue / dispatch / send ----------

export function consumePending(sessionId: string): void {
  const [head, _rest] = shiftPendingQueue(sessionId);
  if (!head) return;
  const dispatchOptions: DispatchOptions = { subScenario: head.subScenario ?? '' };
  if (head.planActive !== undefined) dispatchOptions.planActive = head.planActive;
  if (head.clientIntent) dispatchOptions.clientIntent = head.clientIntent;
  if (head.optimisticUserMessageId) dispatchOptions.optimisticUserMessageId = head.optimisticUserMessageId;
  if (head.workDisabledPreferenceIds) dispatchOptions.workDisabledPreferenceIds = head.workDisabledPreferenceIds;
  void dispatchWs(sessionId, head.query, head.attachments ?? [], dispatchOptions);
}

export function sendQueueItemNow(sessionId: string, id: string): void {
  const queue = getPendingQueue(sessionId);
  const index = queue.findIndex((item) => item.id === id);
  if (index < 0 || isBusy(sessionId)) return;
  const item = queue[index]!;
  removePendingQueueItem(sessionId, index);
  setQueueHintWithUi(sessionId, '');
  void dispatchWs(sessionId, item.query, item.attachments ?? [], {
    subScenario: item.subScenario ?? '',
    ...(item.planActive !== undefined ? { planActive: item.planActive } : {}),
    ...(item.workDisabledPreferenceIds
      ? { workDisabledPreferenceIds: item.workDisabledPreferenceIds }
      : {}),
  });
}

export function editQueueItem(sessionId: string, index: number): void {
  const item = getPendingQueue(sessionId)[index];
  if (!item) return;
  // 回填输入框供用户修改：从队列移除，下一次发送按新的提交时间追加到队尾。
  queueEditDraft = { sessionId };
  removePendingQueueItem(sessionId, index);
  setDisabledWorkPreferenceIdsForTurn(item.workDisabledPreferenceIds ?? []);
  if (getPendingQueue(sessionId).length === 0) setQueueHintWithUi(sessionId, '');
  const input = queryPrimaryComposer<HTMLTextAreaElement>('[data-composer-input]');
  if (input) {
    input.value = item.query;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.focus();
  }
}

export async function dispatchWs(
  sessionId: string,
  query: string,
  attachments = takeAttachmentsForSend(),
  subScenarioOrOptions: string | DispatchOptions = '',
  planActiveOverride?: boolean,
): Promise<boolean> {
  const dispatchOptions: DispatchOptions = typeof subScenarioOrOptions === 'string'
    ? {
        subScenario: subScenarioOrOptions,
        ...(planActiveOverride !== undefined ? { planActive: planActiveOverride } : {}),
      }
    : subScenarioOrOptions;
  const subScenario = dispatchOptions.subScenario ?? '';
  if (!requireRendererLogin()) return false;
  if (isDraftSession(sessionId)) {
    await persistDraftSessionModel(sessionId);
  }
  // 新一轮发送：解除对该会话的「迟到分片屏蔽」
  removeSuppressedSession(sessionId);
  if (!state.socket || !state.backendConnected) {
    appendMessage(sessionId, 'error', '服务未连接，请稍后重试。');
    setBusyWithUi(sessionId, false);
    renderChat();
    notify('服务未连接');
    return false;
  }
  // 乐观 UI：先把用户消息渲染出来，再 await WS send。
  // 修订式引导在点击瞬间已经追加了 user 气泡；这里复用该气泡，避免当前 turn 收束后重复出现同一句。
  const optimisticUserMessageId = dispatchOptions.optimisticUserMessageId;
  const hasOptimisticUserMessage = optimisticUserMessageId
    ? getMessages(sessionId).some((m) => m.id === optimisticUserMessageId)
    : false;
  if (!hasOptimisticUserMessage) {
    appendMessage(sessionId, 'user', query, {
      attachments,
      ...(optimisticUserMessageId ? { id: optimisticUserMessageId } : {}),
    });
  }
  renderChat();
  // 用户提交新消息：强制跳到底部（重置 stickyBottom）。对齐 hermes 的 runStart → jumpToBottom。
  jumpChatToBottom();
  const planActiveForSend = dispatchOptions.planActive ?? state.composerMode === 'plan';
  const workDisabledPreferenceIds = dispatchOptions.workDisabledPreferenceIds
    ?? (productModeStore.get().productMode === 'work' ? takeDisabledWorkPreferenceIds() : []);
  // Plan 模式：发送实际 query 之前先发一帧 plan_enter。
  // 注意：openTurn 必须在 plan_enter 之后——plan 控制 status 会把 busy 置 idle，
  // 若先开乐观回合再 plan_enter，会把刚置起的 busy/计时打回 idle。
  await ensurePlanModeForSession(sessionId, planActiveForSend);
  // 外源 Team 路由已写入 session_agent_config；WS payload 再带上 id，便于网关直连场景。
  const externalTeamId = state.activeExternalTeamIdBySession[sessionId] || '';
  const mode = externalTeamId ? 'team' : 'agent';
  state.mode = mode;
  if (externalTeamId) {
    await primeTeamCollaborationIdentity(sessionId);
    renderChat();
  }
  const requestId = newTurnRequestId();
  openTurnForRequest(sessionId, requestId);
  // 乐观置 running + 立刻渲染「正在思考」：覆盖后续 await socket.send 与长 TTFT 空白。
  setBusyWithUi(sessionId, true);
  setStatusWithUi(sessionId, 'running');
  renderChat();
  syncTurnDurationTicker();
  jumpChatToBottom();
  const clientTs = Date.now();
  ttftRequestStartedAt.set(requestId, clientTs);
  logStream('dispatch', 'send-ws', {
    sessionId,
    requestId,
    mode,
    queryLen: query.length,
    backendConnected: state.backendConnected,
    clientTs,
  });
  // 专用 Wiki Agent 经注册口提供本会话的 wiki_kb_id。
  const wikiExtras = wikiSendExtrasResolver?.(sessionId) ?? null;
  const ok = await state.socket.send({
    query,
    session_id: sessionId,
    request_id: requestId,
    mode,
    workspace_id: workspaceForSessionDispatch(sessionId),
    attachments,
    ...(planActiveForSend ? { plan_active: true } : {}),
    ...(dispatchOptions.clientIntent ? { client_intent: dispatchOptions.clientIntent } : {}),
    ...(externalTeamId ? { external_team_id: externalTeamId } : {}),
    ...(subScenario ? { sub_scenario: subScenario } : {}),
    ...(wikiExtras ? { wiki_kb_id: wikiExtras.wikiKbId } : {}),
    ...(dispatchOptions.wikiConfirmationId ? { wiki_confirmation_id: dispatchOptions.wikiConfirmationId } : {}),
    ...(workDisabledPreferenceIds.length > 0 ? { work_disabled_preference_ids: workDisabledPreferenceIds } : {}),
  });
  if (!ok) {
    logStream('dispatch', 'send-ws-failed', { sessionId, requestId });
    ttftRequestStartedAt.delete(requestId);
    discardEmptyOptimisticAssistant(sessionId);
    appendMessage(sessionId, 'error', '服务未连接，请稍后重试。');
    setBusyWithUi(sessionId, false);
    setStatusWithUi(sessionId, 'idle');
    renderChat();
    syncTurnDurationTicker();
    return false;
  }
  subscribeSessions([sessionId]);
  logStream('dispatch', 'local-state-ready', { sessionId, requestId, busy: true });
  renderChat();
  syncTurnDurationTicker();
  return true;
}

export async function sendMessage(text: string): Promise<void> {
  if (!requireRendererLogin()) return;
  const plainContent = text.trim();
  const attachments = state.attachments;
  const activeHasAnnotations = state.activeSessionId
    && (hasSiteAnnotationDraft(state.activeSessionId) || hasBlueprintAnnotationDraft(state.activeSessionId));
  if (!plainContent && attachments.length === 0 && !activeHasAnnotations) return;

  // 无活跃会话（欢迎页）时新建草稿：用 composerWorkspaceId()（即右下角选择器所显示的空间），
  // 而不是裸读 state.currentWorkspaceId——避免二者不一致时消息落到选择器之外的空间。
  const sessionId = state.activeSessionId ?? createSessionInWorkspace(composerWorkspaceId(), openSessionFn);
  const hasAnnotationDraft = hasSiteAnnotationDraft(sessionId) || hasBlueprintAnnotationDraft(sessionId);
  const content = composeBlueprintAnnotationMessage(
    sessionId,
    composeSiteAnnotationMessage(sessionId, plainContent),
  );
  const previewText = plainContent || (hasAnnotationDraft ? '页面注释' : '附件消息');

  if (isBusy(sessionId)) {
    const pendingItem = {
      id: newMessageId('q'),
      query: content || '(附件)',
      attachments: [...state.attachments],
      subScenario: takeArmedSubScenario(),
      planActive: state.composerMode === 'plan',
      workDisabledPreferenceIds: productModeStore.get().productMode === 'work'
        ? takeDisabledWorkPreferenceIds()
        : [],
    };
    if (queueEditDraft?.sessionId === sessionId) {
      queueEditDraft = null;
    }
    enqueuePending(sessionId, pendingItem);
    clearSiteAnnotationDraft(sessionId);
    clearBlueprintAnnotationDraft(sessionId);
    setQueueHintWithUi(sessionId, '正在排队…');
    setStatusWithUi(sessionId, 'queued');
    // 入队不改写会话标题/预览：会话主题由首条消息确定（已在下方非-busy 分支经 dispatchWs +
    // commitDraftSession/updateSessionPreview 落定；草稿的首轮回合在飞行中也会走那条路径提交）。
    // 这里若再 updateSessionPreview/commitDraftSession，会把侧栏标题改成这条「待发/引导」消息——
    // 即用户观察到的「出现引导对话后，当前会话标题变成 steer 的内容」。只更新 updatedAt 维持
    // 「最近活动」排序，标题/预览保持首条消息。
    const enqueuedSession = state.sessions.find((s) => s.id === sessionId);
    if (enqueuedSession) enqueuedSession.updatedAt = Date.now();
    clearAttachments();
    renderAttachmentPreview();
    if (isStudioView()) openStudioChatPanel();
    renderChat();
    setTabFn('chat');
    return;
  }

  if (queueEditDraft?.sessionId === sessionId) {
    queueEditDraft = null;
  }

  const sent = await dispatchWs(sessionId, content, takeAttachmentsForSend(), takeArmedSubScenario());
  if (sent) {
    clearSiteAnnotationDraft(sessionId);
    clearBlueprintAnnotationDraft(sessionId);
  }
  clearScenarioChip();
  if (isDraftSession(sessionId)) {
    commitDraftSession(sessionId, makeSessionTitle(previewText), previewText.slice(0, 48), openSessionFn);
  } else {
    updateSessionPreview(sessionId, previewText);
  }
  renderWorkspaceHistory(openSessionFn);
  syncComposerWorkspaceLabel();
  if (isStudioView()) openStudioChatPanel();
  setTabFn('chat');
  renderChat();
}

/** 把指定会话当前正在 streaming 的 assistant 消息冻结并结算回合耗时。
 *  用于中断/断连/撤回等"非正常 final"路径——这些路径不会有 final/error 帧来触发正常结算，
 *  若不处理，streaming 会一直为 true，耗时会随每次渲染无限累加（停不下来）。
 *  只结算耗时与冻结 streaming，不动 book/busy/queue（那些由各自路径的既有逻辑负责）。 */
export function finalizeStreamingTurn(sessionId: string): void {
  const list = state.messages[sessionId];
  if (!list) return;
  for (const m of list) {
    if (m.role === 'assistant' && m.streaming) {
      const startedAt = m.turnStartedAt;
      patchMessage(sessionId, m.id, {
        streaming: false,
        interrupted: true,
        turnDurationMs: startedAt != null ? Date.now() - startedAt : 0,
        timestamp: Date.now(),
      });
    }
  }
  // Team 仅追加自己的结算分支；上面单 Agent/专家团的原循环不改。
  for (const message of list) {
    if (message.role !== 'team_internal' || !message.streaming) continue;
    const startedAt = message.turnStartedAt;
    patchMessage(sessionId, message.id, {
      streaming: false,
      interrupted: true,
      turnDurationMs: startedAt != null ? Date.now() - startedAt : 0,
      timestamp: Date.now(),
    });
  }
}

export function stopGeneration(sessionIdOverride?: string | Event): void {
  const sessionId = typeof sessionIdOverride === 'string' ? sessionIdOverride : state.activeSessionId;
  if (!sessionId) return;
  // 修法3：用户主动停止是终态（每会话仅一个活跃回合）→ 清该会话 delta 重组缓冲，防泄漏。
  // withdrawMessage 走 stopGeneration，故撤回编辑也由此覆盖。
  resetSession(sessionId);
  replacePendingQueue(sessionId, []);
  setQueueHintWithUi(sessionId, '');
  const stopSent = state.socket?.stop(sessionId) ?? Promise.resolve(false);
  // 用户主动停止：立即以停止时刻结算当前 streaming 回合的耗时显示。
  // 同时屏蔽迟到分片，直到下一次 dispatchWs 解除，避免取消竞态重建旧回复。
  finalizeStreamingTurn(sessionId);
  resetBook(sessionId);
  addSuppressedSession(sessionId);
  setBusyWithUi(sessionId, false);
  setStatusWithUi(sessionId, 'idle');
  void stopSent.then((ok) => {
    if (!ok) {
      setStatusWithUi(sessionId, 'error');
      appendMessage(sessionId, 'error', '服务未连接，已在本地停止等待');
      renderChat();
    }
  });
  renderChat();
  // 用户停止后立刻拉取后端最新看板状态，避免右侧阶段仍显示“进行中”。
  void refreshKanbanBoard();
}

/** 撤回修改一条用户消息：中断当前工作流 → 删掉该消息及之后所有内容 → 文本回到输入框 →
 *  发送按钮红色高亮，等用户改完重新发送。 */
export function withdrawMessage(msgId: string): void {
  const sessionId = state.activeSessionId;
  if (!sessionId) return;
  const list = state.messages[sessionId];
  const idx = list.findIndex((m) => m.id === msgId);
  if (idx < 0 || list[idx].role !== 'user') return;
  const content = list[idx].content;

  // 1. 中断当前工作流 + 立即解除 busy（让发送按钮可点）
  stopGeneration();
  setBusyWithUi(sessionId, false);

  // 2. 冻结「该消息起」的所有内容（把还在流式的助手消息标记为非流式，
  //    这样取消编辑还原时，它就停在被打断那一刻，不再带 … 光标）。
  //    注意：不删除！只是用 editFromIdx 标记隐藏范围，取消编辑可整段还原。
  //    不可变更新：原实现就地改 list[k].streaming 不触发 messageStore 订阅，
  //    改用 store 不可变写回，避免与并发 patchMessage 互相覆盖。
  const frozen = list.map((m, k) => (k >= idx && m.streaming ? { ...m, streaming: false } : m));
  messageStore.set({ messages: { ...messageStore.get().messages, [sessionId]: frozen } });
  // 3. 复位记账：assistantId 指向正在生成的消息，必须清掉，否则下一轮的回复会 patch 到它上面
  resetBook(sessionId);
  // 4. 屏蔽中断后可能迟到的流式分片，直到重新发送（dispatchWs 会清掉）
  addSuppressedSession(sessionId);
  // 5. 标记编辑起点：渲染时隐藏 [idx..] 的内容，但不删除
  setEditFrom(sessionId, idx);

  // 6. 回填输入框
  const input = queryPrimaryComposer<HTMLTextAreaElement>('[data-composer-input]');
  if (input) {
    input.value = content;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.focus();
  }

  // 7. 显示「正在编辑」提示条 + 重新渲染（保持对话流页面，不切欢迎页）
  updateComposerControls();
  renderChat();
}

/** 取消编辑：还原被隐藏的内容（editFromIdx 清掉即可，因为从没真正删除过）。 */
export function cancelEdit(): void {
  const sessionId = state.activeSessionId;
  if (!sessionId) return;
  setEditFrom(sessionId, null);
  const input = queryPrimaryComposer<HTMLTextAreaElement>('[data-composer-input]');
  if (input) {
    input.value = '';
    input.dispatchEvent(new Event('input', { bubbles: true }));
  }
  updateComposerControls();
  renderChat();
}

function requestRevisionAfterCurrentTurn(sessionId: string): void {
  setQueueHintWithUi(sessionId, '已引导，当前回复将尽快结束…');
  if (!isBusy(sessionId)) {
    consumePending(sessionId);
    return;
  }
  const sent = state.socket?.interrupt(sessionId);
  if (!sent) {
    notify('服务未连接，已保留为下一条待发消息');
    return;
  }
  void sent.then((ok) => {
    if (!ok && !isBusy(sessionId)) {
      consumePending(sessionId);
      return;
    }
    if (!ok) notify('当前回复暂时无法中断，已保留为下一条待发消息');
  });
}

/** 待发卡片「引导」按钮：立刻显示为用户气泡，再把该队列项提升为修订式下一轮。 */
export function steerQueuedItem(sessionId: string, index: number): void {
  const optimisticUserMessageId = newMessageId('user');
  const item = promotePendingQueueItemAsRevision(sessionId, index, { optimisticUserMessageId });
  if (!item) return;
  appendMessage(sessionId, 'user', item.query, {
    id: optimisticUserMessageId,
    attachments: item.attachments ?? [],
  });
  requestRevisionAfterCurrentTurn(sessionId);
  renderChat();
  jumpChatToBottom();
}

export function steerGeneration(text: string): void {
  if (!requireRendererLogin()) return;
  const sessionId = state.activeSessionId;
  const t = text.trim();
  if (!sessionId || !t) return;
  const optimisticUserMessageId = newMessageId('user');
  appendMessage(sessionId, 'user', t, { id: optimisticUserMessageId });
  replacePendingQueue(sessionId, [
    {
      id: newMessageId('q'),
      query: t,
      attachments: [],
      clientIntent: 'revision',
      optimisticUserMessageId,
    },
    ...getPendingQueue(sessionId),
  ]);
  requestRevisionAfterCurrentTurn(sessionId);
  renderChat();
  jumpChatToBottom();
}

export function subscribeSessions(sessionIds: string[]): void {
  const sessions = addSubscribedSessions(sessionIds);
  void state.socket?.subscribe(sessions, getLastGatewaySequences(sessions));
}

export async function refreshSessions(): Promise<void> {
  if (!requireRendererLogin()) return;
  await refreshAllSessions();
  const { loadChannelSessions } = await import('./channel-sessions');
  await loadChannelSessions();
  renderWorkspaceHistory(openSessionFn);
}

export function seedStatuses(seed: Record<string, string>): void {
  for (const [sid, st] of Object.entries(seed)) {
    if (isBusy(sid)) continue;
    setSessionStatus(sid, (st as SessionStatus) ?? 'idle');
  }
  renderWorkspaceHistory(openSessionFn);
}
