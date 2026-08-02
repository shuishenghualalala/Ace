/**
 * Chat 流式处理与渲染入口。
 *
 * 这里集中了「会话消息流」相关的全部逻辑：流式分片 dispatch、回合结算、
 * 消息发送/撤回/编辑，以及与之强耦合的 DOM 渲染（renderChat /
 * patchStreamingTurn / renderQueueSlot）。
 *
 * 与 index.ts 的解耦：本模块不 import index.ts（避免循环）。需要回调
 * openSession / renderWorkspaceHistory 的位置，通过模块级 registry（setCallbacks）
 * 由 index.ts 在 init 时注入。
 */

import { getLastGatewaySequences, isDuplicateGatewayChunk, noteGatewaySequence, touchStreamActivity } from './gateway-sequence';
import { noteDelta, resetAssistant, resetSession } from '../stream-reassembly';
import {
  type ChatMessage,
  type AgentTurnOptions,
  type MessageRole,
  type SessionStatus,
  type ToolCallInfo,
  renderAgentTurn,
  renderEmptyState,
  renderMessageHtml,
  renderTeamInternalMessage,
  renderQueueHintCard,
  renderQueuePanelHtml,
  renderTodoProgressPanelHtml,
  renderTypingIndicator,
  resolveLiveFoldLabel,
  resolveTeamTurnFoldLabel,
  resolveTurnDurationMs,
  shouldShowTodoPanel,
  turnHasProcessContent,
  hasVisibleAnswerText,
} from '../chat-render';
import { renderMarkdownHtmlStreaming } from '../markdown';
import { diffRenderUnits, type RenderUnit } from '../chat-diff';
import { recordTurn } from './usage-tracker';
import { onAfterFinal } from './cron-page';
import { attachScrollAnchor, type ScrollAnchor } from './scroll-anchor';
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
import {
  primeTeamCollaborationIdentity,
  resolveTeamCollaborationMember,
  resolveTeamCollaborationName,
} from './team-collaboration-board';
import { syncCraftLabel, syncComposerModelLabel } from './composer-toolbar';
import { syncModelUi } from './model-picker';
import { resetToAgentMode } from './session-mode';
import { isExternalTeamSession, persistDraftSessionModel, sessionDisplayModelLabel } from './session-model';
import { renderSystemOverview } from './system-page';
import { isInspectorOpen, getInspectorActiveTab, openBrowserWorkbench, openInspectorToTab, refreshInspector, refreshInspectorChrome, invalidateFileDiffCachePaths, setUsageSnapshot, resetPlanBoardDraft } from './inspector';
import { showFileOpenMenu } from './file-open-menu';
import { openBrowserArtifact, openUserBrowser } from './browser-panel';
import { shouldAutoOpenBrowserWorkbench } from './browser-auto-open';
import { htmlArtifactPathFromHref, httpUrlFromHref } from '../artifact-links';
import { syncRunningIntroSlot } from './running-intro';
import {
  applyBusyUi,
  discardEmptyOptimisticAssistant,
  newTurnRequestId,
  openTurnForRequest,
  resumeSessionGeneration,
} from './session-busy';
import { clearScenarioChip, takeArmedSubScenario } from './scenario-arm';
import { bindFollowupCard, formatFollowupAnswerMessage, renderFollowupCardElement } from '../followup';
import type { FollowupAnswer } from '../backend-client';
import type { ChatChunk, WikiIngestProgress } from '../backend-client';
import { makeSessionTitle, mergeTeamInternalMessage } from './history-mapping';
import { applyFoldState, createChatRenderCoalescer, createStreamingPatchCoalescer } from '../render-utils';
import { getToolFold, setToolFold, setTurnFold } from './fold-state';
import { attachCopyButtons } from './copy-button';
import { renderMermaidBlocks } from './mermaid-render';
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
  $$,
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
  movePendingQueueItem,
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
import { isStreamDebugEnabled, logStream } from '../stream-debug';

// ---------- registry: 由 index.ts 在 init 时注入的回调（破循环） ----------

let openSessionFn: OpenSessionFn = async () => {};
let setTabFn: (tab: TabKey) => void = () => {};
let queueEditDraft: { sessionId: string } | null = null;
let teamIdentityRefreshBound = false;

interface DispatchOptions {
  subScenario?: string;
  planActive?: boolean;
  clientIntent?: 'revision';
  optimisticUserMessageId?: string;
  wikiConfirmationId?: string;
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

/** 在主聊天区打开指定会话（Wiki 右栏等外部面板跳转会话用）：先 openSession 再 setTab('chat')。 */
export async function openSessionInChat(sessionId: string): Promise<void> {
  if (!sessionId) return;
  await openSessionFn(sessionId);
  setTabFn('chat');
}

// ---------- Wiki ingest 进度帧转发 ----------
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
  const sessionId = state.activeSessionId;
  const busy = sessionId ? isBusy(sessionId) : false;
  // busy 且输入框有内容 → 切「发送」态（可直接发送→入队待发卡片）；输入清空（发送后/手动删）
  // → 回到「停止」态。CSS 据 --busy / --composing 显隐 send/stop 按钮。
  const input = $('#chat-input') as HTMLTextAreaElement | null;
  const composing = busy && !!input?.value.trim();
  const ctrl = $('#composer-controls');
  ctrl?.classList.toggle('composer-controls--busy', busy);
  ctrl?.classList.toggle('composer-controls--composing', composing);
  // 撤回修改后，在输入框上方显示「正在编辑」提示条（仅当前会话处于编辑态时）
  const editing = sessionId ? state.editFromIdx[sessionId] != null : false;
  $('#composer-edit-banner')?.classList.toggle('show', editing);
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
    const anchor = getScrollAnchorInstance();
    anchor.pinToBottomIfSticky();
  });
}

// ---------- scroll anchor：会话内 stickyBottom 管理 ----------

/** 当前容器的 scroll anchor 实例（懒创建 + 容器切换时重建）。 */
let scrollAnchor: ScrollAnchor | null = null;
let scrollAnchorContainerId: string | null = null;

/** 拿到当前渲染容器的 scroll anchor；容器变了就 dispose 旧的、建新的。
 *  命名带 Instance 后缀以避免与 chat-diff 渲染层同名的 getScrollAnchor 冲突。 */
function getScrollAnchorInstance(): ScrollAnchor {
  const containerId = 'chat-messages';
  const container = document.getElementById(containerId);
  if (!container) {
    // 容器还没挂载：返回一个 no-op anchor，避免外部炸。
    return noOpScrollAnchor;
  }
  if (scrollAnchor && scrollAnchorContainerId === containerId) {
    return scrollAnchor;
  }
  // 容器被替换时销毁旧实例并重新绑定。
  scrollAnchor?.dispose();
  scrollAnchor = attachScrollAnchor(container);
  scrollAnchorContainerId = containerId;
  return scrollAnchor;
}

const noOpScrollAnchor: ScrollAnchor = {
  jumpToBottom: () => {},
  pinToBottomIfSticky: () => {},
  isStickyBottom: () => true,
  disarm: () => {},
  dispose: () => {},
};

/** 用户提交新消息 / 切会话 / 空→非空 时调用：强制跳到底部并重置 sticky。 */
export function jumpChatToBottom(): void {
  requestAnimationFrame(() => {
    getScrollAnchorInstance().jumpToBottom();
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
  if (sessionId === state.activeSessionId) renderQueueSlot();
}

// ---------- fold 委托（一次性 capture 监听） ----------

/** 「已编辑文件」卡：打开看板 Files / 在资源管理器中显示（与 fold 委托同容器、各绑一次）。
 *  WeakSet：Wiki 右栏面板等会整体重建 DOM 的容器也会绑定，避免持有已分离元素。 */
const fileChangesBoundContainers = new WeakSet<HTMLElement>();
export function ensureFileChangesDelegation(container: HTMLElement): void {
  if (fileChangesBoundContainers.has(container)) return;
  fileChangesBoundContainers.add(container);
  container.addEventListener('click', (event) => {
    const target = event.target instanceof Element ? event.target : null;
    if (!target) return;
    const revealBtn = target.closest<HTMLElement>('[data-file-reveal]');
    if (revealBtn && container.contains(revealBtn)) {
      event.preventDefault();
      event.stopPropagation();
      const path = revealBtn.getAttribute('data-file-reveal');
      if (path) void showFileOpenMenu(revealBtn, path);
      return;
    }
    const artifact = target.closest<HTMLElement>('[data-browser-artifact]');
    if (artifact && container.contains(artifact)) {
      event.preventDefault();
      event.stopPropagation();
      const path = artifact.getAttribute('data-browser-artifact');
      if (path) {
        void openBrowserArtifact(path, true, { confirmTakeover: true }).then((destination) => {
          if (destination === 'in_app') openBrowserWorkbench({ createTab: false });
        });
      }
      return;
    }
    const anchor = target.closest<HTMLAnchorElement>('a[href]');
    if (anchor && container.contains(anchor)) {
      const href = anchor.getAttribute('href') || '';
      const artifactPath = htmlArtifactPathFromHref(href);
      const httpUrl = httpUrlFromHref(href);
      if (artifactPath || httpUrl) {
        event.preventDefault();
        if (artifactPath) {
          void openBrowserArtifact(artifactPath, true, { confirmTakeover: true }).then((destination) => {
            if (destination === 'in_app') openBrowserWorkbench({ createTab: false });
          });
        } else if (httpUrl) {
          void openUserBrowser(httpUrl, true, {
            confirmTakeover: true,
          }).then((destination) => {
            if (destination === 'in_app') openBrowserWorkbench({ createTab: false });
          });
        }
      }
    }
    const openBtn = target.closest<HTMLElement>('[data-file-changes-open]');
    if (openBtn && container.contains(openBtn)) {
      event.preventDefault();
      const expandPath = openBtn.getAttribute('data-file-changes-path');
      let filePaths: string[] | null = null;
      let fileChanges: NonNullable<Parameters<typeof openInspectorToTab>[1]>['fileChanges'] = null;
      const rawPaths = openBtn.getAttribute('data-file-changes-paths');
      if (rawPaths) {
        try {
          const parsed = JSON.parse(rawPaths) as unknown;
          if (Array.isArray(parsed)) filePaths = parsed.filter((path): path is string => typeof path === 'string');
        } catch {
          filePaths = null;
        }
      }
      const rawSummaries = openBtn.getAttribute('data-file-changes-summaries');
      if (rawSummaries) {
        try {
          const parsed = JSON.parse(rawSummaries) as unknown;
          if (Array.isArray(parsed)) {
            fileChanges = parsed
              .filter((item): item is Record<string, unknown> => !!item && typeof item === 'object' && typeof item.path === 'string')
              .map((item) => ({
                path: item.path as string,
                name: typeof item.name === 'string' ? item.name : (item.path as string).split(/[\\/]/).pop() || (item.path as string),
                added: typeof item.added === 'number' ? item.added : 0,
                removed: typeof item.removed === 'number' ? item.removed : 0,
                status: item.status === 'added' || item.status === 'deleted' || item.status === 'modified' ? item.status : 'modified',
                diff: [],
                binary: item.binary === true,
              }));
          }
        } catch {
          fileChanges = null;
        }
      }
      openInspectorToTab('files', { expandFilePath: expandPath, filePaths, fileChanges });
    }
  });
}

/** 一次性事件委托：在消息容器上 capture 监听 toggle（toggle 不冒泡）。
 *  对话容器只绑定一次。
 *  覆盖两类折叠：
 *   - 回合级 `.msg__foldable`：写 state.userFoldedTurns/userUnfoldedTurns（现有路径）+ fold-state.ts 持久化。
 *   - 时间线工具项 `.process-timeline__details`：直接写 fold-state.ts（data-fold-key 由 renderToolCard 写入；
 *     无 data-fold-key 的项（如思考）不持久化）。 */
const foldBoundContainers = new Set<HTMLElement>();
/**
 * 推理阶段（尚无硬确认正文）用户手动展开的 turnId。
 * 正式正文到来时清除，使过程区仍自动折；不写入 localStorage。
 * 正文出现后的手动展开走 setTurnFold 持久化，不受此集合影响。
 */
const ephemeralUnfoldedTurns = new Set<string>();

function ensureFoldDelegation(container: HTMLElement): void {
  if (foldBoundContainers.has(container)) return;
  foldBoundContainers.add(container);

  const markUserFoldIntent = (target: EventTarget | null): void => {
    const el = target instanceof Element
      ? target.closest<HTMLDetailsElement>('details.msg__foldable, details.process-timeline__details')
      : null;
    if (!el) return;
    el.dataset.userFoldIntent = '1';
  };

  container.addEventListener('pointerdown', (e) => markUserFoldIntent(e.target), true);
  container.addEventListener(
    'keydown',
    (e) => {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      markUserFoldIntent(e.target);
    },
    true,
  );
  container.addEventListener(
    'toggle',
    (e) => {
      // 回合级折叠
      const turnDetails = e.target as HTMLDetailsElement | null;
      if (turnDetails && turnDetails.matches('details.msg__foldable')) {
        const userInitiated = turnDetails.dataset.userFoldIntent === '1';
        delete turnDetails.dataset.userFoldIntent;
        if (!userInitiated) return;
        const turnEl = turnDetails.closest<HTMLElement>('.msg[data-message-id]');
        const turnId = turnEl?.getAttribute('data-message-id');
        if (!turnId) return;
        const sid = state.activeSessionId;
        const msgs = sid ? getMessages(sid) : [];
        // 找到该 turn 的 batch，判断是否已有硬确认正文
        let batchStart = msgs.findIndex((m) => m.id === turnId);
        if (batchStart < 0) batchStart = 0;
        let batchEnd = batchStart + 1;
        while (batchEnd < msgs.length) {
          const r = msgs[batchEnd].role;
          const hasAgent = msgs[batchEnd].agentName;
          if ((r === 'assistant' || r === 'error' || (r === 'status' && !hasAgent))) batchEnd += 1;
          else break;
        }
        const batch = msgs.slice(batchStart, batchEnd);
        const answerConfirmed = hasVisibleAnswerText(batch);

        if (turnDetails.open) {
          if (!answerConfirmed) {
            // 推理中临时展开：只记 ephemeral，不持久化
            ephemeralUnfoldedTurns.add(turnId);
            applyFoldState(turnId, true, {
              unfolded: state.userUnfoldedTurns,
              folded: state.userFoldedTurns,
            });
          } else {
            ephemeralUnfoldedTurns.delete(turnId);
            setTurnFold(turnId, true, {
              unfolded: state.userUnfoldedTurns,
              folded: state.userFoldedTurns,
            });
          }
        } else {
          // 手动折叠：持久化；并清掉临时展开
          ephemeralUnfoldedTurns.delete(turnId);
          setTurnFold(turnId, false, {
            unfolded: state.userUnfoldedTurns,
            folded: state.userFoldedTurns,
          });
        }
        return;
      }
      // 时间线工具项折叠（无 data-fold-key 的项不持久化）
      const toolDetails = e.target as HTMLDetailsElement | null;
      if (toolDetails && toolDetails.matches('details.process-timeline__details')) {
        const userInitiated = toolDetails.dataset.userFoldIntent === '1';
        delete toolDetails.dataset.userFoldIntent;
        if (!userInitiated) return;
        const foldKey = toolDetails.getAttribute('data-fold-key');
        if (!foldKey) return;
        setToolFold(foldKey, toolDetails.open);
      }
    },
    true,
  );
}

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

/** 流式渲染合并：同一帧内多次 schedule 只触发一次 renderChat。 */
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
        // 保留单 Agent/Dynamic Kanban 原 patch 路径；Team 仅在原路径无活跃
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

// ---------- 队列槽渲染 ----------

export function renderQueueSlot(): void {
  const slot = $('#chat-queue-slot');
  if (!slot) return;
  const sessionId = state.activeSessionId;
  if (!sessionId) {
    slot.innerHTML = '';
    return;
  }
  const queue = state.pendingQueues[sessionId] ?? [];
  // 动态看板后端 steer 目前是日志占位（crew/dynamickanban/manager.py），不实际注入——
  // 这类会话隐藏「引导」按钮，避免点了触发缓存空等（即「引导后空白数分钟」）。
  const canSteer = !isDynamicKanbanSession(sessionId);
  // 流式期间 renderChat 每帧（1s 计时器 + 各类非-delta chunk）都会回调本函数。
  // 若每帧都 slot.innerHTML = renderQueuePanelHtml(...) 全量重建，会：
  //   ① 把已点开的「更多」菜单 panel.hidden 重置回默认隐藏 → 菜单一闪而过；
  //   ② 在 pointerdown→click 之间替换按钮节点 → 卡片点不上。
  // 队列内容仅在显式增删/移动/编辑/引导或切会话时才变，故用 sig 比对：内容未变就跳过重建，
  // 保留菜单展开态与已绑定的事件监听。
  const sig = `${canSteer ? '1' : '0'}|${queue.length}|${queue.map((i) => `${i.id}:${i.query}:${i.optimisticUserMessageId ?? ''}`).join(' ')}`;
  if (slot.dataset.queueSig === sig) return;
  slot.dataset.queueSig = sig;
  if (queue.length === 0) {
    slot.innerHTML = '';
    return;
  }
  slot.innerHTML = renderQueuePanelHtml(queue, canSteer);
  $$('[data-queue-remove]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const index = Number(btn.getAttribute('data-queue-remove'));
      removePendingQueueItem(sessionId, index);
      if (getPendingQueue(sessionId).length === 0) setQueueHintWithUi(sessionId, '');
      renderQueueSlot();
    });
  });
  $$('[data-queue-steer]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const index = Number(btn.getAttribute('data-queue-steer'));
      steerQueuedItem(sessionId, index);
    });
  });
  $$('[data-queue-edit]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const index = Number(btn.getAttribute('data-queue-edit'));
      editQueueItem(sessionId, index);
    });
  });
  $$('[data-queue-move]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const index = Number(btn.getAttribute('data-queue-move'));
      const direction = Number(btn.getAttribute('data-queue-move-dir'));
      movePendingQueueItem(sessionId, index, index + direction);
      renderQueueSlot();
    });
  });
  $$('[data-queue-menu]').forEach((btn) => {
    btn.addEventListener('click', (event) => {
      event.stopPropagation();
      const index = Number(btn.getAttribute('data-queue-menu'));
      const panel = slot.querySelector<HTMLElement>(`[data-queue-menu-panel="${index}"]`);
      const shouldOpen = !!panel?.hidden;
      slot.querySelectorAll<HTMLElement>('[data-queue-menu-panel]').forEach((el) => {
        el.hidden = true;
      });
      slot.querySelectorAll<HTMLElement>('[data-queue-menu]').forEach((el) => {
        el.setAttribute('aria-expanded', 'false');
      });
      if (panel) {
        panel.hidden = !shouldOpen;
        btn.setAttribute('aria-expanded', shouldOpen ? 'true' : 'false');
      }
    });
  });
}

export function renderTodoSlot(): void {
  const slot = $('#chat-todo-slot');
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
 * 流式正文增量 patch：只更新当前 streaming 回合的正文 + 计时 label，
 * 不重建整棵消息树，以避免：
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
  const containerId = 'chat-messages';
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
  const containerId = 'chat-messages';
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
  const textEl = turnEl.querySelector<HTMLElement>(`[data-text-for="${assistantId}"]`);
  if (textEl) {
    textEl.classList.remove('typing-inline');
    textEl.innerHTML = msg.content ? renderMarkdownHtmlStreaming(msg.content) : '';
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

// ---------- renderChat：keyed 增量 diff 渲染 ----------

/**
 * 跨帧维护 Map<key, HTMLElement>，每帧只重建真正变化的单元。
 *
 * 每帧的渲染目标是有序的 render-unit 列表（key + sig + build fn），
 * 由纯逻辑 diffRenderUnits(prev, next) 算出最小 op：
 * reuse（原样保留）/ patch（sig 变了，重建该单元）/ append（新单元）/ remove（消失）。
 *
 * 性能收益：流式中只有「在飞的最后一个回合」sig 变化 → 只重建它；前 N-1 个回合的节点原样复用。
 *
 * 行为等价性：
 *  - reuse：节点是上一帧 render* 的产物；只要 sig 保守地覆盖了 render* 的全部输入，
 *    「sig 相同」⟺「render* 输出相同」⟺ 复用旧节点 == 全量重建该单元的输出。
 *  - patch/append：调的是同一批 render*（renderAgentTurn / renderMessageHtml / ...），
 *    与全量重建走完全相同的代码路径，输出逐字节相同。
 *  - 因此 reuse + patch + append 在可见 DOM 上与「全量重建后 replaceChildren」等价，
 *    但复用了绝大多数节点（省掉它们的 build + GC 开销）。
 *
 * 模块级缓存：按容器 id 维护 ChatRenderTarget。
 */
interface ChatRenderTarget {
  wrapper: HTMLElement | null;
  lastUnits: Map<string, HTMLElement>;
  lastUnitMetas: RenderUnit[];
  lastSessionId: string | null;
  scrollAnchorNode: HTMLDivElement | null;
}

const renderTargets = new Map<string, ChatRenderTarget>();

function getChatRenderTarget(containerId: string): ChatRenderTarget {
  let target = renderTargets.get(containerId);
  if (!target) {
    target = {
      wrapper: null,
      lastUnits: new Map(),
      lastUnitMetas: [],
      lastSessionId: null,
      scrollAnchorNode: null,
    };
    renderTargets.set(containerId, target);
  }
  return target;
}

/** 会话切换时清空所有容器的 diff 缓存，避免串会话。 */
function resetAllChatRenderTargetsForSession(sessionId: string | null): void {
  for (const target of renderTargets.values()) {
    if (target.lastSessionId !== sessionId) {
      target.lastUnits = new Map();
      target.lastUnitMetas = [];
      target.wrapper = null;
      target.lastSessionId = sessionId;
    }
  }
}

/** 给定 build() 产物：null 或 data-empty 占位视为「缺席」（不进 DOM、不占 Map 槽位）。
 *  build 返回 null 的单元不进 DOM；
 *  renderAgentTurn 空批次返回 data-empty div）。 */
function isPresent(node: HTMLElement | null): node is HTMLElement {
  if (!node) return false;
  if (node.dataset.empty === 'true') return false;
  return true;
}

/** 稳定的 scroll-anchor 节点：sig 恒定，跨帧复用同一个 div（避免每帧新建）。 */
function getScrollAnchor(target: ChatRenderTarget): HTMLDivElement {
  if (!target.scrollAnchorNode) {
    const div = document.createElement('div');
    div.id = 'chat-scroll-anchor';
    target.scrollAnchorNode = div;
  }
  return target.scrollAnchorNode;
}

/** 对一条 ChatMessage 算 sig：覆盖 renderMessageHtml 实际依赖的全部字段（偏细 = 安全）。 */
function sigUserMessage(msg: ChatMessage, configModel: string): string {
  // renderMessageHtml(user) 依赖：role / content / model / timestamp / attachments / id
  // status+agentName 分支单独有 sig（见 sigAgentRoleCard）。
  const att = msg.attachments
    ? msg.attachments.map((a) => `${a.type}|${a.path}|${a.name}`).join(',')
    : '';
  return `u|${msg.id}|${msg.role}|${msg.content}|${msg.model ?? ''}|${configModel}|${msg.timestamp}|${att}`;
}

/** 带 agentName 的 status/assistant（Dynamic Kanban 角色卡片/最终结果）sig。 */
function sigAgentRoleCard(msg: ChatMessage, configModel: string): string {
  return `arc|${msg.id}|${msg.role}|${msg.content}|${msg.agentName ?? ''}|${msg.agentAvatar ?? ''}|${msg.segmentRole ?? ''}|${msg.timestamp}|${configModel}`;
}

/** Dynamic Kanban workflow 进度面板 sig。 */
function sigWorkflowProgress(msg: ChatMessage, _configModel: string): string {
  const wp = msg.workflowProgress;
  if (!wp) return `wp|${msg.id}|${msg.timestamp}`;
  const cp = (wp.completed_phases || []).map((p) => `${p.id}:${p.status}`).join(',');
  const ac = (wp.active_calls || []).map((c) => `${c.call_id}:${c.role}`).join(',');
  const cur = wp.current_phase ? `${wp.current_phase.id}:${wp.current_phase.status}` : '';
  return `wp|${msg.id}|${wp.workflow_id}|${wp.status}|${cur}|${cp}|${ac}|${wp.message ?? ''}|${msg.timestamp}`;
}

function sigTeamInternal(msg: ChatMessage, isStreaming: boolean): string {
  const tools = (msg.toolCalls || []).map((tool) =>
    `${tool.toolCallId}|${tool.name}|${tool.args || ''}|${tool.result || ''}|${tool.status}|${tool.duration || ''}`,
  ).join(';');
  const artifacts = (msg.artifacts || []).map((artifact) =>
    `${artifact.artifact_id || artifact.id || ''}|${artifact.title || ''}|${artifact.path || ''}|${artifact.summary || ''}`,
  ).join(';');
  return `team|${msg.id}|${msg.content}|${msg.thinking || ''}|${tools}|${artifacts}|${msg.agentId || ''}|${msg.agentName || ''}|${msg.agentRole || ''}|${msg.agentTone || 0}|${msg.eventType || ''}|${msg.nodeId || ''}|${msg.displayMode || ''}|${msg.collapsedTitle || ''}|${msg.processText || ''}|${isStreaming ? '1' : '0'}`;
}

/** 一段 batch（同一回合的连续 agent 消息）的 sig。
 *
 *  关键：流式计时由独立 ticker 原地更新 label 和工具 duration，时间本身不能进入结构签名。
 *  否则每跨一个整秒，整个回合 DOM 都会被 replaceChild，导致 Timeline 闪烁、动画重启，
 *  以及 details 展开状态短暂跳变。
 *
 *  覆盖 renderAgentTurn 的结构输入：isStreaming / userPinnedOpen /
 *  batch 内每条消息的 role|content|thinking|toolCalls|planReview|todoSnapshot|turnFileChanges|
 *  streaming|agentName|model|timestamp。
 *  toolCall 逐项展开（name/args/result/status/startedAt/duration）确保工具状态变化触发 patch。
 *  planReview / todoSnapshot / turnFileChanges 同样展开，否则 todo_updated / plan_review /
 *  hydrate 剔除无效文件后字段变了但 sig 不变时会错误复用旧 DOM，造成 todo/文件卡不刷新。 */
function sigAgentTurn(
  batch: ChatMessage[],
  isStreaming: boolean,
  userPinnedOpen: boolean | null,
): string {
  const parts = batch.map((m) => {
    const tc = m.toolCalls
      ? m.toolCalls
          .map(
            (t) =>
              `${t.toolCallId}|${t.name}|${t.args ?? ''}|${t.result ?? ''}|${t.status}|${t.startedAt}|${t.duration ?? ''}`,
          )
          .join(';')
      : '';
    // \x1f（单元分隔符）在正文/计划文本中不会出现，避免分隔符碰撞导致 sig 误判相等、漏刷新卡片。
    const US = '\x1f';
    const pr = m.planReview
      ? `${m.planReview.status}|${m.planReview.planFile ?? ''}|${m.planReview.plan ?? ''}`
      : '';
    const ts = m.todoSnapshot
      ? m.todoSnapshot.map((t) => `${t.status}:${t.content ?? ''}`).join(US)
      : '';
    // hydrate 剔除幽灵路径后必须进 sig，否则增量渲染复用旧「已编辑」卡 DOM
    const tfc = m.turnFileChanges
      ? m.turnFileChanges
          .map((f) => `${f.path}|${f.added}|${f.removed}|${f.status}`)
          .join(US)
      : '';
    // Wiki 卡片同样进 sig，否则 wiki_cards patch 后增量渲染复用旧 DOM。
    const wc = m.wikiCards
      ? m.wikiCards.map((p) => `${p.id}|${p.title}`).join(US)
      : '';
    return `${m.id}|${m.role}|${m.content}|${m.thinking ?? ''}|${m.segmentRole ?? ''}|${tc}|${pr}|${ts}|${tfc}|${wc}|${m.streaming ? '1' : '0'}|${m.agentName ?? ''}|${m.model ?? ''}|${m.timestamp}`;
  });
  return `t|${isStreaming ? '1' : '0'}|${userPinnedOpen === null ? '_' : userPinnedOpen ? '1' : '0'}|${parts.join('||')}`;
}

/**
 * 外部会话才返回展示身份；内置 Crew 会话返回 undefined，确保沿用原渲染路径。
 */
function sessionTurnIdentity(sessionId: string | null): AgentTurnOptions['identity'] | undefined {
  if (!sessionId) return undefined;
  const display = getSessionAgentDisplay(sessionId);
  const provider = String(display?.agentLabel?.provider || '').trim().toLowerCase();
  if (!provider || provider === 'crew' || provider === 'builtin' || provider === 'client') return undefined;
  // Team 是会话容器，不是发言者。团队消息由 team_internal.agent_id 决定头像；
  // 首帧前的通用等待态沿用内置 Leader（Crew），聊天区不展示 Team Logo。
  if (provider === 'team') return undefined;
  const name = String(display?.agentLabel?.name || 'Agent').trim();
  const badge = String(display?.agentLabel?.display_badge || '?');
  return { kind: 'external', name, badge };
}

export function renderChat(): void {
  const welcomePanel = $('#welcome-panel');
  const chatPanel = $('#chat-panel');
  const containerId = 'chat-messages';
  const container = document.getElementById(containerId);
  if (!welcomePanel || !chatPanel || !container) return;
  const sessionId = state.activeSessionId;
  const allMessages = sessionId ? getMessages(sessionId) : [];
  const busy = sessionId ? isBusy(sessionId) : false;
  const queueHint = sessionId ? state.queueHints[sessionId] : '';
  const pendingFollowup = sessionId ? bookFor(sessionId).pendingFollowup : null;
  const turnIdentity = sessionTurnIdentity(sessionId);

  // 撤回修改中：隐藏 [editFromIdx..] 的内容（不删除），只渲染前面部分
  const editFrom = sessionId ? state.editFromIdx[sessionId] : undefined;
  const editing = editFrom != null;
  const messages = editing ? allMessages.slice(0, editFrom) : allMessages;

  // 编辑态 / 等待用户交互 / todo 面板存在时，即使消息为空也保留对话流页面，不切欢迎页。
  const showChat = messages.length > 0
    || busy
    || editing
    || Boolean(pendingFollowup);
  welcomePanel.hidden = showChat;
  chatPanel.hidden = !showChat;
  document.body.classList.toggle('welcome-active', state.activeTab === 'chat' && !showChat);

  resetAllChatRenderTargetsForSession(sessionId);
  const target = getChatRenderTarget(containerId);
  // 容器元素被重建（单测 body 重排 / 未来视图重挂）时，缓存的 wrapper 已脱离文档：
  // 连同 diff 缓存一并作废，否则单元会渲染进游离节点、容器永远空白。
  if (target.wrapper && target.wrapper.parentElement !== container) {
    target.wrapper = null;
    target.lastUnits = new Map();
    target.lastUnitMetas = [];
    target.scrollAnchorNode = null;
  }
  const { lastUnits } = target;
  let { wrapper: chatWrapper } = target;

  // ---- 构建本帧的 render-unit 列表（key + sig + build fn） ----
  // 一个「构建描述」= 纯元数据 + 一个延迟到 apply 时才调用的 build（避免 reuse 时白 build）。
  interface UnitPlan {
    meta: RenderUnit;
    build: () => HTMLElement | null;
  }
  const plans: UnitPlan[] = [];
  const pushPlan = (key: string, sig: string, build: () => HTMLElement | null): void => {
    plans.push({ meta: { key, sig }, build });
  };

  if (messages.length === 0 && !busy && !editing) {
    pushPlan('__empty', 'empty', () => renderEmptyState());
  } else if (messages.length === 0 && editing) {
    // 编辑态 + 空流：留白（提示条已在输入框上方）—— 不 push 任何单元
  } else {
    // 把连续的 agent 消息（assistant / status / error）合并成一个 .msg 块。
    let i = 0;
    // 末尾没有实际内容的 streaming assistant turn（Dynamic Kanban 里由首个 status/workflow 帧
    // 开出的空 anchor）本质上等同于「正在生成」的 typing 指示器。如果把它留在原位置，
    // 后续 workflow_progress / agent 角色卡片会追加在它下面，导致「转圈等待」被旧输出压在上方。
    // 这里把它识别出来、跳过原位置渲染，改为在所有消息之后以 __typing 单元渲染，确保它始终
    // 紧跟最新消息。
    let trailingEmptyTypingBatch: ChatMessage[] | null = null;
    while (i < messages.length) {
      const msg = messages[i];
      const isAgent = (msg.role === 'assistant' && !msg.agentName) || msg.role === 'error' || (msg.role === 'status' && !msg.agentName && !msg.workflowProgress);
      if (!isAgent) {
        // 用户消息 / 带 agentName 的 status 或 assistant（Dynamic Kanban 角色卡片/最终结果）/ workflow 进度面板：按 msg.id keyed。
        // sig 必须覆盖 renderMessageHtml 对该 role 实际依赖的字段：
        //  - user 分支：content/model/timestamp/attachments → sigUserMessage
        //  - agentName 分支：content/agentName/agentAvatar/timestamp/segmentRole → sigAgentRoleCard
        //    （sigUserMessage 不含 agentName，用它会导致角色卡片内容变化时不 patch → stale，故单独分流）
        //  - workflowProgress 分支：payload 全量 → sigWorkflowProgress
        let sig: string;
        if (msg.role === 'team_internal') {
          const member = resolveTeamCollaborationMember(sessionId, msg);
          const isPlanning = msg.eventType === 'team_planning_progress';
          const sessionTeamName = String(getSessionAgentDisplay(sessionId)?.agentLabel?.name || '').trim();
          const teamName = resolveTeamCollaborationName(sessionId) || sessionTeamName || '团队';
          const displayed = isPlanning
            ? {
                ...msg,
                agentName: teamName,
                agentRole: '',
                isLeader: false,
              }
            : member
            ? {
                ...msg,
                ...(member.agentId ? { agentId: member.agentId } : {}),
                agentName: member.name,
                agentRole: member.isLeader ? 'leader' : (msg.agentRole || member.role),
                agentTone: member.tone,
                isLeader: Boolean(member.isLeader || msg.isLeader),
              }
            : msg;
          // Team Turn 使用自身 streaming 生命周期，不借用 Session 全局 busy。
          // 这样新节点启动时不会“复活”已完成的成员回合。
          const isStreaming = msg.streaming === true;
          sig = sigTeamInternal(displayed, isStreaming);
          const captured = displayed;
          pushPlan(msg.id, sig, () => renderTeamInternalMessage(captured, isStreaming));
          i += 1;
          continue;
        } else if (msg.role === 'status' && msg.workflowProgress) {
          sig = sigWorkflowProgress(msg, state.configModel);
        } else if (msg.agentName) {
          sig = sigAgentRoleCard(msg, state.configModel);
        } else {
          sig = sigUserMessage(msg, state.configModel);
        }
        const captured = msg;
        pushPlan(msg.id, sig, () => renderMessageHtml(captured, state.configModel));
        i += 1;
        continue;
      }
      let j = i + 1;
      while (j < messages.length) {
        const r = messages[j].role;
        const hasAgent = messages[j].agentName;
        const hasWorkflowProgress = messages[j].workflowProgress;
        if (((r === 'assistant' && !hasAgent) || r === 'error' || (r === 'status' && !hasAgent && !hasWorkflowProgress))) j += 1;
        else break;
      }
      const batch = messages.slice(i, j);
      const isLastBatch = j >= messages.length;
      const isEmptyTyping =
        isLastBatch &&
        batch.length === 1 &&
        batch[0].role === 'assistant' &&
        batch[0].streaming &&
        !batch[0].content &&
        !batch[0].thinking &&
        !batch[0].toolCalls?.length &&
        !batch[0].planReview &&
        !batch[0].todoSnapshot &&
        !batch[0].wikiCards?.length;
      if (isEmptyTyping) {
        trailingEmptyTypingBatch = batch;
        i = j;
        continue;
      }
      const turnId = batch[0].id;
      // liveness = batch 内是否存在 streaming 消息（anchor 可能不在尾部：动态看板的 status/workflow
      // 帧常排在 assistant anchor 之后）。per-turn 自有信号，不引用 session 全局 busy——否则新回合让
      // session 重新 busy 时会复活所有已封口回合（停任务1再启任务2，两个回合都显示执行中）。
      const isStreaming = batch.some((m) => m.streaming === true);
      const isLastTurn = j >= messages.length;
      const isLiveTurn = isLastTurn && isStreaming;
      const turnDurationMs = resolveTurnDurationMs(batch, { isLive: isLiveTurn });
      // 正式正文硬确认后：清掉推理阶段的临时展开（不碰正文后的持久化展开）
      if (hasVisibleAnswerText(batch) && ephemeralUnfoldedTurns.has(turnId)) {
        ephemeralUnfoldedTurns.delete(turnId);
        state.userUnfoldedTurns.delete(turnId);
      }
      let userPinnedOpen: boolean | null = null;
      if (state.userUnfoldedTurns.has(turnId)) userPinnedOpen = true;
      else if (state.userFoldedTurns.has(turnId)) userPinnedOpen = false;
      // 无偏好时：有正式正文 → 折；尚无正文（推理/旁白）→ 展。见 renderAgentTurn。
      const identitySig = turnIdentity
        ? `${turnIdentity.kind}|${turnIdentity.name}|${turnIdentity.badge}`
        : 'crew';
      const sig = `${sigAgentTurn(batch, isStreaming, userPinnedOpen)}|${identitySig}`;
      const capturedBatch = batch;
      pushPlan(turnId, sig, () =>
        renderAgentTurn(capturedBatch, {
          isStreaming,
          userPinnedOpen,
          turnDurationMs,
          ...(turnIdentity ? { identity: turnIdentity } : {}),
        }),
      );
      i = j;
    }
    // 带 agentName 的 status（Dynamic Kanban 角色卡片）和 workflowProgress 面板走上面的 !isAgent 分支，
    // 分别由 sigAgentRoleCard / sigWorkflowProgress 提供 sig，避免 stale。
    if (queueHint && !(state.pendingQueues[sessionId ?? '']?.length)) {
      pushPlan('__queue', `q|${queueHint}`, () => renderQueueHintCard(queueHint));
    }
    const emptyTypingId = trailingEmptyTypingBatch?.[0]?.id;
    const hasVisibleAgentMessage = messages.some(
      (m) => m.id !== emptyTypingId && (m.role === 'assistant' || m.role === 'status' || m.role === 'error' || m.role === 'team_internal'),
    );
    if (busy && (trailingEmptyTypingBatch || !hasVisibleAgentMessage)) {
      const identitySig = turnIdentity
        ? `${turnIdentity.kind}|${turnIdentity.name}|${turnIdentity.badge}`
        : 'crew';
      pushPlan('__typing', `typing|${identitySig}`, () => renderTypingIndicator(turnIdentity));
    }
  }
  if (pendingFollowup && sessionId) {
    pushPlan('__followup', `f|${pendingFollowup.questionId}`, () =>
      renderFollowupCardElement(pendingFollowup),
    );
  }
  // __anchor：scroll-anchor 永远是最后一个单元，sig 恒定 → 跨帧复用同一节点
  pushPlan('__anchor', 'anchor', () => getScrollAnchor(target));

  // ---- diff + apply ----
  const nextMetas = plans.map((p) => p.meta);
  const ops = diffRenderUnits(target.lastUnitMetas, nextMetas);

  // 持久 wrapper：首次 / 会话切换时新建，之后跨帧复用
  if (!chatWrapper) {
    chatWrapper = document.createElement('div');
    chatWrapper.className = 'messages__inner';
    container.replaceChildren(chatWrapper);
  }
  const wrapper = chatWrapper;

  const buildByPlan = new Map<string, () => HTMLElement | null>();
  for (const p of plans) buildByPlan.set(p.meta.key, p.build);

  // 1) 处理 remove：从 Map 删 + 节点移除（anchor 复用，不删）
  for (const op of ops) {
    if (op.type !== 'remove') continue;
    const node = lastUnits.get(op.key);
    if (node) {
      if (op.key !== '__anchor') node.remove();
      lastUnits.delete(op.key);
    }
  }

  // 2) 处理 patch：重建该单元节点（调 build → 若 present 则替换旧节点；若 absent 则视作 remove）
  for (const op of ops) {
    if (op.type !== 'patch') continue;
    const build = buildByPlan.get(op.key)!;
    const fresh = build();
    const old = lastUnits.get(op.key);
    if (isPresent(fresh)) {
      if (old && old.parentNode === wrapper) wrapper.replaceChild(fresh, old);
      lastUnits.set(op.key, fresh);
    } else {
      // build 产出 null/data-empty → 该单元缺席：移除旧节点并清出 Map
      if (old) {
        if (op.key !== '__anchor') old.remove();
        lastUnits.delete(op.key);
      }
    }
  }

  // 3) 处理 append：新建节点进 Map（present 才进；absent 则跳过，留个 absent 标记）
  const absentKeys = new Set<string>();
  for (const op of ops) {
    if (op.type !== 'append') continue;
    const build = buildByPlan.get(op.key)!;
    const fresh = build();
    if (isPresent(fresh)) {
      lastUnits.set(op.key, fresh);
    } else {
      absentKeys.add(op.key);
    }
  }

  // 4) 强制 DOM 顺序 == next 顺序：按 nextMetas 顺序对每个 present 单元 appendChild。
  //    appendChild 一个已挂载节点 = 移动它（cheap）；新建节点首次挂载也是 appendChild。
  //    缺席单元（absent / 不在 Map）跳过 —— 它们本就不该出现在 DOM 里。
  //    这一步同时把 anchor 排到最后（它是 nextMetas 最后一项）。
  for (const meta of nextMetas) {
    if (absentKeys.has(meta.key)) continue;
    const node = lastUnits.get(meta.key);
    if (node) wrapper.appendChild(node);
  }

  // ---- 渲染后的幂等副作用 ----
  ensureFoldDelegation(container);
  ensureFileChangesDelegation(container);
  // 代码块复制按钮：patch/append 后新节点需重新绑定（幂等，旧节点跳过）。
  attachCopyButtons(container);
  // Mermaid 图表：懒加载 mermaid.js 渲染 [data-mermaid] 占位。幂等，已渲染的跳过。
  // 不 await：渲染是异步的，不阻塞 DOM 布局；失败时保留源码占位，下次 patch 重试。
  void renderMermaidBlocks(container);
  bindFollowupCard(container, {
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
        // Permission is a side-channel decision inside the running tool call.
        // Preserve assistantId/toolMap so the same turn resumes in place.
        patchBook(sid, { pendingFollowup: null });
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
  });
  renderQueueSlot();
  renderTodoSlot();
  updateComposerControls();
  syncRunningIntroSlot();
  scrollChatToBottom();
  // 流式每帧只刷 tab 角标，不重建 inspector body（否则计划 MD / 文件 diff 滚动会被 innerHTML 冲回顶部）。
  if (isInspectorOpen()) refreshInspectorChrome();
  window.dispatchEvent(new CustomEvent('messages:changed', { detail: { sessionId } }));

  syncTurnDurationTicker();

  // 5) 记账：本帧的元数据成为下一帧的 prev
  target.lastUnitMetas = nextMetas;
  target.wrapper = chatWrapper;
}

// ---------- 迟到分片排队 ----------

// loadBackendHistory 期间到达的分片排队，history 写回后再 flush，
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
  // ponytail: 只更新预览/时间，不动 title。标题归摘要帧 applySessionTitle（titleFromSummary=true）、
  // 首条 commitDraftSession、或用户 rename。这里若再写 title=makeSessionTitle(text)，已有会话每发一条
  // 消息都会把标题盖成最新输入的截断——即"标题变成用户最后一条输入"。预览才是每条更新的对象。
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
  // 回合封口时清理该回合的 delta 重组缓冲（assistantId 每回合唯一，不影响后续回合）。
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

// applyChunk 是 dispatch + apply + 副作用的薄适配层。
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
    const body = (chunk.body ?? {}) as { platform?: string; event?: string; query?: string };
    // 渠道会话开始/结束时都确保前端已订阅：开始订阅后才能收到实时 delta，
    // 结束订阅后也能通过 replay 补到可能错过的帧。
    subscribeSessions([sid]);
    if (body.event === 'agent:start') {
      // 渠道入站的用户消息不作为 WS 帧广播，先把原文补插到本地，
      // 否则实时流出的回答会直接接在上一轮尾部，看起来像「串轮」。
      // 去重：桌面本地发送的渠道会话消息已有乐观用户消息，不重复补插。
      // 注意本地发送时尾部是乐观 assistant 占位，要向前找最后一条 user 消息比较。
      const query = typeof body.query === 'string' ? body.query.trim() : '';
      if (query) {
        const msgs = getMessages(sid);
        const lastUser = [...msgs].reverse().find((m) => m.role === 'user');
        const alreadyLocal = !!lastUser
          && (lastUser.content === query || lastUser.content.startsWith(query));
        if (!alreadyLocal) {
          appendSessionMessage(sid, {
            id: newMessageId('user'),
            role: 'user',
            content: query,
            timestamp: Date.now(),
          });
        }
      }
      // 渠道外部发起的回合：本地 book 处于封口状态，turn gate 会把实时帧全部丢弃。
      // 立即开门（挂 process 占位 + acceptingNewRequest），让首帧绑定 request_id 实时渲染。
      resumeSessionGeneration(sid);
      renderChat();
    }
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
    void import('./audit-page').then(({ onAuditDataChanged }) => onAuditDataChanged());
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
  touchStreamActivity(sid);
  // history 正在加载：排队，等 loadBackendHistory 写回后统一 flush，防止被全量替换覆盖。
  // 必须先排队再记 gateway_sequence：否则 flush 重新 applyChunk 时会被判重误丢，
  // 且序号水位已推进、重连 replay 也补不回来。
  if (historyLoading.has(sid)) {
    enqueuePendingChunk(sid, chunk);
    return;
  }
  if (isDuplicateGatewayChunk(sid, chunk)) {
    return;
  }
  noteGatewaySequence(sid, chunk);

  const parsed = normalizeChunk(chunk);
  if (!parsed) {
    logStream('apply-chunk', 'drop-unrecognized', { sid, kind: chunk.kind });
    return;
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
    renderQueueSlot();
    renderWorkspaceHistory(openSessionFn);
    syncTurnDurationTicker();
    return;
  }

  // Dynamic Kanban 看板：只在回合 gate 接收后刷新，避免旧 request 的迟到帧触发 UI 副作用。
  // 使用 per-session 动态看板状态判断，避免全局 state.mode 与后台会话不一致导致漏刷新。
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

  // delta 按 gateway_sequence 重组，避免依赖网络到达顺序累积正文。
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

  // 状态 hint / busy：仅由 reducer 的 statusHint 与等待用户交互的 kind 驱动，禁止「收到任意 chunk → busy」。
  if (typeof result.queueHint === 'string') setQueueHintWithUi(sid, result.queueHint);
  if (USER_WAIT_CHUNK_KINDS.has(parsed.kind)) {
    finalizeStreamingTurn(sid);
    setQueueHintWithUi(sid, '');
  }
  const busyNext = resolveBusyTransition(parsed.kind, result.statusHint, bookFor(sid).turnSealed);
  if (busyNext !== null) setBusyWithUi(sid, busyNext);
  if (typeof result.statusHint === 'string') setStatusWithUi(sid, result.statusHint);

  // final / error：触发 finalize + usage + 全量重渲染
  if (result.finalize) {
    logStream('apply-chunk', 'finalize-turn', { sid, kind: parsed.kind });
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

/** 把 reducer 输出的 MessageUpsert 应用到 messageStore。 */
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
  void dispatchWs(sessionId, head.query, head.attachments ?? [], dispatchOptions);
}

export function sendQueueItemNow(sessionId: string, id: string): void {
  const queue = getPendingQueue(sessionId);
  const index = queue.findIndex((item) => item.id === id);
  if (index < 0 || isBusy(sessionId)) return;
  const item = queue[index]!;
  removePendingQueueItem(sessionId, index);
  setQueueHintWithUi(sessionId, '');
  void dispatchWs(sessionId, item.query, item.attachments ?? [], item.subScenario ?? '', item.planActive);
  renderQueueSlot();
}

export function editQueueItem(sessionId: string, index: number): void {
  const item = getPendingQueue(sessionId)[index];
  if (!item) return;
  // 回填输入框供用户修改：从队列移除，下一次发送按新的提交时间追加到队尾。
  queueEditDraft = { sessionId };
  removePendingQueueItem(sessionId, index);
  if (getPendingQueue(sessionId).length === 0) setQueueHintWithUi(sessionId, '');
  const input = $('#chat-input') as HTMLTextAreaElement | null;
  if (input) {
    input.value = item.query;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.focus();
  }
  renderQueueSlot();
}

export async function dispatchWs(
  sessionId: string,
  query: string,
  attachments = takeAttachmentsForSend(),
  subScenarioOrOptions: string | DispatchOptions = '',
  planActiveOverride?: boolean,
): Promise<void> {
  const dispatchOptions: DispatchOptions = typeof subScenarioOrOptions === 'string'
    ? {
        subScenario: subScenarioOrOptions,
        ...(planActiveOverride !== undefined ? { planActive: planActiveOverride } : {}),
      }
    : subScenarioOrOptions;
  const subScenario = dispatchOptions.subScenario ?? '';
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
    return;
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
  // 用户提交新消息：强制跳到底部（重置 stickyBottom）。采用 的 runStart → jumpToBottom。
  jumpChatToBottom();
  const planActiveForSend = dispatchOptions.planActive ?? state.composerMode === 'plan';
  // Plan 模式：发送实际 query 之前先发一帧 plan_enter。
  // 注意：openTurn 必须在 plan_enter 之后——plan 控制 status 会把 busy 置 idle，
  // 若先开乐观回合再 plan_enter，会把刚置起的 busy/计时打回 idle。
  await ensurePlanModeForSession(sessionId, planActiveForSend);
  // 外援 Team 路由已写入 session_agent_config；WS payload 再带上 id，
  // 便于网关直连场景。
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
  logStream('dispatch', 'send-ws', {
    sessionId,
    requestId,
    mode,
    queryLen: query.length,
    backendConnected: state.backendConnected,
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
  });
  if (!ok) {
    logStream('dispatch', 'send-ws-failed', { sessionId, requestId });
    discardEmptyOptimisticAssistant(sessionId);
    appendMessage(sessionId, 'error', '服务未连接，请稍后重试。');
    setBusyWithUi(sessionId, false);
    setStatusWithUi(sessionId, 'idle');
    renderChat();
    syncTurnDurationTicker();
    return;
  }
  subscribeSessions([sessionId]);
  logStream('dispatch', 'local-state-ready', { sessionId, requestId, busy: true });
  renderChat();
  syncTurnDurationTicker();
}

export function sendMessage(text: string): void {
  const content = text.trim();
  const attachments = state.attachments;
  if (!content && attachments.length === 0) return;

  // 无活跃会话（欢迎页）时新建草稿：用 composerWorkspaceId()（即右下角选择器所显示的空间），
  // 而不是裸读 state.currentWorkspaceId——避免二者不一致时消息落到选择器之外的空间。
  const sessionId = state.activeSessionId ?? createSessionInWorkspace(composerWorkspaceId(), openSessionFn);
  const previewText = content || '附件消息';

  if (isBusy(sessionId)) {
    const pendingItem = {
      id: newMessageId('q'),
      query: content || '(附件)',
      attachments: [...state.attachments],
      subScenario: takeArmedSubScenario(),
      planActive: state.composerMode === 'plan',
    };
    if (queueEditDraft?.sessionId === sessionId) {
      queueEditDraft = null;
    }
    enqueuePending(sessionId, pendingItem);
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
    renderChat();
    setTabFn('chat');
    return;
  }

  if (queueEditDraft?.sessionId === sessionId) {
    queueEditDraft = null;
  }

  void (async () => {
    await dispatchWs(sessionId, content, takeAttachmentsForSend(), takeArmedSubScenario());
    clearScenarioChip();
    if (isDraftSession(sessionId)) {
      commitDraftSession(sessionId, makeSessionTitle(previewText), previewText.slice(0, 48), openSessionFn);
    } else {
      updateSessionPreview(sessionId, previewText);
    }
    renderWorkspaceHistory(openSessionFn);
    syncComposerWorkspaceLabel();
    setTabFn('chat');
    renderChat();
  })();
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
  // Team 仅追加自己的结算分支；上面单 Agent/Dynamic Kanban 的原循环不改。
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
  // 用户主动停止是终态（每会话仅一个活跃回合），清理该会话 delta 重组缓冲。
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
  renderQueueSlot();
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
  //    必须通过 store 不可变写回，触发 messageStore 订阅并避免与并发
  //    patchMessage 互相覆盖。
  const frozen = list.map((m, k) => (k >= idx && m.streaming ? { ...m, streaming: false } : m));
  messageStore.set({ messages: { ...messageStore.get().messages, [sessionId]: frozen } });
  // 3. 复位记账：assistantId 指向正在生成的消息，必须清掉，否则下一轮的回复会 patch 到它上面
  resetBook(sessionId);
  // 4. 屏蔽中断后可能迟到的流式分片，直到重新发送（dispatchWs 会清掉）
  addSuppressedSession(sessionId);
  // 5. 标记编辑起点：渲染时隐藏 [idx..] 的内容，但不删除
  setEditFrom(sessionId, idx);

  // 6. 回填输入框
  const input = $('#chat-input') as HTMLTextAreaElement | null;
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
  const input = $('#chat-input') as HTMLTextAreaElement | null;
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
    renderQueueSlot();
    return;
  }
  const sent = state.socket?.interrupt(sessionId);
  if (!sent) {
    notify('服务未连接，已保留为下一条待发消息');
    renderQueueSlot();
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
  renderQueueSlot();
}

export function steerGeneration(text: string): void {
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
  renderQueueSlot();
}

export function subscribeSessions(sessionIds: string[]): void {
  const sessions = addSubscribedSessions(sessionIds);
  void state.socket?.subscribe(sessions, getLastGatewaySequences(sessions));
}

export async function refreshSessions(): Promise<void> {
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
