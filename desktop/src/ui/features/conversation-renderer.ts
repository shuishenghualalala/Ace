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
  renderMessageHtml,
  renderQueueHintCard,
  renderTeamInternalMessage,
  renderTypingIndicator,
  resolveTurnDurationMs,
} from '../chat-render';
import { diffRenderUnits, type RenderUnit } from '../chat-diff';
import { attachScrollAnchor, type ScrollAnchor } from './scroll-anchor';
import { getSessionAgentDisplay } from './workspaces';
import {
  resolveTeamCollaborationMember,
  resolveTeamCollaborationName,
} from './team-collaboration-board';
import { applyFoldState } from '../render-utils';
import { setToolFold, setTurnFold } from './fold-state';
import { bindFollowupCard, isRuntimeStaffingFollowup, renderFollowupCardElement } from '../followup';
import type { FollowupAnswer } from '../backend-client';
import { attachCopyButtons } from './copy-button';
import { renderMermaidBlocks } from './mermaid-render';
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

// ---------- scroll anchor：按容器多实例（原 chat-controller 单例的多实例化） ----------

const conversationScrollAnchors = new Map<string, { container: HTMLElement; anchor: ScrollAnchor }>();

const noOpScrollAnchor: ScrollAnchor = {
  jumpToBottom: () => {},
  pinToBottomIfSticky: () => {},
  isStickyBottom: () => true,
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
  requestAnimationFrame(() => {
    getConversationScrollAnchor(containerId).jumpToBottom();
  });
}

// ---------- 事件委托（一次性绑定，按容器） ----------

/** 「已编辑文件」卡：打开看板 Files / 在资源管理器中显示（与 fold 委托同容器、各绑一次）。 */
const fileChangesBoundContainers = new WeakSet<HTMLElement>();
export function ensureFileChangesDelegation(container: HTMLElement): void {
  if (fileChangesBoundContainers.has(container)) return;
  fileChangesBoundContainers.add(container);
  container.addEventListener('click', (event) => {
    const target = event.target instanceof Element ? event.target : null;
    if (!target) return;
    const previewBtn = target.closest<HTMLElement>('[data-attachment-preview]');
    if (previewBtn && container.contains(previewBtn)) {
      event.preventDefault();
      event.stopPropagation();
      const filePath = previewBtn.dataset.attachmentPreview?.trim();
      if (filePath) {
        openInspectorToTab('files', {
          expandFilePath: filePath,
          filePaths: [filePath],
          workspaceId: previewBtn.dataset.attachmentWorkspace || null,
        });
      }
      return;
    }
    const downloadBtn = target.closest<HTMLButtonElement>('[data-attachment-download]');
    if (downloadBtn && container.contains(downloadBtn)) {
      event.preventDefault();
      event.stopPropagation();
      const filePath = downloadBtn.dataset.attachmentDownload?.trim();
      const fileName = downloadBtn.dataset.attachmentName?.trim();
      const workspaceId = downloadBtn.dataset.attachmentWorkspace?.trim() || state.currentWorkspaceId;
      if (!filePath || !fileName || !workspaceId || !window.Crew?.saveAttachment) return;
      downloadBtn.disabled = true;
      void window.Crew.saveAttachment(filePath, fileName, workspaceId)
        .then((result) => {
          if (result.ok) notify('附件已保存');
        })
        .catch((error) => {
          notify(`附件保存失败：${error instanceof Error ? error.message : String(error)}`);
        })
        .finally(() => { downloadBtn.disabled = false; });
      return;
    }
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
        const sid = getSessionId();
        const msgs = sid ? ensureSessionMessages(sid) : [];
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

// ---------- render target（X3b：keyed 增量 diff 渲染，按容器 id 分键） ----------

/**
 * 模块级缓存：按容器 id 维护独立 ChatRenderTarget（主对话 / 工作室 / Wiki 面板多轨）。
 * 多面板共存时各自的 diff 缓存与 scroll anchor 互不干扰。
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

/** 给定 build() 产物：null 或 data-empty 占位视为「缺席」（不进 DOM、不占 Map 槽位）。
 *  与 X3a 的 appendRendered 过滤语义一致（build 返回 null 的单元不进 DOM；
 *  renderAgentTurn 空批次返回 data-empty div）。 */
function isPresent(node: HTMLElement | null): node is HTMLElement {
  if (!node) return false;
  if (node.dataset.empty === 'true') return false;
  return true;
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
  const author = msg.companionAuthor
    ? `${msg.companionAuthor.kind}|${msg.companionAuthor.id}|${msg.companionAuthor.name}|${msg.companionAuthor.isSelf}|${JSON.stringify(msg.companionAuthor.avatar ?? null)}`
    : '';
  return `u|${msg.id}|${msg.role}|${msg.content}|${msg.model ?? ''}|${configModel}|${msg.timestamp}|${att}|${author}`;
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
  const durationBucket = Math.floor(Math.max(0, msg.turnDurationMs || 0) / 1000);
  const files = (msg.turnFileChanges || []).map((file) =>
    `${file.path}|${file.status}|${file.added}|${file.removed}|${file.binary ? '1' : '0'}`,
  ).join(';');
  return `team|${msg.id}|${msg.content}|${msg.thinking || ''}|${tools}|${artifacts}|${files}|${msg.agentId || ''}|${msg.agentName || ''}|${msg.agentRole || ''}|${msg.agentTone || 0}|${msg.eventType || ''}|${msg.nodeId || ''}|${msg.displayMode || ''}|${msg.collapsedTitle || ''}|${msg.processText || ''}|${durationBucket}|${isStreaming ? '1' : '0'}`;
}

/** 一段 batch（同一回合的连续 agent 消息）的 sig。
 *
 *  关键：流式计时由 updateActiveToolDurations() 原地更新，不能让纯时间变化重建整个回合。
 *  流结束时 streaming/tool 状态本身会改变 sig，届时再渲染最终时长。
 *
 *  覆盖 renderAgentTurn 的全部输入：isStreaming / userPinnedOpen / turnDurationMs 桶 /
 *  batch 内每条消息的 role|content|thinking|toolCalls|planReview|todoSnapshot|turnFileChanges|
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
  if (provider === 'sites') {
    return { kind: 'external', name, badge: '', icon: 'icon-inspiration' };
  }
  const badge = provider.includes('codex') ? 'X' : (name || provider).slice(0, 1).toUpperCase();
  return { kind: 'external', name, badge };
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
  const turnIdentity = sessionTurnIdentity(sessionId);

  // 撤回修改中：隐藏 [editFromIdx..] 的内容（不删除），只渲染前面部分
  const editFrom = sessionId ? state.editFromIdx[sessionId] : undefined;
  const editing = editFrom != null;
  const messages = editing ? allMessages.slice(0, editFrom) : allMessages;

  const target = getChatRenderTarget(containerId);
  // 会话切换：只重置本容器的 diff 缓存（多面板共存，不能按全局会话重置其它容器）。
  if (target.lastSessionId !== sessionId) {
    target.lastUnits = new Map();
    target.lastUnitMetas = [];
    target.wrapper = null;
    target.lastSessionId = sessionId;
  }
  // 容器元素被重建（单测 body 重排 / 视图重挂）时，缓存的 wrapper 已脱离文档：
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
    pushPlan('__empty', 'empty', () => hooks.emptyState?.() ?? renderEmptyState());
  } else if (messages.length === 0 && editing) {
    // 编辑态 + 空流：留白（提示条已在输入框上方）—— 不 push 任何单元
  } else {
    // 把连续的 agent 消息（assistant / status / error）合并成一个 .msg 块（与 X3a 完全一致的切分逻辑）
    let i = 0;
    // 末尾没有实际内容的 streaming assistant turn（Dynamic Kanban 里由首个 status/workflow 帧
    // 开出的空 anchor）本质上等同于「正在生成」的 typing 指示器。如果把它留在原位置，
    // 后续 workflow_progress / agent 角色卡片会追加在它下面，导致「转圈等待」被旧输出压在上方。
    // 这里把它识别出来、跳过原位置渲染，改为在所有消息之后以 __typing 单元渲染，确保它始终
    // 紧跟最新消息。
    let trailingEmptyTypingBatch: ChatMessage[] | null = null;
    while (i < messages.length) {
      const msg = messages[i];
      const isAgent = !msg.companionAuthor && (
        (msg.role === 'assistant' && !msg.agentName)
        || msg.role === 'error'
        || (msg.role === 'status' && !msg.agentName && !msg.workflowProgress)
      );
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
    const followupVersion = isRuntimeStaffingFollowup(pendingFollowup)
      ? `f|${pendingFollowup.questionId}|${pendingFollowup.status ?? ''}|${pendingFollowup.note ?? ''}`
      : `f|${pendingFollowup.questionId}`;
    pushPlan('__followup', followupVersion, () => renderFollowupCardElement(pendingFollowup));
  }
  // __anchor：scroll-anchor 永远是最后一个单元，sig 恒定 → 跨帧复用同一节点
  pushPlan('__anchor', 'anchor', () => getScrollAnchorNode(target, containerId));

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
      if (old && old.parentNode === wrapper) {
        const oldSpinner = old.querySelector('.msg__fold-spinner');
        const freshSpinner = fresh.querySelector('.msg__fold-spinner');
        if (oldSpinner && freshSpinner) freshSpinner.replaceWith(oldSpinner);
        wrapper.replaceChild(fresh, old);
      }
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

  // ---- 副作用：全部与主对话 renderChat 保持一致 ----
  ensureFoldDelegation(container, getSessionId);
  ensureFileChangesDelegation(container);
  // 代码块复制按钮：patch/append 后新节点需重新绑定（幂等，旧节点跳过）。
  attachCopyButtons(container);
  // Mermaid 图表：懒加载 mermaid.js 渲染 [data-mermaid] 占位。幂等，已渲染的跳过。
  // 不 await：渲染是异步的，不阻塞 DOM 布局；失败时保留源码占位，下次 patch 重试。
  void renderMermaidBlocks(container);
  bindFollowupCard(container, hooks.followupHandlers ?? noopFollowupHandlers);
  hooks.afterRender?.(sessionId);
  // 软钉底：用户停在底部时才追底（与主对话 scrollChatToBottom 同一 rAF 节奏）。
  requestAnimationFrame(() => {
    getConversationScrollAnchor(containerId).pinToBottomIfSticky();
  });

  // 5) 记账：本帧的元数据成为下一帧的 prev
  target.lastUnitMetas = nextMetas;
  target.wrapper = chatWrapper;
}

/** 面板卸载：释放滚动锚定监听 + diff 缓存（主对话容器随应用存活，无需调用）。 */
export function disposeConversationRenderer(containerId: string): void {
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
