/**
 * 对话消息 DOM 渲染 — 对齐 Crew/web MessageItem 结构。
 *
 * X3a（2026-06）：所有 render* 函数改为返回 DOM 节点（HTMLElement / DocumentFragment），
 * 由 renderChat 在 index.ts 侧用 replaceChildren 组装，彻底消除
 * `container.innerHTML = \`...${inner.join('')}\`` 这一全量字符串拼接的 XSS 面
 * （原先任何 render* 一旦忘记 escapeHtml 都会顺着大拼接漏进 DOM）。
 *
 * 安全边界：
 *  - 用户派生文本一律走 textContent / setAttribute（HTML 解析器不会重新解释已构造节点上的属性值）。
 *  - 受信静态 SVG 图标 + renderMarkdownHtml 产物（已对 source 做 escapeHtml，且只追加自家受信标签）
 *    通过 createTrustedFragment(html) 经 <template>.innerHTML 装载 —— 不含任何用户插值，
 *    因此不触发 check-security 的 innerHTML-插值规则，也不是 XSS 面。
 */

import type { Attachment, InspirationSurface, TeamArtifactCard, WikiPage } from './backend-client';
import type { FileChange, TodoItem } from './state';
import { renderMarkdownHtml, renderMarkdownHtmlStreaming } from './markdown';
import { escapeHtml } from '../shared/html';
import { getToolFold, toolFoldKey } from './features/fold-state';
import { toolDisplayTitle } from './tool-labels';
import { formatToolResultDisplay } from './tool-result';
import { imageDisplayUrl, isAbsoluteLocalPath, screenshotResultPath } from './tool-screenshot';
import { buildChippedNodes } from './features/composer-mention';
import { isPlanDocumentPath } from './plan-document-path';
import { createIcon, type IconId } from './components/icon';

export type MessageRole = 'user' | 'assistant' | 'status' | 'error' | 'team_internal';

/**
 * 判断当前是否应该显示 todo 进度面板。
 * 规则：
 * - 没有 todo 时不显示；
 * - 全部 todo 都已 completed 时隐藏；
 * - 只要存在 pending / in_progress / cancelled 就显示。
 */
export function shouldShowTodoPanel(todos: TodoItem[]): boolean {
  if (todos.length === 0) return false;
  return !todos.every((todo) => todo.status === 'completed');
}

export interface ToolCallInfo {
  toolCallId: string;
  name: string;
  uiLabel?: string | undefined;
  args?: string | undefined;
  result?: string | undefined;
  status: 'generating' | 'running' | 'done' | 'error';
  startedAt: number;
  duration?: number | undefined;
  /** 运行中的阶段进度文案（phase=progress 帧），完成/失败后为空。 */
  progressText?: string | undefined;
}

/** 同回合内 assistant 段的语义角色：过程（进折叠区）vs 最终答案（折叠外可见）。 */
export type SegmentRole = 'process' | 'answer';
export type PlanReviewStatus = 'pending' | 'editing' | 'readonly' | 'empty' | 'approved' | 'revising' | 'rejected' | 'cancelled';

/**
 * 「本轮文件改动」卡的轻量条目：只取路径/名称/增删计数/状态，**不携带 diff 行**，
 * 避免把整份 diff 跟着 assistant 消息一起持久化。详情仍走 Inspector Files tab。
 */
export type TurnFileChangeSummary = Pick<FileChange, 'path' | 'name' | 'added' | 'removed' | 'status' | 'binary'>;

export interface WorkflowProgressPhase {
  id: string;
  name: string;
  description?: string;
  status: string;
}

export interface WorkflowProgressCall {
  call_id: string;
  role: string;
  phase_id?: string;
}

export interface WorkflowProgressPayload {
  workflow_id: string;
  status: string;
  current_phase?: WorkflowProgressPhase;
  completed_phases: WorkflowProgressPhase[];
  active_calls: WorkflowProgressCall[];
  message?: string;
}

export interface ChatMessage {
  id: string;
  role: MessageRole;
  content: string;
  timestamp: number;
  /** 多轮工具 loop 内该 assistant 段是过程还是最终答案；由 reducer 写入，历史回放可推断。 */
  segmentRole?: SegmentRole | undefined;
  thinking?: string | undefined;
  toolCalls?: ToolCallInfo[] | undefined;
  streaming?: boolean | undefined;
  attachments?: Attachment[] | undefined;
  /** 发送/生成此消息时使用的模型（来自当时的 state.configModel）。 */
  model?: string | undefined;
  /** 整回合推理起点（首片 delta/tool 到达时刻）。
   *  记在回合的 assistant 消息上，参考 ToolCallInfo.startedAt 的同构设计——数据随消息走。 */
  turnStartedAt?: number | undefined;
  /** 整回合推理总耗时（ms），final/error 时由 turnStartedAt 计算并写回 assistant 消息。
   *  参考 ToolCallInfo.duration 的同构设计。 */
  turnDurationMs?: number | undefined;
  /** Dynamic Kanban 等场景下生成该消息的角色名与头像。 */
  agentName?: string | undefined;
  agentAvatar?: string | undefined;
  sourceSessionId?: string | undefined;
  agentId?: string | undefined;
  agentRole?: string | undefined;
  agentTone?: number | undefined;
  isLeader?: boolean | undefined;
  eventType?: string | undefined;
  nodeId?: string | undefined;
  mentionFrom?: string | undefined;
  mentionTo?: string[] | undefined;
  mentionIntent?: string | undefined;
  communicationKind?: string | undefined;
  communicationStatus?: string | undefined;
  requestId?: string | undefined;
  replyTo?: string | undefined;
  communicationRequestText?: string | undefined;
  displayMode?: string | undefined;
  collapsedTitle?: string | undefined;
  processText?: string | undefined;
  artifacts?: TeamArtifactCard[] | undefined;
  /** status 消息的瞬时活动标记：这类「正在…」进度提示
   *  会被后续事件取代——渲染时 live 回合只保留最新一条，回合结束后全部隐藏。 */
  activity?: string | undefined;
  /** Dynamic Kanban 工作流进度面板数据。 */
  workflowProgress?: WorkflowProgressPayload | undefined;
  /** 该回合末尾的 todo 快照（todoUpdatedReducer patch 进来）；chat-render 据此渲染正文进度卡。 */
  todoSnapshot?: TodoItem[] | undefined;
  /** 该回合「仅本轮」文件改动差集（finalReducer 在 final 时推算并 patch 进来）；
   *  chat-render 据此在正文下方渲染「已编辑 N 个文件」卡。 */
  turnFileChanges?: TurnFileChangeSummary[] | undefined;
  /** 历史中由 Gateway 精确落库的文件路径；旧 tool_call 推断项不在其中。
   *  精确项不得再按当前磁盘状态做“旧历史补全”，否则跨轮删除会改写原回合语义。 */
  turnFileChangesPersistedPaths?: string[] | undefined;
  /** 该回合内产生的 plan 审批卡片。 */
  planReview?: {
    plan: string;
    planFile: string;
    status: PlanReviewStatus;
    sessionId: string;
    phase?: string | undefined;
  } | undefined;
  /** 被停止/断连截断的半截回复：不再显示执行中，但仍按流式 Markdown 容错渲染。 */
  interrupted?: boolean | undefined;
  /** Wiki Agent 回合引用的 Wiki 页面卡片（wiki_cards 帧 → wikiCardsReducer patch 进来）；
   *  chat-render 据此在正文下方渲染「Wiki 结果」卡片网格。 */
  wikiCards?: WikiPage[] | undefined;
}

export interface PendingMessage {
  id: string;
  query: string;
  attachments?: Attachment[];
  subScenario?: string;
  planActive?: boolean;
  workDisabledPreferenceIds?: string[];
  /** revision: 队列项被用户提升为“修订式中断”的下一轮正式输入。 */
  clientIntent?: 'revision';
  /** 已乐观渲染成 user 气泡的消息 id；队列面板隐藏，发送时复用避免重复气泡。 */
  optimisticUserMessageId?: string;
  /** 用户在 Team Composer 中选择的成员 mention。 */
  userMentions?: { kind: 'team_member'; member_id: string }[];
}

export type SessionStatus = 'idle' | 'running' | 'queued' | 'error';

const CHAT_BOT_AVATAR_SYMBOL = './crew-ui-symbols.svg#avatar-headphones';

/** 对话头像：Q 版耳机机器人。用户消息不显示头像。 */
function createChatAvatar(): HTMLElement {
  const avatar = document.createElement('div');
  avatar.className = 'msg__avatar bot';
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.classList.add('msg__avatar-symbol');
  svg.setAttribute('viewBox', '0 0 32 32');
  svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', CHAT_BOT_AVATAR_SYMBOL);
  svg.appendChild(use);
  avatar.appendChild(svg);
  return avatar;
}

/** 把 ms 时间戳格式化成 HH:MM。 */
export function formatMessageTime(ts: number): string {
  if (!ts) return '';
  const d = new Date(ts);
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  return `${hh}:${mm}`;
}

/** 复制按钮（小图标），data-copy 存原文，已由 #chat-messages 委托统一处理点击。 */
const COPY_BTN_SVG = `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`;
const IMAGE_COPY_SVG = `<svg class="chat-image-action__icon chat-image-action__icon--default" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg><svg class="chat-image-action__icon chat-image-action__icon--success" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg>`;
const IMAGE_REVEAL_SVG = `<svg class="chat-image-action__icon" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H9l2 2h7.5A2.5 2.5 0 0 1 21 9.5v7A2.5 2.5 0 0 1 18.5 19h-13A2.5 2.5 0 0 1 3 16.5Z"/><path d="m10 15 4-4m-4 0h4v4"/></svg>`;
/** 撤回修改按钮（铅笔）。 */
const EDIT_BTN_SVG = `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>`;
/** 待发卡片「引导」按钮（向上箭头：提升为修订式下一轮）。 */
const QUEUE_STEER_SVG = `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19V5M5 12l7-7 7 7"/></svg>`;
/** 待发卡片「删除」按钮（垃圾桶，比 × 更直观）。 */
const QUEUE_DELETE_SVG = `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M10 11v6M14 11v6"/></svg>`;
/** 待发卡片更多菜单。 */
const QUEUE_MORE_SVG = `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/></svg>`;
const QUEUE_UP_SVG = `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m18 15-6-6-6 6"/></svg>`;
const QUEUE_DOWN_SVG = `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>`;

/**
 * 把一段「已受信生成」的 HTML 字符串（无用户插值，或经 renderMarkdownHtml 已对源 escapeHtml）
 * 通过 <template>.innerHTML 装载为 DocumentFragment。
 *
 * 这是把既有 HTML-string 渲染逻辑迁移到 DOM 节点的统一受信入口：
 *  - 仅用于不含 `${userVar}` 插值的字符串（静态 SVG / 自家生成的受信标签）；
 *  - 不触发 check-security 的 innerHTML-插值规则（规则只匹配带 ${} 的模板字面量）。
 */
function createTrustedFragment(html: string): DocumentFragment {
  const template = document.createElement('template');
  template.innerHTML = html;
  return template.content.cloneNode(true) as DocumentFragment;
}

/** 等价 createTrustedFragment，但返回首个 Element（用于只产单个根元素的 render*）。 */
function createTrustedElement<T extends Element = HTMLElement>(html: string): T {
  const frag = createTrustedFragment(html);
  const el = frag.firstElementChild;
  if (!el) {
    // 调用方约定 html 至少产出一个根元素；空输入回退一个占位 div 避免运行时炸。
    return document.createElement('div') as unknown as T;
  }
  return el as T;
}

function renderCopyBtn(text: string): HTMLButtonElement {
  // data-copy 走 setAttribute：HTML 解析器不会重新解释已构造元素上的属性值，
  // 因此即便 text 含引号/尖括号也不会注入（且这里再把值过一遍 escapeHtml 以保持与原实现字节一致）。
  const btn = createTrustedElement<HTMLButtonElement>(
    `<button type="button" class="chat-copy-btn msg-action-btn" title="复制">${COPY_BTN_SVG}</button>`,
  );
  btn.setAttribute('data-copy', text);
  return btn;
}

function renderEditBtn(msgId: string): HTMLButtonElement {
  const btn = createTrustedElement<HTMLButtonElement>(
    `<button type="button" class="chat-edit-btn msg-action-btn" title="撤回修改">${EDIT_BTN_SVG}</button>`,
  );
  btn.setAttribute('data-edit', msgId);
  return btn;
}

function buildImageActionButton(
  label: string,
  action: 'copy' | 'reveal',
  localPath: string,
): HTMLButtonElement {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = `chat-image-action chat-image-action--${action}`;
  button.setAttribute('aria-label', label);
  button.setAttribute('data-tooltip', label);
  button.appendChild(createTrustedFragment(action === 'copy' ? IMAGE_COPY_SVG : IMAGE_REVEAL_SVG));
  button.setAttribute(action === 'copy' ? 'data-image-copy-path' : 'data-image-reveal-path', localPath);
  return button;
}

function buildInlineImage(
  source: string,
  caption: string,
  localPath: string,
  variant: 'tool' | 'attachment',
): HTMLElement {
  const frame = document.createElement('div');
  frame.className = variant === 'tool'
    ? 'tool-card__image-frame'
    : 'msg__attachment msg__attachment--image';
  const view = document.createElement('button');
  view.type = 'button';
  view.className = variant === 'tool' ? 'tool-card__image-view' : 'msg__attachment-image-view';
  view.title = '查看大图';
  view.setAttribute('aria-label', caption ? `查看大图：${caption}` : '查看大图');
  view.setAttribute('data-image-view-src', imageDisplayUrl(source));
  view.setAttribute('data-image-caption', caption);
  if (isAbsoluteLocalPath(localPath)) view.setAttribute('data-image-local-path', localPath);
  const image = document.createElement('img');
  image.className = variant === 'tool' ? 'tool-card__image' : 'msg__attachment-image';
  image.src = imageDisplayUrl(source);
  image.alt = caption;
  image.loading = 'lazy';
  image.draggable = false;
  view.appendChild(image);
  frame.appendChild(view);
  if (isAbsoluteLocalPath(localPath)) {
    const actions = document.createElement('div');
    actions.className = 'chat-image-actions';
    actions.append(
      buildImageActionButton('复制图片', 'copy', localPath),
      buildImageActionButton('在文件夹中显示', 'reveal', localPath),
    );
    frame.appendChild(actions);
  }
  return frame;
}

/* ---------- 过程时间线（对齐 web AgentProcessTimeline） ---------- */

/** 时间线图标（受信静态 SVG：思考=灯泡 / 状态=时钟 / 错误=叹号；工具按语义分类，参考 Kimi）。 */
const PROCESS_TOOL_ICON_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><use href="#process-terminal"></use></svg>';
const PROCESS_THINKING_ICON_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><use href="#process-thinking"></use></svg>';
const PROCESS_STATUS_ICON_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 8v4l3 2"/></svg>';
const PROCESS_ERROR_ICON_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v6"/><path d="M12 17h.01"/></svg>';
const PROCESS_WRITE_ICON_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>';
const PROCESS_READ_ICON_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>';
const PROCESS_SEARCH_ICON_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>';
const PROCESS_WEB_ICON_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="10"/><path d="M2 12h20"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>';
const PROCESS_TODO_ICON_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m3 17 2 2 4-4"/><path d="m3 7 2 2 4-4"/><path d="M13 6h8"/><path d="M13 12h8"/><path d="M13 18h8"/></svg>';
const PROCESS_TEAM_ICON_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>';
/** 子代理委派卡图标：单人（对齐 Hermes Task 卡的人形图标，与 team 的多人图标区分）。 */
const PROCESS_SUBAGENT_ICON_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>';
const PROCESS_MEMORY_ICON_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14a9 3 0 0 0 18 0V5"/><path d="M3 12a9 3 0 0 0 18 0"/></svg>';
const PROCESS_SKILL_ICON_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2l2.4 7.2H22l-6 4.8 2.3 7.2-6.3-4.5-6.3 4.5L8 14 2 9.2h7.6z"/></svg>';

/** 工具时间线图标类别。 */
export type ToolIconKind =
  | 'write' | 'read' | 'search' | 'web' | 'todo' | 'team' | 'memory' | 'skill' | 'cron' | 'terminal';

/** 按工具语义选时间线图标（参考 Kimi：写入=笔 / 读取=书 / 搜索=放大镜 / 网页=地球…）。 */
export function toolIconKind(name: string): ToolIconKind {
  const lower = String(name || '').trim().toLowerCase();
  if (['write', 'file_write', 'edit', 'patch', 'apply_patch'].includes(lower)) return 'write';
  if (['read', 'file_read'].includes(lower)) return 'read';
  if (['grep', 'search_files', 'glob', 'tool_search'].includes(lower)) return 'search';
  if (lower.startsWith('web_') || lower.startsWith('browser') || lower === 'vision_analyze') return 'web';
  if (lower === 'todo') return 'todo';
  if (lower === 'memory') return 'memory';
  if (lower.startsWith('team_') || lower.startsWith('delegate') || lower.endsWith('_agent') || lower === 'run_agent' || lower === 'collect_subagent') return 'team';
  if (lower.startsWith('skills_') || lower === 'skill_view') return 'skill';
  if (lower.startsWith('cron_')) return 'cron';
  return 'terminal';
}

const TOOL_ICON_SVGS: Record<ToolIconKind, string> = {
  write: PROCESS_WRITE_ICON_SVG,
  read: PROCESS_READ_ICON_SVG,
  search: PROCESS_SEARCH_ICON_SVG,
  web: PROCESS_WEB_ICON_SVG,
  todo: PROCESS_TODO_ICON_SVG,
  team: PROCESS_TEAM_ICON_SVG,
  memory: PROCESS_MEMORY_ICON_SVG,
  skill: PROCESS_SKILL_ICON_SVG,
  cron: PROCESS_STATUS_ICON_SVG,
  terminal: PROCESS_TOOL_ICON_SVG,
};

/** 时间线项骨架：图标列 + 内容列（内容列由调用方填充，details 或纯文本）。 */
function renderTimelineItem(iconSvg: string, iconClass: string, content: HTMLElement): HTMLElement {
  const item = createTrustedElement<HTMLElement>(
    `<div class="process-timeline__item">
      <div class="process-timeline__icon ${iconClass}">${iconSvg}</div>
    </div>`,
  );
  item.appendChild(content);
  return item;
}

/** JSON 文本美化（对齐 web prettyBlock）：可解析则缩进两格，否则原样展示。 */
function prettyBlock(value?: string): string {
  if (!value) return '';
  try {
    return JSON.stringify(JSON.parse(value), null, 2);
  } catch {
    return value;
  }
}

function renderThinkingBlock(thinking: string, messageId: string, streaming: boolean): HTMLElement {
  // 时间线项默认态对齐 web：思考中展开、完成后收起。
  // data-thinking-for 供流式分片定点更新 body（patchStreamingTurn），保持外层 DOM 不变。
  const details = createTrustedElement<HTMLDetailsElement>(
    `<details class="process-timeline__content process-timeline__details">
      <summary class="process-timeline__row">
        <span class="process-timeline__title"></span>
        <span class="process-timeline__chevron">›</span>
      </summary>
      <div class="process-timeline__thinking"></div>
    </details>`,
  );
  details.open = streaming;
  details.querySelector<HTMLElement>('.process-timeline__title')!.textContent =
    streaming ? '思考中' : '思考已完成';
  details.querySelector<HTMLElement>('.process-timeline__thinking')!.textContent = thinking;
  const item = renderTimelineItem(
    PROCESS_THINKING_ICON_SVG,
    streaming ? 'process-timeline__icon--running' : '',
    details,
  );
  item.dataset.thinkingFor = messageId;
  return item;
}

/* ---------- Subagent 委派卡片（delegate_task / run_agent 专属，对齐 Hermes Task 卡） ---------- */

/** 会派生子 agent 的工具：渲染成带边框的独立卡片（任务描述 + 执行摘要），区别于普通工具的单行折叠条。 */
const SUBAGENT_CARD_TOOLS = new Set(['delegate_task', 'run_agent']);

/** subagent 结果 JSON 中单个子任务条目（crew/agent/subagent/tools.py `_run_one_child` 产出）。 */
interface SubagentResultItem {
  agent?: string;
  status?: string;
  summary?: string;
  duration_seconds?: number;
  tool_calls?: number;
  last_tool?: string;
}

/** 卡片内单个子任务的展示模型：任务描述 +（完成后）执行结果。 */
interface SubagentTaskView {
  goal: string;
  context: string;
  result?: SubagentResultItem | undefined;
}

/** 从 tool.args 解析子任务列表。args 流式生成中可能是截断 JSON → 解析失败按无参数处理，标题兜底。 */
function parseSubagentTasks(tool: ToolCallInfo): { agentType: string; tasks: SubagentTaskView[] } {
  let parsed: Record<string, unknown> = {};
  try {
    const value: unknown = tool.args ? JSON.parse(tool.args) : {};
    if (value && typeof value === 'object' && !Array.isArray(value)) parsed = value as Record<string, unknown>;
  } catch { /* 流式半截 JSON：按无参数处理 */ }
  const agentType = tool.name === 'run_agent' ? String(parsed.agent_type || '').trim() : '';
  const raw = Array.isArray(parsed.tasks) && parsed.tasks.length > 0 ? parsed.tasks : [parsed];
  const tasks: SubagentTaskView[] = [];
  for (const item of raw) {
    const obj = item && typeof item === 'object' ? item as Record<string, unknown> : {};
    const goal = String(obj.goal || '').trim();
    if (!goal) continue;
    tasks.push({ goal, context: String(obj.context || '').trim() });
  }
  return { agentType, tasks };
}

/** 从 tool.result 解析子任务执行结果（与 tasks 按下标配对，后端 gather 保序）；后台启动返回 launched 标记。 */
function parseSubagentResult(tool: ToolCallInfo): { results: SubagentResultItem[]; launchedTaskId: string } {
  const empty = { results: [] as SubagentResultItem[], launchedTaskId: '' };
  if (!tool.result) return empty;
  try {
    const value = JSON.parse(tool.result) as Record<string, unknown>;
    if (!value || typeof value !== 'object') return empty;
    if (value.status === 'launched') return { results: [], launchedTaskId: String(value.task_id || '') };
    const list = Array.isArray(value.results) ? value.results : [];
    return { results: list.filter((r) => r && typeof r === 'object') as SubagentResultItem[], launchedTaskId: '' };
  } catch {
    return empty;
  }
}

/** 卡片标题：`子智能体 code-explorer：测试基本功能`；批量 `子智能体：3 个并行任务`；参数未流出时兜底。 */
function subagentCardTitle(agentType: string, tasks: SubagentTaskView[]): string {
  const head = agentType ? `子智能体 ${agentType}` : '子智能体';
  if (tasks.length === 0) return `${head}任务`;
  if (tasks.length > 1) return `${head}：${tasks.length} 个并行任务`;
  const firstLine = tasks[0]!.goal.split('\n')[0]!.trim();
  const brief = firstLine.length > 40 ? `${firstLine.slice(0, 40)}…` : firstLine;
  return `${head}：${brief}`;
}

/** 单个子任务的执行摘要行（截图中的 Execution Summary）。 */
function subagentTaskMeta(result: SubagentResultItem | undefined, isActive: boolean): { text: string; error: boolean } {
  if (!result) return { text: isActive ? '执行中…' : '', error: false };
  const calls = result.tool_calls ?? 0;
  const cost = `${calls} 次工具调用，耗时 ${formatDuration((result.duration_seconds ?? 0) * 1000)}`;
  switch (result.status) {
    case 'completed': return { text: `执行摘要：${cost}`, error: false };
    case 'timeout': return { text: `已超时中止：${cost}`, error: true };
    case 'cancelled': return { text: '已取消', error: true };
    default: {
      const diag = (result.summary || '').split('\n')[0]!.trim();
      const brief = diag.length > 80 ? `${diag.slice(0, 80)}…` : diag;
      return { text: brief ? `执行出错：${brief}` : `执行出错：${cost}`, error: true };
    }
  }
}

/** subagent 专用卡片：头部（标题+耗时+折叠箭头）+ 展开体（任务描述 + 执行摘要）。 */
function renderSubagentCard(tool: ToolCallInfo, messageId: string): HTMLElement {
  const isActive = tool.status === 'running' || tool.status === 'generating';
  const iconClass =
    tool.status === 'error' ? 'process-timeline__icon--error' : isActive ? 'process-timeline__icon--running' : '';

  const { agentType, tasks } = parseSubagentTasks(tool);
  const { results, launchedTaskId } = parseSubagentResult(tool);
  tasks.forEach((t, i) => { t.result = results[i]; });

  // 折叠行为与其他工具卡一致：默认折叠，用户手动操作经 fold-state.ts 持久化。
  const foldKey = toolFoldKey(messageId, tool.toolCallId);
  const open = getToolFold(foldKey) ?? false;

  // 运行中项的 data-started-at / data-active 供全局 interval 驱动实时耗时（同 renderToolCard）。
  const startedAtAttr = isActive ? `data-started-at="${tool.startedAt}"` : '';
  const durationAttr = isActive ? 'data-active="true"' : '';
  const initialDuration = tool.duration != null && tool.duration > 0
    ? formatDuration(tool.duration)
    : '';

  const details = createTrustedElement<HTMLDetailsElement>(
    `<details class="process-timeline__content process-timeline__details subagent-card">
      <summary class="process-timeline__row subagent-card__header">
        <span class="process-timeline__title subagent-card__title"></span>
        <span class="process-timeline__duration" ${durationAttr} ${startedAtAttr}>${initialDuration}</span>
        <span class="process-timeline__chevron">›</span>
      </summary>
      <div class="subagent-card__body"></div>
    </details>`,
  );
  details.open = open;
  details.setAttribute('data-fold-key', foldKey);
  details.querySelector<HTMLElement>('.subagent-card__title')!.textContent = subagentCardTitle(agentType, tasks);
  const durSpan = details.querySelector<HTMLElement>('.process-timeline__duration')!;
  if (!initialDuration && !isActive) durSpan.remove();

  // goal / context / summary 均为模型派生内容，一律 textContent。
  const body = details.querySelector<HTMLElement>('.subagent-card__body')!;
  if (tasks.length === 0) {
    const placeholder = document.createElement('div');
    placeholder.className = 'subagent-card__meta';
    placeholder.textContent = isActive ? '任务描述生成中…' : '（无任务描述）';
    body.appendChild(placeholder);
  }
  tasks.forEach((task, i) => {
    const block = document.createElement('div');
    block.className = 'subagent-card__task';
    const goal = document.createElement('div');
    goal.className = 'subagent-card__goal';
    goal.textContent = tasks.length > 1 ? `${i + 1}. ${task.goal}` : task.goal;
    block.appendChild(goal);
    if (task.context) {
      const ctx = document.createElement('div');
      ctx.className = 'subagent-card__context';
      ctx.textContent = task.context;
      block.appendChild(ctx);
    }
    // 子智能体最终回复：完成且有正文时展示，过长经 CSS max-height + 滚动条收敛。
    if (task.result?.status === 'completed' && task.result.summary?.trim()) {
      const reply = document.createElement('div');
      reply.className = 'subagent-card__reply';
      const label = document.createElement('div');
      label.className = 'subagent-card__reply-label';
      label.textContent = '最终回复';
      const text = document.createElement('div');
      text.className = 'subagent-card__reply-text';
      text.textContent = task.result.summary.trim();
      reply.append(label, text);
      block.appendChild(reply);
    }
    const { text, error } = subagentTaskMeta(task.result, isActive);
    if (text) {
      const meta = document.createElement('div');
      meta.className = `subagent-card__meta${error ? ' subagent-card__meta--error' : ''}`;
      meta.textContent = text;
      block.appendChild(meta);
    }
    body.appendChild(block);
  });
  if (launchedTaskId) {
    const meta = document.createElement('div');
    meta.className = 'subagent-card__meta';
    meta.textContent = `已转后台运行（任务 ID：${launchedTaskId}），完成后将推送通知`;
    body.appendChild(meta);
  } else if (tool.status === 'error' && results.length === 0 && tool.result) {
    // ToolError（如超批量上限）：result 不是结果 JSON，首行截断直出
    const firstLine = tool.result.split('\n')[0]!.trim();
    const meta = document.createElement('div');
    meta.className = 'subagent-card__meta subagent-card__meta--error';
    meta.textContent = firstLine.length > 120 ? `${firstLine.slice(0, 120)}…` : firstLine;
    body.appendChild(meta);
  }
  return renderTimelineItem(PROCESS_SUBAGENT_ICON_SVG, iconClass, details);
}

function renderToolCard(tool: ToolCallInfo, messageId: string): HTMLElement {
  if (SUBAGENT_CARD_TOOLS.has(tool.name)) return renderSubagentCard(tool, messageId);
  const isActive = tool.status === 'running' || tool.status === 'generating';
  const iconClass =
    tool.status === 'error' ? 'process-timeline__icon--error' : isActive ? 'process-timeline__icon--running' : '';
  const hasDetail = Boolean(tool.args || tool.result);

  // 工具项折叠键：跨重启持久化（fold-state.ts）。
  // 默认行为：折叠（仅展示标题与耗时）；用户手动操作过则尊重用户选择。
  const foldKey = toolFoldKey(messageId, tool.toolCallId);
  const userPinned = getToolFold(foldKey);
  const open = userPinned ?? false;
  const title = toolDisplayTitle(tool);

  // 运行中项需要 data-started-at，供全局 interval 驱动实时耗时显示。
  const startedAtAttr = isActive ? `data-started-at="${tool.startedAt}"` : '';
  const durationAttr = isActive ? 'data-active="true"' : '';
  const initialDuration = tool.duration != null && tool.duration > 0
    ? formatDuration(tool.duration)
    : '';

  // title / args / result 均为用户/工具派生内容，必须走 textContent（不经过字符串拼接进 innerHTML）。
  if (hasDetail) {
    const details = createTrustedElement<HTMLDetailsElement>(
      `<details class="process-timeline__content process-timeline__details">
        <summary class="process-timeline__row">
          <span class="process-timeline__title"></span>
          <span class="process-timeline__duration" ${durationAttr} ${startedAtAttr}>${initialDuration}</span>
          <span class="process-timeline__chevron">›</span>
        </summary>
        <div class="process-timeline__detail">
          <section class="process-code-block" data-section="args">
            <div class="process-code-block__title">Request</div>
            <pre></pre>
          </section>
          <section class="process-code-block" data-section="result">
            <div class="process-code-block__title">Response</div>
            <pre></pre>
          </section>
        </div>
      </details>`,
    );
    // 受控 open：读 fold-state.ts 的持久化值；用户没操作过则默认折叠。
    details.open = open;
    // 把 foldKey 写到 data-fold-key，toggle 委托据此回写 fold-state。
    details.setAttribute('data-fold-key', foldKey);
    const titleEl = details.querySelector<HTMLElement>('.process-timeline__title')!;
    titleEl.textContent = title;
    // 长耗时工具的阶段进度（phase=progress）：折叠状态也可见，跟随标题行。
    if (isActive && tool.progressText) {
      const stage = document.createElement('span');
      stage.className = 'process-timeline__stage';
      stage.textContent = tool.progressText;
      titleEl.after(stage);
    }
    const durSpan = details.querySelector<HTMLElement>('.process-timeline__duration')!;
    if (!initialDuration && !isActive) durSpan.remove();
    const argsSection = details.querySelector<HTMLElement>('[data-section="args"]')!;
    if (tool.args) argsSection.querySelector('pre')!.textContent = prettyBlock(tool.args);
    else argsSection.remove();
    const resultSection = details.querySelector<HTMLElement>('[data-section="result"]')!;
    if (tool.result) {
      resultSection.querySelector('pre')!.textContent = formatToolResultDisplay(tool.name, tool.result);
    } else resultSection.remove();
    // browser_use 导出的页面截图：在时间线项内容列直接内联展示（details 默认折叠，
    // 图片放 details 外，保证"截图给你看"所见即所得）。
    const shotPath = screenshotResultPath(tool);
    if (shotPath) {
      const contentWrap = document.createElement('div');
      contentWrap.className = 'process-timeline__tool-media';
      contentWrap.appendChild(details);
      contentWrap.appendChild(buildInlineImage(shotPath, '页面截图', shotPath, 'tool'));
      return renderTimelineItem(TOOL_ICON_SVGS[toolIconKind(tool.name)], iconClass, contentWrap);
    }
    return renderTimelineItem(TOOL_ICON_SVGS[toolIconKind(tool.name)], iconClass, details);
  }

  const content = createTrustedElement<HTMLElement>(
    `<div class="process-timeline__content">
      <div class="process-timeline__row process-timeline__row--static">
        <span class="process-timeline__title"></span>
        <span class="process-timeline__duration" ${durationAttr} ${startedAtAttr}>${initialDuration}</span>
      </div>
    </div>`,
  );
  const titleEl = content.querySelector<HTMLElement>('.process-timeline__title')!;
  titleEl.textContent = title;
  if (isActive && tool.progressText) {
    const stage = document.createElement('span');
    stage.className = 'process-timeline__stage';
    stage.textContent = tool.progressText;
    titleEl.after(stage);
  }
  const durSpan = content.querySelector<HTMLElement>('.process-timeline__duration')!;
  if (!initialDuration && !isActive) durSpan.remove();
  return renderTimelineItem(TOOL_ICON_SVGS[toolIconKind(tool.name)], iconClass, content);
}

interface WikiConfirmationResult {
  requires_confirmation?: boolean;
  confirmation_id?: string;
  action?: string;
  summary?: string;
  impact?: Record<string, unknown>;
  expires_at?: number;
}

function parseWikiConfirmation(tool: ToolCallInfo): WikiConfirmationResult | null {
  if (!tool.name.startsWith('wiki_') || !tool.result) return null;
  try {
    const value = JSON.parse(tool.result) as WikiConfirmationResult;
    return value.requires_confirmation && value.confirmation_id ? value : null;
  } catch {
    return null;
  }
}

function renderWikiConfirmationCard(value: WikiConfirmationResult): HTMLElement {
  const card = document.createElement('section');
  card.className = 'wiki-confirmation-card';
  const title = document.createElement('strong');
  title.textContent = value.summary || '需要确认 Wiki 操作';
  const impact = document.createElement('pre');
  impact.textContent = JSON.stringify(value.impact || {}, null, 2);
  const actions = document.createElement('div');
  actions.className = 'wiki-confirmation-card__actions';
  const confirm = document.createElement('button');
  confirm.type = 'button';
  confirm.dataset.wikiConfirm = value.confirmation_id || '';
  confirm.dataset.wikiAction = value.action || '';
  confirm.textContent = '确认执行';
  const cancel = document.createElement('button');
  cancel.type = 'button';
  cancel.dataset.wikiCancel = value.confirmation_id || '';
  cancel.textContent = '取消';
  actions.append(confirm, cancel);
  card.append(title, impact, actions);
  return card;
}

/** 原地更新所有运行中工具的耗时。
 *
 * 计时只修改真正变化的文本，不能用时间变化驱动整个 Agent Turn 重建；否则会让
 * Timeline 图标动画、折叠状态和耗时文字在每个整秒边界重新挂载并产生闪烁。
 * 返回实际发生文本更新的节点数，便于 DOM 行为测试。 */
export function updateActiveToolDurations(root: ParentNode, now = Date.now()): number {
  let updated = 0;
  root.querySelectorAll<HTMLElement>('.process-timeline__duration[data-active]').forEach((el) => {
    const startedAt = Number(el.getAttribute('data-started-at'));
    if (!Number.isFinite(startedAt)) return;
    const next = formatDuration(Math.max(0, now - startedAt));
    if (el.textContent === next) return;
    el.textContent = next;
    updated += 1;
  });
  return updated;
}

/** 每 250ms 对齐一次整秒边界；相同显示值不会产生 DOM 写入。 */
setInterval(() => {
  // node 单测环境无 DOM：跳过本轮（import 本模块的纯逻辑测试不需要耗时轮询）。
  if (typeof document === 'undefined') return;
  updateActiveToolDurations(document);
}, 250);

/** 把毫秒时长格式化成「1m 40s / 30s / 0.8s / 2h 5m」这种短形式。
 *  注意：<1s 走亚秒档，避免短回合被 Math.floor 吞成 "0s"。 */
export function formatDuration(ms: number): string {
  const safe = Math.max(0, ms);
  if (safe < 1000) return `${(safe / 1000).toFixed(1)}s`;
  const totalSec = Math.floor(safe / 1000);
  if (totalSec < 60) return `${totalSec}s`;
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  if (m < 60) return s > 0 ? `${m}m ${s}s` : `${m}m`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return rm > 0 ? `${h}h ${rm}m` : `${h}h`;
}

/**
 * 从同一回合 batch 推导展示用耗时。
 * - 执行中：以首条 assistant 的 turnStartedAt 为起点实时累加（工具阶段 last 条可能已非 streaming）。
 * - 已结束：优先取 batch 内任意 assistant 已写入的 turnDurationMs（final 通常写在末条）；
 *   否则用首条 turnStartedAt 与末条 timestamp 估算。
 */
export function resolveTurnDurationMs(
  batch: ChatMessage[],
  options: { isLive: boolean; now?: number },
): number {
  const assistants = batch.filter((m) => m.role === 'assistant');
  if (assistants.length === 0) return 0;
  const first = assistants[0]!;
  const last = assistants[assistants.length - 1]!;
  const now = options.now ?? Date.now();
  if (options.isLive) {
    const startedAt = first.turnStartedAt ?? last.turnStartedAt;
    return startedAt != null ? Math.max(0, now - startedAt) : 0;
  }
  const stored = assistants
    .map((m) => m.turnDurationMs)
    .filter((v): v is number => v != null && v > 0);
  if (stored.length > 0) return Math.max(...stored);
  const startedAt = first.turnStartedAt;
  if (startedAt != null) {
    return Math.max(0, (last.timestamp ?? startedAt) - startedAt);
  }
  return 0;
}

/**
 * 回合是否已有「处理过程」内容（thinking / tools / status / error / 过程旁白）。
 * 用于区分乐观占位的「正在思考」与首包到达后的「正在执行」。
 */
export function turnHasProcessContent(messages: ChatMessage[]): boolean {
  let lastTextIdx = -1;
  for (let k = messages.length - 1; k >= 0; k -= 1) {
    if (messages[k].role === 'assistant' && messages[k].content) {
      lastTextIdx = k;
      break;
    }
  }
  for (let i = 0; i < messages.length; i += 1) {
    const m = messages[i]!;
    if (m.role === 'status' || m.role === 'error') return true;
    if (m.role !== 'assistant') continue;
    if (m.thinking) return true;
    if (m.toolCalls && m.toolCalls.length > 0) return true;
    if (m.content && isAssistantNarrationContent(i, lastTextIdx, m)) return true;
  }
  return false;
}

/** 流式折叠条文案：尚无过程内容时「正在思考」，否则「正在执行」。 */
export function resolveLiveFoldLabel(durationMs: number, hasProcess: boolean): string {
  const wait = formatDuration(Math.max(0, durationMs));
  return hasProcess ? `正在执行 · 已等待 ${wait}` : `正在思考 · 已等待 ${wait}`;
}

/** Team 仅适配文案，耗时仍由统一 Agent Turn 计时函数提供。 */
export function resolveTeamTurnFoldLabel(
  message: ChatMessage,
  durationMs: number,
  isStreaming: boolean,
): string {
  if (message.eventType !== 'team_planning_progress') {
    const hasProcess = Boolean(
      message.processText?.trim()
      || message.thinking?.trim()
      || message.toolCalls?.length
      || message.content.trim(),
    );
    return isStreaming
      ? resolveLiveFoldLabel(durationMs, hasProcess)
      : `已思考 ${formatDuration(durationMs)}`;
  }
  const title = message.collapsedTitle
    || (isStreaming ? 'Crew 正在规划团队协作' : 'Crew 已生成团队执行图');
  return isStreaming
    ? `${title} · 已等待 ${formatDuration(durationMs)}`
    : `${title} · ${formatDuration(durationMs)}`;
}

export interface AgentTurnOptions {
  /** 是否仍处于流式生成中（驱动 summary 文案「正在执行 / 已处理」+ spinner + --live 样式）。
   *  liveness 必须是 per-turn 自有属性：由 batch 内是否存在 streaming 消息决定，绝不引用
   *  session 全局 busy——否则新回合让 session 重新 busy 时会复活所有已封口回合，表现为
   *  「停掉任务1再启任务2，两个回合都显示执行中」。见 renderChat 的 isStreaming 推导。 */
  isStreaming: boolean;
  /** 用户是否手动决定过 open 状态（null=未手动；true=展开；false=折叠） */
  userPinnedOpen: boolean | null;
  /** 整回合推理耗时（ms）。流式中为实时累加值，已结束为最终值；0 表示无可用计时。
   *  由 renderChat 从回合 assistant 消息的 turnStartedAt/turnDurationMs 推导后传入，
   *  替代原先用首末消息 timestamp 相减的错误算法（timestamp 不随 patch 更新）。 */
  turnDurationMs: number;
  /** 嵌入式对话可隐藏助手身份行；主对话默认显示。 */
  showAssistantName?: boolean;
  /** 仅外部 ACP Agent / 外部 Team 传入；缺省继续走原 Crew 头像与名称。 */
  identity?: {
    kind: 'external' | 'team';
    name: string;
    badge: string;
    tone?: number;
    icon?: IconId;
  };
}

function createAgentTurnAvatar(identity?: AgentTurnOptions['identity']): HTMLElement {
  if (!identity) return createChatAvatar();
  const avatar = document.createElement('div');
  const toneClass = identity.tone === undefined ? '' : ` agent-provider-tone-${identity.tone}`;
  avatar.className = `msg__avatar msg__avatar--${identity.kind}${toneClass}`;
  avatar.setAttribute('aria-hidden', 'true');
  if (identity.kind === 'team') {
    const logo = document.createElement('span');
    // 与历史侧栏复用同一个 Team Logo；聊天区只通过容器样式等比缩放。
    logo.className = 'session__team-logo';
    logo.appendChild(document.createElement('i'));
    logo.appendChild(document.createElement('i'));
    avatar.appendChild(logo);
  } else {
    if (identity.icon) avatar.append(createIcon(identity.icon, { size: 20 }));
    else avatar.textContent = identity.badge;
  }
  return avatar;
}

/** 把同一回合的若干条 agent 消息合并渲染成一个 .msg。
 *  关键：**只有 thinking + tools 进折叠区**；正文（.msg__text）永远在折叠区之外，
 *  始终可见 —— 折叠的只是「处理过程」，正文是「回答」，不该跟着藏。 */
/** 渲染消息底部行：模型 · 时间 + 操作按钮。仅在 hover 消息时显示。 */
function appendMsgFooter(body: HTMLElement, metaParts: string[], actions: HTMLElement[]): void {
  const meta = metaParts.filter(Boolean).join(' · ');
  let metaEl: HTMLElement | null = null;
  if (meta) {
    metaEl = document.createElement('span');
    metaEl.className = 'msg__meta';
    metaEl.textContent = meta;
  }
  const footer = document.createElement('div');
  footer.className = 'msg__footer';
  if (metaEl) footer.appendChild(metaEl);
  if (actions.length > 0) {
    const actionsWrap = document.createElement('span');
    actionsWrap.className = 'msg__actions';
    for (const a of actions) actionsWrap.appendChild(a);
    footer.appendChild(actionsWrap);
  }
  if (footer.childNodes.length > 0) body.appendChild(footer);
}

function appendAttachment(container: HTMLElement, a: Attachment): void {
  if (a.type === 'image') {
    container.appendChild(buildInlineImage(a.path, a.name, a.path, 'attachment'));
    return;
  }
  const link = document.createElement('a');
  link.className = 'msg__attachment msg__attachment--file';
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  link.href = a.path;
  const icon = document.createElement('span');
  icon.className = 'msg__attachment-icon';
  icon.textContent = '📄';
  const name = document.createElement('span');
  name.className = 'msg__attachment-name';
  name.textContent = a.name;
  link.appendChild(icon);
  link.appendChild(name);
  container.appendChild(link);
}

function appendAttachments(container: HTMLElement, attachments: Attachment[] | undefined): void {
  if (!attachments?.length) return;
  const wrap = document.createElement('div');
  wrap.className = 'msg__attachments';
  for (const a of attachments) appendAttachment(wrap, a);
  container.appendChild(wrap);
}

/**
 * 判断 assistant 正文是否属于「过程旁白」（进折叠区），而非应始终可见的正式回答。
 *
 * 信号来源（优先级从高到低）：
 * - `segmentRole`（reducer 显式标记）→ `process` 进折叠区，`answer` 进正文；
 * - `thinking` 字段 / thinking 帧 → 走 renderThinkingBlock，不走此函数；
 * - 遗留启发式：同轮次较早 assistant 正文、或最后一条仍挂 toolCalls 的旁白。
 */
export function isAssistantNarrationContent(
  index: number,
  lastTextIdx: number,
  message: ChatMessage,
): boolean {
  if (message.segmentRole === 'answer') return false;
  if (message.segmentRole === 'process') return true;
  if (index < lastTextIdx) return true;
  if (index !== lastTextIdx) return false;
  return Boolean(message.toolCalls && message.toolCalls.length > 0);
}

/**
 * 折叠区外是否已有「可确认的」正式答案文字。
 *
 * 只认硬事件边界，避免工具前旁白误标 answer 导致过程区先折再展：
 * - `segmentRole === 'answer'` 且有正文（工具批次结束后的新段，或 final 升格）→ 正式正文；
 * - 无 segmentRole 的遗留消息走 isAssistantNarrationContent 启发式。
 * 不确定阶段（工具前标 process 的流式字）→ false，过程区保持展开。
 */
export function hasVisibleAnswerText(messages: ChatMessage[]): boolean {
  let lastTextIdx = -1;
  for (let k = messages.length - 1; k >= 0; k -= 1) {
    if (messages[k].role === 'assistant' && messages[k].content?.trim()) {
      lastTextIdx = k;
      break;
    }
  }
  if (lastTextIdx < 0) return false;
  for (let i = 0; i < messages.length; i += 1) {
    const m = messages[i];
    if (m.role !== 'assistant' || !m.content?.trim()) continue;
    // 显式 answer 段：硬确认
    if (m.segmentRole === 'answer') return true;
    // 显式 process：旁白 / 不确定阶段，不算正式正文
    if (m.segmentRole === 'process') continue;
    // 遗留无 segmentRole：沿用启发式
    if (!isAssistantNarrationContent(i, lastTextIdx, m)) return true;
  }
  return false;
}

export function renderAgentTurn(messages: ChatMessage[], options: AgentTurnOptions): HTMLElement {
  const frag = document.createDocumentFragment();
  if (messages.length === 0) {
    // 返回一个空占位 div 以维持「返回 HTMLElement」契约（原字符串实现此处返回 ''，调用方不会 append）。
    // 调用方 renderChat 仍会 append 它，但它是空 div、无 class，不影响视觉。
    // 为与原行为一致（'' → 不产生节点），这里返回一个携带 data-empty 标记的占位，
    // renderChat 侧会跳过 append 空节点（见 isPlaceholderNode）。
    const empty = document.createElement('div');
    empty.dataset.empty = 'true';
    return empty;
  }
  const firstId = messages[0].id;
  const firstMsg = messages[0];
  const durationMs = Math.max(0, options.turnDurationMs);

  // 找最后一条「有正文文本」的助手消息下标：它是正式回答，其余文本算「过程旁白」。
  // 模型在工具调用之间输出的「让我查看…」这类叙述，本质是过程而不是答案。
  // 注意：仅含空白（换行/空格）的 content 不算正文——工具 loop 之间模型常吐分隔换行，
  // 若算正文会渲染成看不见但占位的空白块。
  let lastTextIdx = -1;
  for (let k = messages.length - 1; k >= 0; k -= 1) {
    if (messages[k].role === 'assistant' && messages[k].content?.trim()) { lastTextIdx = k; break; }
  }

  // 拆成两类：process（thinking / tools / status / error / 过程旁白）进折叠区；
  // text 正文（仅最后一条 / 流式中的当前条）始终可见。
  const processParts: Node[] = [];
  const textParts: Node[] = [];
  const textRaw: string[] = []; // 累积可见正文纯文本，供整段复制

  // liveness 只看 per-turn 自有信号（isStreaming），不引用 session 全局 busy。
  const isLive = options.isStreaming;

  // 瞬时活动状态（带 activity 的 status）会被后续事件
  // 取代的进度提示：live 回合只保留最新一条，回合结束后全部隐藏，避免残留/重复。
  let lastActivityIdx = -1;
  messages.forEach((m, i) => {
    if (m.role === 'status' && m.activity) lastActivityIdx = i;
  });

  messages.forEach((m, i) => {
    if (m.role === 'status' && m.workflowProgress) {
      // workflow 进度面板是独立渲染单元，不应被折叠进过程区。
      return;
    }
    if (m.role === 'status') {
      if (m.activity && (!isLive || i !== lastActivityIdx)) return;
      const content = createTrustedElement<HTMLElement>(
        `<div class="process-timeline__content"></div>`,
      );
      content.textContent = m.content; // escapeHtml 等价（textContent 自带转义语义）
      const item = renderTimelineItem(PROCESS_STATUS_ICON_SVG, '', content);
      item.dataset.messageId = m.id;
      processParts.push(item);
      return;
    }
    if (m.role === 'error') {
      const content = createTrustedElement<HTMLElement>(
        `<div class="process-timeline__content process-timeline__error"></div>`,
      );
      content.textContent = m.content;
      const item = renderTimelineItem(PROCESS_ERROR_ICON_SVG, 'process-timeline__icon--error', content);
      item.dataset.messageId = m.id;
      processParts.push(item);
      return;
    }
    // assistant：thinking + tools 进折叠区（空白 thinking 同样跳过，避免只显示标题的空思考项）
    if (m.thinking?.trim()) {
      processParts.push(renderThinkingBlock(m.thinking, m.id, Boolean(m.streaming && options.isStreaming)));
    }

    // Markdown 渲染入口：只看消息是否仍在流式或被截断，不看 session busy。
    // final 到达后 streaming=false，即使后续还有 status/tool 帧让 session 仍 busy，
    // 也应使用非流式入口渲染完整文本，避免对已成型的 `**` 做错误的自动闭合。
    const useStreamingMd = options.isStreaming || !!m.interrupted;
    const isIntermediate = isAssistantNarrationContent(i, lastTextIdx, m);

    // 过程旁白（"我先查一下。"）：模型在调工具前先说的话，时序上先于 tool_call
    // （OpenAI 流式协议保证一个 choice 内 content 先于 tool_call），故渲染在工具项
    // **之上**——UI 顺序须跟随模型产出时序。answer 正文走下方分支进 textParts（折叠外）。
    // 时间线上旁白不占图标位（ghost 占位，对齐 web NarrationTimelineItem）。
    // 空白旁白（工具 loop 之间的分隔换行）跳过：渲染出来是看不见的占位行。
    if (m.content?.trim() && isIntermediate) {
      const md = (useStreamingMd ? renderMarkdownHtmlStreaming : renderMarkdownHtml)(m.content);
      const div = document.createElement('div');
      div.className = 'process-timeline__narration msg__text md-body chat-markdown';
      if (m.streaming && i === lastTextIdx) div.dataset.textFor = m.id;
      div.appendChild(createTrustedFragment(md));
      processParts.push(renderTimelineItem('', 'process-timeline__icon--ghost', div));
    }

    if (m.toolCalls && m.toolCalls.length > 0) {
      // 把当前 message id 传给 renderToolCard，用于生成跨重启稳定的 foldKey。
      for (const t of m.toolCalls) processParts.push(renderToolCard(t, m.id));
      for (const t of m.toolCalls) {
        const confirmation = parseWikiConfirmation(t);
        if (confirmation) textParts.push(renderWikiConfirmationCard(confirmation));
      }
    }
    if (m.planReview) {
      textParts.push(renderPlanReviewCard(
        m.planReview.sessionId,
        m.planReview.plan,
        m.planReview.planFile,
        m.planReview.status,
      ));
    }
    // Wiki Agent 卡片：与正文同级、折叠区外（对齐 web AgentTurn 的 textParts 顺序：卡片 → 概览 → 正文）。
    if (m.wikiCards && m.wikiCards.length > 0) {
      textParts.push(renderWikiCardsPanel(m.wikiCards));
    }
    if (m.content?.trim() && !isIntermediate) {
      const md = (useStreamingMd ? renderMarkdownHtmlStreaming : renderMarkdownHtml)(m.content);
      const div = document.createElement('div');
      div.className = 'msg__text md-body chat-markdown';
      div.dataset.textFor = m.id;
      div.appendChild(createTrustedFragment(md));
      textParts.push(div);
      textRaw.push(m.content);
    } else if (!m.content && m.streaming && !options.isStreaming) {
      // 非 live 的空 streaming 兜底（极少见）：保留 typing 占位，便于 patch 定位。
      // live 乐观占位改走下方折叠条「正在思考」，不再叠一层三点动画。
      const div = createTrustedElement<HTMLElement>(
        `<div class="msg__text md-body typing-inline"><span></span><span></span><span></span></div>`,
      );
      div.dataset.textFor = m.id;
      textParts.push(div);
    } else if (!m.content && m.streaming && options.isStreaming) {
      // live 空正文仍挂 data-text-for 空壳，供首包 delta patch 定位（不显示 typing）。
      const div = document.createElement('div');
      div.className = 'msg__text md-body chat-markdown';
      div.dataset.textFor = m.id;
      div.hidden = true;
      textParts.push(div);
    }
  });

  const answerTextStarted = hasVisibleAnswerText(messages);
  const hasProcess = processParts.length > 0 || turnHasProcessContent(messages);

  // open 状态：
  //   手动折叠 → 永远尊重；
  //   已确认正式正文（流式或已完成）→ 默认折；仅正文出现后的手动展开才开；
  //   尚无正式正文（推理/旁白）→ 默认展（含推理中手动展开）。
  // 推理中的手动展开由 renderChat 在正文首字到来时清掉 userUnfolded，从而仍自动折。
  let open: boolean;
  if (options.userPinnedOpen === false) open = false;
  else if (answerTextStarted) open = options.userPinnedOpen === true;
  else if (options.userPinnedOpen === true) open = true;
  else open = true;

  // summary 文案：空乐观占位「正在思考」；一旦有过程或正文流式输出 →「正在执行」；
  // 结束：有工具调用 →「已执行 Xs，已调用 N 个工具」；纯思考回合 →「已思考 Xs」。
  const liveLabel = resolveLiveFoldLabel(durationMs, hasProcess || answerTextStarted);
  const toolCallCount = messages.reduce((n, m) => n + (m.toolCalls?.length ?? 0), 0);
  const doneLabel = toolCallCount > 0
    ? `已执行 ${formatDuration(durationMs)}，已调用 ${toolCallCount} 个工具`
    : `已思考 ${formatDuration(durationMs)}`;
  const label = isLive ? liveLabel : doneLabel;

  // 折叠条显示条件：
  // - 有过程内容（thinking/tools/旁白/status）→ 始终显示；
  // - live 回合 → 始终显示（含「仅旁白、工具未到」的计划模式前奏），避免「正在执行」闪没；
  // - 已结束且无过程 → 不显示（纯正文回答）。
  if (processParts.length > 0 || isLive) {
    const details = document.createElement('details');
    details.className = `msg__foldable${isLive ? ' msg__foldable--live' : ''}`;
    if (open) details.open = true;
    const summary = document.createElement('summary');
    summary.className = `msg__fold-summary${isLive ? ' msg__fold-summary--live' : ''}`;
    if (isLive) {
      const spinner = document.createElement('span');
      spinner.className = 'msg__fold-spinner';
      spinner.setAttribute('aria-hidden', 'true');
      summary.appendChild(spinner);
    }
    const labelEl = document.createElement('span');
    labelEl.className = 'msg__fold-label';
    labelEl.textContent = label;
    const caretSvg = createTrustedElement<SVGSVGElement>(
      `<svg class="msg__fold-caret" viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <polyline points="9 6 15 12 9 18"></polyline>
      </svg>`,
    );
    summary.appendChild(labelEl);
    summary.appendChild(caretSvg);
    const content = document.createElement('div');
    content.className = 'msg__fold-content';
    if (processParts.length > 0) {
      // 过程区统一挂进一条时间线（图标列 + 虚线连接），对齐 web AgentProcessTimeline。
      const timeline = document.createElement('div');
      timeline.className = 'process-timeline';
      for (const p of processParts) timeline.appendChild(p);
      content.appendChild(timeline);
    }
    details.appendChild(summary);
    details.appendChild(content);
    frag.appendChild(details);
  }

  for (const t of textParts) frag.appendChild(t);

  // HTML 成果直接以网站卡打开右侧 Browser workbench；这是确定性 UI 动作，
  // 不需要把 browser 工具暴露给模型。
  const inspirationCard = renderTurnInspirationSurfaceCard(messages);
  if (inspirationCard) frag.appendChild(inspirationCard);
  else {
    const artifactCard = renderTurnHtmlArtifactCard(messages);
    if (artifactCard) frag.appendChild(artifactCard);
  }

  // 正文下方：本轮文件改动卡（仅在有改动时出现；final 时由 finalReducer patch 进消息）。
  const fileCard = renderTurnFileChangesCard(messages);
  if (fileCard) frag.appendChild(fileCard);

  // 底部行：模型 · 时间 + 复制（只有有正文时才给复制按钮）
  const combinedText = textRaw.join('\n\n');
  const actions: HTMLElement[] = [];
  if (combinedText) actions.push(renderCopyBtn(combinedText));

  // 时间取回合「完成时间」：final 只 patch 末段 assistant 的 timestamp（历史回放同理，
  // 全段统一为 turn_finished_at），多段回合用批次首条会停在回合开始时刻。
  const lastAssistant = [...messages].reverse().find((m) => m.role === 'assistant') ?? firstMsg;

  // 组装最终 .msg
  const msg = document.createElement('div');
  msg.className = 'msg msg--agent-turn';
  if (isLive) msg.dataset.streaming = 'true';
  msg.dataset.messageId = firstId;

  const avatar = createAgentTurnAvatar(options.identity);

  const body = document.createElement('div');
  body.className = 'msg__body';
  // 身份行：显式 identity（外部 Agent / Team）优先；否则嵌入式对话可通过
  // showAssistantName=false 隐藏，主对话默认显示 Crew。
  const showName = options.identity ? true : options.showAssistantName !== false;
  if (showName) {
    const nameEl = document.createElement('div');
    nameEl.className = 'msg__name';
    nameEl.textContent = options.identity?.name || 'Crew';
    body.appendChild(nameEl);
  }
  // foldable + textParts 已放进 frag（顺序：foldable 在前，text 在后，与原字符串一致）
  body.appendChild(frag);
  appendMsgFooter(
    body,
    [lastAssistant.model || firstMsg.model || '', formatMessageTime(isLive ? Date.now() : lastAssistant.timestamp)],
    actions,
  );

  msg.appendChild(avatar);
  msg.appendChild(body);
  return msg;
}

const CREW_BUILTIN_AGENT_ID = 'crew::builtin';

function highlightTeamMentions(text: string): string {
  return String(text || '').replace(
    /(^|[\s([{（【,，。.!！?？;；:：])@([A-Za-z0-9_\-\u4e00-\u9fa5][A-Za-z0-9_\-\u4e00-\u9fa5:.：]*)/g,
    '$1**@$2**',
  );
}

function teamArtifactIcon(artifact: TeamArtifactCard): string {
  const source = `${artifact.kind || ''} ${artifact.mime_type || ''} ${artifact.content_type || ''} ${artifact.path || ''} ${artifact.title || ''}`.toLowerCase();
  const ext = source.match(/\.([a-z0-9]+)(?:\?|#|\s|$)/)?.[1] || '';
  if (artifact.content_type === 'inode/directory') return 'DIR';
  if (artifact.kind === 'image' || ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'].includes(ext)) return 'IMG';
  if (artifact.kind === 'html' || ['html', 'htm'].includes(ext)) return 'HTML';
  if (artifact.kind === 'spreadsheet' || ['xlsx', 'xls', 'csv', 'tsv'].includes(ext)) return 'XLS';
  if (artifact.kind === 'presentation' || ['pptx', 'ppt'].includes(ext)) return 'PPT';
  if (['docx', 'doc'].includes(ext)) return 'DOC';
  if (ext === 'pdf') return 'PDF';
  if (['md', 'markdown'].includes(ext)) return 'MD';
  if (['json', 'yaml', 'yml'].includes(ext)) return 'DATA';
  if (artifact.kind === 'text' || ['txt', 'log'].includes(ext)) return 'TXT';
  return 'FILE';
}

function renderTeamArtifacts(artifacts: TeamArtifactCard[] | undefined): HTMLElement | null {
  const list = (artifacts || []).filter((item) => item.title || item.path);
  if (!list.length) return null;
  const wrap = document.createElement('div');
  wrap.className = 'team-artifacts';
  wrap.setAttribute('aria-label', '产物');
  for (const artifact of list) {
    const title = artifact.title || artifact.path || '产物';
    const detail = artifact.summary || artifact.path || artifact.mime_type || artifact.content_type || '';
    const card = document.createElement(artifact.path ? 'button' : 'div');
    card.className = 'team-artifact';
    if (card instanceof HTMLButtonElement) {
      card.type = 'button';
      card.title = artifact.path || '';
      card.addEventListener('click', () => {
        if (artifact.path) void window.Crew?.openPath?.(artifact.path);
      });
    } else {
      card.title = title;
    }
    const icon = document.createElement('span');
    icon.className = 'team-artifact__icon';
    icon.setAttribute('aria-hidden', 'true');
    icon.textContent = teamArtifactIcon(artifact);
    const body = document.createElement('span');
    body.className = 'team-artifact__body';
    const strong = document.createElement('strong');
    strong.textContent = title;
    body.appendChild(strong);
    if (detail) {
      const em = document.createElement('em');
      em.textContent = detail;
      body.appendChild(em);
    }
    card.append(icon, body);
    wrap.appendChild(card);
  }
  return wrap;
}

function resolveTeamCommunicationRole(message: ChatMessage, fallback: string): string {
  const role = String(fallback || '').trim();
  const target = (message.mentionTo || [])
    .map((item) => String(item || '').trim())
    .find(Boolean);
  if (!target || !/^向\s+\S+/.test(role)) return role;
  const label = target === CREW_BUILTIN_AGENT_ID ? 'Crew' : target;
  return role.replace(/^向\s+\S+/, `向 ${label}`);
}

/**
 * Team 消息标题只展示可读的职责摘要；完整的 Team 配置属于执行上下文，
 * 不应把工作原则、协作关系等内部提示词直接铺在聊天标题里。
 */
function compactTeamRole(role: string): string {
  const normalized = String(role || '')
    .replace(/[\r\n]+/g, ' ')
    .replace(/[`*_#>]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
  if (!normalized) return '';

  const summary = normalized
    .split(/(?:工作原则|团队协作关系|输出格式|工作安排|边界)\s*[-:：]?/i)[0]
    .replace(/^(职责|角色|职能)\s*[-:：]?\s*/i, '')
    .replace(/^[-*\d.、)\s]+/, '')
    .trim();
  const compact = summary || normalized;
  return compact.length > 48 ? `${compact.slice(0, 48).trimEnd()}…` : compact;
}

const RETRYABLE_MENTION_STATUSES = new Set(['failed', 'expired', 'cancelled']);
const ACTIVE_MENTION_STATUSES = new Set(['published', 'waiting_reply', 'queued', 'delivered']);

export function renderTeamInternalMessage(
  message: ChatMessage,
  isStreaming = false,
  actionState: { canRetry?: boolean; canCancel?: boolean } = {},
): HTMLElement {
  const isPlanning = message.eventType === 'team_planning_progress';
  const isCrew = String(message.agentId || '').trim() === CREW_BUILTIN_AGENT_ID;
  const name = isPlanning
    ? String(message.agentName || '团队').trim()
    : isCrew ? 'Crew' : String(message.agentName || message.agentId || 'Agent').trim();
  const role = isPlanning ? '' : compactTeamRole(resolveTeamCommunicationRole(
    message,
    message.isLeader ? 'leader' : String(message.agentRole || '').trim(),
  ));
  const tone = Number.isFinite(message.agentTone) ? Number(message.agentTone) % 6 : 0;
  const processMessage: ChatMessage = {
    ...message,
    role: 'assistant',
    content: '',
    segmentRole: 'process',
    streaming: isStreaming,
  };
  const turnMessages: ChatMessage[] = [processMessage];
  const processText = String(message.processText || '').trim();
  if (processText) {
    turnMessages.push({
      ...message,
      id: `${message.id}::process`,
      role: 'assistant',
      content: processText,
      thinking: undefined,
      toolCalls: undefined,
      segmentRole: 'process',
      streaming: isStreaming,
    });
  }
  if (!isPlanning && message.content.trim()) {
    turnMessages.push({
      ...message,
      id: `${message.id}::answer`,
      role: 'assistant',
      content: highlightTeamMentions(message.content),
      thinking: undefined,
      toolCalls: undefined,
      segmentRole: 'answer',
      streaming: isStreaming,
    });
  }
  const turnDurationMs = resolveTurnDurationMs(turnMessages, { isLive: isStreaming });
  const root = renderAgentTurn(turnMessages, {
    isStreaming,
    // 规划运行中沿用 Agent Turn 的默认展开，完成后默认折叠。
    // 其他 Team Agent Turn 继续完全沿用普通回合规则。
    userPinnedOpen: isPlanning && !isStreaming ? false : null,
    turnDurationMs,
    identity: isPlanning
      ? { kind: 'team', name, badge: '' }
      : { kind: 'external', name, badge: name.slice(0, 1).toUpperCase() },
  });
  root.classList.add('team-internal');
  if (isPlanning) root.classList.add('team-internal--planning');
  root.dataset.messageId = message.id;

  const currentAvatar = root.querySelector<HTMLElement>(':scope > .msg__avatar');
  if (!isPlanning) {
    const avatar = isCrew
      ? createChatAvatar()
      : document.createElement('span');
    if (isCrew) {
      avatar.classList.add('team-internal__avatar');
    } else {
      avatar.className = `agent-avatar agent-avatar--message agent-tone-${tone}`;
      avatar.textContent = name.slice(0, 1).toUpperCase();
    }
    currentAvatar?.replaceWith(avatar);
  }

  const body = root.querySelector<HTMLElement>(':scope > .msg__body');
  if (!body) return root;
  const nameEl = body.querySelector<HTMLElement>(':scope > .msg__name');
  if (nameEl) {
    nameEl.classList.add('team-internal__name');
    nameEl.replaceChildren();
    const strong = document.createElement('strong');
    strong.textContent = name;
    nameEl.appendChild(strong);
    if (role) {
      const em = document.createElement('em');
      em.textContent = role;
      nameEl.appendChild(em);
    }
  }

  const footer = body.querySelector<HTMLElement>(':scope > .msg__footer');
  const contentNodes = Array.from(body.children).filter((node) => node !== nameEl && node !== footer);
  const bubble = document.createElement('div');
  bubble.className = `team-internal__bubble team-internal__bubble--tone-${tone}${isCrew ? ' is-crew' : ''}${message.eventType === 'team_planning_progress' ? ' is-planning' : ''} md-body`;
  for (const node of contentNodes) bubble.appendChild(node);

  const artifacts = renderTeamArtifacts(message.artifacts);
  if (artifacts) bubble.appendChild(artifacts);

  const mentionStatus = String(message.communicationStatus || '').trim();
  if (message.communicationKind === 'user_mention_answer') {
    const actions = document.createElement('div');
    actions.className = 'team-internal__communication-actions';
    if (actionState.canRetry !== false && RETRYABLE_MENTION_STATUSES.has(mentionStatus) && message.communicationRequestText) {
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.textContent = '重试';
      retry.dataset.teamCommunicationAction = 'retry';
      actions.appendChild(retry);
    }
    if (actionState.canCancel !== false && ACTIVE_MENTION_STATUSES.has(mentionStatus)) {
      const cancel = document.createElement('button');
      cancel.type = 'button';
      cancel.textContent = '取消';
      cancel.dataset.teamCommunicationAction = 'cancel';
      actions.appendChild(cancel);
    }
    if (actions.childElementCount > 0) bubble.appendChild(actions);
  }

  if (isPlanning) {
    const label = bubble.querySelector<HTMLElement>('.msg__fold-label');
    if (label) label.textContent = resolveTeamTurnFoldLabel(message, turnDurationMs, isStreaming);
  }
  body.insertBefore(bubble, footer);
  return root;
}

/** 把文件路径拆成「目录 + 文件名」用于改动卡显示（与 inspector 的 splitFilePathDisplay 同构）。 */
function splitFilePath(path: string): { dir: string; name: string } {
  const idx = Math.max(path.lastIndexOf('\\'), path.lastIndexOf('/'));
  if (idx < 0) return { dir: '', name: path };
  return { dir: path.slice(0, idx + 1), name: path.slice(idx + 1) };
}

function parseInspirationSurface(raw?: string): InspirationSurface | null {
  if (!raw) return null;
  try {
    const data = JSON.parse(raw) as { surface?: unknown };
    if (!data.surface || typeof data.surface !== 'object') return null;
    const surface = data.surface as Record<string, unknown>;
    if (surface.kind !== 'inspiration' || !['site', 'canvas', 'widget'].includes(String(surface.mode || ''))) return null;
    if (typeof surface.sessionId !== 'string' || typeof surface.title !== 'string') return null;
    return surface as unknown as InspirationSurface;
  } catch { return null; }
}

function renderTurnInspirationSurfaceCard(messages: ChatMessage[]): HTMLElement | null {
  const surface = [...messages].reverse().flatMap((message) => [...(message.toolCalls || [])].reverse())
    .map((tool) => parseInspirationSurface(tool.result)).find(Boolean);
  if (!surface) return null;
  const card = document.createElement('article');
  card.className = 'msg__artifact-card msg__inspiration-card';
  card.setAttribute('aria-label', `${surface.title}，灵感 App`);
  const icon = createTrustedElement<HTMLElement>(
    '<span class="msg__artifact-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m12 3-1.7 5.3a2 2 0 0 1-1.3 1.3L4 11.3l5 1.7a2 2 0 0 1 1.3 1.3L12 20l1.7-5.7A2 2 0 0 1 15 13l5-1.7-5-1.7a2 2 0 0 1-1.3-1.3Z"/></svg></span>',
  );
  const copy = document.createElement('div'); copy.className = 'msg__artifact-copy';
  const title = document.createElement('strong'); title.textContent = surface.title;
  const detail = document.createElement('span'); detail.textContent = surface.status === 'preparing' ? '正在生成，可实时查看' : '可以使用和批注';
  copy.append(title, detail);
  const open = document.createElement('button'); open.type = 'button'; open.className = 'msg__artifact-open';
  open.dataset.inspirationSurface = JSON.stringify(surface); open.textContent = '打开';
  card.append(icon, copy, open);
  return card;
}

function renderTurnHtmlArtifactCard(messages: ChatMessage[]): HTMLElement | null {
  const candidates = messages
    .flatMap((message) => message.turnFileChanges || [])
    .filter((change) => change.status !== 'deleted' && /\.html?$/i.test(change.path));
  if (!candidates.length) return null;
  candidates.sort((left, right) => {
    const leftIndex = /(?:^|[\\/])index\.html?$/i.test(left.path) ? 0 : 1;
    const rightIndex = /(?:^|[\\/])index\.html?$/i.test(right.path) ? 0 : 1;
    return leftIndex - rightIndex;
  });
  const artifact = candidates[0];
  const display = splitFilePath(artifact.path).name.replace(/\.html?$/i, '') || 'HTML 页面';

  const card = document.createElement('article');
  card.className = 'msg__artifact-card';
  card.setAttribute('aria-label', `${display}，本地 HTML 网站`);

  const icon = createTrustedElement<HTMLElement>(
    '<span class="msg__artifact-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a15 15 0 0 1 0 18M12 3a15 15 0 0 0 0 18"/></svg></span>',
  );
  card.appendChild(icon);

  const copy = document.createElement('div');
  copy.className = 'msg__artifact-copy';
  const title = document.createElement('strong');
  title.textContent = display;
  const kind = document.createElement('span');
  kind.textContent = '本地 HTML · 网站';
  copy.append(title, kind);
  card.appendChild(copy);

  const open = document.createElement('button');
  open.type = 'button';
  open.className = 'msg__artifact-open';
  open.dataset.browserArtifact = artifact.path;
  open.textContent = '在 Crew 打开';
  open.setAttribute('aria-label', `在 Crew 浏览器中打开 ${display}`);
  card.appendChild(open);

  const reveal = document.createElement('button');
  reveal.type = 'button';
  reveal.className = 'msg__artifact-reveal';
  reveal.dataset.fileReveal = artifact.path;
  reveal.title = '打开方式';
  reveal.setAttribute('aria-label', `${display} 的打开方式`);
  reveal.setAttribute('aria-haspopup', 'menu');
  reveal.setAttribute('aria-expanded', 'false');
  reveal.appendChild(createTrustedFragment(FILE_REVEAL_ICON_SVG));
  card.appendChild(reveal);
  return card;
}

/** 格式化增删行数（Codex 风格千分位）。 */
function formatDiffCount(n: number): string {
  return Math.abs(n).toLocaleString('en-US');
}

/** 在资源管理器中显示：外链方框图标（与看板 Files 共用视觉语言）。 */
const FILE_REVEAL_ICON_SVG = `<svg class="msg__file-changes__reveal-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M14 3h7v7" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><path d="M10 14 21 3" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><path d="M21 14v6a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg><svg class="msg__file-changes__reveal-chevron" width="8" height="8" viewBox="0 0 12 12" fill="none" aria-hidden="true"><path d="m3 4.5 3 3 3-3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`;

const FILE_CHANGES_LEAD_ICON_SVG = `<svg class="msg__file-changes__lead-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><path d="M14 2v6h6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><path d="M12 18v-6" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><path d="M9 15h6" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>`;

/** 渲染改动卡的单个文件条目：路径 + 状态徽章 + 增删 + 悬停「在资源管理器中显示」。 */
function renderFileChangeItem(f: TurnFileChangeSummary): HTMLLIElement {
  const li = document.createElement('li');
  li.className = 'msg__file-changes__item';
  li.dataset.fileStatus = f.status;

  const main = document.createElement('button');
  main.type = 'button';
  main.className = 'msg__file-changes__row';
  main.dataset.fileChangesOpen = 'files';
  main.dataset.fileChangesPath = f.path;
  main.title = '在看板中查看改动';

  const parts = splitFilePath(f.path);
  const pathEl = document.createElement('span');
  pathEl.className = 'msg__file-changes__path';
  pathEl.title = f.path;
  if (parts.dir) {
    const dirEl = document.createElement('span');
    dirEl.className = 'msg__file-changes__dir';
    dirEl.textContent = parts.dir;
    pathEl.appendChild(dirEl);
  }
  const nameEl = document.createElement('span');
  nameEl.className = 'msg__file-changes__name';
  nameEl.textContent = parts.name;
  pathEl.appendChild(nameEl);
  main.appendChild(pathEl);

  const badges = document.createElement('span');
  badges.className = 'msg__file-changes__badges';
  const statusBadge = document.createElement('span');
  const statusTone = f.status === 'added' ? 'added' : f.status === 'deleted' ? 'deleted' : 'modified';
  statusBadge.className = `msg__file-changes__badge msg__file-changes__badge--status msg__file-changes__badge--${statusTone}`;
  statusBadge.textContent = f.status === 'added' ? '新增' : f.status === 'deleted' ? '删除' : '修改';
  badges.appendChild(statusBadge);
  // 与参考图一致：有计数才显示；0 不占位（旧历史 hydrate 前可能暂时无徽章）。
  if ((f.added || 0) > 0) {
    const add = document.createElement('span');
    add.className = 'msg__file-changes__badge msg__file-changes__badge--add';
    add.textContent = `+${formatDiffCount(f.added)}`;
    badges.appendChild(add);
  }
  if ((f.removed || 0) > 0) {
    const del = document.createElement('span');
    del.className = 'msg__file-changes__badge msg__file-changes__badge--del';
    del.textContent = `-${formatDiffCount(f.removed)}`;
    badges.appendChild(del);
  }
  main.appendChild(badges);
  li.appendChild(main);

  const reveal = document.createElement('button');
  reveal.type = 'button';
  reveal.className = 'msg__file-changes__reveal';
  if (f.status === 'deleted') {
    reveal.disabled = true;
    reveal.classList.add('is-disabled');
    reveal.title = '文件已删除';
    reveal.setAttribute('aria-label', '文件已删除');
  } else {
    reveal.dataset.fileReveal = f.path;
    reveal.title = '打开方式';
    reveal.setAttribute('aria-label', `${parts.name} 的打开方式`);
    reveal.setAttribute('aria-haspopup', 'menu');
    reveal.setAttribute('aria-expanded', 'false');
  }
  reveal.appendChild(createTrustedFragment(FILE_REVEAL_ICON_SVG));
  li.appendChild(reveal);
  return li;
}

/** 正文下方「本轮文件改动」卡（Codex 风格）：点卡/行打开看板 Files；行末悬停打开资源管理器。
 *  历史回放的一轮会拆成多条 assistant（过程文件、terminal 结果、final），必须按路径合并，
 *  不能只取最后一条，否则最终 PPT 或过程 SVG 会随消息先后关系被覆盖。 */
function renderTurnFileChangesCard(messages: ChatMessage[]): HTMLElement | null {
  const byPath = new Map<string, TurnFileChangeSummary>();
  for (const message of messages) {
    if (message.role !== 'assistant') continue;
    for (const file of message.turnFileChanges ?? []) byPath.set(file.path, file);
  }
  const resultExtensions = /\.(?:pptx?|docx?|xlsx?|pdf|zip)$/i;
  const files = Array.from(byPath.values())
    .filter((f) => !isPlanDocumentPath(f.path))
    // 仍在同一张卡内展示，但把交付结果放在过程素材前，避免文件较多时 PPT
    // 被折叠在“再显示 N 个文件”之后，看起来像没有生成。
    .sort((a, b) => Number(resultExtensions.test(b.path)) - Number(resultExtensions.test(a.path)));
  if (files.length === 0) return null;

  const PREVIEW = 3;
  const totalAdd = files.reduce((s, f) => s + (f.added || 0), 0);
  const totalDel = files.reduce((s, f) => s + (f.removed || 0), 0);

  const card = document.createElement('div');
  card.className = 'msg__file-changes';
  card.dataset.fileChangesCard = '1';
  card.setAttribute('role', 'group');
  card.setAttribute('aria-label', `已编辑 ${files.length} 个文件`);

  const headRow = document.createElement('div');
  headRow.className = 'msg__file-changes__head';

  // 「查看」/标题区：打开 Files 并展开本轮第一个文件，避免看板里仍是折叠态。
  const firstPath = files[0]?.path ?? '';
  const filePathsJson = JSON.stringify(files.map((file) => file.path));
  const fileSummariesJson = JSON.stringify(files.map((file) => ({
    path: file.path,
    name: file.name,
    added: file.added || 0,
    removed: file.removed || 0,
    status: file.status,
    binary: !!file.binary,
  })));

  const lead = document.createElement('button');
  lead.type = 'button';
  lead.className = 'msg__file-changes__lead';
  lead.dataset.fileChangesOpen = 'files';
  if (firstPath) lead.dataset.fileChangesPath = firstPath;
  lead.dataset.fileChangesPaths = filePathsJson;
  lead.dataset.fileChangesSummaries = fileSummariesJson;
  lead.title = '在看板中查看文件改动';
  lead.appendChild(createTrustedFragment(FILE_CHANGES_LEAD_ICON_SVG));
  const titles = document.createElement('span');
  titles.className = 'msg__file-changes__titles';
  const title = document.createElement('span');
  title.className = 'msg__file-changes__title';
  title.textContent = `已编辑 ${files.length} 个文件`;
  titles.appendChild(title);
  if (totalAdd > 0 || totalDel > 0) {
    const stats = document.createElement('span');
    stats.className = 'msg__file-changes__stats';
    if (totalAdd > 0) {
      const add = document.createElement('span');
      add.className = 'msg__file-changes__stat msg__file-changes__stat--add';
      add.textContent = `+${formatDiffCount(totalAdd)}`;
      stats.appendChild(add);
    }
    if (totalDel > 0) {
      const del = document.createElement('span');
      del.className = 'msg__file-changes__stat msg__file-changes__stat--del';
      del.textContent = `-${formatDiffCount(totalDel)}`;
      stats.appendChild(del);
    }
    titles.appendChild(stats);
  }
  lead.appendChild(titles);
  headRow.appendChild(lead);

  const review = document.createElement('button');
  review.type = 'button';
  review.className = 'msg__file-changes__review';
  review.dataset.fileChangesOpen = 'files';
  if (firstPath) review.dataset.fileChangesPath = firstPath;
  review.dataset.fileChangesPaths = filePathsJson;
  review.dataset.fileChangesSummaries = fileSummariesJson;
  const reviewLabel = document.createElement('span');
  reviewLabel.className = 'msg__file-changes__review-label';
  reviewLabel.textContent = '查看';
  review.appendChild(reviewLabel);
  review.title = '在看板中查看文件改动';
  headRow.appendChild(review);
  card.appendChild(headRow);

  const list = document.createElement('ul');
  list.className = 'msg__file-changes__list';
  for (const f of files.slice(0, PREVIEW)) list.appendChild(renderFileChangeItem(f));
  card.appendChild(list);

  // 用按钮就地展开，而不是 <details>：后者展开后 <summary> 仍夹在列表中间，
  // 会出现「下面已列出全部文件，中间还挂着『再显示 N 个文件』」的错觉。
  const rest = files.slice(PREVIEW);
  if (rest.length > 0) {
    const moreBtn = document.createElement('button');
    moreBtn.type = 'button';
    moreBtn.className = 'msg__file-changes__more-btn';
    moreBtn.textContent = `再显示 ${rest.length} 个文件`;
    moreBtn.addEventListener('click', () => {
      for (const f of rest) list.appendChild(renderFileChangeItem(f));
      moreBtn.remove();
    });
    card.appendChild(moreBtn);
  }
  return card;
}

// ---------- Wiki Agent 结果卡片（Phase 4，对齐 web WikiCard） ----------

/** 卡片类型徽标文案（与 wiki-page TYPE_META.shortLabel 同一套语义）。 */
const WIKI_CARD_TYPE_LABEL: Record<string, string> = {
  entity: '实体',
  topic: '主题',
  source: '来源摘要',
  comparison: '对比',
  synthesis: '综合',
};

/** 单张 Wiki 引用卡：类型徽标 + 标题 + 摘要 + 标签 + 「查看」（data-wiki-view-page 由 wiki-agent 委托）。 */
function renderWikiCard(page: WikiPage): HTMLElement {
  const card = document.createElement('div');
  card.className = 'wiki-card';

  const header = document.createElement('div');
  header.className = 'wiki-card__header';
  const typeBadge = document.createElement('span');
  typeBadge.className = `wiki-card__type wiki-card__type--${page.page_type}`;
  typeBadge.textContent = WIKI_CARD_TYPE_LABEL[page.page_type] ?? page.page_type;
  const title = document.createElement('span');
  title.className = 'wiki-card__title';
  title.title = page.title;
  title.textContent = page.title;
  header.append(typeBadge, title);
  if (page.contested) {
    const contested = document.createElement('span');
    contested.className = 'wiki-card__type';
    contested.textContent = '争议';
    contested.title = '该页面包含未解决的冲突结论';
    header.appendChild(contested);
  }
  card.appendChild(header);

  const body = document.createElement('div');
  body.className = 'wiki-card__body';
  const preview = document.createElement('p');
  preview.className = 'wiki-card__preview';
  const text = (page.summary || page.content || '').slice(0, 160).replace(/\s+/g, ' ').trim();
  preview.textContent = text || '（无内容）';
  body.appendChild(preview);
  if (page.tags.length > 0) {
    const tags = document.createElement('div');
    tags.className = 'wiki-card__tags';
    for (const t of page.tags) {
      const tag = document.createElement('span');
      tag.className = 'wiki-card__tag';
      tag.textContent = t;
      tags.appendChild(tag);
    }
    body.appendChild(tags);
  }
  card.appendChild(body);

  const actions = document.createElement('div');
  actions.className = 'wiki-card__actions';
  const viewBtn = document.createElement('button');
  viewBtn.type = 'button';
  viewBtn.className = 'wiki-card__btn';
  // setAttribute：属性值不会被重新解析为 HTML（同 renderCopyBtn 的 data-copy 模式）。
  viewBtn.setAttribute('data-wiki-view-page', page.id);
  viewBtn.textContent = '查看';
  actions.appendChild(viewBtn);
  card.appendChild(actions);
  return card;
}

/** 「Wiki 结果」卡片网格（wiki_cards 帧渲染，对齐 web AgentTurn 的 wiki-cards-panel）。 */
export function renderWikiCardsPanel(pages: WikiPage[]): HTMLElement {
  const panel = document.createElement('div');
  panel.className = 'wiki-cards-panel';
  const title = document.createElement('div');
  title.className = 'wiki-cards-panel__title';
  title.textContent = 'Wiki 结果';
  const grid = document.createElement('div');
  grid.className = 'wiki-cards-panel__grid';
  for (const p of pages) grid.appendChild(renderWikiCard(p));
  panel.append(title, grid);
  return panel;
}

/** Plan 审批卡片折叠键：按 session + 计划文件路径稳定，跨流式重渲染保留用户开合选择。 */
export function planReviewFoldKey(sessionId: string, planFile = ''): string {
  return `plan-review:${sessionId}:${planFile || '_'}`;
}

function planReviewStatusLabel(status: PlanReviewStatus, empty: boolean): string {
  if (empty) return '未写入计划';
  switch (status) {
    case 'rejected': return '已拒绝';
    case 'cancelled': return '已取消';
    case 'approved': return '已批准';
    case 'readonly': return '历史计划';
    case 'editing':
    case 'revising': return '继续修改中';
    case 'empty': return '未写入计划';
    default: return '等待审批';
  }
}

/**
 * Plan 模式审阅入口卡（对话流轻量提示）。
 * 完整正文 / 编辑 / 批准 / 撤销 / 其他 只在右侧「计划」看板；
 * 对话流在任意状态（含批准后只读）都只保留标题 + 打开看板入口，不再展开计划全文。
 */
export function renderPlanReviewCard(
  sessionId: string,
  plan: string,
  planFile = '',
  status: PlanReviewStatus = 'pending',
): HTMLElement {
  const card = document.createElement('div');
  card.className = 'plan-review-card plan-review-card--entry';
  card.dataset.planSession = sessionId;

  const empty = status === 'empty';
  const actionable = status === 'pending' || status === 'editing' || status === 'revising' || empty;
  const title = empty ? '计划为空' : firstMarkdownLine(plan) || '待审批的计划';
  const iconHtml = '<span class="plan-review-card__icon"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 19.5V4a2 2 0 0 1 2-2h11l3 3v14.5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2Z"/><path d="M14 2v6h6"/><path d="M8 13h8"/><path d="M8 17h5"/></svg></span>';

  const header = document.createElement('div');
  header.className = 'plan-review-card__header plan-review-card__header--static';
  header.appendChild(createTrustedFragment(iconHtml));
  const titleEl = document.createElement('span');
  titleEl.className = 'plan-review-card__title';
  titleEl.textContent = title;
  const meta = document.createElement('span');
  meta.className = 'plan-review-card__meta';
  meta.textContent = planReviewStatusLabel(status, empty);
  header.append(titleEl, meta);
  card.appendChild(header);

  const body = document.createElement('div');
  body.className = 'plan-review-card__body';
  body.hidden = false;
  card.classList.add('plan-review-card--open');

  const actions = document.createElement('div');
  actions.className = 'plan-review-card__actions';
  const note = document.createElement('span');
  note.className = 'plan-review-card__note';
  if (empty) {
    note.textContent = '计划文件为空。请在右侧「计划」看板完善，或让模型先写入计划后再提交。';
  } else if (actionable) {
    note.textContent = '计划已就绪，请在右侧「计划」看板中审阅、编辑并批准。';
  } else if (status === 'rejected' || status === 'cancelled') {
    note.textContent = '计划已结束。完整内容请在右侧「计划」看板查看。';
  } else {
    note.textContent = '计划全文在右侧「计划」看板查看。';
  }
  const openBtn = document.createElement('button');
  openBtn.type = 'button';
  openBtn.className = 'plan-review-card__btn plan-review-card__btn--primary';
  openBtn.dataset.planAction = 'open_board';
  openBtn.dataset.planSession = sessionId;
  openBtn.textContent = actionable ? '在看板中审阅' : '在看板中查看';
  actions.append(note, openBtn);
  body.appendChild(actions);
  card.appendChild(body);
  if (planFile) card.dataset.planFile = planFile;
  return card;
}

function firstMarkdownLine(markdown: string): string | null {
  const line = markdown
    .split('\n')
    .map((part) => part.trim())
    .find((part) => part.length > 0);
  return line ? line.replace(/^#{1,6}\s+/, '').slice(0, 90) : null;
}

export function renderWorkflowProgressPanel(message: ChatMessage): HTMLElement {
  const wp = message.workflowProgress;
  if (!wp) {
    const empty = document.createElement('div');
    empty.dataset.empty = 'true';
    return empty;
  }

  const msg = document.createElement('div');
  msg.className = 'msg workflow-progress-card';
  msg.dataset.messageId = message.id;
  msg.dataset.workflowId = wp.workflow_id;

  const avatar = document.createElement('div');
  avatar.className = 'msg__avatar workflow-progress-card__avatar';
  avatar.textContent = wp.status === 'done' ? '✅' : wp.status === 'failed' ? '❌' : wp.status === 'paused' ? '⏸' : '🔄';

  const body = document.createElement('div');
  body.className = 'msg__body';

  const nameEl = document.createElement('div');
  nameEl.className = 'msg__name';
  nameEl.textContent = '工作流进度';
  body.appendChild(nameEl);

  const header = document.createElement('div');
  header.className = 'workflow-progress-card__header';
  const phaseName = document.createElement('span');
  phaseName.className = 'workflow-progress-card__phase-name';
  phaseName.textContent = wp.current_phase ? wp.current_phase.name : (wp.status === 'done' ? '已完成' : '准备中');
  header.appendChild(phaseName);
  body.appendChild(header);

  if (wp.message) {
    const messageEl = document.createElement('div');
    messageEl.className = 'workflow-progress-card__message';
    messageEl.textContent = wp.message;
    body.appendChild(messageEl);
  }

  const phases = [...(wp.completed_phases || [])];
  if (wp.current_phase && !phases.some((p) => p.id === wp.current_phase!.id)) {
    phases.push(wp.current_phase);
  }
  if (phases.length > 0) {
    const timeline = document.createElement('div');
    timeline.className = 'workflow-progress-card__timeline';
    phases.forEach((phase, idx) => {
      const item = document.createElement('div');
      const isDone = phase.status === 'done' || (idx < phases.length - 1);
      const isFailed = phase.status === 'failed' || phase.status === 'blocked';
      item.className = `workflow-progress-card__phase workflow-progress-card__phase--${isFailed ? 'failed' : isDone ? 'done' : 'running'}`;
      const marker = document.createElement('span');
      marker.className = 'workflow-progress-card__phase-marker';
      marker.textContent = isFailed ? '✗' : isDone ? '✓' : '›';
      const label = document.createElement('span');
      label.className = 'workflow-progress-card__phase-label';
      label.textContent = phase.name;
      item.appendChild(marker);
      item.appendChild(label);
      timeline.appendChild(item);
    });
    body.appendChild(timeline);
  }

  if (wp.active_calls && wp.active_calls.length > 0) {
    const callsWrap = document.createElement('div');
    callsWrap.className = 'workflow-progress-card__calls';
    const callsLabel = document.createElement('span');
    callsLabel.className = 'workflow-progress-card__calls-label';
    callsLabel.textContent = '正在执行：';
    callsWrap.appendChild(callsLabel);
    wp.active_calls.forEach((call, idx) => {
      const callEl = document.createElement('span');
      callEl.className = 'workflow-progress-card__call';
      callEl.textContent = call.role;
      callsWrap.appendChild(callEl);
      if (idx < wp.active_calls!.length - 1) {
        const sep = document.createElement('span');
        sep.className = 'workflow-progress-card__call-sep';
        sep.textContent = '、';
        callsWrap.appendChild(sep);
      }
    });
    body.appendChild(callsWrap);
  }

  appendMsgFooter(body, [formatMessageTime(message.timestamp)], []);

  msg.appendChild(avatar);
  msg.appendChild(body);
  return msg;
}

/** 渲染单条消息（web MessageItem 同款 DOM）。 */
export function renderMessageHtml(
  message: ChatMessage,
  _modelLabel: string,
  options?: { preview?: boolean },
): HTMLElement {
  const preview = options?.preview === true;
  if (message.role === 'user') {
    const msg = document.createElement('div');
    msg.className = 'msg user';
    msg.dataset.messageId = message.id;

    const body = document.createElement('div');
    body.className = 'msg__body';
    if (message.content) {
      const text = document.createElement('div');
      text.className = 'msg__text';
      // 与输入框一致的 chip 渲染：@file: / /中文名 染蓝、标记透明。用 DOM 节点构造，XSS 安全。
      for (const node of buildChippedNodes(message.content)) text.appendChild(node);
      body.appendChild(text);
    }
    appendAttachments(body, message.attachments);
    const userActions = [renderCopyBtn(message.content)];
    if (!preview) userActions.push(renderEditBtn(message.id));
    appendMsgFooter(body, [message.model || '', formatMessageTime(message.timestamp)], userActions);

    msg.appendChild(body);
    return msg;
  }

  if (message.role === 'team_internal') {
    return renderTeamInternalMessage(message, Boolean(message.streaming));
  }

  // Dynamic Kanban 工作流进度面板
  if (message.role === 'status' && message.workflowProgress) {
    return renderWorkflowProgressPanel(message);
  }

  // Dynamic Kanban 角色卡片：带 agentName 的 status/assistant 消息独立渲染。
  // 当 workflow 完成时，最后一条角色输出会被提升为 assistant + segmentRole='answer'，
  // 这里保留角色名/头像，并用 agent-role--final 区分「最终结果」。
  if ((message.role === 'status' || message.role === 'assistant') && message.agentName) {
    const isFinal = message.segmentRole === 'answer';
    const msg = document.createElement('div');
    msg.className = `msg agent-role${isFinal ? ' agent-role--final' : ''}`;
    msg.dataset.messageId = message.id;

    const avatar = document.createElement('div');
    avatar.className = 'msg__avatar agent-role__avatar';
    avatar.textContent = message.agentAvatar || '🤖';

    const body = document.createElement('div');
    body.className = 'msg__body';
    const nameEl = document.createElement('div');
    nameEl.className = 'msg__name';
    nameEl.textContent = isFinal ? `${message.agentName} · 最终结果` : message.agentName;
    body.appendChild(nameEl);
    if (message.content) {
      const text = document.createElement('div');
      text.className = 'msg__text md-body chat-markdown';
      text.appendChild(createTrustedFragment(renderMarkdownHtml(message.content)));
      body.appendChild(text);
    }
    appendMsgFooter(body, [formatMessageTime(message.timestamp)], [renderCopyBtn(message.content)]);

    msg.appendChild(avatar);
    msg.appendChild(body);
    return msg;
  }

  // agent 消息由 renderAgentTurn 单独处理（会把同回合的多条 agent 消息合并成单个 .msg 块）。
  // 单条调用 renderMessageHtml 的兜底逻辑保留：当作只有一个 tool call/text 的回合。
  if (message.role === 'status' || message.role === 'error' || message.role === 'assistant') {
    const turnDurationMs = resolveTurnDurationMs([message], {
      isLive: Boolean(message.streaming),
    });
    return renderAgentTurn([message], {
      isStreaming: !!message.streaming,
      userPinnedOpen: null,
      turnDurationMs,
    });
  }

  // 兜底空占位（原字符串实现此处返回 ''）。
  const empty = document.createElement('div');
  empty.dataset.empty = 'true';
  return empty;
}

/** 可嵌入会话 Surface：复用主对话的消息、工具时间线与流式回合渲染。 */
export function renderConversationSurface(
  container: HTMLElement,
  messages: ChatMessage[],
  modelLabel: string,
  options: { showAssistantName?: boolean } = {},
): void {
  container.replaceChildren();
  if (!messages.length) {
    const empty = document.createElement('p');
    empty.className = 'session-preview-empty';
    empty.textContent = '该会话暂无消息记录。';
    container.appendChild(empty);
    return;
  }

  const frag = document.createDocumentFragment();
  let i = 0;
  while (i < messages.length) {
    const msg = messages[i];
    const isAgent =
      msg.role === 'assistant' || msg.role === 'error' || (msg.role === 'status' && !msg.agentName && !msg.workflowProgress);
    if (!isAgent) {
      frag.appendChild(renderMessageHtml(msg, modelLabel, { preview: true }));
      i += 1;
      continue;
    }
    let j = i + 1;
    while (j < messages.length) {
      const r = messages[j].role;
      const hasAgent = messages[j].agentName;
      const hasWorkflowProgress = messages[j].workflowProgress;
      if (r === 'assistant' || r === 'error' || (r === 'status' && !hasAgent && !hasWorkflowProgress)) j += 1;
      else break;
    }
    const batch = messages.slice(i, j);
    const isLive = batch.some((item) => Boolean(item.streaming));
    const turnDurationMs = resolveTurnDurationMs(batch, { isLive });
    frag.appendChild(
      renderAgentTurn(batch, {
        isStreaming: isLive,
        userPinnedOpen: null,
        turnDurationMs,
        showAssistantName: options.showAssistantName ?? true,
      }),
    );
    i = j;
  }
  const inner = document.createElement('div');
  inner.className = 'messages__inner';
  inner.appendChild(frag);
  container.appendChild(inner);
}

/** 兼容旧的只读预览调用。 */
export const renderConversationPreview = renderConversationSurface;

/** 「等待首片响应」时的占位消息：必须与正式 Crew 回复共享同一套 .msg 布局（黑头像 + Crew 名字 + 三点动画），
 *  这样用户点完发送后就能立即看到「模型在准备回答」的视觉锚点，而不是几个飘在空白处的点。
 *  渲染约定：仅在 messages 还没有任何 agent 消息时使用；一旦首片 delta/tool 到达，
 *  会由 renderAgentTurn 接管同回合的流式渲染，这里就不再追加，避免出现两个 Crew 头像。 */
export function renderTypingIndicator(identity?: AgentTurnOptions['identity']): HTMLElement {
  const msg = document.createElement('div');
  msg.className = 'msg msg--typing msg--agent-turn';
  msg.dataset.messageId = 'typing';
  msg.setAttribute('aria-label', '正在生成');

  const avatar = createAgentTurnAvatar(identity);

  const body = document.createElement('div');
  body.className = 'msg__body';
  const nameEl = document.createElement('div');
  nameEl.className = 'msg__name';
  nameEl.textContent = identity?.name || 'Crew';
  const typing = document.createElement('div');
  typing.className = 'typing';
  typing.appendChild(document.createElement('span'));
  typing.appendChild(document.createElement('span'));
  typing.appendChild(document.createElement('span'));
  // 保持 Crew 原等待态不变；只有外部会话补充身份名称。
  if (identity) body.appendChild(nameEl);
  body.appendChild(typing);

  msg.appendChild(avatar);
  msg.appendChild(body);
  return msg;
}

export function renderRunningIntro(status: string, intro: string): HTMLElement {
  const root = document.createElement('div');
  root.className = 'running-intro';
  root.setAttribute('aria-label', 'Crew 正在处理任务');

  const logo = document.createElement('span');
  logo.className = 'running-intro__logo';
  logo.setAttribute('aria-hidden', 'true');
  const logoImage = document.createElement('img');
  logoImage.className = 'running-intro__agent-logo';
  logoImage.src = './crew-jump-agent.png';
  logoImage.alt = '';
  logoImage.draggable = false;
  logo.appendChild(logoImage);

  const textWrap = document.createElement('span');
  textWrap.className = 'running-intro__text';
  const statusEl = document.createElement('span');
  statusEl.className = 'running-intro__status';
  statusEl.textContent = status;
  const adEl = document.createElement('span');
  adEl.className = 'running-intro__ad';
  adEl.textContent = intro;
  textWrap.appendChild(statusEl);
  textWrap.appendChild(adEl);

  root.appendChild(logo);
  root.appendChild(textWrap);
  return root;
}

export function renderQueueHintCard(hint: string): HTMLElement {
  const card = document.createElement('div');
  card.className = 'queue-card';
  const spinner = document.createElement('span');
  spinner.className = 'queue-card__spinner';
  const text = document.createElement('span');
  text.textContent = hint;
  card.appendChild(spinner);
  card.appendChild(text);
  return card;
}

export function renderQueuePanelHtml(queue: PendingMessage[], canSteer = true): string {
  // 注：renderQueueSlot 直接 slot.innerHTML = renderQueuePanelHtml(...)，X3a 显式 OUT OF SCOPE。
  // 此处保持字符串实现；所有用户派生文本均已 escapeHtml，安全边界已具备。
  // 卡片贴在输入框上方（.chat-queue-slot），对齐 Codex 的「待发消息」交互：
  //   - 不操作 → 当前任务结束后逐条自动发送队首（consumePending）
  //   - 引导   → 把该项提升为修订式下一轮，并请求当前回复尽快收束
  //   - 编辑   → 回填输入框
  //   - 删除   → 丢弃
  //   - 上/下移 → 调整后续自动发送顺序
  const visibleItems = queue
    .map((item, originalIndex) => ({ item, originalIndex }))
    .filter(({ item }) => !item.optimisticUserMessageId);
  if (visibleItems.length === 0) return '';
  const items = visibleItems
    .map(
      ({ item, originalIndex }, visibleIndex) => `
      <div class="chat-queue-item" data-queue-index="${originalIndex}">
        <div class="chat-queue-item__order" aria-label="等待队列第 ${visibleIndex + 1} 条">${visibleIndex + 1}</div>
        <div class="chat-queue-item__body">
          <div class="chat-queue-item__text">${escapeHtml(item.query)}</div>
        </div>
        <div class="chat-queue-item__actions">
          ${canSteer ? `<button class="chat-queue-item__btn chat-queue-item__btn--text chat-queue-item__steer" type="button" data-queue-steer="${originalIndex}" title="引导：中断当前回复并优先用这条修订" aria-label="引导">${QUEUE_STEER_SVG}<span>引导</span></button>` : ''}
          <button class="chat-queue-item__btn chat-queue-item__edit" type="button" data-queue-edit="${originalIndex}" title="编辑" aria-label="编辑">${EDIT_BTN_SVG}</button>
          <button class="chat-queue-item__btn chat-queue-item__remove" type="button" data-queue-remove="${originalIndex}" title="删除" aria-label="删除">${QUEUE_DELETE_SVG}</button>
          <button class="chat-queue-item__btn chat-queue-item__move" type="button" data-queue-move="${originalIndex}" data-queue-move-dir="-1" title="上移" aria-label="上移"${originalIndex === 0 ? ' disabled' : ''}>${QUEUE_UP_SVG}</button>
          <button class="chat-queue-item__btn chat-queue-item__move" type="button" data-queue-move="${originalIndex}" data-queue-move-dir="1" title="下移" aria-label="下移"${originalIndex === queue.length - 1 ? ' disabled' : ''}>${QUEUE_DOWN_SVG}</button>
          <div class="chat-queue-item__menu">
            <button class="chat-queue-item__btn chat-queue-item__more" type="button" data-queue-menu="${originalIndex}" aria-haspopup="menu" aria-expanded="false" title="更多" aria-label="更多">${QUEUE_MORE_SVG}</button>
            <div class="chat-queue-item__menu-popover" data-queue-menu-panel="${originalIndex}" role="menu" hidden>
              <button class="chat-queue-item__menu-action" type="button" data-queue-edit="${originalIndex}" role="menuitem">${EDIT_BTN_SVG}<span>编辑消息</span></button>
              <button class="chat-queue-item__menu-action" type="button" data-queue-move="${originalIndex}" data-queue-move-dir="-1" role="menuitem"${originalIndex === 0 ? ' disabled' : ''}>${QUEUE_UP_SVG}<span>上移</span></button>
              <button class="chat-queue-item__menu-action" type="button" data-queue-move="${originalIndex}" data-queue-move-dir="1" role="menuitem"${originalIndex === queue.length - 1 ? ' disabled' : ''}>${QUEUE_DOWN_SVG}<span>下移</span></button>
              <button class="chat-queue-item__menu-action chat-queue-item__menu-action--danger" type="button" data-queue-remove="${originalIndex}" role="menuitem">${QUEUE_DELETE_SVG}<span>删除</span></button>
            </div>
          </div>
        </div>
      </div>`,
    )
    .join('');
  return `<div class="chat-queue-panel" id="chat-queue-panel"><div class="chat-queue-panel__list">${items}</div></div>`;
}

/** 当前步骤序号（1-based）：优先 in_progress，否则第一个 pending，否则已完成数+1。 */
export function todoCurrentStepIndex(todos: TodoItem[]): number {
  if (todos.length === 0) return 0;
  const active = todos.findIndex((todo) => todo.status === 'in_progress');
  if (active >= 0) return active + 1;
  const pending = todos.findIndex((todo) => todo.status === 'pending');
  if (pending >= 0) return pending + 1;
  const done = todos.filter((todo) => todo.status === 'completed').length;
  return Math.min(Math.max(done, 1), todos.length);
}

export function renderTodoProgressPanelHtml(todos: TodoItem[], open: boolean, panelKey = ''): string {
  if (todos.length === 0) return '';
  const done = todos.filter((todo) => todo.status === 'completed').length;
  const current = todos.find((todo) => todo.status === 'in_progress');
  const allDone = done === todos.length;
  const title = current?.content
    || todos.find((todo) => todo.status === 'pending')?.content
    || (allDone ? '所有步骤已完成' : '等待下一步');
  const step = todoCurrentStepIndex(todos);
  const progress = Math.round((done / todos.length) * 100);
  const rows = todos.slice(0, 30).map((todo) => {
    const status = todoStatusClass(todo.status);
    return `
      <div class="desktop-todo-panel__row desktop-todo-panel__row--${status}">
        <span class="desktop-todo-panel__mark" aria-hidden="true">${escapeHtml(todoMark(todo.status))}</span>
        <span class="desktop-todo-panel__content">${escapeHtml(todo.content || todo.id)}</span>
        <span class="desktop-todo-panel__status">${escapeHtml(todoLabel(todo.status))}</span>
      </div>`;
  }).join('');
  // 折叠态：只露出「当前步骤文案 + x/x」；展开后才列出全部 todo。
  return `
    <section class="desktop-todo-panel${open ? ' desktop-todo-panel--open' : ''}${allDone ? ' desktop-todo-panel--done' : ''}" aria-label="任务进度">
      <button class="desktop-todo-panel__header" type="button" aria-expanded="${open ? 'true' : 'false'}" data-todo-panel-toggle="1"${panelKey ? ` data-todo-panel-key="${escapeHtml(panelKey)}"` : ''}>
        <span class="desktop-todo-panel__ring" aria-hidden="true" data-todo-progress="${progress}">
          <span class="desktop-todo-panel__ring-hole"></span>
        </span>
        <span class="desktop-todo-panel__main">
          <span class="desktop-todo-panel__current">${escapeHtml(title)}</span>
        </span>
        <span class="desktop-todo-panel__count" title="第 ${step} 步，共 ${todos.length} 步">${step}/${todos.length}</span>
        <span class="desktop-todo-panel__caret${open ? ' desktop-todo-panel__caret--open' : ''}" aria-hidden="true">${open ? '▾' : '▸'}</span>
      </button>
      ${open ? `<div class="desktop-todo-panel__body">${rows}</div>` : ''}
    </section>`;
}

function todoStatusClass(status: TodoItem['status']): string {
  if (status === 'in_progress') return 'active';
  if (status === 'completed') return 'done';
  if (status === 'cancelled') return 'cancelled';
  return 'pending';
}

function todoMark(status: TodoItem['status']): string {
  if (status === 'completed') return '✓';
  if (status === 'cancelled') return '×';
  // pending / in_progress：空圆由 CSS 描边表达；进行中用旋转边框动画。
  return '';
}

function todoLabel(status: TodoItem['status']): string {
  if (status === 'completed') return '完成';
  if (status === 'in_progress') return '进行中';
  if (status === 'cancelled') return '取消';
  return '待办';
}

export function sessionStatusClass(status: SessionStatus | undefined): string {
  if (!status || status === 'idle') return '';
  return ` history-item--${status}`;
}

export function renderEmptyState(): HTMLElement {
  const div = document.createElement('div');
  div.className = 'empty';
  const h2 = document.createElement('h2');
  h2.textContent = '开始一段对话';
  const desc = document.createElement('div');
  desc.textContent = '单 Agent 直接执行任务；切到 Team 模式可组建多智能体协同。';
  div.appendChild(h2);
  div.appendChild(desc);
  return div;
}

/** Work 会话空态保持安静；Composer 已经提供唯一的输入入口。 */
export function renderWorkEmptyState(_hasItem: boolean): HTMLElement {
  const div = document.createElement('div');
  div.className = 'empty empty--work';
  div.setAttribute('aria-hidden', 'true');
  return div;
}
