/**
 * 对话渲染本体（从 chat-controller.renderChat 抽出）：unit-plan 构建 + X3b 增量 diff apply。
 *
 * 主对话（chat-controller.renderChat）与嵌入面板（conversation-panel，Wiki 问答）
 * 共用同一套渲染本体；面板差异（空态、followup 提交/取消、渲染后钩子）经 hooks 注入。
 *
 * scroll anchor 按 containerId 多实例管理（Map）：每个对话容器持有独立的
 * stickyBottom 状态，主对话与 Wiki 面板流式追底/上滑 disarm 互不干扰。
 *
 * X3b 增量 diff 的行为等价性论证见 chat-controller 原注释（build-followup-x3b.md）：
 * sig 保守覆盖 render* 全部输入，reuse + patch + append 与全量重建可见 DOM 等价。
 */

import {
  type AgentTurnOptions,
  type ChatMessage,
  hasVisibleAnswerText,
  renderAgentTurn,
  renderEmptyState,
  renderHistoryErrorCard,
  renderMessageHtml,
  renderQueueHintCard,
  renderTeamInternalMessage,
  renderTypingIndicator,
  renderUnitErrorCard,
  resolveTurnDurationMs,
} from '../chat-render';
import { reportRendererError, safeStringify } from '../renderer-error-report';
import { diffRenderUnits } from '../chat-diff';
import { startsAnotherOutcomeTurn, userTurnOutcomeMarker } from '../turn-outcome';
import { setRuntimeStyle } from '../components/runtime-style';
import { ConversationViewport, type ConversationViewportAnchor, type ConversationViewportRow } from './conversation-viewport';
import { firstChangedMessageIndex } from '../conversation-message-changes';
import { attachScrollAnchor, type ScrollAnchor } from './scroll-anchor';
import { getSessionStreamRecovery } from './session-stream-recovery';
import { getSessionAgentDisplay } from './workspaces';
import { isExternalAgentSession, isExternalTeamSession } from './external-agents-feature';
import {
  resolveTeamCollaborationMember,
  resolveTeamCollaborationMentionText,
  resolveTeamCollaborationName,
  teamTurnDisplayKey,
} from './team-collaboration-board';
import { disposeConversationDom, syncConversationDom } from './conversation-dom-index';
import { interactiveOwnerKey } from './interactive-owner';
import { applyFoldState } from '../render-utils';
import { setToolFold, setTurnFold } from './fold-state';
import {
  bindFollowupCard,
  isRuntimeStaffingFollowup,
  renderFollowupCardElement,
  disposePermissionCardPosition,
  syncPermissionCardPositions,
} from '../followup';
import type { FollowupAnswer } from '../backend-client';
import { attachCopyButtons } from './copy-button';
import { renderMermaidBlocks } from './mermaid-render';
import { inlineSurfaces } from './inline-surfaces';
import { disposeNativeVisuals, hydrateNativeVisuals, renderNativeVisual } from './native-visual';
import { showFileOpenMenu } from './file-open-menu';
import { mountBlueprintSurface } from './blueprint-surface';
import { openBrowserArtifact, openUserBrowser } from './browser-panel';
import { openBrowserWorkbench, openInspectorToTab } from './inspector';
import { htmlArtifactPathFromHref, httpUrlFromHref } from '../artifact-links';
import {
  ensureSessionBook,
  ensureSessionMessages,
  isBusySession,
  notify,
  state,
} from '../state';
import { openWikiPageByTitle } from './wiki-page';

// ---------- scroll anchor：按容器多实例（原 chat-controller 单例的多实例化） ----------

const conversationScrollAnchors = new Map<string, { container: HTMLElement; anchor: ScrollAnchor }>();

const noOpScrollAnchor: ScrollAnchor = {
  jumpToBottom: () => {},
  pinToBottomIfSticky: () => {},
  isStickyBottom: () => true,
  restoreReadingTop: () => {},
  disarm: () => {},
  dispose: () => {},
};

/** 拿到指定容器的 scroll anchor（懒创建）；容器元素被替换时 dispose 旧的、建新的。 */
export function getConversationScrollAnchor(containerId: string): ScrollAnchor {
  const container = document.getElementById(containerId);
  if (!container) {
    // 容器还没挂载：返回一个 no-op anchor，避免外部炸。
    return noOpScrollAnchor;
  }
  const cached = conversationScrollAnchors.get(containerId);
  if (cached && cached.container === container) return cached.anchor;
  cached?.anchor.dispose();
  const anchor = attachScrollAnchor(container);
  conversationScrollAnchors.set(containerId, { container, anchor });
  return anchor;
}

/** 用户提交新消息 / 切会话 / 空→非空 时调用：强制跳到底部并重置 sticky。 */
export function jumpConversationToBottom(containerId: string): void {
  const requestedTarget = renderTargets.get(containerId);
  requestAnimationFrame(() => {
    if (requestedTarget && renderTargets.get(containerId) !== requestedTarget) return;
    const anchor = getConversationScrollAnchor(containerId);
    anchor.jumpToBottom();
    const target = renderTargets.get(containerId);
    const container = target?.wrapper?.parentElement;
    if (target && container && target.viewport.enabled) {
      target.pendingRestore = undefined;
      applyConversationWindow(container, containerId, target);
      anchor.pinToBottomIfSticky();
    }
  });
}

// ---------- 事件委托（一次性绑定，按容器） ----------

/** 「已编辑文件」卡：打开看板 Files / 在资源管理器中显示（与 fold 委托同容器、各绑一次）。 */
const fileChangesBoundContainers = new WeakSet<HTMLElement>();

/** 「历史加载失败」卡的重试按钮（各容器绑一次；动态 import 破除与 session-controller 的循环）。 */
const historyRetryBoundContainers = new WeakSet<HTMLElement>();
function ensureHistoryRetryDelegation(
  container: HTMLElement,
  getSessionId: () => string | null,
): void {
  if (historyRetryBoundContainers.has(container)) return;
  historyRetryBoundContainers.add(container);
  container.addEventListener('click', (event) => {
    const target = event.target instanceof Element ? event.target : null;
    const btn = target?.closest<HTMLElement>('[data-history-retry]');
    if (!btn || !container.contains(btn)) return;
    const sid = getSessionId();
    if (!sid) return;
    void import('./session-controller').then(({ loadBackendHistory }) => loadBackendHistory(sid));
  });
}
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
    const inspirationBtn = target.closest<HTMLElement>('[data-inspiration-surface]');
    if (inspirationBtn && container.contains(inspirationBtn)) {
      event.preventDefault();
      try {
        const surface = JSON.parse(inspirationBtn.dataset.inspirationSurface || '{}') as {
          mode?: string; siteId?: string; inspirationId?: string; sessionId?: string;
        };
        if (surface.mode === 'site') {
          window.dispatchEvent(new CustomEvent('inspiration:site-surface', {
            detail: {
              siteId: surface.siteId || surface.inspirationId || '',
              sessionId: surface.sessionId || state.activeSessionId || '',
            },
          }));
        } else if (surface.mode === 'canvas' || surface.mode === 'widget') {
          void mountBlueprintSurface(surface as Parameters<typeof mountBlueprintSurface>[0]);
        }
      } catch { notify('无法打开这个灵感预览'); }
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
    const wikiLink = target.closest<HTMLElement>('[data-rel-title]');
    if (wikiLink && container.contains(wikiLink)) {
      event.preventDefault();
      const title = wikiLink.getAttribute('data-rel-title') || '';
      if (title) void openWikiPageByTitle(title);
      return;
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
              }));
          }
        } catch {
          fileChanges = null;
        }
      }
      openInspectorToTab('files', { expandFilePath: expandPath, filePaths, fileChanges: fileChanges ?? null });
    }
  });
}

type TeamCommunicationActionHandlers = {
  onRetry: (message: ChatMessage) => void;
  onCancel: (message: ChatMessage) => void;
};

const teamCommunicationBoundContainers = new WeakMap<HTMLElement, {
  handlers: TeamCommunicationActionHandlers;
  messages: () => ChatMessage[];
}>();

function ensureTeamCommunicationDelegation(
  container: HTMLElement,
  handlers: TeamCommunicationActionHandlers | undefined,
  messages: () => ChatMessage[],
): void {
  const existing = teamCommunicationBoundContainers.get(container);
  if (existing) {
    existing.handlers = handlers ?? { onRetry: () => {}, onCancel: () => {} };
    existing.messages = messages;
    return;
  }
  const binding = {
    handlers: handlers ?? { onRetry: () => {}, onCancel: () => {} },
    messages,
  };
  teamCommunicationBoundContainers.set(container, binding);
  container.addEventListener('click', (event) => {
    const target = event.target instanceof Element
      ? event.target.closest<HTMLElement>('[data-team-communication-action]')
      : null;
    if (!target || !container.contains(target)) return;
    const root = target.closest<HTMLElement>('.msg[data-message-id]');
    const messageId = root?.dataset.messageId;
    const message = messageId
      ? binding.messages().find((item) => item.id === messageId)
      : undefined;
    if (!message) return;
    event.preventDefault();
    event.stopPropagation();
    target.setAttribute('disabled', 'true');
    if (target.dataset.teamCommunicationAction === 'retry') binding.handlers.onRetry(message);
    if (target.dataset.teamCommunicationAction === 'cancel') binding.handlers.onCancel(message);
  });
}

/** 一次性事件委托：在消息容器上 capture 监听 toggle（toggle 不冒泡）。
 *  工作室与对话页各有一个容器，分别绑定一次。
 *  覆盖两类折叠：
 *   - 回合级 `.msg__foldable`：写 state.userFoldedTurns/userUnfoldedTurns（现有路径）+ fold-state.ts 持久化。
 *   - 时间线工具项 `.process-timeline__details`：直接写 fold-state.ts（data-fold-key 由 renderToolCard 写入；
 *     无 data-fold-key 的项（如思考）不持久化）。 */
const foldBoundContainers = new WeakSet<HTMLElement>();
/**
 * 推理阶段（尚无硬确认正文）用户手动展开的 turnId。
 * 正式正文到来时清除，使过程区仍自动折；不写入 localStorage。
 * 正文出现后的手动展开走 setTurnFold 持久化，不受此集合影响。
 */
const ephemeralUnfoldedTurns = new Set<string>();

function ensureFoldDelegation(container: HTMLElement, getSessionId: () => string | null): void {
  if (foldBoundContainers.has(container)) return;
  foldBoundContainers.add(container);

  const markUserFoldIntent = (target: EventTarget | null): void => {
    const el = target instanceof Element
      ? target.closest<HTMLDetailsElement>('details.msg__foldable, details.process-timeline__details, details.rich-reply__history')
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
        const sid = getSessionId();
        const msgs = sid ? ensureSessionMessages(sid) : [];
        // 找到该 turn 的 batch，判断是否已有硬确认正文
        let batchStart = msgs.findIndex((m) => m.id === turnId);
        if (batchStart < 0) batchStart = 0;
        let batchEnd = batchStart + 1;
        let requestId = msgs[batchStart]?.requestId;
        while (batchEnd < msgs.length) {
          const next = msgs[batchEnd];
          if (startsAnotherOutcomeTurn(requestId, next, msgs[batchEnd - 1])) break;
          const { role, agentName, workflowProgress } = next;
          if ((role === 'assistant' && !agentName) || role === 'error' || (role === 'status' && !agentName && !workflowProgress)) {
            requestId ??= next.requestId;
            batchEnd += 1;
          } else break;
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
        if (sid) invalidateConversationFold(sid, turnId);
        return;
      }
      // 时间线工具项折叠（无 data-fold-key 的项不持久化）
      const toolDetails = e.target as HTMLDetailsElement | null;
      if (toolDetails && toolDetails.matches('details.process-timeline__details, details.rich-reply__history')) {
        const userInitiated = toolDetails.dataset.userFoldIntent === '1';
        delete toolDetails.dataset.userFoldIntent;
        if (!userInitiated) return;
        const foldKey = toolDetails.getAttribute('data-fold-key');
        if (foldKey) setToolFold(foldKey, toolDetails.open);
        else rememberTransientDetail(container, toolDetails);
      }
    },
    true,
  );
}

// ---------- render target（X3b：keyed 增量 diff 渲染，按容器 id 分键） ----------

/**
 * 模块级缓存：按容器 id 维护独立 ChatRenderTarget（主对话 / 工作室 / Wiki 面板多轨）。
 * 多面板共存时各自的 diff 缓存与 scroll anchor 互不干扰。
 */
interface UnitPlan extends ConversationViewportRow {
  build: () => HTMLElement | null;
}

interface ProjectedSpan {
  start: number;
  planStart: number;
  hasVisibleAgent: boolean;
  emptyTyping: boolean;
  teamRows: boolean;
}

interface ChatRenderTarget {
  wrapper: HTMLElement | null;
  lastUnits: Map<string, HTMLElement>;
  mounted: UnitPlan[];
  viewport: ConversationViewport<UnitPlan>;
  spans: ProjectedSpan[];
  extras: UnitPlan[];
  spacers: Map<string, HTMLElement>;
  transientDetails: Map<string, { sig: string; open: Map<number, boolean> }>;
  followupHandlers: ConversationRenderHooks['followupHandlers'];
  stopViewport: (() => void) | null;
  viewportObserver: ResizeObserver | null;
  viewportFrame: number | null;
  cancelViewportFrame: (() => void) | null;
  restoredViewportTop: number | undefined;
  scheduleViewport: ((preferred?: ConversationViewportAnchor) => void) | null;
  readingAnchor: ConversationViewportAnchor | undefined;
  pendingPrepend: ConversationViewportAnchor | undefined;
  pendingRestore: ConversationViewportAnchor | undefined;
  source: ChatMessage[] | null;
  visibleLength: number;
  displayKey: string;
  folded: Set<string> | null;
  unfolded: Set<string> | null;
  invalidFrom: number;
  teamRows: boolean;
  teamBusy: boolean;
  latestVisualRevisions: Map<string, number>;
  ownerKey: string;
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
      mounted: [],
      viewport: new ConversationViewport(),
      spans: [],
      extras: [],
      spacers: new Map(),
      transientDetails: new Map(),
      followupHandlers: undefined,
      stopViewport: null,
      viewportObserver: null,
      viewportFrame: null,
      cancelViewportFrame: null,
      restoredViewportTop: undefined,
      scheduleViewport: null,
      readingAnchor: undefined,
      pendingPrepend: undefined,
      pendingRestore: undefined,
      source: null,
      visibleLength: 0,
      displayKey: '',
      folded: null,
      unfolded: null,
      invalidFrom: Infinity,
      teamRows: false,
      teamBusy: false,
      latestVisualRevisions: new Map(),
      ownerKey: '',
      lastSessionId: null,
      scrollAnchorNode: null,
    };
    renderTargets.set(containerId, target);
  }
  return target;
}

/** Fold state changes can arrive without replacing the message array. */
export function invalidateConversationFold(sessionId: string, turnId: string): void {
  for (const target of renderTargets.values()) {
    if (target.lastSessionId !== sessionId || !target.source) continue;
    const index = target.source.findIndex((message) => message.id === turnId);
    if (index >= 0) target.invalidFrom = Math.min(target.invalidFrom, index);
  }
}

function reusableSpanCount(spans: ProjectedSpan[], changed: number): number {
  // The preceding span may now merge with a new assistant/error/status item.
  let low = 0;
  let high = spans.length;
  while (low < high) {
    const middle = (low + high) >>> 1;
    if (spans[middle].start < changed) low = middle + 1;
    else high = middle;
  }
  return Math.max(0, low - 1);
}

function changedWidgetResults(
  previous: ChatMessage[], previousLength: number, next: ChatMessage[], nextLength: number, start: number,
): boolean {
  // A new revision changes the read-only state of all older matching widgets.
  const inputs = (messages: ChatMessage[], end: number): string[] => {
    const results: string[] = [];
    for (let index = start; index < end; index += 1) {
      const tools = messages[index].toolCalls;
      if (!Array.isArray(tools)) continue;
      for (const tool of tools) {
        if (tool?.name === 'Widget' && tool.result) results.push(tool.toolCallId, tool.result);
      }
    }
    return results;
  };
  const before = inputs(previous, previousLength);
  const after = inputs(next, nextLength);
  return before.length !== after.length || before.some((value, index) => value !== after[index]);
}

/** 给定 build() 产物：null 或 data-empty 占位视为「缺席」（不进 DOM、不占 Map 槽位）。
 *  与 X3a 的 appendRendered 过滤语义一致（build 返回 null 的单元不进 DOM；
 *  renderAgentTurn 空批次返回 data-empty div）。 */
function isPresent(node: HTMLElement | null): node is HTMLElement {
  if (!node) return false;
  if (node.dataset.empty === 'true') return false;
  return true;
}

function markSettledHistory(node: HTMLElement): void {
  if (node.dataset.streaming === 'true') return;
  if (node.matches('.msg.user, .msg--agent-turn')
    && !node.querySelector('.interaction-card, [data-plan-session], .msg__artifact-card')) {
    node.dataset.chatHistoryRow = node.classList.contains('user') ? 'user' : 'agent';
  }
}

/** 稳定的 scroll-anchor 节点：sig 恒定，跨帧复用同一个 div（避免每帧新建）。 */
function getScrollAnchorNode(target: ChatRenderTarget, containerId: string): HTMLDivElement {
  if (!target.scrollAnchorNode) {
    const div = document.createElement('div');
    div.id = containerId === 'studio-chat-messages'
      ? 'studio-chat-scroll-anchor'
      : containerId === 'chat-messages'
        ? 'chat-scroll-anchor'
        : `${containerId}-scroll-anchor`;
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
    `${tool.toolCallId}|${tool.name}|${tool.args || ''}|${tool.result || ''}|${tool.status}|${tool.duration || ''}|${JSON.stringify(tool.terminalProgress ?? null)}`,
  ).join(';');
  const artifacts = (msg.artifacts || []).map((artifact) =>
    `${artifact.artifact_id || artifact.id || ''}|${artifact.title || ''}|${artifact.path || ''}|${artifact.summary || ''}`,
  ).join(';');
  const durationBucket = Math.floor(Math.max(0, msg.turnDurationMs || 0) / 1000);
  const files = (msg.turnFileChanges || []).map((file) =>
    `${file.path}|${file.status}|${file.added}|${file.removed}|${file.binary ? '1' : '0'}`,
  ).join(';');
  return `team|${msg.id}|${msg.content}|${msg.thinking || ''}|${msg.thinkingStartedAt ?? ''}|${msg.thinkingDurationMs ?? ''}|${tools}|${artifacts}|${files}|${msg.agentId || ''}|${msg.agentName || ''}|${msg.agentRole || ''}|${msg.agentTone || 0}|${msg.eventType || ''}|${msg.nodeId || ''}|${msg.displayMode || ''}|${msg.collapsedTitle || ''}|${msg.processText || ''}|${msg.communicationKind || ''}|${msg.communicationStatus || ''}|${msg.requestId || ''}|${msg.replyTo || ''}|${msg.communicationRequestText || ''}|${durationBucket}|${isStreaming ? '1' : '0'}`;
}

/** 一段 batch（同一回合的连续 agent 消息）的 sig。
 *
 *  关键：流式计时由 updateActiveToolDurations() 原地更新，不能让纯时间变化重建整个回合。
 *  流结束时 streaming/tool 状态本身会改变 sig，届时再渲染最终时长。
 *
 *  覆盖 renderAgentTurn 的全部输入：isStreaming / userPinnedOpen / turnDurationMs 桶 /
 *  batch 内每条消息的 role|content|thinking|thinkingStartedAt|thinkingDurationMs|
 *  toolCalls|planReview|todoSnapshot|turnFileChanges|
 *  streaming|agentName|model|timestamp。
 *  toolCall 逐项展开（name/args/result/status/startedAt/duration）确保工具状态变化触发 patch。
 *  planReview / todoSnapshot / turnFileChanges 同样展开，否则 todo_updated / plan_review /
 *  hydrate 剔幽灵文件后字段变了但 sig 不变，X3b 会复用旧 DOM，造成 todo/文件卡不刷新。 */
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
              `${t.toolCallId}|${t.name}|${t.args ?? ''}|${t.result ?? ''}|${t.status}|${t.startedAt}|${t.duration ?? ''}|${JSON.stringify(t.terminalProgress ?? null)}`,
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
    // Wiki 卡片同样进 sig，否则 wiki.cards patch 后增量渲染复用旧 DOM。
    const wc = m.wikiCards
      ? m.wikiCards.map((p) => `${p.id}|${p.title}`).join(US)
      : '';
    return `${m.id}|${m.role}|${m.content}|${m.thinking ?? ''}|${m.thinkingStartedAt ?? ''}|${m.thinkingDurationMs ?? ''}|${m.segmentRole ?? ''}|${tc}|${pr}|${ts}|${tfc}|${wc}|${m.streaming ? '1' : '0'}|${m.agentName ?? ''}|${m.model ?? ''}|${m.timestamp}|${m.requestId ?? ''}|${m.turnOutcome ? JSON.stringify(m.turnOutcome) : ''}`;
  });
  return `t|${isStreaming ? '1' : '0'}|${userPinnedOpen === null ? '_' : userPinnedOpen ? '1' : '0'}|${parts.join('||')}`;
}

/**
 * 外部会话才返回展示身份；内置 Crew 会话返回 undefined，确保沿用原渲染路径。
 */
function sessionTurnIdentity(display: ReturnType<typeof getSessionAgentDisplay>): AgentTurnOptions['identity'] | undefined {
  const provider = String(display?.agentLabel?.provider || '').trim().toLowerCase();
  // Team 是会话容器，不是发言者。团队消息由 team_internal.agent_id 决定头像；
  // 首帧前的通用等待态沿用内置 Leader（Crew），聊天区不展示 Team Logo。
  if (isExternalTeamSession(display?.agentBinding)) return undefined;
  if (provider === 'sites') {
    const name = String(display?.agentLabel?.name || 'Agent').trim();
    return { kind: 'external', name, badge: '', icon: 'icon-inspiration' };
  }
  if (!isExternalAgentSession(display?.agentBinding)) return undefined;
  const name = String(display?.agentLabel?.name || 'Agent').trim();
  const badge = provider.includes('codex') ? 'X' : (name || provider).slice(0, 1).toUpperCase();
  return { kind: 'external', name, badge };
}

const savedViewports = new Map<string, { owner: string; anchor: ConversationViewportAnchor }>();

function viewportMemoryKey(containerId: string, sessionId: string): string {
  return JSON.stringify([containerId, sessionId]);
}

function viewportCoordinates(container: HTMLElement, wrapper: HTMLElement): { top: number; origin: number } {
  const top = container.getBoundingClientRect().top + container.clientTop;
  return { top, origin: container.scrollTop + wrapper.getBoundingClientRect().top - top };
}

function captureConversationAnchor(
  container: HTMLElement,
  target: ChatRenderTarget,
): ConversationViewportAnchor | undefined {
  const wrapper = target.wrapper;
  if (!wrapper || container.clientHeight <= 0 || !target.viewport.rows.length) return undefined;
  const { top, origin } = viewportCoordinates(container, wrapper);
  for (const child of wrapper.children) {
    if (!(child instanceof HTMLElement)) continue;
    const key = child.dataset.conversationUnit;
    if (key === undefined || target.viewport.topOf(key) === undefined) continue;
    const rect = child.getBoundingClientRect();
    if (rect.bottom > top && rect.top < top + container.clientHeight) return { key, offset: rect.top - top };
  }
  return target.viewport.anchorAt(container.scrollTop - origin);
}

const transientDetailsSelector = 'details.process-timeline__details:not([data-fold-key]), details.rich-reply__history';

function rememberTransientDetail(container: HTMLElement, detail: HTMLDetailsElement): void {
  const target = renderTargets.get(container.id);
  const row = detail.closest<HTMLElement>('[data-conversation-unit]');
  const key = row?.dataset.conversationUnit;
  if (!target || !row || key === undefined || target.lastUnits.get(key) !== row) return;
  const plan = target.viewport.rowForKey(key);
  if (!plan) return;
  const index = Array.from(row.querySelectorAll(transientDetailsSelector)).indexOf(detail);
  if (index < 0) return;
  const previous = target.transientDetails.get(key);
  const saved = previous?.sig === plan.sig ? previous : { sig: plan.sig, open: new Map<number, boolean>() };
  saved.open.set(index, detail.open);
  target.transientDetails.delete(key);
  target.transientDetails.set(key, saved);
  if (target.transientDetails.size > 256) target.transientDetails.delete(target.transientDetails.keys().next().value!);
}

function restoreTransientDetails(target: ChatRenderTarget, plan: UnitPlan, row: HTMLElement): void {
  const saved = target.transientDetails.get(plan.key);
  if (!saved) return;
  if (saved.sig !== plan.sig) {
    target.transientDetails.delete(plan.key);
    return;
  }
  row.querySelectorAll<HTMLDetailsElement>(transientDetailsSelector).forEach((detail, index) => {
    const open = saved.open.get(index);
    if (open !== undefined) detail.open = open;
  });
}

function preservedDomKeys(target: ChatRenderTarget): Set<string> {
  const keys = new Set<string>();
  const wrapper = target.wrapper!;
  const owner = wrapper.ownerDocument;
  const focused = owner.activeElement?.closest<HTMLElement>('[data-conversation-unit]');
  if (focused && wrapper.contains(focused) && focused.dataset.conversationUnit !== undefined) {
    keys.add(focused.dataset.conversationUnit);
  }
  const selection = owner.getSelection();
  if (selection && !selection.isCollapsed && selection.rangeCount > 0
    && (wrapper.contains(selection.anchorNode) || wrapper.contains(selection.focusNode))) {
    const range = selection.getRangeAt(0);
    for (const [key, node] of target.lastUnits) {
      if (range.intersectsNode(node)) keys.add(key);
    }
  }
  return keys;
}

function applyConversationWindow(
  container: HTMLElement,
  containerId: string,
  target: ChatRenderTarget,
  preferred?: ConversationViewportAnchor,
  replacedWrapper?: HTMLElement | null,
  resetDom = false,
): void {
  const wrapper = target.wrapper;
  if (!wrapper || wrapper.parentElement !== container) return;
  target.cancelViewportFrame?.();
  const viewport = target.viewport;
  const anchor = getConversationScrollAnchor(containerId);
  const { top, origin } = viewport.enabled || preferred ? viewportCoordinates(container, wrapper) : { top: 0, origin: 0 };
  const preferredTop = preferred && viewport.topOf(preferred.key);
  const offset = preferredTop !== undefined
    ? preferredTop - preferred!.offset
    : container.scrollTop - origin;
  const keep = viewport.enabled ? preservedDomKeys(target) : new Set<string>();
  if (preferred) keep.add(preferred.key);
  const selected = viewport.select(offset, container.clientHeight || 600, !preferred && anchor.isStickyBottom(), keep);
  const desired = selected.map((index) => viewport.rows[index]).concat(target.extras);
  const previous = target.mounted;
  const nodes = target.lastUnits;
  const ops = diffRenderUnits(previous, desired);
  let missingRows = false;
  const builders = new Map(desired.map((plan) => [plan.key, plan]));
  const added: HTMLElement[] = [];
  const removed: HTMLElement[] = replacedWrapper ? [replacedWrapper] : [];

  for (const op of ops) {
    if (op.type === 'reuse') continue;
    const old = nodes.get(op.key);
    if (old) {
      target.viewportObserver?.unobserve(old);
      removed.push(old);
    }
    const plan = builders.get(op.key);
    const fresh = plan ? buildConversationUnit(plan) : null;
    if (plan && isPresent(fresh)) {
      restoreTransientDetails(target, plan, fresh);
      markSettledHistory(fresh);
      fresh.dataset.conversationUnit = op.key;
      if (old?.parentNode === wrapper) {
        const spinner = old.querySelector('.msg__fold-spinner');
        const replacement = fresh.querySelector('.msg__fold-spinner');
        if (spinner && replacement) replacement.replaceWith(spinner);
        old.replaceWith(fresh);
      }
      nodes.set(op.key, fresh);
      added.push(fresh);
    } else {
      old?.remove();
      nodes.delete(op.key);
      if (plan) missingRows = viewport.measure(plan.key, plan.sig, 0) || missingRows;
    }
  }

  const layout: HTMLElement[] = [];
  const retainedSpacers = new Map<string, HTMLElement>();
  const appendSpacer = (start: number, end: number, nextKey: string): void => {
    if (start >= end) return;
    const height = viewport.spacerHeight(start, end);
    if (height <= 0) return;
    const spacer = target.spacers.get(nextKey) ?? wrapper.ownerDocument.createElement('div');
    spacer.className = 'conversation-viewport-spacer mw-runtime-style';
    spacer.setAttribute('aria-hidden', 'true');
    const cssHeight = height + 'px';
    if (spacer.style.getPropertyValue('--mw-runtime-height') !== cssHeight) setRuntimeStyle(spacer, 'height', cssHeight);
    retainedSpacers.set(nextKey, spacer);
    layout.push(spacer);
  };

  let next = 0;
  for (const index of selected) {
    const row = viewport.rows[index];
    const node = nodes.get(row.key);
    if (!node) continue;
    appendSpacer(next, index, row.key);
    layout.push(node);
    next = index + 1;
  }
  appendSpacer(next, viewport.rows.length, '__history_end');
  for (const extra of target.extras) {
    const node = nodes.get(extra.key);
    if (node) layout.push(node);
  }
  for (const [key, spacer] of target.spacers) {
    if (!retainedSpacers.has(key)) spacer.remove();
  }
  target.spacers = retainedSpacers;

  let cursor = wrapper.firstChild;
  let reordered = false;
  for (const node of layout) {
    if (node !== cursor) {
      if (node.parentNode === wrapper && !node.classList.contains('conversation-viewport-spacer')) reordered = true;
      wrapper.insertBefore(node, cursor);
    }
    cursor = node.nextSibling;
  }

  if (resetDom || added.length || removed.length || reordered) {
    syncConversationDom(container, target.lastSessionId, { added, removed, reset: resetDom, reordered });
    for (const root of removed) {
      if (root === replacedWrapper || root.classList.contains('native-visual')) disposeNativeVisuals(root);
      if (root.classList.contains('followup-card-wrap--permission')) disposePermissionCardPosition(root);
    }
    for (const root of resetDom ? [wrapper] : added) {
      attachCopyButtons(root);
      void renderMermaidBlocks(root);
      if (resetDom || root.classList.contains('native-visual')) hydrateNativeVisuals(root);
    }
  }
  for (const node of added) {
    if (node.dataset.conversationUnit && viewport.topOf(node.dataset.conversationUnit) !== undefined) {
      target.viewportObserver?.observe(node);
    }
  }
  const followup = nodes.get('__followup');
  if (followup) {
    syncPermissionCardPositions(followup);
    bindFollowupCard(followup, target.followupHandlers ?? noopFollowupHandlers);
  }

  target.mounted = desired;
  if (preferred && preferredTop !== undefined) {
    const node = nodes.get(preferred.key);
    const rect = node?.getBoundingClientRect();
    const scrollTop = rect && rect.height > 0
      ? container.scrollTop + rect.top - top - preferred.offset
      : origin + preferredTop - preferred.offset;
    const previousTop = container.scrollTop;
    if (anchor.isStickyBottom() || Math.abs(scrollTop - previousTop) > 0.25) {
      anchor.restoreReadingTop(scrollTop);
      if (viewport.enabled && Math.abs(container.scrollTop - previousTop) > 0.25) {
        target.restoredViewportTop = container.scrollTop;
      }
    }
  }
  target.readingAnchor = viewport.enabled && !anchor.isStickyBottom() ? captureConversationAnchor(container, target) : undefined;
  if (viewport.enabled && missingRows) target.scheduleViewport?.(target.readingAnchor);
}

function buildConversationUnit(plan: UnitPlan): HTMLElement | null {
  try {
    return plan.build();
  } catch (error) {
    reportRendererError('render', error, { unitKey: plan.key });
    return renderUnitErrorCard();
  }
}

function bindConversationViewport(container: HTMLElement, containerId: string, target: ChatRenderTarget): void {
  const wrapper = target.wrapper!;
  const anchor = getConversationScrollAnchor(containerId);
  const syncGeometry = (): boolean => {
    const style = getComputedStyle(wrapper);
    const gap = Number.parseFloat(style.rowGap);
    return target.viewport.setGeometry(wrapper.clientWidth, Number.isFinite(gap) ? gap : 0);
  };
  syncGeometry();
  let nextAnchor: ConversationViewportAnchor | undefined;
  const schedule = (preferred?: ConversationViewportAnchor): void => {
    nextAnchor = preferred;
    if (target.viewportFrame !== null) return;
    target.viewportFrame = requestAnimationFrame(() => {
      target.viewportFrame = null;
      if (renderTargets.get(containerId) !== target) return;
      const measuredAnchor = nextAnchor;
      applyConversationWindow(container, containerId, target, measuredAnchor);
      anchor.pinToBottomIfSticky();
    });
  };
  target.scheduleViewport = schedule;
  target.cancelViewportFrame = () => {
    nextAnchor = undefined;
    if (target.viewportFrame !== null) cancelAnimationFrame(target.viewportFrame);
    target.viewportFrame = null;
  };
  const onScroll = (): void => {
    if (!target.viewport.enabled) return;
    const restoredTop = target.restoredViewportTop;
    target.restoredViewportTop = undefined;
    // Acknowledging a layout scroll must not overwrite a newer measurement.
    if (restoredTop !== undefined && Math.abs(container.scrollTop - restoredTop) <= 0.25) return;
    target.readingAnchor = anchor.isStickyBottom() ? undefined : captureConversationAnchor(container, target);
    schedule();
  };
  const onSelectionOrFocus = (): void => {
    if (target.viewport.enabled) schedule(nextAnchor);
  };
  container.addEventListener('scroll', onScroll, { passive: true });
  container.addEventListener('focusout', onSelectionOrFocus);
  container.ownerDocument.addEventListener('selectionchange', onSelectionOrFocus);
  if (typeof ResizeObserver !== 'undefined') {
    target.viewportObserver = new ResizeObserver((entries) => {
      const reading = !anchor.isStickyBottom() ? target.readingAnchor : undefined;
      let updated = entries.some((entry) => entry.target === wrapper) && syncGeometry();
      for (const entry of entries) {
        if (!(entry.target instanceof HTMLElement)) continue;
        const row = entry.target;
        const key = row.dataset.conversationUnit;
        if (key === undefined || target.lastUnits.get(key) !== row) continue;
        const plan = target.viewport.rowForKey(key);
        const box = entry.borderBoxSize?.[0]?.blockSize ?? row.getBoundingClientRect().height;
        if (!plan || !box) continue;
        const style = getComputedStyle(row);
        const margin = (Number.parseFloat(style.marginBlockStart) || 0) + (Number.parseFloat(style.marginBlockEnd) || 0);
        updated = target.viewport.measure(key, plan.sig, Math.max(1, box + margin)) || updated;
      }
      if (target.viewport.enabled && (updated || entries.some((entry) => entry.target === container))) schedule(reading);
    });
    target.viewportObserver.observe(wrapper);
    target.viewportObserver.observe(container);
  }
  target.stopViewport = () => {
    target.cancelViewportFrame?.();
    target.viewportObserver?.disconnect();
    container.removeEventListener('scroll', onScroll);
    container.removeEventListener('focusout', onSelectionOrFocus);
    container.ownerDocument.removeEventListener('selectionchange', onSelectionOrFocus);
    container.classList.remove('conversation-viewport--active');
  };
}

// ---------- renderConversation：渲染主体 ----------

export interface ConversationRenderHooks {
  /** 面板自己的 sessionId 来源（缺省 = 全局活跃会话）；fold 委托按此解析消息。 */
  getSessionId?: () => string | null;
  /** 空态（无消息且非 busy 非编辑态时渲染）。缺省 = 主对话通用空态。 */
  emptyState?: () => HTMLElement;
  /** followup 卡提交/取消；面板必须注入自己的 resolve/cancel，缺省为 no-op。 */
  followupHandlers?: {
    onSubmit: (questionId: string, answers: FollowupAnswer[]) => void;
    onCancel: (questionId: string) => void;
  };
  teamCommunicationHandlers?: TeamCommunicationActionHandlers;
  /** DOM apply 之后、软钉底之前的渲染后钩子（todo 槽位 / running intro / composer 刷新等）。 */
  afterRender?: (sessionId: string | null) => void;
}

const noopFollowupHandlers = {
  onSubmit: () => {},
  onCancel: () => {},
};

/**
 * 把指定会话的消息流增量渲染进 container。
 * 调用方负责：面板可见性、sessionId 解析、以及 main-only 的渲染后副作用
 * （inspector 角标 / messages:changed / 回合计时器，见 chat-controller.renderChat）。
 */
export function renderConversation(
  container: HTMLElement,
  containerId: string,
  sessionId: string | null,
  hooks: ConversationRenderHooks = {},
): void {
  const getSessionId = hooks.getSessionId ?? (() => state.activeSessionId);
  const allMessages = sessionId ? ensureSessionMessages(sessionId) : [];
  const busy = sessionId ? isBusySession(sessionId) : false;
  const queueHint = sessionId ? state.queueHints[sessionId] : '';
  const pendingFollowup = sessionId ? ensureSessionBook(sessionId).pendingFollowup : null;
  const display = getSessionAgentDisplay(sessionId);
  const turnIdentity = sessionTurnIdentity(display);
  const ownerKey = interactiveOwnerKey();

  // 撤回修改中：隐藏 [editFromIdx..] 的内容（不删除），只渲染前面部分
  const editFrom = sessionId ? state.editFromIdx[sessionId] : undefined;
  const editing = editFrom != null;
  const messages = allMessages;
  const visibleLength = editing ? Math.min(
    allMessages.length, Math.max(0, editFrom < 0 ? allMessages.length + editFrom : editFrom),
  ) : allMessages.length;

  let target = getChatRenderTarget(containerId);
  const replacedWrapper = target.lastSessionId !== sessionId || target.ownerKey !== ownerKey || target.wrapper?.parentElement !== container
    ? target.wrapper : null;
  const replacedFollowup = replacedWrapper ? target.lastUnits.get('__followup') : undefined;
  if (replacedWrapper) {
    const previousSession = target.lastSessionId;
    const previousContainer = replacedWrapper.parentElement;
    const scroll = getConversationScrollAnchor(containerId);
    if (previousSession && previousContainer === container && target.ownerKey === ownerKey) {
      const key = viewportMemoryKey(containerId, previousSession);
      const previousAnchor = !scroll.isStickyBottom() ? captureConversationAnchor(previousContainer, target) : undefined;
      savedViewports.delete(key);
      if (previousAnchor) savedViewports.set(key, { owner: ownerKey, anchor: previousAnchor });
      if (savedViewports.size > 64) savedViewports.delete(savedViewports.keys().next().value!);
    }
    target.stopViewport?.();
    scroll.jumpToBottom();
    renderTargets.delete(containerId);
    target = getChatRenderTarget(containerId);
  }
  target.ownerKey = ownerKey;
  target.lastSessionId = sessionId;

  const modelLabel = state.configModel;
  const identitySig = turnIdentity ? JSON.stringify(turnIdentity) : 'crew';
  const teamSessionName = String(display?.agentLabel?.name || '').trim();
  const teamKey = teamTurnDisplayKey(sessionId);
  const displayKey = JSON.stringify([modelLabel, identitySig, teamKey, teamSessionName]);
  const folded = state.userFoldedTurns;
  const unfolded = state.userUnfoldedTurns;
  const sameDisplay = target.displayKey === displayKey && target.folded === folded && target.unfolded === unfolded;
  const source = target.source;
  let changed = source && sameDisplay
    ? firstChangedMessageIndex(source, messages)
    : undefined;
  if (changed !== undefined && target.teamRows && target.teamBusy !== busy) changed = undefined;
  if (changed !== undefined && source && (source !== messages || visibleLength !== target.visibleLength)
    && changedWidgetResults(source, target.visibleLength, messages, visibleLength,
      Math.min(changed, target.visibleLength, visibleLength))) {
    changed = undefined;
  }
  if (changed === undefined) {
    target.latestVisualRevisions = new Map(sessionId
      ? inlineSurfaces(visibleLength === messages.length ? messages : messages.slice(0, visibleLength), sessionId)
        .visuals.map(({ spec }) => [spec.id, spec.revision])
      : []);
  }
  const messageChange = Math.min(changed ?? 0, target.invalidFrom);
  const reused = target.visibleLength === visibleLength && messageChange >= visibleLength
    ? target.spans.length
    : reusableSpanCount(target.spans, Math.min(messageChange, target.visibleLength, visibleLength));
  const previousSpan = target.spans[reused - 1];
  const start = target.spans[reused]?.start ?? target.visibleLength;
  const historyFrom = target.spans[reused]?.planStart ?? target.viewport.rows.length;
  const spans: ProjectedSpan[] = [];
  const plans: UnitPlan[] = [];
  let hasVisibleAgent = previousSpan?.hasVisibleAgent ?? false;
  let trailingEmptyTyping = previousSpan?.emptyTyping ?? false;
  let teamRows = previousSpan?.teamRows ?? false;
  const finishSpan = (index: number, firstPlan: number, visibleAgent: boolean, emptyTyping = false): void => {
    hasVisibleAgent ||= visibleAgent;
    trailingEmptyTyping = emptyTyping;
    teamRows ||= messages[index].role === 'team_internal';
    spans.push({ start: index, planStart: historyFrom + firstPlan, hasVisibleAgent, emptyTyping, teamRows });
  };
  let extrasStart = 0;
  const pushPlan = (
    key: string, sig: string, build: () => HTMLElement | null, estimatedHeight = 132, live = false,
  ): void => {
    plans.push({ key, sig, build, estimatedHeight, live });
  };
  // sig 计算异常隔离：sig 与 build 读同一批消息字段，字段形状异常（非数组等）必须同样
  // 只损失自己——否则在 diff 之前就抛错，整轮渲染失败、会话界面冻结在旧 DOM。
  // 回落 sig 用安全摘要而非常量，保证「数据变化 → sig 变化 → 重试」语义仍然成立。
  const safeSig = (key: string, compute: () => string, source: unknown): string => {
    try {
      return compute();
    } catch (err) {
      reportRendererError('render', err, { unitKey: key, phase: 'sig' });
      return `${key}|__sig-error|${safeStringify(source)}`;
    }
  };

  if (visibleLength === 0 && !busy && !editing && sessionId && state.historyLoadErrors.has(sessionId)) {
    // 历史回填失败且无消息可显示：错误卡（带重试）优先于默认空态，失败不再表现为「空会话」。
    pushPlan('__history_error', 'history-error', () => renderHistoryErrorCard());
  } else if (visibleLength === 0 && !busy && !editing && sessionId && state.historyLoading.has(sessionId)) {
    // 首屏窗口在途（P1-6）：骨架占位先于空态，openSession 不再等历史返回才渲染。
    pushPlan('__history_loading', 'history-loading', () => renderQueueHintCard('正在加载会话历史…'));
  } else if (visibleLength === 0 && !busy && !editing) {
    pushPlan('__empty', 'empty', () => hooks.emptyState?.() ?? renderEmptyState());
  } else if (visibleLength === 0 && editing) {
    // 编辑态 + 空流：留白（提示条已在输入框上方）—— 不 push 任何单元
  } else {
    // 把连续的 agent 消息（assistant / status / error）合并成一个 .msg 块（与 X3a 完全一致的切分逻辑）
    let i = start;
    // 末尾没有实际内容的 streaming assistant turn（Dynamic Kanban 里由首个 status/workflow 帧
    // 开出的空 anchor）本质上等同于「正在生成」的 typing 指示器。如果把它留在原位置，
    // 后续 workflow_progress / agent 角色卡片会追加在它下面，导致「转圈等待」被旧输出压在上方。
    // 这里把它识别出来、跳过原位置渲染，改为在所有消息之后以 __typing 单元渲染，确保它始终
    // 紧跟最新消息。
    while (i < visibleLength) {
      const firstPlan = plans.length;
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
          const displayText = resolveTeamCollaborationMentionText(sessionId, msg.content);
          const isPlanning = msg.eventType === 'team_planning_progress';
          const teamName = resolveTeamCollaborationName(sessionId) || teamSessionName || '团队';
          const displayed = isPlanning
            ? {
                ...msg,
                content: displayText,
                agentName: teamName,
                agentRole: '',
                isLeader: false,
              }
            : member
            ? {
                ...msg,
                content: displayText,
                ...(member.agentId ? { agentId: member.agentId } : {}),
                agentName: member.name,
                agentRole: member.isLeader ? 'leader' : (msg.agentRole || member.role),
                agentTone: member.tone,
                isLeader: Boolean(member.isLeader || msg.isLeader),
              }
            : {
                ...msg,
                text: displayText,
              };
          // Team Turn 使用自身 streaming 生命周期，不借用 Session 全局 busy。
          // 这样新节点启动时不会“复活”已完成的成员回合。
          const isStreaming = msg.streaming === true;
          sig = safeSig(
            msg.id,
            () => `${sigTeamInternal(displayed, isStreaming)}|actions:${busy ? 'busy' : 'idle'}`,
            displayed,
          );
          const captured = displayed;
          pushPlan(msg.id, sig, () => renderTeamInternalMessage(captured, isStreaming, {
            canRetry: !busy,
            canCancel: busy,
          }), 144, isStreaming);
          finishSpan(i, firstPlan, true);
          i += 1;
          continue;
        } else if (msg.role === 'status' && msg.workflowProgress) {
          sig = safeSig(msg.id, () => sigWorkflowProgress(msg, modelLabel), msg);
        } else if (msg.agentName) {
          sig = safeSig(msg.id, () => sigAgentRoleCard(msg, modelLabel), msg);
        } else {
          sig = safeSig(msg.id, () => sigUserMessage(msg, modelLabel), msg);
        }
        pushPlan(msg.id, sig, () => renderMessageHtml(msg, modelLabel),
          msg.role === 'user' ? 72 : 132, msg.streaming === true);
        const marker = userTurnOutcomeMarker(msg);
        if (marker) {
          pushPlan(marker.id, JSON.stringify([marker.timestamp, marker.turnOutcome]), () =>
            renderAgentTurn([marker], { isStreaming: false, userPinnedOpen: null, turnDurationMs: 0 }));
        }
        finishSpan(i, firstPlan, msg.role === 'assistant' || msg.role === 'status');
        i += 1;
        continue;
      }
      let j = i + 1;
      let requestId = msg.requestId;
      while (j < visibleLength) {
        const next = messages[j];
        if (startsAnotherOutcomeTurn(requestId, next, messages[j - 1])) break;
        const { role, agentName, workflowProgress } = next;
        if (!((role === 'assistant' && !agentName) || role === 'error'
          || (role === 'status' && !agentName && !workflowProgress))) break;
        requestId ??= next.requestId;
        j += 1;
      }
      const batch = messages.slice(i, j);
      const isLastBatch = j >= visibleLength;
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
        finishSpan(i, firstPlan, false, true);
        i = j;
        continue;
      }
      const turnId = batch[0].id;
      // liveness = batch 内是否存在 streaming 消息（anchor 可能不在尾部：动态看板的 status/workflow
      // 帧常排在 assistant anchor 之后）。per-turn 自有信号，不引用 session 全局 busy——否则新回合让
      // session 重新 busy 时会复活所有已封口回合（停任务1再启任务2，两个回合都显示执行中）。
      const isStreaming = batch.some((m) => m.streaming === true);
      const isLiveTurn = isLastBatch && isStreaming;
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
      const sig = safeSig(
        turnId,
        () => `${sigAgentTurn(batch, isStreaming, userPinnedOpen)}|${identitySig}`,
        batch,
      );
      const capturedBatch = batch;
      const surfaces = sessionId ? inlineSurfaces(batch, sessionId) : null;
      for (const visual of surfaces?.visuals ?? []) {
        const readOnly = (target.latestVisualRevisions.get(visual.spec.id) ?? visual.spec.revision) > visual.spec.revision;
        pushPlan(visual.key, JSON.stringify([visual.spec, readOnly]), () => renderNativeVisual({ ...visual, readOnly }), 280, isLiveTurn);
      }
      pushPlan(turnId, sig, () =>
        renderAgentTurn(capturedBatch, {
          isStreaming,
          userPinnedOpen,
          turnDurationMs,
          ...(turnIdentity ? { identity: turnIdentity } : {}),
        }), 132, isStreaming,
      );
      finishSpan(i, firstPlan, true);
      i = j;
    }
    extrasStart = plans.length;
    // 带 agentName 的 status（Dynamic Kanban 角色卡片）和 workflowProgress 面板走上面的 !isAgent 分支，
    // 分别由 sigAgentRoleCard / sigWorkflowProgress 提供 sig，避免 stale。
    if (queueHint && !(state.pendingQueues[sessionId ?? '']?.length)) {
      pushPlan('__queue', `q|${queueHint}`, () => renderQueueHintCard(queueHint));
    }
    if (busy && (trailingEmptyTyping || !hasVisibleAgent)) {
      pushPlan('__typing', `typing|${identitySig}`, () => renderTypingIndicator(turnIdentity));
    }
  }
  const streamRecovery = sessionId ? getSessionStreamRecovery(sessionId) : undefined;
  if (streamRecovery && (!pendingFollowup || streamRecovery.historyAppliedForFollowup !== pendingFollowup.questionId)) {
    pushPlan('__stream-recovery', 'stream-recovery', () => renderQueueHintCard('正在恢复回复，完成后将同步完整内容。'));
  }
  if (pendingFollowup && sessionId) {
    const followupVersion = isRuntimeStaffingFollowup(pendingFollowup)
      ? `f|${pendingFollowup.questionId}|${pendingFollowup.status ?? ''}|${pendingFollowup.note ?? ''}`
      : `f|${pendingFollowup.questionId}`;
    pushPlan('__followup', followupVersion, () => renderFollowupCardElement(pendingFollowup));
  }
  // __anchor：scroll-anchor 永远是最后一个单元，sig 恒定 → 跨帧复用同一节点
  pushPlan('__anchor', 'anchor', () => getScrollAnchorNode(target, containerId));

  const scroll = getConversationScrollAnchor(containerId);
  const localAnchor = target.pendingPrepend
    ?? (target.viewport.enabled && !scroll.isStickyBottom() ? captureConversationAnchor(container, target) : undefined);
  target.pendingPrepend = undefined;

  const resetDom = !target.wrapper;
  if (!target.wrapper) {
    const wrapper = document.createElement('div');
    wrapper.className = 'messages__inner';
    container.replaceChildren(wrapper);
    target.wrapper = wrapper;
    bindConversationViewport(container, containerId, target);
  }
  target.viewport.replaceFrom(historyFrom, plans.slice(0, extrasStart));
  target.extras = plans.slice(extrasStart);
  target.source = messages;
  target.visibleLength = visibleLength;
  target.displayKey = displayKey;
  target.folded = folded;
  target.unfolded = unfolded;
  target.invalidFrom = Infinity;
  target.teamRows = teamRows;
  target.teamBusy = busy;
  target.spans.length = reused;
  for (const span of spans) target.spans.push(span);
  target.followupHandlers = hooks.followupHandlers;

  container.classList.toggle('conversation-viewport--active', target.viewport.enabled);
  target.wrapper.classList.toggle('messages__inner--long', !target.viewport.enabled && visibleLength >= 200);
  ensureFoldDelegation(container, getSessionId);
  ensureFileChangesDelegation(container);
  ensureHistoryRetryDelegation(container, getSessionId);
  ensureTeamCommunicationDelegation(container, hooks.teamCommunicationHandlers, () => allMessages);
  if (replacedFollowup) disposePermissionCardPosition(replacedFollowup);

  let preferred = localAnchor;
  if (target.pendingRestore && target.viewport.rows.length > 0) {
    preferred = target.pendingRestore;
    target.pendingRestore = undefined;
    if (target.viewport.topOf(preferred.key) === undefined) {
      preferred = undefined;
      scroll.jumpToBottom();
    }
  }
  applyConversationWindow(container, containerId, target, preferred, replacedWrapper, resetDom);
  hooks.afterRender?.(sessionId);
  requestAnimationFrame(() => {
    if (renderTargets.get(containerId) === target) scroll.pinToBottomIfSticky();
  });
}
export function preserveConversationHistoryViewport(containerId: string, sessionId: string): boolean {
  const target = renderTargets.get(containerId);
  const container = target?.wrapper?.parentElement;
  if (!target || !container || target.lastSessionId !== sessionId || target.ownerKey !== interactiveOwnerKey()
    || getConversationScrollAnchor(containerId).isStickyBottom()) return false;
  const anchor = captureConversationAnchor(container, target);
  if (!anchor) return false;
  target.pendingPrepend = anchor;
  return true;
}

export function restoreConversationViewport(containerId: string, sessionId: string): boolean {
  const key = viewportMemoryKey(containerId, sessionId);
  const saved = savedViewports.get(key);
  const target = renderTargets.get(containerId);
  const container = target?.wrapper?.parentElement;
  if (!saved || saved.owner !== interactiveOwnerKey() || !target || !container || target.lastSessionId !== sessionId) return false;
  if (!target.viewport.rows.length) {
    getConversationScrollAnchor(containerId).disarm();
    target.pendingRestore = saved.anchor;
    return true;
  }
  if (target.viewport.topOf(saved.anchor.key) === undefined) {
    savedViewports.delete(key);
    return false;
  }
  applyConversationWindow(container, containerId, target, saved.anchor);
  return true;
}

export function revealConversationMessage(containerId: string, sessionId: string, messageId: string): HTMLElement | null {
  const target = renderTargets.get(containerId);
  const container = target?.wrapper?.parentElement;
  if (!target || !container || target.lastSessionId !== sessionId || target.ownerKey !== interactiveOwnerKey()) return null;
  let key = messageId;
  if (target.viewport.topOf(key) === undefined) {
    const index = target.source?.findIndex((message) => message.id === messageId) ?? -1;
    if (index < 0 || index >= target.visibleLength) return null;
    let low = 0;
    let high = target.spans.length;
    while (low < high) {
      const middle = (low + high) >>> 1;
      if (target.spans[middle].start <= index) low = middle + 1;
      else high = middle;
    }
    const start = target.spans[low - 1]?.planStart;
    const end = target.spans[low]?.planStart ?? target.viewport.rows.length;
    if (start === undefined || end <= start) return null;
    key = target.viewport.rows[end - 1].key;
  }
  const offset = Math.max(0, container.clientHeight / 3);
  target.pendingRestore = undefined;
  applyConversationWindow(container, containerId, target, { key, offset });
  const node = target.lastUnits.get(key);
  if (!node) return null;
  return Array.from(node.querySelectorAll<HTMLElement>('[data-message-id]'))
    .find((child) => child.dataset.messageId === messageId) ?? node;
}

/** 面板卸载：释放滚动锚定监听 + diff 缓存（主对话容器随应用存活，无需调用）。 */
export function disposeConversationRenderer(containerId: string): void {
  const target = renderTargets.get(containerId);
  const wrapper = target?.wrapper;
  target?.stopViewport?.();
  if (wrapper) disposeNativeVisuals(wrapper);
  if (wrapper?.parentElement) disposeConversationDom(wrapper.parentElement);
  const followup = target?.lastUnits.get('__followup');
  if (followup) disposePermissionCardPosition(followup);
  conversationScrollAnchors.get(containerId)?.anchor.dispose();
  conversationScrollAnchors.delete(containerId);
  renderTargets.delete(containerId);
}

export interface ConversationRenderer {
  render(sessionId: string | null): void;
  jumpToBottom(): void;
  dispose(): void;
}

/** 面板形态工厂：绑定固定容器，后续只传 sessionId（conversation-panel 使用）。 */
export function createConversationRenderer(
  container: HTMLElement,
  opts: { containerId: string } & ConversationRenderHooks,
): ConversationRenderer {
  const { containerId, ...hooks } = opts;
  return {
    render: (sessionId) => renderConversation(container, containerId, sessionId, hooks),
    jumpToBottom: () => jumpConversationToBottom(containerId),
    dispose: () => disposeConversationRenderer(containerId),
  };
}
