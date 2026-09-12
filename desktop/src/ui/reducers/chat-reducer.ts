/**
 * Chat 流式分片 reducer。
 *
 * 把 applyChunk 中的 7 个 kind 分支拆成独立的纯函数 reducer。
 * 每个 reducer 接受当前快照（messages + books + status hint），返回一组"待 apply 的 patch"。
 * applyChunk 负责 apply patch + 触发渲染 / 副作用（renderChat / recordUsageTurn / finalizeTurn）。
 *
 * 优势：
 * - reducer 纯函数，可在 tests 里直接 mock 数据验证分支覆盖
 * - applyChunk 函数从 ~130 行降到 ~40 行（dispatch + apply）
 * - 未知 kind / 异常 body 由 `unknown` reducer 兜底，不抛错
 */

import type { ChatMessage, PlanReviewStatus, SessionStatus, ToolCallInfo, TurnFileChangeSummary } from '../chat-render';
import type { Bookkeeping, FileChange, TodoItem } from '../state';
import { isPlanDocumentPath } from '../plan-document-path';
import {
  featureEventRegistry,
  emptyFeatureReducerResult,
  uniqueMessageId,
  type FeatureReducerResult,
  type FeatureReducerContext,
  type MessageUpsert,
  type StatusHint,
  type ToolUpsert,
} from '../features/event-reducer-registry';

// Re-export shared reducer types so existing importers keep working.
export type {
  MessageUpsert,
  ToolUpsert,
  StatusHint,
} from '../features/event-reducer-registry';

// ---------- 输入 chunk 类型（窄化版，避免 Record<string, any>） ----------

export type ChatChunkKind =
  | 'delta'
  | 'tool'
  | 'task'
  | 'status'
  | 'final'
  | 'error'
  | 'thinking'
  | 'plan_review'
  | 'followup_question'
  | 'todo_updated'
  | 'file_changes'
  | 'feature_event';

export interface DeltaChunk {
  kind: 'delta';
  body: { text?: string; delta_start?: number | string; delta_end?: number | string };
  sequence: number;
  session_id?: string;
}
export interface ThinkingChunk {
  kind: 'thinking';
  body: { text?: string };
  sequence: number;
  session_id?: string;
}
export interface ToolChunk {
  kind: 'tool';
  body: {
    tool_call_id?: string;
    phase?: 'generating' | 'start' | 'result' | 'end' | 'error' | 'progress';
    name?: string;
    ui_label?: string;
    args?: string;
    detail?: string;
    /** phase=progress 时的阶段进度文案。 */
    text?: string;
  };
  sequence: number;
  session_id?: string;
}
export interface StatusChunk {
  kind: 'status';
  body: {
    message?: string;
    agent_name?: string;
    agent_avatar?: string;
    detail?: string;
    control?: boolean;
    activity?: string;
  };
  sequence: number;
  session_id?: string;
}

export function isPlanControlStatus(message: string): boolean {
  return (
    message.startsWith('已进入 Plan 模式') ||
    message.startsWith('已保留 Plan 模式') ||
    message.startsWith('已退出 Plan 模式')
  );
}
/** LLM 返回的 token 用量（final chunk body.usage）。cache 字段视 provider 而定。 */
export interface PromptBreakdown {
  system?: number;
  reminder?: number;
  tools?: number;
}
export interface UsagePayload {
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  cache_creation_input_tokens?: number;  // Anthropic
  cache_read_input_tokens?: number;       // Anthropic
  cached_tokens?: number;                  // OpenAI / DeepSeek
  prompt_breakdown?: PromptBreakdown;      // 系统提示/技能·上下文/工具定义 的 token 估算
}
export interface FinalChunk {
  kind: 'final';
  body: { text?: string; replace_content?: boolean; usage?: UsagePayload };
  sequence: number;
  session_id?: string;
}
export interface ErrorChunk {
  kind: 'error';
  body: { message?: string };
  sequence: number;
  session_id?: string;
}
export interface PlanReviewChunk {
  kind: 'plan_review';
  body: {
    plan?: string;
    plan_file?: string;
    empty?: boolean;
    status?: string;
    phase?: string;
  };
  sequence: number;
  session_id?: string;
}
export interface FollowupQuestionChunk {
  kind: 'followup_question';
  body: {
    question_id?: string;
    title?: string;
    record_history?: boolean;
    status?: string;
    note?: string;
    origin?: {
      type?: string;
      agent_name?: string;
      origin_session_id?: string;
      mention_intent?: string;
    };
    questions?: Array<{
      id?: string;
      question?: string;
      options?: unknown[];
      allowFreeText?: boolean;
      allow_free_text?: boolean;
      multiSelect?: boolean;
    }>;
  };
  sequence: number;
  session_id?: string;
}
export interface TodoUpdatedChunk {
  kind: 'todo_updated';
  body: { todos?: Array<{ id?: string; content?: string; status?: string }> };
  sequence: number;
  session_id?: string;
}

/** 后台任务进度帧：crew/core/envelope.py ResponseChunk.task() 产生。
 *  桌面端当前通过 REST /api/tasks 驱动 kanban，WS task 帧仅作进度提示，
 *  故 reducer 走空实现（不丢帧、不报错，保留未来切换到 WS 驱动的入口）。 */
export interface TaskChunk {
  kind: 'task';
  body: {
    task_id?: string;
    task_kind?: string;
    phase?: string;
    status?: string;
    progress?: Record<string, unknown>;
    output_ref?: string;
    summary?: string;
  };
  sequence: number;
  session_id?: string;
}

export interface FileChangesChunk {
  kind: 'file_changes';
  body: { files?: FileChange[] };
  sequence: number;
  session_id?: string;
}

/** 后端 feature_event 统一命名空间帧：业务事件以 (feature, event, version) 寻址，payload 携带业务数据。 */
export interface FeatureEventChunk {
  kind: 'feature_event';
  body: {
    feature?: string;
    event?: string;
    version?: number;
    payload?: unknown;
  };
  sequence: number;
  session_id?: string;
}

export type AnyChatChunk =
  | DeltaChunk
  | ThinkingChunk
  | ToolChunk
  | StatusChunk
  | FinalChunk
  | ErrorChunk
  | PlanReviewChunk
  | FollowupQuestionChunk
  | TodoUpdatedChunk
  | FileChangesChunk
  | TaskChunk
  | FeatureEventChunk;

// ---------- reducer 输入快照 ----------

export interface ReducerSnapshot {
  sessionId: string;
  messages: ChatMessage[];
  book: Bookkeeping;
  /** 当前 sessionStatus hint（reducer 仅消费，不写）。 */
  currentStatus: SessionStatus;
  /** 当前 Date.now() 的注入点，便于 tests 固定时间。 */
  now: number;
  /** 当前 chunk 的 sequence 序号，用于生成稳定 message id。 */
  sequence: number;
}

/**
 * 生成会话内唯一的消息 id。
 * 基础形式 `${prefix}-${now}-${sequence}` 在同毫秒连发帧（sequence 恒为 0 的
 * status/tool 等旁路帧）下会撞 id；撞 id 后 patch/render 按 id 匹配会命中
 * 第一条同名消息，表现为内容错位/乱序。已存在同名 id 时追加递增后缀避让。
 */
// ---------- reducer 输出 ----------

/** Agent 挂起等待用户点选/审批：后端 turn 可能仍 live，但 UI 不应显示「生成中」。 */
export const USER_WAIT_CHUNK_KINDS = new Set<ChatChunkKind>([
  'followup_question',
  'plan_review',
]);

/** 这些帧表示模型仍在推进本轮生成；封口后若 request_id 匹配旧回合，应整帧拒收。 */
export const TURN_GENERATION_CHUNK_KINDS = new Set<ChatChunkKind>([
  'delta',
  'thinking',
  'tool',
  'status',
  'final',
  'error',
]);

export const TURN_SCOPED_CHUNK_KINDS = new Set<ChatChunkKind>([
  ...TURN_GENERATION_CHUNK_KINDS,
  'plan_review',
  'followup_question',
  'todo_updated',
  'file_changes',
  'task',
]);

/**
 * 根据 reducer 的 statusHint（及等待用户交互的 kind）决定 busy 应置 true/false。
 * 返回 null 表示本帧不改变 busy（例如 todo_updated / file_changes 等附属帧）。
 *
 * 禁止用「是否收到 chunk」推断 busy——否则 final 后迟到的 todo_updated 会把 UI 打回「对话中」。
 */
export function resolveBusyTransition(
  kind: ChatChunkKind,
  statusHint: StatusHint | undefined,
  turnSealed = false,
  presentationOnly = false,
): boolean | null {
  if (presentationOnly) return null;
  if (USER_WAIT_CHUNK_KINDS.has(kind)) return false;
  if (statusHint === 'running' || statusHint === 'queued') {
    if (turnSealed) return null;
    return true;
  }
  if (statusHint === 'idle' || statusHint === 'error') return false;
  return null;
}

export interface TurnGateState {
  turnSealed: boolean;
  activeRequestId: string | null;
  acceptingNewRequest: boolean;
}

export type TurnGateDecision =
  | { action: 'accept' }
  | { action: 'accept'; bindRequestId: string }
  | { action: 'drop' };

/** 帧的回合归属语义：generation=生成期帧，turn-scoped=回合附属帧，bypass=不参与回合 gate。 */
type TurnSemantics = 'generation' | 'turn-scoped' | 'bypass';

/**
 * feature_event 帧的回合归属分类（判定源是 (feature, event)，不把裸 feature_event 塞进 kind 集合）：
 * - team.internal_message → generation（生成期帧）；
 * - wiki.cards 与 kanban 全部事件 → turn-scoped（回合附属帧）；
 * - 其余（wiki.changed / wiki.ingest_progress 等 owner 级带外事件、未知事件）→ bypass。
 */
function featureEventTurnSemantics(feature: string, event: string): TurnSemantics {
  if (feature === 'team' && event === 'internal_message') return 'generation';
  if (feature === 'wiki' && event === 'cards') return 'turn-scoped';
  if (feature === 'kanban') return 'turn-scoped';
  return 'bypass';
}

/** chunk → 回合归属语义：feature_event 按 (feature, event) 分类，普通 kind 查 kind 集合。 */
function turnSemanticsOf(chunk: AnyChatChunk): TurnSemantics {
  if (chunk.kind === 'feature_event') {
    const featureEvent = featureEventFrameOf(chunk);
    return featureEvent
      ? featureEventTurnSemantics(featureEvent.feature, featureEvent.event)
      : 'bypass';
  }
  if (TURN_GENERATION_CHUNK_KINDS.has(chunk.kind)) return 'generation';
  if (TURN_SCOPED_CHUNK_KINDS.has(chunk.kind)) return 'turn-scoped';
  return 'bypass';
}

/**
 * 决定 chunk 是否属于当前可接收回合。该 gate 必须在 reducer patch 写入 store 前执行。
 *
 * 设计意图：
 * - request_id 不匹配当前回合的帧，直接丢弃，避免旧回合附属帧污染当前 book。
 * - request_id 匹配且回合已封口的生成帧，是同一回合迟到帧，直接丢弃。
 * - 新发送/恢复/重连打开 acceptingNewRequest 后，首个带 request_id 的生成帧绑定当前回合。
 * - 无 request_id 的控制帧保持兼容；无法证明归属时只按 sealed 处理生成帧。
 * - 帧的回合归属语义由 turnSemanticsOf 统一给出：普通 kind 查 kind 集合，
 *   feature_event 按 (feature, event) 分类。
 */
export function resolveTurnGate(
  chunk: AnyChatChunk,
  requestId: string | undefined,
  gate: TurnGateState,
): TurnGateDecision {
  const semantics = turnSemanticsOf(chunk);
  if (semantics === 'bypass') return { action: 'accept' };
  if (!requestId) {
    if (semantics !== 'generation') return { action: 'accept' };
    if (chunk.kind === 'status') return { action: 'accept' };
    return gate.turnSealed ? { action: 'drop' } : { action: 'accept' };
  }
  if (gate.activeRequestId === requestId) {
    return gate.turnSealed && semantics === 'generation'
      ? { action: 'drop' }
      : { action: 'accept' };
  }
  if (gate.activeRequestId) return { action: 'drop' };
  if (gate.acceptingNewRequest) {
    return { action: 'accept', bindRequestId: requestId };
  }
  return gate.turnSealed ? { action: 'drop' } : { action: 'accept' };
}

export function chunkRequestId(chunk: AnyChatChunk, fallback?: string): string | undefined {
  if (fallback) return fallback;
  const body = chunk.body as { request_id?: unknown };
  return typeof body.request_id === 'string' && body.request_id.trim() ? body.request_id : undefined;
}

export type ReducerResult = FeatureReducerResult;

// ---------- 7 个 reducer ----------

// ---------- feature_event 帧解析（业务事件的唯一协议形态） ----------

/** feature_event 帧的规范化身份：(feature, event, version) 寻址 + 业务 payload。 */
export interface FeatureEventFrame {
  feature: string;
  event: string;
  version: number;
  payload: unknown;
}

/**
 * 从 chunk 解析 feature_event 身份；非 feature_event 帧返回 null。
 * 字段容错：feature/event 缺失或非字符串记为空串，version 缺失回落 1（与 registry
 * 的 v1 注册约定一致）；未登记的 (feature, event, version) 由 registry 在 dispatch 时
 * console.warn 可诊断忽略。
 */
export function featureEventFrameOf(
  chunk: { kind: string; body: Record<string, unknown> },
): FeatureEventFrame | null {
  if (chunk.kind !== 'feature_event') return null;
  const body = chunk.body;
  return {
    feature: typeof body['feature'] === 'string' ? body['feature'] : '',
    event: typeof body['event'] === 'string' ? body['event'] : '',
    version: typeof body['version'] === 'number' ? body['version'] : 1,
    payload: body['payload'],
  };
}

/**
 * 解析 chunk → 统一 AnyChatChunk 形态。
 * 输入是 unknown（来自 IPC），reducer 层做容错。
 */
export function normalizeChunk(raw: unknown): AnyChatChunk | null {
  if (typeof raw !== 'object' || raw === null) return null;
  const r = raw as Record<string, unknown>;
  const kind = r['kind'];
  if (typeof kind !== 'string') return null;
  const sequence = typeof r['sequence'] === 'number' ? r['sequence'] : 0;
  const session_id = typeof r['session_id'] === 'string' ? r['session_id'] : undefined;
  const body = (r['body'] ?? {}) as Record<string, unknown>;
  // session_id 用 spread 让可选字段在 exactOptionalPropertyTypes 下完全省略而非显式 undefined
  const base = session_id ? { session_id } : {};
  switch (kind) {
    case 'delta': return { kind: 'delta', body: body as DeltaChunk['body'], sequence, ...base };
    case 'thinking': return { kind: 'thinking', body: body as ThinkingChunk['body'], sequence, ...base };
    case 'tool': return { kind: 'tool', body: body as ToolChunk['body'], sequence, ...base };
    case 'status': return { kind: 'status', body: body as StatusChunk['body'], sequence, ...base };
    case 'final': return { kind: 'final', body: body as FinalChunk['body'], sequence, ...base };
    case 'error': return { kind: 'error', body: body as ErrorChunk['body'], sequence, ...base };
    case 'plan_review': return { kind: 'plan_review', body: body as PlanReviewChunk['body'], sequence, ...base };
    case 'followup_question': return { kind: 'followup_question', body: body as FollowupQuestionChunk['body'], sequence, ...base };
    case 'todo_updated': return { kind: 'todo_updated', body: body as TodoUpdatedChunk['body'], sequence, ...base };
    case 'file_changes': return { kind: 'file_changes', body: body as FileChangesChunk['body'], sequence, ...base };
    case 'task': return { kind: 'task', body: body as TaskChunk['body'], sequence, ...base };
    case 'feature_event': return { kind: 'feature_event', body: body as FeatureEventChunk['body'], sequence, ...base };
    default: return null;
  }
}

function textOf(body: Record<string, unknown>): string {
  const v = body['text'];
  return typeof v === 'string' ? v : '';
}

function overlapConcat(left: string, right: string): string {
  const max = Math.min(left.length, right.length);
  for (let size = max; size > 0; size -= 1) {
    if (left.slice(-size) === right.slice(0, size)) {
      return `${left}${right.slice(size)}`;
    }
  }
  return `${left}${right}`;
}

function mergeStreamingText(existing: string | undefined, incoming: string | undefined): string {
  const left = String(existing || '');
  const right = String(incoming || '');
  if (!right) return left;
  if (!left) return right;
  if (left === right || left.endsWith(right) || left.includes(right)) return left;
  if (right.startsWith(left) || right.includes(left)) return right;
  return overlapConcat(left, right);
}

function deltaRangeOf(chunk: DeltaChunk): { start: number; end: number } | null {
  const startRaw = chunk.body.delta_start ?? chunk.sequence;
  const endRaw = chunk.body.delta_end ?? chunk.sequence;
  const start = typeof startRaw === 'number' ? startRaw : Number(startRaw);
  const end = typeof endRaw === 'number' ? endRaw : Number(endRaw);
  if (!Number.isFinite(start) || !Number.isFinite(end) || start <= 0 || end <= 0) return null;
  return { start: Math.min(start, end), end: Math.max(start, end) };
}

function renderDeltaText(book: Pick<Bookkeeping, 'deltaSpans' | 'legacyDeltaText'>): string {
  return book.legacyDeltaText + book.deltaSpans
    .slice()
    .sort((a, b) => a.start - b.start || a.end - b.end)
    .map((span) => span.text)
    .join('');
}

export function applyOrderedDelta(
  book: Pick<Bookkeeping, 'deltaSpans' | 'legacyDeltaText'>,
  chunk: DeltaChunk,
): string {
  const text = textOf(chunk.body);
  const range = deltaRangeOf(chunk);
  if (!range) {
    book.legacyDeltaText += text;
    return renderDeltaText(book);
  }
  if (book.deltaSpans.some((span) => span.start <= range.start && span.end >= range.end)) {
    return renderDeltaText(book);
  }
  book.deltaSpans = book.deltaSpans.filter(
    (span) => !(span.start >= range.start && span.end <= range.end),
  );
  book.deltaSpans.push({ ...range, text });
  return renderDeltaText(book);
}

export function resolveFinalContent(accumulated: string, finalText: string): string {
  if (!finalText) return accumulated;
  const accTrim = accumulated.trimEnd();
  const textTrim = finalText.trimEnd();
  if (accTrim.includes(textTrim)) return accumulated;
  if (textTrim.length > accTrim.length && textTrim.includes(accTrim)) return finalText;
  if (textTrim.length >= Math.max(1, accTrim.length * 0.95)) return finalText;
  return accumulated;
}

function emptyReducer(_snapshot: ReducerSnapshot): ReducerResult {
  return emptyFeatureReducerResult();
}

/** 上一轮工具均已结束，可开始新的模型轮次（与后端每轮 Message.assistant 对齐）。 */
function allToolsSettled(toolMap: Map<string, ToolCallInfo>): boolean {
  if (toolMap.size === 0) return false;
  return [...toolMap.values()].every((t) => t.status === 'done' || t.status === 'error');
}

/**
 * 工具批次结束后切段：封存当前 assistant 为 process，新 delta 开启 answer 段。
 * A-1：无论封存段是否有 priorContent（含「先工具、后文字」路径）均切段。
 */
function splitAssistantAfterSettledTools(
  book: Bookkeeping,
  snapshot: ReducerSnapshot,
  text: string,
  upserts: MessageUpsert[],
): void {
  const currentId = book.assistantId!;
  const current = snapshot.messages.find((m) => m.id === currentId);
  const now = snapshot.now;
  const startedAt = current?.turnStartedAt;
  upserts.push({
    op: 'patch',
    messageId: currentId,
    patch: {
      streaming: false,
      segmentRole: 'process',
      turnDurationMs: startedAt != null ? Math.max(0, now - startedAt) : 0,
    },
  });
  const nextId = uniqueMessageId(snapshot, 'm');
  book.assistantId = nextId;
  book.toolMap = new Map();
  book.firstChunkAt = now;
  upserts.push({
    op: 'append',
    message: {
      id: nextId,
      role: 'assistant',
      content: text,
      timestamp: now,
      streaming: true,
      segmentRole: 'answer',
      turnStartedAt: startedAt ?? now,
    },
  });
}

export function deltaReducer(chunk: DeltaChunk, snapshot: ReducerSnapshot): ReducerResult {
  const book = {
    ...snapshot.book,
    deltaSpans: [...(snapshot.book.deltaSpans ?? [])],
    legacyDeltaText: snapshot.book.legacyDeltaText ?? '',
  };
  if (book.assistantId && book.deltaSpans.length === 0 && !book.legacyDeltaText) {
    book.legacyDeltaText = snapshot.messages.find((m) => m.id === book.assistantId)?.content ?? '';
  }
  const text = applyOrderedDelta(book, chunk);
  const upserts: MessageUpsert[] = [];

  // 首片 delta 来时若 book 已 finalize（assistantId=null），重置 firstChunkAt。
  if (book.assistantId == null) {
    book.firstChunkAt = null;
  }
  // 不在此清空 pendingPlan：批准后执行的首包 delta 仍需看板展示已批准方案正文。
  // 仅在退出 Plan（status）/ 新 plan_review 替换 / 产品明确的新回合策略处清理。

  if (!book.assistantId) {
    const now = snapshot.now;
    book.assistantId = uniqueMessageId(snapshot, 'm');
    book.firstChunkAt = now;
    // 工具前不确定阶段标 process：字实时进思考区，但不触发「正式正文」自动折。
    // 硬事件后再升 answer：① 工具批次结束后的新 delta（下方 split）；② final 且无工具。
    upserts.push({
      op: 'append',
      message: {
        id: book.assistantId,
        role: 'assistant',
        content: text,
        timestamp: now,
        streaming: true,
        segmentRole: 'process',
        turnStartedAt: now,
      },
    });
  } else if (allToolsSettled(book.toolMap)) {
    // 工具结束后开新 answer 段：新段只含本片 delta 的原始文本，不复用前段累积正文
    //（applyOrderedDelta 的 legacyDeltaText 承载会把前段「我先查一下。」带进 text，污染新段）。
    const rawText = textOf(chunk.body);
    splitAssistantAfterSettledTools(book, snapshot, rawText, upserts);
    // 新段独立累积 delta：丢弃前段的 span / legacy 缓存，后续 delta 从该段自身正文重新累积。
    book.deltaSpans = [];
    book.legacyDeltaText = '';
  } else {
    if (book.firstChunkAt == null) book.firstChunkAt = snapshot.now;
    const existing = snapshot.messages.find((m) => m.id === book.assistantId);
    // 乐观占位若缺 segmentRole，首包补 process，避免旁白被当成正式正文提前折过程区。
    const patch: { content: string; streaming: true; segmentRole?: 'process' } = {
      content: text,
      streaming: true,
    };
    if (!existing?.segmentRole) patch.segmentRole = 'process';
    upserts.push({
      op: 'patch',
      messageId: book.assistantId,
      patch,
    });
  }

  return {
    messageUpserts: upserts,
    toolUpserts: [],
    replaceBook: book,
    statusHint: 'running',
    queueHint: '',
    finalize: false,
  };
}

export function thinkingReducer(chunk: ThinkingChunk, snapshot: ReducerSnapshot): ReducerResult {
  const book = { ...snapshot.book };
  const text = textOf(chunk.body);
  const upserts: MessageUpsert[] = [];
  if (book.firstChunkAt == null && book.assistantId) book.firstChunkAt = snapshot.now;
  if (book.assistantId) {
    const existing = snapshot.messages.find((m) => m.id === book.assistantId);
    upserts.push({
      op: 'patch',
      messageId: book.assistantId,
      patch: { thinking: mergeStreamingText(existing?.thinking, text) },
    });
  }
  return {
    messageUpserts: upserts,
    toolUpserts: [],
    replaceBook: book,
    statusHint: 'running',
    queueHint: '',
    finalize: false,
  };
}

export function toolReducer(chunk: ToolChunk, snapshot: ReducerSnapshot): ReducerResult {
  const book = { ...snapshot.book };
  const upserts: MessageUpsert[] = [];
  const toolCallId = (typeof chunk.body.tool_call_id === 'string' && chunk.body.tool_call_id) || `tool-${snapshot.now}-${snapshot.sequence}`;
  const existing = book.toolMap.get(toolCallId);
  const toolUpsert: ToolUpsert = {
    toolCallId,
    name: typeof chunk.body.name === 'string' ? chunk.body.name : existing?.name || 'unknown',
    status: chunk.body.phase === 'generating' ? 'generating' : 'running',
    startedAt: existing?.startedAt ?? snapshot.now,
  };
  if (typeof chunk.body.ui_label === 'string') toolUpsert.uiLabel = chunk.body.ui_label;
  else if (existing?.uiLabel) toolUpsert.uiLabel = existing.uiLabel;
  if (typeof chunk.body.args === 'string') toolUpsert.args = chunk.body.args;
  else if (existing?.args) toolUpsert.args = existing.args;

  const phase = chunk.body.phase;
  if (phase === 'progress') {
    // 进度帧：保持运行态，进度作为独立过程行保留；不能落入完成分支。
    if (existing) {
      toolUpsert.status = existing.status === 'generating' ? 'generating' : 'running';
      toolUpsert.startedAt = existing.startedAt;
      if (existing.duration != null) toolUpsert.duration = existing.duration;
    }
    const text = typeof chunk.body.text === 'string' ? chunk.body.text.trim() : '';
    const history = existing?.progressHistory ? [...existing.progressHistory] : [];
    if (text && history.at(-1) !== text) history.push(text);
    if (text) toolUpsert.progressText = text;
    else if (existing?.progressText) toolUpsert.progressText = existing.progressText;
    toolUpsert.progressHistory = history;
  } else if (phase === 'generating' || phase === 'start') {
    // 新建
  } else {
    if (existing) {
      toolUpsert.status = phase === 'error' ? 'error' : 'done';
      toolUpsert.startedAt = existing.startedAt;
      toolUpsert.duration = snapshot.now - existing.startedAt;
      if (typeof chunk.body.detail === 'string') toolUpsert.result = chunk.body.detail;
      else if (existing.result) toolUpsert.result = existing.result;
    } else {
      toolUpsert.status = phase === 'error' ? 'error' : 'done';
      toolUpsert.duration = 0;
      if (typeof chunk.body.detail === 'string') toolUpsert.result = chunk.body.detail;
    }
  }

  // 确保 assistantId 存在
  if (!book.assistantId) {
    const now = snapshot.now;
    book.assistantId = uniqueMessageId(snapshot, 'm');
    book.firstChunkAt = now;
    upserts.push({
      op: 'append',
      message: {
        id: book.assistantId,
        role: 'assistant',
        content: '',
        timestamp: now,
        streaming: true,
        turnStartedAt: now,
      },
    });
  }

  // tool_calls upsert：apply 端负责把所有 toolMap.values() 写回 message.toolCalls。
  // 这里只标记 book 替换。
  const newToolMap = new Map(book.toolMap);
  newToolMap.set(toolCallId, {
    toolCallId,
    name: toolUpsert.name,
    uiLabel: toolUpsert.uiLabel,
    args: toolUpsert.args,
    result: toolUpsert.result,
    status: toolUpsert.status,
    startedAt: toolUpsert.startedAt,
    duration: toolUpsert.duration,
    progressText: toolUpsert.progressText,
    progressHistory: toolUpsert.progressHistory ?? existing?.progressHistory,
  });
  book.toolMap = newToolMap;

  // 把最新 tool_calls upsert 到 assistant 消息
  upserts.push({
    op: 'patch',
    messageId: book.assistantId,
    patch: { toolCalls: Array.from(newToolMap.values()), segmentRole: 'process' },
  });

  return {
    messageUpserts: upserts,
    toolUpserts: [toolUpsert],
    replaceBook: book,
    statusHint: 'running',
    queueHint: '',
    finalize: false,
  };
}

export function statusReducer(chunk: StatusChunk, snapshot: ReducerSnapshot): ReducerResult {
  const message = typeof chunk.body.message === 'string' ? chunk.body.message : '';
  const upserts: MessageUpsert[] = [];
  const control = chunk.body.control === true;
  if (message.includes('排队')) {
    return {
      messageUpserts: upserts,
      toolUpserts: [],
      replaceBook: null,
      statusHint: control ? undefined : 'queued',
      queueHint: control ? undefined : message,
      finalize: false,
    };
  }
  if (isPlanControlStatus(message)) {
    const book = { ...snapshot.book };
    if (message.startsWith('已进入 Plan 模式')) {
      book.planActive = true;
    } else if (message.startsWith('已退出 Plan 模式')) {
      book.planActive = false;
      book.pendingPlan = null;
    } else if (message.startsWith('已保留 Plan 模式')) {
      book.planActive = true;
      book.pendingPlan = book.pendingPlan ? { ...book.pendingPlan, status: 'editing' } : null;
    }
    return {
      messageUpserts: upserts,
      toolUpserts: [],
      replaceBook: book,
      statusHint: 'idle',
      queueHint: '',
      finalize: false,
    };
  }
  const content = typeof chunk.body.detail === 'string' && chunk.body.detail
    ? `${message}\n\n${chunk.body.detail}`
    : message;
  // 与 delta/tool/thinking reducer 同构：status 帧到达时若回合尚未开 anchor，开一条 streaming:true
  // 的 assistant anchor。这样「只来 status/workflow 帧、无 assistant 正文」的回合（动态看板）也拥有
  // per-turn 的 live 信号——renderAgentTurn 据 batch 内是否存在 streaming 消息判断「执行中」，不再
  // 借用 session 全局 busy。!turnSealed 守卫：status 帧不像 tool/delta 被 gate 在封口后丢弃
  // （resolveTurnGate 对无 request_id 的 status 恒 accept），故显式排除已封口回合的迟到 status，
  // 避免给它开出幽灵 streaming 回合；control 帧（如「已停止」）是控制信号，同样不开 anchor。
  let book: Bookkeeping | null = null;
  if (!snapshot.book.assistantId && !snapshot.book.turnSealed && !control) {
    const now = snapshot.now;
    const anchorId = uniqueMessageId(snapshot, 'm');
    book = {
      ...snapshot.book,
      assistantId: anchorId,
      firstChunkAt: now,
    };
    upserts.push({
      op: 'append',
      message: {
        id: anchorId,
        role: 'assistant',
        content: '',
        timestamp: now,
        streaming: true,
        turnStartedAt: now,
      },
    });
  }
  upserts.push({
    op: 'append',
    message: {
      id: uniqueMessageId(snapshot, 'status'),
      role: 'status',
      content,
      timestamp: snapshot.now,
      agentName: typeof chunk.body.agent_name === 'string' ? chunk.body.agent_name : undefined,
      agentAvatar: typeof chunk.body.agent_avatar === 'string' ? chunk.body.agent_avatar : undefined,
      // 瞬时活动提示：渲染层据此做
      // 「live 只留最新、完成即隐藏」的过滤，避免进度提示在回合结束后残留。
      activity: typeof chunk.body.activity === 'string' ? chunk.body.activity : undefined,
    },
  });
  return {
    messageUpserts: upserts,
    toolUpserts: [],
    replaceBook: book,
    statusHint: control ? undefined : 'running',
    queueHint: control ? undefined : '',
    finalize: false,
  };
}

export function finalReducer(chunk: FinalChunk, snapshot: ReducerSnapshot): ReducerResult {
  const book = { ...snapshot.book };
  const text = textOf(chunk.body);
  const upserts: MessageUpsert[] = [];
  let assistantId = book.assistantId;
  let turnDurationMs = 0;
  let firstTokenMs: number | undefined;
  if (book.hadTeamInternal) {
    // 与 Web hadTeamInternal 语义一致：Team Runtime 的最终答案已经通过
    // team_internal/team_summary 展示；根 final 只负责结束回合，不能再产生团队自身气泡。
    if (assistantId) {
      const startedAt = snapshot.messages.find((m) => m.id === assistantId)?.turnStartedAt;
      turnDurationMs = snapshot.now - (startedAt ?? snapshot.now);
      if (book.firstChunkAt != null && startedAt != null) firstTokenMs = book.firstChunkAt - startedAt;
      upserts.push({ op: 'remove', messageId: assistantId });
    }
    assistantId = null;
    book.assistantId = null;
  } else if (assistantId) {
    const startedAt = snapshot.messages.find((m) => m.id === assistantId)?.turnStartedAt;
    turnDurationMs = snapshot.now - (startedAt ?? snapshot.now);
    if (book.firstChunkAt != null && startedAt != null) firstTokenMs = book.firstChunkAt - startedAt;
    // 修法2（配合 stream-reassembly 的按序重组）：累积正文现已按 gateway_sequence 重组为序号权威，
    // 乱序不再需要这里兜底。覆盖只在「final.text 是累积正文的超集前缀」时发生——即 acc 是 text 的
    // 前缀（text.startsWith(acc)），此时 text ⊇ acc，覆盖只补全（单段回合丢尾帧的合法恢复），不丢
    // 任何已累积文字。其它情况一律保留累积正文：多步回合的 final 只含末段（builtin executor，
    // crew/agent/executor/builtin.py），中段丢帧时 acc 也不是 final 的前缀——这两种若拿 final 覆盖
    // 都会丢前言。真缺口（推送降级 / 回放缓冲淘汰）留给重连后的 history 回填自愈。
    const acc = snapshot.messages.find((m) => m.id === assistantId)?.content ?? '';
    const forceReplace = !!chunk.body.replace_content;
    const resolved = resolveFinalContent(acc, text);
    // 硬事件：本段仍挂工具 → process；无工具 → 升格 answer（纯文字回复确认）
    const segmentRole = book.toolMap.size > 0 ? 'process' : 'answer';
    if (text && (forceReplace || resolved !== acc)) {
      const finalContent = forceReplace ? text : resolved;
      upserts.push({
        op: 'patch',
        messageId: assistantId,
        patch: { content: finalContent, streaming: false, turnDurationMs, timestamp: snapshot.now, segmentRole },
      });
    } else {
      upserts.push({
        op: 'patch',
        messageId: assistantId,
        patch: { streaming: false, turnDurationMs, timestamp: snapshot.now, segmentRole },
      });
    }
  } else if (text) {
    assistantId = uniqueMessageId(snapshot, 'm');
    upserts.push({
      op: 'append',
      message: {
        id: assistantId,
        role: 'assistant',
        content: text,
        timestamp: snapshot.now,
        segmentRole: 'answer',
      },
    });
  }
  if (assistantId && book.toolMap.size > 0) {
    const settledToolMap = new Map(book.toolMap);
    let changed = false;
    for (const [id, tool] of settledToolMap) {
      if (tool.status === 'running' || tool.status === 'generating') {
        settledToolMap.set(id, {
          ...tool,
          status: 'done',
          duration: snapshot.now - tool.startedAt,
        });
        changed = true;
      }
    }
    if (changed) book.toolMap = settledToolMap;
    upserts.push({
      op: 'patch',
      messageId: assistantId,
      patch: { toolCalls: Array.from((changed ? settledToolMap : book.toolMap).values()), segmentRole: 'process' },
    });
  }

  // 「仅本轮」文件改动差集：与上一轮结束签名比对，patch 到本回合 assistant 消息，
  // 供 chat-render 在正文下方渲染「已编辑 N 个文件」卡。基准无条件前进到当前快照。
  const turnFiles = computeTurnFileDelta(book.fileChanges ?? [], book.prevTurnFileSignature);
  if (assistantId && turnFiles.length > 0) {
    upserts.push({ op: 'patch', messageId: assistantId, patch: { turnFileChanges: turnFiles } });
  }
  book.prevTurnFileSignature = snapshotFileSignature(book.fileChanges ?? []);

  return {
    messageUpserts: upserts,
    toolUpserts: [],
    replaceBook: book,
    statusHint: 'idle',
    queueHint: '',
    finalize: true,
    turn: { status: 200, turnDurationMs, firstTokenMs, assistantId, ...(chunk.body.usage ? { usage: chunk.body.usage } : {}) },
  };
}

export function errorReducer(chunk: ErrorChunk, snapshot: ReducerSnapshot): ReducerResult {
  const book = { ...snapshot.book };
  const message = typeof chunk.body.message === 'string' ? chunk.body.message : '未知错误';
  const upserts: MessageUpsert[] = [];
  let turnDurationMs = 0;
  let firstTokenMs: number | undefined;
  if (book.assistantId) {
    const startedAt = snapshot.messages.find((m) => m.id === book.assistantId)?.turnStartedAt;
    turnDurationMs = snapshot.now - (startedAt ?? snapshot.now);
    if (book.firstChunkAt != null && startedAt != null) firstTokenMs = book.firstChunkAt - startedAt;
    upserts.push({
      op: 'patch',
      messageId: book.assistantId,
      patch: { streaming: false, turnDurationMs, timestamp: snapshot.now },
    });
  }
  upserts.push({
    op: 'append',
    message: {
      id: uniqueMessageId(snapshot, 'error'),
      role: 'error',
      content: message,
      timestamp: snapshot.now,
    },
  });

  return {
    messageUpserts: upserts,
    toolUpserts: [],
    replaceBook: book,
    statusHint: 'error',
    queueHint: '',
    finalize: true,
    turn: { status: 500, turnDurationMs, firstTokenMs, assistantId: book.assistantId },
  };
}

export function planReviewReducer(chunk: PlanReviewChunk, snapshot: ReducerSnapshot): ReducerResult {
  const book = { ...snapshot.book };
  const empty = chunk.body.empty === true;
  const rawStatus = typeof chunk.body.status === 'string' ? chunk.body.status : '';
  const status: PlanReviewStatus = empty
    ? 'empty'
    : (['pending', 'editing', 'readonly', 'approved', 'revising', 'rejected', 'cancelled'].includes(rawStatus)
      ? rawStatus as PlanReviewStatus
      : 'pending');
  const planReview = {
    plan: typeof chunk.body.plan === 'string' ? chunk.body.plan : '',
    planFile: typeof chunk.body.plan_file === 'string' ? chunk.body.plan_file : '',
    status,
    sessionId: snapshot.sessionId,
    phase: typeof chunk.body.phase === 'string' ? chunk.body.phase : undefined,
  } satisfies NonNullable<ChatMessage['planReview']>;
  book.pendingPlan = {
    plan: planReview.plan,
    planFile: planReview.planFile,
    status: planReview.status,
  };
  book.planActive = true;
  const upserts: MessageUpsert[] = [];
  if (book.assistantId) {
    upserts.push({ op: 'patch', messageId: book.assistantId, patch: { planReview } });
  } else {
    book.assistantId = uniqueMessageId(snapshot, 'plan');
    upserts.push({
      op: 'append',
      message: {
        id: book.assistantId,
        role: 'assistant',
        content: '',
        timestamp: snapshot.now,
        planReview,
      },
    });
  }
  return {
    messageUpserts: upserts,
    toolUpserts: [],
    replaceBook: book,
    statusHint: 'idle',
    queueHint: undefined,
    finalize: false,
  };
}

export function followupQuestionReducer(chunk: FollowupQuestionChunk, snapshot: ReducerSnapshot): ReducerResult {
  const book = { ...snapshot.book };
  const status = typeof chunk.body.status === 'string' ? chunk.body.status : '';
  const current = book.pendingFollowup;
  const targetsCurrent = !chunk.body.question_id || current?.questionId === chunk.body.question_id;
  const isRuntimeStaffing = current?.origin?.mentionIntent === 'runtime_staffing';
  if (
    current
    && targetsCurrent
    && isRuntimeStaffing
    && ['resolved', 'applying', 'applied', 'declined', 'failed'].includes(status)
  ) {
    book.pendingFollowup = {
      ...current,
      status: status === 'resolved' ? 'applying' : status,
      ...(typeof chunk.body.note === 'string' && chunk.body.note.trim()
        ? { note: chunk.body.note.trim() }
        : {}),
    };
    return {
      messageUpserts: [],
      toolUpserts: [],
      replaceBook: book,
      statusHint: undefined,
      queueHint: undefined,
      finalize: false,
    };
  }
  if (['expired', 'cancelled', 'resolved'].includes(status)) {
    if (!chunk.body.question_id || book.pendingFollowup?.questionId === chunk.body.question_id) {
      book.pendingFollowup = null;
    }
    return {
      messageUpserts: [],
      toolUpserts: [],
      replaceBook: book,
      statusHint: undefined,
      queueHint: undefined,
      finalize: false,
    };
  }
  const origin = chunk.body.origin && typeof chunk.body.origin === 'object' ? {
    ...(typeof chunk.body.origin.type === 'string' ? { type: chunk.body.origin.type } : {}),
    ...(typeof chunk.body.origin.agent_name === 'string' ? { agentName: chunk.body.origin.agent_name } : {}),
    ...(typeof chunk.body.origin.origin_session_id === 'string'
      ? { originSessionId: chunk.body.origin.origin_session_id }
      : {}),
    ...(typeof chunk.body.origin.mention_intent === 'string'
      ? { mentionIntent: chunk.body.origin.mention_intent }
      : {}),
  } : undefined;
  book.pendingFollowup = {
    questionId: typeof chunk.body.question_id === 'string' ? chunk.body.question_id : '',
    title: typeof chunk.body.title === 'string' ? chunk.body.title : '',
    recordHistory: chunk.body.record_history !== false,
    ...(status ? { status } : {}),
    ...(typeof chunk.body.note === 'string' && chunk.body.note.trim()
      ? { note: chunk.body.note.trim() }
      : {}),
    ...(origin ? { origin } : {}),
    questions: (chunk.body.questions ?? []).map((q) => ({
      id: typeof q.id === 'string' ? q.id : '',
      question: typeof q.question === 'string' ? q.question : '',
      options: Array.isArray(q.options) ? q.options.map((opt) => {
        if (opt && typeof opt === 'object') {
          const raw = opt as Record<string, unknown>;
          const label = String(raw.label ?? raw.text ?? raw.name ?? raw.value ?? '');
          const value = String(raw.value ?? label);
          const description = typeof raw.description === 'string' ? raw.description.trim() : '';
          return description ? { label, value, description } : { label, value };
        }
        const text = String(opt ?? '');
        return { label: text, value: text };
      }) : [],
      allowFreeText: q.allowFreeText !== false && q.allow_free_text !== false,
      multiSelect: !!q.multiSelect,
    })),
  };
  return {
    messageUpserts: [],
    toolUpserts: [],
    replaceBook: book,
    statusHint: undefined,
    queueHint: undefined,
    finalize: false,
  };
}

export function todoUpdatedReducer(chunk: TodoUpdatedChunk, snapshot: ReducerSnapshot): ReducerResult {
  const book = { ...snapshot.book };
  const raw = chunk.body.todos;
  const todos = Array.isArray(raw)
    ? raw.map((t): TodoItem => ({
        id: typeof t.id === 'string' ? t.id : '?',
        content: typeof t.content === 'string' ? t.content : '',
        status:
          t.status === 'in_progress' || t.status === 'completed' || t.status === 'cancelled'
            ? t.status
            : 'pending',
      }))
    : [];
  book.todos = todos;
  const upserts: MessageUpsert[] = [];
  if (todos.length > 0) {
    if (book.assistantId) {
      upserts.push({ op: 'patch', messageId: book.assistantId, patch: { todoSnapshot: todos } });
    } else {
      book.assistantId = uniqueMessageId(snapshot, 'todo');
      upserts.push({
        op: 'append',
        message: {
          id: book.assistantId,
          role: 'assistant',
          content: '',
          timestamp: snapshot.now,
          todoSnapshot: todos,
        },
      });
    }
  }
  return {
    messageUpserts: upserts,
    toolUpserts: [],
    replaceBook: book,
    statusHint: undefined,
    queueHint: undefined,
    finalize: false,
  };
}

/** 单个 FileChange 的稳定签名：状态 + 增删行数 + diff 内容轻量哈希（djb2）。
 *  用于判断「相比上一轮结束，该文件本轮是否真的改动」。 */
function fileChangeSignature(f: FileChange): string {
  let h = 5381;
  for (const r of f.diff) {
    h = (Math.imul(h, 33) ^ r.kind.charCodeAt(0) ^ r.text.length) | 0;
  }
  return `${f.status}|${f.added}|${f.removed}|${f.binary ? 1 : 0}|${f.revision || ''}|${h >>> 0}`;
}

/** 整份 fileChanges 快照 → path→签名 映射，存入 book.prevTurnFileSignature 供下一轮差集。 */
function snapshotFileSignature(files: FileChange[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const f of files) out[f.path] = fileChangeSignature(f);
  return out;
}

/** 推算「仅本轮」文件改动：当前 fileChanges 中签名与上一轮结束快照不同的文件。
 *  无论后端 file_changes 帧是累计快照还是仅本轮，结果都只含本轮真正变动的文件。 */
function computeTurnFileDelta(
  current: FileChange[],
  prev: Record<string, string> | null,
): TurnFileChangeSummary[] {
  const baseline = prev ?? {};
  return current
    .filter((f) => (baseline[f.path] ?? '') !== fileChangeSignature(f))
    .filter((f) => !isPlanDocumentPath(f.path))
    // 兜底：无意义的空 added（对账帧缺失时避免幽灵条目）
    // 二进制结果文件没有文本行数，不能因为 +/- 为 0 而过滤。
    .filter((f) => f.binary || !(f.status === 'added' && f.added === 0 && f.removed === 0))
    .map((f): TurnFileChangeSummary => ({
      path: f.path,
      name: f.name,
      added: f.added,
      removed: f.removed,
      status: f.status,
      ...(f.binary ? { binary: true } : {}),
    }));
}

export function fileChangesReducer(chunk: FileChangesChunk, snapshot: ReducerSnapshot): ReducerResult {
  const book = { ...snapshot.book };
  const raw = chunk.body.files;
  book.fileChanges = Array.isArray(raw) ? (raw as FileChange[]) : [];
  return {
    messageUpserts: [],
    toolUpserts: [],
    replaceBook: book,
    statusHint: undefined,
    queueHint: undefined,
    finalize: false,
  };
}

export function unknownReducer(_chunk: AnyChatChunk, _snapshot: ReducerSnapshot): ReducerResult {
  return emptyReducer(_snapshot);
}

function featureReducerContextFromSnapshot(snapshot: ReducerSnapshot): FeatureReducerContext {
  return {
    sessionId: snapshot.sessionId,
    messages: snapshot.messages,
    book: snapshot.book,
    now: snapshot.now,
    sequence: snapshot.sequence,
  };
}

/**
 * 把 feature_event 帧按 (feature, event, version) 转调 Feature Event Reducer Registry。
 * 未命中（包括模块未加载导致的未注册）返回 null，让 reduceChunk 回退到 unknownReducer，
 * 由 registry console.warn 提供可诊断信息，不抛错。
 */
function dispatchFeatureEventFromChunk(
  chunk: AnyChatChunk,
  snapshot: ReducerSnapshot,
): ReducerResult | null {
  if (chunk.kind !== 'feature_event') return null;
  const featureEvent = featureEventFrameOf(chunk);
  if (!featureEvent) return null;
  return featureEventRegistry.dispatch(
    featureEvent.feature,
    featureEvent.event,
    featureEvent.version,
    featureEvent.payload,
    featureReducerContextFromSnapshot(snapshot),
  );
}

/** 顶层 dispatch：根据 chunk.kind 选 reducer；未知 kind 返回空 patch（不抛错）。 */
export function reduceChunk(chunk: AnyChatChunk, snapshot: ReducerSnapshot): ReducerResult {
  const featureResult = dispatchFeatureEventFromChunk(chunk, snapshot);
  if (featureResult) return featureResult;

  switch (chunk.kind) {
    case 'delta': return deltaReducer(chunk, snapshot);
    case 'thinking': return thinkingReducer(chunk, snapshot);
    case 'tool': return toolReducer(chunk, snapshot);
    case 'status': return statusReducer(chunk, snapshot);
    case 'final': return finalReducer(chunk, snapshot);
    case 'error': return errorReducer(chunk, snapshot);
    case 'plan_review': return planReviewReducer(chunk, snapshot);
    case 'followup_question': return followupQuestionReducer(chunk, snapshot);
    case 'todo_updated': return todoUpdatedReducer(chunk, snapshot);
    case 'file_changes': return fileChangesReducer(chunk, snapshot);
    case 'task': return emptyReducer(snapshot); // 桌面端走 REST /api/tasks 驱动 kanban，WS task 帧仅保留入口
    default: return unknownReducer(chunk, snapshot);
  }
}
